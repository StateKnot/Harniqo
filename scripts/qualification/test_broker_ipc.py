"""The local IPC Interface, using real Linux sockets and a broker-edge fixture.

These tests establish neither libvirt isolation nor distinct production UIDs.
"""
import array
from dataclasses import replace
import errno
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from apps.runner.resource_broker import BrokerError, Observation, sign_assignment, sign_grant
from apps.runner.resource_broker.ipc import BrokerClient, BrokerServer, IPCError, PeerPolicy, ResourceAccess
from apps.runner.resource_broker import ipc


RESOURCE = '00000000-0000-4000-8000-000000000001'
OTHER = '00000000-0000-4000-8000-000000000002'


class BrokerFixture:
    def __init__(self):
        self.calls = []

    def ensure(self, envelope):
        self.calls.append(('ensure', envelope))
        return Observation(envelope.resource_id, 'running', envelope.generation, envelope.recovery_generation)

    def observe(self, resource_id):
        self.calls.append(('observe', resource_id))
        return Observation(resource_id, 'running', 1, 1)

    def renew(self, envelope):
        self.calls.append(('renew', envelope))
        return Observation(envelope.resource_id, 'running', envelope.generation, envelope.recovery_generation)

    def revoke(self, resource_id, cause):
        self.calls.append(('revoke', resource_id, cause))
        return Observation(resource_id, 'reconciling', 1, 1, stop_cause=cause)


