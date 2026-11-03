"""Signed qualification peer grammar/execution regressions, not native VM proof."""
from copy import deepcopy
from dataclasses import asdict
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from types import SimpleNamespace

import broker_peer as peer
from apps.runner.resource_broker import Assignment, Grant, Observation
from apps.runner.resource_broker.ipc import IPCError

RID = '932334eb-368e-4879-a8c8-e6e0a0242225'
OTHER = '752127ad-e831-4702-89d9-fb9c62cfb859'


def phase():
    scope = {'resource_id': RID, 'organization_id': 'org-a', 'workspace_id': 'workspace-a',
        'template_id': 'sealed-a', 'configuration_sha256': 'a' * 64}
    assignment = {**scope, 'attempt_id': 'attempt-a', 'generation': 1,
        'recovery_generation': 1, 'issued_at': 10, 'expires_at': 40, 'signature': 'b' * 64}
    renewal = {**assignment, 'generation': 2, 'issued_at': 20, 'expires_at': 50}
    return {'schema_version': 1, 'peer': {'uid': 21001, 'gid': 21001, 'ipc_gid': 21003},
        'broker': {'uid': 21002, 'gid': 21002, 'ipc_directory': '/run/harniqo/qualification-ipc'},
        'resource': scope, 'timeout_seconds': 30,
        'operations': [{'operation': 'ensure', 'envelope': assignment}, {'operation': 'observe'},
            {'operation': 'renew', 'envelope': renewal}, {'operation': 'revoke', 'cause': 'cancelled'}]}


class GrammarTests(unittest.TestCase):
    def parsed(self, value):
        return peer.parse_phase(json.dumps(value).encode())

    def test_accepts_all_four_operations_without_signing_or_key_reference(self):
        value = phase(); self.assertEqual(self.parsed(value), value)
        self.assertFalse({'issuer_key', 'issuer_key_file', 'secret', 'command'} & set(value))

    def test_each_scope_dimension_is_bound_to_the_pre_signed_envelope(self):
        for name, replacement in [('resource_id', OTHER), ('organization_id', 'org-b'),
                ('workspace_id', 'workspace-b'), ('template_id', 'sealed-b'),
                ('configuration_sha256', 'c' * 64)]:
            for operation in (0, 2):
                value = phase(); value['operations'][operation]['envelope'][name] = replacement
                with self.subTest(name=name, operation=operation), self.assertRaisesRegex(peer.PeerError, 'scope_mismatch'):
                    self.parsed(value)

    def test_rejects_commands_paths_keys_and_unknown_operations(self):
        for extra in ('issuer_key_file', 'command', 'pid', 'xml', 'base_url'):
            value = phase(); value[extra] = '/not-an-input'
            with self.subTest(extra=extra), self.assertRaises(peer.PeerError): self.parsed(value)
        value = phase(); value['operations'][0] = {'operation': 'resource_limits'}
        with self.assertRaises(peer.PeerError): self.parsed(value)

    def test_duplicate_members_are_rejected_before_their_values_are_selected(self):
        raw = peer.canonical(phase()).replace(b'"schema_version":1', b'"schema_version":1,"schema_version":1')
        with self.assertRaisesRegex(peer.PeerError, 'duplicate_member'): peer.parse_phase(raw)

    def test_nonfinite_numbers_and_bools_are_rejected(self):
        for value in (float('nan'), float('inf'), True):
            item = phase(); item['operations'][0]['envelope']['issued_at'] = value
            with self.subTest(value=value), self.assertRaises(peer.PeerError): self.parsed(item)
        for name in ('uid', 'gid', 'ipc_gid'):
            item = phase(); item['peer'][name] = True
            with self.assertRaises(peer.PeerError): self.parsed(item)

    def test_phase_count_and_time_are_bounded(self):
        for steps in ([], [{'operation': 'observe'}] * 9):
            item = phase(); item['operations'] = steps
            with self.assertRaises(peer.PeerError): self.parsed(item)
        for seconds in (0, 61, True, 1.5, '30'):
            item = phase(); item['timeout_seconds'] = seconds
            with self.assertRaises(peer.PeerError): self.parsed(item)

    def test_cause_and_envelope_extra_members_are_rejected(self):
        for cause in ('shutdown', None, []):
            item = phase(); item['operations'][-1]['cause'] = cause
            with self.subTest(cause=cause), self.assertRaises(peer.PeerError): self.parsed(item)
        item = phase(); item['operations'][0]['envelope']['key'] = 'not-read'
        with self.assertRaises(peer.PeerError): self.parsed(item)

    def test_positive_identity_and_distinct_service_groups_are_required(self):
        for name in ('uid', 'gid', 'ipc_gid'):
            item = phase(); item['peer'][name] = 0
            with self.assertRaises(peer.PeerError): self.parsed(item)
        item = phase(); item['peer']['uid'] = item['broker']['uid']
        with self.assertRaises(peer.PeerError): self.parsed(item)
        item = phase(); item['peer']['ipc_gid'] = item['broker']['gid']
        with self.assertRaises(peer.PeerError): self.parsed(item)

    def test_paths_must_be_absolute_canonical_and_not_control_characters(self):
        for path in ('relative', '/run/../tmp/socket', '/run//socket', '//run/socket', '/run/socket\n', '/'):
            item = phase(); item['broker']['ipc_directory'] = path
            with self.subTest(path=path), self.assertRaises(peer.PeerError): self.parsed(item)

    def test_malformed_encoding_and_input_size_have_finite_errors(self):
        for raw in (b'', b'\xff', b'{', b' ' * (peer.MAX_INPUT + 1), b'\xef\xbb\xbf{}'):
            with self.assertRaises(peer.PeerError): peer.parse_phase(raw)

    def test_signature_and_positive_generation_shape(self):
        for field, value in [('signature', 'not-a-signature'), ('generation', 0),
                ('generation', True), ('recovery_generation', 9007199254740992)]:
            item = phase(); item['operations'][0]['envelope'][field] = value
            with self.subTest(field=field), self.assertRaises(peer.PeerError): self.parsed(item)


