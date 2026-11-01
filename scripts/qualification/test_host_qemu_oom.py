"""Synthetic Host QEMU OOM evidence and bounded control regressions.

No VM, model request, kernel setting, journal command or real pressure worker is
run. Guest execution uses a logical buffer that stores no allocated pages.
"""
from __future__ import annotations

import builtins
import copy
from contextlib import ExitStack
import errno
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import host_qemu_oom as oom


BOOT = '0123456789abcdef0123456789abcdef'
START = 200_000_000
TOKEN = 'a1' * 20
INSTANCE = {'uuid': 'fixture-a', 'nonce': 'fixture-nonce-a'}
TARGET = {'pid': 4217, 'start_time': 170, 'comm': 'qemu-system-x86', 'uid': 64055,
          'memory_cgroup': '/machine.slice/machine-qemu-fixture.scope/libvirt'}


def record(message='fixture kernel entry', *, cursor='opaque-cursor-1', stamp=START + 10, **fields):
    return {'_BOOT_ID': BOOT, '_TRANSPORT': 'kernel', '__CURSOR': cursor,
            '__MONOTONIC_TIMESTAMP': str(stamp), '__REALTIME_TIMESTAMP': '1793480400000000',
            'MESSAGE': message, **fields}


def encode(records):
    return b''.join(json.dumps(item, separators=(',', ':')).encode() + b'\n' for item in records)


def cause_pair():
    group = TARGET['memory_cgroup']
    context = ('oom-kill:constraint=CONSTRAINT_MEMCG,nodemask=(null),cpuset=emulator,'
               'mems_allowed=0,oom_memcg=' + group + ',task_memcg=' + group + '/emulator,'
               'task=qemu-system-x86,pid=4217,uid=64055')
    victim = ('Memory cgroup out of memory: Killed process 4217 (qemu-system-x86) '
              'total-vm:1400000kB, anon-rss:700000kB, file-rss:2048kB, shmem-rss:0kB, '
              'UID:64055 pgtables:2048kB oom_score_adj:0')
    return [record(context, cursor='context-cursor', stamp=START + 10),
            record(victim, cursor='victim-cursor', stamp=START + 20)]


def match(records):
    return oom.match_oom(oom.journal_records(encode(records), BOOT), TARGET, START)


def limits_sample(*, control=False):
    group = '/sys/fs/cgroup' + TARGET['memory_cgroup']
    return {'pid': 4218 if control else TARGET['pid'], 'process_start_time': TARGET['start_time'],
            'cgroup_path': group, 'memory_cgroup_path': group, 'swap_cgroup_path': group,
            'cpu_period_usec': 100000, 'cpu_quota_usec': 100000,
            'memory_max_bytes': 2 * 1024**3 if control else 768 * 1024**2,
            'swap_max_bytes': 2 * 1024**3 if control else 0,
            'memory_current_bytes': 200 * 1024**2, 'memory_peak_bytes': 300 * 1024**2,
            'memory_events': {'oom': 0, 'oom_kill': 0}}


