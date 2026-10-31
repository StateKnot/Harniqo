"""Bounded console fault fixture; uses the production sink and Broker observations.

This is an acceptance operator, not an event spool, filesystem quota or Agent.
"""
from __future__ import annotations

import hashlib
import os
import time
import uuid

import guest_memory_pressure as protocol
import qualify_kvm as kvm


STAGE_BYTES = 128 * 1024
FLOOD_BYTES = 3 * 1024**2
REQUIRED_CHECKS = frozenset({
    'console_storage_reserved', 'console_progress_identity_stable',
    'b_healthy_during_console_pressure', 'console_capacity_failure_latched',
    'console_no_overshoot', 'console_broker_stop_confirmed',
    'console_stop_tombstone_rebuilt', 'console_old_authorization_rejected',
    'b_healthy_after_console_stop',
})


def qualification_limits():
    return {
        'virtual_machines': 2, 'console_bytes_per_vm': 2*1024**2,
        'guest_stage_bytes': STAGE_BYTES, 'guest_stage_count': 3,
        'guest_final_burst_bytes': FLOOD_BYTES, 'guest_write_chunk_bytes': 4096,
        'guest_a_total_seconds': 100, 'guest_b_control_seconds': 120,
        'guest_a_independent_alarm_seconds': 100, 'guest_command_bytes': 128,
        'host_pressure_seconds': 40, 'readiness_seconds': 10,
        'stage_seconds': 8, 'challenge_seconds_including_write': 3,
        'control_write_seconds': 2, 'console_receive_chunk_bytes': 8192,
        'console_drain_seconds': kvm.CONSOLE_DRAIN_SECONDS,
        'source_payload_reserved_bytes': 4*1024**2,
        # The archive is a separately admitted copy, not covered by source cap.
        'maximum_source_plus_archive_payload_bytes': 8*1024**2,
    }


def guest_program():
    """Three controlled bursts, each paused for B's causal challenge, then overflow."""
    return r'''
import select,re,signal
fd=os.open('/dev/virtio-ports/org.harniqo.m0',os.O_RDWR|os.O_NONBLOCK)
deadline=time.monotonic()+100
def console_timeout(*_): raise TimeoutError('console fixture deadline')
signal.signal(signal.SIGALRM,console_timeout)
signal.alarm(100)
pending=b''
def read_command():
    global pending
    while time.monotonic()<deadline:
        if b'\n' in pending:
            line,pending=pending.split(b'\n',1)
            if not re.fullmatch(rb'[0-9a-f]{40}',line): raise RuntimeError('console command invalid')
            return line.decode('ascii')
        if not select.select([fd],[],[],min(.1,max(0,deadline-time.monotonic())))[0]: continue
        chunk=os.read(fd,128)
        if not chunk: continue
        pending+=chunk
        if len(pending)>128: raise RuntimeError('console command bound')
    raise TimeoutError('console fixture deadline')
def emit(count):
    block=b'C'*4095+b'\n'
    for _ in range(count//4096):
        if time.monotonic()>=deadline: raise TimeoutError('console fixture deadline')
        view=memoryview(block)
        while view:
            written=os.write(1,view)
            if written<=0: raise RuntimeError('console write stalled')
            view=view[written:]
print('HARNIQO-CONSOLE-READY '+C['nonce'],flush=True)
for _ in range(3):
    token=read_command()
    emit(128*1024)
    print('HARNIQO-CONSOLE-STAGE '+C['nonce']+' '+token,flush=True)
read_command()
emit(3*1024**2)
print('HARNIQO-CONSOLE-UNEXPECTED-END '+C['nonce'],flush=True)
os.close(fd)
signal.alarm(0)
'''


def _identity(sample):
    return sample['pid'], sample['process_start_time'], sample['cgroup_path']


def _wait(predicate, streams, deadline, message):
    # Absolute deadline is rechecked after predicates and polls as well.
    while True:
        if time.monotonic() >= deadline:
            raise TimeoutError(message)
        answer = predicate()
        now = time.monotonic()
        if now >= deadline:
            raise TimeoutError(message)
        if answer:
            return answer
        streams._poll(min(.05, deadline-now))


def _lines(streams, instance):
    return protocol._complete_lines(protocol.console_text(streams, instance))


