#!/usr/bin/env python3
"""Bound and retain one trusted native ACP qualification container."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
import uuid
from probe_hermes import no_links, private_directory, read_file

from run_octop import command,finalize_container,save_operator_report,operator_passes,private_file

def full_log_runner(args,**kwargs):
 # Retain the shared helper's ownership checks and immutable ID, but capture
 # the entire available Docker log instead of its generic 500-line tail.
 if args[3:6]==['logs','--tail','500']:args=[*args[:4],*args[6:]]
 return subprocess.run(args,**kwargs)

def main():
 os.umask(0o077)
 p=argparse.ArgumentParser();p.add_argument('--root',type=Path,required=True);p.add_argument('--image',required=True);p.add_argument('--mode',choices=['bootstrap','model'],default='bootstrap');p.add_argument('--bootstrap-report',type=Path);p.add_argument('--variant',choices=['upstream','patched','patched512'],default='upstream');a=p.parse_args()
 if not re.fullmatch(r'sha256:[a-f0-9]{64}',a.image):raise ValueError('immutable image ID required')
 root=no_links(a.root);print(command(['df','-h',str(root)]).stdout,end='')
 if shutil.disk_usage(root).free<50*1024**3:raise ValueError('50GiB required')
 stamp=time.strftime('%Y%m%dT%H%M%SZ',time.gmtime())+'-'+uuid.uuid4().hex[:8]
 private=private_directory(root/'.private/qualification/octop-acp',create=True)
 profile_names={'upstream':'acp-profile.json','patched':'acp-patched-profile.json','patched512':'acp-patched512-profile.json'}
 profile_path=root/'deploy/qualification/octop'/profile_names[a.variant];profile=json.loads(profile_path.read_text())
 if profile['image_id']!=a.image:raise ValueError('profile image mismatch')
 if hashlib.sha256((root/'scripts/qualification/run_octop.py').read_bytes()).hexdigest()!=profile['helper_sha256']:raise ValueError('shared helper changed')
 if hashlib.sha256((root/'scripts/qualification/probe_hermes.py').read_bytes()).hexdigest()!=profile['path_helper_sha256']:raise ValueError('path helper changed')
 if a.mode=='model':
  if a.bootstrap_report is None:raise ValueError('successful bootstrap report required')
  prior=no_links(a.bootstrap_report);prior.relative_to(private);private_directory(prior.parent)
  bootstrap=json.loads(read_file(prior,2*1024**2));native=no_links(prior.parent/'report.json')
  if not bootstrap.get('qualified') or bootstrap.get('image_id')!=a.image or hashlib.sha256(read_file(native,2*1024**2)).hexdigest()!=bootstrap.get('native_report_sha256'):raise ValueError('unverified bootstrap')
  data=json.loads(read_file(native,2*1024**2))
  if data.get('scope')!=profile['scope']+'-bootstrap':raise ValueError('bootstrap scope mismatch')
  expected_source=profile.get('patch',{}).get('patched_sha256','fd03c837bca1aefb6629a094a0ee783b823af06832cc371633e55d11c41ecac9')
  if data.get('loaded_acp_runner_sha256')!=expected_source:raise ValueError('bootstrap loaded source mismatch')
  if data.get('budgets')!={k:profile[k] for k in ['outer_max_requests','inner_max_requests','outer_max_output_tokens','inner_max_output_tokens','runtime_limit_seconds']}:raise ValueError('bootstrap budget snapshot mismatch')
  if not all(data.get(k) for k in ['inner_observer_ready','inner_no_sends','disconnect_rejection_created_no_resources','positive_native_session_ids']):raise ValueError('bootstrap gate incomplete')
 attempt=private/((a.variant+'-' if a.variant!='upstream' else '')+a.mode+'-'+stamp);attempt.mkdir(mode=0o700)
 for name in ['workspace','outer-workspace','inner-home','inner-audit','observer']:(attempt/name).mkdir(mode=0o700)
 shutil.copyfile(profile_path,attempt/'profile.json')
 for path,expected in profile['observers'].items():
  source=root/path
  if hashlib.sha256(source.read_bytes()).hexdigest()!=expected:raise ValueError('observer source changed')
  shutil.copyfile(source,attempt/'observer'/source.name)
 probe=attempt/'probe.py';shutil.copyfile(root/'scripts/qualification/probe_octop_acp.py',probe);probe.chmod(0o400)
 guard=attempt/'guard_octop_acp.py';shutil.copyfile(root/'scripts/qualification/guard_octop_acp.py',guard);guard.chmod(0o400)
 shutil.copyfile(root/'scripts/qualification/probe_hermes.py',attempt/'path-helper.py')
 shutil.copyfile(Path(__file__),attempt/'operator.py');shutil.copyfile(root/'scripts/qualification/run_octop.py',attempt/'operator-common.py')
 name='harniqo-octop-acp-'+stamp;ownership=uuid.uuid4().hex
 report=dict(scope=profile['scope']+('-bootstrap' if a.mode=='bootstrap' else ''),qualified=False,image_id=a.image,ownership=ownership,dispatch_state='intent',pid_zero=False,removed=False,
   probe_sha256=hashlib.sha256(probe.read_bytes()).hexdigest(),guard_sha256=hashlib.sha256(guard.read_bytes()).hexdigest(),helper_sha256=hashlib.sha256((attempt/'operator-common.py').read_bytes()).hexdigest(),path_helper_sha256=profile['path_helper_sha256'],host_ports=[],network='none' if a.mode=='bootstrap' else 'bridge')
 extra=[]
 if a.mode=='model':
  original=no_links(root/'.private/model-endpoints.json');private_directory(original.parent);config=json.loads(read_file(original,2*1024**2))
  endpoints=config if isinstance(config,list) else config['endpoints'];matches=[c for c in endpoints if c['model']==profile['model']]
  if len(matches)!=1:raise ValueError('unique approved endpoint required')
  selected=matches[0];key=Path(selected['api_key_file'])
  if not key.is_absolute():key=original.parent/key
  key=no_links(key);private_directory(key.parent)
  private_file(key);cfg=attempt/'model.json';cfg.write_text(json.dumps({'base_url':selected['base_url'],'model':selected['model']}));cfg.chmod(0o600)
  extra=['-v',f'{cfg}:/run/model.json:ro','-v',f'{key}:/run/model-key:ro']
  reservation=private/(a.variant+'-model-reservation.json' if a.variant!='upstream' else 'model-reservation.json')
  with reservation.open('x') as stream:json.dump({'attempt':attempt.name,'state':'before_dispatch','image_id':a.image},stream);stream.flush();os.fsync(stream.fileno())
 save_operator_report(attempt,report)
 try:
  command(['docker','run','--detach','--pull','never','--name',name,'--label','io.harniqo.qualification=octop-acp','--label','io.harniqo.attempt='+ownership,
   '--network',report['network'],'--user',f'{os.getuid()}:{os.getgid()}','--init','--read-only','--cap-drop','ALL','--security-opt','no-new-privileges',
   '--pids-limit','128','--memory','4g','--cpus','2','--tmpfs','/tmp:rw,nosuid,nodev,size=256m,mode=1777','-e','HOME=/tmp',
   '-v',f'{attempt}:/evidence','-v',f'{probe}:/run/probe.py:ro','-v',f'{guard}:/run/guard_octop_acp.py:ro',
   '-v',f'{attempt / "observer"}:/run/observer:ro','-v',f'{attempt / "profile.json"}:/run/qualification-profile.json:ro',*extra,a.image,'timeout','--signal=TERM','--kill-after=5s','175s','python','/run/probe.py','--mode',a.mode,'--observer'])
  report['dispatch_state']='acknowledged';save_operator_report(attempt,report)
  result=command(['docker','wait',name],timeout=190);report['exit_code']=int(result.stdout.strip())
 except Exception as error:report['operator_error']=type(error).__name__
 finally:
  report.update(finalize_container(name,ownership,attempt,expected_image=a.image,runner=full_log_runner))
  native=attempt/'report.json'
  if native.exists():
   try:
    data=json.loads(read_file(native,2*1024**2));report['native_report_sha256']=hashlib.sha256(native.read_bytes()).hexdigest();report['qualified']=operator_passes(data,report)
    report['outer_model_calls']=data.get('outer_model_calls');report['inner_model_calls']=data.get('inner_model_calls')
   except Exception as error:report['native_report_error']=type(error).__name__
  save_operator_report(attempt,report)
 print(json.dumps(report))
 return 0 if report['qualified'] else 1

if __name__=='__main__':raise SystemExit(main())