class JournalRecordTests(unittest.TestCase):
    def test_quiet_poll_and_complete_kernel_json_records(self):
        self.assertEqual(oom.journal_records(b'', BOOT), [])
        expected = cause_pair()
        self.assertEqual(oom.journal_records(encode(expected), BOOT), expected)

    def test_boot_and_transport_are_trusted_exact_string_fields(self):
        for field, bad in (('_BOOT_ID', 'f' * 32), ('_BOOT_ID', [BOOT]),
                           ('_BOOT_ID', None), ('_TRANSPORT', 'stdout'),
                           ('_TRANSPORT', ['kernel']), ('_TRANSPORT', None)):
            with self.subTest(field=field, value=bad):
                with self.assertRaises(RuntimeError):
                    oom.journal_records(encode([record(**{field: bad})]), BOOT)

    def test_cursor_must_be_present_bounded_single_ascii_value(self):
        for cursor in (None, '', ['cursor'], 'c' * 1025, 'cursor with space',
                       'cursor\nnext', 'cursor\x00', 'nonascii-游标'):
            with self.subTest(cursor=cursor):
                with self.assertRaises(RuntimeError):
                    oom.journal_records(encode([record(cursor=cursor)]), BOOT)

    def test_timestamps_are_bounded_decimal_strings_not_json_numbers(self):
        for field in ('__MONOTONIC_TIMESTAMP', '__REALTIME_TIMESTAMP'):
            for bad in (None, 12, True, '', '-1', '1.0', '1e3', '1\n', '1' * 21):
                with self.subTest(field=field, value=bad):
                    with self.assertRaises(RuntimeError):
                        oom.journal_records(encode([record(**{field: bad})]), BOOT)

    def test_message_arrays_and_null_cannot_supply_victim_fields(self):
        for bad in (None, ['first', 'second'], [255], 123):
            with self.subTest(message=bad):
                with self.assertRaises(RuntimeError):
                    oom.journal_records(encode([record(MESSAGE=bad)]), BOOT)

    def test_unrelated_multiline_kernel_messages_are_retained_without_becoming_cause(self):
        for separator in ('\n', '\r'):
            with self.subTest(separator=separator):
                message = 'kernel stack entry' + separator + cause_pair()[0]['MESSAGE']
                expected = record(message)
                self.assertEqual(oom.journal_records(encode([expected]), BOOT), [expected])
                self.assertIsNone(match([expected, cause_pair()[1]]))

    def test_nonobject_and_invalid_json_do_not_become_empty_evidence(self):
        for raw in (b'[]\n', b'null\n', b'123\n', b'\n', b'{broken}\n', b'\xff\n'):
            with self.subTest(raw=raw):
                with self.assertRaises((RuntimeError, ValueError, UnicodeError)):
                    oom.journal_records(raw, BOOT)

    def test_record_and_total_read_bounds_are_enforced_before_acceptance(self):
        oversized_record = encode([record('x' * oom.MAX_RECORD_BYTES)])
        with self.assertRaisesRegex(RuntimeError, 'record bound'):
            oom.journal_records(oversized_record, BOOT)
        with self.assertRaisesRegex(RuntimeError, 'input bound'):
            oom.journal_records(b'x' * (oom.MAX_JOURNAL_READ + 1), BOOT)
        with self.assertRaisesRegex(RuntimeError, 'input bound'):
            oom.journal_records(encode([record()]).decode(), BOOT)


