"""Real Guest -> production EventSpool capacity fault fixture.

The trusted JSONL worker is a native qualification edge, not a production RPC or
control-plane acknowledgement. SQLite engine capacity is deliberately smaller
than the logical queue budget. No mount, filesystem quota or guest disk is used.
"""
from __future__ import annotations

import base64
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import resource
import select
import signal
import stat
import subprocess
import time
import uuid

import guest_memory_pressure as protocol
import qualify_kvm as kvm


NODE_RELATIVE = '.state/toolchains/node-v24.22.1-linux-x64/bin/node'
WORKER_RELATIVE = 'scripts/qualification/event_spool_worker.mjs'
MAX_LINE_BYTES = 128 * 1024
MAX_REPLY_BYTES = 2 * 1024 * 1024
MAX_GUEST_FRAME_BYTES = 64 * 1024
MAX_GUEST_EVENTS = 32
MAX_EVIDENCE_BYTES = 1024 * 1024
SQLITE_SOURCE_ID = '2026-07-24 19:02:57 bf7c7f30031888f4e796e429ab3978879485813aaca6f641c7b33e4e09459bcc'
LIMITS = {
    'max_pages': 32, 'max_pending_bytes': 1048576, 'max_pending_records': 1024,
    'max_event_bytes': 16384, 'max_batch_bytes': 1048576,
    'max_batch_records': 32, 'max_forward_gap': 4096,
}
REQUIRED_CHECKS = frozenset({
    'event_spool_native_runtime_verified', 'event_spool_three_durable_stages',
    'b_healthy_during_event_spool_pressure', 'event_spool_sqlite_full',
    'event_spool_bounds_verified', 'event_spool_unacked_retained',
    'event_spool_b_store_independent', 'event_spool_identity_stable',
    'event_spool_broker_stop_confirmed', 'event_spool_stop_tombstone_rebuilt',
    'event_spool_old_authorization_rejected', 'b_healthy_after_event_spool_stop',
    'event_spool_fixture_generation_link',
    'event_spool_private_data_root_unchanged',
})


def _absolute_data_path(path):
    value = os.fspath(path)
    candidate = Path(value)
    if (not candidate.is_absolute() or value != os.path.normpath(value)
            or candidate == candidate.parent):
        raise RuntimeError('Spool data root requires an absolute normalized directory')
    return candidate


def _check_data_ancestors(path):
    for current in (path, *path.parents):
        info = current.lstat()
        if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
                or info.st_uid not in {0, os.geteuid()}
                or (info.st_mode & 0o022 and not (info.st_uid == 0 and info.st_mode & stat.S_ISVTX))):
            raise RuntimeError('Spool data directory ancestry is unsafe')


def directory_identity(path):
    path = _absolute_data_path(path)
    _check_data_ancestors(path)
    info = path.lstat()
    if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise RuntimeError('Spool data directory requires existing private owner permissions')
    return {'path': str(path), 'device': info.st_dev, 'inode': info.st_ino,
            'mode': stat.S_IMODE(info.st_mode), 'uid': info.st_uid}


def prepare_data_root(path):
    """Create only the configured leaf; never repair shared ancestor modes."""
    path = _absolute_data_path(path)
    _check_data_ancestors(path.parent)
    try: path.mkdir(mode=0o700)
    except FileExistsError: pass
    identity = directory_identity(path)
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try: os.fsync(descriptor)
    finally: os.close(descriptor)
    return identity


def verify_data_root(identity):
    if not isinstance(identity, dict) or directory_identity(Path(identity['path'])) != identity:
        raise RuntimeError('Spool data directory identity changed')
    return Path(identity['path'])


def create_data_run(root_identity, name):
    root = verify_data_root(root_identity)
    if not isinstance(name, str) or not re.fullmatch('[a-z0-9-]{1,80}', name):
        raise RuntimeError('Spool data run name differs')
    directory = root / name
    directory.mkdir(mode=0o700)
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try: os.fsync(descriptor)
    finally: os.close(descriptor)
    verify_data_root(root_identity)
    return directory_identity(directory)


def verify_data_run(root_identity, run_identity):
    root = verify_data_root(root_identity)
    directory = verify_data_root(run_identity)
    if directory.parent != root:
        raise RuntimeError('Spool data run escapes its configured root')
    return directory


