#!/usr/bin/env python3
"""Prepare verified public OpenCode inputs; optionally build/archive offline.

Default operation neither executes a downloaded binary nor starts a listener.
--build invokes only offline version/linkage checks; no model or server runs.
"""
from __future__ import annotations

import argparse
import base64
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
import time
import urllib.parse
import urllib.request

COMMIT = '810c0eba1a7348036f8bdf61e6632d0c36c6876c'
VERSION = '1.19.5'
BASE = 'python@sha256:faf7953db05ee8e060aa9fda8a389e5779cdba26a22e1363379861023e910830'
OPENCODE = {
    'filename': 'opencode-linux-x64-baseline.tar.gz', 'size': 79651821,
    'sha256': '17c200fd4b2289954cc01552559f9b6d03ec157b2ddfb5eb54144f491656333d',
    'url': 'https://github.com/anomalyco/opencode/releases/download/v1.19.5/opencode-linux-x64-baseline.tar.gz',
}
RIPGREP = {
    'filename': 'ripgrep-15.1.0-x86_64-unknown-linux-musl.tar.gz', 'size': 2263077,
    'sha256': '1c9297be4a084eea7ecaedf93eb03d058d6faae29bbc57ecdaf5063921491599',
    'url': 'https://github.com/BurntSushi/ripgrep/releases/download/15.1.0/ripgrep-15.1.0-x86_64-unknown-linux-musl.tar.gz',
}
OPENCODE_NPM = {
    'filename': 'opencode-linux-x64-baseline-1.19.5.tgz',
    'maximum': 96 * 1024**2,
    'integrity': 'sha512-9kA+st3XXtCXMPx5f3Ld1u19uoHTO2u2Btt8a2cqhJ4GIqVbD1mXLevj3LHFEmtuQoFGk+UstGnl38sCqXj5sQ==',
    'sha1': 'eef35247d05578ffa6d0333884e5b72ee6bd0b28',
    'url': 'https://registry.npmjs.org/opencode-linux-x64-baseline/-/opencode-linux-x64-baseline-1.19.5.tgz',
    'metadata_url': 'https://registry.npmjs.org/opencode-linux-x64-baseline/1.19.5',
    'unpacked_size': 214387010, 'file_count': 2,
}
# Git object identities come from the official immutable commit's recursive tree.
# In addition to validating these object bytes, provenance records SHA-256.
SOURCE_FILES = {
    'LICENSE': ('6439474beed8e0271df9862eff97ffd70ec2464c', 1065),
    'packages/opencode/package.json': ('f7acdb6d8465e88d2dc2ec7d72830ecf7e9cd6d1', 5269),
    'bun.lock': ('45098943436dfae810cd05f64376fcc887090752', 870354),
}
PUBLIC_HOSTS = {'github.com', 'raw.githubusercontent.com', 'release-assets.githubusercontent.com',
                'objects.githubusercontent.com', 'registry.npmjs.org'}


class PublicRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        approved_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def approved_url(url):
    target = urllib.parse.urlsplit(url)
    if (target.scheme != 'https' or target.hostname not in PUBLIC_HOSTS
            or target.username or target.password or target.port not in (None, 443)):
        raise ValueError('Public input left the approved HTTPS sources')


def sha(path):
    digest = hashlib.sha256()
    with path.open('rb') as source:
        for chunk in iter(lambda: source.read(1024**2), b''):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path, value):
    with path.open('x', encoding='utf-8') as output:
        json.dump(value, output, ensure_ascii=False, sort_keys=True, indent=2)
        output.write('\n')
        output.flush()
        os.fsync(output.fileno())


