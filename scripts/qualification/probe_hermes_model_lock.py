#!/usr/bin/env python3
"""Verify native Hermes model locks without changing its agent loop.

This is the supported session-chat stream, which interrupts on disconnect. It
does not claim /v1/runs reconnect semantics. Only a separate transport observer
can substantiate the actual endpoint; configuration alone never fills that gap.
"""
from __future__ import annotations

import argparse
from collections import Counter
import json
import os
from pathlib import Path
import re
import secrets
import signal
import subprocess
import sys
import time
import urllib.parse

import probe_hermes as native


def read_session_sse(response, session_id, observe=None):
    """Read one uninterrupted native session stream (wire event + JSON seq)."""
    native.require(response.status == 200 and response.headers.get_content_type() == "text/event-stream",
                   "invalid_sse_response")
    frames, fields, total, frame_size, run_id = [], [], 0, 0, None
    while True:
        line = response.readline(65537)
        total += len(line)
        frame_size += len(line)
        native.require(len(line) <= 65536 and total <= 8 * 1024**2 and frame_size <= 256 * 1024,
                       "sse_size_exceeded")
        if not line:
            native.require(not fields, "incomplete_sse_frame")
            native.require(frames and frames[-1]["event"] == "done", "native_stream_missing_done")
            return frames
        line = line.decode("utf-8").rstrip("\r\n")
        if line:
            if not line.startswith(":"):
                fields.append(line)
            continue
        frame_size = 0
        if not fields:
            continue
        names, data = [], []
        for field in fields:
            key, _, value = field.partition(":")
            value = value[1:] if value.startswith(" ") else value
            if key == "event":
                names.append(value)
            elif key == "data":
                data.append(value)
            else:
                raise native.Rejected("unexpected_session_sse_field")
        fields = []
        native.require(len(names) == 1 and data, "session_sse_event_missing")
        payload = native.json_value("\n".join(data))
        native.require(isinstance(payload, dict) and "event" not in payload,
                       "session_sse_invalid_payload")
        native.require(type(payload.get("seq")) is int and payload["seq"] == len(frames) + 1,
                       "sse_gap_duplicate_or_cursor_mismatch")
        candidate = payload.get("run_id")
        native.require(isinstance(candidate, str) and re.fullmatch(r"run_[0-9a-f]{32}", candidate)
                       and payload.get("session_id") == session_id, "session_sse_wrong_identity")
        if run_id is None:
            run_id = candidate
        native.require(run_id == candidate and (not frames or frames[-1]["event"] != "done"),
                       "session_sse_wrong_run_or_after_done")
        frame = {"event": names[0], **payload}
        frames.append(frame)
        native.require(len(frames) <= 8192, "sse_event_count_exceeded")
        if observe is not None:
            observe(frame)


def validate_locked_metadata(event, expected_model):
    evidence = native.metadata_evidence(event, expected_model)
    evidence["model_evidence"]["source"] = "native_locked_session_terminal_actual_agent_runtime"
    native.validate_metadata(evidence)
    runtime = event.get("runtime")
    native.require(runtime.get("provider") == "custom", "native_effective_provider_mismatch")
    native.require(runtime.get("model_lock") == "confirmed", "native_model_lock_not_confirmed")
    requested = runtime.get("requested")
    native.require(isinstance(requested, dict) and requested.get("model") == expected_model
                   and requested.get("provider") == "custom", "native_requested_runtime_mismatch")
    return {**evidence, "effective_provider": "custom", "native_model_lock_confirmed": True}


def configured_endpoint(path, expected_model):
    config = native.json_value(native.read_file(path, 8192))
    native.require(isinstance(config, dict) and set(config) == {"model", "base_url"}
                   and config["model"] == expected_model, "model_config_identity_mismatch")
    base = config["base_url"]
    native.require(isinstance(base, str) and len(base) <= 2048, "invalid_model_endpoint")
    parsed = urllib.parse.urlsplit(base)
    native.require(parsed.scheme == "https" and parsed.hostname and not parsed.username
                   and not parsed.password and not parsed.query and not parsed.fragment,
                   "invalid_model_endpoint")
    return native.digest((base.rstrip("/") + "/chat/completions").encode())