def run_pressure(broker, instances, streams, result):
    a, b = instances
    ready = time.monotonic()+10
    _wait(lambda: 'HARNIQO-CONSOLE-READY '+a['nonce'] in _lines(streams,a),
          streams, ready, 'Console fixture readiness deadline')
    before = [broker.resource_limits(i['uuid']) for i in instances]
    snapshots = [streams.logs[i['uuid']].sink.snapshot() for i in instances]
    result['checks']['console_storage_reserved'] = all(
        s['capacity_bytes'] == kvm.MAX_CONSOLE_BYTES and s['reserved_bytes'] >= s['capacity_bytes']
        and s['bytes_saved'] == s['durable_bytes'] and not s['failure_code']
        for s in snapshots)
    if not result['checks']['console_storage_reserved']:
        raise RuntimeError('Production console reserve evidence invalid')
    started = time.monotonic(); deadline = started+40
    stages = []
    for index in range(3):
        token = uuid.uuid4().hex+uuid.uuid4().hex[:8]
        baseline = streams.logs[a['uuid']]['bytes_saved']
        protocol._send(streams.report_channels[a['uuid']], token+'\n', streams, deadline=deadline)
        marker = 'HARNIQO-CONSOLE-STAGE '+a['nonce']+' '+token
        _wait(lambda: marker in _lines(streams,a), streams, min(deadline,time.monotonic()+8),
              'Console allocation stage deadline')
        saved = streams.logs[a['uuid']]['bytes_saved']
        if saved-baseline < STAGE_BYTES:
            raise RuntimeError('Console stage did not save the required fresh payload')
        challenge = protocol.challenge(streams,b,deadline=deadline)
        stages.append({'stage':index+1,'bytes_before':baseline,'bytes_after':saved,
                       'b_challenge':challenge})
    after = [broker.resource_limits(i['uuid']) for i in instances]
    result['checks']['console_progress_identity_stable'] = all(
        _identity(x) == _identity(y) for x,y in zip(before,after))
    result['checks']['b_healthy_during_console_pressure'] = len(stages)==3
    if not result['checks']['console_progress_identity_stable']:
        raise RuntimeError('QEMU identity changed during console pressure')
    token = uuid.uuid4().hex+uuid.uuid4().hex[:8]
    # Writing the trigger itself may poll and receive the first quota failure.
    try:
        protocol._send(streams.report_channels[a['uuid']],token+'\n',streams,deadline=deadline)
        _wait(lambda: False,streams,deadline,'Console overflow was not observed')
    except kvm.GuestConsoleError as error:
        if time.monotonic()>=deadline:
            raise TimeoutError('Late console failure cannot satisfy pressure gate') from error
        if error.instance_uuid!=a['uuid'] or error.failure_code!='console_capacity_exceeded':
            raise
    log=streams.logs[a['uuid']]
    snapshot=log.sink.snapshot()
    try:
        log.sink.append(b'')
    except Exception as error:
        if getattr(error,'code',None)!='console_capacity_exceeded': raise
    else:
        raise RuntimeError('Failed console writer resumed accepting bytes')
    result['checks']['console_capacity_failure_latched']=(
        snapshot['failure_code']=='console_capacity_exceeded' and snapshot['rejected_bytes']>0
        and snapshot['bytes_saved']==snapshot['durable_bytes'])
    result['checks']['console_no_overshoot']=(
        0<snapshot['bytes_saved']<=snapshot['capacity_bytes'] and log['truncated']
        and snapshot['bytes_seen']>snapshot['bytes_saved'])
    if not all(result['checks'][k] for k in
               ('console_capacity_failure_latched','console_no_overshoot')):
        raise RuntimeError('Console capacity predicate failed')
    result['console_pressure']={
        'seconds':time.monotonic()-started, 'stages':stages,
        'before':before,'after':after,'initial_storage':snapshots,
        'a_at_failure':snapshot,
        'a_retained_prefix_sha256':hashlib.sha256(os.pread(log['fd'],log['bytes_saved'],0)).hexdigest(),
        'b_before_stop_challenge':protocol.challenge(streams,b,deadline=deadline),
    }


def validate_archives(logs, pressure_mode):
    """An intentional quota failure never turns a truncated archive into complete evidence."""
    expected=1 if pressure_mode else 0
    failures=[log for log in logs if log.get('failure_code')]
    if len(failures)!=expected:
        return False
    if pressure_mode and (failures[0].get('failure_code')!='console_capacity_exceeded'
                          or not failures[0].get('truncated')):
        return False
    return all(log.get('saved') is True and log.get('archive_sha256')==log.get('source_sha256')
               and type(log.get('archive_sha256')) is str and len(log['archive_sha256'])==64
               and log.get('archive_reserved_bytes',0)>=log['limit_bytes']
               for log in logs)
