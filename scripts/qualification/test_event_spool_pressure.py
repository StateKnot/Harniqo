"""Fault operator decisions only; no Node worker, VM, listener or host quota."""
from dataclasses import replace
import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

import event_spool_pressure as pressure
import qualify_resource_broker as operator
from apps.runner.resource_broker import Observation, StopEvidence, BrokerError


def stats(events=None):
    events = events or {}
    return {
        'pending_records': len(events), 'pending_bytes': sum(len(x) for x in events.values()),
        'database_bytes': 16384, 'wal_bytes': 0, 'shm_bytes': 0,
        'page_count': 4, 'database_bound_bytes': pressure.LIMITS['max_pages'] * 4096,
        'wal_bound_bytes': 32 + (pressure.LIMITS['max_pages'] + 32) * 4120,
        'committed_through': 0, 'contiguous_received_through': len(events),
        'highest_received': len(events), 'gaps': [], 'failure_code': None,
        'node_version': 'v24.22.1', 'sqlite_version': '3.53.4',
        'sqlite_source_id': pressure.SQLITE_SOURCE_ID,
        'sqlite_compile_options': ['THREADSAFE=1'],
        'sqlite_compile_options_sha256': hashlib.sha256(b'["THREADSAFE=1"]').hexdigest(),
        'scope_sha256': 'a' * 64, 'configuration_sha256': 'b' * 64,
        'limits': dict(pressure.LIMITS),
        'pragmas': {'locking_mode': 'exclusive', 'journal_mode': 'wal', 'synchronous': 2,
            'page_size': 4096, 'cache_spill': 0, 'cache_size': pressure.LIMITS['max_pages'],
            'temp_store': 2, 'trusted_schema': 0, 'wal_autocheckpoint': 0,
            'mmap_size': 0, 'max_page_count': pressure.LIMITS['max_pages']},
    }


class FakeSpool:
    def __init__(self, *, fail_at=None, failure='storage_full', uncertain=False):
        self.events = {}
        self.fail_at = fail_at
        self.failure = failure
        self.uncertain = uncertain
        self.records = []

    def _record(self, value): self.records.append(value)

    def request(self, operation, **parameters):
        if operation == 'append':
            seq = parameters['source_seq']
            if seq == self.fail_at:
                return {'ok': False, 'code': self.failure, 'mayHaveCommitted': self.uncertain,
                        'stats': stats(self.events)}
            self.events[seq] = base64.b64decode(parameters['body'], validate=True)
            return {'ok': True, 'receipt': {'level': 'durable_received', 'source_seq': seq,
                                          'duplicate': False}, 'stats': stats(self.events)}
        if operation == 'batch':
            events = [{'source_seq': seq, 'body': base64.b64encode(body).decode(),
                       'sha256': hashlib.sha256(body).hexdigest()}
                      for seq, body in sorted(self.events.items())]
            return {'ok': True, 'batch': {**stats(self.events), 'events': events}, 'stats': stats(self.events)}
        raise AssertionError('Unexpected native operation')


