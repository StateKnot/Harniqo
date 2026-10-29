import fs from 'node:fs';
import { createHash, generateKeyPairSync, createPublicKey, sign, randomUUID, randomBytes } from 'node:crypto';
import { fileURLToPath } from 'node:url';
import { privateFile } from '../../deploy/qualification/openclaw/entrypoint.mjs';

const hash = (value) => createHash('sha256').update(value).digest('hex');
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
export const canonical = (value) => JSON.stringify(value, (_, item) => item && typeof item === 'object' && !Array.isArray(item)
  ? Object.fromEntries(Object.keys(item).sort().map((key) => [key, item[key]])) : item);
function requireValue(condition, code) { if (!condition) throw new Error(code); }
function save(name, value) {
  const path = '/data/evidence/' + name;
  fs.writeFileSync(path, JSON.stringify(value, null, 2) + '\n', { mode: 0o600, flag: 'wx' });
}

export function deviceProof(identity, nonce, signedAt, token, scopes) {
  requireValue(typeof nonce === 'string' && nonce.length > 0 && Number.isSafeInteger(signedAt) && signedAt >= 0,
    'invalid_native_challenge');
  const raw = createPublicKey(identity.privateKey).export({ type: 'spki', format: 'der' }).subarray(-32);
  const id = hash(raw);
  // v3 field order is the fixed upstream wire contract, including metadata.
  const payload = ['v3', id, 'cli', 'cli', 'operator', scopes.join(','), String(signedAt), token ?? '', nonce, 'linux', ''].join('|');
  return { id, publicKey: raw.toString('base64url'), signedAt, nonce,
    signature: sign(null, Buffer.from(payload), identity.privateKey).toString('base64url') };
}

export class Gateway {
  constructor(identity) { this.identity = identity; this.pending = new Map(); this.events = []; this.bytes = 0; }
  async connect(token) {
    this.socket = new WebSocket('ws://127.0.0.1:18789');
    this.socket.addEventListener('message', ({ data }) => {
      try {
        requireValue(typeof data === 'string' && Buffer.byteLength(data) <= 1024 * 1024, 'native_frame_bound');
        const frame = JSON.parse(data);
        if (frame.type === 'res') {
          const pending = this.pending.get(frame.id);
          if (pending) { clearTimeout(pending.timer); this.pending.delete(frame.id); pending.resolve(frame); }
        } else if (frame.type === 'event') {
          if (frame.event === 'connect.challenge') this.challenge = frame.payload;
          else {
            this.bytes += Buffer.byteLength(data);
            requireValue(this.bytes <= 4 * 1024 * 1024 && this.events.length < 10000, 'native_event_bound');
            this.events.push(frame);
          }
        }
      } catch (error) { this.failure = error; this.close(); }
    });
    this.socket.addEventListener('error', () => { this.failure = new Error('native_socket_error'); });
    this.socket.addEventListener('close', () => {
      this.closed = true;
      for (const pending of this.pending.values()) { clearTimeout(pending.timer); pending.reject(new Error('native_socket_closed')); }
      this.pending.clear();
    });
    await this.until(() => this.challenge, 10000);
    const scopes = ['operator.read', 'operator.write', 'operator.approvals'];
    const response = await this.request('connect', {
      minProtocol: 4, maxProtocol: 4,
      client: { id: 'cli', displayName: 'Harniqo qualification', version: '0.0.0-qualification', platform: 'linux', mode: 'cli' },
      role: 'operator', scopes, caps: ['tool-events', 'exec-approvals'],
      ...(token ? { auth: { token } } : {}),
      device: deviceProof(this.identity, this.challenge.nonce, this.challenge.ts, token, scopes),
    }, false);
    if (!response.ok) return response;
    const hello = response.payload;
    requireValue(hello.type === 'hello-ok' && hello.protocol === 4 && hello.server.version === '2026.10.8', 'native_hello_identity');
    requireValue(scopes.every((scope) => hello.auth.scopes.includes(scope)), 'native_scope_not_granted');
    this.hello = { protocol: hello.protocol, version: hello.server.version, scopes: hello.auth.scopes,
      methods: hello.features.methods };
    return response;
  }
  request(method, params, checked = true) {
    requireValue(this.socket.readyState === WebSocket.OPEN, 'native_socket_not_open');
    const id = randomUUID();
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => { this.pending.delete(id); reject(new Error('native_request_deadline')); }, 15000);
      this.pending.set(id, { timer, reject, resolve: (value) => {
        if (checked && !value.ok) reject(Object.assign(new Error('native_rpc_rejected'), { nativeError: value.error }));
        else resolve(checked ? value.payload : value);
      } });
      this.socket.send(JSON.stringify({ type: 'req', id, method, params }));
    });
  }
  async until(predicate, milliseconds) {
    const deadline = Date.now() + milliseconds;
    while (Date.now() < deadline) {
      if (this.failure) throw this.failure;
      const value = predicate(); if (value) return value;
      if (this.closed) throw new Error('native_socket_closed');
      await sleep(25);
    }
    throw new Error('native_event_deadline');
  }
  close() { if (this.socket && this.socket.readyState < WebSocket.CLOSING) this.socket.close(); }
}