def validate_transport(raw, target_sha256, expected_model):
    """Compare actual HTTP observations; requested/configured values are expectations."""
    native.require(0 < len(raw) <= 256 * 1024 and raw.endswith(b"\n"), "transport_evidence_incomplete")
    lines = raw.splitlines()
    native.require(0 < len(lines) <= 128, "transport_evidence_count_exceeded")
    statuses = Counter()
    expected_hash = native.digest(expected_model.encode())
    keys = {"schema_version", "kind", "method", "target_sha256", "body_model_sha256", "status_code",
            "redirect_count", "network_error"}
    for line in lines:
        native.require(len(line) <= 4096, "transport_record_size_exceeded")
        value = native.json_value(line)
        native.require(isinstance(value, dict) and set(value) == keys
                       and type(value["schema_version"]) is int and value["schema_version"] == 1
                       and value["kind"] == "chat_completions_http_response" and value["method"] == "POST",
                       "transport_record_invalid")
        native.require(value["target_sha256"] == target_sha256
                       and value["body_model_sha256"] == expected_hash, "actual_transport_target_or_model_mismatch")
        native.require(value["redirect_count"] == 0 and type(value["redirect_count"]) is int
                       and value["network_error"] is False, "transport_redirect_or_network_error")
        code = value["status_code"]
        native.require(type(code) is int and 100 <= code <= 599, "transport_status_invalid")
        statuses[code] += 1
    native.require(set(statuses) == {200}, "transport_non_success_response")
    native.require(statuses[200] >= 3, "transport_two_tool_loop_requests_missing")
    return {"status": "observed_matched", "source": "profile_actual_http_send_observer",
            "expected_target_sha256": target_sha256, "requests": len(lines),
            "response_status_counts": dict(statuses), "evidence_sha256": native.digest(raw),
            "headers_or_api_key_recorded": False}


