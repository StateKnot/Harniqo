// Drives the actual fixed dsh SDK wire. Raw frames and stderr stay private.
import fs from 'node:fs';
import path from 'node:path';
import { spawn } from 'node:child_process';
import { createHash, randomUUID } from 'node:crypto';
const mode = process.argv[2];
if (!['preflight','transport','smoke'].includes(mode)) throw Error('invalid_mode');
const config = JSON.parse(fs.readFileSync('/run/config/model.json','utf8'));
const hash = v => createHash('sha256').update(v).digest('hex');
const evidence = '/data/evidence';
const workspace = '/data/workspace';
const sessionId = 'harniqo-'+randomUUID();
const challenge = randomUUID();
const patch = [
  {id:'llm-pi-ai',config:{providers:{harniqo:{apiKeyEnv:'HARNIQO_MODEL_KEY',api:'openai-completions',
    baseURL:config.base_url,retryPolicy:{mode:'normal',maxRetries:0},models:[{id:config.model,contextWindow:32768,maxTokens:1024,input:['text']}]}}}},
  {id:'session-telemetry-otel',disabled:true},
  {id:'tool-fs',config:{read:true,write:true,edit:false,readImage:false,readLimit:100,readMaxBytes:8192}},
  ...['tool-bash','tool-pwsh','tool-jobs','tool-fs-search','tool-skill','tool-subagent-control','tool-subagent','tool-subagent-fork',
    'tool-workflow','tool-todo','tool-goal','tool-web','tool-working-directory','tool-ask-user','present','tool-schedule'].map(id=>({id,disabled:true})),
];
fs.writeFileSync('/data/home/qualification.patch.json',JSON.stringify(patch),{mode:0o600});
const key = mode==='smoke' ? fs.readFileSync('/run/secrets/model-api-key','utf8').trim() : 'qualification-not-a-real-key';
const env = {PATH:'/usr/local/bin:/usr/bin:/bin',HOME:'/data/home',DSH_HOME:'/data/home',DSH_TELEMETRY_MODE:'OFF',DSH_PRIMARY_RUNTIME:'',
  HARNIQO_MODEL_KEY:key,DSH_MAX_TOKENS_AS_SUCCESS:'false',NARB_DISABLE_NATIVE_CACHE:'1'};
