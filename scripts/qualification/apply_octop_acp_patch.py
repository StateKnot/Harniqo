#!/usr/bin/env python3
"""Apply exactly one reviewed unified patch to its hash-pinned upstream file."""
import argparse
import hashlib
import json
from pathlib import Path


def sha(data):return hashlib.sha256(data).hexdigest()


def patched_source(source, patch, profile):
 if sha(source)!=profile['upstream_sha256'] or sha(patch)!=profile['patch_sha256']:
  raise ValueError('source or patch identity mismatch')
 lines=patch.decode().splitlines(keepends=True)
 if len(lines)<4 or not lines[0].startswith('--- a/') or not lines[1].startswith('+++ b/') or sum(x.startswith('@@') for x in lines)!=1:
  raise ValueError('exactly one unified hunk required')
 hunk=lines[3:]
 if any(x[0] not in ' +-' for x in hunk):raise ValueError('unsupported patch syntax')
 old=''.join(x[1:] for x in hunk if x[0] in ' -');new=''.join(x[1:] for x in hunk if x[0] in ' +')
 text=source.decode()
 if text.count(old)!=1:raise ValueError('unique source hunk required')
 result=text.replace(old,new,1).encode()
 if sha(result)!=profile['patched_sha256']:raise ValueError('patched identity mismatch')
 return result


def main():
 p=argparse.ArgumentParser();p.add_argument('--source',type=Path,required=True);p.add_argument('--patch',type=Path,required=True);p.add_argument('--profile',type=Path,required=True);a=p.parse_args()
 result=patched_source(a.source.read_bytes(),a.patch.read_bytes(),json.loads(a.profile.read_text()))
 a.source.write_bytes(result)
 print(json.dumps({'patched_source_sha256':sha(result)}))

if __name__=='__main__':main()
