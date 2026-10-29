#!/usr/bin/env python3
"""Real KVM NIC-filter qualification with fixed TLS canaries, NOT a production proxy.

Creates only owned transient libvirt resources. No forwarding, DNS, DHCP, model
calls, provider credentials, sudo, Docker execution or global firewall changes.
The trusted short-lived fixture process runs as the operator; production service
identities, egress, host quotas and lease/watchdog remain explicitly unqualified.
"""
from __future__ import annotations

import argparse
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import platform
import re
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import time
import uuid
import xml.etree.ElementTree as ET

import qualify_kvm as kvm
from kvm_host_policy import OwnedInputPolicy

PORT, DENIED_PORT = 10018, 10019
UNVERIFIED = ["production selective upstream/model egress", "provider credential isolation",
              "production service identities", "host firewall ruleset audit",
              "host CPU/memory/disk exhaustion", "lease/fencing/watchdog", "disaster recovery"]


def object_inventory() -> dict:
    networks = {}
    for identity in kvm.virsh("net-list", "--all", "--uuid").splitlines():
        identity = str(uuid.UUID(identity.strip()))
        networks[identity] = {"xml": kvm.virsh("net-dumpxml", identity),
                              "info": kvm.virsh("net-info", identity)}
    filters = {}
    for line in kvm.virsh("nwfilter-list").splitlines():
        fields = line.split()
        if fields and re.fullmatch(r"[a-f0-9-]{36}", fields[0]):
            identity = str(uuid.UUID(fields[0]))
            filters[identity] = kvm.virsh("nwfilter-dumpxml", identity)
    return {"domains": kvm.inventory(), "networks": networks, "filters": filters}


def host_networks() -> list:
    """Read only addresses/routes and Docker IPAM, never container commands/env."""
    result = []
    for family in ["-4", "-6"]:
        for item in json.loads(kvm.run(["ip", "-j", family, "route", "show", "table", "all"])):
            address = item.get("dst")
            if address and address != "default":
                result.append(ipaddress.ip_network(address, strict=False))
    for interface in json.loads(kvm.run(["ip", "-j", "address", "show"])):
        for address in interface.get("addr_info", []):
            result.append(ipaddress.ip_network(f"{address['local']}/{address['prefixlen']}", strict=False))
    ids = kvm.run(["docker", "network", "ls", "--quiet"]).splitlines()
    for identity in ids:
        ipam = json.loads(kvm.run(["docker", "network", "inspect", "--format", "{{json .IPAM.Config}}", identity]))
        for item in ipam or []:
            if item.get("Subnet"):
                result.append(ipaddress.ip_network(item["Subnet"], strict=False))
    return result


def select_subnet(occupied: list) -> ipaddress.IPv4Network:
    for subnet in ipaddress.ip_network("172.29.240.0/20").subnets(new_prefix=28):
        if not any(other.version == 4 and subnet.overlaps(other) for other in occupied):
            return subnet
    raise RuntimeError("No non-overlapping task subnet; refusing to alter existing networks")


def network_xml(owner: dict, subnet) -> str:
    root = ET.Element("network")
    ET.SubElement(root, "name").text = owner["name"]
    ET.SubElement(root, "uuid").text = owner["uuid"]
    ET.SubElement(root, "bridge", name=owner["bridge"], stp="off", delay="0")
    ET.SubElement(root, "port", isolated="yes")
    ET.SubElement(root, "dns", enable="no")
    ET.SubElement(root, "ip", address=str(subnet[1]), prefix=str(subnet.prefixlen))
    return ET.tostring(root, encoding="unicode")


