#!/usr/bin/env python3
"""Finite Linux UDS process acceptance; the persistent backend is synthetic.

Uses the production IPC server/client and real kernel credentials. No VM,
network listener, model request, production service account or administrator
change is made. Raw identities, protocol and sources stay in private evidence.
"""
from __future__ import annotations

import argparse
import array
import base64
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import selectors
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
import uuid

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
SOURCE_FILES = (
    'apps/runner/resource_broker/__init__.py',
    'apps/runner/resource_broker/resources.py',
    'apps/runner/resource_broker/ipc.py',
    'scripts/qualification/qualify_broker_ipc.py',
    'scripts/qualification/test_broker_ipc_operator.py',
)
BUDGET = {
    'wall_seconds': 180, 'workers_total': 48, 'workers_concurrent': 3,
    'worker_start_seconds': 4, 'worker_reply_seconds': 5,
    'worker_exit_seconds': 3, 'client_timeout_seconds': 2,
    'source_file_bytes': 384 * 1024, 'source_total_bytes': 2 * 1024**2,
    'worker_output_bytes': 16 * 1024, 'worker_stderr_bytes': 256 * 1024,
    'state_bytes': 128 * 1024, 'evidence_bytes': 8 * 1024**2,
    # Metadata-only qualification creates no image/build cache. Local runs
    # still follow the user's separate pre-build 50 GiB assessment rule.
    'minimum_free_bytes': 1024**3,
}
FALSE_CLAIMS = {key: False for key in (
    'virtual_machine_isolation_verified', 'actual_vm_stop_verified',
    'dedicated_production_uid_verified', 'control_plane_authorization_verified',
    'signed_envelope_policy_verified', 'power_loss_verified',
    'full_m0_05_accepted',
)}
REQUIRED_CHECKS = frozenset({
    'independent_process_authorized_calls', 'peer_identity_policy_denied',
    'client_authenticates_server', 'operation_resource_scope_denied_before_backend',
    'malformed_oversized_ancillary_denied_without_fd_leak',
    'stalled_peer_deadline_preserves_service',
    'inherited_socket_sender_pid_denied', 'duplicate_request_is_not_transport_deduplicated',
    'lost_reply_unconfirmed_no_automatic_retry', 'restart_observes_durable_fixture_effect',
    'listener_ownership_and_exact_cleanup', 'all_owned_workers_exited',
    'immutable_sources', 'immutable_runtime_directories',
})


def require(value, message):
    if not value:
        raise RuntimeError(message)


def no_symlinks(path):
    path = Path(path).absolute()
    for item in (path, *path.parents):
        if item.is_symlink():
            raise RuntimeError('Symlink path rejected')


def private_directory(path, *, create=True):
    path = Path(path)
    no_symlinks(path)
    if create:
        path.mkdir(mode=0o700, exist_ok=True)
    info = path.lstat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid()
            and stat.S_IMODE(info.st_mode) == 0o700,
            'Private directory identity or permissions differ')
    return (info.st_dev, info.st_ino)


