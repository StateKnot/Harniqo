#!/usr/bin/env python3
"""Read-only, native Linux proof of effective libvirt per-API access control.

Root starts two already-provisioned identities; this tool never installs rules,
changes socket permissions, restarts daemons, or manages domains. A passing
report proves only the declared bootstrap ACL, not production activation.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import selectors
import signal
import socket
import stat
import struct
import subprocess
import sys
import time

URI = 'qemu:///system'
SOCKETS = frozenset({'/run/libvirt/libvirt-sock', '/run/libvirt/virtqemud-sock',
                    '/run/libvirt/virtproxyd-sock'})
MAX_OUTPUT = 65536
CAPABILITIES = ('CapInh', 'CapPrm', 'CapEff', 'CapBnd', 'CapAmb')
ENVIRONMENT = {'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LANG': 'C', 'LC_ALL': 'C',
               'PYTHONDONTWRITEBYTECODE': '1'}


class QualificationError(ValueError):
    def __init__(self, code, evidence=None):
        super().__init__(code)
        self.evidence = evidence


def _require(condition, code):
    if not condition:
        raise QualificationError(code)


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def _pairs(pairs):
    value = {}
    for key, item in pairs:
        _require(key not in value, 'duplicate_json_key')
        value[key] = item
    return value


def _decode(raw):
    _require(len(raw) <= MAX_OUTPUT, 'worker_output_limit')
    try:
        value = json.loads(raw, object_pairs_hook=_pairs,
            parse_constant=lambda unused: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError):
        raise QualificationError('worker_json_invalid') from None
    _require(type(value) is dict, 'worker_json_invalid')
    return value


def _path(value):
    path = Path(value)
    _require(path.is_absolute() and str(path) == value and not value.startswith('//')
        and '..' not in path.parts
        and not any(ord(character) < 32 or ord(character) == 127 for character in value),
        'path_invalid')
    return path


def _trusted(path, *, regular=True):
    for parent in path.parents:
        info = parent.lstat()
        _require(stat.S_ISDIR(info.st_mode) and info.st_uid == 0
            and not info.st_mode & 0o022, 'untrusted_ancestor')
    info = path.lstat()
    _require(info.st_uid == 0 and not info.st_mode & 0o022
        and (stat.S_ISREG(info.st_mode) if regular else stat.S_ISDIR(info.st_mode)),
        'untrusted_file')
    if regular:
        _require(info.st_nlink == 1, 'untrusted_file')
    return info


def _read_process(pid):
    root = Path('/proc') / str(pid)
    def started():
        value = (root / 'stat').read_text()
        return int(value[value.rfind(')') + 2:].split()[19])
    before = started()
    fields = {}
    for line in (root / 'status').read_text().splitlines():
        if ':' in line:
            key, value = line.split(':', 1)
            fields[key] = value.strip()
    result = {'pid': pid, 'start_time': before,
        'uid': [int(item) for item in fields['Uid'].split()],
        'gid': [int(item) for item in fields['Gid'].split()],
        'groups': sorted(int(item) for item in fields['Groups'].split()),
        'capabilities': {name: int(fields[name], 16) for name in CAPABILITIES},
        'no_new_privs': int(fields['NoNewPrivs'])}
    _require(before == started(), 'process_identity_changed')
    return result


def _identity(value, uid, gid, ipc_gid):
    _require(value['uid'] == [uid] * 4 and value['gid'] == [gid] * 4
        and set(value['groups']) == {gid, ipc_gid}, 'worker_identity_mismatch')
    _require(set(value['capabilities']) == set(CAPABILITIES)
        and all(number == 0 for number in value['capabilities'].values())
        and value['no_new_privs'] == 1, 'worker_privilege_invalid')


def _socket_metadata(path):
    info = path.lstat()
    _require(stat.S_ISSOCK(info.st_mode) and info.st_uid == 0, 'management_socket_invalid')
    for parent in path.parents:
        _require(not parent.is_symlink(), 'management_socket_symlink')
    return {'path': str(path), 'device': info.st_dev, 'inode': info.st_ino,
        'uid': info.st_uid, 'gid': info.st_gid, 'mode': format(stat.S_IMODE(info.st_mode), '04o'),
        'ctime_ns': info.st_ctime_ns}


def _raw_connection(path):
    before = _socket_metadata(path)
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(3)
            connection.connect(str(path))
            pid, uid, gid = struct.unpack('3i', connection.getsockopt(socket.SOL_SOCKET,
                socket.SO_PEERCRED, struct.calcsize('3i')))
    except OSError as error:
        raise QualificationError('management_socket_connect_failed', {'errno': error.errno}) from None
    # systemd may have created/listened on an activation socket before passing
    # it to libvirtd. SO_PEERCRED identifies that socket peer, not the daemon.
    _require(pid >= 1 and uid == 0, 'management_peer_invalid')
    _require(before == _socket_metadata(path), 'management_socket_changed')
    return {'connected': True, 'socket': before, 'peer': {'pid': pid, 'uid': uid, 'gid': gid}}


def _native_socket(path, peer):
    """Read the live client's own UNIX peer; no access to another process FDs."""
    matches = []
    names = list(Path('/proc/self/fd').iterdir())
    _require(len(names) <= 128, 'native_descriptor_limit')
    for entry in names:
        duplicate = None; descriptor = None
        try:
            descriptor = os.dup(int(entry.name))
            duplicate = socket.socket(fileno=descriptor)
            descriptor = None
            address = duplicate.getpeername()
            if address not in {str(path), '/var/run/' + str(path).removeprefix('/run/')}:
                continue
            actual = struct.unpack('3i', duplicate.getsockopt(socket.SOL_SOCKET,
                socket.SO_PEERCRED, struct.calcsize('3i')))
            _require(actual == (peer['pid'], peer['uid'], peer['gid']), 'native_peer_mismatch')
            matches.append({'peer_path': address, 'peer': peer})
        except OSError:
            pass
        finally:
            if duplicate is not None:
                duplicate.close()
            elif descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
    _require(len(matches) == 1, 'native_management_socket_unconfirmed')
    return matches[0]


