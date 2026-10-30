#!/usr/bin/env python3
"""Run one actual DSH SDK case. Does not retry an accepted/unknown prompt."""
import argparse
import json
import os
from pathlib import Path
import secrets
import shutil
import hashlib
import signal
import subprocess
import time
from prepare_dsh import COMMIT, VERSION
from probe_hermes import no_links, private_directory, read_file, json_value, require, write_report, Rejected
from run_hermes_qualification import docker, inspect, owned, cleanup, select_endpoint, create_private, TASK_LABEL
from dsh_support import (local_docker_environment, validate_image, QUALIFIED_IMAGE,
  PROBE_SOURCE, OBSERVER_SOURCE, QUALIFICATION_PROFILE, endpoint_configuration,
  qualification_binding, validate_admission)

def execute(args):
    with local_docker_environment():return _execute(args)

def _execute(args):
    root,case=no_links(args.root),no_links(args.case)
    require(os.geteuid()!=0,'non_root_operator_required')
    require(shutil.disk_usage(root).free>=50*1024**3,'disk_space_threshold')
    image=json_value(read_file(no_links(args.provenance),65536,private=False))
    require(image['source_commit']==COMMIT and image['version']==VERSION,'provenance_mismatch')
    image_id=image['image_id']
    require(image_id==QUALIFIED_IMAGE,'pinned_image_mismatch')
    sources={p:read_file(no_links(root/p),262144,private=False) for p in (PROBE_SOURCE,OBSERVER_SOURCE)}
    model=endpoint_configuration(no_links(args.config))
    binding=qualification_binding(image_id,model,sources)
    admission=None
    if args.mode=='smoke':
      # This gate precedes credential resolution, key mounts and dispatch.
      admission=validate_admission(getattr(args,'admission_report',None),binding,model)
    installed=validate_image(image_id,expected=QUALIFIED_IMAGE)
    require(installed['Config']['Labels']['io.harniqo.qualification.dsh.source']==COMMIT,'image_source_mismatch')
    key=None
    if args.mode=='smoke':
      selected,key=select_endpoint(no_links(args.config),'secondary')
      require(selected==model,'endpoint_changed_after_admission')
    require(not case.exists(),'case_already_exists')
    private_directory(case.parent,create=True);case.mkdir(mode=0o700)
    for directory in ('home','workspace','evidence'):(case/directory).mkdir(mode=0o700)
    (case/'native-sources').mkdir(mode=0o700)
    for source,data in sources.items():
      snapshot=case/'native-sources'/Path(source).name
      create_private(snapshot,data);snapshot.chmod(0o400)
    create_private(case/'model.json',json.dumps(model).encode())
    attempt=secrets.token_hex(16);name='harniqo-dsh-'+attempt
    report={'schema_version':1,'source_commit':COMMIT,'version':VERSION,'image_id':image_id,'mode':args.mode,
      'attempt_id':attempt,'status':'running','native_case_passed':False,'production_isolation_passed':False,
      'scope':'trusted_native_qualification',**{name:QUALIFICATION_PROFILE[name] for name in
        ('max_http_requests','max_output_tokens','execution_deadline_seconds')},'admission_binding':binding}
    if admission is not None:report['admission']=admission
    report['source_sha256']={p:hashlib.sha256((root/p).read_bytes()).hexdigest() for p in (
      'scripts/qualification/run_dsh.py','scripts/qualification/probe_dsh.mjs',
      'scripts/qualification/test_dsh_transport.mjs','deploy/qualification/dsh/transport_audit.mjs')}
    report['source_sha256'].update({name:hashlib.sha256(data).hexdigest() for name,data in sources.items()})
    write_report(case/'operator.json',report)
    command=['run','--detach','--pull','never','--name',name,'--label',TASK_LABEL+'='+attempt,
      '--label','io.harniqo.task=m0-dsh-native-qualification','--user',f'{os.getuid()}:{os.getgid()}',
      '--init','--read-only','--cap-drop','ALL','--security-opt','no-new-privileges','--no-healthcheck',
      '--pids-limit','128','--memory','4g','--cpus','2','--log-driver','local','--log-opt','max-size=10m','--log-opt','max-file=2',
      '--tmpfs','/tmp:rw,noexec,nosuid,nodev,size=256m,mode=1777','--workdir','/data/workspace',
      '--entrypoint','/usr/local/bin/node']
    if args.mode!='smoke':command+=['--network','none']
    mounts=[(case/'home','/data/home',False),(case/'workspace','/data/workspace',False),(case/'evidence','/data/evidence',False),
      (case/'model.json','/run/config/model.json',True),
      (case/'native-sources'/Path(OBSERVER_SOURCE).name,'/opt/qualification/transport_audit.mjs',True),
      (case/'native-sources'/Path(PROBE_SOURCE).name,'/opt/qualification/probe_dsh.mjs',True)]
    mounts.append((root/'scripts/qualification/test_dsh_transport.mjs','/opt/qualification/test_dsh_transport.mjs',True))
    if args.mode=='smoke':mounts.append((key,'/run/secrets/model-api-key',True))
    for source,target,ro in mounts:
      require(',' not in str(source) and '\n' not in str(source),'unsafe_mount')
      command+=['--mount',f'type=bind,src={source},dst={target}'+(',readonly' if ro else '')]
    command+=[image_id,'/opt/qualification/'+('test_dsh_transport.mjs' if args.mode=='component' else 'probe_dsh.mjs')]
    if args.mode!='component':command.append(args.mode)
    identity=None
    try:
      docker(*command,timeout=30)
      value=inspect(name);identity=owned(value,attempt,image_id);report['container_id']=identity
      report['started_monotonic']=time.monotonic()
      # A detached docker wait has a deadline; cleanup then stops the actual
      # container and verifies PID 0, including an uncertain launch response.
      result=docker('wait',identity,timeout=QUALIFICATION_PROFILE['execution_deadline_seconds'])
      require(result.stdout.strip()==b'0','native_probe_failed')
      probe=json_value(read_file(case/'evidence/probe.json',65536))
      require(probe.get('status')=='passed','probe_status_failed')
      report.update(status='passed',native_case_passed=probe['native_case_passed'],native_preflight_passed=probe['native_preflight_passed'])
    except BaseException as error:
      report.update(status='failed',failure={'code':getattr(error,'code',type(error).__name__)})
    finally:
      try:
        report['cleanup']=cleanup(name,attempt,image_id,identity,case)
        require(report['cleanup'].get('execution_stop_confirmed') and report['cleanup'].get('logs_complete'),'cleanup_not_confirmed')
      except BaseException as error:report.update(status='failed',cleanup_failure={'code':getattr(error,'code',type(error).__name__)})
      write_report(case/'operator.json',report)
    print(json.dumps({'status':report['status'],'native_case_passed':report['native_case_passed'],
      'cleanup_confirmed':report.get('cleanup',{}).get('execution_stop_confirmed',False),'case':str(case.relative_to(root))}))
    return 0 if report['status']=='passed' else 1

if __name__=='__main__':
    os.umask(0o077)
    def stop(signum,_):raise Rejected('operator_signal_'+signal.Signals(signum).name.lower())
    for signum in (signal.SIGTERM,signal.SIGHUP):signal.signal(signum,stop)
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('root','case','config','provenance'):parser.add_argument('--'+name,type=Path,required=True)
    parser.add_argument('--mode',choices=('component','preflight','transport','smoke'),required=True)
    parser.add_argument('--admission-report',type=Path,help='Successful bound native transport operator.json; required for smoke')
    try:result=execute(parser.parse_args())
    except (Rejected,ValueError,OSError) as error:
      print(json.dumps({'status':'rejected','code':getattr(error,'code',type(error).__name__)}));result=2
    raise SystemExit(result)
