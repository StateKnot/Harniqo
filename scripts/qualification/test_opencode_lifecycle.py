#!/usr/bin/env python3
"""Offline native lifecycle判据 tests; no listener, Docker or paid model."""
import copy
from pathlib import Path
import unittest

import run_opencode_qualification as probe


class NativeEvidenceTests(unittest.TestCase):
    def history(self):
        return [{'info': {'role': 'assistant', 'modelID': 'test-model', 'providerID': 'harniqo',
                          'time': {'created': 1}},
                 'parts': [{'type': 'text', 'text': '1. Fictional'}]}]

    def test_generation_requires_content_busy_and_unfinished(self):
        history = self.history()
        self.assertTrue(probe.is_streaming(history, {'type': 'busy'}))
        self.assertFalse(probe.is_streaming(history, {'type': 'idle'}))
        self.assertFalse(probe.is_streaming([], {'type': 'busy'}))
        for update in ({'completed': 2},):
            history[0]['info']['time'].update(update)
            self.assertFalse(probe.is_streaming(history, {'type': 'busy'}))
        history = self.history();history[0]['parts'] = []
        self.assertFalse(probe.is_streaming(history, {'type': 'busy'}))

    def test_cancel_requires_native_abort_completion_and_idle(self):
        history = self.history()
        self.assertFalse(probe.cancelled(history, {'type': 'idle'}))
        history[0]['info'].update(error={'name': 'MessageAbortedError'}, time={'completed': 2})
        self.assertTrue(probe.cancelled(history, {'type': 'idle'}))
        self.assertFalse(probe.cancelled(history, {'type': 'busy'}))
        history[0]['info']['error']['name'] = 'APIError'
        self.assertFalse(probe.cancelled(history, {'type': 'idle'}))

    def test_stream_delta_must_match_current_native_message(self):
        history = self.history();history[0]['parts'] = []
        history[0]['info'].update(id='msg1', sessionID='ses1')
        event = {'type': 'message.part.delta', 'properties': {'sessionID': 'ses1', 'messageID': 'msg1', 'field': 'text', 'delta': '1.'}}
        self.assertTrue(probe.is_streaming(history, {'type': 'busy'}, event))
        for key, value in [('sessionID', 'other'), ('messageID', 'other'), ('field', 'tool'), ('delta', '')]:
            changed = copy.deepcopy(event);changed['properties'][key] = value
            self.assertFalse(probe.is_streaming(history, {'type': 'busy'}, changed))

    def test_pending_or_running_tool_blocks_stop_claim(self):
        history = self.history()
        history[0]['info'].update(error={'name': 'MessageAbortedError'}, time={'completed': 2})
        for status in ('running', 'pending'):
            value = copy.deepcopy(history)
            value[0]['parts'].append({'type': 'tool', 'state': {'status': status}})
            self.assertFalse(probe.cancelled(value, {'type': 'idle'}))

    def test_model_identity_covers_all_native_assistant_messages(self):
        history = self.history()
        self.assertTrue(probe.configured_model(history, 'test-model'))
        self.assertFalse(probe.configured_model([], 'test-model'))
        history += copy.deepcopy(history)
        history[-1]['info']['providerID'] = 'different'
        self.assertFalse(probe.configured_model(history, 'test-model'))

    def test_launch_reuses_exact_state_only_and_readonly_credentials(self):
        args = probe.launch('test', 'attempt', Path('/case'), Path('/prior'), Path('/private/key'))
        self.assertEqual(args[-1], probe.IMAGE)
        self.assertIn('127.0.0.1:10012:4096', args)
        self.assertIn('type=bind,src=/prior/home,dst=/data/opencode', args)
        self.assertIn('type=bind,src=/private/key,dst=/run/secrets/model-api-key,readonly', args)
        self.assertNotIn('--env', args)
        self.assertIn('--read-only', args)
        self.assertIn('no-new-privileges', args)

    def test_submit_records_unknown_outcome_and_never_retries(self):
        import tempfile
        class FailedTransport:
            calls = 0
            def request(self, path, payload):
                self.calls += 1
                raise OSError('unknown transport outcome')
        with tempfile.TemporaryDirectory() as tmp:
            client = FailedTransport();case = Path(tmp)
            with self.assertRaises(OSError):
                probe.submit(client, case, 'session-test', 'test-model', 'fixed prompt')
            self.assertTrue((case/'submission.json').is_file())
            with self.assertRaises(FileExistsError):
                probe.submit(client, case, 'session-test', 'test-model', 'fixed prompt')
            self.assertEqual(client.calls, 1)


if __name__ == '__main__':
    unittest.main()
