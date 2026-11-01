#!/usr/bin/env python3
"""Qualify the real persistent ResourceBroker with sealed KVMs and killed workers.

No sudo, model requests, NICs, provider credentials or firewall changes. This is
an acceptance operator, not a replacement Broker implementation or service UID.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, replace
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import pwd
import resource
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
import uuid

import qualify_kvm as kvm
import qualify_kvm_network as network
import guest_memory_pressure as memory_pressure
import console_pressure
import event_spool_pressure
import host_qemu_oom as host_oom

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
DISK_BYTES = 8 * 1024**3
MAX_CONFIG_BYTES = 256 * 1024
SERVICE_MODULE = 'apps.runner.resource_broker.service'
SERVICE_SOURCES = ('apps/runner/resource_broker/service.py', 'apps/runner/resource_broker/ipc.py')
SERVICE_CHECKS = frozenset({'service_initialized_explicitly', 'service_operational_readiness',
    'ipc_scope_denied', 'service_server_killed_with_both_vms_live',
    'service_restart_preserves_vm_identities', 'service_watch_survives_server_restart',
    'service_sigterm_closes_admission', 'service_sigterm_preserves_live_vm',
    'four_operations_over_ipc', 'service_configuration_frozen', 'service_live_revoke_confirmed'})


def sealed_guest_config(instance: dict, other: dict, root: Path, *, resource_profile: str = "sealed-raw8g-q35-v1", pressure: bool = False, guest_memory_pressure: bool = False, console_pressure_mode: bool = False, event_spool_pressure_mode: bool = False, host_oom_mode: bool = False) -> str:
    """Run the unchanged sealed probe before unrelated final-stage services."""
    config = json.loads(kvm.guest_config(instance, other, root, DISK_BYTES).split("\n", 1)[1])
    program = config.pop("write_files")[0]["content"]
    config.pop("runcmd")
    if resource_profile == "sealed-raw8g-q35-limited-v2" or host_oom_mode:
        # Qualification fixture only: report the unchanged eleven checks first.
        # CPU pressure and the independent control heartbeat stay in the Guest.
        if pressure and host_oom_mode:
            program += host_oom.guest_program()
        elif pressure and event_spool_pressure_mode:
            program += event_spool_pressure.guest_program(instance['event_scope'])
        elif pressure and console_pressure_mode:
            program += console_pressure.guest_program()
        elif pressure and guest_memory_pressure:
            program += memory_pressure.supervised_guest_program()
        elif pressure:
            program += "\nprint('HARNIQO-RESOURCE-START ' + C['nonce'], flush=True)\nuntil=time.monotonic()+45\nwhile time.monotonic()<until: pass\n"
        else:
            program += r"""
import select,re
fd=os.open('/dev/virtio-ports/org.harniqo.m0', os.O_RDWR|os.O_NONBLOCK)
print('HARNIQO-RESOURCE-START ' + C['nonce'], flush=True)
until=time.monotonic()+45
pending=b''
while time.monotonic()<until:
    if not select.select([fd],[],[],0.25)[0]: continue
    chunk=os.read(fd,1024)
    if not chunk: continue
    pending+=chunk
    if len(pending)>4096: raise RuntimeError('health challenge bound')
    while b'\n' in pending:
        line,pending=pending.split(b'\n',1)
        if re.fullmatch(b'[0-9a-f]{40}',line):
            print('HARNIQO-HEARTBEAT '+C['nonce']+' '+line.decode('ascii'),flush=True)
os.close(fd)
"""
        if (guest_memory_pressure or console_pressure_mode or event_spool_pressure_mode or host_oom_mode) and not pressure:
            lifetime = 150 if host_oom_mode else 120
            program = program.replace("until=time.monotonic()+45", "until=time.monotonic()+" + str(lifetime))
        program += "print('HARNIQO-RESOURCE-END ' + C['nonce'], flush=True)\n"
    config["bootcmd"] = [["/usr/bin/python3", "-c", program]]
    return "#cloud-config\n" + json.dumps(config) + "\n"


def resource_pressure(broker, instances, streams, result: dict) -> None:
    """Measure the production Interface while A is busy and B reports liveness."""
    def console(instance):
        log = streams.logs[instance["uuid"]]
        return os.pread(log["fd"], log["bytes_saved"], 0).decode("utf-8", errors="replace")

    markers = ["HARNIQO-RESOURCE-START " + i["nonce"] for i in instances]
    deadline = time.monotonic() + 10
    while not all(marker in console(i) for marker, i in zip(markers, instances)):
        if time.monotonic() >= deadline:
            raise RuntimeError("Resource pressure fixture did not start")
        streams._poll(.1)
    before = [broker.resource_limits(i["uuid"]) for i in instances]
    channel = streams.report_channels.get(instances[1]["uuid"])
    if channel is None:
        raise RuntimeError("Control Guest report channel unavailable")
    challenges = []
    start = time.monotonic()
    while time.monotonic() - start < 6:
        if len(challenges) < 3 and time.monotonic() - start >= 2 * len(challenges):
            value = uuid.uuid4().hex + uuid.uuid4().hex[:8]
            channel.sendall((value + "\n").encode("ascii"))
            challenges.append(value)
        streams._poll(.1)
    challenge_elapsed = time.monotonic() - start
    after = [broker.resource_limits(i["uuid"]) for i in instances]
    identity = lambda item: (item["pid"], item["process_start_time"], item["cgroup_path"])
    result["checks"]["resource_pressure_identity_stable"] = all(
        identity(x) == identity(y) for x, y in zip(before, after))
    if not result["checks"]["resource_pressure_identity_stable"]:
        raise RuntimeError("QEMU identity changed across pressure samples")
    elapsed = after[0]["cpu_stat_monotonic"] - before[0]["cpu_stat_monotonic"]
    heartbeats = sum(("HARNIQO-HEARTBEAT " + instances[1]["nonce"] + " " + value)
                     in console(instances[1]) for value in challenges)
    if any("HARNIQO-RESOURCE-END " + i["nonce"] in console(i) for i in instances):
        raise RuntimeError("Resource fixture ended before measurement")
    # These samples come from the same production kernel observation used by
    # ensure/observe. The qualifier does not enforce or write any cgroup value.
    cpu_before, cpu_after = before[0]["cpu_stat"], after[0]["cpu_stat"]
    usage = cpu_after["usage_usec"] - cpu_before["usage_usec"]
    throttled = cpu_after["nr_throttled"] - cpu_before["nr_throttled"]
    throttled_usec = cpu_after["throttled_usec"] - cpu_before["throttled_usec"]
    result["resource_pressure"] = {"seconds": elapsed, "challenge_window_seconds": challenge_elapsed, "challenge_count": len(challenges), "before": before, "after": after,
        "a_cpu_usage_usec_delta": usage, "a_nr_throttled_delta": throttled,
        "a_throttled_usec_delta": throttled_usec, "b_heartbeats_delta": heartbeats}
    result["checks"]["qemu_cpu_throttled"] = (throttled > 0 and throttled_usec > 0
        and .5 * elapsed * 1e6 <= usage <= 1.2 * elapsed * 1e6)
    result["checks"]["b_heartbeat_during_pressure"] = len(challenges) == heartbeats == 3
    result["checks"]["host_limits_verified"] = True  # All four production reads succeeded.
    result["checks"]["normal_memory_within_cap"] = all(
        0 < sample["memory_current_bytes"] <= sample["memory_peak_bytes"] < sample["memory_max_bytes"]
        and sample["memory_events"]["oom"] == sample["memory_events"]["oom_kill"] == 0
        for sample in before + after)
    if not all(result["checks"][name] for name in
               ("qemu_cpu_throttled", "b_heartbeat_during_pressure", "host_limits_verified", "normal_memory_within_cap", "resource_pressure_identity_stable")):
        raise RuntimeError("Host resource or control liveness predicate failed")


def close_worker_logs(descriptors) -> list[dict]:
    """Log I/O failure must not bypass the later physical VM stop attempts."""
    errors = []
    for stream in descriptors:
        try:
            stream.flush(); os.fsync(stream.fileno())
        except Exception as error:
            errors.append({"phase": "worker_log_sync", "type": type(error).__name__})
        finally:
            try: stream.close()
            except Exception as error:
                errors.append({"phase": "worker_log_close", "type": type(error).__name__})
    return errors


def api():
    from apps.runner.resource_broker import (Template, ResourceBroker, sign_assignment,
                                            sign_grant, BrokerError)
    return Template, ResourceBroker, sign_assignment, sign_grant, BrokerError


def private_directory(path: Path, *, create: bool = False) -> None:
    kvm.no_symlinks(path)
    if create:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise RuntimeError("Private directory identity/permissions differ")


def runtime_directory(path: Path, *, create: bool = False) -> None:
    """Outer IPC traversal only; authority children remain strictly 0700."""
    kvm.no_symlinks(path)
    if create:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
        raise RuntimeError("Runtime directory identity differs")
    mode = stat.S_IMODE(info.st_mode)
    if mode == 0o700:
        return
    if mode != 0o710:
        raise RuntimeError("Runtime directory permissions differ")
    qemu_uid = pwd.getpwnam("libvirt-qemu").pw_uid
    if qemu_uid <= 0:
        raise RuntimeError("QEMU account must not be root")
    acl = kvm.run(["getfacl", "--absolute-names", "--numeric", "--omit-header", str(path)])
    entries = [line.split("#", 1)[0].strip() for line in acl.splitlines() if line.strip()]
    expected = {"user::rwx", f"user:{qemu_uid}:--x", "group::---", "mask::--x", "other::---"}
    if len(entries) != len(expected) or set(entries) != expected:
        raise RuntimeError("Runtime directory requires only named QEMU traverse access")


def _coordination_directory(path: Path, *, create: bool = False) -> None:
    """Legacy cooperative lock paths may be group-shared, never authority."""
    kvm.no_symlinks(path)
    if create:
        path.mkdir(mode=0o700, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o002:
        raise RuntimeError("Invalid coordination directory identity/permissions")


def _coordination_file(info) -> None:
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
            or info.st_nlink != 1 or info.st_mode & 0o002):
        raise RuntimeError("Invalid KVM coordination lock identity/permissions")


def verify_coordination_lock(root: Path, descriptor: int) -> None:
    parent = root / ".state/qualification"
    for directory in (root, root / ".state", parent):
        _coordination_directory(directory)
    path = parent / "kvm.lock"; kvm.no_symlinks(path)
    opened, current = os.fstat(descriptor), path.lstat()
    _coordination_file(opened); _coordination_file(current)
    if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
        raise RuntimeError("KVM coordination lock was replaced")


def coordination_lock(root: Path) -> int:
    """Acquire the old KVM lock without changing its directory or file mode.

    This only coordinates trusted operators. Signed authority and its lock live
    in the separately validated private ResourceBroker state directory.
    """
    _coordination_directory(root)
    _coordination_directory(root / ".state", create=True)
    parent = root / ".state/qualification"
    _coordination_directory(parent, create=True)
    path = parent / "kvm.lock"; kvm.no_symlinks(path)
    if path.exists():
        _coordination_file(path.lstat())
    directory_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    descriptor = None
    try:
        # Hold the checked directory during open. NONBLOCK also prevents a
        # concurrently substituted FIFO from hanging before fstat rejects it.
        directory_info = os.fstat(directory_fd); current = parent.lstat()
        if (directory_info.st_dev, directory_info.st_ino) != (current.st_dev, current.st_ino):
            raise RuntimeError("KVM coordination directory was replaced")
        descriptor = os.open("kvm.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                             0o600, dir_fd=directory_fd)
        _coordination_file(os.fstat(descriptor))
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        verify_coordination_lock(root, descriptor)
        return descriptor
    except BaseException:
        if descriptor is not None: os.close(descriptor)
        raise
    finally:
        os.close(directory_fd)


def private_bytes(path: Path, maximum: int) -> bytes:
    kvm.no_symlinks(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1
                or not 1 <= info.st_size <= maximum):
            raise RuntimeError("Private file identity/permissions differ")
        value = os.read(fd, maximum + 1)
        after = os.fstat(fd)
        current = path.lstat()
        if len(value) != info.st_size or ((info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
                != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)) or (
                (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino)):
            raise RuntimeError("Private file changed during read")
        return value
    finally:
        os.close(fd)


def public_source(path: Path) -> bytes:
    kvm.no_symlinks(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.geteuid()
                or before.st_nlink != 1 or before.st_mode & 0o022 or before.st_size > 4 * 1024**2):
            raise RuntimeError("Qualification source metadata differs")
        data = b""
        while len(data) <= 4 * 1024**2:
            part = os.read(fd, 65536)
            if not part: break
            data += part
        after = os.fstat(fd); current = path.lstat()
        if len(data) != before.st_size or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
                after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) or (
                current.st_dev, current.st_ino) != (before.st_dev, before.st_ino):
            raise RuntimeError("Qualification source changed during snapshot")
        data.decode("utf-8")
        if b"\0" in data: raise RuntimeError("Qualification source must be text")
        return data
    finally:
        os.close(fd)


def save(path: Path, value) -> None:
    kvm.write(path, json.dumps(value, indent=2) + "\n")



def freeze_qualification_limits(reports: Path, result: dict, *, console_mode=False, event_spool_mode=False, host_oom_mode=False) -> None:
    """Persist this fault experiment's budget before creating any VM resource."""
    limits = (host_oom if host_oom_mode else event_spool_pressure if event_spool_mode else console_pressure if console_mode else memory_pressure).qualification_limits()
    manifest = reports / 'qualification-limits.json'
    save(manifest, limits)
    result['qualification_limits'] = limits
    result['qualification_limits_manifest'] = manifest.name
    result['qualification_limits_sha256'] = kvm.sha256(manifest)