def filter_xml(instance: dict, gateway: str) -> str:
    root = ET.Element("filter", name=instance["filter_name"], chain="root")
    ET.SubElement(root, "uuid").text = instance["filter_uuid"]
    def rule(action, direction, priority, protocol=None, **attrs):
        node = ET.SubElement(root, "rule", action=action, direction=direction, priority=str(priority))
        if protocol: ET.SubElement(node, protocol, **attrs)
    rule("drop", "out", 100, "mac", match="no", srcmacaddr=instance["mac"])
    for opcode in ["Request", "Reply"]:
        rule("accept", "out", 200, "arp", opcode=opcode, srcmacaddr=instance["mac"],
             arpsrcmacaddr=instance["mac"], arpsrcipaddr=instance["ip"], arpdstipaddr=gateway)
        rule("accept", "in", 200, "arp", opcode=opcode, arpsrcipaddr=gateway,
             arpdstipaddr=instance["ip"])
    # Ethernet IPv4 passes to the independently default-deny iptables layer.
    rule("accept", "inout", 300, "mac", protocolid="ipv4")
    rule("accept", "out", 500, "tcp", srcmacaddr=instance["mac"], srcipaddr=instance["ip"],
         dstipaddr=gateway, dstportstart=str(PORT), dstportend=str(PORT), state="NEW,ESTABLISHED")
    rule("accept", "in", 500, "tcp", srcipaddr=gateway, dstipaddr=instance["ip"],
         srcportstart=str(PORT), srcportend=str(PORT), state="ESTABLISHED")
    rule("drop", "inout", 900, "all")
    rule("drop", "inout", 1000)  # Other EtherTypes, including IPv6/VLAN.
    return ET.tostring(root, encoding="unicode")


def require_identity(xml: str, owner: dict, *, filter_object=False) -> None:
    tree = ET.fromstring(xml)
    name = tree.get("name") if filter_object else tree.findtext("name")
    if tree.findtext("uuid") != owner["uuid"] or name != owner["name"]:
        raise RuntimeError("Resource identity changed; refusing mutation")
    if owner.get("bridge") and tree.find("bridge").get("name") != owner["bridge"]:
        raise RuntimeError("Network bridge identity changed; refusing mutation")


def validate_reports(received: dict, instances: list, host_address_count: int) -> None:
    required = {"no_initial_default_route", "kvm", "no_host_sockets", "peer_canary_listening",
                "trusted_tls_positive", "host_listening_denied_port", "peer_listening_canary",
                "metadata_ipv4", "external_ipv4_direct", "source_ip_spoof_rejected",
                "allowed_after_negatives", "peer_no_nonloopback_hits", "peer_listener_positive_seen"}
    required.update("host_address_"+str(i) for i in range(host_address_count))
    if set(received) != {item["uuid"] for item in instances}:
        raise RuntimeError("Guest network report set is incomplete")
    for guest in received.values():
        checks = guest.get("checks", {})
        if guest.get("passed") is not True or any(checks.get(key) is not True for key in required):
            raise RuntimeError("Guest network checks failed or positive control is missing")


CANARY = r'''#!/usr/bin/python3
import json,os,resource,selectors,signal,socket,ssl,sys,time
from pathlib import Path
c=json.loads(Path(sys.argv[1]).read_text())
resource.setrlimit(resource.RLIMIT_CORE,(0,0))
resource.setrlimit(resource.RLIMIT_NOFILE,(32,32))
resource.setrlimit(resource.RLIMIT_AS,(256*1024**2,256*1024**2))
resource.setrlimit(resource.RLIMIT_CPU,(30,30))
resource.setrlimit(resource.RLIMIT_FSIZE,(1024**2,1024**2))
ctx=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
ctx.minimum_version=ssl.TLSVersion.TLSv1_2
ctx.load_cert_chain(c['cert'],c['key'])
sel=selectors.DefaultSelector();listeners=[];ready=set();finished=set();running=True
def stop(*_):
 global running
 running=False
signal.signal(signal.SIGTERM,stop)
log=open(c['audit'],'x',buffering=1);os.chmod(c['audit'],0o600)
try:
 for port in [10018,10019]:
  listener=socket.socket();listener.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
  listener.bind((c['gateway'],port));listener.listen(8)
  listener.setblocking(False);sel.register(listener,selectors.EVENT_READ,port);listeners.append(listener)
 Path(c['ready']).write_text('ready\n')
 deadline=time.monotonic()+c['seconds']
 while running and time.monotonic()<deadline:
  for key,_ in sel.select(.25):
   conn,address=key.fileobj.accept();conn.settimeout(2)
   entry={'at':time.time(),'port':key.data,'source':address[0],'accepted':True,'valid_request':False}
   try:
    with ctx.wrap_socket(conn,server_side=True) as stream:
     request=b''
     while b'\r\n\r\n' not in request and len(request)<4096:
      part=stream.recv(min(1024,4096-len(request)))
      if not part:break
      request+=part
     line=request.split(b'\r\n',1)[0]
     host_health=address[0]==c['gateway'] and line==b'GET /health HTTP/1.1'
     guest=c['guests'].get(address[0])
     done=guest and line==('GET /'+guest['nonce']+'/done HTTP/1.1').encode()
     valid=key.data==10018 and guest and (line==('GET /'+guest['nonce']+' HTTP/1.1').encode() or done)
     if valid:ready.add(address[0])
     if valid and done:finished.add(address[0])
     entry['valid_request']=bool(valid or host_health)
     payload=json.dumps({'fixture':'harniqo-fixed-tls-canary','nonce':guest['nonce'] if valid else None,'ready':len(ready),'finished':len(finished)}).encode()
     if not entry['valid_request']:payload=b'{"error":"forbidden"}'
     status=b'200 OK' if entry['valid_request'] else b'403 Forbidden'
     stream.sendall(b'HTTP/1.1 '+status+b'\r\nConnection: close\r\nContent-Type: application/json\r\nContent-Length: '+str(len(payload)).encode()+b'\r\n\r\n'+payload)
   except Exception as error:entry['error']=type(error).__name__
   finally:
    conn.close();log.write(json.dumps(entry)+'\n')
finally:
 for listener in listeners:listener.close()
 sel.close();log.flush();os.fsync(log.fileno());log.close()
'''


