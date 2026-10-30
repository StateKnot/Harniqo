import copy
import unittest

from run_opencode_transport import evidence, sha


class TransportEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.base, self.model = 'https://provider.example.invalid/v1/', 'synthetic-model'
        endpoint = sha(b'https://provider.example.invalid/v1/chat/completions')
        self.records = [
            {'schema': 1, 'kind': 'request_started', 'request_id': 'one', 'url_sha256': endpoint,
             'model_known': True, 'model_sha256': sha(self.model.encode()), 'model_matches_expected': True},
            {'schema': 1, 'kind': 'response', 'request_id': 'one', 'status': 200, 'redirected': False,
             'response_url_sha256': endpoint, 'network_error': False},
        ]

    def test_exact_request_and_response_pair_required(self):
        self.assertTrue(evidence(self.records, self.base, self.model)['verified'])
        for records in ([], self.records[:1], self.records[1:], self.records*2):
            self.assertFalse(evidence(records, self.base, self.model)['verified'])

    def test_request_model_url_and_response_identity_are_independent(self):
        for index, key, value in [(0, 'model_sha256', 'different'), (0, 'url_sha256', 'different'),
                                  (0, 'model_known', False), (0, 'model_matches_expected', False),
                                  (1, 'request_id', 'different'), (1, 'response_url_sha256', 'different'),
                                  (1, 'redirected', True), (1, 'status', 302), (1, 'network_error', True)]:
            changed = copy.deepcopy(self.records); changed[index][key] = value
            self.assertFalse(evidence(changed, self.base, self.model)['verified'], key)

    def test_unknown_errors_and_budget_blocks_never_pass(self):
        for kind in ('network_error', 'request_blocked', 'unknown'):
            self.assertFalse(evidence(self.records + [{'schema': 1, 'kind': kind}], self.base, self.model)['verified'])
        self.assertFalse(evidence(self.records, self.base, self.model, maximum=0)['verified'])


if __name__ == '__main__':
    unittest.main()
