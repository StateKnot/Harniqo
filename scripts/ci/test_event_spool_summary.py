"""Verify CI fails closed and retains sanitized evidence without Node or network."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).with_name('event_spool_summary.py')
SPEC = importlib.util.spec_from_file_location('event_spool_summary', SCRIPT)
summary = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(summary)
COMMIT = 'a' * 40
SUCCESS = dict.fromkeys(('install', 'typecheck', 'tests'), 'success')


class SummaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.folder = self.root / 'ci-evidence'
        self.package = self.root / summary.PACKAGE
        self.folder.mkdir()
        self.package.mkdir(parents=True)
        self.manifest = self.package / 'package.json'
        self.lock = self.package / 'package-lock.json'
        self.runtime = self.folder / 'event-spool-runtime.json'
        self.marker = self.folder / 'event-spool-typecheck.json'
        self.junit = self.folder / 'event-spool.junit.xml'
        self.manifest.write_text(json.dumps({'engines': {'node': '24.22.1'},
                                             'devDependencies': {'typescript': '6.0.3'}}))
        self.lock.write_text('{"lockfileVersion":3}\n')
        for name in ('tsconfig.json', 'spool.ts', 'types.ts', 'spool.test.ts'):
            (self.package / name).write_text('{}\n' if name.endswith('.json') else '// synthetic source\n')
        # These installed files must never enter the reviewed source set.
        (self.package / 'node_modules/typescript').mkdir(parents=True)
        (self.package / 'node_modules/typescript/package.json').write_text('{"version":"6.0.3"}')
        self.runtime_data = {
            'node': summary.NODE, 'sqlite_version': summary.SQLITE,
            'sqlite_source_id': summary.SQLITE_SOURCE, 'typescript': summary.COMPILER,
            'source_commit': COMMIT, 'source_sha256': summary.source_hashes(self.root),
            'capture_issues': [],
            'sqlite_compile_options': ['DEFAULT_SYNCHRONOUS=2', 'THREADSAFE=1'],
        }
        self.runtime_data['sqlite_compile_options_sha256'] = self.compile_hash(self.runtime_data['sqlite_compile_options'])
        self.runtime.write_text(json.dumps(self.runtime_data))
        self.marker_data = {'node': summary.NODE, 'typescript': summary.COMPILER,
                            'source_commit': COMMIT, 'passed': True}
        self.marker.write_text(json.dumps(self.marker_data))
        self.write_junit(35)

    def write_junit(self, count, result=''):
        self.junit.write_text('<testsuites><testsuite>' + ''.join(
            '<testcase name="case-%d">%s</testcase>' % (number, result if number == 0 else '')
            for number in range(count)) + '</testsuite></testsuites>')

    def report(self, commit=COMMIT, outcomes=None):
        return summary.summarize(self.root, commit, SUCCESS if outcomes is None else outcomes)

    @staticmethod
    def compile_hash(options):
        return hashlib.sha256(json.dumps(options, separators=(',', ':')).encode('ascii')).hexdigest()

    def test_valid_evidence_binds_runtime_source_and_artifact_hashes(self):
        report = self.report()
        self.assertTrue(report['passed'])
        self.assertTrue(report['typecheck_passed'])
        self.assertEqual(report['tests'], 35)
        self.assertEqual(report['evidence_issues'], [])
        self.assertEqual(report['source_commit'], COMMIT)
        self.assertEqual(report['node'], summary.NODE)
        self.assertEqual(report['sqlite_version'], summary.SQLITE)
        self.assertEqual(report['sqlite_source_id'], summary.SQLITE_SOURCE)
        self.assertEqual(report['sqlite_compile_options'], self.runtime_data['sqlite_compile_options'])
        self.assertEqual(report['sqlite_compile_options_sha256'], self.runtime_data['sqlite_compile_options_sha256'])
        self.assertEqual(report['junit_sha256'], hashlib.sha256(self.junit.read_bytes()).hexdigest())
        self.assertEqual(report['package_lock_sha256'], hashlib.sha256(self.lock.read_bytes()).hexdigest())
        self.assertEqual(report['runtime_sha256'], hashlib.sha256(self.runtime.read_bytes()).hexdigest())
        self.assertIn('apps/runner/event-spool/spool.test.ts', report['source_sha256'])
        self.assertFalse(any('node_modules' in path for path in report['source_sha256']))
        self.assertEqual(report['real_model_calls'], 0)
        for key, value in report.items():
            if key.endswith('_verified'):
                self.assertIs(value, False, key)

    def test_missing_or_unsuccessful_steps_cannot_pass(self):
        for step in SUCCESS:
            for outcome in ('failure', 'cancelled', 'skipped', None, 'unknown'):
                with self.subTest(step=step, outcome=outcome):
                    report = self.report(outcomes=dict(SUCCESS, **{step: outcome}))
                    self.assertFalse(report['passed'])
                    self.assertIn('workflow_step_not_successful', report['evidence_issues'])
        self.assertFalse(self.report(outcomes={})['passed'])
        self.assertFalse(self.report(outcomes=dict(SUCCESS, unexpected='success'))['passed'])

    def test_missing_and_malformed_evidence_cannot_pass(self):
        for path in (self.manifest, self.lock, self.runtime, self.marker, self.junit):
            original = path.read_bytes()
            with self.subTest(file=path.name, kind='missing'):
                path.unlink()
                self.assertFalse(self.report()['passed'])
                path.write_bytes(original)
            for malformed in (b'not a document', b'[]', b'{'):
                with self.subTest(file=path.name, kind=malformed):
                    path.write_bytes(malformed)
                    self.assertFalse(self.report()['passed'])
                    path.write_bytes(original)

    def test_wrong_runtime_compiler_or_source_commit_cannot_pass(self):
        for key, value in (('node', 'v26.4.0'), ('sqlite_version', '3.51.2'),
                           ('sqlite_source_id', 'private-hostname.invalid'),
                           ('typescript', '6.0.2'), ('source_commit', 'b' * 40),
                           ('source_commit', None), ('capture_issues', ['runtime_capture_failed'])):
            with self.subTest(key=key, value=value):
                self.runtime.write_text(json.dumps(dict(self.runtime_data, **{key: value})))
                report = self.report()
                self.assertFalse(report['passed'])
                self.assertIn('runtime_not_verified', report['evidence_issues'])
                self.assertNotIn('private-hostname.invalid', json.dumps(report))
        for commit in (None, '', 'private-hostname.invalid', 'g' * 40):
            with self.subTest(commit=commit):
                self.assertFalse(self.report(commit=commit)['passed'])
        invalid_options = ([], ['THREADSAFE=1', 'DEFAULT_SYNCHRONOUS=2'], [None],
                           ['THREADSAFE=1'] * 257, ['X' * 257], ['private payload hostname.invalid'])
        for options in invalid_options:
            with self.subTest(options=options):
                self.runtime.write_text(json.dumps(dict(self.runtime_data, sqlite_compile_options=options,
                                                       sqlite_compile_options_sha256=self.compile_hash(options))))
                report = self.report()
                self.assertFalse(report['passed'])
                self.assertIn('compile_options_not_verified', report['evidence_issues'])
                self.assertNotIn('hostname.invalid', json.dumps(report))
        for option in ('NO_SYNC', 'NO_SYNC=0', 'OMIT_WAL', 'OMIT_PAGER_PRAGMAS',
                       'OMIT_COMPILEOPTION_DIAGS', 'THREADSAFE=0'):
            with self.subTest(unsafe=option):
                options = [option]
                self.runtime.write_text(json.dumps(dict(self.runtime_data, sqlite_compile_options=options,
                                                       sqlite_compile_options_sha256=self.compile_hash(options))))
                report = self.report()
                self.assertFalse(report['passed'])
                self.assertIn('unsafe_sqlite_build', report['evidence_issues'])
        self.runtime.write_text(json.dumps(dict(self.runtime_data, sqlite_compile_options_sha256='0' * 64)))
        self.assertIn('compile_options_not_verified', self.report()['evidence_issues'])

    def test_typecheck_marker_must_be_successful_same_source_and_compiler(self):
        for key, value in (('node', 'v24.22.0'), ('typescript', '6.0.2'),
                           ('source_commit', 'b' * 40), ('passed', False), ('passed', 'true')):
            with self.subTest(key=key, value=value):
                self.marker.write_text(json.dumps(dict(self.marker_data, **{key: value})))
                report = self.report()
                self.assertFalse(report['passed'])
                self.assertFalse(report['typecheck_passed'])

    def test_source_change_new_file_or_stale_lock_cannot_pass(self):
        for path in (self.package / 'spool.ts', self.package / 'spool.test.ts', self.lock):
            original = path.read_bytes()
            with self.subTest(file=path.name):
                path.write_bytes(original + b'\n')
                report = self.report()
                self.assertFalse(report['passed'])
                self.assertFalse(report['typecheck_passed'])
                self.assertIn('source_hashes_not_current', report['evidence_issues'])
                path.write_bytes(original)
        added = self.package / 'additional.ts'
        added.write_text('// additional reviewed source\n')
        self.assertFalse(self.report()['passed'])

    def test_empty_or_inconsistent_junit_cannot_pass(self):
        self.write_junit(0)
        report = self.report()
        self.assertFalse(report['passed'])
        self.assertIn('no_test_cases', report['evidence_issues'])
        for count in (1, 32, 34):
            with self.subTest(count=count):
                self.write_junit(count)
                report = self.report()
                self.assertTrue(report['passed'])
                self.assertEqual(report['tests'], count)
        self.write_junit(35)
        self.junit.write_text(self.junit.read_text().replace('<testsuite>', '<testsuite tests="36">'))
        report = self.report()
        self.assertFalse(report['passed'])
        self.assertIn('test_evidence_unavailable', report['evidence_issues'])
        for document in ('<unrelated/>', '<testsuites>' + '<testcase name="case"/>' * 35
                         + '</testsuites>'):
            with self.subTest(document=document[:30]):
                self.junit.write_text(document)
                self.assertFalse(self.report()['passed'])

    def test_failures_errors_skips_and_private_payload_are_never_success(self):
        for tag, field in (('failure', 'failures'), ('error', 'errors'), ('skipped', 'skipped')):
            with self.subTest(tag=tag):
                self.write_junit(35, '<%s message="private payload from a fixture"/>' % tag)
                report = self.report()
                self.assertFalse(report['passed'])
                self.assertEqual(report[field], 1)
                self.assertNotIn('private payload', json.dumps(report))

    def test_oversized_or_entity_junit_retains_only_a_diagnostic(self):
        for raw in (b'x' * (summary.JUNIT_LIMIT + 1),
                    b'<!DOCTYPE testsuites [<!ENTITY secret "private payload">]><testsuites/>'):
            with self.subTest(kind=raw[:20]):
                self.junit.write_bytes(raw)
                report = self.report()
                self.assertFalse(report['passed'])
                self.assertIn('test_evidence_unavailable', report['evidence_issues'])
                self.assertNotIn('private payload', json.dumps(report))

    def test_symlink_source_is_not_hashed_or_treated_as_current(self):
        source = self.package / 'spool.ts'
        source.unlink()
        source.symlink_to(self.package / 'types.ts')
        report = self.report()
        self.assertFalse(report['passed'])
        self.assertIn('source_evidence_unavailable', report['evidence_issues'])

    def test_main_always_writes_failure_report_without_node_or_dependencies(self):
        script = self.root / 'scripts/ci/event_spool_summary.py'
        script.parent.mkdir(parents=True)
        script.write_bytes(SCRIPT.read_bytes())
        for path in (self.runtime, self.marker, self.junit):
            path.unlink()
        environment = {'PATH': '', 'GITHUB_SHA': COMMIT, 'INSTALL_OUTCOME': 'failure',
                       'TYPECHECK_OUTCOME': 'skipped', 'TESTS_OUTCOME': 'skipped'}
        process = subprocess.run([sys.executable, str(script)], env=environment,
                                 capture_output=True, text=True, timeout=10)
        self.assertEqual(process.returncode, 1, process.stderr)
        raw = (self.folder / 'event-spool.json').read_bytes()
        self.assertLess(len(raw), summary.JSON_LIMIT)
        report = json.loads(raw)
        self.assertFalse(report['passed'])
        self.assertIn('runtime_evidence_unavailable', report['evidence_issues'])
        self.assertIn('workflow_step_not_successful', report['evidence_issues'])
        self.assertEqual(report, json.loads(process.stdout))

    def test_runtime_capture_failure_retains_a_bounded_marker(self):
        script = self.root / 'scripts/ci/event_spool_summary.py'
        script.parent.mkdir(parents=True)
        script.write_bytes(SCRIPT.read_bytes())
        process = subprocess.run([sys.executable, str(script), '--capture-runtime'],
                                 env={'PATH': '', 'GITHUB_SHA': COMMIT},
                                 capture_output=True, text=True, timeout=10)
        self.assertEqual(process.returncode, 1, process.stderr)
        marker = json.loads(self.runtime.read_text())
        self.assertEqual(marker['capture_issues'], ['runtime_capture_failed'])
        self.assertTrue(marker['source_sha256'])
        self.assertEqual(process.stdout, '')


if __name__ == '__main__':
    unittest.main()
