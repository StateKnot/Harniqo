#!/usr/bin/env python3
"""No network: exercise request budgets at the actual service wrapper boundary."""
import json
from pathlib import Path
import tempfile
import unittest
from guard_octop_service import Budget

class BudgetTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
  self.out=Path(self.tmp.name)/'calls.json'
  self.b=Budget({'base_url':'https://example.invalid/v1','model':'qwen3.8-flash'},'receipt',self.out)
 def body(self,n=0):return json.dumps({'model':'qwen3.8-flash','messages':[{'role':'user','content':'receipt '+str(n)}]}).encode()
 def test_fifth_send_rejected_and_fourth_saved(self):
  for n in range(4):self.b.admit(self.b.target,self.body(n))
  with self.assertRaises(RuntimeError):self.b.admit(self.b.target,self.body(4))
  self.assertEqual(len(json.loads(self.out.read_text())['calls']),4)
 def test_auth_failure_latches_before_retry(self):
  record=self.b.admit(self.b.target,self.body());self.b.completed(record,401)
  with self.assertRaises(RuntimeError):self.b.admit(self.b.target,self.body(1))
  self.assertTrue(json.loads(self.out.read_text())['auth_failed'])
 def test_exact_retry_rejected(self):
  self.b.admit(self.b.target,self.body())
  with self.assertRaises(RuntimeError):self.b.admit(self.b.target,self.body())
 def test_wrong_target_model_or_background_rejected(self):
  cases=[(self.b.target+'x',self.body()),(self.b.target,self.body().replace(b'qwen3.8-flash',b'other')),
         (self.b.target,self.body().replace(b'receipt',b'background'))]
  for target,body in cases:
   with self.subTest(target=target,body=body),self.assertRaises(RuntimeError):self.b.admit(target,body)
  self.assertEqual(self.b.calls,[])

if __name__=='__main__':unittest.main()
