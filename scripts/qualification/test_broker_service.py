"""Service composition checks; libvirt remains the existing command-edge fixture.

No VM, system service, account, platform authorization or dedicated UID is
qualified here. Linux-only checks additionally cross the real IPC Interface.
"""
from dataclasses import FrozenInstanceError, asdict
import hashlib
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from apps.runner import resource_broker as native
from apps.runner.resource_broker import BrokerError
from apps.runner.resource_broker import service
import test_resource_broker as broker_fixture


class ServiceFixture(unittest.TestCase):
    new_broker = broker_fixture.BrokerInterfaceTests.new_broker
    claims = broker_fixture.BrokerInterfaceTests.claims
    assignment = broker_fixture.BrokerInterfaceTests.assignment

    def setUp(self):
        # The configuration admission rejects writable ancestors, including
        # shared /tmp. The fixture's private source-workspace directory is real.
        private = Path(os.environ.get('HARNIQO_SERVICE_TEST_ROOT', str(REPO / '.state')))
        private.mkdir(mode=0o700, exist_ok=True)
        original = tempfile.TemporaryDirectory
        with patch.object(tempfile, 'TemporaryDirectory',
                          side_effect=lambda *a, **kw: original(*a, **{**kw, 'dir': private})):
            broker_fixture.BrokerInterfaceTests.setUp(self)
        self.ipc = self.root / 'ipc'
        self.ipc.mkdir(mode=0o700)
        self.path = self.root / 'service.json'
        self.value = {
            'schema_version': 1,
            'broker_identity': {'uid': os.getuid(), 'gid': os.getgid()},
            'state_dir': str(self.root / 'state'), 'images_root': str(self.images),
            'channel_root': str(self.channels), 'ipc_directory': str(self.ipc),
            'issuer_key_file': str(self.key_file),
            'lease': {'max_seconds': 300, 'max_clock_skew_seconds': 5},
            'templates': [{k: str(v) if isinstance(v, Path) else v
                           for k, v in asdict(t).items()} for t in self.templates],
            'peers': [{'uid': os.getuid(), 'gid': os.getgid(), 'resources': [
                {'resource_id': t.resource_id, 'organization_id': 'org-a',
                 'workspace_id': 'workspace-' + str(i),
                 'operations': ['ensure', 'renew', 'observe', 'revoke']}
                for i, t in enumerate(self.templates)]}],
        }
        self.write_configuration()

    def write_configuration(self, data=None):
        self.data = json.dumps(self.value, separators=(',', ':')).encode() if data is None else data
        self.path.write_bytes(self.data)
        self.path.chmod(0o600)
        self.digest = hashlib.sha256(self.data).hexdigest()

    def load(self):
        return service.load_configuration(self.path, expected_sha256=self.digest)