def bounded_read(path, limit):
    no_symlinks(path)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    try:
        info = os.fstat(descriptor)
        require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_size <= limit,
                'File type, link count or byte bound differs')
        pieces, total = [], 0
        while piece := os.read(descriptor, min(65536, limit + 1 - total)):
            total += len(piece)
            require(total <= limit, 'File changed beyond byte bound')
            pieces.append(piece)
        require(total == info.st_size, 'File changed during read')
        return b''.join(pieces)
    finally:
        os.close(descriptor)


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def write(path, value):
    body = value if isinstance(value, bytes) else encode(value) + b'\n'
    no_symlinks(path.parent)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        remaining = memoryview(body)
        while remaining:
            count = os.write(descriptor, remaining)
            require(count > 0, 'Evidence short write')
            remaining = remaining[count:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    return hashlib.sha256(body).hexdigest()


def snapshot_sources(root, reports):
    destination = reports / 'source'
    private_directory(destination)
    hashes, total = {}, 0
    for relative in SOURCE_FILES:
        body = bounded_read(root / relative, BUDGET['source_file_bytes'])
        total += len(body)
        require(total <= BUDGET['source_total_bytes'], 'Source snapshot total bound')
        target = destination / relative
        for parent in reversed(target.parents):
            if parent == destination or destination in parent.parents:
                private_directory(parent)
        hashes[relative] = write(target, body)
    return hashes


def safe_summary(result, report_sha256=None):
    """Only allowlisted static keys/booleans reach public command output."""
    hashes = result.get('sources_sha256', {})
    known_hashes = {key: value for key in SOURCE_FILES if isinstance((value := hashes.get(key)), str)
                    and len(value) == 64 and all(c in '0123456789abcdef' for c in value)}
    observed = result.get('environment', {})
    observed = observed if type(observed) is dict else {}
    environment = {key: value for key in ('system', 'kernel_release', 'machine', 'python')
        if type((value := observed.get(key))) is str
        and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._+:-]{0,127}', value) is not None}
    cpu_count = observed.get('logical_cpu_count')
    if type(cpu_count) is int and 1 <= cpu_count <= 1048576:
        environment['logical_cpu_count'] = cpu_count
    summary = {
        'passed': result.get('passed') is True,
        'checks': {key: result.get('checks', {}).get(key) is True for key in sorted(REQUIRED_CHECKS)},
        'gate_count': len(REQUIRED_CHECKS),
        'gates_passed': sum(result.get('checks', {}).get(key) is True for key in REQUIRED_CHECKS),
        'synthetic_backend': True,
        'backend_scope': 'deterministic_persistent_admitted_broker_interface_adapter',
        'real_linux_uds_processes': result.get('checks', {}).get('independent_process_authorized_calls') is True,
        'peer_identity_negative_scope': 'same_development_account_kernel_credentials_vs_wrong_configured_uid',
        'sources_sha256': known_hashes,
        'outcome': 'passed' if result.get('passed') is True else 'failed',
        'workers_started': len(result.get('worker_returncodes', [])),
        'deadlines_seconds': {key: BUDGET[key] for key in ('wall_seconds', 'worker_start_seconds',
            'worker_reply_seconds', 'worker_exit_seconds', 'client_timeout_seconds')},
        'real_model_calls': 0, 'virtual_machines_started': 0,
        'tcp_listeners_started': 0, 'administrator_changes': 0,
        'milestone_complete': False, 'claims': dict(FALSE_CLAIMS),
        'environment': environment,
    }
    if isinstance(report_sha256, str) and len(report_sha256) == 64 and all(c in '0123456789abcdef' for c in report_sha256):
        summary['private_report_sha256'] = report_sha256
    return summary


def api():
    from apps.runner import resource_broker as native
    from apps.runner.resource_broker import ipc
    return native, ipc


class PersistentFixture:
    """Deterministic admitted Broker-interface adapter, never a VM backend.

    Invocation and effect counts are independent private disk observations.
    This fixture only accepts the exact pre-generated signed test envelopes;
    general signature, lease and VM lifecycle policy remain core test scope.
    """
    def __init__(self, directory, config):
        self.directory, self.config = directory, config
        self.path = directory / 'fixture-state.json'
        if not self.path.exists():
            write(self.path, {'invocations': [], 'effects': 0, 'records': {}})

    def _load(self):
        return json.loads(bounded_read(self.path, BUDGET['state_bytes']))

    def _save(self, body):
        temporary = self.directory / ('fixture-' + uuid.uuid4().hex + '.tmp')
        write(temporary, body)
        os.replace(temporary, self.path)
        descriptor = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _call(self, operation, resource_id, envelope=None, cause=None):
        native, _ = api()
        body = self._load()
        require(len(body['invocations']) < 64, 'Fixture invocation bound')
        call = {'operation': operation, 'resource_id': resource_id}
        if envelope is not None:
            actual = asdict(envelope)
            require(actual == self.config[operation], 'Exact signed fixture envelope differs')
            call['envelope_sha256'] = hashlib.sha256(encode(actual)).hexdigest()
        if cause is not None:
            call['cause'] = cause
        body['invocations'].append(call)
        record = body['records'].get(resource_id)
        if operation == 'ensure' and record is None:
            record = {'state': 'running', 'generation': envelope.generation,
                      'recovery_generation': envelope.recovery_generation}
            body['records'][resource_id] = record
            body['effects'] += 1
        elif operation == 'renew':
            require(record is not None, 'Fixture resource missing')
            record['generation'] = envelope.generation
            body['effects'] += 1
        elif operation == 'revoke' and record is not None and record['state'] != 'stopped':
            record['state'] = 'stopped'
            body['effects'] += 1
        self._save(body)
        if operation == 'ensure' and self.config.get('pause_after_effect'):
            marker = self.directory / 'effect-durable.json'
            if not marker.exists():
                write(marker, {'effect_count': body['effects'], 'invocations': len(body['invocations'])})
            # An external owned-process SIGKILL ends this known checkpoint.
            # The finite wait also bounds it if the parent itself disappears.
            until = time.monotonic() + 10
            while time.monotonic() < until:
                time.sleep(.05)
        if record is None:
            raise native.BrokerError('resource_not_registered')
        return native.Observation(resource_id, record['state'], record['generation'], record['recovery_generation'])

    def ensure(self, assignment):
        return self._call('ensure', assignment.resource_id, assignment)

    def renew(self, grant):
        return self._call('renew', grant.resource_id, grant)

    def observe(self, resource_id):
        return self._call('observe', resource_id)

    def revoke(self, resource_id, cause):
        return self._call('revoke', resource_id, cause=cause)


def worker(config):
    """Private subprocess entry; stdout is a small finite control protocol."""
    import resource
    resource.setrlimit(resource.RLIMIT_FSIZE, (BUDGET['worker_stderr_bytes'], BUDGET['worker_stderr_bytes']))
    os.umask(0o077)
    native, ipc = api()
    action = config['action']
    if action == 'server':
        stop = threading.Event()
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
        directory = Path(config['directory'])
        peer = ipc.PeerPolicy(config['peer_uid'], config['peer_gid'], tuple(
            ipc.ResourceAccess(item['resource_id'], item['organization_id'], item['workspace_id'], frozenset(item['operations']))
            for item in config['resources']))
        with ipc.BrokerServer(directory=directory, broker=PersistentFixture(directory, config), peers=(peer,)) as server:
            print(json.dumps({'ready': True, 'pid': os.getpid(), 'uid': os.geteuid(), 'gid': os.getegid()}), flush=True)
            server.serve(stop)
        return
    try:
        if action == 'client':
            client = ipc.BrokerClient(directory=Path(config['directory']), server_uid=config['server_uid'],
                server_gid=config['server_gid'], timeout_seconds=config.get('timeout_seconds', BUDGET['client_timeout_seconds']))
            operation = config['operation']
            if operation in ('ensure', 'renew'):
                envelope = (native.Assignment if operation == 'ensure' else native.Grant)(**config['envelope'])
                result = getattr(client, operation)(envelope)
            elif operation == 'observe':
                result = client.observe(config['resource_id'])
            else:
                result = client.revoke(config['resource_id'], cause=config.get('cause', 'cancelled'))
            print(json.dumps({'ok': True, 'observation': asdict(result), 'pid': os.getpid()}), flush=True)
        elif action in ('raw', 'inherited'):
            if action == 'inherited':
                connection = socket.socket(fileno=config['descriptor'])
            else:
                connection = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
                connection.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
                connection.connect(config['socket'])
            with connection:
                connection.settimeout(3)
                payload = base64.b64decode(config['payload'], validate=True)
                descriptors = []
                try:
                    ancillary = []
                    if config.get('send_fd'):
                        descriptors = [os.open('/dev/null', os.O_RDONLY | os.O_CLOEXEC)]
                        ancillary = [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array('i', descriptors))]
                    try:
                        connection.sendmsg([payload], ancillary)
                        reply, controls, flags, _ = connection.recvmsg(16384, socket.CMSG_SPACE(256), socket.MSG_CMSG_CLOEXEC)
                        for level, kind, data in controls:
                            if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
                                received = array.array('i'); received.frombytes(data[:len(data) - len(data) % received.itemsize])
                                for descriptor in received:
                                    os.close(descriptor)
                        parsed = json.loads(reply) if reply else None
                        print(json.dumps({'ok': True, 'reply': parsed, 'flags': flags, 'closed': not reply, 'pid': os.getpid()}), flush=True)
                    except (ConnectionResetError, BrokenPipeError):
                        print(json.dumps({'ok': True, 'closed': True, 'pid': os.getpid()}), flush=True)
                finally:
                    for descriptor in descriptors:
                        os.close(descriptor)
        else:
            raise RuntimeError('Unknown private worker action')
    except native.BrokerError as error:
        print(json.dumps({'ok': False, 'code': error.code, 'outcome': getattr(error, 'outcome', None), 'pid': os.getpid()}), flush=True)


