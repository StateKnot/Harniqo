"""Read the fixed libvirt cgroup-v2 profile; never write kernel resource files.

Only the parent Broker passes an admitted name/UUID. PID, proc and cgroup paths
come from trusted host state, not caller-selected filesystem locations.
"""
from pathlib import Path
from dataclasses import dataclass
import os
import pwd
import re
import stat
import time
import errno

CPU_PERIOD = 100000
CPU_QUOTA = 100000
MEMORY_BYTES = 2 * 1024**3
SWAP_BYTES = 2 * 1024**3
LEGACY_PROFILE = 'sealed-raw8g-q35-v1'
LIMITED_PROFILE = 'sealed-raw8g-q35-limited-v2'
HOST_OOM_PROFILE = 'sealed-raw8g-q35-host-oom-v1'
PROFILES = frozenset({LEGACY_PROFILE, LIMITED_PROFILE, HOST_OOM_PROFILE})
PID_ROOT = Path('/run/libvirt/qemu')
PROC_ROOT = Path('/proc')
CGROUP_ROOT = Path('/sys/fs/cgroup')
MAX_THREADS = 4096
ERROR_PHASES = frozenset({'configuration', 'qemu_identity', 'pidfile', 'process_identity',
    'process_command', 'cgroup_location', 'threads', 'control_values', 'statistics',
    'auxiliary_statistics', 'final_threads', 'final_control_values', 'process_recheck'})
ERROR_CODES = frozenset({'resource_limits_path_untrusted', 'resource_limits_file_untrusted',
    'resource_limits_file_writable', 'resource_limits_read_unstable', 'resource_limits_value_invalid',
    'resource_limits_process_invalid', 'resource_limits_cgroup_v2_required',
    'resource_limits_cgroup_path_invalid', 'resource_limits_statistics_invalid',
    'resource_limits_statistics_missing', 'resource_limits_name_invalid',
    'resource_limits_process_binding_invalid', 'resource_limits_qemu_identity_invalid',
    'resource_limits_process_owner_invalid', 'resource_limits_domain_cgroup_invalid',
    'resource_limits_thread_count_invalid', 'resource_limits_thread_escaped',
    'resource_limits_threads_unstable', 'resource_limits_cpu_mismatch',
    'resource_limits_memory_mismatch', 'resource_limits_process_changed',
    'resource_limits_controller_changed',
    'resource_limits_observation_unavailable', 'resource_limits_profile_unsupported'})


class LimitReadError(RuntimeError):
    def __init__(self, code, phase=None):
        self.code = code
        self.phase = phase
        super().__init__(code)


@dataclass(frozen=True)
class HostLimits:
    cpu_period_usec: int
    cpu_quota_usec: int
    memory_max_bytes: int
    swap_max_bytes: int


_LIMITED_LIMITS = HostLimits(CPU_PERIOD, CPU_QUOTA, MEMORY_BYTES, SWAP_BYTES)
# Explicit fault qualification only; never selected by a default or live update.
# The fixed libvirt creation path must read back memory.swap.max=0. A live
# memtune operation using zero has different semantics and is not used here.
_HOST_OOM_LIMITS = HostLimits(CPU_PERIOD, CPU_QUOTA, 768 * 1024**2, 0)


def limits_for_profile(profile):
    """Return the immutable policy selected by an admitted finite profile name."""
    if type(profile) is not str or profile not in PROFILES:
        raise LimitReadError('resource_limits_profile_unsupported', 'configuration')
    if profile == LEGACY_PROFILE:
        return None
    return _LIMITED_LIMITS if profile == LIMITED_PROFILE else _HOST_OOM_LIMITS


def _read(path, *, bound=16384, owner=None, immutable=False):
    for item in (path, *path.parents):
        if item.is_symlink():
            raise LimitReadError('resource_limits_path_untrusted')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or (owner is not None and info.st_uid != owner):
            raise LimitReadError('resource_limits_file_untrusted')
        if immutable and info.st_mode & 0o022:
            raise LimitReadError('resource_limits_file_writable')
        value = os.read(fd, bound + 1)
        after = path.lstat()
        if len(value) > bound or (info.st_dev, info.st_ino) != (after.st_dev, after.st_ino):
            raise LimitReadError('resource_limits_read_unstable')
        return value
    finally:
        os.close(fd)


def _number(value):
    if re.fullmatch(rb'[0-9]+\n?', value) is None:
        raise LimitReadError('resource_limits_value_invalid')
    return int(value)


def _start_time(pid):
    value = _read(PROC_ROOT / str(pid) / 'stat').decode('ascii')
    # comm may contain spaces or ')' characters; the suffix follows the last ')'.
    start = value.rfind(') ')
    fields = value[start + 2:].split()
    if start < 0 or len(fields) < 20 or fields[0] in {'Z', 'X', 'x'} or not fields[19].isdigit():
        raise LimitReadError('resource_limits_process_invalid')
    return int(fields[19])