export function toolsIn(messages) {
  const calls = [];
  const results = new Map();
  for (const message of messages) {
    if (message.role === 'assistant' && Array.isArray(message.content)) {
      for (const part of message.content) if (part.type === 'toolCall') calls.push(part);
    }
    if (message.role === 'toolResult') results.set(message.toolCallId, message);
  }
  return calls.map((call) => ({ ...call, result: results.get(call.id) }));
}

export function fixedFileTrace(messages) {
  const calls = toolsIn(messages);
  const input = new Set(['qualification-input.json', '/data/workspace/qualification-input.json']);
  const output = new Set(['qualification-result.json', '/data/workspace/qualification-result.json']);
  let stage = 0;
  for (const call of calls) {
    const path = call.arguments?.path;
    if (!call.result || call.result.isError || !['read', 'write'].includes(call.name)) return false;
    if (call.name === 'read' && !(input.has(path) || output.has(path))) return false;
    if (call.name === 'write' && !output.has(path)) return false;
    if (stage === 0 && call.name === 'read' && input.has(path)) stage = 1;
    else if (stage === 1 && call.name === 'write' && output.has(path)) stage = 2;
    else if (stage === 2 && call.name === 'read' && output.has(path)) stage = 3;
  }
  return calls.length >= 3 && calls.length <= 4 && stage === 3;
}

export function idle(history) {
  const state = history.sessionInfo;
  if (!state) return false;
  if ('hasActiveRun' in state && state.hasActiveRun !== false) return false;
  if ('activeRunIds' in state && (!Array.isArray(state.activeRunIds) || state.activeRunIds.length !== 0)) return false;
  return state.hasActiveRun === false || Array.isArray(state.activeRunIds);
}

export function transportEvidence(path, base, model) {
  if (!fs.existsSync(path)) return { verified: false, reason: 'no_observed_fetch' };
  const records = privateFile(path, 256 * 1024).trim().split('\n').filter(Boolean).map(JSON.parse);
  const starts = records.filter((value) => value.kind === 'request_started');
  const endpoint = hash(base.replace(/\/$/, '') + '/chat/completions');
  const expectedModel = hash(model);
  const completed = records.filter((value) => value.kind === 'response');
  // The observer's request identifier binds each actual send to its response.
  const ids = new Set(starts.map((value) => value.request_id));
  const valid = starts.length >= 1 && starts.length <= 6 && starts.length === completed.length
    && ids.size === starts.length && starts.every((value) => typeof value.request_id === 'string')
    && !records.some((value) => ['network_error', 'request_blocked'].includes(value.kind))
    && starts.every((value) => value.url_sha256 === endpoint && value.model_sha256 === expectedModel && value.model_known
      && completed.filter((end) => end.request_id === value.request_id).length === 1)
    && completed.every((value) => ids.has(value.request_id) && value.status === 200 && value.redirected === false
      && value.response_url_sha256 === endpoint);
  return { verified: valid, requests: starts.length, responses: completed.length, record_count: records.length,
    observed_transports: [...new Set(completed.flatMap((value) => value.observed_transports ?? []))] };
}

async function connectReady(identity, token, label) {
  const deadline = Date.now() + 60000;
  for (let attempt = 0; Date.now() < deadline; attempt++) {
    const client = new Gateway(identity);
    let response;
    try { response = await client.connect(token); }
    catch (error) { client.close(); throw error; }
    save(label + '-connect-' + attempt + '.json', response);
    // A listening socket precedes native sidecar readiness. Only this explicit
    // no-submission startup response may be retried, with a fresh challenge.
    if (response.ok === false && response.error?.code === 'UNAVAILABLE'
        && response.error?.details?.reason === 'startup-sidecars') {
      client.close();
      await sleep(250);
      continue;
    }
    return { client, response };
  }
  throw new Error('native_startup_sidecars_deadline');
}