def download(url, path, size=None, expected_sha256=None, expected_blob=None, *, maximum=None, integrity=None):
    approved_url(url)
    if path.is_symlink():
        raise ValueError('Public input cache must not contain symlinks')

    def verify(candidate):
        actual_size = candidate.stat().st_size
        if (not candidate.is_file() or actual_size == 0 or (size is not None and actual_size != size)
                or (maximum is not None and actual_size > maximum)):
            raise ValueError('Pinned input size mismatch')
        if expected_sha256 and sha(candidate) != expected_sha256:
            raise ValueError('Pinned input SHA-256 mismatch')
        if expected_blob:
            body = candidate.read_bytes()
            identity = hashlib.sha1(b'blob ' + str(len(body)).encode() + b'\0' + body).hexdigest()
            if identity != expected_blob:
                raise ValueError('Pinned source Git blob mismatch')
        if integrity:
            if not integrity.startswith('sha512-'):
                raise ValueError('Only pinned SHA-512 SRI is supported')
            expected = base64.b64decode(integrity[7:], validate=True)
            digest = hashlib.sha512()
            with candidate.open('rb') as source:
                for chunk in iter(lambda: source.read(1024**2), b''):
                    digest.update(chunk)
            if len(expected) != 64 or digest.digest() != expected:
                raise ValueError('Pinned npm SHA-512 SRI mismatch')

    if path.exists():
        verify(path)
        return
    temporary = path.with_name(path.name + '.partial-' + str(os.getpid()))
    started, count, next_progress = time.monotonic(), 0, 8 * 1024**2
    last_progress = started
    print(json.dumps({'download': path.name, 'status': 'started'}), flush=True)
    request = urllib.request.Request(url, headers={'User-Agent': 'Harniqo-M0-qualification/1'})
    try:
        with urllib.request.build_opener(PublicRedirects()).open(request, timeout=30) as response, temporary.open('xb') as output:
            while True:
                if time.monotonic() - started > 300:
                    raise TimeoutError('Public download exceeded cooperative 300-second deadline')
                chunk = response.read1(64 * 1024)
                if not chunk:
                    break
                count += len(chunk)
                if count > (size if size is not None else maximum):
                    raise ValueError('Public input exceeds its pinned size')
                output.write(chunk)
                if count >= next_progress or time.monotonic() - last_progress >= 5:
                    print(json.dumps({'download': path.name, 'bytes': count}), flush=True)
                    next_progress = count + 8 * 1024**2
                    last_progress = time.monotonic()
            output.flush()
            os.fsync(output.fileno())
        verify(temporary)
        os.link(temporary, path)
        print(json.dumps({'download': path.name, 'status': 'verified', 'bytes': count}), flush=True)
    finally:
        temporary.unlink(missing_ok=True)