class EventSpoolFixtureTests(unittest.TestCase):
    def test_fault_is_opt_in_and_control_guest_keeps_independent_lifetime(self):
        a = {'uuid': str(uuid.uuid4()), 'nonce': 'a'}
        a['event_scope'] = pressure.new_scope(a['uuid'])
        b = {'uuid': str(uuid.uuid4())}
        def generate(**kwargs):
            return json.loads(operator.sealed_guest_config(a, b, Path('/workspace'),
                resource_profile='sealed-raw8g-q35-limited-v2', **kwargs).split('\n', 1)[1])['bootcmd'][0][2]
        default = generate(pressure=True)
        fault = generate(pressure=True, event_spool_pressure_mode=True)
        control = generate(pressure=False, event_spool_pressure_mode=True)
        self.assertNotIn('HARNIQO-EVENT-SPOOL-READY', default)
        self.assertNotIn('HARNIQO-EVENT-SPOOL-READY', control)
        self.assertIn('HARNIQO-EVENT-SPOOL-READY', fault)
        self.assertIn('until=time.monotonic()+120', control)
        self.assertIn('signal.alarm(100)', fault)
        self.assertIn('range(1,33)', fault)
        self.assertIn("'type':'text.delta'", fault)
        compile(fault, '<event-spool-Guest>', 'exec')

    def test_conflicting_modes_and_legacy_profile_fail_before_preparation(self):
        for memory, console, profile in ((True, False, 'sealed-raw8g-q35-limited-v2'),
                                         (False, True, 'sealed-raw8g-q35-limited-v2'),
                                         (False, False, 'sealed-raw8g-q35-v1')):
            args = SimpleNamespace(timeout=120, guest_memory_pressure=memory,
                console_pressure=console, event_spool_pressure=True, resource_profile=profile)
            with patch.object(operator.sys, 'platform', 'linux'), \
                 patch.object(operator.platform, 'machine', return_value='x86_64'), \
                 patch.object(operator.os, 'geteuid', return_value=1000), \
                 patch.object(operator.sys, 'version_info', (3, 12)), \
                 patch.object(operator, 'check_base') as prepare:
                with self.assertRaises(RuntimeError): operator.execute(args)
                prepare.assert_not_called()

    def test_native_limits_are_frozen_without_filesystem_or_commit_claim(self):
        with tempfile.TemporaryDirectory() as folder:
            result = {}
            operator.freeze_qualification_limits(Path(folder), result, event_spool_mode=True)
            manifest = Path(folder) / 'qualification-limits.json'
            self.assertEqual(json.loads(manifest.read_text()), pressure.qualification_limits())
            self.assertEqual(result['qualification_limits_sha256'], hashlib.sha256(manifest.read_bytes()).hexdigest())
            self.assertFalse(result['qualification_limits']['generic_filesystem_quota'])
            self.assertFalse(result['qualification_limits']['control_plane_commit'])
            self.assertGreater(pressure.LIMITS['max_pending_bytes'], pressure.LIMITS['max_pages'] * 4096)

    def test_native_stats_reject_wrong_engine_caps_commit_gap_and_scalar_type(self):
        self.assertEqual(pressure.validate_stats(stats()), stats())
        changes = ({'sqlite_source_id': 'another-build'}, {'sqlite_version': '3.45.0'},
                   {'node_version': 'v24.0.0'}, {'database_bytes': 200000},
                   {'wal_bytes': 1000000}, {'shm_bytes': 65536}, {'page_count': 33},
                   {'committed_through': 1}, {'gaps': [{'from': 1, 'through': 3}]},
                   {'contiguous_received_through': 1}, {'pending_records': False},
                   {'limits': {**pressure.LIMITS, 'max_pages': 64}})
        for change in changes:
            with self.subTest(change=change), self.assertRaises(RuntimeError):
                pressure.validate_stats({**stats(), **change})

    def test_split_guest_frame_preserves_exact_bytes_and_owner(self):
        scope = pressure.new_scope(str(uuid.uuid4()))
        body = json.dumps(pressure.source_event(scope, 1, 'E' * 64), separators=(',', ':')).encode()
        token = 'a' * 40
        frame = (json.dumps({'challenge': token, 'body': base64.b64encode(body).decode()}) + '\n').encode()
        chunks = [frame[:13], frame[13:]]
        channel = SimpleNamespace(recv=lambda _: chunks.pop(0))
        returned = pressure._guest_body(channel, SimpleNamespace(_poll=lambda _: None), scope, 1, token, pressure.time.monotonic() + 3)
        self.assertEqual(returned, body)

    def test_guest_wrong_challenge_unsolicited_data_and_scope_are_rejected(self):
        scope = pressure.new_scope(str(uuid.uuid4()))
        token = 'a' * 40
        event = pressure.source_event(scope, 1, 'E' * 64)
        cases = [({'challenge': 'b' * 40, 'body': base64.b64encode(json.dumps(event).encode()).decode()}, b''),
                 ({'challenge': token, 'body': base64.b64encode(json.dumps(event).encode()).decode()}, b'unsolicited'),
                 ({'challenge': token, 'body': base64.b64encode(json.dumps({**event, 'attempt_id': str(uuid.uuid4())}).encode()).decode()}, b'')]
        for frame, tail in cases:
            channel = SimpleNamespace(recv=lambda _: (json.dumps(frame) + '\n').encode() + tail)
            with self.assertRaises(RuntimeError):
                pressure._guest_body(channel, SimpleNamespace(_poll=lambda _: None), scope, 1, token, pressure.time.monotonic() + 3)

    def test_late_pipe_data_cannot_satisfy_a_reply_deadline(self):
        worker = pressure.NativeSpool.__new__(pressure.NativeSpool)
        worker._pending = bytearray(b'{"ok":true}\n')
        with patch.object(pressure.time, 'monotonic', side_effect=(0, 2)):
            with self.assertRaises(TimeoutError): worker._readline(1)

    def test_close_escalates_only_owned_process_and_log_failure_does_not_skip_exit(self):
        actions = []
        class Process:
            pid = 100
            stdin = stdout = None
            returncode = None
            def poll(self): return self.returncode
            def terminate(self): actions.append('terminate')
            def kill(self): actions.append('kill'); self.returncode = -9
            def wait(self, timeout):
                if self.returncode is None: raise subprocess.TimeoutExpired('owned', timeout)
                actions.append('exit'); return self.returncode
        class Log:
            def flush(self): raise OSError('controlled')
            def close(self): actions.append('log_closed')
        worker = pressure.NativeSpool.__new__(pressure.NativeSpool)
        worker.closed = False; worker.process = Process(); worker._stderr = Log()
        worker.request = lambda *_: (_ for _ in ()).throw(TimeoutError('controlled'))
        errors = worker.close()
        self.assertEqual(actions, ['terminate', 'kill', 'exit', 'log_closed'])
        self.assertTrue(worker.closed)
        self.assertEqual([e['phase'] for e in errors], ['event_spool_close', 'event_spool_log_sync'])
        self.assertEqual(worker.close(), [])

    def test_rejected_startup_reaps_exit_two_without_sending_rpc(self):
        for initial_code in (None, 2, 1):
            actions = []
            class Process:
                stdin = stdout = None
                returncode = initial_code
                def poll(self): return self.returncode
                def wait(self, timeout):
                    actions.append('wait')
                    if self.returncode is None: self.returncode = 2
                    return self.returncode
            worker = pressure.NativeSpool.__new__(pressure.NativeSpool)
            worker.closed = False; worker.process = Process(); worker._stderr = None
            worker.startup_failure = {'kind': 'startup_failed', 'code': 'unsafe_path'}
            worker.request = lambda *_: self.fail('Rejected worker must not receive a close request')
            errors = worker.close()
            self.assertTrue(worker.closed)
            self.assertEqual(actions, ['wait'])
            self.assertEqual(errors, [] if initial_code != 1 else
                             [{'phase': 'event_spool_close', 'type': 'RuntimeError'}])

    def test_data_root_is_required_only_for_opt_in_spool_mode(self):
        for spool, directory in ((True, None), (False, Path('/private/data'))):
            args = SimpleNamespace(timeout=120, guest_memory_pressure=False,
                console_pressure=False, event_spool_pressure=spool,
                spool_data_root=directory, resource_profile='sealed-raw8g-q35-limited-v2')
            with patch.object(operator.sys, 'platform', 'linux'), \
                 patch.object(operator.platform, 'machine', return_value='x86_64'), \
                 patch.object(operator.os, 'geteuid', return_value=1000), \
                 patch.object(operator.sys, 'version_info', (3, 12)), \
                 patch.object(operator, 'check_base') as prepare:
                with self.assertRaisesRegex(RuntimeError, 'spool-data-root'): operator.execute(args)
                prepare.assert_not_called()


class EventSpoolPrivateDataTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=Path('/tmp').resolve())
        self.addCleanup(self.temporary.cleanup)
        self.parent = Path(self.temporary.name)

    def test_new_private_data_root_has_frozen_identity_and_exclusive_runs(self):
        root = pressure.prepare_data_root(self.parent / 'data')
        self.assertEqual(root['mode'], 0o700)
        self.assertEqual(pressure.verify_data_root(root), self.parent / 'data')
        run = pressure.create_data_run(root, 'event-spool-native-a')
        self.assertEqual(pressure.verify_data_run(root, run), self.parent / 'data/event-spool-native-a')
        with self.assertRaises(FileExistsError): pressure.create_data_run(root, 'event-spool-native-a')
        with self.assertRaises(RuntimeError): pressure.create_data_run(root, '../escape')
        foreign = pressure.prepare_data_root(self.parent / 'foreign')
        with self.assertRaises(RuntimeError): pressure.verify_data_run(root, foreign)

    def test_shared_project_ancestor_is_rejected_without_repair(self):
        project = self.parent / 'project'; project.mkdir(mode=0o775); project.chmod(0o775)
        with self.assertRaisesRegex(RuntimeError, 'ancestry'):
            pressure.prepare_data_root(project / 'state')
        self.assertFalse((project / 'state').exists())
        self.assertEqual(stat.S_IMODE(project.stat().st_mode), 0o775)
        private = pressure.prepare_data_root(self.parent / 'separate')
        self.assertEqual(private['mode'], 0o700)

    def test_existing_public_leaf_and_symlink_are_rejected_without_repair(self):
        leaf = self.parent / 'leaf'; leaf.mkdir(mode=0o750); leaf.chmod(0o750)
        with self.assertRaises(RuntimeError): pressure.prepare_data_root(leaf)
        self.assertEqual(stat.S_IMODE(leaf.stat().st_mode), 0o750)
        link = self.parent / 'link'; link.symlink_to(leaf)
        with self.assertRaises(RuntimeError): pressure.prepare_data_root(link)
        with self.assertRaises(RuntimeError): pressure.prepare_data_root(Path('relative'))
        with self.assertRaises(RuntimeError): pressure.prepare_data_root('/tmp/../escape')

    def test_replaced_or_permission_changed_data_directory_is_rejected(self):
        root = pressure.prepare_data_root(self.parent / 'data')
        path = Path(root['path']); path.rename(self.parent / 'old')
        path.mkdir(mode=0o700)
        with self.assertRaises(RuntimeError): pressure.verify_data_root(root)
        fresh = pressure.directory_identity(path); path.chmod(0o750)
        with self.assertRaises(RuntimeError): pressure.verify_data_root(fresh)

    def test_retained_data_manifest_has_exact_bytes_and_refuses_unsafe_files(self):
        root = pressure.prepare_data_root(self.parent / 'data')
        run = pressure.create_data_run(root, 'qualification-a')
        store = Path(run['path']) / 'store'; store.mkdir(mode=0o700)
        database = store / 'spool.sqlite'; database.write_bytes(b'native-data'); database.chmod(0o600)
        manifest = pressure.data_tree_manifest(root, run)
        self.assertEqual(manifest['bytes'], 11)
        self.assertEqual(manifest['file_count'], 1)
        self.assertEqual(manifest['files'][0]['relative_path'], 'store/spool.sqlite')
        self.assertEqual(manifest['files'][0]['sha256'], hashlib.sha256(b'native-data').hexdigest())
        with self.assertRaises(RuntimeError): pressure.data_tree_manifest(root, run, max_bytes=10)
        os.link(database, store / 'hardlink')
        with self.assertRaises(RuntimeError): pressure.data_tree_manifest(root, run)
        (store / 'hardlink').unlink()
        link = store / 'symlink'; link.symlink_to(database)
        with self.assertRaises(RuntimeError): pressure.data_tree_manifest(root, run)