if(mode==='transport') env.HARNIQO_TRANSPORT_SELFTEST='1';
let active;
// Leave ten seconds for the outer operator to confirm container stop. This
// deadline bounds actual local execution even if a provider request stalls.
const watchdog=setTimeout(()=>{active?.kill('SIGKILL');process.exit(124);},110000);
function start(phase) {
  const child = spawn('/usr/local/bin/node',['--import','/opt/qualification/transport_audit.mjs',
    '/opt/dsh/node_modules/@deepseek-ai/dsh/lib/bin.js','--profile','sdk','--patch','/data/home/qualification.patch.json'],
    {cwd:workspace,env,stdio:['pipe','pipe','pipe']});
  active = child;
  const output = fs.openSync(path.join(evidence,phase+'-wire.jsonl'),'wx',0o600);
  const errors = fs.openSync(path.join(evidence,phase+'-stderr.log'),'wx',0o600);
  let wireBytes=0,stderrBytes=0,buffer='',nextId=0,exitResult;
  const pending = new Map(), notifications=[];
  const exit = new Promise(resolve=>child.once('close',(code,signal)=>{exitResult={code,signal};resolve(exitResult);
    for (const p of pending.values()) p.reject(Error('native_exit'));pending.clear();fs.closeSync(output);fs.closeSync(errors);}));
  child.stderr.on('data',b=>{stderrBytes+=b.length;if(stderrBytes>2*1024*1024) child.kill('SIGTERM');else fs.writeSync(errors,b);});
  child.stdout.on('data',b=>{
    wireBytes+=b.length;if(wireBytes>4*1024*1024){child.kill('SIGTERM');return;}fs.writeSync(output,b);buffer+=b.toString();
    let at;
    while((at=buffer.indexOf('\n'))>=0){
      const line=buffer.slice(0,at);buffer=buffer.slice(at+1);let v;
      try {v=JSON.parse(line);} catch {child.kill('SIGTERM');continue;}
      if(v.method && v.id!==undefined){
        // This task grants no arbitrary action. Workspace native read/write is
        // admitted by its real policy; any approval prompt is explicitly denied.
        child.stdin.write(JSON.stringify({jsonrpc:'2.0',id:v.id,result:{outcome:'rejected'}})+'\n');
        notifications.push(v);
      } else if(v.id!==undefined){const p=pending.get(v.id);if(p){pending.delete(v.id);v.error?p.reject(Error('rpc_'+v.error.code)):p.resolve(v.result);}}
      else notifications.push(v);
    }
  });
  function rpc(method,params,timeout=20000){
    const id=++nextId;
    return new Promise((resolve,reject)=>{
      const timer=setTimeout(()=>{pending.delete(id);reject(Error('rpc_deadline_'+method));},timeout);
      pending.set(id,{resolve:v=>{clearTimeout(timer);resolve(v);},reject:e=>{clearTimeout(timer);reject(e);}});
      child.stdin.write(JSON.stringify({jsonrpc:'2.0',id,method,...(params===undefined?{}:{params})})+'\n');
    });
  }
  return {child,exit,rpc,notifications,get exitResult(){return exitResult;}};
}
async function initialized(peer){
  const reply=await peer.rpc('initialize',{cwd:workspace,approvalRequests:true,provider:'harniqo',model:config.model,maxTokens:1024});
  if(reply.serverInfo?.name!=='deepseek-harness-sdk-runtime') throw Error('native_identity_mismatch');
  return reply;
}
async function shutdown(peer){await peer.rpc('shutdown');const result=await Promise.race([peer.exit,new Promise((_,rej)=>setTimeout(()=>rej(Error('shutdown_deadline')),10000))]);
  if(result.code!==0||result.signal) throw Error('unclean_native_shutdown');return result;}
let report={schema_version:1,kind:'dsh_native_sdk_qualification',source_commit:'f3803c96974ad4a94195fb97e5c637e9c2a3afe3',
  mode,session_id:sessionId,model:config.model,scope:'trusted_native_qualification',production_isolation_passed:false,native_prompt_cancel_supported:false,
  native_case_passed:false};