def extract_selected(archive, context, component):
    """Never extract archive paths; copy only validated regular named members."""
    outputs, seen, total = {}, set(), 0
    with tarfile.open(archive, 'r:gz') as source:
        for count, member in enumerate(source, start=1):
            name = PurePosixPath(member.name)
            if (count > 256 or name.is_absolute() or '..' in name.parts or '\\' in member.name
                    or member.name in seen or not (member.isdir() or member.isfile())
                    or member.size < 0):
                raise ValueError('Public tar archive contains unsafe/duplicate/unsupported entries')
            seen.add(member.name)
            total += member.size
            if total > 512 * 1024**2:
                raise ValueError('Public archive expands beyond its bound')
            destination = None
            if component == 'opencode' and member.name in {'opencode', 'bin/opencode'} and member.isfile():
                destination = context / 'opencode'
            if component == 'opencode-npm' and member.isfile():
                if member.name == 'package/bin/opencode':
                    destination = context / 'opencode'
                if member.name == 'package/package.json':
                    if member.size > 16384:
                        raise ValueError('npm package metadata exceeds its bound')
                    destination = context / 'upstream' / 'opencode-npm-package.json'
            if component == 'ripgrep' and name.parts[:1] == ('ripgrep-15.1.0-x86_64-unknown-linux-musl',):
                if len(name.parts) == 2 and name.name in {'rg', 'COPYING', 'LICENSE-MIT', 'UNLICENSE'} and member.isfile():
                    destination = context / 'rg' if name.name == 'rg' else context / 'upstream' / ('ripgrep-' + name.name)
            if destination is not None:
                if destination.name in outputs:
                    raise ValueError('Duplicate selected binary or license')
                incoming = source.extractfile(member)
                if incoming is None:
                    raise ValueError('Missing selected regular file')
                with incoming, destination.open('xb') as output:
                    shutil.copyfileobj(incoming, output)
                destination.chmod(0o555 if destination.name in {'opencode', 'rg'} else 0o444)
                outputs[destination.name] = {'size': destination.stat().st_size, 'sha256': sha(destination),
                                             'archive_member': member.name}
    required = ({'opencode'} if component == 'opencode' else
                {'opencode', 'opencode-npm-package.json'} if component == 'opencode-npm' else
                {'rg', 'ripgrep-COPYING', 'ripgrep-LICENSE-MIT', 'ripgrep-UNLICENSE'})
    if set(outputs) != required:
        raise ValueError('Official archive omitted an expected binary or license')
    if component == 'opencode-npm':
        package = json.loads((context / 'upstream' / 'opencode-npm-package.json').read_text())
        if (package.get('name') != 'opencode-linux-x64-baseline' or package.get('version') != VERSION
                or package.get('os') != ['linux'] or package.get('cpu') != ['x64'] or package.get('scripts')):
            raise ValueError('npm platform package identity or script-free metadata mismatch')
    for name in required & {'opencode', 'rg'}:
        with (context / name).open('rb') as binary:
            header = binary.read(20)
        if len(header) != 20 or header[:6] != b'\x7fELF\x02\x01' or header[18:20] != b'\x3e\x00':
            raise ValueError('Selected runtime is not an ELF64 little-endian x86_64 binary')
    return outputs


def command(args, **kwargs):
    return subprocess.run(args, check=True, text=True, capture_output=True, timeout=kwargs.pop('timeout', 60), **kwargs).stdout


