"""Synthetic OOM evidence gates and real socket IO; never execute Guest code.

These tests do not exhaust memory, write kernel settings, create VM resources or
establish production isolation. Guest programs are compiled as source only.
"""
from __future__ import annotations

import ast
import builtins
import hashlib
import copy
import io
import os
import stat
import json
import socket
from pathlib import PurePosixPath
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

import guest_memory_pressure as memory


BOOT = '01234567-89ab-cdef-0123-456789abcdef'
MIB = 1024**2
GIB = 1024**3



def monitor_evidence(nonce='nonce-a'):
    unit='harniqo-m0-memory-'+nonce+'.service'
    return {'pid':200,'start_time':70,'unit':unit,'cgroup':'/system.slice/'+unit,'score':-1000,
        'oom_group':0,'runtime_max_usec':185000000,'stop_timeout_usec':3000000,
        'log_identity':{'device':8,'inode':16},'log_fsize_limit_bytes':2*1024**2,
        'properties':{'Id':unit,'MainPID':'200','ControlGroup':'/system.slice/'+unit,'OOMPolicy':'continue',
            'DefaultDependencies':'no','OOMScoreAdjust':'-1000','KillMode':'control-group',
            'MemoryMax':'infinity','MemoryHigh':'infinity','MemorySwapMax':'infinity',
            'RuntimeMaxUSec':'3min 5s','TimeoutStopUSec':'3s','StandardOutput':'append',
            'StandardError':'append','RemainAfterExit':'yes'}}

def evidence(comm='hnq-0123456789'):
    start = {'monitor':monitor_evidence(),'boot_id': BOOT, 'pid': 321, 'start_time': 71, 'comm': comm,
             'allocated_bytes': 128*MIB, 'oom_before': 3, 'memtotal_bytes': GIB,
             'swap_total_bytes': 0, 'allocation_ceiling_bytes': 2*GIB,
             'kernel_cursor_new_only': True, 'guest_cgroup_unlimited': True,
             'kernel_release': '6.8.0-test', 'os_release': {'ID': 'ubuntu', 'VERSION_ID': '24.04'},
             'worker_oom_score_adj': 1000}
    line = 'Out of memory: Killed process 321 ('+comm+') total-vm:800000kB, anon-rss:600000kB'
    finish = {key: start[key] for key in ('monitor','boot_id', 'pid', 'start_time', 'comm', 'oom_before', 'kernel_release', 'os_release', 'worker_oom_score_adj')}
    finish.update(returncode=-9, oom_after=4, allocated_bytes=640*MIB,
                  timed_out=False, kernel_victim_matched=True,
                  kernel_victim_line=line,
                  kernel_victim_sha256=hashlib.sha256(line.encode()).hexdigest())
    return start, finish


class GuestEvidenceTests(unittest.TestCase):
    def test_exact_kernel_victim_and_unchanged_boot_are_accepted(self):
        start, finish = evidence()
        memory.validate_guest_evidence(start, finish)

    def test_sigkill_without_new_global_oom_cannot_prove_memory_exhaustion(self):
        for change in ({'returncode': 0}, {'returncode': 1}, {'returncode': -15},
                       {'oom_after': 3}, {'oom_after': 2}, {'oom_after': True},
                       {'timed_out': True}, {'timed_out': 0},
                       {'kernel_victim_matched': False}):
            with self.subTest(change=change):
                start, finish = evidence()
                finish.update(change)
                with self.assertRaises(RuntimeError):
                    memory.validate_guest_evidence(start, finish)

    def test_pid_comm_starttime_and_boot_cannot_be_rebound(self):
        for key, value in (('pid', 322), ('start_time', 72), ('comm', 'hnq-aaaaaaaaaa'),
                           ('boot_id', 'ffffffff-89ab-cdef-0123-456789abcdef'),
                           ('oom_before', 2), ('kernel_release', '6.9.0-other'),
                           ('os_release', {'ID': 'other', 'VERSION_ID': '24.04'}),
                           ('worker_oom_score_adj', 0)):
            with self.subTest(key=key):
                start, finish = evidence()
                finish[key] = value
                with self.assertRaisesRegex(RuntimeError, 'identity changed'):
                    memory.validate_guest_evidence(start, finish)

    def test_wrong_victim_or_memcg_message_cannot_prove_global_oom(self):
        _, valid = evidence()
        for line in (valid['kernel_victim_line'].replace('process 321 ', 'process 322 '),
                     valid['kernel_victim_line'].replace('hnq-0123456789', 'hnq-aaaaaaaaaa'),
                     'Memory cgroup out of memory: Killed process 321 (hnq-0123456789) x',
                     'Out of memory: Killed process 321 (hnq-0123456789x) x',
                     None, 'x'*2049):
            with self.subTest(line=line):
                start, finish = evidence()
                finish['kernel_victim_line'] = line
                finish['kernel_victim_sha256'] = hashlib.sha256(line.encode()).hexdigest() if line else None
                with self.assertRaisesRegex(RuntimeError, 'victim log not proven'):
                    memory.validate_guest_evidence(start, finish)

    def test_missing_or_changed_kernel_hash_is_rejected(self):
        for change in ({'kernel_victim_sha256': None}, {'kernel_victim_sha256': '0'*64}):
            with self.subTest(change=change):
                start, finish = evidence()
                finish.update(change)
                with self.assertRaisesRegex(RuntimeError, 'victim log not proven'):
                    memory.validate_guest_evidence(start, finish)

    def test_ineligible_guest_shape_and_historical_log_are_rejected(self):
        changes = [('pid', 1), ('pid', True), ('start_time', 0), ('start_time', '71'),
                   ('allocated_bytes', 127*MIB), ('oom_before', -1),
                   ('memtotal_bytes', 699*MIB), ('memtotal_bytes', 1101*MIB),
                   ('swap_total_bytes', MIB), ('allocation_ceiling_bytes', 3*GIB),
                   ('kernel_cursor_new_only', False), ('guest_cgroup_unlimited', False),
                   ('comm', 'python3'), ('boot_id', 'not-a-uuid'),
                   ('kernel_release', ''), ('kernel_release', None),
                   ('os_release', {'ID': 'ubuntu', 'VERSION_ID': '22.04'}),
                   ('worker_oom_score_adj', -1000), ('worker_oom_score_adj', '1000')]
        for key, value in changes:
            with self.subTest(key=key, value=value):
                start, finish = evidence()
                start[key] = value
                with self.assertRaises(RuntimeError):
                    memory.validate_guest_evidence(start, finish)

    def test_allocation_ceiling_or_regression_is_not_counted_as_oom(self):
        for allocated in (127*MIB, 2*GIB, 3*GIB, None, True):
            with self.subTest(allocated=allocated):
                start, finish = evidence()
                finish['allocated_bytes'] = allocated
                with self.assertRaisesRegex(RuntimeError, 'kernel OOM not proven'):
                    memory.validate_guest_evidence(start, finish)

    def test_generated_guest_and_worker_only_compile_and_guard_before_mutation(self):
        # Compiling deliberately does not run the source or its embedded worker.
        source = memory.guest_program()
        compile(source, '<qualification-guest-source>', 'exec')
        compile(memory.WORKER, '<qualification-worker-source>', 'exec')
        self.assertLess(memory.WORKER.index('guest_identity_guard'),
                        memory.WORKER.index("write_text('1000')"))
        self.assertLess(memory.WORKER.index('guest_start_gate'),
                        memory.WORKER.index('bytearray('))
        for guard in ('guest_identity_guard', 'guest_memory_shape_guard', 'guest_finite_cgroup_guard'):
            self.assertLess(memory.GUEST.index(guard),
                            memory.GUEST.index("write_text('-1000')"))


