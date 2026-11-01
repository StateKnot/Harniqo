"""Acceptance operator safety only; native Linux credential gates run separately."""
import contextlib
from dataclasses import asdict
import hashlib
import io
import json
import os
from pathlib import Path
import socket
import stat
import subprocess
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import qualify_broker_ipc as operator


class BrokerIPCOperatorSafety(unittest.TestCase):
    def temporary(self):
        temporary = tempfile.TemporaryDirectory(prefix='hbip-', dir=Path('/tmp').resolve())
        self.addCleanup(temporary.cleanup)
        return Path(temporary.name)

    def test_cli_requires_explicit_source_and_private_runtime_roots(self):
        with patch('sys.argv', ['qualify_broker_ipc.py', '--root', '/source']), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as error:
                operator.main()
        self.assertEqual(error.exception.code, 2)

    def test_cli_keeps_source_and_runtime_roots_distinct(self):
        with patch('sys.argv', ['qualify_broker_ipc.py', '--root', '/source', '--runtime-root', '/private-runtime']), \
                patch.object(operator, 'execute', return_value=0) as execute:
            self.assertEqual(operator.main(), 0)
        execute.assert_called_once_with(Path('/source'), Path('/private-runtime'))

    def test_shared_source_parent_is_preserved_and_existing_shared_runtime_is_rejected(self):
        root = self.temporary()
        shared = root / 'source'; shared.mkdir(); shared.chmod(0o775)
        private = shared / 'reports'
        operator.private_directory(private)
        self.assertEqual(stat.S_IMODE(private.stat().st_mode), 0o700)
        with self.assertRaisesRegex(RuntimeError, 'Private directory'):
            operator.private_directory(shared)
        self.assertEqual(stat.S_IMODE(shared.stat().st_mode), 0o775)

    def test_symlink_ancestor_cannot_hide_a_private_runtime_or_source(self):
        root = self.temporary()
        target = root / 'target'; target.mkdir(mode=0o700)
        (target / 'input').write_bytes(b'original')
        link = root / 'link'; link.symlink_to(target, target_is_directory=True)
        with self.assertRaisesRegex(RuntimeError, 'Symlink'):
            operator.private_directory(link, create=False)
        with self.assertRaisesRegex(RuntimeError, 'Symlink'):
            operator.bounded_read(link / 'input', 32)
        self.assertEqual((target / 'input').read_bytes(), b'original')

    def test_bounded_source_read_rejects_hardlinks_and_nonregular_files(self):
        root = self.temporary()
        source = root / 'source'; source.write_bytes(b'12345')
        self.assertEqual(operator.bounded_read(source, 5), b'12345')
        with self.assertRaises(RuntimeError):
            operator.bounded_read(source, 4)
        os.link(source, root / 'second-link')
        with self.assertRaises(RuntimeError):
            operator.bounded_read(source, 5)
        with self.assertRaises(RuntimeError):
            operator.bounded_read(root, 65536)
        fifo = root / 'fifo'; os.mkfifo(fifo, 0o600)
        with self.assertRaises(RuntimeError):
            operator.bounded_read(fifo, 65536)

    def test_source_snapshot_is_exact_private_exclusive_and_hashed(self):
        root = self.temporary()
        (root / 'nested').mkdir()
        source = root / 'nested/source.py'; source.write_bytes(b'exact source\n')
        reports = root / 'reports'; operator.private_directory(reports)
        with patch.object(operator, 'SOURCE_FILES', ('nested/source.py',)):
            hashes = operator.snapshot_sources(root, reports)
        snapshot = reports / 'source/nested/source.py'
        self.assertEqual(snapshot.read_bytes(), source.read_bytes())
        self.assertEqual(stat.S_IMODE(snapshot.stat().st_mode), 0o600)
        self.assertEqual(hashes['nested/source.py'], hashlib.sha256(source.read_bytes()).hexdigest())
        with self.assertRaises(FileExistsError):
            operator.write(snapshot, b'replaced')
        source.write_bytes(b'mutated')
        self.assertNotEqual(hashes['nested/source.py'], hashlib.sha256(source.read_bytes()).hexdigest())

    def test_source_snapshot_enforces_aggregate_bound_before_more_writes(self):
        root = self.temporary()
        (root / 'a').write_bytes(b'12345'); (root / 'b').write_bytes(b'67890')
        reports = root / 'reports'; operator.private_directory(reports)
        with patch.object(operator, 'SOURCE_FILES', ('a', 'b')), \
                patch.dict(operator.BUDGET, {'source_total_bytes': 8}):
            with self.assertRaisesRegex(RuntimeError, 'total bound'):
                operator.snapshot_sources(root, reports)
        self.assertTrue((reports / 'source/a').exists())
        self.assertFalse((reports / 'source/b').exists())

    def test_public_summary_allowlist_excludes_raw_identity_and_failure_details(self):
        private_value = '/private/host/secret-identity-987654321'
        source = operator.SOURCE_FILES[0]
        report = {'passed': False, 'checks': {key: True for key in operator.REQUIRED_CHECKS},
            'processes': [{'pid': 987654321, 'uid': 987654321, 'gid': 987654321}],
            'checks_extra': private_value, 'failure_reason': private_value,
            'sources_sha256': {source: 'a' * 64, private_value: 'b' * 64},
            'worker_returncodes': [0, -9], 'runtime_identity': private_value}
        report['checks'][private_value] = True
        summary = operator.safe_summary(report, 'c' * 64)
        rendered = json.dumps(summary)
        self.assertNotIn(private_value, rendered)
        self.assertNotIn('987654321', rendered)
        self.assertEqual(summary['sources_sha256'], {source: 'a' * 64})
        self.assertEqual(summary['private_report_sha256'], 'c' * 64)
        self.assertEqual(summary['workers_started'], 2)
        self.assertFalse(summary['passed'])
        self.assertFalse(summary['milestone_complete'])
        self.assertTrue(summary['synthetic_backend'])
        self.assertTrue(all(value is False for value in summary['claims'].values()))

    def test_public_summary_rejects_unvalidated_hash_values_and_unrun_gates(self):
        source = operator.SOURCE_FILES[0]
        summary = operator.safe_summary({'checks': {}, 'sources_sha256': {source: '/secret/key'}}, '/secret/report')
        self.assertEqual(summary['sources_sha256'], {})
        self.assertNotIn('private_report_sha256', summary)
        self.assertFalse(summary['real_linux_uds_processes'])
        self.assertEqual(summary['gates_passed'], 0)
        self.assertEqual(set(summary['checks']), operator.REQUIRED_CHECKS)
        self.assertEqual(summary['real_model_calls'], 0)
        self.assertEqual(summary['virtual_machines_started'], 0)
        self.assertEqual(summary['tcp_listeners_started'], 0)

    def test_public_environment_is_bounded_and_omits_invalid_values_and_private_fields(self):
        environment = {'system': 'Linux', 'kernel_release': '6.8.0-1-generic', 'machine': 'x86_64',
            'python': '3.12.3', 'logical_cpu_count': 72, 'hostname': 'private-host', 'executable': '/private/python'}
        summary = operator.safe_summary({'environment': environment})
        self.assertEqual(summary['environment'], {key: environment[key] for key in
            ('system', 'kernel_release', 'machine', 'python', 'logical_cpu_count')})
        self.assertNotIn('private', json.dumps(summary))
        invalid = {'system': 'Linux', 'kernel_release': '/private/kernel', 'machine': 'x86_64\nprivate-host',
            'python': True, 'logical_cpu_count': True}
        self.assertEqual(operator.safe_summary({'environment': invalid})['environment'], {'system': 'Linux'})
        self.assertEqual(operator.safe_summary({'environment': []})['environment'], {})
        self.assertEqual(operator.safe_summary({'environment': {'python': 'x' * 129, 'logical_cpu_count': 1048577}})['environment'], {})

    def test_owned_child_is_registered_before_any_reply_read(self):
        root = self.temporary()
        q = operator.Qualification(root, root, root, {'checks': {}, 'processes': []})
        fake_process = SimpleNamespace(stdout=io.BytesIO(), poll=lambda: None)
        with patch.object(operator.subprocess, 'Popen', return_value=fake_process) as spawn:
            child = q.start({'action': 'test'})
        self.addCleanup(child.stdout.close); self.addCleanup(child.stderr.close)
        self.assertEqual(q.workers, [child])
        self.assertIs(child.process, fake_process)
        self.assertIs(spawn.call_args.kwargs['stdin'], subprocess.DEVNULL)
        self.assertTrue(spawn.call_args.kwargs['close_fds'])
        self.assertEqual(stat.S_IMODE((root / 'worker-0.json').stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((root / 'stderr-0.log').stat().st_mode), 0o600)

    def test_worker_count_and_concurrency_bounds_precede_process_creation(self):
        root = self.temporary()
        q = operator.Qualification(root, root, root, {'checks': {}, 'processes': []})
        q.workers = [SimpleNamespace(process=SimpleNamespace(poll=lambda: None))]
        with patch.dict(operator.BUDGET, {'workers_concurrent': 1}), patch.object(operator.subprocess, 'Popen') as spawn:
            with self.assertRaisesRegex(RuntimeError, 'Concurrent'):
                q.start({'action': 'test'})
        spawn.assert_not_called()

    def test_stop_never_signals_an_unregistered_or_already_exited_process(self):
        root = self.temporary()
        q = operator.Qualification(root, root, root, {'checks': {}, 'processes': []})
        process = SimpleNamespace(poll=lambda: None, kill=Mock(), terminate=Mock())
        child = SimpleNamespace(process=process)
        with self.assertRaises(RuntimeError):
            q.stop(child, kill=True)
        q.workers.append(child); process.poll = lambda: 0
        with self.assertRaises(RuntimeError):
            q.stop(child, kill=True)
        process.kill.assert_not_called(); process.terminate.assert_not_called()

    def test_cleanup_escalates_only_owned_child_and_closes_all_logs_after_sync_failure(self):
        root = self.temporary()
        output = open(root / 'stdout', 'xb'); errors = open(root / 'stderr', 'xb')
        pipe = io.BytesIO()
        process = SimpleNamespace(stdout=pipe, poll=lambda: None, terminate=Mock(), kill=Mock(),
            wait=Mock(side_effect=[subprocess.TimeoutExpired('owned-child', 1), -9]))
        child = operator.OwnedProcess(process, output, errors, root / 'stdout', time.monotonic() + 5)
        with patch.object(operator.os, 'fsync', side_effect=[OSError('private path detail'), None]):
            failures = child.close()
        process.terminate.assert_called_once(); process.kill.assert_called_once()
        self.assertEqual(failures, [{'phase': 'owned_child_log_sync', 'type': 'OSError'}])
        self.assertTrue(output.closed and errors.closed and pipe.closed)

    def test_persistent_fixture_reopen_retains_original_effect_and_each_invocation(self):
        root = self.temporary()
        q = operator.Qualification(root, root, root, {'checks': {}, 'processes': []})
        native, _ = operator.api()
        config = {'ensure': q.assignment, 'renew': q.grant}
        envelope = native.Assignment(**q.assignment)
        fixture = operator.PersistentFixture(root, config)
        self.assertEqual(fixture.ensure(envelope).state, 'running')
        reopened = operator.PersistentFixture(root, config)
        self.assertEqual(reopened.observe(q.resource).state, 'running')
        self.assertEqual(reopened.ensure(envelope).state, 'running')
        state = json.loads(operator.bounded_read(root / 'fixture-state.json', operator.BUDGET['state_bytes']))
        self.assertEqual(state['effects'], 1)
        self.assertEqual([item['operation'] for item in state['invocations']], ['ensure', 'observe', 'ensure'])
        self.assertEqual(state['invocations'][0]['envelope_sha256'], hashlib.sha256(operator.encode(q.assignment)).hexdigest())

    def test_fixture_rejects_mutated_envelope_without_a_false_invocation_receipt(self):
        root = self.temporary()
        q = operator.Qualification(root, root, root, {'checks': {}, 'processes': []})
        native, _ = operator.api()
        fixture = operator.PersistentFixture(root, {'ensure': q.assignment, 'renew': q.grant})
        altered = native.Assignment(**{**q.assignment, 'signature': 'b' * 64})
        with self.assertRaisesRegex(RuntimeError, 'Exact signed fixture'):
            fixture.ensure(altered)
        self.assertEqual(json.loads((root / 'fixture-state.json').read_bytes()), {'invocations': [], 'effects': 0, 'records': {}})

    def test_transport_error_is_not_mistaken_for_no_dispatch_proof(self):
        root = self.temporary()
        q = operator.Qualification(root, root, root, {'checks': {}, 'processes': []})
        self.assertFalse(q.no_dispatch({'ok': False, 'outcome': 'unconfirmed'}))
        self.assertTrue(q.no_dispatch({'ok': False, 'outcome': 'not_dispatched'}))
        self.assertFalse(q.raw_denied({'ok': True, 'reply': {'observation': {}}}))
        self.assertTrue(q.raw_denied({'ok': True, 'closed': True}))
        self.assertTrue(q.raw_denied({'ok': True, 'reply': {'error': {'outcome': 'not_dispatched'}}}))

    def test_failed_native_run_retains_private_report_sources_and_only_safe_public_summary(self):
        root = self.temporary()
        source = root / 'scripts/qualification/qualify_broker_ipc.py'
        source.parent.mkdir(parents=True); source.write_bytes(b'captured source\n')
        runtime = root / 'r'; runtime.mkdir(mode=0o700)
        private_detail = 'private-host-identity-and-unconfirmed-effect'

        class FailingQualification:
            def __init__(self, *_):
                self.workers, self.directories = [], {}

            def run(self):
                raise RuntimeError(private_detail)

        native = SimpleNamespace(__file__=root / 'apps/runner/resource_broker/__init__.py')
        ipc = SimpleNamespace(__file__=root / 'apps/runner/resource_broker/ipc.py')
        captured = io.StringIO()
        with patch.object(operator, '__file__', str(source)), patch.object(operator.sys, 'platform', 'linux'), \
                patch.object(operator.socket, 'SO_PEERCRED', 17, create=True), \
                patch.object(operator.socket, 'SCM_CREDENTIALS', 2, create=True), \
                patch.object(operator, 'SOURCE_FILES', ('scripts/qualification/qualify_broker_ipc.py',)), \
                patch.object(operator, 'api', return_value=(native, ipc)), \
                patch.object(operator, 'Qualification', FailingQualification), \
                patch.object(operator.subprocess, 'run', return_value=SimpleNamespace(stdout=b'bounded-df-evidence\n')), \
                contextlib.redirect_stdout(captured):
            previous_umask = os.umask(0o077)
            try:
                self.assertEqual(operator.execute(root, runtime), 1)
            finally:
                os.umask(previous_umask)
        reports = tuple((root / '.private/qualification').iterdir())
        self.assertEqual(len(reports), 1)
        report_body = (reports[0] / 'report.json').read_bytes()
        private_report = json.loads(report_body)
        self.assertEqual(private_report['failure_reason'], private_detail)
        self.assertEqual((reports[0] / 'source/scripts/qualification/qualify_broker_ipc.py').read_bytes(), b'captured source\n')
        public = json.loads((reports[0] / 'public-summary.json').read_bytes())
        self.assertEqual(public, json.loads(captured.getvalue()))
        self.assertEqual(public['private_report_sha256'], hashlib.sha256(report_body).hexdigest())
        self.assertNotIn(private_detail, json.dumps(public))
        self.assertFalse(public['passed'])


if __name__ == '__main__':
    unittest.main()