class Calls:
    def __init__(self, *, fail=None, wrong_resource=False):
        self.calls = []; self.configurations = []; self.fail = fail; self.wrong_resource = wrong_resource
    def factory(self, **kwargs):
        self.configurations.append(kwargs); return self
    def reply(self, operation, argument):
        self.calls.append((operation, argument))
        if self.fail: raise self.fail
        return Observation(OTHER if self.wrong_resource else RID,
            'stopped' if operation == 'revoke' else 'running', 1, 1)
    def ensure(self, value): return self.reply('ensure', value)
    def observe(self, value): return self.reply('observe', value)
    def renew(self, value): return self.reply('renew', value)
    def revoke(self, value, cause): return self.reply('revoke', (value, cause))


class ExecutionTests(unittest.TestCase):
    def run_phase(self, calls, value=None, clock=lambda: 0):
        return peer.execute_phase(peer.parse_phase(peer.canonical(value or phase())),
            client_factory=calls.factory, monotonic=clock)

    def test_exact_typed_calls_and_order_are_the_production_client_interface(self):
        calls = Calls(); result = self.run_phase(calls)
        self.assertEqual(result['status'], 'PASSED')
        self.assertEqual([name for name, unused in calls.calls], ['ensure', 'observe', 'renew', 'revoke'])
        self.assertIs(type(calls.calls[0][1]), Assignment)
        self.assertIs(type(calls.calls[2][1]), Grant)
        self.assertEqual(calls.calls[-1][1], (RID, 'cancelled'))
        self.assertEqual([x['operation'] for x in result['operations']], ['ensure', 'observe', 'renew', 'revoke'])

    def test_unconfirmed_outcome_is_not_retried_or_followed_by_later_operations(self):
        calls = Calls(fail=IPCError('ipc_timeout', outcome='unconfirmed')); result = self.run_phase(calls)
        self.assertEqual(len(calls.calls), 1)
        self.assertEqual(result['status'], 'FAILED')
        self.assertEqual(result['error']['outcome'], 'unconfirmed')
        self.assertEqual(result['error']['code'], 'ipc_timeout')

    def test_authenticated_not_dispatched_refusal_is_preserved(self):
        calls = Calls(fail=IPCError('ipc_scope_denied')); result = self.run_phase(calls)
        self.assertEqual(result['error']['outcome'], 'not_dispatched')
        self.assertEqual(len(calls.calls), 1)

    def test_unexpected_exception_after_a_call_is_also_uncertain(self):
        calls = Calls(fail=OSError('not exposed')); result = self.run_phase(calls)
        self.assertEqual(result['error']['outcome'], 'unconfirmed')
        self.assertEqual(result['error']['code'], 'peer_operation_failed')
        self.assertNotIn('not exposed', json.dumps(result))

    def test_wrong_resource_reply_stops_phase(self):
        calls = Calls(wrong_resource=True); result = self.run_phase(calls)
        self.assertEqual(result['error']['code'], 'peer_resource_reply_mismatch')
        self.assertEqual(len(calls.calls), 1)

    def test_total_deadline_is_forwarded_as_a_shrinking_client_budget(self):
        calls = Calls(); ticks = iter([0, 1, 1, 3, 3, 7, 7, 20, 20]); self.run_phase(calls, clock=lambda: next(ticks))
        self.assertEqual([v['timeout_seconds'] for v in calls.configurations], [29, 27, 23, 10])
        self.assertTrue(all(v['server_uid'] == 21002 and v['server_gid'] == 21002 for v in calls.configurations))

    def test_deadline_exhaustion_prevents_the_first_client_constructor(self):
        calls = Calls(); ticks = iter([0, 31]); result = self.run_phase(calls, clock=lambda: next(ticks))
        self.assertFalse(calls.calls); self.assertFalse(calls.configurations)
        self.assertEqual(result['error']['outcome'], 'not_dispatched')
        self.assertEqual(result['error']['code'], 'phase_deadline_exhausted')

    def test_successful_prior_receipts_survive_a_later_error(self):
        calls = Calls()
        def observe(value):
            calls.calls.append(('observe', value)); raise IPCError('ipc_closed', outcome='unconfirmed')
        calls.observe = observe
        result = self.run_phase(calls)
        self.assertEqual(len(result['operations']), 1)
        self.assertEqual(result['operations'][0]['operation'], 'ensure')
        self.assertEqual(len(calls.calls), 2)

    def test_real_total_deadline_interrupts_a_stalled_call_without_retry(self):
        calls = Calls()
        def stalled(value):
            calls.calls.append(('ensure', value)); time.sleep(5)
        calls.ensure = stalled
        value = phase(); value['timeout_seconds'] = 1
        result = peer.execute_phase(peer.parse_phase(peer.canonical(value)), client_factory=calls.factory)
        self.assertEqual(result['error']['code'], 'phase_deadline_exhausted')
        self.assertEqual(result['error']['outcome'], 'unconfirmed')
        self.assertEqual(len(calls.calls), 1)
        self.assertEqual(len(calls.configurations), 1)

    def test_constructor_failure_never_dispatches(self):
        def refused(**unused): raise IPCError('ipc_directory_invalid')
        result = peer.execute_phase(phase(), client_factory=refused)
        self.assertEqual(result['error']['outcome'], 'not_dispatched')
        self.assertFalse(result['operations'])


