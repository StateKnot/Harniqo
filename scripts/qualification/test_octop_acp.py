#!/usr/bin/env python3
"""Pure boundaries: archive publication and native-observer serialization."""
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from prepare_octop_acp import archive_image
from probe_octop_acp import inner_environment,serial
from guard_octop_acp import OuterBudget,DELEGATED_PROMPT,permitted_call

class ArchiveTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.path=Path(self.tmp.name)/'owned.tar';self.report={}
 def runner(self,args,**kwargs):
  self.assertEqual(args[:3],['docker','--host','unix:///var/run/docker.sock'])
  Path(args[args.index('-o')+1]).write_bytes(b'qualification Docker save fixture')
 def test_success_promotes_complete_readonly_archive(self):
  sha,size=archive_image('sha256:'+'a'*64,self.path,'attempt',self.report,runner=self.runner)
  self.assertEqual(size,len(self.path.read_bytes()));self.assertEqual(sha,hashlib.sha256(self.path.read_bytes()).hexdigest())
  self.assertEqual(self.path.stat().st_mode&0o777,0o444);self.assertNotIn('partial_archive_name',self.report)
 def test_interrupted_save_retains_partial_without_final(self):
  def interrupted(*a,**kw):self.runner(*a,**kw);raise subprocess.TimeoutExpired('save',120)
  with self.assertRaises(subprocess.TimeoutExpired):archive_image('sha256:'+'a'*64,self.path,'attempt',self.report,runner=interrupted)
  self.assertFalse(self.path.exists());self.assertTrue(self.path.with_name('owned.tar.attempt.partial').exists());self.assertGreater(self.report['partial_archive_bytes'],0)
 def test_existing_archive_is_never_overwritten(self):
  self.path.write_text('prior')
  with self.assertRaises(ValueError):archive_image('sha256:'+'a'*64,self.path,'attempt',self.report,runner=lambda *a,**kw:self.fail('must not save'))
  self.assertEqual(self.path.read_text(),'prior')
 def test_empty_save_never_promoted(self):
  def empty(args,**kw):Path(args[args.index('-o')+1]).touch()
  with self.assertRaises(ValueError):archive_image('sha256:'+'a'*64,self.path,'attempt',self.report,runner=empty)
  self.assertFalse(self.path.exists())

class ProfileTests(unittest.TestCase):
 def test_bootstrap_has_no_real_model_key_and_no_fetch_enabled(self):
  env=inner_environment({'base_url':'https://example.invalid/v1','model':'qwen3.8-flash'})
  self.assertEqual(env['HARNIQO_MODEL_API_KEY'],'qualification-not-a-real-key');self.assertEqual(env['OPENCODE_DISABLE_MODELS_FETCH'],'1')
 def test_observer_serializes_values_without_replacing_them(self):
  value={'sessionId':'native','result':[1,True,None]};self.assertEqual(serial(value),value)

class BudgetTests(unittest.TestCase):
 def setUp(self):self.b=OuterBudget({'base_url':'https://example.invalid/v1','model':'qwen3.8-flash'},lambda *args:None)
 def body(self,n=0,**extra):return json.dumps({'model':'qwen3.8-flash','max_tokens':128,'messages':[{'role':'user','content':str(n)}],**extra}).encode()
 def test_four_actual_sends_then_reject_fifth(self):
  for n in range(4):self.b.admit(self.b.target,self.body(n))
  with self.assertRaises(RuntimeError):self.b.admit(self.b.target,self.body(4))
  self.assertEqual(len(self.b.calls),4)
 def test_auth_failure_prevents_later_send(self):
  row=self.b.admit(self.b.target,self.body());self.b.completed(row,401)
  with self.assertRaises(RuntimeError):self.b.admit(self.b.target,self.body(1))
 def test_duplicate_unknown_result_never_retried(self):
  self.b.admit(self.b.target,self.body())
  with self.assertRaises(RuntimeError):self.b.admit(self.b.target,self.body())
 def test_target_model_output_bound_before_send(self):
  for url,body in [(self.b.target+'x',self.body()),(self.b.target,self.body(model='other')),(self.b.target,self.body(max_tokens=129)),(self.b.target,self.body(max_tokens=True))]:
   with self.subTest(url=url),self.assertRaises(RuntimeError):self.b.admit(url,body)
  self.assertFalse(self.b.calls)
 def test_delegate_exact_scope(self):
  call={'name':'acp_runner','args':{'action':'start','runner':'opencode','message':DELEGATED_PROMPT,'cwd':'/evidence/workspace','max_runtime':90}}
  self.assertTrue(permitted_call(call))
  for k,v in [('action','message'),('runner','other'),('message',DELEGATED_PROMPT+' extra'),('cwd','/'),('max_runtime',None),('max_runtime',91)]:
   modified={'name':call['name'],'args':{**call['args'],k:v}}
   self.assertFalse(permitted_call(modified))


