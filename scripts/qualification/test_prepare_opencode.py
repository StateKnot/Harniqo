#!/usr/bin/env python3
"""Offline adversarial input tests; no downloads, Docker, listeners or models."""
from __future__ import annotations

import base64
import hashlib
import io
import json
from pathlib import Path
import runpy
import tarfile
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
PREP = runpy.run_path(str(ROOT / 'scripts/qualification/prepare_opencode.py'))
ENTRY = runpy.run_path(str(ROOT / 'deploy/qualification/opencode/entrypoint.py'))
PROBE = runpy.run_path(str(ROOT / 'scripts/qualification/probe_opencode.py'))
ELF = b'\x7fELF\x02\x01' + b'\0' * 12 + b'\x3e\x00'


class PublicArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.context = self.directory / 'context'
        self.context.mkdir()
        (self.context / 'upstream').mkdir()

    def archive(self, entries):
        path = self.directory / 'input.tar.gz'
        with tarfile.open(path, 'w:gz') as archive:
            for name, body, kind in entries:
                member = tarfile.TarInfo(name)
                member.type = kind
                member.size = len(body) if kind == tarfile.REGTYPE else 0
                if kind in {tarfile.SYMTYPE, tarfile.LNKTYPE}:
                    member.linkname = '../../escaped'
                archive.addfile(member, io.BytesIO(body) if member.size else None)
        return path

    def test_official_shape_extracts_only_native_binary(self):
        archive = self.archive([('opencode', ELF, tarfile.REGTYPE), ('README', b'doc', tarfile.REGTYPE)])
        inventory = PREP['extract_selected'](archive, self.context, 'opencode')
        self.assertEqual((self.context / 'opencode').read_bytes(), ELF)
        self.assertEqual(set(inventory), {'opencode'})
        self.assertFalse((self.context / 'README').exists())

    def test_traversal_absolute_links_duplicates_and_wrong_elf_fail(self):
        cases = [
            [('opencode', ELF, tarfile.REGTYPE), ('../escape', b'x', tarfile.REGTYPE)],
            [('/opencode', ELF, tarfile.REGTYPE)],
            [('opencode', b'', tarfile.SYMTYPE)],
            [('opencode', b'', tarfile.LNKTYPE)],
            [('opencode', ELF, tarfile.REGTYPE), ('opencode', ELF, tarfile.REGTYPE)],
            [('opencode', b'#!/bin/sh\necho fake', tarfile.REGTYPE)],
        ]
        for number, entries in enumerate(cases):
            with self.subTest(number=number), tempfile.TemporaryDirectory(dir=self.directory) as destination:
                context = Path(destination)
                (context / 'upstream').mkdir()
                with self.assertRaises(ValueError):
                    PREP['extract_selected'](self.archive(entries), context, 'opencode')

    def test_oversized_archive_fails_before_reading_payload(self):
        archive = self.directory / 'oversized.tar.gz'
        with tarfile.open(archive, 'w:gz') as output:
            member = tarfile.TarInfo('unselected')
            member.size = 512 * 1024**2 + 1
            # Header alone proves the expansion limit; no large fixture needed.
            output.fileobj.write(member.tobuf())
        with self.assertRaises(ValueError):
            PREP['extract_selected'](archive, self.context, 'opencode')

    def test_npm_identity_is_checked_without_install_hooks(self):
        metadata = {'name': 'opencode-linux-x64-baseline', 'version': '1.19.5', 'os': ['linux'], 'cpu': ['x64']}
        entries = [('package/bin/opencode', ELF, tarfile.REGTYPE),
                   ('package/package.json', json.dumps(metadata).encode(), tarfile.REGTYPE)]
        inventory = PREP['extract_selected'](self.archive(entries), self.context, 'opencode-npm')
        self.assertIn('opencode-npm-package.json', inventory)
        metadata['scripts'] = {'postinstall': 'untrusted'}
        with tempfile.TemporaryDirectory(dir=self.directory) as destination:
            context = Path(destination)
            (context / 'upstream').mkdir()
            entries[1] = ('package/package.json', json.dumps(metadata).encode(), tarfile.REGTYPE)
            with self.assertRaises(ValueError):
                PREP['extract_selected'](self.archive(entries), context, 'opencode-npm')

    def test_cached_input_verifies_hash_size_git_object_and_sri(self):
        cached = self.directory / 'input'
        body = b'public fixed artifact\n'
        cached.write_bytes(body)
        url = 'https://registry.npmjs.org/public-fixed-test.tgz'
        sri = 'sha512-' + base64.b64encode(hashlib.sha512(body).digest()).decode()
        blob = hashlib.sha1(b'blob ' + str(len(body)).encode() + b'\0' + body).hexdigest()
        PREP['download'](url, cached, len(body), hashlib.sha256(body).hexdigest(), blob, integrity=sri)
        for arguments in ({'expected_sha256': '0' * 64}, {'expected_blob': '0' * 40},
                          {'integrity': 'sha512-' + base64.b64encode(b'\0' * 64).decode()},
                          {'maximum': len(body) - 1}):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                PREP['download'](url, cached, len(body), **arguments)