def data_tree_manifest(root_identity, run_identity, *, max_bytes=64 * 1024**2, max_files=128):
    """Hash retained private data after all owned workers have exited."""
    directory = verify_data_run(root_identity, run_identity)
    entries, identities, total = [], [], 0
    pending = [(directory, 0)]
    while pending:
        current, depth = pending.pop()
        if depth > 8 or len(identities) + len(entries) > max_files * 2:
            raise RuntimeError('Spool retained data directory bound')
        frozen = directory_identity(current)
        if frozen['device'] != run_identity['device']:
            raise RuntimeError('Spool retained data crosses a filesystem')
        identities.append(frozen)
        with os.scandir(current) as children:
            for child in children:
                path = Path(child.path)
                info = child.stat(follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode):
                    pending.append((path, depth + 1))
                    if len(pending) + len(identities) + len(entries) > max_files * 2:
                        raise RuntimeError('Spool retained data directory bound')
                    continue
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                        or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600
                        or info.st_dev != run_identity['device']
                        or len(entries) >= max_files or total + info.st_size > max_bytes):
                    raise RuntimeError('Spool retained data file identity or bound differs')
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
                try:
                    opened = os.fstat(fd)
                    fingerprint = lambda value: (value.st_dev, value.st_ino, value.st_size,
                        value.st_mtime_ns, value.st_ctime_ns, value.st_mode, value.st_uid, value.st_nlink)
                    if fingerprint(opened) != fingerprint(info):
                        raise RuntimeError('Spool retained data file changed before read')
                    digest, count = hashlib.sha256(), 0
                    while chunk := os.read(fd, min(65536, info.st_size + 1 - count)):
                        count += len(chunk)
                        if count > info.st_size: raise RuntimeError('Spool retained data file grew')
                        digest.update(chunk)
                    if count != info.st_size or fingerprint(os.fstat(fd)) != fingerprint(info):
                        raise RuntimeError('Spool retained data file changed during read')
                finally: os.close(fd)
                if fingerprint(path.lstat()) != fingerprint(info):
                    raise RuntimeError('Spool retained data file replaced after read')
                total += count
                entries.append({'relative_path': str(path.relative_to(directory)), 'bytes': count,
                    'sha256': digest.hexdigest(), 'device': info.st_dev, 'inode': info.st_ino})
    for frozen in identities: verify_data_root(frozen)
    verify_data_run(root_identity, run_identity)
    return {'files': sorted(entries, key=lambda item: item['relative_path']), 'bytes': total,
            'file_count': len(entries), 'root_identity_verified': True}


def qualification_limits():
    return {
        'virtual_machines': 2, 'native_store_workers': 2, 'limits': dict(LIMITS),
        'guest_events_maximum': MAX_GUEST_EVENTS, 'guest_initial_small_events': 3,
        'guest_small_payload_bytes': 64, 'guest_pressure_payload_bytes': 8192,
        'guest_frame_bytes': MAX_GUEST_FRAME_BYTES, 'worker_jsonl_bytes': MAX_LINE_BYTES,
        'worker_reply_bytes': MAX_REPLY_BYTES,
        'guest_lifetime_seconds': 100, 'control_guest_lifetime_seconds': 120,
        'host_pressure_seconds': 40, 'readiness_seconds': 10,
        'guest_frame_seconds_including_write': 3, 'worker_write_seconds': 3,
        'worker_reply_seconds': 5, 'worker_exit_seconds_per_stage': 3,
        'worker_stderr_bytes': 2 * 1024**2, 'protocol_evidence_bytes': MAX_EVIDENCE_BYTES,
        'retained_data_bytes': 64 * 1024**2, 'retained_data_files': 128,
        'retained_data_directory_depth': 8,
        'sqlite_engine_cap': True, 'generic_filesystem_quota': False,
        'control_plane_commit': False,
    }


def new_scope(instance_id):
    return {
        **{key: str(uuid.uuid4()) for key in
           ('organization_id', 'project_id', 'run_id', 'attempt_id', 'stream_id')},
        'runtime_instance_id': instance_id,
        # These fixtures start producing only after the explicit local grant 2.
        # This numeric alignment is not control-plane authorization evidence.
        'binding': {'platform_epoch': str(uuid.uuid4()), 'lease_generation': 2},
    }


def source_event(scope, source_seq, text):
    return {
        'contract_version': '0.1.0',
        'scope': {k: scope[k] for k in ('organization_id', 'project_id')},
        **{k: scope[k] for k in ('run_id', 'attempt_id', 'runtime_instance_id', 'stream_id')},
        'binding': dict(scope['binding']), 'source_seq': source_seq,
        'observed_at': datetime.datetime.now(datetime.timezone.utc).isoformat().replace('+00:00', 'Z'),
        'type': 'text.delta', 'payload': {'block_id': scope['run_id'], 'text': text},
    }