class KernelCauseTests(unittest.TestCase):
    def test_linux7_context_and_kill_pair_bind_the_frozen_leader(self):
        rows = cause_pair()
        self.assertEqual(match(rows), {'context': rows[0], 'victim': rows[1]})

    def test_journal_origin_pid_and_comm_are_not_the_oom_victim(self):
        rows = cause_pair()
        for row in rows:
            row.update(_PID='99999', _COMM='kernel-origin', SYSLOG_PID='88888')
        self.assertIsNotNone(match(rows))
        rows[1]['MESSAGE'] = rows[1]['MESSAGE'].replace('process 4217 ', 'process 99999 ')
        self.assertIsNone(match(rows))

    def test_victim_task_can_belong_to_exact_memory_group(self):
        rows = cause_pair()
        group = TARGET['memory_cgroup']
        rows[0]['MESSAGE'] = rows[0]['MESSAGE'].replace('task_memcg=' + group + '/emulator',
                                                       'task_memcg=' + group)
        self.assertIsNotNone(match(rows))

    def test_stale_context_or_victim_cannot_complete_a_fresh_pair(self):
        for index in (0, 1):
            with self.subTest(index=index):
                rows = cause_pair()
                rows[index]['__MONOTONIC_TIMESTAMP'] = str(START - 1)
                self.assertIsNone(match(rows))

    def test_neither_context_nor_sigkill_victim_line_alone_proves_cause(self):
        for rows in ([], cause_pair()[:1], cause_pair()[1:],
                     [record('qemu-system-x86 terminated by SIGKILL')]):
            with self.subTest(rows=rows):
                self.assertIsNone(match(rows))

    def test_foreign_pid_cannot_be_selected_by_a_shared_comm(self):
        rows = cause_pair()
        rows[0]['MESSAGE'] = rows[0]['MESSAGE'].replace('pid=4217', 'pid=4218')
        self.assertIsNone(match(rows))
        rows = cause_pair()
        rows[1]['MESSAGE'] = rows[1]['MESSAGE'].replace('process 4217 ', 'process 4218 ')
        self.assertIsNone(match(rows))

    def test_foreign_oom_pair_does_not_replace_the_exact_victim(self):
        extra = cause_pair()
        for index, row in enumerate(extra):
            row['__CURSOR'] = 'foreign-' + str(index)
            row['MESSAGE'] = row['MESSAGE'].replace('pid=4217', 'pid=4218').replace('process 4217 ', 'process 4218 ')
        self.assertIsNotNone(match(extra + cause_pair()))

    def test_context_uid_comm_constraint_and_limit_group_are_exact(self):
        replacements = [('uid=64055', 'uid=64056'), ('task=qemu-system-x86', 'task=qemu-system-arm'),
                        ('constraint=CONSTRAINT_MEMCG', 'constraint=CONSTRAINT_NONE'),
                        ('constraint=CONSTRAINT_MEMCG', 'constraint=CONSTRAINT_CPUSET'),
                        ('oom_memcg=' + TARGET['memory_cgroup'], 'oom_memcg=/machine.slice')]
        for old, new in replacements:
            with self.subTest(replacement=new):
                rows = cause_pair()
                rows[0]['MESSAGE'] = rows[0]['MESSAGE'].replace(old, new)
                with self.assertRaises(RuntimeError):
                    match(rows)

    def test_task_path_same_prefix_sibling_and_ancestor_are_not_contained(self):
        group = TARGET['memory_cgroup']
        for path in (group + '-sibling/emulator', '/machine.slice', '/other/emulator'):
            with self.subTest(path=path):
                rows = cause_pair()
                rows[0]['MESSAGE'] = rows[0]['MESSAGE'].replace('task_memcg=' + group + '/emulator',
                                                               'task_memcg=' + path)
                with self.assertRaises(RuntimeError):
                    match(rows)

    def test_global_oom_does_not_satisfy_memcg_attribution(self):
        rows = cause_pair()
        rows[0]['MESSAGE'] = ('oom-kill:constraint=CONSTRAINT_NONE,nodemask=(null),global_oom,'
                              'task_memcg=' + TARGET['memory_cgroup'] + ',task=qemu-system-x86,pid=4217,uid=64055')
        with self.assertRaises(RuntimeError):
            match(rows)
        self.assertIsNone(match([record(cause_pair()[1]['MESSAGE'].replace('Memory cgroup out of memory:',
                                                                         'Out of memory:'))]))

    def test_kill_record_uid_and_comm_cannot_disagree_with_context(self):
        for old, new in (('UID:64055', 'UID:64056'), ('UID:64055', 'uid:64055'),
                         ('UID:64055', 'no_uid'), ('(qemu-system-x86)', '(qemu-system-arm)')):
            with self.subTest(replacement=new):
                rows = cause_pair()
                rows[1]['MESSAGE'] = rows[1]['MESSAGE'].replace(old, new)
                with self.assertRaises(RuntimeError):
                    match(rows)

    def test_duplicate_context_fields_cannot_select_a_favorable_value(self):
        for field in ('pid=4217', 'uid=64055', 'constraint=CONSTRAINT_MEMCG'):
            with self.subTest(field=field):
                rows = cause_pair()
                rows[0]['MESSAGE'] += ',' + field
                with self.assertRaisesRegex(RuntimeError, 'Duplicate'):
                    match(rows)

    def test_duplicate_context_or_victim_records_are_ambiguous(self):
        for index in (0, 1):
            with self.subTest(index=index):
                rows = cause_pair()
                duplicate = copy.deepcopy(rows[index])
                duplicate['__CURSOR'] = 'another-entry'
                with self.assertRaisesRegex(RuntimeError, 'Ambiguous'):
                    match(rows + [duplicate])

    def test_context_and_victim_must_identify_two_distinct_journal_entries(self):
        rows = cause_pair()
        rows[1]['__CURSOR'] = rows[0]['__CURSOR']
        with self.assertRaises(RuntimeError):
            match(rows)

    def test_context_must_precede_victim_within_the_frozen_pair_window(self):
        for delta in (-1, 5_000_001):
            with self.subTest(delta=delta):
                rows = cause_pair()
                rows[1]['__MONOTONIC_TIMESTAMP'] = str(int(rows[0]['__MONOTONIC_TIMESTAMP']) + delta)
                with self.assertRaisesRegex(RuntimeError, 'timestamps'):
                    match(rows)


class JournalContinuityTests(unittest.TestCase):
    def test_quiet_batch_retains_exact_anchor_and_only_returns_new_records(self):
        anchor = record(cursor='anchor')
        seen = {'earlier-entry', 'anchor'}
        self.assertEqual(oom.journal_batch(encode([anchor]), BOOT, 'anchor', seen), [])
        current = record(cursor='new-entry')
        self.assertEqual(oom.journal_batch(encode([anchor, current]), BOOT, 'anchor', seen), [current])
        self.assertEqual(seen, {'earlier-entry', 'anchor'})

    def test_empty_or_nearest_available_entry_is_not_an_exact_anchor(self):
        anchor = record(cursor='anchor')
        nearest = record(cursor='nearest-entry')
        for rows in ([], [nearest], [nearest, anchor]):
            with self.subTest(rows=rows):
                with self.assertRaisesRegex(RuntimeError, 'anchor is no longer present'):
                    oom.journal_batch(encode(rows), BOOT, 'anchor', {'anchor'})

    def test_repeated_anchor_and_any_previously_seen_entry_are_rejected(self):
        anchor = record(cursor='anchor')
        for rows in ([anchor, anchor], [anchor, record(cursor='old-entry')]):
            with self.subTest(rows=rows):
                with self.assertRaises(RuntimeError):
                    oom.journal_batch(encode(rows), BOOT, 'anchor', {'anchor', 'old-entry'})