GUEST = r'''#!/usr/bin/python3
import json,os,socket,ssl,subprocess,threading,time
from pathlib import Path
C=json.loads(CONFIG_JSON)
report={'schema_version':1,'instance_uuid':C['uuid'],'nonce':C['nonce'],'checks':{},'probes':{}}
stop=threading.Event();hits=[];listener=None
def cmd(*args):
 return subprocess.run(args,check=True,capture_output=True,text=True,timeout=10).stdout
def blocked(name,address,port):
 started=time.monotonic()
 try:
  with socket.create_connection((address,port),timeout=2):pass
  result={'blocked':False,'error':None}
 except OSError as error:result={'blocked':True,'error':type(error).__name__,'errno':error.errno}
 result['seconds']=time.monotonic()-started;report['probes'][name]=result
 report['checks'][name]=result['blocked']
def positive(done=False):
 ctx=ssl.create_default_context(cafile='/etc/harniqo-canary.pem')
 with socket.create_connection((C['gateway'],10018),timeout=3) as conn:
  with ctx.wrap_socket(conn,server_hostname=C['gateway']) as stream:
   stream.sendall(('GET /'+C['nonce']+('/done' if done else '')+' HTTP/1.1\r\nHost: '+C['gateway']+'\r\nConnection: close\r\n\r\n').encode())
   data=b''
   while len(data)<8192:
    part=stream.recv(4096)
    if not part:break
    data+=part
 if not data.startswith(b'HTTP/1.1 200 '):raise RuntimeError('Canary did not accept identity')
 body=json.loads(data.split(b'\r\n\r\n',1)[1])
 if body.get('nonce')!=C['nonce']:raise RuntimeError('Wrong canary nonce')
 return body
try:
 iface=next(p.name for p in Path('/sys/class/net').iterdir() if p.name!='lo')
 report['initial_routes']=cmd('ip','-j','route','show','default')
 report['checks']['no_initial_default_route']=json.loads(report['initial_routes'])==[]
 report['checks']['kvm']=cmd('systemd-detect-virt','--vm').strip()=='kvm'
 report['checks']['no_host_sockets']=not any(Path(p).exists() for p in ['/run/libvirt/libvirt-sock','/run/docker.sock'])
 listener=socket.socket();listener.bind(('0.0.0.0',10019));listener.listen(8);listener.settimeout(.2)
 def serve():
  while not stop.is_set():
   try:
    conn,address=listener.accept();hits.append(address[0]);conn.close()
   except socket.timeout:pass
   except OSError:
    if not stop.is_set():raise
 worker=threading.Thread(target=serve,daemon=True);worker.start()
 with socket.create_connection(('127.0.0.1',10019),timeout=2):pass
 report['checks']['peer_canary_listening']=True
 deadline=time.monotonic()+90
 while positive()['ready']<2:
  if time.monotonic()>deadline:raise TimeoutError('Peer readiness barrier')
  time.sleep(1)
 report['checks']['trusted_tls_positive']=True
 report['barrier_passed_at']=time.time()
 cmd('ip','route','replace','default','via',C['gateway'],'dev',iface)
 report['attacker_default_route']=json.loads(cmd('ip','-j','route','show','default'))
 blocked('host_listening_denied_port',C['gateway'],10019)
 blocked('peer_listening_canary',C['peer_ip'],10019)
 blocked('metadata_ipv4','169.254.169.254',80)
 blocked('external_ipv4_direct','1.1.1.1',443)
 for idx,address in enumerate(C['host_addresses']):blocked('host_address_'+str(idx),address,10018)
 # Bind an additional source address; host policy must reject it, even on the allowed route.
 spoof=C['spoof_ip']
 cmd('ip','addr','add',spoof+'/28','dev',iface)
 try:
  probe=socket.socket();probe.settimeout(2);probe.bind((spoof,0))
  try:probe.connect((C['gateway'],10018));report['checks']['source_ip_spoof_rejected']=False
  except OSError:report['checks']['source_ip_spoof_rejected']=True
  finally:probe.close()
 finally:cmd('ip','addr','del',spoof+'/28','dev',iface)
 report['checks']['allowed_after_negatives']=positive()['nonce']==C['nonce']
 # Keep both positive-control listeners alive until both negative suites finish.
 deadline=time.monotonic()+90
 while positive(done=True)['finished']<2:
  if time.monotonic()>deadline:raise TimeoutError('Peer completion barrier')
  time.sleep(1)
 report['peer_listener_hits']=hits[:32]
 report['checks']['peer_no_nonloopback_hits']=all(x=='127.0.0.1' for x in hits)
 report['checks']['peer_listener_positive_seen']='127.0.0.1' in hits
 report['passed']=all(report['checks'].values())
except Exception as error:report.update({'passed':False,'error':repr(error)})
finally:
 for name,args in [('neighbors',('ip','-j','neigh','show')),('addresses',('ip','-j','addr','show')),('routes',('ip','-j','route','show'))]:
  try:report[name]=json.loads(cmd(*args))
  except Exception as error:report[name]={'diagnostic_error':repr(error)}
 stop.set()
 if listener:listener.close()
report['finished_at']=time.time()
with open('/dev/virtio-ports/org.harniqo.m0','wb',buffering=0) as output:
 output.write((json.dumps(report)+'\n').encode())
'''


