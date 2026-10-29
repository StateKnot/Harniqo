#!/usr/bin/env python3
"""Component-only lifecycle evidence: real files and typed native seam substitutes.

No model, native gateway, HTTP listener, Docker or VM is executed. Native
interrupt/approval transport behavior requires the independent live probe.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch

import probe_hermes_lifecycle as lifecycle

ROOT = Path(__file__).resolve().parents[2]


def load_profile():
    registry = types.ModuleType("tools.registry")
    registry.registry = types.SimpleNamespace(register=lambda **_kw: None)
    audit = types.ModuleType("transport_audit")
    audit.install = lambda: None
    spec = importlib.util.spec_from_file_location("lifecycle_profile", ROOT / "deploy/qualification/hermes/entrypoint.py")
    module = importlib.util.module_from_spec(spec)
    with patch.dict("sys.modules", {"tools.registry": registry, "transport_audit": audit}):
        spec.loader.exec_module(module)
    return module


class LifecycleToolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="harniqo-lifecycle-")
        self.workspace = Path(self.tmp.name).resolve()
        self.profile = load_profile()
        self.profile.WORKSPACE = self.workspace
        self.nonce = "85c317859bad4288"

    def tearDown(self):
        self.tmp.cleanup()

    def gate(self, result):
        fake = types.ModuleType("tools.approval")
        fake.request_tool_approval = lambda tool, reason, **kwargs: result
        return patch.dict("sys.modules", {"tools.approval": fake})

    def test_native_gate_denial_and_ambiguous_results_never_write_marker(self):
        for result in [{"approved": False}, {}, None, {"approved": "yes"}, {"approved": 1}]:
            with self.subTest(result=result), self.gate(result):
                value = json.loads(self.profile.qualification_approved_write({"nonce": self.nonce}))
            self.assertTrue(value["denied"])
            self.assertEqual(value["approval_gate"], "native_request_tool_approval")
            self.assertFalse((self.workspace / "qualification-approved-write.json").exists())

    def test_explicit_gate_approval_writes_only_fixed_private_marker(self):
        with self.gate({"approved": True}):
            value = json.loads(self.profile.qualification_approved_write({"nonce": self.nonce}))
        self.assertTrue(value["ok"])
        self.assertFalse(value["denied"])
        marker = self.workspace / "qualification-approved-write.json"
        self.assertEqual(json.loads(marker.read_text()), {"nonce": self.nonce, "receipt": value["receipt"]})
        self.assertEqual(marker.stat().st_mode & 0o777, 0o600)
        self.assertEqual(list(self.workspace.iterdir()), [marker])

    def test_invalid_arguments_never_reach_native_gate_or_wait(self):
        for method in [self.profile.qualification_wait, self.profile.qualification_approved_write]:
            for args in [{"nonce": "../bad"}, {"nonce": self.nonce, "path": "escape"}, {}, {"nonce": 123}]:
                with self.subTest(method=method.__name__, args=args):
                    self.assertIn("error", json.loads(method(args)))
        self.assertEqual(list(self.workspace.iterdir()), [])

    def wait_context(self, flags, clock):
        fake = types.ModuleType("tools.interrupt")
        fake.is_interrupted = lambda: next(flags)
        # /proc stat fields3–22: state,18fields,starttime. Name contains a ')'.
        stat = "123 (tool worker) odd)) S " + "0 " * 18 + "98421\n"
        return fake, stat, types.SimpleNamespace(monotonic=lambda: next(clock), sleep=lambda _: None)

    def test_native_interrupt_marker_tied_to_actual_tool_identity_no_natural_finish(self):
        fake, stat, clock = self.wait_context(iter([False, True]), iter([0, 0, 1]))
        with patch.dict("sys.modules", {"tools.interrupt": fake}), patch.object(Path, "read_text", return_value=stat), \
                patch.object(self.profile, "time", clock):
            value = json.loads(self.profile.qualification_wait({"nonce": self.nonce}))
        started = json.loads((self.workspace / "qualification-wait-started.json").read_text())
        stopped = json.loads((self.workspace / "qualification-wait-interrupted.json").read_text())
        self.assertEqual(started, stopped)
        self.assertEqual(value["status"], "native_interrupt_observed")
        self.assertEqual(started["start_ticks"], 98421)
        self.assertRegex(started["receipt"], r"^[0-9a-f]{48}$")
        self.assertFalse((self.workspace / "qualification-wait-finished.json").exists())
        self.assertEqual((self.workspace / "qualification-wait-interrupted.json").stat().st_mode & 0o777, 0o600)

    def test_natural_expiry_produces_distinct_marker_never_interrupt_evidence(self):
        fake, stat, clock = self.wait_context(iter([]), iter([0, 46]))
        with patch.dict("sys.modules", {"tools.interrupt": fake}), patch.object(Path, "read_text", return_value=stat), \
                patch.object(self.profile, "time", clock):
            value = json.loads(self.profile.qualification_wait({"nonce": self.nonce}))
        self.assertEqual(value["status"], "wait_expired")
        self.assertTrue((self.workspace / "qualification-wait-finished.json").exists())
        self.assertFalse((self.workspace / "qualification-wait-interrupted.json").exists())

    def test_unknown_marker_or_symlink_workspace_cannot_escape(self):
        with self.assertRaises(ValueError):
            self.profile._write_marker("../escape.json", {})
        target = self.workspace / "target"
        target.mkdir()
        link = self.workspace / "link"
        link.symlink_to(target, target_is_directory=True)
        self.profile.WORKSPACE = link
        with self.assertRaises(ValueError):
            self.profile._write_marker("qualification-wait-started.json", {})
        self.assertEqual(list(target.iterdir()), [])


class EvidenceValidationTests(unittest.TestCase):
    def marker(self):
        return {"nonce": "85c317859bad4288", "receipt": "a" * 48, "pid": 123, "start_ticks": 98421}

    def test_worker_marker_rejects_wrong_nonce_boolean_pid_and_missing_birth(self):
        value = self.marker()
        self.assertEqual(lifecycle.validate_marker(value, value["nonce"]), value)
        for changed in [dict(value, nonce="different"), dict(value, pid=True), dict(value, pid=1),
                        dict(value, start_ticks=0), {k: v for k, v in value.items() if k != "start_ticks"},
                        dict(value, receipt="invented")]:
            with self.subTest(value=changed), self.assertRaises(lifecycle.native.Rejected):
                lifecycle.validate_marker(changed, value["nonce"])

    def pending(self):
        return {"status": "waiting_for_approval", "execution_generation": 3, "pending_controls": [
            {"kind": "approval", "prompt_id": "approval_123", "execution_generation": 3,
             "description": "Synthetic marker write requires approval", "choices": ["once", "deny"]}]}

    def test_native_pending_requires_current_generation_exact_request_and_reason(self):
        value = self.pending()
        self.assertEqual(lifecycle.validate_pending(value)["prompt_id"], "approval_123")
        for name in ["generation", "reason", "request", "choices", "status", "multiple"]:
            value = self.pending()
            p = value["pending_controls"][0]
            if name == "generation": p["execution_generation"] = 2
            if name == "reason": p["description"] = " "
            if name == "request": p["prompt_id"] = ""
            if name == "choices": p["choices"] = ["once"]
            if name == "status": value["status"] = "completed"
            if name == "multiple": value["pending_controls"].append(dict(p))
            with self.subTest(fault=name), self.assertRaises(lifecycle.native.Rejected):
                lifecycle.validate_pending(value)

    def messages(self):
        nonce = self.marker()["nonce"]
        return [{"role": "assistant", "tool_calls": [{"id": "call_123", "function": {
            "name": "qualification_approved_write", "arguments": json.dumps({"nonce": nonce})}}]},
                {"role": "tool", "tool_call_id": "call_123", "content": json.dumps(
                    {"ok": False, "denied": True, "nonce": nonce, "approval_gate": "native_request_tool_approval"})}]

    def test_real_denial_call_result_identity_and_nonce_are_required(self):
        nonce = self.marker()["nonce"]
        self.assertEqual(lifecycle.validate_denied_history(self.messages(), nonce)["message_count"], 2)
        for fault in ["nonce", "identity", "approved", "tool", "extra_call"]:
            messages = self.messages()
            if fault == "nonce": messages[0]["tool_calls"][0]["function"]["arguments"] = '{"nonce":"wrong"}'
            if fault == "identity": messages[1]["tool_call_id"] = "wrong"
            if fault == "approved": messages[1]["content"] = '{"ok":true}'
            if fault == "tool": messages[0]["tool_calls"][0]["function"]["name"] = "write_file"
            if fault == "extra_call": messages.append(messages[0])
            with self.subTest(fault=fault), self.assertRaises(lifecycle.native.Rejected):
                lifecycle.validate_denied_history(messages, nonce)

    def cancellation_messages(self):
        identity = self.marker()
        return [{"role": "assistant", "tool_calls": [{"id": "call_wait", "function": {
            "name": "qualification_wait", "arguments": json.dumps({"nonce": identity["nonce"]})}}]},
                {"role": "tool", "tool_call_id": "call_wait", "content": json.dumps(
                    {"ok": False, "status": "native_interrupt_observed", **identity})}]

    def test_cancellation_requires_completed_handler_future_result(self):
        evidence = lifecycle.validate_cancelled_history(self.cancellation_messages(), self.marker())
        self.assertTrue(evidence["handler_future_completed"])
        self.assertTrue(evidence["native_per_call_executor_joined"])

    def test_synthetic_abandonment_partial_result_and_forged_identity_cannot_confirm_stop(self):
        identity = self.marker()
        for fault in ["synthetic", "missing", "receipt", "nonce", "pid", "birth", "expired", "wrong_call", "extra"]:
            messages = self.cancellation_messages()
            value = json.loads(messages[1]["content"])
            if fault == "synthetic": messages[1]["content"] = "[Tool execution cancelled — qualification_wait was abandoned]"
            elif fault == "missing": messages.pop()
            elif fault == "wrong_call": messages[1]["tool_call_id"] = "another_call"
            elif fault == "extra": messages.append(messages[0])
            else:
                if fault == "receipt": value["receipt"] = "b" * 48
                if fault == "nonce": value["nonce"] = "wrong_nonce"
                if fault == "pid": value["pid"] += 1
                if fault == "birth": value["start_ticks"] += 1
                if fault == "expired": value["status"] = "wait_expired"
                messages[1]["content"] = json.dumps(value)
            with self.subTest(fault=fault), self.assertRaises(lifecycle.native.Rejected):
                lifecycle.validate_cancelled_history(messages, identity)


if __name__ == "__main__":
    unittest.main()
