"""Strict, pinned configuration and separate host-local Broker process roles.

``parse_configuration`` is pure so deployment preparation and runtime admission
share one grammar. Runtime entrypoints require ``load_configuration`` and recheck
its pinned source before constructing the real Broker. Initialization is explicit;
normal serving/watching cannot manufacture an empty journal.

Readiness describes process admission and an initial reconciliation attempt, not
VM health, dedicated identity qualification or a bound on host stopping time.
The watcher is its own supervised process; this module does not start one inside
the IPC server. No systemd, network listener, privilege or backend switch is used.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, replace
import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import stat
import sys
import threading
import time
from types import MappingProxyType
import uuid

from . import BrokerError, ResourceBroker, Template, resources
from .ipc import BrokerServer, PeerPolicy, ResourceAccess

MAX_CONFIGURATION_BYTES = 64 * 1024
OPERATIONS = frozenset({'ensure', 'renew', 'observe', 'revoke'})
_FIELDS = frozenset({'schema_version', 'broker_identity', 'state_dir', 'images_root',
    'channel_root', 'ipc_directory', 'issuer_key_file', 'lease', 'templates', 'peers'})
_PATH_FIELDS = ('state_dir', 'images_root', 'channel_root', 'ipc_directory', 'issuer_key_file')
_TEMPLATE_FIELDS = frozenset(Template.__dataclass_fields__)


class ServiceError(BrokerError):
    """A finite local deployment/configuration error, never native exception text."""


@dataclass(frozen=True)
class ServiceIdentity:
    uid: int
    gid: int


@dataclass(frozen=True)
class LeasePolicy:
    max_seconds: int | float
    max_clock_skew_seconds: int | float


@dataclass(frozen=True)
class ServiceConfig:
    schema_version: int
    broker_identity: ServiceIdentity
    state_dir: Path
    images_root: Path
    channel_root: Path
    ipc_directory: Path
    issuer_key_file: Path
    lease: LeasePolicy
    templates: tuple[Template, ...]
    peers: tuple[PeerPolicy, ...]
    sha256: str
    source_path: Path | None = None


def _reject(code='service_configuration_invalid'):
    raise ServiceError(code)


def _object(value, fields):
    if type(value) is not dict or set(value) != set(fields):
        _reject()
    return value


def _id(value):
    return type(value) is int and 0 <= value < 4294967295


def _identifier(value):
    return type(value) is str and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}', value) is not None


def _uuid(value):
    try:
        return type(value) is str and str(uuid.UUID(value)) == value
    except (ValueError, AttributeError, TypeError):
        return False


def _digest(value):
    return type(value) is str and re.fullmatch(r'[0-9a-f]{64}', value) is not None


def _number(value, low, high):
    try:
        return type(value) in (int, float) and math.isfinite(value) and low <= value <= high
    except OverflowError:
        return False


def _path(value):
    try:
        encoded = os.fsencode(value) if type(value) is str else b''
    except UnicodeError:
        _reject('service_path_invalid')
    if (type(value) is not str or not value or len(encoded) > 4096
            or '\0' in value or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        _reject('service_path_invalid')
    path = Path(value)
    if (not path.is_absolute() or str(path) != value or '..' in path.parts
            or value.startswith('//') or path == Path('/')):
        _reject('service_path_invalid')
    return path


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError()
        result[key] = value
    return result


def _integer(value):
    if len(value) > 20:
        raise ValueError()
    return int(value)


def _constant(value):
    raise ValueError()


def parse_configuration(data: bytes) -> ServiceConfig:
    """Parse the deployment grammar without filesystem, account or native I/O.

    The returned frozen DTO contains only references to the issuer key, never key
    bytes. This pure Interface also supports preparing a different host's plan.
    """
    if type(data) is not bytes or not 0 < len(data) <= MAX_CONFIGURATION_BYTES:
        _reject('service_configuration_size')
    try:
        value = json.loads(data.decode('utf-8'), object_pairs_hook=_pairs,
                           parse_int=_integer, parse_constant=_constant)
    except (ValueError, UnicodeError, RecursionError, OverflowError):
        _reject()
    _object(value, _FIELDS)
    if type(value['schema_version']) is not int or value['schema_version'] != 1:
        _reject()
    identity = _object(value['broker_identity'], {'uid', 'gid'})
    if not _id(identity['uid']) or identity['uid'] == 0 or not _id(identity['gid']):
        _reject('service_identity_invalid')
    lease = _object(value['lease'], {'max_seconds', 'max_clock_skew_seconds'})
    if not _number(lease['max_seconds'], 1, 600) or not _number(lease['max_clock_skew_seconds'], 0, 30):
        _reject('service_lease_invalid')
    paths = {field: _path(value[field]) for field in _PATH_FIELDS}
    state, ipc, key = (paths[field] for field in ('state_dir', 'ipc_directory', 'issuer_key_file'))
    if (state == ipc or state in ipc.parents or ipc in state.parents
            or key == ipc or ipc in key.parents):
        _reject('service_roots_overlap')
    entries = value['templates']
    if type(entries) is not list or not 1 <= len(entries) <= 64:
        _reject('service_catalog_invalid')
    templates = []; template_ids = set(); resource_ids = set(); names = set(); files = set()
    for entry in entries:
        _object(entry, _TEMPLATE_FIELDS)
        if (not _identifier(entry['template_id']) or not _uuid(entry['resource_id'])
                or not _identifier(entry['name']) or len(entry['name']) > 80
                or not _digest(entry['source_base_sha256'])
                or type(entry['resource_profile']) is not str
                or entry['resource_profile'] not in resources.PROFILES):
            _reject('service_catalog_invalid')
        if (entry['template_id'] in template_ids or entry['resource_id'] in resource_ids
                or entry['name'] in names):
            _reject('service_catalog_invalid')
        template_ids.add(entry['template_id']); resource_ids.add(entry['resource_id']); names.add(entry['name'])
        fields = dict(entry)
        for role in ('disk', 'seed', 'channel', 'console'):
            path = _path(entry[role])
            root = paths['images_root' if role in {'disk', 'seed'} else 'channel_root']
            if root not in path.parents or path in files:
                _reject('service_catalog_invalid')
            files.add(path); fields[role] = path
        templates.append(Template(**fields))
    entries = value['peers']
    if type(entries) is not list or not 1 <= len(entries) <= 16:
        _reject('service_peers_invalid')
    peers = []; peer_ids = set(); scopes = {}; count = 0
    for entry in entries:
        _object(entry, {'uid', 'gid', 'resources'})
        if (not _id(entry['uid']) or not _id(entry['gid'])
                or (entry['uid'], entry['gid']) in peer_ids
                or type(entry['resources']) is not list or not entry['resources']):
            _reject('service_peers_invalid')
        peer_ids.add((entry['uid'], entry['gid']))
        accesses = []; seen = set()
        for access in entry['resources']:
            count += 1
            _object(access, {'resource_id', 'organization_id', 'workspace_id', 'operations'})
            operations = access['operations']
            if (count > 64 or not _uuid(access['resource_id']) or access['resource_id'] not in resource_ids
                    or access['resource_id'] in seen or not _identifier(access['organization_id'])
                    or not _identifier(access['workspace_id']) or type(operations) is not list
                    or not 1 <= len(operations) <= 4 or any(type(op) is not str for op in operations)
                    or len(set(operations)) != len(operations) or not set(operations) <= OPERATIONS):
                _reject('service_peers_invalid')
            scope = (access['organization_id'], access['workspace_id'])
            if access['resource_id'] in scopes and scopes[access['resource_id']] != scope:
                _reject('service_peers_invalid')
            seen.add(access['resource_id']); scopes[access['resource_id']] = scope
            accesses.append(ResourceAccess(access['resource_id'], *scope, frozenset(operations)))
        peers.append(PeerPolicy(entry['uid'], entry['gid'], tuple(accesses)))
    return ServiceConfig(1, ServiceIdentity(**identity), **paths, lease=LeasePolicy(**lease),
                         templates=tuple(templates), peers=tuple(peers), sha256=hashlib.sha256(data).hexdigest())


def _no_symlinks(path):
    for part in (path, *path.parents):
        if part.is_symlink():
            _reject('service_symlink_rejected')


def _trusted_ancestors(path, uid):
    result = []
    for parent in path.parents:
        info = parent.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid not in {0, uid}
                or stat.S_IMODE(info.st_mode) & 0o022):
            _reject('service_configuration_parent_untrusted')
        result.append((parent, info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid))
    return result


def _token(info):
    return (info.st_dev, info.st_ino, info.st_uid, info.st_gid, info.st_mode,
            info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def load_configuration(path: Path, *, expected_sha256: str) -> ServiceConfig:
    """Admit an immutable file, mandatory digest and the current non-root identity.

    Native key, catalog and journal validation remains in ResourceBroker. This
    function never constructs it, changes permissions or initializes state.
    """
    if not _digest(expected_sha256):
        _reject('service_configuration_digest_invalid')
    try:
        path = _path(os.fspath(path))
        uid, gid = os.getuid(), os.getgid()
        if uid == 0 or uid != os.geteuid() or gid != os.getegid():
            _reject('service_identity_invalid')
        _no_symlinks(path)
        before_parents = _trusted_ancestors(path, uid)
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as stream:
            before = os.fstat(stream.fileno())
            if (not stat.S_ISREG(before.st_mode) or before.st_uid not in {0, uid}
                    or stat.S_IMODE(before.st_mode) not in {0o600, 0o640} or before.st_nlink != 1):
                _reject('service_configuration_file_untrusted')
            data = stream.read(MAX_CONFIGURATION_BYTES + 1)
            if (_token(before) != _token(os.fstat(stream.fileno()))
                    or _token(before) != _token(path.lstat())
                    or before_parents != _trusted_ancestors(path, uid)):
                _reject('service_configuration_changed')
        if hashlib.sha256(data).hexdigest() != expected_sha256:
            _reject('service_configuration_digest_mismatch')
        config = parse_configuration(data)
        if config.broker_identity != ServiceIdentity(uid, gid):
            _reject('service_identity_mismatch')
        for field in _PATH_FIELDS:
            _no_symlinks(getattr(config, field))
        for template in config.templates:
            for role in ('disk', 'seed', 'channel', 'console'):
                _no_symlinks(getattr(template, role))
        return replace(config, source_path=path)
    except (OSError, ValueError, TypeError):
        _reject('service_configuration_unavailable')


def _admitted(config):
    if type(config) is not ServiceConfig or config.source_path is None:
        _reject('service_configuration_not_admitted')
    current = load_configuration(config.source_path, expected_sha256=config.sha256)
    if current != config:
        _reject('service_configuration_changed')
    return current


def _private_file(path, uid, *, exact_size=None):
    _no_symlinks(path)
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != uid
            or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1
            or (exact_size is not None and info.st_size != exact_size)):
        _reject('service_private_file_untrusted')


def _existing_state(config, *, include_journal=False):
    """Check startup prerequisites without preventing core journal recovery.

    Missing/corrupt journals must reach the original constructor: an authenticated
    anchor lets it attempt ownership-checked emergency stops before refusing.
    ``initialize_state=False`` in that constructor closes the deletion race and
    cannot create a new directory, lock, anchor or journal.
    """
    try:
        _no_symlinks(config.state_dir)
        info = config.state_dir.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != config.broker_identity.uid
                or stat.S_IMODE(info.st_mode) != 0o700):
            _reject('service_state_untrusted')
        names = ('broker.lock', 'anchor.json', 'journal.json') if include_journal else ('broker.lock', 'anchor.json')
        for name in names:
            _private_file(config.state_dir / name, config.broker_identity.uid)
    except OSError:
        _reject('state_not_initialized')


def _broker(config, *, initialize_state=False):
    return ResourceBroker(state_dir=config.state_dir, templates=config.templates,
        issuer_key_file=config.issuer_key_file, images_root=config.images_root,
        channel_root=config.channel_root, max_lease_seconds=config.lease.max_seconds,
        max_clock_skew_seconds=config.lease.max_clock_skew_seconds,
        initialize_state=initialize_state)


def initialize(config: ServiceConfig) -> None:
    """Initialize only a new/empty, caller-prepared fixed instance group."""
    config = _admitted(config)
    try:
        if config.state_dir.exists() and any(config.state_dir.iterdir()):
            _reject('service_state_already_initialized')
        _broker(config, initialize_state=True)
    except OSError:
        _reject('service_initialization_unavailable')


def _safe_code(error):
    code = error.code if isinstance(error, BrokerError) else None
    if type(code) is str and re.fullmatch(r'[a-z][a-z0-9_]{0,79}', code):
        return code
    return 'service_operation_failed'


def _initial_reconcile(broker):
    try:
        observations = broker.reconcile()
        for observation in observations:
            if observation.error_code is not None or observation.state == 'reconciling':
                code = observation.error_code or 'service_reconciling'
                return {'status': 'degraded', 'error_code': _safe_code(BrokerError(code))}
        return {'status': 'operational'}
    except BrokerError as error:
        # The core has already attempted its authenticated fail-closed action.
        # Keep this same instance: watch() retains its trusted retry snapshot.
        return {'status': 'degraded', 'error_code': _safe_code(error)}


def _ready(callback, role, status):
    if callback is not None:
        callback(MappingProxyType({'schema_version': 1, 'event': 'ready',
            'role': role, 'pid': os.getpid(), **status}))


def watch(config: ServiceConfig, *, stop_event=None, ready=None) -> None:
    """Run the original cached, degraded-retrying watch in this process."""
    config = _admitted(config)
    _existing_state(config)
    broker = _broker(config)
    stop = stop_event if stop_event is not None else threading.Event()
    status = _initial_reconcile(broker)
    _ready(ready, 'watch', status)
    broker.watch(stop_event=stop)


def serve(config: ServiceConfig, *, stop_event=None, ready=None) -> None:
    """Serve IPC, closing admission on stop and draining an admitted core call.

    The control thread can close an accepted idle connection's admission while
    the listener thread finishes transport or a core operation. No unsafe thread
    cancellation is attempted; an external process supervisor owns stop timeouts.
    """
    config = _admitted(config)
    _existing_state(config)
    broker = _broker(config)
    stop = stop_event if stop_event is not None else threading.Event()
    status = _initial_reconcile(broker)
    done = threading.Event(); failures = []
    with BrokerServer(directory=config.ipc_directory, broker=broker, peers=config.peers) as server:
        def run():
            try:
                server.serve(stop)
            except BaseException as error:
                failures.append(error)
            finally:
                done.set()
        worker = threading.Thread(target=run, name='broker-ipc')
        worker.start()
        try:
            if not done.is_set() and not stop.is_set():
                _ready(ready, 'serve', status)
            while not done.is_set() and not stop.is_set():
                done.wait(.05)
        finally:
            server.close()
            stop.set()
            worker.join()
        if failures:
            raise failures[0]


def _check(config):
    _existing_state(config, include_journal=True)
    try:
        for directory in (config.images_root, config.channel_root, config.issuer_key_file.parent):
            info = directory.lstat()
            if (not stat.S_ISDIR(info.st_mode) or info.st_uid != config.broker_identity.uid
                    or info.st_mode & (0o077 if directory == config.issuer_key_file.parent else 0o022)):
                _reject('service_directory_untrusted')
        _private_file(config.issuer_key_file, config.broker_identity.uid, exact_size=32)
        info = config.ipc_directory.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != config.broker_identity.uid
                or stat.S_IMODE(info.st_mode) not in {0o700, 0o750}
                or info.st_gid not in {os.getgid(), *os.getgroups()}):
            _reject('service_ipc_directory_untrusted')
    except OSError:
        _reject('service_directory_unavailable')


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        _reject('service_arguments_invalid')


class _SignalStop:
    """CLI stop flag with no lock acquisition in the Python signal handler.

    A signal can interrupt Event.wait() while its Condition lock is held. Calling
    Event.set() there would deadlock the main thread. Keep signal handling to a
    boolean assignment and poll outside the handler; external API callers can
    continue supplying their own threading.Event.
    """
    def __init__(self):
        self._stopped = False

    def set(self):
        self._stopped = True

    def is_set(self):
        return self._stopped

    def wait(self, timeout=None):
        until = None if timeout is None else time.monotonic() + max(0, timeout)
        while not self._stopped:
            remaining = .05 if until is None else min(.05, until - time.monotonic())
            if remaining <= 0:
                break
            time.sleep(remaining)
        return self._stopped


def _emit(event):
    try:
        sys.stdout.write(json.dumps(dict(event), sort_keys=True, separators=(',', ':')) + '\n')
        sys.stdout.flush()
    except OSError:
        _reject('service_output_unavailable')


def main(argv=None) -> int:
    """CLI emits only finite JSON readiness/results/errors, never tracebacks."""
    handlers = {}
    try:
        parser = _Parser(description=__doc__)
        parser.add_argument('role', choices=('initialize', 'serve', 'watch', 'check'))
        parser.add_argument('--config', required=True)
        parser.add_argument('--sha256', required=True)
        args = parser.parse_args(argv)
        stop = _SignalStop()
        def stopped(signum, frame):
            stop.set()
        for sig in (signal.SIGTERM, signal.SIGINT):
            handlers[sig] = signal.signal(sig, stopped)
        config = load_configuration(Path(args.config), expected_sha256=args.sha256)
        if args.role == 'initialize':
            initialize(config)
            _emit({'schema_version': 1, 'event': 'initialized'})
        elif args.role == 'check':
            _check(config)
            _emit({'schema_version': 1, 'event': 'checked', 'scope': 'configuration_and_metadata'})
        elif args.role == 'serve':
            serve(config, stop_event=stop, ready=_emit)
        else:
            watch(config, stop_event=stop, ready=_emit)
        return 0
    except Exception as error:
        try:
            _emit({'schema_version': 1, 'event': 'error', 'code': _safe_code(error)})
        except ServiceError:
            # A closed supervisor pipe cannot receive diagnostics. Do not emit
            # a secondary traceback to an unrelated stderr stream.
            pass
        return 1
    except KeyboardInterrupt:
        return 130
    finally:
        for sig, previous in handlers.items():
            signal.signal(sig, previous)


if __name__ == '__main__':
    raise SystemExit(main())
