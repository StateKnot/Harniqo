#!/usr/bin/env python3
"""Export the pinned upstream uv.lock core closure, selecting this interpreter's wheels.
Uses pip's bundled packaging only in the immutable qualification Python image.
It does not install or execute downloaded packages. All wheels must match lock hashes.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import tomllib
import urllib.request
from pip._vendor.packaging.markers import Marker
from pip._vendor.packaging.tags import sys_tags
from pip._vendor.packaging.utils import parse_wheel_filename


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--lock',type=Path,required=True)
    p.add_argument('--destination',type=Path,required=True)
    p.add_argument('--scope',choices=['harness','service'],default='harness')
    p.add_argument('--allow-sdist',action='store_true')
    args=p.parse_args()
    expected={'harness':'1766085847229271984a3f3080fd9d2c7958f843f84c4a185b32ab4240d1c992','service':'c6b4c1a8f96c37351731b7eab79772fccffbc1ae7aadd501df28be954b52c907'}[args.scope]
    if hashlib.sha256(args.lock.read_bytes()).hexdigest()!=expected: raise ValueError('pinned source lock mismatch')
    lock=tomllib.loads(args.lock.read_text())
    packages=lock['package']
    tags={t:i for i,t in enumerate(sys_tags())}
    selected={}
    seen=set()
    root_name,root_version=('octop-harness','1.0.5') if args.scope=='harness' else ('octop','1.0.3b1')
    pending=[({'name':root_name,'version':root_version},())]
    while pending:
        dep,extra=pending.pop()
        if dep.get('marker') and not Marker(dep['marker']).evaluate():
            continue
        matches=[r for r in packages if r['name']==dep['name'] and (not dep.get('version') or r['version']==dep['version']) and
                 (not r.get('resolution-markers') or any(Marker(m).evaluate() for m in r['resolution-markers']))]
        if len(matches)!=1:
            raise ValueError('ambiguous dependency '+dep['name'])
        package=matches[0]
        extras=tuple(sorted(set(dep.get('extra',[]))|set(extra)))
        identity=(package['name'],package['version'],extras)
        if identity in seen: continue
        seen.add(identity)
        if package['name'] in selected and selected[package['name']]['version']!=package['version']:
            raise ValueError('conflicting versions')
        selected[package['name']]=package
        pending += [(d,()) for d in package.get('dependencies',[])]
        for e in extras:
            pending += [(d,()) for d in package.get('optional-dependencies',{}).get(e,[])]
    records=[]
    for package in selected.values():
        name,version=package['name'],package['version']
        if name==root_name:
            cached=args.destination/'locked-selection.json'
            root_pin={'harness':'40a27c3ba62eb7cb6e555ed73f8e888b668cfbf447fc7ed4229e200f780ae1b4','service':'3801d38b586ff6e3c9aae48f95d3adf8042c8df9d514b3caa7005366aff2f454'}[args.scope]
            entries=[r for r in json.loads(cached.read_text()) if r['name']==name and r['version']==version] if cached.exists() else []
            if len(entries)==1 and entries[0]['sha256']==root_pin:
                r=entries[0];wheels=[dict(url=r['url'],hash='sha256:'+r['sha256'],size=r['size'])]
            else:
                with urllib.request.urlopen(f'https://pypi.org/pypi/{name}/{version}/json',timeout=60) as r:
                    meta=json.load(r)
                wheels=[dict(url=w['url'],hash='sha256:'+w['digests']['sha256'],size=w['size']) for w in meta['urls'] if w['filename'].endswith('.whl') and not w['yanked'] and w['digests']['sha256']==root_pin]
        else:
            if package['source']!={'registry':'https://pypi.org/simple'}:
                raise ValueError('non-official registry')
            wheels=package.get('wheels',[])
        compatible=[]
        for wheel in wheels:
            filename=wheel['url'].rsplit('/',1)[-1]
            if not wheel['url'].startswith('https://files.pythonhosted.org/'):
                raise ValueError('unexpected wheel host')
            _,_,_,wheel_tags=parse_wheel_filename(filename)
            ranks=[tags[t] for t in wheel_tags if t in tags]
            if ranks: compatible.append((min(ranks),filename,wheel))
        if not compatible:
            if not args.allow_sdist or not package.get('sdist'): raise ValueError('no compatible locked wheel: '+name+'=='+version)
            wheel=package['sdist']
            filename=wheel['url'].rsplit('/',1)[-1]
        else:
            _,filename,wheel=min(compatible,key=lambda r:(r[0],r[1]))
        records.append(dict(name=name,version=version,filename=filename,url=wheel['url'],sha256=wheel['hash'].removeprefix('sha256:'),size=wheel['size']))
    args.destination.mkdir(parents=True,exist_ok=True)
    (args.destination/'locked-selection.json').write_text(json.dumps(records,indent=2)+'\n')
    def download(r):
        path=args.destination/r['filename']
        if not path.exists():
            temp=path.with_suffix('.partial')
            with urllib.request.urlopen(r['url'],timeout=180) as response, temp.open('wb') as out:
                while chunk:=response.read(1024*1024): out.write(chunk)
            temp.replace(path)
        if path.stat().st_size!=r['size'] or hashlib.file_digest(path.open('rb'),'sha256').hexdigest()!=r['sha256']:
            raise ValueError('locked wheel verification failed: '+r['name'])
        return r['name']
    with ThreadPoolExecutor(max_workers=8) as pool:
        for name in pool.map(download,records): print('verified',name,flush=True)
    print(json.dumps(dict(wheels=len(records),bytes=sum(r['size'] for r in records),source_lock_sha256=hashlib.sha256(args.lock.read_bytes()).hexdigest())))

if __name__=='__main__': main()
