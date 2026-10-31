"""Guest-only OOM qualification fixture and host gates, never a memory policy.

Generating/validating code is safe on a development host. Execution is guarded
inside the fixture by the exact VM DMI UUID, root, RAM, swap and cgroup checks.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
import uuid

MIN_PRESSURE = 128 * 1024**2
MAX_ALLOCATION = 2 * 1024**3
# Exact observed Ubuntu cloud-init serial prefix; bounded ASCII fields only.
_CLOUD_INIT_PREFIX = re.compile(r'^\[[ \t]{0,12}[0-9]{1,10}\.[0-9]{1,9}\][ \t]{1,4}cloud-init\[[0-9]{1,10}\]: ')
REQUIRED_CHECKS = frozenset({
    'guest_oom_victim_verified', 'b_heartbeat_during_guest_pressure',
    'guest_pressure_identity_stable', 'host_oom_counters_unchanged',
    'guest_pressure_host_limits_verified', 'a_healthy_after_guest_oom',
})

# Kept as source text so importing this module cannot execute a pressure worker.
WORKER = r'''
import ctypes,json,os,signal,sys,time
from pathlib import Path
expected_uuid,comm=sys.argv[1:]
if os.geteuid()!=0 or Path('/sys/class/dmi/id/product_uuid').read_text().strip().lower()!=expected_uuid:
    raise RuntimeError('guest_identity_guard')
if ctypes.CDLL(None,use_errno=True).prctl(15,comm.encode(),0,0,0)!=0:
    raise RuntimeError('guest_comm_failed')
Path('/proc/self/oom_score_adj').write_text('1000')
if Path('/proc/self/comm').read_text().strip()!=comm or Path('/proc/self/oom_score_adj').read_text().strip()!='1000':
    raise RuntimeError('guest_worker_identity_failed')
started=int(Path('/proc/self/stat').read_text().rsplit(')',1)[1].split()[19])
print(json.dumps({'kind':'identity','pid':os.getpid(),'start_time':started,'comm':comm,'oom_score_adj':int(Path('/proc/self/oom_score_adj').read_text())}),flush=True)
if sys.stdin.buffer.read(1)!=b'G': raise RuntimeError('guest_start_gate')
signal.alarm(32)  # Independent Guest worker lifetime even if its parent stalls.
blocks=[]
for number in range(128):
    block=bytearray(16*1024**2)
    # Unique nonzero page contents avoid zero-page or same-page dedup shortcuts.
    for offset in range(0,len(block),4096):
        block[offset:offset+8]=(number*len(block)+offset+1).to_bytes(8,'little')
    blocks.append(block)
    print(json.dumps({'kind':'progress','allocated_bytes':len(blocks)*len(block)}),flush=True)
    time.sleep(.20)
raise RuntimeError('guest_allocation_ceiling_without_oom')
'''


def guest_program() -> str:
    """Dedicated interpreter payload; errors never disclose native exceptions."""
    return ('\nMEMORY_WORKER=' + repr(WORKER) + '\n'
            + 'try:\n    exec(' + repr(GUEST) + ')\n'
            + "except BaseException as failure:\n    if not globals().get('_error_emitted',False):\n        print('HARNIQO-MEM-ERROR '+C['nonce']+' '+str(globals().get('token') or 'bootstrap')+' '+json.dumps({'code':'guest_monitor_failed','kind':type(failure).__name__}),flush=True)\n    raise SystemExit(1)\n")


def supervised_guest_program() -> str:
    """Guest-only launcher, appended after the unchanged sealed probe."""
    return '\nMEMORY_CONTROLLER='+repr(guest_program())+'\n'+LAUNCHER


LAUNCHER = r'''
import hashlib,re,stat,sys
from pathlib import Path
if sys.platform!='linux' or os.geteuid()!=0 or Path('/sys/class/dmi/id/product_uuid').read_text().strip().lower()!=C['instance_uuid']:
    raise RuntimeError('guest_identity_guard')
if not re.fullmatch('[0-9a-f]{32}',C['nonce']): raise RuntimeError('guest_nonce_guard')
unit='harniqo-m0-memory-'+C['nonce']+'.service'
folder=Path('/run')/('harniqo-memory-'+C['nonce'])
deadline=time.monotonic()+190
log_fd=None; launched=False; state={}
def remaining(maximum):
    value=deadline-time.monotonic()
    if value<=0: raise TimeoutError('guest_supervisor_deadline')
    return min(maximum,value)
def drain_log():
    global pending,seen
    info=os.fstat(log_fd); current=log.lstat()
    if ((info.st_dev,info.st_ino)!=(current.st_dev,current.st_ino) or info.st_uid!=0
            or not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode)!=0o600
            or info.st_nlink!=1 or info.st_size>2*1024**2):
        raise RuntimeError('guest_supervisor_log_identity')
    while True:
        remaining(1)
        chunk=os.read(log_fd,4096)
        if not chunk: break
        seen+=len(chunk); pending+=chunk
        if seen>2*1024**2: raise RuntimeError('guest_supervisor_log_bound')
        while b'\n' in pending:
            line,pending=pending.split(b'\n',1)
            if len(line)>16384: raise RuntimeError('guest_supervisor_line_bound')
            print(line.decode('utf-8'),flush=True)
        if len(pending)>16384: raise RuntimeError('guest_supervisor_line_bound')
        remaining(1)
try:
    if Path('/run').is_symlink(): raise RuntimeError('guest_launcher_path_guard')
    folder.mkdir(mode=0o700)
    script=folder/'monitor.py'; log=folder/'monitor.log'
    source='import json,os,subprocess,time\nfrom pathlib import Path\nC=json.loads('+repr(json.dumps(C))+')\n'+MEMORY_CONTROLLER
    descriptor=os.open(script,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW|os.O_CLOEXEC,0o600)
    with os.fdopen(descriptor,'w') as output:
        output.write(source); output.flush(); os.fsync(output.fileno()); os.fchmod(output.fileno(),0o400)
    descriptor=os.open(log,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW|os.O_CLOEXEC,0o600)
    os.close(descriptor)
    log_fd=os.open(log,os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC)
    pending=b''; seen=0
    command=['/usr/bin/systemd-run','--quiet','--expand-environment=no',
        '--unit='+unit,'--service-type=exec','--property=DefaultDependencies=no',
        '--property=OOMPolicy=continue','--property=OOMScoreAdjust=-1000',
        '--property=MemoryAccounting=yes','--property=RuntimeMaxSec=185s',
        '--property=TimeoutStopSec=3s','--property=KillMode=control-group',
        '--property=RemainAfterExit=yes','--property=LimitFSIZE=2097152',
        '--property=StandardOutput=append:'+str(log),'--property=StandardError=append:'+str(log),
        '--','/usr/bin/python3',str(script)]
    launched=True  # A timed-out start is uncertain; cleanup still addresses only this nonce unit.
    subprocess.run(command,stdin=subprocess.DEVNULL,check=True,timeout=remaining(5))
    remaining(1)
    while True:
        remaining(1); drain_log()
        fields=('ActiveState','SubState','Result','ExecMainCode','ExecMainStatus','MainPID')
        query=subprocess.run(['/usr/bin/systemctl','show',unit,*['--property='+name for name in fields]],capture_output=True,text=True,check=True,timeout=remaining(2))
        remaining(1)
        if len(query.stdout.encode())>4096: raise RuntimeError('guest_supervisor_state_bound')
        state=dict(line.split('=',1) for line in query.stdout.splitlines() if '=' in line)
        if set(state)!=set(fields): raise RuntimeError('guest_supervisor_state_unknown')
        if state=={'ActiveState':'active','SubState':'exited','Result':'success','ExecMainCode':'1','ExecMainStatus':'0','MainPID':'0'}:
            drain_log()
            if pending: raise RuntimeError('guest_supervisor_partial_final_line')
            remaining(1)
            break
        if (state.get('ActiveState') not in {'active','activating'} or state.get('SubState')=='exited'
                or not re.fullmatch('[1-9][0-9]*',state.get('MainPID',''))):
            raise RuntimeError('guest_supervisor_abnormal_exit')
        time.sleep(min(.05,remaining(.05)))
except BaseException as failure:
    print('HARNIQO-MEM-ERROR '+C['nonce']+' bootstrap '+json.dumps({'code':'guest_supervisor_failed','kind':type(failure).__name__,'state':state}),flush=True)
    raise
finally:
    if launched:
        try:
            subprocess.run(['/usr/bin/systemctl','stop','--no-block',unit],check=True,timeout=2,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        except BaseException:
            print('HARNIQO-MEM-ERROR '+C['nonce']+' bootstrap '+json.dumps({'code':'guest_supervisor_failed','kind':'CleanupUnconfirmed'}),flush=True)
    if log_fd is not None: os.close(log_fd)
'''



GUEST = r'''
import hashlib,re,resource,select,signal,stat,sys
from pathlib import Path
# These checks precede every potentially dangerous Guest mutation/allocation.
if sys.platform!='linux' or os.geteuid()!=0 or Path('/sys/class/dmi/id/product_uuid').read_text().strip().lower()!=C['instance_uuid']:
    raise RuntimeError('guest_identity_guard')
boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip()
kernel_release=os.uname().release
os_release={k:v.strip().strip(chr(34)) for k,v in (line.split('=',1) for line in Path('/etc/os-release').read_text().splitlines() if '=' in line) if k in {'ID','VERSION_ID'}}
meminfo={line.split(':',1)[0]:int(line.split()[1])*1024 for line in Path('/proc/meminfo').read_text().splitlines() if line.startswith(('MemTotal:','SwapTotal:'))}
if not 700*1024**2<=meminfo['MemTotal']<=1100*1024**2 or meminfo['SwapTotal']!=0 or len(Path('/proc/swaps').read_text().splitlines())!=1:
    raise RuntimeError('guest_memory_shape_guard')
member=Path('/proc/self/cgroup').read_text().strip()
if not member.startswith('0::/') or '\n' in member: raise RuntimeError('guest_cgroup_guard')
group=Path('/sys/fs/cgroup')/member[4:]
while True:
    maximum=group/'memory.max'
    if maximum.exists() and maximum.read_text().strip()!='max': raise RuntimeError('guest_finite_cgroup_guard')
    if group==Path('/sys/fs/cgroup'): break
    group=group.parent
unit='harniqo-m0-memory-'+C['nonce']+'.service'
if member!='0::/system.slice/'+unit: raise RuntimeError('guest_monitor_unit_guard')
def monitor_facts():
    fresh_member=Path('/proc/self/cgroup').read_text().strip()
    if fresh_member!='0::/system.slice/'+unit: raise RuntimeError('guest_monitor_unit_guard')
    properties=('Id','MainPID','ControlGroup','OOMPolicy','DefaultDependencies','OOMScoreAdjust','RuntimeMaxUSec','TimeoutStopUSec','KillMode','MemoryMax','MemoryHigh','MemorySwapMax','StandardOutput','StandardError','RemainAfterExit')
    query=subprocess.run(['/usr/bin/systemctl','show',unit,*['--property='+name for name in properties]],capture_output=True,text=True,timeout=2,check=True)
    if len(query.stdout.encode())>4096: raise RuntimeError('guest_supervisor_property_bound')
    values=dict(line.split('=',1) for line in query.stdout.splitlines() if '=' in line)
    expected={'Id':unit,'MainPID':str(os.getpid()),'ControlGroup':fresh_member[3:],'OOMPolicy':'continue','DefaultDependencies':'no','OOMScoreAdjust':'-1000','KillMode':'control-group','MemoryMax':'infinity','MemoryHigh':'infinity','MemorySwapMax':'infinity','StandardOutput':'append','StandardError':'append','RemainAfterExit':'yes'}
    if any(values.get(key)!=value for key,value in expected.items()): raise RuntimeError('guest_supervisor_property_mismatch')
    runtime={'3min 5s':185000000,'185s':185000000,'185000000':185000000}.get(values.get('RuntimeMaxUSec'))
    stopping={'3s':3000000,'3000000':3000000}.get(values.get('TimeoutStopUSec'))
    own_group=Path('/sys/fs/cgroup')/fresh_member[4:]
    oom_group=(own_group/'memory.oom.group').read_text().strip()
    score=int(Path('/proc/self/oom_score_adj').read_text())
    if runtime!=185000000 or stopping!=3000000 or oom_group!='0' or score!=-1000: raise RuntimeError('guest_supervisor_policy_mismatch')
    log=Path('/run')/('harniqo-memory-'+C['nonce'])/'monitor.log'
    if log.is_symlink(): raise RuntimeError('guest_supervisor_log_identity')
    info=log.stat(); identity=(info.st_dev,info.st_ino)
    if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode)!=0o600 or info.st_uid!=0
            or info.st_nlink!=1 or info.st_size>2*1024**2
            or any((os.fstat(number).st_dev,os.fstat(number).st_ino)!=identity for number in (1,2))
            or resource.getrlimit(resource.RLIMIT_FSIZE)!=(2*1024**2,2*1024**2)):
        raise RuntimeError('guest_supervisor_log_identity')
    return {'pid':os.getpid(),'start_time':int(Path('/proc/self/stat').read_text().rsplit(')',1)[1].split()[19]),'unit':unit,'cgroup':fresh_member[3:],'score':score,'oom_group':0,'runtime_max_usec':runtime,'stop_timeout_usec':stopping,'properties':values,'log_identity':{'device':info.st_dev,'inode':info.st_ino},'log_fsize_limit_bytes':2*1024**2}
monitor=monitor_facts()
fd=os.open('/dev/virtio-ports/org.harniqo.m0',os.O_RDWR|os.O_NONBLOCK)
print('HARNIQO-RESOURCE-START '+C['nonce'],flush=True)
channel_pending=b''; child_pending=b''; child_bytes=0; child=None; token=None
started_payload=None; allocated=0; kernel_fd=None; kernel_bytes=0; victim_line=None; completed=False
until=time.monotonic()+120; pressure_deadline=None; exit_seen=None; timed_out=False

def vmstat_oom():
    return int(next(line.split()[1] for line in Path('/proc/vmstat').read_text().splitlines() if line.startswith('oom_kill ')))

def emit(kind,payload):
    print('HARNIQO-MEM-'+kind+' '+C['nonce']+' '+str(token or 'bootstrap')+' '+json.dumps(payload,separators=(',',':')),flush=True)

_error_emitted=False
def monitor_terminated(signum,frame):
    global _error_emitted
    _error_emitted=True
    emit('ERROR',{'code':'guest_monitor_terminated','signal':signum,'monitor_pid':os.getpid()})
    raise RuntimeError('guest_monitor_terminated')
signal.signal(signal.SIGTERM,monitor_terminated)

try:
    while time.monotonic()<until:
        watched=[fd]+([child.stdout.fileno()] if child is not None and child.stdout is not None and not child.stdout.closed else [])
        ready=select.select(watched,[],[],.05)[0]
        if fd in ready:
            chunk=os.read(fd,1024)
            channel_pending+=chunk
            if len(channel_pending)>4096: raise RuntimeError('guest_control_bound')
            while b'\n' in channel_pending:
                line,channel_pending=channel_pending.split(b'\n',1)
                if re.fullmatch(b'[0-9a-f]{40}',line):
                    print('HARNIQO-HEARTBEAT '+C['nonce']+' '+line.decode(),flush=True)
                elif line.startswith(('MEM '+C['nonce']+' ').encode()) and child is None:
                    token=line.decode().split(' ')[2]
                    if not re.fullmatch('[0-9a-f]{40}',token): raise RuntimeError('guest_token_shape')
                    kernel_fd=os.open('/dev/kmsg',os.O_RDONLY|os.O_NONBLOCK|os.O_CLOEXEC)
                    os.lseek(kernel_fd,0,os.SEEK_END)  # Only new kernel records can qualify.
                    oom_before=vmstat_oom(); comm='hnq-'+token[:10]
                    old_score=Path('/proc/self/oom_score_adj').read_text().strip()
                    Path('/proc/self/oom_score_adj').write_text('-1000')
                    child=subprocess.Popen([sys.executable,'-c',MEMORY_WORKER,C['instance_uuid'],comm],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,bufsize=0,close_fds=True)
                    os.set_blocking(child.stdout.fileno(),False)
                    pressure_deadline=min(until,time.monotonic()+35)
                elif token is not None and line.startswith(('LIVE '+C['nonce']+' '+token+' ').encode()):
                    challenge=line.decode().split(' ')[3]
                    if re.fullmatch('[0-9a-f]{40}',challenge) and started_payload is not None and child.poll() is None:
                        emit('LIVE',{'challenge':challenge,'pid':child.pid,'allocated_bytes':allocated,'boot_id':boot_id})
        if child is not None:
            if child.stdout is not None and not child.stdout.closed and child.stdout.fileno() in ready:
                part=os.read(child.stdout.fileno(),4096)
                if not part:
                    child.stdout.close()
                else:
                    child_pending+=part; child_bytes+=len(part)
                    if child_bytes>16384 or len(child_pending)>4096: raise RuntimeError('guest_worker_output_bound')
                    while b'\n' in child_pending:
                        line,child_pending=child_pending.split(b'\n',1); item=json.loads(line)
                        if item.get('kind')=='identity':
                            identity=item
                            if item['pid']!=child.pid or item['comm']!=comm or item['start_time']!=int(Path('/proc/'+str(child.pid)+'/stat').read_text().rsplit(')',1)[1].split()[19]):
                                raise RuntimeError('guest_worker_binding')
                            child.stdin.write(b'G'); child.stdin.close()
                        elif item.get('kind')=='progress':
                            allocated=item['allocated_bytes']
                            if allocated%(128*1024**2)==0: emit('PROGRESS',{'allocated_bytes':allocated})
                            if started_payload is None and allocated>=128*1024**2:
                                started_payload={'monitor':monitor,'kernel_release':kernel_release,'os_release':os_release,'worker_oom_score_adj':identity['oom_score_adj'],'boot_id':boot_id,'pid':identity['pid'],'start_time':identity['start_time'],'comm':comm,'allocated_bytes':allocated,'oom_before':oom_before,'memtotal_bytes':meminfo['MemTotal'],'swap_total_bytes':0,'allocation_ceiling_bytes':2*1024**3,'kernel_cursor_new_only':True,'guest_cgroup_unlimited':True}
                                emit('START',started_payload)
            if pressure_deadline is not None and time.monotonic()>=pressure_deadline:
                timed_out=True
                # A delayed observation cannot prove that an earlier exit met its deadline.
                raise RuntimeError('guest_pressure_deadline')
            if child.poll() is not None and exit_seen is None:
                exit_seen=time.monotonic()
                emit('CHILD-EXIT',{'pid':child.pid,'returncode':child.returncode,'monitor_pid':os.getpid(),'boot_id':boot_id})
            if kernel_fd is not None:
                while True:
                    if time.monotonic()>=pressure_deadline: raise RuntimeError('guest_pressure_deadline')
                    try: record=os.read(kernel_fd,8192)
                    except BlockingIOError: break
                    if not record: raise RuntimeError('guest_kernel_log_closed')
                    kernel_bytes+=len(record)
                    if kernel_bytes>256*1024: raise RuntimeError('guest_kernel_log_bound')
                    message=record.decode('utf-8','replace').partition(';')[2].strip()
                    if message.startswith('Out of memory: Killed process '+str(child.pid)+' ('+comm+') '):
                        if victim_line is not None: raise RuntimeError('duplicate_guest_oom_victim')
                        victim_line=message
            if exit_seen is not None and (victim_line is not None or time.monotonic()-exit_seen>2):
                emit('RESULT',{'monitor':monitor_facts(),'kernel_release':os.uname().release,'os_release':os_release,'worker_oom_score_adj':identity['oom_score_adj'],'boot_id':Path('/proc/sys/kernel/random/boot_id').read_text().strip(),'pid':child.pid,'start_time':identity['start_time'],'comm':comm,'returncode':child.returncode,'oom_before':oom_before,'oom_after':vmstat_oom(),'allocated_bytes':allocated,'timed_out':timed_out,'kernel_victim_matched':victim_line is not None,'kernel_victim_line':victim_line,'kernel_victim_sha256':hashlib.sha256(victim_line.encode()).hexdigest() if victim_line else None,'worker_output_bytes':child_bytes})
                Path('/proc/self/oom_score_adj').write_text(old_score)
                os.close(kernel_fd); kernel_fd=None
                if child.stdout is not None and not child.stdout.closed: child.stdout.close()
                # Continue servicing new health challenges after the real OOM.
                pressure_deadline=None; exit_seen=None
                child=None; started_payload=None; completed=True
                # Do not accept a second memory pressure command in this VM.
                break
    if not completed: raise RuntimeError('guest_controller_deadline')
    # Post-OOM health phase has its own bounded lifetime.
    end=time.monotonic()+60
    while time.monotonic()<end:
        if not select.select([fd],[],[],.1)[0]: continue
        channel_pending+=os.read(fd,1024)
        if len(channel_pending)>4096: raise RuntimeError('guest_control_bound')
        while b'\n' in channel_pending:
            line,channel_pending=channel_pending.split(b'\n',1)
            if re.fullmatch(b'[0-9a-f]{40}',line):
                print('HARNIQO-HEARTBEAT '+C['nonce']+' '+line.decode(),flush=True)
            elif line.startswith(('HEALTH '+C['nonce']+' '+token+' ').encode()):
                value=line.decode().split(' ')[3]
                if re.fullmatch('[0-9a-f]{40}',value):
                    emit('HEALTH',{'challenge':value,'boot_id':Path('/proc/sys/kernel/random/boot_id').read_text().strip()})
finally:
    if child is not None and child.poll() is None:
        child.kill(); child.wait(timeout=3)
    if kernel_fd is not None: os.close(kernel_fd)
    os.close(fd)
print('HARNIQO-RESOURCE-END '+C['nonce'],flush=True)
'''


def _integer(value):
    return type(value) is int and value >= 0



def validate_monitor(monitor):
    if not isinstance(monitor,dict): raise RuntimeError('Guest monitor evidence missing')
    unit=monitor.get('unit')
    if not all(type(monitor.get(k)) is int for k in ('pid','start_time','score','oom_group','runtime_max_usec','stop_timeout_usec')):
        raise RuntimeError('Guest monitor scalar types invalid')
    if (not _integer(monitor.get('pid')) or monitor['pid']<=1
            or not _integer(monitor.get('start_time')) or monitor['start_time']==0
            or type(unit) is not str or not re.fullmatch(r'harniqo-m0-memory-[A-Za-z0-9-]{1,64}\.service',unit)
            or monitor.get('cgroup')!='/system.slice/'+unit or monitor.get('score')!=-1000
            or monitor.get('oom_group')!=0 or monitor.get('runtime_max_usec')!=185000000
            or monitor.get('stop_timeout_usec')!=3000000):
        raise RuntimeError('Guest monitor identity or bounds invalid')
    log=monitor.get('log_identity')
    if (not isinstance(log,dict) or not all(_integer(log.get(k)) for k in ('device','inode'))
            or log['inode']==0 or monitor.get('log_fsize_limit_bytes')!=2*1024**2):
        raise RuntimeError('Guest monitor log binding invalid')
    values=monitor.get('properties')
    expected={'Id':unit,'MainPID':str(monitor['pid']),'ControlGroup':monitor['cgroup'],'OOMPolicy':'continue','DefaultDependencies':'no','OOMScoreAdjust':'-1000','KillMode':'control-group','MemoryMax':'infinity','MemoryHigh':'infinity','MemorySwapMax':'infinity','StandardOutput':'append','StandardError':'append','RemainAfterExit':'yes'}
    if (not isinstance(values,dict) or any(values.get(k)!=v for k,v in expected.items())
            or values.get('RuntimeMaxUSec') not in {'3min 5s','185s','185000000'}
            or values.get('TimeoutStopUSec') not in {'3s','3000000'}):
        raise RuntimeError('Guest monitor unit policy invalid')

def validate_guest_evidence(start: dict, finish: dict) -> None:
    """Reject unbound, non-kernel, timeout or allocation-ceiling failures."""
    if not all(_integer(start.get(k)) for k in ('pid', 'start_time', 'allocated_bytes', 'oom_before', 'memtotal_bytes', 'swap_total_bytes', 'allocation_ceiling_bytes')):
        raise RuntimeError('Guest memory start evidence invalid')
    if (start['pid'] <= 1 or start['start_time'] == 0 or start['allocated_bytes'] < MIN_PRESSURE
            or not 700*1024**2 <= start['memtotal_bytes'] <= 1100*1024**2
            or start['swap_total_bytes'] != 0 or start['allocation_ceiling_bytes'] != MAX_ALLOCATION
            or start.get('kernel_cursor_new_only') is not True or start.get('guest_cgroup_unlimited') is not True
            or start.get('worker_oom_score_adj') != 1000
            or type(start.get('kernel_release')) is not str or not start['kernel_release']
            or start.get('os_release') != {'ID':'ubuntu','VERSION_ID':'24.04'}
            or not re.fullmatch(r'hnq-[0-9a-f]{10}', str(start.get('comm')))):
        raise RuntimeError('Guest memory shape evidence invalid')
    if type(start.get('boot_id')) is not str or not re.fullmatch('[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}',start['boot_id']):
        raise RuntimeError('Guest boot identity invalid')
    validate_monitor(start.get('monitor'))
    if any(finish.get(k) != start[k] for k in ('monitor','pid', 'start_time', 'comm', 'oom_before', 'boot_id', 'kernel_release', 'os_release', 'worker_oom_score_adj')):
        raise RuntimeError('Guest OOM victim identity changed')
    if (finish.get('returncode') != -9 or finish.get('timed_out') is not False
            or not _integer(finish.get('oom_after')) or finish['oom_after'] <= start['oom_before']
            or not _integer(finish.get('allocated_bytes')) or not start['allocated_bytes'] <= finish['allocated_bytes'] < MAX_ALLOCATION
            or finish.get('kernel_victim_matched') is not True):
        raise RuntimeError('Guest kernel OOM not proven')
    line = finish.get('kernel_victim_line')
    prefix = 'Out of memory: Killed process ' + str(start['pid']) + ' (' + start['comm'] + ') '
    if (type(line) is not str or len(line) > 2048 or not line.startswith(prefix)
            or finish.get('kernel_victim_sha256') != hashlib.sha256(line.encode()).hexdigest()):
        raise RuntimeError('Guest kernel victim log not proven')


def console_text(streams, instance):
    log = streams.logs[instance['uuid']]
    if log['bytes_saved'] > 2*1024**2:
        raise RuntimeError('Console evidence bound')
    text=os.pread(log['fd'], log['bytes_saved'], 0).decode('utf-8', 'replace')
    prefix='HARNIQO-MEM-ERROR '+instance['nonce']+' '
    for line in _complete_lines(text):
        if not line.startswith(prefix): continue
        token,separator,payload=line[len(prefix):].partition(' ')
        if not separator or (token!='bootstrap' and not re.fullmatch('[0-9a-f]{40}',token)):
            raise RuntimeError('Guest monitor error frame invalid')
        data=json.loads(payload)
        code=data.get('code') if isinstance(data,dict) else None
        if code not in {'guest_monitor_failed','guest_supervisor_failed','guest_monitor_terminated'}:
            raise RuntimeError('Guest monitor error frame invalid')
        raise RuntimeError('Guest monitor reported '+code)
    return text


def _events(text, kind, nonce, token):
    prefix = 'HARNIQO-MEM-' + kind + ' ' + nonce + ' ' + token + ' '
    # Console socket reads can split a JSON record at any byte. Only complete
    # newline-delimited records are evidence; a partial suffix remains pending.
    lines = _complete_lines(text)
    return [json.loads(line[len(prefix):]) for line in lines if line.startswith(prefix)]


def _before_deadline(deadline, message):
    now = time.monotonic()
    if now >= deadline:
        raise TimeoutError(message)
    return now


def _wait_until(predicate, streams, deadline, message):
    """Only accept evidence whose complete observation finished in its window."""
    while True:
        _before_deadline(deadline, message)
        observed = predicate()
        now = _before_deadline(deadline, message)
        if observed:
            return observed
        streams._poll(min(.05, deadline-now))


def _complete_lines(text):
    # Protocol framing is LF/CRLF, not Python's wider Unicode splitlines set.
    # Strip at most one known prefix, never search for an embedded marker.
    lines=[]
    for line in text.split('\n')[:-1]:
        if line.endswith('\r'): line=line[:-1]
        prefix=_CLOUD_INIT_PREFIX.match(line)
        lines.append(line[prefix.end():] if prefix else line)
    return lines


def _send(channel, payload, streams, timeout=2, *, deadline=None):
    """Bound the whole write, including successful but late syscall completion."""
    expires = time.monotonic()+timeout
    if deadline is not None:
        expires = min(expires, deadline)
    pending = memoryview(payload.encode('ascii'))
    while pending:
        _before_deadline(expires, 'Guest control write deadline')
        try:
            count = channel.send(pending)
            if count == 0: raise RuntimeError('Guest control channel closed')
            _before_deadline(expires, 'Guest control write deadline')
            pending = pending[count:]
        except BlockingIOError:
            now = _before_deadline(expires, 'Guest control write deadline')
            streams._poll(min(.02, expires-now))


def challenge(streams, instance, *, timeout=3, deadline=None):
    expires = time.monotonic()+timeout
    if deadline is not None:
        expires = min(expires, deadline)
    value = uuid.uuid4().hex + uuid.uuid4().hex[:8]
    _send(streams.report_channels[instance['uuid']], value+'\n', streams, deadline=expires)
    marker = 'HARNIQO-HEARTBEAT '+instance['nonce']+' '+value
    _wait_until(lambda: marker in _complete_lines(console_text(streams, instance)), streams,
                expires, 'Fresh Guest health challenge deadline')
    return value


def qualification_limits():
    """Fresh copy of the fixed fault-qualification budget, separate from VM policy."""
    return {
        'virtual_machines': 2, 'simultaneous_memory_workers': 1,
        'max_allocation_bytes': MAX_ALLOCATION, 'worker_lifetime_seconds': 32,
        'guest_pressure_seconds': 35, 'guest_controller_seconds': 120,
        'host_pressure_seconds': 40, 'readiness_seconds': 10,
        'challenge_seconds_including_write': 3, 'control_write_seconds': 2,
        'post_oom_health_seconds': 60, 'guest_unit_runtime_seconds':185,
        'guest_unit_stop_seconds':3, 'guest_supervisor_timeout_seconds':190,
        'guest_unit_property_query_seconds':2, 'guest_unit_property_bytes':4096,
        'guest_supervisor_start_seconds':5,'guest_supervisor_cleanup_seconds':2,
        'guest_supervisor_log_bytes':2*1024**2,'guest_supervisor_line_bytes':16384,
        'guest_supervisor_read_chunk_bytes':4096,'guest_supervisor_poll_milliseconds':50, 'worker_stdout_bytes': 16384,
        'worker_line_bytes': 4096, 'control_pending_bytes': 4096,
        'kernel_log_bytes': 256*1024, 'kernel_record_bytes': 8192,
        'kernel_victim_line_bytes': 2048,
        'console_bytes_per_vm': 2*1024**2,
    }


def run_pressure(broker, instances, streams, result):
    a, b = instances
    for item in instances:
        if item['uuid'] not in streams.report_channels:
            raise RuntimeError('Memory control channel unavailable')
    ready_deadline = time.monotonic()+10
    _wait_until(lambda: all('HARNIQO-RESOURCE-START '+i['nonce'] in _complete_lines(console_text(streams,i))
                           for i in instances), streams, ready_deadline, 'Memory fixture readiness deadline')
    before = [broker.resource_limits(i['uuid']) for i in instances]
    token = uuid.uuid4().hex + uuid.uuid4().hex[:8]
    pressure_started = time.monotonic()
    deadline = pressure_started+40
    _send(streams.report_channels[a['uuid']], 'MEM '+a['nonce']+' '+token+'\n', streams, deadline=deadline)
    def wait_events(kind):
        def read():
            events = _events(console_text(streams, a), kind, a['nonce'], token)
            if len(events) > 1: raise RuntimeError('Duplicate Guest memory evidence')
            return events
        return _wait_until(read, streams, deadline, 'Guest memory evidence deadline')[0]
    start = wait_events('START')
    if start.get('monitor',{}).get('unit')!='harniqo-m0-memory-'+a['nonce']+'.service':
        raise RuntimeError('Guest memory monitor run binding invalid')
    if start.get('comm') != 'hnq-'+token[:10] or start.get('boot_id') != result['guests'][a['uuid']]['boot_id']:
        raise RuntimeError('Guest memory run binding invalid')
    live = []
    result['guest_memory_pressure'] = {'start':start,'live_acknowledgements':live,'before':before}
    for threshold in (128*1024**2,256*1024**2,384*1024**2):
        _wait_until(lambda: any(_integer(x.get('allocated_bytes')) and x['allocated_bytes']>=threshold
                               for x in _events(console_text(streams,a),'PROGRESS',a['nonce'],token)),
                    streams, deadline, 'Guest allocation progress deadline')
        value = challenge(streams, b, deadline=deadline)
        # A live acknowledgement is causally after B's received fresh reply.
        _send(streams.report_channels[a['uuid']], 'LIVE '+a['nonce']+' '+token+' '+value+'\n', streams, deadline=deadline)
        def read_live():
            matches = [x for x in _events(console_text(streams, a), 'LIVE', a['nonce'], token) if x.get('challenge') == value]
            if len(matches) > 1: raise RuntimeError('Duplicate memory live acknowledgement')
            return matches
        item = _wait_until(read_live, streams, deadline, 'Guest live acknowledgement deadline')[0]
        if item.get('pid') != start.get('pid') or not _integer(item.get('allocated_bytes')) or not threshold <= item['allocated_bytes'] < MAX_ALLOCATION or item.get('boot_id') != start.get('boot_id') or (live and item['allocated_bytes'] <= live[-1]['allocated_bytes']):
            raise RuntimeError('Memory live acknowledgement invalid')
        live.append(item)
    finish = wait_events('RESULT')
    result['guest_memory_pressure']['finish'] = finish
    validate_guest_evidence(start, finish)
    after = [broker.resource_limits(i['uuid']) for i in instances]
    result['guest_memory_pressure']['after'] = after
    identity = lambda x: tuple(x.get(k) for k in ('pid','process_start_time','cgroup_path','memory_cgroup_path','swap_cgroup_path'))
    stable = all(identity(x) == identity(y) for x, y in zip(before, after))
    events_stable = all(x['memory_events'][k] == y['memory_events'][k] for x, y in zip(before, after) for k in ('oom','oom_kill'))
    bounded = all(0 < x['memory_current_bytes'] <= x['memory_peak_bytes'] < x['memory_max_bytes'] == 2*1024**3 and x['swap_max_bytes'] == 2*1024**3 for x in before+after)
    health_value=uuid.uuid4().hex+uuid.uuid4().hex[:8]
    _send(streams.report_channels[a['uuid']], 'HEALTH '+a['nonce']+' '+token+' '+health_value+'\n', streams, deadline=deadline)
    health=wait_events('HEALTH')
    healthy=health.get('challenge')==health_value and health.get('boot_id')==start['boot_id']
    result['guest_memory_pressure']['post_oom_health']=health
    result['guest_memory_pressure']['b_after_oom_challenge']=challenge(streams,b,deadline=deadline)
    finished = _before_deadline(deadline, 'Guest memory completion deadline')
    result['guest_memory_pressure']['duration_seconds']=finished-pressure_started
    result['checks'].update(guest_oom_victim_verified=True, b_heartbeat_during_guest_pressure=len(live)==3,
        guest_pressure_identity_stable=stable, host_oom_counters_unchanged=events_stable,
        guest_pressure_host_limits_verified=bounded, a_healthy_after_guest_oom=healthy)
    if not all(result['checks'][name] for name in REQUIRED_CHECKS):
        raise RuntimeError('Guest memory host predicate failed')
