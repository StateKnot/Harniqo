#!/usr/bin/env python3
"""Bounded single-VM stream holder and resource observer; never manages a VM."""
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
import subprocess
import sys
import time

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(Path(__file__).parent))
from apps.runner.console_store import BoundedConsole
from apps.runner.resource_broker import prepare_seed_access, resources
from apps.runner.resource_broker import service
from qualify_libvirt_acl import _read_process, _identity, _trusted, _decode

class FixtureError(ValueError): pass

def need(value, code):
    if not value: raise FixtureError(code)

def emit(value):
    raw = json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
    need(len(raw) < 65536, 'fixture_output_limit')
    sys.stdout.buffer.write(raw + b'\n'); sys.stdout.buffer.flush()

def guest_report(raw, nonce):
    need(len(raw) <= 16384, 'fixture_report_limit')
    value = _decode(raw)
    need(set(value) == {'kind', 'nonce', 'boot_id', 'uid'} and value['kind'] == 'harniqo-vm-probe-v1'
        and value['nonce'] == nonce and type(value['uid']) is int and value['uid'] == 0
        and type(value['boot_id']) is str
        and re.fullmatch('[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}', value['boot_id']), 'fixture_guest_report_invalid')
    return value

def load_plan(path, digest):
    _trusted(path)
    info = path.lstat()
    need(stat.S_IMODE(info.st_mode) == 0o640 and info.st_gid == os.getgid(), 'fixture_plan_mode')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        before = os.fstat(stream.fileno()); raw = stream.read(65537); after = os.fstat(stream.fileno())
    need((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
        == (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
        and len(raw) <= 65536 and hashlib.sha256(raw).hexdigest() == digest, 'fixture_plan_changed')
    value = _decode(raw)
    need(set(value) == {'schema_version', 'configuration_path', 'configuration_sha256', 'nonce', 'report_directory', 'timeout_seconds'}
        and type(value['schema_version']) is int and value['schema_version'] == 1
        and type(value['nonce']) is str and re.fullmatch('[a-f0-9]{32}', value['nonce'])
        and type(value['timeout_seconds']) is int and 30 <= value['timeout_seconds'] <= 300, 'fixture_plan_invalid')
    config = service.load_configuration(Path(value['configuration_path']), expected_sha256=value['configuration_sha256'])
    need(len(config.templates) == 1, 'fixture_single_template_required')
    directory = Path(value['report_directory'])
    need(directory.is_absolute() and '..' not in directory.parts, 'fixture_report_directory')
    for p in (directory, *directory.parents):
        s = p.lstat(); need(stat.S_ISDIR(s.st_mode) and not s.st_mode & 0o022
            and s.st_uid in {0, os.geteuid()}, 'fixture_report_ancestor')
    need(directory.stat().st_uid == os.geteuid() and stat.S_IMODE(directory.stat().st_mode) == 0o700, 'fixture_report_private')
    return value, config, directory

class StreamCollector:
    """Bounded raw report/console collection, including post-exit socket tail."""
    def __init__(self, console, raw_report, nonce):
        self.console, self.raw_report, self.nonce = console, raw_report, nonce
        self.selector = selectors.DefaultSelector(); self.connections = []
        self.accepted = set(); self.closed = set(); self.buffer = bytearray(); self.received = None
    def listen(self, role, endpoint):
        self.selector.register(endpoint, selectors.EVENT_READ, (role, True))
    def attach(self, role, connection):
        self.connections.append(connection)
        need(role not in self.accepted, 'fixture_duplicate_connection')
        self.accepted.add(role); connection.setblocking(False)
        self.selector.register(connection, selectors.EVENT_READ, (role, False))
    def poll(self, timeout, *, validate=True):
        report = None
        for key, unused in self.selector.select(timeout):
            role, listener = key.data
            if listener:
                connection, unused = key.fileobj.accept(); self.attach(role, connection); continue
            chunk = key.fileobj.recv(8192)
            if not chunk:
                self.selector.unregister(key.fileobj); key.fileobj.close(); self.closed.add(role)
                emit({'event':role+'_closed'}); continue
            if role == 'console': self.console.append(chunk)
            else:
                # Retain exact bounded bytes before parsing, including invalid frames.
                try:self.raw_report.append(chunk)
                except BaseException:
                    if validate:raise
                if not validate:continue
                self.buffer.extend(chunk)
                need(self.received is None, 'fixture_duplicate_report')
                need(len(self.buffer) <= 16384, 'fixture_report_limit')
                if b'\n' in self.buffer:
                    need(self.buffer.count(b'\n') == 1 and self.buffer.endswith(b'\n'), 'fixture_report_framing')
                    self.received = guest_report(bytes(self.buffer[:-1]), self.nonce); report = self.received
        return report
    def drain(self, seconds=5, *, validate=True):
        until = time.monotonic() + seconds
        while 'console' not in self.closed and time.monotonic() < until:
            self.poll(min(.1, max(0, until-time.monotonic())), validate=validate)
        need('console' in self.closed, 'fixture_console_eof_missing')
    def close(self):
        self.selector.close()
        for connection in self.connections: connection.close()

def failure(error):
    code = getattr(error, 'code', None)
    if not isinstance(code, str): code = str(error) if isinstance(error, FixtureError) else ''
    return {'type':type(error).__name__, 'code':code if re.fullmatch('[a-z_]{1,100}', code or '') else 'fixture_operation_failed'}

def run(plan, config, directory):
    identity = _read_process(os.getpid()); ipc_gid = config.ipc_directory.stat().st_gid
    _identity(identity, config.broker_identity.uid, config.broker_identity.gid, ipc_gid)
    template = config.templates[0]; qemu = pwd.getpwnam('libvirt-qemu').pw_uid
    need(qemu > 0 and qemu != os.getuid(), 'fixture_qemu_identity')
    listeners = []; console = raw_report = streams = None; stop = False; sampled = False
    receipt = {'event':'fixture_stopped','status':'FAILED','process':identity,'errors':[],'console_eof':False,'configuration_sha256':config.sha256}
    phase = 'setup'
    def stopping(signum, frame):
        nonlocal stop
        stop = True
    def expired(signum, frame): raise FixtureError('fixture_timeout')
    need(signal.getitimer(signal.ITIMER_REAL) == (0., 0.), 'fixture_timer_already_active')
    old = {n:signal.signal(n, stopping) for n in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)}
    old_alarm = signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, plan['timeout_seconds'])
    try:
        console = BoundedConsole.create(directory/'console.log', capacity_bytes=2*1024**2)
        raw_report = BoundedConsole.create(directory/'guest.raw', capacity_bytes=16384)
        streams = StreamCollector(console, raw_report, plan['nonce'])
        phase = 'catalog'
        for path, rights in ((template.disk,'rw-'), (template.seed,'r--')):
            subprocess.run(['/usr/bin/setfacl','-m','u:'+str(qemu)+':'+rights,'--',str(path)],
                check=True,stdin=subprocess.DEVNULL,capture_output=True,timeout=5)
        prepare_seed_access(template.seed, images_root=config.images_root)
        for role,path in (('report',template.channel),('console',template.console)):
            need(not os.path.lexists(path), 'fixture_endpoint_exists')
            endpoint=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);listeners.append(endpoint)
            endpoint.bind(str(path));os.chmod(path,0o600)
            subprocess.run(['/usr/bin/setfacl','-m','u:'+str(qemu)+':rw-','--',str(path)],
                check=True,stdin=subprocess.DEVNULL,capture_output=True,timeout=5)
            endpoint.listen(1);endpoint.setblocking(False);streams.listen(role,endpoint)
        emit({'event':'fixture_ready','process':identity,'template_sha256':template.configuration_sha256})
        phase = 'streams'
        while not stop:
            value=streams.poll(.1)
            if value is not None:
                emit({'event':'guest_report','report':value,'sha256':hashlib.sha256(streams.buffer).hexdigest()})
            if streams.received is not None and not sampled:
                phase='resources'
                sample=resources.read_limits(name=template.name,resource_id=template.resource_id,profile=template.resource_profile)
                emit({'event':'limits_sample','process':identity,'sample':sample});sampled=True;phase='streams'
        phase='drain';streams.drain();receipt['status']='PASSED'
    except BaseException as error:
        receipt['errors'].append({'phase':phase,**failure(error)})
    finally:
        signal.setitimer(signal.ITIMER_REAL,0)
        # Further stop requests cannot interrupt descriptor and artifact cleanup.
        for n in old: signal.signal(n, lambda *unused: None)
        receipt['guest_received']=bool(streams and streams.received is not None)
        receipt['limits_sampled']=sampled;receipt['console_eof']=bool(streams and 'console' in streams.closed)
        if streams is not None:
            if 'console' not in streams.closed:
                try:streams.drain(validate=False)
                except BaseException as error:receipt['status']='FAILED';receipt['errors'].append({'phase':'failure_drain',**failure(error)})
            receipt['console_eof']='console' in streams.closed
            streams.close()
        for endpoint in listeners: endpoint.close()
        for label,sink in (('console',console),('raw_report',raw_report)):
            if sink is not None:
                try:
                    receipt[label]=sink.close()
                    if receipt[label]['failure_code'] is not None:
                        receipt['status']='FAILED';receipt['errors'].append({'phase':'close_'+label,'code':receipt[label]['failure_code']})
                except BaseException as error:
                    receipt['status']='FAILED';receipt['errors'].append({'phase':'close_'+label,**failure(error)})
                    try:receipt[label]=sink.snapshot()
                    except BaseException as snapshot_error:receipt['errors'].append({'phase':'snapshot_'+label,**failure(snapshot_error)})
    try:
        finalize_receipt(directory,receipt)
        need(receipt['status']=='PASSED','fixture_failed')
    finally:
        for n,handler in old.items():signal.signal(n,handler)
        signal.signal(signal.SIGALRM,old_alarm)

def finalize_receipt(directory,receipt):
    # Preserve full in-memory evidence even if its final file cannot be written.
    try:
        fd=os.open(directory/'fixture-result.json',os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,'wb') as stream:
            stream.write(json.dumps(receipt,sort_keys=True).encode()+b'\n');stream.flush();os.fsync(stream.fileno())
        descriptor=os.open(directory,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        try:os.fsync(descriptor)
        finally:os.close(descriptor)
    except BaseException as error:
        receipt['status']='FAILED';receipt['errors'].append({'phase':'receipt_persistence',**failure(error)})
    emit(receipt)

def main():
    p = argparse.ArgumentParser(description=__doc__); p.add_argument('--plan', required=True); p.add_argument('--sha256', required=True)
    args = p.parse_args()
    try:
        need(sys.platform.startswith('linux') and os.getuid() == os.geteuid() != 0, 'fixture_linux_identity')
        plan, config, directory = load_plan(Path(args.plan), args.sha256); run(plan, config, directory); return 0
    except Exception as error:
        emit({'event':'fixture_failed','code':str(error) if isinstance(error, FixtureError) else 'fixture_operation_failed'}); return 1
if __name__ == '__main__': raise SystemExit(main())
