import fs from 'node:fs';
import path from 'node:path';
import { createHash, randomUUID } from 'node:crypto';
import { createRequire } from 'node:module';
import { AsyncLocalStorage } from 'node:async_hooks';

const MARK = Symbol.for('harniqo.openclaw.qualification.fetch-audit.v1');
const MAX_LOG = 256 * 1024;
const MAX_BODY = 512 * 1024;
const controllers = new Map();
const hash = value => createHash('sha256').update(value).digest('hex');
const fail = code => { throw new Error(`OpenClaw qualification audit: ${code}`); };
const same = (a, b) => a.dev === b.dev && a.ino === b.ino;

function privateDirectory(filename, uid) {
  const directory = path.dirname(filename);
  let current = path.parse(directory).root;
  for (const part of directory.slice(current.length).split('/').filter(Boolean)) {
    current = path.join(current, part);
    const stat = fs.lstatSync(current);
    if (!stat.isDirectory() || stat.isSymbolicLink() || ![0, uid].includes(stat.uid)
        || (stat.mode & 0o022)) fail('unsafe_directory');
  }
  const stat = fs.lstatSync(directory);
  if (stat.uid !== uid || (stat.mode & 0o077)) fail('private_directory_required');
  return stat;
}

function validFile(stat, uid) {
  if (!stat.isFile() || stat.uid !== uid || stat.nlink !== 1 || (stat.mode & 0o077)
      || stat.size > MAX_LOG) fail('unsafe_or_oversized_log');
}

function inputUrl(input) {
  if (typeof input === 'string') return new URL(input);
  if (input instanceof URL) return new URL(input.href);
  if (typeof Request !== 'undefined' && input instanceof Request) return new URL(input.url);
  return null;
}

function modelObservation(input, init, expectedHash) {
  const body = init?.body;
  let text;
  if (typeof body === 'string') {
    if (body.length > MAX_BODY || Buffer.byteLength(body) > MAX_BODY) return { model_known: false, model_unknown_reason: 'body_bound' };
    text = body;
  } else if (body instanceof Uint8Array) {
    if (body.byteLength > MAX_BODY) return { model_known: false, model_unknown_reason: 'body_bound' };
    text = Buffer.from(body.buffer, body.byteOffset, body.byteLength).toString('utf8');
  } else {
    // A Request's stream belongs to fetch. Do not consume, tee or replace it.
    return { model_known: false, model_unknown_reason: 'uninspected_body_type' };
  }
  try {
    const value = JSON.parse(text)?.model;
    if (typeof value !== 'string' || value.length < 1 || value.length > 1024) {
      return { model_known: false, model_unknown_reason: 'missing_model' };
    }
    const valueHash = hash(value);
    return { model_known: true, model_sha256: valueHash, model_matches_expected: valueHash === expectedHash };
  } catch {
    return { model_known: false, model_unknown_reason: 'invalid_json' };
  }
}

/**
 * Install once on a fetch-owning object. Production preload uses globalThis;
 * tests pass a separate target. No headers, credentials or payloads are logged.
 * This is a bounded qualification observer, not a tenant or billing boundary.
 */
