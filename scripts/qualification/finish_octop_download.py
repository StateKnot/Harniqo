#!/usr/bin/env python3
"""Complete one public locked artifact using verified HTTP ranges and final SHA.
Only needed when a single large PyPI wheel stalls the bounded parallel closure.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import re
import urllib.request
from prepare_octop import digest


def main():
 p=argparse.ArgumentParser();p.add_argument('--directory',type=Path,required=True);p.add_argument('--name',required=True);a=p.parse_args()
 rows=json.loads((a.directory/'locked-selection.json').read_text());matches=[r for r in rows if r['name']==a.name]
 if len(matches)!=1:raise ValueError('ambiguous locked artifact')
 r=matches[0]
 if not r['url'].startswith('https://files.pythonhosted.org/') or Path(r['filename']).name!=r['filename'] or not re.fullmatch('[a-f0-9]{64}',r['sha256']):raise ValueError('invalid public artifact record')
 out=a.directory/r['filename']
 if out.exists() and digest(out)==r['sha256']:print('already verified',a.name);return
 partial=out.with_suffix('.partial');prefix=partial.stat().st_size if partial.exists() else 0
 if prefix>r['size']:raise ValueError('oversized partial')
 prefix_data=partial.open('rb').read(prefix) if prefix else b''
 if len(prefix_data)!=prefix:raise ValueError('partial changed while reading')
 step=max(1,(r['size']-prefix+7)//8)
 ranges=[(start,min(r['size']-1,start+step-1)) for start in range(prefix,r['size'],step)]
 def fetch(span):
  start,end=span;req=urllib.request.Request(r['url'],headers={'Range':f'bytes={start}-{end}'})
  with urllib.request.urlopen(req,timeout=90) as response:
   if response.status!=206 or response.headers.get('Content-Range')!=f'bytes {start}-{end}/{r["size"]}':raise ValueError('range not honored')
   data=response.read(end-start+2)
   if len(data)!=end-start+1:raise ValueError('range size mismatch')
   return data
 with ThreadPoolExecutor(max_workers=8) as pool:data=prefix_data+b''.join(pool.map(fetch,ranges))
 if len(data)!=r['size'] or hashlib.sha256(data).hexdigest()!=r['sha256']:raise ValueError('full artifact mismatch')
 temporary=out.with_name(out.name+'.range-complete');temporary.write_bytes(data);temporary.replace(out)
 (a.directory/(r['filename']+'.range-provenance.json')).write_text(json.dumps({'source':r,'reused_prefix_bytes':prefix,'verified_ranges':ranges,'full_sha256':r['sha256']},indent=2)+'\n')
 print(json.dumps({'artifact':a.name,'bytes':len(data),'sha256':r['sha256'],'prefix':prefix,'ranges':len(ranges)}))

if __name__=='__main__':main()
