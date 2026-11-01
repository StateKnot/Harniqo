"""Bounded Host QEMU memcg-OOM evidence; kernel messages never authorize stop.

Only the private qualification operator calls this fixture. The production
Broker remains responsible for resource identity, physical stop and tombstones.
"""
from __future__ import annotations

import hashlib
import errno
import json
import os
from pathlib import Path
import pwd
import re
import select
import selectors
import signal
import stat
import subprocess
import time
import uuid

import guest_memory_pressure as protocol
import qualify_kvm as kvm

HOST_PROFILE = 'sealed-raw8g-q35-host-oom-v1'
CONTROL_PROFILE = 'sealed-raw8g-q35-limited-v2'
CHUNK_BYTES = 32 * 1024**2
MAX_STEPS = 25
MAX_JOURNAL_READ = 2 * 1024**2
MAX_JOURNAL_TOTAL = 16 * 1024**2
MAX_JOURNAL_READS = 256
MAX_RECORD_BYTES = 64 * 1024
PRESSURE_SECONDS = 45
REQUIRED_CHECKS = frozenset({
    'host_qemu_oom_kernel_victim_verified', 'host_qemu_oom_memcg_bound',
    'host_qemu_pressure_b_healthy', 'host_qemu_oom_physical_absence',
    'host_qemu_oom_journal_unchanged_boot', 'host_qemu_oom_fixed_caps_verified',
    'host_qemu_oom_three_touch_stages', 'host_qemu_oom_b_limits_unchanged',
    'b_healthy_after_host_qemu_oom_stop',
})


def require(value, message):
    if not value:
        raise RuntimeError(message)


def qualification_limits():
    return {
        'virtual_machines': 2, 'a_profile': HOST_PROFILE, 'b_profile': CONTROL_PROFILE,
        'a_memory_max_bytes': 768 * 1024**2, 'a_swap_max_bytes': 0,
        'b_memory_max_bytes': 2 * 1024**3, 'b_swap_max_bytes': 2 * 1024**3,
        'guest_memory_bytes': 1024**3, 'cpu_period_usec': 100000, 'cpu_quota_usec': 100000,
        'guest_touch_chunk_bytes': CHUNK_BYTES, 'guest_touch_steps_maximum': MAX_STEPS,
        'guest_touch_ceiling_bytes': CHUNK_BYTES * MAX_STEPS,
        'minimum_a_headroom_bytes': 4 * CHUNK_BYTES,
        'guest_lifetime_seconds': 100, 'control_guest_lifetime_seconds': 150,
        'host_pressure_seconds': PRESSURE_SECONDS, 'guest_step_seconds': 5,
        'control_challenge_seconds': 3, 'control_write_seconds': 2,
        'memory_events_samples_maximum': MAX_STEPS + 3, 'pressure_stage_limit_samples': 6,
        'journal_read_seconds': 5, 'journal_read_bytes': MAX_JOURNAL_READ,
        'journal_total_bytes': MAX_JOURNAL_TOTAL, 'journal_record_bytes': MAX_RECORD_BYTES,
        'journal_reads_maximum': MAX_JOURNAL_READS,
        'kernel_cause_pair_seconds': 5, 'physical_absence_seconds': 10,
        'readiness_seconds': 10, 'minimum_free_bytes': 50 * 1024**3,
        'minimum_available_memory_bytes': 8 * 1024**3,
        'global_host_memory_pressure': False, 'model_calls': 0,
    }


def _save(path, data):
    data = data if isinstance(data, bytes) else (json.dumps(data, sort_keys=True, indent=2) + '\n').encode()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        remaining = memoryview(data)
        while remaining:
            count = os.write(fd, remaining)
            require(count > 0, 'Host OOM evidence short write')
            remaining = remaining[count:]
        os.fsync(fd)
    finally:
        os.close(fd)
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    return hashlib.sha256(data).hexdigest()


