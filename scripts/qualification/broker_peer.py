#!/usr/bin/env python3
"""Finite signed-envelope peer for qualification; not a production Supervisor.

The trusted issuer supplies a pinned, root-controlled phase. This peer never
reads an issuer key, signs a claim, constructs a Broker, or retries an outcome.
Only the four production BrokerClient methods are called.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import asdict
import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import stat
import sys
import time
import uuid

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
from apps.runner.resource_broker import Assignment, Grant
from apps.runner.console_store import BoundedConsole, ConsoleStoreError
from apps.runner.resource_broker.ipc import BrokerClient, IPCError

MAX_INPUT = 32768
MAX_REPORT = 65536
MAX_OPERATIONS = 8
CAPS = ('CapInh', 'CapPrm', 'CapEff', 'CapBnd', 'CapAmb')
ENVELOPE_FIELDS = frozenset(Assignment.__dataclass_fields__)
SCOPE_FIELDS = frozenset({'resource_id', 'organization_id', 'workspace_id',
                          'template_id', 'configuration_sha256'})


class PeerError(ValueError):
    pass


class EvidenceError(PeerError):
    pass


def safe_error(error):
    if isinstance(error, ConsoleStoreError):
        return error.code
    if isinstance(error, PeerError):
        return str(error)
    return 'peer_evidence_io_failed'


def record_event(record, value):
    if record is not None:
        try:
            record(value)
        except BaseException as error:
            raise EvidenceError(safe_error(error)) from error


def require(value, code):
    if not value:
        raise PeerError(code)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def fields(value, names):
    require(type(value) is dict and set(value) == set(names), 'phase_shape_invalid')


def identifier(value):
    return type(value) is str and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}', value) is not None


def digest(value):
    return type(value) is str and re.fullmatch(r'[0-9a-f]{64}', value) is not None


def identity_id(value):
    return type(value) is int and 0 < value < 2**31


def resource_uuid(value):
    try:
        return type(value) is str and str(uuid.UUID(value)) == value
    except (ValueError, TypeError, AttributeError):
        return False


def absolute(value):
    require(type(value) is str and 0 < len(os.fsencode(value)) <= 4096
        and not any(ord(c) < 32 or ord(c) == 127 for c in value), 'phase_path_invalid')
    path = Path(value)
    require(path.is_absolute() and str(path) == value and '..' not in path.parts
        and path != Path('/') and not value.startswith('//'), 'phase_path_invalid')
    return path


def parse_phase(raw):
    """Pure finite grammar; a resource scope binds every operation in the phase."""
    require(type(raw) is bytes and 0 < len(raw) <= MAX_INPUT, 'phase_input_size')
    def pairs(items):
        value = {}
        for key, item in items:
            require(key not in value, 'phase_duplicate_member')
            value[key] = item
        return value
    def integer(value):
        require(len(value) <= 20, 'phase_number_invalid')
        return int(value)
    def floating(value):
        result = float(value)
        require(math.isfinite(result), 'phase_number_invalid')
        return result
    def constant(unused):
        raise PeerError('phase_number_invalid')
    try:
        phase = json.loads(raw.decode('utf-8'), object_pairs_hook=pairs, parse_int=integer,
            parse_float=floating, parse_constant=constant)
    except (UnicodeError, ValueError, RecursionError, OverflowError) as error:
        if isinstance(error, PeerError):
            raise
        raise PeerError('phase_json_invalid') from None
    fields(phase, {'schema_version', 'peer', 'broker', 'resource', 'timeout_seconds', 'operations'})
    require(type(phase['schema_version']) is int and phase['schema_version'] == 1, 'phase_version_invalid')
    fields(phase['peer'], {'uid', 'gid', 'ipc_gid'})
    fields(phase['broker'], {'uid', 'gid', 'ipc_directory'})
    peer, broker, scope = phase['peer'], phase['broker'], phase['resource']
    require(all(identity_id(peer[k]) for k in peer)
        and all(identity_id(broker[k]) for k in ('uid', 'gid'))
        and peer['uid'] != broker['uid']
        and len({peer['gid'], peer['ipc_gid'], broker['gid']}) == 3, 'phase_identity_invalid')
    absolute(broker['ipc_directory'])
    fields(scope, SCOPE_FIELDS)
    require(resource_uuid(scope['resource_id']) and digest(scope['configuration_sha256'])
        and all(identifier(scope[k]) for k in ('organization_id', 'workspace_id', 'template_id')),
        'phase_scope_invalid')
    require(type(phase['timeout_seconds']) is int and 1 <= phase['timeout_seconds'] <= 60,
        'phase_timeout_invalid')
    steps = phase['operations']
    require(type(steps) is list and 1 <= len(steps) <= MAX_OPERATIONS, 'phase_operations_invalid')
    for step in steps:
        require(type(step) is dict and type(step.get('operation')) is str, 'phase_operation_invalid')
        op = step['operation']
        require(op in {'ensure', 'renew', 'observe', 'revoke'}, 'phase_operation_invalid')
        fields(step, {'operation', 'envelope'} if op in {'ensure', 'renew'} else
            {'operation', 'cause'} if op == 'revoke' else {'operation'})
        if op == 'revoke':
            require(type(step['cause']) is str and step['cause'] in {'cancelled', 'stop_intent'}, 'phase_cause_invalid')
        if op in {'ensure', 'renew'}:
            envelope = step['envelope']; fields(envelope, ENVELOPE_FIELDS)
            require(all(envelope[k] == scope[k] for k in SCOPE_FIELDS), 'phase_envelope_scope_mismatch')
            require(identifier(envelope['attempt_id']) and digest(envelope['signature']), 'phase_envelope_invalid')
            require(all(type(envelope[k]) is int and 1 <= envelope[k] <= 9007199254740991
                for k in ('generation', 'recovery_generation')), 'phase_envelope_invalid')
            require(all(type(envelope[k]) in (float, int) and math.isfinite(envelope[k])
                for k in ('issued_at', 'expires_at')), 'phase_envelope_invalid')
    return phase


def no_links(path):
    for item in (path, *path.parents):
        require(not item.is_symlink(), 'phase_path_symlink')


def file_token(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid,
            info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def read_phase(path, expected_sha256):
    require(digest(expected_sha256), 'phase_digest_invalid')
    path = absolute(str(path)); no_links(path)
    for parent in path.parents:
        info = parent.lstat()
        require(stat.S_ISDIR(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o022,
            'phase_parent_untrusted')
    before = path.lstat()
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    with os.fdopen(fd, 'rb') as stream:
        require(file_token(os.fstat(stream.fileno())) == file_token(before)
            and stat.S_ISREG(before.st_mode) and before.st_uid == 0
            and before.st_nlink == 1 and stat.S_IMODE(before.st_mode) == 0o640
            and 0 < before.st_size <= MAX_INPUT, 'phase_file_untrusted')
        raw = stream.read(MAX_INPUT + 1)
        require(file_token(before) == file_token(os.fstat(stream.fileno())) == file_token(path.lstat()),
            'phase_file_changed')
    require(hashlib.sha256(raw).hexdigest() == expected_sha256, 'phase_digest_mismatch')
    phase = parse_phase(raw)
    require(before.st_gid == phase['peer']['gid'], 'phase_file_group_invalid')
    return phase


def process_identity(peer):
    require(sys.platform.startswith('linux') and os.getuid() == os.geteuid() != 0,
        'peer_linux_identity_required')
    status = dict(line.split(':', 1) for line in Path('/proc/self/status').read_text().splitlines() if ':' in line)
    proc_stat = Path('/proc/self/stat').read_text()
    started = int(proc_stat[proc_stat.rfind(')') + 2:].split()[19])
    value = {'pid': os.getpid(), 'start_time': started, 'uid': [int(x) for x in status['Uid'].split()],
        'gid': [int(x) for x in status['Gid'].split()],
        'groups': sorted(int(x) for x in status['Groups'].split()),
        'capabilities': {k: int(status[k], 16) for k in CAPS},
        'no_new_privs': int(status['NoNewPrivs'])}
    require(value['uid'] == [peer['uid']] * 4 and value['gid'] == [peer['gid']] * 4
        and set(value['groups']) == {peer['gid'], peer['ipc_gid']}, 'peer_identity_mismatch')
    require(all(v == 0 for v in value['capabilities'].values()) and value['no_new_privs'] == 1,
        'peer_privilege_invalid')
    return value


@contextmanager
def call_deadline(seconds):
    """Bound constructor/native wait as well as the SDK's transport budget."""
    require(signal.getitimer(signal.ITIMER_REAL) == (0, 0), 'peer_timer_already_active')
    previous = signal.getsignal(signal.SIGALRM)
    def expired(unused_signal, unused_frame):
        raise PeerError('phase_deadline_exhausted')
    signal.signal(signal.SIGALRM, expired)
    try:
        signal.setitimer(signal.ITIMER_REAL, seconds)
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def execute_phase(phase, *, client_factory=BrokerClient, monotonic=time.monotonic, record=None, result=None):
    """Execute once in order, stopping at the first error without retry/replay."""
    if result is None:
        result = {'status': 'FAILED', 'operations': [], 'error': None}
    deadline = monotonic() + phase['timeout_seconds']
    broker, scope = phase['broker'], phase['resource']
    for index, step in enumerate(phase['operations']):
        operation = step['operation']; attempted = False; response_received = False
        try:
            remaining = deadline - monotonic()
            require(remaining > 0, 'phase_deadline_exhausted')
            record_event(record, {'event': 'dispatch_intent', 'index': index,
                'operation': operation, 'resource_id': scope['resource_id']})
            remaining = deadline - monotonic()
            require(remaining > 0, 'phase_deadline_exhausted')
            with call_deadline(remaining):
                client = client_factory(directory=Path(broker['ipc_directory']),
                    server_uid=broker['uid'], server_gid=broker['gid'], timeout_seconds=min(60, remaining))
                attempted = True
                if operation in {'ensure', 'renew'}:
                    envelope = (Assignment if operation == 'ensure' else Grant)(**step['envelope'])
                    observation = getattr(client, operation)(envelope)
                elif operation == 'revoke':
                    observation = client.revoke(scope['resource_id'], cause=step['cause'])
                else:
                    observation = client.observe(scope['resource_id'])
            require(observation.resource_id == scope['resource_id'], 'peer_resource_reply_mismatch')
            receipt = {'operation': operation, 'observation': asdict(observation)}
            result['operations'].append(receipt); response_received = True
            record_event(record, {'event': 'operation_receipt', 'index': index, **receipt})
        except BaseException as error:
            code = error.code if isinstance(error, IPCError) else str(error) if isinstance(error, PeerError) else 'peer_operation_failed'
            outcome = error.outcome if isinstance(error, IPCError) else 'unconfirmed' if attempted else 'not_dispatched'
            result['error'] = {'operation': operation, 'code': code, 'outcome': outcome,
                'exception_type': type(error).__name__, 'response_received': response_received}
            if isinstance(error, EvidenceError):
                result['error']['code'] = 'peer_evidence_write_failed'
                result['evidence_error'] = safe_error(error)
            else:
                try:
                    record_event(record, {'event': 'operation_error', 'index': index,
                        'error': result['error']})
                except EvidenceError as storage_error:
                    result['evidence_error'] = safe_error(storage_error)
            return result
    result['status'] = 'PASSED'
    return result


