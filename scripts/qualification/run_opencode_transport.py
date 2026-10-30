#!/usr/bin/env python3
"""Native SDK fetch observation: offline first, then one explicit model task."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import subprocess
import sys
import time

from probe_hermes import Rejected, no_links, private_directory, read_file, require, write_report
from run_hermes_qualification import cleanup, create_private, docker, inspect, owned, port_available, select_endpoint
from run_opencode_qualification import IMAGE, COMMIT, PORT, launch, ready
from probe_opencode import Client, private_bytes

FILES = ('deploy/qualification/opencode/transport_entrypoint.py',
         'deploy/qualification/opencode/transport_plugin.mjs',
         'deploy/qualification/opencode/transport_audit.mjs',
         'deploy/qualification/opencode/transport_selftest_plugin.mjs',
         'scripts/qualification/test_opencode_transport.mjs',
         'scripts/qualification/probe_opencode.py')
sha = lambda data: hashlib.sha256(data).hexdigest()


def evidence(records, base, model, maximum=6):
    starts = [r for r in records if r.get('kind') == 'request_started']
    ends = [r for r in records if r.get('kind') == 'response']
    endpoint = sha((base.rstrip('/') + '/chat/completions').encode())
    expected = sha(model.encode())
    ids = [r.get('request_id') for r in starts]
    valid = (1 <= len(starts) <= maximum and len(starts) == len(ends)
             and all(isinstance(i, str) and i for i in ids) and len(set(ids)) == len(ids)
             and all(r.get('schema') == 1 and r.get('kind') in {'request_started', 'response'} for r in records)
             and all(r.get('url_sha256') == endpoint and r.get('model_known') is True
                     and r.get('model_sha256') == expected and r.get('model_matches_expected') is True
                     and sum(end.get('request_id') == r['request_id'] for end in ends) == 1 for r in starts)
             and all(r.get('request_id') in ids and r.get('status') == 200 and r.get('redirected') is False
                     and r.get('response_url_sha256') == endpoint and r.get('network_error') is False for r in ends))
    return {'verified': valid, 'sdk_fetch_sends': len(starts), 'paired_responses': len(ends),
            'actual_request_url_sha256': endpoint, 'request_model_sha256': expected,
            'wire_request_hard_cap': False, 'provider_weights_verified': False}


LOCAL_CONTROL = r'''
import base64,json,time,urllib.request
from pathlib import Path
password=Path('/run/secrets/api-password').read_text().strip()
headers={'Authorization':'Basic '+base64.b64encode(('opencode:'+password).encode()).decode(),
         'x-opencode-directory':'/data/workspace','Content-Type':'application/json'}
opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
def request(route,data=None):
 raw=None if data is None else json.dumps(data).encode()
 with opener.open(urllib.request.Request('http://127.0.0.1:4096'+route,data=raw,headers=headers),timeout=3) as r:
  body=r.read();return r.status,json.loads(body) if body else None
deadline=time.monotonic()+35
while True:
 try:
  assert request('/global/health')[1]=={'healthy':True,'version':'1.19.5'};break
 except Exception:
  if time.monotonic()>deadline:raise
  time.sleep(.2)
status,_=request('/provider');assert status==200
print(json.dumps({'native_health':True,'native_provider_resolution':True}))
'''

OFFLINE_SEND = LOCAL_CONTROL + r'''
evidence=Path('/data/evidence');model=json.loads(Path('/run/config/model.json').read_text())['model']
status,session=request('/session',{'title':'Harniqo offline native fetch seam'})
assert status==200 and isinstance(session.get('id'),str)
session_id=session['id']
(evidence/'offline-submission.json').write_text(json.dumps({'session_id':session_id,'scope':'network-none closed-loopback negative transport; no model service'})+'\n')
status,_=request('/session/'+session_id+'/prompt_async',{'model':{'providerID':'harniqo','modelID':model},'parts':[{'type':'text','text':'Reply OK without tools.'}]})
assert status==204
deadline=time.monotonic()+20
try:
 while True:
  records=[json.loads(line) for line in (evidence/'transport.jsonl').read_text().splitlines()]
  if any(row['kind']=='network_error' for row in records):break
  if time.monotonic()>deadline:raise RuntimeError('native_sdk_transport_not_observed')
  time.sleep(.05)
finally:
 request('/session/'+session_id+'/abort',{})
status,history=request('/session/'+session_id+'/message')
(evidence/'offline-native-history.json').write_text(json.dumps(history)+'\n')
print(json.dumps({'native_sdk_negative_dispatch':True,'external_model_calls':0}))
'''


def run(args):
    require(sys.platform == 'linux' and os.geteuid() != 0, 'non_root_linux_required')
    root, case = no_links(args.root), no_links(args.case)
    subprocess.run(['df', '-h', str(root)], check=True)
    require(shutil.disk_usage(root).free >= 10*1024**3, 'insufficient_disk')
    provenance = json.loads(read_file(no_links(args.provenance), 1024*1024, private=False))
    require(provenance.get('image_id') == IMAGE and provenance.get('commit') == COMMIT, 'image_identity')
    require(provenance['entrypoint_sha256'] == sha((root/'deploy/qualification/opencode/entrypoint.py').read_bytes())
            and provenance['recipe_sha256'] == sha((root/'deploy/qualification/opencode/Dockerfile').read_bytes()), 'immutable_profile_changed')
    installed = json.loads(docker('image', 'inspect', '--format', '{{json .Config.Labels}}', IMAGE).stdout)
    require(installed.get('org.opencontainers.image.revision') == COMMIT, 'installed_source_identity')
    require(not case.exists(), 'case_exists');private_directory(case.parent, create=True)
    locks = private_directory(root/'.private/qualification/locks', create=True)
    descriptor = os.open(locks/'opencode-port-10012', os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'a+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX|fcntl.LOCK_NB);require(port_available(PORT), 'port_busy')
        case.mkdir(mode=0o700)
        for name in ('home', 'workspace', 'evidence'): (case/name).mkdir(mode=0o700)
        if args.mode == 'smoke':
            require(args.config is not None and args.endpoint_id == 'secondary', 'explicit_approved_qwen_endpoint_required')
            model, key = select_endpoint(no_links(args.config), args.endpoint_id)
            require(model['model'] == 'qwen3.8-flash', 'unapproved_model')
        else:
            require(args.config is None and args.endpoint_id is None, 'offline_mode_rejects_credentials')
            model={'base_url':'https://127.0.0.1:10080/v1','model':'offline-qualified-model'}
            key=case/'synthetic-key';create_private(key,b'synthetic-offline-key')
        token=secrets.token_urlsafe(48)
        create_private(case/'api-password',token.encode());create_private(case/'model.json',json.dumps(model).encode())
        attempt=secrets.token_hex(16);name='harniqo-opencode-transport-'+attempt
        sources=(*FILES,'scripts/qualification/run_opencode_transport.py','scripts/qualification/run_hermes_qualification.py',
                 'scripts/qualification/run_opencode_qualification.py','scripts/qualification/probe_hermes.py')
        report={'scope':'native_opencode_instrumented_provider_fetch_qualification','mode':args.mode,
                'image_id':IMAGE,'source_commit':COMMIT,'status':'running','native_case_passed':False,
                'real_model_task_attempted':False,'source_sha256':{p:sha((root/p).read_bytes()) for p in sources},
                'sdk_fetch_send_limit':1 if args.mode=='offline-send' else 6,'wire_or_billing_hard_cap':False}
        write_report(case/'operator.json',report);identity=None
        try:
            argv=launch(name,attempt,case,case,key)[:-1]
            if args.mode!='smoke':
                at=argv.index('--publish');del argv[at:at+2];argv+=['--network','none']
            argv+=['--entrypoint','python3','--mount',f'type=bind,src={case}/evidence,dst=/data/evidence']
            for source in FILES:
                argv+=['--mount',f'type=bind,src={root/source},dst=/opt/harniqo/{source},readonly']
            docker(*argv,IMAGE,'/opt/harniqo/deploy/qualification/opencode/transport_entrypoint.py',args.mode)
            value=inspect(name);identity=owned(value,attempt,IMAGE);report['container_id']=identity
            if args.mode=='smoke':
                require(value.get('ports')=={'4096/tcp':[{'HostIp':'127.0.0.1','HostPort':'10012'}]},'port_mapping')
            else: require(not any((value.get('ports') or {}).values()),'unexpected_port_mapping')
            control=docker('exec',identity,'python3','-c',LOCAL_CONTROL,timeout=45)
            report['native_startup']=json.loads(control.stdout)
            ready_path=case/'evidence/transport-ready.json'
            require(ready_path.exists(),'native_fetch_observer_not_installed')
            receipt=json.loads(read_file(ready_path,65536));report['observer_ready']=receipt
            require(receipt['model_sha256']==sha(model['model'].encode()) and receipt['bun_version']
                    and receipt['max_sdk_fetch_sends']==report['sdk_fetch_send_limit'],'observer_identity')
            if args.mode=='components':
                components=json.loads(read_file(case/'evidence/transport-components.json',65536))
                report['components']=components
                require(components['tests']==21 and components['passed']==21 and components['failed']==0
                        and components['bun_version']==receipt['bun_version'],'native_bun_components_failed')
                require((case/'evidence/transport.jsonl').stat().st_size==0,'unexpected_model_request')
            elif args.mode=='smoke':
                started=time.monotonic();report['real_model_task_attempted']=True;write_report(case/'operator.json',report)
                command=[sys.executable,str(root/'scripts/qualification/probe_opencode.py'),
                         '--api-password',str(case/'api-password'),'--model-config',str(case/'model.json'),
                         '--workspace',str(case/'workspace'),'--evidence-dir',str(case/'probe')]
                with (case/'probe-process.log').open('xb') as out:
                    result=subprocess.run(command,stdin=subprocess.DEVNULL,stdout=out,stderr=subprocess.STDOUT,timeout=110)
                report['model_task_elapsed_seconds']=time.monotonic()-started
                native=json.loads(private_bytes(case/'probe/report.json',4*1024**2))
                require(result.returncode==0 and native.get('native_smoke_passed') is True,'native_smoke_failed')
                records=[json.loads(x) for x in read_file(case/'evidence/transport.jsonl',256*1024).decode().splitlines()]
                report['transport']=evidence(records,model['base_url'],model['model'])
                require(report['transport']['verified'],'actual_model_transport_unproven')
                report['native_case_passed']=True
            elif args.mode=='offline-send':
                docker('exec',identity,'python3','-c',OFFLINE_SEND,timeout=30)
                records=[json.loads(x) for x in read_file(case/'evidence/transport.jsonl',256*1024).decode().splitlines()]
                starts=[r for r in records if r.get('kind')=='request_started']
                ends=[r for r in records if r.get('kind')=='network_error']
                expected_url=sha((model['base_url']+'/chat/completions').encode())
                require(len(starts)==1 and len(ends)==1 and ends[0]['request_id']==starts[0]['request_id']
                        and starts[0]['model_sha256']==sha(model['model'].encode())
                        and starts[0]['url_sha256']==expected_url,'native_sdk_negative_dispatch_not_proven')
                report['actual_native_sdk_fetch_observed']=True
                report['negative_transport_scope']='network-none closed-loopback; no model service or response'
            report['status']='passed'
        except BaseException as error:
            report.update(status='failed',failure_code=error.code if isinstance(error,Rejected) else type(error).__name__)
        finally:
            try:
                report['cleanup']=cleanup(name,attempt,IMAGE,identity,case)
                require(report['cleanup'].get('execution_stop_confirmed') is True
                        and report['cleanup'].get('logs_complete') is True
                        and report['cleanup'].get('container_removed') is True,'cleanup_unconfirmed')
                require(port_available(PORT),'port_not_released');report['port_released']=True
            except BaseException as error:
                report.update(status='failed',cleanup_failure=getattr(error,'code',type(error).__name__))
            write_report(case/'operator.json',report)
        print(json.dumps({'status':report['status'],'mode':args.mode,'native_case_passed':report['native_case_passed'],
                          'real_model_task_attempted':report['real_model_task_attempted'],'report':str(case/'operator.json')}))
        return 0 if report['status']=='passed' else 1


def main():
    os.umask(0o077)
    def stop(signum,_): raise Rejected('operator_signal_'+str(signum))
    for sig in (signal.SIGTERM,signal.SIGHUP): signal.signal(sig,stop)
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('root','case','provenance'):parser.add_argument('--'+name,type=Path,required=True)
    parser.add_argument('--mode',choices=('baseline','components','offline-send','smoke'),required=True)
    parser.add_argument('--config',type=Path);parser.add_argument('--endpoint-id')
    return run(parser.parse_args())

if __name__=='__main__':raise SystemExit(main())
