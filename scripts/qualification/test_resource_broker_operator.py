"""Host filesystem guardrails only; no VM, native libvirt, model or privilege use."""
from pathlib import Path
import fcntl
import hashlib
import json
import os
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

import qualify_resource_broker as operator


class GuestBootstrapTests(unittest.TestCase):
    def test_sealed_probe_runs_early_without_a_second_final_stage_invocation(self):
        instance = {'uuid': 'instance-a', 'nonce': 'nonce-a'}
        other = {'uuid': 'instance-b'}
        root = Path('/qualification/workspace')
        original = json.loads(operator.kvm.guest_config(instance, other, root, operator.DISK_BYTES).split('\n', 1)[1])
        config = json.loads(operator.sealed_guest_config(instance, other, root).split('\n', 1)[1])
        self.assertEqual(config['bootcmd'], [['/usr/bin/python3', '-c', original['write_files'][0]['content']]])
        self.assertNotIn('runcmd', config)
        self.assertNotIn('write_files', config)
        for field in ('package_update', 'package_upgrade', 'resize_rootfs'):
            self.assertIs(config[field], False)


    def test_limited_guest_fixture_keeps_sealed_checks_and_separates_load_from_heartbeat(self):
        instance = {'uuid': 'instance-a', 'nonce': 'nonce-a'}
        other = {'uuid': 'instance-b'}
        root = Path('/qualification/workspace')
        original = json.loads(operator.kvm.guest_config(instance, other, root, operator.DISK_BYTES).split('\n', 1)[1])['write_files'][0]['content']
        programs = []
        for pressure in (True, False):
            config = json.loads(operator.sealed_guest_config(instance, other, root,
                resource_profile='sealed-raw8g-q35-limited-v2', pressure=pressure).split('\n', 1)[1])
            program = config['bootcmd'][0][2]
            self.assertTrue(program.startswith(original))
            compile(program, '<sealed-resource-guest>', 'exec')
            self.assertNotIn('runcmd', config)
            programs.append(program[len(original):])
        self.assertIn('while time.monotonic()<until: pass', programs[0])
        self.assertNotIn('HARNIQO-HEARTBEAT', programs[0])
        self.assertIn('HARNIQO-HEARTBEAT', programs[1])
        self.assertIn("re.fullmatch(b'[0-9a-f]{40}',line)", programs[1])
        self.assertIn('select.select', programs[1])


class WorkerLogCleanupTests(unittest.TestCase):
    def test_sync_failure_is_reported_without_skipping_other_log_closure(self):
        logs = [tempfile.TemporaryFile(mode='w+'), tempfile.TemporaryFile(mode='w+')]
        for log in logs:
            self.addCleanup(log.close)
            log.write('private diagnostic content')
        with patch.object(operator.os, 'fsync', side_effect=[OSError('private filesystem detail'), None]) as sync:
            errors = operator.close_worker_logs(logs)
        self.assertTrue(all(log.closed for log in logs))
        self.assertEqual(sync.call_count, 2)
        self.assertEqual(errors, [{'phase': 'worker_log_sync', 'type': 'OSError'}])


