// Budget controls only; every upstream fetch is a test function, with no HTTP.
import assert from 'node:assert/strict';
import fs from 'node:fs';
const config=JSON.parse(fs.readFileSync('/run/config/model.json','utf8'));
const url=config.base_url.replace(/\/$/,'')+'/chat/completions';
let delegated=0;
const response=new Response('component fixture',{status:200});
Object.defineProperty(response,'url',{value:url});
globalThis.fetch=async()=>{delegated++;return response;};
await import('/opt/qualification/transport_audit.mjs');
const send=body=>fetch(url,{method:'POST',body:JSON.stringify(body)});
await assert.rejects(send({model:'wrong',max_tokens:1024}));
await assert.rejects(send({model:config.model,max_tokens:1025}));
await assert.rejects(send({model:config.model}));
await assert.rejects(fetch('https://example.invalid',{method:'GET'}));
assert.equal(delegated,0);
for(let i=0;i<6;i++)assert.equal(await send({model:config.model,max_tokens:1024}),response);
await assert.rejects(send({model:config.model,max_tokens:1024}));
assert.equal(delegated,6);
const audit=fs.readFileSync('/data/evidence/transport.jsonl','utf8').trim().split('\n').map(JSON.parse);
assert.equal(audit.filter(r=>r.kind==='request_started').length,6);
assert.equal(audit.filter(r=>r.kind==='response').length,6);
assert.equal(audit.filter(r=>r.kind==='blocked').length,5);
assert.ok(!JSON.stringify(audit).includes(config.base_url));
fs.writeFileSync('/data/evidence/probe.json',JSON.stringify({status:'passed',component_tests:8,actual_http_sent:0,
  native_preflight_passed:false,native_case_passed:false})+'\n',{mode:0o600});
