"""Host-local, signed sealed-KVM resource lifecycle.

This Interface is a private Resource Broker envelope, not the control-plane
Assignment or model/file ExecutionGrant. No guest request crosses this Interface.
A trusted local caller supplies an administrator-owned immutable catalog; user
assignments never contain XML, host paths or QEMU arguments.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
import fcntl
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import pwd
import re
import secrets
import selectors
import copy
import stat
import subprocess
import threading
import time
from typing import Sequence
import uuid
import xml.etree.ElementTree as ET
from . import resources

CHANNEL = 'org.harniqo.m0'
NAMESPACE = 'urn:harniqo:resource-broker:1'
DISK_BYTES = 8 * 1024**3
MAX_JOURNAL = 4 * 1024**2
MAX_OUTPUT = 2 * 1024**2
VIRSH = '/usr/bin/virsh'
CONNECTION = 'qemu:///system'
LEGACY_PROFILE = 'sealed-raw8g-q35-v1'
LIMITED_PROFILE = 'sealed-raw8g-q35-limited-v2'
STOP_CAUSES = frozenset({'cancelled', 'qualification_cleanup', 'stop_intent', 'stop_retry',
    'terminal_resource_reappeared', 'lease_expired_or_clock_changed',
    'lease_expired_during_observation', 'lease_expired_during_persistence',
    'lease_expired_during_renewal', 'lease_expired_during_resource_sample'})
ENVIRONMENT = {'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LC_ALL': 'C'}
ET.register_namespace('harniqo', NAMESPACE)


class BrokerError(RuntimeError):
    """A stable, non-sensitive error code; native stderr never enters this error."""
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class Template:
    template_id: str
    resource_id: str
    name: str
    disk: Path
    seed: Path
    channel: Path
    console: Path
    source_base_sha256: str
    resource_profile: str = LEGACY_PROFILE

    @property
    def configuration_sha256(self) -> str:
        return hashlib.sha256(_json(_template_description(self))).hexdigest()


@dataclass(frozen=True)
class Assignment:
    resource_id: str
    organization_id: str
    workspace_id: str
    attempt_id: str
    template_id: str
    configuration_sha256: str
    generation: int
    recovery_generation: int
    issued_at: float
    expires_at: float
    signature: str


@dataclass(frozen=True)
class Grant(Assignment):
    pass


@dataclass(frozen=True)
class StopEvidence:
    resource_id: str
    confirmed_at: float
    method: str
    configuration_sha256: str


@dataclass(frozen=True)
class Observation:
    resource_id: str
    state: str
    generation: int
    recovery_generation: int
    stop_evidence: StopEvidence | None = None
    error_code: str | None = None
    stop_cause: str | None = None
    limits_error: dict | None = None


def _json(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


def _signature(key: bytes, purpose: str, value: dict) -> str:
    return hmac.new(key, b'harniqo-resource-broker-v1\0' + purpose.encode() + b'\0' + _json(value), hashlib.sha256).hexdigest()


def sign_assignment(key: bytes, **claims) -> Assignment:
    """Issuer utility; key remains in the trusted host issuer, never a guest."""
    if not isinstance(key, bytes) or len(key) != 32:
        raise BrokerError('issuer_key_length')
    value = Assignment(signature='', **claims)
    body = asdict(value); body.pop('signature')
    return replace(value, signature=_signature(key, 'assignment', body))


def sign_grant(key: bytes, **claims) -> Grant:
    if not isinstance(key, bytes) or len(key) != 32:
        raise BrokerError('issuer_key_length')
    value = Grant(signature='', **claims)
    body = asdict(value); body.pop('signature')
    return replace(value, signature=_signature(key, 'renewal', body))


def _uuid(value: str) -> str:
    try:
        if type(value) is not str or str(uuid.UUID(value)) != value:
            raise ValueError()
    except (ValueError, AttributeError, TypeError):
        raise BrokerError('resource_uuid_invalid') from None
    return value


def _identifier(value) -> bool:
    return type(value) is str and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}', value) is not None


def _no_links(path: Path) -> None:
    if not path.is_absolute() or '..' in path.parts:
        raise BrokerError('absolute_path_required')
    for part in (path, *path.parents):
        if part.is_symlink():
            raise BrokerError('symlink_path_rejected')


def _directory(path: Path, *, create=False, private=False) -> None:
    _no_links(path)
    if create and not path.exists():
        # Parent must already be trusted; never create/chmod an arbitrary chain.
        parent = path.parent.stat()
        if parent.st_uid != os.geteuid() or parent.st_mode & 0o022:
            raise BrokerError('directory_parent_untrusted')
        path.mkdir(mode=0o700)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
        raise BrokerError('directory_owner_invalid')
    if info.st_mode & (0o077 if private else 0o022):
        raise BrokerError('directory_permissions_invalid')


def _read_private(path: Path, *, limit: int, exact=None) -> bytes:
    _no_links(path)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o777 != 0o600 or info.st_nlink != 1:
                raise BrokerError('private_file_permissions_invalid')
            data = stream.read(limit + 1)
            if len(data) > limit or (exact is not None and len(data) != exact):
                raise BrokerError('private_file_size_invalid')
            return data
    except BrokerError:
        raise
    except OSError:
        raise BrokerError('private_file_unavailable') from None


def _fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_private(path: Path, data: bytes) -> None:
    _directory(path.parent, private=True)
    _no_links(path)
    temporary = path.with_name('.' + path.name + '-' + secrets.token_hex(12))
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _owner_policy(role: str) -> dict:
    try:
        qemu_uid = pwd.getpwnam('libvirt-qemu').pw_uid
    except KeyError:
        raise BrokerError('qemu_account_missing') from None
    if type(qemu_uid) is not int or qemu_uid <= 0:
        raise BrokerError('qemu_account_invalid')
    broker_uid = os.geteuid()
    return {'broker_uid': broker_uid, 'qemu_uid': qemu_uid,
            'permitted_owner_uids': sorted({broker_uid, qemu_uid} if role in {'disk', 'seed'} else {broker_uid, qemu_uid, 0}),
            'named_access': {str(qemu_uid): 'r--' if role == 'seed' else 'rw-',
                             **({str(broker_uid): 'r--'} if role == 'seed' else {})}}


def _check_acl(path: Path, role: str, policy: dict, *, require_seed_access=True) -> None:
    acl = _invoke_local(['/usr/bin/getfacl', '--absolute-names', '--numeric', '--omit-header', str(path)])
    named = {}; mask = None
    for line in acl.splitlines():
        line = line.split('#', 1)[0].strip()
        if not line:
            continue
        parts = line.split(':')
        if len(parts) != 3:
            error = BrokerError('template_acl_invalid')
            error.configuration_error = {'failed_fields': ['acl_record_format']}
            raise error
        kind, identifier, rights = parts
        if kind == 'user' and not identifier:
            continue
        if kind == 'mask' and not identifier:
            mask = rights; continue
        if kind == 'user' and policy['named_access'].get(identifier) == rights:
            named[identifier] = rights; continue
        if kind in {'group', 'other'} and not identifier and rights == '---':
            continue
        error = BrokerError('template_acl_principal_rejected')
        field = {'user': 'acl_named_user', 'group': 'acl_group', 'other': 'acl_other', 'mask': 'acl_mask'}.get(kind, 'acl_record')
        error.configuration_error = {'failed_fields': [field]}
        raise error
    if role == 'seed' and require_seed_access:
        if named.get(str(policy['broker_uid'])) != 'r--' or mask is None or mask[0] != 'r':
            error = BrokerError('seed_broker_read_acl_required')
            error.configuration_error = {'failed_fields': ['acl_broker_named_read' if named.get(str(policy['broker_uid'])) != 'r--' else 'acl_read_mask']}
            raise error


def _file_identity(path: Path, role: str, *, require_seed_access=True) -> dict:
    _no_links(path)
    _directory(path.parent)
    info = path.lstat(); policy = _owner_policy(role)
    failed_fields = []
    if info.st_uid not in policy['permitted_owner_uids']: failed_fields.append('owner_uid')
    if info.st_nlink != 1: failed_fields.append('link_count')
    if info.st_mode & 0o007: failed_fields.append('other_permissions')
    if failed_fields:
        error = BrokerError('template_file_permissions_invalid')
        error.configuration_error = {'role': role, 'failed_fields': failed_fields}
        raise error
    if role in {'channel', 'console'}:
        if not stat.S_ISSOCK(info.st_mode):
            raise BrokerError('template_socket_required')
    elif not stat.S_ISREG(info.st_mode):
        raise BrokerError('template_regular_file_required')
    if role == 'seed' or info.st_mode & 0o070:
        _check_acl(path, role, policy, require_seed_access=require_seed_access)
    result = {'path': str(path), 'device': info.st_dev, 'inode': info.st_ino, 'role': role, 'ownership_policy': policy,
              'parents': [{'path': str(parent), 'device': parent.stat().st_dev, 'inode': parent.stat().st_ino}
                          for parent in path.parents]}
    if role == 'disk':
        if info.st_size != DISK_BYTES:
            raise BrokerError('raw_disk_size_invalid')
        result['bytes'] = info.st_size
    if role == 'seed':
        if not 512 <= info.st_size <= 16 * 1024**2:
            raise BrokerError('seed_size_invalid')
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        with os.fdopen(fd, 'rb') as stream:
            if (os.fstat(stream.fileno()).st_dev, os.fstat(stream.fileno()).st_ino) != (info.st_dev, info.st_ino):
                raise BrokerError('template_identity_changed')
            digest = hashlib.sha256()
            for chunk in iter(lambda: stream.read(65536), b''):
                digest.update(chunk)
            result['sha256'] = digest.hexdigest()
        result['bytes'] = info.st_size
    return result


def prepare_seed_access(seed: Path, *, images_root: Path) -> None:
    """Explicit catalog preparation, before libvirt can change seed ownership.

    Only adds the broker's named read entry to its own approved seed. No sudo,
    chown, group/other grants, permission repair or changes to an admitted group.
    """
    seed = Path(seed); images_root = Path(images_root)
    _directory(images_root); _no_links(seed)
    if not seed.is_relative_to(images_root) or seed == images_root:
        raise BrokerError('catalog_path_outside_root')
    for directory in seed.parents:
        _directory(directory)
        if directory == images_root:
            break
    before_info = seed.lstat()
    if before_info.st_uid != os.geteuid():
        raise BrokerError('seed_preparation_owner_invalid')
    before = _file_identity(seed, 'seed', require_seed_access=False)
    _invoke_local(['/usr/bin/setfacl', '-m', 'u:' + str(os.geteuid()) + ':r--', '--', str(seed)])
    after = _file_identity(seed, 'seed')
    if before != after or seed.lstat().st_uid != os.geteuid():
        raise BrokerError('seed_preparation_identity_changed')


def _template_description(template: Template) -> dict:
    if type(template) is not Template or not _identifier(template.template_id):
        raise BrokerError('template_invalid')
    _uuid(template.resource_id)
    if not _identifier(template.name) or len(template.name) > 80 or re.fullmatch(r'[0-9a-f]{64}', template.source_base_sha256) is None:
        raise BrokerError('template_invalid')
    if type(template.resource_profile) is not str or template.resource_profile not in {LEGACY_PROFILE, LIMITED_PROFILE}:
        raise BrokerError('resource_profile_invalid')
    files = []
    for role in ('disk', 'seed', 'channel', 'console'):
        try:
            files.append(_file_identity(getattr(template, role), role))
        except BrokerError as error:
            diagnostic = copy.deepcopy(getattr(error, 'configuration_error', {}))
            diagnostic['role'] = role
            diagnostic.setdefault('failed_fields', ['parent_directory'] if error.code.startswith('directory_') else ['metadata_validation'])
            error.configuration_error = diagnostic
            raise
    description = {'version': 1, 'profile': template.resource_profile, 'template_id': template.template_id,
            'resource_id': template.resource_id, 'name': template.name,
            'source_base_sha256': template.source_base_sha256,
            'files': files,
            'memory_mib': 1024, 'vcpus': 1, 'channel_name': CHANNEL, 'network': 'none',
            'disk_driver': 'raw', 'seed_driver': 'raw-readonly'}
    if template.resource_profile == LIMITED_PROFILE:
        description['host_limits'] = {'cpu_period_usec': resources.CPU_PERIOD,
            'cpu_quota_usec': resources.CPU_QUOTA, 'memory_max_bytes': resources.MEMORY_BYTES,
            'swap_max_bytes': resources.SWAP_BYTES, 'mechanism': 'libvirt-cgroup-v2'}
    return description


def _read_resource_limits(template: Template) -> dict:
    try:
        return resources.read_limits(name=template.name, resource_id=template.resource_id)
    except resources.LimitReadError as error:
        code = error.code if error.code in resources.ERROR_CODES else 'resource_limits_observation_unavailable'
        wrapped = BrokerError(code)
        wrapped.limits_error = {'code': code, 'phase': error.phase if error.phase in resources.ERROR_PHASES else 'unknown'}
        raise wrapped from None


def _invoke_local(args: list[str], timeout=10) -> str:
    """Bound stdout, stderr and wall time; never expose native error text."""
    process = None
    selector = selectors.DefaultSelector()
    output = bytearray(); error = bytearray()
    try:
        process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, env=ENVIRONMENT, close_fds=True)
        selector.register(process.stdout, selectors.EVENT_READ, output)
        selector.register(process.stderr, selectors.EVENT_READ, error)
        until = time.monotonic() + timeout
        while selector.get_map():
            remaining = until - time.monotonic()
            if remaining <= 0:
                raise BrokerError('libvirt_call_unconfirmed')
            for key, _ in selector.select(min(remaining, .2)):
                chunk = os.read(key.fileobj.fileno(), 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                else:
                    key.data.extend(chunk)
                    if len(output) + len(error) > MAX_OUTPUT:
                        raise BrokerError('native_output_limit')
        if process.wait(timeout=max(.01, until - time.monotonic())) != 0:
            raise BrokerError('libvirt_operation_failed')
        try:
            return output.decode('utf-8')
        except UnicodeError:
            raise BrokerError('libvirt_output_invalid') from None
    except (OSError, subprocess.TimeoutExpired):
        raise BrokerError('libvirt_call_unconfirmed') from None
    finally:
        selector.close()
        if process is not None:
            if process.poll() is None:
                process.kill(); process.wait()
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    stream.close()


def _virsh(*args: str) -> str:
    return _invoke_local([VIRSH, '--connect', CONNECTION, *args])


def _clock() -> dict:
    try:
        boot_id = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        _uuid(boot_id)
        return {'wall': time.time(), 'monotonic': time.clock_gettime(time.CLOCK_BOOTTIME), 'boot_id': boot_id}
    except (OSError, AttributeError):
        raise BrokerError('host_clock_unavailable') from None


class ResourceBroker:
    def __init__(self, *, state_dir: Path, templates: Sequence[Template], issuer_key_file: Path,
                 images_root: Path, channel_root: Path, max_lease_seconds=120,
                 max_clock_skew_seconds=5):
        self._state = Path(state_dir); self._images = Path(images_root); self._channels = Path(channel_root)
        for directory in (self._images, self._channels):
            _directory(directory)
        _directory(self._state, create=True, private=True)
        _directory(Path(issuer_key_file).parent, private=True)
        self._key = _read_private(Path(issuer_key_file), limit=32, exact=32)
        if type(max_lease_seconds) not in (int, float) or not math.isfinite(max_lease_seconds) or not 1 <= max_lease_seconds <= 600:
            raise BrokerError('lease_policy_invalid')
        if type(max_clock_skew_seconds) not in (int, float) or not math.isfinite(max_clock_skew_seconds) or not 0 <= max_clock_skew_seconds <= 30:
            raise BrokerError('clock_policy_invalid')
        self._maximum = max_lease_seconds; self._skew = max_clock_skew_seconds
        self._last_configuration_error = None
        self._last_limits_error = None
        self._templates = {}; self._descriptions = {}; aliases = set(); inodes = set(); resource_ids = set()
        if not 1 <= len(templates) <= 64:
            raise BrokerError('catalog_size_invalid')
        for catalog_index, template in enumerate(templates):
            try:
                description = _template_description(template)
            except BrokerError as cause:
                raise self._configuration_fault(None, 'catalog_initialization', cause=cause, code=cause.code, index=catalog_index) from cause
            if template.template_id in self._templates or template.resource_id in resource_ids:
                raise BrokerError('catalog_duplicate_identity')
            resource_ids.add(template.resource_id)
            for entry in description['files']:
                path = Path(entry['path']); root = self._images if entry['role'] in {'disk', 'seed'} else self._channels
                if path == root or not path.is_relative_to(root):
                    raise BrokerError('catalog_path_outside_root')
                for directory in path.parents:
                    if directory == root:
                        break
                    _directory(directory)
                identity = entry['device'], entry['inode']
                if entry['path'] in aliases or identity in inodes:
                    raise BrokerError('catalog_file_alias')
                aliases.add(entry['path']); inodes.add(identity)
            self._templates[template.template_id] = template; self._descriptions[template.template_id] = description
        self._catalog_hash = hashlib.sha256(_json(self._descriptions)).hexdigest()
        self._trusted = {}; self._anchor = None; self.last_error = None
        self._thread_lock = threading.RLock()
        self._lock_path = self._state / 'broker.lock'
        created = False
        try:
            fd = os.open(self._lock_path, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
            created = True
        except FileExistsError:
            fd = os.open(self._lock_path, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC)
        info = os.fstat(fd); self._lock_identity = (info.st_dev, info.st_ino)
        os.close(fd)
        with self._locked():
            marker = self._state / 'anchor.json'; journal = self._state / 'journal.json'
            if not marker.exists():
                if not created or set(p.name for p in self._state.iterdir()) != {'broker.lock'}:
                    raise BrokerError('state_anchor_missing')
                self._anchor = secrets.token_hex(32)
                anchor = {'anchor_id': self._anchor, 'catalog_sha256': self._catalog_hash,
                          'lock_device': self._lock_identity[0], 'lock_inode': self._lock_identity[1]}
                _atomic_private(marker, _json({'body': anchor, 'mac': _signature(self._key, 'anchor', anchor)}))
                self._save({'version': 1, 'anchor_id': self._anchor, 'catalog_sha256': self._catalog_hash,
                            'records': {}, 'workspace_epochs': {}})
            self._load()

    @property
    def last_configuration_error(self) -> dict | None:
        """Bounded role/field diagnostics; never filesystem paths or values."""
        return copy.deepcopy(self._last_configuration_error)

    @property
    def last_limits_error(self) -> dict | None:
        """Only enumerated code/phase diagnostics, without native values."""
        return copy.deepcopy(self._last_limits_error)

    def _remember_limits_error(self, record, error):
        detail = getattr(error, 'limits_error', {})
        code = error.code if error.code in resources.ERROR_CODES | {'resource_limits_xml_mismatch'} else 'resource_limits_observation_unavailable'
        phase = 'domain_xml' if code == 'resource_limits_xml_mismatch' else detail.get('phase', 'unknown')
        if phase not in resources.ERROR_PHASES | {'domain_xml'}:
            phase = 'unknown'
        diagnostic = {'code': code, 'phase': phase}
        self._last_limits_error = copy.deepcopy(diagnostic)
        record['limits_error'] = diagnostic

    def _configuration_fault(self, template_id, phase: str, *, cause=None, current=None,
                             code='state_catalog_changed', index=None) -> BrokerError:
        if index is None:
            index = list(self._descriptions).index(template_id)
        diagnostic = {'phase': phase, 'catalog_index': index}
        if cause is not None:
            cause_code = cause.code if re.fullmatch(r'[a-z0-9_]{1,80}', cause.code) else 'configuration_check_failed'
            diagnostic['cause_code'] = cause_code
            detail = getattr(cause, 'configuration_error', {})
            diagnostic['role'] = detail.get('role', 'template')
            diagnostic['failed_fields'] = list(detail.get('failed_fields', ['metadata_validation']))[:8]
        else:
            diagnostic['cause_code'] = 'descriptor_changed'
            expected = self._descriptions[template_id]; changed = []
            for field in ('version', 'profile', 'template_id', 'resource_id', 'name', 'source_base_sha256',
                          'memory_mib', 'vcpus', 'channel_name', 'network', 'disk_driver', 'seed_driver'):
                if expected.get(field) != current.get(field):
                    changed.append({'role': 'template', 'field': field})
            roles = ('disk', 'seed', 'channel', 'console')
            before_files = expected.get('files', []); after_files = current.get('files', [])
            if len(before_files) != len(after_files):
                changed.append({'role': 'template', 'field': 'file_count'})
            for position, role in enumerate(roles):
                if position >= len(before_files) or position >= len(after_files):
                    continue
                for field in ('path', 'device', 'inode', 'bytes', 'sha256', 'parents', 'ownership_policy'):
                    if before_files[position].get(field) != after_files[position].get(field):
                        changed.append({'role': role, 'field': field})
            diagnostic['changed_fields'] = changed[:32]
        self._last_configuration_error = copy.deepcopy(diagnostic)
        error = BrokerError(code)
        error.configuration_error = copy.deepcopy(diagnostic)
        return error

    @contextmanager
    def _locked(self):
        with self._thread_lock:
            fd = None
            try:
                _directory(self._state, private=True)
                _no_links(self._lock_path)
                fd = os.open(self._lock_path, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC)
                info = os.fstat(fd)
                if (info.st_dev, info.st_ino) != self._lock_identity or info.st_uid != os.geteuid() or info.st_mode & 0o777 != 0o600 or info.st_nlink != 1:
                    raise BrokerError('state_lock_invalid')
                fcntl.flock(fd, fcntl.LOCK_EX)
                current = self._lock_path.lstat()
                if (current.st_dev, current.st_ino) != self._lock_identity:
                    raise BrokerError('state_lock_replaced')
                yield
            except (OSError, ValueError, KeyError, TypeError):
                self._fail_closed('state_unavailable')
                raise BrokerError('state_unavailable') from None
            except BrokerError as error:
                if error.code.startswith(('state_', 'journal_', 'private_', 'directory_', 'host_clock_')):
                    self._fail_closed(error.code)
                raise
            finally:
                if fd is not None:
                    os.close(fd)

    def _load(self) -> dict:
        try:
            envelope = json.loads(_read_private(self._state / 'anchor.json', limit=4096))
            anchor = envelope['body']
            if set(envelope) != {'body', 'mac'} or set(anchor) != {'anchor_id', 'catalog_sha256', 'lock_device', 'lock_inode'} or not hmac.compare_digest(envelope['mac'], _signature(self._key, 'anchor', anchor)):
                raise BrokerError('state_anchor_invalid')
            previous_anchor = self._anchor
            self._anchor = anchor['anchor_id']
            if (type(anchor['lock_device']) is not int or type(anchor['lock_inode']) is not int or
                    (anchor['lock_device'], anchor['lock_inode']) != self._lock_identity):
                raise BrokerError('state_lock_replaced')
            if anchor['catalog_sha256'] != self._catalog_hash or (previous_anchor is not None and previous_anchor != anchor['anchor_id']):
                fields = []
                if anchor['catalog_sha256'] != self._catalog_hash: fields.append('catalog_sha256')
                if previous_anchor is not None and previous_anchor != anchor['anchor_id']: fields.append('anchor_id')
                self._last_configuration_error = {'phase': 'journal_load', 'role': 'catalog',
                    'cause_code': 'anchor_binding_mismatch', 'failed_fields': fields}
                error = BrokerError('state_catalog_changed')
                error.configuration_error = self.last_configuration_error
                raise error
            if re.fullmatch(r'[0-9a-f]{64}', anchor['anchor_id']) is None:
                raise BrokerError('state_anchor_invalid')
            for template_id, template in self._templates.items():
                try:
                    current = _template_description(template)
                except BrokerError as cause:
                    raise self._configuration_fault(template_id, 'journal_load', cause=cause) from cause
                if current != self._descriptions[template_id]:
                    raise self._configuration_fault(template_id, 'journal_load', current=current)
            envelope = json.loads(_read_private(self._state / 'journal.json', limit=MAX_JOURNAL))
            body = envelope['body']
            if set(envelope) != {'body', 'mac'} or not hmac.compare_digest(envelope['mac'], _signature(self._key, 'journal', body)):
                raise BrokerError('journal_integrity_invalid')
            if set(body) != {'version', 'anchor_id', 'catalog_sha256', 'records', 'workspace_epochs'} or body['version'] != 1 or body['anchor_id'] != self._anchor or body['catalog_sha256'] != self._catalog_hash:
                raise BrokerError('journal_schema_invalid')
            if type(body['records']) is not dict or type(body['workspace_epochs']) is not dict:
                raise BrokerError('journal_schema_invalid')
            for resource_id, record in body['records'].items():
                _uuid(resource_id)
                if resource_id != record['claims']['resource_id'] or record['state'] not in {'running', 'reconciling', 'stopped'}:
                    raise BrokerError('journal_record_invalid')
                if record['claims']['template_id'] not in self._templates:
                    raise BrokerError('journal_template_missing')
            self._trusted = copy.deepcopy(body['records'])
            return body
        except BrokerError:
            raise
        except (ValueError, KeyError, TypeError, UnicodeError):
            raise BrokerError('journal_corrupt') from None

    def _save(self, body: dict) -> None:
        encoded = _json({'body': body, 'mac': _signature(self._key, 'journal', body)})
        if len(encoded) > MAX_JOURNAL:
            raise BrokerError('journal_capacity_exceeded')
        try:
            _atomic_private(self._state / 'journal.json', encoded)
        except (OSError, BrokerError):
            raise BrokerError('journal_persistence_failed') from None
        self._trusted = copy.deepcopy(body['records'])

    def _verify(self, value: Assignment, purpose: str, sample: dict) -> dict:
        expected = Assignment if purpose == 'assignment' else Grant
        if type(value) is not expected:
            raise BrokerError('grant_type_invalid')
        # Reject hostile scalar values before JSON/HMAC. A bad request must not
        # be mistaken for journal corruption and stop another valid workspace.
        _uuid(value.resource_id)
        if any(not _identifier(getattr(value, name)) for name in ('organization_id', 'workspace_id', 'attempt_id', 'template_id')):
            raise BrokerError('grant_identity_invalid')
        if type(value.configuration_sha256) is not str or re.fullmatch(r'[0-9a-f]{64}', value.configuration_sha256) is None:
            raise BrokerError('configuration_digest_invalid')
        for name in ('generation', 'recovery_generation'):
            if type(getattr(value, name)) is not int or not 1 <= getattr(value, name) <= 9007199254740991:
                raise BrokerError('generation_invalid')
        for name in ('issued_at', 'expires_at'):
            if type(getattr(value, name)) not in (int, float) or not math.isfinite(getattr(value, name)):
                raise BrokerError('deadline_invalid')
        body = asdict(value); signature = body.pop('signature')
        if type(signature) is not str or re.fullmatch(r'[0-9a-f]{64}', signature) is None or not hmac.compare_digest(signature, _signature(self._key, purpose, body)):
            raise BrokerError('signature_invalid')
        if value.issued_at > sample['wall'] + self._skew:
            raise BrokerError('issued_in_future')
        if value.expires_at <= sample['wall'] or value.expires_at <= value.issued_at:
            raise BrokerError('lease_expired')
        if value.expires_at - value.issued_at > self._maximum or value.expires_at - sample['wall'] > self._maximum:
            raise BrokerError('lease_too_long')
        template = self._templates.get(value.template_id)
        if template is None or template.resource_id != value.resource_id:
            raise BrokerError('template_binding_invalid')
        try:
            current = _template_description(template)
        except BrokerError as cause:
            raise self._configuration_fault(value.template_id, 'grant_verification', cause=cause, code=cause.code) from cause
        digest = hashlib.sha256(_json(current)).hexdigest()
        if current != self._descriptions[value.template_id]:
            raise self._configuration_fault(value.template_id, 'grant_verification', current=current, code='configuration_changed')
        if value.configuration_sha256 != digest:
            raise BrokerError('configuration_changed')
        return body

    def _workspace(self, claims: dict) -> str:
        return json.dumps([claims['organization_id'], claims['workspace_id']], separators=(',', ':'))

    def _lease_valid(self, record: dict, sample: dict) -> bool:
        clock = record['clock']; previous = record['last_clock']
        if sample['boot_id'] != clock['boot_id'] or sample['monotonic'] < previous['monotonic']:
            return False
        wall_delta = sample['wall'] - previous['wall']; mono_delta = sample['monotonic'] - previous['monotonic']
        if abs(wall_delta - mono_delta) > self._skew:
            return False
        return sample['wall'] < record['claims']['expires_at'] and sample['monotonic'] < record['monotonic_deadline']

    def _observation(self, record: dict) -> Observation:
        evidence = record.get('stop_evidence')
        return Observation(record['claims']['resource_id'], record['state'], record['claims']['generation'],
                           record['claims']['recovery_generation'], StopEvidence(**evidence) if evidence else None,
                           record.get('error_code'), record.get('stop_cause'), copy.deepcopy(record.get('limits_error')))

    def _metadata(self, record: dict) -> dict:
        fields = {'anchor_id': self._anchor, 'owner_token': record['owner_token'],
                  'launch_claims': record['launch_claims']}
        return {'claims': _json(fields).decode(), 'signature': _signature(self._key, 'ownership', fields)}

    def _xml(self, record: dict) -> str:
        template = self._templates[record['claims']['template_id']]
        domain = ET.Element('domain', type='kvm')
        ET.SubElement(domain, 'name').text = template.name; ET.SubElement(domain, 'uuid').text = template.resource_id
        metadata = ET.SubElement(domain, 'metadata')
        ET.SubElement(metadata, '{' + NAMESPACE + '}ownership', **self._metadata(record))
        ET.SubElement(domain, 'memory', unit='MiB').text = '1024'
        ET.SubElement(domain, 'currentMemory', unit='MiB').text = '1024'
        ET.SubElement(domain, 'vcpu', placement='static').text = '1'
        if template.resource_profile == LIMITED_PROFILE:
            cpu = ET.SubElement(domain, 'cputune')
            ET.SubElement(cpu, 'global_period').text = str(resources.CPU_PERIOD)
            ET.SubElement(cpu, 'global_quota').text = str(resources.CPU_QUOTA)
            memory = ET.SubElement(domain, 'memtune')
            ET.SubElement(memory, 'hard_limit', unit='KiB').text = str(resources.MEMORY_BYTES // 1024)
            # On the fixed libvirt-10/cgroup-v2 backend this maps directly to
            # memory.swap.max: an additional finite 2 GiB, not zero swap.
            ET.SubElement(memory, 'swap_hard_limit', unit='KiB').text = str(resources.SWAP_BYTES // 1024)
        operating = ET.SubElement(domain, 'os'); ET.SubElement(operating, 'type', arch='x86_64', machine='q35').text = 'hvm'
        ET.SubElement(operating, 'boot', dev='hd')
        features = ET.SubElement(domain, 'features'); ET.SubElement(features, 'acpi'); ET.SubElement(features, 'apic')
        ET.SubElement(domain, 'cpu', mode='host-model')
        for event, action in [('on_poweroff', 'destroy'), ('on_reboot', 'restart'), ('on_crash', 'destroy')]:
            ET.SubElement(domain, event).text = action
        devices = ET.SubElement(domain, 'devices'); ET.SubElement(devices, 'emulator').text = '/usr/bin/qemu-system-x86_64'
        for device, filename, target, bus in [('disk', template.disk, 'vda', 'virtio'), ('cdrom', template.seed, 'sda', 'sata')]:
            node = ET.SubElement(devices, 'disk', type='file', device=device)
            ET.SubElement(node, 'driver', name='qemu', type='raw'); ET.SubElement(node, 'source', file=str(filename))
            ET.SubElement(node, 'target', dev=target, bus=bus)
            if device == 'cdrom': ET.SubElement(node, 'readonly')
        ET.SubElement(devices, 'controller', type='sata', index='0')
        ET.SubElement(devices, 'controller', type='virtio-serial', index='0')
        channel = ET.SubElement(devices, 'channel', type='unix')
        ET.SubElement(channel, 'source', mode='connect', path=str(template.channel)); ET.SubElement(channel, 'target', type='virtio', name=CHANNEL)
        serial = ET.SubElement(devices, 'serial', type='unix')
        ET.SubElement(serial, 'source', mode='connect', path=str(template.console)); ET.SubElement(serial, 'target', port='0')
        ET.SubElement(ET.SubElement(devices, 'video'), 'model', type='none')
        ET.SubElement(devices, 'memballoon', model='none')
        return ET.tostring(domain, encoding='unicode')

    def _ids(self) -> set[str]:
        values = _virsh('list', '--all', '--uuid').splitlines()
        return {_uuid(value.strip()) for value in values if value.strip()}

    def _match(self, record: dict, xml: str, *, check_limits=True) -> None:
        try:
            tree = ET.fromstring(xml); claims = record['claims']; template = self._templates[claims['template_id']]
            node = tree.find('./metadata/{' + NAMESPACE + '}ownership')
            if tree.get('type') != 'kvm' or tree.findtext('uuid') != template.resource_id or tree.findtext('name') != template.name or node is None or dict(node.attrib) != self._metadata(record):
                raise BrokerError('resource_ownership_mismatch')
            if tree.findall('./devices/interface') or tree.findall('./devices/filesystem') or tree.findall('./devices/hostdev') or any(n.tag.endswith('commandline') for n in tree):
                raise BrokerError('resource_topology_mismatch')
            disks = tree.findall('./devices/disk')
            sources = {(d.get('device'), d.find('source').get('file'), d.find('driver').get('type')) for d in disks}
            if len(disks) != 2 or sources != {('disk', str(template.disk), 'raw'), ('cdrom', str(template.seed), 'raw')}:
                raise BrokerError('resource_topology_mismatch')
            channels = tree.findall('./devices/channel'); serials = tree.findall('./devices/serial')
            if len(channels) != 1 or len(serials) != 1:
                raise BrokerError('resource_topology_mismatch')
            for nodes, path in [(channels, template.channel), (serials, template.console)]:
                source = nodes[0].find('source')
                if source.get('path') != str(path) or source.get('mode') != 'connect':
                    raise BrokerError('resource_topology_mismatch')
            if channels[0].find('target').get('name') != CHANNEL or tree.findtext('vcpu') != '1':
                raise BrokerError('resource_topology_mismatch')
            if check_limits and template.resource_profile == LIMITED_PROFILE:
                if (tree.findtext('./cputune/global_period') != str(resources.CPU_PERIOD)
                        or tree.findtext('./cputune/global_quota') != str(resources.CPU_QUOTA)):
                    raise BrokerError('resource_limits_xml_mismatch')
                for field, expected in [('hard_limit', resources.MEMORY_BYTES), ('swap_hard_limit', resources.SWAP_BYTES)]:
                    element = tree.find('./memtune/' + field)
                    if element is None or element.get('unit') != 'KiB' or element.text != str(expected // 1024):
                        raise BrokerError('resource_limits_xml_mismatch')
        except (ET.ParseError, AttributeError, TypeError, KeyError):
            raise BrokerError('resource_xml_invalid') from None

    def _status(self, record: dict, *, check_profile=True, check_limits=True) -> str:
        if check_profile:
            template_id = record['claims']['template_id']
            try:
                current = _template_description(self._templates[template_id])
            except BrokerError as cause:
                raise self._configuration_fault(template_id, 'host_observation', cause=cause) from cause
            if current != self._descriptions[template_id]:
                raise self._configuration_fault(template_id, 'host_observation', current=current)
        identity = record['claims']['resource_id']
        if identity not in self._ids():
            return 'absent'
        try:
            self._match(record, _virsh('dumpxml', identity), check_limits=check_limits)
            template = self._templates[record['claims']['template_id']]
            if check_limits and template.resource_profile == LIMITED_PROFILE:
                _read_resource_limits(template)
        except BrokerError as error:
            if error.code.startswith('resource_limits_'):
                self._remember_limits_error(record, error)
            raise
        return 'present'

    def _stopped(self, body: dict, record: dict, sample: dict, method: str) -> Observation:
        sample = _clock()  # Sample after the host absence observation, not before RPC.
        record.setdefault('stop_cause', 'host_absence_after_confirmed_dispatch')
        record.update(state='stopped', error_code=None, stop_evidence=asdict(StopEvidence(
            record['claims']['resource_id'], sample['wall'], method, record['claims']['configuration_sha256'])))
        self._save(body)
        return self._observation(record)

    def _stop(self, body: dict, record: dict, sample: dict, cause: str) -> Observation:
        record['state'] = 'reconciling'; record['error_code'] = cause; record['stop_evidence'] = None
        record['stop_intent'] = True  # Durable and irreversible for this execution generation.
        safe_causes = STOP_CAUSES | resources.ERROR_CODES | {'resource_limits_xml_mismatch'}
        record.setdefault('stop_cause', cause if cause in safe_causes else 'requested_revoke')
        self._save(body)
        try:
            status = self._status(record, check_limits=False)
            if status == 'absent':
                if record['dispatch'] not in {'confirmed', 'not_sent'} or record.get('stop_dispatch') == 'pending':
                    raise BrokerError('dispatch_unconfirmed')
                method = 'libvirt_absent_after_confirmed_start' if record['dispatch'] == 'confirmed' else 'libvirt_absent_without_dispatch'
                return self._stopped(body, record, sample, method)
            record['dispatch'] = 'confirmed'; record['stop_dispatch'] = 'pending'; self._save(body)
            _virsh('destroy', record['claims']['resource_id'])
            record['stop_dispatch'] = 'acknowledged'; self._save(body)
            if self._status(record, check_limits=False) != 'absent':
                raise BrokerError('stop_unconfirmed')
            return self._stopped(body, record, sample, 'libvirt_destroy_then_absent')
        except BrokerError as error:
            if error.code.startswith(('journal_', 'state_', 'private_')):
                raise
            record['state'] = 'reconciling'; record['error_code'] = error.code; self._save(body)
            return self._observation(record)

    def _inspect(self, body: dict, record: dict, sample: dict) -> Observation:
        sample = _clock()  # A previous resource may have spent seconds in libvirt.
        if record['state'] == 'stopped':
            # A terminal resource unexpectedly reappearing is never silently
            # reused or reported stopped; it requires ownership-checked stop.
            try:
                if self._status(record, check_limits=False) == 'present':
                    return self._stop(body, record, sample, 'terminal_resource_reappeared')
            except BrokerError as error:
                if error.code.startswith(('state_', 'journal_', 'private_')):
                    raise
                record.update(state='reconciling', error_code=error.code, stop_evidence=None); self._save(body)
            return self._observation(record)
        if record.get('stop_intent'):
            return self._stop(body, record, sample, 'stop_intent')
        if not self._lease_valid(record, sample):
            return self._stop(body, record, sample, 'lease_expired_or_clock_changed')
        try:
            status = self._status(record)
            if status == 'absent':
                if record['dispatch'] in {'confirmed', 'not_sent'} and record.get('stop_dispatch') != 'pending':
                    method = 'libvirt_absent_after_confirmed_start' if record['dispatch'] == 'confirmed' else 'libvirt_absent_without_dispatch'
                    return self._stopped(body, record, sample, method)
                raise BrokerError('dispatch_unconfirmed')
            if record.get('stop_dispatch') == 'pending':
                return self._stop(body, record, sample, 'stop_retry')
            sample = _clock()
            if not self._lease_valid(record, sample):
                return self._stop(body, record, sample, 'lease_expired_during_observation')
            record.update(state='running', dispatch='confirmed', error_code=None, last_clock=sample)
        except BrokerError as error:
            if error.code.startswith(('state_', 'journal_', 'private_')):
                raise
            if error.code.startswith('resource_limits_'):
                return self._stop(body, record, _clock(), error.code)
            record.update(state='reconciling', error_code=error.code)
        self._save(body)
        if record['state'] == 'running' and not self._lease_valid(record, _clock()):
            return self._stop(body, record, _clock(), 'lease_expired_during_persistence')
        return self._observation(record)

    def ensure(self, assignment: Assignment) -> Observation:
        with self._locked():
            body = self._load(); sample = _clock(); claims = self._verify(assignment, 'assignment', sample)
            identity = claims['resource_id']; existing = body['records'].get(identity)
            digest = hashlib.sha256(_json(claims)).hexdigest()
            if existing is not None:
                old = existing['claims']
                if (old['organization_id'], old['workspace_id']) != (claims['organization_id'], claims['workspace_id']):
                    raise BrokerError('resource_scope_immutable')
                if existing['state'] != 'stopped':
                    if digest != existing['assignment_digest'] or claims['generation'] != old['generation']:
                        raise BrokerError('assignment_conflict')
                    return self._inspect(body, existing, sample)
            workspace = self._workspace(claims)
            for record in body['records'].values():
                if self._workspace(record['claims']) == workspace:
                    if record['state'] == 'stopped':
                        self._inspect(body, record, sample)
                    if record['state'] != 'stopped':
                        raise BrokerError('workspace_busy')
            epoch = (claims['recovery_generation'], claims['generation'])
            previous = body['workspace_epochs'].get(workspace)
            if previous is not None and epoch <= tuple(previous):
                raise BrokerError('generation_stale')
            if existing is not None and epoch <= (existing['claims']['recovery_generation'], existing['claims']['generation']):
                raise BrokerError('generation_stale')
            if identity in self._ids():
                raise BrokerError('resource_already_exists')
            sample = _clock(); self._verify(assignment, 'assignment', sample)
            record = {'claims': claims, 'launch_claims': dict(claims), 'owner_token': secrets.token_hex(32),
                      'assignment_digest': digest, 'state': 'reconciling', 'dispatch': 'pending',
                      'clock': sample, 'last_clock': sample,
                      'monotonic_deadline': sample['monotonic'] + claims['expires_at'] - sample['wall'],
                      'stop_evidence': None, 'error_code': 'starting', 'stop_intent': False}
            body['records'][identity] = record; body['workspace_epochs'][workspace] = list(epoch)
            # The intent and writer reservation are durable before any dispatch.
            self._save(body)
            xml_path = self._state / ('domain-' + identity + '.xml')
            dispatched = False
            try:
                _atomic_private(xml_path, self._xml(record).encode())
                self._verify(assignment, 'assignment', _clock())
                # Descriptor validation may block. No further file/ACL work is
                # allowed between this cheap fresh-clock check and dispatch.
                if not self._lease_valid(record, _clock()):
                    raise BrokerError('lease_expired_before_dispatch')
                dispatched = True
                _virsh('create', str(xml_path), '--validate')
                if self._status(record) != 'present':
                    raise BrokerError('start_not_observed')
                record.update(dispatch='confirmed', state='running', error_code=None)
            except BrokerError as error:
                if not dispatched:
                    record['dispatch'] = 'not_sent'
                record.update(state='reconciling', error_code=error.code)
                if error.code.startswith('resource_limits_'):
                    return self._stop(body, record, _clock(), error.code)
            self._save(body)
            return self._inspect(body, record, _clock())

    def renew(self, grant: Grant) -> Observation:
        with self._locked():
            body = self._load(); sample = _clock(); claims = self._verify(grant, 'renewal', sample)
            record = body['records'].get(claims['resource_id'])
            if record is None:
                raise BrokerError('resource_not_registered')
            if record['state'] == 'stopped':
                raise BrokerError('resource_terminal')
            if record.get('stop_intent'):
                self._stop(body, record, _clock(), 'stop_intent')
                raise BrokerError('resource_stopping')
            if not self._lease_valid(record, sample):
                self._stop(body, record, sample, 'lease_expired_or_clock_changed')
                raise BrokerError('lease_expired')
            old = record['claims']
            if any(claims[key] != old[key] for key in ('resource_id', 'organization_id', 'workspace_id', 'attempt_id', 'template_id', 'configuration_sha256', 'recovery_generation')):
                raise BrokerError('renewal_binding_mismatch')
            if claims['generation'] <= old['generation'] or claims['issued_at'] < old['issued_at']:
                raise BrokerError('generation_stale')
            status = self._inspect(body, record, sample)
            if status.state != 'running':
                raise BrokerError('resource_not_running')
            sample = _clock()
            if not self._lease_valid(record, sample):
                self._stop(body, record, sample, 'lease_expired_during_renewal')
                raise BrokerError('lease_expired')
            claims = self._verify(grant, 'renewal', sample)
            record['claims'] = claims; record['last_clock'] = sample; record['clock'] = sample
            record['monotonic_deadline'] = sample['monotonic'] + claims['expires_at'] - sample['wall']
            body['workspace_epochs'][self._workspace(claims)] = [claims['recovery_generation'], claims['generation']]
            self._save(body)
            return self._inspect(body, record, _clock())

    def observe(self, resource_id: str) -> Observation:
        _uuid(resource_id)
        with self._locked():
            body = self._load(); record = body['records'].get(resource_id)
            if record is None:
                raise BrokerError('resource_not_registered')
            return self._inspect(body, record, _clock())

    def resource_limits(self, resource_id: str) -> dict:
        """Read a verified fixed-profile host sample; unverifiable limits stop it.

        The returned PID/cgroup/thread identities are host-private diagnostics.
        Callers do not select PID, paths, quota values or writable controllers.
        """
        _uuid(resource_id)
        with self._locked():
            body = self._load(); record = body['records'].get(resource_id)
            if record is None:
                raise BrokerError('resource_not_registered')
            template = self._templates[record['claims']['template_id']]
            if template.resource_profile != LIMITED_PROFILE:
                raise BrokerError('resource_limits_profile_unsupported')
            if self._inspect(body, record, _clock()).state != 'running':
                raise BrokerError('resource_not_running')
            try:
                sample = _read_resource_limits(template)
            except BrokerError as error:
                self._remember_limits_error(record, error)
                self._stop(body, record, _clock(), error.code)
                raise
            if not self._lease_valid(record, _clock()):
                self._stop(body, record, _clock(), 'lease_expired_during_resource_sample')
                raise BrokerError('lease_expired')
            return sample

    def revoke(self, resource_id: str, cause: str) -> Observation:
        _uuid(resource_id)
        if not _identifier(cause):
            raise BrokerError('revoke_cause_invalid')
        with self._locked():
            body = self._load(); record = body['records'].get(resource_id)
            if record is None:
                raise BrokerError('resource_not_registered')
            if record['state'] == 'stopped':
                return self._inspect(body, record, _clock())
            return self._stop(body, record, _clock(), cause)

    def reconcile(self) -> list[Observation]:
        with self._locked():
            body = self._load()
            return [self._inspect(body, record, _clock()) for record in body['records'].values()]

    def _fail_closed(self, code: str) -> None:
        """Stop only cached, authenticated ownership; never repair bad journals.

        Existing watchers retain this trusted snapshot and retry on every cycle.
        With no verified snapshot, a fresh process may only stop domains whose
        ownership MAC binds them to its readable anchor and immutable catalog.
        """
        self.last_error = code
        records = copy.deepcopy(self._trusted)
        if not records and self._anchor:
            for template in self._templates.values():
                try:
                    if template.resource_id not in self._ids():
                        continue
                    xml = _virsh('dumpxml', template.resource_id)
                    node = ET.fromstring(xml).find('./metadata/{' + NAMESPACE + '}ownership')
                    if node is None:
                        continue
                    fields = json.loads(node.get('claims', ''))
                    if fields['anchor_id'] != self._anchor or not hmac.compare_digest(node.get('signature', ''), _signature(self._key, 'ownership', fields)):
                        continue
                    launch = fields['launch_claims']
                    if launch['resource_id'] != template.resource_id or launch['template_id'] != template.template_id:
                        continue
                    records[template.resource_id] = {'claims': launch, 'launch_claims': launch, 'owner_token': fields['owner_token']}
                except (BrokerError, ValueError, KeyError, TypeError, ET.ParseError):
                    continue
        self.emergency_observations = []
        for identity, record in records.items():
            entry = {'resource_id': identity, 'state': 'reconciling', 'error_code': code, 'host_stop_confirmed': False}
            try:
                if self._status(record, check_profile=False, check_limits=False) == 'present':
                    _virsh('destroy', identity)
                    entry['host_stop_confirmed'] = self._status(record, check_profile=False, check_limits=False) == 'absent'
                elif record.get('dispatch') == 'confirmed' and record.get('stop_dispatch') != 'pending':
                    entry['host_stop_confirmed'] = True
            except BrokerError as error:
                entry['stop_error_code'] = error.code
            self.emergency_observations.append(entry)

    def watch(self, *, interval_seconds=.25, stop_event=None) -> None:
        if type(interval_seconds) not in (int, float) or not math.isfinite(interval_seconds) or not .05 <= interval_seconds <= 5:
            raise BrokerError('watch_interval_invalid')
        while stop_event is None or not stop_event.is_set():
            try:
                self.reconcile()
            except BrokerError as error:
                # State failure has already attempted ownership-checked stop.
                # Keep retrying: pending creation may become visible later.
                self.last_error = error.code
            if stop_event is not None:
                stop_event.wait(interval_seconds)
            else:
                time.sleep(interval_seconds)