class WorkerDeadlineTests(unittest.TestCase):
    def test_worker_arms_its_own_bounded_alarm_before_allocating(self):
        # Parse, never execute, the independent worker guard. Parent scheduling
        # cannot be the only mechanism bounding a child after parent failure.
        nodes = list(ast.walk(ast.parse(memory.WORKER)))
        alarms = [node for node in nodes if isinstance(node, ast.Call)
                  and isinstance(node.func, ast.Attribute)
                  and isinstance(node.func.value, ast.Name)
                  and node.func.value.id == 'signal' and node.func.attr == 'alarm']
        allocations = [node for node in nodes if isinstance(node, ast.Call)
                       and isinstance(node.func, ast.Name) and node.func.id == 'bytearray']
        self.assertEqual(len(alarms), 1)
        self.assertTrue(allocations)
        self.assertEqual(len(alarms[0].args), 1)
        duration = ast.literal_eval(alarms[0].args[0])
        self.assertIs(type(duration), int)
        self.assertTrue(0 < duration <= 35)
        self.assertLess(alarms[0].lineno, min(node.lineno for node in allocations))


class IsolatedGuest:
    """Execute only GUEST control flow against explicit fake OS capabilities.

    No fallback import, file IO, process launch, signal or allocation is allowed.
    The pressure WORKER remains an uninterpreted command argument to fake Popen.
    """
    def __init__(self, scenario):
        self.scenario = scenario
        self.now = 0.0
        self.steps = 0
        self.kmsg_seen = False
        self.closed = []
        self.prints = []
        self.writes = []
        self.vm_uuid = 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'
        self.paths = {
            '/sys/class/dmi/id/product_uuid': self.vm_uuid,
            '/proc/sys/kernel/random/boot_id': BOOT,
            '/etc/os-release': 'ID=ubuntu\nVERSION_ID="24.04"\n',
            '/proc/meminfo': 'MemTotal: 1048576 kB\nSwapTotal: 0 kB\n',
            '/proc/swaps': 'Filename Type Size Used Priority\n',
            '/proc/self/cgroup': '0::/system.slice/harniqo-m0-memory-nonce-a.service\n', '/sys/fs/cgroup/memory.max': 'max',
            '/proc/self/oom_score_adj': '-1000',
            '/proc/self/stat': '200 (monitor) '+('0 '*19)+'70',
            '/sys/fs/cgroup/system.slice/harniqo-m0-memory-nonce-a.service/memory.oom.group':'0',
            '/proc/321/stat': '321 (hnq-aaaaaaaaaa) '+('0 '*19)+'71',
        }
        machine = self

        class FakePath:
            def __init__(self, name): self.name = str(PurePosixPath(name))
            def __truediv__(self, name): return FakePath(str(PurePosixPath(self.name)/name))
            def __eq__(self, other): return self.name == other.name
            @property
            def parent(self): return FakePath(str(PurePosixPath(self.name).parent))
            def exists(self): return self.name in machine.paths
            def is_symlink(self): return False
            def stat(self): return SimpleNamespace(st_dev=8,st_ino=16,st_uid=0,st_mode=stat.S_IFREG|0o600,st_nlink=1,st_size=0)
            def read_text(self):
                if self.name == '/proc/vmstat':
                    return 'oom_kill '+str(int(machine.scenario == 'late_oom' and machine.now >= 40))+'\n'
                return machine.paths[self.name]
            def write_text(self, value):
                if self.name != '/proc/self/oom_score_adj': raise AssertionError('Unexpected fake write')
                machine.writes.append((self.name, value))
                machine.paths[self.name] = value
                return len(value)

        class Pipe:
            closed = False
            def fileno(self): return 13
            def write(self, data): return len(data)
            def close(self): self.closed = True

        class Process:
            pid = 321
            returncode = None
            killed = False
            stdout = Pipe()
            stdin = Pipe()
            def poll(self):
                if machine.scenario == 'late_oom' and machine.now >= 40: self.returncode = -9
                return self.returncode
            def kill(self): self.killed = True; self.returncode = -9
            def wait(self, timeout): return self.returncode

        self.process = Process()
        fake_os = SimpleNamespace(geteuid=lambda: 0, getpid=lambda:200,fstat=lambda fd:SimpleNamespace(st_dev=8,st_ino=16), uname=lambda: SimpleNamespace(release='6.8.0-test'),
            open=self.open, read=self.read, close=self.closed.append, lseek=lambda *args: 0,
            set_blocking=lambda *args: None, O_RDWR=2, O_NONBLOCK=2048, O_RDONLY=0,
            O_CLOEXEC=524288, SEEK_END=2)
        self.imports = {'hashlib': hashlib, 're': memory.re,'stat':stat,
            'resource':SimpleNamespace(RLIMIT_FSIZE=1,getrlimit=lambda kind:(2*1024**2,2*1024**2)),
            'select': SimpleNamespace(select=self.select), 'signal': SimpleNamespace(SIGTERM=15,signal=lambda *args:None),
            'sys': SimpleNamespace(platform='linux', executable='/fake/python'),
            'pathlib': SimpleNamespace(Path=FakePath)}
        self.namespace = {'__builtins__': {**vars(builtins), '__import__': self.import_module,
                'print': lambda *args, **kwargs: self.prints.append(' '.join(map(str, args)))},
            'os': fake_os, 'time': SimpleNamespace(monotonic=lambda: self.now), 'json': json,
            'subprocess': SimpleNamespace(run=lambda *args,**kwargs:SimpleNamespace(stdout='\n'.join(k+'='+v for k,v in monitor_evidence()['properties'].items())),Popen=lambda *args, **kwargs: self.process,
                PIPE=-1, DEVNULL=-3), 'MEMORY_WORKER': memory.WORKER,
            'C': {'instance_uuid': self.vm_uuid, 'nonce': 'nonce-a'}}

    def import_module(self, name, *args, **kwargs):
        if name not in self.imports: raise AssertionError('Unexpected import '+name)
        return self.imports[name]

    def open(self, path, flags):
        return {'/dev/virtio-ports/org.harniqo.m0': 11, '/dev/kmsg': 12}[path]

    def read(self, descriptor, bound):
        if descriptor == 11: return ('MEM nonce-a '+'a'*40+'\n').encode()
        if descriptor == 13:
            item = ({'kind': 'identity', 'pid': 321, 'start_time': 71,
                     'comm': 'hnq-aaaaaaaaaa', 'oom_score_adj': 1000} if self.steps == 2
                    else {'kind': 'progress', 'allocated_bytes': 128*MIB})
            return (json.dumps(item)+'\n').encode()
        if descriptor == 12:
            if self.scenario == 'late_oom' and self.now >= 40 and not self.kmsg_seen:
                self.kmsg_seen = True
                return b'6,123,40000000,-;Out of memory: Killed process 321 (hnq-aaaaaaaaaa) total-vm:800000kB\n'
            raise BlockingIOError()
        raise AssertionError('Unexpected fake descriptor')

    def select(self, read, write, error, timeout):
        self.steps += 1
        if self.scenario == 'outer_deadline':
            self.now = 119.0 if self.steps == 1 else self.now+2
            return ([11] if self.steps == 1 else [], [], [])
        self.now = {1: 1.0, 2: 2.0, 3: 40.0}.get(self.steps, self.now+1)
        return ([11] if self.steps == 1 else [13] if self.steps <= 3 else [], [], [])

    def run(self):
        exec(compile(memory.GUEST, '<isolated-guest-control>', 'exec'), self.namespace)


