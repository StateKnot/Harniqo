"""Bounded Linux host-local IPC for the fixed Resource Broker instance group.

The listener owns transport framing, peer/resource authorization and safe wire
projection. The broker still owns signatures, durable intent and host facts.
``observe`` reconciles lifecycle and may stop expired/unverifiable resources.
Request IDs correlate replies; they are not durable idempotency receipts.

There are no retries. After core entry, failure, disconnect and client timeout
have an unconfirmed outcome: query the authorized UUID before deciding what to
do next. A socket deadline does not interrupt a broker call, blocking core flock
or filesystem operation. Independently supervised broker watch and process
supervision remain deployment requirements; no hard lease-stop latency is
established by this Module. Forking after listener construction is unsupported.

Provision the directory as broker-owned 0700 or 0750, with no symlink ancestors.
The latter permits a distinct Supervisor UID through its explicitly provisioned
group. The socket is 0660 in that group; the broker must belong to it. State/key
directories remain private. Do not grant clients write permission on this
directory. Same-UID filesystem writers are trusted. Corrupt lock metadata or a
socket created by a crash before identity persistence is preserved for operator
recovery, never guessed to be safe to delete.
"""
from __future__ import annotations

import array
from dataclasses import asdict, dataclass
import errno
import fcntl
import json
import math
import os
from pathlib import Path
import re
import secrets
import socket
import stat
import struct
import sys
import threading
import time
import uuid

from . import Assignment, BrokerError, Grant, Observation, StopEvidence, STOP_CAUSES, resources

REQUEST_LIMIT = 16384
REPLY_LIMIT = 8192
TRANSPORT_SECONDS = 1.0
DISPATCH_SECONDS = 5.0
BACKLOG = 8
SOCKET_NAME = 'broker.sock'
LOCK_NAME = 'listener.lock'
OPERATIONS = frozenset({'ensure', 'renew', 'observe', 'revoke'})
CAUSES = frozenset({'cancelled', 'stop_intent'})
_ENVELOPE_FIELDS = frozenset(Assignment.__dataclass_fields__)
_UCRED = struct.Struct('=iII')
_SAFE_BROKER_CODES = frozenset({
    'broker_operation_failed', 'assignment_conflict', 'configuration_changed',
    'configuration_digest_invalid', 'deadline_invalid', 'dispatch_unconfirmed',
    'generation_invalid', 'generation_stale', 'grant_identity_invalid',
    'grant_type_invalid', 'host_clock_unavailable', 'issued_in_future',
    'journal_capacity_exceeded', 'journal_corrupt', 'journal_integrity_invalid',
    'journal_record_invalid', 'journal_schema_invalid', 'journal_template_missing',
    'lease_expired', 'lease_too_long', 'libvirt_call_unconfirmed',
    'libvirt_operation_failed', 'libvirt_output_invalid', 'native_output_limit',
    'renewal_binding_mismatch', 'resource_already_exists', 'resource_not_registered',
    'resource_not_running', 'resource_scope_immutable', 'resource_stopping',
    'resource_terminal', 'resource_uuid_invalid', 'revoke_cause_invalid',
    'signature_invalid', 'start_not_observed', 'starting', 'state_anchor_invalid',
    'state_anchor_missing', 'state_catalog_changed', 'state_lock_invalid',
    'state_lock_replaced', 'state_unavailable', 'stop_unconfirmed',
    'template_binding_invalid', 'workspace_busy', 'resource_limits_xml_mismatch',
    'lease_expired_before_dispatch', 'lease_expired_during_resource_sample',
}) | resources.ERROR_CODES | STOP_CAUSES
_IPC_CODES = frozenset({
    'ipc_platform_unsupported', 'ipc_identity_invalid', 'ipc_policy_invalid',
    'ipc_directory_invalid', 'ipc_directory_changed', 'ipc_listener_in_use',
    'ipc_lock_invalid', 'ipc_lock_changed', 'ipc_socket_untrusted',
    'ipc_socket_active', 'ipc_unavailable', 'ipc_closed', 'ipc_fork_unsupported',
    'ipc_protocol_invalid', 'ipc_frame_limit', 'ipc_ancillary_invalid',
    'ipc_peer_denied', 'ipc_resource_denied', 'ipc_operation_denied',
    'ipc_scope_denied', 'ipc_dispatch_expired', 'ipc_timeout',
    'ipc_reply_invalid', 'ipc_broker_result_invalid',
})