class ConfigurationTests(ServiceFixture):
    def test_pure_parser_builds_deeply_immutable_configuration_without_io_or_identity(self):
        with patch.object(service.os, 'getuid', side_effect=AssertionError('identity read')):
            config = service.parse_configuration(self.data)
        self.assertEqual(config.templates, tuple(self.templates))
        self.assertIsNone(config.source_path)
        self.assertEqual(config.sha256, self.digest)
        with self.assertRaises(FrozenInstanceError):
            config.lease.max_seconds = 1
        with self.assertRaises(FrozenInstanceError):
            config.peers[0].resources[0].workspace_id = 'other'
        self.assertIsInstance(config.peers[0].resources[0].operations, frozenset)

    def test_loaded_configuration_pins_hash_and_current_normal_identity(self):
        config = self.load()
        self.assertEqual(config.source_path, self.path)
        self.assertEqual(config.broker_identity.uid, os.getuid())
        for digest in ('', 'A' * 64, '0' * 63, True, None, '0' * 64):
            with self.subTest(digest=digest), self.assertRaises(BrokerError):
                service.load_configuration(self.path, expected_sha256=digest)
        with patch.object(service.os, 'geteuid', return_value=os.getuid() + 1), self.assertRaises(BrokerError):
            self.load()
        with patch.object(service.os, 'getegid', return_value=os.getgid() + 1), self.assertRaises(BrokerError):
            self.load()

    def test_root_broker_and_implicit_or_mismatched_identity_are_refused(self):
        self.value['broker_identity']['uid'] = 0
        self.write_configuration()
        with self.assertRaises(BrokerError):
            self.load()
        self.value['broker_identity']['uid'] = os.getuid() + 1
        self.write_configuration()
        with self.assertRaises(BrokerError):
            self.load()

    def test_json_duplicates_unknown_fields_depth_nonfinite_and_size_are_refused(self):
        hostile = [b'', b'[]', b'{}', b'\xff', b'{"schema_version":1,"schema_version":1}',
                   b'{"x":NaN}', b'{"x":1e9999}', b' ' * (64 * 1024 + 1),
                   b'[' * 1100 + b'0' + b']' * 1100]
        for data in hostile:
            with self.subTest(data=data[:50]), self.assertRaises(BrokerError):
                service.parse_configuration(data)
        self.value['issuer_key'] = 'secret-must-not-be-a-config-value'
        self.write_configuration()
        with self.assertRaises(BrokerError):
            self.load()

    def test_strict_scalar_paths_templates_and_policies_reject_hostile_input(self):
        edits = [
            lambda v: v.update(schema_version=True),
            lambda v: v.update(state_dir='relative'),
            lambda v: v.update(state_dir=str(self.root) + '/a/../state'),
            lambda v: v.update(state_dir=str(self.root) + '//state'),
            lambda v: v.update(state_dir='/unencodable-\ud800'),
            lambda v: v['lease'].update(max_seconds=True),
            lambda v: v['lease'].update(max_clock_skew_seconds=float('inf')),
            lambda v: v['templates'][0].update(resource_id='not-a-uuid'),
            lambda v: v['templates'][0].update(source_base_sha256='F' * 64),
            lambda v: v['templates'][0].update(resource_profile='arbitrary'),
            lambda v: v['templates'].append(v['templates'][0].copy()),
            lambda v: v['peers'][0].update(uid=True),
            lambda v: v['peers'][0]['resources'][0].update(operations=['observe', 'observe']),
            lambda v: v['peers'][0]['resources'][0].update(operations=['watch']),
            lambda v: v['peers'][0]['resources'][0].update(resource_id='00000000-0000-4000-8000-000000000099'),
            lambda v: v['peers'].append(v['peers'][0].copy()),
            lambda v: v['peers'][0]['resources'][0].update(workspace_id=''),
            lambda v: v.update(ipc_directory=v['state_dir']),
        ]
        original = json.loads(self.data)
        for edit in edits:
            self.value = json.loads(json.dumps(original))
            edit(self.value)
            self.write_configuration()
            with self.subTest(edit=edit), self.assertRaises(BrokerError):
                service.parse_configuration(self.data)

    def test_catalog_and_peer_limits_and_conflicting_scopes_are_refused(self):
        for field in ('templates', 'peers'):
            original = self.value[field]
            self.value[field] = []
            self.write_configuration()
            with self.assertRaises(BrokerError):
                service.parse_configuration(self.data)
            self.value[field] = original
        other = json.loads(json.dumps(self.value['peers'][0]))
        other['uid'] += 1
        other['resources'][0]['workspace_id'] = 'different'
        self.value['peers'].append(other)
        self.write_configuration()
        with self.assertRaises(BrokerError):
            service.parse_configuration(self.data)

    def test_config_permissions_symlink_hardlink_and_unsafe_parent_are_refused(self):
        for mode in (0o644, 0o660, 0o400, 0o666):
            self.path.chmod(mode)
            with self.subTest(mode=mode), self.assertRaises(BrokerError):
                self.load()
        self.path.chmod(0o600)
        alias = self.root / 'alias.json'
        alias.symlink_to(self.path)
        with self.assertRaises(BrokerError):
            service.load_configuration(alias, expected_sha256=self.digest)
        alias.unlink()
        os.link(self.path, alias)
        with self.assertRaises(BrokerError):
            self.load()
        alias.unlink()
        self.root.chmod(0o777)
        try:
            with self.assertRaises(BrokerError):
                self.load()
        finally:
            self.root.chmod(0o700)

    def test_check_does_not_construct_broker_or_initialize_missing_state(self):
        self.value['state_dir'] = str(self.root / 'not-initialized')
        self.write_configuration()
        with patch.object(service, 'ResourceBroker', side_effect=AssertionError('construction')), \
                patch('sys.stdout', new_callable=io.StringIO) as output:
            result = service.main(['check', '--config', str(self.path), '--sha256', self.digest])
        self.assertEqual(result, 1)
        self.assertFalse((self.root / 'not-initialized').exists())
        self.assertEqual(json.loads(output.getvalue())['event'], 'error')


