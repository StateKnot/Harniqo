import fs from 'node:fs';
import path from 'node:path';
import assert from 'node:assert/strict';
import test from 'node:test';
import { createHash } from 'node:crypto';
import { createRequire } from 'node:module';
import { pathToFileURL } from 'node:url';
const moduleUrl = process.env.HARNIQO_AUDIT_TEST_MODULE
  ? pathToFileURL(path.resolve(process.env.HARNIQO_AUDIT_TEST_MODULE))
  : new URL('../../deploy/qualification/openclaw/transport_audit.mjs', import.meta.url);
const { install, installRuntime } = await import(moduleUrl.href);

const parent = process.env.HARNIQO_TEST_TEMP_ROOT
  ? fs.realpathSync(process.env.HARNIQO_TEST_TEMP_ROOT)
  : path.join(fs.realpathSync(process.cwd()), '.state', 'qualification');
fs.mkdirSync(parent, { recursive: true, mode: 0o700 });
const sha = value => createHash('sha256').update(value).digest('hex');
const base = 'https://provider.example.invalid/compatible/v1/';
const endpoint = base + 'chat/completions';
const model = 'test-model';
const body = JSON.stringify({ model, messages: [{ role: 'user', content: 'PRIVATE-PROMPT-CANARY' }] });
const response = (url = endpoint, redirected = false) => ({ status: 200, url, redirected, bodyUsed: false });

function fixture(t, original = async () => response(), overrides = {}) {
  const directory = fs.mkdtempSync(path.join(parent, 'openclaw-transport-test-'));
  t.after(() => fs.rmSync(directory, { recursive: true }));
  const auditFile = path.join(directory, 'transport.jsonl');
  const target = { fetch: original };
  const options = { target, auditFile, expectedModel: model, apiBase: base, ...overrides };
  return { directory, auditFile, target, options,
    records: () => fs.readFileSync(auditFile, 'utf8').split('\n').filter(Boolean).map(JSON.parse) };
}

test('preserves fetch arguments, this, response and payload; records only hashes', async t => {
  const result = new Response('PRIVATE-RESPONSE-CANARY', { status: 200 });
  Object.defineProperty(result, 'url', { value: endpoint });
  let received;
  const f = fixture(t, async function (...args) { received = { args, receiver: this };return result; });
  const state = install(f.options);
  const headers = { Authorization: 'Bearer PRIVATE-KEY-CANARY' };
  const init = { method: 'POST', headers, body };
  const actual = await f.target.fetch(endpoint, init);
  assert.equal(actual, result);assert.equal(received.receiver, f.target);
  assert.equal(received.args[0], endpoint);assert.equal(received.args[1], init);
  assert.equal(init.headers, headers);assert.equal(init.body, body);
  assert.equal(actual.bodyUsed, false);assert.equal(state.healthy, true);
  const records = f.records();assert.equal(records.length, 2);
  assert.equal(records[0].model_sha256, sha(model));assert.equal(records[0].model_matches_expected, true);
  assert.equal(records[0].url_sha256, sha(endpoint));assert.equal(records[1].response_url_sha256, sha(endpoint));
  const raw = fs.readFileSync(f.auditFile, 'utf8');
  for (const value of [endpoint, base, model, 'PRIVATE-PROMPT-CANARY', 'PRIVATE-KEY-CANARY', 'PRIVATE-RESPONSE-CANARY', 'Authorization']) assert.equal(raw.includes(value), false);
  assert.equal(await actual.text(), 'PRIVATE-RESPONSE-CANARY');
  assert.equal(fs.statSync(f.auditFile).mode & 0o777, 0o600);
});

test('bounded Buffer input is inspected without mutation', async t => {
  const bytes = Buffer.from(body);const before = Buffer.from(bytes);
  const f = fixture(t);install(f.options);
  await f.target.fetch(new URL(endpoint), { method: 'post', body: bytes });
  assert.deepEqual(bytes, before);assert.equal(f.records()[0].model_known, true);
});

test('Request stream is never consumed or cloned by observer', async t => {
  const request = new Request(endpoint, { method: 'POST', body });let consumed;
  request.clone = () => { throw new Error('observer cloned body'); };
  const f = fixture(t, async input => { assert.equal(input.bodyUsed, false);consumed = await input.text();return response(); });
  install(f.options);await f.target.fetch(request);
  assert.equal(consumed, body);assert.equal(f.records()[0].model_known, false);
  assert.equal(f.records()[0].model_unknown_reason, 'uninspected_body_type');
});