def _api_denial(call, module, name, permission):
    try:
        value = call()
    except module.libvirtError as error:
        result = {'call': name, 'permission': permission, 'code': error.get_error_code(),
                  'domain': error.get_error_domain()}
        result['access_denied'] = result['code'] == 88 and result['domain'] == 55
        if not result['access_denied']:
            raise QualificationError('unexpected_libvirt_denial', result) from None
        return result
    if value is not None and hasattr(value, 'close'):
        value.close()
    raise QualificationError('forbidden_api_allowed', {'call': name, 'permission': permission})


def _positive(call, module, name):
    try:
        return call()
    except module.libvirtError as error:
        raise QualificationError('permitted_api_denied', {'call': name,
            'code': error.get_error_code(), 'domain': error.get_error_domain()}) from None


def _libvirt_probe(module, role, path, raw, native_check=_native_socket):
    module.registerErrorHandler(lambda unused_context, unused_error: None, None)
    _require(module.VIR_ERR_ACCESS_DENIED == 88 and module.VIR_FROM_ACCESS == 55,
        'unsupported_libvirt_error_contract')
    if role == 'supervisor':
        # qemuConnectOpen itself checks connect.getattr; all-NO denies here.
        return {'rw_open_requested': True, 'uri': URI,
            'denial': _api_denial(lambda: module.open(URI), module, 'virConnectOpen',
                'connect.getattr'), 'connection_established': False,
            'route_evidence': 'same_fixed_uri_as_broker_and_successful_raw_rw_socket_connect'}
    # Never openReadOnly, never transport fallback.
    connection = _positive(lambda: module.open(URI), module, 'virConnectOpen')
    _require(connection is not None, 'native_rw_connection_missing')
    try:
        route = native_check(path, raw['peer'])
        hostname = _positive(connection.getHostname, module, 'virConnectGetHostname')
        _require(type(hostname) is str and 0 < len(hostname.encode()) <= 4096,
            'hostname_positive_missing')
        negative = _api_denial(connection.getCapabilities, module,
            'virConnectGetCapabilities', 'connect.read')
        _require(_positive(connection.getHostname, module, 'virConnectGetHostname') == hostname,
            'same_connection_positive_lost')
        return {'uri': URI, 'rw_open_requested': True, 'connection_established': True,
            'native_socket': route, 'hostname_before_and_after_denial': True,
            'hostname_sha256': hashlib.sha256(hostname.encode()).hexdigest(),
            'denial': negative, 'same_connection': True}
    finally:
        connection.close()


def _emit(value):
    sys.stdout.buffer.write(_json(value) + b'\n')
    sys.stdout.buffer.flush()


def _parent_death(parent_pid):
    _require(type(parent_pid) is int and parent_pid > 1 and os.getppid() == parent_pid,
        'parent_identity_invalid')
    import ctypes
    library = ctypes.CDLL(None, use_errno=True)
    library.prctl.argtypes = [ctypes.c_int] + [ctypes.c_ulong] * 4
    library.prctl.restype = ctypes.c_int
    _require(library.prctl(1, signal.SIGKILL, 0, 0, 0) == 0, 'parent_death_signal_failed')
    _require(os.getppid() == parent_pid, 'parent_identity_changed')


