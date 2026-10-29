#!/usr/bin/env python3
"""Qualify one pinned Hermes native API run with two bounded builtin tools.

Only this trusted qualification profile is tested. This program never configures
the model, reads its key, starts a container, or grants tools/approvals. The outer
process enforces a hard deadline on its HTTP worker, not on remote inference.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import signal
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

COMMIT = "a382af23f4c067d23463b5781958684c2cda82bc"
TOOLS = ("qualification_write_file", "qualification_read_file")
IDENTIFIER = re.compile(r"[A-Za-z0-9_.:-]{1,160}\Z")
IMAGE = re.compile(r"sha256:[0-9a-f]{64}\Z")
LIMIT = 2 * 1024**2


class Rejected(Exception):
    def __init__(self, code, http_status=None):
        self.code, self.http_status = code, http_status
        super().__init__(code)


def require(condition, code):
    if not condition:
        raise Rejected(code)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def no_links(path):
    path = Path(os.path.abspath(path))
    for part in (path, *path.parents):
        require(not part.is_symlink(), "symlink_path_rejected")
    return path


def private_directory(path, create=False):
    path = no_links(path)
    require(".private" in path.parts, "evidence_requires_private_storage")
    boundary = path.parts.index(".private")
    current = Path(*path.parts[:boundary])
    for part in path.parts[boundary:]:
        current /= part
        if create:
            current.mkdir(mode=0o700, exist_ok=True)
        metadata = current.lstat()
        require(stat.S_ISDIR(metadata.st_mode) and metadata.st_uid == os.geteuid()
                and not metadata.st_mode & 0o077, "private_directory_permissions")
    return path


def read_file(path, maximum, private=True):
    path = no_links(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    with os.fdopen(fd, "rb") as stream:
        metadata = os.fstat(stream.fileno())
        require(stat.S_ISREG(metadata.st_mode) and metadata.st_size <= maximum,
                "input_not_bounded_regular_file")
        if private:
            require(metadata.st_uid == os.geteuid() and not metadata.st_mode & 0o077,
                    "private_file_permissions")
        data = stream.read(maximum + 1)
        require(len(data) <= maximum, "input_size_exceeded")
        return data


def json_value(data):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            require(key not in value, "duplicate_json_key")
            value[key] = item
        return value
    return json.loads(data, object_pairs_hook=unique,
                      parse_constant=lambda _: (_ for _ in ()).throw(Rejected("nonfinite_json")))


def write_report(path, value):
    descriptor, name = tempfile.mkstemp(prefix=".native-report-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        temporary.unlink(missing_ok=True)


class NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        fp.close()
        raise Rejected("http_redirect_rejected")


class API:
    def __init__(self, port, token):
        self.base = "http://127.0.0.1:" + str(port)
        self.token = token
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirects())

    def open(self, path, method="GET", body=None, auth="valid", headers=None):
        require(path.startswith("/") and not path.startswith("//") and "\r" not in path
                and "\n" not in path, "invalid_api_path")
        request_headers = {"Accept": "application/json", **(headers or {})}
        if auth != "missing":
            request_headers["Authorization"] = "Bearer " + (
                self.token if auth == "valid" else "harniqo-invalid-" + secrets.token_hex(24))
        encoded = None if body is None else json.dumps(body, allow_nan=False).encode()
        if encoded is not None:
            require(len(encoded) <= 16384, "request_size_exceeded")
            request_headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.base + path, data=encoded,
                                         headers=request_headers, method=method)
        try:
            return self.opener.open(request, timeout=15)
        except urllib.error.HTTPError as error:
            status = error.code
            error.close()  # Never read or emit untrusted HTTP error bodies.
            raise Rejected("api_http_error", status) from None

    def request(self, path, method="GET", body=None, headers=None, expected=200):
        with self.open(path, method, body, headers=headers) as response:
            require(response.status == expected, "unexpected_http_status")
            require(response.headers.get_content_type() == "application/json", "non_json_response")
            raw = response.read(LIMIT + 1)
            require(len(raw) <= LIMIT, "http_response_size_exceeded")
            return json_value(raw)

    def denied(self, auth):
        try:
            with self.open("/api/sessions?limit=1", auth=auth):
                raise Rejected("authentication_was_not_denied")
        except Rejected as error:
            require(error.code == "api_http_error" and error.http_status in {401, 403},
                    "authentication_was_not_denied")
            return error.http_status


def read_sse(response, run_id, previous=-1, first_only=False):
    """Parse bounded actual SSE frames. Comments are not events/cursors."""
    require(response.status == 200 and response.headers.get_content_type() == "text/event-stream",
            "invalid_sse_response")
    frames, total, frame_bytes, fields = [], 0, 0, []
    while True:
        line = response.readline(65537)
        total += len(line)
        frame_bytes += len(line)
        require(len(line) <= 65536 and total <= 8 * 1024**2 and frame_bytes <= 256 * 1024,
                "sse_size_exceeded")
        if not line:
            require(not fields, "incomplete_sse_frame")
            return frames
        line = line.decode("utf-8").rstrip("\r\n")
        if line:
            if not line.startswith(":"):
                fields.append(line)
            continue
        frame_bytes = 0
        if not fields:
            continue
        ids, data = [], []
        for field in fields:
            key, _, value = field.partition(":")
            value = value[1:] if value.startswith(" ") else value
            if key == "id":
                ids.append(value)
            elif key == "data":
                data.append(value)
            elif key not in {"event", "retry"}:
                raise Rejected("unknown_sse_field")
        fields = []
        require(len(ids) == 1 and re.fullmatch(r"[0-9]{1,10}", ids[0]) and data,
                "sse_missing_numeric_cursor")
        event = json_value("\n".join(data))
        sequence = int(ids[0])
        require(isinstance(event, dict) and type(event.get("seq")) is int
                and event["seq"] == sequence and sequence == previous + 1,
                "sse_gap_duplicate_or_cursor_mismatch")
        require(event.get("run_id") == run_id and isinstance(event.get("event"), str),
                "sse_wrong_run_or_event")
        previous = sequence
        frames.append(event)
        require(len(frames) <= 8192, "sse_event_count_exceeded")
        if first_only:
            return frames  # Caller closes the live connection before reconnecting.


def inspect_container(name, image_id, port):
    require(IDENTIFIER.fullmatch(name), "invalid_container_identifier")
    template = ('{"id":{{json .Id}},"image":{{json .Image}},'
                '"running":{{json .State.Running}},"ports":{{json .NetworkSettings.Ports}}}')
    result = subprocess.run(["docker", "inspect", "--format", template, name],
                            stdin=subprocess.DEVNULL, capture_output=True, timeout=15)
    require(result.returncode == 0 and len(result.stdout) <= 65536, "container_inspection_failed")
    value = json_value(result.stdout)
    require(value.get("image") == image_id and value.get("running") is True
            and re.fullmatch(r"[0-9a-f]{64}", value.get("id", "")), "runtime_image_identity_mismatch")
    require(value.get("ports") == {"8642/tcp": [{"HostIp": "127.0.0.1", "HostPort": str(port)}]},
            "runtime_port_binding_mismatch")
    return {"container_id": value["id"], "image_id": image_id, "running": True}


def decode_tool_json(value):
    return json_value(value) if isinstance(value, str) else value


def validate_tools(messages, artifact_raw, nonce, content):
    artifact = json_value(artifact_raw)
    require(isinstance(artifact, dict) and set(artifact) == {"nonce", "content", "receipt"}
            and artifact["nonce"] == nonce and artifact["content"] == content
            and isinstance(artifact.get("receipt"), str)
            and re.fullmatch(r"[0-9a-f]{48}", artifact["receipt"]), "native_artifact_mismatch")
    calls, results = {}, {}
    final = []
    for message in messages:
        require(isinstance(message, dict), "invalid_native_message")
        role = message.get("role")
        if role == "assistant":
            if message.get("content"):
                final.append(message["content"])
            for call in decode_tool_json(message.get("tool_calls") or []):
                require(isinstance(call, dict) and isinstance(call.get("function"), dict),
                        "invalid_native_tool_call")
                call_id, function = call.get("id"), call["function"]
                require(isinstance(call_id, str) and IDENTIFIER.fullmatch(call_id)
                        and call_id not in calls and function.get("name") in TOOLS,
                        "unexpected_native_tool_call")
                args = decode_tool_json(function.get("arguments"))
                expected = {"nonce": nonce, "content": content} if function["name"] == TOOLS[0] else {"nonce": nonce}
                require(args == expected, "native_tool_arguments_mismatch")
                calls[call_id] = function["name"]
        elif role == "tool":
            call_id = message.get("tool_call_id")
            require(isinstance(call_id, str) and call_id not in results, "duplicate_native_tool_result")
            results[call_id] = decode_tool_json(message.get("content"))
    require(Counter(calls.values()) == Counter(TOOLS) and set(results) == set(calls),
            "native_two_tool_loop_missing")
    artifact_hash = digest(artifact_raw)
    for call_id, name in calls.items():
        value = results[call_id]
        expected = {"ok": True, "nonce": nonce, "receipt": artifact["receipt"],
                    "relative_path": "qualification-result.json", "sha256": artifact_hash}
        if name == TOOLS[1]:
            expected["content"] = content
        require(value == expected, "native_tool_result_mismatch")
    require(final and isinstance(final[-1], str) and artifact["receipt"] in final[-1]
            and nonce in final[-1], "native_final_receipt_missing")
    return {"artifact_sha256": artifact_hash, "tool_calls": calls,
            "receipt_verified": True, "message_count": len(messages),
            "messages_sha256": digest(json.dumps(messages, sort_keys=True, ensure_ascii=False).encode())}


def run_case(args, record):
    report = args.report
    def stage(name, **values):
        record.update(values)
        record["stage"] = name
        record["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        write_report(report, record)

    token = read_file(args.token_file, 1024).decode().strip()
    require(re.fullmatch(r"[A-Za-z0-9_-]{32,256}", token), "invalid_api_token")
    provenance = json_value(read_file(args.provenance, 4 * 1024**2, private=False))
    image_id = provenance.get("image_id", "")
    require(IMAGE.fullmatch(image_id) and provenance.get("platform") == "linux/amd64"
            and provenance.get("inputs", {}).get("source_commit") == COMMIT,
            "unexpected_provenance_source")
    tool_hash = provenance.get("inputs", {}).get("recipe_sha256", {}).get("entrypoint.py")
    require(tool_hash == digest(read_file(args.root / "deploy/qualification/hermes/entrypoint.py", 65536,
                                         private=False)), "qualification_profile_identity_mismatch")
    before = inspect_container(args.container, image_id, args.port)
    workspace = private_directory(args.workspace)
    require(not (workspace / "qualification-result.json").exists()
            and not (workspace / "qualification-result.json").is_symlink(), "workspace_not_fresh")
    stage("runtime_identity", runtime=before, tool_profile_sha256=tool_hash,
          expected_model=args.expected_model)
    api = API(args.port, token)
    status_codes = {auth: api.denied(auth) for auth in ("missing", "invalid")}
    stage("authentication_denied", authentication=status_codes)
    session_id = "harniqo_qualification_" + secrets.token_hex(12)
    created = api.request("/api/sessions", "POST", {"id": session_id, "title": session_id}, expected=201)
    require(created.get("session", {}).get("id") == session_id, "native_session_identity_mismatch")
    stage("session_created", native_session_id=session_id)
    nonce = secrets.token_hex(16)
    content = "Harniqo native Hermes tool qualification " + nonce
    prompt = (
        "Perform this small qualification task using only the two supplied tools. "
        "First call qualification_write_file with exactly nonce=" + json.dumps(nonce) +
        " and content=" + json.dumps(content) + ". Wait for its result. "
        "Next call qualification_read_file with exactly that nonce. Do not call both in parallel. "
        "After reading, return a short final answer containing the nonce and the exact random receipt "
        "returned by the tools. Do not invent a receipt or use any other tool.")
    accepted = api.request("/v1/runs", "POST", {"session_id": session_id, "input": prompt},
                           headers={"Idempotency-Key": "harniqo-" + nonce}, expected=202)
    run_id = accepted.get("run_id", "")
    require(re.fullmatch(r"run_[0-9a-f]{32}", run_id), "invalid_native_run_id")
    stage("run_accepted", native_run_id=run_id, nonce=nonce)
    event_path = "/v1/runs/" + run_id + "/events"
    with api.open(event_path, headers={"Accept": "text/event-stream"}) as response:
        first = read_sse(response, run_id, first_only=True)
    require(first and first[0]["event"] not in {"run.completed", "run.failed", "run.cancelled"},
            "no_preterminal_event_for_disconnect")
    cursor = first[-1]["seq"]
    stage("sse_disconnected", disconnected_after_seq=cursor)
    with api.open(event_path, headers={"Accept": "text/event-stream", "Last-Event-ID": str(cursor)}) as response:
        resumed = read_sse(response, run_id, previous=cursor)
    events = first + resumed
    require(resumed and events[-1].get("event") == "run.completed", "native_run_not_completed_in_sse")
    observed = Counter((e.get("event"), e.get("tool")) for e in events)
    require(all(observed[(event, tool)] >= 1 for tool in TOOLS
                for event in ("tool.started", "tool.completed")), "native_tool_sse_missing")
    require(not any(e.get("error") for e in events if e.get("event") == "tool.completed"),
            "native_tool_sse_error")
    stage("sse_resumed", sse={"events": len(events), "first_seq": events[0]["seq"],
          "last_seq": events[-1]["seq"], "real_disconnect": True, "retained_backlog_resume": True,
          "event_types": dict(Counter(e["event"] for e in events))})
    result = api.request("/v1/runs/" + run_id)
    require(result.get("run_id") == run_id and result.get("status") == "completed"
            and result.get("session_id") == session_id and result.get("completed") is True
            and not result.get("partial") and not result.get("interrupted"), "native_run_status_mismatch")
    runtime = result.get("runtime")
    require(isinstance(runtime, dict) and runtime.get("model") == args.expected_model,
            "native_effective_model_mismatch")
    usage = result.get("usage")
    keys = ("input_tokens", "output_tokens", "total_tokens", "cache_read_tokens", "cache_write_tokens")
    require(isinstance(usage, dict), "native_usage_missing")
    safe_usage = {key: value for key, value in usage.items() if key in keys and type(value) is int and value >= 0}
    require(safe_usage.get("total_tokens", safe_usage.get("input_tokens", 0) + safe_usage.get("output_tokens", 0)) > 0,
            "native_token_usage_missing")
    stage("terminal_status", terminal_status="completed", effective_model=runtime["model"], usage=safe_usage,
          usage_source="native_harness_provider_reported", cost_cap_enforced=False)
    history = api.request("/api/sessions/" + session_id + "/messages?limit=100&order=oldest")
    require(history.get("session_id") == session_id and isinstance(history.get("data"), list)
            and len(history["data"]) < 100, "native_history_missing_or_truncated")
    tools = validate_tools(history["data"], read_file(workspace / "qualification-result.json", 8192), nonce, content)
    require(isinstance(result.get("output"), str) and artifact_receipt(workspace) in result["output"],
            "terminal_output_receipt_missing")
    after = inspect_container(args.container, image_id, args.port)
    require(after == before, "container_changed_during_case")
    stage("completed", status="passed", complete=True, native_tools=tools,
          native_api_case_passed=True, milestone_complete=False)


def artifact_receipt(workspace):
    return json_value(read_file(workspace / "qualification-result.json", 8192))["receipt"]


def cli():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--port", type=int, default=10010)
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--provenance", type=Path, required=True)
    parser.add_argument("--container", required=True)
    parser.add_argument("--expected-model", required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--deadline", type=int, default=150)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    require(10000 <= args.port <= 10100 and args.port != 10023, "port_outside_authorized_range")
    require(30 <= args.deadline <= 300, "deadline_out_of_range")
    require(re.fullmatch(r"[A-Za-z0-9_.:/-]{1,200}", args.expected_model), "invalid_expected_model")
    args.root = no_links(args.root)
    args.report = no_links(args.report)
    private_directory(args.report.parent, create=not args.worker)
    return args


def main():
    os.umask(0o077)
    args = cli()
    if args.worker:
        record = json_value(read_file(args.report, LIMIT))
        try:
            run_case(args, record)
            return 0
        except BaseException as error:
            record.update(status="failed", complete=True, native_api_case_passed=False,
                          failure={"code": error.code if isinstance(error, Rejected) else type(error).__name__,
                                   "http_status": getattr(error, "http_status", None)})
            write_report(args.report, record)
            return 1
    require(not args.report.exists(), "report_already_exists")
    record = {"schema_version": 1, "kind": "hermes_native_api_qualification", "status": "running",
              "complete": False, "stage": "started", "source_commit": COMMIT,
              "scope": "trusted_api_profile_two_tools_single_model",
              "native_api_case_passed": False, "milestone_complete": False,
              "saas_isolation_passed": False, "hard_worker_deadline_seconds": args.deadline,
              "unverified": ["multi_model_interchangeability", "native_continuation_after_restart", "restart_recovery",
                             "cancellation_execution_confirmation", "human_approval_denial",
                             "generic_file_terminal_tools", "production_vm_network_and_isolation"],
              "failure_does_not_confirm_remote_execution_stopped": True,
              "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    # Exclusive creation reserves the requested evidence name before atomic updates.
    with args.report.open("x") as stream:
        os.chmod(args.report, 0o600)
        json.dump(record, stream)
    started = time.monotonic()
    with subprocess.Popen([sys.executable, str(Path(__file__).resolve()), *sys.argv[1:], "--worker"],
                          stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                          start_new_session=True) as worker:
        try:
            code = worker.wait(timeout=args.deadline)
        except (subprocess.TimeoutExpired, KeyboardInterrupt) as error:
            code = 124 if isinstance(error, subprocess.TimeoutExpired) else 130
            # The worker can briefly own a docker-inspect subprocess. Kill only
            # this newly created local process group, never the remote gateway.
            try:
                os.killpg(worker.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass  # The worker may have exited at the deadline boundary.
            worker.wait(timeout=5)
            record = json_value(read_file(args.report, LIMIT))
            record.update(status="failed", complete=False, native_api_case_passed=False,
                          failure={"code": "worker_hard_deadline" if code == 124 else "operator_interrupt"},
                          worker_terminated=True)
            write_report(args.report, record)
    record = json_value(read_file(args.report, LIMIT))
    record["elapsed_seconds"] = round(time.monotonic() - started, 3)
    record["worker_exit_code"] = code
    record["ended_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    if code != 0 and record.get("status") == "running":
        record.update(status="failed", complete=False, failure={"code": "worker_exited_without_complete_report"})
    write_report(args.report, record)
    print(json.dumps({"status": record["status"], "native_api_case_passed": record["native_api_case_passed"],
                      "milestone_complete": False, "report": str(args.report)}))
    if code == 0 and record.get("native_api_case_passed"):
        return 0
    return code if code in {124, 130} else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BaseException as error:
        if isinstance(error, SystemExit):
            raise
        print(json.dumps({"status": "rejected", "code": error.code if isinstance(error, Rejected)
                          else type(error).__name__}), file=sys.stderr)
        raise SystemExit(2)
