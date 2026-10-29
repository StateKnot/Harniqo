#!/usr/bin/env python3
"""Operator reconciliation fixtures; no Docker, model calls, or external network."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from run_octop import finalize_container, save_operator_report, operator_passes, DOCKER_SOCKET

class CleanupTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.path=Path(self.tmp.name);self.calls=[]
 def state(self,running=False,owned=True):
  return subprocess.CompletedProcess([],0,json.dumps([{'Id':'a'*64,'Image':'sha256:'+'c'*64,'Config':{'Image':'sha256:'+'b'*64,'Labels':{'io.harniqo.attempt':'owner' if owned else 'other'}},'State':{'Pid':123 if running else 0,'Running':running}}]),'')
 def runner(self,responses):
  def run(args,**kwargs):
   self.calls.append(args)
   result=responses.pop(0)
   if isinstance(result,BaseException):raise result
   return result
  return run
 def ok(self):return subprocess.CompletedProcess([],0,'logs','')
 def test_bad_exit_or_operator_error_cannot_pass(self):
  report={'exit_code':0,'pid_zero':True,'removed':True,'cleanup_errors':[]}
  self.assertTrue(operator_passes({'qualified':True},report))
  for update in [{'exit_code':1},{'operator_error':'TimeoutExpired'},{'cleanup_errors':[{'step':'stop'}]}]:
   self.assertFalse(operator_passes({'qualified':True},{**report,**update}))
 def test_mutations_use_immutable_id_and_local_daemon(self):
  r=finalize_container('exact','owner',self.path,expected_image='sha256:'+'b'*64,runner=self.runner([self.state(True),self.ok(),self.state(),self.ok(),self.state(),self.ok()]))
  for args in self.calls:
   self.assertEqual(args[:3],['docker','--host',DOCKER_SOCKET])
   if args[3] in {'stop','kill','logs','rm'}:self.assertEqual(args[-1],'a'*64)
 def test_expected_image_mismatch_never_stops(self):
  r=finalize_container('exact','owner',self.path,expected_image='sha256:'+'f'*64,runner=self.runner([self.state(True)]))
  self.assertEqual(len(self.calls),1);self.assertEqual(r['resource_state'],'ownership-mismatch')
 def test_unknown_dispatch_still_cleans_owned_container(self):
  r=finalize_container('exact','owner',self.path,expected_image='sha256:'+'b'*64,runner=self.runner([self.state(True),self.ok(),self.state(),self.ok(),self.state(),self.ok()]))
  self.assertTrue(r['pid_zero']);self.assertTrue(r['removed']);self.assertEqual(r['cleanup_errors'],[])
 def test_stop_timeout_still_saves_state_and_logs(self):
  r=finalize_container('exact','owner',self.path,expected_image='sha256:'+'b'*64,runner=self.runner([self.state(True),subprocess.TimeoutExpired('stop',20),self.state(),self.ok(),self.state(),self.ok()]))
  self.assertTrue(r['removed']);self.assertEqual(r['cleanup_errors'][0]['step'],'stop')
  save_operator_report(self.path,r);self.assertTrue((self.path/'operator-report.json').exists())
 def test_failed_stop_forces_owned_process_then_checks_pid(self):
  r=finalize_container('exact','owner',self.path,expected_image='sha256:'+'b'*64,runner=self.runner([self.state(True),self.ok(),self.state(True),self.ok(),self.state(),self.ok(),self.state(),self.ok()]))
  self.assertTrue(r['forced_stop_attempted']);self.assertTrue(r['pid_zero']);self.assertIn('kill',[c[3] for c in self.calls])
 def test_unknown_inspect_never_stops_unverified_resource(self):
  r=finalize_container('exact','owner',self.path,expected_image='sha256:'+'b'*64,runner=self.runner([subprocess.TimeoutExpired('inspect',20)]))
  self.assertEqual(len(self.calls),1);self.assertFalse(r['removed'])
 def test_ownership_mismatch_never_stops(self):
  r=finalize_container('exact','owner',self.path,expected_image='sha256:'+'b'*64,runner=self.runner([self.state(True,False)]))
  self.assertEqual(len(self.calls),1);self.assertEqual(r['resource_state'],'ownership-mismatch')
 def test_failed_logs_preserves_stopped_container(self):
  r=finalize_container('exact','owner',self.path,expected_image='sha256:'+'b'*64,runner=self.runner([self.state(),self.state(),subprocess.TimeoutExpired('logs',20)]))
  self.assertTrue(r['pid_zero']);self.assertFalse(r['removed']);self.assertNotIn('rm',[c[3] for c in self.calls])
 def test_failed_remove_keeps_report(self):
  r=finalize_container('exact','owner',self.path,expected_image='sha256:'+'b'*64,runner=self.runner([self.state(),self.state(),self.ok(),self.state(),subprocess.TimeoutExpired('rm',20)]))
  save_operator_report(self.path,r);self.assertFalse(json.loads((self.path/'operator-report.json').read_text())['removed'])
 def test_malformed_inspect_never_stops(self):
  r=finalize_container('exact','owner',self.path,expected_image='sha256:'+'b'*64,runner=self.runner([subprocess.CompletedProcess([],0,'[]','')]))
  self.assertEqual(len(self.calls),1);self.assertFalse(r['pid_zero'])


class OperatorMainTests(unittest.TestCase):
 def exercise(self,unknown_dispatch=False,stop_timeout=False):
  from unittest.mock import patch
  import run_octop as op
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);(root/'.private/qualification/octop').mkdir(parents=True)
   (root/'scripts/qualification').mkdir(parents=True)
   (root/'scripts/qualification/probe_octop.py').write_text('# synthetic test only\n')
   key=root/'.private/key';key.write_text('synthetic-not-a-real-key');key.chmod(0o600)
   cfg=root/'.private/model-endpoints.json';cfg.write_text(json.dumps({'endpoints':[{'model':'qwen3.8-flash','base_url':'https://example.invalid/v1','api_key_file':'key'}]}));cfg.chmod(0o600)
   identity={};states=[True,False,False]
   def dispatch(args,**kwargs):
    if args[0]=='df':return subprocess.CompletedProcess(args,0,'fixture df','')
    if args[1]=='run':
     identity['owner']=next(x.split('=',1)[1] for x in args if x.startswith('io.harniqo.attempt='))
     if unknown_dispatch:raise subprocess.TimeoutExpired('docker run',1)
     return subprocess.CompletedProcess(args,0,'container-id','')
    if args[1]=='wait':return subprocess.CompletedProcess(args,0,'0','')
    raise AssertionError(args)
   def docker(args,**kwargs):
    method=args[3]
    if method=='inspect':
     running=states.pop(0)
     return subprocess.CompletedProcess(args,0,json.dumps([{'Id':'a'*64,'Image':'sha256:'+'c'*64,'Config':{'Image':'sha256:'+'b'*64,'Labels':{'io.harniqo.attempt':identity['owner']}},'State':{'Running':running,'Pid':1 if running else 0}}]),'')
    if method=='stop' and stop_timeout:raise subprocess.TimeoutExpired('stop',20)
    return subprocess.CompletedProcess(args,0,'fixture','')
   with patch('sys.argv',['run_octop','--root',str(root),'--image','sha256:'+'b'*64,'--case','allow']),patch.object(op,'command',dispatch),patch.object(op.subprocess,'run',docker):
    result=op.main()
   reports=list((root/'.private/qualification/octop').glob('native-*/operator-report.json'))
   self.assertEqual(len(reports),1);report=json.loads(reports[0].read_text())
   self.assertEqual(result,1);self.assertFalse(report['qualified']);self.assertTrue(report['pid_zero']);self.assertTrue(report['removed'])
   return report
 def test_unknown_run_outcome_reconciles_and_preserves_report(self):
  self.assertEqual(self.exercise(unknown_dispatch=True)['operator_error'],'TimeoutExpired')
 def test_stop_timeout_preserves_final_report(self):
  self.assertTrue(self.exercise(stop_timeout=True)['cleanup_errors'])

if __name__=='__main__':unittest.main()