def _journal(arguments, deadline):
    """Bound both pipes while reading; never accumulate unbounded journal output."""
    process = subprocess.Popen(
        ['/usr/bin/journalctl', '--quiet', '--dmesg', '--boot', '--output=json', '--no-pager', *arguments],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env={'PATH': '/usr/bin:/bin', 'LC_ALL': 'C'}, close_fds=True,
    )
    buffers = {'stdout': bytearray(), 'stderr': bytearray()}
    try:
        with selectors.DefaultSelector() as selector:
            for name in buffers:
                pipe = getattr(process, name)
                os.set_blocking(pipe.fileno(), False)
                selector.register(pipe, selectors.EVENT_READ, name)
            while selector.get_map():
                require(time.monotonic() < deadline, 'Host kernel journal deadline')
                for key, _ in selector.select(min(.1, max(0, deadline - time.monotonic()))):
                    chunk = os.read(key.fileobj.fileno(), 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    body = buffers[key.data]
                    maximum = MAX_JOURNAL_READ if key.data == 'stdout' else 65536
                    require(len(body) + len(chunk) <= maximum, 'Host kernel journal byte bound')
                    body.extend(chunk)
            require(time.monotonic() < deadline, 'Host kernel journal late read')
            require(process.wait(timeout=max(.001, deadline - time.monotonic())) == 0,
                    'Host kernel journal is unavailable')
            require(time.monotonic() < deadline, 'Host kernel journal command returned late')
        return bytes(buffers['stdout'])
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=3)
        for name in buffers:
            getattr(process, name).close()


def _boot_id():
    return uuid.UUID(Path('/proc/sys/kernel/random/boot_id').read_text().strip()).hex


def journal_records(raw, boot_id):
    require(isinstance(raw, bytes) and len(raw) <= MAX_JOURNAL_READ, 'Host journal input bound')
    records = []
    for line in raw.splitlines():
        require(0 < len(line) <= MAX_RECORD_BYTES, 'Host journal record bound')
        value = json.loads(line)
        require(isinstance(value, dict) and value.get('_TRANSPORT') == 'kernel'
                and value.get('_BOOT_ID') == boot_id, 'Host kernel journal identity differs')
        cursor, message = value.get('__CURSOR'), value.get('MESSAGE')
        require(isinstance(cursor, str) and re.fullmatch(r'[\x21-\x7e]{1,1024}', cursor)
                and isinstance(message, str),
                'Host journal metadata differs')
        for field in ('__MONOTONIC_TIMESTAMP', '__REALTIME_TIMESTAMP'):
            require(isinstance(value.get(field), str) and re.fullmatch(r'[0-9]{1,20}', value[field]),
                    'Host journal timestamps missing')
        records.append(value)
    require(len({record['__CURSOR'] for record in records}) == len(records),
            'Duplicate host journal cursor')
    return records


def journal_batch(raw, boot_id, cursor, seen):
    """Seek includes the anchor; vacuum/rotation must never become a silent gap."""
    records = journal_records(raw, boot_id)
    require(bool(records) and records[0]['__CURSOR'] == cursor,
            'Host kernel journal anchor is no longer present')
    current = records[1:]
    require(not {row['__CURSOR'] for row in current} & seen,
            'Host journal repeated an earlier entry')
    return current


def _anchor(reports, name):
    boot = _boot_id()
    raw = _journal(['--lines=1'], time.monotonic() + 5)
    records = journal_records(raw, boot)
    require(len(records) == 1, 'Host kernel journal anchor missing')
    record = records[0]
    return {'boot_id': boot, 'cursor': record['__CURSOR'],
            'monotonic_usec': int(record['__MONOTONIC_TIMESTAMP']),
            'realtime_usec': int(record['__REALTIME_TIMESTAMP']),
            'journal_sha256': _save(reports / name, raw)}


def preflight(root, reports, result):
    require(os.geteuid() != 0 and hasattr(os, 'pidfd_open'), 'Ordinary Linux pidfd support required')
    kvm.no_symlinks(reports)
    info = reports.lstat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid()
            and stat.S_IMODE(info.st_mode) == 0o700, 'Private Host OOM report directory required')
    require(os.statvfs(root).f_bavail * os.statvfs(root).f_frsize >= 50 * 1024**3,
            'Host OOM disk reserve is unavailable')
    available = next(int(line.split()[1]) * 1024 for line in Path('/proc/meminfo').read_text().splitlines()
                     if line.startswith('MemAvailable:'))
    require(available >= 8 * 1024**3, 'Host OOM memory reserve is unavailable')
    anchor = _anchor(reports, 'host-oom-journal-preflight.jsonl')
    anchor['report_directory'] = str(reports)
    result['host_qemu_oom_journal_anchor'] = anchor


