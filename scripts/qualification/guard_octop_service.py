#!/usr/bin/env python3
"""Qualification-only send boundary around the unmodified service entry point.

It rejects extra/background requests; it never substitutes model responses.
Run with one worker so the budget is process-wide and persisted before send.
"""
import hashlib
import importlib.metadata
import json
from pathlib import Path
import sys
from urllib.parse import urlsplit


class Budget:
    def __init__(self, config, marker, output):
        endpoint = urlsplit(config['base_url'])
        if (config['model'] != 'qwen3.8-flash' or endpoint.scheme != 'https' or
            endpoint.username or endpoint.password or endpoint.query or endpoint.fragment):
            raise ValueError('invalid qualification endpoint')
        self.target = config['base_url'].rstrip('/')+'/chat/completions'
        self.model = config['model']
        self.marker = marker
        self.output = output
        self.calls = []
        self.auth_failed = False
        self.save()

    def save(self):
        temporary = self.output.with_suffix('.pending')
        temporary.write_text(json.dumps({'calls': self.calls, 'auth_failed': self.auth_failed}))
        temporary.chmod(0o600)
        temporary.replace(self.output)

    def admit(self, url, content):
        body = json.loads(content)
        digest = hashlib.sha256(content).hexdigest()
        if (self.auth_failed or url != self.target or body.get('model') != self.model or
            len(self.calls) >= 4 or self.marker not in json.dumps(body.get('messages', [])) or
            any(c['request_sha256'] == digest for c in self.calls)):
            raise RuntimeError('qualification request boundary rejected')
        record = {'model': self.model, 'target_sha256': hashlib.sha256(url.encode()).hexdigest(),
                  'request_sha256': digest, 'status': None}
        self.calls.append(record)
        self.save()
        return record

    def completed(self, record, status):
        record['status'] = status
        self.auth_failed = self.auth_failed or status in (401, 403)
        self.save()


def main():
    import httpx
    from octop_harness.llm.factory import ChatModelFactory
    cfg = json.loads(Path('/run/model.json').read_text())
    budget = Budget(cfg, Path('/evidence/challenge.txt').read_text(), Path('/evidence/model-calls.json'))
    send = httpx.AsyncClient.send
    async def audited(client, request, **kwargs):
        record = budget.admit(str(request.url), request.content)
        request.extensions['timeout'] = dict(connect=30, read=45, write=30, pool=30)
        kwargs['follow_redirects'] = False
        response = await send(client, request, **kwargs)
        budget.completed(record, response.status_code)
        if response.is_redirect:
            await response.aclose()
            raise RuntimeError('qualification redirect refused')
        return response
    httpx.AsyncClient.send = audited
    def reject_sync(*args, **kwargs):
        raise RuntimeError('qualification synchronous network refused')
    httpx.Client.send = reject_sync
    get_model = ChatModelFactory.get_chat_model
    def no_retry(factory, model_ref, *, max_retries=None):
        return get_model(factory, model_ref, max_retries=0)
    ChatModelFactory.get_chat_model = no_retry
    sys.argv = ['octop', 'run', '--host', '127.0.0.1', '--port', '10014', '--workers', '1']
    entry = next(e for e in importlib.metadata.distribution('octop').entry_points if e.name == 'octop')
    entry.load()()


if __name__ == '__main__':
    main()
