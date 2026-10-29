#!/usr/bin/env python3
"""Start the unmodified pinned OpenCode server with a trusted qualification profile.

Importing this file does not read secrets, mutate state, or start a process.
Secrets enter only the native process environment; this is not a tenant sandbox.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import stat
import urllib.parse


def private_file(path, maximum=16384):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    with os.fdopen(descriptor, 'rb') as source:
        metadata = os.fstat(source.fileno())
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077
                or metadata.st_uid not in {0, os.geteuid()} or metadata.st_size > maximum):
            raise ValueError('Private input must be a private regular file owned by runtime UID or root')
        if not os.fstatvfs(source.fileno()).f_flag & os.ST_RDONLY:
            raise ValueError('Private inputs require read-only mounts')
        raw = source.read(maximum + 1)
        if len(raw) > maximum:
            raise ValueError('Private input too large')
        return raw


def configuration(model):
    if not isinstance(model, dict) or not {'base_url', 'model'} <= set(model) <= {
            'base_url', 'model', 'context', 'output', 'interleaved'}:
        raise ValueError('Model configuration requires base_url and model; only context/output/interleaved are optional')
    base_url, model_id = model['base_url'], model['model']
    if not isinstance(base_url, str) or not isinstance(model_id, str):
        raise ValueError('Invalid model configuration types')
    url = urllib.parse.urlsplit(base_url)
    if (url.scheme != 'https' or not url.hostname or url.username or url.password or url.query or url.fragment
            or len(base_url) > 2048 or re.search(r'[\s\x00-\x1f\x7f]', base_url)
            or not 1 <= len(model_id) <= 256 or re.search(r'[\s\x00-\x1f\x7f]', model_id)):
        raise ValueError('Model endpoint must be HTTPS without embedded credentials/query; model ID must be nonempty')
    context, output = model.get('context', 8192), model.get('output', 1024)
    if (type(context) is not int or not 2048 <= context <= 2000000
            or type(output) is not int or not 128 <= output <= 1024 or output >= context):
        raise ValueError('Invalid context/output qualification limits')
    interleaved = model.get('interleaved', False)
    if interleaved is not False and interleaved != 'reasoning_content':
        raise ValueError('Only explicit reasoning_content interleaving is supported by this profile')
    selected = 'harniqo/' + model_id
    return {
        'model': selected, 'small_model': selected, 'enabled_providers': ['harniqo'],
        'share': 'disabled', 'autoupdate': False, 'snapshot': False,
        'plugin': [], 'mcp': {}, 'lsp': False, 'formatter': False,
        'instructions': [], 'compaction': {'auto': False, 'prune': False},
        'agent': {'title': {'disable': True}, 'summary': {'disable': True},
                  'build': {'steps': 4}},
        # Native approval is exercised with write/edit; read needs no prompt.
        # Shell, web, delegation, skill and external-directory access stay denied.
        'permission': {'*': 'deny', 'read': 'allow', 'write': 'ask', 'edit': 'ask'},
        'provider': {'harniqo': {
            'npm': '@ai-sdk/openai-compatible', 'name': 'Harniqo qualification',
            'options': {'baseURL': base_url.rstrip('/'), 'apiKey': '{env:HARNIQO_MODEL_API_KEY}',
                        'timeout': 60000},
            'models': {model_id: {'name': 'Approved qualification model',
                                   'tool_call': True, 'reasoning': bool(interleaved),
                                   'interleaved': {'field': interleaved} if interleaved else False,
                                   'limit': {'context': context, 'output': output}}},
        }},
    }


def main():
    os.umask(0o077)
    for directory in (Path('/data/opencode'), Path('/data/workspace')):
        metadata = directory.lstat()
        if (not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid()
                or metadata.st_mode & 0o077):
            raise ValueError('Runtime home/workspace must be private directories owned by runtime UID')
    for forbidden in (Path('/data/opencode/.opencode'), Path('/data/opencode/data/opencode/auth.json')):
        if forbidden.exists() or forbidden.is_symlink():
            raise ValueError('Qualification home contains external configuration or saved credentials')
    config = configuration(json.loads(private_file('/run/config/model.json')))
    key = private_file('/run/secrets/model-api-key').decode().strip()
    password = private_file('/run/secrets/api-password').decode().strip()
    if not key or len(key) > 8192 or re.search(r'[\s\x00-\x1f\x7f]', key):
        raise ValueError('Invalid model API key')
    if not re.fullmatch(r'[A-Za-z0-9_-]{32,256}', password):
        raise ValueError('Server password must contain 32-256 base64url characters')
    # Config paths are read-only; only a key reference is serialized in memory.
    # Do not print config or errors containing secret payloads.
    environment = {name: os.environ[name] for name in ('LANG', 'LC_ALL', 'TZ') if name in os.environ}
    environment.update({
        'PATH': '/usr/local/bin:/usr/bin:/bin', 'HOME': '/data/opencode',
        'XDG_DATA_HOME': '/data/opencode/data', 'XDG_STATE_HOME': '/data/opencode/state',
        'XDG_CACHE_HOME': '/data/opencode/cache', 'XDG_CONFIG_HOME': '/opt/harniqo/config',
        'OPENCODE_DISABLE_AUTOUPDATE': '1', 'OPENCODE_DISABLE_MODELS_FETCH': '1',
        'OPENCODE_DISABLE_DEFAULT_PLUGINS': 'true', 'OPENCODE_DISABLE_EXTERNAL_SKILLS': 'true',
        'OPENCODE_DISABLE_LSP_DOWNLOAD': 'true', 'OPENCODE_DISABLE_CLAUDE_CODE': 'true',
        'OPENCODE_DISABLE_PROJECT_CONFIG': '1', 'OPENCODE_DISABLE_FFF': '1',
        'OPENCODE_DISABLE_EMBEDDED_WEB_UI': 'true', 'OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX': '1024',
    })
    environment['HARNIQO_MODEL_API_KEY'] = key
    environment['OPENCODE_SERVER_PASSWORD'] = password
    environment['OPENCODE_SERVER_USERNAME'] = 'opencode'
    environment['OPENCODE_CONFIG_CONTENT'] = json.dumps(config, separators=(',', ':'))
    os.chdir('/data/workspace')
    os.execve('/usr/local/bin/opencode', ['opencode', 'serve', '--hostname', '0.0.0.0', '--port', '4096'], environment)


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, UnicodeError):
        raise SystemExit('OpenCode qualification configuration rejected') from None
