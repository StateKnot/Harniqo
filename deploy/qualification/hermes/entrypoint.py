#!/usr/bin/env python3
"""Trusted Hermes API qualification launcher and two bounded builtin tools.

The same reviewed file is installed under tools/ so native managed workers
discover its literal registry registrations after exec. Importing it never
reads credentials or starts the gateway. This is not a plugin loader.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import sys
import urllib.parse

WORKSPACE = Path("/data/workspace")
HOME = Path("/data/hermes")
RESULT = "qualification-result.json"
TOOLSET = "harniqo_qualification"
NONCE = re.compile(r"[A-Za-z0-9_-]{8,128}\Z")


def _read_private_file(path, maximum=16384, require_read_only=False):
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open(path, flags)
    with os.fdopen(fd, "rb") as incoming:
        metadata = os.fstat(incoming.fileno())
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077
                or metadata.st_uid not in {0, os.geteuid()} or metadata.st_size > maximum):
            raise ValueError("Private input must be an owner-readable private regular file")
        if require_read_only and not os.fstatvfs(incoming.fileno()).f_flag & os.ST_RDONLY:
            raise ValueError("Private configuration and credentials require read-only mounts")
        data = incoming.read(maximum + 1)
        if len(data) > maximum:
            raise ValueError("Private input too large")
        return data


def _tool_error():
    return json.dumps({"error": "qualification_tool_input_or_artifact_rejected"})


def _valid_nonce(args, allowed):
    return (isinstance(args, dict) and set(args) == allowed
            and isinstance(args.get("nonce"), str) and NONCE.fullmatch(args["nonce"]))


def qualification_write_file(args, **_kwargs):
    """Write only the fixed synthetic artifact; model input cannot choose a path."""
    temporary = None
    try:
        if (not _valid_nonce(args, {"nonce", "content"}) or not isinstance(args.get("content"), str)
                or len(args["content"].encode("utf-8")) > 2048):
            return _tool_error()
        # The directory is code-defined, private, and mounted only for this run.
        if WORKSPACE.is_symlink() or not WORKSPACE.is_dir():
            return _tool_error()
        receipt = secrets.token_hex(24)
        body = json.dumps({"nonce": args["nonce"], "content": args["content"], "receipt": receipt},
                          ensure_ascii=False, sort_keys=True).encode() + b"\n"
        temporary = WORKSPACE / (".qualification-" + secrets.token_hex(12))
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        with os.fdopen(fd, "wb") as outgoing:
            outgoing.write(body)
            outgoing.flush()
            os.fsync(outgoing.fileno())
        os.replace(temporary, WORKSPACE / RESULT)
        directory = os.open(WORKSPACE, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return json.dumps({"ok": True, "nonce": args["nonce"], "receipt": receipt,
                           "relative_path": RESULT, "sha256": hashlib.sha256(body).hexdigest()})
    except (OSError, ValueError, UnicodeError):
        return _tool_error()
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def qualification_read_file(args, **_kwargs):
    if not _valid_nonce(args, {"nonce"}):
        return _tool_error()
    try:
        raw = _read_private_file(WORKSPACE / RESULT, maximum=8192)
        artifact = json.loads(raw)
        if (not isinstance(artifact, dict) or set(artifact) != {"nonce", "content", "receipt"}
                or artifact["nonce"] != args["nonce"] or not isinstance(artifact["content"], str)
                or len(artifact["content"].encode()) > 2048 or not isinstance(artifact["receipt"], str)
                or not re.fullmatch(r"[0-9a-f]{48}", artifact["receipt"])):
            return _tool_error()
        return json.dumps({"ok": True, **artifact, "relative_path": RESULT,
                           "sha256": hashlib.sha256(raw).hexdigest()})
    except (OSError, ValueError, UnicodeError):
        return _tool_error()


# Literal module-scope registrations are intentionally visible to the pinned
# upstream tools.registry AST scanner, including in agent.managed_worker.
from tools.registry import registry

registry.register(
    name="qualification_write_file", toolset=TOOLSET, handler=qualification_write_file,
    schema={"name": "qualification_write_file", "description": "Write a bounded synthetic qualification file and obtain its unpredictable receipt. The path is fixed.",
            "parameters": {"type": "object", "additionalProperties": False,
                           "properties": {"nonce": {"type": "string", "pattern": "^[A-Za-z0-9_-]{8,128}$"},
                                          "content": {"type": "string", "maxLength": 2048}},
                           "required": ["nonce", "content"]}},
    check_fn=lambda: True, max_result_size_chars=4096, execution_timeout=5)
registry.register(
    name="qualification_read_file", toolset=TOOLSET, handler=qualification_read_file,
    schema={"name": "qualification_read_file", "description": "Read the fixed synthetic qualification file and verify its nonce. No path argument is accepted.",
            "parameters": {"type": "object", "additionalProperties": False,
                           "properties": {"nonce": {"type": "string", "pattern": "^[A-Za-z0-9_-]{8,128}$"}},
                           "required": ["nonce"]}},
    check_fn=lambda: True, max_result_size_chars=8192, execution_timeout=5)


def _prepare_home(config):
    for directory in (HOME, WORKSPACE):
        metadata = directory.lstat()
        if (not stat.S_ISDIR(metadata.st_mode) or directory.is_symlink()
                or metadata.st_uid != os.geteuid() or metadata.st_mode & 0o077):
            raise ValueError("Qualification home/workspace must be private directories owned by the runtime UID")
    # JSON is a YAML subset accepted by ruamel.yaml. This never contains keys.
    desired = (json.dumps(config, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
    target = HOME / "config.yaml"
    if target.exists() or target.is_symlink():
        if _read_private_file(target, maximum=65536) != desired:
            raise ValueError("Existing Hermes config differs; use a fresh qualification home")
    else:
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as output:
            output.write(desired)


def main():
    os.umask(0o077)
    config = json.loads(_read_private_file("/run/config/model.json", require_read_only=True))
    if not isinstance(config, dict) or set(config) != {"base_url", "model"}:
        raise ValueError("Model config must have exactly base_url and model")
    base_url, model = config["base_url"], config["model"]
    if not isinstance(base_url, str) or not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9_.:/-]{1,200}", model):
        raise ValueError("Invalid model configuration")
    parsed = urllib.parse.urlsplit(base_url)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or len(base_url) > 2048):
        raise ValueError("Model endpoint must be credential-free HTTPS")
    key = _read_private_file("/run/secrets/model-api-key", require_read_only=True).decode().strip()
    token = _read_private_file("/run/secrets/api-token", require_read_only=True).decode().strip()
    if (not key or len(key) > 8192 or any(c.isspace() or ord(c) < 32 for c in key)
            or not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", token)):
        raise ValueError("Invalid private credential format")
    # Introduced only after container launch, never Docker ENV/build args/argv.
    # Managed workers inherit these in-memory values; no .env/auth file is written.
    os.environ.update({"HARNIQO_MODEL_API_KEY": key, "API_SERVER_KEY": token,
                       "HERMES_HOME": str(HOME), "HOME": str(HOME), "HERMES_SAFE_MODE": "1",
                       "HERMES_DISABLE_LAZY_INSTALLS": "1", "HERMES_WRITE_SAFE_ROOT": str(WORKSPACE)})
    _prepare_home({
        "model": {"default": model, "provider": "custom", "base_url": base_url,
                  "key_env": "HARNIQO_MODEL_API_KEY", "api_mode": "chat_completions", "streaming": True},
        "agent": {"max_turns": 4, "run_budget_seconds": 90, "api_max_retries": 1, "coding_context": "off",
                  "auto_recovery_cycles": 0}, "terminal": {"cwd": str(WORKSPACE)},
        "auxiliary": {"title_generation": {"enabled": False}},
        "tools": {"tool_search": {"enabled": "off"}},
        "platform_toolsets": {"api_server": [TOOLSET]}, "mcp_servers": {},
        "platforms": {"api_server": {"enabled": True, "host": "0.0.0.0", "port": 8642,
                                     "model_name": "hermes-agent", "cors_origins": []}},
        "plugins": {"enabled": []}, "security": {"allow_lazy_installs": False},
        "gateway": {"multiplex_profiles": False},
    })
    from gateway.run import start_gateway
    result = asyncio.run(start_gateway(verbosity=0, replace=False))
    return 0 if result else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, UnicodeError):
        # Do not emit credential source, endpoint or raw exception text.
        print("Hermes qualification launcher rejected its private inputs or runtime state", file=sys.stderr)
        raise SystemExit(2)