class EventSpoolPressureDecisions(unittest.TestCase):
    def invoke(self, failure='storage_full', uncertain=False, fail_at=6, generation=2):
        a, b = [{'uuid': str(uuid.uuid4()), 'nonce': label} for label in ('a', 'b')]
        for instance in (a, b): instance['event_scope'] = pressure.new_scope(instance['uuid'])
        wa = FakeSpool(fail_at=fail_at, failure=failure, uncertain=uncertain)
        wb = FakeSpool()
        streams = SimpleNamespace(report_channels={a['uuid']: object()}, _poll=lambda _: None)
        sample = {'pid': 100, 'process_start_time': 1000, 'cgroup_path': 'owned'}
        broker = SimpleNamespace(resource_limits=lambda _: dict(sample),
                                 observe=lambda _: SimpleNamespace(generation=generation))
        result = {'checks': {}}
        def body(_channel, _streams, scope, seq, _token, _deadline):
            return json.dumps(pressure.source_event(scope, seq, 'E' * (64 if seq <= 3 else 8192))).encode()
        with patch.object(pressure.protocol, '_wait_until'), patch.object(pressure.protocol, '_send'), \
             patch.object(pressure.protocol, 'challenge', side_effect=lambda *_args, **_kwargs: uuid.uuid4().hex + '1' * 8), \
             patch.object(pressure, '_guest_body', side_effect=body):
            pressure.run_pressure(broker, (a, b), streams, result, (wa, wb))
            pressure.after_stop((a, b), streams, result, (wa, wb))
        return result, wa, wb

    def test_real_capacity_class_requires_durable_stages_and_preserves_all_rows(self):
        result, a, b = self.invoke()
        self.assertEqual(len(a.events), 5)
        self.assertEqual(len(b.events), 5)
        self.assertEqual(len(result['event_spool_pressure']['stages']), 3)
        self.assertEqual(result['event_spool_pressure']['failure']['reply']['code'], 'storage_full')
        self.assertTrue(all(result['checks'].values()))

    def test_logical_cap_unknown_commit_and_failure_before_three_stages_do_not_pass(self):
        for kwargs in ({'failure': 'queue_full'}, {'uncertain': True}, {'fail_at': 2}):
            with self.subTest(kwargs=kwargs), self.assertRaises(RuntimeError): self.invoke(**kwargs)

    def test_fixture_does_not_accept_a_different_local_resource_generation(self):
        with self.assertRaisesRegex(RuntimeError, 'generation differs'): self.invoke(generation=1)

    def test_missing_or_changed_unacked_event_never_passes_retention(self):
        worker = FakeSpool()
        worker.events[1] = b'one'
        expected = [{'source_seq': 1, 'bytes': 3, 'sha256': hashlib.sha256(b'one').hexdigest()}]
        self.assertEqual(pressure._verify_retained(worker, expected, pressure.time.monotonic() + 3, None)['pending_records'], 1)
        for wanted in ([], [{**expected[0], 'sha256': '0' * 64}]):
            with self.assertRaises(RuntimeError): pressure._verify_retained(worker, wanted, pressure.time.monotonic() + 3, None)


