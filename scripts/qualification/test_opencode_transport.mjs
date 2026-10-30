import fs from 'node:fs';
import path from 'node:path';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { fileURLToPath } from 'node:url';
import { install } from '../../deploy/qualification/opencode/transport_audit.mjs';
import transportPlugin from '../../deploy/qualification/opencode/transport_plugin.mjs';

export async function runTests(tempRoot) {
const parent = fs.realpathSync(tempRoot);
const cases = [];
const test = (name, callback) => cases.push({name, callback});
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
  await assert.rejects(f.target.fetch(endpoint, { method: 'POST', body }), /qualification_request_rejected/);
  assert.equal(calls, 6);assert.equal(f.records().filter(r => r.kind === 'request_started').length, 6);
  assert.equal(f.records().at(-1).kind, 'request_blocked');
});

test('a second installation preserves the file request count', async t => {
  const f = fixture(t, undefined, { maxRequests: 1 });install(f.options);
  await f.target.fetch(endpoint, { method: 'POST', body });
  let sends = 0;const target = { fetch: async () => { sends++;return response(); } };
  install({ ...f.options, target });
  await assert.rejects(target.fetch(endpoint, { method: 'POST', body }), /qualification_request_rejected/);
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
  fs.writeFileSync(f.auditFile, '', { mode: 0o644 });
  fs.chmodSync(f.auditFile, 0o644); // Native profile intentionally starts with umask 077.
  assert.equal(fs.statSync(f.auditFile).mode & 0o777, 0o644, 'nonprivate fixture precondition');
  assert.throws(() => install(f.options), /unsafe_or_oversized_log/);
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


test('native plugin hook only changes selected provider fetch and preserves native options', async t => {
  const f = fixture(t);const originalGlobal = globalThis.fetch;
  const providerOptions = {baseURL:base,apiKey:'PRIVATE-PLUGIN-KEY',timeout:60000,fetch:f.target.fetch};
  const config = {model:'harniqo/'+model,enabled_providers:['harniqo'],provider:{harniqo:{
    npm:'@ai-sdk/openai-compatible',models:{[model]:{}},options:providerOptions}}};
  const before = JSON.stringify(config);
  const hook = await transportPlugin({}, {providerId:'harniqo',expectedModel:model,auditFile:f.auditFile,maxRequests:4});
  await hook.config(config);
  assert.equal(globalThis.fetch, originalGlobal);assert.equal(JSON.stringify(config),before);
  assert.notEqual(providerOptions.fetch, f.target.fetch);
  await providerOptions.fetch(endpoint,{method:'POST',body});
  assert.equal(f.records().filter(r=>r.kind==='request_started').length,1);
  const ready=JSON.parse(fs.readFileSync(path.join(f.directory,'transport-ready.json'),'utf8'));
  assert.equal(ready.kind,'native_provider_fetch_hook_installed');assert.equal(ready.max_sdk_fetch_sends,4);
  assert.equal(JSON.stringify(ready).includes('PRIVATE-PLUGIN-KEY'),false);
});

test('401 and 403 keep their original response and block later SDK sends', async t => {
  for (const status of [401,403]) {
    let sends=0;const denied={...response(),status};
    const f=fixture(t,async()=>{sends++;return denied;});install(f.options);
    assert.equal(await f.target.fetch(endpoint,{method:'POST',body}),denied);
    await assert.rejects(f.target.fetch(endpoint,{method:'POST',body}),/qualification_request_rejected/);
    assert.equal(sends,1);assert.equal(f.records().at(-1).reason,'authentication_failed');
  }
});

test('native config reentry reuses receipt and fetch without duplicate counting', async t => {
  let sends=0;const f=fixture(t,async()=>{sends++;return response();});
  const args={providerId:'harniqo',expectedModel:model,auditFile:f.auditFile,maxRequests:4};
  const config={model:'harniqo/'+model,enabled_providers:['harniqo'],provider:{harniqo:{
    npm:'@ai-sdk/openai-compatible',models:{[model]:{}},options:{baseURL:base,fetch:f.target.fetch}}}};
  const hook=await transportPlugin({},args);await hook.config(config);
  const wrapped=config.provider.harniqo.options.fetch;
  await hook.config(config);await (await transportPlugin({},args)).config(config);
  assert.equal(config.provider.harniqo.options.fetch,wrapped);
  await wrapped(endpoint,{method:'POST',body});
  assert.equal(sends,1);assert.equal(f.records().filter(r=>r.kind==='request_started').length,1);
  const changed=await transportPlugin({},{...args,maxRequests:3});
  await assert.rejects(changed.config(config));assert.equal(config.provider.harniqo.options.fetch,wrapped);
});

test('native ready receipt rejects symlink and mismatched configuration', async t => {
  const f=fixture(t);const args={providerId:'harniqo',expectedModel:model,auditFile:f.auditFile,maxRequests:4};
  const original=f.target.fetch;
  const config={model:'harniqo/'+model,enabled_providers:['harniqo'],provider:{harniqo:{
    npm:'@ai-sdk/openai-compatible',models:{[model]:{}},options:{baseURL:base,fetch:original}}}};
  const ready=path.join(f.directory,'transport-ready.json');
  const other=path.join(f.directory,'other');fs.writeFileSync(other,'{}',{mode:0o600});fs.symlinkSync(other,ready);
  const hook=await transportPlugin({},args);await assert.rejects(hook.config(config));
  assert.equal(config.provider.harniqo.options.fetch,original);fs.unlinkSync(ready);
  fs.writeFileSync(ready,'{}',{mode:0o600});await assert.rejects(hook.config(config));
  assert.equal(config.provider.harniqo.options.fetch,original);
});

test('authentication failure remains blocked across another observer installation', async t => {
  const f=fixture(t,async()=>({...response(),status:401}));install(f.options);
  await f.target.fetch(endpoint,{method:'POST',body});
  let sends=0;const target={fetch:async()=>{sends++;return response();}};
  install({...f.options,target});
  await assert.rejects(target.fetch(endpoint,{method:'POST',body}),/qualification_request_rejected/);
  assert.equal(sends,0);assert.equal(f.records().at(-1).reason,'authentication_failed');
});

test('native plugin refuses a different model/provider or unknown options before mutation', async t => {
  const f = fixture(t);const args={providerId:'harniqo',expectedModel:model,auditFile:f.auditFile,maxRequests:4};
  await assert.rejects(transportPlugin({}, {...args,extra:true}));
  await assert.rejects(transportPlugin({}, {...args,maxRequests:7}));
  const hook=await transportPlugin({},args);
  for(const config of [{},{model:'harniqo/wrong',enabled_providers:['harniqo']}]) await assert.rejects(hook.config(config));
  assert.equal(fs.existsSync(f.auditFile),false);
});

const results=[];
for(const item of cases){
  const callbacks=[];
  try{await item.callback({after:fn=>callbacks.push(fn)});results.push({name:item.name,passed:true});}
  catch(error){results.push({name:item.name,passed:false,error_name:error.name,error_code:error.code??null,
    fixture_actual:typeof error.actual==='number'?error.actual:null,
    fixture_expected:typeof error.expected==='number'?error.expected:null});}
  finally{for(const cleanup of callbacks.reverse()) await cleanup();}
}
return {scope:'observer_and_native_config_hook_components_only',tests:results.length,
 passed:results.filter(r=>r.passed).length,failed:results.filter(r=>!r.passed).length,
 bun_version:typeof Bun==='undefined'?null:Bun.version,node_compat_version:process.version,results};
}
if(process.argv[1]&&fileURLToPath(import.meta.url)===process.argv[1]){
 const report=await runTests(process.env.HARNIQO_TEST_TEMP_ROOT??'.private/validation');
 console.log(JSON.stringify(report,null,2));process.exitCode=report.failed?1:0;
}
