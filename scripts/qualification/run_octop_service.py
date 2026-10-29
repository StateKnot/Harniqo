#!/usr/bin/env python3
"""Start the immutable complete service without network/model secrets; probe and stop."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import time
import uuid
from run_octop import command,docker_args,docker_env,finalize_container,save_operator_report,operator_passes,private_file


def main():
 p=argparse.ArgumentParser();p.add_argument('--root',type=Path,required=True);p.add_argument('--image',required=True);p.add_argument('--model',action='store_true');a=p.parse_args()
 if not re.fullmatch(r'sha256:[a-f0-9]{64}',a.image):raise ValueError('immutable image required')
 root=a.root.resolve();print(command(['df','-h',str(root)]).stdout,end='')
 if shutil.disk_usage(root).free<50*1024**3:raise ValueError('50GiB required')
 stamp=time.strftime('%Y%m%dT%H%M%SZ',time.gmtime())+'-'+uuid.uuid4().hex[:8]
 attempt=root/'.private/qualification/octop'/(('service-model-' if a.model else 'service-zero-')+stamp);attempt.mkdir(mode=0o700)
 home=attempt/'home';home.mkdir(mode=0o700)
 probe=attempt/'probe.py';shutil.copyfile(root/'scripts/qualification'/('probe_octop_service_model.py' if a.model else 'probe_octop_service.py'),probe);probe.chmod(0o400)
 shutil.copyfile(Path(__file__),attempt/'operator.py')
 shutil.copyfile(root/'scripts/qualification/run_octop.py',attempt/'operator-common.py')
 network_args=['--network','none'];extra_mounts=[]
 entry=[a.image,'octop','run','--host','127.0.0.1','--port','10014','--workers','1']
 if a.model:
  original=root/'.private/model-endpoints.json';private_file(original)
  config=json.loads(original.read_text());endpoints=config if isinstance(config,list) else config['endpoints']
  matches=[c for c in endpoints if c['model']=='qwen3.8-flash']
  if len(matches)!=1:raise ValueError('one approved qwen config required')
  selected=matches[0];key=Path(selected['api_key_file'])
  if not key.is_absolute():key=original.parent/key
  if any(path.is_symlink() for path in [key,*key.parents]):raise ValueError('secret path symlink')
  private_file(key)
  cfg=attempt/'model.json';cfg.write_text(json.dumps({'base_url':selected['base_url'],'model':selected['model']}));cfg.chmod(0o600)
  guard=attempt/'guard.py';shutil.copyfile(root/'scripts/qualification/guard_octop_service.py',guard);guard.chmod(0o400)
  (attempt/'challenge.txt').write_text('HARQ-'+secrets.token_hex(16))
  (attempt/'workspace').mkdir(mode=0o700)
  extra_mounts=['-v',f'{cfg}:/run/model.json:ro','-v',f'{key}:/run/model-key:ro','-v',f'{guard}:/run/guard.py:ro']
  network_args=['--network','bridge'];entry=[a.image,'python','/run/guard.py']
 name='harniqo-octop-service-'+stamp
 report=dict(scope='octop-service-native-ws' if a.model else 'octop-service-zero-model',image_id=a.image,model_calls=0,network='bridge' if a.model else 'none',host_ports=[],qualified=False,
             probe_sha256=hashlib.sha256(probe.read_bytes()).hexdigest(),pid_zero=False,removed=False)
 if a.model:report['guard_sha256']=hashlib.sha256(guard.read_bytes()).hexdigest()
 ownership=uuid.uuid4().hex
 report.update(ownership=ownership,dispatch_state="intent")
 save_operator_report(attempt,report)
 try:
  command(['docker','run','--detach','--name',name,'--label','io.harniqo.qualification=octop','--label','io.harniqo.attempt='+ownership,*network_args,
           '--user',f'{os.getuid()}:{os.getgid()}','--init','--read-only','--cap-drop','ALL','--security-opt','no-new-privileges',
           '--pids-limit','128','--memory','4g','--cpus','2','--tmpfs','/tmp:rw,nosuid,nodev,size=256m,mode=1777',
           '-e','HOME=/tmp','-e','OCTOP_HOME=/data/octop','-e','OCTOP_ENABLE_API_DOCS=true','-e','OCTOP_ENABLE_DASHBOARD=false',
           '-e','OCTOP_BACKUP_AUTO_ENABLED=false','-v',f'{home}:/data/octop','-v',f'{attempt}:/evidence',
           '-v',f'{probe}:/run/probe.py:ro',*extra_mounts,*entry])
  report["dispatch_state"]="acknowledged"
  save_operator_report(attempt,report)
  result=subprocess.run(docker_args(['docker','exec',name,'python','/run/probe.py']),capture_output=True,text=True,timeout=240 if a.model else 120,env=docker_env())
  (attempt/'probe.log').write_text(result.stdout+result.stderr)
  report['probe_exit_code']=result.returncode
  report['exit_code']=result.returncode
 except Exception as e:report['operator_error']=type(e).__name__
 finally:
  report.update(finalize_container(name,ownership,attempt,expected_image=a.image))
  if a.model and (attempt/'model-calls.json').exists():
   try:report['model_calls']=len(json.loads((attempt/'model-calls.json').read_text())['calls'])
   except Exception as error:report['operator_error']=type(error).__name__
  native=attempt/'service-probe.json'
  if native.exists():
   try:
    report['native_report_sha256']=hashlib.sha256(native.read_bytes()).hexdigest()
    report['qualified']=operator_passes(json.loads(native.read_text()),report)
   except Exception as error:
    report['native_report_error']=type(error).__name__
  save_operator_report(attempt,report);print(json.dumps(report))
 return 0 if report['qualified'] else 1

if __name__=='__main__':raise SystemExit(main())