def _worker(args):
    _require(sys.platform.startswith('linux'), 'native_linux_required')
    _parent_death(args.parent_pid)
    process = _read_process(os.getpid())
    uid, gid = ((args.broker_uid, args.broker_gid) if args.worker == 'broker' else
                (args.supervisor_uid, args.supervisor_gid))
    _identity(process, uid, gid, args.ipc_gid)
    _emit({'event': 'ready', 'process': process})
    import select
    _require(bool(select.select([sys.stdin.buffer], [], [], 5)[0]), 'parent_handshake_timeout')
    _require(os.read(sys.stdin.fileno(), 128) == b'GO\n', 'parent_handshake_invalid')
    try:
        import libvirt
    except ImportError:
        raise QualificationError('native_libvirt_binding_missing') from None
    binding = Path(libvirt.__file__).resolve()
    _trusted(binding)
    raw = _raw_connection(_path(args.socket))
    probe = _libvirt_probe(libvirt, args.worker, _path(args.socket), raw)
    _require(raw['socket'] == _socket_metadata(_path(args.socket)), 'management_socket_changed')
    after = _read_process(os.getpid())
    _require(process == after, 'worker_identity_changed')
    _emit({'event': 'result', 'passed': True, 'role': args.worker, 'process': after,
        'raw_rw_socket': raw, 'probe': probe,
        'binding': {'path': str(binding), 'sha256': hashlib.sha256(binding.read_bytes()).hexdigest(),
                    'libvirt_version': libvirt.getVersion()}})
    return 0


def _exception_info(error):
    value = {'type': type(error).__name__}
    if isinstance(error, QualificationError):
        value['code'] = str(error)
    if isinstance(error, OSError):
        value['errno'] = error.errno
    if isinstance(error, subprocess.TimeoutExpired):
        value['timeout'] = error.timeout
    return value


def _wait_exit(process, deadline):
    """Observe Linux exit status while retaining the owned leader PID."""
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(process.args, 0)
        result = os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOWAIT | os.WNOHANG)
        if result is not None:
            _require(result.si_pid == process.pid, 'worker_exit_identity_mismatch')
            if result.si_code == os.CLD_EXITED:
                return result.si_status
            _require(result.si_code in {os.CLD_KILLED, os.CLD_DUMPED}, 'worker_exit_status_invalid')
            return -result.si_status
        time.sleep(min(.02, remaining))


def _terminate(process):
    # No poll/wait/reap precedes this, even on success. WNOWAIT retains the
    # leader PID, so the registered group cannot be confused with a reused PID.
    # Kill descendants with closed/DEVNULL pipes before finally reaping leader.
    errors = []
    if process.returncode is None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except BaseException as error:
            errors.append({'phase': 'kill_owned_group', 'error': _exception_info(error)})
    try:
        process.wait(timeout=3)
    except BaseException as error:
        errors.append({'phase': 'reap_owned_leader', 'error': _exception_info(error)})
    return errors


