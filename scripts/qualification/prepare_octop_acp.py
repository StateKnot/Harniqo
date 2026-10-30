#!/usr/bin/env python3
"""Create a new offline qualification image; leave prior scope images unchanged."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
import urllib.request
import uuid
from probe_hermes import no_links, private_directory, read_file

from run_octop import docker_args, docker_env

HARNESS='sha256:714e015d1c1281e1c91bd2ae67f39191b99c408eb7523b62a409831e912a73d5'
OPENCODE='sha256:ef392b1148602b61ad4ea1e9dcca9b05337adc3c7ef9fc97434d632c69b8373c'
LOCK='1766085847229271984a3f3080fd9d2c7958f843f84c4a185b32ab4240d1c992'

def digest(path):return hashlib.sha256(path.read_bytes()).hexdigest()

def archive_image(image,archive,stamp,report,runner=None):
 """Only a completed, fsynced Docker save may acquire the final archive name."""
 if archive.exists() or archive.is_symlink():raise ValueError('archive exists')
 partial=archive.with_name(archive.name+'.'+stamp+'.partial')
 if partial.exists() or partial.is_symlink():raise ValueError('partial exists')
 report['partial_archive_name']=partial.name
 run=runner or subprocess.run
 try:
  run(docker_args(['docker','save','-o',str(partial),image]),env=docker_env(),check=True,timeout=120)
  if partial.is_symlink() or not partial.is_file() or partial.stat().st_size<=0:raise ValueError('invalid Docker archive')
  with partial.open('rb') as stream:os.fsync(stream.fileno())
  sha,size=digest(partial),partial.stat().st_size
  partial.chmod(0o444)
  if archive.exists() or archive.is_symlink():raise ValueError('archive appeared during save')
  # Exclusive same-directory promotion does not overwrite a concurrent result.
  os.link(partial,archive);partial.unlink()
  directory=os.open(archive.parent,os.O_RDONLY|os.O_DIRECTORY)
  try:os.fsync(directory)
  finally:os.close(directory)
  report.pop('partial_archive_name',None)
  return sha,size
 except BaseException:
  if partial.is_file() and not partial.is_symlink():
   report['partial_archive_bytes']=partial.stat().st_size
   report['partial_archive_sha256']=digest(partial)
  raise

def main():
 import tomllib
 os.umask(0o077)
 p=argparse.ArgumentParser();p.add_argument('--root',type=Path,required=True);a=p.parse_args();root=no_links(a.root)
 subprocess.run(['df','-h',str(root)],check=True)
 if shutil.disk_usage(root).free<50*1024**3:raise ValueError('50GiB required')
 stamp=time.strftime('%Y%m%dT%H%M%SZ',time.gmtime())+'-'+uuid.uuid4().hex[:8]
 private=private_directory(root/'.private/qualification/octop-acp',create=True)
 attempt=private/('build-'+stamp);attempt.mkdir(mode=0o700)
 shutil.copyfile(root/'scripts/qualification/probe_hermes.py',attempt/'path-helper.py')
 source=root/'images/sources/octop/acp';source.mkdir(parents=True,exist_ok=True)
 upstream=source/'harness-upstream-uv.lock'
 if digest(upstream)!=LOCK:raise ValueError('upstream lock mismatch')
 packages=tomllib.loads(upstream.read_text())['package']
 records=json.loads((root/'deploy/qualification/octop/acp-extra-provenance.json').read_text())
 if {r['name'] for r in records}!={'agent-client-protocol','psutil'} or len(records)!=2:raise ValueError('exact extras required')
 context=root/'.state/qualification/octop-acp'/('build-'+stamp);context.mkdir(parents=True);(context/'wheels').mkdir()
 report={'scope':'octop-harness-acp-delegated','outer_image':HARNESS,'inner_image':OPENCODE,'qualified':False,'model_calls':0,'path_helper_sha256':digest(root/'scripts/qualification/probe_hermes.py'),'source_lock_sha256':LOCK}
 try:
  for record in records:
   matches=[pkg for pkg in packages if pkg['name']==record['name'] and pkg['version']==record['version']]
   if len(matches)!=1 or not any(w['url']==record['url'] and w['hash']=='sha256:'+record['sha256'] and w['size']==record['size'] for w in matches[0]['wheels']):raise ValueError('artifact not in pinned lock')
   if not record['url'].startswith('https://files.pythonhosted.org/') or record['filename']!=record['url'].rsplit('/',1)[-1]:raise ValueError('official source required')
   with urllib.request.urlopen('https://pypi.org/pypi/'+record['name']+'/'+record['version']+'/json',timeout=30) as response:metadata=json.load(response)
   if not any(w['url']==record['url'] and w['digests']['sha256']==record['sha256'] and w['size']==record['size'] and not w['yanked'] for w in metadata['urls']):raise ValueError('official release metadata mismatch')
   artifact=source/record['filename']
   if not artifact.exists():
    with urllib.request.urlopen(record['url'],timeout=30) as response:artifact.write_bytes(response.read())
   if artifact.is_symlink() or digest(artifact)!=record['sha256'] or artifact.stat().st_size!=record['size']:raise ValueError('artifact integrity mismatch')
   shutil.copyfile(artifact,context/'wheels'/record['filename'])
  recipe=root/'deploy/qualification/octop/acp.Dockerfile';requirements=root/'deploy/qualification/octop/acp-extra.requirements.lock'
  expected=''.join(r['name']+'=='+r['version']+' --hash=sha256:'+r['sha256']+'\n' for r in records)
  if requirements.read_text()!=expected:raise ValueError('requirements and provenance mismatch')
  shutil.copyfile(recipe,context/'Dockerfile');shutil.copyfile(requirements,context/'requirements.lock')
  source_tags={}
  for image in [HARNESS,OPENCODE]:
   actual=subprocess.check_output(docker_args(['docker','image','inspect',image,'--format','{{.Id}}']),env=docker_env(),text=True).strip()
   if actual!=image:raise ValueError('source image identity mismatch')
   # BuildKit interprets bare sha256:IDs as registry repository tags. Give each
   # verified local ID a task-owned full-hash tag and verify it before use.
   source_tag='harniqo-octop-acp-source:'+image.removeprefix('sha256:')
   existing=subprocess.run(docker_args(['docker','image','inspect',source_tag,'--format','{{.Id}}']),env=docker_env(),text=True,capture_output=True)
   if existing.returncode==0 and existing.stdout.strip()!=image:raise ValueError('source tag collision')
   subprocess.run(docker_args(['docker','tag',image,source_tag]),env=docker_env(),check=True)
   source_tags[image]=source_tag
  tag='harniqo-octop-acp:'+stamp
  with (attempt/'build.log').open('w') as log:
   subprocess.run(docker_args(['docker','build','--network=none','--pull=false','--build-arg','HARNESS_IMAGE='+source_tags[HARNESS],'--build-arg','OPENCODE_IMAGE='+source_tags[OPENCODE],'--tag',tag,str(context)]),env=docker_env(),stdout=log,stderr=subprocess.STDOUT,check=True,timeout=180)
  for expected,source_tag in source_tags.items():
   if subprocess.check_output(docker_args(['docker','image','inspect',source_tag,'--format','{{.Id}}']),env=docker_env(),text=True).strip()!=expected:raise ValueError('source identity changed during build')
  image=subprocess.check_output(docker_args(['docker','image','inspect',tag,'--format','{{.Id}}']),env=docker_env(),text=True).strip()
  target=root/'images/containers/octop/acp';target.mkdir(parents=True,exist_ok=True)
  archive=target/(image.replace(':','-')+'.docker.tar')
  sha,size=archive_image(image,archive,stamp,report)
  report.update(status='built',image_id=image,archive_relative=str(archive.relative_to(root)),archive_sha256=sha,archive_bytes=size,recipe_sha256=digest(recipe),extra_lock_sha256=digest(requirements),helper_sha256=digest(root/'scripts/qualification/run_octop.py'))
 except Exception as error:report.update(status='failed',error_type=type(error).__name__);raise
 finally:
  (attempt/'report.json').write_text(json.dumps(report,indent=2)+'\n');shutil.rmtree(context)
 print(json.dumps(report))

if __name__=='__main__':main()