class GuestDeadlineTests(unittest.TestCase):
    def test_outer_deadline_cleans_active_worker_without_entering_health_phase(self):
        guest = IsolatedGuest('outer_deadline')
        with self.assertRaises(RuntimeError):
            guest.run()
        self.assertLessEqual(guest.now, 122)
        self.assertTrue(guest.process.killed)
        self.assertEqual(set(guest.closed), {11, 12})
        self.assertFalse(any('HARNIQO-MEM-RESULT ' in line for line in guest.prints))

    def test_kernel_log_eof_cannot_spin_without_progress_or_a_deadline(self):
        guest=IsolatedGuest('outer_deadline')
        original=guest.namespace['os'].read
        reads=[]
        def eof(descriptor,bound):
            if descriptor==12:
                reads.append(descriptor)
                if len(reads)>3: raise AssertionError('Kernel EOF was retried without progress')
                return b''
            return original(descriptor,bound)
        guest.namespace['os'].read=eof
        with self.assertRaisesRegex(RuntimeError,'guest_kernel_log_closed'):
            guest.run()
        self.assertEqual(reads,[12])
        self.assertTrue(guest.process.killed)

    def test_exited_worker_cannot_hide_a_missed_pressure_deadline(self):
        guest = IsolatedGuest('late_oom')
        with self.assertRaises(RuntimeError):
            guest.run()
        self.assertEqual(guest.now, 40)
        self.assertEqual(set(guest.closed), {11, 12})
        self.assertFalse(any('HARNIQO-MEM-RESULT ' in line for line in guest.prints))


class Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now


class SocketFixture:
    """Two real nonblocking socketpairs and bounded synthetic console files."""
    def __init__(self, case, *, mode=None):
        self.clock = Clock()
        self.mode = mode
        self.token = None
        self.live_count = 0
        self.commands = []
        self.logs = {}
        self.files = {}
        self.peers = {}
        self.pending = {}
        self.report_channels = {}
        self.instances = [{'uuid': name, 'nonce': 'nonce-'+name} for name in ('a', 'b')]
        for item in self.instances:
            name = item['uuid']
            stream = tempfile.TemporaryFile()
            case.addCleanup(stream.close)
            self.files[name] = stream
            self.logs[name] = {'fd': stream.fileno(), 'bytes_saved': 0}
            host, peer = socket.socketpair()
            case.addCleanup(host.close)
            case.addCleanup(peer.close)
            host.setblocking(False)
            peer.setblocking(False)
            self.report_channels[name] = host
            self.peers[name] = peer
            self.pending[name] = b''
            self.append(name, 'HARNIQO-RESOURCE-START '+item['nonce'])
        self.start = None
        self.finish = None

    def append(self, name, text):
        value = (text+'\n').encode()
        self.files[name].write(value)
        self.files[name].flush()
        self.logs[name]['bytes_saved'] += len(value)

    def event(self, kind, payload):
        self.append('a', 'HARNIQO-MEM-'+kind+' nonce-a '+self.token+' '+json.dumps(payload))

    def _poll(self, timeout):
        self.clock.now += timeout
        for name, peer in self.peers.items():
            try:
                chunk = peer.recv(4096)
            except BlockingIOError:
                continue
            self.pending[name] += chunk
            while b'\n' in self.pending[name]:
                raw, self.pending[name] = self.pending[name].split(b'\n', 1)
                line = raw.decode()
                self.commands.append((name, line))
                if line.startswith('MEM nonce-a '):
                    self.token = line.split()[2]
                    self.start, self.finish = evidence('hnq-'+self.token[:10])
                    if self.mode == 'old_boot':
                        self.start['boot_id'] = 'ffffffff-89ab-cdef-0123-456789abcdef'
                    if self.mode == 'wrong_comm':
                        self.start['comm'] = 'hnq-fffffffff0'
                    self.event('START', self.start)
                    if self.mode == 'duplicate_start':
                        self.event('START', self.start)
                    self.event('PROGRESS', {'allocated_bytes': 128*MIB})
                elif name == 'b' and self.mode == 'silent_b':
                    pass
                elif line.startswith('LIVE nonce-a '+str(self.token)+' '):
                    challenge = line.split()[3]
                    if self.mode == 'stale_live':
                        challenge = '0'*40
                    payload = {'challenge': challenge, 'pid': 321,
                               'allocated_bytes': (self.live_count+1)*128*MIB, 'boot_id': BOOT}
                    if self.mode == 'regressed_live':
                        payload['allocated_bytes'] = 128*MIB
                    if self.mode == 'wrong_live_boot':
                        payload['boot_id'] = 'ffffffff-89ab-cdef-0123-456789abcdef'
                    self.event('LIVE', payload)
                    if self.mode == 'duplicate_live':
                        self.event('LIVE', payload)
                    self.live_count += 1
                    if self.live_count < 3:
                        self.event('PROGRESS', {'allocated_bytes': (self.live_count+1)*128*MIB})
                    if self.live_count == 3:
                        if self.mode == 'no_oom':
                            self.finish['oom_after'] = self.finish['oom_before']
                        self.event('RESULT', self.finish)
                elif line.startswith('HEALTH nonce-a '+str(self.token)+' '):
                    boot = BOOT if self.mode != 'wrong_health_boot' else 'ffffffff-89ab-cdef-0123-456789abcdef'
                    self.event('HEALTH', {'challenge': line.split()[3], 'boot_id': boot})
                else:
                    self.append(name, 'HARNIQO-HEARTBEAT nonce-'+name+' '+line)


class Samples:
    def __init__(self, *, changed=None, oom=False, cap=False):
        self.calls = {}
        self.changed = changed
        self.oom = oom
        self.cap = cap

    def resource_limits(self, name):
        count = self.calls.get(name, 0)+1
        self.calls[name] = count
        sample = {'pid': 10 if name == 'a' else 20, 'process_start_time': 100,
                  'cgroup_path': '/cpu/'+name, 'memory_cgroup_path': '/memory/'+name,
                  'swap_cgroup_path': '/swap/'+name, 'memory_current_bytes': 300*MIB,
                  'memory_peak_bytes': 500*MIB, 'memory_max_bytes': 2*GIB,
                  'swap_max_bytes': 2*GIB, 'memory_events': {'oom': 0, 'oom_kill': 0}}
        if count == 2:
            if self.changed and self.changed[0] == name:
                key = self.changed[1]
                sample[key] = 200 if isinstance(sample[key], int) else '/changed'
            if self.oom:
                sample['memory_events']['oom_kill'] = 1
            if self.cap:
                sample['memory_peak_bytes'] = 2*GIB
        return sample