class IPCError(BrokerError):
    def __init__(self, code: str, *, outcome='not_dispatched'):
        if code not in _IPC_CODES | _SAFE_BROKER_CODES:
            code = 'broker_operation_failed'
        super().__init__(code)
        self.outcome = outcome if outcome in {'not_dispatched', 'unconfirmed'} else 'unconfirmed'


@dataclass(frozen=True)
class ResourceAccess:
    resource_id: str
    organization_id: str
    workspace_id: str
    operations: frozenset[str]


@dataclass(frozen=True)
class PeerPolicy:
    uid: int
    gid: int
    resources: tuple[ResourceAccess, ...]


def _platform():
    if sys.platform != 'linux' or any(not hasattr(socket, name) for name in
            ('SOCK_SEQPACKET', 'SO_PEERCRED', 'SO_PASSCRED', 'SCM_CREDENTIALS', 'MSG_CMSG_CLOEXEC')):
        raise IPCError('ipc_platform_unsupported')
    if os.getuid() != os.geteuid() or os.getgid() != os.getegid():
        raise IPCError('ipc_identity_invalid')


def _id(value):
    return type(value) is int and 0 <= value < 4294967295


def _identifier(value):
    return type(value) is str and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}', value) is not None


def _uuid(value):
    try:
        return type(value) is str and str(uuid.UUID(value)) == value
    except (ValueError, AttributeError, TypeError):
        return False


def _finite(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def _boottime():
    return time.clock_gettime(time.CLOCK_BOOTTIME)


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate key')
        result[key] = value
    return result


def _integer(value):
    if len(value) > 20:
        raise ValueError('integer size')
    return int(value)


def _constant(value):
    raise ValueError('nonfinite JSON')


def _decode(data, limit):
    if not data or len(data) > limit:
        raise IPCError('ipc_frame_limit')
    try:
        result = json.loads(data.decode('utf-8'), object_pairs_hook=_pairs,
            parse_int=_integer, parse_constant=_constant)
        if type(result) is not dict:
            raise ValueError()
        return result
    except (ValueError, UnicodeError, RecursionError, OverflowError):
        raise IPCError('ipc_protocol_invalid') from None


def _encode(value, limit):
    try:
        data = json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')
    except (ValueError, TypeError, RecursionError, OverflowError):
        raise IPCError('ipc_protocol_invalid') from None
    if len(data) > limit:
        raise IPCError('ipc_frame_limit')
    return data


def _no_acl(target, *, directory=False, code='ipc_directory_invalid'):
    """Mode/group provisioning only; unexpected named/default ACLs fail closed."""
    try:
        before = os.fstat(target) if type(target) is int else os.lstat(target)
        names = ('system.posix_acl_access', 'system.posix_acl_default') if directory else ('system.posix_acl_access',)
        for name in names:
            try:
                if type(target) is int:
                    os.getxattr(target, name)
                else:
                    os.getxattr(target, name, follow_symlinks=False)
            except OSError as error:
                if error.errno not in {errno.ENODATA, errno.ENOTSUP, errno.EOPNOTSUPP}:
                    raise IPCError(code) from None
            else:
                raise IPCError(code)
        after = os.fstat(target) if type(target) is int else os.lstat(target)
        if (before.st_dev, before.st_ino, before.st_uid, before.st_gid, before.st_mode, before.st_nlink) != (
                after.st_dev, after.st_ino, after.st_uid, after.st_gid, after.st_mode, after.st_nlink):
            raise IPCError(code)
    except OSError:
        raise IPCError(code) from None


def _path_info(directory, owner):
    directory = Path(directory)
    if not directory.is_absolute() or '..' in directory.parts:
        raise IPCError('ipc_directory_invalid')
    try:
        if any(part.is_symlink() for part in (directory, *directory.parents)):
            raise IPCError('ipc_directory_invalid')
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != owner or stat.S_IMODE(info.st_mode) not in {0o700, 0o750}:
            raise IPCError('ipc_directory_invalid')
        _no_acl(directory, directory=True)
        if _identity(directory.lstat()) != _identity(info):
            raise IPCError('ipc_directory_changed')
        return info
    except OSError:
        raise IPCError('ipc_directory_invalid') from None


def _identity(info):
    return info.st_dev, info.st_ino


def _socket_token(info):
    # UNIX socket inode numbers may be reused immediately after unlink. Bind
    # cleanup authority to the final metadata change as well as device/inode.
    return info.st_dev, info.st_ino, info.st_ctime_ns


def _credentials(connection):
    try:
        return _UCRED.unpack(connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, _UCRED.size))
    except (OSError, struct.error):
        raise IPCError('ipc_peer_denied') from None