def ready(path: Path, value) -> None:
    # Publish a complete ready receipt to the reader, never a partially written
    # JSON file. The task directory is private and this role is its only writer.
    temporary = path.with_name(path.name + ".pending-" + uuid.uuid4().hex)
    save(temporary, value)
    if path.exists() or path.is_symlink():
        raise RuntimeError("Ready receipt already exists")
    os.rename(temporary, path)
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try: os.fsync(fd)
    finally: os.close(fd)


def broker_from(config: dict):
    Template, ResourceBroker, *_ = api()
    templates = [Template(**{k: Path(v) if k in {"disk", "seed", "channel", "console"} else v
                            for k, v in item.items()}) for item in config["templates"]]
    return ResourceBroker(state_dir=Path(config["state_dir"]), templates=templates,
                          issuer_key_file=Path(config["issuer_key_file"]),
                          images_root=Path(config["images_root"]), channel_root=Path(config["channel_root"]),
                          max_lease_seconds=300, max_clock_skew_seconds=5,
                          **({'initialize_state': False} if 'service' in config else {}))


def ipc_client(config):
    from apps.runner.resource_broker.ipc import BrokerClient
    service = config['service']
    return BrokerClient(directory=Path(service['ipc_directory']),
        server_uid=service['uid'], server_gid=service['gid'], timeout_seconds=30)


class QualificationIPC:
    """Four production wire operations; limits are trusted local diagnostics."""
    def __init__(self, config, diagnostic, calls):
        self.client, self.diagnostic, self.calls = ipc_client(config), diagnostic, calls

    def _call(self, operation, *arguments):
        observation = getattr(self.client, operation)(*arguments)
        self.calls[operation] = self.calls.get(operation, 0) + 1
        return observation

    def ensure(self, assignment): return self._call('ensure', assignment)
    def renew(self, grant): return self._call('renew', grant)
    def observe(self, identity): return self._call('observe', identity)
    def revoke(self, identity, cause='cancelled'): return self._call('revoke', identity, cause)
    def resource_limits(self, identity): return self.diagnostic.resource_limits(identity)


def service_configuration_root(path):
    if not isinstance(path, Path) or not path.is_absolute():
        raise RuntimeError('Service requires an explicit absolute private configuration root')
    private_directory(path)
    for parent in path.parents:
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid not in {0, os.getuid()} or info.st_mode & 0o022:
            raise RuntimeError('Service configuration ancestor is writable or foreign owned')
    info = path.lstat()
    return info.st_dev, info.st_ino


def prepare_service(config, reports, configuration_root, root_identity):
    """Freeze the actual CLI grammar around these two prepared fixed templates."""
    directory = reports / 'ipc'; private_directory(directory, create=True)
    value = {key: config[key] for key in ('state_dir', 'images_root', 'channel_root',
                                        'issuer_key_file', 'templates')}
    value.update(schema_version=1, broker_identity={'uid': os.getuid(), 'gid': os.getgid()},
        ipc_directory=str(directory), lease={'max_seconds': 300, 'max_clock_skew_seconds': 5},
        peers=[{'uid': os.getuid(), 'gid': os.getgid(), 'resources': [
            {key: claim[key] for key in ('resource_id', 'organization_id', 'workspace_id')}
            | {'operations': ['ensure', 'renew', 'observe', 'revoke']}
            for claim in config['assignments']]}])
    if service_configuration_root(configuration_root) != root_identity:
        raise RuntimeError('Service configuration root was replaced')
    child = configuration_root / reports.name
    child.mkdir(mode=0o700); private_directory(child)
    path = child / 'service-config.json'; save(path, value)
    evidence_copy = reports / 'service-config.json'; save(evidence_copy, value)
    if private_bytes(path, MAX_CONFIG_BYTES) != private_bytes(evidence_copy, MAX_CONFIG_BYTES):
        raise RuntimeError('Service configuration evidence copy differs')
    config['service'] = {'config': str(path), 'sha256': kvm.sha256(path),
        'evidence_dir': str(reports), 'evidence_config': str(evidence_copy),
        'configuration_root_identity': root_identity,
        'configuration_child_identity': (child.stat().st_dev, child.stat().st_ino),
        'ipc_directory': str(directory), 'uid': os.getuid(), 'gid': os.getgid()}
    return config['service']


def spawn_service(root, config, role, label, children, descriptors):
    """Register every owned CLI process/log before readiness or exit waits."""
    service = config['service']; reports = Path(service['evidence_dir'])
    files = []
    for suffix in ('.stdout', '.stderr'):
        stream = open(reports / (label + suffix), 'xb')
        descriptors.append(stream); files.append(stream); os.fchmod(stream.fileno(), 0o600)
    previous = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT, signal.SIGTERM, signal.SIGHUP})
    try:
        def unblock():
            signal.pthread_sigmask(signal.SIG_SETMASK, previous)
            resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
            resource.setrlimit(resource.RLIMIT_FSIZE, (2 * 1024**2, 2 * 1024**2))
        process = subprocess.Popen([sys.executable, '-m', SERVICE_MODULE, role,
            '--config', service['config'], '--sha256', service['sha256']], cwd=root,
            stdin=subprocess.DEVNULL, stdout=files[0], stderr=files[1],
            env={'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LC_ALL': 'C',
                 'PYTHONDONTWRITEBYTECODE': '1'}, preexec_fn=unblock)
        children.append(process)
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, previous)
    return process


def service_event(process, config, role, label, result, timeout=10):
    """Accept only the finite actual CLI event, then save its bound receipt."""
    service = config['service']; reports = Path(service['evidence_dir'])
    deadline = time.monotonic() + timeout
    path = reports / (label + '.stdout')
    while time.monotonic() < deadline:
        if path.stat().st_size:
            body = private_bytes(path, 4096)
            if body.endswith(b'\n'):
                event = json.loads(body)
                if role == 'initialize':
                    if event != {'schema_version': 1, 'event': 'initialized'}:
                        raise RuntimeError('Service initialization did not report initialized')
                    if process.wait(timeout=max(.001, deadline-time.monotonic())) != 0:
                        raise RuntimeError('Service initialization exited unsuccessfully')
                elif (set(event) != {'schema_version', 'event', 'role', 'pid', 'status'}
                        or type(event['schema_version']) is not int or event['schema_version'] != 1
                        or event['event'] != 'ready' or event['role'] != role
                        or type(event['pid']) is not int or event['pid'] != process.pid
                        or event['status'] != 'operational' or process.poll() is not None):
                    raise RuntimeError('Service operational readiness identity differs')
                digest = hashlib.sha256(private_bytes(Path(service['config']), MAX_CONFIG_BYTES)).hexdigest()
                if digest != service['sha256']:
                    raise RuntimeError('Service configuration changed before readiness')
                receipt = {'actual_cli_event': event, 'pid': process.pid,
                    'configuration_sha256': digest, 'sources_sha256': {
                        name: result['source_sha256'][name] for name in SERVICE_SOURCES}}
                ready(reports / (label + '-ready.json'), receipt)
                result.setdefault('service_process_receipts', []).append(receipt)
                return event
        if process.poll() is not None:
            raise RuntimeError('Service exited before its CLI event')
        time.sleep(.05)
    raise TimeoutError('Service CLI event deadline')