class ModelConfigurationTests(unittest.TestCase):
    def test_explicit_model_endpoint_and_low_cost_profile(self):
        config = ENTRY['configuration']({'base_url': 'https://example.invalid/v1', 'model': 'test/model'})
        self.assertEqual(config['model'], 'harniqo/test/model')
        self.assertEqual(config['small_model'], config['model'])
        self.assertEqual(config['provider']['harniqo']['options']['apiKey'], '{env:HARNIQO_MODEL_API_KEY}')
        self.assertEqual(config['provider']['harniqo']['npm'], '@ai-sdk/openai-compatible')
        self.assertTrue(config['agent']['title']['disable'])
        self.assertEqual(config['permission']['*'], 'deny')
        self.assertEqual(config['permission']['edit'], 'ask')

    def test_unsupported_types_urls_budgets_and_fields_fail(self):
        valid = {'base_url': 'https://example.invalid/v1', 'model': 'model'}
        cases = [[], None, {'model': 'm'}, {**valid, 'base_url': 'http://example.invalid/v1'},
                 {**valid, 'base_url': 'https://key@example.invalid/v1'},
                 {**valid, 'base_url': 'https://example.invalid/v1?key=secret'},
                 {**valid, 'base_url': ['https://example.invalid/v1']},
                 {**valid, 'model': ''}, {**valid, 'model': '\nmodel'}, {**valid, 'model': 1},
                 {**valid, 'output': True}, {**valid, 'output': 4096}, {**valid, 'context': '8192'},
                 {**valid, 'interleaved': 0}, {**valid, 'interleaved': {}}, {**valid, 'apiKey': 'secret'}]
        for case in cases:
            with self.subTest(case=case), self.assertRaises(ValueError):
                ENTRY['configuration'](case)


class NativeApprovalBoundaryTests(unittest.TestCase):
    def test_only_exact_single_output_patterns_are_eligible(self):
        allowed = {'permission': 'edit', 'patterns': ['qualification-result.json'],
                   'metadata': {'filepath': '/data/workspace/qualification-result.json'}}
        self.assertTrue(PROBE['permitted_output'](allowed))
        for request in (None, {}, {**allowed, 'permission': 'bash'}, {**allowed, 'patterns': ['*']},
                        {**allowed, 'patterns': ['../qualification-result.json']},
                        {**allowed, 'patterns': ['qualification-input.json']},
                        {**allowed, 'patterns': ['qualification-result.json', '/run/secrets/model-api-key']},
                        {**allowed, 'patterns': []}):
            with self.subTest(request=request):
                self.assertFalse(PROBE['permitted_output'](request))

    def test_nongit_global_project_patterns_are_relative_to_filesystem_root(self):
        request = {'permission': 'edit', 'patterns': ['data/workspace/qualification-result.json'],
                   'metadata': {'filepath': '/data/workspace/qualification-result.json'}}
        self.assertTrue(PROBE['permitted_output'](request, '/'))
        self.assertFalse(PROBE['permitted_output'](request, '/data/workspace'))
        self.assertFalse(PROBE['permitted_output']({**request, 'patterns': ['data/workspace/*']}, '/'))
        self.assertFalse(PROBE['permitted_output']({**request, 'metadata': {'filepath': '/run/secrets/model-api-key'}}, '/'))

    def test_failed_calls_must_also_use_approved_native_tools_and_paths(self):
        good = [
            {'tool': 'read', 'state': {'status': 'completed', 'input': {'filePath': '/data/workspace/qualification-input.json'}}},
            {'tool': 'write', 'state': {'status': 'completed', 'input': {'filePath': 'qualification-result.json'}}},
            {'tool': 'read', 'state': {'status': 'completed', 'input': {'filePath': './qualification-result.json'}}},
        ]
        self.assertTrue(PROBE['exact_tool_paths'](good))
        for bad in (
            {'tool': 'read', 'state': {'status': 'error', 'input': {'filePath': '/run/secrets/model-api-key'}}},
            {'tool': 'write', 'state': {'status': 'error', 'input': {'filePath': 'qualification-input.json'}}},
            {'tool': 'bash', 'state': {'status': 'error', 'input': {'command': 'true'}}},
            {'tool': 'read', 'state': {'status': 'error', 'input': {}}},
        ):
            with self.subTest(bad=bad):
                self.assertFalse(PROBE['exact_tool_paths'](good + [bad]))

    def test_completed_history_requires_input_read_then_output_write_then_output_read(self):
        def call(tool, path):
            return {'tool': tool, 'state': {'status': 'completed', 'input': {'filePath': path}}}
        read_input = call('read', 'qualification-input.json')
        write_output = call('write', 'qualification-result.json')
        read_output = call('read', 'qualification-result.json')
        self.assertTrue(PROBE['read_write_read']([read_input, write_output, read_output]))
        self.assertFalse(PROBE['read_write_read']([read_input, read_input, write_output]))
        self.assertFalse(PROBE['read_write_read']([read_output, read_input, write_output]))

    def test_assistant_terminal_uses_completed_and_successful_finish(self):
        info = {'finish': 'stop', 'time': {'created': 1, 'completed': 2}}
        self.assertTrue(PROBE['assistant_completed']([{'info': info}]))
        self.assertFalse(PROBE['assistant_completed']([{'info': {**info, 'finish': 'tool-calls'}}]))
        self.assertFalse(PROBE['assistant_completed']([{'info': {**info, 'time': {'created': 1, 'end': 2}}}]))
        self.assertFalse(PROBE['assistant_completed']([{'info': {**info, 'error': {'name': 'NativeError'}}}]))


if __name__ == '__main__':
    unittest.main()