def guest_program(scope):
    """A waits for each fresh host token; no pressure before the sealed report."""
    return '\nS=' + repr(scope) + r'''
import base64,datetime,json,os,re,select,signal,time
fd=os.open('/dev/virtio-ports/org.harniqo.m0',os.O_RDWR|os.O_NONBLOCK)
def spool_timeout(*_): raise TimeoutError('event spool Guest deadline')
signal.signal(signal.SIGALRM,spool_timeout)
signal.alarm(100)
deadline=time.monotonic()+100
pending=b''
print('HARNIQO-EVENT-SPOOL-READY '+C['nonce'],flush=True)
try:
    for seq in range(1,33):
        while b'\n' not in pending:
            if time.monotonic()>=deadline: raise TimeoutError('event spool Guest deadline')
            if not select.select([fd],[],[],.1)[0]: continue
            chunk=os.read(fd,128)
            if not chunk: continue
            pending+=chunk
            if len(pending)>128: raise RuntimeError('event spool Guest command bound')
        token,pending=pending.split(b'\n',1)
        if not re.fullmatch(rb'[0-9a-f]{40}',token): raise RuntimeError('event spool Guest token invalid')
        event={'contract_version':'0.1.0',
            'scope':{k:S[k] for k in ('organization_id','project_id')},
            **{k:S[k] for k in ('run_id','attempt_id','runtime_instance_id','stream_id')},
            'binding':dict(S['binding']),'source_seq':seq,
            'observed_at':datetime.datetime.now(datetime.timezone.utc).isoformat().replace('+00:00','Z'),
            'type':'text.delta','payload':{'block_id':S['run_id'],'text':'E'*(64 if seq<=3 else 8192)}}
        body=json.dumps(event,separators=(',',':')).encode('utf-8')
        frame=(json.dumps({'challenge':token.decode('ascii'),'body':base64.b64encode(body).decode('ascii')},separators=(',',':'))+'\n').encode('ascii')
        if len(body)>16384 or len(frame)>65536: raise RuntimeError('event spool Guest frame bound')
        view=memoryview(frame)
        while view:
            if time.monotonic()>=deadline: raise TimeoutError('event spool Guest deadline')
            try:
                written=os.write(fd,view)
                if written<=0: raise RuntimeError('event spool Guest report closed')
                view=view[written:]
            except BlockingIOError: select.select([],[fd],[],.1)
    raise RuntimeError('event spool Guest engine cap not reached')
finally:
    os.close(fd)
    signal.alarm(0)
'''


def _before(deadline, message):
    now = time.monotonic()
    if now >= deadline:
        raise TimeoutError(message)
    return now


def _child_limits(previous_mask):
    signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_FSIZE, (2 * 1024**2, 2 * 1024**2))