class OwnedProcess:
    def __init__(self, process, stdout, stderr, protocol, deadline):
        self.process, self.stdout, self.stderr = process, stdout, stderr
        self.protocol, self.deadline = protocol, deadline
        self.buffer, self.received = bytearray(), 0

    def reply(self, seconds):
        until = min(self.deadline, time.monotonic() + seconds)
        selector = selectors.DefaultSelector()
        selector.register(self.process.stdout, selectors.EVENT_READ)
        try:
            while b'\n' not in self.buffer:
                remaining = until - time.monotonic()
                require(remaining > 0, 'Worker reply deadline')
                require(selector.select(remaining), 'Worker reply deadline')
                piece = os.read(self.process.stdout.fileno(), 4096)
                require(piece, 'Worker exited without reply')
                self.received += len(piece)
                require(self.received <= BUDGET['worker_output_bytes'], 'Worker output byte bound')
                self.buffer.extend(piece)
                self.stdout.write(piece); self.stdout.flush()
            line, _, remainder = self.buffer.partition(b'\n')
            self.buffer = bytearray(remainder)
            value = json.loads(line)
            require(isinstance(value, dict), 'Worker reply must be object')
            return value
        finally:
            selector.close()

    def close(self):
        errors = []
        try:
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=BUDGET['worker_exit_seconds'])
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=BUDGET['worker_exit_seconds'])
        except BaseException as error:
            errors.append({'phase': 'owned_child_exit', 'type': type(error).__name__})
        finally:
            for stream in (self.stdout, self.stderr):
                try:
                    if stream is not None and not stream.closed:
                        stream.flush()
                        os.fsync(stream.fileno())
                except BaseException as error:
                    errors.append({'phase': 'owned_child_log_sync', 'type': type(error).__name__})
            for stream in (self.process.stdout, self.stdout, self.stderr):
                try:
                    if stream is not None:
                        stream.close()
                except BaseException as error:
                    errors.append({'phase': 'owned_child_log_close', 'type': type(error).__name__})
        return errors