try {
  const peer=start('initial');report.initialize=await initialized(peer);
  const cwd=await peer.rpc('session/working-directory/get',{sessionId});
  if(cwd.cwd!==workspace) throw Error('native_cwd_mismatch');
  report.native_preflight_passed=true;
  if(mode==='transport'){
    await peer.rpc('session/prompt',{sessionId,contentBlocks:[{type:'text',text:'Reply briefly with OK.'}]});
    try {await peer.rpc('session/wait',{sessionId},30000);} catch {}
    const audit=fs.readFileSync(path.join(evidence,'transport.jsonl'),'utf8').trim().split('\n').filter(Boolean).map(JSON.parse);
    if(audit.length!==1||audit[0].kind!=='component_intercepted'||audit[0].actual_http_sent!==false||audit[0].model_matches_expected!==true||audit[0].output_cap!==1024) throw Error('native_transport_seam_unverified');
    report.native_transport_path_observed_without_http=true;
    report.component_only_intentional_provider_interception=true;
  }
  if(mode==='smoke'){
    fs.writeFileSync(path.join(workspace,'challenge.json'),JSON.stringify({challenge})+'\n',{mode:0o600});
    fs.writeFileSync(path.join(evidence,'submission-marker.json'),JSON.stringify({sessionId,submitted:true,at:new Date().toISOString()}),{mode:0o600});
    const receipt=await peer.rpc('session/prompt',{sessionId,contentBlocks:[{type:'text',text:'Use the native read tool to read /data/workspace/challenge.json. Use the native write tool to write /data/workspace/result.json containing exactly the same JSON object. Then use read to verify result.json. Reply briefly when done. Do not use any other tool.'}]});
    if(typeof receipt.messageId!=='string'||!receipt.messageId) throw Error('missing_native_message_id');
    report.message_id=receipt.messageId;
    await peer.rpc('session/wait',{sessionId},100000);
    const raw=fs.readFileSync(path.join(workspace,'result.json'));
    if(raw.length>4096||JSON.parse(raw).challenge!==challenge) throw Error('artifact_challenge_mismatch');
    const events=peer.notifications.filter(n=>n.method==='session.event' && n.params.sessionId===sessionId).map(n=>n.params.event);
    report.event_types=[...new Set(events.map(e=>e.type))];
    if(!events.some(e=>e.type==='turn/end'&&e.data?.reason?.kind==='completed')) throw Error('missing_successful_native_terminal');
    const calls=events.filter(e=>e.type==='tool/call');
    const expected=[['read','/data/workspace/challenge.json'],['write','/data/workspace/result.json'],['read','/data/workspace/result.json']];
    if(calls.length!==3) throw Error('unexpected_native_tool_count');
    report.tools=calls.map((event,index)=>{
      const args=JSON.parse(event.data.arguments);
      const result=events.find(e=>e.type==='tool/result'&&e.data?.message?.toolCallId===event.data.callId);
      if(event.data.name!==expected[index][0]||args.file_path!==expected[index][1]||!result||result.data.message.isError===true||result.data.error) throw Error('native_tool_sequence_or_result');
      return {name:event.data.name,call_id:event.data.callId,path:args.file_path,result_success:true};
    });
    const transport=fs.readFileSync(path.join(evidence,'transport.jsonl'),'utf8').trim().split('\n').map(JSON.parse);
    const sent=transport.filter(r=>r.kind==='request_started'),responses=transport.filter(r=>r.kind==='response');
    if(sent.length<1||sent.length>6||responses.length!==sent.length||transport.length!==2*sent.length
      ||sent.some(r=>!r.model_matches_expected||r.output_cap!==1024||r.model_sha256!==hash(config.model))
      ||responses.some(r=>r.status!==200||r.response_url_sha256!==sent.find(s=>s.request_id===r.request_id)?.url_sha256)) throw Error('actual_transport_evidence_mismatch');
    report.transport={actual_http_requests:sent.length,statuses:responses.map(r=>r.status),outbound_model_verified:true,served_model_identity:'unknown'};
    const usage=events.filter(e=>e.type==='assistant/message').map(e=>e.data.usage??null);
    report.usage={records:usage,precision:usage.every(v=>v!==null)?'provider_reported':'partial_or_unknown',cost:'unknown'};
    report.artifact={path:'result.json',bytes:raw.length,sha256:hash(raw),unprompted_challenge_matched:true};
    report.native_events=events.length;
  }
  report.initial_shutdown=await shutdown(peer);
  // A second process restores the same native identity without any new model
  // request. Existing durable log files are retained for independent checking.
  const restored=start('restored');await initialized(restored);
  await restored.rpc('session/resume',{sessionId});
  const restoredCwd=await restored.rpc('session/working-directory/get',{sessionId});
  if(restoredCwd.cwd!==workspace) throw Error('restored_cwd_mismatch');
  report.same_native_session_restored_without_prompt=true;
  report.restored_shutdown=await shutdown(restored);
  report.native_case_passed=mode==='smoke';report.status='passed';
} catch(error){report.status='failed';report.failure={code:error.message};}
finally {
  if(active && active.exitCode===null){active.kill('SIGTERM');await new Promise(r=>setTimeout(r,2000));if(active.exitCode===null) active.kill('SIGKILL');}
  fs.writeFileSync(path.join(evidence,'probe.json'),JSON.stringify(report,null,2)+'\n',{mode:0o600});
  clearTimeout(watchdog);
}
process.exit(report.status==='passed'?0:1);
