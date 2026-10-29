#!/usr/bin/env python3
"""Component checks, including real httpx MockTransport when the SDK is present.

No HTTP listener, inference, gateway, VM or container is launched by these tests.
SDK tests must also run in the pinned Hermes image; a skip is not a pass there.
"""
from __future__ import annotations

import asyncio
from email.message import Message
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts/qualification"))
import probe_hermes_model_lock as probe


def load_audit():
    spec = importlib.util.spec_from_file_location("harniqo_transport_component", ROOT /
                                              "deploy/qualification/hermes/transport_audit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Response(io.BytesIO):
    status = 200

    def __init__(self, raw):
        super().__init__(raw)
        self.headers = Message()
        self.headers["Content-Type"] = "text/event-stream"


RUN = "run_" + "a" * 32
SESSION = "session_fixture"


def frame(seq, event="assistant.delta", run=RUN, session=SESSION, **fields):
    return ("event: " + event + "\ndata: " + json.dumps({"seq": seq, "run_id": run,
            "session_id": session, **fields}) + "\n\n").encode()


class StreamAndMetadataTests(unittest.TestCase):
    def test_native_wire_event_and_sequence_begin_at_one_without_cursor(self):
        raw = b": keepalive\n\n" + frame(1, "run.started") + frame(2) + frame(3, "done")
        seen = []
        events = probe.read_session_sse(Response(raw), SESSION, seen.append)
        self.assertEqual([e["seq"] for e in events], [1, 2, 3])
        self.assertEqual(seen, events)

    def test_wrong_session_run_gap_duplicate_cursor_and_post_done_rejected(self):
        cases = [frame(1, session="other"), frame(2), frame(1) + frame(1),
                 frame(1) + frame(2, run="run_" + "b" * 32), frame(1, "done") + frame(2),
                 b"id: 1\n" + frame(1), frame(1).replace(b'"seq": 1', b'"event":"invented","seq": 1')]
        for raw in cases:
            with self.subTest(raw=raw), self.assertRaises(probe.native.Rejected):
                probe.read_session_sse(Response(raw), SESSION)

    def test_incomplete_bounded_and_missing_done_rejected(self):
        for raw in (frame(1).rstrip(b"\n"), frame(1), b":" + b"x" * 65536 + b"\n"):
            with self.subTest(size=len(raw)), self.assertRaises(probe.native.Rejected):
                probe.read_session_sse(Response(raw), SESSION)

    def test_actual_model_provider_lock_and_usage_are_required(self):
        event = {"runtime": {"model": "approved", "provider": "custom", "model_lock": "confirmed",
                             "requested": {"model": "approved", "provider": "custom"}},
                 "usage": {"input_tokens": 10, "output_tokens": 3, "total_tokens": 13}}
        valid = probe.validate_locked_metadata(event, "approved")
        self.assertTrue(valid["native_model_lock_confirmed"])
        for key, value in (("model", "other"), ("model", ""), ("provider", "other"),
                           ("model_lock", "accepted"), ("requested", {"model": "other", "provider": "custom"})):
            bad = {**event, "runtime": {**event["runtime"], key: value}}
            with self.subTest(key=key, value=value), self.assertRaises(probe.native.Rejected):
                probe.validate_locked_metadata(bad, "approved")

    def test_request_echo_never_fills_missing_actual_metadata(self):
        with self.assertRaises(probe.native.Rejected) as caught:
            probe.validate_locked_metadata({"model": "approved", "runtime": {
                "provider": "custom", "model_lock": "confirmed", "requested": {"model": "approved"}},
                "usage": {"total_tokens": 10}}, "approved")
        self.assertEqual(caught.exception.code, "native_effective_model_not_reported")


class TransportEvidenceTests(unittest.TestCase):
    def sample(self, **changes):
        return {"schema_version": 1, "kind": "chat_completions_http_response", "method": "POST",
                "target_sha256": "a" * 64, "body_model_sha256": probe.native.digest(b"approved"),
                "status_code": 200, "redirect_count": 0, "network_error": False, **changes}

    def encoded(self, records):
        return b"".join((json.dumps(row) + "\n").encode() for row in records)

    def test_observed_endpoint_and_model_match_all_three_requests(self):
        value = probe.validate_transport(self.encoded([self.sample()] * 3), "a" * 64, "approved")
        self.assertEqual(value["requests"], 3)
        self.assertEqual(value["status"], "observed_matched")

    def test_mismatch_redirect_error_extra_fields_status_and_incomplete_rejected(self):
        bad = [{"target_sha256": "b" * 64}, {"body_model_sha256": "b" * 64},
               {"redirect_count": 1}, {"redirect_count": False}, {"network_error": True, "status_code": 0},
               {"status_code": True}, {"status_code": 0}, {"status_code": 429},
               {"headers": {"Authorization": "synthetic"}}]
        for change in bad:
            with self.subTest(change=change), self.assertRaises(probe.native.Rejected):
                probe.validate_transport(self.encoded([self.sample()] * 3 + [self.sample(**change)]),
                                         "a" * 64, "approved")
        for raw in (b"", self.encoded([self.sample()] * 2), self.encoded([self.sample()] * 3).rstrip(b"\n")):
            with self.subTest(size=len(raw)), self.assertRaises(probe.native.Rejected):
                probe.validate_transport(raw, "a" * 64, "approved")


class AuditFileTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="harniqo-transport-component-")
        self.directory = Path(self.temporary.name).resolve()
        self.audit = load_audit()
        self.audit.WORKSPACE = self.directory

    def tearDown(self):
        self.temporary.cleanup()

    def test_real_private_file_append_and_bounded_hash_only_record(self):
        request = types.SimpleNamespace(method="POST", content=b'{"model":"approved","api_key":"synthetic"}',
                                        url="https://synthetic.invalid/v1/chat/completions")
        self.audit.record_send(request, types.SimpleNamespace(request=request, status_code=200, history=[]))
        path = self.directory / self.audit.FILENAME
        raw = path.read_bytes()
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertNotIn(b"synthetic", raw)
        self.assertNotIn(b"approved", raw)
        self.assertEqual(json.loads(raw)["body_model_sha256"], probe.native.digest(b"approved"))

    def test_log_symlink_and_unsafe_permissions_rejected(self):
        target = self.directory / "other"
        target.write_bytes(b"untouched")
        path = self.directory / self.audit.FILENAME
        path.symlink_to(target)
        with self.assertRaises(OSError):
            self.audit.append_record({"fixture": True})
        self.assertEqual(target.read_bytes(), b"untouched")
        path.unlink()
        path.write_bytes(b"")
        path.chmod(0o644)
        with self.assertRaisesRegex(RuntimeError, "file_permissions"):
            self.audit.append_record({"fixture": True})

    def test_workspace_symlink_unsafe_directory_and_size_rejected(self):
        linked = self.directory / "link"
        linked.symlink_to(self.directory, target_is_directory=True)
        self.audit.WORKSPACE = linked
        with self.assertRaisesRegex(RuntimeError, "workspace_symlink"):
            self.audit.append_record({"fixture": True})
        self.audit.WORKSPACE = self.directory
        self.directory.chmod(0o755)
        with self.assertRaisesRegex(RuntimeError, "workspace_permissions"):
            self.audit.append_record({"fixture": True})
        self.directory.chmod(0o700)
        path = self.directory / self.audit.FILENAME
        path.write_bytes(b"x" * self.audit.MAX_LOG)
        path.chmod(0o600)
        with self.assertRaisesRegex(RuntimeError, "log_size"):
            self.audit.append_record({"fixture": True})

    def test_unparseable_missing_and_large_model_fail_matching(self):
        expected = probe.native.digest(b"approved")
        for raw in (b"invalid", b"{}", b'{"model":null}', b'{"model":"other","model":"approved"}',
                    b'{"model":"approved","bad":NaN}', b"x" * (self.audit.MAX_BODY + 1)):
            self.assertNotEqual(self.audit._model_hash(types.SimpleNamespace(content=raw)), expected)