def _receive(connection, limit, expected, deadline):
    remaining = min(TRANSPORT_SECONDS, deadline - _boottime())
    if remaining <= 0:
        raise IPCError('ipc_timeout')
    connection.settimeout(remaining)
    delivered = []
    try:
        data, ancillary, flags, _ = connection.recvmsg(limit + 1,
            socket.CMSG_SPACE(_UCRED.size) + socket.CMSG_SPACE(256 * array.array('i').itemsize),
            socket.MSG_CMSG_CLOEXEC)
        credentials = []
        invalid = False
        for level, kind, value in ancillary:
            if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
                descriptors = array.array('i')
                descriptors.frombytes(value[:len(value) - len(value) % descriptors.itemsize])
                delivered.extend(descriptors)
                invalid = True
            elif level == socket.SOL_SOCKET and kind == socket.SCM_CREDENTIALS and len(value) == _UCRED.size:
                credentials.append(_UCRED.unpack(value))
            else:
                invalid = True
        if flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC) or len(data) > limit:
            raise IPCError('ipc_frame_limit')
        if invalid or credentials != [expected]:
            raise IPCError('ipc_ancillary_invalid')
        if _boottime() >= deadline:
            raise IPCError('ipc_timeout')
        return _decode(data, limit)
    except socket.timeout:
        raise IPCError('ipc_timeout') from None
    except OSError:
        raise IPCError('ipc_unavailable') from None
    finally:
        for descriptor in delivered:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _send(connection, value, limit, deadline):
    data = _encode(value, limit)
    remaining = min(TRANSPORT_SECONDS, deadline - _boottime())
    if remaining <= 0:
        raise IPCError('ipc_timeout')
    connection.settimeout(remaining)
    try:
        if connection.send(data) != len(data):
            raise IPCError('ipc_unavailable')
    except socket.timeout:
        raise IPCError('ipc_timeout') from None
    except OSError:
        raise IPCError('ipc_unavailable') from None


def _envelope(value, operation):
    if type(value) is not dict or set(value) != _ENVELOPE_FIELDS:
        raise IPCError('ipc_protocol_invalid')
    if not _uuid(value['resource_id']) or any(not _identifier(value[name]) for name in
            ('organization_id', 'workspace_id', 'attempt_id', 'template_id')):
        raise IPCError('ipc_protocol_invalid')
    if any(type(value[name]) is not str or re.fullmatch(r'[0-9a-f]{64}', value[name]) is None
            for name in ('configuration_sha256', 'signature')):
        raise IPCError('ipc_protocol_invalid')
    if any(type(value[name]) is not int or not 1 <= value[name] <= 9007199254740991
            for name in ('generation', 'recovery_generation')):
        raise IPCError('ipc_protocol_invalid')
    if any(not _finite(value[name]) for name in ('issued_at', 'expires_at')):
        raise IPCError('ipc_protocol_invalid')
    return (Assignment if operation == 'ensure' else Grant)(**value)