export function install({ target = globalThis, auditFile = process.env.HARNIQO_AUDIT_FILE,
  expectedModel = process.env.HARNIQO_EXPECTED_MODEL, apiBase = process.env.HARNIQO_MODEL_API_BASE,
  maxRequests = Number(process.env.HARNIQO_MAX_MODEL_REQUESTS ?? 6), transport = 'global' } = {}) {
  if (typeof process.getuid !== 'function' || typeof target.fetch !== 'function') fail('unsupported_runtime');
  if (typeof auditFile !== 'string' || !path.isAbsolute(auditFile) || path.normalize(auditFile) !== auditFile
      || typeof expectedModel !== 'string' || !expectedModel || expectedModel.length > 1024
      || typeof apiBase !== 'string' || !Number.isSafeInteger(maxRequests) || maxRequests < 1 || maxRequests > 6
      || !['global', 'undici'].includes(transport)) {
    fail('invalid_configuration');
  }
  let base;
  try { base = new URL(apiBase); } catch { fail('invalid_base'); }
  if (base.protocol !== 'https:' || base.username || base.password || base.search || base.hash) fail('invalid_base');
  const signature = hash(JSON.stringify([auditFile, expectedModel, base.href, maxRequests]));
  if (target[MARK]) {
    if (target[MARK].signature !== signature || target[MARK].transport !== transport
        || target.fetch !== target[MARK].wrapper) fail('incompatible_install');
    return target[MARK].interface;
  }
  const uid = process.getuid();
  const directoryIdentity = privateDirectory(auditFile, uid);
  const originalFetch = target.fetch;
  const expectedHash = hash(expectedModel);
  const basePath = base.pathname.replace(/\/$/, '');
  const lockPath = auditFile + '.lock';
  const controller = controllers.get(signature) ?? { identity: undefined, poisoned: false, context: new AsyncLocalStorage() };
  controllers.set(signature, controller);

  function locked(callback) {
    if (!same(privateDirectory(auditFile, uid), directoryIdentity)) fail('directory_changed');
    // O_EXCL serializes across processes; a stale lock is a visible failure.
    // It is never silently removed, because its writer might still be active.
    const lock = fs.openSync(lockPath, fs.constants.O_WRONLY | fs.constants.O_CREAT
      | fs.constants.O_EXCL | fs.constants.O_NOFOLLOW, 0o600);
    const lockIdentity = fs.fstatSync(lock);
    try {
      const fd = fs.openSync(auditFile, fs.constants.O_RDWR | fs.constants.O_APPEND
        | fs.constants.O_CREAT | fs.constants.O_NOFOLLOW, 0o600);
      try {
        const stat = fs.fstatSync(fd);validFile(stat, uid);
        if (controller.identity && !same(controller.identity, stat)) fail('log_replaced');
        controller.identity = stat;
        if (!same(fs.lstatSync(auditFile), stat)
            || !same(privateDirectory(auditFile, uid), directoryIdentity)) fail('path_changed');
        const buffer = Buffer.alloc(stat.size);
        let position = 0;
        while (position < buffer.length) {
          const count = fs.readSync(fd, buffer, position, buffer.length - position, position);
          if (!count) fail('log_truncated');
          position += count;
        }
        const text = buffer.toString('utf8');
        if (text && !text.endsWith('\n')) fail('incomplete_log_record');
        let records;
        try { records = text.split('\n').filter(Boolean).map(line => JSON.parse(line)); }
        catch { fail('invalid_log_record'); }
        if (records.some(record => record.schema !== 1 || !['request_started', 'response', 'network_error', 'request_blocked'].includes(record.kind))) {
          fail('unknown_log_record');
        }
        const append = record => {
          const bytes = Buffer.from(JSON.stringify({ schema: 1, ...record }) + '\n');
          const current = fs.fstatSync(fd);validFile(current, uid);
          if (!same(fs.lstatSync(auditFile), current) || current.size !== stat.size
              || current.size + bytes.length > MAX_LOG) fail('log_bound_or_concurrent_change');
          if (fs.writeSync(fd, bytes) !== bytes.length) fail('incomplete_log_write');
          fs.fsyncSync(fd);
        };
        return callback(records, append);
      } finally { fs.closeSync(fd); }
    } finally {
      fs.closeSync(lock);
      if (!same(fs.lstatSync(lockPath), lockIdentity)) fail('lock_replaced');
      fs.unlinkSync(lockPath);
    }
  }

  // Validate the path before replacing fetch; installation sends no request.
  locked(() => undefined);
  async function wrapper(...args) {
    let url;
    try { url = inputUrl(args[0]); } catch { return Reflect.apply(originalFetch, this, args); }
    const method = args[1]?.method ?? (typeof Request !== 'undefined' && args[0] instanceof Request ? args[0].method : 'GET');
    if (!url || url.origin !== base.origin || (url.pathname !== basePath && !url.pathname.startsWith(basePath + '/'))
        || typeof method !== 'string' || method.toUpperCase() !== 'POST') {
      return Reflect.apply(originalFetch, this, args);
    }
    if (controller.poisoned) fail('previous_audit_failure');
    const body = args[1]?.body;
    const bodyKey = typeof body === 'string' && body.length <= MAX_BODY && Buffer.byteLength(body) <= MAX_BODY ? hash(body)
      : body instanceof Uint8Array && body.byteLength <= MAX_BODY ? hash(body) : body;
    const key = hash(url.href + '\nPOST');
    const parent = controller.context.getStore();
    if (parent?.active && !parent.transports.has(transport) && parent.key === key && parent.bodyKey === bodyKey
        && (body !== undefined || parent.input === args[0])) {
      parent.transports.add(transport);
      // Only the first delegation shares a reservation. A subsequent native
      // fetch is a separate attempt even if an outer SDK call catches/retries.
      try {
        const response = await Reflect.apply(originalFetch, this, args);
        parent.complete('response', response);
        return response;
      } catch (error) {
        parent.complete('network_error');
        throw error;
      }
    }
    const id = randomUUID();
    const metadata = { request_id: id, transport, method: 'POST', url_sha256: hash(url.href),
      expected_model_sha256: expectedHash, ...modelObservation(args[0], args[1], expectedHash) };
    const allowed = locked((records, append) => {
      const sent = records.filter(record => record.kind === 'request_started').length;
      if (sent >= maxRequests) {
        append({ kind: 'request_blocked', ...metadata, reason: 'qualification_request_limit', max_requests: maxRequests });
        return false;
      }
      append({ kind: 'request_started', ...metadata });
      return true;
    });
    if (!allowed) fail('qualification_request_limit');
    const record = value => {
      try { locked((records, append) => append(value)); }
      catch { controller.poisoned = true; }
    };
    const context = { active: true, completed: false, key, bodyKey, input: args[0], transports: new Set([transport]) };
    context.complete = (kind, response) => {
      if (context.completed) return;
      context.completed = true;
      record({ kind, request_id: id, observed_transports: [...context.transports],
        status: response && Number.isInteger(response.status) ? response.status : null,
        response_url_sha256: response && typeof response.url === 'string' && response.url ? hash(response.url) : null,
        redirected: response ? response.redirected === true : null, network_error: kind === 'network_error' });
    };
    const receiver = this;
    return controller.context.run(context, async () => {
      try {
        const response = await Reflect.apply(originalFetch, receiver, args);
        context.complete('response', response);
        return response;
      } catch (error) {
        context.complete('network_error');
        throw error;
      } finally { context.active = false; }
    });
  }
  const publicInterface = Object.freeze({ installed: true, maxRequests, scope: 'configured_base_post_requests',
    get healthy() { return !controller.poisoned; } });
  Object.defineProperty(target, MARK, { value: { signature, transport, wrapper, interface: publicInterface } });
  target.fetch = wrapper;
  return publicInterface;
}

/**
 * Match the pinned OpenClaw loader's CommonJS subpath. Preload executes before
 * application imports, so its lazy createRequire(...)("undici/index.js") sees
 * this same export when both resolve to the same installed package. Never set
 * a mock flag, replace a dispatcher, or inject OpenClaw's test dependency key.
 */
export function installRuntime({ requireFrom = '/app/package.json', globalTarget = globalThis, ...options } = {}) {
  if (!path.isAbsolute(requireFrom)) fail('absolute_runtime_module_anchor_required');
  const requireRuntime = createRequire(requireFrom);
  const resolved = requireRuntime.resolve('undici/index.js');
  const undici = requireRuntime('undici/index.js');
  if (!undici || typeof undici.fetch !== 'function') fail('undici_fetch_required');
  const globalState = install({ ...options, target: globalTarget, transport: 'global' });
  const undiciState = install({ ...options, target: undici, transport: 'undici' });
  return Object.freeze({ global: globalState, undici: undiciState, modulePathSha256: hash(resolved) });
}

if ([process.env.HARNIQO_AUDIT_FILE, process.env.HARNIQO_EXPECTED_MODEL, process.env.HARNIQO_MODEL_API_BASE].some(value => value !== undefined)) {
  installRuntime();
}
