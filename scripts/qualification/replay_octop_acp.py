#!/usr/bin/env python3
"""Offline component replay of retained real ACP events; never an ACP peer.

Runs pinned upstream client and tool formatting against a recording fixture.
No model, child agent or protocol connection is constructed in replay mode.
"""
import argparse
import asyncio
from collections import Counter
import hashlib
import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


def sha(data):return hashlib.sha256(data).hexdigest()
def text_parts(message):return ''.join(p.get('text','') for p in message.get('parts',[]) if p.get('type')=='text')
def measure(text,receipt):return {'type':type(text).__name__,'length':len(text) if isinstance(text,str) else None,'receipt_exact':text==receipt,'receipt_contains':isinstance(text,str) and receipt in text}


async def invoke(module,service):
 from octop_harness.acp.models import ACPConfig,ACPRunnerConfig
 config=ACPConfig(runners={'opencode':ACPRunnerConfig(enabled=True,command='never-launched')})
 with patch.object(module,'_get_service',return_value=service),patch.object(module,'_thread_id_from_config',return_value='offline-recording'):
  return await module._run_streaming_action(service_key='offline',config=config,action_name='start',runner_name='opencode',message_text='recorded',execution_cwd=Path('/evidence/workspace'),timeout_seconds=5)


async def replay(source,out,patch_file,profile_file,installed_patched=False):
 import acp.schema
 from octop_harness.acp.client import ACPHostedClient
 from octop_harness.acp.models import ACPRunnerConfig
 from octop_harness.acp.tool_adapter import render_event_text,format_final_response
 import octop_harness.builtin.tools.acp_runner as native
 from apply_octop_acp_patch import patched_source
 profile=json.loads(profile_file.read_text());loaded=Path(native.__file__).read_bytes()
 def load_module(data,name):
  path=out/(name+'.py');path.write_bytes(data);spec=importlib.util.spec_from_file_location(name,path);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module
 if installed_patched:
  if sha(loaded)!=profile['patched_sha256']:raise ValueError('installed patched identity mismatch')
  lines=patch_file.read_text().splitlines(keepends=True)[3:]
  before=''.join(x[1:] for x in lines if x[0] in ' -');after=''.join(x[1:] for x in lines if x[0] in ' +')
  text=loaded.decode()
  if text.count(after)!=1:raise ValueError('cannot reconstruct exact original fixture')
  original=text.replace(after,before,1).encode()
  if sha(original)!=profile['upstream_sha256']:raise ValueError('reconstructed fixture identity mismatch')
  patched=patched_source(original,patch_file.read_bytes(),profile)
  if patched!=loaded:raise ValueError('installed patch differs')
  modified=native;native=load_module(original,'offline_original_acp_runner')
 else:
  original=loaded
  if sha(original)!=profile['upstream_sha256']:raise ValueError('upstream source identity mismatch')
  patched=patched_source(original,patch_file.read_bytes(),profile);modified=load_module(patched,'offline_patched_acp_runner')
 names=['acp-observed.json','outer-history.json','inner-history.json','outer-tool-actions.json','outer-input.json','report.json','input.txt']
 hashes={n:sha((source/n).read_bytes()) for n in names}
 events=json.loads((source/'acp-observed.json').read_text());outer=json.loads((source/'outer-history.json').read_text());inner=json.loads((source/'inner-history.json').read_text())
 actions=json.loads((source/'outer-tool-actions.json').read_text());inputs=json.loads((source/'outer-input.json').read_text());prior=json.loads((source/'report.json').read_text());receipt=(source/'input.txt').read_text()
 updates=[e['kwargs'] for e in events if e.get('method')=='session/update'];counts=Counter(u['update']['sessionUpdate'] for u in updates)
 class RecordingService:
  def __init__(self):self.callbacks=[];self.final=None
  async def get_session(self,*args):return None
  async def run_turn(self,**kwargs):
   client=ACPHostedClient(runner_name='opencode',runner_config=ACPRunnerConfig(),cwd='/evidence/workspace')
   async def callback(payload,last):self.callbacks.append(payload);await kwargs['on_message'](payload,last)
   client.start_prompt(callback)
   for u in updates:
    parsed=acp.schema.SessionNotification.model_validate({'sessionId':u['session_id'],'update':u['update']})
    await client.session_update(session_id=parsed.session_id,update=parsed.update)
   self.final=await client.finish_prompt()
   return {'status':'completed','event':self.final}
 baseline=RecordingService();native_text=await invoke(native,baseline)
 repaired=RecordingService();patched_text=await invoke(modified,repaired)
 body='\n\n'.join(x for x in map(render_event_text,baseline.callbacks) if x).strip()
 tool=[m for m in outer if m.get('type')=='tool'][0]['content'];outer_final=[m for m in outer if m.get('type')=='ai'][-1]['content']
 inner_final=text_parts([m for m in inner if m['info']['role']=='assistant'][-1]);inner_user=text_parts([m for m in inner if m['info']['role']=='user'][0])
 outer_user=[m for m in outer if m.get('type')=='human'][0]['content'];args=actions[0]['args']['message']
 prompt=[e['kwargs']['prompt'] for e in events if e.get('direction')=='outer-request' and e.get('method')=='prompt'][0]
 acp_text=''.join(p.get('text','') for p in prompt if p.get('type')=='text');chunk_text=''.join(u['update']['content'].get('text','') for u in updates if u['update']['sessionUpdate']=='agent_message_chunk')
 checks={'original_run_failed':prior['qualified'] is False,'input_receipt_hash_matches':sha(receipt.encode())==prior['receipt_sha256'],
  'outer_user_matches_saved_input':outer_user==inputs['prompt'],'delegated_args_embedded_in_outer_user':args in outer_user,
  'args_to_acp_text_exact':args==acp_text,'acp_text_to_inner_user_exact':acp_text==inner_user,
  'inner_receipt_exact':inner_final==receipt,'acp_chunks_exact':chunk_text==receipt,'native_client_final_exact':baseline.final=={'type':'text','text':receipt,'is_chunk':False},
  'native_tool_replay_equals_saved_tool':native_text==tool,'native_tool_equals_callback_summaries':native_text==body,'saved_outer_equals_saved_tool':outer_final==tool,
  'native_tool_loses_receipt':receipt not in native_text,'patched_preserves_tool_summaries':patched_text.startswith(body+'\n\n'),
  'patched_contains_receipt_exactly_once':patched_text.count(receipt)==1,'patched_client_events_unchanged':baseline.callbacks==repaired.callbacks and baseline.final==repaired.final}
 # Formatting regression fixtures: these are not ACP/model/network qualification.
 class FixtureService:
  def __init__(self,callbacks,result):self.callbacks=callbacks;self.result=result
  async def get_session(self,*args):return None
  async def run_turn(self,**kwargs):
   for event in self.callbacks:await kwargs['on_message'](event,True)
   return self.result
 text_event={'type':'text','text':'fixture receipt'};tool_event={'type':'tool_start','kind':'read','detail':'fixture'};error_event={'type':'error','message':'fixture failure'}
 cases=[
  ('text_only',[],{'event':text_event},None),
  ('no_output',[],{'event':None},None),
  ('already_emitted_text',[text_event],{'event':text_event},'[assistant]\nfixture receipt'),
  ('tool_plus_final',[tool_event],{'event':text_event},'[tool_call] read (fixture)\n\n[assistant]\nfixture receipt'),
  ('tool_plus_error',[tool_event],{'event':error_event},'[tool_call] read (fixture)\n\n[error] fixture failure'),
  ('tool_only',[tool_event],{'event':None},'[tool_call] read (fixture)'),
  ('permission_suspension',[tool_event],{'status':'permission_required','suspended_permission':SimpleNamespace(summary='approval',options=[])},None),
  ('callback_final_fallback',[text_event],{},'[assistant]\nfixture receipt')]
 regressions={}
 for name,callbacks,result,expected in cases:
  before=await invoke(native,FixtureService(callbacks,result));after=await invoke(modified,FixtureService(callbacks,result))
  regressions[name]=after==(expected if expected is not None else before)
 report={'scope':'offline-real-trace-component-replay','qualified':all(checks.values()) and all(regressions.values()),'model_calls':0,'new_acp_connections':0,
  'installed_patched':installed_patched,'loaded_module_sha256':sha(loaded),'upstream_sha256':sha(original),'patched_sha256':sha(patched),'patch_sha256':sha(patch_file.read_bytes()),'source_artifact_hashes':hashes,
  'checks':checks,'formatting_fixtures':regressions,'notification_types':dict(counts),'native_callback_types':dict(Counter(x['type'] for x in baseline.callbacks)),
  'measurements':{k:measure(v,receipt) for k,v in [('outer_user',outer_user),('acp_runner_message_arg',args),('acp_prompt_text',acp_text),('inner_user',inner_user),('inner_assistant',inner_final),('acp_agent_message_chunks',chunk_text),('client_finish_prompt',baseline.final['text']),('native_tool_result',tool),('outer_final',outer_final),('patched_replayed_tool_result',patched_text)]},
  'original_model_sends':{'outer':prior['outer_model_calls'],'inner':prior['inner_model_calls']},'original_failed_report_unchanged':sha((source/'report.json').read_bytes())==hashes['report.json']}
 report['qualified']=report['qualified'] and report['original_failed_report_unchanged']
 (out/'report.json').write_text(json.dumps(report,indent=2)+'\n')
 return 0 if report['qualified'] else 1


if __name__=='__main__':
 os.umask(0o077);p=argparse.ArgumentParser();p.add_argument('--source',type=Path,required=True);p.add_argument('--out',type=Path,required=True);p.add_argument('--patch',type=Path,required=True);p.add_argument('--profile',type=Path,required=True);p.add_argument('--installed-patched',action='store_true');a=p.parse_args()
 raise SystemExit(asyncio.run(replay(a.source,a.out,a.patch,a.profile,a.installed_patched)))