@unittest.skipUnless(sys.platform == 'linux', 'Linux SO_PEERCRED/SCM_CREDENTIALS are required')
class BrokerIPCTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='harniqo-ipc-')
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name).resolve()
        self.directory.chmod(0o700)
        self.broker = BrokerFixture()
        self.access = ResourceAccess(RESOURCE, 'org-a', 'workspace-a', frozenset({'ensure', 'renew', 'observe', 'revoke'}))
        self.policy = PeerPolicy(os.getuid(), os.getgid(), (self.access,))
        self.server = BrokerServer(directory=self.directory, broker=self.broker, peers=(self.policy,))
        self.stop = threading.Event()
        self.thread_errors = []
        self.start_thread()
        self.addCleanup(self.shutdown)
        self.client = BrokerClient(directory=self.directory, server_uid=os.getuid(), server_gid=os.getgid())

    def start_thread(self):
        def run():
            try:
                self.server.serve(self.stop)
            except BaseException as error:
                self.thread_errors.append(error)
        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()

    def shutdown(self):
        self.stop.set()
        self.thread.join(3)
        self.server.close()
        self.assertFalse(self.thread.is_alive())

    def request(self, **changes):
        value = {'version': 1, 'request_id': 'a' * 32, 'operation': 'observe',
            'arguments': {'resource_id': RESOURCE},
            'dispatch_before': time.clock_gettime(time.CLOCK_BOOTTIME) + 4}
        value.update(changes)
        return value

    def connection(self):
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        connection.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
        connection.settimeout(3)
        connection.connect(str(self.directory / ipc.SOCKET_NAME))
        return connection

    def raw(self, data):
        with self.connection() as connection:
            connection.send(data if isinstance(data, bytes) else json.dumps(data).encode())
            return json.loads(connection.recv(ipc.REPLY_LIMIT))

    def test_authorized_observation_crosses_public_interface(self):
        self.assertEqual(self.client.observe(RESOURCE), Observation(RESOURCE, 'running', 1, 1))
        self.assertEqual(self.broker.calls, [('observe', RESOURCE)])

    def test_unknown_resource_and_wrong_first_use_scope_never_call_broker(self):
        with self.assertRaises(IPCError) as denied:
            self.client.observe(OTHER)
        self.assertEqual(denied.exception.outcome, 'not_dispatched')
        envelope = sign_assignment(b'k' * 32, resource_id=RESOURCE, organization_id='org-other',
            workspace_id='workspace-a', attempt_id='attempt-a', template_id='template-a',
            configuration_sha256='a' * 64, generation=1, recovery_generation=1,
            issued_at=1000.0, expires_at=1060.0)
        with self.assertRaises(IPCError) as mismatch:
            self.client.ensure(envelope)
        self.assertEqual(mismatch.exception.outcome, 'not_dispatched')
        self.assertEqual(self.broker.calls, [])

    def test_second_listener_cannot_replace_active_socket(self):
        with self.assertRaises(IPCError):
            BrokerServer(directory=self.directory, broker=self.broker, peers=(self.policy,))
        self.assertEqual(self.client.observe(RESOURCE).state, 'running')

    def test_authorized_signed_envelopes_and_revoke_use_only_four_operations(self):
        claims = dict(resource_id=RESOURCE, organization_id='org-a', workspace_id='workspace-a',
            attempt_id='attempt-a', template_id='template-a', configuration_sha256='a' * 64,
            generation=1, recovery_generation=1, issued_at=1000.0, expires_at=1060.0)
        self.assertEqual(self.client.ensure(sign_assignment(b'k' * 32, **claims)).state, 'running')
        claims['generation'] = 2
        self.assertEqual(self.client.renew(sign_grant(b'k' * 32, **claims)).generation, 2)
        self.assertEqual(self.client.revoke(RESOURCE).stop_cause, 'cancelled')
        self.assertEqual([call[0] for call in self.broker.calls], ['ensure', 'renew', 'revoke'])
        for operation in ('resource_limits', 'reconcile', 'watch', 'sign_assignment', '__dict__'):
            reply = self.raw(self.request(operation=operation))
            self.assertEqual(reply['error']['outcome'], 'not_dispatched')

    def test_malformed_duplicate_nested_oversized_and_nonfinite_frames_never_dispatch(self):
        invalid = [b'[]', b'{}', b'{"version":1,"version":1}', b'\xff',
            b'{"dispatch_before":NaN}', b'{"dispatch_before":1e9999}',
            b'{"nested":' + b'[' * 1100 + b'0' + b']' * 1100 + b'}',
            b'{' + b' ' * ipc.REQUEST_LIMIT + b'}',
            self.request(version=True), self.request(arguments={'resource_id': RESOURCE, 'path': '/host'}),
            self.request(operation='revoke', arguments={'resource_id': RESOURCE, 'cause': []}),
            self.request(dispatch_before=True), self.request(request_id='../private'),
            self.request(arguments={'resource_id': 'NONCANONICAL'}),
        ]
        for value in invalid:
            with self.subTest(value=str(value)[:70]):
                reply = self.raw(value)
                self.assertEqual(reply['error']['outcome'], 'not_dispatched')
        self.assertEqual(self.broker.calls, [])
        self.assertEqual(self.client.observe(RESOURCE).state, 'running')
        self.assertEqual(self.thread_errors, [])

    def test_operation_and_exact_uid_gid_denials_precede_broker_lookup(self):
        self.shutdown()
        policy = replace(self.policy, resources=(replace(self.access, operations=frozenset({'ensure'})),))
        self.server = BrokerServer(directory=self.directory, broker=self.broker, peers=(policy,))
        self.stop = threading.Event()
        self.start_thread()
        with self.assertRaises(IPCError) as denied:
            self.client.observe(RESOURCE)
        self.assertEqual((denied.exception.code, denied.exception.outcome), ('ipc_operation_denied', 'not_dispatched'))
        self.shutdown()
        policy = replace(self.policy, gid=(os.getgid() + 1) % 65535)
        self.server = BrokerServer(directory=self.directory, broker=self.broker, peers=(policy,))
        self.stop = threading.Event()
        self.start_thread()
        with self.assertRaises(IPCError):
            self.client.observe(RESOURCE)
        self.assertEqual(self.broker.calls, [])

    def test_expired_or_excessively_future_dispatch_deadline_never_calls_broker(self):
        now = time.clock_gettime(time.CLOCK_BOOTTIME)
        for deadline in (now - 1, now + 100):
            reply = self.raw(self.request(dispatch_before=deadline))
            self.assertEqual(reply['error'], {'code': 'ipc_dispatch_expired', 'outcome': 'not_dispatched'})
        self.assertEqual(self.broker.calls, [])

    def test_core_errors_have_unconfirmed_outcome_and_safe_codes(self):
        for error, code in [(BrokerError('lease_expired'), 'lease_expired'),
                (BrokerError('/private/issuer-key'), 'broker_operation_failed'),
                (RuntimeError('/private/raw-native-stderr'), 'broker_operation_failed')]:
            with self.subTest(code=code), patch.object(self.broker, 'observe', side_effect=error):
                with self.assertRaises(IPCError) as failed:
                    self.client.observe(RESOURCE)
                self.assertEqual((failed.exception.code, failed.exception.outcome), (code, 'unconfirmed'))

    def test_projection_filters_host_private_diagnostics(self):
        result = Observation(RESOURCE, 'reconciling', 1, 1, error_code='/private/secret',
            stop_cause='/var/host/path', limits_error={'code': 'resource_limits_cpu_mismatch',
                'phase': 'control_values', 'path': '/private/issuer', 'pid': 123})
        with patch.object(self.broker, 'observe', return_value=result):
            observed = self.client.observe(RESOURCE)
        self.assertEqual(observed.error_code, 'broker_operation_failed')
        self.assertIsNone(observed.stop_cause)
        self.assertEqual(observed.limits_error, {'code': 'resource_limits_cpu_mismatch', 'phase': 'control_values'})
        self.assertNotIn('/private', repr(observed))
        for cause in ('configuration_changed', 'libvirt_call_unconfirmed'):
            with self.subTest(cause=cause), patch.object(self.broker, 'observe',
                    return_value=replace(result, stop_cause=cause)):
                self.assertEqual(self.client.observe(RESOURCE).stop_cause, cause)

    def test_client_timeout_does_not_retry_or_cancel_dispatched_operation(self):
        started = threading.Event()
        finish = threading.Event()
        calls = []
        def slow(resource_id):
            calls.append(resource_id)
            started.set()
            finish.wait(3)
            return Observation(resource_id, 'running', 1, 1)
        client = BrokerClient(directory=self.directory, server_uid=os.getuid(), server_gid=os.getgid(), timeout_seconds=.1)
        with patch.object(self.broker, 'observe', side_effect=slow):
            try:
                with self.assertRaises(IPCError) as failed:
                    client.observe(RESOURCE)
                self.assertEqual(failed.exception.outcome, 'unconfirmed')
                self.assertTrue(started.is_set())
                self.assertEqual(calls, [RESOURCE])
            finally:
                finish.set()
        self.assertEqual(self.client.observe(RESOURCE).state, 'running')

    def test_queued_request_expiring_before_core_entry_is_not_dispatched(self):
        started = threading.Event()
        finish = threading.Event()
        calls = []
        def slow(resource_id):
            calls.append(resource_id)
            started.set()
            finish.wait(3)
            return Observation(resource_id, 'running', 1, 1)
        with patch.object(self.broker, 'observe', side_effect=slow):
            with self.connection() as first:
                first.send(json.dumps(self.request()).encode())
                self.assertTrue(started.wait(1))
                with self.connection() as queued:
                    queued.send(json.dumps(self.request(dispatch_before=time.clock_gettime(time.CLOCK_BOOTTIME) + .05)).encode())
                    time.sleep(.08)
                    finish.set()
                    first.recv(ipc.REPLY_LIMIT)
                    reply = json.loads(queued.recv(ipc.REPLY_LIMIT))
                    self.assertEqual(reply['error']['code'], 'ipc_dispatch_expired')
                    self.assertEqual(calls, [RESOURCE])

    def test_close_keeps_singleton_lock_until_dispatched_operation_finishes(self):
        started = threading.Event()
        finish = threading.Event()
        def slow(resource_id):
            started.set()
            finish.wait(3)
            return Observation(resource_id, 'running', 1, 1)
        with patch.object(self.broker, 'observe', side_effect=slow):
            with self.connection() as connection:
                connection.send(json.dumps(self.request()).encode())
                self.assertTrue(started.wait(1))
                try:
                    self.server.close()
                    with self.assertRaises(IPCError) as busy:
                        BrokerServer(directory=self.directory, broker=self.broker, peers=(self.policy,))
                    self.assertEqual(busy.exception.code, 'ipc_listener_in_use')
                finally:
                    finish.set()
                connection.recv(ipc.REPLY_LIMIT)
        self.thread.join(3)
        self.assertFalse(self.thread.is_alive())

    def test_close_refuses_accepted_idle_connection_before_core_admission(self):
        baseline = len(list(Path('/proc/self/fd').iterdir()))
        with self.connection() as connection:
            # One client FD plus one accepted server FD proves acceptance,
            # without reaching into the listener Implementation.
            until = time.monotonic() + .5
            while len(list(Path('/proc/self/fd').iterdir())) < baseline + 2:
                self.assertLess(time.monotonic(), until, 'connection was not accepted')
                time.sleep(.005)
            self.server.close()
            connection.send(json.dumps(self.request()).encode())
            reply = json.loads(connection.recv(ipc.REPLY_LIMIT))
            self.assertEqual(reply['error'], {'code': 'ipc_closed', 'outcome': 'not_dispatched'})
        self.thread.join(3)
        self.assertFalse(self.thread.is_alive())
        self.assertEqual(self.broker.calls, [])

    def test_client_refuses_wrong_server_identity_before_request(self):
        client = BrokerClient(directory=self.directory, server_uid=os.getuid(),
            server_gid=(os.getgid() + 1) % 65535)
        with self.assertRaises(IPCError) as failed:
            client.observe(RESOURCE)
        self.assertEqual(failed.exception.outcome, 'not_dispatched')
        self.assertEqual(self.broker.calls, [])

    def test_received_file_descriptors_are_closed_before_rejection(self):
        with tempfile.TemporaryFile(dir=self.directory) as stream:
            marker = os.readlink('/proc/self/fd/' + str(stream.fileno()))
            def references():
                count = 0
                for path in Path('/proc/self/fd').iterdir():
                    try:
                        count += os.readlink(path) == marker
                    except FileNotFoundError:
                        pass
                return count
            self.assertEqual(references(), 1)
            with self.connection() as connection:
                descriptors = array.array('i', [stream.fileno()] * 253)
                connection.sendmsg([json.dumps(self.request()).encode()],
                    [(socket.SOL_SOCKET, socket.SCM_RIGHTS, descriptors)])
                reply = json.loads(connection.recv(ipc.REPLY_LIMIT))
                self.assertEqual(reply['error']['code'], 'ipc_ancillary_invalid')
                self.assertEqual(references(), 1)
        self.assertEqual(self.broker.calls, [])

    def test_client_rejects_reply_descriptors_and_closes_them(self):
        self.shutdown()
        with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
            listener.bind(str(self.directory / ipc.SOCKET_NAME))
            listener.listen(1)
            with tempfile.TemporaryFile(dir=self.directory) as stream:
                marker = os.readlink('/proc/self/fd/' + str(stream.fileno()))
                def send():
                    connection, _ = listener.accept()
                    with connection:
                        request = json.loads(connection.recv(ipc.REQUEST_LIMIT))
                        reply = {'version': 1, 'request_id': request['request_id'], 'observation': {}}
                        connection.sendmsg([json.dumps(reply).encode()], [(socket.SOL_SOCKET,
                            socket.SCM_RIGHTS, array.array('i', [stream.fileno()] * 253))])
                worker = threading.Thread(target=send, daemon=True)
                worker.start()
                with self.assertRaises(IPCError) as failed:
                    self.client.observe(RESOURCE)
                worker.join(3)
                self.assertFalse(worker.is_alive())
                self.assertEqual((failed.exception.code, failed.exception.outcome), ('ipc_ancillary_invalid', 'unconfirmed'))
                targets = []
                for path in Path('/proc/self/fd').iterdir():
                    try:
                        targets.append(os.readlink(path))
                    except FileNotFoundError:
                        pass
                self.assertEqual(targets.count(marker), 1)

    def test_client_malformed_or_oversized_replies_are_unconfirmed(self):
        self.shutdown()
        invalid = [
            lambda request: {'version': 1, 'request_id': request['request_id'],
                'error': {'code': 'ipc_resource_denied', 'outcome': []}},
            lambda request: {'version': 1, 'request_id': request['request_id'],
                'error': {'code': '/private/issuer', 'outcome': 'not_dispatched'}},
            lambda request: {'version': True, 'request_id': request['request_id'], 'observation': {}},
            lambda request: {'version': 1, 'request_id': 'b' * 32, 'observation': {}},
            lambda request: {'version': 1, 'request_id': request['request_id'], 'observation': []},
            lambda request: b'\xff',
            lambda request: b'{' + b' ' * ipc.REPLY_LIMIT + b'}',
            lambda request: b'{"version":1,"version":1}',
        ]
        with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
            listener.bind(str(self.directory / ipc.SOCKET_NAME))
            listener.listen(1)
            for build in invalid:
                def send():
                    connection, _ = listener.accept()
                    with connection:
                        request = json.loads(connection.recv(ipc.REQUEST_LIMIT))
                        reply = build(request)
                        connection.send(reply if type(reply) is bytes else json.dumps(reply).encode())
                worker = threading.Thread(target=send, daemon=True)
                worker.start()
                with self.assertRaises(IPCError) as failed:
                    self.client.observe(RESOURCE)
                worker.join(3)
                self.assertFalse(worker.is_alive())
                self.assertEqual(failed.exception.outcome, 'unconfirmed')

    def test_delegated_connection_rejects_different_sender_pid(self):
        with self.connection() as connection:
            program = "import os,socket,sys; s=socket.socket(fileno=int(sys.argv[1])); s.send(bytes.fromhex(sys.argv[2]))"
            result = subprocess.run([sys.executable, '-c', program, str(connection.fileno()),
                json.dumps(self.request()).encode().hex()], pass_fds=(connection.fileno(),),
                capture_output=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr.decode())
            reply = json.loads(connection.recv(ipc.REPLY_LIMIT))
            self.assertEqual(reply['error']['code'], 'ipc_ancillary_invalid')
        self.assertEqual(self.broker.calls, [])

    def test_clean_close_retains_lock_and_removes_only_owned_socket(self):
        self.shutdown()
        self.assertFalse((self.directory / ipc.SOCKET_NAME).exists())
        lock = self.directory / ipc.LOCK_NAME
        self.assertIsNone(json.loads(lock.read_bytes())['socket'])
        identity = lock.stat().st_ino
        with BrokerServer(directory=self.directory, broker=self.broker, peers=(self.policy,)):
            self.assertEqual(lock.stat().st_ino, identity)
        self.assertEqual(lock.stat().st_ino, identity)

    def test_unrecorded_socket_is_preserved(self):
        self.shutdown()
        path = self.directory / ipc.SOCKET_NAME
        with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as foreign:
            foreign.bind(str(path))
            path.chmod(0o660)
            identity = path.stat().st_ino
            with self.assertRaises(IPCError):
                BrokerServer(directory=self.directory, broker=self.broker, peers=(self.policy,))
            self.assertEqual(path.stat().st_ino, identity)

    def test_reused_socket_inode_cannot_inherit_stale_cleanup_authority(self):
        self.shutdown()
        path = self.directory / ipc.SOCKET_NAME
        with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as foreign:
            foreign.bind(str(path))
            path.chmod(0o660)
            info = path.stat()
            lock = self.directory / ipc.LOCK_NAME
            metadata = json.loads(lock.read_bytes())
            # Model immediate device/inode reuse without relying on allocator
            # behavior. The old socket's metadata-change instant differs.
            metadata['socket'] = [info.st_dev, info.st_ino, info.st_ctime_ns - 1]
            lock.write_text(json.dumps(metadata))
            with self.assertRaises(IPCError) as failed:
                BrokerServer(directory=self.directory, broker=self.broker, peers=(self.policy,))
            self.assertEqual(failed.exception.code, 'ipc_socket_untrusted')
            self.assertEqual(path.stat().st_ino, info.st_ino)

    def test_ready_process_sigkill_leaves_recorded_socket_recoverable(self):
        self.shutdown()
        lock_identity = (self.directory / ipc.LOCK_NAME).stat().st_ino
        program = '''
import os,sys,threading
from pathlib import Path
from apps.runner.resource_broker.ipc import BrokerServer,PeerPolicy,ResourceAccess
access=ResourceAccess(sys.argv[2],'org-a','workspace-a',frozenset({'observe'}))
server=BrokerServer(directory=Path(sys.argv[1]),broker=object(),peers=(PeerPolicy(os.getuid(),os.getgid(),(access,)),))
print('ready',flush=True)
server.serve(threading.Event())
'''
        child = subprocess.Popen([sys.executable, '-c', program, str(self.directory), RESOURCE],
            cwd=Path(__file__).resolve().parents[2], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})
        self.addCleanup(lambda: child.poll() is None and (child.kill(), child.wait()))
        import select
        self.assertTrue(select.select([child.stdout], [], [], 5)[0], 'listener readiness timed out')
        ready = child.stdout.readline()
        if ready != b'ready\n':
            self.fail('listener failed: ' + child.stderr.read().decode())
        child.kill()
        child.wait(timeout=5)
        child.stdout.close()
        child.stderr.close()
        self.assertTrue((self.directory / ipc.SOCKET_NAME).exists())
        with BrokerServer(directory=self.directory, broker=self.broker, peers=(self.policy,)):
            self.assertEqual((self.directory / ipc.LOCK_NAME).stat().st_ino, lock_identity)

    def test_matching_recorded_socket_with_active_listener_is_preserved(self):
        self.shutdown()
        path = self.directory / ipc.SOCKET_NAME
        with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as active:
            active.bind(str(path))
            path.chmod(0o660)
            active.listen(1)
            info = path.stat()
            lock = self.directory / ipc.LOCK_NAME
            metadata = json.loads(lock.read_bytes())
            metadata['socket'] = [info.st_dev, info.st_ino, info.st_ctime_ns]
            lock.write_text(json.dumps(metadata))
            with self.assertRaises(IPCError) as failed:
                BrokerServer(directory=self.directory, broker=self.broker, peers=(self.policy,))
            self.assertEqual(failed.exception.code, 'ipc_socket_active')
            self.assertEqual(path.stat().st_ino, info.st_ino)

    def test_lock_and_socket_replacement_are_preserved_and_no_further_dispatch_occurs(self):
        path = self.directory / ipc.SOCKET_NAME
        path.unlink()
        with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as replacement:
            replacement.bind(str(path))
            path.chmod(0o660)
            inode = path.stat().st_ino
            self.thread.join(2)
            self.assertFalse(self.thread.is_alive())
            self.assertTrue(self.thread_errors)
            self.assertEqual(path.stat().st_ino, inode)
        self.assertEqual(self.broker.calls, [])

    def test_replaced_lock_is_preserved_and_stops_listener(self):
        lock = self.directory / ipc.LOCK_NAME
        replacement = self.directory / 'replacement'
        replacement.write_bytes(b'foreign metadata')
        replacement.chmod(0o600)
        replacement.replace(lock)
        identity = lock.stat().st_ino
        self.thread.join(2)
        self.assertFalse(self.thread.is_alive())
        self.assertTrue(self.thread_errors)
        self.assertEqual(lock.stat().st_ino, identity)
        self.assertEqual(lock.read_bytes(), b'foreign metadata')
        self.assertEqual(self.broker.calls, [])