class ReportFiles:
    """Two fresh bounded files admitted and physically reserved before dispatch.

    The events file is a durability record, not a resume/replay authority. A
    partial prefix is retained on failure. The final file holds one JSON object.
    """
    def __init__(self, path, *, store_factory=BoundedConsole.create):
        self.final = self.events = None
        self.path = absolute(str(path)); self.events_path = self.path.with_name(self.path.name + '.events')
        try:
            self.final = store_factory(self.path, capacity_bytes=MAX_REPORT)
            self.events = store_factory(self.events_path, capacity_bytes=MAX_REPORT)
        except BaseException:
            self.close()
            raise

    def record(self, value):
        self.events.append(canonical(value) + b'\n')

    def finalize(self, report):
        raw = canonical(report) + b'\n'; require(len(raw) <= MAX_REPORT, 'peer_report_limit')
        self.final.append(raw)
        observed = os.pread(self.final.fd, len(raw) + 1, 0)
        require(observed == raw and self.final.snapshot()['durable_bytes'] == len(raw),
            'peer_report_readback_failed')
        return hashlib.sha256(raw).hexdigest()

    def close(self):
        errors = []
        for store in (self.events, self.final):
            if store is not None:
                try:
                    store.close()
                except BaseException as error:
                    errors.append(safe_error(error))
        return errors