def stop_service(process, client, identity):
    """Observe actual normal CLI shutdown and a subsequent refused admission."""
    from apps.runner.resource_broker.ipc import IPCError, SOCKET_NAME
    if process.poll() is not None:
        raise RuntimeError('Service exited before injected SIGTERM')
    process.terminate()
    if process.wait(timeout=10) != 0:
        raise RuntimeError('Service did not exit successfully after SIGTERM')
    try:
        client.observe(identity)
    except IPCError as error:
        if error.code != 'ipc_unavailable' or error.outcome != 'not_dispatched':
            raise RuntimeError('Service admission refusal was not confirmed') from error
    else:
        raise RuntimeError('Service accepted a request after shutdown')
    if (client.directory / SOCKET_NAME).exists() or (client.directory / SOCKET_NAME).is_symlink():
        raise RuntimeError('Service socket remained after normal shutdown')
    return {'pid': process.pid, 'signal': 'SIGTERM', 'returncode': process.returncode,
            'admission_refused_before_dispatch': True, 'socket_removed': True}


def rejected(action, *, expected: str | None = None) -> dict:
    BrokerError = api()[-1]
    try:
        action()
    except BrokerError as error:
        code = getattr(error, "code", None)
        if expected is not None and code != expected:
            raise RuntimeError("Rejection did not prove the required gate: " + str(code)) from error
        return {"rejected": True, "code": code or type(error).__name__}
    raise RuntimeError("Broker accepted a forbidden operation")


def worker(config_path: Path, role: str, root: Path) -> int:
    if sys.platform != "linux" or os.geteuid() == 0 or sys.version_info[:2] != (3, 12):
        raise RuntimeError("Worker requires ordinary Linux/Python 3.12 operator")
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_FSIZE, (2 * 1024**2, 2 * 1024**2))
    root = root.absolute()
    kvm.no_symlinks(root); kvm.no_symlinks(config_path)
    if not config_path.is_relative_to(root / ".private/qualification"):
        raise RuntimeError("Worker config must be in this workspace private qualification directory")
    private_directory(config_path.parent)
    config = json.loads(private_bytes(config_path, MAX_CONFIG_BYTES))
    broker = None
    failure = None
    try:
        if 'service' in config:
            if role != 'launcher':
                raise RuntimeError('Service mode requires the actual service watch CLI')
            broker = ipc_client(config)
        else:
            broker = broker_from(config)
        key = private_bytes(Path(config["issuer_key_file"]), 32)
        _, _, sign_assignment, _, _ = api()
        stop = threading.Event()
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
        signal.signal(signal.SIGINT, lambda *_: stop.set())
        signal.signal(signal.SIGHUP, lambda *_: stop.set())
        if role == "launcher":
            a, b = config["assignments"]
            first = broker.ensure(sign_assignment(key, **a))
            collision = dict(b, organization_id=a["organization_id"], workspace_id=a["workspace_id"])
            gate = rejected(lambda: broker.ensure(sign_assignment(key, **collision)),
                            expected='ipc_scope_denied' if 'service' in config else 'workspace_busy')
            second = broker.ensure(sign_assignment(key, **b))
            if first.state != "running" or second.state != "running":
                raise RuntimeError("Launcher did not observe both fixed resources running")
            ready(config_path.parent / "launcher-ready.json", {
                "pid": os.getpid(), "observations": [asdict(first), asdict(second)],
                "workspace_conflict": gate, "ready": True})
            # This trusted qualification launcher is deliberately killed. It is not
            # the deadline authority; the independent Broker watch owns that role.
            stop.wait(300)
        else:
            ready(config_path.parent / "watch-ready.json", {"pid": os.getpid(), "ready": True})
            timer = threading.Timer(300, stop.set)
            timer.daemon = True; timer.start()
            try:
                broker.watch(interval_seconds=.25, stop_event=stop)
            finally:
                timer.cancel()
        return 0
    except BaseException as error:
        failure = error
        raise
    finally:
        diagnostic = getattr(failure, "configuration_error", None)
        if diagnostic is None and broker is not None:
            diagnostic = getattr(broker, "last_configuration_error", None)
        if diagnostic:
            allowed = {"phase", "catalog_index", "role", "cause_code", "failed_fields", "changed_fields"}
            record = {name: value for name, value in diagnostic.items() if name in allowed}
            try:
                save(config_path.parent / (role + "-configuration-error.json"), record)
            except Exception as error:
                print(json.dumps({"diagnostic_write_error": type(error).__name__}), file=sys.stderr)


def wait_ready(process, path: Path, timeout: float) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("Worker exited before readiness")
        if path.exists():
            result = json.loads(private_bytes(path, MAX_CONFIG_BYTES))
            if result.get("pid") != process.pid or result.get("ready") is not True:
                raise RuntimeError("Worker readiness identity differs")
            return result
        time.sleep(.1)
    raise TimeoutError("Worker readiness deadline")


def spawn_worker(root: Path, config: Path, role: str, children: list, descriptors: list):
    files = [open(config.parent / (role + suffix), "xb") for suffix in (".stdout", ".stderr")]
    for f in files: os.fchmod(f.fileno(), 0o600)
    descriptors.extend(files)
    previous = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT, signal.SIGTERM, signal.SIGHUP})
    try:
        def unblock():
            signal.pthread_sigmask(signal.SIG_SETMASK, previous)
            resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
            resource.setrlimit(resource.RLIMIT_FSIZE, (2 * 1024**2, 2 * 1024**2))
        process = subprocess.Popen([sys.executable, str(Path(__file__).absolute()), "--root", str(root),
                                    "--worker", role, "--config", str(config)],
                                   stdin=subprocess.DEVNULL, stdout=files[0], stderr=files[1],
                                   env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C",
                                        "PYTHONDONTWRITEBYTECODE": "1"}, preexec_fn=unblock)
        children.append(process)
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, previous)
    return process


def kill_worker(process) -> dict:
    if process.poll() is not None:
        raise RuntimeError("Worker was not alive before injected SIGKILL")
    process.kill(); status = process.wait(timeout=10)
    if status != -signal.SIGKILL:
        raise RuntimeError("Worker SIGKILL was not confirmed")
    return {"pid": process.pid, "signal": "SIGKILL", "confirmed_exit": True,
            "confirmed_exit_at": time.time()}


def claim(base: dict, generation: int, seconds: float) -> dict:
    instant = time.time()
    return dict(base, generation=generation, issued_at=instant, expires_at=instant + seconds)


