#!/usr/bin/env python3
"""Run the saved-trace formatting component with no network and no credentials."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import time
import uuid
from probe_hermes import no_links, private_directory, read_file

from run_octop import command,finalize_container,save_operator_report,operator_passes
from run_octop_acp import full_log_runner


def main():
 os.umask(0o077);p=argparse.ArgumentParser();p.add_argument('--root',type=Path,required=True);p.add_argument('--source-attempt',type=Path,required=True);p.add_argument('--variant',choices=['upstream','patched'],default='upstream');a=p.parse_args()
 root=no_links(a.root);print(command(['df','-h',str(root)]).stdout,end='')
 if shutil.disk_usage(root).free<50*1024**3:raise ValueError('50GiB required')
 parent=private_directory(root/'.private/qualification/octop-acp',create=True);source=private_directory(a.source_attempt);source.relative_to(parent)
 profile=json.loads((root/('deploy/qualification/octop/acp-patched-profile.json' if a.variant=='patched' else 'deploy/qualification/octop/acp-profile.json')).read_text());image=profile['image_id']
 if hashlib.sha256((root/'scripts/qualification/run_octop.py').read_bytes()).hexdigest()!=profile['helper_sha256']:raise ValueError('helper changed')
 if hashlib.sha256((root/'scripts/qualification/probe_hermes.py').read_bytes()).hexdigest()!=profile['path_helper_sha256']:raise ValueError('path helper changed')
 original_report=json.loads(read_file(source/'report.json',2*1024**2))
 if original_report['scope']!='octop-harness-acp-delegated' or original_report['qualified'] is not False:raise ValueError('expected retained original failed scope')
 stamp=time.strftime('%Y%m%dT%H%M%SZ',time.gmtime())+'-'+uuid.uuid4().hex[:8];attempt=parent/(('patched-' if a.variant=='patched' else '')+'replay-'+stamp);attempt.mkdir(mode=0o700)
 fixture=attempt/'recording';fixture.mkdir(mode=0o700);code=attempt/'code';code.mkdir(mode=0o700)
 for name in ['acp-observed.json','outer-history.json','inner-history.json','outer-tool-actions.json','outer-input.json','report.json']:
  (fixture/name).write_bytes(read_file(source/name,64*1024**2));(fixture/name).chmod(0o400)
 private_directory(source/'workspace')
 # This synthetic challenge in the retained old run is 0644 inside a 0700 directory.
 # Never relax secret/config checks or mutate the original evidence to replay it.
 challenge=no_links(source/'workspace/input.txt')
 if challenge.stat().st_uid!=os.geteuid():raise ValueError('challenge owner mismatch')
 (fixture/'input.txt').write_bytes(read_file(challenge,1024,private=False));(fixture/'input.txt').chmod(0o400)
 for name in ['replay_octop_acp.py','apply_octop_acp_patch.py','run_octop_acp_replay.py','run_octop_acp.py','run_octop.py','probe_hermes.py']:
  shutil.copyfile(root/'scripts/qualification'/name,code/name);(code/name).chmod(0o400)
 for name in ['acp-final-text.patch','acp-patch-profile.json']:
  shutil.copyfile(root/'deploy/qualification/octop'/name,code/name);(code/name).chmod(0o400)
 ownership=uuid.uuid4().hex;name='harniqo-octop-acp-replay-'+stamp
 report={'scope':'offline-real-trace-component-replay','qualified':False,'image_id':image,'ownership':ownership,'dispatch_state':'intent','pid_zero':False,'removed':False,'model_calls':0,'network':'none','secret_mounts':False,'source_attempt':source.name,'code_sha256':{f.name:hashlib.sha256(f.read_bytes()).hexdigest() for f in code.iterdir()}}
 save_operator_report(attempt,report)
 try:
  command(['docker','run','--detach','--pull','never','--name',name,'--label','io.harniqo.qualification=octop-acp','--label','io.harniqo.attempt='+ownership,'--network','none','--user',f'{os.getuid()}:{os.getgid()}','--init','--read-only','--cap-drop','ALL','--security-opt','no-new-privileges','--pids-limit','32','--memory','1g','--cpus','1','--tmpfs','/tmp:rw,nosuid,nodev,size=64m,mode=1777','-e','PYTHONDONTWRITEBYTECODE=1','-v',f'{attempt}:/evidence','-v',f'{fixture}:/recording:ro','-v',f'{code}:/code:ro',image,'timeout','--signal=TERM','--kill-after=5s','45s','python','/code/replay_octop_acp.py','--source','/recording','--out','/evidence','--patch','/code/acp-final-text.patch','--profile','/code/acp-patch-profile.json',*(['--installed-patched'] if a.variant=='patched' else [])])
  report['dispatch_state']='acknowledged';save_operator_report(attempt,report);report['exit_code']=int(command(['docker','wait',name],timeout=55).stdout.strip())
 except Exception as error:report['operator_error']=type(error).__name__
 finally:
  report.update(finalize_container(name,ownership,attempt,expected_image=image,runner=full_log_runner))
  native=attempt/'report.json'
  if native.exists():
   data=json.loads(read_file(native,2*1024**2));report['qualified']=operator_passes(data,report);report['native_report_sha256']=hashlib.sha256(native.read_bytes()).hexdigest()
  save_operator_report(attempt,report)
 print(json.dumps(report));return 0 if report['qualified'] else 1

if __name__=='__main__':raise SystemExit(main())