class PressureStageTests(unittest.TestCase):
    def stage(self, *, process=None, exited=None, sample=None):
        from apps.runner.resource_broker import resources
        identity = {key: TARGET[key] for key in ('pid', 'start_time', 'comm', 'uid')}
        with patch.object(oom, '_pidfd_exited', side_effect=[False, False] if exited is None else exited), \
                patch.object(oom, '_process', return_value=identity if process is None else process), \
                patch.object(resources, 'read_limits', return_value=limits_sample() if sample is None else sample) as sampler:
            result = oom.a_stage_sample({**INSTANCE, 'name': 'fixture-domain-a'}, TARGET, limits_sample(), 99)
        return result, sampler

    def test_stage_calls_only_production_read_sampler_with_the_fault_profile(self):
        value, sampler = self.stage()
        sampler.assert_called_once_with(name='fixture-domain-a', resource_id=INSTANCE['uuid'], profile=oom.HOST_PROFILE)
        self.assertEqual(value['sample'], limits_sample())
        self.assertIsInstance(value['observed_monotonic'], float)

    def test_exited_pidfd_prevents_process_and_cgroup_sampling(self):
        from apps.runner.resource_broker import resources
        with patch.object(oom, '_pidfd_exited', return_value=True), \
                patch.object(oom, '_process') as process, patch.object(resources, 'read_limits') as sampler:
            with self.assertRaisesRegex(RuntimeError, 'exited before'):
                oom.a_stage_sample({**INSTANCE, 'name': 'fixture-domain-a'}, TARGET, limits_sample(), 99)
            process.assert_not_called()
            sampler.assert_not_called()

    def test_recycled_pid_or_changed_process_identity_is_not_a_live_stage(self):
        identity = {key: TARGET[key] for key in ('pid', 'start_time', 'comm', 'uid')}
        for key, changed in (('start_time', 171), ('pid', 4218), ('comm', 'other-qemu'), ('uid', 64056)):
            with self.subTest(key=key):
                with self.assertRaisesRegex(RuntimeError, 'identity changed at a pressure stage'):
                    self.stage(process={**identity, key: changed})

    def test_changed_cap_process_or_control_path_is_not_a_stage(self):
        sample = limits_sample()
        for key in ('pid', 'process_start_time', 'cgroup_path', 'memory_cgroup_path', 'swap_cgroup_path',
                    'cpu_period_usec', 'cpu_quota_usec', 'memory_max_bytes', 'swap_max_bytes'):
            changed = sample[key] + 1 if isinstance(sample[key], int) else sample[key] + '-foreign'
            with self.subTest(key=key):
                with self.assertRaisesRegex(RuntimeError, 'identity or fixed caps changed'):
                    self.stage(sample={**sample, key: changed})

    def test_exit_during_the_raw_read_cannot_leave_an_accepted_live_sample(self):
        with self.assertRaisesRegex(RuntimeError, 'identity or fixed caps changed'):
            self.stage(exited=[False, True])


