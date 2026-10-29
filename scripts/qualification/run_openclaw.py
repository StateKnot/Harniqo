#!/usr/bin/env python3
"""Bound one pinned native Gateway case; retain evidence and prove worker stop.

This trusted qualification operator is not the production execution boundary.
Validation and preflight never submit a model task. Smoke submits one task and
never retries an unknown outcome. Credentials remain in original private files.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import subprocess
import sys
import time

from prepare_openclaw import COMMIT, MANIFEST, CONFIG, VERSION
from probe_hermes import Rejected, digest, json_value, no_links, private_directory, read_file, require, write_report
from run_hermes_qualification import (DOCKER_HOST, TASK_LABEL, cleanup, create_private,
                                     docker, inspect, owned, port_available, select_endpoint)

SOURCES = (
    'deploy/qualification/openclaw/entrypoint.mjs',
    'deploy/qualification/openclaw/transport_audit.mjs',
    'scripts/qualification/probe_openclaw.mjs',
)


def launch_arguments(root, case, key, name, attempt, image, port, mode):
    mounts = [(case/'home', '/data/openclaw', False), (case/'workspace', '/data/workspace', False),
              (case/'evidence', '/data/evidence', False), (case/'model.json', '/run/config/model.json', True),
              (key, '/run/secrets/model-api-key', True), (case/'api-token', '/run/secrets/api-token', True)]
    mounts.extend((root/path, '/opt/harniqo/'+path, True) for path in SOURCES)
    mounts.append((root/SOURCES[1], '/opt/harniqo/transport_audit.mjs', True))
    args = ['run', '--detach', '--pull', 'never', '--name', name, '--label', TASK_LABEL+'='+attempt,
            '--label', 'io.harniqo.task=m0-native-qualification',
            '--user', f'{os.geteuid()}:{os.getegid()}', '--init', '--read-only', '--cap-drop', 'ALL',
            '--security-opt', 'no-new-privileges', '--pids-limit', '128', '--memory', '4g', '--cpus', '2',
            '--log-driver', 'local', '--log-opt', 'max-size=10m', '--log-opt', 'max-file=2',
            '--tmpfs', '/tmp:rw,noexec,nosuid,nodev,size=256m,mode=1777', '--entrypoint', 'node']
    if mode == 'validate':
        args += ['--network', 'none']
    else:
        args += ['--publish', f'127.0.0.1:{port}:18789']
    for source, target, readonly in mounts:
        require(',' not in str(source) and '\n' not in str(source), 'mount_path_rejected')
        args += ['--mount', f'type=bind,src={source},dst={target}'+(',readonly' if readonly else '')]
    return [*args, image, '/opt/harniqo/'+SOURCES[0], *(['--validate'] if mode == 'validate' else [])]


def execute(args):
    root, case = no_links(args.root), no_links(args.case)
    require(sys.platform == 'linux', 'linux_operator_required')
    require(os.geteuid() != 0, 'non_root_operator_required')
    require(shutil.disk_usage(root).free >= 10*1024**3, 'insufficient_disk_space')
    require(10000 <= args.port <= 10100 and args.port != 10023, 'port_outside_authorized_range')
    provenance = json_value(read_file(no_links(args.provenance), 65536, private=False))
    require(provenance.get('source_commit') == COMMIT and provenance.get('version') == VERSION
            and provenance.get('registry_manifest_digest') == MANIFEST
            and provenance.get('image_config_digest') == CONFIG
            and provenance.get('acquisition_passed') is True, 'image_provenance_mismatch')
    image = provenance['image_id']
    require(image in {CONFIG, MANIFEST}, 'image_store_identity_mismatch')
    installed = json_value(docker('image', 'inspect', image).stdout)[0]
    require(installed['Os'] == 'linux' and installed['Architecture'] == 'amd64'
            and installed['Config']['Labels']['org.opencontainers.image.revision'] == COMMIT,
            'installed_image_mismatch')
    model, key = select_endpoint(no_links(args.config), args.endpoint_id)
    source_hashes = {name: digest(read_file(no_links(root/name), 131072, private=False)) for name in SOURCES}
    require(not case.exists(), 'case_already_exists')
    private_directory(case.parent, create=True)
    locks = private_directory(root/'.private/qualification/locks', create=True)
    descriptor = os.open(locks/f'openclaw-port-{args.port}', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'a+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        require(port_available(args.port), 'port_in_use')
        case.mkdir(mode=0o700)
        for name in ('home', 'workspace', 'evidence'):
            (case/name).mkdir(mode=0o700)
        create_private(case/'model.json', json.dumps(model, sort_keys=True).encode())
        create_private(case/'api-token', secrets.token_urlsafe(48).encode())
        attempt = secrets.token_hex(16)
        name = 'harniqo-openclaw-'+attempt
        report = {'schema_version': 1, 'kind': 'native_openclaw_operator', 'source_commit': COMMIT,
                  'image_id': image, 'mode': args.mode, 'endpoint_id': args.endpoint_id,
                  'model': model['model'], 'source_sha256': source_hashes, 'attempt_id': attempt,
                  'started_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
                  'status': 'running', 'native_case_passed': False, 'milestone_complete': False,
                  'production_isolation_passed': False, 'scope': 'trusted_native_qualification'}
        write_report(case/'operator.json', report)
        identity = None
        try:
            docker(*launch_arguments(root, case, key, name, attempt, image, args.port, args.mode))
            value = inspect(name)
            identity = owned(value, attempt, image)
            report['container_id'] = identity
            if args.mode == 'validate':
                result = docker('wait', identity, timeout=90)
                require(result.stdout.strip() == b'0', 'native_configuration_rejected')
                report.update(native_configuration_passed=True, status='passed')
            else:
                require(value['ports'] == {'18789/tcp': [{'HostIp': '127.0.0.1', 'HostPort': str(args.port)}]},
                        'unexpected_port_mapping')
                deadline = time.monotonic()+90
                while True:
                    require(time.monotonic() < deadline, 'native_start_deadline')
                    value = inspect(identity); owned(value, attempt, image, identity)
                    require(value['running'], 'native_gateway_exited')
                    ready = docker('exec', identity, 'node', '-e',
                                   "let s=require('net').connect(18789,'127.0.0.1');s.on('connect',()=>{s.end();process.exit(0)});s.on('error',()=>process.exit(1));setTimeout(()=>process.exit(1),1000)",
                                   timeout=5, check=False)
                    if ready.returncode == 0:
                        break
                    time.sleep(.25)
                with (case/'probe-process.log').open('xb') as output:
                    result = subprocess.run(['docker', '--host', DOCKER_HOST, 'exec', identity, 'node',
                                             '/opt/harniqo/scripts/qualification/probe_openclaw.mjs', args.mode],
                                            stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                                            timeout=180)
                evidence = json_value(read_file(case/'evidence/probe.json', 65536))
                passed = evidence.get('native_preflight_passed' if args.mode == 'preflight' else 'native_case_passed') is True
                require(result.returncode == 0 and passed, 'native_probe_failed')
                report.update(status='passed', native_case_passed=args.mode != 'preflight',
                              native_preflight_passed=True, transport=evidence.get('transport'))
        except BaseException as error:
            report.update(status='failed', failure={'code': error.code if isinstance(error, Rejected) else type(error).__name__})
        finally:
            try:
                report['cleanup'] = cleanup(name, attempt, image, identity, case)
                if identity:
                    require(report['cleanup'].get('execution_stop_confirmed') is True
                            and report['cleanup'].get('logs_complete') is True, 'cleanup_not_confirmed')
                require(port_available(args.port), 'port_not_released')
                report['cleanup']['loopback_port_released'] = True
            except BaseException as error:
                report.update(status='failed', cleanup_failure={'code': error.code if isinstance(error, Rejected) else type(error).__name__})
            report['ended_at'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
            write_report(case/'operator.json', report)
        print(json.dumps({'status': report['status'], 'mode': args.mode, 'native_case_passed': report['native_case_passed'],
                          'cleanup_confirmed': report.get('cleanup', {}).get('execution_stop_confirmed', False),
                          'report': str((case/'operator.json').relative_to(root))}))
        return 0 if report['status'] == 'passed' else 1


def main():
    os.umask(0o077)
    def stop(signum, _):
        raise Rejected('operator_signal_'+signal.Signals(signum).name.lower())
    for signum in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, stop)
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('root', 'case', 'config', 'provenance'):
        parser.add_argument('--'+name, type=Path, required=True)
    parser.add_argument('--endpoint-id', required=True)
    parser.add_argument('--mode', choices=('validate', 'preflight', 'smoke', 'cancellation'), required=True)
    parser.add_argument('--port', type=int, default=10016)
    try:
        return execute(parser.parse_args())
    except (Rejected, ValueError, OSError) as error:
        print(json.dumps({'status': 'rejected', 'code': error.code if isinstance(error, Rejected) else type(error).__name__}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
