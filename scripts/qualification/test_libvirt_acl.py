#!/usr/bin/env python3
"""Component tests only; fixtures below do not establish native ACL or VM proof."""
import importlib.util
import hashlib
import json
import os
from pathlib import Path
import signal
import struct
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

SPEC = importlib.util.spec_from_file_location('qualify_libvirt_acl',
    Path(__file__).with_name('qualify_libvirt_acl.py'))
acl = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(acl)


class LibvirtError(Exception):
    def __init__(self, code=88, domain=55):
        self.code, self.domain = code, domain
    def get_error_code(self): return self.code
    def get_error_domain(self): return self.domain


class Connection:
    def __init__(self, *, denial=(88, 55), hostname_error=False, changed_hostname=False):
        self.denial = denial; self.closed = False; self.hostname_calls = 0
        self.hostname_error = hostname_error; self.changed_hostname = changed_hostname
    def getHostname(self):
        self.hostname_calls += 1
        if self.hostname_error:
            raise LibvirtError(45, 7)
        return 'different' if self.changed_hostname and self.hostname_calls > 1 else 'machine'
    def getCapabilities(self):
        if self.denial is not None:
            raise LibvirtError(*self.denial)
        return '<capabilities/>'
    def close(self): self.closed = True


class Binding:
    VIR_ERR_ACCESS_DENIED = 88
    VIR_FROM_ACCESS = 55
    libvirtError = LibvirtError
    def __init__(self, connection=None, *, open_denial=None):
        self.connection = connection or Connection()
        self.open_denial = open_denial; self.uris = []; self.handler = None
    def registerErrorHandler(self, handler, context): self.handler = handler
    def open(self, uri):
        self.uris.append(uri)
        if self.open_denial is not None:
            raise LibvirtError(*self.open_denial)
        return self.connection
    def openReadOnly(self, uri):
        raise AssertionError('read-only connections must never be used')


class NativeCallTests(unittest.TestCase):
    path = Path('/run/libvirt/libvirt-sock')
    raw = {'peer': {'pid': 123, 'uid': 0, 'gid': 0}}
    def probe(self, binding, role='broker', native_check=lambda *args: {'verified': True}):
        return acl._libvirt_probe(binding, role, self.path, self.raw, native_check)

    def test_same_rw_connection_positive_negative_positive(self):
        binding = Binding(); result = self.probe(binding)
        self.assertEqual(binding.uris, [acl.URI])
        self.assertEqual(binding.connection.hostname_calls, 2)
        self.assertTrue(result['same_connection'])
        self.assertEqual(result['denial']['call'], 'virConnectGetCapabilities')
        self.assertEqual(result['denial']['permission'], 'connect.read')
        self.assertTrue(binding.connection.closed)
        self.assertIsNotNone(binding.handler)

    def test_unrestricted_capabilities_is_failure(self):
        binding = Binding(Connection(denial=None))
        with self.assertRaisesRegex(acl.QualificationError, 'forbidden_api_allowed'):
            self.probe(binding)
        self.assertTrue(binding.connection.closed)

    def test_only_exact_api_access_denial_is_accepted(self):
        for code, domain in ((42, 10), (45, 7), (29, 20), (88, 7), (42, 55)):
            with self.subTest(code=code, domain=domain):
                with self.assertRaisesRegex(acl.QualificationError, 'unexpected_libvirt_denial') as failure:
                    self.probe(Binding(Connection(denial=(code, domain))))
                self.assertEqual(failure.exception.evidence['code'], code)
                self.assertFalse(failure.exception.evidence['access_denied'])

    def test_positive_connection_rejection_not_acl_proof(self):
        with self.assertRaisesRegex(acl.QualificationError, 'permitted_api_denied') as failure:
            self.probe(Binding(open_denial=(88, 55)))
        self.assertEqual(failure.exception.evidence['call'], 'virConnectOpen')

    def test_positive_hostname_failure_closes_connection(self):
        binding = Binding(Connection(hostname_error=True))
        with self.assertRaisesRegex(acl.QualificationError, 'permitted_api_denied'):
            self.probe(binding)
        self.assertTrue(binding.connection.closed)

    def test_lost_second_positive_fails(self):
        with self.assertRaisesRegex(acl.QualificationError, 'same_connection_positive_lost'):
            self.probe(Binding(Connection(changed_hostname=True)))

    def test_wrong_native_socket_fails_before_api(self):
        binding = Binding()
        def wrong_route(*args): raise acl.QualificationError('native_peer_mismatch')
        with self.assertRaisesRegex(acl.QualificationError, 'native_peer_mismatch'):
            self.probe(binding, native_check=wrong_route)
        self.assertEqual(binding.connection.hostname_calls, 0)
        self.assertTrue(binding.connection.closed)

    def test_supervisor_is_denied_at_open_getattr(self):
        binding = Binding(open_denial=(88, 55))
        result = self.probe(binding, 'supervisor')
        self.assertFalse(result['connection_established'])
        self.assertEqual(result['denial']['call'], 'virConnectOpen')
        self.assertEqual(result['denial']['permission'], 'connect.getattr')

    def test_supervisor_open_allowed_fails_and_closes(self):
        binding = Binding()
        with self.assertRaisesRegex(acl.QualificationError, 'forbidden_api_allowed'):
            self.probe(binding, 'supervisor')
        self.assertTrue(binding.connection.closed)

    def test_supervisor_socket_or_auth_error_not_api_denial(self):
        with self.assertRaisesRegex(acl.QualificationError, 'unexpected_libvirt_denial'):
            self.probe(Binding(open_denial=(45, 7)), 'supervisor')

    def test_error_contract_is_explicit(self):
        binding = Binding(); binding.VIR_FROM_ACCESS = 999
        with self.assertRaisesRegex(acl.QualificationError, 'unsupported_libvirt_error_contract'):
            self.probe(binding)


