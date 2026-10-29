#!/usr/bin/env python3
"""Safety/parser tests using synthetic HTTP responses, never real-model evidence.

Run: python3 scripts/qualification/test_probe_model.py -v
No credential, provider URL or live model is needed. These tests do not qualify
a Harness, prove a provider's identity, or evaluate actual model reasoning.
"""

from contextlib import redirect_stdout
import errno
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import secrets
import stat
import tempfile
import threading
import unittest
from unittest.mock import patch

import probe_model as probe


def loopback_server(handler):
    """Bind only in the user-approved host port range; never choose port 0."""
    for port in range(10080, 10101):
        try:
            return ThreadingHTTPServer(("127.0.0.1", port), handler)
        except OSError as error:
            if error.errno != errno.EADDRINUSE:
                raise
    raise RuntimeError("No free loopback test port in the approved 10080-10100 range")


class SyntheticHandler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_POST(self):
        data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        server = self.server
        server.requests.append({"json": data, "authorization": self.headers.get("Authorization")})
        scenario = server.scenario
        if scenario == "http_error":
            self.send_response(401)
            self.end_headers()
            self.wfile.write(("upstream private body " + server.secret + " " + server.url).encode())
            return
        if scenario == "redirect":
            self.send_response(307)
            self.send_header("Location", server.redirect_target)
            self.end_headers()
            return
        if scenario == "malformed_json":
            body = ("invalid JSON " + server.secret).encode()
        elif scenario == "oversized":
            body = b"x" * (probe.MAX_RESPONSE_BYTES + 1)
        else:
            first = "tool" not in {item["role"] for item in data["messages"]}
            if first:
                nonce = data["tools"][0]["function"]["parameters"]["properties"]["nonce"]["const"]
                arguments = {"nonce": nonce, "note": "synthetic fixture"}
                if scenario == "bad_nonce":
                    arguments["nonce"] = "wrong-nonce"
                if scenario == "arbitrary_path":
                    arguments["path"] = str(server.forbidden_path)
                text = json.dumps(arguments)
                if scenario == "duplicate_argument":
                    text = '{"nonce":' + json.dumps(nonce) + ',"note":"a","note":"b"}'
                message = {"role": "assistant", "content": None, "tool_calls": [
                    {"id": "call-fixture-1", "type": "function", "function": {
                        "name": "shell" if scenario == "unknown_tool" else "record_probe", "arguments": text}}]}
                if scenario == "multiple_tools":
                    message["tool_calls"] *= 2
                reason = "tool_calls"
            else:
                result = json.loads(data["messages"][-1]["content"])
                answer = {"nonce": result["nonce"], "receipt": result["receipt"]}
                if scenario == "invented_receipt":
                    answer["receipt"] = "not-the-real-receipt"
                message = {"role": "assistant", "content": json.dumps(answer)}
                reason = "stop"
            response = {"id": "completion-fixture-1", "model": data["model"],
                        "choices": [{"index": 0, "message": message, "finish_reason": reason}],
                        "usage": {"prompt_tokens": 10, "completion_tokens": 12, "total_tokens": 22}}
            if scenario == "model_mismatch":
                response["model"] = "unexpected-model"
            if scenario == "reflected_metadata":
                response["id"] = server.secret
                response["usage"]["debug"] = server.secret
            body = json.dumps(response).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("x-request-id", server.secret if scenario == "reflected_metadata" else "request-fixture-1")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass


class ModelProbeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="harniqo-model-probe-", dir=Path(tempfile.gettempdir()).resolve())
        self.root = Path(self.temporary.name)
        self.private = self.root / ".private"
        self.private.mkdir(mode=0o700)
        self.secret = secrets.token_urlsafe(24)
        self.server = loopback_server(SyntheticHandler)
        self.server.requests = []
        self.server.secret = self.secret
        self.server.scenario = "success"
        self.server.url = f"http://127.0.0.1:{self.server.server_port}/v1"
        self.server.forbidden_path = self.root / "must-not-be-written"
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        self.key = self.private / "credential"
        self.key.write_text(self.secret)
        self.key.chmod(0o600)
        self.config = self.private / "models.json"
        self.endpoint = {"id": "fixture", "base_url": self.server.url,
                         "model": "fixture-model", "api_key_file": "credential"}
        self.write_config()
        self.report = self.private / "reports" / "report.json"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temporary.cleanup()

    def write_config(self, *, raw=None):
        self.config.write_text(raw if raw is not None else json.dumps({"endpoints": [self.endpoint]}))
        self.config.chmod(0o600)

    def run_probe(self, *, flags=None, loopback=True, report=None):
        args = ["--config", str(self.config), "--endpoint-id", "fixture", "--report", str(report or self.report),
                "--timeout", "2", "--max-tokens", "128"]
        if loopback:
            args.append("--allow-http-loopback")
        args.extend(flags or [])
        capture = io.StringIO()
        with redirect_stdout(capture):
            code = probe.main(args)
        output = capture.getvalue()
        self.assertNotIn(self.secret, output)
        self.assertNotIn(self.server.url, output)
        result = json.loads(output)
        saved = json.loads(self.report.read_text()) if self.report.exists() else None
        if saved:
            text = self.report.read_text()
            self.assertNotIn(self.secret, text)
            self.assertNotIn(self.server.url, text)
        return code, result, saved

    def assert_failure(self, scenario, error, *, requests=1):
        self.server.scenario = scenario
        code, _, report = self.run_probe()
        self.assertEqual(code, 1)
        self.assertEqual(report["verdict"], "failed")
        self.assertEqual(report["error"]["code"], error)
        self.assertEqual(len(self.server.requests), requests)
        return report

    def test_two_round_receipt_and_fixed_artifact(self):
        code, summary, report = self.run_probe()
        self.assertEqual(code, 0)
        self.assertEqual(summary["verdict"], "passed")
        self.assertEqual(report["evidence_kind"], "transport_fixture_only")
        self.assertEqual(report["socket_timeout_seconds"], 2)
        self.assertNotIn("timeout_seconds_per_request", report)
        self.assertFalse(report["streaming_tested"])
        self.assertEqual(len(self.server.requests), 2)
        for request in self.server.requests:
            self.assertEqual(request["authorization"], "Bearer " + self.secret)
            self.assertFalse(request["json"]["stream"])
        artifact = self.report.parent / report["controlled_tool"]["artifact"]
        data = json.loads(artifact.read_text())
        self.assertEqual(stat.S_IMODE(artifact.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.report.stat().st_mode), 0o600)
        self.assertEqual(hashlib.sha256(artifact.read_bytes()).hexdigest(), report["controlled_tool"]["artifact_sha256"])
        self.assertEqual(len(data["receipt"]), 48)
        self.assertNotIn(data["receipt"], json.dumps(self.server.requests[0]["json"]))
        self.assertTrue(report["controlled_tool"]["receipt_verified_in_final_response"])

    def test_invented_final_receipt_is_not_accepted(self):
        self.assert_failure("invented_receipt", "TOOL_RECEIPT_NOT_USED", requests=2)

    def test_bad_nonce_stops_before_tool_or_second_request(self):
        self.assert_failure("bad_nonce", "TOOL_NONCE_MISMATCH")
        self.assertFalse((self.report.parent / "report.artifacts").exists())

    def test_arbitrary_model_path_is_never_used(self):
        self.assert_failure("arbitrary_path", "TOOL_ARGUMENT_FIELDS")
        self.assertFalse(self.server.forbidden_path.exists())

    def test_unknown_tool_cannot_execute_shell(self):
        self.assert_failure("unknown_tool", "UNEXPECTED_TOOL_NAME")

    def test_multiple_tools_are_rejected(self):
        self.assert_failure("multiple_tools", "EXPECTED_SINGLE_TOOL_CALL")

    def test_duplicate_json_argument_is_rejected(self):
        self.assert_failure("duplicate_argument", "INVALID_TOOL_ARGUMENTS_JSON")

    def test_http_error_body_and_headers_are_not_saved(self):
        report = self.assert_failure("http_error", "HTTP_ERROR")
        self.assertEqual(report["error"]["http_status"], 401)
        self.assertFalse(report["error"]["retryable"])

    def test_redirect_never_forwards_credentials(self):
        target = loopback_server(SyntheticHandler)
        target.requests = []
        thread = threading.Thread(target=target.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        try:
            self.server.redirect_target = f"http://localhost:{target.server_port}/steal"
            self.assert_failure("redirect", "REDIRECT_REFUSED")
            self.assertEqual(target.requests, [])
        finally:
            target.shutdown(); target.server_close(); thread.join(timeout=2)

    def test_invalid_json_does_not_leak_response(self):
        self.assert_failure("malformed_json", "INVALID_RESPONSE_JSON")

    def test_oversized_response_is_bounded(self):
        self.assert_failure("oversized", "RESPONSE_TOO_LARGE")

    def test_model_metadata_must_match_requested_model(self):
        self.assert_failure("model_mismatch", "MODEL_METADATA_MISMATCH")

    def test_reflected_secret_in_usage_and_ids_is_removed(self):
        self.server.scenario = "reflected_metadata"
        code, _, report = self.run_probe()
        self.assertEqual(code, 0)
        self.assertIsNone(report["requests"][0]["request_id"])
        self.assertNotIn("debug", report["requests"][0]["usage"])

    def test_public_http_is_refused_even_in_test_mode(self):
        self.endpoint["base_url"] = "http://example.invalid/v1"
        self.write_config()
        code, _, report = self.run_probe()
        self.assertEqual(code, 2)
        self.assertEqual(report["error"]["code"], "UNSAFE_BASE_URL")
        self.assertEqual(self.server.requests, [])

    def test_http_requires_explicit_loopback_flag(self):
        code, _, report = self.run_probe(loopback=False)
        self.assertEqual(code, 2)
        self.assertEqual(report["error"]["code"], "UNSAFE_BASE_URL")

    def test_embedded_url_credentials_are_refused(self):
        self.endpoint["base_url"] = "https://person:credential@example.invalid/v1"
        self.write_config()
        code, _, report = self.run_probe()
        self.assertEqual(code, 2)
        self.assertEqual(report["error"]["code"], "UNSAFE_BASE_URL")

    def test_config_permissions_are_enforced(self):
        self.config.chmod(0o644)
        code, _, report = self.run_probe()
        self.assertEqual(code, 2)
        self.assertEqual(report["error"]["code"], "PRIVATE_FILE_PERMISSIONS")
        self.assertEqual(self.server.requests, [])

    def test_key_permissions_are_enforced(self):
        self.key.chmod(0o640)
        code, _, report = self.run_probe()
        self.assertEqual(code, 2)
        self.assertEqual(report["error"]["code"], "PRIVATE_FILE_PERMISSIONS")

    def test_credential_env_reference_supported(self):
        self.endpoint.pop("api_key_file")
        self.endpoint["api_key_env"] = "HARNIQO_TEST_MODEL_KEY"
        self.write_config()
        with patch.dict(os.environ, {"HARNIQO_TEST_MODEL_KEY": self.secret}):
            code, _, _ = self.run_probe()
        self.assertEqual(code, 0)

    def test_two_credential_sources_are_rejected(self):
        self.endpoint["api_key_env"] = "HARNIQO_TEST_MODEL_KEY"
        self.write_config()
        code, _, report = self.run_probe()
        self.assertEqual(code, 2)
        self.assertEqual(report["error"]["code"], "EXACTLY_ONE_CREDENTIAL_SOURCE_REQUIRED")

    def test_duplicate_config_json_member_is_rejected(self):
        self.write_config(raw='{"endpoints":[],"endpoints":[]}')
        code, _, report = self.run_probe()
        self.assertEqual(code, 2)
        self.assertEqual(report["error"]["code"], "INVALID_CONFIG_JSON")

    def test_existing_report_is_not_overwritten(self):
        self.report.parent.mkdir(mode=0o700)
        self.report.write_text("preserve-existing-report")
        # This path is intentionally non-JSON, so inspect main directly.
        capture = io.StringIO()
        with redirect_stdout(capture):
            code = probe.main(["--config", str(self.config), "--endpoint-id", "fixture", "--report", str(self.report)])
        self.assertEqual(code, 2)
        self.assertEqual(self.report.read_text(), "preserve-existing-report")
        self.assertEqual(json.loads(capture.getvalue())["error"]["code"], "REPORT_ALREADY_EXISTS")
        self.assertEqual(self.server.requests, [])

    def test_report_outside_private_is_refused(self):
        destination = self.root / "public" / "report.json"
        code, summary, _ = self.run_probe(report=destination)
        self.assertEqual(code, 2)
        self.assertEqual(summary["error"]["code"], "REPORT_NOT_PRIVATE")
        self.assertFalse(destination.exists())

    def test_dotdot_cannot_escape_private_report_directory(self):
        destination = self.private / ".." / "escaped" / "report.json"
        code, summary, _ = self.run_probe(report=destination)
        self.assertEqual(code, 2)
        self.assertEqual(summary["error"]["code"], "REPORT_NOT_PRIVATE")
        self.assertFalse((self.root / "escaped").exists())

    def test_key_symlink_is_refused(self):
        link = self.private / "linked-key"
        link.symlink_to(self.key)
        self.endpoint["api_key_file"] = "linked-key"
        self.write_config()
        code, _, report = self.run_probe()
        self.assertEqual(code, 2)
        self.assertEqual(report["error"]["code"], "UNSAFE_SYMLINK")

    def test_request_limits_are_bounded(self):
        code, summary, _ = self.run_probe(flags=["--max-tokens", "999999"])
        self.assertEqual(code, 2)
        self.assertEqual(summary["error"]["code"], "INVALID_REQUEST_LIMITS")
        self.assertEqual(self.server.requests, [])


if __name__ == "__main__":
    unittest.main()