def _membership(path):
    lines = _read(path).decode('ascii').splitlines()
    values = [line[3:] for line in lines if line.startswith('0::')]
    if len(values) != 1 or len(lines) != 1:
        raise LimitReadError('resource_limits_cgroup_v2_required')
    value = Path(values[0])
    if not value.is_absolute() or '..' in value.parts or '.' in values[0].split('/'):
        raise LimitReadError('resource_limits_cgroup_path_invalid')
    return value


def _statistics(path, required):
    values = {}
    for line in _read(path).decode('ascii').splitlines():
        fields = line.split()
        if len(fields) != 2 or fields[0] in values or not fields[1].isdigit():
            raise LimitReadError('resource_limits_statistics_invalid')
        values[fields[0]] = int(fields[1])
    if not required <= set(values):
        raise LimitReadError('resource_limits_statistics_missing')
    return values


def _optional(path, parser):
    """Auxiliary evidence may be absent; limits themselves are never optional."""
    try:
        return {'state': 'observed', 'value': parser(_read(path))}
    except FileNotFoundError:
        return {'state': 'missing', 'value': None}
    except PermissionError:
        return {'state': 'permission_denied', 'value': None}
    except OSError as error:
        if error.errno in {errno.EOPNOTSUPP, errno.ENOTSUP}:
            return {'state': 'unsupported', 'value': None}
        raise


def _optional_text(path):
    return _optional(path, lambda value: value.decode('ascii').strip())


def _thread_memberships(tasks, domains):
    identities = {int(p.name) for p in tasks.iterdir() if p.name.isdigit()}
    if not identities or len(identities) > MAX_THREADS:
        raise LimitReadError('resource_limits_thread_count_invalid')
    try:
        for tid in identities:
            location = _membership(tasks / str(tid) / 'cgroup')
            if not all(location.is_relative_to(domain) for domain in domains):
                raise LimitReadError('resource_limits_thread_escaped')
    except FileNotFoundError:
        raise LimitReadError('resource_limits_threads_unstable') from None
    after = {int(p.name) for p in tasks.iterdir() if p.name.isdigit()}
    if identities != after:
        raise LimitReadError('resource_limits_threads_unstable')
    return sorted(identities)


def _control_values(scope, limits):
    """Locate whole-domain caps only at the scope or its libvirt parent group.

    Verify each controller independently across the two permitted whole-domain
    ancestors. No per-thread leaf or broader slice qualifies.
    """
    selected = {}
    for role, filename, expected in [('cpu', 'cpu.max', (limits.cpu_quota_usec, limits.cpu_period_usec)),
                                     ('memory', 'memory.max', limits.memory_max_bytes),
                                     ('swap', 'memory.swap.max', limits.swap_max_bytes)]:
        for group in (scope, scope / 'libvirt'):
            path = group / filename
            try:
                value = _read(path, owner=0, immutable=True)
            except FileNotFoundError:
                continue
            if role == 'cpu':
                fields = value.split()
                if len(fields) != 2 or not fields[1].isdigit() or (fields[0] != b'max' and not fields[0].isdigit()):
                    raise LimitReadError('resource_limits_value_invalid')
                if fields[0] == b'max' or (int(fields[0]), int(fields[1])) != expected:
                    continue
            elif value.strip() == b'max' or _number(value) != expected:
                continue
            directory, control = group.lstat(), path.lstat()
            selected[role] = {'group': group, 'identity': (directory.st_dev, directory.st_ino,
                                                          control.st_dev, control.st_ino)}
            break
        else:
            raise LimitReadError('resource_limits_cpu_mismatch' if role == 'cpu' else 'resource_limits_memory_mismatch')
    return selected