def _wire_observation(value):
    if type(value) is not Observation or not _uuid(value.resource_id) or value.state not in {'running', 'reconciling', 'stopped'}:
        raise IPCError('ipc_broker_result_invalid', outcome='unconfirmed')
    if any(type(number) is not int or not 1 <= number <= 9007199254740991
            for number in (value.generation, value.recovery_generation)):
        raise IPCError('ipc_broker_result_invalid', outcome='unconfirmed')
    result = dict(resource_id=value.resource_id, state=value.state,
        generation=value.generation, recovery_generation=value.recovery_generation,
        stop_evidence=None, error_code=None, stop_cause=None, limits_error=None)
    if value.error_code is not None:
        result['error_code'] = value.error_code if value.error_code in _SAFE_BROKER_CODES else 'broker_operation_failed'
    if value.stop_cause is not None:
        allowed = _SAFE_BROKER_CODES | {'requested_revoke', 'host_absence_after_confirmed_dispatch'}
        result['stop_cause'] = value.stop_cause if value.stop_cause in allowed else None
    if value.limits_error is not None:
        diagnostic = value.limits_error if type(value.limits_error) is dict else {}
        result['limits_error'] = {
            'code': diagnostic.get('code') if diagnostic.get('code') in resources.ERROR_CODES | {'resource_limits_xml_mismatch'} else 'resource_limits_observation_unavailable',
            'phase': diagnostic.get('phase') if diagnostic.get('phase') in resources.ERROR_PHASES | {'unknown', 'domain_xml'} else 'unknown',
        }
    evidence = value.stop_evidence
    if evidence is not None:
        if (type(evidence) is not StopEvidence or evidence.resource_id != value.resource_id
                or not _finite(evidence.confirmed_at) or evidence.confirmed_at < 0
                or type(evidence.configuration_sha256) is not str
                or re.fullmatch(r'[0-9a-f]{64}', evidence.configuration_sha256) is None
                or evidence.method not in {'libvirt_destroy_then_absent', 'libvirt_absent_after_confirmed_start', 'libvirt_absent_without_dispatch'}):
            raise IPCError('ipc_broker_result_invalid', outcome='unconfirmed')
        result['stop_evidence'] = asdict(evidence)
    return result


def _observation(value):
    try:
        if type(value) is not dict or set(value) != set(Observation.__dataclass_fields__):
            raise ValueError()
        fields = dict(value)
        if fields['stop_evidence'] is not None:
            fields['stop_evidence'] = StopEvidence(**fields['stop_evidence'])
        result = Observation(**fields)
        if _wire_observation(result) != value:
            raise ValueError()
        return result
    except (TypeError, ValueError, KeyError, IPCError):
        raise IPCError('ipc_reply_invalid', outcome='unconfirmed') from None


