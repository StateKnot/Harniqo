#!/usr/bin/env python3
"""Qualify two sealed KVM guests without touching existing domains or networks.

Requires an ordinary local user authorized for qemu:///system, qemu-img,
cloud-localds, getfacl/setfacl and an authenticated Ubuntu cloud-image receipt.
This is an M0 sealed/no-NIC baseline, not full production sandbox acceptance.
"""
from __future__ import annotations

import argparse
import copy
import fcntl
import grp
import hashlib
import json
import os
from pathlib import Path
import platform
import pwd
import re
import selectors
import signal
import shutil
import socket
import subprocess
import sys
import time
import uuid
import xml.etree.ElementTree as ET


CONNECTION = "qemu:///system"
CHANNEL = "org.harniqo.m0"
MAX_FRAME = 64 * 1024
MAX_CONSOLE_BYTES = 2 * 1024 * 1024
CONSOLE_DRAIN_SECONDS = 10
SIGNER = "D2EB44626FDDC30B513D5BB71A5D6C4C7DB87C81"


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def run(args: list[str], *, timeout: int = 30, data: str | None = None) -> str:
    done = subprocess.run(args, input=data, capture_output=True, text=True,
                          timeout=timeout, env={**os.environ, "LC_ALL": "C"})
    if done.returncode:
        raise RuntimeError(f"{args[0]} failed ({done.returncode}): {done.stderr[-3000:].strip()}")
    return done.stdout.strip()


def virsh(*args: str) -> str:
    return run(["virsh", "--connect", CONNECTION, *args])


