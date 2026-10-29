#!/usr/bin/env python3
"""Metadata assertions against nested native WS shapes, without model calls."""
import unittest
from probe_octop_service_model import response_models

class MetadataTests(unittest.TestCase):
 def test_nested_snapshots_and_updates(self):
  values=[{'type':'state_snapshot','values':{'messages':[{'response_metadata':{'model_name':'qwen3.8-flash'}}]}},
          {'type':'state_update','data':{'response_metadata':{'model':'qwen3.8-flash'}}}]
  self.assertEqual(response_models(values),{'qwen3.8-flash'})
 def test_absent_or_different_model_fails_exact_set(self):
  for value in [{}, {'response_metadata':{}}, {'response_metadata':{'model':'other'}},
                [{'response_metadata':{'model':'qwen3.8-flash'}},{'response_metadata':{'model':'other'}}]]:
   self.assertNotEqual(response_models(value),{'qwen3.8-flash'})

if __name__=='__main__':unittest.main()