class MemoryEventSamplingTests(unittest.TestCase):
    def sample(self, *, raw=b'low 0\nmax 2\noom 1\noom_kill 1\n', before=None, after=None,
               open_error=None, file_info=None):
        target = {**TARGET, 'memory_cgroup_device': 8, 'memory_cgroup_inode': 16}
        group_info = SimpleNamespace(st_mode=stat.S_IFDIR | 0o755, st_uid=0, st_dev=8, st_ino=16)
        regular_info = SimpleNamespace(st_mode=stat.S_IFREG | 0o444, st_uid=0)
        with patch.object(oom.Path, 'lstat', side_effect=[before or group_info, after or group_info]), \
                patch.object(oom.os, 'open', return_value=99, side_effect=open_error) as opened, \
                patch.object(oom.os, 'fstat', return_value=file_info or regular_info), \
                patch.object(oom.os, 'read', return_value=raw) as read, \
                patch.object(oom.os, 'close') as closed, \
                patch.object(oom.time, 'monotonic', side_effect=[200.0, 200.001]):
            value = oom.memory_events_sample(target)
        return value, opened, read, closed

    def test_observed_counters_are_inode_bound_and_read_under_a_byte_bound(self):
        value, opened, read, closed = self.sample()
        self.assertEqual(value, {'state': 'observed', 'value': {'low': 0, 'max': 2, 'oom': 1, 'oom_kill': 1},
                                 'started_monotonic': 200.0, 'finished_monotonic': 200.001})
        opened.assert_called_once_with(Path('/sys/fs/cgroup' + TARGET['memory_cgroup']) / 'memory.events',
                                       os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        read.assert_called_once_with(99, 4097)
        closed.assert_called_once_with(99)

    def test_actual_enoent_at_group_or_file_preserves_missing_with_no_counter_value(self):
        for kwargs in ({'before': FileNotFoundError(errno.ENOENT, 'fixture group removed')},
                       {'open_error': FileNotFoundError(errno.ENOENT, 'fixture file removed')}):
            with self.subTest(kwargs=kwargs):
                value, _, read, closed = self.sample(**kwargs)
                self.assertEqual(value, {'state': 'missing', 'errno': errno.ENOENT, 'value': None,
                                         'started_monotonic': 200.0, 'finished_monotonic': 200.001})
                read.assert_not_called()
                closed.assert_not_called()

    def test_permission_failure_is_not_reported_as_deleted_cgroup(self):
        with self.assertRaises(PermissionError):
            self.sample(open_error=PermissionError(errno.EACCES, 'fixture access denied'))
        with self.assertRaisesRegex(RuntimeError, 'error differs'):
            self.sample(open_error=FileNotFoundError(errno.EACCES, 'wrong errno class'))

    def test_replaced_group_before_or_after_read_cannot_supply_counters(self):
        replacement = SimpleNamespace(st_mode=stat.S_IFDIR | 0o755, st_uid=0, st_dev=8, st_ino=17)
        for kwargs in ({'before': replacement}, {'after': replacement}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaisesRegex(RuntimeError, 'group (was replaced|changed during sampling)'):
                    self.sample(**kwargs)

    def test_bad_event_shape_duplicate_keys_missing_oom_or_oversized_reads_fail(self):
        for raw in (b'oom 1\noom 2\noom_kill 1\n', b'oom -1\noom_kill 1\n', b'oom 1\n',
                    b'oom 1 extra\noom_kill 1\n', b'oom 1\noom_kill \xff\n', b'x' * 4097):
            with self.subTest(raw=raw[:60]):
                with self.assertRaises((RuntimeError, UnicodeError)):
                    self.sample(raw=raw)
        with self.assertRaisesRegex(RuntimeError, 'file differs'):
            self.sample(file_info=SimpleNamespace(st_mode=stat.S_IFREG | 0o444, st_uid=1000))


class CollectorOrderingTests(unittest.TestCase):
    def collect(self, *, death=None):
        from apps.runner.resource_broker import resources
        state = SimpleNamespace(now=START / 1_000_000, sequence=0, challenges=0, dead=False)
        a = {**INSTANCE, 'name': 'fixture-domain-a'}
        b = {'uuid': 'fixture-b', 'nonce': 'fixture-nonce-b', 'name': 'fixture-domain-b'}
        target = {**TARGET, 'memory_cgroup_device': 8, 'memory_cgroup_inode': 16}
        anchor_record = record(cursor='anchor', stamp=START)
        anchor = {'boot_id': BOOT, 'cursor': 'anchor', 'report_directory': '/fixture/reports'}
        result = {'checks': {}, 'host_qemu_oom_journal_anchor': anchor}
        broker = SimpleNamespace(resource_limits=lambda identity: limits_sample(control=identity == b['uuid']))

        def poll(duration):
            state.now += duration

        streams = SimpleNamespace(_poll=poll, report_channels={a['uuid']: object()})

        def send(*_, **kwargs):
            state.sequence += 1

        def journal(arguments, deadline):
            self.assertEqual(arguments, ['--cursor=anchor'])
            if state.sequence <= 3:
                return encode([anchor_record])
            state.dead = True
            pair = cause_pair()
            for index, row in enumerate(pair):
                row['__MONOTONIC_TIMESTAMP'] = str(int(state.now * 1_000_000) + index)
            return encode([anchor_record, *pair])

        def progress(_, instance, token, sequence):
            if death == 'after_progress' and sequence == 3:
                state.dead = True
            return {'sequence': sequence, 'allocated_bytes': sequence * oom.CHUNK_BYTES, 'token': token}

        def challenge(*_, **kwargs):
            state.challenges += 1
            if death == 'during_challenge' and state.challenges == 3:
                state.dead = True
            return 'fresh-challenge-' + str(state.challenges)

        def events(_):
            return {'state': 'missing' if state.dead else 'observed',
                    'value': None if state.dead else {'oom': 0, 'oom_kill': 0},
                    **({'errno': errno.ENOENT} if state.dead else {}),
                    'started_monotonic': state.now, 'finished_monotonic': state.now}

        with ExitStack() as stack:
            mocks = [(oom, '_boot_id', {'return_value': BOOT}), (oom, '_target', {'return_value': target}),
                     (oom, '_anchor', {'return_value': anchor}), (oom, '_journal', {'side_effect': journal}),
                     (oom, '_save', {'return_value': 'fixture-hash'}), (oom, '_progress', {'side_effect': progress}),
                     (oom, '_process', {'return_value': {key: TARGET[key] for key in ('pid', 'start_time', 'comm', 'uid')}}),
                     (oom, '_pidfd_exited', {'side_effect': lambda descriptor: state.dead}),
                     (oom, 'memory_events_sample', {'side_effect': events}),
                     (oom.os, 'pidfd_open', {'create': True, 'return_value': 99}),
                     (oom.os, 'close', {}), (oom.time, 'monotonic', {'side_effect': lambda: state.now}),
                     (oom.time, 'time', {'return_value': 1793480400.0}),
                     (oom.protocol, 'console_text', {'return_value': 'HARNIQO-HOST-OOM-READY ' + a['nonce'] + '\n'}),
                     (oom.protocol, '_send', {'side_effect': send}),
                     (oom.protocol, 'challenge', {'side_effect': challenge}),
                     (resources, 'read_limits', {'side_effect': lambda **kwargs: limits_sample()}),
                     (oom.kvm, 'domain_ids', {'return_value': {b['uuid']}})]
            for owner, name, kwargs in mocks:
                stack.enter_context(patch.object(owner, name, **kwargs))
            try:
                oom.collect_pressure(broker, [a, b], streams, result)
            except RuntimeError as error:
                state.error = error
            else:
                state.error = None
        return result, state

    def test_death_after_third_progress_cannot_pass_the_third_during_pressure_challenge(self):
        for moment, challenge_count in (('after_progress', 2), ('during_challenge', 3)):
            with self.subTest(moment=moment):
                result, state = self.collect(death=moment)
                self.assertIsInstance(state.error, RuntimeError)
                self.assertEqual(state.challenges, challenge_count)
                self.assertEqual(len(result['host_qemu_oom']['stages']), 2)
                self.assertEqual(result['checks'], {})
                self.assertIsNotNone(result['host_qemu_oom']['a_last_successful_memory_events'])

    def test_success_retains_before_after_stage_samples_and_true_missing_provenance(self):
        result, state = self.collect()
        self.assertIsNone(state.error)
        evidence = result['host_qemu_oom']
        self.assertEqual(len(evidence['stages']), 3)
        self.assertEqual(state.challenges, 4)
        for stage in evidence['stages']:
            self.assertEqual(stage['a_before_challenge']['sample'], limits_sample())
            self.assertEqual(stage['a_after_challenge']['sample'], limits_sample())
        timeline = evidence['a_memory_events_timeline']
        self.assertEqual([row['state'] for row in timeline], ['observed'] * 4 + ['missing'] * 2)
        self.assertEqual(evidence['a_last_successful_memory_events'], timeline[3])
        self.assertEqual(evidence['a_first_missing_memory_events_at'], timeline[4]['started_monotonic'])
        self.assertIsNone(evidence['a_post_memory_events']['value'])
        self.assertTrue(all(result['checks'][key] for key in oom.REQUIRED_CHECKS - {'b_healthy_after_host_qemu_oom_stop'}))


class ProgressTests(unittest.TestCase):
    def progress(self, text, *, token=TOKEN, sequence=1):
        with patch.object(oom.protocol, 'console_text', return_value=text):
            return oom._progress(None, INSTANCE, token, sequence)

    def frame(self, *, nonce=None, **changes):
        value = {'sequence': 1, 'allocated_bytes': oom.CHUNK_BYTES, 'token': TOKEN, **changes}
        return 'HARNIQO-HOST-OOM-PROGRESS ' + (nonce or INSTANCE['nonce']) + ' ' + json.dumps(value) + '\n'

    def test_only_complete_nonce_and_token_bound_progress_is_accepted(self):
        frame = self.frame()
        self.assertEqual(self.progress(frame), {'sequence': 1, 'allocated_bytes': oom.CHUNK_BYTES, 'token': TOKEN})
        for text in (frame[:-1], self.frame(nonce='other-nonce'), self.frame(token='b2' * 20),
                     'noise ' + frame):
            with self.subTest(text=text):
                self.assertIsNone(self.progress(text))

    def test_sequence_bytes_and_extra_fields_cannot_be_adjusted(self):
        for changes in ({'sequence': 2}, {'allocated_bytes': oom.CHUNK_BYTES - 1},
                        {'sequence': '1'}, {'allocated_bytes': str(oom.CHUNK_BYTES)},
                        {'unexpected': True}):
            with self.subTest(changes=changes):
                with self.assertRaises(RuntimeError):
                    self.progress(self.frame(**changes))

    def test_boolean_sequence_is_not_an_integer_progress_ack(self):
        with self.assertRaises(RuntimeError):
            self.progress(self.frame(sequence=True))

    def test_duplicate_progress_frames_and_oversized_frames_are_rejected(self):
        with self.assertRaisesRegex(RuntimeError, 'Duplicate'):
            self.progress(self.frame() * 2)
        with self.assertRaisesRegex(RuntimeError, 'bound'):
            self.progress(self.frame(unexpected='x' * 4096))

    def test_known_cloud_init_prefix_and_crlf_preserve_complete_frames(self):
        text = '[   42.125] cloud-init[123]: ' + self.frame().rstrip('\n') + '\r\n'
        self.assertEqual(self.progress(text)['token'], TOKEN)


class GuestControlTests(unittest.TestCase):
    def execute_guest(self, frames, *, timeout=False):
        state = SimpleNamespace(blocks=[], frames=bytearray(frames), output=[], alarms=[], reads=0)

        class LogicalBuffer:
            def __init__(self, size):
                self.size = size
                self.touches = 0
                self.last_offset = -4096
                state.blocks.append(self)

            def __len__(self):
                return self.size

            def __setitem__(self, offset, value):
                self.last_offset += 4096
                if offset != self.last_offset or value != 1 or offset >= self.size:
                    raise AssertionError('logical page touch differs')
                self.touches += 1

        def read(descriptor, maximum):
            self.assertEqual(descriptor, 99)
            self.assertEqual(maximum, 1024)
            state.reads += 1
            value = bytes(state.frames[:maximum])
            del state.frames[:maximum]
            return value

        def ready(*_):
            if timeout:
                state.handler()
            if not state.frames:
                raise AssertionError('Guest attempted to wait beyond the supplied bounded controls')
            return [99], [], []

        fake_select = SimpleNamespace(select=ready)
        fake_signal = SimpleNamespace(SIGALRM=14,
                                      signal=lambda _, handler: setattr(state, 'handler', handler),
                                      alarm=state.alarms.append)
        fake_os = SimpleNamespace(O_RDWR=2, O_NONBLOCK=4, read=read,
                                  open=lambda path, flags: 99 if path == '/dev/virtio-ports/org.harniqo.m0' else self.fail(path))
        environment = {'C': INSTANCE, 'json': json, 'os': fake_os,
                       '__builtins__': {**vars(builtins), 'bytearray': LogicalBuffer,
                                        'print': lambda *args, **kwargs: state.output.append(' '.join(map(str, args)))}}
        code = compile(oom.guest_program(), '<bounded-host-oom-guest>', 'exec')
        with patch.dict(sys.modules, {'select': fake_select, 'signal': fake_signal}):
            try:
                exec(code, environment)
            except (RuntimeError, TimeoutError) as error:
                state.error = error
            else:
                self.fail('Guest fixture must terminate at a finite bound')
        return state

    def test_guest_source_compiles_and_control_steps_touch_logical_pages_only(self):
        tokens = [format(index, '040x') for index in range(1, oom.MAX_STEPS + 1)]
        state = self.execute_guest(('\n'.join(tokens) + '\n').encode())
        self.assertIn('ceiling reached', str(state.error))
        self.assertEqual(state.alarms, [100])
        self.assertEqual(len(state.blocks), oom.MAX_STEPS)
        self.assertTrue(all(block.size == oom.CHUNK_BYTES and block.touches == oom.CHUNK_BYTES // 4096
                            for block in state.blocks))
        frames = [json.loads(line.split(' ', 2)[2]) for line in state.output if '-PROGRESS ' in line]
        self.assertEqual([row['token'] for row in frames], tokens)
        self.assertEqual([row['sequence'] for row in frames], list(range(1, oom.MAX_STEPS + 1)))
        self.assertEqual(frames[-1]['allocated_bytes'], oom.qualification_limits()['guest_touch_ceiling_bytes'])

    def test_extra_control_in_a_batch_cannot_allocate_a_twenty_sixth_chunk(self):
        state = self.execute_guest(((TOKEN + '\n') * (oom.MAX_STEPS + 1)).encode())
        self.assertEqual(len(state.blocks), oom.MAX_STEPS)
        self.assertIn('allocation bound', str(state.error))

    def test_malformed_or_unterminated_oversized_controls_never_allocate(self):
        for frames in (b'bad-token\n', (TOKEN.upper() + '\n').encode(), b'x' * 4097):
            with self.subTest(frames=frames[:45]):
                state = self.execute_guest(frames)
                self.assertEqual(state.blocks, [])
                self.assertIn('bound' if len(frames) > 4096 else 'token differs', str(state.error))

    def test_waiting_guest_has_independent_alarm_before_any_logical_allocation(self):
        state = self.execute_guest(b'', timeout=True)
        self.assertIsInstance(state.error, TimeoutError)
        self.assertEqual(state.blocks, [])
        self.assertEqual(state.alarms, [100])


class PreflightTests(unittest.TestCase):
    def setUp(self):
        directory = '/private/tmp' if Path('/private/tmp').is_dir() else None
        self.temporary = tempfile.TemporaryDirectory(prefix='hnq-host-oom-test-', dir=directory)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.reports = self.root / 'reports'
        self.reports.mkdir(mode=0o700)

    def preflight(self, *, mode=0o700, free_gib=60, memory_gib=10, uid=None, symlink=False, raw=None):
        self.reports.chmod(mode)
        actual_uid = os.geteuid()
        caller_uid = actual_uid if actual_uid != 0 else 1000
        info = self.reports.lstat()
        original_lstat = Path.lstat

        def lstat(path):
            if path == self.reports:
                return SimpleNamespace(st_mode=info.st_mode, st_uid=caller_uid if uid is None else uid)
            return original_lstat(path)

        original_read_text = Path.read_text

        def read_text(path, *args, **kwargs):
            if path == Path('/proc/meminfo'):
                return 'MemTotal: 16777216 kB\nMemAvailable: ' + str(memory_gib * 1024**2) + ' kB\n'
            return original_read_text(path, *args, **kwargs)

        result = {}
        with patch.object(oom.os, 'geteuid', return_value=caller_uid), \
                patch.object(oom.os, 'pidfd_open', create=True), \
                patch.object(oom.os, 'statvfs', return_value=SimpleNamespace(f_bavail=free_gib * 1024**3, f_frsize=1)), \
                patch.object(oom.Path, 'lstat', lstat), patch.object(oom.Path, 'read_text', read_text), \
                patch.object(oom, '_boot_id', return_value=BOOT), \
                patch.object(oom, '_journal', return_value=encode([record()]) if raw is None else raw) as journal:
            if symlink:
                with patch.object(oom.kvm, 'no_symlinks', side_effect=RuntimeError('symlink rejected')):
                    oom.preflight(self.root, self.reports, result)
            else:
                oom.preflight(self.root, self.reports, result)
        return result, journal

    def test_private_preflight_persists_exact_mock_journal_anchor(self):
        result, journal = self.preflight()
        path = self.reports / 'host-oom-journal-preflight.jsonl'
        raw = encode([record()])
        self.assertEqual(path.read_bytes(), raw)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        anchor = result['host_qemu_oom_journal_anchor']
        self.assertEqual(anchor['journal_sha256'], hashlib.sha256(raw).hexdigest())
        self.assertEqual((anchor['boot_id'], anchor['cursor']), (BOOT, 'opaque-cursor-1'))
        self.assertEqual(anchor['report_directory'], str(self.reports))
        journal.assert_called_once()

    def test_wrong_directory_mode_or_owner_is_rejected_before_journal_read(self):
        for kwargs in ({'mode': 0o755}, {'mode': 0o777}, {'uid': 99999}, {'symlink': True}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(RuntimeError):
                    self.preflight(**kwargs)
        self.assertEqual(list(self.reports.iterdir()), [])

    def test_low_disk_or_available_memory_cannot_start_fault_qualification(self):
        for kwargs in ({'free_gib': 49}, {'memory_gib': 7}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaisesRegex(RuntimeError, 'reserve'):
                    self.preflight(**kwargs)
        self.assertEqual(list(self.reports.iterdir()), [])

    def test_preflight_requires_exactly_one_readable_current_boot_anchor(self):
        for raw in (b'', encode([record(), record(cursor='second-cursor')]),
                    encode([record(_BOOT_ID='f' * 32)])):
            with self.subTest(raw=raw):
                with self.assertRaises(RuntimeError):
                    self.preflight(raw=raw)
        self.assertEqual(list(self.reports.iterdir()), [])

    def test_root_execution_is_rejected_without_reading_the_host(self):
        with patch.object(oom.os, 'geteuid', return_value=0), \
                patch.object(oom, '_journal') as journal:
            with self.assertRaisesRegex(RuntimeError, 'Ordinary Linux'):
                oom.preflight(self.root, self.reports, {})
            journal.assert_not_called()


if __name__ == '__main__':
    unittest.main()
