#!/usr/bin/env python3
"""One native service WS turn and REST history read; no direct model invocation."""
import asyncio
import hashlib
import json
from pathlib import Path
import secrets
import time
import urllib.request
import urllib.error

BASE = 'http://127.0.0.1:10014/api'
OUT = Path('/evidence')

def request(path, payload=None, token=None, method=None):
    headers = {'Content-Type':'application/json'}
    if token: headers['Authorization'] = 'Bearer '+token
    req = urllib.request.Request(BASE+path, data=json.dumps(payload).encode() if payload is not None else None,
                                 headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=40) as response:
            data = response.read()
            return response.status, json.loads(data) if data else None
    except urllib.error.HTTPError as error:
        return error.code, None

def require(result, status):
    if result[0] != status: raise RuntimeError('native HTTP status '+str(result[0]))
    return result[1]

def response_models(value):
    """Read original provider metadata from native WS snapshots and updates."""
    models = set()
    if isinstance(value, dict):
        metadata = value.get('response_metadata', {})
        if isinstance(metadata, dict):
            model = metadata.get('model_name') or metadata.get('model')
            if isinstance(model, str) and model: models.add(model)
        for item in value.values(): models.update(response_models(item))
    elif isinstance(value, list):
        for item in value: models.update(response_models(item))
    return models

async def main():
    report = dict(scope='octop-service-native-ws-echo-history', qualified=False, model_calls=0)
    events = []
    try:
        deadline = time.monotonic()+60
        while True:
            try:
                require(request('/health'), 200)
                break
            except OSError:
                if time.monotonic()>deadline: raise
                await asyncio.sleep(1)
        setup = require(request('/setup/status'), 200)
        if not setup['database_bound']: require(request('/setup/database', {'driver':'sqlite'}), 200)
        password = Path('/data/octop/octop-login.txt').read_text().strip()
        wizard = require(request('/setup/verify-password', {'password':password}), 200)['wizard_token']
        admin = require(request('/setup/initial-admin', {'username':'qualification-admin',
            'password':secrets.token_urlsafe(32),'locale':'en'}, wizard), 201)
        access = admin.get('access_token') or admin.get('token')
        cfg = json.loads(Path('/run/model.json').read_text())
        provider = {'name':'qualification','kind':'openai','base_url':cfg['base_url'],
                    'api_key':Path('/run/model-key').read_text().strip(),
                    'models':[{'id':cfg['model'],'name':cfg['model'],'api_mode':'chat_completions'}]}
        require(request('/admin/providers', provider, access), 201)
        agent = require(request('/agents', {'name':'Qualification echo','default_model':'qualification/'+cfg['model'],
            'system_prompt':'Return only the exact receipt requested by the user. Do not use tools.',
            # max_iters counts LangGraph nodes, not model sends. The wrapper
            # independently enforces the approved four actual-request ceiling.
            'max_iters':32,'max_tokens':128,'temperature':0,
            'mcp_servers':[],'knowledge_base_ids':[],'skill_package_ids':[],
            'config':{'backend':{'type':'filesystem','root_dir':'/evidence/workspace'},
                      'workspace_dir':'/','memory':{'memory_enabled':False}}}, access), 201)
        aid = agent['agent_id']
        prefix = '/agents/'+aid
        require(request(prefix+'/workspace/file?path=.bootstrapped', {'content':'qualification'}, access, 'PUT'), 200)
        catalog = require(request(prefix+'/tool-settings', token=access), 200)['tools']
        plugins = {}
        for tool in catalog:
            if tool['source']=='plugin':
                plugins.setdefault(tool['plugin_id'], {'enabled':False,'tools':{}})['tools'][tool['name']]={'enabled':False}
        require(request(prefix+'/tool-settings', {'disabled_builtin':[t['name'] for t in catalog if t['source']=='builtin' and t['disableable']],
                                                 'plugins':plugins}, access, 'PUT'), 200)
        require(request(prefix+'/reload', {}, access), 204)
        challenge = (OUT/'challenge.txt').read_text()
        import websockets
        async with websockets.connect('ws://127.0.0.1:10014/api'+prefix+'/chat/ws?token='+access,
                                      open_timeout=10, max_size=4*1024*1024) as ws:
            await ws.send(json.dumps({'type':'user_turn','text':'Reply exactly '+challenge+' and nothing else. Do not call tools.',
                'default_model':'qualification/'+cfg['model'],'skills':[],'mcp_servers':[],'knowledge_base_ids':[],
                'conversation_mode':'ask','reasoning_mode':'disabled'}))
            async with asyncio.timeout(120):
                while True:
                    event = json.loads(await ws.recv())
                    events.append(event)
                    (OUT/'ws-events.json').write_text(json.dumps(events,ensure_ascii=False,indent=2))
                    if event.get('type') in {'done','turn.ended'}: break
        threads = {e['thread_id'] for e in events if e.get('thread_id')}
        if len(threads)!=1: raise RuntimeError('native thread identity missing or ambiguous')
        tid = threads.pop()
        report['thread_id']=tid
        history = None
        for _ in range(20):
            history = require(request(prefix+'/threads/'+tid+'/history', token=access),200)
            if not history.get('history_loading'): break
            await asyncio.sleep(0.25)
        (OUT/'service-history.json').write_text(json.dumps(history,ensure_ascii=False,indent=2))
        messages = history['messages']
        assistant = [m for m in messages if m.get('role')=='assistant']
        text = ''.join(block.get('text','') for m in assistant for block in m.get('content',[]) if block.get('type')=='text')
        report.update(history_count=len(messages),assistant_count=len(assistant),response_matches=text==challenge,
            response_sha256=hashlib.sha256(text.encode()).hexdigest(),event_count=len(events),
            no_error_events=not any(e.get('type')=='error' for e in events),turn_inactive=history.get('turn_active') is False,
            no_tool_history=not any(m.get('role')=='tool' or any(b.get('type')=='tool_use' for b in m.get('content',[])) for m in messages))
        calls = json.loads((OUT/'model-calls.json').read_text())
        report['model_calls']=len(calls['calls'])
        report['request_statuses']=[c['status'] for c in calls['calls']]
        report['served_model_matches']=response_models(events)=={cfg['model']}
        report['qualified']=(len(messages)==2 and len(assistant)==1 and report['response_matches'] and
            report['no_error_events'] and report['turn_inactive'] and report['no_tool_history'] and
            report['served_model_matches'] and
            1<=len(calls['calls'])<=4 and all(c['status']==200 for c in calls['calls']) and not calls['auth_failed'])
    except Exception as error:
        report['error_type']=type(error).__name__
        report['error']=str(error)
    finally:
        (OUT/'service-probe.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps({k:v for k,v in report.items() if k!='error'}))
    return 0 if report['qualified'] else 1

if __name__=='__main__': raise SystemExit(asyncio.run(main()))