class Output:
    def __init__(self): self.buffer = io.BytesIO()
    def result(self): return json.loads(self.buffer.getvalue())


class MemoryReports:
    """Fault-injectable durability seam; never presented as native storage."""
    def __init__(self, *, fail_event=None, fail_final=False, close_errors=()):
        self.events = []; self.final = None
        self.fail_event = fail_event; self.fail_final = fail_final; self.close_errors = close_errors
    def record(self, value):
        if self.fail_event is not None and len(self.events) >= self.fail_event:
            raise OSError('private storage message must not escape')
        self.events.append(deepcopy(value))
    def finalize(self, value):
        if self.fail_final: raise OSError('private path must not escape')
        self.final = deepcopy(value)
        return hashlib.sha256(peer.canonical(value) + b'\n').hexdigest()
    def close(self): return list(self.close_errors)


class RunTests(unittest.TestCase):
    def run_peer(self, reports, calls=None):
        calls = calls or Calls(); output = Output()
        args = SimpleNamespace(input=Path('/etc/harniqo/phase.json'), sha256='a' * 64,
            report=Path('/var/lib/harniqo/peer/report.json'))
        identity = {'pid': 123, 'uid': [21001] * 4}
        with patch.object(peer, 'read_phase', return_value=phase()), \
                patch.object(peer, 'process_identity', return_value=identity), patch.object(peer.sys, 'stdout', output):
            code = peer.run(args, client_factory=calls.factory, reports_factory=lambda unused: reports)
        return code, output.result(), calls

    def test_durable_phase_dispatch_and_receipts_precede_the_next_call(self):
        reports = MemoryReports(); calls = Calls()
        real = calls.reply
        def reply(operation, value):
            self.assertEqual(reports.events[-1]['event'], 'dispatch_intent')
            self.assertEqual(reports.events[-1]['operation'], operation)
            if calls.calls:
                self.assertEqual(reports.events[-2]['event'], 'operation_receipt')
            return real(operation, value)
        calls.reply = reply
        code, event, unused = self.run_peer(reports, calls)
        self.assertEqual(code, 0); self.assertEqual(event['status'], 'PASSED')
        self.assertEqual(reports.events[0]['event'], 'phase_intent')
        self.assertEqual(reports.events[-1]['event'], 'phase_result')
        self.assertEqual(event['report_sha256'], hashlib.sha256(peer.canonical(reports.final) + b'\n').hexdigest())

    def test_receipt_io_failure_preserves_reply_and_stops_later_dispatch(self):
        reports = MemoryReports(fail_event=2)
        code, event, calls = self.run_peer(reports)
        self.assertEqual(code, 1); self.assertEqual(len(calls.calls), 1)
        self.assertEqual(event['operations'][0]['observation']['state'], 'running')
        self.assertEqual(event['error']['code'], 'peer_evidence_write_failed')
        self.assertTrue(event['error']['response_received'])
        self.assertEqual(event['error']['outcome'], 'unconfirmed')
        self.assertNotIn('private storage message', json.dumps(event))

    def test_dispatch_intent_failure_prevents_even_the_first_client_constructor(self):
        code, event, calls = self.run_peer(MemoryReports(fail_event=1))
        self.assertEqual(code, 1); self.assertFalse(calls.calls); self.assertFalse(calls.configurations)
        self.assertEqual(event['error']['outcome'], 'not_dispatched')

    def test_final_write_failure_has_full_stdout_fallback(self):
        code, event, calls = self.run_peer(MemoryReports(fail_final=True))
        self.assertEqual(code, 1); self.assertEqual(event['status'], 'FAILED')
        self.assertEqual(len(event['operations']), 4); self.assertEqual(len(calls.calls), 4)
        self.assertIsNone(event['report_sha256'])
        self.assertEqual(event['report_finalization_errors'], ['peer_evidence_io_failed'])
        self.assertNotIn('private path', json.dumps(event))

    def test_finalization_failure_does_not_replace_original_ipc_outcome(self):
        calls = Calls(fail=IPCError('ipc_timeout', outcome='unconfirmed'))
        code, event, unused = self.run_peer(MemoryReports(fail_final=True, close_errors=['console_close_failed']), calls)
        self.assertEqual(code, 1); self.assertEqual(event['error']['code'], 'ipc_timeout')
        self.assertEqual(event['error']['outcome'], 'unconfirmed')
        self.assertEqual(event['report_finalization_errors'], ['peer_evidence_io_failed', 'console_close_failed'])

    def test_interruption_before_execute_returns_keeps_the_shared_receipt(self):
        reports = MemoryReports(); output = Output()
        args = SimpleNamespace(input=Path('/etc/harniqo/phase.json'), sha256='a' * 64,
            report=Path('/var/lib/harniqo/peer/report.json'))
        receipt = {'operation': 'ensure', 'observation': asdict(Observation(RID, 'running', 1, 1))}
        def interrupted(unused, **kwargs):
            kwargs['result']['operations'].append(receipt)
            raise peer.PeerError('peer_interrupted')
        with patch.object(peer, 'read_phase', return_value=phase()), \
                patch.object(peer, 'process_identity', return_value={'pid': 123}), \
                patch.object(peer, 'execute_phase', side_effect=interrupted), patch.object(peer.sys, 'stdout', output):
            code = peer.run(args, reports_factory=lambda unused: reports)
        result = output.result()
        self.assertEqual(code, 1); self.assertEqual(result['operations'], [receipt])
        self.assertEqual(result['error']['code'], 'peer_interrupted')
        self.assertEqual(result['error']['outcome'], 'unconfirmed')

    def test_signal_like_error_retains_the_preceding_response(self):
        calls = Calls()
        def interrupted(value): raise peer.PeerError('peer_interrupted')
        calls.observe = interrupted
        code, event, unused = self.run_peer(MemoryReports(), calls)
        self.assertEqual(code, 1); self.assertEqual(len(event['operations']), 1)
        self.assertEqual(event['operations'][0]['operation'], 'ensure')
        self.assertEqual(event['error']['code'], 'peer_interrupted')
        self.assertEqual(event['error']['outcome'], 'unconfirmed')