def guest_config(instance, peer, gateway, subnet, cert, host_addresses):
    context = {k: instance[k] for k in ["uuid", "nonce", "ip"]}
    context.update(peer_ip=peer["ip"], gateway=gateway, spoof_ip=str(subnet[4]), host_addresses=host_addresses)
    service = ("[Unit]\nAfter=network-online.target\nWants=network-online.target\n"
               "[Service]\nType=oneshot\nExecStart=/usr/bin/python3 /usr/local/lib/harniqo-network-probe.py\n"
               "TimeoutStartSec=180\n[Install]\nWantedBy=multi-user.target\n")
    config = {"disable_root": True, "ssh_pwauth": False, "package_update": False, "package_upgrade": False,
              "growpart": {"mode": "off"}, "resize_rootfs": False,
              "write_files": [{"path": "/usr/local/lib/harniqo-network-probe.py", "permissions": "0700",
                               "content": GUEST.replace("CONFIG_JSON", repr(json.dumps(context)))},
                              {"path": "/etc/harniqo-canary.pem", "permissions": "0644", "content": cert},
                              {"path": "/etc/systemd/system/harniqo-network-probe.service", "permissions": "0644", "content": service}],
              "runcmd": [["systemctl", "daemon-reload"], ["systemctl", "enable", "--now", "harniqo-network-probe.service"]]}
    return "#cloud-config\n" + json.dumps(config) + "\n"


def health(gateway, port, cert):
    context = ssl.create_default_context(cafile=str(cert))
    with socket.create_connection((gateway, port), timeout=3) as conn:
        with context.wrap_socket(conn, server_hostname=gateway) as stream:
            stream.sendall(b"GET /health HTTP/1.1\r\nHost: canary\r\nConnection: close\r\n\r\n")
            data = stream.recv(4096)
    if not data.startswith(b"HTTP/1.1 200 "):
        raise RuntimeError("Host positive canary control failed")


