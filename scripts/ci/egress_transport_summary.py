"""Retain sanitized CI evidence even when install/typecheck/tests failed."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import xml.etree.ElementTree as ET


def summarize(root: Path, node: str | None, source_commit: str | None, outcomes: dict) -> dict:
    folder = root / 'ci-evidence'
    issues = []
    marker = {}
    expected_compiler = None
    try:
        package = json.loads((root / 'apps/model-egress/package.json').read_text())
        expected_compiler = package['devDependencies']['typescript']
    except (OSError, ValueError, KeyError, TypeError):
        issues.append('package_manifest_unavailable')
    try:
        marker = json.loads((folder / 'egress-transport-typecheck.json').read_text())
        if not isinstance(marker, dict):
            marker = {}
    except (OSError, ValueError):
        issues.append('typecheck_evidence_unavailable')
    typecheck = (node == 'v24.22.1' and marker.get('node') == node
                 and marker.get('passed') is True and expected_compiler is not None
                 and marker.get('typescript') == expected_compiler
                 and marker.get('source_commit') == source_commit)
    if not typecheck:
        issues.append('typecheck_not_verified')
    tests = []
    counts = {'failure': 0, 'error': 0, 'skipped': 0}
    junit_hash = None
    try:
        raw = (folder / 'egress-transport.junit.xml').read_bytes()
        xml = ET.fromstring(raw)
        tests = list(xml.iter('testcase'))
        counts = {name: len(list(xml.iter(name))) for name in counts}
        junit_hash = hashlib.sha256(raw).hexdigest()
    except (OSError, ValueError, ET.ParseError):
        issues.append('test_evidence_unavailable')
    if not tests:
        issues.append('no_test_cases')
    lock_hash = None
    try:
        lock_hash = hashlib.sha256((root / 'apps/model-egress/package-lock.json').read_bytes()).hexdigest()
    except OSError:
        issues.append('package_lock_unavailable')
    valid_outcomes = {'success', 'failure', 'cancelled', 'skipped'}
    statuses = {key: value if value in valid_outcomes else None for key, value in outcomes.items()}
    workflow_evidence = source_commit is not None or any(value is not None for value in outcomes.values())
    if workflow_evidence and (set(statuses) != {'install', 'typecheck', 'tests'}
                              or any(value != 'success' for value in statuses.values())):
        issues.append('workflow_step_not_successful')
    return {
        'scope': 'transport_module_regressions_only', 'node': node,
        'typescript': marker.get('typescript') if typecheck else expected_compiler,
        'typecheck_passed': typecheck, 'tests': len(tests),
        'failures': counts['failure'], 'errors': counts['error'], 'skipped': counts['skipped'],
        'passed': not issues and all(value == 0 for value in counts.values()),
        'source_commit': source_commit, 'step_outcomes': statuses, 'evidence_issues': issues,
        'junit_sha256': junit_hash, 'package_lock_sha256': lock_hash,
        'real_model_calls': 0, 'vm_isolation_verified': False, 'production_egress_verified': False,
        'execution_authorization_verified': False, 'budget_accounting_verified': False,
        'kernel_egress_verified': False,
    }


def main():
    root = Path(__file__).resolve().parents[2]
    try:
        node = subprocess.check_output(['node', '--version'], text=True, stderr=subprocess.PIPE).strip()
    except (OSError, subprocess.SubprocessError):
        node = None
    report = summarize(root, node, os.environ.get('GITHUB_SHA'), {
        key: os.environ.get(key.upper() + '_OUTCOME') for key in ('install', 'typecheck', 'tests')})
    folder = root / 'ci-evidence'
    folder.mkdir(exist_ok=True)
    (folder / 'egress-transport.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