def run(args, *, client_factory=BrokerClient, reports_factory=ReportFiles, monotonic=time.monotonic):
    phase = read_phase(args.input, args.sha256)
    identity = process_identity(phase['peer'])
    report = {'schema_version': 1, 'qualification_peer_only': True,
        'production_supervisor_integrated': False, 'input_sha256': args.sha256,
        'process': identity, 'started_at': time.time(), 'status': 'FAILED',
        'operations': [], 'error': None}
    files = None; report_digest = None; finalize_errors = []
    try:
        # Both exclusive targets, private-parent admission and block reservations
        # must succeed before phase intent or the first client constructor.
        files = reports_factory(args.report)
        files.record({'event': 'phase_intent', 'input_sha256': args.sha256,
            'resource': phase['resource'], 'process': identity})
        execute_phase(phase, client_factory=client_factory,
            monotonic=monotonic, record=files.record, result=report)
        require(process_identity(phase['peer']) == identity, 'peer_identity_changed')
    except BaseException as error:
        report['status'] = 'FAILED'
        if report['error'] is None:
            report['error'] = {'code': safe_error(error), 'outcome': 'unconfirmed' if report['operations'] else 'not_dispatched',
                'exception_type': type(error).__name__}
    finally:
        # Catchable repeated signals may not discard already-received replies
        # while finalizing. SIGKILL/power loss retain the fsynced events prefix.
        signals = {signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGALRM}
        old_mask = signal.pthread_sigmask(signal.SIG_BLOCK, signals)
        try:
            report['finished_at'] = time.time()
            if files is not None:
                try:
                    files.record({'event': 'phase_result', 'status': report['status'], 'error': report['error']})
                except BaseException as error:
                    finalize_errors.append(safe_error(error))
                    report['status'] = 'FAILED'
                try:
                    report_digest = files.finalize(report)
                except BaseException as error:
                    finalize_errors.append(safe_error(error))
                finalize_errors.extend(files.close())
            else:
                finalize_errors.append('peer_report_not_admitted')
            # Do not replace or strip operations / IPC code/outcome on any
            # finalization failure. The stdout fallback carries full memory.
            event = {'event': 'peer_complete', **report, 'report_sha256': report_digest,
                'report_finalization_errors': finalize_errors}
            if finalize_errors:
                event['status'] = 'FAILED'
            raw = canonical(event) + b'\n'; require(len(raw) <= MAX_REPORT, 'peer_output_limit')
            sys.stdout.buffer.write(raw); sys.stdout.buffer.flush()
            code = 0 if event['status'] == 'PASSED' else 1
        finally:
            # Restore the mask after stdout evidence is exposed; a pending
            # interruption can fail the process but cannot erase that record.
            signal.pthread_sigmask(signal.SIG_SETMASK, old_mask)
    return code


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', required=True, type=Path)
    parser.add_argument('--sha256', required=True)
    parser.add_argument('--report', required=True, type=Path)
    args = parser.parse_args(argv)
    previous = {}
    def interrupted(unused_number, unused_frame):
        raise PeerError('peer_interrupted')
    try:
        for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            previous[number] = signal.signal(number, interrupted)
        return run(args)
    except BaseException as error:
        code = str(error) if isinstance(error, PeerError) else 'peer_qualification_failed'
        sys.stdout.buffer.write(canonical({'event': 'peer_failed', 'status': 'FAILED', 'error_code': code,
            'qualification_peer_only': True, 'production_supervisor_integrated': False}) + b'\n')
        sys.stdout.buffer.flush()
        return 1
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


if __name__ == '__main__':
    raise SystemExit(main())