def await_absent(resource_id: str, seconds: float, streams=None) -> None:
    """Only observe libvirt here: Broker.observe may itself enforce a deadline."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if resource_id not in kvm.domain_ids():
            return
        if streams is not None: streams._poll(.1)
        else: time.sleep(.1)
    raise TimeoutError("Host stop evidence deadline")


def stopped_receipt(broker, resource_id: str):
    observed = broker.observe(resource_id)
    if observed.state != "stopped" or observed.stop_evidence is None:
        raise RuntimeError("Host absence did not yield durable Broker StopEvidence")
    return observed


def service_live_revoke(broker, key, original_claim, first_stop, instances, streams,
                        extra_streams, work, console_logs, reports, watch, result):
    """A newly signed epoch proves that IPC revoke stops a live, same-UUID VM.

    The first epoch's Guest reports and deadline stop remain separate evidence.
    The second A console uses the existing bounded collector in its own round;
    observing console bytes is not a second Guest-health qualification.
    """
    instance, control = instances
    identity = instance['uuid']
    if (not result['checks'].get('a_watch_deadline_stop')
            or not result['checks'].get('independent_watch_survives_launcher')
            or first_stop.resource_id != identity or first_stop.state != 'stopped'
            or first_stop.stop_evidence is None
            or first_stop.stop_evidence.configuration_sha256 != original_claim['configuration_sha256']
            or first_stop.generation != original_claim['generation'] + 1
            or first_stop.recovery_generation != original_claim['recovery_generation']
            or identity in kvm.domain_ids()):
        raise RuntimeError('Live IPC revoke requires the verified original deadline stop')
    deadline = time.monotonic() + kvm.CONSOLE_DRAIN_SECONDS
    while not streams.logs[identity]['stream_closed']:
        if time.monotonic() >= deadline:
            raise TimeoutError('Original A console did not reach EOF before its next epoch')
        streams._poll(.1)
    # Keep B and the complete original A archive in their first-round owner.
    # The retained report channel is already unregistered by GuestStreams.
    for listener in ('listener', 'console_listener'):
        streams.selector.unregister(instance[listener])
    channel = streams.report_channels.pop(identity, None)
    if channel is not None:
        streams.connections.remove(channel); channel.close()
    restarted_streams = kvm.GuestStreams([instance], work, 2, console_logs)
    extra_streams.append(restarted_streams)
    restarted_streams.mark_start(instance)
    fresh = dict(claim(original_claim, first_stop.generation + 1, 30),
                 recovery_generation=first_stop.recovery_generation + 1, attempt_id=str(uuid.uuid4()))
    if fresh['attempt_id'] == original_claim['attempt_id']:
        raise RuntimeError('Live IPC revoke requires fresh explicit authorization')
    assignment = api()[2](key, **fresh)
    evidence = {'signed_new_assignment': asdict(assignment), 'first_epoch_stop': asdict(first_stop),
                'second_boot_guest_health_verified': False, 'same_fixed_resource_uuid': identity}
    result['service_live_revoke'] = evidence
    save(reports / 'service-live-revoke-intent.json', evidence)
    running = broker.ensure(assignment)
    evidence['new_running'] = asdict(running)
    if (running.resource_id != identity or running.state != 'running' or running.stop_evidence is not None
            or running.generation != assignment.generation
            or running.recovery_generation != assignment.recovery_generation):
        raise RuntimeError('Freshly authorized A was not observed running over IPC')
    # Preserve this boot's real console through EOF under the same archive
    # contract as all other boots; no duplicate first-round frames are accepted.
    deadline = time.monotonic() + 5
    while not restarted_streams.logs[identity]['bytes_seen']:
        if time.monotonic() >= deadline:
            raise TimeoutError('New A console did not produce bounded boot evidence')
        restarted_streams._poll(.1)
    sample = broker.resource_limits(identity)  # Explicit trusted local diagnostic.
    evidence['before_limits'] = sample
    evidence['before_time'] = time.time()
    before = kvm.domain_ids()
    if (identity not in before or control['uuid'] not in before or watch.poll() is not None
            or type(sample.get('pid')) is not int or sample['pid'] <= 1
            or type(sample.get('process_start_time')) is not int or sample['process_start_time'] <= 0
            or type(sample.get('cgroup_path')) is not str or not Path(sample['cgroup_path']).is_absolute()
            or not assignment.issued_at <= evidence['before_time'] < assignment.expires_at):
        raise RuntimeError('Live host QEMU identity before IPC revoke was not confirmed')
    evidence['host_uuid_present_before'] = True
    evidence['revoke_started_at'] = time.time()
    stopped = broker.revoke(identity, 'cancelled')
    evidence['revoke_after'] = asdict(stopped)
    stop = stopped.stop_evidence
    if (stopped.resource_id != identity or stopped.state != 'stopped' or stop is None
            or stopped.generation != assignment.generation
            or stopped.recovery_generation != assignment.recovery_generation
            or stopped.stop_cause != 'cancelled' or stop.resource_id != identity
            or stop.configuration_sha256 != assignment.configuration_sha256
            or stop.method != 'libvirt_destroy_then_absent'
            or not evidence['revoke_started_at'] <= stop.confirmed_at <= time.time()
            or stop.confirmed_at <= first_stop.stop_evidence.confirmed_at):
        raise RuntimeError('Live IPC revoke did not return fresh physical StopEvidence')
    await_absent(identity, 10, restarted_streams)
    evidence['host_uuid_absent_after'] = identity not in kvm.domain_ids()
    evidence['control_after'] = asdict(broker.observe(control['uuid']))
    if (not evidence['host_uuid_absent_after'] or watch.poll() is not None
            or control['uuid'] not in kvm.domain_ids() or evidence['control_after']['state'] != 'running'):
        raise RuntimeError('Live IPC revoke did not preserve the independent control VM')
    restarted_streams.finish()
    result['checks']['service_live_revoke_confirmed'] = True



def external_vm_stop_fault(broker, config, instance, streams, reports, result, assignment, grant):
    """Trusted, exact-owned VM fault injection, separate from Broker stopping."""
    import xml.etree.ElementTree as ET
    identity = instance['uuid']
    observed = broker.observe(identity)
    if observed.state != 'running' or observed.stop_evidence is not None:
        raise RuntimeError('External stop requires verified running owned resource')
    sample = broker.resource_limits(identity)
    xml = kvm.virsh('dumpxml', identity)
    tree = ET.fromstring(xml)
    expected = next(t for t in config['templates'] if t['resource_id'] == identity)
    ownership = tree.find('./metadata/{urn:harniqo:resource-broker:1}ownership')
    if (tree.findtext('uuid') != identity or tree.findtext('name') != expected['name']
            or ownership is None or not ownership.get('signature')):
        raise RuntimeError('External stop VM identity differs')
    if broker.observe(identity) != observed:
        raise RuntimeError('External stop ownership changed during fault preparation')
    # The production observation verifies ownership MAC, full template and limits.
    # Persist intent before this qualification-only external destroy operation.
    evidence = {'resource_id': identity, 'pre_observation': asdict(observed),
                'pre_limits': sample, 'domain_xml_sha256': hashlib.sha256(xml.encode()).hexdigest(),
                'domain_xml': xml, 'intent_at': time.time(), 'operation': 'qualification_external_virsh_destroy'}
    result['external_vm_stop'] = evidence
    save(reports / 'external-stop-intent.json', evidence)
    kvm.virsh('destroy', identity)
    evidence['command_acknowledged_at'] = time.time()
    await_absent(identity, 15, streams)
    stopped = stopped_receipt(broker, identity)
    evidence['observed'] = asdict(stopped)
    if (stopped.stop_evidence.method != 'libvirt_absent_after_confirmed_start'
            or stopped.stop_evidence.confirmed_at < evidence['intent_at']):
        raise RuntimeError('External termination absence evidence invalid')
    fresh = broker_from(config)
    rebuilt = stopped_receipt(fresh, identity)
    evidence['rebuilt_observation'] = asdict(rebuilt)
    if asdict(stopped) != asdict(rebuilt):
        raise RuntimeError('External termination tombstone changed across reconstruction')
    evidence['old_assignment'] = rejected(lambda: fresh.ensure(assignment), expected='generation_stale')
    evidence['old_grant'] = rejected(lambda: fresh.renew(grant), expected='resource_terminal')
    if identity in kvm.domain_ids() or not instance['overlay'].is_file():
        raise RuntimeError('External termination resurrected or lost disk')
    result['checks'].update(external_stop_absence_verified=True,
        external_stop_tombstone_rebuilt=True, external_stop_old_authorization_rejected=True)
    return fresh, stopped


def confirm_host_oom_stop(broker, config, instance, result, assignment, grant):
    """Persist observed physical absence after separate host-kernel OOM proof.

    This function never injects a stop: an observed disappearance is not
    interchangeable with the qualification's separate external-destroy fault.
    """
    identity = instance['uuid']
    cause = result.get('host_qemu_oom')
    if (not isinstance(cause, dict) or type(cause.get('trigger_at')) not in {int, float}
            or not math.isfinite(cause['trigger_at']) or cause['trigger_at'] <= 0
            or grant.resource_id != identity or not cause['trigger_at'] < grant.expires_at):
        raise RuntimeError('Verified host OOM trigger binding missing')
    observed = broker.observe(identity)
    stop = observed.stop_evidence
    if (observed.resource_id != identity or observed.state != 'stopped' or stop is None
            or observed.generation != grant.generation or observed.recovery_generation != grant.recovery_generation
            or stop.resource_id != identity
            or stop.configuration_sha256 != grant.configuration_sha256
            or stop.method != 'libvirt_absent_after_confirmed_start'
            or not cause['trigger_at'] <= stop.confirmed_at < grant.expires_at):
        raise RuntimeError('Host OOM did not yield the required physical absence receipt')
    fresh = broker_from(config)
    rebuilt = fresh.observe(identity)
    if asdict(rebuilt) != asdict(observed):
        raise RuntimeError('Host OOM stopped identity changed across reconstruction')
    result['host_qemu_oom_old_assignment'] = rejected(lambda: fresh.ensure(assignment), expected='generation_stale')
    result['host_qemu_oom_old_grant'] = rejected(lambda: fresh.renew(grant), expected='resource_terminal')
    if identity in kvm.domain_ids() or not instance['overlay'].is_file():
        raise RuntimeError('Host OOM stopped identity revived or lost its retained disk')
    result['observations']['a_host_qemu_oom_stop'] = asdict(observed)
    result['observations']['a_host_qemu_oom_stop_rebuilt'] = asdict(rebuilt)
    result['checks'].update(host_qemu_oom_stop_absence_confirmed=True,
        host_qemu_oom_stop_tombstone_rebuilt=True, host_qemu_oom_old_authorization_rejected=True)
    return fresh, observed


def stop_console_resource(broker, config, instance, result, assignment, grant):
    """A typed sink failure triggers the existing authenticated physical stop."""
    identity=instance['uuid']
    result['console_stop_intent_at']=time.time()
    stopped=broker.revoke(identity,'console_capacity_exceeded')
    if (stopped.resource_id!=identity or stopped.state!='stopped' or stopped.stop_evidence is None
            or stopped.stop_evidence.resource_id!=identity
            or stopped.stop_evidence.method!='libvirt_destroy_then_absent'
            or stopped.stop_cause!='console_capacity_exceeded'
            or stopped.stop_evidence.confirmed_at<result['console_stop_intent_at']):
        raise RuntimeError('Console failure did not produce a physical Broker stop')
    fresh=broker_from(config)
    rebuilt=stopped_receipt(fresh,identity)
    if asdict(stopped)!=asdict(rebuilt):
        raise RuntimeError('Console stop tombstone changed after reconstruction')
    result['console_old_assignment']=rejected(lambda:fresh.ensure(assignment),expected='generation_stale')
    result['console_old_grant']=rejected(lambda:fresh.renew(grant),expected='resource_terminal')
    if identity in kvm.domain_ids() or not instance['overlay'].is_file():
        raise RuntimeError('Console stop resurrected or lost the workspace disk')
    result['observations']['a_console_stop']=asdict(stopped)
    result['observations']['a_console_stop_rebuilt']=asdict(rebuilt)
    result['checks'].update(console_broker_stop_confirmed=True,
        console_stop_tombstone_rebuilt=True,console_old_authorization_rejected=True)
    return fresh,stopped


def stop_event_spool_resource(broker, config, instance, result, assignment, grant):
    """A native SQLITE_FULL stops exactly the authenticated affected execution."""
    identity = instance['uuid']
    result['event_spool_stop_intent_at'] = time.time()
    stopped = broker.revoke(identity, 'event_spool_capacity_exceeded')
    if (stopped.resource_id != identity or stopped.state != 'stopped' or stopped.stop_evidence is None
            or stopped.stop_evidence.resource_id != identity
            or stopped.stop_evidence.method != 'libvirt_destroy_then_absent'
            or stopped.stop_cause != 'event_spool_capacity_exceeded'
            or stopped.stop_evidence.confirmed_at < result['event_spool_stop_intent_at']):
        raise RuntimeError('Event spool failure did not produce a physical Broker stop')
    fresh = broker_from(config)
    rebuilt = stopped_receipt(fresh, identity)
    if asdict(stopped) != asdict(rebuilt):
        raise RuntimeError('Event spool stop tombstone changed after reconstruction')
    result['event_spool_old_assignment'] = rejected(lambda: fresh.ensure(assignment), expected='generation_stale')
    result['event_spool_old_grant'] = rejected(lambda: fresh.renew(grant), expected='resource_terminal')
    if identity in kvm.domain_ids() or not instance['overlay'].is_file():
        raise RuntimeError('Event spool stop resurrected or lost the workspace disk')
    result['observations']['a_event_spool_stop'] = asdict(stopped)
    result['observations']['a_event_spool_stop_rebuilt'] = asdict(rebuilt)
    result['checks'].update(event_spool_broker_stop_confirmed=True,
        event_spool_stop_tombstone_rebuilt=True, event_spool_old_authorization_rejected=True)
    return fresh, stopped

def inventory() -> dict:
    result = network.object_inventory()
    result["domain_xml"] = {identity: kvm.virsh("dumpxml", identity) for identity in result["domains"]}
    return result


def check_base(root: Path, base: Path) -> None:
    for p in (root, base, base.with_suffix(".provenance.json")): kvm.no_symlinks(p)
    if (not (root / "images/README.md").is_file() or not base.is_relative_to(root / "images/vm/base")
            or len(base.stem) != 64 or any(c not in "0123456789abcdef" for c in base.stem)
            or base.suffix != ".qcow2"):
        raise RuntimeError("Require authenticated workspace Canonical base")
    info = base.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1 or info.st_mode & 0o222:
        raise RuntimeError("Base must be owned, regular, single-link and immutable by mode")
    provenance = json.loads(base.with_suffix(".provenance.json").read_text())
    if provenance.get("sha256") != base.stem or provenance.get("signer_fingerprint") != kvm.SIGNER:
        raise RuntimeError("Canonical provenance differs")
    if kvm.sha256(base) != base.stem:
        raise RuntimeError("Canonical image digest differs")
    details = json.loads(kvm.run(["qemu-img", "info", "--output=json", str(base)]))
    if details.get("format") != "qcow2" or details.get("backing-filename") or not 0 < details.get("virtual-size", 0) <= DISK_BYTES:
        raise RuntimeError("Require standalone qcow2 fitting raw 8 GiB")


def execute(args) -> int:
    if (sys.platform != "linux" or platform.machine() not in {"x86_64", "amd64"}
            or os.geteuid() == 0 or sys.version_info[:2] != (3, 12) or not 30 <= args.timeout <= 150):
        raise RuntimeError("Require ordinary Linux x86_64/Python 3.12 operator and timeout 30..150")
    memory_mode = getattr(args, 'guest_memory_pressure', False)
    console_mode = getattr(args, 'console_pressure', False)
    event_spool_mode = getattr(args, 'event_spool_pressure', False)
    host_oom_mode = getattr(args, 'host_qemu_oom', False)
    service_mode = getattr(args, 'service_processes', False)
    if sum((memory_mode, console_mode, event_spool_mode, host_oom_mode, service_mode)) > 1:
        raise RuntimeError('Choose one fault or service qualification at a time' if service_mode
                           else 'Choose one fault qualification at a time')
    if service_mode and args.resource_profile != 'sealed-raw8g-q35-limited-v2':
        raise RuntimeError('Service process qualification requires fixed limited-v2 profile')
    service_config_root = getattr(args, 'service_config_root', None)
    if service_mode != (service_config_root is not None):
        raise RuntimeError('Service mode requires an explicit --service-config-root; other modes cannot use it')
    service_config_root_identity = service_configuration_root(service_config_root) if service_mode else None
    if memory_mode and args.resource_profile != 'sealed-raw8g-q35-limited-v2':
        raise RuntimeError('Guest memory pressure requires fixed limited-v2 profile')
    if console_mode and args.resource_profile != 'sealed-raw8g-q35-limited-v2':
        raise RuntimeError('Console pressure requires fixed limited-v2 profile')
    if event_spool_mode and args.resource_profile != 'sealed-raw8g-q35-limited-v2':
        raise RuntimeError('Event spool pressure requires fixed limited-v2 profile')
    if host_oom_mode and args.resource_profile != 'sealed-raw8g-q35-limited-v2':
        raise RuntimeError('Host QEMU OOM qualification requires limited-v2 control profile')
    spool_data_root = getattr(args, 'spool_data_root', None)
    if event_spool_mode != (spool_data_root is not None):
        raise RuntimeError('Event spool pressure requires an explicit private --spool-data-root; other modes cannot use it')
    os.umask(0o077)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    root = args.root.absolute(); base = args.base_image.absolute()
    for command in ("virsh", "qemu-img", "qemu-system-x86_64", "cloud-localds", "getfacl", "setfacl"):
        if not shutil.which(command): raise RuntimeError("Missing prerequisite: " + command)
    check_base(root, base)
    console_copies = 6 if service_mode else 4
    if shutil.disk_usage(root).free < 50 * 1024**3 + 2 * DISK_BYTES + console_copies*kvm.MAX_CONSOLE_BYTES:
        raise RuntimeError("Preserve 50 GiB, two fully allocated disks and source/archive console budgets")
    lock_fd = coordination_lock(root)
    parent = root / ".state/resource-broker-qualification"
    try:
        runtime_directory(parent, create=True)
    except BaseException:
        os.close(lock_fd)
        raise
    rid = uuid.uuid4().hex[:12]
    reports_parent = root / ".private/qualification"
    private_directory(root / ".private", create=True); private_directory(reports_parent, create=True)
    reports = reports_parent / ("resource-broker-" + rid); private_directory(reports, create=True)
    work = parent / ("rb-" + rid); private_directory(work, create=True)
    images = root / "images/vm/instances" / ("rb-" + rid); private_directory(images, create=True)
    directories = {p: (p.stat().st_dev, p.stat().st_ino) for p in (work, images)}
    ownership = {"qualification": "M0-05-service-cli-ipc-vm-lifecycle" if service_mode else "M0-05-host-qemu-memcg-oom-recovery" if host_oom_mode else "M0-05-local-event-spool-stop" if event_spool_mode else "M0-05-bounded-console-stop" if console_mode else "M0-05-guest-memory-external-stop" if memory_mode else "M0-05-resource-limits-lifecycle" if args.resource_profile == "sealed-raw8g-q35-limited-v2" else "M0-05-resource-broker-lifecycle", "run_id": rid}
    for p in (work, images): save(p / ".harniqo-m0-owner.json", ownership)
    baseline = inventory(); save(reports / "baseline.json", baseline)
    key_file = reports / "issuer-key"; key = os.urandom(32)
    fd = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try: os.write(fd, key); os.fsync(fd)
    finally: os.close(fd)
    config_path = reports / "config.json"
    instances, children, descriptors, console_logs = [], [], [], []
    spool_workers = []
    streams = None; broker = None; config = None
    service_calls = {}; service_server = None
    extra_streams = []
    acl = kvm.TemporaryACL("libvirt-qemu")
    result = {"schema_version": 1, "qualification": ownership["qualification"], "run_id": rid,
              "started_at": kvm.now(), "base_sha256": base.stem, "resource_profile": args.resource_profile,
              "host_hard_quotas_verified": False, "memory_exhaustion_verified": False,
              "guest_memory_exhaustion_verified": False, "host_qemu_oom_verified": False,
              "host_global_oom_verified": False,
              "external_vm_stop_recovery_verified": False, "checks": {}, "observations": {},
              "bounded_console_writer_verified": False, "console_failure_vm_stop_verified": False,
              "local_event_spool_verified": False, "event_spool_failure_vm_stop_verified": False,
              "control_plane_commit_verified": False, "generic_filesystem_quota_verified": False,
              "errors": [], "cleanup_errors": [], "milestone_complete": False,
              "dedicated_service_identity_verified": False, "production_egress_verified": False,
              "global_fencing_verified": False, "real_model_calls": 0}
    if service_mode:
        result.update(service_cli_verified=False, actual_vm_ipc_verified=False,
            dedicated_service_uid_verified=False, polkit_delegation_verified=False,
            service_processes_requested=True, service_ipc_successful_calls=service_calls,
            expected_vm_starts=3, unique_resource_uuids=2,
            sealed_guest_checks_scope='initial_epoch_of_each_fixed_resource_only',
            trusted_qualification_local_operations=['resource_limits',
                'post_watch_exit_reconstruction_and_reconcile', 'cleanup_after_service_process_exit'])
    try:
        if memory_mode or console_mode or event_spool_mode or host_oom_mode:
            freeze_qualification_limits(reports, result, console_mode=console_mode, event_spool_mode=event_spool_mode,
                                        host_oom_mode=host_oom_mode)
        if host_oom_mode:
            host_oom.preflight(root, reports, result)
        if event_spool_mode:
            data_root_identity = event_spool_pressure.prepare_data_root(spool_data_root)
        result["df_before"] = kvm.run(["df", "-h", str(root)])
        for label in ("a", "b"):
            resource_profile = args.resource_profile
            if host_oom_mode and label == 'a':
                from apps.runner.resource_broker import HOST_OOM_PROFILE
                resource_profile = HOST_OOM_PROFILE
            instances.append({"uuid": str(uuid.uuid4()), "name": "harniqo-rb-" + rid + "-" + label,
                              "nonce": uuid.uuid4().hex, "overlay": images / (label + ".raw"),
                              "seed": images / (label + "-seed.img"), "socket": work / (label + ".sock"),
                              "console_socket": work / (label + "-console.sock"), "template_id": "sealed-" + label,
                              "resource_profile": resource_profile})
            if event_spool_mode:
                instances[-1]['event_scope'] = event_spool_pressure.new_scope(instances[-1]['uuid'])
        if any(i["uuid"] in baseline["domains"] for i in instances): raise RuntimeError("UUID collision")
        result['resource_profiles'] = {i['template_id']: i['resource_profile'] for i in instances}
        save(reports / "ownership.json", {**ownership, "directories": {str(p): v for p, v in directories.items()},
                                          "instances": [{k: str(v) for k, v in i.items()} for i in instances]})
        sources = [Path(__file__).absolute(), REPO / "scripts/qualification/qualify_kvm.py",
                   REPO / "scripts/qualification/qualify_kvm_network.py",
                   REPO / "scripts/qualification/kvm_host_policy.py",
                   *sorted((REPO / "apps/runner/resource_broker").rglob("*.py"))]
        sources.append(REPO / 'scripts/qualification/guest_memory_pressure.py')
        sources += [REPO/'scripts/qualification/console_pressure.py',REPO/'apps/runner/console_store.py']
        if event_spool_mode:
            sources += [REPO / 'scripts/qualification/event_spool_pressure.py', REPO / event_spool_pressure.WORKER_RELATIVE,
                        REPO / 'apps/runner/event-spool/spool.ts', REPO / 'apps/runner/event-spool/types.ts',
                        REPO / 'apps/runner/event-spool/package.json', REPO / 'apps/runner/event-spool/package-lock.json']
        if host_oom_mode:
            sources.append(REPO / 'scripts/qualification/host_qemu_oom.py')
        snapshots = {p: public_source(p) for p in sources}
        result["source_sha256"] = {str(p.relative_to(REPO)): hashlib.sha256(data).hexdigest()
                                   for p, data in snapshots.items()}
        if service_mode and not set(SERVICE_SOURCES) <= result['source_sha256'].keys():
            raise RuntimeError('Service and IPC source snapshots required')
        source_folder = reports / "source"; source_folder.mkdir(mode=0o700)
        for p, data in snapshots.items():
            target = source_folder / p.relative_to(REPO); target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            kvm.write(target, data.decode("utf-8"))
        if event_spool_mode:
            event_spool_pressure.prepare_workers(root, reports, instances, spool_workers, result, data_root_identity)
        for i, other in ((instances[0], instances[1]), (instances[1], instances[0])):
            kvm.run(["qemu-img", "convert", "-f", "qcow2", "-O", "raw", str(base), str(i["overlay"])], timeout=180)
            kvm.run(["qemu-img", "resize", "-f", "raw", str(i["overlay"]), str(DISK_BYTES)])
            i["overlay"].chmod(0o600)
            disk_fd = os.open(i["overlay"], os.O_RDWR | os.O_NOFOLLOW)
            try:
                os.posix_fallocate(disk_fd, 0, DISK_BYTES); os.fsync(disk_fd)
                metadata = os.fstat(disk_fd)
                if metadata.st_size != DISK_BYTES or metadata.st_blocks * 512 < DISK_BYTES:
                    raise RuntimeError("Raw disk is not fully allocated")
                result.setdefault("raw_disks", {})[i["uuid"]] = {
                    "device": metadata.st_dev, "inode": metadata.st_ino,
                    "bytes": metadata.st_size, "allocated_bytes": metadata.st_blocks * 512}
            finally: os.close(disk_fd)
            user, meta, net = [work / (i["template_id"] + "-" + n) for n in ("user", "meta", "network")]
            kvm.write(user, sealed_guest_config(i, other, root, resource_profile=i['resource_profile'], pressure=i is instances[0], guest_memory_pressure=memory_mode, console_pressure_mode=console_mode, event_spool_pressure_mode=event_spool_mode, host_oom_mode=host_oom_mode))
            save(meta, {"instance-id": i["uuid"], "local-hostname": i["name"]})
            save(net, {"version": 2, "ethernets": {}})
            kvm.run(["cloud-localds", "--network-config", str(net), str(i["seed"]), str(user), str(meta)])
            i["seed"].chmod(0o600)
            i["listener"] = kvm.listener_at(i["socket"])
            i["console_listener"] = kvm.listener_at(i["console_socket"])
            for path, permissions in ((i["overlay"], "rw-"), (i["seed"], "r--"),
                                      (i["socket"], "rw-"), (i["console_socket"], "rw-")):
                acl.traverse(path); acl.grant(path, permissions, restore=False)
            from apps.runner.resource_broker import prepare_seed_access
            prepare_seed_access(i["seed"], images_root=images)
        templates = [{"template_id": i["template_id"], "resource_id": i["uuid"], "name": i["name"],
                      "disk": str(i["overlay"]), "seed": str(i["seed"]),
                      "channel": str(i["socket"]), "console": str(i["console_socket"]),
                      "source_base_sha256": base.stem, "resource_profile": i['resource_profile']} for i in instances]
        Template = api()[0]
        template_hashes = {item["template_id"]: Template(**{
            k: Path(v) if k in {"disk", "seed", "channel", "console"} else v
            for k, v in item.items()}).configuration_sha256 for item in templates}
        instant = time.time()
        claims = [{"resource_id": i["uuid"], "organization_id": i['event_scope']['organization_id'] if event_spool_mode else str(uuid.uuid4()),
                   "workspace_id": str(uuid.uuid4()), "attempt_id": i['event_scope']['attempt_id'] if event_spool_mode else str(uuid.uuid4()),
                   "template_id": i["template_id"], "configuration_sha256": template_hashes[i["template_id"]],
                   "generation": 1, "recovery_generation": 1,
                   "issued_at": instant, "expires_at": instant + 240} for i in instances]
        config = {"templates": templates, "assignments": claims, "state_dir": str(work / "broker"),
                  "issuer_key_file": str(key_file), "images_root": str(images), "channel_root": str(work)}
        if service_mode:
            result['service_configuration_sha256'] = prepare_service(config, reports,
                service_config_root, service_config_root_identity)['sha256']
            result['retained_private_service_configuration'] = config['service']
        save(config_path, config)
        verify_coordination_lock(root, lock_fd)
        if service_mode:
            initialized = spawn_service(root, config, 'initialize', 'service-initialize', children, descriptors)
            service_event(initialized, config, 'initialize', 'service-initialize', result)
            result['checks']['service_initialized_explicitly'] = True
            diagnostic = broker_from(config)
            watch = spawn_service(root, config, 'watch', 'service-watch', children, descriptors)
            service_event(watch, config, 'watch', 'service-watch', result)
            service_server = spawn_service(root, config, 'serve', 'service-serve', children, descriptors)
            service_event(service_server, config, 'serve', 'service-serve', result)
            result['checks']['service_operational_readiness'] = True
            broker = QualificationIPC(config, diagnostic, service_calls)
        else:
            broker = broker_from(config)
            watch = spawn_worker(root, config_path, "watch", children, descriptors)
            wait_ready(watch, reports / "watch-ready.json", 10)
        streams = kvm.GuestStreams(instances, work, 1, console_logs,
                                   retain_report_channels=args.resource_profile == "sealed-raw8g-q35-limited-v2")
        for i in instances: streams.mark_start(i)
        launcher = spawn_worker(root, config_path, "launcher", children, descriptors)
        readiness = wait_ready(launcher, reports / "launcher-ready.json", min(args.timeout, 90))
        result["checks"]['ipc_scope_denied' if service_mode else 'workspace_busy'] = (
            readiness['workspace_conflict']['rejected'] and readiness['workspace_conflict']['code'] ==
            ('ipc_scope_denied' if service_mode else 'workspace_busy'))
        _, _, sign_assignment, sign_grant, _ = api()
        assignments = [sign_assignment(key, **c) for c in claims]
        result["observations"]["repeat_ensure"] = [asdict(broker.ensure(a)) for a in assignments]
        result["checks"]["repeat_ensure_same_uuids"] = all(o["resource_id"] == c["resource_id"] and o["state"] == "running"
            for o, c in zip(result["observations"]["repeat_ensure"], claims))
        result["checks"]["only_two_owned_domains"] = kvm.domain_ids() == set(baseline["domains"]) | {i["uuid"] for i in instances}
        qemu_uid = pwd.getpwnam("libvirt-qemu").pw_uid
        owner_classes = {os.geteuid(): "broker", 0: "root", qemu_uid: "qemu"}
        result["runtime_owner_classes"] = [
            {role: owner_classes.get(Path(item[field]).lstat().st_uid, "other")
             for role, field in (("disk", "overlay"), ("seed", "seed"),
                                 ("channel", "socket"), ("console", "console_socket"))}
            for item in instances]
        result["checks"]["runtime_qemu_socket_observed"] = any(
            owners[role] == "qemu" for owners in result["runtime_owner_classes"]
            for role in ("channel", "console"))
        result["wrong_signature"] = rejected(lambda: broker.ensure(replace(assignments[0], signature="0" * 64)), expected="signature_invalid")
        received = streams.collect(args.timeout); kvm.validate_round(received, instances, 1, None)
        result["guests"] = received
        result["checks"]["guest_checks"] = len(received) == 2 and all(len(x["checks"]) == 11 and all(x["checks"].values()) for x in received.values())
        if watch.poll() is not None:
            raise RuntimeError("Independent watch exited during Guest boot")
        if service_mode:
            before_restart = [asdict(broker.observe(i['uuid'])) for i in instances]
            owned = {i['uuid'] for i in instances}
            if not owned <= kvm.domain_ids() or any(o['state'] != 'running' for o in before_restart):
                raise RuntimeError('Service restart requires both original VMs live')
            result['service_server_kill'] = kill_worker(service_server)
            result['checks']['service_server_killed_with_both_vms_live'] = owned <= kvm.domain_ids()
            if watch.poll() is not None:
                raise RuntimeError('Service watch exited when server was killed')
            service_server = spawn_service(root, config, 'serve', 'service-serve-restarted', children, descriptors)
            service_event(service_server, config, 'serve', 'service-serve-restarted', result)
            broker = QualificationIPC(config, diagnostic, service_calls)
            after_restart = [asdict(broker.observe(i['uuid'])) for i in instances]
            result['service_restart_observations'] = {'before': before_restart, 'after': after_restart}
            result['checks']['service_restart_preserves_vm_identities'] = before_restart == after_restart
            result['checks']['service_watch_survives_server_restart'] = watch.poll() is None
            if not all(result['checks'][name] for name in (
                    'service_server_killed_with_both_vms_live', 'service_restart_preserves_vm_identities',
                    'service_watch_survives_server_restart')):
                raise RuntimeError('Service restart did not preserve independent live resources')
        if args.resource_profile == "sealed-raw8g-q35-limited-v2":
            if memory_mode: memory_pressure.run_pressure(broker, instances, streams, result)
            elif not console_mode and not event_spool_mode and not host_oom_mode: resource_pressure(broker, instances, streams, result)
        a2, b2 = claim(claims[0], 2, 120 if memory_mode or console_mode or event_spool_mode or host_oom_mode else 8), claim(claims[1], 2, 120)
        result["observations"]["renewed"] = [asdict(broker.renew(sign_grant(key, **c))) for c in (a2, b2)]
        stale = claim(claims[0], 1, 60)
        result["stale_generation"] = rejected(lambda: broker.renew(sign_grant(key, **stale)), expected="generation_stale")
        result["launcher_kill"] = kill_worker(launcher)
        result["checks"]["a_live_after_launcher_exit"] = instances[0]["uuid"] in kvm.domain_ids()
        result["a_live_after_launcher_exit_at"] = time.time()
        if not result["checks"]["a_live_after_launcher_exit"]:
            raise RuntimeError("A was not live after confirmed launcher exit")
        if host_oom_mode:
            host_oom.collect_pressure(broker, instances, streams, result)
            broker, stopped_a = confirm_host_oom_stop(broker, config, instances[0], result,
                assignments[0], sign_grant(key, **a2))
            host_oom.after_stop(instances, streams, result)
        elif event_spool_mode:
            event_spool_pressure.run_pressure(broker, instances, streams, result, spool_workers)
            broker, stopped_a = stop_event_spool_resource(broker, config, instances[0], result,
                assignments[0], sign_grant(key, **a2))
            event_spool_pressure.after_stop(instances, streams, result, spool_workers)
        elif console_mode:
            console_pressure.run_pressure(broker,instances,streams,result)
            broker,stopped_a=stop_console_resource(broker,config,instances[0],result,assignments[0],sign_grant(key,**a2))
            result['console_pressure']['b_after_console_stop_challenge']=memory_pressure.challenge(streams,instances[1])
            result['checks']['b_healthy_after_console_stop']=True
        elif memory_mode:
            broker, stopped_a = external_vm_stop_fault(broker, config, instances[0], streams, reports,
                result, assignments[0], sign_grant(key, **a2))
            result["guest_memory_pressure"]["b_after_external_stop_challenge"] = memory_pressure.challenge(streams, instances[1])
            result["checks"]["b_healthy_after_external_stop"] = True
        else:
            await_absent(instances[0]["uuid"], 30, streams)
            result["a_absent_before_observe_at"] = time.time()
            stopped_a = stopped_receipt(broker, instances[0]["uuid"])
            result["observations"]["a_after_launcher_death"] = asdict(stopped_a)
            result["checks"]["a_watch_deadline_stop"] = (stopped_a.stop_evidence.method == "libvirt_destroy_then_absent"
                and stopped_a.stop_evidence.confirmed_at >= a2["expires_at"] - .25)
        result["observations"]["b_survivor"] = asdict(broker.observe(instances[1]["uuid"]))
        result["checks"]["independent_watch_survives_launcher"] = watch.poll() is None and instances[1]["uuid"] in kvm.domain_ids() and result["observations"]["b_survivor"]["state"] == "running"
        if service_mode:
            service_live_revoke(broker, key, claims[0], stopped_a, instances, streams,
                extra_streams, work, console_logs, reports, watch, result)
        b3 = claim(claims[1], 3, 5)
        broker.renew(sign_grant(key, **b3))
        if service_mode:
            result['service_sigterm_with_live_b'] = stop_service(service_server, broker.client, instances[1]['uuid'])
            result['checks']['service_sigterm_closes_admission'] = True
            result['checks']['service_sigterm_preserves_live_vm'] = (
                watch.poll() is None and instances[1]['uuid'] in kvm.domain_ids())
            if not result['checks']['service_sigterm_preserves_live_vm']:
                raise RuntimeError('Service shutdown did not preserve independently watched live B')
        result["watch_kill"] = kill_worker(watch)
        while time.time() <= b3["expires_at"] + 1: streams._poll(.2)
        if instances[1]["uuid"] not in kvm.domain_ids():
            raise RuntimeError("B was not live before restarted Broker recovery")
        result["b_live_before_broker_restart_at"] = time.time()
        recovery = broker_from(config)
        if not service_mode:
            broker = recovery
        result["reconcile"] = [asdict(o) for o in recovery.reconcile()]
        await_absent(instances[1]["uuid"], 15, streams)
        if service_mode:
            service_server = spawn_service(root, config, 'serve', 'service-serve-recovered', children, descriptors)
            service_event(service_server, config, 'serve', 'service-serve-recovered', result)
            broker = QualificationIPC(config, recovery, service_calls)
        else:
            broker = recovery
        stopped_b = stopped_receipt(broker, instances[1]["uuid"])
        result["observations"]["b_after_broker_restart"] = asdict(stopped_b)
        result["checks"]["b_restart_deadline_stop"] = (stopped_b.stop_evidence.method == "libvirt_destroy_then_absent"
            and stopped_b.stop_evidence.confirmed_at >= b3["expires_at"])
        result["expired_renew"] = rejected(lambda: broker.renew(sign_grant(key, **b3)), expected="lease_expired")
        result["stopped_old_assignment"] = rejected(lambda: broker.ensure(assignments[1]), expected="generation_stale")
        result["checks"]["no_revival_or_replacement"] = kvm.domain_ids() == set(baseline["domains"])
        result["checks"]["disks_retained_until_host_stop"] = all(i["overlay"].is_file() and i["overlay"].stat().st_size == DISK_BYTES for i in instances)
        streams.finish()
        if service_mode:
            result['service_ipc_final_revoke'] = [asdict(broker.revoke(i['uuid'])) for i in instances]
            result['checks']['four_operations_over_ipc'] = set(service_calls) == {'ensure', 'renew', 'observe', 'revoke'}
            result['service_final_sigterm'] = stop_service(service_server, broker.client, instances[1]['uuid'])
    except BaseException as error:
        result["errors"].append({"type": type(error).__name__, "message": str(error)})
    finally:
        for process in children:
            try:
                if process.poll() is None: process.terminate(); process.wait(timeout=10)
            except Exception as error:
                result["cleanup_errors"].append({"phase": "worker_stop", "type": type(error).__name__})
        result["cleanup_errors"].extend(close_worker_logs(descriptors))
        stopped = False; console_ok = True
        workers_stopped = all(process.poll() is not None for process in children)
        result["workers_confirmed_exited"] = workers_stopped
        try:
            if (event_spool_mode or host_oom_mode) and broker is None and config is None and not children:
                # Fault preflight precedes raw-disk allocation and all Broker
                # submissions. Preserve the failed cohort without attempting
                # to revoke resources that were never registered. This is
                # deliberately not a physical-stop receipt.
                result['event_spool_preflight_failed_before_broker_submission' if event_spool_mode
                       else 'host_qemu_oom_preflight_failed_before_broker_submission'] = True
                if any(i['uuid'] in kvm.domain_ids() for i in instances):
                    raise RuntimeError('Unexpected resource exists before Broker submission')
                result['unsubmitted_resources_absent'] = True
            else:
                if service_mode and config is not None:
                    # Trusted operator recovery is deliberately outside the
                    # four-operation service protocol and survives CLI failure.
                    broker = broker_from(config)
                if broker is None and config is not None: broker = broker_from(config)
                if broker is None:
                    raise RuntimeError("No Broker stop evidence; preserve files and ACL")
                revocations = []
                for i in instances:
                    try:
                        observed = broker.revoke(i["uuid"], "qualification_cleanup")
                        revocations.append(asdict(observed))
                    except Exception as error:
                        result["cleanup_errors"].append({"phase": "resource_revoke", "resource_id": i["uuid"],
                                                         "type": type(error).__name__, "message": str(error)})
                result["final_revoke"] = revocations
                stopped = (workers_stopped and len(revocations) == len(instances)
                           and all(o["state"] == "stopped" and o["stop_evidence"] is not None for o in revocations)
                           and not any(i["uuid"] in kvm.domain_ids() for i in instances))
                if not stopped:
                    raise RuntimeError("Stop evidence or creator exit uncertain; preserve files and ACL")
        except Exception as error:
            result["cleanup_errors"].append({"phase": "host_stop", "type": type(error).__name__, "message": str(error)})
        # Stop state is independent of the queue. A spool pipe/log/close error
        # cannot bypass the physical resource revocations above.
        for spool_worker in spool_workers:
            try: result['cleanup_errors'].extend(spool_worker.close())
            except Exception as error:
                result['cleanup_errors'].append({'phase': 'event_spool_worker_cleanup', 'type': type(error).__name__})
        if event_spool_mode:
            result['event_spool_workers_confirmed_exited'] = (
                all(w.process is not None and w.process.poll() is not None for w in spool_workers))
            if not result['event_spool_workers_confirmed_exited']:
                result['cleanup_errors'].append({'phase': 'event_spool_worker_exit', 'message': 'Native spool worker exit unconfirmed'})
            if result.get('event_spool_data') is not None:
                try: event_spool_pressure.retained_data(result, spool_workers)
                except Exception as error:
                    result['cleanup_errors'].append({'phase': 'event_spool_data_preservation', 'type': type(error).__name__})
        if streams is not None and stopped:
            try: streams.finish()
            except Exception as error:
                console_ok = False; result["cleanup_errors"].append({"phase": "console_drain", "type": type(error).__name__})
        for extra in extra_streams:
            if stopped:
                try: extra.finish()
                except Exception as error:
                    console_ok = False
                    result['cleanup_errors'].append({'phase': 'service_epoch_console_drain', 'type': type(error).__name__})
        for i in instances:
            for name in ("listener", "console_listener"):
                if name in i: i[name].close()
        for log in console_logs:
            try: kvm.save_console(log, reports)
            except Exception as error:
                console_ok = False; result["cleanup_errors"].append({"phase": "console_save", "type": type(error).__name__})
        result["console_logs"] = [{k: str(v) if isinstance(v, Path) else v for k, v in log.items() if k not in {"fd", "inode"}} for log in console_logs]
        if console_logs:
            result['checks']['console_archives_explicit']=console_pressure.validate_archives(console_logs,console_mode)
            if console_mode and result.get('console_pressure'):
                a_log=next(log for log in console_logs if log['instance_uuid']==instances[0]['uuid'])
                result['checks']['console_retained_archive_matches']=(
                    a_log.get('archive_sha256')==result['console_pressure']['a_retained_prefix_sha256'])
                result['checks']['console_archives_explicit'] &= result['checks']['console_retained_archive_matches']
            if not result['checks']['console_archives_explicit']:
                console_ok=False
                result['errors'].append({'phase':'console_archive','message':'Unexpected incomplete console evidence'})
        if stopped:
            result["cleanup_errors"].extend({"phase": "acl_restore", "message": e} for e in acl.restore())
        else: result["acl_preserved_for_recovery"] = True
        try:
            verify_coordination_lock(root, lock_fd)
            result["coordination_lock_unchanged"] = True
            result["baseline_unchanged"] = inventory() == baseline
            result["base_unchanged"] = kvm.sha256(base) == base.stem
            result["sources_unchanged"] = all(kvm.sha256(REPO / name) == digest
                                               for name, digest in result.get("source_sha256", {}).items())
            if not result["baseline_unchanged"] or not result["base_unchanged"] or not result["sources_unchanged"]:
                raise RuntimeError("Preexisting libvirt objects or base changed")
            if stopped and console_ok and not result["cleanup_errors"]:
                state = work / "broker"
                if not state.is_dir():
                    raise RuntimeError("Broker journal archive missing; preserve files")
                for entry in state.rglob("*"): kvm.no_symlinks(entry)
                shutil.copytree(state, reports / "broker-journal", symlinks=False)
                for directory in (images, work):
                    kvm.no_symlinks(directory)
                    if (directory.stat().st_dev, directory.stat().st_ino) != directories[directory] or json.loads((directory / ".harniqo-m0-owner.json").read_text()) != ownership:
                        raise RuntimeError("Owned directory identity changed; preserve files")
                    shutil.rmtree(directory)
                result["temporary_files_removed"] = True
            else: result["temporary_files_preserved"] = True
            result["df_after"] = kvm.run(["df", "-h", str(root)])
        except Exception as error:
            result["cleanup_errors"].append({"phase": "final_verification", "type": type(error).__name__, "message": str(error)})
        result["finished_at"] = kvm.now()
        expected = {"workspace_busy", "repeat_ensure_same_uuids", "only_two_owned_domains", "guest_checks", "runtime_qemu_socket_observed",
                    "independent_watch_survives_launcher", "no_revival_or_replacement", "disks_retained_until_host_stop",
                    "a_live_after_launcher_exit", "a_watch_deadline_stop", "b_restart_deadline_stop"}
        if service_mode:
            expected.remove('workspace_busy')
            expected |= SERVICE_CHECKS
            try:
                service = config['service']
                original = Path(service['config'])
                private_directory(original.parent)
                original_info = original.parent.stat()
                original_bytes = private_bytes(original, MAX_CONFIG_BYTES)
                result['checks']['service_configuration_frozen'] = (
                    service_configuration_root(service_config_root) == service_config_root_identity
                    and (original_info.st_dev, original_info.st_ino) == service['configuration_child_identity']
                    and hashlib.sha256(original_bytes).hexdigest() == result['service_configuration_sha256']
                    and original_bytes == private_bytes(Path(service['evidence_config']), MAX_CONFIG_BYTES))
            except (OSError, TypeError, KeyError, RuntimeError):
                result['checks']['service_configuration_frozen'] = False
        if host_oom_mode:
            expected.remove('a_watch_deadline_stop')
            expected |= host_oom.REQUIRED_CHECKS | {'qualification_limits_frozen', 'console_archives_explicit',
                'host_qemu_oom_stop_absence_confirmed', 'host_qemu_oom_stop_tombstone_rebuilt',
                'host_qemu_oom_old_authorization_rejected'}
        elif event_spool_mode:
            expected.remove('a_watch_deadline_stop')
            expected |= event_spool_pressure.REQUIRED_CHECKS | {'qualification_limits_frozen',
                'event_spool_configuration_frozen', 'console_archives_explicit'}
            try:
                result['checks']['event_spool_configuration_frozen'] = event_spool_pressure.verify_configuration(root, reports, result)
            except (OSError, ValueError, RuntimeError, KeyError):
                result['checks']['event_spool_configuration_frozen'] = False
        elif console_mode:
            expected.remove('a_watch_deadline_stop')
            expected |= console_pressure.REQUIRED_CHECKS | {'qualification_limits_frozen','console_archives_explicit','console_retained_archive_matches'}
        elif memory_mode:
            expected.remove('a_watch_deadline_stop')
            expected |= memory_pressure.REQUIRED_CHECKS | {'external_stop_absence_verified',
                'external_stop_tombstone_rebuilt', 'external_stop_old_authorization_rejected',
                'b_healthy_after_external_stop'}
        elif args.resource_profile == "sealed-raw8g-q35-limited-v2":
            expected |= {"qemu_cpu_throttled", "b_heartbeat_during_pressure", "host_limits_verified", "normal_memory_within_cap", "resource_pressure_identity_stable"}
        if memory_mode or console_mode or event_spool_mode or host_oom_mode:
            expected.add('qualification_limits_frozen')
            try:
                manifest = reports / 'qualification-limits.json'
                frozen = (result.get('qualification_limits') == (host_oom if host_oom_mode else event_spool_pressure if event_spool_mode else console_pressure if console_mode else memory_pressure).qualification_limits()
                    and json.loads(manifest.read_text()) == result['qualification_limits']
                    and kvm.sha256(manifest) == result.get('qualification_limits_sha256'))
            except (OSError, ValueError):
                frozen = False
            result['checks']['qualification_limits_frozen'] = frozen
            if not frozen:
                result['errors'].append({'phase':'qualification_limits','message':'Frozen qualification budget missing or changed'})
        result["passed"] = not result["errors"] and not result["cleanup_errors"] and all(result["checks"].get(c) is True for c in expected)
        result["host_cpu_memory_limits_verified"] = result["passed"] and args.resource_profile == "sealed-raw8g-q35-limited-v2"
        result['guest_memory_exhaustion_verified'] = result['passed'] and memory_mode
        result['external_vm_stop_recovery_verified'] = result['passed'] and memory_mode
        result['host_qemu_oom_verified'] = result['passed'] and host_oom_mode
        result['bounded_console_writer_verified'] = result['passed'] and console_mode
        result['console_failure_vm_stop_verified'] = result['passed'] and console_mode
        result['local_event_spool_verified'] = result['passed'] and event_spool_mode
        result['event_spool_failure_vm_stop_verified'] = result['passed'] and event_spool_mode
        if service_mode:
            result['service_cli_verified'] = result['passed']
            result['actual_vm_ipc_verified'] = result['passed']
        save(reports / "report.json", result)
        print(json.dumps({"qualification": result["qualification"], "passed": result["passed"],
                          "milestone_complete": False, "errors": len(result["errors"]),
                          "cleanup_errors": len(result["cleanup_errors"]),
                          "report": str((reports / "report.json").relative_to(root))}), flush=True)
        os.close(lock_fd)
    return 0 if result["passed"] else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--base-image", type=Path)
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--resource-profile", choices=("sealed-raw8g-q35-v1", "sealed-raw8g-q35-limited-v2"), default="sealed-raw8g-q35-v1")
    parser.add_argument("--guest-memory-pressure", action="store_true", help="Explicit limited-v2 Guest OOM and external-stop fault qualification")
    parser.add_argument("--console-pressure", action="store_true", help="Explicit limited-v2 bounded-console overflow and physical-stop qualification")
    parser.add_argument("--event-spool-pressure", action="store_true", help="Explicit limited-v2 local SQLite spool FULL and physical-stop qualification")
    parser.add_argument("--host-qemu-oom", action="store_true", help="Explicit A-only host-memcg OOM fault profile with an unchanged limited-v2 control")
    parser.add_argument('--service-processes', action='store_true', help='Explicit limited-v2 real service CLI, IPC and KVM lifecycle qualification')
    parser.add_argument('--service-config-root', type=Path, help='Existing owned 0700 directory with trusted ancestors; required only with --service-processes')
    parser.add_argument("--spool-data-root", type=Path, help="Operator-owned 0700 data directory with safe ancestors; required only with --event-spool-pressure")
    parser.add_argument("--worker", choices=("launcher", "watch"))
    parser.add_argument("--config", type=Path)
    args = parser.parse_args()
    if args.worker:
        if args.config is None or args.base_image is not None or args.guest_memory_pressure or args.console_pressure or args.event_spool_pressure or args.host_qemu_oom or args.service_processes or args.service_config_root is not None or args.spool_data_root is not None: parser.error("Worker requires only --config")
        return worker(args.config.absolute(), args.worker, args.root)
    if args.base_image is None or args.config is not None: parser.error("Qualification requires --base-image")
    def terminate(signum, _frame): raise KeyboardInterrupt("Qualification interrupted by signal " + str(signum))
    for signum in (signal.SIGTERM, signal.SIGHUP): signal.signal(signum, terminate)
    return execute(args)


if __name__ == "__main__":
    raise SystemExit(main())
