#!/usr/bin/env python3
"""Run exactly one approved native SDK case; preserve stop evidence and private logs."""
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


DOCKER_SOCKET = 'unix:///var/run/docker.sock'

def docker_args(args):
    if args and args[0] == 'docker':
        return ['docker', '--host', DOCKER_SOCKET, *args[1:]]
    return args

def docker_env():
    return {k:v for k,v in os.environ.items() if k not in {
        'DOCKER_HOST','DOCKER_CONTEXT','DOCKER_TLS_VERIFY','DOCKER_CERT_PATH'}}

def command(args, **kwargs):
    return subprocess.run(docker_args(args), check=True, capture_output=True, text=True,
                          env=docker_env(), **kwargs)

def private_file(path):
    if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
        raise ValueError('private regular file required')
    if path.stat().st_uid not in {os.getuid(),0}:
        raise ValueError('unexpected private file owner')

def finalize_container(name, ownership, attempt, *, expected_image, runner=None):
    """Reconcile a random attempt label, requested immutable image and container ID.

    All mutations use the initially observed immutable container ID. The exact
    owner/image/ID is checked again before stop, kill, logs and remove. Failed
    capture retains the stopped resource. Unknown dispatch is always inspected.
    """
    runner = runner or subprocess.run
    result = dict(pid_zero=False, removed=False, cleanup_errors=[])
    container_id = actual_image = None
    def invoke(args, timeout=20):
        try:
            response = runner(docker_args(args), capture_output=True, text=True,
                              timeout=timeout, env=docker_env())
            if response.returncode:
                result['cleanup_errors'].append({'step':args[1], 'exit_code':response.returncode})
            return response
        except Exception as error:
            result['cleanup_errors'].append({'step':args[1], 'error_type':type(error).__name__})
            return None
    def inspect():
        nonlocal container_id, actual_image
        response = invoke(['docker','inspect',container_id or name])
        if response is None or response.returncode:
            result['resource_state'] = ('absent' if response is not None and
                'No such' in response.stderr else 'unknown')
            return None
        try:
            info = json.loads(response.stdout)[0]
            observed_id = info['Id']
            observed_image = info['Image']
            config = info['Config']
            if (not re.fullmatch('[a-f0-9]{64}', observed_id) or
                config.get('Labels',{}).get('io.harniqo.attempt') != ownership or
                config.get('Image') != expected_image or
                (container_id is not None and observed_id != container_id) or
                (actual_image is not None and observed_image != actual_image)):
                result['resource_state']='ownership-mismatch'
                result['cleanup_errors'].append({'step':'ownership','error_type':'OwnershipMismatch'})
                return None
            state=info['State']
            if not isinstance(state.get('Running'),bool) or not isinstance(state.get('Pid'),int):
                raise ValueError('invalid native state')
            container_id, actual_image = observed_id, observed_image
            result.update(resource_state='owned',container_id=container_id,actual_image=actual_image,state=state)
            result['pid_zero'] = state['Pid'] == 0 and not state['Running']
            return info
        except Exception as error:
            result['cleanup_errors'].append({'step':'inspect-parse','error_type':type(error).__name__})
            return None
    info = inspect()
    if info is None:
        return result
    if not result['pid_zero']:
        invoke(['docker','stop','--timeout','10',container_id])
    info = inspect()
    if info is not None and not result['pid_zero']:
        result['forced_stop_attempted']=True
        invoke(['docker','kill',container_id])
        info=inspect()
    if info is None:
        return result
    logs = invoke(['docker','logs','--tail','500',container_id])
    captured = False
    if logs is not None and logs.returncode == 0:
        try:
            (attempt/'container.log').write_text(logs.stdout+logs.stderr)
            (attempt/'container.log').chmod(0o600)
            captured = True
        except Exception as error:
            result['cleanup_errors'].append({'step':'save-logs','error_type':type(error).__name__})
    if result['pid_zero'] and captured and inspect() is not None and result['pid_zero']:
        removed = invoke(['docker','rm',container_id])
        result['removed'] = removed is not None and removed.returncode == 0
    return result

def operator_passes(native, report):
    return (native.get('qualified',False) and report.get('pid_zero',False) and
            report.get('removed',False) and report.get('exit_code') == 0 and
            not report.get('cleanup_errors') and 'operator_error' not in report)

