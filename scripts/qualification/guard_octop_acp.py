#!/usr/bin/env python3
"""Outer HTTP and delegation argument boundaries; no model/ACP replacement."""
import hashlib
import json
from urllib.parse import urlsplit

DELEGATED_PROMPT='Read input.txt using the read tool. Reply with only its exact text.'

def permitted_call(call):
 args=call.get('args',{})
 return (call.get('name')=='acp_runner' and set(args)<={'action','runner','message','cwd','max_runtime'} and
  args.get('action')=='start' and args.get('runner')=='opencode' and args.get('message')==DELEGATED_PROMPT and
  args.get('cwd')=='/evidence/workspace' and type(args.get('max_runtime')) in {int,float} and 0<args['max_runtime']<=90)

class OuterBudget:
 def __init__(self,config,save,*,max_output_tokens=128):
  if type(max_output_tokens) is not int or max_output_tokens not in {128,512}:raise ValueError('unapproved outer output budget')
  self.max_output_tokens=max_output_tokens
  url=urlsplit(config['base_url'])
  if config['model']!='qwen3.8-flash' or url.scheme!='https' or not url.hostname or url.username or url.password or url.query or url.fragment:raise ValueError('invalid approved model endpoint')
  self.target=config['base_url'].rstrip('/')+'/chat/completions';self.model=config['model'];self.save=save
  self.calls=[];self.auth_failed=False
 def admit(self,url,content):
  body=json.loads(content);tokens=body.get('max_completion_tokens',body.get('max_tokens'))
  if (self.auth_failed or url!=self.target or body.get('model')!=self.model or len(self.calls)>=4 or
      type(tokens) is not int or not 1<=tokens<=self.max_output_tokens):raise RuntimeError('outer model budget rejected')
  row={'model':self.model,'target_sha256':hashlib.sha256(url.encode()).hexdigest(),'request_sha256':hashlib.sha256(content).hexdigest(),'max_output_tokens':tokens,'status':None}
  if any(r['request_sha256']==row['request_sha256'] for r in self.calls):raise RuntimeError('outer duplicate send rejected')
  self.calls.append(row);self.save('outer-http.json',{'calls':self.calls,'auth_failed':self.auth_failed})
  return row
 def completed(self,row,status):
  row['status']=status;self.auth_failed=self.auth_failed or status in {401,403}
  self.save('outer-http.json',{'calls':self.calls,'auth_failed':self.auth_failed})