@unittest.skipUnless(sys.platform == 'linux', 'Linux host-local IPC required')
class BrokerCoreIPCTests(unittest.TestCase):
    def setUp(self):
        from scripts.qualification.test_resource_broker import BrokerInterfaceTests
        self.fixture = BrokerInterfaceTests(methodName='runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.directory = self.fixture.root / 'ipc'
        self.directory.mkdir(mode=0o700)
        # The fixture's public claim builder is authoritative for its scope.
        policy = PeerPolicy(os.getuid(), os.getgid(), tuple(ResourceAccess(template.resource_id,
            self.fixture.claims(index)['organization_id'], self.fixture.claims(index)['workspace_id'], ipc.OPERATIONS)
            for index, template in enumerate(self.fixture.templates)))
        self.server = BrokerServer(directory=self.directory, broker=self.fixture.broker, peers=(policy,))
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.server.serve, args=(self.stop,), daemon=True)
        self.thread.start()
        self.addCleanup(self.shutdown)
        self.client = BrokerClient(directory=self.directory, server_uid=os.getuid(), server_gid=os.getgid())

    def shutdown(self):
        self.stop.set()
        self.thread.join(5)
        self.server.close()
        self.assertFalse(self.thread.is_alive())

    def test_real_core_signed_lifecycle_persists_and_rejects_reuse(self):
        assignment = self.fixture.assignment()
        self.assertEqual(self.client.ensure(assignment).state, 'running')
        grant = sign_grant(self.fixture.key, **self.fixture.claims(generation=2))
        self.assertEqual(self.client.renew(grant).generation, 2)
        self.assertEqual(self.client.observe(assignment.resource_id).state, 'running')
        with self.assertRaises(IPCError) as stale:
            self.client.renew(grant)
        self.assertEqual((stale.exception.code, stale.exception.outcome), ('generation_stale', 'unconfirmed'))
        stopped = self.client.revoke(assignment.resource_id)
        self.assertEqual(stopped.state, 'stopped')
        self.assertEqual(stopped.stop_evidence.method, 'libvirt_destroy_then_absent')
        self.assertEqual(self.fixture.new_broker().observe(assignment.resource_id), stopped)
        with self.assertRaises(IPCError):
            self.client.ensure(assignment)
        self.assertEqual(self.fixture.libvirt.creates, 1)

    def test_real_core_rejects_forged_signature_without_dispatch(self):
        assignment = self.fixture.assignment()
        with self.assertRaises(IPCError) as failed:
            self.client.ensure(replace(assignment, signature='f' * 64))
        self.assertEqual((failed.exception.code, failed.exception.outcome), ('signature_invalid', 'unconfirmed'))
        self.assertEqual(self.fixture.libvirt.creates, 0)