class LifecycleTests(ServiceFixture):
    def test_only_explicit_initialization_creates_state_and_refuses_reinitialization(self):
        self.value['state_dir'] = str(self.root / 'fresh-state')
        self.write_configuration()
        config = self.load()
        stop = threading.Event(); stop.set()
        for operation in (service.serve, service.watch):
            with self.assertRaises(BrokerError):
                operation(config, stop_event=stop)
            self.assertFalse(config.state_dir.exists())
        service.initialize(config)
        self.assertTrue((config.state_dir / 'anchor.json').is_file())
        before = (config.state_dir / 'journal.json').read_bytes()
        with self.assertRaises(BrokerError):
            service.initialize(config)
        self.assertEqual((config.state_dir / 'journal.json').read_bytes(), before)

    def test_startup_rechecks_pinned_file_before_any_broker_construction(self):
        config = self.load()
        self.path.write_bytes(self.data + b' ')
        with patch.object(service, 'ResourceBroker') as constructor, self.assertRaises(BrokerError):
            service.watch(config, stop_event=threading.Event())
        constructor.assert_not_called()

    def test_watch_uses_actual_core_and_emits_finite_ready_after_reconcile(self):
        assignment = self.assignment()
        self.broker.ensure(assignment)
        self.clock['wall'] += 301
        self.clock['monotonic'] += 301
        stop = threading.Event(); events = []
        def ready(event):
            events.append(dict(event)); stop.set()
        service.watch(self.load(), stop_event=stop, ready=ready)
        self.assertEqual(events, [{'schema_version': 1, 'event': 'ready', 'role': 'watch',
                                   'pid': os.getpid(), 'status': 'operational'}])
        self.assertNotIn(assignment.resource_id, self.libvirt.domains)
        self.assertEqual(self.new_broker().observe(assignment.resource_id).state, 'stopped')

    def test_missing_and_corrupt_journal_reach_existing_authenticated_emergency_stop(self):
        for missing in (True, False):
            with self.subTest(missing=missing):
                # Restore the valid envelope to make each failure independent.
                journal = self.root / 'state' / 'journal.json'
                original = journal.read_bytes()
                assignment = self.assignment()
                if not self.libvirt.domains:
                    self.broker.ensure(assignment)
                valid = journal.read_bytes()
                if missing:
                    journal.unlink()
                else:
                    journal.write_bytes(b'corrupt')
                with self.assertRaises(BrokerError):
                    service.watch(self.load(), stop_event=threading.Event())
                self.assertNotIn(assignment.resource_id, self.libvirt.domains)
                self.assertEqual(journal.exists(), not missing)
                journal.write_bytes(valid); journal.chmod(0o600)
                # The fixture can observe the natural absence before next case.
                self.broker.reconcile()
                journal.write_bytes(original); journal.chmod(0o600)

    def test_running_watch_keeps_cached_ownership_and_retries_persistent_corruption(self):
        assignment = self.assignment()
        self.broker.ensure(assignment)
        original_xml = self.libvirt.domains[assignment.resource_id]
        stop = threading.Event(); waits = []
        def ready(event):
            (self.root / 'state' / 'journal.json').write_bytes(b'corrupt')
        def wait(seconds):
            waits.append(seconds)
            if len(waits) == 1:
                self.libvirt.domains[assignment.resource_id] = original_xml
            else:
                stop.set()
            return stop.is_set()
        with patch.object(stop, 'wait', side_effect=wait):
            service.watch(self.load(), stop_event=stop, ready=ready)
        self.assertGreaterEqual(self.libvirt.destroys, 2)
        self.assertEqual((self.root / 'state' / 'journal.json').read_bytes(), b'corrupt')

    def test_initial_reconcile_error_is_degraded_process_readiness(self):
        self.broker.ensure(self.assignment())
        stop = threading.Event(); events = []
        def ready(event):
            events.append(dict(event)); stop.set()
        self.libvirt.unavailable = True
        service.watch(self.load(), stop_event=stop, ready=ready)
        self.assertEqual(events[0]['status'], 'degraded')
        self.assertNotIn('healthy', events[0])
        self.assertNotIn(str(self.root), json.dumps(events))

    def test_unadmitted_parsed_config_cannot_start_or_initialize(self):
        config = service.parse_configuration(self.data)
        for operation in (service.initialize, service.watch, service.serve):
            with self.subTest(operation=operation), self.assertRaises(BrokerError):
                operation(config)

    def test_cli_errors_are_one_safe_json_line_and_no_traceback_or_sensitive_paths(self):
        for argv in (["watch", '--config', str(self.path), '--sha256', '0' * 64],
                     ['unknown', '--config', '/private/secret-location', '--sha256', self.digest],
                     ['check', '--config', str(self.path)]):
            with self.subTest(argv=argv), patch('sys.stdout', new_callable=io.StringIO) as out, \
                    patch('sys.stderr', new_callable=io.StringIO) as err:
                self.assertEqual(service.main(argv), 1)
            self.assertEqual(err.getvalue(), '')
            self.assertEqual(len(out.getvalue().splitlines()), 1)
            self.assertEqual(json.loads(out.getvalue())['event'], 'error')
            self.assertNotIn(str(self.path), out.getvalue())
            self.assertNotIn('secret-location', out.getvalue())

    def test_cli_signal_handlers_only_set_stop_flag_and_restore_previous_handlers(self):
        previous = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT)}
        def watch(config, *, stop_event, ready):
            for sig in previous:
                handler = signal.getsignal(sig)
                self.assertTrue(callable(handler))
                handler(sig, None)
                self.assertTrue(stop_event.is_set())
        with patch.object(service, 'watch', side_effect=watch), \
                patch('sys.stdout', new_callable=io.StringIO):
            self.assertEqual(service.main(['watch', '--config', str(self.path), '--sha256', self.digest]), 0)
        self.assertEqual({s: signal.getsignal(s) for s in previous}, previous)

    def test_closed_supervisor_output_does_not_emit_a_secondary_traceback(self):
        with patch('sys.stdout') as out, patch('sys.stderr', new_callable=io.StringIO) as err:
            out.write.side_effect = BrokenPipeError()
            self.assertEqual(service.main(['invalid']), 1)
        self.assertEqual(err.getvalue(), '')

    @unittest.skipUnless(sys.platform == 'linux', 'actual Linux credential-checked IPC required')
    def test_serve_actual_broker_and_client_cleanup_without_revoking_valid_resource(self):
        from apps.runner.resource_broker.ipc import BrokerClient
        stop = threading.Event(); admitted = threading.Event(); errors = []
        def run():
            try:
                service.serve(self.load(), stop_event=stop, ready=lambda event: admitted.set())
            except BaseException as error:
                errors.append(error)
        worker = threading.Thread(target=run)
        worker.start()
        try:
            self.assertTrue(admitted.wait(3))
            client = BrokerClient(directory=self.ipc, server_uid=os.getuid(), server_gid=os.getgid())
            observation = client.ensure(self.assignment())
            self.assertEqual(observation.state, 'running')
            stop.set(); worker.join(5)
            self.assertFalse(worker.is_alive())
            self.assertEqual(errors, [])
            self.assertIn(observation.resource_id, self.libvirt.domains)
            self.assertFalse((self.ipc / 'broker.sock').exists())
        finally:
            stop.set(); worker.join(5)


