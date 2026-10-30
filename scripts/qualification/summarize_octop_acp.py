#!/usr/bin/env python3
"""Append a sanitized, offline supplement; never modify original verdicts."""
import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import time
import uuid
from probe_hermes import no_links,private_directory,read_file


def digest(data):return hashlib.sha256(data).hexdigest()
def text_parts(message):return ''.join(x.get('text','') for x in message.get('parts',[]) if x.get('type')=='text')
def measured(text,receipt):return {'type':type(text).__name__,'length':len(text) if isinstance(text,str) else None,'receipt_exact':text==receipt,'receipt_contains':isinstance(text,str) and receipt in text,'receipt_prefix':isinstance(text,str) and bool(text) and receipt.startswith(text)}
def token_usage(message):
 usage=message.get('usage_metadata') or {};details=usage.get('output_token_details') or {}
 fields={k:usage.get(k) for k in ['input_tokens','output_tokens','total_tokens']};fields['reasoning_tokens']=details.get('reasoning')
 return {k:v for k,v in fields.items() if type(v) is int and v>=0}


def summarize(source):
 files={n:read_file(source/n,64*1024**2) for n in ['report.json','operator-report.json','outer-input.json','outer-tool-actions.json','outer-history.json','inner-history.json','acp-observed.json','outer-http.json','inner-audit-summary.json']}
 data={n:json.loads(v) for n,v in files.items()};private_directory(source/'workspace');challenge=no_links(source/'workspace/input.txt')
 if challenge.stat().st_uid!=os.geteuid():raise ValueError('challenge owner mismatch')
 receipt=read_file(challenge,1024,private=False).decode();native=data['report.json'];operator=data['operator-report.json']
 if native['scope'] not in ['octop-harness-acp-delegated','octop-harness-acp-delegated-patched','octop-harness-acp-delegated-patched-outer512']:raise ValueError('unsupported native scope')
 events=data['acp-observed.json'];updates=[e['kwargs']['update'] for e in events if e.get('method')=='session/update'];outer=data['outer-history.json'];inner=data['inner-history.json']
 outer_ai=[m for m in outer if m.get('type')=='ai'];tool=[m for m in outer if m.get('type')=='tool'];inner_ai=[m for m in inner if m.get('info',{}).get('role')=='assistant'];inner_user=[m for m in inner if m.get('info',{}).get('role')=='user']
 user=[m for m in outer if m.get('type')=='human'][0]['content'];args=data['outer-tool-actions.json'][0]['args']['message'];prompts=[e['kwargs']['prompt'] for e in events if e.get('direction')=='outer-request' and e.get('method')=='prompt']
 acp_text=''.join(p.get('text','') for p in prompts[0] if p.get('type')=='text');chunks=''.join(u['content'].get('text','') for u in updates if u.get('sessionUpdate')=='agent_message_chunk')
 inner_final=text_parts(inner_ai[-1]);inner_input=text_parts(inner_user[0]);tool_text=tool[0]['content'];final=outer_ai[-1]['content'];terminal=outer_ai[-1].get('response_metadata',{}).get('finish_reason')
 safe_reason=terminal if terminal in ['stop','length','tool_calls','content_filter',None] else 'other'
 values={'outer_user':user,'tool_message_arg':args,'acp_prompt_text':acp_text,'inner_user':inner_input,'inner_final':inner_final,'acp_chunks':chunks,'outer_tool_result':tool_text,'outer_final':final}
 chains={'outer_input_matches_native_user':user==data['outer-input.json']['prompt'],'outer_user_contains_delegate_arg':args in user,'args_acp_inner_user_exact':args==acp_text==inner_input,'inner_and_acp_receipt_exact':inner_final==chunks==receipt,'native_tool_contains_receipt':receipt in tool_text,'outer_receipt_exact':final==receipt}
 calls=data['outer-http.json']['calls'];inner_audit=data['inner-audit-summary.json'];sends=[r for r in inner_audit if r['kind']=='request_started'];responses=[r for r in inner_audit if r['kind']=='response']
 request_ids=[r['request_id'] for r in sends];response_ids=[r['request_id'] for r in responses]
 return {'scope':'retained-native-acp-offline-supplement','new_model_calls':0,'source_scope':native['scope'],'original_native_qualified':native['qualified'],'original_operator_qualified':operator['qualified'],
  'source_sha256':{n:digest(v) for n,v in files.items()},'receipt_sha256_matches':digest(receipt.encode())==native['receipt_sha256'],'measurements':{k:measured(v,receipt) for k,v in values.items()},
  'chain_checks':chains,'notification_types':dict(Counter(u['sessionUpdate'] for u in updates)),'outer_terminal_reason':safe_reason,'outer_usage':[token_usage(m) for m in outer_ai],
  'outer_visible_receipt_prefix_truncated':terminal=='length' and bool(final) and final!=receipt and receipt.startswith(final),
  'original_requests':{'outer':len(calls),'inner':len(sends),'outer_all_200':all(x['status']==200 for x in calls),'inner_response_count':len(responses),'inner_unique_request_ids':len(set(request_ids))==len(request_ids),'inner_paired_request_ids':sorted(request_ids)==sorted(response_ids),'inner_all_200':all(x['status']==200 for x in responses)},
  'loaded_acp_runner_sha256':native.get('loaded_acp_runner_sha256'),'patch_sha256':native.get('patch_sha256'),'cleanup':{k:operator.get(k) for k in ['pid_zero','removed','exit_code','cleanup_errors']},
  'limitations':['offline_analysis_only','original_failed_verdict_unchanged','no_production_isolation_claim','no_complete_service_acp_claim']}


def main():
 os.umask(0o077);p=argparse.ArgumentParser();p.add_argument('--root',type=Path,required=True);p.add_argument('--source-attempt',type=Path,required=True);a=p.parse_args();root=no_links(a.root)
 parent=private_directory(root/'.private/qualification/octop-acp');source=private_directory(a.source_attempt);source.relative_to(parent)
 report=summarize(source);stamp=time.strftime('%Y%m%dT%H%M%SZ',time.gmtime())+'-'+uuid.uuid4().hex[:8];attempt=parent/('analysis-'+stamp);attempt.mkdir(mode=0o700)
 script=Path(__file__).read_bytes();(attempt/'summarize.py').write_bytes(script);report['script_sha256']=digest(script);report['source_attempt']=source.name
 report['original_reports_unchanged']=all(digest(read_file(source/n,64*1024**2))==h for n,h in report['source_sha256'].items())
 (attempt/'report.json').write_text(json.dumps(report,indent=2)+'\n');print(json.dumps({'report':str(attempt.relative_to(root)/'report.json'),'report_sha256':digest((attempt/'report.json').read_bytes()),'original_reports_unchanged':report['original_reports_unchanged'],'chain_checks':report['chain_checks'],'outer_terminal_reason':report['outer_terminal_reason'],'outer_visible_receipt_prefix_truncated':report['outer_visible_receipt_prefix_truncated']}))

if __name__=='__main__':main()