def require_ports_free():
    listening = kvm.run(["ss", "-H", "-ltn", "(", "sport", "=", ":10018", "or", "sport", "=", ":10019", ")"])
    if listening: raise RuntimeError("A reserved canary port has a listener")
    for port in [PORT, DENIED_PORT]:
        with socket.socket() as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind(("0.0.0.0", port))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--base-image", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=240)
    parser.add_argument("--allow-owned-host-canary-input", action="store_true",
                        help="Explicit administrator authorization for two temporary owned bridge TCP10018 INPUT rules")
    args = parser.parse_args()
    if sys.platform != "linux" or platform.machine() not in {"x86_64", "amd64"} or os.geteuid() == 0:
        parser.error("Use an authorized ordinary Linux x86_64 operator")
    if not 180 <= args.timeout <= 600: parser.error("timeout must be 180..600")
    root, base = args.root.absolute(), args.base_image.absolute()
    kvm.no_symlinks(root); kvm.no_symlinks(base)
    root, base = root.resolve(strict=True), base.resolve(strict=True)
    if not (root / "images/README.md").is_file() or not base.is_relative_to(root / "images/vm/base"):
        parser.error("Expected workspace and its authenticated base image")
    for command in ["virsh", "qemu-img", "qemu-system-x86_64", "cloud-localds", "getfacl", "setfacl", "ip", "ss", "docker", "openssl"]:
        if not shutil.which(command): parser.error("Missing " + command)
    receipt = json.loads(base.with_suffix(".provenance.json").read_text())
    if (not re.fullmatch(r"[a-f0-9]{64}\.qcow2", base.name) or receipt.get("sha256") != base.stem
            or receipt.get("signer_fingerprint") != kvm.SIGNER or kvm.sha256(base) != base.stem):
        raise RuntimeError("Authenticated base image verification failed")
    info = json.loads(kvm.run(["qemu-img", "info", "--output=json", str(base)]))
    if info.get("format") != "qcow2" or info.get("backing-filename") or info.get("virtual-size", 0) > 8*1024**3:
        raise RuntimeError("Expected standalone base image fitting 8 GiB")
    print(kvm.run(["df", "-h", str(root)]), flush=True)
    if shutil.disk_usage(root).free < 66*1024**3: raise RuntimeError("Preserve 50 GiB plus two disks")
    state = root / ".state/qualification"; state.mkdir(parents=True, exist_ok=True); kvm.no_symlinks(state)
    lock = (state / "kvm.lock").open("a"); fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    require_ports_free()
    baseline = object_inventory()
    occupied = host_networks()
    for item in baseline["networks"].values():
        for ip in ET.fromstring(item["xml"]).findall("ip"):
            occupied.append(ipaddress.ip_network(f"{ip.get('address')}/{ip.get('prefix') or ip.get('netmask')}", strict=False))
    subnet = select_subnet(occupied); gateway = str(subnet[1])
    host_addresses = sorted({address["local"] for link in json.loads(kvm.run(["ip", "-j", "-4", "address", "show"]))
                             for address in link.get("addr_info", []) if address.get("scope") == "global"})
    run_id = uuid.uuid4().hex[:12]
    work = state / ("network-" + run_id)
    images = root / "images/vm/instances" / ("network-" + run_id)
    private = root / ".private"; reports_parent = private / "qualification"
    for directory in [private, reports_parent]:
        kvm.no_symlinks(directory); directory.mkdir(mode=0o700, exist_ok=True)
        if directory.stat().st_uid != os.geteuid(): raise RuntimeError("Private directory owner mismatch")
        directory.chmod(0o700)
    reports = reports_parent / ("network-" + run_id)
    for directory in [work, images, reports]:
        kvm.no_symlinks(directory); directory.mkdir(mode=0o700, parents=True)
    identities = {p: (p.stat().st_dev, p.stat().st_ino) for p in [work, images]}
    owner = {"qualification": "harniqo-m0-network", "run_id": run_id}
    for p in [work, images]: kvm.write(p / ".harniqo-m0-owner.json", json.dumps(owner))
    network = {"uuid": str(uuid.uuid4()), "name": "harniqo-net-"+run_id, "bridge": "hqn"+run_id[:10]}
    instances = []
    for n, label in enumerate(["a", "b"], 2):
        item = {"uuid": str(uuid.uuid4()), "name": f"harniqo-net-{run_id}-{label}", "nonce": uuid.uuid4().hex,
                "ip": str(subnet[n]), "mac": "52:54:00:"+":".join([run_id[0:2], run_id[2:4], f"{n:02x}"]),
                "filter_uuid": str(uuid.uuid4()), "filter_name": f"harniqo-net-{run_id}-{label}",
                "overlay": images/f"{label}.qcow2", "seed": images/f"{label}-seed.img",
                "socket": work/f"{label}.sock", "console_socket": work/f"{label}-console.sock", "xml": work/f"{label}.xml"}
        if item["uuid"] in baseline["domains"] or item["filter_uuid"] in baseline["filters"]: raise RuntimeError("UUID collision")
        instances.append(item)
    if network["uuid"] in baseline["networks"]: raise RuntimeError("Network UUID collision")
    result = {"schema_version": 1, "qualification": "M0-05-kvm-nic-filter-canary", "run_id": run_id,
              "started_at": kvm.now(), "phase": "prepare", "passed": False, "milestone_complete": False,
              "script_sha256": kvm.sha256(Path(__file__)), "sealed_helper_sha256": kvm.sha256(Path(kvm.__file__)),
              "host_input_opt_in": args.allow_owned_host_canary_input,
              "base_sha256": base.stem, "libvirt_version": kvm.virsh("version"), "python_version": sys.version,
              "disk_available_before_bytes": shutil.disk_usage(root).free,
              "production_egress_verified": False, "not_validated": UNVERIFIED, "network": network,
              "subnet": str(subnet), "baseline": baseline, "errors": [], "cleanup_errors": [], "rounds": [],
              "canary_kind": "synthetic fixed TLS response; no forwarding or model calls",
              "instances": [{key: str(value) for key, value in x.items()} for x in instances]}
    kvm.write(reports / "ownership.json", json.dumps(result, indent=2))
    kvm.write(reports / "qualification-source.py", Path(__file__).read_text())
    kvm.write(reports / "sealed-helper-source.py", Path(kvm.__file__).read_text())
    policy_source = Path(__file__).with_name("kvm_host_policy.py")
    kvm.write(reports / "host-policy-helper-source.py", policy_source.read_text())
    result["host_policy_helper_sha256"] = kvm.sha256(policy_source)
    print(json.dumps({"phase":"owned_plan", "run_id":run_id, "subnet":str(subnet), "ports":[PORT,DENIED_PORT], "report":str(reports/"report.json")}), flush=True)
    acl = kvm.TemporaryACL("libvirt-qemu"); streams = None; logs = []; canary = None; evidence = None; host_policy = None
    def terminate(signum, frame): raise KeyboardInterrupt(f"Signal {signum}")
    signal.signal(signal.SIGTERM, terminate)
    signal.signal(signal.SIGHUP, terminate)
    try:
        result["phase"] = "network_create"
        path=work/"network.xml"; kvm.write(path,network_xml(network,subnet)); kvm.virsh("net-create",str(path))
        actual=kvm.virsh("net-dumpxml",network["uuid"]);require_identity(actual,network)
        tree=ET.fromstring(actual)
        if tree.find("forward") is not None or tree.find("dns").get("enable")!="no" or tree.findall(".//dhcp") or tree.find("port").get("isolated")!="yes":raise RuntimeError("Unexpected actual network")
        result["actual_network_xml"]=actual
        for item in instances:
            path=work/(item["name"]+"-filter.xml");kvm.write(path,filter_xml(item,gateway));kvm.virsh("nwfilter-define",str(path))
            actual=kvm.virsh("nwfilter-dumpxml",item["filter_uuid"])
            require_identity(actual,{"uuid":item["filter_uuid"],"name":item["filter_name"]},filter_object=True)
            kvm.write(reports/(item["name"]+"-filter.xml"),actual)
        result["phase"] = "canary_start"
        cert,key=work/"canary.pem",work/"canary.key"
        kvm.run(["openssl","req","-x509","-newkey","rsa:2048","-nodes","-days","1","-subj","/CN=Harniqo qualification canary",
                 "-addext","subjectAltName=IP:"+gateway,"-keyout",str(key),"-out",str(cert)])
        key.chmod(0o600);cert.chmod(0o600)
        config={"gateway":gateway,"cert":str(cert),"key":str(key),"audit":str(reports/"canary.jsonl"),
                "ready":str(work/"canary.ready"),"seconds":args.timeout+90,"guests":{x["ip"]:{"nonce":x["nonce"]} for x in instances}}
        kvm.write(work/"canary.json",json.dumps(config));kvm.write(work/"canary.py",CANARY)
        with (reports/"canary-stderr.log").open("xb") as log:
            os.fchmod(log.fileno(),0o600)
            canary=subprocess.Popen([sys.executable,str(work/"canary.py"),str(work/"canary.json")],stdin=subprocess.DEVNULL,stdout=log,stderr=log,start_new_session=True,
                                    env={"PATH":"/usr/bin:/bin","LC_ALL":"C","PYTHONDONTWRITEBYTECODE":"1"})
        deadline=time.monotonic()+10
        while not (work/"canary.ready").exists():
            if canary.poll() is not None or time.monotonic()>deadline:raise RuntimeError("Canary startup failed")
            time.sleep(.1)
        health(gateway,PORT,cert);health(gateway,DENIED_PORT,cert)
        result["host_canary_positive_controls"]={str(PORT):True,str(DENIED_PORT):True}
        if args.allow_owned_host_canary_input:
            result["phase"] = "owned_host_input_exception"
            host_policy = OwnedInputPolicy(root, reports, network, subnet, instances, result["actual_network_xml"])
            host_policy.apply()
            result["host_input_exception"] = host_policy.receipt
        for item,peer in [(instances[0],instances[1]),(instances[1],instances[0])]:
            kvm.run(["qemu-img","create","-f","qcow2","-F","qcow2","-b",str(base),str(item["overlay"]),"8G"]);item["overlay"].chmod(0o600)
            user,meta,net=[work/(item["name"]+suffix) for suffix in ["-user","-meta","-network"]]
            kvm.write(user,guest_config(item,peer,gateway,subnet,cert.read_text(),host_addresses))
            kvm.write(meta,json.dumps({"instance-id":item["uuid"],"local-hostname":item["name"]}))
            kvm.write(net,json.dumps({"version":2,"ethernets":{"probe0":{"match":{"macaddress":item["mac"]},"set-name":"probe0","dhcp4":False,"dhcp6":False,"accept-ra":False,"addresses":[item["ip"]+"/28"],"link-local":[]}}}))
            kvm.run(["cloud-localds","--network-config",str(net),str(item["seed"]),str(user),str(meta)]);item["seed"].chmod(0o600)
            item["listener"]=kvm.listener_at(item["socket"]);item["console_listener"]=kvm.listener_at(item["console_socket"])
            tree=ET.fromstring(kvm.domain_xml(item,shutil.which("qemu-system-x86_64")))
            interface=ET.SubElement(tree.find("devices"),"interface",type="network")
            ET.SubElement(interface,"mac",address=item["mac"]);ET.SubElement(interface,"source",network=network["name"])
            ET.SubElement(interface,"model",type="virtio");ET.SubElement(interface,"filterref",filter=item["filter_name"])
            ET.SubElement(interface,"port",isolated="yes")
            kvm.write(item["xml"],ET.tostring(tree,encoding="unicode"))
            for file,rights in [(base,"r--"),(item["overlay"],"rw-"),(item["seed"],"r--"),(item["socket"],"rw-"),(item["console_socket"],"rw-")]:
                acl.traverse(file);acl.grant(file,rights,restore=file==base)
        with kvm.RoundEvidence(result["rounds"],1,instances) as evidence:
            streams=kvm.GuestStreams(instances,work,1,logs);evidence.streams=streams
            evidence.record["phase"]="boot";result["phase"]="boot"
            for item in instances:
                streams.mark_start(item);kvm.virsh("create",str(item["xml"]))
                actual=ET.fromstring(kvm.virsh("dumpxml",item["uuid"]))
                interfaces=actual.findall("./devices/interface")
                if actual.get("type")!="kvm" or len(interfaces)!=1 or actual.findall("./devices/filesystem") or interfaces[0].find("filterref").get("filter")!=item["filter_name"]:
                    raise RuntimeError("Actual VM topology differs")
                kvm.write(reports/(item["name"]+"-domain.xml"),ET.tostring(actual,encoding="unicode"))
            evidence.record["phase"]="collect";received=streams.collect(args.timeout)
            evidence.record["phase"]="validate"
            validate_reports(received,instances,len(host_addresses))
            health(gateway,DENIED_PORT,cert)
            for item in instances:kvm.stop_owned(item,baseline["domains"])
            streams.finish()
        result["phase"]="complete"
    except BaseException as error:result["errors"].append(type(error).__name__+": "+str(error))
    finally:
        stopped=True;console_saved=True
        try:result["host_neighbors_before_cleanup"]=json.loads(kvm.run(["ip","-j","neigh","show","dev",network["bridge"]]))
        except Exception as error:result["host_network_diagnostic_error"]=str(error)
        for item in instances:
            try:kvm.stop_owned(item,baseline["domains"])
            except Exception as error:stopped=False;result["cleanup_errors"].append("stop: "+str(error))
        if host_policy:
            try:host_policy.restore()
            except Exception as error:result["cleanup_errors"].append("host input policy: "+str(error))
            result["host_input_exception"] = host_policy.receipt
        if streams:
            try:streams.finish()
            except Exception as error:console_saved=False;result["cleanup_errors"].append("console drain: "+str(error))
        if evidence:evidence.snapshot()
        for item in instances:
            for name in ["listener","console_listener"]:
                if item.get(name):item[name].close()
        for log in logs:
            try:kvm.save_console(log,reports)
            except Exception as error:console_saved=False;result["cleanup_errors"].append("console save: "+str(error))
        result["console_logs"]=[{k:str(v) if isinstance(v,Path) else v for k,v in x.items() if k not in {"fd","inode"}} for x in logs]
        if canary:
            try:
                canary.terminate()
                try:canary.wait(timeout=5)
                except subprocess.TimeoutExpired:canary.kill();canary.wait(timeout=5)
            except Exception as error:result["cleanup_errors"].append("canary stop: "+str(error))
        audit=reports/"canary.jsonl"
        if audit.exists():
            try:
                entries=[json.loads(x) for x in audit.read_text().splitlines()]
                forbidden=[x for x in entries if x["port"]==DENIED_PORT and x["source"]!=gateway]
                result["canary_audit"]={"requests":len(entries),"forbidden_port_guest_hits":len(forbidden)}
                if forbidden:result["errors"].append("Guest reached the denied host listener")
            except Exception as error:result["cleanup_errors"].append("canary audit: "+str(error))
        result["cleanup_errors"].extend(acl.restore())
        try:
            current=object_inventory()
            if stopped and (host_policy is None or host_policy.receipt["restored"]) and not any(x["uuid"] in current["domains"] for x in instances):
                for item in instances:
                    if item["filter_uuid"] in current["filters"]:
                        require_identity(current["filters"][item["filter_uuid"]],{"uuid":item["filter_uuid"],"name":item["filter_name"]},filter_object=True)
                        kvm.virsh("nwfilter-undefine",item["filter_uuid"])
                if network["uuid"] in current["networks"]:
                    require_identity(current["networks"][network["uuid"]]["xml"],network)
                    if kvm.virsh("net-port-list",network["uuid"]).splitlines()[2:]:
                        raise RuntimeError("Network ports remain; preserving owned network")
                    kvm.virsh("net-destroy",network["uuid"])
                for directory in [images]+([work] if console_saved else []):
                    kvm.no_symlinks(directory)
                    if (directory.stat().st_dev,directory.stat().st_ino)!=identities[directory] or json.loads((directory/".harniqo-m0-owner.json").read_text())!=owner:
                        raise RuntimeError("Cleanup directory ownership mismatch")
                    shutil.rmtree(directory)
                result["temporary_images_removed"]=True
            else:raise RuntimeError("Owned VM stop or host policy restore unconfirmed; preserving resources")
            after=object_inventory();result["preexisting_objects_unchanged"]=after==baseline
            if after!=baseline:raise RuntimeError("Pre-existing domain/network/filter inventory differs")
            result["base_unchanged"]=kvm.sha256(base)==base.stem
            if not result["base_unchanged"]:raise RuntimeError("Base image digest changed")
            require_ports_free()
            result["ports_released"]=True
        except Exception as error:result["cleanup_errors"].append("final cleanup: "+str(error))
        result["finished_at"]=kvm.now()
        result["disk_available_after_bytes"]=shutil.disk_usage(root).free
        result["disk_available_delta_bytes"]=result["disk_available_after_bytes"]-result["disk_available_before_bytes"]
        result["passed"]=not result["errors"] and not result["cleanup_errors"] and len(result["rounds"])==1 and result["rounds"][0]["status"]=="passed"
        kvm.write(reports/"report.json",json.dumps(result,indent=2)+"\n")
        print(json.dumps({"qualification":result["qualification"],"passed":result["passed"],"milestone_complete":False,"report":str(reports/"report.json"),"errors":result["errors"],"cleanup_errors":result["cleanup_errors"]}),flush=True)
        print(kvm.run(["df","-h",str(root)]),flush=True)
        lock.close()
    return 0 if result["passed"] else 1


if __name__=="__main__":raise SystemExit(main())