export function liveText(events, runId) {
  const matching = events.filter((event) => event.event === 'chat' && event.payload?.runId === runId);
  return !matching.some((event) => ['final', 'error', 'aborted'].includes(event.payload.state))
    && matching.some((event) => event.payload.state === 'delta'
      && typeof event.payload.deltaText === 'string' && event.payload.deltaText.trim().length > 0);
}

async function cancellation(client, key, model, report) {
  const workspaceBefore = fs.readdirSync('/data/workspace').sort();
  const prompt = 'Without using any tools, write a detailed technical essay of at least 1800 words about sorting algorithms. '
    + 'Continue until the requested length; start the answer immediately.';
  const inputId = randomUUID();
  save('submission.json', { session_key: key, idempotency_key: inputId, prompt, model: model.model, state: 'about_to_submit' });
  report.model_task_attempted = true;
  const submitted = await client.request('chat.send', { sessionKey: key, message: prompt,
    idempotencyKey: inputId, timeoutMs: 90000, thinking: 'off', deliver: false });
  save('native-submission.json', submitted);
  requireValue(typeof submitted.runId === 'string', 'native_run_id_missing');
  report.native_run_id = submitted.runId;
  await client.until(() => liveText(client.events, submitted.runId), 60000);
  const before = await client.request('chat.history', { sessionKey: key, limit: 100, maxBytes: 1024 * 1024 });
  save('before-abort-history.json', before);
  requireValue(liveText(client.events, submitted.runId) && before.sessionInfo?.hasActiveRun === true,
    'run_not_active_at_cancellation');
  report.observed_live_text_before_abort = true;
  save('before-abort-events.json', client.events);
  const started = performance.now();
  const aborted = await client.request('chat.abort', { sessionKey: key, runId: submitted.runId });
  save('native-abort-response.json', aborted);
  requireValue(aborted.aborted === true && aborted.runIds?.includes(submitted.runId), 'native_abort_not_matched');
  const terminal = await client.until(() => client.events.find((event) => event.event === 'chat'
    && event.payload?.runId === submitted.runId && event.payload.state === 'aborted'), 10000);
  save('native-terminal.json', terminal);
  let history;
  for (let attempt = 0; attempt < 50; attempt++) {
    history = await client.request('chat.history', { sessionKey: key, limit: 100, maxBytes: 1024 * 1024 });
    if (idle(history)) break;
    await sleep(100);
  }
  save('native-history.json', history);
  report.cancel_confirmation_seconds = (performance.now() - started) / 1000;
  report.native_idle = idle(history);
  report.no_tool_side_effects = toolsIn(history.messages ?? []).length === 0
    && canonical(fs.readdirSync('/data/workspace').sort()) === canonical(workspaceBefore);
  report.terminal_state = terminal.payload.state;
  report.transport = transportEvidence('/data/evidence/transport.jsonl', model.base_url, model.model);
  requireValue(report.native_idle && report.no_tool_side_effects && report.transport.verified,
    'native_cancellation_evidence_missing');
  report.native_case_passed = true;
}