def sha256(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(4 * 1024**2):
            result.update(chunk)
    return result.hexdigest()


def write(path: Path, content: str, mode: int = 0o600) -> None:
    with path.open("x") as output:
        output.write(content)
        output.flush()
        os.fsync(output.fileno())
    path.chmod(mode)


def domain_ids() -> set[str]:
    result = set()
    for identity in virsh("list", "--all", "--uuid").splitlines():
        if identity.strip():
            result.add(str(uuid.UUID(identity.strip())))
    return result


def inventory() -> dict[str, str]:
    return {identity: virsh("domstate", identity) for identity in sorted(domain_ids())}


def no_symlinks(path: Path) -> None:
    for item in [path, *path.parents]:
        if item.is_symlink():
            raise RuntimeError(f"Symlink paths are not accepted: {item}")


class TemporaryACL:
    """Grant only missing access; restore existing paths unless another actor changed them."""

    def __init__(self, user: str):
        account = pwd.getpwnam(user)
        if account.pw_uid == 0:
            raise RuntimeError("The QEMU account must not be root")
        self.uid = account.pw_uid
        self.groups = {account.pw_gid} | {g.gr_gid for g in grp.getgrall() if user in g.gr_mem}
        self.changes: list[tuple[Path, str, str, bool]] = []

    @staticmethod
    def read(path: Path) -> str:
        return run(["getfacl", "--absolute-names", "--numeric", str(path)]) + "\n"

    @staticmethod
    def bits(permissions: str) -> int:
        return sum(value for char, value in zip(permissions, (4, 2, 1)) if char != "-")

    def effective(self, path: Path, acl: str) -> int:
        entries = {}
        for line in acl.splitlines():
            line = line.split("#", 1)[0].strip()
            fields = line.split(":")
            if len(fields) == 3 and fields[0] in ("user", "group", "mask", "other"):
                entries[(fields[0], fields[1])] = self.bits(fields[2])
        info = path.stat()
        mask = entries.get(("mask", ""), 7)
        if info.st_uid == self.uid:
            return entries[("user", "")]
        if ("user", str(self.uid)) in entries:
            return entries[("user", str(self.uid))] & mask
        matched = False
        permissions = 0
        if info.st_gid in self.groups:
            matched = True
            permissions |= entries[("group", "")]
        for group in self.groups:
            if ("group", str(group)) in entries:
                matched = True
                permissions |= entries[("group", str(group))]
        return permissions & mask if matched else entries[("other", "")]

    def grant(self, path: Path, permissions: str, *, restore: bool) -> None:
        no_symlinks(path)
        original = self.read(path)
        needed = self.bits(permissions)
        if self.effective(path, original) & needed == needed:
            return
        if path.stat().st_uid != os.geteuid():
            raise RuntimeError(f"Cannot alter ACL of a path not owned by this user: {path}")
        existing = "---"
        for line in original.splitlines():
            if line.startswith(f"user:{self.uid}:"):
                existing = line.split(":", 2)[2].split()[0]
        combined = self.bits(existing) | needed
        value = "".join(char if combined & bit else "-" for char, bit in zip("rwx", (4, 2, 1)))
        run(["setfacl", "-m", f"u:{self.uid}:{value}", str(path)])
        applied = self.read(path)
        self.changes.append((path, original, applied, restore))
        # Widening an ACL mask must not unmask another named user's/group's rights.
        def entries(text: str) -> dict[tuple[str, str], int]:
            result = {}
            for line in text.splitlines():
                fields = line.split("#", 1)[0].strip().split(":")
                if len(fields) == 3 and fields[0] in {"user", "group", "mask"}:
                    result[(fields[0], fields[1])] = self.bits(fields[2])
            return result
        before, after = entries(original), entries(applied)
        old_mask, new_mask = before.get(("mask", ""), 7), after.get(("mask", ""), 7)
        for key, permissions_before in before.items():
            if key[0] == "group" or (key[0] == "user" and key[1] not in {"", str(self.uid)}):
                if permissions_before & old_mask != after.get(key, 0) & new_mask:
                    run(["setfacl", "--restore=-"], data=original)
                    self.changes.pop()
                    raise RuntimeError(f"ACL mask would expand another principal's access: {path}")
        if self.effective(path, applied) & needed != needed:
            raise RuntimeError(f"QEMU ACL grant was ineffective: {path}")

    def traverse(self, path: Path, *, restore: bool = True) -> None:
        for parent in reversed(path.parents):
            self.grant(parent, "--x", restore=restore)

    def restore(self) -> list[str]:
        errors = []
        for path, original, applied, restore in reversed(self.changes):
            if not restore:
                continue
            try:
                if self.read(path) != applied:
                    raise RuntimeError("ACL changed concurrently; leaving it unchanged")
                run(["setfacl", "--restore=-"], data=original)
                if self.read(path) != original:
                    raise RuntimeError("Restored ACL differs from its saved value")
            except Exception as exc:
                errors.append(f"{path}: {exc}")
        return errors


GUEST = r'''#!/usr/bin/python3
import json,os,subprocess,time
from pathlib import Path
C=json.loads(CONFIG_JSON)
report={'schema_version':1,'instance_uuid':C['instance_uuid'],'nonce':C['nonce']}
try:
    directory=Path('/var/lib/harniqo-m0')
    directory.mkdir(mode=0o700,exist_ok=True)
    canary=directory/('canary-'+C['instance_uuid']+'.json')
    existed=canary.exists()
    previous=json.loads(canary.read_text()) if existed else {}
    boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    nonce_valid=not existed or (previous.get('nonce')==C['nonce'] and previous.get('instance_uuid')==C['instance_uuid'])
    count=previous.get('boot_count',0)+1
    state={'instance_uuid':C['instance_uuid'],'nonce':C['nonce'],'boot_count':count,'last_boot_id':boot_id}
    with canary.open('w') as output:
        os.chmod(canary,0o600)
        json.dump(state,output)
        output.flush();os.fsync(output.fileno())
    shared=[]
    for line in Path('/proc/self/mountinfo').read_text().splitlines():
        tail=line.split(' - ',1)[1].split()
        if tail[0] in {'virtiofs','9p','nfs','nfs4','cifs','fuse.sshfs','vboxsf','fuse.vmhgfs-fuse'}:
            shared.append({'type':tail[0],'source':tail[1]})
    routes=[]
    for line in Path('/proc/net/route').read_text().splitlines()[1:]:
        fields=line.split()
        if len(fields)>3 and fields[1]=='00000000' and fields[0]!='lo':routes.append(fields[0])
    ipv6=Path('/proc/net/ipv6_route')
    if ipv6.exists():
        for line in ipv6.read_text().splitlines():
            fields=line.split()
            if len(fields)>=10 and fields[0]=='0'*32 and fields[1]=='00' and fields[-1]!='lo':routes.append(fields[-1])
    sockets={p:Path(p).exists() for p in ['/run/docker.sock','/var/run/docker.sock','/run/libvirt/libvirt-sock','/var/run/libvirt/libvirt-sock']}
    memory={k:v.strip() for k,v in (line.split(':',1) for line in Path('/proc/meminfo').read_text().splitlines())}
    virtual=subprocess.run(['systemd-detect-virt','--vm'],capture_output=True,text=True,timeout=10).stdout.strip()
    total_kib=int(memory['MemTotal'].split()[0])
    block_bytes=int(Path('/sys/block/vda/size').read_text())*512
    interfaces=sorted(p.name for p in Path('/sys/class/net').iterdir())
    report.update({'boot_id':boot_id,'previous_boot_id':previous.get('last_boot_id'),'boot_count':count,
      'canary_preexisting':existed,'canary_nonce_valid':nonce_valid,'other_canary_absent':not (directory/('canary-'+C['other_uuid']+'.json')).exists(),
      'virtualization':virtual,'cpus':os.cpu_count(),'memory_kib':total_kib,'virtual_disk_bytes':block_bytes,
      'network_interfaces':interfaces,'non_loopback_default_routes':routes,'host_socket_paths':sockets,
      'shared_filesystems':shared,'host_workspace_visible':Path(C['host_workspace']).exists()})
    report['checks']={'kvm_guest':virtual=='kvm','one_vcpu':os.cpu_count()==1,
      'one_gib_visible':512*1024<=total_kib<=1024*1024,'disk_capacity':block_bytes==C['disk_bytes'],
      'no_nic':interfaces==['lo'],'no_external_default_route':not routes,'no_host_socket':not any(sockets.values()),
      'no_shared_filesystem':not shared,'no_host_workspace':not report['host_workspace_visible'],
      'canary_valid':nonce_valid,'other_canary_absent':report['other_canary_absent']}
    report['passed']=all(report['checks'].values())
    os.sync()
except Exception as error:
    report.update({'passed':False,'error':repr(error)})
port=Path('/dev/virtio-ports/org.harniqo.m0')
for _ in range(60):
    if port.exists():break
    time.sleep(1)
with port.open('wb',buffering=0) as output:
    output.write((json.dumps(report,separators=(',',':'))+'\n').encode())
'''


def guest_config(instance: dict, other: dict, root: Path, disk_bytes: int) -> str:
    context = {"instance_uuid": instance["uuid"], "other_uuid": other["uuid"],
               "nonce": instance["nonce"], "host_workspace": str(root), "disk_bytes": disk_bytes}
    program = GUEST.replace("CONFIG_JSON", repr(json.dumps(context)))
    service = ("[Unit]\nDescription=Harniqo sealed M0 probe\nAfter=local-fs.target\n"
               "[Service]\nType=oneshot\nExecStart=/usr/bin/python3 /usr/local/lib/harniqo-m0-probe.py\n"
               "TimeoutStartSec=90\nRemainAfterExit=yes\n[Install]\nWantedBy=multi-user.target\n")
    config = {"preserve_hostname": False, "disable_root": True, "ssh_pwauth": False,
              "package_update": False, "package_upgrade": False, "growpart": {"mode": "off"},
              "resize_rootfs": False,
              "write_files": [{"path": "/usr/local/lib/harniqo-m0-probe.py", "permissions": "0700", "content": program},
                              {"path": "/etc/systemd/system/harniqo-m0-probe.service", "permissions": "0644", "content": service}],
              "runcmd": [["systemctl", "daemon-reload"],
                         ["systemctl", "enable", "--now", "harniqo-m0-probe.service"]]}
    return "#cloud-config\n" + json.dumps(config) + "\n"


def domain_xml(instance: dict, emulator: str) -> str:
    domain = ET.Element("domain", type="kvm")
    ET.SubElement(domain, "name").text = instance["name"]
    ET.SubElement(domain, "uuid").text = instance["uuid"]
    ET.SubElement(domain, "memory", unit="MiB").text = "1024"
    ET.SubElement(domain, "currentMemory", unit="MiB").text = "1024"
    ET.SubElement(domain, "vcpu", placement="static").text = "1"
    os_node = ET.SubElement(domain, "os")
    ET.SubElement(os_node, "type", arch="x86_64", machine="q35").text = "hvm"
    ET.SubElement(os_node, "boot", dev="hd")
    features = ET.SubElement(domain, "features")
    ET.SubElement(features, "acpi"); ET.SubElement(features, "apic")
    ET.SubElement(domain, "cpu", mode="host-model")
    ET.SubElement(domain, "on_poweroff").text = "destroy"
    ET.SubElement(domain, "on_reboot").text = "restart"
    ET.SubElement(domain, "on_crash").text = "destroy"
    devices = ET.SubElement(domain, "devices")
    ET.SubElement(devices, "emulator").text = emulator
    for device, image, target, bus, image_format in [
        ("disk", instance["overlay"], "vda", "virtio", "qcow2"),
        ("cdrom", instance["seed"], "sda", "sata", "raw"),
    ]:
        disk = ET.SubElement(devices, "disk", type="file", device=device)
        ET.SubElement(disk, "driver", name="qemu", type=image_format)
        ET.SubElement(disk, "source", file=str(image))
        ET.SubElement(disk, "target", dev=target, bus=bus)
        if device == "cdrom": ET.SubElement(disk, "readonly")
    ET.SubElement(devices, "controller", type="sata", index="0")
    ET.SubElement(devices, "controller", type="virtio-serial", index="0")
    channel = ET.SubElement(devices, "channel", type="unix")
    ET.SubElement(channel, "source", mode="connect", path=str(instance["socket"]))
    ET.SubElement(channel, "target", type="virtio", name=CHANNEL)
    serial = ET.SubElement(devices, "serial", type="unix")
    ET.SubElement(serial, "source", mode="connect", path=str(instance["console_socket"]))
    ET.SubElement(serial, "target", port="0")
    video = ET.SubElement(devices, "video")
    ET.SubElement(video, "model", type="none")
    ET.SubElement(devices, "memballoon", model="none")
    return ET.tostring(domain, encoding="unicode") + "\n"


def listener_at(path: Path) -> socket.socket:
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        if len(os.fsencode(path)) >= 104:
            raise RuntimeError("Workspace path is too long for the private UNIX socket")
        listener.bind(str(path))
        path.chmod(0o600)
        listener.listen(2)
        listener.setblocking(False)
        return listener
    except BaseException:
        listener.close()
        raise


class GuestStreams:
    """Collect JSON reports and bounded serial bytes through separate host-owned sockets."""

    def __init__(self, instances: list[dict], work: Path, round_index: int, logs: list[dict], *, retain_report_channels: bool = False):
        self.instances = instances
        self.received: dict[str, dict] = {}
        self.retain_report_channels = retain_report_channels
        self.report_channels: dict[str, socket.socket] = {}
        self.logs: dict[str, dict] = {}
        self.connections: set[socket.socket] = set()
        self.buffers: dict[socket.socket, bytearray] = {}
        self.accepted: set[tuple[str, str]] = set()
        self.selector = selectors.DefaultSelector()
        self.closed = False
        try:
            for instance in instances:
                path = work / f"{instance['name']}-round-{round_index}-console.log"
                descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                os.fchmod(descriptor, 0o600)
                info = os.fstat(descriptor)
                log = {"instance_uuid": instance["uuid"], "round": round_index, "source_path": path,
                       "fd": descriptor, "inode": (info.st_dev, info.st_ino), "limit_bytes": MAX_CONSOLE_BYTES,
                       "boot_attempted": False, "connected": False, "stream_closed": False,
                       "bytes_seen": 0, "bytes_saved": 0, "discarded_bytes": 0, "truncated": False}
                logs.append(log)
                self.logs[instance["uuid"]] = log
                for role, listener_key in [("report", "listener"), ("console", "console_listener")]:
                    self.selector.register(instance[listener_key], selectors.EVENT_READ, (instance, role, True))
        except BaseException:
            self._close()
            raise

    def mark_start(self, instance: dict) -> None:
        self.logs[instance["uuid"]]["boot_attempted"] = True

    def _disconnect(self, connection: socket.socket) -> None:
        self.selector.unregister(connection)
        self.connections.remove(connection)
        self.buffers.pop(connection, None)
        connection.close()

    def _poll(self, timeout: float) -> None:
        for key, _ in self.selector.select(timeout):
            instance, role, is_listener = key.data
            if is_listener:
                connection, _ = key.fileobj.accept()
                connection.setblocking(False)
                identity = (instance["uuid"], role)
                if identity in self.accepted:
                    connection.close()
                    raise RuntimeError(f"Duplicate {role} connection: {instance['name']}")
                self.accepted.add(identity)
                self.connections.add(connection)
                if role == "console":
                    self.logs[instance["uuid"]]["connected"] = True
                else:
                    self.buffers[connection] = bytearray()
                self.selector.register(connection, selectors.EVENT_READ, (instance, role, False))
                continue
            connection = key.fileobj
            chunk = connection.recv(8192)
            if not chunk:
                if role == "console":
                    self.logs[instance["uuid"]]["stream_closed"] = True
                self._disconnect(connection)
                continue
            if role == "console":
                log = self.logs[instance["uuid"]]
                log["bytes_seen"] += len(chunk)
                saved = chunk[:max(0, MAX_CONSOLE_BYTES - log["bytes_saved"])]
                view = memoryview(saved)
                while view:
                    written = os.write(log["fd"], view)
                    if written <= 0:
                        raise RuntimeError("Console log write made no progress")
                    log["bytes_saved"] += written
                    view = view[written:]
                log["discarded_bytes"] = log["bytes_seen"] - log["bytes_saved"]
                log["truncated"] = log["discarded_bytes"] > 0
                continue
            self.buffers[connection].extend(chunk)
            if len(self.buffers[connection]) > MAX_FRAME:
                raise RuntimeError("Guest frame exceeded its bound")
            if b"\n" in self.buffers[connection]:
                frame = bytes(self.buffers[connection]).split(b"\n", 1)[0]
                report = json.loads(frame)
                if report.get("instance_uuid") != instance["uuid"] or report.get("nonce") != instance["nonce"]:
                    raise RuntimeError("Unexpected guest identity")
                if instance["uuid"] in self.received:
                    raise RuntimeError("Duplicate guest report")
                self.received[instance["uuid"]] = report
                if self.retain_report_channels:
                    self.selector.unregister(connection)
                    self.buffers.pop(connection, None)
                    self.report_channels[instance["uuid"]] = connection
                else:
                    self._disconnect(connection)

    def collect(self, timeout: int) -> dict[str, dict]:
        deadline = time.monotonic() + timeout
        while (len(self.received) < len(self.instances)
               or any(not log["connected"] or not log["bytes_seen"] for log in self.logs.values())):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                missing = [x["name"] for x in self.instances if x["uuid"] not in self.received]
                missing_console = [identity for identity, log in self.logs.items()
                                   if not log["connected"] or not log["bytes_seen"]]
                raise TimeoutError(f"Guest stream deadline exceeded: reports={missing}, consoles={missing_console}")
            self._poll(min(remaining, 1))
        return self.received

    def finish(self) -> None:
        """Call after stopping the guests; preserve all buffered console bytes through EOF."""
        if self.closed:
            return
        deadline = time.monotonic() + CONSOLE_DRAIN_SECONDS
        try:
            while any(log["boot_attempted"] and not log["stream_closed"] for log in self.logs.values()):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Stopped guest console did not reach EOF before drain deadline")
                self._poll(min(remaining, 1))
        finally:
            self._close()

    def _close(self) -> None:
        for connection in self.connections:
            connection.close()
        self.connections.clear()
        self.report_channels.clear()
        self.selector.close()
        self.closed = True


def save_console(log: dict, reports: Path) -> None:
    """Archive the host's log inode; QEMU never receives this file's path or descriptor."""
    descriptor = log["fd"]
    try:
        os.fsync(descriptor)
        info = os.fstat(descriptor)
        current = log["source_path"].lstat()
        if ((info.st_dev, info.st_ino) != log["inode"]
                or (current.st_dev, current.st_ino) != log["inode"]
                or info.st_uid != os.geteuid() or info.st_mode & 0o777 != 0o600
                or info.st_size != log["bytes_saved"] or info.st_size > MAX_CONSOLE_BYTES):
            raise RuntimeError("Host console log identity, permissions or bounded size differs")
        os.lseek(descriptor, 0, os.SEEK_SET)
        destination = reports / log["source_path"].name
        output_fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        os.fchmod(output_fd, 0o600)
        with os.fdopen(output_fd, "wb") as output:
            while chunk := os.read(descriptor, 1024**2):
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        log["archive_path"] = str(destination)
        if log["boot_attempted"] and not (log["connected"] and log["bytes_seen"] and log["stream_closed"]):
            raise RuntimeError("Boot console evidence requires a nonempty connection drained through EOF")
        log["saved"] = True
    finally:
        os.close(descriptor)


class RoundEvidence:
    """Keep received reports and the failing phase even when a round raises."""

    def __init__(self, rounds: list[dict], round_index: int, instances: list[dict]):
        self.record = {"round": round_index, "status": "running", "phase": "prepare_streams",
                       "started_at": now(), "expected_guest_uuids": [item["uuid"] for item in instances],
                       "guests": {}, "validation_error": None}
        self.streams: GuestStreams | None = None
        rounds.append(self.record)

    def __enter__(self) -> RoundEvidence:
        return self

    def snapshot(self) -> None:
        self.record["guests"] = copy.deepcopy(self.streams.received if self.streams else {})
        self.record["missing_guest_uuids"] = [identity for identity in self.record["expected_guest_uuids"]
                                              if identity not in self.record["guests"]]

    def __exit__(self, error_type, error, traceback) -> bool:
        try:
            if error is not None:
                self.record.update({"status": "failed", "failed_phase": self.record["phase"],
                                    "validation_error": f"{error_type.__name__}: {error}"})
            else:
                self.record.update({"status": "passed", "phase": "complete"})
        finally:
            self.snapshot()
            self.record["finished_at"] = now()
        return False


def validate_round(received: dict[str, dict], instances: list[dict], round_index: int,
                   previous: dict[str, dict] | None) -> None:
    for instance in instances:
        guest = received[instance["uuid"]]
        if not guest.get("passed") or guest.get("boot_count") != round_index or guest.get("canary_preexisting") != (round_index == 2):
            raise RuntimeError(f"Guest checks failed: {instance['name']}: {json.dumps(guest)}")
        if round_index == 2:
            prior = previous[instance["uuid"]]
            if guest.get("previous_boot_id") != prior["boot_id"] or guest.get("boot_id") == prior["boot_id"]:
                raise RuntimeError("Cold restart/persistent canary proof failed")


def stop_owned(instance: dict, baseline: dict[str, str]) -> None:
    identity = instance["uuid"]
    if identity in baseline:
        raise RuntimeError("Refusing to stop a pre-existing UUID")
    if identity not in domain_ids(): return
    tree = ET.fromstring(virsh("dumpxml", identity))
    if tree.findtext("name") != instance["name"] or tree.findtext("uuid") != identity:
        raise RuntimeError("Domain ownership no longer matches")
    virsh("destroy", identity)
    deadline = time.monotonic() + 15
    while identity in domain_ids():
        if time.monotonic() > deadline:
            raise RuntimeError("Transient domain remained after destroy; preserving files")
        time.sleep(0.2)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--base-image", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=240, help="Guest report timeout per boot round (30..900 seconds)")
    parser.add_argument("--disk-gib", type=int, default=8, help="Virtual disk capacity per guest (4..32 GiB)")
    parser.add_argument("--qemu-user", default="libvirt-qemu")
    args = parser.parse_args()
    if not 30 <= args.timeout <= 900 or not 4 <= args.disk_gib <= 32:
        parser.error("Timeout/disk bound is outside the supported range")
    if sys.platform != "linux" or platform.machine() not in {"x86_64", "amd64"} or os.geteuid() == 0:
        parser.error("Run as an ordinary authorized user on Linux x86_64, not root")
    root = args.root.absolute()
    base = args.base_image.absolute()
    no_symlinks(root); no_symlinks(base)
    root = root.resolve(strict=True); base = base.resolve(strict=True)
    if not (root / "images/README.md").is_file() or not base.is_relative_to(root / "images/vm/base"):
        parser.error("Use a Harniqo workspace and its authenticated images/vm/base image")
    for command in ["virsh", "qemu-img", "qemu-system-x86_64", "cloud-localds", "getfacl", "setfacl"]:
        if not shutil.which(command): parser.error(f"Missing prerequisite: {command}")
    if not re.fullmatch(r"[0-9a-f]{64}\.qcow2", base.name):
        parser.error("Base image must be named by its verified SHA-256")
    receipt = json.loads(base.with_suffix(".provenance.json").read_text())
    if receipt.get("sha256") != base.stem or receipt.get("signer_fingerprint") != SIGNER:
        parser.error("Expected the pinned Canonical provenance from fetch_ubuntu_image.py")
    if sha256(base) != base.stem:
        raise RuntimeError("Base image differs from its authenticated digest")
    info = json.loads(run(["qemu-img", "info", "--output=json", str(base)]))
    disk_bytes = args.disk_gib * 1024**3
    if info.get("format") != "qcow2" or info.get("backing-filename") or info.get("virtual-size", 0) > disk_bytes:
        raise RuntimeError("Expected standalone qcow2 fitting the requested virtual disk")
    if shutil.disk_usage(root).free < 50 * 1024**3 + 2 * disk_bytes:
        raise RuntimeError("Insufficient free space; preserve 50 GiB plus both disk allowances")

    state_parent = root / ".state/qualification"
    state_parent.mkdir(parents=True, exist_ok=True)
    no_symlinks(state_parent)
    lock = (state_parent / "kvm.lock").open("a")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    def terminate(signum, frame):
        raise KeyboardInterrupt(f"Received signal {signum}")
    signal.signal(signal.SIGTERM, terminate)
    run_id = uuid.uuid4().hex[:12]
    work = state_parent / ("kvm-" + run_id)
    images = root / "images/vm/instances" / ("m0-" + run_id)
    private = root / ".private"
    report_parent = private / "qualification"
    for directory in [private, report_parent]:
        no_symlinks(directory)
        directory.mkdir(mode=0o700, exist_ok=True)
        if not directory.is_dir() or directory.stat().st_uid != os.geteuid():
            raise RuntimeError(f"Private report directory is not owned by this user: {directory}")
        directory.chmod(0o700)
    reports = report_parent / ("kvm-" + run_id)
    for directory in [work, images, reports]:
        no_symlinks(directory)
        directory.mkdir(mode=0o700, parents=True)
        directory.chmod(0o700)
    directory_identities = {directory: (directory.stat().st_dev, directory.stat().st_ino) for directory in [work, images]}
    ownership = {"qualification": "harniqo-m0-kvm", "run_id": run_id}
    for directory in [work, images]:
        write(directory / ".harniqo-m0-owner.json", json.dumps(ownership) + "\n")
    baseline = inventory()
    instances = []
    stream_rounds: list[RoundEvidence] = []
    console_logs: list[dict] = []
    acl = TemporaryACL(args.qemu_user)
    result = {"schema_version": 1, "qualification": "M0-05-sealed-kvm-baseline", "run_id": run_id,
              "started_at": now(), "connection": CONNECTION, "base_sha256": base.stem,
              "preexisting_domains": baseline, "rounds": [], "errors": [], "cleanup_errors": [],
              "milestone_complete": False,
              "not_validated": ["selective model/tool egress", "production tenant policy enforcement",
                                "Harness execution and native recovery", "execution leases and fencing",
                                "metering/budget", "CPU/memory/disk exhaustion handling", "disaster recovery"]}
    try:
        capabilities = ET.fromstring(virsh("capabilities"))
        if not capabilities.findall(".//domain[@type='kvm']"):
            raise RuntimeError("libvirt does not advertise KVM")
        run(["df", "-h", str(root)])
        for label in ["a", "b"]:
            instance = {"uuid": str(uuid.uuid4()), "name": f"harniqo-m0-{run_id}-{label}",
                        "nonce": uuid.uuid4().hex, "overlay": images / f"{label}.qcow2",
                        "seed": images / f"{label}-seed.img", "console_socket": work / f"{label}-console.sock",
                        "socket": work / f"{label}.sock", "xml": work / f"{label}.xml"}
            if instance["uuid"] in baseline:
                raise RuntimeError("Generated UUID already exists")
            instances.append(instance)
        for instance, other in [(instances[0], instances[1]), (instances[1], instances[0])]:
            run(["qemu-img", "create", "-f", "qcow2", "-F", "qcow2", "-b", str(base), str(instance["overlay"]), str(disk_bytes)])
            instance["overlay"].chmod(0o600)
            user_data, metadata, network = [work / f"{instance['name']}-{name}.json" for name in ["user", "meta", "network"]]
            write(user_data, guest_config(instance, other, root, disk_bytes))
            write(metadata, json.dumps({"instance-id": instance["uuid"], "local-hostname": instance["name"]}) + "\n")
            write(network, json.dumps({"version": 2, "ethernets": {}}) + "\n")
            run(["cloud-localds", "--network-config", str(network), str(instance["seed"]), str(user_data), str(metadata)])
            instance["seed"].chmod(0o600)
            instance["listener"] = listener_at(instance["socket"])
            instance["console_listener"] = listener_at(instance["console_socket"])
            write(instance["xml"], domain_xml(instance, shutil.which("qemu-system-x86_64")))
            for path, permissions in [(base, "r--"), (instance["overlay"], "rw-"),
                                      (instance["seed"], "r--"), (instance["socket"], "rw-"),
                                      (instance["console_socket"], "rw-")]:
                acl.traverse(path)
                acl.grant(path, permissions, restore=path == base)
        result["instances"] = [{k: str(v) for k, v in item.items()
                                if k not in {"listener", "console_listener"}} for item in instances]
        for round_index in [1, 2]:
            with RoundEvidence(result["rounds"], round_index, instances) as evidence:
                stream_rounds.append(evidence)
                streams = GuestStreams(instances, work, round_index, console_logs)
                evidence.streams = streams
                evidence.record["phase"] = "boot"
                print(json.dumps({"phase": "boot", "round": round_index, "guests": 2}), flush=True)
                for instance in instances:
                    streams.mark_start(instance)
                    virsh("create", str(instance["xml"]))
                    tree = ET.fromstring(virsh("dumpxml", instance["uuid"]))
                    if tree.get("type") != "kvm" or tree.findall("./devices/interface") or tree.findall("./devices/filesystem"):
                        raise RuntimeError("Actual domain is not the expected sealed KVM topology")
                evidence.record["phase"] = "collect"
                received = streams.collect(args.timeout)
                evidence.record["phase"] = "validate"
                validate_round(received, instances, round_index,
                               result["rounds"][0]["guests"] if round_index == 2 else None)
                evidence.record["phase"] = "stop"
                for instance in instances: stop_owned(instance, baseline)
                evidence.record["phase"] = "console_drain"
                streams.finish()
            print(json.dumps({"phase": "round_complete", "round": round_index, "checks_passed": True}), flush=True)
    except BaseException as exc:
        result["errors"].append(f"{type(exc).__name__}: {exc}")
    finally:
        console_save_failed = False
        for instance in instances:
            try: stop_owned(instance, baseline)
            except Exception as exc: result["cleanup_errors"].append(f"stop {instance['uuid']}: {exc}")
        for evidence in stream_rounds:
            try:
                if evidence.streams: evidence.streams.finish()
            except Exception as exc:
                console_save_failed = True
                result["cleanup_errors"].append(f"drain console: {exc}")
            finally:
                evidence.snapshot()
        for instance in instances:
            for listener_key in ["listener", "console_listener"]:
                listener = instance.get(listener_key)
                if listener: listener.close()
        for log in console_logs:
            try:
                save_console(log, reports)
            except Exception as exc:
                console_save_failed = True
                result["cleanup_errors"].append(f"save console {log['source_path'].name}: {exc}")
        result["console_logs"] = [{key: str(value) if isinstance(value, Path) else value
                                    for key, value in log.items() if key not in {"fd", "inode"}}
                                  for log in console_logs]
        result["cleanup_errors"].extend(acl.restore())
        try:
            after = inventory()
            result["postexisting_domains"] = after
            result["preexisting_domains_unchanged"] = after == baseline
            if after != baseline: result["errors"].append("Domain inventory/state differs from before qualification")
            result["base_unchanged"] = sha256(base) == base.stem
            if not result["base_unchanged"]: result["errors"].append("Base image digest changed")
            if not any(item["uuid"] in after for item in instances):
                removable = [(images, root / "images/vm/instances")]
                if console_save_failed:
                    result["diagnostic_work_directory_preserved"] = str(work)
                else:
                    removable.append((work, state_parent))
                for directory, parent in removable:
                    if directory.parent != parent or directory.is_symlink() or directory.resolve() != directory:
                        raise RuntimeError("Cleanup path no longer matches the task-owned directory")
                    if (directory.stat().st_dev, directory.stat().st_ino) != directory_identities[directory]:
                        raise RuntimeError("Task directory was replaced; refusing cleanup")
                    if json.loads((directory / ".harniqo-m0-owner.json").read_text()) != ownership:
                        raise RuntimeError("Task directory ownership marker differs; refusing cleanup")
                    shutil.rmtree(directory)
                result["temporary_images_removed"] = True
            else:
                result["cleanup_errors"].append("Owned domains remain; preserving all task files")
        except Exception as exc:
            result["cleanup_errors"].append(f"final verification: {exc}")
        result["finished_at"] = now()
        result["passed"] = (not result["errors"] and not result["cleanup_errors"] and len(result["rounds"]) == 2
                            and all(item["status"] == "passed" for item in result["rounds"]))
        write(reports / "report.json", json.dumps(result, indent=2) + "\n")
        print(json.dumps({"qualification": result["qualification"], "passed": result["passed"],
                          "milestone_complete": False, "report": str(reports / "report.json"),
                          "errors": len(result["errors"]), "cleanup_errors": len(result["cleanup_errors"])}), flush=True)
        lock.close()
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
