// A trusted qualification observer at the real global fetch seam. No mock,
// relay, response-body replacement, credential logging, or protocol conversion.
import fs from 'node:fs';
import { createHash } from 'node:crypto';
const hash = v => createHash('sha256').update(v).digest('hex');
const config = JSON.parse(fs.readFileSync('/run/config/model.json', 'utf8'));
const base = new URL(config.base_url);
const expected = new URL(base.href.replace(/\/$/, '') + '/chat/completions');
const audit = '/data/evidence/transport.jsonl';
const original = globalThis.fetch;
function record(value) {
  const fd = fs.openSync(audit, fs.constants.O_CREAT | fs.constants.O_APPEND | fs.constants.O_WRONLY | fs.constants.O_NOFOLLOW, 0o600);
  try {
    const st = fs.fstatSync(fd);
    if (!st.isFile() || st.nlink !== 1 || st.uid !== process.getuid() || (st.mode & 0o077) || st.size > 65536) throw Error('audit_file_rejected');
    fs.writeSync(fd, JSON.stringify({schema:1,...value})+'\n'); fs.fsyncSync(fd);
  } finally { fs.closeSync(fd); }
}
globalThis.fetch = async function(input, init) {
  const url = new URL(typeof input === 'string' || input instanceof URL ? input : input.url);
  const method = init?.method ?? input?.method ?? 'GET';
  if (url.href !== expected.href || method.toUpperCase() !== 'POST' || typeof init?.body !== 'string' || init.body.length > 2*1024*1024) {
    record({kind:'blocked',reason:'unexpected_fetch',url_sha256:hash(url.href)}); throw Error('qualification_unexpected_fetch');
  }
  const body = JSON.parse(init.body);
  const cap = body.max_tokens ?? body.max_completion_tokens;
  if (body.model !== config.model || !Number.isInteger(cap) || cap < 1 || cap > 1024) {
    record({kind:'blocked',reason:'model_or_output_cap'}); throw Error('qualification_model_or_output_cap');
  }
  const prior = fs.existsSync(audit) ? fs.readFileSync(audit,'utf8').trim().split('\n').filter(Boolean).map(JSON.parse) : [];
  const count = prior.filter(r => r.kind === 'request_started').length;
  if (count >= 6) { record({kind:'blocked',reason:'request_limit'}); throw Error('qualification_request_limit'); }
  const request_id = count+1;
  if(process.env.HARNIQO_TRANSPORT_SELFTEST==='1') {
    record({kind:'component_intercepted',url_sha256:hash(url.href),model_sha256:hash(body.model),
      model_matches_expected:true,output_cap:cap,actual_http_sent:false});
    throw Error('qualification_component_intercept');
  }
  record({kind:'request_started',request_id,url_sha256:hash(url.href),model_sha256:hash(body.model),
    model_matches_expected:true,output_cap:cap,body_sha256:hash(init.body)});
  try {
    const response = await Reflect.apply(original, this, [input, {...init,redirect:'error'}]);
    record({kind:'response',request_id,status:response.status,response_url_sha256:hash(response.url)});
    return response;
  } catch(error) { record({kind:'network_error',request_id}); throw error; }
};
