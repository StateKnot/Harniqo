#!/usr/bin/env python3
"""Private native SQLite/SIGKILL acceptance; fixture ACKs are never platform commits."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import platform
import signal
import stat
import subprocess
import sys
import time
import uuid

import event_spool_pressure as pressure
import qualify_kvm as kvm

SOURCE_FILES = (
    'apps/runner/event-spool/spool.ts', 'apps/runner/event-spool/types.ts',
    'apps/runner/event-spool/package.json', 'apps/runner/event-spool/package-lock.json',
    'scripts/qualification/event_spool_worker.mjs', 'scripts/qualification/event_spool_pressure.py',
    'scripts/qualification/qualify_event_spool.py', 'scripts/qualification/qualify_kvm.py',
    'scripts/qualification/guest_memory_pressure.py', 'apps/runner/console_store.py',
)
BUDGET = {
    'store_directories': 5, 'workers_total': 12, 'workers_concurrent': 2,
    'wall_seconds': 360, 'worker_start_seconds': 10, 'worker_reply_seconds': 5,
    'worker_kill_wait_seconds': 3, 'capacity_attempts': 32,
    'source_file_bytes': 300 * 1024, 'source_total_bytes': 2 * 1024**2,
    'report_total_bytes': 64 * 1024**2, 'worker_stderr_bytes': 2 * 1024**2,
    'worker_protocol_bytes': pressure.MAX_EVIDENCE_BYTES,
    'worker_request_bytes': pressure.MAX_LINE_BYTES, 'worker_reply_bytes': pressure.MAX_REPLY_BYTES,
    'minimum_free_bytes': 50 * 1024**3, 'limits': dict(pressure.LIMITS),
}
FALSE_CLAIMS = {key: False for key in (
    'control_plane_commit_verified', 'control_plane_authentication_verified',
    'power_loss_verified', 'generic_filesystem_quota_verified', 'host_oom_verified',
    'virtual_machine_isolation_verified', 'all_corruption_detected', 'always_ack_drain_possible',
)}
PRAGMAS = {
    'locking_mode': 'exclusive', 'journal_mode': 'wal', 'synchronous': 2,
    'page_size': 4096, 'cache_spill': 0, 'cache_size': pressure.LIMITS['max_pages'],
    'temp_store': 2, 'trusted_schema': 0, 'wal_autocheckpoint': 0, 'mmap_size': 0,
    'max_page_count': pressure.LIMITS['max_pages'],
}
REQUIRED_CHECKS = frozenset({
    'durable_replay_ack_and_process_lock', 'append_precommit_rollback', 'ack_precommit_rollback',
    'sqlite_full_preserves_drains_and_independent_b', 'native_runtime_and_bounds_readbacks',
    'all_owned_workers_exited', 'immutable_sources', 'immutable_runtime',
    'immutable_data_directories',
})


def require(value, message):
    if not value: raise RuntimeError(message)


def write(path, data):
    data = data if isinstance(data, bytes) else (json.dumps(data, sort_keys=True, indent=2) + '\n').encode()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        view = memoryview(data)
        while view:
            count = os.write(fd, view)
            require(count > 0, 'Evidence short write')
            view = view[count:]
        os.fsync(fd)
    finally: os.close(fd)
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try: os.fsync(directory)
    finally: os.close(directory)
    return hashlib.sha256(data).hexdigest()


def private_directory(path):
    path.mkdir(mode=0o700, exist_ok=True)
    kvm.no_symlinks(path)
    info = path.lstat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid()
            and stat.S_IMODE(info.st_mode) == 0o700, 'Private evidence directory differs')


def bounded_read(path, limit):
    kvm.no_symlinks(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1
                and info.st_size <= limit, 'Source or evidence file bound differs')
        chunks, total = [], 0
        while chunk := os.read(fd, min(65536, limit + 1 - total)):
            total += len(chunk)
            require(total <= limit, 'Source changed beyond bound')
            chunks.append(chunk)
        require(total == info.st_size, 'Source changed during read')
        return b''.join(chunks)
    finally: os.close(fd)


def snapshot_sources(root, reports):
    destination = reports / 'source'
    private_directory(destination)
    hashes, total = {}, 0
    for relative in SOURCE_FILES:
        body = bounded_read(root / relative, BUDGET['source_file_bytes'])
        total += len(body)
        require(total <= BUDGET['source_total_bytes'], 'Source snapshot bound differs')
        target = destination / relative
        for parent in reversed(target.parents):
            if parent == destination or destination in parent.parents: private_directory(parent)
        hashes[relative] = write(target, body)
    return hashes


class Qualification:
    def __init__(self, root, reports, result, *, data_run):
        self.root, self.reports, self.result, self.data_run = root, reports, result, data_run
        self.workers, self.identities = [], {}
        self.deadline = time.monotonic() + BUDGET['wall_seconds']

    def rpc(self, worker, operation, **parameters):
        require(time.monotonic() < self.deadline, 'Qualification deadline')
        reply = worker.request(operation, deadline=self.deadline, **parameters)
        if reply.get('stats') is not None: self.statistics(worker, reply['stats'])
        return reply

    def statistics(self, worker, stats):
        # pressure.validate_stats intentionally requires cursor=0/no gaps; these
        # native cases additionally exercise committed cursors and real gaps.
        scalar = ('pending_records', 'pending_bytes', 'database_bytes', 'wal_bytes', 'shm_bytes',
                  'page_count', 'database_bound_bytes', 'wal_bound_bytes', 'committed_through',
                  'contiguous_received_through', 'highest_received')
        require(isinstance(stats, dict) and all(type(stats.get(k)) is int and stats[k] >= 0 for k in scalar), 'Statistics scalar differs')
        require(stats['node_version'] == 'v24.22.1' and stats['sqlite_version'] == '3.53.4'
                and stats['sqlite_source_id'] == pressure.SQLITE_SOURCE_ID and stats['limits'] == pressure.LIMITS
                and stats.get('pragmas') == PRAGMAS
                and all(type(stats['pragmas'][key]) is type(value) for key, value in PRAGMAS.items())
                and stats['database_bound_bytes'] == pressure.LIMITS['max_pages'] * 4096
                and stats['wal_bound_bytes'] == 32 + (pressure.LIMITS['max_pages'] + 32) * 4120
                and stats['database_bytes'] <= stats['database_bound_bytes'] and stats['wal_bytes'] <= stats['wal_bound_bytes']
                and stats['shm_bytes'] == 0 and stats['page_count'] <= pressure.LIMITS['max_pages']
                and stats['pending_records'] <= pressure.LIMITS['max_pending_records']
                and stats['pending_bytes'] <= pressure.LIMITS['max_pending_bytes'] and stats['failure_code'] is None,
                'Native runtime, bounds or fault state differs')
        pressure.validate_compile_options(stats)
        identity = (stats['scope_sha256'], stats['configuration_sha256'])
        require(all(isinstance(x, str) and len(x) == 64 and all(c in '0123456789abcdef' for c in x) for x in identity), 'Statistics identity differs')
        key = str(worker.directory)
        require(self.identities.setdefault(key, identity) == identity, 'Store identity changed')
        self.result['observations'].append({'case': worker.case, 'stats': stats})
        worker.last_stats = stats
        return stats

    def store(self, name):
        folder = self.reports / name
        private_directory(folder)
        # Evidence may live under a shared source checkout. Live SQLite files
        # must instead inherit the explicitly configured private data root.
        data_folder = self.data_run / name
        private_directory(data_folder)
        directory = data_folder / 'store'
        private_directory(directory)
        scope = pressure.new_scope(str(uuid.uuid4()))
        config = folder / 'config.json'
        write(config, {'scope': scope, 'limits': pressure.LIMITS})
        return (folder, directory, config, scope)

    def start(self, store, *, reopen=False, paused=False):
        require(time.monotonic() < self.deadline, 'Qualification deadline')
        require(len(self.workers) < BUDGET['workers_total'], 'Worker count bound')
        require(sum(w.process is not None and w.process.poll() is None for w in self.workers)
                < BUDGET['workers_concurrent'], 'Concurrent worker bound')
        folder, directory, config, _ = store
        worker = pressure.NativeSpool(self.root, directory, config,
            folder / ('protocol-' + str(len(self.workers)) + '.jsonl'), self.workers,
            reopen=reopen, pause_before_commit=paused)
        worker.directory, worker.case = directory, folder.name
        return worker

    def event(self, store, seq, text):
        body = json.dumps(pressure.source_event(store[3], seq, text), separators=(',', ':')).encode()
        require(len(body) <= pressure.LIMITS['max_event_bytes'], 'Source event bound')
        # Preserve exact bytes before delivery, including failed/retried events.
        path = store[0] / ('source-' + str(seq) + '-' + hashlib.sha256(body).hexdigest() + '.json')
        write(path, body)
        return body

    def append(self, worker, seq, body, *, duplicate=False):
        reply = self.rpc(worker, 'append', source_seq=seq, body=base64.b64encode(body).decode())
        require(reply.get('ok') is True and reply.get('receipt') == {
            'level': 'durable_received', 'source_seq': seq, 'duplicate': duplicate}, 'Local durable receipt differs')
        return reply

    def ack(self, store, seq):
        scope = store[3]
        return {k: scope[k] for k in ('attempt_id', 'stream_id', 'binding')} | {'level': 'committed', 'committed_through': seq}

    def retained(self, worker, expected, cursor=0):
        reply = self.rpc(worker, 'batch')
        require(reply.get('ok') is True and isinstance(reply.get('batch'), dict), 'Replay failed')
        batch, actual = reply['batch'], {}
        require(len(batch['events']) <= pressure.LIMITS['max_batch_records'], 'Replay count bound')
        for entry in batch['events']:
            data = base64.b64decode(entry['body'], validate=True)
            require(entry['source_seq'] not in actual and entry['sha256'] == hashlib.sha256(data).hexdigest(), 'Replay hash differs')
            actual[entry['source_seq']] = data
        require(actual == expected and list(actual) == sorted(actual) and batch['committed_through'] == cursor
                and reply['stats']['pending_records'] == len(expected)
                and reply['stats']['pending_bytes'] == sum(map(len, expected.values())), 'Durable replay or cursor differs')
        previous = contiguous = cursor
        gaps = []
        for seq in sorted(expected):
            require(seq > previous, 'Replay sequence behind cursor')
            if seq > previous + 1: gaps.append({'from': previous + 1, 'through': seq - 1})
            if seq == contiguous + 1: contiguous = seq
            previous = seq
        require(all(value['gaps'] == gaps and value['contiguous_received_through'] == contiguous
                    and value['highest_received'] == previous for value in (batch, reply['stats'])), 'Replay frontier or gaps differ')
        return reply['stats']

    def kill(self, worker, name, *, committed_wal=False):
        require(any(worker is own for own in self.workers) and worker.process is not None
                and worker.process.poll() is None, 'Owned live child missing')
        wal = worker.directory / 'spool.sqlite-wal'
        body = bounded_read(wal, 32 + (pressure.LIMITS['max_pages'] + 32) * 4120) if wal.exists() else b''
        require(not committed_wal or len(body) > 32, 'Committed WAL missing before SIGKILL')
        if committed_wal: require(len(body) == worker.last_stats['wal_bytes'], 'Actual WAL differs from last native statistics')
        digest = write(self.reports / worker.case / (name + '.wal'), body)
        worker.process.kill()
        require(worker.process.wait(timeout=3) == -signal.SIGKILL, 'Owned child did not exit by SIGKILL')
        require(worker.close() == [], 'Killed child cleanup failed')
        self.result['kills'].append({'case': worker.case, 'phase': name, 'wal_bytes': len(body), 'wal_sha256': digest,
                                     'returncode': worker.process.returncode,
                                     'pending_stats': getattr(worker, 'last_stats', None)})

    def run(self):
        replay = self.store('replay')
        w = self.start(replay)
        events = {seq: self.event(replay, seq, 'native-' + str(seq)) for seq in (1, 2, 3)}
        self.append(w, 2, events[2]); self.retained(w, {2: events[2]})
        self.append(w, 2, events[2], duplicate=True)
        conflict = self.event(replay, 2, 'conflict')
        rejected = self.rpc(w, 'append', source_seq=2, body=base64.b64encode(conflict).decode())
        require(rejected.get('code') == 'event_conflict' and rejected.get('mayHaveCommitted') is False, 'Conflicting duplicate accepted')
        rejected = self.rpc(w, 'ack', ack=self.ack(replay, 2))
        require(rejected.get('code') == 'ack_crosses_gap' and rejected.get('mayHaveCommitted') is False, 'Gap ACK accepted')
        self.append(w, 1, events[1]); self.append(w, 3, events[3])
        self.retained(w, events); self.kill(w, 'durable-append', committed_wal=True)
        w = self.start(replay, reopen=True); self.retained(w, events)
        require(self.rpc(w, 'ack', ack=self.ack(replay, 2)).get('ok') is True, 'Fixture ACK rejected')
        self.retained(w, {3: events[3]}, 2); self.kill(w, 'durable-ack', committed_wal=True)
        w = self.start(replay, reopen=True); self.retained(w, {3: events[3]}, 2)
        rejected = self.rpc(w, 'append', source_seq=1, body=base64.b64encode(events[1]).decode())
        require(rejected.get('code') == 'event_already_committed', 'Acknowledged sequence accepted')
        before = self.retained(w, {3: events[3]}, 2)
        count = len(self.workers)
        try: self.start(replay, reopen=True)
        except RuntimeError:
            require(len(self.workers) == count + 1, 'Competitor child was not registered')
            competitor = self.workers[-1]
            require(competitor.startup_failure == {'kind': 'startup_failed', 'code': 'store_locked'}
                    and competitor.process.wait(timeout=3) == 2 and competitor.close() == [], 'Second process lock rejection differs')
        else: raise RuntimeError('Second process unexpectedly opened store')
        require(self.retained(w, {3: events[3]}, 2) == before, 'Lock competitor changed owner store')
        require(w.close() == [], 'Replay worker close failed')
        self.result['checks']['durable_replay_ack_and_process_lock'] = True

        for operation in ('append', 'ack'):
            store = self.store('rollback-' + operation)
            w = self.start(store)
            seed = {seq: self.event(store, seq, 'seed-' + str(seq)) for seq in range(1, 2 if operation == 'append' else 3)}
            for seq, body in seed.items(): self.append(w, seq, body)
            require(w.close() == [], 'Seed close failed')
            w = self.start(store, reopen=True, paused=True)
            if operation == 'append':
                retry = self.event(store, 2, 'retry')
                w.pause_request('append', source_seq=2, body=base64.b64encode(retry).decode())
            else: w.pause_request('ack', ack=self.ack(store, 2))
            self.kill(w, 'before-' + operation + '-commit')
            w = self.start(store, reopen=True); self.retained(w, seed)
            if operation == 'append': self.append(w, 2, retry); self.retained(w, seed | {2: retry})
            else:
                require(self.rpc(w, 'ack', ack=self.ack(store, 2)).get('ok') is True, 'Retry fixture ACK failed')
                self.retained(w, {}, 2)
            require(w.close() == [], 'Rollback worker close failed')
            self.result['checks'][operation + '_precommit_rollback'] = True

        a, b = self.store('capacity-a'), self.store('capacity-b')
        wa, wb, accepted = self.start(a), self.start(b), {}
        for seq in range(1, BUDGET['capacity_attempts'] + 1):
            body = self.event(a, seq, 'E' * 8192)
            reply = self.rpc(wa, 'append', source_seq=seq, body=base64.b64encode(body).decode())
            if reply.get('ok') is not True:
                require(reply.get('code') == 'storage_full' and reply.get('mayHaveCommitted') is False and accepted, 'Expected native SQLITE_FULL missing')
                break
            require(reply['receipt'] == {'level': 'durable_received', 'source_seq': seq, 'duplicate': False}, 'Capacity receipt differs')
            accepted[seq] = body
        else: raise RuntimeError('SQLite engine did not reach its physical page cap')
        self.retained(wa, accepted)
        nonce = self.event(b, 1, uuid.uuid4().hex)
        self.append(wb, 1, nonce); self.retained(wb, {1: nonce})
        self.retained(wa, accepted)
        require(self.rpc(wa, 'ack', ack=self.ack(a, len(accepted))).get('ok') is True, 'Full queue could not drain valid contiguous prefix')
        self.retained(wa, {}, len(accepted))
        seq = len(accepted) + 1
        body = self.event(a, seq, 'reused-capacity')
        self.append(wa, seq, body); self.retained(wa, {seq: body}, seq - 1)
        self.result['checks']['sqlite_full_preserves_drains_and_independent_b'] = True
        self.result['checks']['native_runtime_and_bounds_readbacks'] = bool(self.result['observations'])


def execute(root, spool_data_root):
    require(sys.platform == 'linux' and platform.machine() == 'x86_64' and os.geteuid() != 0
            and sys.version_info[:2] == (3, 12), 'Requires ordinary Linux x86_64 user and Python 3.12')
    kvm.no_symlinks(root)
    root = root.resolve(strict=True)
    executed = {
        'scripts/qualification/qualify_event_spool.py': __file__,
        'scripts/qualification/event_spool_pressure.py': pressure.__file__,
        'scripts/qualification/qualify_kvm.py': kvm.__file__,
        'scripts/qualification/guest_memory_pressure.py': pressure.protocol.__file__,
        'apps/runner/console_store.py': sys.modules[kvm.BoundedConsole.__module__].__file__,
    }
    require(all(Path(path).resolve(strict=True) == root / relative for relative, path in executed.items()),
            'Executing Python sources differ from requested root')
    os.umask(0o077)
    for path in (root / '.private', root / '.private/qualification'): private_directory(path)
    reports = root / '.private/qualification' / ('event-spool-native-' + uuid.uuid4().hex)
    private_directory(reports)
    result = {'started_at': kvm.now(), 'passed': False, 'checks': {}, 'claims': dict(FALSE_CLAIMS),
              'observations': [], 'kills': [], 'cleanup_errors': []}
    qualification, hashes, node_hash = None, {}, None
    data_root_identity, data_run_identity = None, None
    try:
        result['budget_sha256'] = write(reports / 'budget.json', BUDGET)
        space = os.statvfs(root)
        result['free_bytes_before'] = space.f_bavail * space.f_frsize
        df = subprocess.run(['df', '-B1', str(root)], capture_output=True, timeout=5, check=True,
                            env={'PATH': '/usr/bin:/bin', 'LC_ALL': 'C'})
        require(len(df.stdout) <= 65536, 'df output bound')
        write(reports / 'df-before.txt', df.stdout)
        require(result['free_bytes_before'] >= BUDGET['minimum_free_bytes'], 'Less than 50 GiB available')
        data_root_identity = pressure.prepare_data_root(spool_data_root)
        data_run_identity = pressure.create_data_run(data_root_identity, reports.name)
        data_run = pressure.verify_data_run(data_root_identity, data_run_identity)
        result['data_root_identity'], result['data_run_identity'] = data_root_identity, data_run_identity
        result['data_configuration_sha256'] = write(reports / 'data-configuration.json', {
            'data_root_identity': data_root_identity, 'data_run_identity': data_run_identity,
            'store_names': ['replay', 'rollback-append', 'rollback-ack', 'capacity-a', 'capacity-b'],
            'retention': 'preserve_original_data_after_workers_exit',
        })
        data_space = os.statvfs(data_run)
        result['data_free_bytes_before'] = data_space.f_bavail * data_space.f_frsize
        require(result['data_free_bytes_before'] >= BUDGET['minimum_free_bytes'], 'Less than 50 GiB available on spool data filesystem')
        df = subprocess.run(['df', '-B1', str(data_run)], capture_output=True, timeout=5, check=True,
                            env={'PATH': '/usr/bin:/bin', 'LC_ALL': 'C'})
        require(len(df.stdout) <= 65536, 'Data df output bound')
        write(reports / 'df-data-before.txt', df.stdout)
        hashes = snapshot_sources(root, reports)
        node = root / pressure.NODE_RELATIVE
        kvm.no_symlinks(node)
        node_hash = kvm.sha256(node)
        result['sources_sha256'], result['node_sha256'] = hashes, node_hash
        qualification = Qualification(root, reports, result, data_run=data_run)
        qualification.run()
    except BaseException as error:
        result['error_type'] = type(error).__name__
        result['failure_reason'] = str(error)[:1024]
    finally:
        if qualification is not None:
            for worker in reversed(qualification.workers):
                result['cleanup_errors'].extend(worker.close())
            result['checks']['all_owned_workers_exited'] = all(w.process is not None and w.process.poll() is not None for w in qualification.workers)
            result['worker_returncodes'] = [w.process.returncode for w in qualification.workers]
        try:
            result['checks']['immutable_sources'] = bool(hashes) and all(hashlib.sha256(bounded_read(root / p, BUDGET['source_file_bytes'])).hexdigest() == digest for p, digest in hashes.items())
            result['checks']['immutable_runtime'] = node_hash is not None and kvm.sha256(root / pressure.NODE_RELATIVE) == node_hash
        except BaseException as error: result['final_identity_error_type'] = type(error).__name__
        try:
            require(data_root_identity is not None and data_run_identity is not None, 'Private data root was not prepared')
            data_run = pressure.verify_data_run(data_root_identity, data_run_identity)
            require(hashlib.sha256(bounded_read(reports / 'data-configuration.json', 65536)).hexdigest()
                    == result.get('data_configuration_sha256'), 'Private data root catalog changed')
            result['checks']['immutable_data_directories'] = True
            # These hashes are of the original retained SQLite files after
            # every registered worker has exited, not a substitute fixture.
            require(result['checks'].get('all_owned_workers_exited') is True, 'Data manifest requires every owned worker to exit')
            result['data_artifacts'] = pressure.data_tree_manifest(data_root_identity, data_run_identity,
                max_bytes=BUDGET['report_total_bytes'] - 2 * 1024**2)
        except BaseException as error:
            result['checks']['immutable_data_directories'] = False
            result['data_artifact_error_type'] = type(error).__name__
        try:
            space = os.statvfs(root)
            result['free_bytes_after'] = space.f_bavail * space.f_frsize
            result['free_bytes_delta_shared_host'] = result['free_bytes_after'] - result.get('free_bytes_before', result['free_bytes_after'])
            result['space_delta_attributed_to_task'] = False
            result['evidence_retained'] = True
            df = subprocess.run(['df', '-B1', str(root)], capture_output=True, timeout=5, check=True,
                                env={'PATH': '/usr/bin:/bin', 'LC_ALL': 'C'})
            require(len(df.stdout) <= 65536, 'df output bound')
            write(reports / 'df-after.txt', df.stdout)
            if data_run_identity is not None:
                data_run = pressure.verify_data_run(data_root_identity, data_run_identity)
                data_space = os.statvfs(data_run)
                result['data_free_bytes_after'] = data_space.f_bavail * data_space.f_frsize
                df = subprocess.run(['df', '-B1', str(data_run)], capture_output=True, timeout=5, check=True,
                                    env={'PATH': '/usr/bin:/bin', 'LC_ALL': 'C'})
                require(len(df.stdout) <= 65536, 'Data df output bound')
                write(reports / 'df-data-after.txt', df.stdout)
        except BaseException as error: result['final_space_error_type'] = type(error).__name__
        try:
            files, total = {}, result.get('data_artifacts', {}).get('bytes', 0)
            for path in sorted(reports.rglob('*')):
                info = path.lstat()
                require(not stat.S_ISLNK(info.st_mode) and info.st_uid == os.geteuid(), 'Evidence ownership differs')
                if stat.S_ISDIR(info.st_mode): require(stat.S_IMODE(info.st_mode) == 0o700, 'Evidence directory mode differs')
                else:
                    require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1
                            and stat.S_IMODE(info.st_mode) == 0o600, 'Evidence file identity differs')
                    total += info.st_size
                    require(total <= BUDGET['report_total_bytes'] - 2 * 1024**2, 'Total evidence bound differs')
                    files[str(path.relative_to(reports))] = {'bytes': info.st_size, 'sha256': kvm.sha256(path)}
            result['artifact_bytes'], result['artifacts'] = total, files
        except BaseException as error: result['artifact_error_type'] = type(error).__name__
        result['finished_at'] = kvm.now()
        result['passed'] = ('error_type' not in result and 'final_identity_error_type' not in result
                            and 'artifact_error_type' not in result and 'final_space_error_type' not in result
                            and 'data_artifact_error_type' not in result
                            and not result['cleanup_errors'] and set(result['checks']) == REQUIRED_CHECKS and all(result['checks'].values()))
        write(reports / 'report.json', result)
    # Deliberately excludes private paths, scopes, payloads, PIDs and protocol.
    print(json.dumps({'passed': result['passed'], 'checks': result['checks'],
                      'error_type': result.get('error_type')}, sort_keys=True))
    return 0 if result['passed'] else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--spool-data-root', type=Path, required=True,
                        help='Trusted, private 0700 data root outside a shared source checkout; stores are retained')
    args = parser.parse_args()
    def interrupted(_number, _frame):
        # One interruption enters execute's finally; repeated TERM/HUP cannot
        # interrupt its finite owned-child cleanup. An outer SIGKILL still wins.
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
        raise KeyboardInterrupt('Native acceptance interrupted')
    previous = {number: signal.signal(number, interrupted) for number in (signal.SIGTERM, signal.SIGHUP)}
    try:
        try: return execute(args.root.absolute(), args.spool_data_root.absolute())
        except BaseException as error:
            print(json.dumps({'passed': False, 'error_type': type(error).__name__}))
            return 1
    finally:
        for number, handler in previous.items(): signal.signal(number, handler)


if __name__ == '__main__': raise SystemExit(main())