test('similar prefix, other host and non POST remain untouched and uncounted', async t => {
  let calls = 0;const f = fixture(t, async () => { calls++;return response(); });install(f.options);
  await f.target.fetch(base.replace('/v1/', '/v1evil/') + 'chat/completions', { method: 'POST', body });
  await f.target.fetch('https://elsewhere.example.invalid/v1/chat/completions', { method: 'POST', body });
  await f.target.fetch(endpoint, { method: 'GET' });
  assert.equal(calls, 3);assert.deepEqual(f.records(), []);
  await f.target.fetch(endpoint, { method: 'POST', body });assert.equal(f.records().length, 2);
});

test('redirect is recorded and original returned response is unchanged', async t => {
  const redirected = response('https://redirect.example.invalid/v1/answer', true);
  const f = fixture(t, async () => redirected);install(f.options);
  assert.equal(await f.target.fetch(endpoint, { method: 'POST', body }), redirected);
  assert.equal(f.records()[1].redirected, true);
  assert.equal(f.records()[1].response_url_sha256, sha(redirected.url));
});

test('network error identity is preserved without recording its secret text', async t => {
  const error = new Error('PRIVATE-NETWORK-ERROR-CANARY');
  const f = fixture(t, async () => { throw error; });install(f.options);
  await assert.rejects(f.target.fetch(endpoint, { method: 'POST', body }), value => value === error);
  assert.equal(f.records()[1].network_error, true);
  assert.equal(fs.readFileSync(f.auditFile, 'utf8').includes(error.message), false);
});

test('installation is idempotent and incompatible reinstallation is rejected', async t => {
  let calls = 0;const f = fixture(t, async () => { calls++;return response(); });
  const first = install(f.options);const wrapper = f.target.fetch;
  assert.equal(install(f.options), first);assert.equal(f.target.fetch, wrapper);
  assert.throws(() => install({ ...f.options, expectedModel: 'different' }), /incompatible_install/);
  await f.target.fetch(endpoint, { method: 'POST', body });assert.equal(calls, 1);assert.equal(f.records().length, 2);
});

test('qualification limit records blocked attempts before any extra send', async t => {
  let calls = 0;const f = fixture(t, async () => { calls++;return response(); });install(f.options);
  for (let i = 0; i < 6; i++) await f.target.fetch(endpoint, { method: 'POST', body });
  await assert.rejects(f.target.fetch(endpoint, { method: 'POST', body }), /qualification_request_limit/);
  assert.equal(calls, 6);assert.equal(f.records().filter(r => r.kind === 'request_started').length, 6);
  assert.equal(f.records().at(-1).kind, 'request_blocked');
});

test('a second installation preserves the file request count', async t => {
  const f = fixture(t, undefined, { maxRequests: 1 });install(f.options);
  await f.target.fetch(endpoint, { method: 'POST', body });
  let sends = 0;const target = { fetch: async () => { sends++;return response(); } };
  install({ ...f.options, target });
  await assert.rejects(target.fetch(endpoint, { method: 'POST', body }), /qualification_request_limit/);
  assert.equal(sends, 0);
});

test('unknown, oversized and wrong model bodies are explicit without payload logging', async t => {
  const f = fixture(t);install(f.options);
  await f.target.fetch(endpoint, { method: 'POST', body: '{invalid-json-canary' });
  await f.target.fetch(endpoint, { method: 'POST', body: 'x'.repeat(512*1024+1) });
  await f.target.fetch(endpoint, { method: 'POST', body: JSON.stringify({ model: 'other-model' }) });
  const starts = f.records().filter(r => r.kind === 'request_started');
  assert.equal(starts[0].model_unknown_reason, 'invalid_json');
  assert.equal(starts[1].model_unknown_reason, 'body_bound');
  assert.equal(starts[2].model_matches_expected, false);
  assert.equal(fs.readFileSync(f.auditFile, 'utf8').includes('other-model'), false);
});

test('symlink, hardlink and nonprivate existing files are rejected', async t => {
  const f = fixture(t);
  const other = path.join(f.directory, 'other');fs.writeFileSync(other, '', { mode: 0o600 });
  fs.symlinkSync(other, f.auditFile);assert.throws(() => install(f.options));fs.unlinkSync(f.auditFile);
  fs.linkSync(other, f.auditFile);assert.throws(() => install(f.options), /unsafe_or_oversized_log/);fs.unlinkSync(f.auditFile);
  fs.writeFileSync(f.auditFile, '', { mode: 0o644 });assert.throws(() => install(f.options), /unsafe_or_oversized_log/);
});

test('replaced log cannot reset the request budget', async t => {
  let sends = 0;const f = fixture(t, async () => { sends++;return response(); });install(f.options);
  fs.renameSync(f.auditFile, f.auditFile + '.old');fs.writeFileSync(f.auditFile, '', { mode: 0o600 });
  await assert.rejects(f.target.fetch(endpoint, { method: 'POST', body }), /log_replaced/);
  assert.equal(sends, 0);
});