def _read_limits_sample(*, name, resource_id, profile):
    """Return a PID/start-time-bound sample, or reject unverifiable limits."""
    if type(name) is not str or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}', name):
        raise LimitReadError('resource_limits_name_invalid', 'configuration')
    if type(resource_id) is not str or not re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', resource_id):
        raise LimitReadError('resource_limits_process_binding_invalid', 'configuration')
    limits = limits_for_profile(profile)
    if limits is None:
        raise LimitReadError('resource_limits_profile_unsupported', 'configuration')
    phase = 'qemu_identity'
    try:
        qemu_uid = pwd.getpwnam('libvirt-qemu').pw_uid
        if qemu_uid <= 0:
            raise LimitReadError('resource_limits_qemu_identity_invalid')
        pidfile = PID_ROOT / (name + '.pid')
        phase = 'pidfile'
        pid = _number(_read(pidfile, bound=32, owner=0, immutable=True))
        phase = 'process_identity'
        if pid <= 1 or (PROC_ROOT / str(pid)).stat().st_uid != qemu_uid:
            raise LimitReadError('resource_limits_process_owner_invalid')
        started = _start_time(pid)
        phase = 'process_command'
        command = _read(PROC_ROOT / str(pid) / 'cmdline').split(b'\0')
        if not command or command[0] != b'/usr/bin/qemu-system-x86_64' or command.count(b'-uuid') != 1:
            raise LimitReadError('resource_limits_process_binding_invalid')
        index = command.index(b'-uuid')
        if index + 1 >= len(command) or command[index + 1] != resource_id.encode('ascii'):
            raise LimitReadError('resource_limits_process_binding_invalid')
        phase = 'cgroup_location'
        member = _membership(PROC_ROOT / str(pid) / 'cgroup')
        parts = member.parts
        # Global cgroup tuning applies to the entire domain scope. libvirt's
        # per-thread /libvirt/{emulator,vcpuN} leaves inherit those parent caps.
        if (len(parts) < 4 or parts[1] != 'machine.slice' or not parts[2].startswith('machine-qemu')
                or not parts[2].endswith('.scope') or parts[3] != 'libvirt'):
            raise LimitReadError('resource_limits_domain_cgroup_invalid')
        domain = Path(*parts[:3]); scope = CGROUP_ROOT / str(domain).lstrip('/')
        tasks = PROC_ROOT / str(pid) / 'task'
        phase = 'control_values'
        controls = _control_values(scope, limits)
        cpu_group, memory_group, swap_group = [controls[role]['group'] for role in ('cpu', 'memory', 'swap')]
        selected_domains = [Path('/') / group.relative_to(CGROUP_ROOT) for group in (cpu_group, memory_group, swap_group)]
        phase = 'threads'
        thread_ids = _thread_memberships(tasks, selected_domains)
        phase = 'statistics'
        cpu_stat = _statistics(cpu_group / 'cpu.stat', {'usage_usec', 'nr_throttled', 'throttled_usec'})
        cpu_sample_monotonic = time.monotonic()
        result = {'pid': pid, 'process_start_time': started, 'cgroup_path': str(cpu_group),
                  'memory_cgroup_path': str(memory_group), 'swap_cgroup_path': str(swap_group),
                  'thread_ids': thread_ids, 'cpu_period_usec': limits.cpu_period_usec, 'cpu_quota_usec': limits.cpu_quota_usec,
                  'memory_max_bytes': limits.memory_max_bytes, 'swap_max_bytes': limits.swap_max_bytes,
                  'cpu_stat': cpu_stat, 'cpu_stat_monotonic': cpu_sample_monotonic,
                  'memory_current_bytes': _number(_read(memory_group / 'memory.current')),
                  'memory_peak_bytes': _number(_read(memory_group / 'memory.peak')),
                  'memory_events': _statistics(memory_group / 'memory.events', {'oom', 'oom_kill'})}
        phase = 'auxiliary_statistics'
        result['memory_events_local'] = _optional_text(memory_group / 'memory.events.local')
        result['swap_current_bytes'] = _optional(swap_group / 'memory.swap.current', _number)
        result['swap_events'] = _optional_text(swap_group / 'memory.swap.events')
        result['cgroup_procs'] = _optional_text(cpu_group / 'cgroup.procs')
        result['cgroup_type'] = _optional_text(cpu_group / 'cgroup.type')
        ancestors = []
        parent = cpu_group.parent
        while parent.is_relative_to(CGROUP_ROOT):
            ancestors.append(parent)
            if parent == CGROUP_ROOT:
                break
            parent = parent.parent
        result['ancestors'] = [{'cgroup_path': str(parent),
            'cpu_max': _optional_text(parent / 'cpu.max'),
            'memory_max': _optional_text(parent / 'memory.max'),
            'swap_max': _optional_text(parent / 'memory.swap.max')}
            for parent in ancestors]
        phase = 'final_threads'
        if _thread_memberships(tasks, selected_domains) != thread_ids:
            raise LimitReadError('resource_limits_threads_unstable')
        phase = 'final_control_values'
        if _control_values(scope, limits) != controls:
            raise LimitReadError('resource_limits_controller_changed')
        phase = 'process_recheck'
        if (_start_time(pid) != started or _number(_read(pidfile, bound=32, owner=0, immutable=True)) != pid
                or _membership(PROC_ROOT / str(pid) / 'cgroup') != member):
            raise LimitReadError('resource_limits_process_changed')
        return result
    except LimitReadError as error:
        if error.phase is None:
            error.phase = phase
        raise
    except (OSError, ValueError, UnicodeError, KeyError, IndexError):
        raise LimitReadError('resource_limits_observation_unavailable', phase) from None


def read_limits(*, name, resource_id, profile=LIMITED_PROFILE):
    """Bound transient thread creation to three complete, revalidated samples."""
    for attempt in range(3):
        try:
            return _read_limits_sample(name=name, resource_id=resource_id, profile=profile)
        except LimitReadError as error:
            if error.code != 'resource_limits_threads_unstable' or attempt == 2:
                raise
