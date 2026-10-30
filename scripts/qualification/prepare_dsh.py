#!/usr/bin/env python3
"""Acquire a pinned official npm runtime; retain the resolved integrity lock.

Trusted qualification preparation only. No credentials, repository build
context, global host installs, lifecycle scripts, or model requests are used.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import secrets
import subprocess
import tarfile
import urllib.request
from dsh_support import command, builder, archive_image, validate_image
from probe_hermes import private_directory, write_report

COMMIT = 'f3803c96974ad4a94195fb97e5c637e9c2a3afe3'
VERSION = '0.2.2-alpha.2'
SOURCE_SHA = '798cab9a2acbc886a40ad9f7916e52f9380fbd4b51b41f685f3ed681f77e0ccb'
BASE = 'sha256:eba17fa04b4ac5616496c2ce374a95afb6db6ff52d8bf73bf87dc8c7b8656cad'
BASE_TAG = 'harniqo-dsh-input-node:'+BASE.split(':')[1]
NPM_INTEGRITY = 'sha512-mdcpgF8D1eFQV8Gu3T/sYLkBwfw9h/Li0uPIxXyby1VybJgrFg8BU1Le40FiMnN+heL7k3zOMuAVe7sFq/JO4w=='

def run(*args, **kwargs):
    return command(*args, **kwargs)

def ensure_source(root):
    cache=root/'images/sources/dsh';cache.mkdir(parents=True,exist_ok=True)
    archive=cache/'source-f3803c9.tar.gz'
    if not archive.exists():
        with urllib.request.urlopen('https://codeload.github.com/deepseek-ai/deepseek-harness/tar.gz/'+COMMIT,timeout=60) as response, archive.open('xb') as output:
            count=0
            while block:=response.read(1024**2):
                count+=len(block)
                if count>512*1024**2:raise ValueError('source_download_bound')
                output.write(block)
    if hashlib.sha256(archive.read_bytes()).hexdigest()!=SOURCE_SHA:raise ValueError('source_archive_hash_mismatch')
    source=cache/('deepseek-harness-'+COMMIT)
    if not source.exists():
        with tarfile.open(archive,'r:gz') as bundle:bundle.extractall(cache,filter='data')
    return source

def prepare(root):
    if os.geteuid() == 0:
        raise ValueError('non_root_operator_required')
    source=ensure_source(root)
    work = root/'images/sources/dsh/npm-runtime'
    work.mkdir(parents=True, exist_ok=False)
    metadata = json.load(urllib.request.urlopen('https://registry.npmjs.org/@deepseek-ai%2fdsh/'+VERSION, timeout=30))
    if metadata['dist']['integrity'] != NPM_INTEGRITY:
        raise ValueError('official_tarball_integrity_changed')
    tag = json.load(urllib.request.urlopen('https://api.github.com/repos/deepseek-ai/deepseek-harness/git/ref/tags/dsh-v'+VERSION, timeout=30))
    if tag['object'] != {'sha': COMMIT, 'type': 'commit', 'url': 'https://api.github.com/repos/deepseek-ai/deepseek-harness/git/commits/'+COMMIT}:
        raise ValueError('fixed_tag_mismatch')
    (work/'official-release.json').write_text(json.dumps({'npm': metadata, 'tag': tag}, indent=2)+'\n')
    (work/'package.json').write_text(json.dumps({'private': True, 'dependencies': {'@deepseek-ai/dsh': VERSION}}, indent=2)+'\n')
    validate_image(BASE,expected=BASE)
    attempts=private_directory(root/'.private/qualification',create=True)
    attempt=attempts/('dsh-prepare-'+secrets.token_hex(16));attempt.mkdir(mode=0o700)
    arguments = ['--pull','never','--no-healthcheck',
      '--user',f'{os.getuid()}:{os.getgid()}','--read-only','--cap-drop','ALL',
      '--security-opt','no-new-privileges','--memory','4g','--cpus','2','--pids-limit','128',
      '--tmpfs','/tmp:rw,nosuid,nodev,size=512m','--mount',f'type=bind,src={work},dst=/build',
      '--workdir','/build','--env','HOME=/tmp','--entrypoint','sh',BASE,'-ec',
      'node --version; npm --version; npm install --package-lock-only --ignore-scripts --no-audit --no-fund --registry=https://registry.npmjs.org; npm ci --ignore-scripts --no-audit --no-fund --registry=https://registry.npmjs.org; node node_modules/@deepseek-ai/dsh/lib/bin.js --version']
    prepared=builder(arguments,attempt,BASE)
    lock = json.loads((work/'package-lock.json').read_text())
    packages = lock['packages']
    if packages['node_modules/@deepseek-ai/dsh']['integrity'] != NPM_INTEGRITY:
        raise ValueError('locked_cli_mismatch')
    for location, package in packages.items():
        if location and (not package.get('resolved','').startswith('https://registry.npmjs.org/') or not package.get('integrity','').startswith('sha512-')):
            raise ValueError('unlocked_or_nonofficial_package:'+location)
    record = {'source_commit':COMMIT, 'version':VERSION, 'base_image':BASE, 'resolved_package_count':len(packages)-1,
              'installed_package_count':sum((work/name/'package.json').is_file() for name in packages if name),
              'node_version':'24.22.1','npm_version':'12.2.1',
              'official_source_archive_sha256':SOURCE_SHA,
              'official_pnpm_lock_sha256':hashlib.sha256((source/'pnpm-lock.yaml').read_bytes()).hexdigest(),
              'dependency_tree_relation':'Harniqo frozen npm release combination; not the official pnpm workspace tree',
              'lock_sha256':hashlib.sha256((work/'package-lock.json').read_bytes()).hexdigest(),
              'builder_evidence':str(attempt.relative_to(root)), 'lifecycle_scripts':'disabled', 'scope':'resolved_official_npm_runtime_closure', 'model_requests':0}
    (work/'provenance.json').write_text(json.dumps(record,indent=2)+'\n')
    print(json.dumps(record))

def build(root):
    work=root/'images/sources/dsh/npm-runtime'
    if shutil.disk_usage(root).free<50*1024**3:raise ValueError('disk_space_threshold')
    record=json.loads((work/'provenance.json').read_text())
    if record['source_commit']!=COMMIT or record['base_image']!=BASE:raise ValueError('input_mismatch')
    if hashlib.sha256((work/'package-lock.json').read_bytes()).hexdigest()!=record['lock_sha256']:raise ValueError('lock_changed')
    source=ensure_source(root)
    packages=json.loads((work/'package-lock.json').read_text())['packages']
    record.update(resolved_package_count=len(packages)-1,
      installed_package_count=sum((work/name/'package.json').is_file() for name in packages if name),
      node_version='24.22.1',npm_version='12.2.1',official_source_archive_sha256=SOURCE_SHA,
      official_pnpm_lock_sha256=hashlib.sha256((source/'pnpm-lock.yaml').read_bytes()).hexdigest(),
      dependency_tree_relation='Harniqo frozen npm release combination; not the official pnpm workspace tree')
    attempts=private_directory(root/'.private/qualification',create=True)
    attempt=attempts/('dsh-build-'+secrets.token_hex(16));attempt.mkdir(mode=0o700)
    record.update(status='building',model_requests=0)
    try:
        validate_image(BASE,expected=BASE)
        # Do not overwrite an existing different tag. Absent tags are created
        # only after verifying the immutable source image on the local daemon.
        inspected=command('docker','image','inspect',BASE_TAG,capture_output=True,text=True,timeout=20,check=False)
        if inspected.returncode==0 and json.loads(inspected.stdout)[0]['Id']!=BASE:raise ValueError('base_tag_collision')
        run('docker','tag',BASE,BASE_TAG)
        if json.loads(command('docker','image','inspect',BASE_TAG,capture_output=True,text=True).stdout)[0]['Id']!=BASE:raise ValueError('base_tag_mismatch')
        shutil.copyfile(root/'deploy/qualification/dsh/Dockerfile',work/'Dockerfile')
        (work/'.dockerignore').write_text('*\n!Dockerfile\n!package.json\n!package-lock.json\n!node_modules\n!node_modules/**\n')
        iid=work/'build.iid'
        if iid.exists():raise ValueError('build_attempt_already_exists')
        with (attempt/'build.log').open('xb') as log:
            run('docker','build','--network=none','--pull=false','--iidfile',str(iid),'-t','harniqo-dsh:'+VERSION,str(work),stdout=log,stderr=subprocess.STDOUT,timeout=600)
        if json.loads(command('docker','image','inspect',BASE_TAG,capture_output=True,text=True).stdout)[0]['Id']!=BASE:raise ValueError('base_tag_changed')
        image_id=iid.read_text().strip();installed=validate_image(image_id)
        if installed['Config']['Labels'].get('io.harniqo.qualification.dsh.source')!=COMMIT:raise ValueError('built_identity_mismatch')
        destination=root/'images/containers/dsh'/VERSION;destination.mkdir(parents=True,exist_ok=True)
        archive=destination/(image_id.replace(':','-')+'.docker.tar')
        digest,size=archive_image(image_id,archive,record)
        record.update(status='built',image_id=image_id,archive_sha256=digest,archive_bytes=size,
          archive_relative_path=str(archive.relative_to(root)),production_isolation_passed=False,qualification_passed=False)
        output=archive.with_suffix(archive.suffix+'.provenance.json')
        with output.open('x') as stream:json.dump(record,stream,indent=2);stream.write('\n')
        shutil.copyfile(work/'package-lock.json',root/'deploy/qualification/dsh/package-lock.json')
        (root/'deploy/qualification/dsh/runtime-inputs.json').write_text(json.dumps(record,indent=2)+'\n')
    except BaseException as error:
        record.update(status='failed',failure={'code':getattr(error,'code',type(error).__name__)})
        raise
    finally:write_report(attempt/'operator.json',record)
    print(json.dumps(record))

if __name__ == '__main__':
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,required=True)
    parser.add_argument('--build',action='store_true')
    args=parser.parse_args()
    (build if args.build else prepare)(args.root.resolve())