def save_operator_report(attempt, report):
    temporary=attempt/'operator-report.pending'
    temporary.write_text(json.dumps(report,indent=2)+'\n')
    temporary.chmod(0o600)
    with temporary.open('rb') as stream: os.fsync(stream.fileno())
    temporary.replace(attempt/'operator-report.json')


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--root',type=Path,required=True)
    p.add_argument('--image',required=True)
    p.add_argument('--case',choices=['allow','deny','cancel'],required=True)
    args=p.parse_args()
    if not re.fullmatch(r'sha256:[0-9a-f]{64}',args.image): raise ValueError('immutable image ID required')
    root=args.root.resolve()
    print(command(['df','-h',str(root)]).stdout,end='')
    if shutil.disk_usage(root).free < 50*1024**3: raise ValueError('less than50GiB available')
    original=root/'.private/model-endpoints.json'
    private_file(original)
    config=json.loads(original.read_text())
    endpoints=config if isinstance(config,list) else config['endpoints']
    matches=[c for c in endpoints if c['model']=='qwen3.8-flash']
    if len(matches)!=1: raise ValueError('expected one approved qwen config')
    selected=matches[0]
    key=Path(selected['api_key_file'])
    if not key.is_absolute(): key=original.parent/key
    # Do not resolve away symlinks before checking their presence.
    if any(p.is_symlink() for p in [key,*key.parents]): raise ValueError('secret path symlink')
    private_file(key)
    attempt=root/'.private/qualification/octop'/('native-'+args.case+'-'+time.strftime('%Y%m%dT%H%M%SZ',time.gmtime())+'-'+uuid.uuid4().hex[:8])
    attempt.mkdir(mode=0o700)
    cfg=attempt/'model.json'
    cfg.write_text(json.dumps({'base_url':selected['base_url'],'model':selected['model']}))
    cfg.chmod(0o600)
    probe_snapshot=attempt/'probe.py'
    shutil.copyfile(root/'scripts/qualification/probe_octop.py',probe_snapshot)
    probe_snapshot.chmod(0o400)
    shutil.copyfile(Path(__file__),attempt/'operator.py')
    name='harniqo-octop-'+attempt.name
    report=dict(case=args.case,container_name=name,image_id=args.image,qualified=False,pid_zero=False,removed=False,probe_sha256=hashlib.sha256(probe_snapshot.read_bytes()).hexdigest())
    ownership=uuid.uuid4().hex
    report['ownership']=ownership
    report['dispatch_state']='intent'
    save_operator_report(attempt,report)
    try:
        run=['docker','run','--detach','--name',name,'--label','io.harniqo.qualification=octop','--label','io.harniqo.attempt='+ownership,
             '--user',f'{os.getuid()}:{os.getgid()}','--init','--read-only','--cap-drop','ALL',
             '--security-opt','no-new-privileges','--pids-limit','128','--memory','4g','--cpus','2',
             '--tmpfs','/tmp:rw,nosuid,nodev,size=256m,mode=1777',
             '-e','HOME=/tmp','-e','LANGSMITH_TRACING=false','-e','LANGCHAIN_TRACING_V2=false',
             '-v',f'{attempt}:/evidence','-v',f'{cfg}:/run/model.json:ro','-v',f'{key}:/run/model-key:ro',
             '-v',f'{probe_snapshot}:/run/probe.py:ro',
             args.image,'python','/run/probe.py','--config','/run/model.json','--key','/run/model-key',
             '--output','/evidence/result','--case',args.case]
        command(run)
        report['dispatch_state']='acknowledged'
        save_operator_report(attempt,report)
        result=command(['docker','wait',name],timeout=300)
        report['exit_code']=int(result.stdout.strip())
    except Exception as error:
        report['operator_error']=type(error).__name__
    finally:
        report.update(finalize_container(name, ownership, attempt, expected_image=args.image))
        native=attempt/'result/report.json'
        if native.exists():
            try:
                data=json.loads(native.read_text())
                report['native_report_sha256']=hashlib.sha256(native.read_bytes()).hexdigest()
                report['qualified']=operator_passes(data,report)
                report['model_calls']=data.get('model_calls')
            except Exception as error:
                report['native_report_error']=type(error).__name__
        save_operator_report(attempt,report)
        print(json.dumps(report))
    return 0 if report['qualified'] else 1

if __name__=='__main__': raise SystemExit(main())
