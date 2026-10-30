#!/usr/bin/env python3
"""Observe native Octop ACP calls; never implement or replace an ACP peer."""
import argparse
import asyncio
from contextlib import asynccontextmanager
import hashlib
import json
import os
from pathlib import Path
import time
import traceback
import secrets
import urllib.request
import urllib.parse

OUT=Path('/evidence')

def serial(value):
 if hasattr(value,'model_dump'):return value.model_dump(mode='json',by_alias=True,exclude_none=True)
 if isinstance(value,dict):return {k:serial(v) for k,v in value.items()}
 if isinstance(value,(list,tuple)):return [serial(v) for v in value]
 if value is None or isinstance(value,(str,int,float,bool)):return value
 return {'python_type':type(value).__name__}

def save(name,value):
 p=OUT/name;p.write_text(json.dumps(serial(value),indent=2,ensure_ascii=False)+'\n');p.chmod(0o600)

def qualification_identity():
 import octop_harness.builtin.tools.acp_runner as module
 profile=json.loads(Path('/run/qualification-profile.json').read_text())
 source_hash=hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest()
 expected=profile.get('patch',{}).get('patched_sha256','fd03c837bca1aefb6629a094a0ee783b823af06832cc371633e55d11c41ecac9')
 if source_hash!=expected:raise ValueError('loaded acp_runner source identity mismatch')
 if profile.get('patch'):
  patch_hash=hashlib.sha256(Path('/opt/acp-qualification/patch/acp-final-text.patch').read_bytes()).hexdigest()
  if patch_hash!=profile['patch']['patch_sha256']:raise ValueError('installed patch identity mismatch')
 budgets={k:profile[k] for k in ['outer_max_requests','inner_max_requests','outer_max_output_tokens','inner_max_output_tokens','runtime_limit_seconds']}
 expected_outer=512 if profile['scope']=='octop-harness-acp-delegated-patched-outer512' else 128
 if budgets!={'outer_max_requests':4,'inner_max_requests':4,'outer_max_output_tokens':expected_outer,'inner_max_output_tokens':1024,'runtime_limit_seconds':180}:raise ValueError('runtime budget profile mismatch')
 return {'scope':profile['scope'],'loaded_acp_runner_sha256':source_hash,'patch_sha256':profile.get('patch',{}).get('patch_sha256'),'budgets':budgets}

def inner_environment(config, *, model=False, observer=False):
 selected='harniqo/'+config['model']
 native={'model':selected,'small_model':selected,'enabled_providers':['harniqo'],
   'share':'disabled','autoupdate':False,'snapshot':False,'plugin':[], 'mcp':{},'lsp':False,'formatter':False,
   'instructions':[],'compaction':{'auto':False,'prune':False},
   'agent':{'title':{'disable':True},'summary':{'disable':True},'build':{'steps':4}},
   'permission':{'*':'deny','read':'allow','external_directory':'deny'},
   'provider':{'harniqo':{'npm':'@ai-sdk/openai-compatible','name':'Harniqo ACP qualification',
     'options':{'baseURL':config['base_url'].rstrip('/'),'apiKey':'{env:HARNIQO_MODEL_API_KEY}','timeout':45000},
     'models':{config['model']:{'name':'Approved qualification model','tool_call':True,'reasoning':False,
       'limit':{'context':32768,'output':1024}}}}}}
 if observer:
  native['plugin']=[['file:///run/observer/transport_plugin.mjs',{'auditFile':'/evidence/inner-audit/transport.jsonl','providerId':'harniqo','expectedModel':config['model'],'maxRequests':4}]]
 return {
  'PATH':'/usr/local/bin:/usr/bin:/bin','HOME':'/evidence/inner-home',
  'XDG_DATA_HOME':'/evidence/inner-home/data','XDG_STATE_HOME':'/evidence/inner-home/state',
  'XDG_CACHE_HOME':'/evidence/inner-home/cache','XDG_CONFIG_HOME':'/opt/harniqo/config',
  'OPENCODE_DISABLE_AUTOUPDATE':'1','OPENCODE_DISABLE_MODELS_FETCH':'1',
  'OPENCODE_DISABLE_DEFAULT_PLUGINS':'true','OPENCODE_DISABLE_EXTERNAL_SKILLS':'true',
  'OPENCODE_DISABLE_LSP_DOWNLOAD':'true','OPENCODE_DISABLE_CLAUDE_CODE':'true',
  'OPENCODE_DISABLE_PROJECT_CONFIG':'1','OPENCODE_DISABLE_FFF':'1',
  'OPENCODE_DISABLE_EMBEDDED_WEB_UI':'true','OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX':'1024',
  'HARNIQO_MODEL_API_KEY':Path('/run/model-key').read_text().strip() if model else 'qualification-not-a-real-key','OPENCODE_CONFIG_CONTENT':json.dumps(native)}

