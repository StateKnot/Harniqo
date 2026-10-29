#!/usr/bin/env python3
"""Freeze official PyPI wheels for independent Octop service / Harness scopes.

Run on Linux amd64. Resolution is online, image install/build is offline.
No model credentials or native tasks are used here. Failed attempts are retained.
"""
import argparse
import email
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import time
import tarfile
import urllib.request
import uuid
import zipfile
from run_octop import docker_args, docker_env, finalize_container

BASE = 'python@sha256:faf7953db05ee8e060aa9fda8a389e5779cdba26a22e1363379861023e910830'
RELEASES = {'harness': ['octop-harness==1.0.5'], 'service': ['octop==1.0.3b1', 'octop-harness==1.0.5']}
COMMITS = {'harness': '76cd38d7b8ffc66c28ba24ad60bbab206a95d3ac', 'service': '890111b7879665840414ca28ab168b05b0db0921'}

def digest(path):
    result = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024*1024), b''):
            result.update(chunk)
    return result.hexdigest()

def wheel_metadata(path):
    with zipfile.ZipFile(path) as archive:
        names = [n for n in archive.namelist() if n.count('/') == 1 and n.endswith('.dist-info/METADATA')]
        if len(names) != 1:
            raise ValueError('ambiguous wheel metadata')
        meta = email.message_from_bytes(archive.read(names[0]))
        return meta['Name'], meta['Version']

def official_record(path):
    name, version = wheel_metadata(path)
    url = f'https://pypi.org/pypi/{name}/{version}/json'
    with urllib.request.urlopen(url, timeout=60) as response:
        metadata = json.load(response)
    matches = [r for r in metadata['urls'] if r['filename'] == path.name]
    if len(matches) != 1:
        raise ValueError('wheel absent from official release')
    record = matches[0]
    if not record['url'].startswith('https://files.pythonhosted.org/'):
        raise ValueError('unexpected wheel source')
    actual = digest(path)
    if record['digests']['sha256'] != actual or record['size'] != path.stat().st_size:
        raise ValueError('official wheel digest/size mismatch')
    return dict(name=name, version=version, filename=path.name, sha256=actual,
                size=path.stat().st_size, url=record['url'], metadata_url=url,
                requires_python=record.get('requires_python'), yanked=record.get('yanked', False))

