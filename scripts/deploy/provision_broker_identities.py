#!/usr/bin/env python3
"""Provision only fresh locked Broker identities; never delegate or activate libvirt."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import asdict, dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import selectors
import stat
import subprocess
import sys
import time

STATE_ROOT = Path('/var/lib/harniqo/identities')
GETENT = '/usr/bin/getent'
LIMIT = 1024 * 1024
COMMAND_SECONDS = 10
RESERVED = frozenset('root nobody daemon bin sync games man lp mail news uucp proxy www-data backup list irc gnats _apt messagebus polkitd libvirt libvirt-qemu docker kvm sudo wheel adm systemd-journal lxd disk shadow tty video render'.split())


class IdentityError(ValueError):
    """Finite diagnostic only; native output and shadow data never leave the Module."""


@dataclass(frozen=True)
class Account:
    name: str
    uid: int
    gid: int


@dataclass(frozen=True)
class Group:
    name: str
    gid: int


@dataclass(frozen=True)
class IdentityPlan:
    schema_version: int
    broker_account: Account
    supervisor_account: Account
    ipc_group: Group


def need(condition, code):
    if not condition:
        raise IdentityError(code)


def _pairs(pairs):
    value = {}
    for key, item in pairs:
        need(key not in value, 'identity_plan_invalid'); value[key] = item
    return value


def _json(data):
    try:
        return json.loads(data.decode('utf-8'), object_pairs_hook=_pairs,
                          parse_constant=lambda _: need(False, 'identity_plan_invalid'))
    except (UnicodeError, ValueError, RecursionError, OverflowError):
        raise IdentityError('identity_plan_invalid') from None


def parse_plan(data: bytes) -> IdentityPlan:
    need(type(data) is bytes and 0 < len(data) <= 32768, 'identity_plan_size')
    value = _json(data)
    need(type(value) is dict and set(value) == {'schema_version', 'broker_account', 'supervisor_account', 'ipc_group'}, 'identity_plan_invalid')
    need(type(value['schema_version']) is int and value['schema_version'] == 1, 'identity_plan_invalid')
    records = []
    for field in ('broker_account', 'supervisor_account', 'ipc_group'):
        entry = value[field]; fields = {'name', 'gid'} | ({'uid'} if field != 'ipc_group' else set())
        need(type(entry) is dict and set(entry) == fields, 'identity_plan_invalid')
        name = entry['name']
        need(type(name) is str and re.fullmatch(r'[a-z_][a-z0-9_-]{0,30}', name) is not None
             and name not in RESERVED, 'identity_name_invalid')
        need(all(type(entry[k]) is int and 1 <= entry[k] < 2147483647 for k in fields - {'name'}), 'identity_number_invalid')
        records.append((Account if field != 'ipc_group' else Group)(**entry))
    broker, supervisor, group = records
    need(broker.uid != supervisor.uid and len({x.gid for x in records}) == 3
         and len({x.name for x in records}) == 3, 'identity_scope_invalid')
    return IdentityPlan(1, broker, supervisor, group)


def _root_owned(info):
    return info.st_uid == info.st_gid == 0


def _ancestors(path, *, root=False):
    need(path.is_absolute() and str(path) == os.fspath(path) and '..' not in path.parts, 'identity_path_invalid')
    for parent in (path, *path.parents):
        need(not parent.is_symlink(), 'identity_path_untrusted')
        if root:
            info = parent.lstat()
            need(_root_owned(info) and not info.st_mode & 0o022, 'identity_path_untrusted')


def _read(path, maximum, *, root=False):
    _ancestors(path, root=root)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        need(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and (_root_owned(info) if root else info.st_uid in {0, os.geteuid()})
             and not info.st_mode & 0o022 and info.st_size <= maximum, 'identity_file_untrusted')
        data = os.read(fd, maximum + 1)
        token = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
        need(len(data) == info.st_size and len(data) <= maximum and token(info) == token(os.fstat(fd)) == token(path.lstat()), 'identity_file_changed')
        return data
    finally:
        os.close(fd)


def _run(args, *, missing=False):
    try:
        child = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env={'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LC_ALL': 'C', 'LANG': 'C', 'HOME': '/'}, close_fds=True)
    except OSError:
        raise IdentityError('identity_command_unavailable') from None
    output = bytearray(); errors = bytearray(); selector = selectors.DefaultSelector(); until = time.monotonic() + COMMAND_SECONDS
    try:
        selector.register(child.stdout, selectors.EVENT_READ, output); selector.register(child.stderr, selectors.EVENT_READ, errors)
        while selector.get_map():
            need(time.monotonic() < until, 'identity_command_timeout')
            for key, _ in selector.select(min(.1, max(0, until - time.monotonic()))):
                part = os.read(key.fileobj.fileno(), 65536)
                if part: key.data.extend(part)
                else: selector.unregister(key.fileobj)
                need(len(output) + len(errors) <= LIMIT, 'identity_command_output_limit')
        code = child.wait(timeout=max(.01, until - time.monotonic()))
        need(code == 0 or (missing and code == 2), 'identity_command_failed')
        return None if code == 2 else output.decode('utf-8')
    except (OSError, UnicodeError, subprocess.TimeoutExpired):
        raise IdentityError('identity_command_unavailable') from None
    finally:
        selector.close()
        if child.poll() is None: child.kill(); child.wait()
        child.stdout.close(); child.stderr.close()


def _rows(database, key=None):
    text = _run([GETENT, database, *([] if key is None else [str(key)])], missing=key is not None)
    rows = [] if text is None else [line.split(':') for line in text.splitlines()]
    need(all(len(row) == (7 if database == 'passwd' else 4) for row in rows), 'identity_nss_invalid')
    return rows


def _snapshot(plan):
    users = _rows('passwd'); groups = _rows('group')
    for item in (plan.broker_account, plan.supervisor_account):
        users += _rows('passwd', item.name) + _rows('passwd', item.uid)
    for item in (plan.broker_account, plan.supervisor_account, plan.ipc_group):
        groups += _rows('group', item.name) + _rows('group', item.gid)
    return users, groups


def _steps(plan):
    return [('group', x.name) for x in (plan.broker_account, plan.supervisor_account, plan.ipc_group)] + [('user', x.name) for x in (plan.broker_account, plan.supervisor_account)]


def _stage(plan, completed):
    users, groups = _snapshot(plan); accounts = (plan.broker_account, plan.supervisor_account)
    for item in (*accounts, plan.ipc_group):
        matches = {tuple(row) for row in groups if row[0] == item.name or row[2] == str(item.gid)}
        expected_members = {a.name for a in accounts if ('user', a.name) in completed} if item == plan.ipc_group else set()
        if ('group', item.name) not in completed:
            need(not matches, 'identity_collision')
        else:
            need(len(matches) == 1, 'managed_identity_drift'); row = next(iter(matches))
            need(row[0] == item.name and row[2] == str(item.gid) and set(filter(None, row[3].split(','))) == expected_members, 'managed_identity_drift')
    for item in accounts:
        matches = {tuple(row) for row in users if row[0] == item.name or row[2] == str(item.uid)}
        if ('user', item.name) not in completed:
            need(not matches, 'identity_collision')
        else:
            need(len(matches) == 1, 'managed_identity_drift'); row = next(iter(matches))
            need((row[0], row[2], row[3], row[4], row[5], row[6]) == (item.name, str(item.uid), str(item.gid), 'Harniqo service', '/nonexistent', '/usr/sbin/nologin'), 'managed_identity_drift')
            shadow = _run([GETENT, 'shadow', item.name]); entries = shadow.rstrip('\n').split(':')
            need(len(entries) == 9 and entries[0] == item.name and entries[1].startswith(('!', '*')), 'identity_password_unlocked')
            init = _run([GETENT, 'initgroups', item.name]).split()
            need(init and init[0] == item.name and set(init[1:]) | {str(item.gid)} == {str(item.gid), str(plan.ipc_group.gid)}, 'identity_membership_invalid')
        # Also reject dangling privileged memberships before a fresh username exists.
        for group in groups:
            if item.name in filter(None, group[3].split(',')):
                need(group[0] == plan.ipc_group.name and group[2] == str(plan.ipc_group.gid) and ('user', item.name) in completed, 'identity_membership_invalid')
    gids = {str(x.gid) for x in (*accounts, plan.ipc_group)}
    need(all(row[3] not in gids or any(row[0] == a.name and row[2] == str(a.uid) and row[3] == str(a.gid) and ('user', a.name) in completed for a in accounts) for row in users), 'identity_primary_group_collision')
    need(not Path('/nonexistent').exists() and not Path('/nonexistent').is_symlink(), 'identity_home_present')


def _sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try: os.fsync(fd)
    finally: os.close(fd)


def _directory(path, mode):
    _ancestors(path.parent, root=True)
    need(not path.is_symlink(), 'identity_state_untrusted')
    if not path.exists():
        os.mkdir(path, 0o700)
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            need(stat.S_ISDIR(os.fstat(fd).st_mode) and _root_owned(os.fstat(fd)), 'identity_state_untrusted')
            os.fchmod(fd, mode)
        finally: os.close(fd)
    _ancestors(path, root=True); info = path.lstat()
    need(stat.S_ISDIR(info.st_mode) and stat.S_IMODE(info.st_mode) == mode, 'identity_state_untrusted')
    for name in ('system.posix_acl_access', 'system.posix_acl_default'):
        try: value = os.getxattr(path, name, follow_symlinks=False)
        except OSError as error:
            import errno
            need(error.errno in {errno.ENODATA, errno.ENOTSUP, getattr(errno, 'ENOATTR', errno.ENODATA)}, 'identity_state_untrusted')
        else: need(not value, 'identity_state_untrusted')
    # Existing names may survive a prior failed parent sync or process crash.
    # Reestablish both durability barriers on every admission without chmod.
    _sync_directory(path); _sync_directory(path.parent)


def _encoded(value):
    data = (json.dumps(value, sort_keys=True, separators=(',', ':')) + '\n').encode()
    need(len(data) <= 65536, 'identity_receipt_size')
    return data


class _Store:
    def __init__(self, directory): self.directory = directory
    def read_raw(self):
        path = self.directory / 'receipt.json'
        need(not path.is_symlink(), 'identity_receipt_untrusted')
        if not path.exists(): return None
        need(stat.S_IMODE(path.lstat().st_mode) == 0o600, 'identity_receipt_untrusted')
        return _read(path, 65536, root=True)
    def read(self):
        raw = self.read_raw()
        return None if raw is None else _json(raw)
    def exclusive(self, name, data):
        fd = os.open(self.directory / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        with os.fdopen(fd, 'wb') as out:
            os.fchmod(out.fileno(), 0o600); out.write(data); out.flush(); os.fsync(out.fileno())
        _sync_directory(self.directory)
    def confirmation_files(self, raw, audit):
        prefix = 'confirm-created-' + audit['receipt_sha256_before'] + '-' + secrets.token_hex(6)
        backup, record = prefix + '.before.json', prefix + '.audit.json'
        self.exclusive(backup, raw)
        body = _encoded(dict(audit, backup_file=backup)); self.exclusive(record, body)
        return backup, record, hashlib.sha256(body).hexdigest()
    def write(self, value):
        data = _encoded(value)
        self.read()
        temporary = self.directory / ('.receipt-' + secrets.token_hex(12))
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        with os.fdopen(fd, 'wb') as out:
            os.fchmod(out.fileno(), 0o600); out.write(data); out.flush(); os.fsync(out.fileno())
        os.replace(temporary, self.directory / 'receipt.json')
        _sync_directory(self.directory)


@contextmanager
def _locked(*, create):
    if create:
        _directory(STATE_ROOT.parent, 0o755); _directory(STATE_ROOT, 0o700)
    else:
        need(STATE_ROOT.is_dir(), 'identity_not_provisioned'); _directory(STATE_ROOT, 0o700)
    flags = os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC
    created = False
    try:
        fd = os.open(STATE_ROOT / 'provision.lock', flags | (os.O_CREAT | os.O_EXCL if create else 0), 0o600)
        created = create
    except FileExistsError:
        fd = os.open(STATE_ROOT / 'provision.lock', flags)
    try:
        if created:
            need(stat.S_ISREG(os.fstat(fd).st_mode) and _root_owned(os.fstat(fd)), 'identity_lock_untrusted')
            os.fchmod(fd, 0o600); os.fsync(fd); _sync_directory(STATE_ROOT)
        info = os.fstat(fd)
        need(stat.S_ISREG(info.st_mode) and _root_owned(info) and stat.S_IMODE(info.st_mode) == 0o600 and info.st_nlink == 1 and info.st_size == 0, 'identity_lock_untrusted')
        try: fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: raise IdentityError('identity_provision_in_progress') from None
        yield _Store(STATE_ROOT)
    finally: os.close(fd)


def _receipt(store, plan, digest, *, create, allow_unknown=False):
    value = store.read(); steps = _steps(plan)
    if value is None:
        need(create, 'identity_not_provisioned')
        _stage(plan, set())
        value = {'schema_version': 1, 'plan_sha256': digest, 'plan': asdict(plan), 'steps': [{'kind': k, 'name': n, 'status': 'new'} for k, n in steps]}
        store.write(value)
    need(type(value) is dict and set(value) == {'schema_version', 'plan_sha256', 'plan', 'steps'} and type(value['schema_version']) is int and value['schema_version'] == 1 and value['plan_sha256'] == digest and value['plan'] == asdict(plan), 'identity_receipt_mismatch')
    need(type(value['steps']) is list and len(value['steps']) == len(steps), 'identity_receipt_mismatch')
    for row, (kind, name) in zip(value['steps'], steps):
        need(type(row) is dict and set(row) == {'kind', 'name', 'status'} and (row['kind'], row['name']) == (kind, name)
             and type(row['status']) is str and row['status'] in {'new', 'pending', 'complete', 'unconfirmed'}, 'identity_receipt_mismatch')
    if not allow_unknown:
        need(not any(row['status'] in {'pending', 'unconfirmed'} for row in value['steps']), 'identity_creation_unconfirmed')
    return value


def _confirm_created(store, plan, digest, receipt_sha256, step):
    raw = store.read_raw()
    need(raw is not None and hashlib.sha256(raw).hexdigest() == receipt_sha256, 'identity_receipt_changed')
    value = _receipt(store, plan, digest, create=False, allow_unknown=True)
    keys = [kind + ':' + name for kind, name in _steps(plan)]
    need(step in keys, 'identity_confirmation_invalid'); index = keys.index(step)
    rows = value['steps']; target = rows[index]
    need(target['status'] in {'pending', 'unconfirmed'} and all(row['status'] == 'complete' for row in rows[:index])
         and all(row['status'] == 'new' for row in rows[index + 1:]), 'identity_confirmation_invalid')
    # Explicit administrator confirmation, never an automatic absence inference.
    _stage(plan, {(row['kind'], row['name']) for row in rows[:index + 1]})
    need(store.read_raw() == raw, 'identity_receipt_changed')
    target['status'] = 'complete'; after_sha = hashlib.sha256(_encoded(value)).hexdigest()
    audit = {'schema_version': 1, 'action': 'confirm-created', 'plan_sha256': digest,
             'step': {'kind': target['kind'], 'name': target['name']}, 'operator_real_uid': os.getuid(),
             'operator_effective_uid': os.geteuid(), 'operator_gid': os.getgid(), 'confirmed_at_ns': time.time_ns(),
             'receipt_sha256_before': receipt_sha256, 'receipt_sha256_after': after_sha}
    backup, record, audit_sha = store.confirmation_files(raw, audit)
    need(store.read_raw() == raw, 'identity_receipt_changed')
    store.write(value)
    need(hashlib.sha256(store.read_raw()).hexdigest() == after_sha, 'identity_receipt_changed')
    return {'status': 'confirmed', 'verified': False, 'steps_completed': index + 1, 'confirmed_step': step,
            'receipt_sha256_before': receipt_sha256, 'receipt_sha256_after': after_sha,
            'backup_file': backup, 'audit_file': record, 'audit_sha256': audit_sha}


def _command(plan, kind, name):
    item = next(x for x in (plan.broker_account, plan.supervisor_account, plan.ipc_group) if x.name == name)
    if kind == 'group': return ['/usr/sbin/groupadd', '--gid', str(item.gid), name]
    return ['/usr/sbin/useradd', '--system', '--uid', str(item.uid), '--gid', str(item.gid), '--groups', plan.ipc_group.name,
            '--no-user-group', '--no-create-home', '--no-log-init', '--home-dir', '/nonexistent',
            '--shell', '/usr/sbin/nologin', '--comment', 'Harniqo service', '--password', '!', name]


def execute(plan_path: Path, *, expected_sha256: str, action='plan', receipt_sha256=None, step=None):
    need(type(action) is str and action in {'plan', 'check', 'apply', 'confirm-created'} and type(expected_sha256) is str and re.fullmatch('[0-9a-f]{64}', expected_sha256) is not None, 'identity_arguments_invalid')
    root = os.getuid() == os.geteuid() == 0 and os.getgid() == os.getegid() == 0
    need(os.getuid() == os.geteuid() and os.getgid() == os.getegid(), 'identity_execution_invalid')
    if action in {'apply', 'confirm-created'}: need(root, 'identity_root_required')
    if action == 'confirm-created':
        need(type(receipt_sha256) is str and re.fullmatch('[0-9a-f]{64}', receipt_sha256) is not None and type(step) is str, 'identity_arguments_invalid')
    else: need(receipt_sha256 is None and step is None, 'identity_arguments_invalid')
    if root and action != 'plan':
        need(sys.platform == 'linux', 'identity_platform_unsupported'); _ancestors(Path(__file__).absolute(), root=True)
    data = _read(Path(plan_path), 32768, root=root and action != 'plan')
    need(hashlib.sha256(data).hexdigest() == expected_sha256, 'identity_digest_mismatch'); plan = parse_plan(data)
    summary = {'schema_version': 1, 'plan_sha256': expected_sha256, 'accounts': 2, 'groups': 3, 'activation_authorized': False, 'libvirt_acl_verified': False}
    if action == 'plan':
        _stage(plan, set())
        return dict(summary, status='fresh', verified=False, privileged_verification_required=False)
    if not root:
        _snapshot(plan)
        return dict(summary, status='unverified', verified=False, privileged_verification_required=True)
    with _locked(create=action == 'apply') as store:
        if action == 'confirm-created': return dict(summary, **_confirm_created(store, plan, expected_sha256, receipt_sha256, step))
        value = _receipt(store, plan, expected_sha256, create=action == 'apply')
        for row in value['steps']:
            completed = {(s['kind'], s['name']) for s in value['steps'] if s['status'] == 'complete'}
            _stage(plan, completed)
            if row['status'] == 'complete': continue
            if action == 'check': return dict(summary, status='partial', verified=False, steps_completed=len(completed))
            row['status'] = 'pending'; store.write(value)
            try:
                _run(_command(plan, row['kind'], row['name']))
                _stage(plan, completed | {(row['kind'], row['name'])})
            except IdentityError:
                # NSS absence cannot disprove partial passwd/shadow/group effects.
                # Once attempted, every native or postcondition failure is unknown.
                row['status'] = 'unconfirmed'
                store.write(value); raise IdentityError('identity_provision_failed') from None
            row['status'] = 'complete'; store.write(value)
        _stage(plan, set(_steps(plan)))
        return dict(summary, status='verified', verified=True, steps_completed=5)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('plan', 'check', 'apply', 'confirm-created')); parser.add_argument('--plan', required=True); parser.add_argument('--sha256', required=True)
    parser.add_argument('--receipt-sha256'); parser.add_argument('--step')
    args = parser.parse_args(argv)
    try: result = execute(Path(args.plan), expected_sha256=args.sha256, action=args.action, receipt_sha256=args.receipt_sha256, step=args.step)
    except (IdentityError, OSError) as error:
        print(json.dumps({'status': 'rejected', 'code': str(error) if isinstance(error, IdentityError) else 'identity_io_failed'})); return 1
    print(json.dumps(result, sort_keys=True)); return 0 if result.get('verified') or args.action in {'plan', 'confirm-created'} else 2


if __name__ == '__main__':
    raise SystemExit(main())