def observe_native_acp():
 import acp
 original=acp.spawn_agent_process
 events=[];processes=[]
 def record(item):events.append(serial(item));save('acp-observed.json',events)
 @asynccontextmanager
 async def observed(client,*args,**kwargs):
  update=client.session_update
  async def session_update(*a,**kw):
   record({'direction':'inner-notification','method':'session/update','args':a,'kwargs':kw})
   return await update(*a,**kw)
  client.session_update=session_update
  permission=client.request_permission
  async def request_permission(*a,**kw):
   record({'direction':'inner-request','method':'session/request_permission','args':a,'kwargs':kw})
   return await permission(*a,**kw)
  client.request_permission=request_permission
  async with original(client,*args,**kwargs) as (connection,process):
   processes.append(process)
   record({'event':'native-process-started','pid':process.pid,'command':args[0],'arguments':list(args[1:])})
   class ObservedConnection:
    def __getattr__(self,name):
     target=getattr(connection,name)
     if name not in {'initialize','new_session','prompt','cancel','close_session','load_session'}:return target
     async def call(*a,**kw):
      record({'direction':'outer-request','method':name,'args':a,'kwargs':kw})
      try:
       result=await target(*a,**kw)
       record({'direction':'inner-response','method':name,'result':result})
       return result
      except BaseException as error:
       record({'direction':'inner-error','method':name,'error_type':type(error).__name__})
       raise
     return call
   yield ObservedConnection(),process
 acp.spawn_agent_process=observed
 return events,processes

async def bootstrap(observer=False):
 from octop_harness.acp.models import ACPConfig,ACPRunnerConfig,ACPSessionError
 from octop_harness.acp.service import ACPService
 env=inner_environment({'base_url':'https://example.invalid/v1','model':'qwen3.8-flash'},observer=observer)
 runner=ACPRunnerConfig(enabled=True,command='/usr/local/bin/opencode',args=['acp','--hostname','127.0.0.1','--port','10012'],env=env,trusted=False)
 service=ACPService(config=ACPConfig(runners={'opencode':runner}))
 events,processes=observe_native_acp()
 identity=qualification_identity()
 report={**identity,'scope':identity['scope']+'-bootstrap','qualified':False,'outer_model_calls':0,'inner_model_calls':0}
 try:
  first=await asyncio.wait_for(service._get_or_create_session(thread_id='qualification-bootstrap',runner='opencode',cwd='/evidence/workspace',require_existing=False),45)
  first_id=first.acp_session_id
  await asyncio.wait_for(service.close_thread_session(thread_id='qualification-bootstrap',runner='opencode'),15)
  report['native_close_returned']=first.process.returncode is not None
  second=await asyncio.wait_for(service._get_or_create_session(thread_id='qualification-bootstrap',runner='opencode',cwd='/evidence/workspace',require_existing=False),45)
  report['new_session_after_close_is_distinct']=first_id!=second.acp_session_id
  report['native_session_ids']=[first_id,second.acp_session_id]
  second.process.terminate();await asyncio.wait_for(second.process.wait(),10)
  report['disconnected_child_exit_observed']=second.process.returncode is not None
  events.append({'event':'qualification-disconnect-confirmed','pid':second.process.pid,'returncode':second.process.returncode});save('acp-observed.json',events)
  before=(len(processes),sum(e.get('method')=='initialize' for e in events),sum(e.get('method')=='new_session' for e in events))
  try:await service._get_or_create_session(thread_id='qualification-bootstrap',runner='opencode',cwd='/evidence/workspace',require_existing=True)
  except (ACPSessionError,ConnectionResetError,BrokenPipeError) as error:
   report['require_existing_after_disconnect_rejected']=True
   report['disconnect_rejection_type']=type(error).__name__
  else:report['require_existing_after_disconnect_rejected']=False
  after=(len(processes),sum(e.get('method')=='initialize' for e in events),sum(e.get('method')=='new_session' for e in events))
  report['disconnect_rejection_created_no_resources']=before==after
  report['positive_native_session_ids']=all(isinstance(s,str) and s.startswith('ses_') and len(s)>4 for s in report['native_session_ids'])
  initializations=[e['result'] for e in events if e.get('direction')=='inner-response' and e.get('method')=='initialize']
  report['protocol_and_agent_identity']=bool(initializations) and all(x.get('protocolVersion')==1 and x.get('agentInfo')=={'name':'OpenCode','version':'1.19.5'} for x in initializations)
  report['no_prompt_sent']=not any(e.get('method')=='prompt' for e in events)
  if observer:
   report['inner_observer_ready']=(OUT/'inner-audit/transport-ready.json').is_file()
   report['inner_no_sends']=not (OUT/'inner-audit/transport.jsonl').read_text().strip()
  report['qualified']=all(report.get(k) for k in ['native_close_returned','new_session_after_close_is_distinct','require_existing_after_disconnect_rejected','protocol_and_agent_identity','no_prompt_sent','disconnected_child_exit_observed','disconnect_rejection_created_no_resources','positive_native_session_ids'])
 except Exception as error:
  report['error_type']=type(error).__name__; (OUT/'error.log').write_text(traceback.format_exc())
 finally:
  try:await asyncio.wait_for(service.close_all_sessions(),15)
  except Exception as error:report.update(qualified=False,close_error=type(error).__name__)
  report['child_return_codes']=[p.returncode for p in processes]
  report['qualified']=report['qualified'] and all(p.returncode is not None for p in processes)
  if observer:report['qualified']=report['qualified'] and report.get('inner_observer_ready',False) and report.get('inner_no_sends',False)
  save('report.json',report)
 return 0 if report['qualified'] else 1