class IdentityAndGrammarTests(unittest.TestCase):
    def identity(self):
        return {'uid': [24001] * 4, 'gid': [24001] * 4, 'groups': [24001, 24003],
            'capabilities': {name: 0 for name in acl.CAPABILITIES}, 'no_new_privs': 1}

    def test_exact_dedicated_identity(self):
        acl._identity(self.identity(), 24001, 24001, 24003)

    def test_identity_rejects_root_or_surplus_groups(self):
        for field, value in (('uid', [24001, 0, 24001, 24001]),
                             ('gid', [24001, 24003, 24001, 24001]),
                             ('groups', [24001, 24003, 999]), ('groups', [24001])):
            with self.subTest(field=field, value=value):
                evidence = self.identity(); evidence[field] = value
                with self.assertRaisesRegex(acl.QualificationError, 'worker_identity_mismatch'):
                    acl._identity(evidence, 24001, 24001, 24003)

    def test_each_capability_class_and_nnp_required(self):
        for name in acl.CAPABILITIES:
            evidence = self.identity(); evidence['capabilities'][name] = 1
            with self.assertRaisesRegex(acl.QualificationError, 'worker_privilege_invalid'):
                acl._identity(evidence, 24001, 24001, 24003)
        evidence = self.identity(); evidence['no_new_privs'] = 0
        with self.assertRaisesRegex(acl.QualificationError, 'worker_privilege_invalid'):
            acl._identity(evidence, 24001, 24001, 24003)

    def test_duplicate_or_nonfinite_json_rejected(self):
        for raw in (b'{"a":1,"a":2}', b'{"a":NaN}', b'{"a":Infinity}', b'[]', b'null'):
            with self.subTest(raw=raw), self.assertRaises(acl.QualificationError):
                acl._decode(raw)

    def test_relative_traversal_and_control_paths_rejected(self):
        for value in ('relative', '/a/../b', '/a\n/b', '/a\x00b', '//a/b'):
            with self.subTest(value=value), self.assertRaises(acl.QualificationError):
                acl._path(value)

    def test_unrelated_parent_pid_is_rejected_before_prctl(self):
        with self.assertRaisesRegex(acl.QualificationError, 'parent_identity_invalid'):
            acl._parent_death(0)


class SocketPeerContractTests(unittest.TestCase):
    # Credential fixtures exercise admission only; no native socket claim.
    def raw_peer(self, pid, uid):
        connection = mock.MagicMock()
        connection.__enter__.return_value = connection
        connection.getsockopt.return_value = struct.pack('3i', pid, uid, 0)
        with mock.patch.object(acl.socket, 'socket', return_value=connection), \
             mock.patch.object(acl.socket, 'SO_PEERCRED', 17, create=True), \
             mock.patch.object(acl, '_socket_metadata', return_value={'inode': 1}):
            return acl._raw_connection(Path('/run/libvirt/libvirt-sock'))

    def test_root_systemd_activation_peer_pid_one_is_supported(self):
        self.assertEqual(self.raw_peer(1, 0)['peer'], {'pid': 1, 'uid': 0, 'gid': 0})

    def test_regular_root_socket_peer_is_supported(self):
        self.assertEqual(self.raw_peer(77, 0)['peer']['pid'], 77)

    def test_zero_pid_or_nonroot_peer_rejected(self):
        for pid, uid in ((0, 0), (-1, 0), (1, 24001), (77, 24001)):
            with self.subTest(pid=pid, uid=uid), \
                 self.assertRaisesRegex(acl.QualificationError, 'management_peer_invalid'):
                self.raw_peer(pid, uid)