class Qualification:
    def __init__(self, root, reports, runtime, result):
        self.root, self.reports, self.runtime, self.result = root, reports, runtime, result
        self.deadline = time.monotonic() + BUDGET['wall_seconds']
        self.workers, self.directories = [], {}
        self.uid, self.gid = os.geteuid(), os.getegid()
        self.resource = '00000000-0000-4000-8000-000000000001'
        self.other = '00000000-0000-4000-8000-000000000002'
        native, _ = api()
        claims = {'resource_id': self.resource, 'organization_id': 'qualification-organization',
                  'workspace_id': 'qualification-workspace', 'attempt_id': 'qualification-attempt',
                  'template_id': 'qualification-template', 'configuration_sha256': 'a' * 64,
                  'generation': 1, 'recovery_generation': 1, 'issued_at': time.time(), 'expires_at': time.time() + 60}
        key = os.urandom(32)
        self.assignment = asdict(native.sign_assignment(key, **claims))
        self.grant = asdict(native.sign_grant(key, **{**claims, 'generation': 2}))

    def start(self, config, *, pass_fds=()):
        require(time.monotonic() < self.deadline, 'Qualification deadline')
        require(len(self.workers) < BUDGET['workers_total'], 'Worker count bound')
        require(sum(w.process.poll() is None for w in self.workers) < BUDGET['workers_concurrent'], 'Concurrent worker bound')
        index = len(self.workers)
        config_path = self.reports / ('worker-' + str(index) + '.json')
        write(config_path, config)
        stdout_path = self.reports / ('stdout-' + str(index) + '.jsonl')
        stderr_path = self.reports / ('stderr-' + str(index) + '.log')
        stdout = os.fdopen(os.open(stdout_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600), 'wb', buffering=0)
        try:
            stderr = os.fdopen(os.open(stderr_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600), 'wb', buffering=0)
        except BaseException:
            stdout.close()
            raise
        try:
            process = subprocess.Popen([sys.executable, '-B', str(self.root / 'scripts/qualification/qualify_broker_ipc.py'),
                '--worker-config', str(config_path)], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=stderr,
                cwd=self.root, env={'PATH': '/usr/bin:/bin', 'LC_ALL': 'C', 'PYTHONDONTWRITEBYTECODE': '1'},
                pass_fds=pass_fds, close_fds=True, start_new_session=True)
        except BaseException:
            stdout.close(); stderr.close()
            raise
        worker = OwnedProcess(process, stdout, stderr, stdout_path, self.deadline)
        worker.config = dict(config)
        self.workers.append(worker)  # Register before any further child I/O.
        return worker

    def finish(self, worker):
        require(worker in self.workers, 'Unregistered owned worker')
        worker.process.wait(timeout=min(BUDGET['worker_exit_seconds'], max(.01, self.deadline - time.monotonic())))
        require(worker.process.returncode == 0, 'Worker return code differs')

    def call(self, directory, operation, **arguments):
        config = {'action': 'client', 'directory': str(directory), 'server_uid': self.uid,
                  'server_gid': self.gid, 'operation': operation, 'resource_id': self.resource, **arguments}
        if operation in ('ensure', 'renew'):
            config.setdefault('envelope', self.assignment if operation == 'ensure' else self.grant)
        worker = self.start(config)
        reply = worker.reply(BUDGET['worker_reply_seconds'])
        self.finish(worker)
        return reply

    def server(self, name, **options):
        directory = self.runtime / name
        private_directory(directory)
        self.directories[directory] = private_directory(directory)
        config = {'action': 'server', 'directory': str(directory), 'peer_uid': self.uid, 'peer_gid': self.gid,
            'resources': [{'resource_id': self.resource, 'organization_id': 'qualification-organization',
                'workspace_id': 'qualification-workspace', 'operations': ['ensure', 'renew', 'observe', 'revoke']}],
            'ensure': self.assignment, 'renew': self.grant, **options}
        process = self.start(config)
        ready = process.reply(BUDGET['worker_start_seconds'])
        require(ready == {'ready': True, 'pid': process.process.pid, 'uid': self.uid, 'gid': self.gid}, 'Server kernel process identity differs')
        self.result['processes'].append(ready)
        return process, directory

    def state(self, directory):
        return json.loads(bounded_read(directory / 'fixture-state.json', BUDGET['state_bytes']))

    def socket_path(self, directory):
        paths = [p for p in directory.iterdir() if stat.S_ISSOCK(p.lstat().st_mode)]
        require(len(paths) == 1, 'Listener socket cardinality differs')
        return paths[0]

    def frame(self, operation='observe', arguments=None, request_id=None):
        return encode({'version': 1, 'request_id': request_id or uuid.uuid4().hex,
            'operation': operation, 'arguments': {'resource_id': self.resource} if arguments is None else arguments,
            'dispatch_before': time.clock_gettime(time.CLOCK_BOOTTIME) + 4})

    def raw(self, directory, payload, *, send_fd=False, inherited=None):
        config = {'action': 'raw', 'socket': str(self.socket_path(directory)),
                  'payload': base64.b64encode(payload).decode(), 'send_fd': send_fd}
        pass_fds = ()
        if inherited is not None:
            config.update(action='inherited', descriptor=inherited.fileno())
            pass_fds = (inherited.fileno(),)
        process = self.start(config, pass_fds=pass_fds)
        reply = process.reply(BUDGET['worker_reply_seconds'])
        self.finish(process)
        return reply

    def check(self, name, predicate):
        self.result['checks'][name] = bool(predicate)
        require(predicate, 'Acceptance predicate failed: ' + name)

    def stop(self, worker, *, kill=False):
        require(worker in self.workers and worker.process.poll() is None, 'Owned live process required')
        if kill:
            worker.process.kill()
        else:
            worker.process.terminate()
        worker.process.wait(timeout=BUDGET['worker_exit_seconds'])

    def startup_denied(self, config):
        worker = self.start(config)
        worker.process.wait(timeout=BUDGET['worker_start_seconds'])
        require(worker.process.returncode != 0, 'Conflicting listener startup succeeded')
        return worker

    @staticmethod
    def inode(path):
        info = path.lstat()
        return info.st_dev, info.st_ino

    def no_dispatch(self, reply):
        return reply.get('ok') is False and reply.get('outcome') == 'not_dispatched'

    def raw_denied(self, reply):
        return (reply.get('ok') is True and (reply.get('closed') is True
            or (isinstance(reply.get('reply'), dict) and isinstance(reply['reply'].get('error'), dict)
                and reply['reply']['error'].get('outcome') == 'not_dispatched')))

    @staticmethod
    def fd_count(worker):
        previous, stable = None, 0
        until = time.monotonic() + 1
        while time.monotonic() < until:
            current = len(tuple(Path('/proc', str(worker.process.pid), 'fd').iterdir()))
            stable = stable + 1 if current == previous else 0
            if stable >= 3:
                return current
            previous = current
            time.sleep(.02)
        raise RuntimeError('Server descriptors did not settle')

    def run(self):
        # Keep the first vertical case small: an independent real client and
        # listener, a signed envelope and an independently persisted effect.
        server, directory = self.server('a')
        first = self.call(directory, 'ensure')
        self.check('independent_process_authorized_calls', first.get('ok') is True
            and first['observation']['state'] == 'running'
            and first['pid'] != server.process.pid and len(self.state(directory)['invocations']) == 1
            and self.state(directory)['effects'] == 1)

        # A request ID correlates replies; retry policy belongs above transport.
        # Both matching frames enter the backend, whose durable ensure is
        # idempotent. No in-memory transport replay cache is implied.
        frame = self.frame('ensure', {'envelope': self.assignment})
        duplicate = [self.raw(directory, frame), self.raw(directory, frame)]
        state = self.state(directory)
        self.check('duplicate_request_is_not_transport_deduplicated',
            all(item.get('reply', {}).get('observation', {}).get('state') == 'running' and 'error' not in item['reply'] for item in duplicate)
            and len(state['invocations']) == 3 and state['effects'] == 1)

        # Successful calls across all four frozen operations remain actual
        # independent processes; the resulting effect is only fixture state.
        renewed = self.call(directory, 'renew')
        observed = self.call(directory, 'observe')
        revoked = self.call(directory, 'revoke')
        self.check('independent_process_authorized_calls', self.result['checks']['independent_process_authorized_calls']
            and renewed.get('ok') is True and renewed['observation']['generation'] == 2
            and observed.get('ok') is True and observed['observation']['state'] == 'running'
            and revoked.get('ok') is True and revoked['observation']['state'] == 'stopped'
            and self.state(directory)['effects'] == 3)

        before = len(self.state(directory)['invocations'])
        wrong_server = self.call(directory, 'observe', server_gid=self.gid + 10000)
        self.check('client_authenticates_server', self.no_dispatch(wrong_server)
            and len(self.state(directory)['invocations']) == before)

        stalled = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        stalled.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
        try:
            stalled.connect(str(self.socket_path(directory)))
            started = time.monotonic()
            queued = self.call(directory, 'observe')
            elapsed = time.monotonic() - started
            stalled.settimeout(2)
            timeout_reply = json.loads(stalled.recv(8192))
        finally:
            stalled.close()
        self.result['stalled_peer_wait_seconds'] = elapsed
        self.check('stalled_peer_deadline_preserves_service', queued.get('ok') is True
            and timeout_reply.get('error', {}).get('code') == 'ipc_timeout'
            and elapsed < BUDGET['client_timeout_seconds'] + .5)
        before = len(self.state(directory)['invocations'])

        fd_before = self.fd_count(server)
        malformed = [b'{', self.frame('arbitrary'), b'x' * 16385]
        expired = json.loads(self.frame()); expired['dispatch_before'] = time.clock_gettime(time.CLOCK_BOOTTIME) - 1
        malformed.append(encode(expired))
        rejected = [self.raw(directory, payload) for payload in malformed]
        rejected += [self.raw(directory, self.frame(), send_fd=True) for _ in range(4)]
        fd_after = self.fd_count(server)
        self.result['fd_observation'] = {'before': fd_before, 'after': fd_after, 'attempts': len(rejected)}
        self.check('malformed_oversized_ancillary_denied_without_fd_leak', all(self.raw_denied(r) for r in rejected)
            and before == len(self.state(directory)['invocations']) and fd_after == fd_before)

        # connect(2) records this parent's PID, while the inherited descriptor's
        # SCM_CREDENTIALS identifies the independent exec'd sender's PID.
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        connection.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
        try:
            connection.connect(str(self.socket_path(directory)))
            inherited = self.raw(directory, self.frame(), inherited=connection)
        finally:
            connection.close()
        self.check('inherited_socket_sender_pid_denied', self.raw_denied(inherited)
            and inherited['pid'] != os.getpid() and before == len(self.state(directory)['invocations']))

        live_path = self.socket_path(directory); live_identity = self.inode(live_path)
        self.startup_denied(server.config)
        active_preserved = self.inode(live_path) == live_identity and server.process.poll() is None
        require(active_preserved, 'Live listener was replaced')
        self.stop(server)
        require(not live_path.exists(), 'Graceful server close retained owned listener')

        wrong_peer, denied_dir = self.server('b', peer_uid=self.uid + 10000)
        denied = self.call(denied_dir, 'ensure')
        self.check('peer_identity_policy_denied', denied.get('ok') is False
            and denied.get('outcome') in {'not_dispatched', 'unconfirmed'}
            and self.state(denied_dir) == {'invocations': [], 'effects': 0, 'records': {}})
        self.result['peer_identity_negative_scope'] = 'actual_same_development_uid_vs_wrong_configured_uid'
        self.stop(wrong_peer)

        restricted, restricted_dir = self.server('c', resources=[{'resource_id': self.resource,
            'organization_id': 'qualification-organization', 'workspace_id': 'qualification-workspace',
            'operations': ['observe']}])
        operation = self.call(restricted_dir, 'revoke')
        resource = self.call(restricted_dir, 'observe', resource_id=self.other)
        self.stop(restricted)
        scoped, scoped_dir = self.server('d')
        scope = self.call(scoped_dir, 'ensure', envelope={**self.assignment, 'organization_id': 'other-organization'})
        self.check('operation_resource_scope_denied_before_backend', all(self.no_dispatch(r) for r in (operation, resource, scope))
            and self.state(restricted_dir)['invocations'] == [] and self.state(scoped_dir)['invocations'] == [])
        self.stop(scoped)

        # Lose a reply after the adapter's effect and invocation are durable.
        # The production client times out without replaying. SIGKILL is applied
        # only to the registered child; restart observes the original state.
        paused, paused_dir = self.server('u', pause_after_effect=True)
        pending = self.start({'action': 'client', 'directory': str(paused_dir), 'server_uid': self.uid,
            'server_gid': self.gid, 'operation': 'ensure', 'envelope': self.assignment, 'timeout_seconds': .6})
        until = min(self.deadline, time.monotonic() + BUDGET['worker_start_seconds'])
        while not (paused_dir / 'effect-durable.json').exists():
            require(time.monotonic() < until, 'Durable synthetic effect checkpoint deadline')
            time.sleep(.02)
        lost = pending.reply(BUDGET['worker_reply_seconds']); self.finish(pending)
        after_loss = self.state(paused_dir)
        self.check('lost_reply_unconfirmed_no_automatic_retry', lost.get('ok') is False
            and lost.get('outcome') == 'unconfirmed' and len(after_loss['invocations']) == 1 and after_loss['effects'] == 1)
        stale_path = self.socket_path(paused_dir); stale_identity = self.inode(stale_path)
        self.stop(paused, kill=True)
        require(self.inode(stale_path) == stale_identity, 'Killed listener inode changed')
        restarted, restarted_dir = self.server('u')
        post_restart = self.call(restarted_dir, 'observe')
        retried = self.call(restarted_dir, 'ensure')
        self.check('restart_observes_durable_fixture_effect', post_restart.get('ok') is True
            and post_restart['observation']['state'] == 'running' and retried.get('ok') is True
            and self.state(restarted_dir)['effects'] == 1 and len(self.state(restarted_dir)['invocations']) == 3)
        self.stop(restarted)

        # An unrelated same-account socket has no matching ownership metadata.
        foreign_dir = self.runtime / 'f'; private_directory(foreign_dir)
        self.directories[foreign_dir] = private_directory(foreign_dir)
        foreign_path = foreign_dir / live_path.name
        foreign = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        try:
            foreign.bind(str(foreign_path)); foreign_identity = self.inode(foreign_path)
            self.startup_denied({**server.config, 'directory': str(foreign_dir)})
            foreign_preserved = self.inode(foreign_path) == foreign_identity
        finally:
            foreign.close()
        require(foreign_preserved, 'Unowned listener inode changed')
        foreign_path.unlink()  # This exact inode was created and closed above.

        replaced, replaced_dir = self.server('r')
        path = self.socket_path(replaced_dir); original_identity = self.inode(path)
        original = replaced_dir / 'original.sock'; path.rename(original)
        replacement = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        try:
            replacement.bind(str(path)); replacement_identity = self.inode(path)
            if replaced.process.poll() is None:
                self.stop(replaced)
            replacement_preserved = self.inode(path) == replacement_identity
        finally:
            replacement.close()
        require(self.inode(original) == original_identity, 'Renamed owned socket changed')
        path.unlink(); original.unlink()  # Both exact owned endpoints are closed.
        self.check('listener_ownership_and_exact_cleanup', active_preserved and foreign_preserved and replacement_preserved)