class NativeSpool:
    """Owned child pipes, bounded writes/replies, exact PID and finite cleanup."""

    def __init__(self, root, directory, config, evidence, registry, *, reopen=False, pause_before_commit=False):
        self.process = None
        self._pending = bytearray()
        self._evidence = evidence
        self._evidence_bytes = 0
        self._stderr = None
        self.closed = False
        self.startup_failure = None
        node = root / NODE_RELATIVE
        for path in (node, root / WORKER_RELATIVE, directory, config): kvm.no_symlinks(path)
        info = node.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or info.st_uid not in {0, os.geteuid()} or info.st_mode & 0o022):
            raise RuntimeError('Pinned Node executable identity differs')
        try:
            self._stderr = open(evidence.with_suffix('.stderr'), 'xb', buffering=0)
            previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT, signal.SIGTERM, signal.SIGHUP})
            try:
                arguments = [str(node), str(root / WORKER_RELATIVE), '--directory', str(directory), '--config', str(config)]
                if reopen: arguments.append('--open')
                if pause_before_commit: arguments.append('--pause-before-commit')
                self.process = subprocess.Popen(
                    arguments,
                    cwd=root, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self._stderr,
                    bufsize=0, close_fds=True, preexec_fn=lambda: _child_limits(previous_mask),
                    env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8', 'NODE_NO_WARNINGS': '1'})
                # Register the child before any signal or readiness read can
                # fail; finally can inspect/reap even a failed constructor.
                registry.append(self)
            finally:
                signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
            os.set_blocking(self.process.stdin.fileno(), False)
            os.set_blocking(self.process.stdout.fileno(), False)
            ready = self._readline(time.monotonic() + 10)
            if ready.get('kind') == 'startup_failed':
                self.startup_failure = ready
                raise RuntimeError('Native spool startup rejected')
            if ready != {'kind': 'ready', 'pid': self.process.pid}:
                raise RuntimeError('Native spool ready PID differs')
            self._record({'kind': 'ready', 'pid': self.process.pid})
        except BaseException:
            if self.process is None: self.close()
            raise

    def _record(self, value):
        # Protocol decisions and hashes are private; payloads remain in the
        # preserved source-frame ledger and database, not duplicated here.
        line = (json.dumps(value, separators=(',', ':')) + '\n').encode()
        if self._evidence_bytes + len(line) > MAX_EVIDENCE_BYTES:
            raise RuntimeError('Event spool protocol evidence bound')
        fd = os.open(self._evidence, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                    or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600
                    or info.st_size != self._evidence_bytes):
                raise RuntimeError('Event spool evidence identity differs')
            view = memoryview(line)
            while view:
                count = os.write(fd, view)
                if count <= 0: raise RuntimeError('Event spool evidence short write')
                view = view[count:]
            os.fsync(fd)
            if self._evidence_bytes == 0:
                parent_fd = os.open(self._evidence.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                try: os.fsync(parent_fd)
                finally: os.close(parent_fd)
            self._evidence_bytes += len(line)
        finally:
            os.close(fd)

    def _readline(self, deadline, streams=None):
        while True:
            _before(deadline, 'Native spool reply deadline')
            if b'\n' in self._pending:
                line, _, tail = self._pending.partition(b'\n')
                self._pending[:] = tail
                value = json.loads(line)
                _before(deadline, 'Native spool reply deadline')
                if not isinstance(value, dict): raise RuntimeError('Native spool reply shape differs')
                return value
            try:
                chunk = os.read(self.process.stdout.fileno(), 16384)
                if not chunk: raise RuntimeError('Native spool worker closed its reply pipe')
                self._pending.extend(chunk)
                if len(self._pending) > MAX_REPLY_BYTES:
                    raise RuntimeError('Native spool reply exceeded bound')
            except BlockingIOError:
                if self.process.poll() is not None: raise RuntimeError('Native spool worker exited')
                remaining = deadline - _before(deadline, 'Native spool reply deadline')
                select.select([self.process.stdout], [], [], min(.02, remaining))
                if streams is not None: streams._poll(0)

    def _send_request(self, operation, parameters, deadline, streams):
        if self.closed or self.process is None or self.process.poll() is not None:
            raise RuntimeError('Native spool worker unavailable')
        identity = uuid.uuid4().hex + uuid.uuid4().hex[:8]
        request = {'id': identity, 'operation': operation, **parameters}
        line = (json.dumps(request, separators=(',', ':')) + '\n').encode('ascii')
        if len(line) > MAX_LINE_BYTES: raise RuntimeError('Native spool request exceeded bound')
        expires = time.monotonic() + 3
        if deadline is not None: expires = min(expires, deadline)
        view = memoryview(line)
        while view:
            _before(expires, 'Native spool write deadline')
            try:
                count = os.write(self.process.stdin.fileno(), view)
                if count <= 0: raise RuntimeError('Native spool worker closed request pipe')
                _before(expires, 'Native spool write deadline')
                view = view[count:]
            except BlockingIOError:
                remaining = expires - _before(expires, 'Native spool write deadline')
                select.select([], [self.process.stdin], [], min(.02, remaining))
                if streams is not None: streams._poll(0)
        return identity

    def request(self, operation, *, deadline=None, streams=None, **parameters):
        identity = self._send_request(operation, parameters, deadline, streams)
        expires = time.monotonic() + 5
        if deadline is not None: expires = min(expires, deadline)
        reply = self._readline(expires, streams)
        if reply.get('id') != identity or type(reply.get('ok')) is not bool:
            raise RuntimeError('Native spool reply request identity differs')
        record = {k: v for k, v in reply.items() if k != 'batch'}
        if 'batch' in reply:
            record['batch'] = {**reply['batch'], 'events': [
                {k: v for k, v in event.items() if k != 'body'} for event in reply['batch']['events']]}
        self._record({'operation': operation, 'reply': record})
        return reply

    def pause_request(self, operation, **parameters):
        """Explicit native fault fixture: pause before SQLite's real COMMIT."""
        identity = self._send_request(operation, parameters, None, None)
        reply = self._readline(time.monotonic() + 5)
        if reply != {'kind': 'paused_before_commit', 'pid': self.process.pid, 'id': identity}:
            raise RuntimeError('Native spool precommit pause identity differs')
        self._record({'operation': operation, 'pause': reply})
        return reply

    def close(self):
        if self.closed: return []
        errors = []
        try:
            if self.process is not None and (self.process.poll() is None
                    or getattr(self, 'startup_failure', None) is not None):
                try:
                    if getattr(self, 'startup_failure', None) is not None:
                        # A startup rejection never entered the JSONL request
                        # loop. Reap its finite failure exit, without a close RPC.
                        self.process.wait(timeout=3)
                        if self.process.returncode != 2:
                            raise RuntimeError('Native spool rejected startup exit differs')
                    else:
                        reply = self.request('close')
                        if not reply['ok']: raise RuntimeError('Native spool close failed')
                        self.process.wait(timeout=3)
                        if self.process.returncode != 0: raise RuntimeError('Native spool exit differs')
                except Exception as error:
                    errors.append({'phase': 'event_spool_close', 'type': type(error).__name__})
                finally:
                    if self.process.poll() is None:
                        self.process.terminate()
                        try: self.process.wait(timeout=3)
                        except subprocess.TimeoutExpired:
                            self.process.kill()
                            self.process.wait(timeout=3)
            if self.process is not None:
                for pipe in (self.process.stdin, self.process.stdout):
                    if pipe is not None: pipe.close()
        except Exception as error:
            errors.append({'phase': 'event_spool_exit', 'type': type(error).__name__})
        finally:
            self.closed = self.process is None or self.process.poll() is not None
            if self._stderr is not None:
                try: self._stderr.flush(); os.fsync(self._stderr.fileno())
                except Exception as error:
                    errors.append({'phase': 'event_spool_log_sync', 'type': type(error).__name__})
                finally: self._stderr.close()
        return errors


def validate_stats(stats):
    if not isinstance(stats, dict): raise RuntimeError('Native spool statistics missing')
    names = ('pending_records', 'pending_bytes', 'database_bytes', 'wal_bytes', 'shm_bytes',
             'page_count', 'database_bound_bytes', 'wal_bound_bytes', 'committed_through',
             'contiguous_received_through', 'highest_received')
    if any(type(stats.get(k)) is not int or stats[k] < 0 for k in names):
        raise RuntimeError('Native spool statistics scalar differs')
    if (stats.get('node_version') != 'v24.22.1' or stats.get('sqlite_version') != '3.53.4'
            or stats.get('sqlite_source_id') != SQLITE_SOURCE_ID
            or stats.get('limits') != LIMITS or stats['database_bound_bytes'] != LIMITS['max_pages'] * 4096
            or stats['wal_bound_bytes'] != 32 + (LIMITS['max_pages'] + 32) * 4120
            or stats['database_bytes'] > stats['database_bound_bytes']
            or stats['wal_bytes'] > stats['wal_bound_bytes'] or stats['shm_bytes'] != 0
            or stats['page_count'] > LIMITS['max_pages']
            or stats['pending_bytes'] > LIMITS['max_pending_bytes']
            or stats['pending_records'] > LIMITS['max_pending_records']
            or stats.get('failure_code') not in {None, 'storage_full'}
            or stats['committed_through'] != 0 or stats.get('gaps') != []
            or stats['contiguous_received_through'] != stats['highest_received']):
        raise RuntimeError('Native spool runtime, bounds or uncommitted sequence differs')
    for name in ('scope_sha256', 'configuration_sha256'):
        if not isinstance(stats.get(name), str) or not re.fullmatch('[0-9a-f]{64}', stats[name]):
            raise RuntimeError('Native spool scope/configuration identity missing')
    expected_pragmas = {'locking_mode': 'exclusive', 'journal_mode': 'wal', 'synchronous': 2,
        'page_size': 4096, 'cache_spill': 0, 'cache_size': LIMITS['max_pages'], 'temp_store': 2,
        'trusted_schema': 0, 'wal_autocheckpoint': 0, 'mmap_size': 0, 'max_page_count': LIMITS['max_pages']}
    observed = stats.get('pragmas')
    if (not isinstance(observed, dict) or set(observed) != set(expected_pragmas)
            or any(type(observed[k]) is not type(v) or observed[k] != v for k, v in expected_pragmas.items())):
        raise RuntimeError('Native spool actual PRAGMA readbacks differ')
    validate_compile_options(stats)
    return stats


def validate_compile_options(stats):
    options = stats.get('sqlite_compile_options')
    if (not isinstance(options, list) or not 1 <= len(options) <= 256
            or any(not isinstance(value, str) or not 1 <= len(value) <= 256 for value in options)
            or options != sorted(options)
            or any(re.match(r'^(NO_SYNC|OMIT_WAL|OMIT_PAGER_PRAGMAS|OMIT_COMPILEOPTION_DIAGS)(=|$)', value)
                   or value == 'THREADSAFE=0' for value in options)
            or hashlib.sha256(json.dumps(options, separators=(',', ':')).encode()).hexdigest()
                != stats.get('sqlite_compile_options_sha256')):
        raise RuntimeError('Native SQLite compile options differ')


def successful(reply, operation):
    if reply.get('ok') is not True:
        raise RuntimeError('Native spool ' + operation + ' failed: ' + str(reply.get('code')))
    return validate_stats(reply.get('stats'))


def _guest_body(channel, streams, scope, source_seq, token, deadline):
    frame = bytearray()
    while b'\n' not in frame:
        _before(deadline, 'Guest SourceEvent frame deadline')
        try:
            chunk = channel.recv(16384)
            if not chunk: raise RuntimeError('Guest SourceEvent report channel closed')
            frame.extend(chunk)
            if len(frame) > MAX_GUEST_FRAME_BYTES: raise RuntimeError('Guest SourceEvent frame bound')
        except BlockingIOError:
            streams._poll(min(.02, deadline - _before(deadline, 'Guest SourceEvent frame deadline')))
    _before(deadline, 'Guest SourceEvent frame deadline')
    line, _, tail = frame.partition(b'\n')
    if tail: raise RuntimeError('Guest SourceEvent emitted unsolicited data')
    value = json.loads(line)
    if not isinstance(value, dict) or set(value) != {'challenge', 'body'} or value['challenge'] != token:
        raise RuntimeError('Guest SourceEvent challenge binding differs')
    if not isinstance(value['body'], str): raise RuntimeError('Guest SourceEvent body encoding differs')
    body = base64.b64decode(value['body'], validate=True)
    if not 1 <= len(body) <= LIMITS['max_event_bytes']: raise RuntimeError('Guest SourceEvent body bound')
    event = json.loads(body.decode('utf-8'))
    wanted = source_event(scope, source_seq, 'E' * (64 if source_seq <= 3 else 8192))
    observed = event.get('observed_at') if isinstance(event, dict) else None
    if not isinstance(observed, str): raise RuntimeError('Guest SourceEvent timestamp missing')
    datetime.datetime.fromisoformat(observed.replace('Z', '+00:00'))
    wanted['observed_at'] = observed
    if event != wanted: raise RuntimeError('Guest SourceEvent contract or owner differs')
    return body


def _append(worker, source_seq, body, deadline, streams):
    return worker.request('append', source_seq=source_seq,
                          body=base64.b64encode(body).decode('ascii'), deadline=deadline, streams=streams)


def _receipt(reply, source_seq):
    successful(reply, 'append')
    if reply.get('receipt') != {'level': 'durable_received', 'source_seq': source_seq, 'duplicate': False}:
        raise RuntimeError('Native spool durable receipt differs')


def _verify_retained(worker, expected, deadline, streams):
    retained = []
    after = 0
    reply = worker.request('batch', deadline=deadline, streams=streams)
    stats = successful(reply, 'batch')
    batch = reply.get('batch')
    if not isinstance(batch, dict) or not isinstance(batch.get('events'), list):
        raise RuntimeError('Native spool retained batch shape differs')
    events = batch['events']
    if len(events) > MAX_GUEST_EVENTS: raise RuntimeError('Native spool batch iteration bound')
    # This native case has <=32 frames and a 128KiB database, smaller than the
    # fixed single-batch budget. No ACK or extra inspection API is needed.
    for event in events:
        if type(event.get('source_seq')) is not int or event['source_seq'] <= after:
            raise RuntimeError('Native spool retained ordering differs')
        body = base64.b64decode(event['body'], validate=True)
        digest = hashlib.sha256(body).hexdigest()
        if event.get('sha256') != digest: raise RuntimeError('Native spool retained digest differs')
        retained.append({'source_seq': event['source_seq'], 'bytes': len(body), 'sha256': digest})
        after = event['source_seq']
    if (retained != expected or stats['pending_records'] != len(expected)
            or stats['pending_bytes'] != sum(item['bytes'] for item in expected)
            or stats['highest_received'] != len(expected)):
        raise RuntimeError('Native spool lost or changed an unacknowledged event')
    return stats


def _b_health(worker, scope, streams, instance, sequence, deadline):
    token = protocol.challenge(streams, instance, deadline=deadline)
    body = json.dumps(source_event(scope, sequence, token), separators=(',', ':')).encode()
    reply = _append(worker, sequence, body, deadline, streams)
    _receipt(reply, sequence)
    return {'challenge': token, 'durable_sequence': sequence, 'stats': reply['stats']}


def run_pressure(broker, instances, streams, result, workers):
    a, b = instances
    wa, wb = workers
    ready = time.monotonic() + 10
    protocol._wait_until(lambda: 'HARNIQO-EVENT-SPOOL-READY ' + a['nonce'] in
                         protocol._complete_lines(protocol.console_text(streams, a)),
                         streams, ready, 'Event spool Guest readiness deadline')
    started = time.monotonic()
    deadline = started + 40
    before = [broker.resource_limits(i['uuid']) for i in instances]
    if any(broker.observe(i['uuid']).generation != i['event_scope']['binding']['lease_generation'] for i in instances):
        raise RuntimeError('Native fixture local grant generation differs')
    result['checks']['event_spool_fixture_generation_link'] = True
    evidence = result['event_spool_pressure'] = {'stages': [], 'appended': [], 'failure': None}
    result['checks']['event_spool_native_runtime_verified'] = True
    full = None
    for seq in range(1, MAX_GUEST_EVENTS + 1):
        expires = min(deadline, time.monotonic() + 3)
        token = uuid.uuid4().hex + uuid.uuid4().hex[:8]
        channel = streams.report_channels[a['uuid']]
        protocol._send(channel, token + '\n', streams, deadline=expires)
        body = _guest_body(channel, streams, a['event_scope'], seq, token, expires)
        digest = {'source_seq': seq, 'bytes': len(body), 'sha256': hashlib.sha256(body).hexdigest()}
        # Keep even the rejected event: a local receipt never permits guest
        # discard before a real authenticated control-plane committed ACK.
        wa._record({'kind': 'guest_source_event', 'challenge': token, **digest,
                    'body': base64.b64encode(body).decode('ascii')})
        reply = _append(wa, seq, body, deadline, streams)
        if reply.get('ok') is not True:
            full = reply
            evidence['failure'] = {'source_seq': seq, 'reply': reply, 'event': digest}
            if (reply.get('code') != 'storage_full' or reply.get('mayHaveCommitted') is not False
                    or seq <= 3):
                raise RuntimeError('Event spool failure was not a rolled-back engine SQLITE_FULL')
            stats = validate_stats(reply.get('stats'))
            if (stats['pending_bytes'] + len(body) >= LIMITS['max_pending_bytes']
                    or stats['pending_records'] >= LIMITS['max_pending_records']):
                raise RuntimeError('Logical queue capacity obscured SQLite engine capacity')
            break
        _receipt(reply, seq)
        evidence['appended'].append(digest)
        if seq <= 3:
            evidence['stages'].append(_b_health(wb, b['event_scope'], streams, b, seq, deadline))
    if full is None: raise RuntimeError('SQLite engine capacity was not reached within the frame budget')
    evidence['retained_stats'] = _verify_retained(wa, evidence['appended'], deadline, streams)
    evidence['b_after_full'] = _b_health(wb, b['event_scope'], streams, b, 4, deadline)
    after = [broker.resource_limits(i['uuid']) for i in instances]
    identity = lambda sample: tuple(sample[k] for k in ('pid', 'process_start_time', 'cgroup_path'))
    _before(deadline, 'Event spool complete pressure deadline')
    evidence.update(before=before, after=after, duration_seconds=time.monotonic() - started)
    result['checks'].update(
        event_spool_three_durable_stages=len(evidence['stages']) == 3,
        b_healthy_during_event_spool_pressure=len(evidence['stages']) == 3,
        event_spool_sqlite_full=True, event_spool_bounds_verified=True,
        event_spool_unacked_retained=True, event_spool_b_store_independent=True,
        event_spool_identity_stable=all(identity(x) == identity(y) for x, y in zip(before, after)))
    if not result['checks']['event_spool_identity_stable']:
        raise RuntimeError('QEMU identity changed during native spool pressure')


def after_stop(instances, streams, result, workers):
    deadline = time.monotonic() + 8
    result['event_spool_pressure']['b_after_stop'] = _b_health(
        workers[1], instances[1]['event_scope'], streams, instances[1], 5, deadline)
    result['checks']['b_healthy_after_event_spool_stop'] = True


def prepare_workers(root, reports, instances, workers, result, data_root_identity):
    """Freeze each owner/configuration and validate native storage before VM boot."""
    result['event_spool_stores'] = []
    runtime_sources = {name: digest for name, digest in result['source_sha256'].items()
                       if name == WORKER_RELATIVE or name.startswith('apps/runner/event-spool/')}
    if not runtime_sources or any(kvm.sha256(root / name) != digest for name, digest in runtime_sources.items()):
        raise RuntimeError('Native worker source differs from the frozen source snapshot')
    result['event_spool_runtime_source_sha256'] = runtime_sources
    result['event_spool_node_sha256'] = kvm.sha256(root / NODE_RELATIVE)
    data_run_identity = create_data_run(data_root_identity, reports.name)
    data_run = verify_data_run(data_root_identity, data_run_identity)
    result['event_spool_data'] = {'root_identity': data_root_identity,
                                'run_identity': data_run_identity, 'stores': []}
    for label, instance in zip(('a', 'b'), instances):
        verify_data_run(data_root_identity, data_run_identity)
        directory = data_run / ('event-spool-' + label)
        directory.mkdir(mode=0o700)
        result['event_spool_data']['stores'].append({'label': label,
                                                    'identity': directory_identity(directory)})
        config = reports / ('event-spool-' + label + '-config.json')
        value = {'scope': instance['event_scope'], 'limits': dict(LIMITS)}
        kvm.write(config, json.dumps(value, indent=2) + '\n')
        worker = NativeSpool(root, directory, config, reports / ('event-spool-' + label + '-protocol.jsonl'), workers)
        stats = successful(worker.request('stats'), 'initial stats')
        if stats['pending_records'] != 0 or stats['highest_received'] != 0:
            raise RuntimeError('Event spool fixture was not a newly initialized empty store')
        result['event_spool_stores'].append({
            'directory': directory.name, 'configuration': config.name,
            'configuration_file_sha256': kvm.sha256(config), 'scope': value['scope'],
            'initial_stats': stats,
        })
    if (result['event_spool_stores'][0]['initial_stats']['scope_sha256'] ==
            result['event_spool_stores'][1]['initial_stats']['scope_sha256']):
        raise RuntimeError('A and B native stores share an owner')
    catalog = reports / 'event-spool-data-catalog.json'
    kvm.write(catalog, json.dumps(result['event_spool_data'], sort_keys=True, indent=2) + '\n')
    result['event_spool_data_catalog_sha256'] = kvm.sha256(catalog)


def verify_configuration(root, reports, result):
    """Stored native owner/limit inputs remain immutable through qualification."""
    data = result.get('event_spool_data', {})
    catalog = reports / 'event-spool-data-catalog.json'
    if (kvm.sha256(catalog) != result.get('event_spool_data_catalog_sha256')
            or json.loads(catalog.read_text()) != data): return False
    data_run = verify_data_run(data['root_identity'], data['run_identity'])
    for entry in data['stores']:
        path = verify_data_root(entry['identity'])
        if path != data_run / ('event-spool-' + entry['label']): return False
    if len(data['stores']) != 2: return False
    kvm.no_symlinks(root / NODE_RELATIVE)
    if kvm.sha256(root / NODE_RELATIVE) != result.get('event_spool_node_sha256'):
        return False
    if any(kvm.sha256(root / name) != digest
           for name, digest in result.get('event_spool_runtime_source_sha256', {}).items()):
        return False
    for entry in result.get('event_spool_stores', []):
        path = reports / entry['configuration']
        value = json.loads(path.read_text())
        if (kvm.sha256(path) != entry['configuration_file_sha256']
                or value != {'scope': entry['scope'], 'limits': LIMITS}):
            return False
    return len(result.get('event_spool_stores', [])) == 2


def retained_data(result, workers):
    """The private native database is evidence; preserve it outside the repo."""
    if any(worker.process is None or worker.process.poll() is None for worker in workers):
        raise RuntimeError('Spool data must not be inspected before worker exit')
    data = result['event_spool_data']
    directory = verify_data_run(data['root_identity'], data['run_identity'])
    for entry in data['stores']:
        path = verify_data_root(entry['identity'])
        if path.parent != directory: raise RuntimeError('Spool store escaped its data run')
    manifest = data_tree_manifest(data['root_identity'], data['run_identity'])
    result['event_spool_data_artifacts'] = manifest
    result['checks']['event_spool_private_data_root_unchanged'] = True