class ServiceProcessQualificationTests(unittest.TestCase):
    """Operator grammar, routing and process receipts only; no VM or service."""
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=Path('/private/tmp' if sys.platform == 'darwin' else '/tmp').resolve())
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.reports = self.root / 'resource-broker-fixture'; self.reports.mkdir(mode=0o700)
        self.configuration_root = self.root / 'private-config'; self.configuration_root.mkdir(mode=0o700)
        info = self.configuration_root.stat(); self.root_identity = (info.st_dev, info.st_ino)
        self.config = {'state_dir': str(self.root/'runtime/broker'),
            'issuer_key_file': str(self.reports/'issuer-key'), 'images_root': str(self.root/'images'),
            'channel_root': str(self.root/'runtime'), 'templates': [], 'assignments': []}
        for index, label in enumerate(('a', 'b'), 1):
            identity = '00000000-0000-4000-8000-' + str(index).zfill(12)
            self.config['templates'].append({'template_id': 'sealed-'+label, 'resource_id': identity,
                'name': 'harniqo-rb-fixture-'+label, 'disk': str(self.root/'images'/(label+'.raw')),
                'seed': str(self.root/'images'/(label+'.img')), 'channel': str(self.root/'runtime'/(label+'.sock')),
                'console': str(self.root/'runtime'/(label+'-console.sock')), 'source_base_sha256': 'a'*64,
                'resource_profile': 'sealed-raw8g-q35-limited-v2'})
            self.config['assignments'].append({'resource_id': identity,
                'organization_id': 'org-'+label, 'workspace_id': 'workspace-'+label})

    def prepare(self):
        # Only serialization is under test here. Actual unsafe shared /tmp
        # ancestry is exercised separately and cannot qualify deployment.
        with patch.object(operator, 'service_configuration_root', return_value=self.root_identity):
            return operator.prepare_service(self.config, self.reports,
                self.configuration_root, self.root_identity)

    def event(self, role='serve', **changes):
        service = self.prepare()
        value = dict(schema_version=1, event='ready', role=role, pid=123, status='operational')
        value.update(changes)
        operator.save(self.reports/'service-test.stdout', value)
        process = SimpleNamespace(pid=123, poll=lambda: None)
        result = {'source_sha256': {name: 'b'*64 for name in operator.SERVICE_SOURCES}}
        return service, process, result, value

    def test_service_configuration_matches_real_grammar_and_pins_two_scopes(self):
        from apps.runner.resource_broker.service import parse_configuration
        service = self.prepare()
        path, copy = Path(service['config']), Path(service['evidence_config'])
        self.assertEqual(path.read_bytes(), copy.read_bytes())
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), service['sha256'])
        parsed = parse_configuration(path.read_bytes())
        self.assertEqual([t.resource_id for t in parsed.templates],
                         [t['resource_id'] for t in self.config['templates']])
        self.assertEqual(parsed.broker_identity.uid, os.getuid())
        self.assertEqual(parsed.broker_identity.gid, os.getgid())
        self.assertEqual(len(parsed.peers[0].resources), 2)
        self.assertTrue(all(access.operations == {'ensure', 'renew', 'observe', 'revoke'}
                            for access in parsed.peers[0].resources))
        self.assertNotIn('assignments', json.loads(path.read_bytes()))
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.configuration_root.stat().st_mode), 0o700)
        with self.assertRaises(FileExistsError):
            self.prepare()

    def test_configuration_root_rejects_writable_ancestors_and_never_chmods(self):
        with self.assertRaisesRegex(RuntimeError, 'ancestor'):
            operator.service_configuration_root(self.configuration_root)
        self.configuration_root.chmod(0o750)
        with self.assertRaisesRegex(RuntimeError, 'Private directory'):
            operator.service_configuration_root(self.configuration_root)
        self.assertEqual(stat.S_IMODE(self.configuration_root.stat().st_mode), 0o750)
        with self.assertRaisesRegex(RuntimeError, 'absolute'):
            operator.service_configuration_root(Path('relative'))

    def test_actual_operational_cli_event_is_bound_to_pid_config_and_sources(self):
        service, process, result, value = self.event()
        self.assertEqual(operator.service_event(process, self.config, 'serve', 'service-test', result), value)
        receipt = json.loads((self.reports/'service-test-ready.json').read_bytes())
        self.assertEqual(receipt['actual_cli_event'], value)
        self.assertEqual(receipt['pid'], process.pid)
        self.assertEqual(receipt['configuration_sha256'], service['sha256'])
        self.assertEqual(receipt['sources_sha256'], result['source_sha256'])

    def test_degraded_wrong_identity_and_oversized_stdout_cannot_be_ready(self):
        for changes in ({'status': 'degraded', 'error_code': 'state_corrupt'},
                        {'pid': 124}, {'role': 'watch'}, {'schema_version': True}):
            with self.subTest(changes=changes):
                with tempfile.TemporaryDirectory(dir=self.root) as temporary:
                    prior = self.reports
                    self.reports = Path(temporary)
                    try:
                        _, process, result, _ = self.event(**changes)
                        with self.assertRaisesRegex(RuntimeError, 'readiness'):
                            operator.service_event(process, self.config, 'serve', 'service-test', result)
                        self.assertFalse((self.reports/'service-test-ready.json').exists())
                    finally:
                        self.reports = prior
        _, process, result, _ = self.event()
        path = self.reports/'service-test.stdout'; path.write_bytes(b'x'*4097)
        with self.assertRaisesRegex(RuntimeError, 'Private file'):
            operator.service_event(process, self.config, 'serve', 'service-test', result)

    def test_changed_pinned_config_cannot_create_a_readiness_receipt(self):
        service, process, result, _ = self.event()
        Path(service['config']).write_text('{}\n')
        with self.assertRaisesRegex(RuntimeError, 'configuration changed'):
            operator.service_event(process, self.config, 'serve', 'service-test', result)
        self.assertFalse((self.reports/'service-test-ready.json').exists())

    def test_spawn_owns_process_and_both_logs_before_event_waits(self):
        service = self.prepare()
        children, descriptors = [], []
        self.addCleanup(lambda: [stream.close() for stream in descriptors])
        process = SimpleNamespace(pid=123)
        def created(arguments, **kwargs):
            self.assertEqual(len(descriptors), 2)
            self.assertTrue(all(not stream.closed for stream in descriptors))
            self.assertEqual(arguments, [sys.executable, '-m', operator.SERVICE_MODULE, 'serve',
                '--config', service['config'], '--sha256', service['sha256']])
            self.assertEqual(kwargs['cwd'], self.root)
            return process
        with patch.object(operator.subprocess, 'Popen', side_effect=created):
            self.assertIs(operator.spawn_service(self.root, self.config, 'serve', 'owned', children, descriptors), process)
        self.assertEqual(children, [process])

    def test_four_operation_proxy_has_no_local_authority_fallback(self):
        invoked = []
        client = SimpleNamespace(**{name: lambda *args, name=name: invoked.append((name, args)) or name
                                    for name in ('ensure', 'renew', 'observe', 'revoke')})
        diagnostic = SimpleNamespace(resource_limits=lambda identity: ('diagnostic', identity))
        calls = {}
        with patch.object(operator, 'ipc_client', return_value=client):
            broker = operator.QualificationIPC({}, diagnostic, calls)
        self.assertEqual(broker.ensure('assignment'), 'ensure')
        self.assertEqual(broker.renew('grant'), 'renew')
        self.assertEqual(broker.observe('uuid'), 'observe')
        self.assertEqual(broker.revoke('uuid'), 'revoke')
        self.assertEqual(calls, dict(ensure=1, renew=1, observe=1, revoke=1))
        self.assertEqual(broker.resource_limits('uuid'), ('diagnostic', 'uuid'))
        self.assertEqual(len(invoked), 4)
        self.assertFalse(hasattr(broker, 'reconcile'))

    def test_only_service_mode_diagnostic_reconstruction_refuses_state_initialization(self):
        self.config['service'] = {'marker': True}
        native = unittest.mock.Mock()
        with patch.object(operator, 'api', return_value=(lambda **values: values, native)):
            operator.broker_from(self.config)
            self.assertIs(native.call_args.kwargs['initialize_state'], False)
            del self.config['service']
            operator.broker_from(self.config)
            self.assertNotIn('initialize_state', native.call_args.kwargs)

    def test_graceful_stop_requires_clean_exit_and_not_dispatched_admission_refusal(self):
        from apps.runner.resource_broker.ipc import IPCError
        process = SimpleNamespace(pid=123, returncode=0, poll=lambda: None,
                                  terminate=lambda: None, wait=lambda **kwargs: 0)
        for outcome in ('not_dispatched', 'unconfirmed'):
            def refused(_identity): raise IPCError('ipc_unavailable', outcome=outcome)
            client = SimpleNamespace(directory=self.reports, observe=refused)
            if outcome == 'not_dispatched':
                self.assertTrue(operator.stop_service(process, client, 'uuid')['admission_refused_before_dispatch'])
            else:
                with self.assertRaisesRegex(RuntimeError, 'admission refusal'):
                    operator.stop_service(process, client, 'uuid')

    def test_service_flag_requires_v2_exclusive_mode_and_explicit_config_root_before_allocation(self):
        base = dict(root=self.root, base_image=self.root/'base.qcow2', timeout=120,
            resource_profile='sealed-raw8g-q35-limited-v2', service_processes=True,
            service_config_root=None, guest_memory_pressure=False, console_pressure=False,
            event_spool_pressure=False, host_qemu_oom=False, spool_data_root=None)
        cases = [({}, 'service-config-root'),
                 ({'resource_profile': 'sealed-raw8g-q35-v1'}, 'limited-v2'),
                 ({'service_processes': False, 'service_config_root': self.configuration_root}, 'service-config-root')]
        cases += [({mode: True}, 'one fault or service') for mode in
                  ('guest_memory_pressure', 'console_pressure', 'event_spool_pressure', 'host_qemu_oom')]
        with patch.object(operator.sys, 'platform', 'linux'), \
                patch.object(operator.sys, 'version_info', (3, 12, 0)), \
                patch.object(operator.platform, 'machine', return_value='x86_64'), \
                patch.object(operator, 'coordination_lock') as allocated:
            for changes, message in cases:
                with self.subTest(changes=changes), self.assertRaisesRegex(RuntimeError, message):
                    operator.execute(SimpleNamespace(**(base | changes)))
        allocated.assert_not_called()


