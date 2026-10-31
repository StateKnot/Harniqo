"""Exercise failure evidence retention without Node, npm or network access."""
from __future__ import annotations

from contextlib import redirect_stdout
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).with_name('egress_transport_summary.py')
SPEC = importlib.util.spec_from_file_location('egress_transport_summary', SCRIPT)
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
        self.package = self.root / 'apps/model-egress'
        self.folder.mkdir()
        self.package.mkdir(parents=True)
        self.manifest = self.package / 'package.json'
        self.lock = self.package / 'package-lock.json'
        self.marker = self.folder / 'egress-transport-typecheck.json'
        self.junit = self.folder / 'egress-transport.junit.xml'
        self.manifest.write_text(json.dumps({'devDependencies': {'typescript': '6.0.3'}}))
        self.lock.write_text('{"lockfileVersion":3}\n')
        self.marker_data = {'node': 'v24.22.1', 'typescript': '6.0.3',
                            'source_commit': COMMIT, 'passed': True}
        self.marker.write_text(json.dumps(self.marker_data))
        self.junit.write_text('<testsuites><testsuite>' + ''.join(
            '<testcase name="case-%d"/>' % number for number in range(14)
        ) + '</testsuite></testsuites>')

    def report(self, node='v24.22.1', commit=COMMIT, outcomes=None):
        return summary.summarize(self.root, node, commit, SUCCESS if outcomes is None else outcomes)

    def test_valid_evidence_binds_toolchain_source_and_hashes(self):
        report = self.report()
        self.assertTrue(report['passed'])
        self.assertTrue(report['typecheck_passed'])
        self.assertEqual(report['tests'], 14)
        self.assertEqual(report['evidence_issues'], [])
        self.assertEqual(report['source_commit'], COMMIT)
        self.assertEqual(report['junit_sha256'], hashlib.sha256(self.junit.read_bytes()).hexdigest())
        self.assertEqual(report['package_lock_sha256'], hashlib.sha256(self.lock.read_bytes()).hexdigest())
        self.assertEqual(report['real_model_calls'], 0)
        for key, value in report.items():
            if key.endswith('_verified'):
                self.assertIs(value, False, key)

    def test_missing_evidence_cannot_report_success(self):
        for path, issue in ((self.manifest, 'package_manifest_unavailable'),
                            (self.lock, 'package_lock_unavailable'),
                            (self.marker, 'typecheck_evidence_unavailable'),
                            (self.junit, 'test_evidence_unavailable')):
            with self.subTest(path=path.name):
                raw = path.read_bytes()
                path.unlink()
                report = self.report()
                self.assertFalse(report['passed'])
                self.assertIn(issue, report['evidence_issues'])
                path.write_bytes(raw)

    def test_corrupt_evidence_cannot_report_success(self):
        for path, value in ((self.manifest, '{'), (self.manifest, '[]'),
                            (self.marker, '{'), (self.marker, '[]'),
                            (self.junit, '<testsuite>')):
            with self.subTest(path=path.name, value=value):
                raw = path.read_bytes()
                path.write_text(value)
                self.assertFalse(self.report()['passed'])
                path.write_bytes(raw)

    def test_unverified_typecheck_cannot_report_success(self):
        for key, value in (('node', 'v26.4.0'), ('typescript', '6.0.2'),
                           ('source_commit', 'b' * 40), ('source_commit', None),
                           ('passed', False), ('passed', 'true')):
            with self.subTest(key=key, value=value):
                marker = dict(self.marker_data, **{key: value})
                self.marker.write_text(json.dumps(marker))
                report = self.report()
                self.assertFalse(report['passed'])
                self.assertFalse(report['typecheck_passed'])
                self.assertIn('typecheck_not_verified', report['evidence_issues'])

    def test_wrong_or_missing_node_cannot_report_success(self):
        for node in (None, 'v26.4.0', 'v24.22.0'):
            with self.subTest(node=node):
                self.assertFalse(self.report(node=node)['passed'])

    def test_failed_errored_or_skipped_test_cannot_report_success(self):
        for element, field in (('failure', 'failures'), ('error', 'errors'), ('skipped', 'skipped')):
            with self.subTest(element=element):
                self.junit.write_text('<testsuites><testsuite><testcase><%s/></testcase>'
                                      '</testsuite></testsuites>' % element)
                report = self.report()
                self.assertFalse(report['passed'])
                self.assertEqual(report[field], 1)

    def test_empty_suite_cannot_report_success(self):
        self.junit.write_text('<testsuites><testsuite/></testsuites>')
        report = self.report()
        self.assertFalse(report['passed'])
        self.assertIn('no_test_cases', report['evidence_issues'])

    def test_unsuccessful_or_missing_workflow_steps_cannot_report_success(self):
        for step in SUCCESS:
            for outcome in ('failure', 'cancelled', 'skipped', None, 'untrusted'):
                with self.subTest(step=step, outcome=outcome):
                    report = self.report(outcomes=dict(SUCCESS, **{step: outcome}))
                    self.assertFalse(report['passed'])
                    self.assertIn('workflow_step_not_successful', report['evidence_issues'])
        self.assertFalse(self.report(outcomes={})['passed'])
        self.assertFalse(self.report(outcomes=dict(SUCCESS, unexpected='success'))['passed'])

    def test_local_verified_run_does_not_require_github_metadata(self):
        self.marker_data.pop('source_commit')
        self.marker.write_text(json.dumps(self.marker_data))
        outcomes = dict.fromkeys(SUCCESS)
        self.assertTrue(self.report(commit=None, outcomes=outcomes)['passed'])
        outcomes['tests'] = 'failure'
        self.assertFalse(self.report(commit=None, outcomes=outcomes)['passed'])

    def test_main_writes_failed_summary_without_node_or_dependencies(self):
        script = self.root / 'scripts/ci/egress_transport_summary.py'
        script.parent.mkdir(parents=True)
        script.write_bytes(SCRIPT.read_bytes())
        for path in (self.manifest, self.lock, self.marker, self.junit):
            path.unlink()
        # The Python executable is absolute; an empty PATH makes Node genuinely unavailable.
        environment = {'PATH': '', 'GITHUB_SHA': COMMIT, 'INSTALL_OUTCOME': 'failure',
                       'TYPECHECK_OUTCOME': 'skipped', 'TESTS_OUTCOME': 'skipped'}
        process = subprocess.run([sys.executable, str(script)], env=environment,
                                 capture_output=True, text=True, timeout=10)
        self.assertEqual(process.returncode, 1, process.stderr)
        report = json.loads((self.folder / 'egress-transport.json').read_text())
        self.assertFalse(report['passed'])
        self.assertIsNone(report['node'])
        self.assertIn('package_manifest_unavailable', report['evidence_issues'])
        self.assertIn('workflow_step_not_successful', report['evidence_issues'])
        self.assertEqual(report, json.loads(process.stdout))

    def test_main_returns_failure_after_retaining_failed_junit(self):
        fake_file = self.root / 'scripts/ci/egress_transport_summary.py'
        self.junit.write_text('<testsuites><testsuite><testcase><failure message="private detail"/>'
                              '</testcase></testsuite></testsuites>')
        environment = dict(GITHUB_SHA=COMMIT, INSTALL_OUTCOME='success',
                           TYPECHECK_OUTCOME='success', TESTS_OUTCOME='failure')
        with patch.object(summary, '__file__', str(fake_file)), \
                patch.object(summary.subprocess, 'check_output', return_value='v24.22.1\n'), \
                patch.dict(os.environ, environment, clear=True), redirect_stdout(io.StringIO()):
            self.assertEqual(summary.main(), 1)
        raw = (self.folder / 'egress-transport.json').read_text()
        report = json.loads(raw)
        self.assertFalse(report['passed'])
        self.assertEqual(report['failures'], 1)
        self.assertNotIn('private detail', raw)


if __name__ == '__main__':
    unittest.main()