def build(root, context, inputs, recipe):
    if platform.system() != 'Linux' or platform.machine() not in {'x86_64', 'AMD64'}:
        raise ValueError('--build requires the designated native Linux amd64 Docker host')
    docker_root = Path(command(['docker', 'info', '--format', '{{.DockerRootDir}}']).strip())
    for directory in (root, docker_root):
        # Display actual df and independently enforce the byte threshold.
        print(command(['df', '-h', str(directory)]), end='', flush=True)
        if shutil.disk_usage(directory).free < 60 * 1024**3:
            raise ValueError('Offline build requires 60 GiB free on workspace and Docker filesystems')
    image = json.loads(command(['docker', 'image', 'inspect', BASE]))[0]
    if image.get('Os') != 'linux' or image.get('Architecture') != 'amd64':
        raise ValueError('Pinned cached Python base is not Linux amd64')
    iid = context / 'image-id'
    args = ['docker', 'build', '--network=none', '--pull=false', '--platform=linux/amd64',
            '--build-arg', 'PYTHON_BASE=' + BASE, '--iidfile', str(iid), str(context)]
    evidence = root / '.private' / 'qualification' / 'opencode-build'
    evidence.mkdir(parents=True, exist_ok=True, mode=0o700)
    timestamp = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime()) + '-' + str(os.getpid())
    log = evidence / (timestamp + '.log')
    # Establish durable evidence before invoking the builder. Streaming to disk
    # also avoids losing output or retaining unbounded build output in memory.
    attempt = evidence / (timestamp + '.attempt.json')
    write_json(attempt, {'status': 'started', 'started_at': timestamp, 'inputs': inputs,
                         'log': log.name, 'qualification_passed': False})
    with log.open('x', encoding='utf-8') as output:
        try:
            result = subprocess.run(args, text=True, stdout=output, stderr=subprocess.STDOUT, timeout=900)
        except BaseException:
            output.flush()
            os.fsync(output.fileno())
            write_json(evidence / (timestamp + '.failed.json'), {'status': 'interrupted_or_failed',
                       'log': log.name, 'qualification_passed': False})
            raise
        output.flush()
        os.fsync(output.fileno())
    if result.returncode != 0:
        write_json(evidence / (timestamp + '.failed.json'), {'status': 'build_failed',
                   'returncode': result.returncode, 'log': log.name, 'qualification_passed': False})
        raise ValueError('Offline OpenCode build failed; preserved private build log')
    build_result_id = iid.read_text().strip()
    if not re.fullmatch(r'sha256:[0-9a-f]{64}', build_result_id):
        raise ValueError('Docker build returned an invalid image store identity')
    inspected = json.loads(command(['docker', 'image', 'inspect', build_result_id]))[0]
    identity = inspected.get('Id')
    if (inspected.get('Os') != 'linux' or inspected.get('Architecture') != 'amd64'
            or not isinstance(identity, str) or not re.fullmatch(r'sha256:[0-9a-f]{64}', identity)):
        raise ValueError('Built image inspection does not match its identity/platform')
    runtime_inventory = None
    container = command(['docker', 'create', '--network=none', identity]).strip()
    try:
        if not re.fullmatch(r'[0-9a-f]{64}', container):
            raise ValueError('Unexpected inventory container identity')
        inventory_file = context / 'runtime-inventory.json'
        command(['docker', 'cp', container + ':/opt/harniqo/runtime-inventory.json', str(inventory_file)])
        runtime_inventory = json.loads(inventory_file.read_text())
        if runtime_inventory.get('opencode') != VERSION or runtime_inventory.get('qualification_passed') is not False:
            raise ValueError('Offline runtime inventory did not match the pinned qualification scope')
    finally:
        if re.fullmatch(r'[0-9a-f]{64}', container):
            command(['docker', 'rm', container])
    destination = root / 'images' / 'containers' / 'opencode' / VERSION
    destination.mkdir(parents=True, exist_ok=True)
    archive = destination / ('sha256-' + identity[7:] + '.docker.tar')
    provenance = archive.with_suffix(archive.suffix + '.provenance.json')
    if archive.exists() or provenance.exists():
        raise ValueError('Image archive or provenance exists; refusing to overwrite evidence')
    temporary = archive.with_name(archive.name + '.partial-' + str(os.getpid()))
    try:
        command(['docker', 'image', 'save', '-o', str(temporary), identity], timeout=300)
        with temporary.open('rb') as source:
            os.fsync(source.fileno())
        temporary.chmod(0o444)
        os.link(temporary, archive)
    finally:
        temporary.unlink(missing_ok=True)
    record = {**inputs, 'image_id': identity, 'image_digest': identity, 'build_result_id': build_result_id,
              'digest_kind': 'docker-image-store-id', 'image_config_digest': None,
              'registry_manifest_digest': None, 'archive_format': 'docker',
              'archive_sha256': sha(archive), 'archive_size': archive.stat().st_size,
              'recipe_sha256': sha(recipe), 'runtime_inventory': runtime_inventory,
              'build_network': 'none', 'qualification_passed': False}
    write_json(provenance, record)
    provenance.chmod(0o444)
    descriptor = os.open(destination, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    write_json(evidence / (timestamp + '.completed.json'), {'status': 'built_and_archived',
               'image_id': identity, 'build_result_id': build_result_id,
               'archive_sha256': record['archive_sha256'], 'qualification_passed': False})
    print(json.dumps({'image_id': identity, 'archive': str(archive), 'provenance': str(provenance),
                      'qualification_passed': False}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument('--build', action='store_true', help='Build offline and archive after preparing public inputs')
    parser.add_argument('--binary-source', choices=('github-release', 'npm-package'), default='github-release',
                        help='Explicit same-version public binary format; failures never switch sources')
    args = parser.parse_args()
    root = args.root.resolve(strict=True)
    os.umask(0o077)
    state = root / '.state' / 'qualification' / 'opencode'
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not state.resolve().is_relative_to(root) or state.is_symlink():
        raise ValueError('Qualification cache escaped the workspace')
    lock = os.open(state / 'prepare.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(lock, 'w') as locked:
        fcntl.flock(locked, fcntl.LOCK_EX | fcntl.LOCK_NB)
        selected = OPENCODE if args.binary_source == 'github-release' else OPENCODE_NPM
        for artifact in (selected, RIPGREP):
            download(artifact['url'], state / artifact['filename'], artifact.get('size'), artifact.get('sha256'),
                     maximum=artifact.get('maximum'), integrity=artifact.get('integrity'))
        source_inputs = {}
        for path, (blob, size) in SOURCE_FILES.items():
            url = 'https://raw.githubusercontent.com/anomalyco/opencode/' + COMMIT + '/' + path
            cached = state / ('upstream-' + path.replace('/', '-'))
            download(url, cached, size, expected_blob=blob)
            source_inputs[path] = {'url': url, 'git_blob_sha1': blob, 'sha256': sha(cached), 'size': size}
        if json.loads((state / 'upstream-packages-opencode-package.json').read_text())['version'] != VERSION:
            raise ValueError('Pinned source package version mismatch')
        recipe_dir = root / 'deploy' / 'qualification' / 'opencode'
        with tempfile.TemporaryDirectory(prefix='context-', dir=state) as temporary:
            context = Path(temporary)
            (context / 'upstream').mkdir()
            binaries = {}
            for artifact, component in ((selected, 'opencode' if args.binary_source == 'github-release' else 'opencode-npm'),
                                         (RIPGREP, 'ripgrep')):
                binaries.update(extract_selected(state / artifact['filename'], context, component))
            for path in SOURCE_FILES:
                shutil.copyfile(state / ('upstream-' + path.replace('/', '-')),
                                context / 'upstream' / path.replace('/', '-'))
            for filename in ('Dockerfile', 'entrypoint.py'):
                source = recipe_dir / filename
                if source.is_symlink() or not source.is_file():
                    raise ValueError('Recipe files must be regular source files')
                shutil.copyfile(source, context / filename)
            inputs = {'component': 'opencode', 'version': VERSION, 'commit': COMMIT,
                      'official_release_url': 'https://github.com/anomalyco/opencode/releases/tag/v' + VERSION,
                      'source_commit_url': 'https://github.com/anomalyco/opencode/tree/' + COMMIT,
                      'platform': 'linux/amd64', 'python_base': BASE,
                      'binary_source': args.binary_source,
                      'release_archives': {'opencode': {**selected, 'observed_sha256': sha(state / selected['filename']),
                                                        'observed_size': (state / selected['filename']).stat().st_size},
                                           'ripgrep': RIPGREP},
                      'source_inputs': source_inputs, 'extracted_inputs': binaries,
                      'recipe_sha256': sha(context / 'Dockerfile'),
                      'entrypoint_sha256': sha(context / 'entrypoint.py'),
                      'preparer_sha256': sha(Path(__file__).resolve()), 'qualification_passed': False}
            write_json(context / 'inputs.json', inputs)
            record = state / ('inputs-' + hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest() + '.json')
            if not record.exists():
                write_json(record, inputs)
            if args.build:
                build(root, context, inputs, context / 'Dockerfile')
            else:
                print(json.dumps({'prepared': True, 'inputs_manifest': str(record),
                                  'executed_downloaded_binary': False, 'qualification_passed': False}), flush=True)


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, TimeoutError, subprocess.SubprocessError, tarfile.TarError) as error:
        raise SystemExit('OpenCode preparation failed: ' + str(error)) from None
