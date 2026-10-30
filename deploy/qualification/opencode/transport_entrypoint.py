#!/usr/bin/env python3
"""Add one local observation plugin to the immutable native image profile."""
import importlib.util
import sys

spec = importlib.util.spec_from_file_location('native_profile', '/opt/harniqo/entrypoint.py')
profile = importlib.util.module_from_spec(spec)
spec.loader.exec_module(profile)


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in {'baseline', 'components', 'offline-send', 'smoke'}:
        raise ValueError('Explicit transport qualification mode required')
    mode = sys.argv[1]
    original = profile.configuration

    def configuration(model):
        result = original(model)
        if mode != 'baseline':
            result['plugin'] = [[
                'file:///opt/harniqo/deploy/qualification/opencode/transport_plugin.mjs',
                {'auditFile': '/data/evidence/transport.jsonl', 'providerId': 'harniqo',
                 'expectedModel': model['model'], 'maxRequests': 1 if mode == 'offline-send' else 6},
            ]]
        if mode == 'components':
            result['plugin'].append([
                'file:///opt/harniqo/deploy/qualification/opencode/transport_selftest_plugin.mjs',
                {'directory': '/data/evidence'},
            ])
        return result

    profile.configuration = configuration
    profile.main()


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, UnicodeError):
        raise SystemExit('OpenCode transport qualification configuration rejected') from None