try:
    import httpx
except ImportError:
    httpx = None


@unittest.skipIf(httpx is None, "real httpx SDK is only available in the pinned runtime")
class ActualSDKTests(unittest.TestCase):
    setUp = AuditFileTests.setUp
    tearDown = AuditFileTests.tearDown
    def test_real_sync_sdk_send_is_unchanged_and_installer_is_idempotent(self):
        actual = []
        def receive(request):
            actual.append(request)
            return httpx.Response(200, json={"ok": True})
        with patch.object(httpx.Client, "send", httpx.Client.send), \
                patch.object(httpx.AsyncClient, "send", httpx.AsyncClient.send):
            self.audit.install()
            installed = httpx.Client.send
            self.audit.install()
            self.assertIs(httpx.Client.send, installed)
            with httpx.Client(transport=httpx.MockTransport(receive)) as client:
                response = client.post("https://synthetic.invalid/v1/chat/completions", json={"model": "approved"})
                self.assertEqual(response.json(), {"ok": True})
                client.get("https://synthetic.invalid/models")
        raw = (self.directory / self.audit.FILENAME).read_bytes()
        self.assertEqual(len(raw.splitlines()), 1)
        self.assertEqual(len(actual), 2)

    def test_real_sdk_redirect_observes_final_target_and_network_failure_is_recorded(self):
        def receive(request):
            if request.url.host == "synthetic.invalid":
                return httpx.Response(307, headers={"location": "https://redirect.invalid/other"})
            return httpx.Response(200, json={"ok": True})
        with patch.object(httpx.Client, "send", httpx.Client.send), \
                patch.object(httpx.AsyncClient, "send", httpx.AsyncClient.send):
            self.audit.install()
            with httpx.Client(transport=httpx.MockTransport(receive), follow_redirects=True) as client:
                client.post("https://synthetic.invalid/v1/chat/completions", json={"model": "approved"})
            def fail(request):
                raise httpx.ConnectError("synthetic secret is never recorded", request=request)
            with httpx.Client(transport=httpx.MockTransport(fail)) as client:
                with self.assertRaises(httpx.ConnectError):
                    client.post("https://synthetic.invalid/v1/chat/completions", json={"model": "approved"})
        rows = [json.loads(row) for row in (self.directory / self.audit.FILENAME).read_bytes().splitlines()]
        self.assertEqual(rows[0]["redirect_count"], 1)
        self.assertEqual(rows[0]["target_sha256"], probe.native.digest(b"https://redirect.invalid/other"))
        self.assertTrue(rows[1]["network_error"])
        self.assertEqual(rows[1]["status_code"], 0)

    def test_real_async_sdk_observed_once(self):
        async def run():
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200))) as client:
                response = await client.post("https://synthetic.invalid/v1/chat/completions", json={"model": "approved"})
                self.assertEqual(response.status_code, 200)
        with patch.object(httpx.Client, "send", httpx.Client.send), \
                patch.object(httpx.AsyncClient, "send", httpx.AsyncClient.send):
            self.audit.install()
            asyncio.run(run())
        self.assertEqual(len((self.directory / self.audit.FILENAME).read_bytes().splitlines()), 1)


if __name__ == "__main__":
    unittest.main()