class HostPressureTests(unittest.TestCase):
    def run_fixture(self, *, mode=None, samples=None):
        streams = SocketFixture(self, mode=mode)
        result = {'checks': {}, 'guests': {'a': {'boot_id': BOOT}}}
        with patch.object(memory.time, 'monotonic', streams.clock.monotonic):
            memory.run_pressure(samples or Samples(), streams.instances, streams, result)
        return result, streams

    def test_causal_live_acknowledgements_and_post_oom_health_are_required(self):
        result, streams = self.run_fixture()
        self.assertTrue(all(result['checks'][key] for key in memory.REQUIRED_CHECKS))
        self.assertEqual(len(result['guest_memory_pressure']['live_acknowledgements']), 3)
        sent = streams.commands
        self.assertEqual([name for name, _ in sent], ['a', 'b', 'a', 'b', 'a', 'b', 'a', 'a', 'b'])
        for index in (1, 3, 5):
            self.assertEqual(sent[index][1], sent[index+1][1].split()[-1])
        # A health is token-bound; B also answers a new challenge after A's OOM.
        self.assertTrue(sent[-2][1].startswith('HEALTH nonce-a '+streams.token+' '))
        self.assertEqual(len(sent[-1][1]), 40)
        self.assertNotIn(sent[-1][1], [line for name, line in sent[:-1] if name == 'b'])

    def test_stale_boot_or_unbound_worker_name_is_rejected_before_liveness(self):
        for mode in ('old_boot', 'wrong_comm'):
            with self.subTest(mode=mode):
                with self.assertRaisesRegex(RuntimeError, 'run binding invalid'):
                    self.run_fixture(mode=mode)

    def test_b_reply_cannot_be_proven_by_a_stale_live_acknowledgement(self):
        with self.assertRaisesRegex(TimeoutError, 'live acknowledgement deadline'):
            self.run_fixture(mode='stale_live')

    def test_b_must_answer_a_fresh_random_challenge(self):
        with self.assertRaisesRegex(TimeoutError, 'health challenge deadline'):
            self.run_fixture(mode='silent_b')

    def test_duplicate_live_acknowledgements_are_rejected(self):
        with self.assertRaisesRegex(RuntimeError, 'Duplicate memory live acknowledgement'):
            self.run_fixture(mode='duplicate_live')

    def test_live_acknowledgements_must_grow_in_the_same_boot(self):
        for mode in ('regressed_live', 'wrong_live_boot'):
            with self.subTest(mode=mode):
                with self.assertRaisesRegex(RuntimeError, 'Memory live acknowledgement invalid'):
                    self.run_fixture(mode=mode)

    def test_duplicate_start_evidence_cannot_select_a_convenient_identity(self):
        with self.assertRaisesRegex(RuntimeError, 'Duplicate Guest memory evidence'):
            self.run_fixture(mode='duplicate_start')

    def test_post_oom_health_must_come_from_the_original_boot(self):
        with self.assertRaisesRegex(RuntimeError, 'host predicate failed'):
            self.run_fixture(mode='wrong_health_boot')

    def test_host_gate_does_not_accept_unproven_guest_oom(self):
        with self.assertRaisesRegex(RuntimeError, 'kernel OOM not proven'):
            self.run_fixture(mode='no_oom')

    def test_any_changed_qemu_identity_or_host_pressure_fails_the_window(self):
        for name in ('a', 'b'):
            for field in ('pid', 'process_start_time', 'cgroup_path', 'memory_cgroup_path', 'swap_cgroup_path'):
                with self.subTest(name=name, field=field):
                    with self.assertRaisesRegex(RuntimeError, 'host predicate failed'):
                        self.run_fixture(samples=Samples(changed=(name, field)))
        for kwargs in ({'oom': True}, {'cap': True}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaisesRegex(RuntimeError, 'host predicate failed'):
                    self.run_fixture(samples=Samples(**kwargs))

    def test_event_nonce_and_run_token_are_exact(self):
        value = 'HARNIQO-MEM-START nonce-a token-a {"pid":321}\n'
        self.assertEqual(memory._events(value, 'START', 'nonce-a', 'token-a'), [{'pid': 321}])
        for nonce, token in (('nonce-b', 'token-a'), ('nonce-a', 'token-b')):
            self.assertEqual(memory._events(value, 'START', nonce, token), [])

    def test_partial_console_json_is_not_an_event_until_its_newline_arrives(self):
        prefix = 'HARNIQO-MEM-START nonce-a token-a '
        fragment = prefix+'{"pid":'
        self.assertEqual(memory._events(fragment, 'START', 'nonce-a', 'token-a'), [])
        completed = fragment+'321}'
        self.assertEqual(memory._events(completed, 'START', 'nonce-a', 'token-a'), [])
        self.assertEqual(memory._events(completed+'\n', 'START', 'nonce-a', 'token-a'), [{'pid': 321}])

    def test_control_writes_preserve_bytes_across_partial_real_socket_sends(self):
        host, peer = socket.socketpair()
        self.addCleanup(host.close)
        self.addCleanup(peer.close)
        host.setblocking(False)
        peer.setblocking(False)
        host.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1024)
        clock = Clock()
        received = bytearray()
        polls = []

        class Partial:
            def send(self, payload):
                return host.send(payload[:37])

        class Drain:
            def _poll(self, seconds):
                clock.now += seconds
                polls.append(seconds)
                while True:
                    try: received.extend(peer.recv(4096))
                    except BlockingIOError: break

        payload = 'x'*32768+'\n'
        with patch.object(memory.time, 'monotonic', clock.monotonic):
            memory._send(Partial(), payload, Drain(), timeout=10)
        Drain()._poll(0)
        self.assertEqual(received, payload.encode())
        self.assertTrue(polls)

    def test_unwritable_control_peer_has_a_finite_deadline(self):
        clock = Clock()

        class Blocked:
            def send(self, payload):
                raise BlockingIOError()

        class Poll:
            def _poll(self, seconds):
                clock.now += seconds

        with patch.object(memory.time, 'monotonic', clock.monotonic):
            with self.assertRaisesRegex(TimeoutError, 'control write deadline'):
                memory._send(Blocked(), 'probe\n', Poll(), timeout=.1)
        self.assertLess(clock.now, .2)