class BrokerIPCConfigurationTests(unittest.TestCase):
    def test_unsupported_platform_is_explicit(self):
        with patch.object(ipc.sys, 'platform', 'darwin'):
            with self.assertRaises(IPCError) as failed:
                BrokerClient(directory=Path('/unused'), server_uid=1, server_gid=1)
        self.assertEqual(failed.exception.code, 'ipc_platform_unsupported')

    @unittest.skipUnless(sys.platform == 'linux', 'Linux host-local IPC required')
    def test_expired_total_budget_during_trusted_fs_check_prevents_connect(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary).resolve()
            directory.chmod(0o700)
            client = BrokerClient(directory=directory, server_uid=os.getuid(), server_gid=os.getgid(), timeout_seconds=.1)
            clock = {'now': 100.0}
            read_xattr = ipc.os.getxattr
            def delayed_acl(*args, **kwargs):
                try:
                    return read_xattr(*args, **kwargs)
                finally:
                    clock['now'] = 101.0
            with patch.object(ipc, '_boottime', side_effect=lambda: clock['now']), \
                    patch.object(ipc.os, 'getxattr', side_effect=delayed_acl), \
                    patch.object(ipc.socket.socket, 'connect', side_effect=AssertionError('expired client attempted connect')) as connect:
                with self.assertRaises(IPCError) as failed:
                    client.observe(RESOURCE)
                self.assertEqual((failed.exception.code, failed.exception.outcome), ('ipc_timeout', 'not_dispatched'))
                connect.assert_not_called()

    @unittest.skipUnless(sys.platform == 'linux', 'Linux ACL xattrs required')
    def test_directory_acl_and_acl_read_errors_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary).resolve()
            directory.chmod(0o700)
            for error in (OSError(errno.EACCES, 'private native detail'), OSError(errno.EIO, 'native')):
                with patch.object(ipc.os, 'getxattr', side_effect=error):
                    with self.assertRaises(IPCError) as failed:
                        BrokerClient(directory=directory, server_uid=os.getuid(), server_gid=os.getgid())
                    self.assertEqual(failed.exception.code, 'ipc_directory_invalid')
            with patch.object(ipc.os, 'getxattr', return_value=b'extended-acl'):
                with self.assertRaises(IPCError):
                    BrokerClient(directory=directory, server_uid=os.getuid(), server_gid=os.getgid())

    @unittest.skipUnless(sys.platform == 'linux', 'Linux ACL xattrs required')
    def test_actual_named_access_and_default_acls_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary).resolve()
            directory.chmod(0o700)
            # Linux POSIX ACL xattr v2, one extra user, and a finite mask.
            extra_uid = (os.getuid() + 1) % 65535
            entries = [(1, 7, 0xffffffff), (2, 1, extra_uid), (4, 0, 0xffffffff),
                (16, 5, 0xffffffff), (32, 0, 0xffffffff)]
            value = struct.pack('<I', 2) + b''.join(struct.pack('<HHI', *entry) for entry in entries)
            for name in ('system.posix_acl_access', 'system.posix_acl_default'):
                with self.subTest(name=name):
                    try:
                        os.setxattr(directory, name, value)
                    except OSError as error:
                        if error.errno in {errno.ENOTSUP, errno.EOPNOTSUPP}:
                            self.skipTest('filesystem does not support POSIX ACL xattrs')
                        raise
                    try:
                        with self.assertRaises(IPCError) as failed:
                            BrokerClient(directory=directory, server_uid=os.getuid(), server_gid=os.getgid())
                        self.assertEqual(failed.exception.code, 'ipc_directory_invalid')
                    finally:
                        os.removexattr(directory, name)
                        directory.chmod(0o700)

    @unittest.skipUnless(sys.platform == 'linux', 'Linux host-local IPC required')
    def test_policy_validation_is_bounded_and_provisioning_never_repairs_permissions(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary).resolve()
            access = ResourceAccess(RESOURCE, 'org-a', 'workspace-a', frozenset({'observe'}))
            policy = PeerPolicy(os.getuid(), os.getgid(), (access,))
            directory.chmod(0o777)
            with self.assertRaises(IPCError):
                BrokerServer(directory=directory, broker=object(), peers=(policy,))
            self.assertEqual(directory.stat().st_mode & 0o777, 0o777)
            directory.chmod(0o700)
            invalid = [(), (policy,) * 17, (policy, policy),
                (replace(policy, uid=True),),
                (replace(policy, resources=(replace(access, operations=frozenset({'watch'})),)),),
                (replace(policy, resources=(access, access)),),
                (replace(policy, resources=(replace(access, operations={'observe'}),)),)]
            for peers in invalid:
                with self.subTest(peers=repr(peers)[:70]), self.assertRaises(IPCError) as failed:
                    BrokerServer(directory=directory, broker=object(), peers=peers)
                self.assertEqual(failed.exception.code, 'ipc_policy_invalid')
            self.assertFalse((directory / ipc.LOCK_NAME).exists())


if __name__ == '__main__':
    unittest.main()