def _run_child(command, *, role, timeout, on_ready, output_limit=MAX_OUTPUT):
    """Bounded real subprocess with parent-verified identity before native I/O."""
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, env=ENVIRONMENT, cwd='/', close_fds=True, start_new_session=True)
    selector = selectors.DefaultSelector()
    output, errors = bytearray(), bytearray()
    snapshot = None; ready_seen = False; output_bytes = 0
    stdout_hash = hashlib.sha256(); stderr_hash = hashlib.sha256()
    stdout_bytes = 0; stderr_bytes = 0
    receipt = None; failure = None; original = None; phase = 'read_worker_output'
    try:
        selector.register(process.stdout, selectors.EVENT_READ, output)
        selector.register(process.stderr, selectors.EVENT_READ, errors)
        until = time.monotonic() + timeout
        while selector.get_map():
            _require(time.monotonic() < until, 'worker_timeout')
            for key, unused in selector.select(min(.1, max(.001, until - time.monotonic()))):
                chunk = os.read(key.fileobj.fileno(), 8192)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                key.data.extend(chunk)
                output_bytes += len(chunk)
                if key.data is output:
                    stdout_hash.update(chunk); stdout_bytes += len(chunk)
                else:
                    stderr_hash.update(chunk); stderr_bytes += len(chunk)
                _require(output_bytes <= output_limit, 'worker_output_limit')
                if not ready_seen and b'\n' in output:
                    first, rest = bytes(output).split(b'\n', 1)
                    event = _decode(first)
                    _require(set(event) == {'event', 'process'} and event['event'] == 'ready',
                        'worker_ready_invalid')
                    snapshot = on_ready(process.pid, event['process'])
                    output[:] = rest; ready_seen = True
                    process.stdin.write(b'GO\n'); process.stdin.flush(); process.stdin.close()
        phase = 'wait_for_worker_exit'
        code = _wait_exit(process, until)
        phase = 'decode_worker_result'
        _require(ready_seen, 'worker_exited_before_ready')
        final = _decode(bytes(output))
        _require(final.get('event') == 'result', 'worker_result_invalid')
        receipt = {'role': role, 'exit_code': code, 'parent_process_observation': snapshot,
            'result': final, 'stdout_sha256': stdout_hash.hexdigest(), 'stdout_bytes': stdout_bytes,
            'stderr_sha256': stderr_hash.hexdigest(), 'stderr_bytes': stderr_bytes}
    except BaseException as error:
        original = error
        if isinstance(error, QualificationError):
            failure = error
        else:
            code = 'worker_timeout_or_exit' if isinstance(error,
                (subprocess.TimeoutExpired, BrokenPipeError)) else 'worker_process_failed'
            failure = QualificationError(code)
    # Each cleanup attempt is independent. Never hide an original failure with
    # a second signal, pipe close error, or a bounded wait failure in cleanup.
    cleanup_errors = _terminate(process)
    for label, stream in (('selector', selector), ('stdin', process.stdin),
                           ('stdout', process.stdout), ('stderr', process.stderr)):
        try:
            stream.close()
        except BaseException as error:
            cleanup_errors.append({'phase': 'close_' + label, 'error': _exception_info(error)})
    if failure is not None or cleanup_errors:
        context = {'role': role, 'phase': phase, 'parent_process_observation': snapshot,
            'stdout_bytes': stdout_bytes, 'stderr_bytes': stderr_bytes,
            'stdout_sha256': stdout_hash.hexdigest(), 'stderr_sha256': stderr_hash.hexdigest()}
        if original is not None:
            context['original_exception'] = _exception_info(original)
        if failure is not None and failure.evidence is not None:
            context['original_failure_evidence'] = failure.evidence
        if cleanup_errors:
            context['cleanup_errors'] = cleanup_errors
        if receipt is not None:
            context['worker_result'] = receipt['result']
        if failure is None:
            failure = QualificationError('worker_cleanup_failed')
        failure.evidence = context
        if original is failure:
            raise failure
        raise failure from original
    return receipt


def _write_report(path, value):
    _trusted(path.parent, regular=False)
    _require(stat.S_IMODE(path.parent.stat().st_mode) == 0o700, 'private_report_parent_required')
    data = _json(value) + b'\n'
    _require(len(data) <= 4 * MAX_OUTPUT, 'report_output_limit')
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        view = memoryview(data)
        while view:
            written = os.write(descriptor, view)
            _require(written > 0, 'report_write_failed')
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return hashlib.sha256(data).hexdigest()


