#!/usr/bin/env python3
"""Acquire one pinned official Linux OpenClaw image and retain its provenance.

This downloads and archives bytes. It never starts the Gateway or reads model
credentials. The official image is a qualification input, not a Harniqo release.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import stat
import secrets
import shutil
import subprocess
import sys
import tarfile
import time

VERSION = '2026.10.8'
COMMIT = 'dc2523e5a5c7a309517fd7fa834add938231d526'
REGISTRY = 'ghcr.io/openclaw/openclaw'
REGISTRIES = {'ghcr': REGISTRY, 'dockerhub': 'docker.io/openclaw/openclaw'}
INDEX = 'sha256:82b6ce63048e75ca749dbc250e872ee21f4b1c098fc6bff94636e357bcb64bf5'
MANIFEST = 'sha256:eba17fa04b4ac5616496c2ce374a95afb6db6ff52d8bf73bf87dc8c7b8656cad'
CONFIG = 'sha256:27b4542156d3701cbfd013c538ec2548545973c13cc714144350ae80125d425c'
DOCKER = ['docker', '--host', 'unix:///var/run/docker.sock']
OCI_TAG = 'harniqo-openclaw-official:2026.10.8-slim'


def verify_oci(path):
    """Bound plain OCI tar members before reading; never extract archive paths."""
    observed, metadata = {}, {}
    prefix = 'blobs/sha256/'
    total = 0
    with path.open('rb') as source:
        while True:
            header = source.read(512)
            total += len(header)
            if header == bytes(512):
                tail = source.read(10241)
                if len(tail) > 10240 or any(tail):
                    raise ValueError('OCI trailing data rejected')
                break
            if len(header) != 512 or len(observed) >= 100:
                raise ValueError('OCI member count or header rejected')
            # frombuf parses one physical header; PAX/GNU expansion is not used.
            item = tarfile.TarInfo.frombuf(header, 'utf-8', 'strict')
            name = item.name
            if (item.type not in {tarfile.REGTYPE, tarfile.AREGTYPE} or name in observed
                    or not (name in {'oci-layout', 'index.json'} or
                            re.fullmatch(r'blobs/sha256/[0-9a-f]{64}', name))
                    or item.size < 0 or item.size > 1024**3 or total + item.size > 2*1024**3):
                raise ValueError('OCI physical member rejected')
            h, remaining, data = hashlib.sha256(), item.size, bytearray()
            keep = name in {'oci-layout', 'index.json', prefix + MANIFEST.split(':')[1]}
            if keep and item.size > 65536:
                raise ValueError('OCI metadata bound')
            while remaining:
                chunk = source.read(min(1024*1024, remaining))
                if not chunk:
                    raise ValueError('Truncated OCI member')
                h.update(chunk)
                if keep:
                    data.extend(chunk)
                remaining -= len(chunk)
            padding = (-item.size) % 512
            if source.read(padding) != bytes(padding):
                raise ValueError('OCI padding rejected')
            total += item.size + padding
            observed[name] = {'size': item.size, 'digest': 'sha256:' + h.hexdigest()}
            if keep:
                metadata[name] = json.loads(data)
    manifest_name = prefix + MANIFEST.split(':')[1]
    if observed[manifest_name]['digest'] != MANIFEST:
        raise ValueError('Official manifest hash mismatch')
    manifest = metadata[manifest_name]
    if manifest['config']['digest'] != CONFIG:
        raise ValueError('Official configuration digest mismatch')
    expected = {'oci-layout', 'index.json', manifest_name}
    for item in [manifest['config'], *manifest['layers']]:
        name = prefix + item['digest'].split(':')[1]
        expected.add(name)
        if observed.get(name) != {'size': item['size'], 'digest': item['digest']}:
            raise ValueError('Official blob size or hash mismatch')
    descriptor = metadata['index.json']['manifests']
    if (set(observed) != expected or len(descriptor) != 1 or descriptor[0]['digest'] != MANIFEST
            or descriptor[0].get('annotations', {}).get('org.opencontainers.image.ref.name') != OCI_TAG
            or metadata['oci-layout'] != {'imageLayoutVersion': '1.0.0'}):
        raise ValueError('OCI layout mismatch')
    return {'sha256': sha256(path), 'bytes': path.stat().st_size, 'verified_blobs': len(manifest['layers'])+1}


def sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def checked_path(path):
    path = path.absolute()
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError('Symbolic links are not valid acquisition paths')
    return path


def write_report(path, value):
    temporary = path.with_name(path.name + '.new-' + secrets.token_hex(4))
    with temporary.open('x') as output:
        json.dump(value, output, indent=2, sort_keys=True)
        output.write('\n')
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--registry', choices=REGISTRIES, default='ghcr',
                        help='Explicit official source; both pinned manifests were independently verified equal')
    parser.add_argument('--official-oci', type=Path,
                        help='Optional offline OCI export; every blob must match the fixed official manifest')
    args = parser.parse_args()
    registry = REGISTRIES[args.registry]
    os.umask(0o077)
    root = checked_path(args.root)
    if sys.platform != 'linux' or platform.machine() not in {'x86_64', 'amd64'}:
        raise ValueError('Acquisition requires the designated Linux amd64 host')
    subprocess.run(['df', '-h', str(root)], check=True)
    docker_root = Path(subprocess.check_output([*DOCKER, 'info', '--format', '{{.DockerRootDir}}'],
                                               text=True, timeout=30).strip())
    for path in (root, docker_root):
        if shutil.disk_usage(path).free < 60 * 1024**3:
            raise ValueError('At least 60 GiB free is required on workspace and Docker filesystems')
    attempts = checked_path(root / '.private/qualification/openclaw-image')
    attempts.mkdir(parents=True, mode=0o700, exist_ok=True)
    attempt = attempts / (time.strftime('%Y%m%dT%H%M%SZ', time.gmtime()) + '-' + secrets.token_hex(4))
    attempt.mkdir(mode=0o700)
    result = {'schema_version': 1, 'kind': 'official-openclaw-image-acquisition',
              'source_commit': COMMIT, 'version': VERSION, 'platform': 'linux/amd64',
              'registry': registry, 'release_tag': VERSION + '-slim',
              'registry_index_digest': INDEX, 'registry_manifest_digest': MANIFEST,
              'image_config_digest': CONFIG, 'qualification_passed': False,
              'acquisition_passed': False, 'preparer_sha256': sha256(Path(__file__))}
    report = attempt / 'report.json'
    write_report(report, result)
    try:
        reference = registry + '@' + MANIFEST
        command = [*DOCKER, 'pull', '--platform', 'linux/amd64', reference]
        if args.official_oci:
            original = checked_path(args.official_oci)
            descriptor = os.open(original, os.O_RDONLY | os.O_NOFOLLOW)
            source = attempt / 'official-input.oci.tar'
            with os.fdopen(descriptor, 'rb') as incoming, source.open('xb') as output:
                before = os.fstat(incoming.fileno())
                if not stat.S_ISREG(before.st_mode) or before.st_size > 2*1024**3:
                    raise ValueError('OCI input type or size rejected')
                copied = 0
                for chunk in iter(lambda: incoming.read(1024*1024), b''):
                    copied += len(chunk)
                    if copied > before.st_size:
                        raise ValueError('OCI input grew during copy')
                    output.write(chunk)
                after = os.fstat(incoming.fileno())
                if copied != before.st_size or (before.st_mtime_ns, before.st_ctime_ns) != (after.st_mtime_ns, after.st_ctime_ns):
                    raise ValueError('OCI input changed during copy')
                output.flush()
                os.fsync(output.fileno())
            source.chmod(0o400)
            result['verified_official_oci'] = verify_oci(source)
            result['acquisition_method'] = 'offline_official_oci'
            # The OCI annotation is not a stable Docker tag contract. Select the
            # verified immutable manifest identity after import instead.
            reference = MANIFEST
            command = [*DOCKER, 'load', '--input', str(source)]
        with (attempt / 'pull.log').open('xb') as log:
            process = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, timeout=1800)
            log.flush()
            os.fsync(log.fileno())
        if process.returncode:
            raise ValueError('Pinned official image pull failed; original log retained')
        image = json.loads(subprocess.check_output([*DOCKER, 'image', 'inspect', reference],
                                                   timeout=30))[0]
        labels = image['Config']['Labels']
        if (image['Os'] != 'linux' or image['Architecture'] != 'amd64'
                or labels.get('org.opencontainers.image.revision') != COMMIT
                or labels.get('org.opencontainers.image.version') != VERSION
                or labels.get('org.opencontainers.image.source') != 'https://github.com/openclaw/openclaw'
                or (not args.official_oci and not {registry + '@' + MANIFEST, registry.removeprefix('docker.io/') + '@' + MANIFEST}
                    .intersection(image.get('RepoDigests', [])))
                or (args.official_oci and image['Id'] not in {CONFIG, MANIFEST})):
            raise ValueError('Image platform, source, version or repository digest mismatch')
        result.update(image_id=image['Id'], image_labels=labels,
                      configured_user=image['Config'].get('User'))
        folder = checked_path(root / 'images/containers/openclaw' / VERSION)
        folder.mkdir(parents=True, exist_ok=True)
        archive = folder / (MANIFEST.replace(':', '-') + '.docker.tar')
        provenance = archive.with_name(archive.name + '.provenance.json')
        if archive.exists() or provenance.exists():
            raise ValueError('Existing archive/provenance is retained; use its recorded identity')
        partial = folder / (archive.name + '.partial-' + secrets.token_hex(4))
        result['partial_archive'] = str(partial.relative_to(root))
        write_report(report, result)
        with (attempt / 'save.log').open('xb') as log:
            saved = subprocess.run([*DOCKER, 'save', '--output', str(partial), reference],
                                   stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, timeout=600)
        if saved.returncode or not partial.is_file() or partial.stat().st_size == 0:
            raise ValueError('Image archive failed; partial output and log retained')
        with partial.open('rb') as stream:
            os.fsync(stream.fileno())
        result.update(archive_sha256=sha256(partial), archive_bytes=partial.stat().st_size,
                      archive_relative_path=str(archive.relative_to(root)), acquisition_passed=True)
        del result['partial_archive']
        partial.chmod(0o444)
        os.rename(partial, archive)
        write_report(provenance, result)
        provenance.chmod(0o444)
    except Exception as error:
        result['failure'] = {'type': type(error).__name__, 'message': str(error)[:256]}
        raise
    finally:
        write_report(report, result)
        print(json.dumps({'acquisition_passed': result['acquisition_passed'],
                          'qualification_passed': False, 'report': str(report.relative_to(root)),
                          'image_id': result.get('image_id'), 'archive_sha256': result.get('archive_sha256')}))


if __name__ == '__main__':
    main()
