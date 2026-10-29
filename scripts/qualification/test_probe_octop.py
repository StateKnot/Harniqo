#!/usr/bin/env python3
"""Pure admission/result checks; fixtures never count as native execution."""
import copy
import unittest
from probe_octop import exact_approval, history_checks, record_checkpoint_reopen

class ProbeChecks(unittest.TestCase):
    def setUp(self):
        self.event={'request':{'action_requests':[{'name':'write_file','args':{'file_path':'/output.txt','content':'random-receipt'}}]}}
    def test_exact_action(self): self.assertTrue(exact_approval(self.event,'random-receipt',False))
    def test_whitespace_remains_significant(self): self.assertFalse(exact_approval(self.event,'random-receipt\n',False))
    def test_extra_arg_rejected(self):
        self.event['request']['action_requests'][0]['args']['unexpected']=True
        self.assertFalse(exact_approval(self.event,'random-receipt',False))
    def test_multiple_actions_rejected(self):
        self.event['request']['action_requests']*=2
        self.assertFalse(exact_approval(self.event,'random-receipt',False))
    def test_output_already_exists_rejected(self): self.assertFalse(exact_approval(self.event,'random-receipt',True))
    def test_wrong_target_rejected(self):
        self.event['request']['action_requests'][0]['args']['file_path']='/elsewhere'
        self.assertFalse(exact_approval(self.event,'random-receipt',False))
    def test_reopen_mismatch_revokes_success(self):
        report={'qualified':True}
        record_checkpoint_reopen(report,[{'id':'one'}],[{'id':'different'}])
        self.assertFalse(report['qualified'])
        self.assertFalse(report['checkpoint_reopen_matches'])
    def test_reopen_match_does_not_erase_prior_failure(self):
        report={'qualified':False}
        record_checkpoint_reopen(report,[{'id':'one'}],[{'id':'one'}])
        self.assertFalse(report['qualified'])
        self.assertTrue(report['checkpoint_reopen_matches'])
    def test_missing_model_never_matches(self):
        self.assertFalse(history_checks([{'type':'ai'}],'cancel','approved')['served_model_matches'])
    def test_no_native_write_not_success(self):
        messages=[{'type':'ai','response_metadata':{'model_name':'approved','finish_reason':'stop'}},
                  {'type':'tool','name':'read_file','status':'success'}]
        self.assertFalse(history_checks(messages,'allow','approved')['native_write_result'])
    def test_denied_write_must_be_native_error(self):
        messages=[{'type':'ai','response_metadata':{'model_name':'approved','finish_reason':'stop'}},
                  {'type':'tool','name':'write_file','status':'success','content':'rejected'}]
        self.assertFalse(history_checks(messages,'deny','approved')['native_write_result'])

if __name__=='__main__':unittest.main()