class ReportTests(unittest.TestCase):
    def setUp(self):
        if sys.platform != 'linux':
            raise RuntimeError('Run actual reserved-report storage regressions on Linux; no skip or emulation')
        temporary = tempfile.TemporaryDirectory(prefix='hpeer-', dir=Path('/tmp').resolve())
        self.addCleanup(temporary.cleanup); self.root = Path(temporary.name); self.root.chmod(0o700)

    def run_actual(self, path):
        calls = Calls(); output = Output()
        args = SimpleNamespace(input=Path('/etc/harniqo/phase.json'), sha256='a' * 64, report=path)
        with patch.object(peer, 'read_phase', return_value=phase()), \
                patch.object(peer, 'process_identity', return_value={'pid': 123}), patch.object(peer.sys, 'stdout', output):
            code = peer.run(args, client_factory=calls.factory)
        return code, output.result(), calls

    def test_report_is_reserved_exclusive_private_and_digest_matches_bytes(self):
        path = self.root / 'report.json'; value = {'status': 'FAILED', 'operations': [], 'error': 'kept'}
        files = peer.ReportFiles(path)
        self.assertEqual(path.stat().st_size, 0)
        self.assertGreaterEqual(path.stat().st_blocks * 512, peer.MAX_REPORT)
        self.assertGreaterEqual(files.events_path.stat().st_blocks * 512, peer.MAX_REPORT)
        digest = files.finalize(value); self.assertFalse(files.close())
        self.assertEqual(digest, hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertEqual(json.loads(path.read_text()), value)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        with self.assertRaises(peer.ConsoleStoreError): peer.ReportFiles(path)
        self.assertEqual(json.loads(path.read_text()), value)

    def test_preexisting_report_prevents_any_sdk_call(self):
        path = self.root / 'report.json'; path.write_bytes(b'original')
        code, event, calls = self.run_actual(path)
        self.assertEqual(code, 1); self.assertFalse(calls.configurations); self.assertFalse(calls.calls)
        self.assertEqual(path.read_bytes(), b'original')
        self.assertEqual(event['operations'], [])
        self.assertEqual(event['error']['outcome'], 'not_dispatched')

    def test_existing_events_file_also_prevents_sdk_calls(self):
        path = self.root / 'report.json'; events = path.with_name(path.name + '.events'); events.write_bytes(b'original')
        code, event, calls = self.run_actual(path)
        self.assertEqual(code, 1); self.assertFalse(calls.configurations)
        self.assertEqual(events.read_bytes(), b'original'); self.assertEqual(path.read_bytes(), b'')

    def test_nonprivate_parent_is_rejected_before_sdk_calls(self):
        self.root.chmod(0o750)
        code, event, calls = self.run_actual(self.root / 'report.json')
        self.assertEqual(code, 1); self.assertFalse(calls.configurations)
        self.assertFalse((self.root / 'report.json').exists())

    def test_report_refuses_symlink_and_preserves_target(self):
        path = self.root / 'report.json'; target = self.root / 'target'; target.write_bytes(b'old')
        path.symlink_to(target)
        code, event, calls = self.run_actual(path)
        self.assertEqual(code, 1); self.assertFalse(calls.configurations)
        self.assertEqual(target.read_bytes(), b'old')

    def test_capacity_rejection_keeps_reserved_empty_report(self):
        path = self.root / 'report.json'; files = peer.ReportFiles(path)
        try:
            with self.assertRaises(peer.PeerError): files.finalize({'value': 'a' * peer.MAX_REPORT})
            self.assertEqual(path.read_bytes(), b'')
        finally:
            files.close()

    def test_real_event_bytes_are_ndjson_and_fsynced(self):
        path = self.root / 'report.json'; files = peer.ReportFiles(path)
        values = [{'event': 'phase_intent'}, {'event': 'dispatch_intent'}, {'event': 'operation_receipt'}]
        for value in values: files.record(value)
        self.assertEqual(files.events.snapshot()['durable_bytes'], files.events_path.stat().st_size)
        self.assertEqual([json.loads(line) for line in files.events_path.read_bytes().splitlines()], values)
        self.assertFalse(files.close())


if __name__ == '__main__':
    unittest.main()
