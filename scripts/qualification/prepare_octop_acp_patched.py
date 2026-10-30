#!/usr/bin/env python3
"""Build and archive the separately identified, reviewed ACP text fix offline."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
import uuid
from probe_hermes import no_links, private_directory, read_file

from run_octop import command,docker_args,docker_env
from prepare_octop_acp import archive_image


def digest(path):return hashlib.sha256(path.read_bytes()).hexdigest()
def main():
 os.umask(0o077);p=argparse.ArgumentParser();p.add_argument('--root',type=Path,required=True);a=p.parse_args();root=no_links(a.root)
 print(command(['df','-h',str(root)]).stdout,end='')
 if shutil.disk_usage(root).free<50*1024**3:raise ValueError('50GiB required')
 recipe=root/'deploy/qualification/octop';profile=json.loads((recipe/'acp-patch-profile.json').read_text());base=profile['original_image_id']
 if digest(recipe/'acp-final-text.patch')!=profile['patch_sha256'] or digest(recipe/'acp-upstream-LICENSE')!=profile['upstream_license_sha256']:raise ValueError('patch/license identity mismatch')
 stamp=time.strftime('%Y%m%dT%H%M%SZ',time.gmtime())+'-'+uuid.uuid4().hex[:8]
 private=private_directory(root/'.private/qualification/octop-acp',create=True)
 attempt=private/('patched-build-'+stamp);attempt.mkdir(mode=0o700)
 shutil.copyfile(root/'scripts/qualification/probe_hermes.py',attempt/'path-helper.py')
 context=root/'.state/qualification/octop-acp'/('patched-build-'+stamp);context.mkdir(parents=True)
 sources=[recipe/'acp-final-text.patch',recipe/'acp-patch-profile.json',recipe/'acp-upstream-LICENSE',root/'scripts/qualification/apply_octop_acp_patch.py']
 for source in sources:shutil.copyfile(source,context/source.name);shutil.copyfile(source,attempt/source.name)
 shutil.copyfile(recipe/'acp-patched.Dockerfile',context/'Dockerfile');shutil.copyfile(recipe/'acp-patched.Dockerfile',attempt/'Dockerfile')
 shutil.copyfile(Path(__file__),attempt/'prepare.py')
 report={'scope':profile['scope'],'original_image_id':base,'qualified':False,'model_calls':0,'path_helper_sha256':digest(root/'scripts/qualification/probe_hermes.py'),'inputs':{x.name:digest(x) for x in sources},'recipe_sha256':digest(recipe/'acp-patched.Dockerfile'),'operator_sha256':digest(Path(__file__)),'archive_helper_sha256':digest(root/'scripts/qualification/prepare_octop_acp.py'),'docker_helper_sha256':digest(root/'scripts/qualification/run_octop.py')}
 try:
  if command(['docker','image','inspect',base,'--format','{{.Id}}']).stdout.strip()!=base:raise ValueError('original image identity mismatch')
  source_tag='harniqo-octop-acp-patch-source:'+base.removeprefix('sha256:')
  prior=subprocess.run(docker_args(['docker','image','inspect',source_tag,'--format','{{.Id}}']),env=docker_env(),capture_output=True,text=True)
  if prior.returncode==0 and prior.stdout.strip()!=base:raise ValueError('source tag collision')
  command(['docker','tag',base,source_tag]);tag='harniqo-octop-acp-patched:'+stamp
  with (attempt/'build.log').open('w') as log:
   subprocess.run(docker_args(['docker','build','--network=none','--pull=false','--build-arg','ORIGINAL_IMAGE='+source_tag,'--tag',tag,str(context)]),env=docker_env(),stdout=log,stderr=subprocess.STDOUT,check=True,timeout=180)
  if command(['docker','image','inspect',source_tag,'--format','{{.Id}}']).stdout.strip()!=base:raise ValueError('original image changed during build')
  image=command(['docker','image','inspect',tag,'--format','{{.Id}}']).stdout.strip()
  target=root/'images/containers/octop/acp-patched';target.mkdir(parents=True,exist_ok=True);archive=target/(image.replace(':','-')+'.docker.tar')
  sha,size=archive_image(image,archive,stamp,report)
  report.update(status='built',image_id=image,archive_relative=str(archive.relative_to(root)),archive_sha256=sha,archive_bytes=size,source_sha256=profile['patched_sha256'],patch_sha256=profile['patch_sha256'])
 except Exception as error:report.update(status='failed',error_type=type(error).__name__)
 finally:
  (attempt/'report.json').write_text(json.dumps(report,indent=2)+'\n');shutil.rmtree(context)
 print(json.dumps(report));return 0 if report.get('status')=='built' else 1

if __name__=='__main__':raise SystemExit(main())