class HostDeadlineAcceptanceTests(unittest.TestCase):
    def test_matching_reply_observed_after_challenge_deadline_is_rejected(self):
        streams = SocketFixture(self)
        original = streams._poll
        def late_reply(seconds):
            original(seconds)
            streams.clock.now += 3.01
        streams._poll = late_reply
        with patch.object(memory.time, 'monotonic', streams.clock.monotonic):
            with self.assertRaises(TimeoutError):
                memory.challenge(streams, streams.instances[1])

    def test_full_send_completing_after_write_deadline_is_rejected(self):
        clock = Clock()
        class DelayedSend:
            def send(self, payload):
                clock.now += 2.01
                return len(payload)
        with patch.object(memory.time, 'monotonic', clock.monotonic):
            with self.assertRaises(TimeoutError):
                memory._send(DelayedSend(), 'bounded\n', None)


    def test_wait_rejects_both_a_late_poll_reply_and_a_slow_successful_observation(self):
        for slow_predicate in (False,True):
            with self.subTest(slow_predicate=slow_predicate):
                clock=Clock()
                class Poll:
                    def _poll(self, seconds): clock.now=2
                def observe():
                    if slow_predicate: clock.now=2
                    return clock.now>0
                with patch.object(memory.time,'monotonic',clock.monotonic):
                    with self.assertRaises(TimeoutError):
                        memory._wait_until(observe,Poll(),1,'deadline')

    def test_each_memory_evidence_kind_is_rejected_if_observation_crosses_its_window(self):
        for kind in ('START','PROGRESS','LIVE','RESULT','HEALTH'):
            with self.subTest(kind=kind):
                streams=SocketFixture(self)
                result={'checks':{},'guests':{'a':{'boot_id':BOOT}}}
                original=memory._events
                expired=[]
                def late_events(text, actual_kind, nonce, token):
                    values=original(text,actual_kind,nonce,token)
                    if actual_kind==kind and values and not expired:
                        streams.clock.now+=40.01
                        expired.append(actual_kind)
                    return values
                with patch.object(memory.time,'monotonic',streams.clock.monotonic), patch.object(memory,'_events',side_effect=late_events):
                    with self.assertRaises(TimeoutError):
                        memory.run_pressure(Samples(),streams.instances,streams,result)
                self.assertEqual(expired,[kind])
                record=result.get('guest_memory_pressure',{})
                if kind=='START': self.assertNotIn('guest_memory_pressure',result)
                if kind in ('PROGRESS','LIVE'): self.assertEqual(record['live_acknowledgements'],[])
                if kind=='RESULT': self.assertNotIn('finish',record)
                if kind=='HEALTH': self.assertNotIn('post_oom_health',record)
                self.assertFalse(any(result['checks'].values()))

    def test_ready_and_post_sample_work_cannot_hide_expired_parent_windows(self):
        streams=SocketFixture(self)
        original=memory.console_text
        def late_ready(*args):
            value=original(*args)
            streams.clock.now=10.01
            return value
        with patch.object(memory.time,'monotonic',streams.clock.monotonic), patch.object(memory,'console_text',side_effect=late_ready):
            with self.assertRaises(TimeoutError):
                memory.run_pressure(Samples(),streams.instances,streams,{'checks':{}})
        self.assertEqual(streams.commands,[])
        streams=SocketFixture(self)
        class SlowSamples(Samples):
            def resource_limits(self,name):
                value=super().resource_limits(name)
                if self.calls[name]==2: streams.clock.now+=40.01
                return value
        result={'checks':{},'guests':{'a':{'boot_id':BOOT}}}
        with patch.object(memory.time,'monotonic',streams.clock.monotonic):
            with self.assertRaises(TimeoutError):
                memory.run_pressure(SlowSamples(),streams.instances,streams,result)
        self.assertNotIn('post_oom_health',result['guest_memory_pressure'])
        self.assertFalse(any(command.startswith('HEALTH ') for _,command in streams.commands))

    def test_send_and_challenge_obey_a_shorter_enclosing_deadline(self):
        for operation in ('send','challenge'):
            with self.subTest(operation=operation):
                streams=SocketFixture(self)
                original=streams._poll
                def delay(seconds):
                    original(seconds)
                    streams.clock.now+=.6
                streams._poll=delay
                class SlowSend:
                    def send(self,payload):
                        streams.clock.now+=.6
                        return len(payload)
                with patch.object(memory.time,'monotonic',streams.clock.monotonic):
                    with self.assertRaises(TimeoutError):
                        if operation=='send': memory._send(SlowSend(),'bounded\n',None,deadline=.5)
                        else: memory.challenge(streams,streams.instances[1],deadline=.5)

    def test_frozen_limits_match_independent_worker_alarm_and_return_fresh_values(self):
        limits=memory.qualification_limits()
        tree=ast.parse(memory.WORKER)
        alarm=[node.args[0].value for node in ast.walk(tree) if isinstance(node,ast.Call) and isinstance(node.func,ast.Attribute) and node.func.attr=='alarm']
        self.assertEqual(alarm,[limits['worker_lifetime_seconds']])
        self.assertEqual(limits['max_allocation_bytes'],memory.MAX_ALLOCATION)
        self.assertEqual(limits['virtual_machines'],2)
        self.assertEqual(limits['simultaneous_memory_workers'],1)
        self.assertTrue(all(type(value) is int and value>0 for value in limits.values()))
        limits['worker_lifetime_seconds']=99
        self.assertEqual(memory.qualification_limits()['worker_lifetime_seconds'],32)