async def model_task():
 from guard_octop_acp import OuterBudget,DELEGATED_PROMPT,permitted_call
 import httpx
 from langchain.agents.middleware import AgentMiddleware
 from octop_harness import HarnessAgent,HarnessAgentConfig,ProviderConfig,ModelConfig,ChatRequest
 from octop_harness.acp.models import ACPRunnerConfig
 from octop_harness.acp.service import get_acp_service
 from octop_harness.llm.factory import ChatModelFactory
 report={**qualification_identity(),'qualified':False,'outer_model_calls':0,'inner_model_calls':0,
         'complete_service_scope':False,'production_isolation_qualified':False,'restart_continuation_qualified':False}
 save('report.json',report)
 agent=factory=service=None;budget=None;events=[];processes=[];started=time.monotonic()
 try:
  cfg=json.loads(Path('/run/model.json').read_text());outer_tokens=report['budgets']['outer_max_output_tokens'];budget=OuterBudget(cfg,save,max_output_tokens=outer_tokens)
  original_send=httpx.AsyncClient.send;requests=[]
  async def audited_send(client,request,**kwargs):
   row=budget.admit(str(request.url),request.content)
   requests.append(json.loads(request.content));save('outer-http-request-bodies.json',requests)
   request.extensions['timeout']=dict(connect=20,read=45,write=20,pool=20);kwargs['follow_redirects']=False
   response=await original_send(client,request,**kwargs);budget.completed(row,response.status_code)
   if response.is_redirect:
    await response.aclose();raise RuntimeError('outer redirect refused')
   return response
  httpx.AsyncClient.send=audited_send
  class NoRetryFactory(ChatModelFactory):
   def get_chat_model(self,model_ref,*,max_retries=None):return super().get_chat_model(model_ref,max_retries=0)
  actions=[]
  class Boundaries(AgentMiddleware):
   async def awrap_model_call(self,request,handler):
    return await handler(request.override(tools=[t for t in request.tools if getattr(t,'name',None)=='acp_runner']))
   async def awrap_tool_call(self,request,handler):
    if actions or not permitted_call(request.tool_call):raise RuntimeError('delegation arguments or task count rejected')
    actions.append(request.tool_call);save('outer-tool-actions.json',actions)
    return await handler(request)
  receipt=secrets.token_hex(16);(OUT/'workspace/input.txt').write_text(receipt)
  env=inner_environment(cfg,model=True,observer=True)
  runner=ACPRunnerConfig(enabled=True,command='/usr/local/bin/opencode',args=['acp','--hostname','127.0.0.1','--port','10012'],env=env,trusted=False)
  provider=ProviderConfig(id='outer',base_url=cfg['base_url'],api_key=Path('/run/model-key').read_text().strip(),models=[ModelConfig(id=cfg['model'],max_input_tokens=32768,max_output_tokens=outer_tokens)])
  factory=NoRetryFactory([provider]);outer_workspace=OUT/'outer-workspace'
  config=HarnessAgentConfig(name='harniqo-octop-acp',providers=[provider],default_model='outer/'+cfg['model'],workspace_dir=outer_workspace,
    backend={'type':'filesystem','root_dir':str(outer_workspace),'virtual_mode':True},
    system_prompt='Use only acp_runner as instructed. Do not read files yourself. After delegation, return only the delegated receipt.',
    memory_enabled=False,memory=(),team_enabled=False,ask_user_enabled=False,subagents_auto_load=False,subagents=[],web_search_tools=False,todos_enabled=False,
    model_retry_enabled=False,tool_search_mode='eager',media_offload_enabled=False,acp_delegate_enabled=True,acp_runners={'opencode':runner},middleware=[Boundaries()],session_log_enabled=True)
  agent=HarnessAgent(config,model_factory=factory,agent_id='harniqo-octop-acp-outer');thread='outer-acp-'+secrets.token_hex(8)
  report['outer_thread_id']=thread;events,processes=observe_native_acp()
  prompt=('Call acp_runner exactly once with action=start, runner=opencode, cwd=/evidence/workspace, max_runtime=90, '
          'and message exactly: '+DELEGATED_PROMPT+' Then reply with only the delegated receipt.')
  save('outer-input.json',{'thread_id':thread,'prompt':prompt,'delegated_prompt':DELEGATED_PROMPT})
  stream=[]
  async with asyncio.timeout(130):
   async for event in agent.stream(ChatRequest(messages=prompt,thread_id=thread,user='qualification-operator',skills=[],recursion_limit=64,
        configurable={'model_settings':{'max_tokens':outer_tokens,'temperature':0}})):
    stream.append(event);save('outer-events.json',stream)
  history=[m.model_dump(mode='json') for m in await agent.aget_history(thread,limit=50)];save('outer-history.json',history)
  service=get_acp_service(str(agent._workspace_path))
  if service is None:raise RuntimeError('native ACP service not created')
  conversation=await service.get_session(thread,'opencode')
  if conversation is None:raise RuntimeError('native inner session missing')
  sid=conversation.acp_session_id;report['inner_session_id']=sid
  query=urllib.parse.urlencode({'directory':'/evidence/workspace'})
  with urllib.request.urlopen('http://127.0.0.1:10012/session/'+sid+'/message?'+query,timeout=10) as response:inner=json.load(response)
  with urllib.request.urlopen('http://127.0.0.1:10012/session/status?'+query,timeout=10) as response:status=json.load(response)
  save('inner-history.json',inner);save('inner-status.json',status)
  ai=[m for m in history if m.get('type')=='ai'];tool=[m for m in history if m.get('type')=='tool']
  final=ai[-1].get('content') if ai else None
  inner_ai=[m for m in inner if m.get('info',{}).get('role')=='assistant']
  inner_users=[m for m in inner if m.get('info',{}).get('role')=='user']
  inner_text=''.join(p.get('text','') for p in (inner_ai[-1].get('parts',[]) if inner_ai else []) if p.get('type')=='text')
  tools=[p for m in inner for p in m.get('parts',[]) if p.get('type')=='tool']
  audit=[json.loads(line) for line in (OUT/'inner-audit/transport.jsonl').read_text().splitlines() if line]
  save('inner-audit-summary.json',audit)
  sends=[r for r in audit if r['kind']=='request_started'];responses=[r for r in audit if r['kind']=='response']
  expected=hashlib.sha256((cfg['base_url'].rstrip('/')+'/chat/completions').encode()).hexdigest()
  prompts=[e['kwargs']['prompt'] for e in events if e.get('direction')=='outer-request' and e.get('method')=='prompt']
  report.update(outer_history_count=len(history),inner_history_count=len(inner),outer_tool_count=len(tool),inner_tool_count=len(tools),
   outer_model_calls=len(budget.calls),inner_model_calls=len(sends),
   checks={'one_native_acp_prompt':len(prompts)==1 and prompts[0]==[{'type':'text','text':DELEGATED_PROMPT}],
    'outer_receipt_exact':final==receipt,'inner_receipt_exact':inner_text==receipt,'independent_session_ids':sid!=thread and sid.startswith('ses_'),
    'outer_native_tool':len(tool)==1 and tool[0].get('name')=='acp_runner' and receipt in str(tool[0].get('content','')),
    'inner_native_read':len(tools)==1 and tools[0].get('tool')=='read' and tools[0].get('state',{}).get('status')=='completed' and tools[0].get('state',{}).get('input',{}).get('filePath') in {'input.txt','/evidence/workspace/input.txt'},
    'inner_prompt_matches':len(inner_users)==1 and ''.join(p.get('text','') for p in inner_users[0].get('parts',[]) if p.get('type')=='text')==DELEGATED_PROMPT,
    'inner_native_model':bool(inner_ai) and all(m['info'].get('modelID')==cfg['model'] and m['info'].get('providerID')=='harniqo' for m in inner_ai),
    'inner_idle':status.get(sid,{'type':'idle'}).get('type')=='idle',
    'outer_served_model':bool(ai) and all(m.get('response_metadata',{}).get('model_name')==cfg['model'] for m in ai),
    'outer_terminal':bool(ai) and ai[-1].get('response_metadata',{}).get('finish_reason')=='stop' and not ai[-1].get('tool_calls'),
    'inner_terminal':bool(inner_ai) and bool(inner_ai[-1].get('info',{}).get('time',{}).get('completed')) and not inner_ai[-1].get('info',{}).get('error'),
    'outer_http':1<=len(budget.calls)<=4 and all(r['status']==200 for r in budget.calls) and not budget.auth_failed,
    'inner_http':1<=len(sends)<=4 and len(responses)==len(sends) and all(r.get('model_matches_expected') and r.get('url_sha256')==expected for r in sends) and
      all(r.get('status')==200 and r.get('response_url_sha256')==expected and r.get('redirected') is False for r in responses) and not any(r['kind'] in {'network_error','request_blocked'} for r in audit),
    'outer_no_error':not any(e.get('type')=='error' for e in stream)},
   inner_served_model_metadata='not_observed',supplemental_readonly_http={'native_history':1,'native_status':1},outer_usage=[m.get('usage_metadata') for m in ai],inner_usage=[m['info'].get('tokens') for m in inner_ai],
   receipt_sha256=hashlib.sha256(receipt.encode()).hexdigest())
  report['qualified']=all(report['checks'].values())
 except Exception as error:
  report.update(qualified=False,error_type=type(error).__name__);(OUT/'error.log').write_text(traceback.format_exc())
 finally:
  cleanup=[]
  for resource in [service,agent,factory]:
   if resource is not None:
    try:
     method=resource.close_all_sessions if resource is service else resource.aclose
     await asyncio.wait_for(method(),10)
    except Exception as error:cleanup.append(type(error).__name__)
  if budget:report['outer_model_calls']=len(budget.calls)
  audit=OUT/'inner-audit/transport.jsonl'
  if audit.exists():report['inner_model_calls']=sum(json.loads(line).get('kind')=='request_started' for line in audit.read_text().splitlines() if line)
  report.update(cleanup_errors=cleanup,child_return_codes=[p.returncode for p in processes],elapsed_seconds=time.monotonic()-started)
  report['qualified']=report['qualified'] and not cleanup and bool(processes) and all(p.returncode is not None for p in processes)
  save('report.json',report)
 return 0 if report['qualified'] else 1

if __name__=='__main__':
 os.umask(0o077)
 parser=argparse.ArgumentParser();parser.add_argument('--mode',choices=['bootstrap','model'],default='bootstrap');parser.add_argument('--observer',action='store_true');args=parser.parse_args()
 raise SystemExit(asyncio.run(model_task() if args.mode=='model' else bootstrap(args.observer)))
