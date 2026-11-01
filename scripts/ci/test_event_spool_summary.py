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
from unittest.mock import patch


SCRIPT = Path(__file__).with_name('event_spool_summary.py')
SPEC = importlib.util.spec_from_file_location('event_spool_summary', SCRIPT)
summary = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(summary)
COMMIT = 'a' * 40
SUCCESS = dict.fromkeys(('install', 'typecheck', 'tests'), 'success')

# Actual official Node24.22.1 Linux binary, retained in
# docs/operations/event-spool-native-2026-11-01.json. This copied build
# observation tests the evidence parser; it does not execute Node.
OFFICIAL_COMPILE_OPTIONS = [
    'ATOMIC_INTRINSICS=1',
    'COMPILER=gcc-12.2.1 20221121 (Red Hat 12.2.1-7)',
    'DEFAULT_AUTOVACUUM',
    'DEFAULT_CACHE_SIZE=-2000',
    'DEFAULT_FILE_FORMAT=4',
    'DEFAULT_JOURNAL_SIZE_LIMIT=-1',
    'DEFAULT_MEMSTATUS=0',
    'DEFAULT_MMAP_SIZE=0',
    'DEFAULT_PAGE_SIZE=4096',
    'DEFAULT_PCACHE_INITSZ=20',
    'DEFAULT_RECURSIVE_TRIGGERS',
    'DEFAULT_SECTOR_SIZE=4096',
    'DEFAULT_SYNCHRONOUS=2',
    'DEFAULT_WAL_AUTOCHECKPOINT=1000',
    'DEFAULT_WAL_SYNCHRONOUS=2',
    'DEFAULT_WORKER_THREADS=0',
    'DIRECT_OVERFLOW_READ',
    'ENABLE_COLUMN_METADATA',
    'ENABLE_DBSTAT_VTAB',
    'ENABLE_FTS3',
    'ENABLE_FTS3_PARENTHESIS',
    'ENABLE_FTS5',
    'ENABLE_GEOPOLY',
    'ENABLE_MATH_FUNCTIONS',
    'ENABLE_PERCENTILE',
    'ENABLE_PREUPDATE_HOOK',
    'ENABLE_RBU',
    'ENABLE_RTREE',
    'ENABLE_SESSION',
    'MALLOC_SOFT_LIMIT=1024',
    'MAX_ATTACHED=10',
    'MAX_COLUMN=2000',
    'MAX_COMPOUND_SELECT=500',
    'MAX_DEFAULT_PAGE_SIZE=8192',
    'MAX_EXPR_DEPTH=1000',
    'MAX_FUNCTION_ARG=1000',
    'MAX_LENGTH=1000000000',
    'MAX_LIKE_PATTERN_LENGTH=50000',
    'MAX_MMAP_SIZE=0x7fff0000',
    'MAX_PAGE_COUNT=0xfffffffe',
    'MAX_PAGE_SIZE=65536',
    'MAX_SQL_LENGTH=1000000000',
    'MAX_TRIGGER_DEPTH=1000',
    'MAX_VARIABLE_NUMBER=32766',
    'MAX_VDBE_OP=250000000',
    'MAX_WORKER_THREADS=8',
    'MUTEX_PTHREADS',
    'SYSTEM_MALLOC',
    'TEMP_STORE=1',
    'THREADSAFE=1',
]
OFFICIAL_COMPILE_OPTIONS_SHA256 = '63d78bf01e3df9f2b3a898217437af8ba2654095924631eff5de238aeab599e0'


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

    def test_actual_official_compile_options_pass_capture_and_summary(self):
        options, digest = summary.checked_compile_options(
            OFFICIAL_COMPILE_OPTIONS, OFFICIAL_COMPILE_OPTIONS_SHA256)
        self.assertEqual(len(options), 50)
        self.assertIn('COMPILER=gcc-12.2.1 20221121 (Red Hat 12.2.1-7)', options)
        self.assertIn('MAX_MMAP_SIZE=0x7fff0000', options)
        self.assertEqual(digest, self.compile_hash(options))
        actual = dict(self.runtime_data, sqlite_compile_options=options,
                      sqlite_compile_options_sha256=digest)
        process = subprocess.CompletedProcess(['node'], 0, stdout=json.dumps(actual), stderr='')
        with patch.object(summary.subprocess, 'run', return_value=process):
            captured = summary.capture_runtime(self.root)
        self.assertEqual(captured['capture_issues'], [])
        self.assertEqual(captured['sqlite_compile_options'], options)
        self.assertEqual(captured['sqlite_compile_options_sha256'], digest)
        self.runtime.write_text(json.dumps(captured))
        report = self.report()
        self.assertTrue(report['passed'], report['evidence_issues'])
        self.assertEqual(report['sqlite_compile_options'], options)
        self.assertEqual(report['sqlite_compile_options_sha256'], digest)

    def test_compile_option_values_reject_controls_non_ascii_and_invalid_names(self):
        for option in ('COMPILER=gcc\nforged', 'COMPILER=gcc\rforged', 'COMPILER=gcc\tforged',
                       'COMPILER=gcc\x00forged', 'COMPILER=gcc\x1bforged', 'COMPILER=gcc\x7fforged',
                       'COMPILER=gcc中文', 'COMPILER=gccé', 'compiler=gcc', 'COMPILER gcc', 'COMPILER=',
                       'COMPILER=' + 'x' * 248):
            with self.subTest(option=repr(option)):
                self.runtime.write_text(json.dumps(dict(self.runtime_data, sqlite_compile_options=[option],
                                                       sqlite_compile_options_sha256=self.compile_hash([option]))))
                report = self.report()
                self.assertFalse(report['passed'])
                self.assertIn('compile_options_not_verified', report['evidence_issues'])
                self.assertEqual(report['sqlite_compile_options'], [])

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
        for document in ('<unrelated/>', '<testsuites><wrapper><testcase name="case"/>'
                         + '</wrapper></testsuites>'):
            with self.subTest(document=document[:30]):
                self.junit.write_text(document)
                self.assertFalse(self.report()['passed'])

    def test_actual_node_flat_junit_shape_counts_all_34_cases(self):
        # The designated server's Node24.22.1 JUnit producer emits direct
        # testcase children, without any testsuite wrapper, for these 34 tests.
        # This self-contained shape has no original timestamps or host paths.
        self.junit.write_text('<?xml version="1.0" encoding="utf-8"?>\n<testsuites>\n'
                              + ''.join('<testcase name="case-%d" time="0.01" classname="test" '
                                        'file="spool.test.ts"/>\n' % number for number in range(34))
                              + '</testsuites>\n')
        report = self.report()
        self.assertTrue(report['passed'], report['evidence_issues'])
        self.assertEqual(report['tests'], 34)
        self.assertEqual(report['junit_sha256'], hashlib.sha256(self.junit.read_bytes()).hexdigest())

    def test_nested_junit_counts_and_case_placement_are_verified(self):
        document = ('<testsuites tests="2" failures="0" errors="0" skipped="0">'
                    '<testcase name="direct"/>'
                    '<testsuite tests="1"><testsuite tests="1">'
                    '<testcase name="nested"/></testsuite></testsuite></testsuites>')
        self.junit.write_text(document)
        self.assertTrue(self.report()['passed'])
        self.assertEqual(self.report()['tests'], 2)
        for invalid in (document.replace('tests="2"', 'tests="3"'),
                        document.replace('tests="1"', 'tests="0"', 1),
                        document.replace('failures="0"', 'failures="1"'),
                        '<testsuites><failure/></testsuites>',
                        '<testsuites><testcase name="outer"><testcase name="hidden"/>'
                        '</testcase></testsuites>',
                        '<testsuites><testcase name="case"><system-out><testcase name="hidden"/>'
                        '</system-out></testcase></testsuites>',
                        '<testsuites><testcase/></testsuites>'):
            with self.subTest(document=invalid):
                self.junit.write_text(invalid)
                self.assertFalse(self.report()['passed'])

    def test_flat_junit_failures_errors_and_skips_cannot_pass(self):
        for tag, field in (('failure', 'failures'), ('error', 'errors'), ('skipped', 'skipped')):
            with self.subTest(tag=tag):
                self.junit.write_text('<testsuites><testcase name="case"><%s message="private payload"/>'
                                      '</testcase></testsuites>' % tag)
                report = self.report()
                self.assertFalse(report['passed'])
                self.assertEqual(report['tests'], 1)
                self.assertEqual(report[field], 1)
                self.assertNotIn('private payload', json.dumps(report))

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