class CloudInitConsoleFramingTests(unittest.TestCase):
    PREFIX='[  123.456789] cloud-init[987]: '

    def test_prefixed_console_supports_the_complete_host_protocol(self):
        prefix=self.PREFIX
        class Prefixed(SocketFixture):
            def append(self,name,text): super().append(name,prefix+text)
        streams=Prefixed(self)
        result={'checks':{},'guests':{'a':{'boot_id':BOOT}}}
        with patch.object(memory.time,'monotonic',streams.clock.monotonic):
            memory.run_pressure(Samples(),streams.instances,streams,result)
        self.assertTrue(all(result['checks'][name] for name in memory.REQUIRED_CHECKS))
        self.assertEqual(len(result['guest_memory_pressure']['live_acknowledgements']),3)

    def test_prefixed_event_still_needs_complete_lf_and_exact_nonce(self):
        body='HARNIQO-MEM-START nonce-a token-a {"pid":321}'
        text=self.PREFIX+body
        self.assertEqual(memory._events(text,'START','nonce-a','token-a'),[])
        self.assertEqual(memory._events(text+'\r\n','START','nonce-a','token-a'),[{'pid':321}])
        self.assertEqual(memory._events(text+'\n','START','nonce-b','token-a'),[])
        self.assertEqual(memory._events(text+'\n','START','nonce-a','token-b'),[])

    def test_unknown_nested_or_embedded_prefixes_cannot_create_a_protocol_line(self):
        body='HARNIQO-MEM-START nonce-a token-a {"pid":321}'
        bad=['[  123.456789] other[987]: ', '[  123.456789] cloud-init[x]: ',
             '[  12345678901.1] cloud-init[1]: ', '['+' '*13+'1.1] cloud-init[1]: ',
             'noise '+self.PREFIX, self.PREFIX+self.PREFIX, self.PREFIX+'noise ',
             'unknown\v', 'unknown\f', 'unknown\r', 'unknown\u2028']
        for prefix in bad:
            with self.subTest(prefix=repr(prefix)):
                self.assertEqual(memory._events(prefix+body+'\n','START','nonce-a','token-a'),[])


