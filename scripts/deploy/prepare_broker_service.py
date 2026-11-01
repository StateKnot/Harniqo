#!/usr/bin/env python3
"""Prepare a bounded, offline Broker deployment bundle; never install or activate it."""
from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
from apps.runner.resource_broker import BrokerError
from apps.runner.resource_broker.service import MAX_CONFIGURATION_BYTES, parse_configuration

MAX_PLAN_BYTES = 32768
CONFIG_INSTALL = '/etc/harniqo/resource-broker.json'
_PLAN_FIELDS = {'schema_version', 'configuration_path', 'configuration_sha256', 'release_root',
    'broker_account', 'supervisor_account', 'ipc_group', 'libvirt_acl_attestation'}
_FORBIDDEN_NAMES = {'root', 'nobody', 'daemon', 'bin', 'sync', 'games', 'man', 'lp', 'mail',
    'news', 'uucp', 'proxy', 'www-data', 'backup', 'list', 'irc', 'gnats', '_apt',
    'messagebus', 'polkitd', 'libvirt', 'libvirt-qemu', 'docker', 'kvm', 'sudo', 'wheel'}
CONNECT_ACTIONS = ('org.libvirt.api.connect.getattr', 'org.libvirt.api.connect.search-domains')
DOMAIN_ACTIONS = tuple('org.libvirt.api.domain.' + p for p in ('getattr', 'read', 'write', 'start', 'stop'))


class DeploymentError(ValueError):
    """Finite error code; CLI output never includes native exceptions or file contents."""


def _reject(code):
    raise DeploymentError(code)


def _object(value, fields):
    if type(value) is not dict or set(value) != set(fields):
        _reject('deployment_plan_invalid')
    return value


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _reject('deployment_plan_invalid')
        result[key] = value
    return result


def _integer(value):
    if len(value) > 20:
        _reject('deployment_plan_invalid')
    return int(value)


def _constant(value):
    _reject('deployment_plan_invalid')


