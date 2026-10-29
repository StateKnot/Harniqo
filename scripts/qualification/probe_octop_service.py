#!/usr/bin/env python3
"""Zero-model native service HTTP smoke; run inside an isolated service container."""
import json
from pathlib import Path
import secrets
import time
import urllib.request
import urllib.error

BASE='http://127.0.0.1:10014'
OUT=Path('/evidence')

def request(path, payload=None, token=None):
    headers={'Content-Type':'application/json'}
    if token:headers['Authorization']='Bearer '+token
    req=urllib.request.Request(BASE+path, data=json.dumps(payload).encode() if payload is not None else None, headers=headers)
    try:
        with urllib.request.urlopen(req,timeout=10) as r:return r.status,json.load(r)
    except urllib.error.HTTPError as e:
        return e.code,json.loads(e.read())

def main():
    report=dict(scope='octop-service-zero-model-http',model_calls=0,qualified=False)
    try:
        deadline=time.monotonic()+60
        while True:
            try:
                status,schema=request('/api/openapi.json');break
            except (OSError, urllib.error.URLError):
                if time.monotonic()>deadline:raise
                time.sleep(1)
        if status!=200:raise RuntimeError('OpenAPI unavailable')
        (OUT/'openapi.json').write_text(json.dumps(schema,indent=2))
        paths=schema['paths']
        def find(suffix):
            canonical='/api'+suffix
            if canonical in paths:return canonical
            matches=[p for p in paths if p.endswith(suffix)]
            if len(matches)!=1:raise RuntimeError('ambiguous native route '+suffix)
            return matches[0]
        health_path=find('/health')
        status,health=request(health_path)
        report['health']=health
        report['openapi_version']=schema['openapi']
        if status!=200 or not health['ok']:raise RuntimeError('health failed')
        status,setup=request(find('/setup/status'))
        report['setup_status']={k:v for k,v in setup.items() if k!='wizard_password_path'}
        if status!=200 or not setup['setup_required']:raise RuntimeError('expected fresh service')
        if not setup['database_bound']:
            status,result=request(find('/setup/database'),{'driver':'sqlite'})
            if status!=200 or not result['ok']:raise RuntimeError('native SQLite binding failed')
        password=(Path('/data/octop')/'octop-login.txt').read_text().strip()
        status,wizard=request(find('/setup/verify-password'),{'password':password})
        if status!=200:raise RuntimeError('native wizard auth failed')
        token=wizard['wizard_token']
        status,result=request(find('/setup/initial-admin'),{'username':'qualification-admin','password':secrets.token_urlsafe(32),'locale':'en'},token)
        report['initial_admin_status']=status
        if status!=201:raise RuntimeError('native admin creation failed')
        # Never publish access tokens. Login/finish can be tested separately with retained state.
        access=result.get('access_token') or result.get('token')
        if not access:raise RuntimeError('initial admin token missing')
        status,_=request(find('/providers'))
        report['provider_without_token_status']=status
        if status not in (401,403):raise RuntimeError('provider without token not rejected')
        status,_=request(find('/providers'),token='invalid-qualification-token')
        report['provider_bad_token_status']=status
        if status not in (401,403):raise RuntimeError('provider invalid token not rejected')
        status,providers=request(find('/providers'),token=access)
        report['authorized_provider_status']=status
        report['provider_count']=len(providers) if isinstance(providers,list) else None
        if status!=200:raise RuntimeError('authorized provider query failed')
        status,final=request(health_path)
        report['final_health']=final
        report['qualified']=status==200 and final['db'] and final['users_loaded']==1
    except Exception as e:
        report['error_type']=type(e).__name__
        report['error']=str(e)
    (OUT/'service-probe.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report))
    return 0 if report['qualified'] else 1

if __name__=='__main__':raise SystemExit(main())