class GuestSupervisionTests(unittest.TestCase):
    def test_monitor_policy_and_main_process_readback_are_strict_and_unchanged(self):
        for runtime in ('3min 5s','185s','185000000'):
            value=monitor_evidence()
            value['properties']['RuntimeMaxUSec']=runtime
            memory.validate_monitor(value)
        for key,value in (('MainPID','201'),('OOMPolicy','stop'),('DefaultDependencies','yes'),
                          ('ControlGroup','/system.slice/other.service'),('MemoryMax','1G'),
                          ('RuntimeMaxUSec','3min'),('OOMScoreAdjust','0')):
            with self.subTest(key=key):
                monitor=monitor_evidence()
                monitor['properties'][key]=value
                with self.assertRaises(RuntimeError): memory.validate_monitor(monitor)
        start,finish=evidence()
        finish['monitor']=copy.deepcopy(finish['monitor'])
        finish['monitor']['start_time']+=1
        with self.assertRaisesRegex(RuntimeError,'identity changed'):
            memory.validate_guest_evidence(start,finish)

    def test_monitor_rechecks_current_cgroup_on_each_readback(self):
        guest=IsolatedGuest('outer_deadline')
        with self.assertRaises(RuntimeError): guest.run()
        readback=guest.namespace['monitor_facts']
        self.assertEqual(readback()['cgroup'],'/system.slice/harniqo-m0-memory-nonce-a.service')
        guest.paths['/proc/self/cgroup']='0::/system.slice/foreign.service\n'
        with self.assertRaisesRegex(RuntimeError,'guest_monitor_unit_guard'):
            readback()

    def test_launcher_uses_private_manager_route_and_bounded_complete_log_relay(self):
        from pathlib import Path as LocalPath
        for mode in ('success','unknown_state','partial_final_line'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as folder:
                local=LocalPath(folder); calls=[]; printed=[]
                def mapped(name): return local/PurePosixPath(str(name)).relative_to('/run')
                def info(value):
                    return SimpleNamespace(st_dev=value.st_dev,st_ino=value.st_ino,st_uid=0,
                        st_mode=value.st_mode,st_nlink=value.st_nlink,st_size=value.st_size)
                class Path:
                    def __init__(self,name): self.name=str(PurePosixPath(name))
                    def __str__(self): return self.name
                    def __truediv__(self,other): return Path(str(PurePosixPath(self.name)/other))
                    def is_symlink(self): return mapped(self.name).is_symlink()
                    def mkdir(self,mode): mapped(self.name).mkdir(mode=mode)
                    def lstat(self): return info(mapped(self.name).lstat())
                    def read_text(self):
                        if self.name!='/sys/class/dmi/id/product_uuid': raise AssertionError('Unexpected fake read')
                        return 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'
                def fake_run(command,**kwargs):
                    calls.append((command,kwargs))
                    if command[0]=='/usr/bin/systemd-run':
                        compile(mapped(command[-1]).read_text(),'<generated-private-monitor>','exec')
                        mapped('/run/harniqo-memory-'+'a'*32+'/monitor.log').write_text('controlled-frame'+('' if mode=='partial_final_line' else '\n'))
                        return SimpleNamespace(stdout='')
                    if command[1]=='stop': return SimpleNamespace(stdout='')
                    values={'ActiveState':'active','SubState':'exited','Result':'success','ExecMainCode':'1','ExecMainStatus':'0','MainPID':'0'}
                    if mode=='unknown_state': values={'ActiveState':'unknown'}
                    return SimpleNamespace(stdout='\n'.join(k+'='+v for k,v in values.items()))
                fake_os=SimpleNamespace(geteuid=lambda:0,
                    open=lambda name,*args:os.open(mapped(name),*args), fdopen=os.fdopen,fsync=os.fsync,
                    fchmod=os.fchmod,close=os.close,read=os.read,fstat=lambda fd:info(os.fstat(fd)),
                    O_WRONLY=os.O_WRONLY,O_RDONLY=os.O_RDONLY,O_CREAT=os.O_CREAT,O_EXCL=os.O_EXCL,
                    O_NOFOLLOW=os.O_NOFOLLOW,O_CLOEXEC=os.O_CLOEXEC)
                imports={'hashlib':hashlib,'re':memory.re,'stat':stat,'sys':SimpleNamespace(platform='linux'),
                         'pathlib':SimpleNamespace(Path=Path)}
                namespace={'__builtins__':{**vars(builtins),'__import__':lambda name,*args,**kwargs:imports[name],
                    'print':lambda *args,**kwargs:printed.append(' '.join(map(str,args)))},
                    'os':fake_os,'json':json,'time':SimpleNamespace(monotonic=lambda:0,sleep=lambda seconds:None),
                    'subprocess':SimpleNamespace(run=fake_run,DEVNULL=-3),
                    'C':{'instance_uuid':'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee','nonce':'a'*32},
                    'MEMORY_CONTROLLER':memory.guest_program()}
                if mode=='success':
                    exec(compile(memory.LAUNCHER,'<isolated-guest-launcher>','exec'),namespace)
                    self.assertEqual(printed,['controlled-frame'])
                else:
                    with self.assertRaises(RuntimeError):
                        exec(compile(memory.LAUNCHER,'<isolated-guest-launcher>','exec'),namespace)
                    self.assertTrue(any('HARNIQO-MEM-ERROR ' in line for line in printed))
                command,kwargs=calls[0]
                self.assertNotIn('-c',command)
                for flag in ('--pipe','--wait','--pty','--collect'): self.assertNotIn(flag,command)
                self.assertIn('--property=OOMPolicy=continue',command)
                self.assertIn('--property=DefaultDependencies=no',command)
                self.assertIn('--property=RuntimeMaxSec=185s',command)
                self.assertIn('--property=LimitFSIZE=2097152',command)
                self.assertIn('--property=RemainAfterExit=yes',command)
                self.assertIn('--expand-environment=no',command)
                self.assertEqual(kwargs['timeout'],5)
                self.assertEqual(command[-2:],['/usr/bin/python3','/run/harniqo-memory-'+'a'*32+'/monitor.py'])
                self.assertEqual(stat.S_IMODE(mapped(command[-1]).stat().st_mode),0o400)
                self.assertEqual(calls[-1][0][:3],['/usr/bin/systemctl','stop','--no-block'])
        self.assertEqual(memory.qualification_limits()['guest_unit_runtime_seconds'],185)
        self.assertEqual(memory.qualification_limits()['guest_supervisor_timeout_seconds'],190)

    def test_fixed_error_frames_fail_fast_but_foreign_nonce_does_not(self):
        streams=SocketFixture(self)
        streams.append('a','HARNIQO-MEM-ERROR nonce-b bootstrap {"code":"guest_supervisor_failed"}')
        memory.console_text(streams,streams.instances[0])
        streams.append('a','HARNIQO-MEM-ERROR nonce-a bootstrap {"code":"guest_supervisor_failed","kind":"CalledProcessError"}')
        with patch.object(memory.time,'monotonic',streams.clock.monotonic):
            with self.assertRaisesRegex(RuntimeError,'Guest monitor reported guest_supervisor_failed'):
                memory.run_pressure(Samples(),streams.instances,streams,{'checks':{}})
        self.assertEqual(streams.clock.now,0)
        self.assertEqual(streams.commands,[])

    def test_sigterm_before_memory_command_emits_a_bootstrap_error_safely(self):
        guest=IsolatedGuest('outer_deadline')
        handlers={}
        guest.imports['signal'].signal=lambda number,handler:handlers.setdefault(number,handler)
        def terminate(*args): handlers[15](15,None)
        guest.imports['select'].select=terminate
        with self.assertRaisesRegex(RuntimeError,'guest_monitor_terminated'):
            guest.run()
        self.assertTrue(any('HARNIQO-MEM-ERROR nonce-a bootstrap ' in line for line in guest.prints))
        self.assertEqual(guest.closed,[11])

    def test_child_exit_milestone_precedes_kernel_log_collection(self):
        guest=IsolatedGuest('late_oom')
        guest.process.poll=lambda:-9 if guest.now>=2 else None
        original=guest.namespace['os'].read
        def stop_at_kernel(descriptor,bound):
            if descriptor==12 and guest.now>=2: raise RuntimeError('kernel-read-sentinel')
            return original(descriptor,bound)
        guest.namespace['os'].read=stop_at_kernel
        with self.assertRaisesRegex(RuntimeError,'kernel-read-sentinel'):
            guest.run()
        self.assertTrue(any('HARNIQO-MEM-CHILD-EXIT ' in line for line in guest.prints))
        self.assertFalse(any('HARNIQO-MEM-RESULT ' in line for line in guest.prints))


if __name__ == '__main__':
    unittest.main()