test('failed outcome logging preserves response then poisons future model sends', async t => {
  let sends = 0;const result = response();
  const f = fixture(t, async () => { sends++;fs.renameSync(f.auditFile, f.auditFile + '.old');return result; });
  const state = install(f.options);
  assert.equal(await f.target.fetch(endpoint, { method: 'POST', body }), result);
  assert.equal(state.healthy, false);
  await assert.rejects(f.target.fetch(endpoint, { method: 'POST', body }), /previous_audit_failure/);
  assert.equal(sends, 1);
});

test('oversized or partial logs fail closed before transport', t => {
  const f = fixture(t);fs.writeFileSync(f.auditFile, 'x'.repeat(256*1024+1), { mode: 0o600 });
  assert.throws(() => install(f.options), /unsafe_or_oversized_log/);
  fs.writeFileSync(f.auditFile, '{"schema":1', { mode: 0o600 });
  assert.throws(() => install(f.options), /incomplete_log_record/);
});

test('a live or abandoned lock is retained, never bypassed', async t => {
  let calls = 0;const f = fixture(t, async () => { calls++;return response(); });install(f.options);
  fs.writeFileSync(f.auditFile + '.lock', 'existing-writer', { mode: 0o600 });
  await assert.rejects(f.target.fetch(endpoint, { method: 'POST', body }), error => error.code === 'EEXIST');
  assert.equal(calls, 0);assert.equal(fs.readFileSync(f.auditFile + '.lock', 'utf8'), 'existing-writer');
});

test('global to undici delegation keeps dispatcher signal and one logical reservation', async t => {
  let sends = 0;let received;
  const leaf = { fetch: async (...args) => { sends++;received = args;return response(); } };
  const f = fixture(t, (...args) => leaf.fetch(...args));
  install(f.options);install({ ...f.options, target: leaf, transport: 'undici' });
  const dispatcher = {};const signal = new AbortController().signal;
  const init = { method: 'POST', body, dispatcher, signal, redirect: 'manual' };
  await f.target.fetch(endpoint, init);
  assert.equal(sends, 1);assert.equal(received[1], init);
  assert.equal(received[1].dispatcher, dispatcher);assert.equal(received[1].signal, signal);
  assert.equal(received[1].redirect, 'manual');assert.equal(f.records().length, 2);
  assert.deepEqual(f.records()[1].observed_transports, ['global', 'undici']);
  assert.equal(f.target.fetch.mock, undefined);assert.equal(leaf.fetch.mock, undefined);
});

test('SDK retries outside an ended nested fetch still consume separate reservations', async t => {
  let sends = 0;
  const leaf = { fetch: async () => { if (++sends === 1) throw new Error('retryable');return response(); } };
  const f = fixture(t, (...args) => leaf.fetch(...args));
  install(f.options);install({ ...f.options, target: leaf, transport: 'undici' });
  await assert.rejects(f.target.fetch(endpoint, { method: 'POST', body }), /retryable/);
  await f.target.fetch(endpoint, { method: 'POST', body });
  assert.equal(sends, 2);assert.equal(f.records().filter(r => r.kind === 'request_started').length, 2);
  assert.equal(f.records().filter(r => r.kind === 'network_error').length, 1);
});

test('SDK retry inside one delegated fetch retains each native attempt and failure', async t => {
  let sends = 0;
  const leaf = { fetch: async () => { if (++sends === 1) throw new Error('retryable');return response(); } };
  const f = fixture(t, async (...args) => {
    try { return await leaf.fetch(...args); } catch { return leaf.fetch(...args); }
  });
  install(f.options);install({ ...f.options, target: leaf, transport: 'undici' });
  assert.equal((await f.target.fetch(endpoint, { method: 'POST', body })).status, 200);
  const records = f.records();
  const starts = records.filter(r => r.kind === 'request_started');
  const outcomes = records.filter(r => ['response', 'network_error'].includes(r.kind));
  assert.equal(sends, 2);assert.equal(starts.length, 2);
  assert.equal(outcomes.length, 2);
  assert.equal(outcomes.filter(r => r.kind === 'network_error').length, 1);
  for (const start of starts) assert.equal(outcomes.filter(r => r.request_id === start.request_id).length, 1);
});