def execute(root, runtime_root):
    root = Path(root).absolute(); runtime_root = Path(runtime_root).absolute()
    require(sys.platform == 'linux' and hasattr(socket, 'SO_PEERCRED') and hasattr(socket, 'SCM_CREDENTIALS'), 'Linux kernel credentials required')
    require(root / 'scripts/qualification/qualify_broker_ipc.py' == Path(__file__).absolute(), 'Executing source differs from requested root')
    os.umask(0o077)
    for path in (root / '.private', root / '.private/qualification'):
        private_directory(path)
    reports = root / '.private/qualification' / ('broker-ipc-native-' + uuid.uuid4().hex)
    private_directory(reports)
    result = {'passed': False, 'started_at': time.time(), 'checks': {}, 'claims': dict(FALSE_CLAIMS),
              'synthetic_backend': True, 'linux_kernel_credentials': True, 'processes': [], 'cleanup_errors': [],
              'environment': {'system': platform.system(), 'kernel_release': platform.release(),
                  'machine': platform.machine(), 'python': platform.python_version(), 'logical_cpu_count': os.cpu_count()}}
    qualification, hashes, runtime_identity, run_identity, run = None, {}, None, None, None
    try:
        result['budget_sha256'] = write(reports / 'budget.json', BUDGET)
        result['free_bytes_before'] = os.statvfs(root).f_bavail * os.statvfs(root).f_frsize
        require(result['free_bytes_before'] >= BUDGET['minimum_free_bytes'], 'Less than metadata acceptance free-space budget available')
        df = subprocess.run(['df', '-B1', str(root), str(runtime_root)], capture_output=True, timeout=5, check=True)
        require(len(df.stdout) <= 65536, 'df output bound')
        write(reports / 'df-before.txt', df.stdout)
        runtime_identity = private_directory(runtime_root, create=False)
        require(len(os.fsencode(runtime_root)) <= 70, 'Runtime root exceeds finite socket pathname bound')
        run = runtime_root / ('hi-' + uuid.uuid4().hex[:12])
        run_identity = private_directory(run)
        hashes = snapshot_sources(root, reports)
        result['sources_sha256'] = hashes
        result['runtime_identity'] = {'root': str(runtime_root), 'root_identity': runtime_identity,
                                      'run': str(run), 'run_identity': run_identity}
        result['python'] = {'version': platform.python_version(), 'executable': sys.executable}
        native, ipc = api()
        require(Path(native.__file__).resolve() == root / 'apps/runner/resource_broker/__init__.py'
            and Path(ipc.__file__).resolve() == root / 'apps/runner/resource_broker/ipc.py', 'Loaded module source differs')
        qualification = Qualification(root, reports, run, result)
        qualification.run()
    except BaseException as error:
        result['error_type'], result['failure_reason'] = type(error).__name__, str(error)[:1024]
    finally:
        if qualification is not None:
            for process in reversed(qualification.workers):
                result['cleanup_errors'].extend(process.close())
            result['checks']['all_owned_workers_exited'] = all(w.process.poll() is not None for w in qualification.workers)
            result['worker_returncodes'] = [w.process.returncode for w in qualification.workers]
        try:
            result['checks']['immutable_sources'] = bool(hashes) and all(
                hashlib.sha256(bounded_read(root / path, BUDGET['source_file_bytes'])).hexdigest() == digest for path, digest in hashes.items())
            result['checks']['immutable_runtime_directories'] = (runtime_identity is not None and run_identity is not None
                and private_directory(runtime_root, create=False) == runtime_identity and private_directory(run, create=False) == run_identity
                and qualification is not None and all(private_directory(path, create=False) == identity for path, identity in qualification.directories.items()))
        except BaseException as error:
            result['final_identity_error_type'] = type(error).__name__
        try:
            result['free_bytes_after'] = os.statvfs(root).f_bavail * os.statvfs(root).f_frsize
            df = subprocess.run(['df', '-B1', str(root)], capture_output=True, timeout=5, check=True)
            write(reports / 'df-after.txt', df.stdout[:65536])
            result['evidence_retained'] = True
            files, total = {}, 0
            for base in (reports, run):
                if base is None:
                    continue
                for path in sorted(base.rglob('*')):
                    info = path.lstat()
                    require(not stat.S_ISLNK(info.st_mode) and info.st_uid == os.geteuid(), 'Evidence ownership differs')
                    if stat.S_ISDIR(info.st_mode):
                        require(stat.S_IMODE(info.st_mode) == 0o700, 'Evidence directory permissions differ')
                    elif stat.S_ISREG(info.st_mode):
                        require(info.st_nlink == 1 and info.st_mode & 0o077 == 0, 'Evidence file permissions differ')
                        total += info.st_size
                        require(total <= BUDGET['evidence_bytes'], 'Evidence total bound')
                        body = bounded_read(path, BUDGET['source_file_bytes'])
                        files[('evidence/' if base == reports else 'runtime/') + str(path.relative_to(base))] = {
                            'bytes': info.st_size, 'sha256': hashlib.sha256(body).hexdigest()}
                    else:
                        require(stat.S_ISSOCK(info.st_mode), 'Unexpected evidence file kind')
            result['artifact_bytes'], result['artifacts'] = total, files
        except BaseException as error:
            result['artifact_error_type'] = type(error).__name__
        result['finished_at'] = time.time()
        result['passed'] = (not any(k.endswith('error_type') for k in result) and not result['cleanup_errors']
            and set(result['checks']) == REQUIRED_CHECKS and all(result['checks'].values()))
        report_hash = write(reports / 'report.json', result)
        summary = safe_summary(result, report_hash)
        write(reports / 'public-summary.json', summary)
    print(json.dumps(summary, sort_keys=True))
    return 0 if result['passed'] else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path)
    parser.add_argument('--runtime-root', type=Path, help='Existing owned private 0700 directory, pathname at most 70 bytes')
    parser.add_argument('--worker-config', type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker_config is not None:
        worker(json.loads(bounded_read(args.worker_config, BUDGET['state_bytes'])))
        return 0
    if args.root is None or args.runtime_root is None:
        parser.error('--root and --runtime-root are required')
    def interrupted(_number, _frame):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
        raise KeyboardInterrupt('Native acceptance interrupted')
    previous = {number: signal.signal(number, interrupted) for number in (signal.SIGTERM, signal.SIGHUP)}
    try:
        try:
            return execute(args.root, args.runtime_root)
        except BaseException:
            print(json.dumps(safe_summary({})))
            return 1
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


if __name__ == '__main__':
    raise SystemExit(main())
