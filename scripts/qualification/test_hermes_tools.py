#!/usr/bin/env python3
"""Component evidence only: bounded native profile tools and probe validation.

No model, HTTP listener, container, VM or native gateway is executed.
"""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
from email.message import Message
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


PROBE = load("harniqo_native_probe", ROOT / "scripts/qualification/probe_hermes.py")


class Response(io.BytesIO):
    status = 200

    def __init__(self, data):
        super().__init__(data)
        self.headers = Message()
        self.headers["Content-Type"] = "text/event-stream"


def frame(seq, name="tool.started", run_id="run_test"):
    return (f"id: {seq}\ndata: " + json.dumps({"seq": seq, "event": name, "run_id": run_id}) + "\n\n").encode()


class SSEValidationTests(unittest.TestCase):
    def test_real_frame_format_and_cursor_resume(self):
        first = PROBE.read_sse(Response(b": open\n\n" + frame(0) + frame(1)), "run_test", first_only=True)
        rest = PROBE.read_sse(Response(b": open\n\n" + frame(1) + frame(2, "run.completed")), "run_test", previous=0)
        self.assertEqual([e["seq"] for e in first + rest], [0, 1, 2])

    def test_duplicate_gap_wrong_run_and_missing_cursor_rejected(self):
        for raw, cursor in [(frame(0), 0), (frame(2), 0), (frame(0, run_id="other"), -1),
                            (b'data: {"event":"replay.truncated"}\n\n', -1)]:
            with self.subTest(raw=raw), self.assertRaises(PROBE.Rejected):
                PROBE.read_sse(Response(raw), "run_test", previous=cursor)

    def test_incomplete_frame_and_oversized_line_rejected(self):
        for raw in [b'id: 0\ndata: {"seq":0}', b":" + b"x" * 65536 + b"\n"]:
            with self.subTest(length=len(raw)), self.assertRaises(PROBE.Rejected):
                PROBE.read_sse(Response(raw), "run_test")

    def test_duplicate_json_key_and_nonfinite_values_rejected(self):
        for raw in ['{"seq":0,"seq":1}', '{"value":NaN}']:
            with self.subTest(raw=raw), self.assertRaises(PROBE.Rejected):
                PROBE.json_value(raw)


class ProfileToolTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="harniqo-tool-component-")
        self.workspace = Path(self.temporary.name).resolve()
        self.registrations = []
        fake = types.ModuleType("tools.registry")
        fake.registry = types.SimpleNamespace(register=lambda **kwargs: self.registrations.append(kwargs))
        with patch.dict("sys.modules", {"tools.registry": fake}):
            self.tools = load("harniqo_component_tools", ROOT / "deploy/qualification/hermes/entrypoint.py")
        self.tools.WORKSPACE = self.workspace
        self.nonce = "a4e4e8dcee8fddbb"
        self.content = "qualification component ✓"

    def tearDown(self):
        self.temporary.cleanup()

    def loop(self):
        written = json.loads(self.tools.qualification_write_file({"nonce": self.nonce, "content": self.content}))
        read = json.loads(self.tools.qualification_read_file({"nonce": self.nonce}))
        raw = (self.workspace / self.tools.RESULT).read_bytes()
        return written, read, raw

    def test_registration_and_real_write_read_receipt_hash(self):
        self.assertEqual([r["name"] for r in self.registrations], list(PROBE.TOOLS))
        written, read, raw = self.loop()
        self.assertTrue(written["ok"])
        self.assertRegex(written["receipt"], r"^[0-9a-f]{48}$")
        self.assertEqual(read["receipt"], written["receipt"])
        self.assertEqual(read["content"], self.content)
        self.assertEqual(written["sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(read["sha256"], written["sha256"])
        self.assertEqual((self.workspace / self.tools.RESULT).stat().st_mode & 0o777, 0o600)

    def test_unknown_path_field_oversize_and_bad_nonce_rejected(self):
        cases = [{"nonce": self.nonce, "content": "x", "path": "../escape"},
                 {"nonce": "../escape", "content": "x"},
                 {"nonce": self.nonce, "content": "中" * 683},
                 {"nonce": self.nonce, "content": "\ud800"},
                 {"nonce": self.nonce, "content": "x" * 2049}]
        for args in cases:
            with self.subTest(args_keys=list(args)):
                self.assertIn("error", json.loads(self.tools.qualification_write_file(args)))
        self.assertEqual(list(self.workspace.iterdir()), [])

    def test_reader_rejects_wrong_nonce_symlink_and_unsafe_permissions(self):
        _, _, raw = self.loop()
        self.assertIn("error", json.loads(self.tools.qualification_read_file({"nonce": "bbbbbbbb"})))
        path = self.workspace / self.tools.RESULT
        path.chmod(0o644)
        self.assertIn("error", json.loads(self.tools.qualification_read_file({"nonce": self.nonce})))
        path.unlink()
        target = self.workspace / "unrelated-private"
        target.write_bytes(raw)
        target.chmod(0o600)
        path.symlink_to(target)
        self.assertIn("error", json.loads(self.tools.qualification_read_file({"nonce": self.nonce})))
        self.assertEqual(target.read_bytes(), raw)

    def test_symlink_workspace_rejected(self):
        link = self.workspace / "linked-workspace"
        link.symlink_to(self.workspace, target_is_directory=True)
        self.tools.WORKSPACE = link
        result = json.loads(self.tools.qualification_write_file({"nonce": self.nonce, "content": "x"}))
        self.assertIn("error", result)
        self.assertFalse((self.workspace / self.tools.RESULT).exists())

    def messages(self, written, read):
        return [{"role": "assistant", "tool_calls": [{"id": "call_write", "function": {
                    "name": PROBE.TOOLS[0], "arguments": json.dumps({"nonce": self.nonce, "content": self.content})}}]},
                {"role": "tool", "tool_call_id": "call_write", "content": json.dumps(written)},
                {"role": "assistant", "tool_calls": [{"id": "call_read", "function": {
                    "name": PROBE.TOOLS[1], "arguments": json.dumps({"nonce": self.nonce})}}]},
                {"role": "tool", "tool_call_id": "call_read", "content": json.dumps(read)},
                {"role": "assistant", "content": self.nonce + " " + read["receipt"]}]

    def test_probe_validates_actual_component_artifact_and_native_shape(self):
        written, read, raw = self.loop()
        value = PROBE.validate_tools(self.messages(written, read), raw, self.nonce, self.content)
        self.assertTrue(value["receipt_verified"])
        self.assertEqual(value["artifact_sha256"], hashlib.sha256(raw).hexdigest())

    def test_invented_receipt_tool_result_or_call_identity_cannot_pass(self):
        written, read, raw = self.loop()
        for fault in ["receipt", "call_id", "tool_name", "final", "argument", "extra_call"]:
            messages = self.messages(written, read)
            if fault == "receipt":
                bad = dict(read, receipt="0" * 48)
                messages[3]["content"] = json.dumps(bad)
            elif fault == "call_id":
                messages[3]["tool_call_id"] = "call_other"
            elif fault == "tool_name":
                messages[0]["tool_calls"][0]["function"]["name"] = "terminal"
            elif fault == "final":
                messages[-1]["content"] = "invented_receipt"
            elif fault == "argument":
                messages[2]["tool_calls"][0]["function"]["arguments"] = '{"nonce":"different"}'
            else:
                messages.insert(0, messages[0])
            with self.subTest(fault=fault), self.assertRaises(PROBE.Rejected):
                PROBE.validate_tools(messages, raw, self.nonce, self.content)


if __name__ == "__main__":
    unittest.main()