class ServiceLiveRevokeTests(unittest.TestCase):
    """Live/terminal receipt guard tests with no native backend or process."""
    def invoke(self, fault=None):
        from apps.runner.resource_broker import Observation, StopEvidence, sign_assignment
        from dataclasses import asdict, replace
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        reports = Path(temporary.name)
        key = b'k'*32
        identity, control = ('00000000-0000-4000-8000-00000000000' + str(i) for i in (1, 2))
        original = dict(resource_id=identity, organization_id='org-a', workspace_id='workspace-a',
            attempt_id='00000000-0000-4000-8000-000000000003', template_id='sealed-a',
            configuration_sha256='a'*64, generation=1, recovery_generation=1,
            issued_at=10, expires_at=250)
        first_stop = Observation(identity, 'stopped', 2, 1,
            StopEvidence(identity, 90, 'libvirt_destroy_then_absent', 'a'*64))
        initial_receipt = asdict(first_stop)
        instance = dict(uuid=identity, listener=object(), console_listener=object())
        selector = unittest.mock.Mock()
        streams = SimpleNamespace(logs={identity: {'stream_closed': True}}, selector=selector,
                                  report_channels={}, connections=set())
        second = SimpleNamespace(logs={identity: {'bytes_seen': 1}},
            mark_start=unittest.mock.Mock(), finish=unittest.mock.Mock())
        watch = SimpleNamespace(poll=lambda: None)
        result = {'checks': {'a_watch_deadline_stop': fault != 'unverified_first_stop',
                             'independent_watch_survives_launcher': True}}
        state = dict(live=False, ensure_calls=0, revoke_calls=0)
        extras = []
        class Broker:
            def ensure(inner, assignment):
                state['ensure_calls'] += 1
                self.assertIn(second, extras)  # The collector is owned before dispatch.
                fields = asdict(assignment); fields.pop('signature')
                self.assertEqual(assignment, sign_assignment(key, **fields))
                self.assertEqual((assignment.generation, assignment.recovery_generation), (3, 2))
                self.assertNotEqual(assignment.attempt_id, original['attempt_id'])
                self.assertEqual(assignment.expires_at - assignment.issued_at, 30)
                state['live'] = True
                if fault == 'terminal_ensure': return first_stop
                return Observation(identity, 'running', 2 if fault == 'old_epoch' else 3, 2)
            def resource_limits(inner, resource_id):
                self.assertEqual(resource_id, identity)
                return {'pid': 0 if fault == 'invalid_pid' else 123,
                        'process_start_time': 88, 'cgroup_path': '/sys/fs/cgroup/owned-qemu'}
            def revoke(inner, resource_id, cause):
                state['revoke_calls'] += 1
                self.assertTrue(state['live'])
                self.assertEqual((resource_id, cause), (identity, 'cancelled'))
                state['live'] = fault == 'host_remains'
                stop = StopEvidence(identity, 90 if fault == 'stale_stop' else 100,
                    'libvirt_absent_after_confirmed_start' if fault == 'wrong_method' else 'libvirt_destroy_then_absent',
                    ('b' if fault == 'wrong_config' else 'a')*64)
                return Observation(identity, 'stopped', 2 if fault == 'wrong_stop_epoch' else 3, 2,
                    stop, stop_cause='lease_expired_or_clock_changed' if fault == 'wrong_cause' else 'cancelled')
            def observe(inner, resource_id):
                self.assertEqual(resource_id, control)
                return Observation(control, 'running', 2, 1)
        def domains():
            return {control, identity} if state['live'] and fault != 'no_host' else {control}
        with patch.object(operator.kvm, 'GuestStreams', return_value=second), \
                patch.object(operator.kvm, 'domain_ids', side_effect=domains), \
                patch.object(operator, 'await_absent'), \
                patch.object(operator.time, 'time', return_value=100):
            operator.service_live_revoke(Broker(), key, original, first_stop,
                [instance, {'uuid': control}], streams, extras, reports, [], reports, watch, result)
        self.assertEqual(asdict(first_stop), initial_receipt)
        return result, state, second, selector

    def test_fresh_signed_epoch_proves_live_ipc_revoke_and_retains_original_stop(self):
        result, state, second, selector = self.invoke()
        self.assertEqual((state['ensure_calls'], state['revoke_calls']), (1, 1))
        self.assertTrue(result['checks']['service_live_revoke_confirmed'])
        evidence = result['service_live_revoke']
        self.assertTrue(evidence['host_uuid_present_before'])
        self.assertTrue(evidence['host_uuid_absent_after'])
        self.assertEqual(evidence['new_running']['resource_id'], evidence['same_fixed_resource_uuid'])
        self.assertGreater(evidence['revoke_after']['stop_evidence']['confirmed_at'],
                           evidence['first_epoch_stop']['stop_evidence']['confirmed_at'])
        self.assertFalse(evidence['second_boot_guest_health_verified'])
        self.assertEqual(selector.unregister.call_count, 2)
        second.finish.assert_called_once_with()

    def test_terminal_old_epoch_missing_live_host_and_stale_stop_cannot_pass(self):
        for fault in ('unverified_first_stop', 'terminal_ensure', 'old_epoch', 'no_host',
                      'invalid_pid', 'stale_stop', 'wrong_method', 'wrong_config',
                      'wrong_stop_epoch', 'wrong_cause', 'host_remains'):
            with self.subTest(fault=fault), self.assertRaises(RuntimeError):
                self.invoke(fault)


class CoordinationPathTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='hbro-', dir=Path('/private/tmp' if sys.platform == 'darwin' else '/tmp').resolve())
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.state = self.root / '.state'; self.state.mkdir(mode=0o775); self.state.chmod(0o775)
        self.legacy = self.state / 'qualification'; self.legacy.mkdir(mode=0o775); self.legacy.chmod(0o775)
        self.lock = self.legacy / 'kvm.lock'; self.lock.write_bytes(b'coordination-only\n'); self.lock.chmod(0o664)

    def test_legacy_shared_modes_are_kept_without_becoming_private(self):
        descriptor = operator.coordination_lock(self.root)
        self.addCleanup(os.close, descriptor)
        self.assertEqual(stat.S_IMODE(self.legacy.stat().st_mode), 0o775)
        self.assertEqual(stat.S_IMODE(self.lock.stat().st_mode), 0o664)
        self.assertEqual(self.lock.read_bytes(), b'coordination-only\n')
        with self.assertRaisesRegex(RuntimeError, 'Private directory'):
            operator.private_directory(self.legacy)
        competing = os.open(self.lock, os.O_RDWR)
        try:
            with self.assertRaises(BlockingIOError):
                fcntl.flock(competing, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(competing)

    def test_private_authority_parent_is_separate_and_0700(self):
        private = self.state / 'resource-broker-qualification'
        operator.private_directory(private, create=True)
        operator.private_directory(private / 'rb-task', create=True)
        self.assertEqual(stat.S_IMODE(private.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((private / 'rb-task').stat().st_mode), 0o700)
        self.assertNotEqual(private, self.legacy)
        self.assertEqual(stat.S_IMODE(self.legacy.stat().st_mode), 0o775)

    def test_existing_public_authority_parent_is_rejected_without_chmod(self):
        private = self.state / 'resource-broker-qualification'; private.mkdir(mode=0o775); private.chmod(0o775)
        with self.assertRaisesRegex(RuntimeError, 'Private directory'):
            operator.private_directory(private, create=True)
        self.assertEqual(stat.S_IMODE(private.stat().st_mode), 0o775)

    def test_existing_qemu_traverse_parent_does_not_weaken_authority(self):
        runtime = self.state / 'runtime'; runtime.mkdir(mode=0o700); runtime.chmod(0o710)
        acl = 'user::rwx\nuser:64055:--x\ngroup::---\nmask::--x\nother::---\n'
        with patch.object(operator.pwd, 'getpwnam', return_value=SimpleNamespace(pw_uid=64055)), \
                patch.object(operator.kvm, 'run', return_value=acl):
            operator.runtime_directory(runtime, create=True)
        self.assertEqual(stat.S_IMODE(runtime.stat().st_mode), 0o710)
        with self.assertRaisesRegex(RuntimeError, 'Private directory'):
            operator.private_directory(runtime)

    def test_runtime_parent_rejects_unsafe_acl_principals(self):
        runtime = self.state / 'runtime'; runtime.mkdir(mode=0o700); runtime.chmod(0o710)
        for acl in (
            'user::rwx\nuser:64056:--x\ngroup::---\nmask::--x\nother::---\n',
            'user::rwx\ngroup::--x\nmask::--x\nother::---\n',
            'user::rwx\nuser:64055:--x\nuser:1234:--x\ngroup::---\nmask::--x\nother::---\n',
        ):
            with self.subTest(acl=acl), \
                    patch.object(operator.pwd, 'getpwnam', return_value=SimpleNamespace(pw_uid=64055)), \
                    patch.object(operator.kvm, 'run', return_value=acl):
                with self.assertRaisesRegex(RuntimeError, 'named QEMU traverse'):
                    operator.runtime_directory(runtime, create=True)
        self.assertEqual(stat.S_IMODE(runtime.stat().st_mode), 0o710)

    def test_shared_runtime_mode_is_rejected_without_chmod(self):
        with self.assertRaisesRegex(RuntimeError, 'Runtime directory permissions'):
            operator.runtime_directory(self.legacy, create=True)
        self.assertEqual(stat.S_IMODE(self.legacy.stat().st_mode), 0o775)

    def test_symlink_runtime_parent_is_rejected(self):
        runtime = self.state / 'runtime'; runtime.symlink_to(self.legacy, target_is_directory=True)
        with self.assertRaisesRegex(RuntimeError, 'Symlink'):
            operator.runtime_directory(runtime, create=True)
        self.assertEqual(stat.S_IMODE(self.legacy.stat().st_mode), 0o775)


    def test_symlink_coordination_directory_is_rejected(self):
        retained = self.state / 'retained'
        self.legacy.rename(retained); self.legacy.symlink_to(retained, target_is_directory=True)
        with self.assertRaisesRegex(RuntimeError, 'Symlink'):
            operator.coordination_lock(self.root)
        self.assertEqual((retained / 'kvm.lock').read_bytes(), b'coordination-only\n')
        self.assertEqual(stat.S_IMODE(retained.stat().st_mode), 0o775)

    def test_symlink_coordination_lock_is_rejected(self):
        target = self.state / 'untouched'; target.write_bytes(b'do-not-change'); target.chmod(0o600)
        self.lock.unlink(); self.lock.symlink_to(target)
        with self.assertRaisesRegex(RuntimeError, 'Symlink'):
            operator.coordination_lock(self.root)
        self.assertEqual(target.read_bytes(), b'do-not-change')
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)

    def test_hardlinked_coordination_lock_is_rejected(self):
        extra = self.state / 'second-link'; os.link(self.lock, extra)
        with self.assertRaisesRegex(RuntimeError, 'coordination lock'):
            operator.coordination_lock(self.root)
        self.assertEqual(self.lock.stat().st_nlink, 2)
        self.assertEqual(extra.read_bytes(), b'coordination-only\n')

    def test_nonregular_coordination_lock_is_rejected(self):
        self.lock.unlink(); os.mkfifo(self.lock, 0o600)
        with self.assertRaisesRegex(RuntimeError, 'coordination lock'):
            operator.coordination_lock(self.root)
        self.assertTrue(stat.S_ISFIFO(self.lock.lstat().st_mode))

    def test_foreign_owner_coordination_path_is_rejected(self):
        uid = os.geteuid()
        with patch.object(operator.os, 'geteuid', return_value=uid + 1):
            with self.assertRaisesRegex(RuntimeError, 'coordination directory'):
                operator.coordination_lock(self.root)
        self.assertEqual(stat.S_IMODE(self.legacy.stat().st_mode), 0o775)
        self.assertEqual(stat.S_IMODE(self.lock.stat().st_mode), 0o664)

    def test_replaced_coordination_lock_cannot_claim_unchanged(self):
        descriptor = operator.coordination_lock(self.root)
        self.addCleanup(os.close, descriptor)
        original = self.legacy / 'original.lock'; self.lock.rename(original)
        self.lock.write_bytes(b'new-coordinator'); self.lock.chmod(0o664)
        with self.assertRaisesRegex(RuntimeError, 'lock was replaced'):
            operator.verify_coordination_lock(self.root, descriptor)
        self.assertEqual(original.read_bytes(), b'coordination-only\n')
        self.assertEqual(self.lock.read_bytes(), b'new-coordinator')

    def test_foreign_owned_base_still_requires_an_independent_owned_copy(self):
        (self.root / 'images/vm/base').mkdir(parents=True)
        (self.root / 'images/README.md').write_text('fixture')
        base = self.root / 'images/vm/base' / ('a' * 64 + '.qcow2')
        base.write_bytes(b'base'); base.chmod(0o444)
        uid = os.geteuid()
        with patch.object(operator.os, 'geteuid', return_value=uid + 1), patch.object(operator.kvm, 'run') as command:
            with self.assertRaisesRegex(RuntimeError, 'Base must be owned'):
                operator.check_base(self.root, base)
            command.assert_not_called()
        self.assertEqual(base.read_bytes(), b'base')
        self.assertEqual(stat.S_IMODE(base.stat().st_mode), 0o444)




class MemoryFailureModeTests(unittest.TestCase):
    def test_explicit_memory_fixture_does_not_change_default_guest_programs(self):
        instance = {'uuid': 'instance-a', 'nonce': 'nonce-a'}
        other = {'uuid': 'instance-b'}
        root = Path('/qualification/workspace')
        def generated(**kwargs):
            return json.loads(operator.sealed_guest_config(instance, other, root,
                resource_profile='sealed-raw8g-q35-limited-v2', **kwargs).split('\n', 1)[1])['bootcmd'][0][2]
        baseline = generated(pressure=True)
        memory = generated(pressure=True, guest_memory_pressure=True)
        control = generated(pressure=False, guest_memory_pressure=True)
        self.assertNotIn('guest_identity_guard', baseline)
        self.assertIn('guest_identity_guard', memory)
        self.assertIn('until=time.monotonic()+120', control)
        self.assertNotIn('MEMORY_WORKER=', control)
        compile(memory, '<memory-failure-fixture>', 'exec')

    def test_memory_budget_manifest_is_saved_with_matching_hash_before_execution(self):
        import hashlib
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            result = {}
            operator.freeze_qualification_limits(root, result)
            manifest = root / 'qualification-limits.json'
            self.assertEqual(result['qualification_limits'], operator.memory_pressure.qualification_limits())
            self.assertEqual(json.loads(manifest.read_text()), result['qualification_limits'])
            self.assertEqual(result['qualification_limits_manifest'], manifest.name)
            self.assertEqual(result['qualification_limits_sha256'], hashlib.sha256(manifest.read_bytes()).hexdigest())
            self.assertEqual(stat.S_IMODE(manifest.stat().st_mode), 0o600)

    def fault_fixture(self, *, wrong_uuid=False, rebuilt_mismatch=False, method='libvirt_absent_after_confirmed_start'):
        from apps.runner.resource_broker import BrokerError, Observation, StopEvidence
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        reports = Path(temporary.name)
        disk = reports/'owned.raw'; disk.write_bytes(b'fixture')
        identity = '00000000-0000-4000-8000-000000000001'
        running = Observation(identity, 'running', 2, 1)
        stopped = Observation(identity, 'stopped', 2, 1, StopEvidence(identity, 100, method, 'a'*64))
        alternate = Observation(identity, 'stopped', 2, 1, StopEvidence(identity, 101, method, 'a'*64))
        state = {'injected': False}
        class Broker:
            def observe(self, _identity): return stopped if state['injected'] else running
            def resource_limits(self, _identity): return {'pid': 123, 'process_start_time': 88}
        class Rebuilt:
            def observe(self, _identity): return alternate if rebuilt_mismatch else stopped
            def ensure(self, assignment): raise BrokerError('generation_stale')
            def renew(self, grant): raise BrokerError('resource_terminal')
        xml = '<domain><uuid>'+('foreign' if wrong_uuid else identity)+'</uuid><name>owned-a</name><metadata><ownership xmlns="urn:harniqo:resource-broker:1" signature="fixture"/></metadata></domain>'
        calls=[]
        def virsh(*args):
            calls.append(args)
            if args[0]=='dumpxml': return xml
            self.assertEqual(args, ('destroy',identity))
            self.assertTrue((reports/'external-stop-intent.json').exists())
            state['injected']=True
            return ''
        def invoke():
            result={'checks':{}}
            with patch.object(operator.kvm,'virsh',side_effect=virsh), \
                    patch.object(operator.kvm,'domain_ids',return_value=set()), \
                    patch.object(operator,'broker_from',return_value=Rebuilt()), \
                    patch.object(operator.time,'time',return_value=99):
                operator.external_vm_stop_fault(Broker(),{'templates':[{'resource_id':identity,'name':'owned-a'}]},
                    {'uuid':identity,'overlay':disk},None,reports,result,object(),object())
            return result
        return invoke,calls

    def test_external_fault_records_intent_and_reconstructs_exact_absence_tombstone(self):
        invoke,calls=self.fault_fixture()
        result=invoke()
        self.assertTrue(all(result['checks'].values()))
        self.assertEqual([item[0] for item in calls],['dumpxml','destroy'])
        self.assertEqual(result['external_vm_stop']['observed'],result['external_vm_stop']['rebuilt_observation'])

    def test_external_fault_rejects_wrong_uuid_before_destroy(self):
        invoke,calls=self.fault_fixture(wrong_uuid=True)
        with self.assertRaisesRegex(RuntimeError,'VM identity differs'): invoke()
        self.assertEqual([item[0] for item in calls],['dumpxml'])

    def test_external_fault_requires_absence_method_and_immutable_reconstruction(self):
        for kwargs in ({'rebuilt_mismatch':True},{'method':'libvirt_destroy_then_absent'}):
            with self.subTest(kwargs=kwargs):
                invoke,_=self.fault_fixture(**kwargs)
                with self.assertRaises(RuntimeError): invoke()


class HostQemuOomModeTests(unittest.TestCase):
    def test_fault_guest_is_explicit_and_control_retains_only_its_extended_heartbeat(self):
        from apps.runner.resource_broker import HOST_OOM_PROFILE
        instance = {'uuid': 'instance-a', 'nonce': 'nonce-a'}
        other = {'uuid': 'instance-b'}
        root = Path('/qualification/workspace')
        original = json.loads(operator.kvm.guest_config(instance, other, root,
            operator.DISK_BYTES).split('\n', 1)[1])['write_files'][0]['content']
        with patch.object(operator.host_oom, 'guest_program', return_value="\nprint('host-oom-fixture')\n") as fixture:
            a = json.loads(operator.sealed_guest_config(instance, other, root,
                resource_profile=HOST_OOM_PROFILE, pressure=True, host_oom_mode=True).split('\n', 1)[1])
            b = json.loads(operator.sealed_guest_config(instance, other, root,
                resource_profile='sealed-raw8g-q35-limited-v2', host_oom_mode=True).split('\n', 1)[1])
            legacy = json.loads(operator.sealed_guest_config(instance, other, root).split('\n', 1)[1])
        fixture.assert_called_once_with()
        for config in (a, b):
            self.assertTrue(config['bootcmd'][0][2].startswith(original))
            self.assertNotIn('runcmd', config)
            compile(config['bootcmd'][0][2], '<host-oom-qualified-guest>', 'exec')
        self.assertIn('host-oom-fixture', a['bootcmd'][0][2])
        self.assertNotIn('host-oom-fixture', b['bootcmd'][0][2])
        self.assertIn('until=time.monotonic()+150', b['bootcmd'][0][2])
        self.assertEqual(legacy['bootcmd'][0][2], original)

    def test_host_fault_budget_is_frozen_independently_of_existing_memory_case(self):
        import hashlib
        limits = {'qualification': 'host-qemu-oom-fixture', 'maximum_events': 64}
        with tempfile.TemporaryDirectory() as temporary, \
                patch.object(operator.host_oom, 'qualification_limits', return_value=limits):
            directory = Path(temporary)
            result = {}
            operator.freeze_qualification_limits(directory, result, host_oom_mode=True)
            manifest = directory / 'qualification-limits.json'
            self.assertEqual(json.loads(manifest.read_bytes()), limits)
            self.assertEqual(result['qualification_limits'], limits)
            self.assertEqual(result['qualification_limits_sha256'], hashlib.sha256(manifest.read_bytes()).hexdigest())
            self.assertEqual(stat.S_IMODE(manifest.stat().st_mode), 0o600)

    def test_invalid_fault_combinations_are_rejected_before_any_host_operation(self):
        base = dict(host_qemu_oom=True, guest_memory_pressure=False, console_pressure=False,
                    event_spool_pressure=False, spool_data_root=None,
                    resource_profile='sealed-raw8g-q35-limited-v2', timeout=120)
        for changes in ({'guest_memory_pressure': True}, {'console_pressure': True},
                        {'event_spool_pressure': True},
                        {'resource_profile': 'sealed-raw8g-q35-v1'},
                        {'spool_data_root': Path('/private/spool')}):
            with self.subTest(changes=changes), patch.object(operator.sys, 'platform', 'linux'), \
                    patch.object(operator.sys, 'version_info', (3, 12, 0)), \
                    patch.object(operator.platform, 'machine', return_value='x86_64'), \
                    patch.object(operator.os, 'geteuid', return_value=1000), \
                    patch.object(operator.kvm, 'run') as command, \
                    patch.object(operator.host_oom, 'preflight') as preflight:
                with self.assertRaises(RuntimeError):
                    operator.execute(SimpleNamespace(**(base | changes)))
                command.assert_not_called()
                preflight.assert_not_called()

    def absence_fixture(self, *, method='libvirt_absent_after_confirmed_start',
                        confirmed_at=100, wrong_binding=False, rebuilt_mismatch=False,
                        generation=2, recovery_generation=1):
        from apps.runner.resource_broker import BrokerError, Observation, StopEvidence
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        disk = Path(temporary.name) / 'owned.raw'; disk.write_bytes(b'fixture')
        identity = '00000000-0000-4000-8000-000000000001'
        stop = StopEvidence(identity, confirmed_at, method, ('b' if wrong_binding else 'a') * 64)
        stopped = Observation(identity, 'stopped', generation, recovery_generation, stop,
                              stop_cause='host_absence_after_confirmed_dispatch')
        alternate = Observation(identity, 'stopped', 2, 1,
            StopEvidence(identity, 101, method, 'a' * 64), stop_cause='host_absence_after_confirmed_dispatch')
        observed = []
        class Broker:
            def observe(self, resource_id):
                observed.append(resource_id)
                return stopped
            def resource_limits(self, resource_id):
                raise AssertionError('Dead QEMU cannot provide a live limits sample')
        class Rebuilt:
            def observe(self, resource_id): return alternate if rebuilt_mismatch else stopped
            def ensure(self, assignment): raise BrokerError('generation_stale')
            def renew(self, grant): raise BrokerError('resource_terminal')
        result = {'checks': {}, 'observations': {}, 'host_qemu_oom': {'trigger_at': 99}}
        def invoke():
            with patch.object(operator, 'broker_from', return_value=Rebuilt()), \
                    patch.object(operator.kvm, 'domain_ids', return_value=set()), \
                    patch.object(operator.kvm, 'virsh', side_effect=AssertionError('No manual destroy is allowed')):
                operator.confirm_host_oom_stop(Broker(), {}, {'uuid': identity, 'overlay': disk}, result,
                    object(), SimpleNamespace(resource_id=identity, configuration_sha256='a' * 64,
                                              generation=2, recovery_generation=1, expires_at=120))
            return result
        return invoke, observed, result

    def test_host_oom_confirmation_records_physical_absence_and_rejects_revival(self):
        invoke, observed, _ = self.absence_fixture()
        result = invoke()
        self.assertEqual(len(observed), 1)
        self.assertTrue(all(result['checks'].values()))
        self.assertEqual(result['observations']['a_host_qemu_oom_stop'],
                         result['observations']['a_host_qemu_oom_stop_rebuilt'])
        self.assertEqual(result['host_qemu_oom_old_assignment']['code'], 'generation_stale')
        self.assertEqual(result['host_qemu_oom_old_grant']['code'], 'resource_terminal')

    def test_manual_stop_expiry_earlier_stop_and_wrong_binding_cannot_prove_host_oom_absence(self):
        for changes in ({'method': 'libvirt_destroy_then_absent'}, {'confirmed_at': 120},
                        {'confirmed_at': 98}, {'wrong_binding': True}, {'rebuilt_mismatch': True},
                        {'generation': 3}, {'recovery_generation': 2}):
            with self.subTest(changes=changes):
                invoke, _, _ = self.absence_fixture(**changes)
                with self.assertRaises(RuntimeError): invoke()

    def test_missing_or_invalid_causal_trigger_cannot_confirm_a_stop(self):
        for trigger in (None, True, float('nan'), float('inf'), -float('inf'), -1, 0, 120):
            with self.subTest(trigger=trigger):
                invoke, observed, result = self.absence_fixture()
                result['host_qemu_oom']['trigger_at'] = trigger
                with self.assertRaises(RuntimeError): invoke()
                self.assertEqual(observed, [])

    def test_host_preflight_failure_allocates_no_raw_disk_and_claims_no_stop_receipt(self):
        import hashlib
        with tempfile.TemporaryDirectory(dir=Path('/private/tmp' if sys.platform == 'darwin' else '/tmp').resolve()) as temporary:
            root = Path(temporary)
            base = root / ('a' * 64 + '.qcow2'); base.write_bytes(b'synthetic base')
            lock = root / 'fixture.lock'; lock.write_bytes(b'fixture')
            descriptor = os.open(lock, os.O_RDONLY)
            baseline = {'domains': {}}
            args = SimpleNamespace(root=root, base_image=base, timeout=120,
                resource_profile='sealed-raw8g-q35-limited-v2', host_qemu_oom=True,
                guest_memory_pressure=False, console_pressure=False, event_spool_pressure=False,
                spool_data_root=None)
            actual_hash = operator.kvm.sha256
            def fixture_hash(path):
                return 'a' * 64 if path == base else actual_hash(path)
            with patch.object(operator.sys, 'platform', 'linux'), \
                    patch.object(operator.sys, 'version_info', (3, 12, 0)), \
                    patch.object(operator.platform, 'machine', return_value='x86_64'), \
                    patch.object(operator.resource, 'setrlimit'), \
                    patch.object(operator.shutil, 'which', return_value='/synthetic/tool'), \
                    patch.object(operator.shutil, 'disk_usage', return_value=SimpleNamespace(free=1024**4)), \
                    patch.object(operator, 'check_base'), \
                    patch.object(operator, 'coordination_lock', return_value=descriptor), \
                    patch.object(operator, 'verify_coordination_lock'), \
                    patch.object(operator, 'inventory', return_value=baseline), \
                    patch.object(operator, 'broker_from') as broker, \
                    patch.object(operator.kvm, 'TemporaryACL'), \
                    patch.object(operator.kvm, 'domain_ids', return_value=set()), \
                    patch.object(operator.kvm, 'sha256', side_effect=fixture_hash), \
                    patch.object(operator.kvm, 'run', return_value='synthetic df') as command, \
                    patch('builtins.print'), \
                    patch.object(operator.host_oom, 'qualification_limits', return_value={'fixture': True}), \
                    patch.object(operator.host_oom, 'preflight', side_effect=RuntimeError('synthetic unavailable journal')):
                self.assertEqual(operator.execute(args), 1)
            broker.assert_not_called()
            self.assertEqual(command.call_args_list, [unittest.mock.call(['df', '-h', str(root)])])
            reports = list((root / '.private/qualification').glob('resource-broker-*/report.json'))
            self.assertEqual(len(reports), 1)
            report = json.loads(reports[0].read_bytes())
            self.assertFalse(report['passed'])
            self.assertFalse(report['host_qemu_oom_verified'])
            self.assertFalse(report['host_global_oom_verified'])
            self.assertTrue(report['host_qemu_oom_preflight_failed_before_broker_submission'])
            self.assertTrue(report['unsubmitted_resources_absent'])
            self.assertNotIn('final_revoke', report)
            self.assertEqual(list((root / 'images').rglob('*.raw')), [])


if __name__ == '__main__':
    unittest.main()