class RealSubprocessTests(unittest.TestCase):
    # Mandatory Linux verification: WNOWAIT is the production ownership seam.
    # macOS must run the pure contract classes only; no skip may pass CI here.
    @classmethod
    def setUpClass(cls):
        if not sys.platform.startswith('linux') or not hasattr(os, 'waitid'):
            raise RuntimeError('Run subprocess regression on Linux with waitid/WNOWAIT; no emulation or skip')

    ready = "import json,os,sys; print(json.dumps({'event':'ready','process':{'pid':os.getpid()}}),flush=True); "
    handshake = "assert sys.stdin.buffer.readline()==b'GO\\n'; "
    success = "print(json.dumps({'event':'result','passed':True}),flush=True)"
    def run_child(self, code, *, timeout=3, on_ready=lambda pid, value: {'observed_pid': pid}, limit=acl.MAX_OUTPUT):
        return acl._run_child([sys.executable, '-I', '-B', '-c', code], timeout=timeout,
            role='component-fixture', on_ready=on_ready, output_limit=limit)

    @staticmethod
    def process_state(pid):
        try:
            content = Path('/proc', str(pid), 'stat').read_text()
        except FileNotFoundError:
            return None
        fields = content[content.rfind(')') + 2:].split()
        return {'state': fields[0], 'start_time': int(fields[19])}

    def stopped(self, pid, started):
        value = self.process_state(pid)
        return value is None or value['start_time'] != started or value['state'] in {'Z', 'X'}

    def test_real_ready_go_and_success(self):
        result = self.run_child(self.ready + self.handshake + self.success)
        self.assertEqual(result['exit_code'], 0)
        self.assertTrue(result['result']['passed'])
        self.assertGreater(result['parent_process_observation']['observed_pid'], 1)

    def test_failed_worker_result_is_preserved(self):
        code = self.ready + self.handshake + "print(json.dumps({'event':'result','passed':False,'error_code':'native_binding_missing'}),flush=True); sys.exit(2)"
        result = self.run_child(code)
        self.assertEqual(result['exit_code'], 2)
        self.assertEqual(result['result']['error_code'], 'native_binding_missing')

    def test_child_cannot_proceed_before_parent_admission(self):
        with tempfile.TemporaryDirectory() as folder:
            marker = str(Path(folder) / 'incorrectly-admitted')
            code = self.ready + self.handshake + f"open({marker!r},'w').write('bad')"
            def denied(pid, value): raise acl.QualificationError('independent_identity_denied')
            with self.assertRaisesRegex(acl.QualificationError, 'independent_identity_denied'):
                self.run_child(code, on_ready=denied)
            self.assertFalse(Path(marker).exists())

    def test_timeout_kills_actual_owned_worker(self):
        with tempfile.TemporaryDirectory() as folder:
            pidfile = Path(folder) / 'pid'
            code = f"from pathlib import Path; Path({str(pidfile)!r}).write_text(str(__import__('os').getpid())); "
            code += self.ready + self.handshake + "__import__('time').sleep(30)"
            with self.assertRaisesRegex(acl.QualificationError, 'worker_timeout'):
                self.run_child(code, timeout=.25)
            pid = int(pidfile.read_text())
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)

    def test_success_kills_devnull_descendant_before_reaping_leader(self):
        with tempfile.TemporaryDirectory() as folder:
            manifest = Path(folder) / 'owned-descendant.json'
            descendant = None; descriptor = None
            code = "import subprocess; from pathlib import Path; "
            code += "child=subprocess.Popen([__import__('sys').executable,'-I','-B','-c','import time; time.sleep(30)'],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); "
            code += "content=Path('/proc',str(child.pid),'stat').read_text(); "
            code += f"Path({str(manifest)!r}).write_text(__import__('json').dumps({{'pid':child.pid,'start_time':int(content[content.rfind(')')+2:].split()[19])}})); "
            code += self.ready + self.handshake + self.success
            try:
                result = self.run_child(code)
                self.assertEqual(result['exit_code'], 0)
                descendant = json.loads(manifest.read_text())
                until = time.monotonic() + 1
                while not self.stopped(descendant['pid'], descendant['start_time']) and time.monotonic() < until:
                    time.sleep(.01)
                self.assertTrue(self.stopped(descendant['pid'], descendant['start_time']),
                    'successful worker left its DEVNULL descendant running')
            finally:
                # If this regression fails, clean up only the pinned fixture
                # identity. pidfd signalling cannot target a reused process.
                if descendant is None and manifest.exists():
                    descendant = json.loads(manifest.read_text())
                if descendant is not None:
                    try:
                        descriptor = os.pidfd_open(descendant['pid'])
                        value = self.process_state(descendant['pid'])
                        if value is not None and value['start_time'] == descendant['start_time']:
                            signal.pidfd_send_signal(descriptor, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    finally:
                        if descriptor is not None:
                            os.close(descriptor)

    def test_closed_pipes_then_hung_exit_keeps_original_context(self):
        code = self.ready + self.handshake + self.success
        code += "; os.close(1); os.close(2); __import__('time').sleep(30)"
        with self.assertRaisesRegex(acl.QualificationError, 'worker_timeout_or_exit') as failure:
            self.run_child(code, timeout=.25)
        evidence = failure.exception.evidence
        self.assertEqual(evidence['role'], 'component-fixture')
        self.assertEqual(evidence['phase'], 'wait_for_worker_exit')
        self.assertEqual(evidence['original_exception']['type'], 'TimeoutExpired')
        self.assertIsInstance(failure.exception.__cause__, subprocess.TimeoutExpired)
        self.assertGreater(evidence['parent_process_observation']['observed_pid'], 1)
        self.assertGreater(evidence['stdout_bytes'], 0)
        self.assertNotEqual(evidence['stdout_sha256'], hashlib.sha256(b'').hexdigest())
        self.assertEqual(evidence['stderr_sha256'], hashlib.sha256(b'').hexdigest())

    def test_broken_handshake_pipe_keeps_original_context(self):
        code = "import os; os.close(0); " + self.ready + "__import__('time').sleep(30)"
        with self.assertRaisesRegex(acl.QualificationError, 'worker_timeout_or_exit') as failure:
            self.run_child(code)
        evidence = failure.exception.evidence
        self.assertEqual(evidence['role'], 'component-fixture')
        self.assertEqual(evidence['original_exception']['type'], 'BrokenPipeError')
        self.assertIsInstance(failure.exception.__cause__, BrokenPipeError)
        self.assertGreater(evidence['parent_process_observation']['observed_pid'], 1)
        self.assertGreater(evidence['stdout_bytes'], 0)

    def test_cleanup_failure_does_not_replace_original_failure(self):
        stop = acl._terminate
        def cleanup_with_error(process):
            return stop(process) + [{'phase': 'test_cleanup', 'error': {'type': 'OSError', 'errno': 5}}]
        def denied(pid, value):
            raise acl.QualificationError('independent_identity_denied', {'original': 'kept'})
        with mock.patch.object(acl, '_terminate', side_effect=cleanup_with_error), \
             self.assertRaisesRegex(acl.QualificationError, 'independent_identity_denied') as failure:
            self.run_child(self.ready + self.handshake + self.success, on_ready=denied)
        evidence = failure.exception.evidence
        self.assertEqual(evidence['original_failure_evidence'], {'original': 'kept'})
        self.assertEqual(evidence['original_exception']['code'], 'independent_identity_denied')
        self.assertEqual(evidence['cleanup_errors'][0]['phase'], 'test_cleanup')

    def test_output_budget_kills_actual_worker(self):
        code = "import sys; sys.stdout.write('x'*20000); sys.stdout.flush(); __import__('time').sleep(30)"
        with self.assertRaisesRegex(acl.QualificationError, 'worker_output_limit') as failure:
            self.run_child(code, limit=2048)
        self.assertGreater(failure.exception.evidence['stdout_bytes'], 2048)

    def test_early_exit_without_ready_rejected(self):
        with self.assertRaisesRegex(acl.QualificationError, 'worker_exited_before_ready'):
            self.run_child('pass')

    def test_malformed_ready_rejected(self):
        with self.assertRaisesRegex(acl.QualificationError, 'worker_ready_invalid'):
            self.run_child("print('{\"event\":\"result\"}',flush=True)")

    def test_duplicate_ready_json_rejected(self):
        with self.assertRaisesRegex(acl.QualificationError, 'worker_json_invalid'):
            self.run_child("print('{\"event\":\"ready\",\"event\":\"ready\",\"process\":{}}',flush=True)")

    def test_stderr_is_counted_and_preserved(self):
        code = self.ready + self.handshake + "sys.stderr.write('diagnostic'); sys.stderr.flush(); " + self.success
        result = self.run_child(code)
        self.assertEqual(result['stderr_bytes'], len('diagnostic'))

    def test_multiple_final_results_rejected(self):
        code = self.ready + self.handshake + self.success + "; " + self.success
        with self.assertRaisesRegex(acl.QualificationError, 'worker_json_invalid'):
            self.run_child(code)


if __name__ == '__main__':
    unittest.main()