class BrokerServer:
    """One in-flight broker operation, bounded backlog, no per-client workers.

    Construct and serve in the same process. ``close`` stops new accepts; an
    already dispatched core call is allowed to finish. The caller must join its
    serve thread before disposing the broker or terminating its process.
    """
    def __init__(self, *, directory: Path, broker, peers: tuple[PeerPolicy, ...]):
        _platform()
        self.directory = Path(directory)
        self._broker = broker
        self._pid = os.getpid()
        self._uid = os.getuid()
        self._policies = self._validate_policies(peers)
        info = _path_info(self.directory, self._uid)
        if info.st_gid not in {os.getgid(), *os.getgroups()}:
            raise IPCError('ipc_identity_invalid')
        if len(os.fsencode(self.directory / SOCKET_NAME)) > 107:
            raise IPCError('ipc_directory_invalid')
        self._directory_identity = _identity(info)
        self._directory_gid = info.st_gid
        self._directory_fd = None
        self._lock_fd = None
        self._socket = None
        self._socket_identity = None
        self._closed = threading.Event()
        self._serving = threading.Lock()
        self._lifecycle = threading.RLock()
        self._active = False
        try:
            self._directory_fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
            if _identity(os.fstat(self._directory_fd)) != self._directory_identity:
                raise IPCError('ipc_directory_changed')
            self._open_lock()
            self._check_integrity()
            self._prepare_socket()
        except OSError:
            self.close()
            raise IPCError('ipc_unavailable') from None
        except BaseException:
            self.close()
            raise

    @staticmethod
    def _validate_policies(peers):
        if type(peers) is not tuple or not 1 <= len(peers) <= 16:
            raise IPCError('ipc_policy_invalid')
        result = {}
        scopes = {}
        count = 0
        for peer in peers:
            if type(peer) is not PeerPolicy or not _id(peer.uid) or not _id(peer.gid) or (peer.uid, peer.gid) in result:
                raise IPCError('ipc_policy_invalid')
            if type(peer.resources) is not tuple or not peer.resources:
                raise IPCError('ipc_policy_invalid')
            access = {}
            for resource in peer.resources:
                count += 1
                if (count > 64 or type(resource) is not ResourceAccess or not _uuid(resource.resource_id)
                        or not _identifier(resource.organization_id) or not _identifier(resource.workspace_id)
                        or type(resource.operations) is not frozenset or not resource.operations
                        or not resource.operations <= OPERATIONS or resource.resource_id in access):
                    raise IPCError('ipc_policy_invalid')
                scope = resource.organization_id, resource.workspace_id
                if resource.resource_id in scopes and scopes[resource.resource_id] != scope:
                    raise IPCError('ipc_policy_invalid')
                scopes[resource.resource_id] = scope
                access[resource.resource_id] = resource
            result[peer.uid, peer.gid] = access
        return result

    def _open_lock(self):
        created = False
        try:
            self._lock_fd = os.open(LOCK_NAME, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=self._directory_fd)
            created = True
        except FileExistsError:
            self._lock_fd = os.open(LOCK_NAME, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=self._directory_fd)
        info = os.fstat(self._lock_fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != self._uid or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1:
            raise IPCError('ipc_lock_invalid')
        _no_acl(self._lock_fd, code='ipc_lock_invalid')
        self._lock_identity = _identity(info)
        try:
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise IPCError('ipc_listener_in_use') from None
        if created:
            self._metadata = dict(version=1, directory=list(self._directory_identity), lock=list(self._lock_identity), socket=None)
            self._write_metadata()
        else:
            try:
                self._metadata = _decode(os.read(self._lock_fd, 1025), 1024)
                if (set(self._metadata) != {'version', 'directory', 'lock', 'socket'}
                        or self._metadata['version'] != 1 or type(self._metadata['version']) is not int
                        or self._metadata['directory'] != list(self._directory_identity)
                        or self._metadata['lock'] != list(self._lock_identity)
                        or (self._metadata['socket'] is not None and
                            (type(self._metadata['socket']) is not list or len(self._metadata['socket']) != 3
                             or any(type(item) is not int or item < 0 for item in self._metadata['socket'])))):
                    raise ValueError()
            except (IPCError, ValueError, TypeError):
                raise IPCError('ipc_lock_invalid') from None

    def _write_metadata(self):
        data = _encode(self._metadata, 1024)
        os.lseek(self._lock_fd, 0, os.SEEK_SET)
        os.ftruncate(self._lock_fd, 0)
        offset = 0
        while offset < len(data):
            offset += os.write(self._lock_fd, data[offset:])
        os.fsync(self._lock_fd)
        os.fsync(self._directory_fd)

    def _check_integrity(self):
        if self._pid != os.getpid():
            raise IPCError('ipc_fork_unsupported')
        _platform()
        if _identity(_path_info(self.directory, self._uid)) != self._directory_identity:
            raise IPCError('ipc_directory_changed')
        try:
            info = os.stat(LOCK_NAME, dir_fd=self._directory_fd, follow_symlinks=False)
            if (_identity(info) != self._lock_identity or not stat.S_ISREG(info.st_mode)
                    or info.st_uid != self._uid or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600):
                raise IPCError('ipc_lock_changed')
            _no_acl(self._lock_fd, code='ipc_lock_changed')
            if self._socket_identity is not None:
                self._check_socket(self._socket_identity)
        except OSError:
            raise IPCError('ipc_lock_changed') from None

    def _check_socket(self, identity):
        info = os.stat(SOCKET_NAME, dir_fd=self._directory_fd, follow_symlinks=False)
        if (not stat.S_ISSOCK(info.st_mode) or _socket_token(info) != tuple(identity) or info.st_uid != self._uid
                or info.st_gid != self._directory_gid or stat.S_IMODE(info.st_mode) != 0o660 or info.st_nlink != 1):
            raise IPCError('ipc_socket_untrusted')
        _no_acl(self.directory / SOCKET_NAME, code='ipc_socket_untrusted')
        return info

    def _prepare_socket(self):
        try:
            info = os.stat(SOCKET_NAME, dir_fd=self._directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            info = None
        if info is not None:
            if self._metadata['socket'] is None:
                raise IPCError('ipc_socket_untrusted')
            self._check_socket(self._metadata['socket'])
            with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as probe:
                probe.settimeout(TRANSPORT_SECONDS)
                try:
                    probe.connect(str(self.directory / SOCKET_NAME))
                except OSError as error:
                    if error.errno != errno.ECONNREFUSED:
                        raise IPCError('ipc_socket_untrusted') from None
                else:
                    raise IPCError('ipc_socket_active')
            self._check_integrity()
            self._check_socket(self._metadata['socket'])
            os.unlink(SOCKET_NAME, dir_fd=self._directory_fd)
        self._socket = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
        self._socket.bind(str(self.directory / SOCKET_NAME))
        os.chown(SOCKET_NAME, self._uid, self._directory_gid, dir_fd=self._directory_fd, follow_symlinks=False)
        os.chmod(SOCKET_NAME, 0o660, dir_fd=self._directory_fd, follow_symlinks=False)
        info = os.stat(SOCKET_NAME, dir_fd=self._directory_fd, follow_symlinks=False)
        self._socket_identity = _socket_token(info)
        self._check_integrity()
        self._metadata['socket'] = list(self._socket_identity)
        self._write_metadata()
        self._socket.listen(BACKLOG)
        self._socket.settimeout(.1)

    def __enter__(self):
        if self._closed.is_set():
            raise IPCError('ipc_closed')
        return self

    def __exit__(self, *ignored):
        self.close()

    def _dispatch(self, request, peer):
        if (set(request) != {'version', 'request_id', 'operation', 'arguments', 'dispatch_before'}
                or type(request['version']) is not int or request['version'] != 1
                or type(request['request_id']) is not str or re.fullmatch(r'[0-9a-f]{32}', request['request_id']) is None
                or type(request['operation']) is not str or request['operation'] not in OPERATIONS
                or type(request['arguments']) is not dict or not _finite(request['dispatch_before'])):
            raise IPCError('ipc_protocol_invalid')
        operation = request['operation']
        arguments = request['arguments']
        envelope = None
        if operation in {'ensure', 'renew'}:
            if set(arguments) != {'envelope'}:
                raise IPCError('ipc_protocol_invalid')
            envelope = _envelope(arguments['envelope'], operation)
            resource_id = envelope.resource_id
        else:
            if set(arguments) != ({'resource_id'} if operation == 'observe' else {'resource_id', 'cause'}):
                raise IPCError('ipc_protocol_invalid')
            resource_id = arguments['resource_id']
            if not _uuid(resource_id) or (operation == 'revoke' and
                    (type(arguments['cause']) is not str or arguments['cause'] not in CAUSES)):
                raise IPCError('ipc_protocol_invalid')
        policy = self._policies.get((peer[1], peer[2]))
        if policy is None:
            raise IPCError('ipc_peer_denied')
        access = policy.get(resource_id)
        if access is None:
            raise IPCError('ipc_resource_denied')
        if operation not in access.operations:
            raise IPCError('ipc_operation_denied')
        if envelope is not None and (envelope.organization_id, envelope.workspace_id) != (access.organization_id, access.workspace_id):
            raise IPCError('ipc_scope_denied')
        self._check_integrity()
        with self._lifecycle:
            if self._closed.is_set():
                raise IPCError('ipc_closed')
            now = _boottime()
            if not now < request['dispatch_before'] <= now + DISPATCH_SECONDS:
                raise IPCError('ipc_dispatch_expired')
            # Admission linearizes here, against close() on the same lock. An
            # admitted call may finish after close; an accepted idle peer may
            # not enter the broker after close. No blocking checks follow.
        try:
            if operation == 'ensure':
                result = self._broker.ensure(envelope)
            elif operation == 'renew':
                result = self._broker.renew(envelope)
            elif operation == 'observe':
                result = self._broker.observe(resource_id)
            else:
                result = self._broker.revoke(resource_id, arguments['cause'])
            projected = _wire_observation(result)
            if projected['resource_id'] != resource_id:
                raise IPCError('ipc_broker_result_invalid', outcome='unconfirmed')
            return {'version': 1, 'request_id': request['request_id'], 'observation': projected}
        except Exception as error:
            code = error.code if isinstance(error, BrokerError) and type(error.code) is str and error.code in _SAFE_BROKER_CODES else 'broker_operation_failed'
            raise IPCError(code, outcome='unconfirmed') from None

    def _handle(self, connection):
        request_id = None
        try:
            connection.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
            peer = _credentials(connection)
            if peer[0] <= 0 or (peer[1], peer[2]) not in self._policies:
                raise IPCError('ipc_peer_denied')
            request = _receive(connection, REQUEST_LIMIT, peer, _boottime() + TRANSPORT_SECONDS)
            candidate = request.get('request_id')
            if type(candidate) is str and re.fullmatch(r'[0-9a-f]{32}', candidate):
                request_id = candidate
            reply = self._dispatch(request, peer)
        except IPCError as error:
            reply = {'version': 1, 'request_id': request_id,
                'error': {'code': error.code, 'outcome': error.outcome}}
        try:
            _send(connection, reply, REPLY_LIMIT, _boottime() + TRANSPORT_SECONDS)
        except IPCError:
            pass

    def serve(self, stop_event):
        if not self._serving.acquire(blocking=False):
            raise IPCError('ipc_listener_in_use')
        try:
            with self._lifecycle:
                if self._closed.is_set():
                    raise IPCError('ipc_closed')
                self._active = True
            while not self._closed.is_set() and not stop_event.is_set():
                self._check_integrity()
                try:
                    listener = self._socket
                    if listener is None:
                        break
                    connection, _ = listener.accept()
                except socket.timeout:
                    continue
                except OSError:
                    if self._closed.is_set():
                        break
                    raise IPCError('ipc_unavailable') from None
                with connection:
                    if self._closed.is_set() or stop_event.is_set():
                        break
                    self._handle(connection)
        finally:
            with self._lifecycle:
                self._active = False
                self.close()
            self._serving.release()

    def close(self):
        with self._lifecycle:
            self._closed.set()
            if self._socket is not None:
                self._socket.close()
                self._socket = None
            # Retain the exclusive lifecycle lock while a dispatched call runs.
            if self._active:
                return
            if self._directory_fd is not None and self._socket_identity is not None:
                try:
                    self._check_integrity()
                    self._check_socket(self._socket_identity)
                    os.unlink(SOCKET_NAME, dir_fd=self._directory_fd)
                    os.fsync(self._directory_fd)
                    self._metadata['socket'] = None
                    self._write_metadata()
                except (IPCError, OSError):
                    pass
                self._socket_identity = None
            if self._lock_fd is not None:
                os.close(self._lock_fd)
                self._lock_fd = None
            if self._directory_fd is not None:
                os.close(self._directory_fd)
                self._directory_fd = None


class BrokerClient:
    """Credential-checked, one-shot calls with a bounded total caller wait."""
    def __init__(self, *, directory: Path, server_uid: int, server_gid: int, timeout_seconds=30):
        _platform()
        if not _id(server_uid) or not _id(server_gid) or not _finite(timeout_seconds) or not 0 < timeout_seconds <= 60:
            raise IPCError('ipc_identity_invalid')
        self.directory = Path(directory)
        self._uid = server_uid
        self._gid = server_gid
        self._timeout = timeout_seconds
        self._directory_identity = _identity(_path_info(self.directory, server_uid))

    def _call(self, operation, arguments):
        _platform()
        deadline = _boottime() + self._timeout
        dispatched = False
        authenticated_error = False
        request_id = secrets.token_hex(16)
        try:
            if _identity(_path_info(self.directory, self._uid)) != self._directory_identity:
                raise IPCError('ipc_directory_changed')
            with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as connection:
                connection.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
                remaining = min(TRANSPORT_SECONDS, deadline - _boottime())
                if remaining <= 0:
                    raise IPCError('ipc_timeout')
                connection.settimeout(remaining)
                connection.connect(str(self.directory / SOCKET_NAME))
                peer = _credentials(connection)
                if peer[0] <= 0 or peer[1:] != (self._uid, self._gid):
                    raise IPCError('ipc_peer_denied')
                if _identity(_path_info(self.directory, self._uid)) != self._directory_identity:
                    raise IPCError('ipc_directory_changed')
                request = {'version': 1, 'request_id': request_id, 'operation': operation,
                    'arguments': arguments, 'dispatch_before': min(deadline, _boottime() + DISPATCH_SECONDS)}
                _encode(request, REQUEST_LIMIT)
                # Once the send is attempted, conservatively reconcile on failure.
                dispatched = True
                _send(connection, request, REQUEST_LIMIT, deadline)
                # Total wait is bounded separately from each server transport phase.
                connection.settimeout(max(.001, deadline - _boottime()))
                reply = _receive_reply(connection, peer, deadline)
            if (set(reply) not in ({'version', 'request_id', 'observation'}, {'version', 'request_id', 'error'})
                    or type(reply['version']) is not int or reply['version'] != 1 or reply['request_id'] != request_id):
                raise IPCError('ipc_reply_invalid')
            if 'error' in reply:
                error = reply['error']
                if (type(error) is not dict or set(error) != {'code', 'outcome'}
                        or type(error['code']) is not str or error['code'] not in _IPC_CODES | _SAFE_BROKER_CODES
                        or type(error['outcome']) is not str or error['outcome'] not in {'not_dispatched', 'unconfirmed'}):
                    raise IPCError('ipc_reply_invalid')
                authenticated_error = True
                raise IPCError(error['code'], outcome=error['outcome'])
            result = _observation(reply['observation'])
            if result.resource_id != (arguments.get('resource_id') or arguments['envelope']['resource_id']):
                raise IPCError('ipc_reply_invalid')
            return result
        except IPCError as error:
            # Only a structurally valid peer-authenticated error can prove refusal.
            if dispatched and not authenticated_error:
                error.outcome = 'unconfirmed'
            raise
        except (OSError, socket.timeout):
            raise IPCError('ipc_unavailable', outcome='unconfirmed' if dispatched else 'not_dispatched') from None

    def ensure(self, assignment: Assignment) -> Observation:
        if type(assignment) is not Assignment:
            raise IPCError('ipc_protocol_invalid')
        value = _envelope(asdict(assignment), 'ensure')
        return self._call('ensure', {'envelope': asdict(value)})

    def renew(self, grant: Grant) -> Observation:
        if type(grant) is not Grant:
            raise IPCError('ipc_protocol_invalid')
        value = _envelope(asdict(grant), 'renew')
        return self._call('renew', {'envelope': asdict(value)})

    def observe(self, resource_id: str) -> Observation:
        if not _uuid(resource_id):
            raise IPCError('ipc_protocol_invalid')
        return self._call('observe', {'resource_id': resource_id})

    def revoke(self, resource_id: str, cause='cancelled') -> Observation:
        if not _uuid(resource_id) or type(cause) is not str or cause not in CAUSES:
            raise IPCError('ipc_protocol_invalid')
        return self._call('revoke', {'resource_id': resource_id, 'cause': cause})


def _receive_reply(connection, expected, deadline):
    # A dispatched broker call may outlive the one-second transport read phase.
    # Poll readiness with the remaining total client budget, then receive one
    # atomic packet through the same credential/ancillary validator.
    import select
    remaining = deadline - _boottime()
    if remaining <= 0 or not select.select([connection], [], [], remaining)[0]:
        raise IPCError('ipc_timeout', outcome='unconfirmed')
    return _receive(connection, REPLY_LIMIT, expected, deadline)
