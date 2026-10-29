"""Qualification-only observation of actual native SDK sends; never log secrets.

This module does not resolve, rewrite, retry, or forward inference requests. The
native HTTP client still performs its original send. Only a bounded model hash,
actual final URL hash and classified response outcome enter private evidence.
"""
from __future__ import annotations

import fcntl
import functools
import hashlib
import json
import os
from pathlib import Path
import stat

WORKSPACE = Path("/data/workspace")
FILENAME = "qualification-transport.jsonl"
MAX_LOG = 256 * 1024
MAX_BODY = 2 * 1024**2
_INSTALLED = "_harniqo_qualification_transport_audit_v1"


def _hash(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _is_inference(request):
    # Match the originally addressed SDK operation, including a final redirect
    # to a different path. The observation itself uses response.request below.
    return request.url.path.rstrip("/").endswith("/chat/completions")


def _model_hash(request):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate_json_key")
            value[key] = item
        return value
    try:
        raw = request.content
        if not isinstance(raw, bytes) or len(raw) > MAX_BODY:
            return _hash("<unobserved-model>")
        payload = json.loads(raw, object_pairs_hook=unique,
                             parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite_json")))
        model = payload.get("model") if isinstance(payload, dict) else None
        if not isinstance(model, str) or not 1 <= len(model) <= 200:
            return _hash("<unobserved-model>")
        return _hash(model)
    except (AttributeError, UnicodeError, ValueError, RuntimeError):
        return _hash("<unobserved-model>")


def append_record(value):
    """Append atomically under a cross-process lock; unsafe evidence fails closed."""
    for path in (WORKSPACE, *WORKSPACE.parents):
        if path.is_symlink():
            raise RuntimeError("qualification_transport_workspace_symlink")
    info = WORKSPACE.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise RuntimeError("qualification_transport_workspace_permissions")
    raw = (json.dumps(value, separators=(",", ":"), allow_nan=False) + "\n").encode()
    if len(raw) > 4096:
        raise RuntimeError("qualification_transport_record_size")
    directory = os.open(WORKSPACE, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        opened = os.fstat(directory)
        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            raise RuntimeError("qualification_transport_workspace_changed")
        descriptor = os.open(FILENAME,
                             os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_CLOEXEC,
                             0o600, dir_fd=directory)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            info = os.fstat(descriptor)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
                raise RuntimeError("qualification_transport_file_permissions")
            if info.st_size + len(raw) > MAX_LOG:
                raise RuntimeError("qualification_transport_log_size")
            if os.write(descriptor, raw) != len(raw):
                raise RuntimeError("qualification_transport_short_write")
            os.fsync(descriptor)
            os.fsync(directory)
        finally:
            os.close(descriptor)
    finally:
        os.close(directory)


def record_send(request, response=None):
    actual = response.request if response is not None else request
    append_record({"schema_version": 1, "kind": "chat_completions_http_response",
                   "method": actual.method, "target_sha256": _hash(str(actual.url)),
                   "body_model_sha256": _model_hash(actual),
                   "status_code": int(response.status_code) if response is not None else 0,
                   "redirect_count": len(response.history) if response is not None else 0,
                   "network_error": response is None})


def _sync_wrapper(original):
    @functools.wraps(original)
    def send(client, request, *args, **kwargs):
        if not _is_inference(request):
            return original(client, request, *args, **kwargs)
        try:
            response = original(client, request, *args, **kwargs)
        except Exception:
            record_send(request)
            raise
        try:
            record_send(request, response)
        except BaseException:
            response.close()
            raise
        return response
    setattr(send, _INSTALLED, True)
    return send


def _async_wrapper(original):
    @functools.wraps(original)
    async def send(client, request, *args, **kwargs):
        if not _is_inference(request):
            return await original(client, request, *args, **kwargs)
        try:
            response = await original(client, request, *args, **kwargs)
        except Exception:
            record_send(request)
            raise
        try:
            record_send(request, response)
        except BaseException:
            await response.aclose()
            raise
        return response
    setattr(send, _INSTALLED, True)
    return send


def install():
    """Install once in each actual gateway/worker process; no file or key reads."""
    import httpx
    if not getattr(httpx.Client.send, _INSTALLED, False):
        httpx.Client.send = _sync_wrapper(httpx.Client.send)
    if not getattr(httpx.AsyncClient.send, _INSTALLED, False):
        httpx.AsyncClient.send = _async_wrapper(httpx.AsyncClient.send)