def guest_program():
    # This runs only after the unchanged sealed probe. It never writes host
    # cgroups, changes swap, protects itself from Guest OOM, or reports host cause.
    return r'''
import select,signal,re
def host_oom_deadline(*_): raise TimeoutError('bounded host OOM fixture deadline')
signal.signal(signal.SIGALRM,host_oom_deadline)
signal.alarm(100)
fd=os.open('/dev/virtio-ports/org.harniqo.m0',os.O_RDWR|os.O_NONBLOCK)
print('HARNIQO-HOST-OOM-READY '+C['nonce'],flush=True)
pending=b''; blocks=[]; sequence=0; allocated=0
while sequence<25:
    if not select.select([fd],[],[],0.25)[0]: continue
    chunk=os.read(fd,1024)
    if not chunk: continue
    pending+=chunk
    if len(pending)>4096: raise RuntimeError('host OOM control frame bound')
    while b'\n' in pending:
        line,pending=pending.split(b'\n',1)
        if not re.fullmatch(b'[0-9a-f]{40}',line): raise RuntimeError('host OOM token differs')
        if sequence>=25: raise RuntimeError('host OOM allocation bound')
        block=bytearray(32*1024**2)
        for offset in range(0,len(block),4096): block[offset]=1
        blocks.append(block);sequence+=1;allocated+=len(block)
        value={'sequence':sequence,'allocated_bytes':allocated,'token':line.decode('ascii')}
        print('HARNIQO-HOST-OOM-PROGRESS '+C['nonce']+' '+json.dumps(value,separators=(',',':')),flush=True)
raise RuntimeError('host OOM allocation ceiling reached without expected host fault')
'''


def _process(pid):
    require(type(pid) is int and 1 < pid < 2**31, 'Host QEMU PID differs')
    folder = Path('/proc') / str(pid)
    value = (folder / 'stat').read_text()
    fields = value.rsplit(') ', 1)[1].split()
    require(len(fields) >= 20 and fields[0] not in {'Z', 'X', 'x'}, 'Host QEMU process is not live')
    comm = (folder / 'comm').read_text().strip()
    require(re.fullmatch(r'[A-Za-z0-9_.+ /:-]{1,64}', comm), 'Host QEMU comm differs')
    return {'pid': pid, 'start_time': int(fields[19]), 'comm': comm, 'uid': folder.stat().st_uid}


def _target(sample):
    process = _process(sample['pid'])
    require(process['start_time'] == sample['process_start_time']
            and process['uid'] == pwd.getpwnam('libvirt-qemu').pw_uid,
            'Host QEMU identity changed')
    path = Path(sample['memory_cgroup_path'])
    relative = path.relative_to('/sys/fs/cgroup')
    require(relative.parts and relative.parts[0] == 'machine.slice', 'Host QEMU memory group differs')
    info = path.lstat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o022,
            'Host QEMU memory group identity differs')
    return {**process, 'memory_cgroup': '/' + str(relative),
            'memory_cgroup_device': info.st_dev, 'memory_cgroup_inode': info.st_ino}