async function run(mode, report) {
  const model = JSON.parse(privateFile('/run/config/model.json'));
  const token = privateFile('/run/secrets/api-token').trim();
  const identityPath = '/data/evidence/client-identity.json';
  const privateKey = generateKeyPairSync('ed25519').privateKey.export({ type: 'pkcs8', format: 'pem' });
  fs.writeFileSync(identityPath, JSON.stringify({ privateKey }), { mode: 0o600, flag: 'wx' });
  const identity = { privateKey };
  for (const [label, value] of [['missing', undefined], ['wrong', 'invalid-qualification-token']]) {
    const { client: negative, response: result } = await connectReady(identity, value, label);
    try {
      save('auth-' + label + '-response.json', result);
      requireValue(result.ok === false && String(result.error?.details?.code ?? '').startsWith('AUTH_'), 'native_auth_not_denied');
      report['auth_' + label] = result.error.details.code;
    } finally { negative.close(); }
  }
  const { client, response: connected } = await connectReady(identity, token, 'valid');
  report.model_task_attempted = false;
  let key;
  try {
    if (!connected.ok) { report.connect_failure = connected.error; throw new Error('native_connect_rejected'); }
    report.hello = client.hello;
    if (mode === 'preflight') { report.native_preflight_passed = true; return; }
    key = 'agent:main:harniqo-' + randomUUID();
    const created = await client.request('sessions.create', { key, agentId: 'main', label: 'Harniqo ' + randomUUID(),
      displayName: 'Harniqo qualification', model: 'harniqo/' + model.model, thinkingLevel: 'off', cwd: '/data/workspace' });
    save('native-session.json', created);
    report.session_key = key;
    const before = await client.request('chat.history', { sessionKey: key, limit: 100, maxBytes: 1024 * 1024 });
    requireValue(Array.isArray(before.messages) && before.messages.length === 0, 'native_session_not_fresh');
    if (mode === 'cancellation') { await cancellation(client, key, model, report); return; }
    const challenge = { nonce: randomBytes(16).toString('hex'), challenge: randomBytes(32).toString('hex') };
    fs.writeFileSync('/data/workspace/qualification-input.json', JSON.stringify(challenge), { mode: 0o600, flag: 'wx' });
    requireValue(!fs.existsSync('/data/workspace/qualification-result.json'), 'output_not_fresh');
    const prompt = 'Use the native read tool to read qualification-input.json in the current workspace. '
      + 'Use the native write tool to create qualification-result.json with exactly the same JSON values. '
      + 'Then use the native read tool to read qualification-result.json and report completion. '
      + 'Use only these two files and these read/write tools. Do not perform other work.';
    const inputId = randomUUID();
    save('submission.json', { session_key: key, idempotency_key: inputId, prompt, model: model.model, state: 'about_to_submit' });
    report.model_task_attempted = true;
    const submitted = await client.request('chat.send', { sessionKey: key, message: prompt,
      idempotencyKey: inputId, timeoutMs: 90000, thinking: 'off', deliver: false });
    save('native-submission.json', submitted);
    requireValue(typeof submitted.runId === 'string', 'native_run_id_missing');
    report.native_run_id = submitted.runId;
    const terminal = await client.until(() => client.events.find((frame) => frame.event === 'chat'
      && frame.payload?.runId === submitted.runId && ['final', 'error', 'aborted'].includes(frame.payload.state)), 100000);
    save('native-terminal.json', terminal);
    report.terminal_state = terminal.payload.state;
    requireValue(terminal.payload.state === 'final', 'native_run_not_completed');
    let history;
    for (let attempt = 0; attempt < 40; attempt++) {
      history = await client.request('chat.history', { sessionKey: key, limit: 100, maxBytes: 1024 * 1024, toolResultMaxChars: 20000 });
      if (idle(history)) break;
      await sleep(100);
    }
    save('native-history.json', history);
    report.native_idle = idle(history);
    report.native_tool_trace = fixedFileTrace(history.messages ?? []);
    const output = privateFile('/data/workspace/qualification-result.json', 8192);
    report.random_artifact_matches = canonical(JSON.parse(output)) === canonical(challenge);
    report.artifact_sha256 = hash(output);
    report.history_sha256 = hash(canonical(history.messages));
    report.transport = transportEvidence('/data/evidence/transport.jsonl', model.base_url, model.model);
    report.native_models = (history.messages ?? []).filter((message) => message.role === 'assistant')
      .map((message) => ({ provider: message.provider ?? null, model: message.model ?? null, usage: message.usage ?? null }));
    requireValue(report.native_idle && report.native_tool_trace && report.random_artifact_matches, 'native_tool_or_idle_evidence_missing');
    report.native_tool_case_passed = true;
    requireValue(report.transport.verified, 'actual_model_transport_evidence_missing');
    report.native_case_passed = true;
  } finally {
    if (key && !report.native_case_passed) {
      try { report.abort_response = await client.request('chat.abort', { sessionKey: key, ...(report.native_run_id ? { runId: report.native_run_id } : {}) }); }
      catch { report.abort_response = 'unconfirmed'; }
    }
    save('native-events.json', client.events);
    client.close();
  }
}

async function main() {
  process.umask(0o077);
  const mode = process.argv[2];
  requireValue(['preflight', 'smoke', 'cancellation'].includes(mode), 'explicit_probe_mode_required');
  const report = { scope: 'native-openclaw-qualification', mode, native_case_passed: false, native_preflight_passed: false };
  try { await run(mode, report); }
  catch (error) { report.failure = { code: error.message, native: error.nativeError ?? null }; }
  finally { save('probe.json', report); }
  console.log(JSON.stringify({ mode, native_case_passed: report.native_case_passed,
    native_preflight_passed: report.native_preflight_passed, model_task_attempted: report.model_task_attempted ?? false }));
  process.exitCode = report.native_case_passed || report.native_preflight_passed ? 0 : 1;
}

if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) await main();
