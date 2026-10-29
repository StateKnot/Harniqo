#!/usr/bin/env python3
"""Bounded OpenAI-compatible tool-protocol probe; not a Harness qualification.

Config (mode 0600; URLs/keys are never copied to the report)::

    {"endpoints": [{"id": "primary", "base_url": "https://api.example.invalid/v1",
                    "model": "model-id", "api_key_file": "keys/primary"}]}

Use exactly one of api_key_file and api_key_env per endpoint. Relative key paths
are relative to the config directory. Reports must be beneath a .private/
directory; existing reports/artifacts are never overwritten. Example:

    python3 scripts/qualification/probe_model.py --config .private/models.json \
        --endpoint-id primary --report .private/probes/primary/report.json

The first call must request record_probe with the generated nonce. Only this
program writes a fixed artifact containing a fresh receipt. The second call
must use that receipt. Model-provided commands and paths are never executed.
The optional loopback HTTP mode exists only for transport tests. This probe
does not test SSE, native Harness sessions, cancellation, or VM isolation.
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import socket
import ssl
import stat
import sys
import tempfile
import time
from datetime import datetime, timezone
from typing import Any
import urllib.error
import urllib.parse
import urllib.request

MAX_RESPONSE_BYTES = 512 * 1024
MAX_CONFIG_BYTES = 64 * 1024
MAX_ARGUMENT_BYTES = 4096
ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")


class ProbeError(Exception):
    def __init__(self, code: str, *, status: int | None = None, retryable: bool = False):
        super().__init__(code)
        self.code, self.status, self.retryable = code, status, retryable

    def summary(self) -> dict[str, Any]:
        value: dict[str, Any] = {"code": self.code, "retryable": self.retryable}
        if self.status is not None:
            value["http_status"] = self.status
        return value


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def strict_json(text: str) -> Any:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON member")
            result[key] = value
        return result

    def invalid_constant(_):
        raise ValueError("non-JSON numeric constant")

    return json.loads(text, object_pairs_hook=unique, parse_constant=invalid_constant)


def no_symlinks(path: Path) -> None:
    for item in [path, *path.parents]:
        if item.is_symlink():
            raise ProbeError("UNSAFE_SYMLINK")


def secure_read(path: Path, *, limit: int) -> str:
    if os.name != "posix":
        raise ProbeError("UNSUPPORTED_FILE_SECURITY")
    path = path.absolute()
    no_symlinks(path)
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
                raise ProbeError("PRIVATE_FILE_PERMISSIONS")
            if info.st_uid not in {0, os.geteuid()}:
                raise ProbeError("PRIVATE_FILE_OWNER")
            data = stream.read(limit + 1)
        if len(data) > limit:
            raise ProbeError("PRIVATE_FILE_TOO_LARGE")
        return data.decode("utf-8")
    except ProbeError:
        raise
    except (OSError, UnicodeError):
        raise ProbeError("PRIVATE_FILE_UNREADABLE") from None


def prepare_report(path: Path) -> Path:
    if os.name != "posix":
        raise ProbeError("UNSUPPORTED_FILE_SECURITY")
    path = path.absolute()
    if ".private" not in path.parent.parts or ".." in path.parts or path.name in {"", ".", ".."}:
        raise ProbeError("REPORT_NOT_PRIVATE")
    no_symlinks(path)
    private_index = path.parts.index(".private")
    private_root = Path(*path.parts[: private_index + 1])
    try:
        # Create missing directories individually with private permissions.
        pending = []
        current = path.parent
        while not current.exists():
            pending.append(current)
            current = current.parent
        for directory in reversed(pending):
            directory.mkdir(mode=0o700)
        info = private_root.stat()
        if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.geteuid():
            raise ProbeError("REPORT_DIRECTORY_PERMISSIONS")
        current = path.parent
        while current != private_root:
            info = current.stat()
            if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077:
                raise ProbeError("REPORT_DIRECTORY_PERMISSIONS")
            if info.st_uid != os.geteuid():
                raise ProbeError("REPORT_DIRECTORY_OWNER")
            current = current.parent
        if path.exists():
            raise ProbeError("REPORT_ALREADY_EXISTS")
        return path
    except ProbeError:
        raise
    except OSError:
        raise ProbeError("REPORT_DIRECTORY_UNWRITABLE") from None


def write_json_exclusive(path: Path, payload: dict[str, Any]) -> bytes:
    data = (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
    temporary = None
    try:
        fd, temporary = tempfile.mkstemp(prefix=".probe-", dir=path.parent)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path, follow_symlinks=False)  # Atomic no-overwrite publish.
        return data
    except FileExistsError:
        raise ProbeError("OUTPUT_ALREADY_EXISTS") from None
    except OSError:
        raise ProbeError("OUTPUT_WRITE_FAILED") from None
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def load_endpoint(config: Path, endpoint_id: str, *, allow_http_loopback: bool):
    try:
        data = strict_json(secure_read(config, limit=MAX_CONFIG_BYTES))
    except ProbeError:
        raise
    except (ValueError, RecursionError):
        raise ProbeError("INVALID_CONFIG_JSON") from None
    endpoints = data.get("endpoints") if isinstance(data, dict) else data
    if not isinstance(endpoints, list) or not 1 <= len(endpoints) <= 64:
        raise ProbeError("INVALID_ENDPOINT_LIST")
    seen = set()
    selected = None
    for entry in endpoints:
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
            raise ProbeError("INVALID_ENDPOINT_ENTRY")
        identifier = entry["id"]
        if not ID_PATTERN.fullmatch(identifier) or identifier in seen:
            raise ProbeError("INVALID_OR_DUPLICATE_ENDPOINT_ID")
        seen.add(identifier)
        if identifier == endpoint_id:
            selected = entry
    if selected is None:
        raise ProbeError("ENDPOINT_NOT_FOUND")
    if set(selected) - {"id", "base_url", "model", "api_key_file", "api_key_env"}:
        raise ProbeError("UNKNOWN_ENDPOINT_FIELD")
    model, base_url = selected.get("model"), selected.get("base_url")
    if (not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/+-]{0,159}", model)
            or "://" in model):
        raise ProbeError("INVALID_MODEL_ID")
    if not isinstance(base_url, str) or len(base_url) > 2048 or any(ord(c) < 33 for c in base_url):
        raise ProbeError("INVALID_BASE_URL")
    try:
        parts = urllib.parse.urlsplit(base_url)
        _ = parts.port
        if not parts.hostname or parts.username is not None or parts.password is not None:
            raise ValueError()
        if parts.query or parts.fragment or parts.scheme not in {"https", "http"}:
            raise ValueError()
        loopback = False
        if parts.scheme == "http":
            loopback = ipaddress.ip_address(parts.hostname).is_loopback
            if not allow_http_loopback or not loopback:
                raise ValueError()
        endpoint_url = urllib.parse.urlunsplit((parts.scheme, parts.netloc,
                                              parts.path.rstrip("/") + "/chat/completions", "", ""))
    except (ValueError, UnicodeError):
        raise ProbeError("UNSAFE_BASE_URL") from None
    file_source, env_source = selected.get("api_key_file"), selected.get("api_key_env")
    if bool(file_source) == bool(env_source):
        raise ProbeError("EXACTLY_ONE_CREDENTIAL_SOURCE_REQUIRED")
    if file_source:
        if not isinstance(file_source, str) or len(file_source) > 2048:
            raise ProbeError("INVALID_CREDENTIAL_FILE")
        key_path = Path(file_source)
        if not key_path.is_absolute():
            key_path = config.absolute().parent / key_path
        key = secure_read(key_path, limit=8192).strip()
    else:
        if not isinstance(env_source, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", env_source):
            raise ProbeError("INVALID_CREDENTIAL_ENV")
        key = os.environ.get(env_source, "").strip()
    if not key or len(key) > 4096 or any(ord(c) < 33 or ord(c) == 127 for c in key):
        raise ProbeError("INVALID_OR_MISSING_CREDENTIAL")
    if key in model:
        raise ProbeError("CONFIG_SECRET_OVERLAP")
    return {"id": endpoint_id, "model": model, "url": endpoint_url, "key": key,
            "transport": "loopback_transport_test" if loopback else "https"}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Refuse all redirects, including same-host redirects.


def safe_identifier(value: Any, key: str) -> str | None:
    if isinstance(value, str) and key not in value and re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", value):
        return value
    return None


def usage_summary(value: Any) -> dict[str, Any]:
    result: dict[str, Any] = {"observability": "unknown"}
    if isinstance(value, dict):
        for field in ("prompt_tokens", "completion_tokens", "total_tokens", "input_tokens", "output_tokens"):
            number = value.get(field)
            if type(number) is int and 0 <= number <= 10**12:
                result[field] = number
        if len(result) > 1:
            result["observability"] = "provider_reported"
    return result


def post_completion(endpoint: dict, payload: dict, record: dict, timeout: float) -> dict:
    handlers = [NoRedirect(), urllib.request.HTTPSHandler(context=ssl.create_default_context())]
    if endpoint["transport"] == "loopback_transport_test":
        handlers.append(urllib.request.ProxyHandler({}))
    opener = urllib.request.build_opener(*handlers)
    request = urllib.request.Request(endpoint["url"], data=json.dumps(payload).encode(), method="POST",
                                     headers={"Authorization": "Bearer " + endpoint["key"],
                                              "Content-Type": "application/json", "Accept": "application/json"})
    started = time.monotonic()
    try:
        with opener.open(request, timeout=timeout) as response:
            record["http_status"] = response.status
            record["request_id"] = safe_identifier(response.headers.get("x-request-id"), endpoint["key"])
            raw = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ProbeError("RESPONSE_TOO_LARGE")
        try:
            data = strict_json(raw.decode("utf-8"))
        except (ValueError, UnicodeError, RecursionError):
            raise ProbeError("INVALID_RESPONSE_JSON") from None
        if not isinstance(data, dict):
            raise ProbeError("INVALID_RESPONSE_SHAPE")
        if record["request_id"] is None:
            record["request_id"] = safe_identifier(data.get("id"), endpoint["key"])
        record["usage"] = usage_summary(data.get("usage"))
        record["reported_model"] = safe_identifier(data.get("model"), endpoint["key"])
        if data.get("model") != endpoint["model"]:
            raise ProbeError("MODEL_METADATA_MISMATCH")
        choices = data.get("choices")
        if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
            raise ProbeError("INVALID_CHOICES")
        message = choices[0].get("message")
        if not isinstance(message, dict) or message.get("role") != "assistant":
            raise ProbeError("INVALID_ASSISTANT_MESSAGE")
        record["finish_reason"] = safe_identifier(choices[0].get("finish_reason"), endpoint["key"])
        record["status"] = "received"
        return message
    except urllib.error.HTTPError as error:
        status = error.code
        error.close()  # Never read/log upstream error bodies or headers.
        record["http_status"] = status
        if 300 <= status < 400:
            raise ProbeError("REDIRECT_REFUSED", status=status) from None
        raise ProbeError("HTTP_ERROR", status=status, retryable=status == 429 or status >= 500) from None
    except (TimeoutError, socket.timeout):
        raise ProbeError("NETWORK_TIMEOUT", retryable=True) from None
    except urllib.error.URLError as error:
        if isinstance(error.reason, ssl.SSLError):
            raise ProbeError("TLS_VERIFICATION_FAILED") from None
        raise ProbeError("NETWORK_ERROR", retryable=True) from None
    except ssl.SSLError:
        raise ProbeError("TLS_VERIFICATION_FAILED") from None
    except (OSError, http.client.HTTPException):
        raise ProbeError("NETWORK_RESPONSE_ERROR", retryable=True) from None
    finally:
        record["elapsed_ms"] = round((time.monotonic() - started) * 1000, 2)


def perform_probe(endpoint: dict, report_path: Path, report: dict, *, timeout: float, max_tokens: int):
    nonce = secrets.token_hex(16)
    definition = {"type": "function", "function": {
        "name": "record_probe", "description": "Record this qualification probe's nonce.",
        "parameters": {"type": "object", "additionalProperties": False,
                       "properties": {"nonce": {"type": "string", "const": nonce},
                                      "note": {"type": "string", "minLength": 1, "maxLength": 160}},
                       "required": ["nonce", "note"]}}}
    messages = [{"role": "system", "content": "Follow the controlled tool protocol. Never invent tool results."},
                {"role": "user", "content": f"Call record_probe exactly once with nonce {nonce} and a short note. "
                 "After the tool result, return only a JSON object with exactly receipt and nonce from that result."}]
    common = {"model": endpoint["model"], "max_tokens": max_tokens, "stream": False}
    report["stage"] = "first_request"
    first = {"round": 1, "status": "started"}; report["requests"].append(first)
    message = post_completion(endpoint, {**common, "messages": messages, "tools": [definition],
                              "tool_choice": {"type": "function", "function": {"name": "record_probe"}},
                              "parallel_tool_calls": False}, first, timeout)
    if message.get("content") is not None and not isinstance(message.get("content"), str):
        raise ProbeError("INVALID_ASSISTANT_CONTENT")
    calls = message.get("tool_calls")
    if not isinstance(calls, list) or len(calls) != 1 or not isinstance(calls[0], dict):
        raise ProbeError("EXPECTED_SINGLE_TOOL_CALL")
    call = calls[0]
    function = call.get("function")
    call_id = safe_identifier(call.get("id"), endpoint["key"])
    if call.get("type") != "function" or not call_id or not isinstance(function, dict):
        raise ProbeError("INVALID_TOOL_CALL")
    if function.get("name") != "record_probe":
        raise ProbeError("UNEXPECTED_TOOL_NAME")
    arguments = function.get("arguments")
    if not isinstance(arguments, str) or len(arguments.encode()) > MAX_ARGUMENT_BYTES:
        raise ProbeError("INVALID_TOOL_ARGUMENTS")
    try:
        parsed = strict_json(arguments)
    except (ValueError, RecursionError):
        raise ProbeError("INVALID_TOOL_ARGUMENTS_JSON") from None
    if not isinstance(parsed, dict) or set(parsed) != {"nonce", "note"}:
        raise ProbeError("TOOL_ARGUMENT_FIELDS")
    if parsed["nonce"] != nonce:
        raise ProbeError("TOOL_NONCE_MISMATCH")
    note = parsed["note"]
    if not isinstance(note, str) or not 1 <= len(note) <= 160 or any(ord(c) < 32 for c in note):
        raise ProbeError("INVALID_TOOL_NOTE")
    report["stage"] = "controlled_tool"
    receipt = secrets.token_hex(24)  # Created only after the first response was validated.
    artifact_dir = report_path.parent / (report_path.stem + ".artifacts")
    try:
        artifact_dir.mkdir(mode=0o700)
    except FileExistsError:
        raise ProbeError("ARTIFACT_DIRECTORY_EXISTS") from None
    except OSError:
        raise ProbeError("ARTIFACT_DIRECTORY_UNWRITABLE") from None
    result = {"tool": "record_probe", "nonce": nonce, "receipt": receipt,
              "note_sha256": hashlib.sha256(note.encode()).hexdigest()}
    raw_artifact = write_json_exclusive(artifact_dir / "probe-result.json", result)
    report["controlled_tool"] = {"name": "record_probe", "arguments_validated": True,
                                "artifact": artifact_dir.name + "/probe-result.json",
                                "artifact_sha256": hashlib.sha256(raw_artifact).hexdigest()}
    assistant = {"role": "assistant", "content": message.get("content"), "tool_calls": calls}
    if "reasoning_content" in message:
        if not isinstance(message["reasoning_content"], str):
            raise ProbeError("INVALID_REASONING_CONTENT")
        assistant["reasoning_content"] = message["reasoning_content"]
    messages.extend([assistant, {"role": "tool", "tool_call_id": call_id,
                                "content": json.dumps(result, separators=(",", ":"))}])
    report["stage"] = "second_request"
    second = {"round": 2, "status": "started"}; report["requests"].append(second)
    final = post_completion(endpoint, {**common, "messages": messages, "tools": [definition],
                            "tool_choice": "none", "response_format": {"type": "json_object"}}, second, timeout)
    if final.get("tool_calls"):
        raise ProbeError("UNEXPECTED_SECOND_TOOL_CALL")
    try:
        answer = strict_json(final.get("content"))
    except (ValueError, TypeError, RecursionError):
        raise ProbeError("INVALID_FINAL_JSON") from None
    if not isinstance(answer, dict) or set(answer) != {"nonce", "receipt"}:
        raise ProbeError("INVALID_FINAL_FIELDS")
    if answer["nonce"] != nonce or answer["receipt"] != receipt:
        raise ProbeError("TOOL_RECEIPT_NOT_USED")
    report["controlled_tool"]["receipt_verified_in_final_response"] = True
    report["stage"] = "complete"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--endpoint-id", required=True)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("--max-tokens", "--max_tokens", type=int, default=192)
    parser.add_argument("--allow-http-loopback", action="store_true", help="Transport tests only; literal loopback IP required")
    args = parser.parse_args(argv)
    report = {"schema_version": 1, "scope": "model_tool_protocol_only_not_harness_qualification",
              "started_at": utc_now(), "verdict": "failed", "stage": "configuration", "requests": [],
              "streaming_tested": False, "redirect_policy": "deny_all", "raw_messages_recorded": False,
              "cost_cap_enforced": False, "model_identity_evidence": "response_metadata_only"}
    report_path = None
    exit_code = 1
    try:
        if not ID_PATTERN.fullmatch(args.endpoint_id):
            raise ProbeError("INVALID_ENDPOINT_ID")
        if not 1 <= args.timeout <= 60 or not 32 <= args.max_tokens <= 512:
            raise ProbeError("INVALID_REQUEST_LIMITS")
        destination = args.report or Path(".private/model-probes") / (
            datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(4)) / "report.json"
        report_path = prepare_report(destination)
        endpoint = load_endpoint(args.config, args.endpoint_id, allow_http_loopback=args.allow_http_loopback)
        # Only approved identifiers, never URL, credential source/path, key, headers or messages.
        report.update({"endpoint_id": endpoint["id"], "requested_model": endpoint["model"],
                       "transport": endpoint["transport"], "socket_timeout_seconds": args.timeout,
                       "evidence_kind": "transport_fixture_only" if endpoint["transport"] == "loopback_transport_test"
                       else "provider_protocol_observation",
                       "max_completion_tokens_per_request": args.max_tokens, "max_requests": 2})
        perform_probe(endpoint, report_path, report, timeout=args.timeout, max_tokens=args.max_tokens)
        report["verdict"] = "passed"
        exit_code = 0
    except ProbeError as error:
        report["error"] = error.summary()
        if report["stage"] == "configuration":
            exit_code = 2
    except KeyboardInterrupt:
        report["error"] = {"code": "CANCELLED_BY_OPERATOR", "retryable": False}
        exit_code = 130
    except Exception:
        # Never expose exception text: upstream libraries may include URLs or keys.
        report["error"] = {"code": "INTERNAL_ERROR", "retryable": False}
    report["finished_at"] = utc_now()
    if report_path is not None:
        try:
            write_json_exclusive(report_path, report)
        except ProbeError as error:
            report["verdict"] = "failed"
            report["error"] = error.summary()
            exit_code = 2
    summary = {"verdict": report["verdict"], "scope": report["scope"],
               "report_saved": report_path is not None and report_path.is_file()}
    if "error" in report:
        summary["error"] = report["error"]
    print(json.dumps(summary, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
