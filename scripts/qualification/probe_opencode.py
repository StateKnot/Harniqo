#!/usr/bin/env python3
"""One paid native OpenCode smoke task after health/auth admission checks.

Uses only fixed loopback port 10012, one fresh native session, one prompt and
one narrowly checked permission approval. Never retries a submitted prompt.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import posixpath
import secrets
import stat
import time
import urllib.error
import urllib.request

BASE = 'http://127.0.0.1:10012'


def private_bytes(path, maximum=16384):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    with os.fdopen(descriptor, 'rb') as source:
        metadata = os.fstat(source.fileno())
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077
                or metadata.st_uid not in {0, os.geteuid()} or metadata.st_size > maximum):
            raise ValueError('Private input permission/size/type rejected')
        result = source.read(maximum + 1)
        if len(result) > maximum:
            raise ValueError('Private input exceeds bound')
        return result


def write_json(path, value):
    with path.open('x', encoding='utf-8') as output:
        json.dump(value, output, ensure_ascii=False, sort_keys=True, indent=2)
        output.write('\n')
        output.flush()
        os.fsync(output.fileno())


class NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError('Native loopback probe refuses redirects')


class Client:
    def __init__(self, password):
        self.authorization = 'Basic ' + base64.b64encode(('opencode:' + password).encode()).decode()
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirects())

    def request(self, path, payload=None, authorization=True, timeout=10):
        headers = {'Accept': 'application/json', 'x-opencode-directory': '/data/workspace'}
        if authorization:
            headers['Authorization'] = self.authorization if authorization is True else authorization
        data = None if payload is None else json.dumps(payload).encode()
        if data is not None:
            headers['Content-Type'] = 'application/json'
        request = urllib.request.Request(BASE + path, data=data, headers=headers)
        try:
            with self.opener.open(request, timeout=timeout) as response:
                status, raw = response.status, response.read(4 * 1024**2 + 1)
        except urllib.error.HTTPError as error:
            status, raw = error.code, error.read(65537)
        if len(raw) > 4 * 1024**2:
            raise ValueError('Native HTTP response exceeds bound')
        return status, json.loads(raw) if raw and status < 400 else None


def permitted_output(request, worktree='/data/workspace'):
    if not isinstance(request, dict) or request.get('permission') != 'edit':
        return False
    if worktree not in {'/', '/data/workspace'}:
        return False
    if request.get('metadata', {}).get('filepath') != '/data/workspace/qualification-result.json':
        return False
    patterns = request.get('patterns')
    if not isinstance(patterns, list) or not patterns:
        return False
    # Native write patterns are relative to Instance.worktree, which is `/`
    # for a nongit global project, rather than relative to the request cwd.
    return all(isinstance(pattern, str) and not any(char in pattern for char in '*?[]\x00')
               and posixpath.normpath(posixpath.join(worktree, pattern)) == '/data/workspace/qualification-result.json'
               for pattern in patterns)


def exact_tool_paths(tools):
    """Check every call, including failed calls, against the two native paths."""
    allowed_reads = {'qualification-input.json', './qualification-input.json',
                     '/data/workspace/qualification-input.json',
                     'qualification-result.json', './qualification-result.json',
                     '/data/workspace/qualification-result.json'}
    allowed_writes = {'qualification-result.json', './qualification-result.json',
                      '/data/workspace/qualification-result.json'}
    for part in tools:
        tool = part.get('tool')
        arguments = part.get('state', {}).get('input')
        if tool not in {'read', 'write'} or not isinstance(arguments, dict):
            return False
        file_path = arguments.get('filePath')
        if not isinstance(file_path, str) or file_path not in (allowed_reads if tool == 'read' else allowed_writes):
            return False
    return bool(tools)


def read_write_read(tools):
    """Require the requested completed operations in native history order."""
    phase = 0
    for part in tools:
        if part.get('state', {}).get('status') != 'completed':
            continue
        tool = part.get('tool')
        file_path = part.get('state', {}).get('input', {}).get('filePath')
        if not isinstance(file_path, str):
            continue
        name = posixpath.basename(file_path)
        if phase == 0 and tool == 'read' and name == 'qualification-input.json':
            phase = 1
        elif phase == 1 and tool == 'write' and name == 'qualification-result.json':
            phase = 2
        elif phase == 2 and tool == 'read' and name == 'qualification-result.json':
            phase = 3
    return phase == 3


def assistant_completed(assistant):
    # Assistant messages use `time.completed`; `time.end` belongs to tool
    # parts. Require the native successful finish and actual completion time.
    if not assistant:
        return False
    info = assistant[-1].get('info', {})
    completed = info.get('time', {}).get('completed')
    return (not info.get('error') and info.get('finish') == 'stop'
            and type(completed) in {int, float} and completed > 0)


def run(args, report):
    password = private_bytes(args.api_password).decode().strip()
    model = json.loads(private_bytes(args.model_config))['model']
    client = Client(password)
    ready = None
    for _ in range(30):
        try:
            status, ready = client.request('/global/health', timeout=2)
            if status == 200:
                break
        except (OSError, ValueError):
            pass
        time.sleep(1)
    if not isinstance(ready, dict) or ready.get('healthy') is not True or ready.get('version') != '1.19.5':
        raise ValueError('Native health/version admission failed before model call')
    report['health_version'] = ready['version']
    for label, authorization in (('missing', False), ('wrong', 'Basic b3BlbmNvZGU6aW52YWxpZA==')):
        status, _ = client.request('/global/health', authorization=authorization)
        report['auth_' + label + '_status'] = status
        if status not in {401, 403}:
            raise ValueError('Native authentication admission failed before model call')
    status, document = client.request('/doc')
    if status != 200 or not isinstance(document, dict):
        raise ValueError('Native OpenAPI discovery failed before model call')
    write_json(args.evidence_dir / 'native-openapi.json', document)
    report['openapi_version'] = document.get('openapi')
    status, project = client.request('/project/current')
    if status != 200 or not isinstance(project, dict) or project.get('worktree') not in {'/', '/data/workspace'}:
        raise ValueError('Native project/worktree discovery failed before model call')
    worktree = project['worktree']
    write_json(args.evidence_dir / 'native-project.json', project)
    challenge = {'nonce': secrets.token_hex(16), 'challenge': secrets.token_hex(32)}
    write_json(args.workspace / 'qualification-input.json', challenge)
    if (args.workspace / 'qualification-result.json').exists():
        raise ValueError('Fresh task output already exists')
    status, session = client.request('/session', {'title': 'Harniqo native qualification ' + challenge['nonce']})
    if status not in {200, 201} or not isinstance(session, dict) or not isinstance(session.get('id'), str):
        raise ValueError('Native session creation failed before model call')
    session_id = session['id']
    report['native_session_id'] = session_id
    write_json(args.evidence_dir / 'native-session.json', session)
    prompt = ('Use the native read tool to read qualification-input.json in the current workspace. '
              'Use the native write tool to create qualification-result.json containing exactly the same JSON. '
              'Then use the native read tool to read qualification-result.json and report completion. '
              'Use only these two paths. Do not use shell, web, delegation, skills or other files.')
    # Persist the fact that the sole paid request is about to be submitted. If
    # transport outcome is unknown, later investigation must not submit again.
    write_json(args.evidence_dir / 'model-task-submission.json', {
        'native_session_id': session_id, 'configured_model_id': model,
        'prompt': prompt, 'maximum_fresh_model_tasks': 1, 'status': 'about_to_submit',
    })
    report['native_model_task_attempted'] = True
    status, _ = client.request('/session/' + session_id + '/prompt_async', {
        'model': {'providerID': 'harniqo', 'modelID': model},
        'parts': [{'type': 'text', 'text': prompt}],
    })
    report['prompt_http_status'] = status
    if status != 204:
        raise ValueError('Sole native prompt was not accepted; no paid retry allowed')
    approvals, messages = [], []
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        status, pending = client.request('/permission')
        if status != 200 or not isinstance(pending, list):
            raise ValueError('Native permission query failed')
        for request in pending:
            if not isinstance(request, dict):
                raise ValueError('Malformed native permission request')
            if request.get('sessionID') != session_id:
                raise ValueError('Unexpected foreign-session permission request')
            # Preserve even a refused native request before inspecting its
            # contents; pending permissions are not durable after abort.
            write_json(args.evidence_dir / ('native-permission-request-' + str(len(approvals)) + '.json'), request)
            if approvals or not permitted_output(request, worktree):
                raise ValueError('Native permission exceeds single exact output approval')
            request_id = request['id']
            status, before_approval = client.request('/session/' + session_id + '/message')
            if status != 200 or not isinstance(before_approval, list):
                raise ValueError('Cannot correlate native permission with tool history')
            write_json(args.evidence_dir / 'native-messages-before-approval.json', before_approval)
            call_id = request.get('tool', {}).get('callID')
            calls = [part for item in before_approval for part in item.get('parts', [])
                     if part.get('type') == 'tool' and part.get('callID') == call_id]
            if not call_id or len(calls) != 1 or calls[0].get('tool') != 'write' or not exact_tool_paths(calls):
                raise ValueError('Native permission is not the exact pending output write call')
            status, reply = client.request('/permission/' + request_id + '/reply', {'reply': 'once'})
            if status != 200:
                raise ValueError('Native exact permission approval failed')
            approvals.append({'id': request_id, 'reply': 'once', 'status': status})
            report['approvals'] = approvals
        status, messages = client.request('/session/' + session_id + '/message')
        if status != 200 or not isinstance(messages, list):
            raise ValueError('Native message history query failed')
        status, states = client.request('/session/status')
        if status != 200 or not isinstance(states, dict):
            raise ValueError('Native session state query failed')
        assistant = [item for item in messages if item.get('info', {}).get('role') == 'assistant']
        if any(item['info'].get('error') for item in assistant):
            write_json(args.evidence_dir / 'native-messages.json', messages)
            raise ValueError('Native model turn reported an error; no paid retry allowed')
        active = states.get(session_id, {}).get('type') in {'busy', 'retry'}
        report['native_session_status_at_last_poll'] = states.get(session_id, {'type': 'idle'})
        if not active and assistant_completed(assistant):
            break
        time.sleep(0.5)
    else:
        raise ValueError('Sole native task exceeded the probe deadline')
    write_json(args.evidence_dir / 'native-messages.json', messages)
    tools = [part for item in messages for part in item.get('parts', []) if part.get('type') == 'tool']
    completed = [part for part in tools if part.get('state', {}).get('status') == 'completed']
    report['native_tool_calls'] = [{'call_id': part.get('callID'), 'tool': part.get('tool'),
                                   'status': part.get('state', {}).get('status')} for part in tools]
    report['native_tool_paths_verified'] = exact_tool_paths(tools)
    if not report['native_tool_paths_verified']:
        raise ValueError('Native tool trace included an unapproved tool or input/output path')
    configured = [{'model_id': item['info'].get('modelID'), 'provider_id': item['info'].get('providerID'),
                   'tokens': item['info'].get('tokens'), 'cost_native': item['info'].get('cost')}
                  for item in messages if item.get('info', {}).get('role') == 'assistant']
    report['model_metadata'] = configured
    report['served_model_evidence'] = 'native_configured_metadata_only'
    if not configured or any(item['model_id'] != model or item['provider_id'] != 'harniqo' for item in configured):
        raise ValueError('Native configured model metadata mismatched the approved route')
    raw = private_bytes(args.workspace / 'qualification-result.json')
    if json.loads(raw) != challenge:
        raise ValueError('Actual output did not contain the unpredictable input challenge')
    if len(approvals) != 1 or not read_write_read(completed):
        raise ValueError('Native read/write/read and exact approval trace was incomplete')
    report.update({'actual_artifact_sha256': hashlib.sha256(raw).hexdigest(),
                   'unpredictable_challenge_verified': True, 'native_smoke_passed': True})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--api-password', type=Path, required=True)
    parser.add_argument('--model-config', type=Path, required=True)
    parser.add_argument('--workspace', type=Path, required=True)
    parser.add_argument('--evidence-dir', type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    metadata = args.workspace.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid() or metadata.st_mode & 0o077:
        raise SystemExit('Workspace ownership/permissions rejected')
    args.evidence_dir.mkdir(mode=0o700)
    report = {'scope': 'one-task-native-opencode-smoke', 'native_model_task_attempted': False,
              'native_smoke_passed': False, 'm0_04_accepted': False}
    try:
        run(args, report)
    except (OSError, ValueError, KeyError, TypeError) as error:
        report['failure'] = str(error)[:256]
    finally:
        # Include KeyboardInterrupt and unexpected exceptions: an asynchronous
        # native task outlives this CLI unless its owning controller stops it.
        # Abort acceptance is recorded; it does not claim execution has stopped.
        if report.get('native_session_id') and not report['native_smoke_passed']:
            try:
                client = Client(private_bytes(args.api_password).decode().strip())
                for name, path in (
                    ('native-messages-on-failure.json', '/session/' + report['native_session_id'] + '/message'),
                    ('native-permissions-on-failure.json', '/permission'),
                ):
                    status, value = client.request(path)
                    if status == 200:
                        write_json(args.evidence_dir / name, value)
                report['cleanup_abort_http_status'] = client.request('/session/' + report['native_session_id'] + '/abort', {})[0]
            except (OSError, ValueError):
                report['cleanup_abort_http_status'] = None
        write_json(args.evidence_dir / 'report.json', report)
    print(json.dumps({'native_smoke_passed': report['native_smoke_passed'],
                      'native_model_task_attempted': report['native_model_task_attempted'],
                      'report': str(args.evidence_dir / 'report.json'), 'm0_04_accepted': False}))
    return 0 if report['native_smoke_passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