class CLISignalTests(unittest.TestCase):
    def test_actual_signals_do_not_reenter_a_held_event_condition(self):
        # Run the dangerous signal point only in an owned, bounded child. A
        # threading.Event.set() handler would acquire this same non-reentrant
        # Condition and deadlock; subprocess.run kills and reaps a timed-out
        # child instead of leaving a hung test or signalling another process.
        script = r'''
import os
import sys
import threading
from unittest.mock import patch
from apps.runner.resource_broker import service

trapped = threading.Event()
def watch(config, *, stop_event, ready):
    with trapped._cond:
        os.kill(os.getpid(), int(sys.argv[1]))
        if not stop_event.is_set():
            raise AssertionError('signal did not set the stop flag')

with patch.object(service.threading, 'Event', return_value=trapped), \
     patch.object(service, 'load_configuration', return_value=object()), \
     patch.object(service, 'watch', side_effect=watch):
    result = service.main(['watch', '--config', '/trusted-config.json', '--sha256', 'a' * 64])
    if result != 0:
        raise AssertionError('CLI did not finish successfully')
print('signal-stop-success')
'''
        for signum in (signal.SIGTERM, signal.SIGINT):
            with self.subTest(signal=signum):
                try:
                    result = subprocess.run([sys.executable, '-c', script, str(int(signum))],
                        cwd=REPO, env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'},
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=3)
                except subprocess.TimeoutExpired:
                    self.fail('CLI signal handler deadlocked on a held Event Condition')
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), 'signal-stop-success')
                self.assertEqual(result.stderr, '')


if __name__ == '__main__':
    unittest.main()