def run(command, log, timeout=1800, *, runner=None):
    runner=runner or subprocess.run
    owned_run=command[:2] == ['docker','run'] and '--name' in command
    ownership=uuid.uuid4().hex
    name=command[command.index('--name')+1] if owned_run else None
    if owned_run:
        if BASE not in command:raise ValueError('builder requires fixed immutable base')
        command=[c for c in command if c != '--rm']
        command=command[:2]+['--label','io.harniqo.attempt='+ownership]+command[2:]
    result=None
    try:
        with log.open('xb') as stream:
            result=runner(docker_args(command),stdout=stream,stderr=subprocess.STDOUT,
                          timeout=timeout,env=docker_env())
    finally:
        if owned_run:
            cleanup=finalize_container(name,ownership,log.parent,expected_image=BASE,runner=runner)
            (log.parent/(log.name+'.cleanup.json')).write_text(json.dumps(cleanup,indent=2)+'\n')
            if not cleanup['pid_zero'] or not cleanup['removed'] or cleanup['cleanup_errors']:
                raise RuntimeError('owned container cleanup not verified; see cleanup report')
    if result is None or result.returncode:
        raise RuntimeError(f'command failed; see {log.name}')

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--scope', choices=RELEASES, required=True)
    parser.add_argument('--download', action='store_true')
    parser.add_argument('--build', action='store_true')
    args = parser.parse_args()
    root = args.root.resolve()
    if platform.system() != 'Linux' or platform.machine() != 'x86_64':
        raise SystemExit('requires designated Linux amd64 environment')
    subprocess.run(['df', '-h', str(root)], check=True)
    if shutil.disk_usage(root).free < 60 * 1024**3:
        raise SystemExit('requires 60 GiB free space')
    inputs = root / 'images/sources/octop'
    wheels = inputs / f'{args.scope}-wheels'
    wheels.mkdir(parents=True, exist_ok=True)
    state = root / '.state/qualification/octop'
    state.mkdir(parents=True, exist_ok=True)
    attempt = root / '.private/qualification/octop' / (time.strftime('%Y%m%dT%H%M%SZ', time.gmtime()) + '-' + uuid.uuid4().hex[:8])
    attempt.mkdir(parents=True, mode=0o700)
    report = dict(scope=args.scope, source_commit=COMMITS[args.scope], base=BASE,
                  qualification_passed=False, status='preparing', model_calls=0)
    context = state / ('context-' + attempt.name)
    try:
        if args.download:
            run(['docker', 'run', '--rm', '--name', 'harniqo-octop-prepare-' + attempt.name,
                 '--label', 'io.harniqo.qualification=octop', '--user', f'{os.getuid()}:{os.getgid()}',
                 '--read-only', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
                 '--pids-limit', '128', '--memory', '4g', '--cpus', '2',
                 '--tmpfs', '/tmp:rw,nosuid,nodev,size=512m,mode=1777',
                 '-v', f'{inputs}:/inputs', '-v', f'{state}:/state',
                 '-e', f'PIP_CACHE_DIR=/state/pip-{args.scope}-cache', BASE,
                 'python', '-m', 'pip', 'download', '--only-binary=:all:',
                 '--index-url', 'https://pypi.org/simple', '--dest', f'/inputs/{args.scope}-wheels',
                 *RELEASES[args.scope]], attempt / 'download.log')
        from concurrent.futures import ThreadPoolExecutor
        paths = sorted(wheels.glob('*.whl'))
        if not paths:
            raise ValueError('empty wheelhouse')
        if any(p.is_symlink() for p in paths):
            raise ValueError('symlink wheel rejected')
        with ThreadPoolExecutor(max_workers=8) as pool:
            records = list(pool.map(official_record, paths))
        names = [r['name'].lower().replace('_', '-') for r in records]
        if len(set(names)) != len(names):
            raise ValueError('duplicate distribution')
        if any(r['yanked'] for r in records):
            raise ValueError('yanked distribution requires separate review')
        expected = dict(spec.split('==') for spec in RELEASES[args.scope])
        for name, version in expected.items():
            if not any(r['name'].lower().replace('_', '-') == name and r['version'] == version for r in records):
                raise ValueError('release pin missing')
        source = inputs / 'harness-76cd38d.tar.gz'
        if digest(source) != '75fcfd10b9d7a654e3092a875d931deb935177f577195f339f895210f09db9e8':
            raise ValueError('pinned upstream source archive mismatch')
        harness_wheel = next(p for p in paths if wheel_metadata(p) == ('octop-harness','1.0.5'))
        source_checked = 0
        with tarfile.open(source, 'r:gz') as tar, zipfile.ZipFile(harness_wheel) as wheel:
            for item in tar.getmembers():
                prefix = 'octop-harness-76cd38d7b8ffc66c28ba24ad60bbab206a95d3ac/src/'
                if item.name.startswith(prefix) and item.name.endswith('.py') and item.isfile():
                    target = item.name[len(prefix):]
                    if tar.extractfile(item).read() != wheel.read(target):
                        raise ValueError('wheel/source Python mismatch: '+target)
                    source_checked += 1
        if source_checked < 50:
            raise ValueError('incomplete source comparison')
        report['source_python_files_verified'] = source_checked
        report['source_archive_sha256'] = digest(source)
        lock = ''.join(f"{r['name']}=={r['version']} --hash=sha256:{r['sha256']}\n" for r in records)
        if args.scope == 'harness' and hashlib.sha256(lock.encode()).hexdigest() != 'f566ec85a38feda0529dd1a9313f5d0289af2aa86207fab81765531e91ed3d71':
            raise ValueError('frozen Harness dependency closure changed')
        (attempt / 'requirements.lock').write_text(lock)
        (attempt / 'wheel-provenance.json').write_text(json.dumps(records, indent=2) + '\n')
        report.update(status='frozen', wheels=len(records), bytes=sum(r['size'] for r in records), lock_sha256=digest(attempt/'requirements.lock'))
        if args.build:
            context.mkdir()
            (context/'wheels').mkdir()
            for path in paths:
                shutil.copyfile(path, context/'wheels'/path.name)
                if digest(context/'wheels'/path.name) != digest(path):
                    raise ValueError('context copy mismatch')
            shutil.copyfile(attempt/'requirements.lock', context/'requirements.lock')
            recipe = root/'deploy/qualification/octop/Dockerfile'
            shutil.copyfile(recipe, context/'Dockerfile')
            image_tag = 'harniqo-octop-' + args.scope + ':' + attempt.name
            run(['docker', 'build', '--network=none', '--pull=false', '--build-arg', f'PYTHON_BASE={BASE}',
                 '--tag', image_tag, str(context)], attempt/'build.log')
            image_id = subprocess.check_output(docker_args(['docker','image','inspect',image_tag,'--format','{{.Id}}']), text=True, env=docker_env()).strip()
            target = root/'images/containers/octop'/args.scope
            target.mkdir(parents=True, exist_ok=True)
            archive = target/(image_id.replace(':','-')+'.docker.tar')
            if archive.exists():
                raise ValueError('refusing overwrite of archive')
            run(['docker','save','-o',str(archive),image_id],attempt/'save.log')
            archive.chmod(0o444)
            report.update(status='built', image_id=image_id, archive_sha256=digest(archive), archive_bytes=archive.stat().st_size,
                          recipe_sha256=digest(recipe), archive_relative=str(archive.relative_to(root)))
        print(json.dumps(report))
    except Exception as error:
        report.update(status='failed', error_type=type(error).__name__, error=str(error))
        raise
    finally:
        (attempt/'report.json').write_text(json.dumps(report, indent=2)+'\n')
        if context.exists():
            shutil.rmtree(context)

if __name__ == '__main__':
    main()
