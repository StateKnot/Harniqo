#!/usr/bin/env python3
"""Freeze and offline-install the complete service closure, including owned sdists."""
import argparse
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import time
import tomllib
import uuid
from prepare_octop import BASE,digest,run,wheel_metadata
from run_octop import docker_args,docker_env


def main():
 p=argparse.ArgumentParser();p.add_argument('--root',type=Path,required=True);p.add_argument('--sdist-report',type=Path,required=True);a=p.parse_args()
 root=a.root.resolve();subprocess.run(['df','-h',str(root)],check=True)
 if shutil.disk_usage(root).free<60*1024**3:raise ValueError('60GiB required')
 source=root/'images/sources/octop/service-locked'
 lock=root/'images/sources/octop/service-upstream-uv.lock'
 if digest(lock)!='c6b4c1a8f96c37351731b7eab79772fccffbc1ae7aadd501df28be954b52c907':raise ValueError('source lock mismatch')
 records=json.loads((source/'locked-selection.json').read_text())
 built=json.loads(a.sdist_report.read_text())
 if built['status']!='built' or built['base']!=BASE or built['source_lock_sha256']!=digest(lock):raise ValueError('invalid builder report')
 built_dir=root/built['output_relative'];built_by_name={r['name']:r for r in built['wheels']}
 packages=tomllib.loads(lock.read_text())['package']
 wheels=[]
 for r in records:
  f=source/r['filename']
  if f.is_symlink() or digest(f)!=r['sha256'] or f.stat().st_size!=r['size']:raise ValueError('source artifact mismatch '+r['name'])
  if r['name']=='octop' and (r['sha256']!='3801d38b586ff6e3c9aae48f95d3adf8042c8df9d514b3caa7005366aff2f454' or r['size']!=19076875):raise ValueError('fixed official service wheel mismatch')
  if r['name']!='octop':
   packages_matching=[x for x in packages if x['name']==r['name'] and x['version']==r['version']]
   hashes={x['hash'] for pkg in packages_matching for x in [*pkg.get('wheels',[]),*([pkg['sdist']] if 'sdist' in pkg else [])]}
   if 'sha256:'+r['sha256'] not in hashes:raise ValueError('artifact absent from fixed upstream lock')
  if not r['filename'].endswith('.whl'):
   replacement=built_by_name[r['name']]
   f=built_dir/replacement['filename']
   if f.is_symlink() or digest(f)!=replacement['sha256']:raise ValueError('built wheel mismatch')
   r={**replacement,'source_sdist':r,'build_report_sha256':digest(a.sdist_report)}
  actual_name,actual_version=wheel_metadata(f)
  if re.sub(r'[-_.]+','-',actual_name).lower()!=re.sub(r'[-_.]+','-',r['name']).lower() or actual_version!=r['version']:raise ValueError('wheel identity mismatch: '+r['name']+' '+actual_name+' '+actual_version+' '+r['version'])
  wheels.append((f,r))
 if len(wheels)!=201 or len({r['name'] for f,r in wheels})!=201:raise ValueError('incomplete service closure')
 if not any(r['name']=='octop' and r['version']=='1.0.3b1' for f,r in wheels):raise ValueError('root version mismatch')
 stamp=time.strftime('%Y%m%dT%H%M%SZ',time.gmtime())+'-'+uuid.uuid4().hex[:8]
 attempt=root/'.private/qualification/octop'/('service-build-'+stamp);attempt.mkdir(mode=0o700)
 context=root/'.state/qualification/octop'/('service-context-'+stamp);context.mkdir();(context/'wheels').mkdir()
 report=dict(scope='octop-service-native',source_commit='890111b7879665840414ca28ab168b05b0db0921',base=BASE,qualified=False,model_calls=0)
 try:
  requirements=''.join(f"{r['name']}=={r['version']} --hash=sha256:{r['sha256']}\n" for f,r in sorted(wheels,key=lambda x:x[1]['name']))
  (attempt/'requirements.lock').write_text(requirements)
  (attempt/'wheel-provenance.json').write_text(json.dumps([r for f,r in wheels],indent=2)+'\n')
  shutil.copyfile(attempt/'requirements.lock',context/'requirements.lock')
  for f,r in wheels:
   dst=context/'wheels'/r['filename'];shutil.copyfile(f,dst)
   if digest(dst)!=r['sha256']:raise ValueError('context copy mismatch')
  recipe=root/'deploy/qualification/octop/Dockerfile';shutil.copyfile(recipe,context/'Dockerfile')
  tag='harniqo-octop-service:'+stamp
  run(['docker','build','--network=none','--pull=false','--build-arg','PYTHON_BASE='+BASE,'--tag',tag,str(context)],attempt/'build.log')
  image=subprocess.check_output(docker_args(['docker','image','inspect',tag,'--format','{{.Id}}']),text=True,env=docker_env()).strip()
  target=root/'images/containers/octop/service';target.mkdir(parents=True,exist_ok=True)
  archive=target/(image.replace(':','-')+'.docker.tar')
  if archive.exists():raise ValueError('refuse archive overwrite')
  run(['docker','save','-o',str(archive),image],attempt/'save.log');archive.chmod(0o444)
  report.update(status='built',image_id=image,archive_sha256=digest(archive),archive_bytes=archive.stat().st_size,
                recipe_sha256=digest(recipe),lock_sha256=digest(attempt/'requirements.lock'),wheels=len(wheels))
 except Exception as e:report.update(status='failed',error_type=type(e).__name__);raise
 finally:
  (attempt/'report.json').write_text(json.dumps(report,indent=2)+'\n');print(json.dumps(report));shutil.rmtree(context)

if __name__=='__main__':main()
