#!/usr/bin/env python3
"""One explicitly selected, bounded OpenCode case; not a production Runner."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import subprocess
import sys
import time
import urllib.request

from probe_hermes import Rejected, digest, no_links, private_directory, read_file, require, write_report
from run_hermes_qualification import (TASK_LABEL, cleanup, create_private, docker, inspect,
                                     owned, port_available, select_endpoint)
from probe_opencode import (BASE, Client, assistant_completed, exact_tool_paths, permitted_output,
                            private_bytes, write_json)

IMAGE = 'sha256:ef392b1148602b61ad4ea1e9dcca9b05337adc3c7ef9fc97434d632c69b8373c'
COMMIT = '810c0eba1a7348036f8bdf61e6632d0c36c6876c'
PORT = 10012


def launch(name, attempt, case, state, key):
    args = ['run', '--detach', '--pull', 'never', '--name', name, '--label', TASK_LABEL + '=' + attempt,
            '--user', f'{os.geteuid()}:{os.getegid()}', '--init', '--read-only', '--cap-drop', 'ALL',
            '--security-opt', 'no-new-privileges', '--pids-limit', '128', '--memory', '4g', '--cpus', '2',
            '--log-driver', 'local', '--log-opt', 'max-size=10m', '--log-opt', 'max-file=2',
            '--tmpfs', '/tmp:rw,noexec,nosuid,nodev,size=256m,mode=1777',
            '--publish', '127.0.0.1:10012:4096']
    for source, target, ro in [(state/'home', '/data/opencode', False),
                               (state/'workspace', '/data/workspace', False),
                               (case/'model.json', '/run/config/model.json', True),
                               (case/'api-password', '/run/secrets/api-password', True),
                               (key, '/run/secrets/model-api-key', True)]:
        require(',' not in str(source) and '\n' not in str(source), 'invalid_mount_path')
        args += ['--mount', f'type=bind,src={source},dst={target}' + (',readonly' if ro else '')]
    return args + [IMAGE]


def ready(client):
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        try:
            status, value = client.request('/global/health', timeout=2)
            if status == 200 and value == {'healthy': True, 'version': '1.19.5'}:
                return
        except (OSError, ValueError):
            pass
        time.sleep(.2)
    raise Rejected('native_health_timeout')


def messages(client, session):
    status, value = client.request('/session/' + session + '/message')
    require(status == 200 and isinstance(value, list), 'history_query_failed')
    return value


def native_state(client, session):
    status, value = client.request('/session/status')
    require(status == 200 and isinstance(value, dict), 'status_query_failed')
    return value.get(session, {'type': 'idle'})


def assistants(history):
    return [item for item in history if item.get('info', {}).get('role') == 'assistant']


def tools(history):
    return [part for item in history for part in item.get('parts', []) if part.get('type') == 'tool']


def is_streaming(history, state, event=None):
    values = assistants(history)
    properties = (event or {}).get('properties', {})
    delta = (bool(values) and (event or {}).get('type') == 'message.part.delta'
             and properties.get('sessionID') == values[-1]['info'].get('sessionID')
             and properties.get('messageID') == values[-1]['info'].get('id')
             and properties.get('field') == 'text' and bool(properties.get('delta')))
    return (state.get('type') == 'busy' and bool(values)
            and not values[-1]['info'].get('time', {}).get('completed')
            and not values[-1]['info'].get('error')
            and (delta or any(p.get('type') in {'text', 'reasoning'} and bool(p.get('text'))
                             for p in values[-1].get('parts', []))))


def cancelled(history, state):
    values = assistants(history)
    return (state.get('type') == 'idle' and bool(values)
            and values[-1]['info'].get('error', {}).get('name') == 'MessageAbortedError'
            and bool(values[-1]['info'].get('time', {}).get('completed'))
            and all(p.get('state', {}).get('status') not in {'running', 'pending'} for p in tools(history)))


def configured_model(history, model):
    values = assistants(history)
    return bool(values) and all(v['info'].get('modelID') == model
                               and v['info'].get('providerID') == 'harniqo' for v in values)


def create_session(client, label):
    status, value = client.request('/session', {'title': 'Harniqo qualification ' + label})
    require(status == 200 and isinstance(value.get('id'), str), 'session_create_failed')
    return value['id']


def submit(client, case, session, model, prompt):
    # This marker is created BEFORE sending; unknown HTTP outcomes never retry.
    write_json(case/'submission.json', {'session_id': session, 'model': model, 'prompt': prompt,
                                       'maximum_fresh_tasks': 1, 'state': 'about_to_submit'})
    status, _ = client.request('/session/' + session + '/prompt_async', {
        'model': {'providerID': 'harniqo', 'modelID': model},
        'parts': [{'type': 'text', 'text': prompt}]})
    require(status == 204, 'prompt_not_accepted_no_retry')


def terminal(client, session, timeout=90):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        history, state = messages(client, session), native_state(client, session)
        values = assistants(history)
        require(not any(v['info'].get('error') for v in values), 'native_turn_error')
        if state.get('type') == 'idle' and assistant_completed(values):
            return history, state
        time.sleep(.1)
    raise Rejected('native_terminal_timeout')


def recovery(client, case, state, name, attempt, identity, model, report):
    prior = json.loads(private_bytes(state/'probe/report.json', 4*1024**2))
    require(prior.get('native_smoke_passed') is True, 'prior_smoke_not_passed')
    session = prior['native_session_id'];report['session_id'] = session
    old = json.loads(private_bytes(state/'probe/native-messages.json', 4*1024**2))
    expected = json.loads(private_bytes(state/'workspace/qualification-input.json'))
    artifact = private_bytes(state/'workspace/qualification-result.json')
    require(messages(client, session) == old, 'history_changed_after_recreation')
    report['history_recreated'] = True
    before = owned(inspect(identity), attempt, IMAGE, identity)
    docker('restart', '--time', '10', before, timeout=30)
    owned(inspect(identity), attempt, IMAGE, identity)
    ready(client)
    require(messages(client, session) == old, 'history_changed_after_restart')
    report['history_after_docker_restart'] = True
    submit(client, case, session, model,
           'Without tools or reading files, recall the exact nonce and challenge from the JSON you previously read. '
           'Return those two values as JSON, with no other operations.')
    current, status = terminal(client, session)
    write_json(case/'native-messages.json', current);write_json(case/'native-status.json', status)
    require(current[:len(old)] == old and len(current) > len(old), 'history_prefix_changed')
    appended = current[len(old):]
    text = '\n'.join(p.get('text', '') for v in assistants(appended) for p in v.get('parts', []) if p.get('type') == 'text')
    require(all(value in text for value in expected.values()), 'recall_mismatch')
    require(not tools(appended) and configured_model(appended, model), 'continuation_tools_or_model_mismatch')
    require(private_bytes(state/'workspace/qualification-result.json') == artifact, 'artifact_changed')
    report.update(native_case_passed=True, history_prefix_preserved=True, no_tools_in_continuation=True,
                  random_values_recalled=True, artifact_sha256=digest(artifact))


def denial(client, case, state, model, report):
    session = create_session(client, 'denial');report['session_id'] = session
    output = state/'workspace/qualification-result.json'
    require(not output.exists(), 'output_not_fresh')
    status, project = client.request('/project/current');require(status == 200, 'project_query_failed')
    submit(client, case, session, model,
           'Use the native write tool to create qualification-result.json containing {"denial_probe":true}. '
           'Do not use other files or tools. If permission is denied, stop and do not retry.')
    deadline = time.monotonic() + 60
    request = None
    while time.monotonic() < deadline:
        status, pending = client.request('/permission');require(status == 200 and isinstance(pending, list), 'permission_query_failed')
        if pending:
            require(len(pending) == 1 and pending[0].get('sessionID') == session, 'unexpected_permission')
            request = pending[0];write_json(case/'native-permission.json', request)
            require(permitted_output(request, project['worktree']), 'permission_path_mismatch')
            history = messages(client, session)
            calls = [p for p in tools(history) if p.get('callID') == request.get('tool', {}).get('callID')]
            require(len(calls) == 1 and calls[0]['tool'] == 'write' and exact_tool_paths(calls), 'permission_call_mismatch')
            write_json(case/'before-reject.json', history)
            require(not output.exists(), 'write_preceded_permission')
            status, _ = client.request('/permission/' + request['id'] + '/reply', {'reply': 'reject'})
            require(status == 200, 'native_reject_failed');report['reject_status'] = status
            break
        time.sleep(.1)
    require(request is not None, 'pending_permission_not_seen')
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        history, status = messages(client, session), native_state(client, session)
        call = [p for p in tools(history) if p.get('callID') == request['tool']['callID']]
        if status.get('type') == 'idle' and call and call[0].get('state', {}).get('status') == 'error':
            break
        time.sleep(.1)
    else:
        raise Rejected('rejection_not_settled')
    write_json(case/'native-messages.json', history);write_json(case/'native-status.json', status)
    require(not output.exists() and exact_tool_paths(tools(history)), 'rejection_side_effect')
    require(all(p.get('state', {}).get('status') not in {'pending', 'running'} for p in tools(history)),
            'tool_still_active_after_reject')
    pending_status, pending = client.request('/permission')
    require(pending_status == 200 and isinstance(pending, list), 'permission_query_failed')
    write_json(case/'permissions-after-reject.json', pending)
    require(not any(p.get('sessionID') == session for p in pending), 'permission_still_pending_after_reject')
    require('reject' in call[0]['state'].get('error', '').lower(), 'native_rejection_reason_missing')
    require(configured_model(history, model), 'configured_model_mismatch')
    # Observe replay semantics without guessing whether upstream rejects or no-ops.
    report['late_reject_status'] = client.request('/permission/' + request['id'] + '/reply', {'reply': 'reject'})[0]
    require(not output.exists(), 'late_reject_side_effect')
    report.update(native_case_passed=True, denied_tool_call_confirmed=True, output_absent=True)


def cancellation(client, case, model, report):
    session = create_session(client, 'generation cancellation');report['session_id'] = session
    request = urllib.request.Request(BASE+'/event', headers={'Authorization': client.authorization,
                                     'x-opencode-directory': '/data/workspace', 'Accept': 'text/event-stream'})
    # Text deltas are emitted before the final persisted text part. Subscribe
    # before submission rather than hoping a SQL history poll catches streaming.
    with client.opener.open(request, timeout=60) as stream:
        require(stream.headers.get_content_type() == 'text/event-stream', 'native_sse_required')
        submit(client, case, session, model,
               'Do not use tools. Produce a long numbered list of 500 distinct fictional city names, '
               'one per line, beginning immediately with item 1. Do not stop early or add explanations.')
        deadline = time.monotonic()+60;seen = 0;total = 0
        while time.monotonic() < deadline and seen < 1024:
            line = stream.readline(65537);total += len(line)
            require(bool(line) and len(line) <= 65536 and total <= 1024**2, 'sse_bound_or_eof')
            if not line.startswith(b'data:'):
                continue
            event = json.loads(line[5:]);seen += 1
            if event.get('type') != 'message.part.delta' or event.get('properties', {}).get('sessionID') != session:
                continue
            history, state = messages(client, session), native_state(client, session)
            if is_streaming(history, state, event):
                write_json(case/'before-abort-event.json', event)
                write_json(case/'before-abort-history.json', history);write_json(case/'before-abort-status.json', state)
                report['native_sse_delta_observed'] = True
                break
            require(not assistant_completed(assistants(history)), 'generation_finished_before_cancel')
        else:
            raise Rejected('active_generation_not_observed')
    start = time.monotonic()
    status, reply = client.request('/session/' + session + '/abort', {})
    report['abort_http_status'] = status;require(status == 200 and reply is True, 'abort_not_accepted')
    while time.monotonic() - start < 20:
        history, state = messages(client, session), native_state(client, session)
        if cancelled(history, state):
            break
        time.sleep(.05)
    else:
        raise Rejected('native_abort_not_confirmed')
    write_json(case/'native-messages.json', history);write_json(case/'native-status.json', state)
    require(not tools(history) and configured_model(history, model), 'cancel_tools_or_model_mismatch')
    report.update(native_case_passed=True, native_abort_confirmed=True,
                  cancellation_seconds=round(time.monotonic()-start, 3),
                  cancellation_scope='observed_native_text_or_reasoning_generation', provider_billing_stop='unknown')


def run(args):
    require(sys.platform == 'linux', 'linux_host_required')
    root = no_links(args.root);case = no_links(args.case)
    require(shutil.disk_usage(root).free >= 10*1024**3, 'insufficient_disk')
    configuration, key = select_endpoint(no_links(args.config), args.endpoint_id)
    provenance = json.loads(read_file(no_links(args.provenance), 4*1024**2, private=False))
    require(provenance.get('image_id') == IMAGE and provenance.get('commit') == COMMIT, 'wrong_image_provenance')
    require(provenance['entrypoint_sha256'] == digest((root/'deploy/qualification/opencode/entrypoint.py').read_bytes()), 'entrypoint_changed')
    require(provenance['recipe_sha256'] == digest((root/'deploy/qualification/opencode/Dockerfile').read_bytes()), 'recipe_changed')
    require(not case.exists(), 'case_exists');private_directory(case.parent, create=True)
    state = case
    if args.mode == 'recovery':
        require(args.recover_from is not None, 'prior_case_required');state = no_links(args.recover_from)
        previous = json.loads(private_bytes(state/'operator.json', 4*1024**2))
        require(previous.get('status') == 'passed' and previous.get('image_id') == IMAGE
                and previous.get('expected_model') == configuration['model']
                and previous.get('cleanup', {}).get('container_removed') is True, 'prior_case_not_stopped')
        require(json.loads(private_bytes(state/'model.json')) == configuration, 'recovery_configuration_changed')
        private_directory(state/'home');private_directory(state/'workspace')
    else:
        require(args.recover_from is None, 'unexpected_prior_case')
    locks = private_directory(root/'.private/qualification/locks', create=True)
    lock_fd = os.open(locks/'opencode-port-10012', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(lock_fd, 'a+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        require(port_available(PORT), 'port_busy')
        case.mkdir(mode=0o700)
        if state == case:
            (state/'home').mkdir(mode=0o700);(state/'workspace').mkdir(mode=0o700)
        token = secrets.token_urlsafe(48)
        create_private(case/'model.json', json.dumps(configuration).encode())
        create_private(case/'api-password', token.encode())
        attempt = secrets.token_hex(16);name = 'harniqo-opencode-' + attempt
        report = {'mode': args.mode, 'status': 'running', 'native_case_passed': False, 'image_id': IMAGE,
                  'source_commit': COMMIT, 'expected_model': configuration['model'], 'attempt': attempt,
                  'served_model_evidence': 'native_configured_metadata_only', 'milestone_complete': False}
        report['source_sha256'] = {str(p.relative_to(root)): digest(p.read_bytes()) for p in [
            root/'scripts/qualification'/n for n in ('run_opencode_qualification.py', 'probe_opencode.py',
                                                   'run_hermes_qualification.py', 'probe_hermes.py')]}
        identity = None;client = Client(token)
        write_report(case/'operator.json', report)
        try:
            docker(*launch(name, attempt, case, state, key))
            value = inspect(name);identity = owned(value, attempt, IMAGE)
            require(value.get('running') is True and value.get('ports') == {'4096/tcp': [{'HostIp': '127.0.0.1', 'HostPort': '10012'}]}, 'launch_state_mismatch')
            report['container_id'] = identity;write_report(case/'operator.json', report)
            ready(client)
            if args.mode == 'smoke':
                command = [sys.executable, str(root/'scripts/qualification/probe_opencode.py'),
                           '--api-password', str(case/'api-password'), '--model-config', str(case/'model.json'),
                           '--workspace', str(state/'workspace'), '--evidence-dir', str(case/'probe')]
                with (case/'probe-cli.log').open('xb') as log:
                    with subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, start_new_session=True) as child:
                        try:
                            code = child.wait(timeout=180)
                        except BaseException:
                            os.killpg(child.pid, signal.SIGKILL);child.wait(timeout=5);raise
                observed = json.loads(private_bytes(case/'probe/report.json', 4*1024**2))
                report['native_case_passed'] = code == 0 and observed.get('native_smoke_passed') is True
            elif args.mode == 'recovery':
                recovery(client, case, state, name, attempt, identity, configuration['model'], report)
            elif args.mode == 'denial':
                denial(client, case, state, configuration['model'], report)
            else:
                cancellation(client, case, configuration['model'], report)
            require(report['native_case_passed'], 'native_case_failed')
            report['status'] = 'passed'
        except BaseException as error:
            report.update(status='failed', failure_code=error.code if isinstance(error, Rejected) else type(error).__name__)
            if report.get('session_id'):
                try:
                    write_json(case/'failure-history.json', messages(client, report['session_id']))
                    report['failure_abort_status'] = client.request('/session/'+report['session_id']+'/abort', {})[0]
                except (OSError, ValueError, Rejected):
                    report['failure_abort_status'] = None
        finally:
            try:
                report['cleanup'] = cleanup(name, attempt, IMAGE, identity, case)
                require(report['cleanup'].get('execution_stop_confirmed') is True and report['cleanup'].get('logs_complete') is True, 'cleanup_not_confirmed')
                report['port_released'] = port_available(PORT);require(report['port_released'], 'port_not_released')
            except BaseException as error:
                report.update(status='failed', cleanup_failure=error.code if isinstance(error, Rejected) else type(error).__name__)
            write_report(case/'operator.json', report)
        print(json.dumps({'status': report['status'], 'native_case_passed': report['native_case_passed'],
                          'mode': args.mode, 'report': str(case/'operator.json')}))
        return 0 if report['status'] == 'passed' else 1


def main():
    os.umask(0o077)
    def interrupt(signum, frame):
        raise Rejected('operator_signal_' + str(signum))
    for signum in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, interrupt)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--endpoint-id', required=True)
    parser.add_argument('--provenance', type=Path, required=True)
    parser.add_argument('--case', type=Path, required=True)
    parser.add_argument('--mode', choices=('smoke', 'recovery', 'denial', 'cancellation'), required=True)
    parser.add_argument('--recover-from', type=Path)
    try:
        return run(parser.parse_args())
    except (Rejected, OSError, ValueError) as error:
        print(json.dumps({'status': 'rejected', 'code': error.code if isinstance(error, Rejected) else type(error).__name__}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