class EventSpoolBrokerStopTests(unittest.TestCase):
    def invoke(self, *, method='libvirt_destroy_then_absent', cause='event_spool_capacity_exceeded',
               mismatch=False, resource_id='a', revived=False, old_accepted=False):
        with tempfile.TemporaryDirectory() as folder:
            disk = Path(folder) / 'owned.raw'; disk.write_bytes(b'owned')
            stopped = Observation(resource_id, 'stopped', 3, 1,
                StopEvidence(resource_id, 101, method, 'f' * 64), stop_cause=cause)
            rebuilt = replace(stopped, generation=4) if mismatch else stopped
            calls = []
            class Broker:
                def revoke(self, identity, why): calls.append((identity, why)); return stopped
            class Rebuilt:
                def observe(self, _): return rebuilt
                def ensure(self, _):
                    if old_accepted: return stopped
                    raise BrokerError('generation_stale')
                def renew(self, _): raise BrokerError('resource_terminal')
            result = {'checks': {}, 'observations': {}}
            with patch.object(operator.time, 'time', return_value=100), \
                 patch.object(operator, 'broker_from', return_value=Rebuilt()), \
                 patch.object(operator.kvm, 'domain_ids', return_value={'a'} if revived else set()):
                returned = operator.stop_event_spool_resource(Broker(), {}, {'uuid': 'a', 'overlay': disk}, result, object(), object())
            self.assertEqual(calls, [('a', 'event_spool_capacity_exceeded')])
            self.assertEqual(returned[1], stopped)
            self.assertTrue(all(result['checks'].values()))
            return result

    def test_formal_stop_tombstone_and_old_authority_rejection(self): self.invoke()

    def test_unproven_stop_wrong_reason_resource_revival_and_old_authority_fail(self):
        for kwargs in ({'method': 'libvirt_absent_after_confirmed_start'},
                       {'cause': 'requested_revoke'}, {'resource_id': 'foreign'},
                       {'mismatch': True}, {'revived': True}, {'old_accepted': True}):
            with self.subTest(kwargs=kwargs), self.assertRaises(RuntimeError): self.invoke(**kwargs)


if __name__ == '__main__': unittest.main()