def memory_events_sample(target):
    """Read the frozen group; preserve actual ENOENT instead of inventing counters."""
    path = Path('/sys/fs/cgroup') / target['memory_cgroup'].lstrip('/')
    stamp = time.monotonic()
    try:
        before = path.lstat()
        require(stat.S_ISDIR(before.st_mode) and before.st_uid == 0
                and (before.st_dev, before.st_ino) ==
                (target['memory_cgroup_device'], target['memory_cgroup_inode']),
                'Host OOM memory group was replaced')
        descriptor = os.open(path / 'memory.events', os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            info = os.fstat(descriptor)
            require(stat.S_ISREG(info.st_mode) and info.st_uid == 0,
                    'Host OOM memory events file differs')
            raw = os.read(descriptor, 4097)
            require(len(raw) <= 4096, 'Host OOM memory events byte bound')
        finally:
            os.close(descriptor)
        after = path.lstat()
        require((after.st_dev, after.st_ino) == (before.st_dev, before.st_ino),
                'Host OOM memory group changed during sampling')
        values = {}
        for line in raw.decode('ascii').splitlines():
            fields = line.split()
            require(len(fields) == 2 and re.fullmatch(r'[a-z_]+', fields[0])
                    and fields[0] not in values and re.fullmatch(r'[0-9]+', fields[1]),
                    'Host OOM memory event counters differ')
            values[fields[0]] = int(fields[1])
        require({'oom', 'oom_kill'} <= values.keys(), 'Host OOM memory event counters missing')
        return {'state': 'observed', 'value': values, 'started_monotonic': stamp,
                'finished_monotonic': time.monotonic()}
    except FileNotFoundError as error:
        require(error.errno == errno.ENOENT, 'Host OOM memory events error differs')
        return {'state': 'missing', 'errno': errno.ENOENT, 'value': None,
                'started_monotonic': stamp, 'finished_monotonic': time.monotonic()}


def _pidfd_exited(descriptor):
    poller = select.poll()
    poller.register(descriptor, select.POLLIN | select.POLLHUP | select.POLLERR)
    events = poller.poll(0)
    require(not any(flags & (select.POLLERR | select.POLLNVAL) for _, flags in events),
            'Host QEMU pidfd observation failed')
    return any(flags & (select.POLLIN | select.POLLHUP) for _, flags in events)


def a_stage_sample(instance, target, baseline, descriptor):
    # Use the production read-only sampler without lifecycle mutation. A
    # transient disappearance here is a failed stage, never an instruction to
    # destroy a VM before the independent kernel cause has been established.
    from apps.runner.resource_broker import resources
    require(not _pidfd_exited(descriptor), 'Host QEMU exited before a pressure-stage challenge')
    require(_process(target['pid']) == {k: target[k] for k in ('pid', 'start_time', 'comm', 'uid')},
            'Host QEMU identity changed at a pressure stage')
    sample = resources.read_limits(name=instance['name'], resource_id=instance['uuid'], profile=HOST_PROFILE)
    require(all(sample[key] == baseline[key] for key in
                ('pid', 'process_start_time', 'cgroup_path', 'memory_cgroup_path', 'swap_cgroup_path',
                 'cpu_period_usec', 'cpu_quota_usec', 'memory_max_bytes', 'swap_max_bytes'))
            and not _pidfd_exited(descriptor), 'Host QEMU pressure identity or fixed caps changed')
    return {'observed_monotonic': time.monotonic(), 'sample': sample}


def match_oom(records, target, not_before_usec):
    """Require two fresh kernel messages binding cause, victim and memory group.

    This fixed fixture conservatively requires the frozen leader, not an unseen
    same-name thread. Journal origin _PID/_COMM are never used as victim fields.
    """
    contexts, victims = [], []
    for record in records:
        stamp = int(record['__MONOTONIC_TIMESTAMP'])
        if stamp < not_before_usec:
            continue
        message = record['MESSAGE']
        if message.startswith('oom-kill:'):
            pieces = message[len('oom-kill:'):].split(',')
            fields = {}
            for piece in pieces:
                if '=' not in piece:
                    continue
                key, value = piece.split('=', 1)
                require(key not in fields, 'Duplicate host OOM context field')
                fields[key] = value
            if fields.get('pid') != str(target['pid']):
                continue
            group, victim_group = fields.get('oom_memcg'), fields.get('task_memcg')
            require(fields.get('constraint') == 'CONSTRAINT_MEMCG'
                    and group == target['memory_cgroup']
                    and isinstance(victim_group, str)
                    and (victim_group == group or victim_group.startswith(group + '/'))
                    and fields.get('task') == target['comm']
                    and fields.get('uid') == str(target['uid']), 'Host OOM context binding differs')
            contexts.append(record)
        match = re.fullmatch(r'Memory cgroup out of memory: Killed process ([0-9]+) \(([^)\r\n]+)\)(.*)', message)
        if match and int(match.group(1)) == target['pid']:
            uid = re.search(r'(?:^|[ ,])UID:([0-9]+)(?:[ ,]|$)', match.group(3))
            require(match.group(2) == target['comm'] and uid is not None
                    and int(uid.group(1)) == target['uid'], 'Host OOM victim binding differs')
            victims.append(record)
    if not contexts or not victims:
        return None
    require(len(contexts) == len(victims) == 1, 'Ambiguous host OOM victim evidence')
    context, victim = contexts[0], victims[0]
    require(context['__CURSOR'] != victim['__CURSOR'], 'Host OOM cause reused one journal entry')
    require(0 <= int(victim['__MONOTONIC_TIMESTAMP']) - int(context['__MONOTONIC_TIMESTAMP']) <= 5_000_000,
            'Host OOM cause and victim timestamps differ')
    return {'context': context, 'victim': victim}


def _progress(streams, instance, token, sequence):
    prefix = 'HARNIQO-HOST-OOM-PROGRESS ' + instance['nonce'] + ' '
    matches = []
    for line in protocol._complete_lines(protocol.console_text(streams, instance)):
        if not line.startswith(prefix):
            continue
        require(len(line) <= 4096, 'Host OOM Guest progress bound')
        value = json.loads(line[len(prefix):])
        if value.get('token') == token:
            require(type(value.get('sequence')) is int and type(value.get('allocated_bytes')) is int
                    and value == {'sequence': sequence, 'allocated_bytes': sequence * CHUNK_BYTES, 'token': token},
                    'Host OOM Guest progress differs')
            matches.append(value)
    require(len(matches) <= 1, 'Duplicate host OOM Guest progress')
    return matches[0] if matches else None


def _fixed_caps(samples):
    for sample, memory, swap in zip(samples, (768 * 1024**2, 2 * 1024**3), (0, 2 * 1024**3)):
        require(all(type(sample.get(key)) is int for key in
                    ('cpu_period_usec', 'cpu_quota_usec', 'memory_max_bytes', 'swap_max_bytes',
                     'memory_current_bytes', 'memory_peak_bytes'))
                and sample['cpu_period_usec'] == sample['cpu_quota_usec'] == 100000
                and sample['memory_max_bytes'] == memory and sample['swap_max_bytes'] == swap
                and 0 < sample['memory_current_bytes'] <= sample['memory_peak_bytes'] < memory
                and sample['memory_events']['oom'] == sample['memory_events']['oom_kill'] == 0,
                'Host OOM initial fixed limits differ')
    require(samples[0]['memory_max_bytes'] - samples[0]['memory_peak_bytes'] >= 4 * CHUNK_BYTES,
            'Host OOM initial headroom is insufficient for staged evidence')


def collect_pressure(broker, instances, streams, result):
    a, b = instances
    anchor = result['host_qemu_oom_journal_anchor']
    reports = Path(anchor['report_directory'])
    require(_boot_id() == anchor['boot_id'], 'Host boot changed after OOM preflight')
    ready = 'HARNIQO-HOST-OOM-READY ' + a['nonce']
    protocol._wait_until(lambda: ready in protocol._complete_lines(protocol.console_text(streams, a)),
                         streams, time.monotonic() + 10, 'Host OOM Guest readiness deadline')
    before = [broker.resource_limits(i['uuid']) for i in instances]
    _fixed_caps(before)
    target = _target(before[0])
    evidence = result['host_qemu_oom'] = {'before': before, 'target': target, 'stages': [], 'progress': [],
                                         'a_memory_events_timeline': []}
    descriptor = os.pidfd_open(target['pid'], 0)
    try:
        require(_process(target['pid']) == {k: target[k] for k in ('pid', 'start_time', 'comm', 'uid')},
                'Host QEMU identity changed during pidfd open')
        evidence['a_memory_events_timeline'].append(memory_events_sample(target))
        begin = _anchor(reports, 'host-oom-journal-before-pressure.jsonl')
        require(begin['boot_id'] == anchor['boot_id'], 'Host boot changed before pressure')
        cursor, records, total, reads = begin['cursor'], [], 0, 0
        seen = {cursor}
        evidence['journal_begin'] = begin
        started = time.monotonic()
        deadline = started + PRESSURE_SECONDS
        evidence['trigger_at'], evidence['trigger_monotonic'] = time.time(), time.monotonic()
        cause = None
        for sequence in range(1, MAX_STEPS + 1):
            token = uuid.uuid4().hex + uuid.uuid4().hex[:8]
            expiry = min(deadline, time.monotonic() + 5)
            protocol._send(streams.report_channels[a['uuid']], token + '\n', streams, deadline=expiry)
            progress = None
            while time.monotonic() < expiry:
                streams._poll(.05)
                require(_boot_id() == anchor['boot_id'], 'Host boot changed during pressure')
                require(reads < MAX_JOURNAL_READS, 'Host OOM journal read-count bound')
                raw = _journal(['--cursor=' + cursor], min(expiry, time.monotonic() + 5))
                require(time.monotonic() < expiry, 'Host OOM journal stage returned late')
                total += len(raw)
                require(total <= MAX_JOURNAL_TOTAL, 'Host OOM total journal byte bound')
                _save(reports / ('host-oom-journal-' + str(reads) + '.jsonl'), raw)
                reads += 1
                current = journal_batch(raw, anchor['boot_id'], cursor, seen)
                if current:
                    cursors = {row['__CURSOR'] for row in current}
                    seen.update(cursors)
                    cursor = current[-1]['__CURSOR']
                    records.extend(current)
                cause = match_oom(records, target, int(evidence['trigger_monotonic'] * 1_000_000))
                if cause is not None:
                    require(time.monotonic() < expiry, 'Host OOM victim evidence arrived late')
                    break
                progress = _progress(streams, a, token, sequence)
                if progress is not None:
                    break
            require(time.monotonic() < deadline, 'Host OOM pressure deadline')
            if cause is not None:
                evidence['a_memory_events_timeline'].append(memory_events_sample(target))
                break
            require(progress is not None and time.monotonic() < expiry, 'Host OOM touch progress deadline')
            evidence['progress'].append(progress)
            evidence['a_memory_events_timeline'].append(memory_events_sample(target))
            if sequence <= 3:
                stage_before = a_stage_sample(a, target, before[0], descriptor)
                challenge = protocol.challenge(streams, b, deadline=deadline)
                stage_after = a_stage_sample(a, target, before[0], descriptor)
                evidence['stages'].append({'allocated_bytes': progress['allocated_bytes'],
                    'b_challenge': challenge, 'a_before_challenge': stage_before,
                    'a_after_challenge': stage_after})
        require(cause is not None and len(evidence['stages']) == 3,
                'Exact host QEMU memcg OOM was not observed')
        absence_deadline = min(deadline, time.monotonic() + 10)
        protocol._wait_until(lambda: _pidfd_exited(descriptor) and a['uuid'] not in kvm.domain_ids(),
                             streams, absence_deadline, 'Kernel victim or libvirt absence unconfirmed')
        evidence.update(cause=cause, journal_total_bytes=total, journal_reads=reads,
                        pidfd_exit_confirmed=True, uuid_absent_confirmed=True,
                        b_after_death_challenge=protocol.challenge(streams, b, deadline=deadline),
                        pressure_seconds=time.monotonic() - started)
        after_b = broker.resource_limits(b['uuid'])
        require(all(after_b[key] == before[1][key] for key in
                    ('pid', 'process_start_time', 'cgroup_path', 'memory_cgroup_path', 'swap_cgroup_path',
                     'cpu_period_usec', 'cpu_quota_usec', 'memory_max_bytes', 'swap_max_bytes'))
                and all(after_b['memory_events'][key] == before[1]['memory_events'][key]
                        for key in ('oom', 'oom_kill')), 'Control QEMU identity or memory events changed')
        evidence['after_b'] = after_b
        # The domain cgroup can already be gone. Absence is not an OOM counter.
        evidence['a_post_memory_events'] = memory_events_sample(target)
        evidence['a_memory_events_timeline'].append(evidence['a_post_memory_events'])
        require(time.monotonic() < deadline, 'Host OOM completion deadline')
        result['checks'].update({key: True for key in REQUIRED_CHECKS - {'b_healthy_after_host_qemu_oom_stop'}})
    finally:
        observed = [row for row in evidence['a_memory_events_timeline'] if row['state'] == 'observed']
        missing = [row for row in evidence['a_memory_events_timeline'] if row['state'] == 'missing']
        evidence['a_last_successful_memory_events'] = observed[-1] if observed else None
        evidence['a_first_missing_memory_events_at'] = missing[0]['started_monotonic'] if missing else None
        os.close(descriptor)


def after_stop(instances, streams, result):
    result['host_qemu_oom']['b_after_formal_stop_challenge'] = protocol.challenge(
        streams, instances[1], deadline=time.monotonic() + 8)
    result['checks']['b_healthy_after_host_qemu_oom_stop'] = True