def run_case(args, record):
    def stage(name, **values):
        record.update(values, stage=name, updated_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        native.write_report(args.report, record)

    token = native.read_file(args.token_file, 1024).decode().strip()
    native.require(re.fullmatch(r"[A-Za-z0-9_-]{32,256}", token), "invalid_api_token")
    provenance = native.json_value(native.read_file(args.provenance, 4 * 1024**2, private=False))
    image_id = provenance.get("image_id", "")
    native.require(native.IMAGE.fullmatch(image_id) and provenance.get("platform") == "linux/amd64"
                   and provenance.get("inputs", {}).get("source_commit") == native.COMMIT,
                   "unexpected_provenance_source")
    profile_hash = provenance.get("inputs", {}).get("recipe_sha256", {}).get("entrypoint.py")
    native.require(profile_hash == native.digest(native.read_file(
        args.root / "deploy/qualification/hermes/entrypoint.py", 65536, private=False)),
        "qualification_profile_identity_mismatch")
    audit_hash = provenance.get("inputs", {}).get("recipe_sha256", {}).get("transport_audit.py")
    native.require(audit_hash == native.digest(native.read_file(
        args.root / "deploy/qualification/hermes/transport_audit.py", 65536, private=False)),
        "qualification_transport_profile_identity_mismatch")
    before = native.inspect_container(args.container, image_id, args.port)
    workspace = native.private_directory(args.workspace)
    for path in (workspace / "qualification-result.json", args.transport_evidence_file):
        native.require(not path.exists() and not path.is_symlink(), "workspace_not_fresh")
    expected_target = configured_endpoint(args.model_config_file, args.expected_model)
    stage("runtime_identity", runtime=before, tool_profile_sha256=profile_hash, transport_profile_sha256=audit_hash,
          endpoint_evidence={"status": "configured_only", "expected_target_sha256": expected_target})
    api = native.API(args.port, token)
    stage("authentication_denied", authentication={auth: api.denied(auth) for auth in ("missing", "invalid")})
    session_id = "harniqo_locked_" + secrets.token_hex(12)
    created = api.request("/api/sessions", "POST", {"id": session_id, "title": session_id}, expected=201)
    native.require(created.get("session", {}).get("id") == session_id, "native_session_identity_mismatch")
    nonce = secrets.token_hex(16)
    content = "Harniqo native locked Hermes tool qualification " + nonce
    stage("session_created", native_session_id=session_id, nonce=nonce)
    prompt = ("Perform this small qualification task using only the two supplied file tools. "
              "First call qualification_write_file with exactly nonce=" + json.dumps(nonce) +
              " and content=" + json.dumps(content) + ". Wait for its result. "
              "Next call qualification_read_file with exactly that nonce. Do not call both in parallel. "
              "Return a short final answer containing the nonce and the exact random receipt returned by the tools. "
              "Do not invent a receipt or call any other tool.")

    def observed(frame):
        if "native_run_id" not in record:
            stage("native_run_started", native_run_id=frame["run_id"])

    # No disconnect/resubmission: this native route deliberately interrupts on disconnect.
    with api.open("/api/sessions/" + session_id + "/chat/stream", "POST",
                  {"message": prompt, "model": args.expected_model, "provider": "custom",
                   "require_model_lock": True}, headers={"Accept": "text/event-stream"}) as response:
        events = read_session_sse(response, session_id, observed)
    terminal = [event for event in events if event["event"].startswith("run.") and event["event"] != "run.started"]
    native.require(len(terminal) == 1 and terminal[0]["event"] == "run.completed", "native_run_not_completed_in_sse")
    terminal = terminal[0]
    run_id = terminal["run_id"]
    result = api.request("/v1/runs/" + run_id)
    native.validate_terminal(result, terminal, run_id, session_id)
    observed_tools = Counter((e["event"], e.get("tool_name")) for e in events)
    native.require(all(observed_tools[(event, tool)] >= 1 for tool in native.TOOLS
                       for event in ("tool.started", "tool.completed"))
                   and not any(e["event"] == "tool.failed" for e in events), "native_tool_sse_missing_or_failed")
    metadata = native.metadata_evidence(terminal, args.expected_model)
    metadata["model_evidence"]["source"] = "native_locked_session_terminal_actual_agent_runtime"
    stage("terminal_observed", terminal_status="completed", **metadata,
          sse={"events": len(events), "first_seq": events[0]["seq"], "last_seq": events[-1]["seq"],
               "event_types": dict(Counter(e["event"] for e in events)), "uninterrupted": True,
               "reconnect_or_replay_claimed": False})
    history = api.request("/api/sessions/" + session_id + "/messages?limit=100&order=oldest")
    native.require(history.get("session_id") == session_id and isinstance(history.get("data"), list)
                   and len(history["data"]) < 100, "native_history_missing_or_truncated")
    tools = native.validate_tools(history["data"], native.read_file(workspace / "qualification-result.json", 8192),
                                  nonce, content)
    native.require(isinstance(result.get("output"), str)
                   and native.artifact_receipt(workspace) in result["output"], "terminal_output_receipt_missing")
    native.require(native.inspect_container(args.container, image_id, args.port) == before,
                   "container_changed_during_case")
    stage("native_tools_verified", native_tools=tools, terminal_output_receipt_verified=True,
          container_identity_unchanged=True)
    locked = validate_locked_metadata(terminal, args.expected_model)
    stage("native_model_lock_verified", **locked)
    transport = validate_transport(native.read_file(args.transport_evidence_file, 256 * 1024),
                                   expected_target, args.expected_model)
    stage("completed", status="passed", complete=True, endpoint_evidence=transport,
          effective_model=args.expected_model, native_api_case_passed=True)


def cli():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--port", type=int, default=10010)
    for name in ("token-file", "workspace", "provenance", "report", "model-config-file"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--transport-evidence-file", type=Path)
    parser.add_argument("--container", required=True)
    parser.add_argument("--expected-model", required=True)
    parser.add_argument("--deadline", type=int, default=150)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    native.require(10000 <= args.port <= 10100 and args.port != 10023, "port_outside_authorized_range")
    native.require(30 <= args.deadline <= 300, "deadline_out_of_range")
    native.require(re.fullmatch(r"[A-Za-z0-9_.:/-]{1,200}", args.expected_model), "invalid_expected_model")
    args.root, args.report = native.no_links(args.root), native.no_links(args.report)
    native.private_directory(args.report.parent, create=not args.worker)
    args.transport_evidence_file = native.no_links(args.transport_evidence_file or
                                                  args.workspace / "qualification-transport.jsonl")
    native.require(args.transport_evidence_file.parent == native.no_links(args.workspace),
                   "transport_evidence_requires_case_workspace")
    return args


def main():
    os.umask(0o077)
    args = cli()
    if args.worker:
        record = native.json_value(native.read_file(args.report, native.LIMIT))
        try:
            run_case(args, record)
            return 0
        except BaseException as error:
            record.update(status="failed", complete=True, native_api_case_passed=False,
                          failure={"code": error.code if isinstance(error, native.Rejected) else type(error).__name__,
                                   "http_status": getattr(error, "http_status", None)})
            native.write_report(args.report, record)
            return 1
    native.require(not args.report.exists(), "report_already_exists")
    record = {"schema_version": 1, "kind": "hermes_native_locked_session_qualification", "status": "running",
              "complete": False, "stage": "started", "source_commit": native.COMMIT,
              "scope": "trusted_profile_native_locked_session_two_tools_actual_http_target",
              "expected_model": args.expected_model, "native_api_case_passed": False,
              "milestone_complete": False, "saas_isolation_passed": False,
              "hard_worker_deadline_seconds": args.deadline,
              "failure_does_not_confirm_remote_execution_stopped": True,
              "unverified": ["canonical_runs_effective_model", "stream_disconnect_replay", "multi_model_interchangeability",
                             "native_continuation_after_restart", "cancellation_execution_confirmation",
                             "human_approval_denial", "generic_file_terminal_tools", "production_vm_network_and_isolation"],
              "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    with args.report.open("x") as stream:
        os.chmod(args.report, 0o600)
        json.dump(record, stream)
    started = time.monotonic()
    with subprocess.Popen([sys.executable, str(Path(__file__).resolve()), *sys.argv[1:], "--worker"],
                          stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                          **native.worker_process_options()) as worker:
        try:
            code = worker.wait(timeout=args.deadline)
        except (subprocess.TimeoutExpired, KeyboardInterrupt) as error:
            code = 124 if isinstance(error, subprocess.TimeoutExpired) else 130
            try:
                os.killpg(worker.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            worker.wait(timeout=5)
            record = native.json_value(native.read_file(args.report, native.LIMIT))
            record.update(status="failed", complete=False, native_api_case_passed=False, worker_terminated=True,
                          failure={"code": "worker_hard_deadline" if code == 124 else "operator_interrupt"})
            native.write_report(args.report, record)
    record = native.json_value(native.read_file(args.report, native.LIMIT))
    record.update(elapsed_seconds=round(time.monotonic() - started, 3), worker_exit_code=code,
                  ended_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    if record.get("status") == "running":
        record.update(status="failed", complete=False, native_api_case_passed=False,
                      failure={"code": "worker_exited_without_complete_report"})
    native.write_report(args.report, record)
    print(json.dumps({"status": record["status"], "native_api_case_passed": record["native_api_case_passed"],
                      "milestone_complete": False, "report": str(args.report)}))
    return 0 if code == 0 and record.get("native_api_case_passed") else code if code in {124, 130} else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BaseException as error:
        if isinstance(error, SystemExit):
            raise
        print(json.dumps({"status": "rejected", "code": error.code if isinstance(error, native.Rejected)
                          else type(error).__name__}), file=sys.stderr)
        raise SystemExit(2)