def _path(value):
    if (type(value) is not str or not value or len(os.fsencode(value)) > 4096
            or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        _reject('deployment_path_invalid')
    path = Path(value)
    if (not path.is_absolute() or str(path) != value or '..' in path.parts
            or path == Path('/') or value.startswith('//')):
        _reject('deployment_path_invalid')
    return path


def _unit_path(value):
    """Quote systemd path-list tokens; %% is a literal specifier character."""
    path = str(_path(str(value)))
    return '"' + path.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%') + '"'


def _directory(path):
    """Walk without following any symlink, including the final component."""
    path = _path(str(path))
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for part in path.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
            os.close(fd); fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def _read_file(path, limit):
    parent = _directory(path.parent)
    try:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=parent)
    finally:
        os.close(parent)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or info.st_uid not in {0, os.geteuid()} or info.st_mode & 0o022):
            _reject('deployment_input_untrusted')
        if not 0 < info.st_size <= limit:
            _reject('deployment_input_size')
        data = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
        if (len(data) > limit or (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
                != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
            _reject('deployment_input_changed')
        return data


def _account(value):
    _object(value, {'name', 'uid', 'gid'})
    if (type(value['name']) is not str or not re.fullmatch(r'[a-z_][a-z0-9_-]{0,30}', value['name'])
            or value['name'] in _FORBIDDEN_NAMES
            or any(type(value[k]) is not int or not 1 <= value[k] < 2147483647 for k in ('uid', 'gid'))):
        _reject('deployment_identity_invalid')
    return value


def _parse_plan(data):
    try:
        value = json.loads(data.decode('utf-8'), object_pairs_hook=_pairs,
                           parse_int=_integer, parse_constant=_constant)
    except (UnicodeError, ValueError, RecursionError, OverflowError):
        _reject('deployment_plan_invalid')
    _object(value, _PLAN_FIELDS)
    if type(value['schema_version']) is not int or value['schema_version'] != 1:
        _reject('deployment_plan_invalid')
    digest = value['configuration_sha256']
    if type(digest) is not str or re.fullmatch('[0-9a-f]{64}', digest) is None:
        _reject('deployment_digest_invalid')
    _path(value['configuration_path'])
    release = _path(value['release_root'])
    if (release.parent != Path('/opt/harniqo/releases')
            or re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,79}', release.name) is None):
        _reject('deployment_release_invalid')
    broker = _account(value['broker_account']); supervisor = _account(value['supervisor_account'])
    group = _object(value['ipc_group'], {'name', 'gid'})
    if (type(group['name']) is not str or re.fullmatch(r'[a-z_][a-z0-9_-]{0,30}', group['name']) is None
            or group['name'] in _FORBIDDEN_NAMES or type(group['gid']) is not int
            or not 1 <= group['gid'] < 2147483647
            or broker['uid'] == supervisor['uid']
            or len({broker['gid'], supervisor['gid'], group['gid']}) != 3
            or len({broker['name'], supervisor['name'], group['name']}) != 3):
        _reject('deployment_identity_invalid')
    # A client JSON assertion is not daemon attestation. v1 has no activation override.
    attestation = _object(value['libvirt_acl_attestation'], {'status'})
    if attestation['status'] != 'unverified':
        _reject('deployment_attestation_unsupported')
    return value


def _check_configuration(plan, config):
    broker = plan['broker_account']; supervisor = plan['supervisor_account']
    if ((config.broker_identity.uid, config.broker_identity.gid) != (broker['uid'], broker['gid'])
            or len(config.peers) != 1
            or (config.peers[0].uid, config.peers[0].gid) != (supervisor['uid'], supervisor['gid'])):
        _reject('deployment_configuration_identity_mismatch')
    key_root = config.issuer_key_file.parent
    private_roots = (config.state_dir, key_root)
    runtime_roots = (config.ipc_directory, config.channel_root)
    for path in private_roots:
        # These are the only Broker-owned directories we render for tmpfiles.
        if path.parent != Path('/var/lib/harniqo'):
            _reject('deployment_layout_invalid')
        if re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,79}', path.name) is None:
            _reject('deployment_layout_invalid')
    for path in runtime_roots:
        if (path.parent != Path('/run/harniqo')
                or re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,79}', path.name) is None):
            _reject('deployment_layout_invalid')
    all_roots = (*private_roots, *runtime_roots, config.images_root)
    if len(set(all_roots)) != len(all_roots):
        _reject('deployment_layout_invalid')
    for left in all_roots:
        for right in all_roots:
            if left != right and (left in right.parents or right in left.parents):
                _reject('deployment_layout_invalid')
    # Images remain in the operator's existing design/images storage; never provision them.
    for path in (config.images_root, config.issuer_key_file):
        _unit_path(path)
    if config.images_root in {Path('/etc'), Path('/run'), Path('/var'), Path('/usr'), Path('/opt')}:
        _reject('deployment_layout_invalid')


def _json(value):
    return (json.dumps(value, ensure_ascii=True, indent=2, sort_keys=True) + '\n').encode()


def _rule(plan, config, *, candidate):
    broker = json.dumps(plan['broker_account']['name']); supervisor = json.dumps(plan['supervisor_account']['name'])
    if not candidate:
        return ("// Bootstrap deny only. Offline bundle: no management delegation.\n"
            "polkit.addRule(function(action, subject) {\n"
            f"    if ((subject.user === {broker} || subject.user === {supervisor}) &&\n"
            "        action.id.indexOf('org.libvirt.') === 0) return polkit.Result.NO;\n"
            "});\n").encode()
    domains = json.dumps([[t.resource_id, t.name] for t in config.templates], ensure_ascii=True)
    return ("// CANDIDATE ONLY: not authorized, not installed, not a .rules file.\n"
        "// Requires live effective polkit access-driver and object/API negative probes.\n"
        "// polkit does not inspect fixed XML, storage paths, topology or configuration hashes.\n"
        "polkit.addRule(function(action, subject) {\n"
        f"    var broker = {broker};\n    var supervisor = {supervisor};\n"
        "    if (subject.user === supervisor && action.id.indexOf('org.libvirt.') === 0)\n"
        "        return polkit.Result.NO;\n"
        "    if (subject.user !== broker) return;\n"
        "    if (action.id === 'org.libvirt.unix.manage') return polkit.Result.YES;\n"
        "    if (action.id.indexOf('org.libvirt.') !== 0) return;\n"
        "    if (action.lookup('connect_driver') !== 'QEMU') return polkit.Result.NO;\n"
        f"    var connect = {json.dumps(CONNECT_ACTIONS)};\n"
        "    if (connect.indexOf(action.id) !== -1) return polkit.Result.YES;\n"
        f"    var actions = {json.dumps(DOMAIN_ACTIONS)};\n"
        f"    var domains = {domains};\n"
        "    if (actions.indexOf(action.id) !== -1) {\n"
        "        for (var i = 0; i < domains.length; i++) {\n"
        "            if (action.lookup('domain_uuid') === domains[i][0] &&\n"
        "                action.lookup('domain_name') === domains[i][1]) return polkit.Result.YES;\n"
        "        }\n    }\n    return polkit.Result.NO;\n});\n").encode()


def _unit(plan, config, role):
    watch = role == 'watch'; broker = plan['broker_account']; group = plan['ipc_group']
    dependency = ('Before=harniqo-broker.service\n' if watch else
                  'Requires=harniqo-broker-watch.service\nAfter=harniqo-broker-watch.service\n')
    command = 'watch' if watch else 'serve'
    # Type=exec proves exec success, not application readiness or a stopping bound.
    return (f'[Unit]\nDescription=Harniqo Resource Broker {command}\n{dependency}'
        '\n[Service]\nType=exec\n'
        f'User={broker["name"]}\nGroup={broker["name"]}\nSupplementaryGroups={group["name"]}\n'
        f'WorkingDirectory={plan["release_root"]}\n'
        f'ExecStart=/usr/bin/python3 -B -m apps.runner.resource_broker.service {command} '
        f'--config {CONFIG_INSTALL} --sha256 {config.sha256}\n'
        'Restart=on-failure\nRestartSec=2s\nTimeoutStopSec=40s\nKillMode=control-group\n'
        'UMask=0077\nNoNewPrivileges=yes\nCapabilityBoundingSet=\nAmbientCapabilities=\n'
        'PrivateUsers=no\nPrivateDevices=yes\nPrivateTmp=yes\nProtectHome=no\n'
        'ProtectSystem=strict\nProtectKernelTunables=yes\nProtectKernelModules=yes\n'
        'ProtectControlGroups=yes\nProtectProc=default\nProcSubset=all\n'
        'RestrictAddressFamilies=AF_UNIX\nSystemCallArchitectures=native\nRestrictSUIDSGID=yes\n'
        f'ReadOnlyPaths={_unit_path(plan["release_root"])} {_unit_path(CONFIG_INSTALL)} '
        f'{_unit_path(config.issuer_key_file)} {_unit_path(config.images_root)} {_unit_path(config.channel_root)}\n'
        f'ReadWritePaths={_unit_path(config.state_dir)} {_unit_path(config.ipc_directory)}\n'
        'InaccessiblePaths=-/run/docker.sock -/run/containerd/containerd.sock\n'
        'Environment=PYTHONDONTWRITEBYTECODE=1\nStandardOutput=journal\nStandardError=journal\n'
        '\n[Install]\nWantedBy=multi-user.target\n').encode()


def _render(plan, config, configuration, plan_digest):
    broker = plan['broker_account']; supervisor = plan['supervisor_account']; group = plan['ipc_group']
    sysusers = ''.join(f'g {a["name"]} {a["gid"]} -\n' for a in (broker, supervisor, group))
    sysusers += ''.join(f'u {a["name"]} {a["uid"]}:{a["gid"]} "Harniqo service" /nonexistent /usr/sbin/nologin\n'
                        for a in (broker, supervisor))
    sysusers += ''.join(f'm {a["name"]} {group["name"]}\n' for a in (broker, supervisor))
    # No RuntimeDirectory lifecycle deletion and no age/removal rules for socket forensics.
    tmpfiles = 'd /var/lib/harniqo 0755 root root - -\nd /run/harniqo 0755 root root - -\n'
    for path in (config.state_dir, config.issuer_key_file.parent):
        tmpfiles += f'd {path} 0700 {broker["name"]} {broker["name"]} - -\n'
    # Preserve the execute ACL mask needed by separately admitted QEMU traversal.
    tmpfiles += f'd {config.channel_root} 0710 {broker["name"]} {broker["name"]} - -\n'
    tmpfiles += f'd {config.ipc_directory} 0750 {broker["name"]} {group["name"]} - -\n'
    permissions = {'connection_driver': 'QEMU', 'connect_actions': list(CONNECT_ACTIONS),
        'domain_actions': list(DOMAIN_ACTIONS),
        'domains': [{'uuid': t.resource_id, 'name': t.name} for t in config.templates],
        'scope_limitation': 'Object/permission ACL only; domain:write is not fixed-XML enforcement.'}
    files = {'resource-broker.json': configuration, 'harniqo-sysusers.conf': sysusers.encode(),
        'harniqo-tmpfiles.conf': tmpfiles.encode(), 'harniqo-broker.service': _unit(plan, config, 'serve'),
        'harniqo-broker-watch.service': _unit(plan, config, 'watch'),
        '00-harniqo-libvirt.rules': _rule(plan, config, candidate=False),
        '00-harniqo-libvirt.rules.candidate': _rule(plan, config, candidate=True),
        'required-libvirt-permissions.json': _json(permissions)}
    destinations = {'resource-broker.json': (CONFIG_INSTALL, 0, broker['gid'], '0640'),
        'harniqo-sysusers.conf': ('/usr/lib/sysusers.d/harniqo-runner.conf', 0, 0, '0644'),
        'harniqo-tmpfiles.conf': ('/usr/lib/tmpfiles.d/harniqo-runner.conf', 0, 0, '0644'),
        '00-harniqo-libvirt.rules': ('/etc/polkit-1/rules.d/00-harniqo-libvirt.rules', 0, 0, '0644')}
    for name in ('harniqo-broker.service', 'harniqo-broker-watch.service'):
        destinations[name] = ('/etc/systemd/system/' + name, 0, 0, '0644')
    manifest = {'schema_version': 1, 'status': 'BLOCKED', 'plan_sha256': plan_digest,
        'configuration_sha256': config.sha256, 'release_root': plan['release_root'],
        'libvirt_acl_attestation': {'status': 'unverified'},
        'polkit': {'bootstrap': 'deny_only', 'candidate': 'candidate_not_authorized',
            'candidate_file': '00-harniqo-libvirt.rules.candidate', 'candidate_install_destination': None,
            'effective_access_driver_required': 'polkit', 'template_enforcement': 'trusted_broker'},
        'identities': {'broker': broker, 'supervisor': supervisor, 'ipc_group': group},
        'files': [{'name': name, 'sha256': hashlib.sha256(data).hexdigest(), 'artifact_mode': '0600',
            'installation': dict(zip(('path', 'uid', 'gid', 'mode'), destinations[name])) if name in destinations else None}
            for name, data in sorted(files.items())],
        'blockers': ['effective_libvirt_acl_unverified', 'identity_collision_inventory_pending',
            'dedicated_uid_native_qualification_pending', 'real_vm_ipc_qualification_pending',
            'supervisor_service_integration_pending'],
        'activation_requirements': [
            'Do not install, enable or start from this unverified offline bundle.',
            'Inventory NSS/local names, UID/GID collisions, all memberships and polkit rule ordering; refuse mismatches.',
            'Use an isolated system-libvirt host or a separately authorized administrator window; never reconfigure existing global services implicitly.',
            'Prove the live target socket/daemon uses polkit access_drivers with current PID/start, loaded configuration hash and API-level positive/negative probes.',
            'Review candidate against the frozen catalog; only an administrator after live attestation may replace the deny rule with reviewed delegation. No renderer activation override exists.',
            'Publish root-owned immutable release and root:BrokerGroup 0640 config; verify every ancestor and all file digests; verify unit syntax in the real installation layout.',
            'Verify existing target directories first: tmpfiles can adjust existing owners/modes, so never run it against unreviewed preexisting paths.',
            'Prepare fresh Broker-owned catalog files and existing images storage without chowning old development state; preserve original design/images physical storage. Verify all template paths, QEMU traversal/ACL and existing report/console listeners; bundle does not prepare images/channels.',
            'Provision the preexisting 32-byte Broker-owned 0600 issuer key in its private 0700 parent without printing it; no key bytes are supplied by this bundle.',
            'As Broker UID explicitly initialize once; serving/watching only reopen admitted existing state. Never initialize over unresolved prior resources.',
            'Validate real/effective IDs, groups, sandbox, forbidden Supervisor key/state/libvirt/Docker access, then perform actual VM ensure/renew/observe/revoke through IPC and lease/restart negatives.',
            'Integrate the actual Supervisor executable using its dedicated identity; no placeholder Supervisor service is provided.',
            'Type=exec and Restart=on-failure are not readiness/VM health/hard-stop guarantees; monitor real ready/failed logs and resource uncertainty independently.'],
        'rollback_requirements': [
            'Stop admitting new Supervisor work; revoke only admitted test UUIDs through Broker IPC and independently confirm QEMU absence/receipts. Keep watcher running while resources are unresolved.',
            'On unknown stop outcome retain issuer key, catalog, state, images, channel/IPC paths and all lock/socket identities for recovery; never unlink to make restart pass.',
            'After confirmed drain, stop the listener, then watcher; restore only files whose pre-apply identity/hash was recorded, and only task-specific polkit delegation.',
            'Do not remove accounts or reuse their numeric IDs while files/resources remain; never delete existing domains, images, daemon configuration, firewall rules or global service state.']}
    files['manifest.json'] = _json(manifest)
    return files, manifest


def prepare_bundle(plan_path, output_dir):
    """Render only into a preexisting empty owned 0700 directory, without privilege."""
    if (os.getuid() == 0 or os.getuid() != os.geteuid() or os.getgid() != os.getegid()):
        _reject('deployment_identity_invalid')
    try:
        plan_data = _read_file(_path(str(plan_path)), MAX_PLAN_BYTES)
        plan = _parse_plan(plan_data)
        configuration = _read_file(_path(plan['configuration_path']), MAX_CONFIGURATION_BYTES)
        if hashlib.sha256(configuration).hexdigest() != plan['configuration_sha256']:
            _reject('deployment_digest_mismatch')
        config = parse_configuration(configuration)
        _check_configuration(plan, config)
        files, manifest = _render(plan, config, configuration, hashlib.sha256(plan_data).hexdigest())
        output = _path(str(output_dir)); directory = _directory(output)
        try:
            info = os.fstat(directory)
            if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700 or os.listdir(directory):
                _reject('deployment_output_untrusted')
            getxattr = getattr(os, 'getxattr', None)
            for attribute in (() if getxattr is None else ('system.posix_acl_access', 'system.posix_acl_default')):
                try:
                    if getxattr(directory, attribute):
                        _reject('deployment_output_untrusted')
                except OSError as error:
                    if error.errno not in {errno.ENODATA, errno.ENOTSUP, getattr(errno, 'ENOATTR', errno.ENODATA)}:
                        raise
            # Validation finishes before the first write. O_EXCL prevents overwrites.
            for name, data in files.items():
                fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                             0o600, dir_fd=directory)
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(data); stream.flush(); os.fsync(stream.fileno())
            os.fsync(directory)
        finally:
            os.close(directory)
        return manifest
    except DeploymentError:
        raise
    except BrokerError:
        _reject('deployment_configuration_invalid')
    except (OSError, ValueError, TypeError, OverflowError):
        _reject('deployment_io_failed')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args(argv)
    try:
        manifest = prepare_bundle(args.plan, args.output)
    except DeploymentError as error:
        print(json.dumps({'status': 'rejected', 'error': str(error)}), file=sys.stderr)
        return 1
    print(json.dumps({'status': manifest['status'], 'plan_sha256': manifest['plan_sha256'],
                      'files': len(manifest['files']) + 1, 'activation_authorized': False}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
