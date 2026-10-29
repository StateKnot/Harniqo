#!/usr/bin/env python3
"""Build pinned Octop service sdists offline; no credentials or host installs."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
import tomllib
import urllib.request
import uuid
from prepare_octop import BASE, digest, official_record, wheel_metadata, run

BUILD_TOOLS={'setuptools':'80.9.0','wheel':'0.45.1','packaging':'26.2'}
INNER='''import json, pathlib, subprocess, os
subprocess.run(['python','-m','venv','/tmp/build-env'],check=True)
pip=['/tmp/build-env/bin/python','-m','pip']
subprocess.run(pip+['install','--no-index','--find-links=/builder','--require-hashes','-r','/builder/requirements.lock'],check=True)
records=json.loads(pathlib.Path('/inputs/locked-selection.json').read_text())
for r in records:
 if not r['filename'].endswith('.whl'):
  subprocess.run(pip+['wheel','--no-index','--no-deps','--no-build-isolation','--wheel-dir=/output','/inputs/'+r['filename']],check=True)
'''

def main():
    p=argparse.ArgumentParser();p.add_argument('--root',type=Path,required=True);a=p.parse_args()
    root=a.root.resolve();subprocess.run(['df','-h',str(root)],check=True)
    if shutil.disk_usage(root).free<60*1024**3:raise ValueError('60GiB required')
    sources=root/'images/sources/octop/service-locked'
    source_lock=root/'images/sources/octop/service-upstream-uv.lock'
    if digest(source_lock)!='c6b4c1a8f96c37351731b7eab79772fccffbc1ae7aadd501df28be954b52c907':raise ValueError('service upstream lock mismatch')
    packages=tomllib.loads(source_lock.read_text())['package']
    records=json.loads((sources/'locked-selection.json').read_text())
    for r in records:
        if r['filename'].endswith('.whl'): continue
        matches=[p for p in packages if p['name']==r['name'] and p['version']==r['version']]
        if len(matches)!=1 or matches[0].get('sdist',{}).get('hash')!='sha256:'+r['sha256'] or matches[0]['sdist']['url']!=r['url'] or r['filename']!=r['url'].rsplit('/',1)[-1]:raise ValueError('sdist not in fixed upstream lock')
        path=sources/r['filename']
        if not path.exists():
            if not r['url'].startswith('https://files.pythonhosted.org/'): raise ValueError('unexpected source host')
            temp=path.with_name(path.name+'.builder-download')
            with urllib.request.urlopen(r['url'],timeout=180) as response: temp.write_bytes(response.read())
            temp.replace(path)
        if path.is_symlink() or not path.is_file() or digest(path)!=r['sha256'] or path.stat().st_size!=r['size']:raise ValueError('incomplete/invalid locked source')
    stamp=time.strftime('%Y%m%dT%H%M%SZ',time.gmtime())+'-'+uuid.uuid4().hex[:8]
    attempt=root/'.private/qualification/octop'/('sdist-build-'+stamp);attempt.mkdir(mode=0o700)
    builder=root/'images/sources/octop/build-tools';builder.mkdir(exist_ok=True)
    tool_records=[]
    for name,version in BUILD_TOOLS.items():
        with urllib.request.urlopen(f'https://pypi.org/pypi/{name}/{version}/json',timeout=60) as response:meta=json.load(response)
        choices=[r for r in meta['urls'] if r['filename'].endswith('-py3-none-any.whl') and not r['yanked']]
        if len(choices)!=1:raise ValueError('ambiguous build wheel')
        r=choices[0];path=builder/r['filename']
        if not path.exists():
            if not r['url'].startswith('https://files.pythonhosted.org/'):raise ValueError('unexpected host')
            with urllib.request.urlopen(r['url'],timeout=180) as response:path.write_bytes(response.read())
        tool_records.append(official_record(path))
    lock=''.join(f"{r['name']}=={r['version']} --hash=sha256:{r['sha256']}\n" for r in tool_records)
    (builder/'requirements.lock').write_text(lock)
    (attempt/'build-tool-provenance.json').write_text(json.dumps(tool_records,indent=2))
    inner=attempt/'build.py';inner.write_text(INNER)
    out=root/'images/sources/octop'/('service-built-'+stamp);out.mkdir()
    report=dict(base=BASE,model_calls=0,qualified=False,source_lock_sha256=digest(root/'images/sources/octop/service-upstream-uv.lock'))
    try:
        run(['docker','run','--name','harniqo-octop-sdist-'+stamp,'--label','io.harniqo.qualification=octop','--network','none',
             '--user',f'{os.getuid()}:{os.getgid()}','--read-only','--cap-drop','ALL','--security-opt','no-new-privileges',
             '--pids-limit','128','--memory','4g','--cpus','2','--tmpfs','/tmp:rw,nosuid,nodev,size=1g,mode=1777',
             '-e','SOURCE_DATE_EPOCH=1793203200','-e','PYTHONDONTWRITEBYTECODE=1','-e','PIP_DISABLE_PIP_VERSION_CHECK=1',
             '-v',f'{sources}:/inputs:ro','-v',f'{builder}:/builder:ro','-v',f'{inner}:/run/build.py:ro','-v',f'{out}:/output',
             BASE,'python','/run/build.py'],attempt/'build.log',timeout=600)
        built=[]
        for path in sorted(out.glob('*.whl')):
            name,version=wheel_metadata(path);built.append(dict(name=name,version=version,filename=path.name,sha256=digest(path),size=path.stat().st_size))
        if len(built)!=len([r for r in records if not r['filename'].endswith('.whl')]):raise ValueError('incomplete built wheel count')
        report.update(status='built',wheels=built,output_relative=str(out.relative_to(root)))
    except Exception as e:report.update(status='failed',error_type=type(e).__name__);raise
    finally:
        (attempt/'report.json').write_text(json.dumps(report,indent=2)+'\n');print(json.dumps(report))

if __name__=='__main__':main()