def qualify(args):
    _require(sys.platform.startswith('linux') and os.getuid() == os.geteuid() == 0,
        'native_root_parent_required')
    _require(5 <= args.timeout <= 120, 'timeout_invalid')
    ids = (args.broker_uid, args.broker_gid, args.supervisor_uid, args.supervisor_gid, args.ipc_gid)
    _require(all(type(number) is int and 0 < number < 2**31 for number in ids)
        and args.broker_uid != args.supervisor_uid
        and len({args.broker_gid, args.supervisor_gid, args.ipc_gid}) == 3, 'identities_invalid')
    for uid, gid in ((args.broker_uid, args.broker_gid), (args.supervisor_uid, args.supervisor_gid)):
        account = pwd.getpwuid(uid)
        _require(account.pw_gid == gid and set(os.getgrouplist(account.pw_name, gid)) == {gid, args.ipc_gid},
            'nss_identity_mismatch')
    source = Path(__file__).absolute()
    _trusted(source)
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    _require(re.fullmatch('[0-9a-f]{64}', args.source_sha256 or '') is not None
        and digest == args.source_sha256, 'source_digest_mismatch')
    for executable in ('/usr/bin/setpriv', '/usr/bin/python3'):
        # Distribution interpreter may be a root-controlled symlink.
        _trusted(Path(executable).resolve())
    target = _path(args.socket)
    _require(str(target) in SOCKETS, 'management_socket_not_allowed')
    baseline_socket = _socket_metadata(target)
    report_path = _path(args.report)
    _trusted(report_path.parent, regular=False)
    _require(not os.path.lexists(report_path), 'report_already_exists')
    report = {'schema_version': 1, 'qualification': 'M0-05-effective-libvirt-bootstrap-acl',
        'status': 'FAILED', 'scope': 'native_dedicated_identity_rw_connection_per_api_acl_only',
        'production_activation_verified': False, 'vm_lifecycle_exercised': False,
        'source_sha256': digest, 'management_socket': baseline_socket, 'workers': {},
        'started_at': time.time(), 'errors': []}
    until = time.monotonic() + args.timeout
    try:
        for role, uid, gid in (('broker', args.broker_uid, args.broker_gid),
                               ('supervisor', args.supervisor_uid, args.supervisor_gid)):
            def ready(pid, worker, uid=uid, gid=gid):
                actual = _read_process(pid)
                _identity(actual, uid, gid, args.ipc_gid)
                _require(worker == actual, 'worker_process_evidence_mismatch')
                return actual
            command = ['/usr/bin/setpriv', '--no-new-privs', '--bounding-set=-all',
                '--inh-caps=-all', '--ambient-caps=-all', '--reuid=' + str(uid),
                '--regid=' + str(gid), '--groups=' + str(gid) + ',' + str(args.ipc_gid),
                '/usr/bin/python3', '-I', '-B', str(source), '--worker', role,
                '--broker-uid', str(args.broker_uid), '--broker-gid', str(args.broker_gid),
                '--supervisor-uid', str(args.supervisor_uid), '--supervisor-gid', str(args.supervisor_gid),
                '--ipc-gid', str(args.ipc_gid), '--socket', str(target),
                '--parent-pid', str(os.getpid())]
            remaining = until - time.monotonic()
            _require(remaining > 0, 'qualification_timeout')
            report['workers'][role] = _run_child(command, role=role, timeout=remaining, on_ready=ready)
            worker = report['workers'][role]
            _require(worker['exit_code'] == 0 and worker['result'].get('passed') is True,
                worker['result'].get('error_code', 'worker_failed'))
            _require(worker['stderr_bytes'] == 0, 'worker_stderr_unexpected')
            _require(baseline_socket == _socket_metadata(target), 'management_socket_changed')
            _require(hashlib.sha256(source.read_bytes()).hexdigest() == digest, 'source_changed')
        broker = report['workers']['broker']['result']; peer = report['workers']['supervisor']['result']
        _require(broker['raw_rw_socket']['peer'] == peer['raw_rw_socket']['peer'], 'management_peer_changed')
        _require(broker['binding'] == peer['binding'], 'native_binding_changed')
        report['status'] = 'PASSED'
    except Exception as error:
        report['errors'].append(str(error) if isinstance(error, QualificationError)
            else 'native_qualification_failed')
        if isinstance(error, QualificationError) and error.evidence is not None:
            report['failure_evidence'] = error.evidence
    report['finished_at'] = time.time()
    digest = _write_report(report_path, report)
    _emit({'qualification': report['qualification'], 'status': report['status'],
        'report_sha256': digest, 'production_activation_verified': False,
        'vm_lifecycle_exercised': False, 'errors': report['errors']})
    return 0 if report['status'] == 'PASSED' else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--broker-uid', type=int, required=True)
    parser.add_argument('--broker-gid', type=int, required=True)
    parser.add_argument('--supervisor-uid', type=int, required=True)
    parser.add_argument('--supervisor-gid', type=int, required=True)
    parser.add_argument('--ipc-gid', type=int, required=True)
    parser.add_argument('--socket', default='/run/libvirt/libvirt-sock')
    parser.add_argument('--source-sha256')
    parser.add_argument('--report')
    parser.add_argument('--timeout', type=float, default=30)
    parser.add_argument('--worker', choices=('broker', 'supervisor'), help=argparse.SUPPRESS)
    parser.add_argument('--parent-pid', type=int, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        if args.worker:
            return _worker(args)
        def interrupted(unused_signal, unused_frame):
            raise QualificationError('qualification_interrupted')
        for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            signal.signal(number, interrupted)
        _require(args.report is not None, 'report_path_required')
        return qualify(args)
    except Exception as error:
        result = {'event': 'result', 'passed': False, 'error_code': str(error)
            if isinstance(error, QualificationError) else 'native_qualification_failed'}
        if isinstance(error, QualificationError) and error.evidence is not None:
            result['failure_evidence'] = error.evidence
        _emit(result)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