test('a retry inside one delegated fetch cannot bypass the shared request limit', async t => {
  let sends = 0;
  const leaf = { fetch: async () => { sends++;throw new Error('retryable'); } };
  const f = fixture(t, async (...args) => {
    try { return await leaf.fetch(...args); } catch { return leaf.fetch(...args); }
  }, { maxRequests: 1 });
  install(f.options);install({ ...f.options, target: leaf, transport: 'undici' });
  await assert.rejects(f.target.fetch(endpoint, { method: 'POST', body }), /qualification_request_limit/);
  assert.equal(sends, 1);
  assert.deepEqual(f.records().map(r => r.kind), ['request_started', 'network_error', 'request_blocked']);
});

test('concurrent identical fetch calls are not mistaken for nested delegation', async t => {
  let sends = 0;
  const leaf = { fetch: async () => { sends++;await Promise.resolve();return response(); } };
  const f = fixture(t, (...args) => leaf.fetch(...args));
  install(f.options);install({ ...f.options, target: leaf, transport: 'undici' });
  await Promise.all([f.target.fetch(endpoint, { method: 'POST', body }), f.target.fetch(endpoint, { method: 'POST', body })]);
  assert.equal(sends, 2);assert.equal(f.records().filter(r => r.kind === 'request_started').length, 2);
});

test('global and undici targets share a poisoned audit state', async t => {
  const f = fixture(t, async () => { fs.renameSync(f.auditFile, f.auditFile + '.old');return response(); });
  let leafSends = 0;const leaf = { fetch: async () => { leafSends++;return response(); } };
  const first = install(f.options);const second = install({ ...f.options, target: leaf, transport: 'undici' });
  await f.target.fetch(endpoint, { method: 'POST', body });
  assert.equal(first.healthy, false);assert.equal(second.healthy, false);
  await assert.rejects(leaf.fetch(endpoint, { method: 'POST', body }), /previous_audit_failure/);
  assert.equal(leafSends, 0);
});

test('pinned native undici CommonJS loader uses wrapped fetch and original MockAgent dispatcher',
  { skip: !process.env.HARNIQO_TEST_UNDICI_ANCHOR }, async t => {
    const anchor = path.resolve(process.env.HARNIQO_TEST_UNDICI_ANCHOR);
    const requireRuntime = createRequire(anchor);
    const undici = requireRuntime('undici/index.js');
    const version = requireRuntime('undici/package.json').version;
    assert.equal(version, '8.11.2');
    const originalAgent = undici.Agent;const originalFetch = undici.fetch;
    let globalSends = 0;const f = fixture(t, async () => { globalSends++;return response(); });
    const state = installRuntime({ ...f.options, globalTarget: f.target, requireFrom: anchor });
    // This matches OpenClaw's lazy createRequire(import.meta.url) subpath.
    const lazyRequire = createRequire(path.join(path.dirname(anchor), 'dist', 'undici-dispatcher-options.js'));
    assert.equal(lazyRequire('undici/index.js'), undici);
    assert.notEqual(lazyRequire('undici/index.js').fetch, originalFetch);
    assert.equal(undici.Agent, originalAgent);assert.equal(undici.fetch.mock, undefined);
    const esm = await import(pathToFileURL(requireRuntime.resolve('undici/index.js')).href);
    assert.equal(esm.default.fetch, undici.fetch);
    if ('fetch' in esm) assert.equal(esm.fetch, undici.fetch);
    const dispatcher = new undici.MockAgent();dispatcher.disableNetConnect();
    t.after(() => dispatcher.close());
    dispatcher.get('https://provider.example.invalid').intercept({ method: 'POST',
      path: '/compatible/v1/chat/completions', body,
      headers: { authorization: 'Bearer PRIVATE-NATIVE-HEADER' } }).reply(200, 'native-response');
    const signal = new AbortController().signal;
    const init = { method: 'POST', body, dispatcher, signal, redirect: 'manual',
      headers: { authorization: 'Bearer PRIVATE-NATIVE-HEADER' } };
    const runtimeFetch = lazyRequire('undici/index.js').fetch;
    const result = await runtimeFetch(endpoint, init);
    assert.equal(result.bodyUsed, false);assert.equal(await result.text(), 'native-response');
    assert.equal(init.dispatcher, dispatcher);assert.equal(init.signal, signal);
    assert.equal(init.redirect, 'manual');assert.equal(globalSends, 0);
    assert.equal(f.records().length, 2);assert.equal(f.records()[0].transport, 'undici');
    assert.deepEqual(f.records()[1].observed_transports, ['undici']);
    assert.equal(state.undici.healthy, true);
    const raw = fs.readFileSync(f.auditFile, 'utf8');
    assert.equal(raw.includes('PRIVATE-NATIVE-HEADER'), false);assert.equal(raw.includes('PRIVATE-PROMPT-CANARY'), false);
  });