class PatchIntegrityTests(unittest.TestCase):
 def fixture(self):
  from apply_octop_acp_patch import sha
  source=b'first\nold\nlast\n';result=b'first\nnew\nlast\n';patch=b'--- a/example\n+++ b/example\n@@ -1,3 +1,3 @@\n first\n-old\n+new\n last\n'
  return source,patch,{'upstream_sha256':sha(source),'patch_sha256':sha(patch),'patched_sha256':sha(result)},result
 def test_exact_patch_is_applied_once(self):
  from apply_octop_acp_patch import patched_source
  source,patch,profile,result=self.fixture();self.assertEqual(patched_source(source,patch,profile),result)
  with self.assertRaises(ValueError):patched_source(result,patch,profile)
 def test_source_and_patch_mismatches_rejected(self):
  from apply_octop_acp_patch import patched_source
  source,patch,profile,_=self.fixture()
  for changed_source,changed_patch in [(source+b'x',patch),(source,patch+b'x')]:
   with self.assertRaises(ValueError):patched_source(changed_source,changed_patch,profile)
 def test_result_identity_mismatch_rejected(self):
  from apply_octop_acp_patch import patched_source
  source,patch,profile,_=self.fixture();profile['patched_sha256']='0'*64
  with self.assertRaises(ValueError):patched_source(source,patch,profile)


class PrivatePathTests(unittest.TestCase):
 def setUp(self):
  import os
  self.tmp=tempfile.TemporaryDirectory(dir='/private/tmp' if Path('/private/tmp').exists() else '/tmp');self.addCleanup(self.tmp.cleanup)
  self.base=Path(self.tmp.name);self.private=self.base/'.private';self.private.mkdir(mode=0o700)
 def test_create_private_chain_without_relaxing_existing_modes(self):
  from probe_hermes import private_directory
  result=private_directory(self.private/'qualification/octop-acp',create=True)
  self.assertEqual(result.stat().st_mode&0o777,0o700);self.assertEqual(result.parent.stat().st_mode&0o777,0o700)
 def test_permissive_ancestor_rejected_unchanged(self):
  from probe_hermes import private_directory,Rejected
  self.private.chmod(0o750)
  with self.assertRaises(Rejected):private_directory(self.private/'child',create=True)
  self.assertEqual(self.private.stat().st_mode&0o777,0o750);self.assertFalse((self.private/'child').exists())
 def test_symlink_ancestor_rejected_before_resolution(self):
  from probe_hermes import private_directory,Rejected
  link=self.base/'alias';link.symlink_to(self.private,target_is_directory=True)
  with self.assertRaises(Rejected):private_directory(link/'child',create=True)
 def test_bootstrap_file_symlink_rejected(self):
  from probe_hermes import no_links,Rejected
  target=self.private/'report.json';target.write_text('{}');link=self.private/'linked.json';link.symlink_to(target)
  with self.assertRaises(Rejected):no_links(link)
 def test_config_file_symlink_rejected(self):
  from probe_hermes import read_file,Rejected
  target=self.private/'config.json';target.write_text('{}');target.chmod(0o600);link=self.private/'linked.json';link.symlink_to(target)
  with self.assertRaises(Rejected):read_file(link,1024)
 def test_key_ancestor_symlink_rejected(self):
  from probe_hermes import no_links,Rejected
  target=self.private/'secrets';target.mkdir(mode=0o700);link=self.private/'alias';link.symlink_to(target,target_is_directory=True)
  with self.assertRaises(Rejected):no_links(link/'key')
 def test_wrong_directory_owner_rejected(self):
  import os
  from unittest.mock import patch
  from probe_hermes import private_directory,Rejected
  other_uid=os.geteuid()+1
  with patch('probe_hermes.os.geteuid',return_value=other_uid),self.assertRaises(Rejected):private_directory(self.private)


class Budget512Tests(unittest.TestCase):
 def budget(self):return OuterBudget({'base_url':'https://example.invalid/v1','model':'qwen3.8-flash'},lambda *args:None,max_output_tokens=512)
 def body(self,limit,n=0):return json.dumps({'model':'qwen3.8-flash','max_completion_tokens':limit,'messages':[{'role':'user','content':str(n)}]}).encode()
 def test_512_limit_admitted_and_513_rejected_before_send(self):
  b=self.budget();b.admit(b.target,self.body(512))
  with self.assertRaises(RuntimeError):b.admit(b.target,self.body(513,1))
  self.assertEqual(len(b.calls),1);self.assertEqual(b.calls[0]['max_output_tokens'],512)
 def test_512_budget_still_refuses_fifth_request(self):
  b=self.budget()
  for n in range(4):b.admit(b.target,self.body(512,n))
  with self.assertRaises(RuntimeError):b.admit(b.target,self.body(512,4))
  self.assertEqual(len(b.calls),4)
 def test_unapproved_budget_configuration_rejected(self):
  for limit in [129,513,True,512.0]:
   with self.assertRaises(ValueError):OuterBudget({'base_url':'https://example.invalid/v1','model':'qwen3.8-flash'},lambda *args:None,max_output_tokens=limit)

if __name__=='__main__':unittest.main()
