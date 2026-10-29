#!/usr/bin/env python3
"""Native Octop Harness SDK qualification inside an isolated, pinned image.

This deliberately does not qualify the Octop service or external ACP delegates.
Only synthetic files are mounted; keys are read at runtime from readonly mounts.
"""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import secrets
import time
import traceback


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str)+'\n')
    path.chmod(0o600)


def read_secret(path):
    if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
        raise ValueError('secret must be a private regular file')
    return path.read_text().strip()


def exact_approval(event, expected_content, output_exists):
    actions = event.get('request', {}).get('action_requests', [])
    return (not output_exists and len(actions) == 1 and
            actions[0].get('name') == 'write_file' and
            actions[0].get('args') == {'file_path':'/output.txt', 'content':expected_content})


def history_checks(messages, case, model):
    ai = [m for m in messages if m.get('type') == 'ai']
    actual = [m.get('response_metadata', {}).get('model_name') or m.get('response_metadata', {}).get('model') for m in ai]
    tools = [m for m in messages if m.get('type') == 'tool']
    writes = [m for m in tools if m.get('name') == 'write_file']
    checks = {'served_model_matches': bool(actual) and all(m == model for m in actual)}
    if case != 'cancel':
        checks['assistant_stopped'] = bool(ai) and ai[-1].get('response_metadata', {}).get('finish_reason') == 'stop'
        checks['native_read_succeeded'] = any(m.get('name') == 'read_file' and m.get('status') == 'success' for m in tools)
        checks['native_write_result'] = (len(writes) == 1 and
            ((writes[0].get('status') == 'success') if case == 'allow' else
             (writes[0].get('status') == 'error' and 'reject' in str(writes[0].get('content', '')).lower())))
    return checks


