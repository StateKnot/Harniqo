#!/usr/bin/env python3
"""Qualify dedicated Linux identities using real, isolated systemd processes.

The privileged parent only supervises its own transient units and new canaries.
The unchanged production Broker CLI runs as its ordinary dedicated UID. A fresh
fixed catalog is admitted, but no valid Assignment is issued and no VM lifecycle
is exercised. libvirt delegation and production activation remain BLOCKED.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
import errno
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import secrets
import selectors
import signal
import socket
import stat
import struct
import subprocess
import sys
import time
import uuid

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
from apps.runner.resource_broker import Assignment, Grant, DISK_BYTES, prepare_seed_access
from apps.runner.resource_broker import ipc, service

MAX_OUTPUT = 65536
MAX_REPORT = 4 * 1024**2
MAX_UNITS = 24
TOTAL_SECONDS = 180
ENVIRONMENT = {'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LC_ALL': 'C',
               'PYTHONDONTWRITEBYTECODE': '1', 'SYSTEMD_COLORS': '0'}
MANAGEMENT_SOCKETS = (
    '/run/libvirt/libvirt-sock', '/run/libvirt/libvirt-sock-ro',
    '/run/libvirt/libvirt-admin-sock', '/run/libvirt/virtqemud-sock',
    '/run/libvirt/virtqemud-sock-ro', '/run/libvirt/virtqemud-admin-sock',
    '/run/libvirt/virtproxyd-sock', '/run/libvirt/virtproxyd-sock-ro',
    '/run/libvirt/virtproxyd-admin-sock', '/run/docker.sock',
    '/run/containerd/containerd.sock', '/run/podman/podman.sock',
)
SOURCE_FILES = ('apps/runner/resource_broker/__init__.py', 'apps/runner/resource_broker/ipc.py',
    'apps/runner/resource_broker/service.py', 'apps/runner/resource_broker/resources.py',
    'scripts/qualification/qualify_broker_identities.py')


class QualificationError(ValueError):
    """Finite diagnostic; native stderr and sensitive data are never printed."""


def _require(condition, code):
    if not condition:
        raise QualificationError(code)


def _path(value):
    _require(type(value) is str and value and len(os.fsencode(value)) <= 4096
        and not any(ord(c) < 32 or ord(c) == 127 for c in value), 'path_invalid')
    result = Path(value)
    _require(result.is_absolute() and str(result) == value and '..' not in result.parts
        and not value.startswith('//') and result != Path('/'), 'path_invalid')
    return result


def _ancestors(path, *, root_owned=True):
    for item in (path, *path.parents):
        info = item.lstat()
        _require(not stat.S_ISLNK(info.st_mode), 'symlink_rejected')
        if item != path or stat.S_ISDIR(info.st_mode):
            _require(stat.S_ISDIR(info.st_mode) and not info.st_mode & 0o022
                and (not root_owned or info.st_uid == 0), 'ancestor_untrusted')


def _read(path, *, limit=MAX_OUTPUT, owner=0, modes=None):
    _ancestors(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        before = os.fstat(stream.fileno())
        _require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1
            and before.st_uid == owner and not before.st_mode & 0o022
            and 0 <= before.st_size <= limit
            and (modes is None or stat.S_IMODE(before.st_mode) in modes), 'file_untrusted')
        data = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
        _require(len(data) <= limit and _token(before) == _token(after)
            and _token(before) == _token(path.lstat()), 'file_changed')
        return data


def _token(info):
    return (info.st_dev, info.st_ino, info.st_uid, info.st_gid, info.st_mode,
            info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _json(data):
    return (json.dumps(data, sort_keys=True, separators=(',', ':'), allow_nan=False) + '\n').encode()


def _pairs(pairs):
    value = {}
    for key, item in pairs:
        _require(key not in value, 'json_duplicate_member')
        value[key] = item
    return value


def _decode(data):
    return json.loads(data.decode('utf-8'), object_pairs_hook=_pairs,
        parse_constant=lambda unused: (_ for _ in ()).throw(QualificationError('json_nonfinite')))


def _write_new(path, data, *, uid=0, gid=0, mode=0o600):
    _require(len(data) <= MAX_REPORT, 'evidence_capacity_exceeded')
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, mode)
    try:
        os.fchown(fd, uid, gid)
        os.fchmod(fd, mode)
        with os.fdopen(fd, 'wb', closefd=False) as stream:
            stream.write(data); stream.flush(); os.fsync(fd)
    finally:
        os.close(fd)


def _command(arguments, *, timeout=8, output_limit=MAX_OUTPUT, allowed_failure=False):
    """Bound real child output and time; never use a shell or unbounded capture."""
    process = subprocess.Popen(arguments, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, env=ENVIRONMENT, close_fds=True)
    data = bytearray(); deadline = time.monotonic() + timeout
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                _require(remaining > 0, 'command_timeout')
                for key, unused in selector.select(min(remaining, .1)):
                    chunk = os.read(key.fd, 8192)
                    if not chunk:
                        selector.unregister(key.fileobj)
                    else:
                        _require(len(data) + len(chunk) <= output_limit, 'command_output_limit')
                        data.extend(chunk)
            result = process.wait(timeout=max(.01, deadline - time.monotonic()))
        _require(allowed_failure or result == 0, 'command_failed')
        return result, data.decode('utf-8', errors='strict')
    except subprocess.TimeoutExpired:
        raise QualificationError('command_timeout') from None
    finally:
        if process.poll() is None:
            process.kill(); process.wait(timeout=2)
        process.stdout.close()


def _process(pid):
    base = Path('/proc') / str(pid)
    first = (base / 'stat').read_text()
    fields = {}
    for line in (base / 'status').read_text().splitlines():
        key, separator, value = line.partition(':')
        if separator and key in {'Uid', 'Gid', 'Groups', 'CapInh', 'CapPrm', 'CapEff',
                'CapBnd', 'CapAmb', 'NoNewPrivs', 'Seccomp'}:
            fields[key] = value.split()
    namespaces = {key: os.readlink(base / 'ns' / key) for key in ('net', 'mnt', 'user')}
    # comm can contain spaces and parentheses; starttime is field 22.
    start = first[first.rfind(')') + 2:].split()[19]
    second = (base / 'stat').read_text()
    _require(start == second[second.rfind(')') + 2:].split()[19], 'process_identity_changed')
    return {'pid': pid, 'start_ticks': start, 'status': fields, 'namespaces': namespaces}


def _identity(evidence, uid, gid, allowed_groups, host_namespaces):
    fields = evidence['status']
    _require(fields.get('Uid') == [str(uid)] * 4 and fields.get('Gid') == [str(gid)] * 4,
        'native_identity_mismatch')
    groups = {int(value) for value in fields.get('Groups', [])}
    _require(groups <= set(allowed_groups) and allowed_groups[-1] in groups, 'native_groups_mismatch')
    _require(all(int(''.join(fields.get(key, ['-1'])), 16) == 0
        for key in ('CapInh', 'CapPrm', 'CapEff', 'CapBnd', 'CapAmb'))
        and fields.get('NoNewPrivs') == ['1'] and fields.get('Seccomp') == ['2'],
        'native_privileges_mismatch')
    _require(evidence['namespaces']['net'] != host_namespaces['net']
        and evidence['namespaces']['user'] == host_namespaces['user'], 'native_namespaces_mismatch')


def _worker_evidence(uid, gid, groups, namespaces):
    _require(os.getuid() != 0 and os.getuid() == os.geteuid()
        and os.getgid() == os.getegid(), 'worker_privileged')
    value = _process(os.getpid())
    _identity(value, uid, gid, groups, namespaces)
    return value


def _metadata(path, *, digest=False):
    info = path.lstat()
    value = {'device': info.st_dev, 'inode': info.st_ino, 'uid': info.st_uid,
        'gid': info.st_gid, 'mode': format(stat.S_IMODE(info.st_mode), '04o'), 'bytes': info.st_size}
    if digest:
        # Metadata only: issuer bytes and VM disk contents are never read.
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        with os.fdopen(fd, 'rb') as stream:
            data = stream.read(MAX_OUTPUT + 1)
            _require(len(data) <= MAX_OUTPUT and _token(info) == _token(os.fstat(stream.fileno())),
                'state_changed')
        value['sha256'] = hashlib.sha256(data).hexdigest()
    return value


def _state(config):
    _require(set(path.name for path in config.state_dir.iterdir()) ==
        {'anchor.json', 'journal.json', 'broker.lock'}, 'state_not_empty_catalog')
    values = {name: _metadata(config.state_dir / name, digest=True)
        for name in ('anchor.json', 'journal.json', 'broker.lock')}
    # The real normal-UID Broker authenticates this same journal on every call.
    envelope = _decode((config.state_dir / 'journal.json').read_bytes())
    _require(envelope['body']['records'] == {} and envelope['body']['workspace_epochs'] == {},
        'resource_records_present')
    return values


def _directory(path, uid, gid, mode):
    # Data stays on the designated developer-owned images filesystem. Its
    # ancestors are separately fingerprinted and checked against the peer UID;
    # do not chmod shared developer directories to manufacture admission.
    for item in (path, *path.parents):
        _require(stat.S_ISDIR(item.lstat().st_mode), 'data_ancestor_untrusted')
    info = path.lstat()
    _require(stat.S_ISDIR(info.st_mode) and info.st_uid == uid and info.st_gid == gid
        and stat.S_IMODE(info.st_mode) == mode, 'directory_untrusted')
    _require(not {'system.posix_acl_access', 'system.posix_acl_default'} & set(os.listxattr(path)),
        'directory_acl_rejected')


def _data_ancestors(config, peer_uid, peer_groups):
    values = {}
    roots = (config.state_dir, config.issuer_key_file.parent, config.images_root,
             config.channel_root, config.ipc_directory)
    for root in roots:
        for path in (root, *root.parents):
            if str(path) in values:
                continue
            _require(len(values) < 64, 'ancestor_budget_exceeded')
            info = path.lstat()
            _require(stat.S_ISDIR(info.st_mode) and info.st_uid != peer_uid
                and not info.st_mode & 0o002
                and not (info.st_gid in peer_groups and info.st_mode & 0o020),
                'peer_can_modify_data_ancestor')
            values[str(path)] = {key: value for key, value in _metadata(path).items() if key != 'bytes'}
            values[str(path)]['access_acl_sha256'] = _peer_acl(path, peer_uid, peer_groups)
    return values


def _peer_acl(path, peer_uid, peer_groups):
    """Check named Linux POSIX ACL grants, including the effective mask."""
    try:
        blob = os.getxattr(path, 'system.posix_acl_access', follow_symlinks=False)
    except OSError as error:
        if error.errno in {errno.ENODATA, errno.ENOTSUP, errno.EOPNOTSUPP}:
            return None
        raise QualificationError('ancestor_acl_unavailable') from None
    _require(4 <= len(blob) <= 4096 and (len(blob) - 4) % 8 == 0
        and struct.unpack_from('<I', blob)[0] == 2, 'ancestor_acl_invalid')
    entries = [struct.unpack_from('<HHI', blob, offset) for offset in range(4, len(blob), 8)]
    _require(all(tag in {1, 2, 4, 8, 16, 32} and rights <= 7 for tag, rights, unused in entries),
        'ancestor_acl_invalid')
    masks = [rights for tag, rights, unused in entries if tag == 16]
    _require(len(masks) == 1, 'ancestor_acl_invalid')
    for tag, rights, principal in entries:
        if (tag == 2 and principal == peer_uid) or (tag == 8 and principal in peer_groups):
            _require(not rights & masks[0] & 2, 'peer_can_modify_data_ancestor_acl')
    return hashlib.sha256(blob).hexdigest()


def _admit(config_path, digest):
    _require(sys.platform.startswith('linux') and os.getuid() == 0 and os.geteuid() == 0
        and os.getgid() == os.getegid(), 'root_linux_parent_required')
    _require(re.fullmatch('[0-9a-f]{64}', digest or '') is not None, 'configuration_digest_invalid')
    data = _read(config_path, limit=service.MAX_CONFIGURATION_BYTES, modes={0o640})
    _require(hashlib.sha256(data).hexdigest() == digest, 'configuration_digest_mismatch')
    config = service.parse_configuration(data)
    _require(len(config.templates) == 1 and len(config.peers) == 1
        and len(config.peers[0].resources) == 1, 'qualification_scope_invalid')
    broker = config.broker_identity; peer = config.peers[0]
    _require(broker.uid != peer.uid and min(broker.uid, broker.gid, peer.uid, peer.gid) > 0,
        'dedicated_identities_required')
    _require(config_path.stat().st_gid == broker.gid, 'configuration_group_invalid')
    ipc_gid = config.ipc_directory.lstat().st_gid
    _require(len({broker.gid, peer.gid, ipc_gid}) == 3 and ipc_gid > 0, 'dedicated_groups_required')
    for uid, gid in ((broker.uid, broker.gid), (peer.uid, peer.gid)):
        account = pwd.getpwuid(uid)
        _require(account.pw_gid == gid and account.pw_shell in {'/usr/sbin/nologin', '/sbin/nologin'}
            and set(os.getgrouplist(account.pw_name, gid)) == {gid, ipc_gid}, 'account_untrusted')
    _require(peer.resources[0].operations == ipc.OPERATIONS, 'qualification_operations_required')
    _directory(config.state_dir, broker.uid, broker.gid, 0o700)
    _require(not any(config.state_dir.iterdir()), 'fresh_state_required')
    _directory(config.issuer_key_file.parent, broker.uid, broker.gid, 0o700)
    _directory(config.images_root, broker.uid, broker.gid, 0o700)
    _directory(config.channel_root, broker.uid, broker.gid, 0o700)
    _directory(config.ipc_directory, broker.uid, ipc_gid, 0o750)
    key = config.issuer_key_file.lstat()
    _require(stat.S_ISREG(key.st_mode) and key.st_uid == broker.uid and key.st_gid == broker.gid
        and key.st_nlink == 1 and stat.S_IMODE(key.st_mode) == 0o600 and key.st_size == 32,
        'key_metadata_invalid')
    for template in config.templates:
        for path in (template.channel, template.console):
            _require(not os.path.lexists(path), 'catalog_endpoint_exists')
        for role, path in (('disk', template.disk), ('seed', template.seed)):
            info = path.lstat()
            _require(stat.S_ISREG(info.st_mode) and info.st_uid == broker.uid
                and info.st_gid == broker.gid and info.st_nlink == 1
                and stat.S_IMODE(info.st_mode) == 0o600
                and (info.st_size == DISK_BYTES if role == 'disk' else 512 <= info.st_size <= 16 * 1024**2),
                'catalog_file_untrusted')
    sources = {}
    for relative in SOURCE_FILES:
        path = REPO / relative
        sources[relative] = hashlib.sha256(_read(path, limit=1024**2)).hexdigest()
    data_ancestors = _data_ancestors(config, peer.uid, {peer.gid, ipc_gid})
    return config, ipc_gid, sources, data_ancestors


def _properties(uid, gid, ipc_gid, *, writable, inaccessible=()):
    # These are actual system units; no namespace remapping or broad delegation.
    values = [f'User={uid}', f'Group={gid}', f'SupplementaryGroups={ipc_gid}',
        'NoNewPrivileges=yes', 'CapabilityBoundingSet=', 'AmbientCapabilities=',
        'UMask=0077', 'PrivateUsers=no', 'PrivateNetwork=yes', 'PrivateDevices=yes',
        'PrivateTmp=yes', 'ProtectSystem=strict', 'ProtectHome=no',
        'ProtectKernelTunables=yes', 'ProtectKernelModules=yes', 'ProtectControlGroups=yes',
        'ProtectProc=default', 'ProcSubset=all', 'RestrictAddressFamilies=AF_UNIX',
        'SystemCallArchitectures=native', 'RestrictSUIDSGID=yes', 'KillMode=control-group',
        'TimeoutStopSec=5s', f'RuntimeMaxSec={TOTAL_SECONDS}s', 'MemoryMax=256M', 'TasksMax=32',
        'LimitNOFILE=128', 'StandardOutput=journal', 'StandardError=journal',
        'LogRateLimitIntervalSec=30s', 'LogRateLimitBurst=32', 'WorkingDirectory=' + str(REPO),
        'Environment=PYTHONDONTWRITEBYTECODE=1',
        'UnsetEnvironment=PYTHONPATH PYTHONHOME PYTHONSTARTUP PYTHONINSPECT LD_PRELOAD LD_LIBRARY_PATH']
    # Whole runtime directories cover modular/admin sockets and /var/run aliases.
    hidden = ('/run/libvirt', '/run/docker.sock', '/run/containerd', '/run/podman', '/run/user',
              '/run/dbus/system_bus_socket',
              *map(str, inaccessible))
    def quote(path):
        return '"' + str(path).replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%') + '"'
    values.append('InaccessiblePaths=' + ' '.join(quote('-' + path) for path in hidden))
    if writable:
        values.append('ReadWritePaths=' + ' '.join(quote(path) for path in writable))
    return values


class Units:
    """Only names reserved and positively reidentified by this invocation may stop."""
    def __init__(self, prefix, report):
        self.prefix = prefix; self.report = report; self.owned = {}
        self.deadline = time.monotonic() + TOTAL_SECONDS
        self.last_observation = {}
        self.invocations = {}
        self.report.setdefault('unit_observations', [])

    def command(self, args, **kwargs):
        remaining = self.deadline - time.monotonic()
        _require(remaining > 0, 'qualification_timeout')
        return _command(args, timeout=min(8, remaining), **kwargs)

    def show(self, name):
        fields = ('Id', 'LoadState', 'ActiveState', 'SubState', 'MainPID', 'ExecMainCode',
            'ExecMainStatus', 'Result', 'Transient', 'Description', 'InvocationID',
            'User', 'Group', 'SupplementaryGroups', 'NoNewPrivileges', 'CapabilityBoundingSet',
            'AmbientCapabilities', 'PrivateUsers', 'PrivateNetwork', 'PrivateDevices', 'PrivateTmp',
            'ProtectSystem', 'ProtectHome', 'ProtectKernelTunables', 'ProtectKernelModules',
            'ProtectControlGroups', 'ProtectProc', 'ProcSubset', 'RestrictAddressFamilies',
            'SystemCallArchitectures', 'RestrictSUIDSGID', 'InaccessiblePaths', 'ReadWritePaths',
            'WorkingDirectory', 'ExecStart', 'MemoryMax', 'TasksMax', 'RuntimeMaxUSec',
            'TimeoutStopUSec', 'KillMode', 'RemainAfterExit')
        unused, output = self.command(['/usr/bin/systemctl', 'show', name,
            *('--property=' + key for key in fields)], allowed_failure=True)
        value = dict(line.split('=', 1) for line in output.splitlines() if '=' in line)
        if name in self.owned and value != self.last_observation.get(name):
            _require(len(self.report['unit_observations']) < 128, 'unit_observation_limit')
            self.report['unit_observations'].append({'unit': name, 'observed_at': time.time(), 'properties': value})
            self.last_observation[name] = value
        return value

    def check_owned(self, name):
        _require(name in self.owned and name.startswith(self.prefix + '-'), 'unit_not_owned')
        value = self.show(name)
        if value.get('LoadState') == 'not-found':
            return None
        _require(value.get('Id') == name and value.get('Transient') == 'yes'
            and value.get('Description') == self.owned[name], 'unit_ownership_changed')
        invocation = value.get('InvocationID', '')
        if invocation:
            _require(re.fullmatch('[0-9a-f]{32}', invocation) is not None,
                'unit_invocation_invalid')
            _require(name not in self.invocations or self.invocations[name] == invocation,
                'unit_invocation_changed')
            self.invocations[name] = invocation
        else:
            _require(value.get('MainPID', '0') == '0', 'unit_invocation_missing')
        return value

    def start(self, role, command, properties):
        _require(re.fullmatch('[a-z0-9-]{1,32}', role) is not None
            and len(self.owned) < MAX_UNITS, 'unit_budget_exceeded')
        name = self.prefix + '-' + role + '.service'
        _require(name not in self.owned and self.show(name).get('LoadState') == 'not-found', 'unit_exists')
        description = 'Harniqo identity qualification ' + self.prefix + ':' + role
        self.owned[name] = description
        self.report['units'].append({'name': name, 'role': role, 'properties': properties})
        self.command(['/usr/bin/systemd-run', '--quiet', '--no-block', '--service-type=exec',
            '--unit=' + name, '--description=' + description,
            *('--property=' + value for value in properties), '--', *command])
        return name

    def logs(self, name):
        self.check_owned(name)
        unused, output = self.command(['/usr/bin/journalctl', '--no-pager', '--output=cat',
            '--lines=48', '--unit=' + name])
        self.report['logs'][name] = output
        events = []
        for line in output.splitlines():
            try:
                event = _decode(line.encode())
                if type(event) is dict:
                    events.append(event)
            except (ValueError, UnicodeError):
                pass
        return events

    def wait(self, name, event_name, *, seconds=20):
        until = min(self.deadline, time.monotonic() + seconds)
        while time.monotonic() < until:
            value = self.check_owned(name)
            for event in self.logs(name):
                if event.get('event') == event_name:
                    _require(event.get('ok', True) is True, 'worker_probe_failed')
                    return event
            _require(value is not None and value.get('ActiveState') not in {'failed', 'inactive'},
                'unit_exited_before_event')
            time.sleep(.1)
        raise QualificationError('unit_event_timeout')

    def completed(self, name):
        until = min(self.deadline, time.monotonic() + 10)
        while time.monotonic() < until:
            value = self.check_owned(name)
            _require(value is not None, 'unit_disappeared')
            if value.get('ActiveState') == 'active' and value.get('SubState') == 'exited':
                _require(value.get('ExecMainStatus') == '0' and value.get('Result') == 'success',
                    'worker_exit_failed')
                _require(value.get('MainPID') == '0', 'worker_still_running')
                return value
            _require(value.get('ActiveState') not in {'failed', 'inactive'}, 'worker_exit_failed')
            time.sleep(.1)
        raise QualificationError('worker_exit_timeout')

    def stop(self, name):
        if self.check_owned(name) is not None:
            self.command(['/usr/bin/systemctl', 'stop', name])
            value = self.check_owned(name)
            _require(value is None or value.get('MainPID') == '0', 'owned_unit_not_stopped')

    def cleanup(self):
        # Cleanup has its own finite deadline even after the main budget expires.
        self.deadline = time.monotonic() + 40
        failures = []
        for name in reversed(self.owned):
            failed = False
            # Evidence collection must never suppress an independently
            # ownership-checked stop attempt for this registered unit.
            for phase, operation in (('logs', self.logs), ('stop', self.stop)):
                try:
                    operation(name)
                except Exception as error:
                    failed = True
                    code = str(error) if isinstance(error, QualificationError) else 'cleanup_operation_failed'
                    self.report.setdefault('cleanup_errors', []).append(
                        {'unit': name, 'phase': phase, 'code': code})
            if failed:
                failures.append(name)
        if failures:
            self.report['status'] = 'FAILED'
        return failures


def _emit(value):
    data = _json(value)
    _require(len(data) <= MAX_OUTPUT, 'worker_output_limit')
    sys.stdout.write(data.decode()); sys.stdout.flush()


def _fixture(config_path, digest, namespaces):
    config = service.load_configuration(config_path, expected_sha256=digest)
    group = config.ipc_directory.stat().st_gid
    evidence = _worker_evidence(config.broker_identity.uid, config.broker_identity.gid,
        [config.broker_identity.gid, group], namespaces)
    endpoints = []; stopped = False
    def stop(unused_signum, unused_frame):
        nonlocal stopped
        stopped = True
    signal.signal(signal.SIGTERM, stop); signal.signal(signal.SIGINT, stop)
    try:
        qemu_uid = pwd.getpwnam('libvirt-qemu').pw_uid
        _require(qemu_uid > 0 and qemu_uid != config.broker_identity.uid, 'qemu_identity_invalid')
        for template in config.templates:
            for path, rights in ((template.disk, 'rw-'), (template.seed, 'r--')):
                _command(['/usr/bin/setfacl', '-m', 'u:' + str(qemu_uid) + ':' + rights, '--', str(path)])
            prepare_seed_access(template.seed, images_root=config.images_root)
            for path in (template.channel, template.console):
                _require(not os.path.lexists(path), 'catalog_endpoint_exists')
                endpoint = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                endpoints.append(endpoint)
                endpoint.bind(str(path)); os.chmod(path, 0o600)
                _command(['/usr/bin/setfacl', '-m', 'u:' + str(qemu_uid) + ':rw-', '--', str(path)])
                endpoint.listen(1)
        hashes = [template.configuration_sha256 for template in config.templates]
        _emit({'event': 'fixture_ready', 'process': evidence, 'template_digests': hashes})
        while not stopped:
            time.sleep(.1)
    finally:
        for endpoint in endpoints:
            endpoint.close()
        # Preserve the exact new endpoints for post-run inspection, including failures.


def _denied_open(path, flags):
    try:
        fd = os.open(path, flags | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    except OSError as error:
        denied = {errno.EACCES, errno.EPERM}
        if flags & (os.O_WRONLY | os.O_RDWR):
            denied.add(errno.EROFS)
        return error.errno in denied
    else:
        os.close(fd)
        return False


def _expected_error(call, code, outcome):
    try:
        call()
    except ipc.IPCError as error:
        return error.code == code and error.outcome == outcome
    return False


def _network_checks():
    checks = {}
    for path in (*MANAGEMENT_SOCKETS, *(path.replace('/run/', '/var/run/', 1) for path in MANAGEMENT_SOCKETS)):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(.2)
            try:
                connection.connect(path)
            except OSError as error:
                checks['management:' + path] = error.errno in {errno.EACCES, errno.EPERM, errno.ENOENT, errno.ENOTDIR}
            else:
                checks['management:' + path] = False
    try:
        connection = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    except OSError as error:
        checks['AF_INET_creation_denied'] = error.errno in {errno.EAFNOSUPPORT, errno.EPERM, errno.EACCES}
    else:
        connection.close(); checks['AF_INET_creation_denied'] = False
    return checks


def _peer_denial(directory, broker):
    """An unapproved peer is refused before the server reads a request ID.

    Do not mislabel the SDK's unconfirmed/correlation failure as a protocol
    not-dispatched result. Receive the actual credential-checked early refusal
    without submitting any request, using the production ancillary validator.
    """
    with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as connection:
        connection.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
        connection.settimeout(3)
        connection.connect(str(directory / ipc.SOCKET_NAME))
        expected = ipc._credentials(connection)
        if expected[0] <= 0 or expected[1:] != (broker['uid'], broker['gid']):
            return False
        reply = ipc._receive_reply(connection, expected, ipc._boottime() + 3)
    return reply == {'version': 1, 'request_id': None,
        'error': {'code': 'ipc_peer_denied', 'outcome': 'not_dispatched'}}


def _probe(input_path, digest, mode):
    raw = _read(input_path, modes={0o640})
    _require(hashlib.sha256(raw).hexdigest() == digest, 'probe_input_changed')
    value = _decode(raw)
    broker = value['broker']; supervisor = value['supervisor']; group = value['ipc_gid']
    uid, gid = ((broker['uid'], broker['gid']) if mode == 'broker-peer' else
        (supervisor['uid'], group if mode == 'wrong-gid' else supervisor['gid']))
    evidence = _worker_evidence(uid, gid,
        [broker['gid'] if mode == 'broker-peer' else supervisor['gid'], group], value['host_namespaces'])
    checks = _network_checks()
    client = ipc.BrokerClient(directory=Path(value['ipc_directory']),
        server_uid=broker['uid'], server_gid=broker['gid'], timeout_seconds=3)
    resource = value['resource']
    if mode in {'broker-peer', 'wrong-gid'}:
        checks['unapproved_kernel_peer_denied_before_request'] = _peer_denial(Path(value['ipc_directory']), broker)
    else:
        checks['authenticated_observe_reaches_real_empty_core'] = _expected_error(
            lambda: client.observe(resource['resource_id']), 'resource_not_registered', 'unconfirmed')
        checks['authenticated_revoke_reaches_real_empty_core'] = _expected_error(
            lambda: client.revoke(resource['resource_id']), 'resource_not_registered', 'unconfirmed')
        checks['foreign_resource_denied_before_core'] = _expected_error(
            lambda: client.observe(value['foreign_resource_id']), 'ipc_resource_denied', 'not_dispatched')
        now = time.time()
        assignment = Assignment(resource_id=resource['resource_id'], organization_id=resource['organization_id'],
            workspace_id=resource['workspace_id'], attempt_id='identity-qualification',
            template_id=value['template_id'], configuration_sha256=value['template_digest'],
            generation=1, recovery_generation=1, issued_at=now, expires_at=now + 30, signature='0' * 64)
        checks['invalid_signature_rejected_by_real_core'] = _expected_error(lambda: client.ensure(assignment),
            'signature_invalid', 'unconfirmed')
        grant = Grant(**asdict(assignment))
        checks['invalid_renewal_signature_rejected_by_real_core'] = _expected_error(lambda: client.renew(grant),
            'signature_invalid', 'unconfirmed')
        for index, path in enumerate(value['private_read_paths']):
            checks['private_read_denied:' + str(index)] = _denied_open(path, os.O_RDONLY)
        for index, path in enumerate(value['immutable_write_paths']):
            checks['immutable_write_denied:' + str(index)] = _denied_open(path, os.O_WRONLY)
        target = Path(value['ipc_directory']) / value['create_canary']
        directory_denials = {errno.EACCES, errno.EPERM}
        if mode != 'supervisor-dac':
            directory_denials.add(errno.EROFS)
        try:
            fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        except OSError as error:
            checks['ipc_directory_create_denied'] = error.errno in directory_denials
        else:
            os.close(fd); checks['ipc_directory_create_denied'] = False
        try:
            os.replace(Path(value['ipc_directory']) / value['replace_source'],
                Path(value['ipc_directory']) / value['replace_target'])
        except OSError as error:
            checks['ipc_directory_replace_canary_denied'] = error.errno in directory_denials
        else:
            checks['ipc_directory_replace_canary_denied'] = False
    _emit({'event': 'probe_complete', 'mode': mode, 'ok': all(checks.values()),
        'checks': checks, 'process': evidence})
    return 0 if all(checks.values()) else 1


def qualify(config_path, digest, report_path):
    """Root-only local operator; no sudo, daemon change, account or VM creation."""
    _require(sys.platform.startswith('linux') and os.getuid() == os.geteuid() == 0,
        'root_linux_parent_required')
    _ancestors(report_path.parent)
    info = report_path.parent.stat()
    _require(info.st_uid == 0 and stat.S_IMODE(info.st_mode) == 0o700, 'report_parent_untrusted')
    report = {'schema_version': 1, 'scope': 'dedicated_uid_service_ipc_and_filesystem',
        'status': 'FAILED', 'activation_status': 'BLOCKED', 'activation_authorized': False,
        'vm_lifecycle_exercised': False, 'libvirt_acl_verified': False,
        'production_supervisor_integrated': False, 'valid_assignment_issued': False,
        'fixture_disk_scope': 'sparse raw catalog metadata only; no guest boot or VM qualification',
        'started_at': time.time(), 'configuration_sha256': digest, 'units': [], 'logs': {}, 'checks': {}}
    # Reserve evidence before the first transient unit or new canary is created.
    _write_new(report_path, _json(report))
    units = None
    try:
        config, group, sources, data_ancestors = _admit(config_path, digest)
        report['source_sha256'] = sources
        report['release_root'] = str(REPO)
        report['data_ancestors'] = data_ancestors
        report['data_ancestor_trust_scope'] = 'Root and existing trusted developer owners; no Supervisor UID or groups can modify these ancestors.'
        broker = config.broker_identity; peer = config.peers[0]; template = config.templates[0]
        report['identities'] = {'broker': asdict(broker), 'supervisor': {'uid': peer.uid, 'gid': peer.gid}, 'ipc_gid': group}
        host_namespaces = _process(os.getpid())['namespaces']
        report['host_namespaces'] = host_namespaces
        prefix = 'harniqo-idq-' + secrets.token_hex(8)
        control = config.ipc_directory.parent / (prefix + '-control')
        _ancestors(control.parent)
        control.mkdir(mode=0o750); os.chown(control, 0, group); os.chmod(control, 0o750)
        report['control_directory'] = str(control)
        units = Units(prefix, report)
        broker_properties = _properties(broker.uid, broker.gid, group,
            writable=(config.state_dir, config.ipc_directory))
        fixture_properties = _properties(broker.uid, broker.gid, group,
            writable=(config.channel_root, config.images_root))
        script = str(REPO / SOURCE_FILES[-1])
        fixture_unit = units.start('fixture', ['/usr/bin/python3', '-B', script, '--worker', 'fixture',
            '--config', str(config_path), '--sha256', digest, '--host-netns', host_namespaces['net'],
            '--host-userns', host_namespaces['user']], fixture_properties)
        fixture = units.wait(fixture_unit, 'fixture_ready')
        report['fixture'] = fixture
        def production(role):
            return ['/usr/bin/python3', '-B', '-m', 'apps.runner.resource_broker.service', role,
                '--config', str(config_path), '--sha256', digest]
        initialize = units.start('initialize', production('initialize'), broker_properties + ['RemainAfterExit=yes'])
        units.wait(initialize, 'initialized'); units.completed(initialize)
        initial_state = _state(config); report['initial_state'] = initial_state
        watch_unit = units.start('watch', production('watch'), broker_properties)
        _require(units.wait(watch_unit, 'ready').get('status') == 'operational', 'watch_not_ready')
        serve_unit = units.start('serve-a', production('serve'), broker_properties)
        _require(units.wait(serve_unit, 'ready').get('status') == 'operational', 'serve_not_ready')
        for name in (watch_unit, serve_unit):
            evidence = _process(int(units.check_owned(name)['MainPID']))
            _identity(evidence, broker.uid, broker.gid, [broker.gid, group], host_namespaces)
            report['checks'][name + ':process'] = evidence
        token = secrets.token_hex(8)
        source_name = '.idq-source-' + token; target_name = '.idq-target-' + token
        _write_new(config.ipc_directory / source_name, b'identity-source\n', uid=peer.uid, gid=peer.gid)
        _write_new(config.ipc_directory / target_name, b'identity-target\n', uid=broker.uid, gid=broker.gid)
        canaries = {name: _metadata(config.ipc_directory / name, digest=True) for name in (source_name, target_name)}
        foreign = str(uuid.uuid4())
        _require(foreign != template.resource_id, 'probe_identity_collision')
        probe = {'schema_version': 1, 'broker': asdict(broker), 'supervisor': {'uid': peer.uid, 'gid': peer.gid},
            'ipc_gid': group, 'host_namespaces': host_namespaces, 'ipc_directory': str(config.ipc_directory),
            'resource': {'resource_id': peer.resources[0].resource_id,
                'organization_id': peer.resources[0].organization_id, 'workspace_id': peer.resources[0].workspace_id},
            'template_id': template.template_id, 'template_digest': fixture['template_digests'][0],
            'foreign_resource_id': foreign, 'create_canary': '.idq-create-' + token,
            'replace_source': source_name, 'replace_target': target_name,
            'private_read_paths': list(map(str, (config.issuer_key_file, config.state_dir / 'anchor.json',
                config.state_dir / 'journal.json', template.disk, template.seed))),
            'immutable_write_paths': [str(config_path), str(REPO / SOURCE_FILES[0]), script]}
        probe_path = control / 'probe.json'; probe_bytes = _json(probe)
        _write_new(probe_path, probe_bytes, gid=group, mode=0o640)
        probe_digest = hashlib.sha256(probe_bytes).hexdigest()
        supervisor_hidden = (config.issuer_key_file.parent, config.state_dir, config.images_root, config.channel_root)
        def run_probe(role, mode, gid):
            uid = broker.uid if mode == 'broker-peer' else peer.uid
            properties = _properties(uid, gid, group,
                writable=(config.ipc_directory,) if mode == 'supervisor-dac' else (),
                inaccessible=supervisor_hidden if mode in {'supervisor', 'wrong-gid'} else ())
            properties.append('RemainAfterExit=yes')
            name = units.start(role, ['/usr/bin/python3', '-B', script, '--worker', mode,
                '--input', str(probe_path), '--input-sha256', probe_digest], properties)
            event = units.wait(name, 'probe_complete'); units.completed(name)
            report['checks'][role] = event
        run_probe('supervisor-dac', 'supervisor-dac', peer.gid)
        run_probe('supervisor-a', 'supervisor', peer.gid)
        run_probe('wrong-peer', 'broker-peer', broker.gid)
        run_probe('wrong-gid', 'wrong-gid', group)
        watch_before = _process(int(units.check_owned(watch_unit)['MainPID']))
        units.stop(serve_unit)
        watch_after = _process(int(units.check_owned(watch_unit)['MainPID']))
        _require(watch_before == watch_after, 'watch_did_not_survive_listener_stop')
        report['checks']['independent_watch_survived_listener_stop'] = True
        serve_restart = units.start('serve-b', production('serve'), broker_properties)
        _require(units.wait(serve_restart, 'ready').get('status') == 'operational', 'restart_not_ready')
        restarted = _process(int(units.check_owned(serve_restart)['MainPID']))
        _identity(restarted, broker.uid, broker.gid, [broker.gid, group], host_namespaces)
        report['checks']['restarted_listener_process'] = restarted
        run_probe('supervisor-b', 'supervisor', peer.gid)
        final_state = _state(config)
        _require(initial_state == final_state, 'journal_or_anchor_changed')
        _require(canaries == {name: _metadata(config.ipc_directory / name, digest=True)
            for name in (source_name, target_name)}, 'ipc_canary_changed')
        _require(data_ancestors == _data_ancestors(config, peer.uid, {peer.gid, group}), 'data_ancestor_changed')
        report['final_state'] = final_state
        report['checks']['same_empty_authenticated_state_reopened'] = True
        report['checks']['ipc_replacement_canaries_unchanged'] = True
        report['status'] = 'VERIFIED'
    except Exception as error:
        report['status'] = 'FAILED'
        report['error_code'] = str(error) if isinstance(error, QualificationError) else 'identity_qualification_failed'
    finally:
        if units is not None:
            report['cleanup_failures'] = units.cleanup()
            if report['cleanup_failures']:
                report['status'] = 'FAILED'
        report['finished_at'] = time.time()
        data = _json(report)
        _require(len(data) <= MAX_REPORT, 'evidence_capacity_exceeded')
        # This file was exclusively created by this invocation before any mutation.
        fd = os.open(report_path, os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            info = os.fstat(fd)
            _require(stat.S_ISREG(info.st_mode) and info.st_uid == 0 and info.st_nlink == 1
                and stat.S_IMODE(info.st_mode) == 0o600, 'report_replaced')
            os.ftruncate(fd, 0)
            with os.fdopen(fd, 'wb', closefd=False) as stream:
                stream.write(data); stream.flush(); os.fsync(fd)
        finally:
            os.close(fd)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config'); parser.add_argument('--sha256'); parser.add_argument('--report')
    parser.add_argument('--worker', choices=('fixture', 'supervisor', 'supervisor-dac', 'broker-peer', 'wrong-gid'))
    parser.add_argument('--input'); parser.add_argument('--input-sha256')
    parser.add_argument('--host-netns'); parser.add_argument('--host-userns')
    handlers = {}
    try:
        args = parser.parse_args(argv)
        if args.worker:
            _require(os.getuid() != 0 and os.getuid() == os.geteuid()
                and os.getgid() == os.getegid(), 'worker_privileged')
            if args.worker == 'fixture':
                _fixture(_path(args.config), args.sha256,
                    {'net': args.host_netns, 'user': args.host_userns})
                return 0
            return _probe(_path(args.input), args.input_sha256, args.worker)
        if os.getuid() == 0:
            def interrupted(unused_signum, unused_frame):
                raise QualificationError('operator_interrupted')
            for sig in (signal.SIGTERM, signal.SIGINT):
                handlers[sig] = signal.signal(sig, interrupted)
        report = qualify(_path(args.config), args.sha256, _path(args.report))
        _emit({'schema_version': 1, 'status': report['status'], 'activation_status': 'BLOCKED',
            'activation_authorized': False, 'report': str(args.report)})
        return 0 if report['status'] == 'VERIFIED' else 1
    except Exception as error:
        code = str(error) if isinstance(error, QualificationError) else 'identity_operator_failed'
        _emit({'schema_version': 1, 'event': 'error', 'code': code})
        return 1
    finally:
        for sig, previous in handlers.items():
            signal.signal(sig, previous)


if __name__ == '__main__':
    raise SystemExit(main())
