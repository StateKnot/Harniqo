#!/usr/bin/env python3
"""Component-only checks; no model, Docker, or external network."""
import hashlib
import io
import json
from pathlib import Path
import tempfile
import subprocess
import unittest
from unittest.mock import patch
import zipfile
import prepare_octop as subject

class InputChecks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)/'example-1.0-py3-none-any.whl'
        with zipfile.ZipFile(self.path,'w') as archive:
            archive.writestr('example-1.0.dist-info/METADATA','Name: example\nVersion: 1.0\n')
        self.record = dict(filename=self.path.name, size=self.path.stat().st_size,
                           url='https://files.pythonhosted.org/example.whl',
                           digests={'sha256':hashlib.sha256(self.path.read_bytes()).hexdigest()})
    def verify(self):
        with patch('urllib.request.urlopen', return_value=io.BytesIO(json.dumps({'urls':[self.record]}).encode())):
            return subject.official_record(self.path)
    def test_official_digest_and_size(self):
        self.assertEqual(self.verify()['version'],'1.0')
    def test_digest_tamper(self):
        self.record['digests']['sha256']='0'*64
        with self.assertRaises(ValueError): self.verify()
    def test_size_tamper(self):
        self.record['size']+=1
        with self.assertRaises(ValueError): self.verify()
    def test_unofficial_host(self):
        self.record['url']='https://files.pythonhosted.org.evil.test/example.whl'
        with self.assertRaises(ValueError): self.verify()
    def test_wrong_filename(self):
        self.record['filename']='other.whl'
        with self.assertRaises(ValueError): self.verify()
    def test_vendored_metadata_is_not_distribution_metadata(self):
        with zipfile.ZipFile(self.path,'a') as archive:
            archive.writestr('example/vendor/other.dist-info/METADATA','Name: vendor\nVersion: 2.0\n')
        self.assertEqual(subject.wheel_metadata(self.path),('example','1.0'))
    def test_launch_collision_never_stops_foreign_container(self):
        calls=[]
        def runner(args,**kwargs):
            calls.append(args)
            if args[3]=='run':return subprocess.CompletedProcess(args,125,'','name already in use')
            if args[3]=='inspect':return subprocess.CompletedProcess(args,0,json.dumps([{
                'Id':'a'*64,'Image':'sha256:'+'b'*64,
                'Config':{'Image':subject.BASE,'Labels':{'io.harniqo.attempt':'foreign'}},
                'State':{'Running':False,'Pid':0}}]),'')
            raise AssertionError('foreign resource mutation '+str(args))
        log=Path(self.tmp.name)/'collision.log'
        with self.assertRaises(RuntimeError):
            subject.run(['docker','run','--name','occupied',subject.BASE],log,runner=runner)
        self.assertEqual([c[3] for c in calls],['run','inspect'])
        cleanup=json.loads((log.parent/'collision.log.cleanup.json').read_text())
        self.assertEqual(cleanup['resource_state'],'ownership-mismatch')
    def test_ambiguous_metadata(self):
        with zipfile.ZipFile(self.path,'a') as archive:
            archive.writestr('second.dist-info/METADATA','Name: second\nVersion: 1.0\n')
        with self.assertRaises(ValueError): subject.wheel_metadata(self.path)

if __name__ == '__main__': unittest.main()