def record_checkpoint_reopen(report, before, after):
    matches = before == after
    report['checkpoint_reopen_history_count'] = len(after)
    report['checkpoint_reopen_matches'] = matches
    report['qualified'] = report.get('qualified', False) and matches


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--key', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--case', choices=['allow', 'deny', 'cancel'], required=True)
    args = parser.parse_args()
    args.output.mkdir(mode=0o700, parents=True, exist_ok=False)
    report = dict(scope='octop-harness-sdk-native', case=args.case, qualified=False, model_calls=0,
                  service_qualified=False, acp_qualified=False, production_isolation_qualified=False)
    agent = factory = None
    try:
        import httpx
        from langchain.agents.middleware import AgentMiddleware
        from langchain_core.tools import tool
        from octop_harness import HarnessAgent, HarnessAgentConfig, ProviderConfig, ModelConfig, ChatRequest
        from octop_harness.llm.factory import ChatModelFactory
        cfg = json.loads(args.config.read_text())
        if cfg['model'] not in {'qwen3.8-flash', 'deepseek-v4.1-flash'}:
            raise ValueError('unapproved model')
        from urllib.parse import urlsplit
        endpoint = urlsplit(cfg['base_url'])
        if endpoint.scheme != 'https' or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment:
            raise ValueError('invalid model endpoint')
        target = cfg['base_url'].rstrip('/') + '/chat/completions'
        auth_failed = False
        sends = []
        send = httpx.AsyncClient.send
        async def audited_send(client, request, **kwargs):
            nonlocal auth_failed
            if auth_failed or str(request.url) != target:
                raise RuntimeError('qualification transport target/auth gate')
            body = json.loads(request.content)
            if body.get('model') != cfg['model'] or len(sends) >= 4:
                raise RuntimeError('qualification transport model/call budget')
            request.extensions['timeout'] = dict(connect=30, read=45, write=30, pool=30)
            record = dict(model=body['model'], target_sha256=hashlib.sha256(target.encode()).hexdigest(), status=None)
            sends.append(record)
            kwargs['follow_redirects'] = False
            response = await send(client, request, **kwargs)
            record['status'] = response.status_code
            if response.status_code in (401,403):
                auth_failed = True
            if response.is_redirect:
                await response.aclose()
                raise RuntimeError('qualification redirect refused')
            return response
        httpx.AsyncClient.send = audited_send
        class NoRetryFactory(ChatModelFactory):
            def get_chat_model(self, model_ref, *, max_retries=None):
                return super().get_chat_model(model_ref, max_retries=0)
        workspace = args.output/'workspace'
        workspace.mkdir(mode=0o700)
        receipt = secrets.token_hex(16)
        (workspace/'input.txt').write_text(receipt)
        started, ended = asyncio.Event(), asyncio.Event()
        @tool
        async def qualification_wait() -> str:
            """Wait for the qualification operator to cancel this synthetic task."""
            started.set()
            try:
                await asyncio.sleep(90)
                (workspace/'natural-completion').write_text('finished')
                return 'wait completed'
            finally:
                ended.set()
        allowed = {'read_file','write_file'} if args.case != 'cancel' else {'qualification_wait'}
        actions = []
        class Boundaries(AgentMiddleware):
            async def awrap_model_call(self, request, handler):
                return await handler(request.override(tools=[t for t in request.tools if getattr(t,'name',None) in allowed]))
            async def awrap_tool_call(self, request, handler):
                call = request.tool_call
                if call['name'] not in allowed:
                    raise RuntimeError('unapproved tool')
                if call['name'] != 'qualification_wait':
                    p = call['args'].get('file_path')
                    permitted = {'/input.txt','/output.txt'} if call['name']=='read_file' else {'/output.txt'}
                    if p not in permitted:
                        raise RuntimeError('unapproved file path')
                actions.append(dict(name=call['name'], id=call['id'], args=call['args']))
                return await handler(request)
        provider = ProviderConfig(id='qualified', base_url=cfg['base_url'], api_key=read_secret(args.key),
                                  models=[ModelConfig(id=cfg['model'], max_input_tokens=32768, max_output_tokens=512)])
        factory = NoRetryFactory([provider])
        config = HarnessAgentConfig(name='harniqo-qualification', providers=[provider],
            default_model='qualified/'+cfg['model'], workspace_dir=workspace,
            backend={'type':'filesystem','root_dir':str(workspace),'virtual_mode':True},
            system_prompt='Follow the operator task exactly. Use only the named tools and paths. Do not delegate.',
            memory_enabled=False, memory=(), team_enabled=False, ask_user_enabled=False,
            subagents_auto_load=False, subagents=[], web_search_tools=False, todos_enabled=False,
            model_retry_enabled=False, tool_search_mode='eager', media_offload_enabled=False,
            tools=[qualification_wait] if args.case=='cancel' else [], middleware=[Boundaries()],
            interrupt_on={'write_file':{'allowed_decisions':['approve','reject']}} if args.case != 'cancel' else None,
            language='en', session_log_enabled=True)
        agent = HarnessAgent(config, model_factory=factory, agent_id='harniqo-octop-sdk')
        thread = 'harniqo-'+args.case+'-'+secrets.token_hex(8)
        report['thread_id'] = thread
        prompt = ('Call qualification_wait exactly once now.' if args.case=='cancel' else
                  'Read /input.txt using read_file, then copy its exact full text to /output.txt with write_file. '
                  'Do not write any other file. After the write completes, reply DONE. Do not reread.')
        request = ChatRequest(messages=prompt, thread_id=thread, user='qualification-operator', skills=[],
                              recursion_limit=96, configurable={'model_settings':{'max_tokens':512,'temperature':0}})
        events = []
        async def consume(stream):
            async for event in stream:
                events.append(event)
                save(args.output/'events.json',events)
        if args.case == 'cancel':
            task = asyncio.create_task(consume(agent.stream(request)))
            await asyncio.wait_for(started.wait(),100)
            begin = time.monotonic()
            agent.cancel(thread)
            await asyncio.wait_for(task,10)
            report.update(cancel_seconds=time.monotonic()-begin, native_stream_returned=True,
                          handler_finally=ended.is_set(), natural_completion=(workspace/'natural-completion').exists())
            report['qualified'] = ended.is_set() and not report['natural_completion']
        else:
            await asyncio.wait_for(consume(agent.stream(request)),120)
            hitl = [e for e in events if isinstance(e,dict) and e.get('type')=='hitl_required']
            if len(hitl)!=1:
                raise RuntimeError('expected one native approval')
            save(args.output/'approval.json',hitl[0])
            # Bind decision to the exact pending native write and expected file/content.
            if not exact_approval(hitl[0], receipt, (workspace/'output.txt').exists()):
                raise RuntimeError('approval content or pre-side-effect mismatch')
            decision = {'type':'approve'} if args.case=='allow' else {'type':'reject','message':'Denied by qualification operator'}
            await asyncio.wait_for(consume(agent.resume_hitl(thread,[decision])),120)
            exists = (workspace/'output.txt').exists()
            report['approval_decision'] = decision['type']
            report['output_exists'] = exists
            if args.case=='allow':
                data = (workspace/'output.txt').read_bytes()
                report['artifact_sha256'] = hashlib.sha256(data).hexdigest()
                report['qualified'] = data == receipt.encode()
            else:
                report['qualified'] = not exists
        history = await agent.aget_history(thread,limit=100)
        serialized_history = [m.model_dump(mode='json') for m in history]
        save(args.output/'history.json', serialized_history)
        report['history_checks'] = history_checks(serialized_history, args.case, cfg['model'])
        report['event_error_count'] = sum(isinstance(e,dict) and e.get('type') == 'error' for e in events)
        report['qualified'] = report['qualified'] and all(report['history_checks'].values()) and not report['event_error_count']
        report.update(history_count=len(history), tool_actions=actions,
                      model_calls=len(sends), transport=sends,
                      usage=[getattr(m,'usage_metadata',None) for m in history if getattr(m,'usage_metadata',None)])
        report['qualified'] = report['qualified'] and len(history)>0 and bool(sends) and all(r['status']==200 for r in sends)
        await agent.aclose()
        agent = None
        # Reopen native checkpoint state without a further model task.
        reopened = HarnessAgent(config, model_factory=factory, agent_id='harniqo-octop-sdk')
        restored = await reopened.aget_history(thread,limit=100)
        record_checkpoint_reopen(report, serialized_history, [m.model_dump(mode='json') for m in restored])
        await reopened.aclose()
    except Exception as error:
        report.update(qualified=False, error_type=type(error).__name__)
        # Private diagnostic only; never print potentially sensitive upstream exception text.
        (args.output/'error.log').write_text(traceback.format_exc())
        (args.output/'error.log').chmod(0o600)
    finally:
        cleanup_errors = []
        for resource in (agent, factory):
            if resource is not None:
                try:
                    await resource.aclose()
                except Exception as cleanup_error:
                    cleanup_errors.append(type(cleanup_error).__name__)
        if cleanup_errors:
            report.update(qualified=False, cleanup_errors=cleanup_errors)
        if 'sends' in locals():
            report.update(model_calls=len(sends), transport=sends)
        save(args.output/'report.json',report)
        print(json.dumps(report))
    return 0 if report['qualified'] else 1

if __name__ == '__main__':
    raise SystemExit(asyncio.run(main()))
