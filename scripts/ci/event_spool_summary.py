"""Retain bounded, sanitized CI evidence for the local event-spool Module only."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import xml.etree.ElementTree as ET


PACKAGE = Path('apps/runner/event-spool')
NODE = 'v24.22.1'
COMPILER = '6.0.3'
SQLITE = '3.53.4'
SQLITE_SOURCE = '2026-07-24 19:02:57 bf7c7f30031888f4e796e429ab3978879485813aaca6f641c7b33e4e09459bcc'
STEPS = ('install', 'typecheck', 'tests')
JSON_LIMIT = 64 * 1024
COMPILE_OPTIONS_LIMIT = 16 * 1024
JUNIT_LIMIT = 2 * 1024 * 1024
SOURCE_LIMIT = 2 * 1024 * 1024
TOTAL_SOURCE_LIMIT = 8 * 1024 * 1024
REQUIRED_SOURCES = {'package.json', 'package-lock.json', 'tsconfig.json',
                    'spool.ts', 'types.ts', 'spool.test.ts'}


def read_bounded(path: Path, limit: int) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise ValueError('invalid_evidence_file')
    with path.open('rb') as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError('evidence_size_limit')
    return raw


def read_object(path: Path) -> tuple[dict, str]:
    raw = read_bounded(path, JSON_LIMIT)
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError('evidence_object_required')
    return value, hashlib.sha256(raw).hexdigest()


def source_hashes(root: Path) -> dict[str, str]:
    package = root / PACKAGE
    if package.is_symlink() or not package.is_dir():
        raise ValueError('source_directory_unavailable')
    result = {}
    total = 0
    for folder, directories, files in os.walk(package, followlinks=False):
        directories[:] = sorted(name for name in directories if name != 'node_modules')
        if any((Path(folder) / name).is_symlink() for name in directories):
            raise ValueError('source_symlink')
        for name in sorted(files):
            path = Path(folder) / name
            if path.suffix not in {'.ts', '.json'}:
                continue
            relative = path.relative_to(root).as_posix()
            if len(relative) > 240 or not re.fullmatch(r'[A-Za-z0-9_./@+\-]+', relative):
                raise ValueError('source_name_invalid')
            raw = read_bounded(path, SOURCE_LIMIT)
            total += len(raw)
            if len(result) >= 128 or total > TOTAL_SOURCE_LIMIT:
                raise ValueError('source_size_limit')
            result[relative] = hashlib.sha256(raw).hexdigest()
    if not {str(PACKAGE / name) for name in REQUIRED_SOURCES}.issubset(result):
        raise ValueError('source_set_incomplete')
    return dict(sorted(result.items()))


def safe_version(value: object, node: bool = False) -> str | None:
    pattern = r'v\d{1,3}\.\d{1,3}\.\d{1,3}' if node else r'\d{1,3}\.\d{1,3}\.\d{1,3}'
    return value if isinstance(value, str) and re.fullmatch(pattern, value) else None


def checked_compile_options(value: object, claimed_hash: object) -> tuple[list[str], str]:
    # SQLite values are printable build descriptions, not identifier tokens.
    # The official Node binary's COMPILER includes spaces and parentheses.
    if (not isinstance(value, list) or not 1 <= len(value) <= 256
            or any(not isinstance(option, str) or not 1 <= len(option) <= 256
                   or not re.fullmatch(r'[A-Z][A-Z0-9_]*(?:=[\x20-\x7e]+)?', option)
                   for option in value)
            or value != sorted(value) or len(set(value)) != len(value)):
        raise ValueError('compile_options_invalid')
    raw = json.dumps(value, separators=(',', ':'), ensure_ascii=True).encode('ascii')
    digest = hashlib.sha256(raw).hexdigest()
    if len(raw) > COMPILE_OPTIONS_LIMIT or claimed_hash != digest:
        raise ValueError('compile_options_hash_invalid')
    return value, digest


def summarize(root: Path, source_commit: str | None, outcomes: dict) -> dict:
    folder = root / 'ci-evidence'
    issues = []
    runtime, marker, hashes = {}, {}, {}
    runtime_hash = junit_hash = lock_hash = None
    expected_compiler = None
    try:
        package, _ = read_object(root / PACKAGE / 'package.json')
        expected_compiler = package['devDependencies']['typescript']
        if (expected_compiler != COMPILER or package['engines']['node'] != NODE[1:]
                or package.get('dependencies', {}) != {}):
            issues.append('package_toolchain_not_pinned')
    except (OSError, ValueError, KeyError, TypeError, RecursionError):
        issues.append('package_manifest_unavailable')
    try:
        hashes = source_hashes(root)
        lock_hash = hashes[(PACKAGE / 'package-lock.json').as_posix()]
    except (OSError, ValueError):
        issues.append('source_evidence_unavailable')
    try:
        runtime, runtime_hash = read_object(folder / 'event-spool-runtime.json')
    except (OSError, ValueError, RecursionError):
        issues.append('runtime_evidence_unavailable')
    try:
        marker, _ = read_object(folder / 'event-spool-typecheck.json')
    except (OSError, ValueError, RecursionError):
        issues.append('typecheck_evidence_unavailable')
    commit_valid = isinstance(source_commit, str) and re.fullmatch(r'[0-9a-f]{40}', source_commit)
    if not commit_valid:
        issues.append('source_commit_unavailable')
    compile_options, compile_hash = [], None
    compile_valid = False
    try:
        compile_options, compile_hash = checked_compile_options(
            runtime.get('sqlite_compile_options'), runtime.get('sqlite_compile_options_sha256'))
        compile_valid = not any(re.match(
            r'^(?:NO_SYNC|OMIT_WAL|OMIT_PAGER_PRAGMAS|OMIT_COMPILEOPTION_DIAGS)(?:=|$)', option)
            or option == 'THREADSAFE=0' for option in compile_options)
        if not compile_valid:
            issues.append('unsafe_sqlite_build')
    except ValueError:
        issues.append('compile_options_not_verified')
    runtime_valid = (runtime.get('node') == NODE and runtime.get('sqlite_version') == SQLITE
                     and runtime.get('sqlite_source_id') == SQLITE_SOURCE
                     and runtime.get('typescript') == expected_compiler == COMPILER
                     and commit_valid and runtime.get('source_commit') == source_commit
                     and runtime.get('capture_issues') == [] and compile_valid)
    if not runtime_valid:
        issues.append('runtime_not_verified')
    sources_current = bool(hashes) and runtime.get('source_sha256') == hashes
    if not sources_current:
        issues.append('source_hashes_not_current')
    valid_outcomes = {'success', 'failure', 'cancelled', 'skipped'}
    statuses = {step: outcomes.get(step) if outcomes.get(step) in valid_outcomes else None for step in STEPS}
    if set(outcomes) != set(STEPS) or any(value != 'success' for value in statuses.values()):
        issues.append('workflow_step_not_successful')
    typecheck = (runtime_valid and sources_current and statuses['install'] == 'success'
                 and statuses['typecheck'] == 'success'
                 and marker.get('passed') is True and marker.get('node') == NODE
                 and marker.get('typescript') == expected_compiler == COMPILER
                 and commit_valid and marker.get('source_commit') == source_commit)
    if not typecheck:
        issues.append('typecheck_not_verified')
    tests = failures = errors = skipped = 0
    try:
        raw = read_bounded(folder / 'event-spool.junit.xml', JUNIT_LIMIT)
        junit_hash = hashlib.sha256(raw).hexdigest()
        if b'<!DOCTYPE' in raw.upper() or b'<!ENTITY' in raw.upper():
            raise ValueError('junit_declaration_forbidden')
        xml = ET.fromstring(raw)
        if xml.tag not in {'testsuites', 'testsuite'}:
            raise ValueError('junit_root_invalid')
        if not list(xml.iter('testsuite')) or (xml.tag == 'testsuites' and any(
                child.tag != 'testsuite' for child in xml)):
            raise ValueError('junit_suites_required')
        cases = list(xml.iter('testcase'))
        if (any(not case.get('name') for case in cases)
                or sum(len(suite.findall('testcase')) for suite in xml.iter('testsuite')) != len(cases)):
            raise ValueError('junit_case_name_required')
        tests = len(cases)
        failures, errors, skipped = (len(list(xml.iter(name))) for name in ('failure', 'error', 'skipped'))
        for suite in xml.iter():
            if suite.tag not in {'testsuites', 'testsuite'}:
                continue
            observed = {'tests': len(list(suite.iter('testcase'))),
                        **{key: len(list(suite.iter(tag))) for key, tag in
                           (('failures', 'failure'), ('errors', 'error'), ('skipped', 'skipped'))}}
            for key, count in observed.items():
                if key in suite.attrib and suite.attrib[key] != str(count):
                    raise ValueError('junit_counts_inconsistent')
        if not tests:
            issues.append('no_test_cases')
        if failures or errors or skipped:
            issues.append('test_results_not_successful')
    except (OSError, ValueError, ET.ParseError, RecursionError):
        issues.append('test_evidence_unavailable')
    return {
        'scope': 'local_event_spool_module_regressions_only',
        'node': safe_version(runtime.get('node'), node=True),
        'sqlite_version': safe_version(runtime.get('sqlite_version')),
        'sqlite_source_id': SQLITE_SOURCE if runtime.get('sqlite_source_id') == SQLITE_SOURCE else None,
        'sqlite_compile_options': compile_options,
        'sqlite_compile_options_sha256': compile_hash,
        'typescript': safe_version(runtime.get('typescript')),
        'typecheck_passed': bool(typecheck), 'tests': tests,
        'failures': failures, 'errors': errors, 'skipped': skipped,
        'passed': not issues, 'source_commit': source_commit if commit_valid else None,
        'source_sha256': hashes, 'step_outcomes': statuses, 'evidence_issues': issues,
        'runtime_sha256': runtime_hash, 'junit_sha256': junit_hash,
        'package_lock_sha256': lock_hash, 'real_model_calls': 0,
        'control_plane_commit_verified': False, 'generic_filesystem_quota_verified': False,
        'vm_isolation_verified': False, 'power_loss_recovery_verified': False,
        'native_sigkill_recovery_verified': False,
    }


def capture_runtime(root: Path) -> dict:
    # No fixture names, event contents, environment dump or hostnames enter this marker.
    program = """import fs from 'node:fs'; import { createHash } from 'node:crypto'; import { DatabaseSync } from 'node:sqlite';
const db = new DatabaseSync(':memory:');
try {
  const statement = db.prepare('SELECT sqlite_version() AS sqlite_version, sqlite_source_id() AS sqlite_source_id');
  let version; try { version = statement.get(); } finally { statement.close(); }
  const compile = db.prepare('PRAGMA compile_options');
  let options; try { options = compile.all().map(row => row.compile_options).sort(); } finally { compile.close(); }
  const optionsHash = createHash('sha256').update(JSON.stringify(options)).digest('hex');
  const typescript = JSON.parse(fs.readFileSync('node_modules/typescript/package.json', 'utf8')).version;
  process.stdout.write(JSON.stringify({node:process.version, ...version, typescript, source_commit:process.env.GITHUB_SHA,
    sqlite_compile_options:options, sqlite_compile_options_sha256:optionsHash}));
} finally { db.close(); }
"""
    marker = {'capture_issues': []}
    try:
        process = subprocess.run(['node', '--input-type=module', '-e', program], cwd=root / PACKAGE,
                                 capture_output=True, text=True, timeout=15, check=True)
        if len(process.stdout) > 32 * 1024:
            raise ValueError('runtime_size_limit')
        actual = json.loads(process.stdout)
        if not isinstance(actual, dict):
            raise ValueError('runtime_object_required')
        checked_compile_options(actual.get('sqlite_compile_options'), actual.get('sqlite_compile_options_sha256'))
        marker.update({key: actual.get(key) for key in
                       ('node', 'sqlite_version', 'sqlite_source_id', 'typescript', 'source_commit',
                        'sqlite_compile_options', 'sqlite_compile_options_sha256')})
    except (OSError, ValueError, subprocess.SubprocessError, RecursionError):
        marker['capture_issues'].append('runtime_capture_failed')
    try:
        marker['source_sha256'] = source_hashes(root)
    except (OSError, ValueError):
        marker['capture_issues'].append('source_capture_failed')
    return marker


def write_report(path: Path, report: dict) -> None:
    raw = json.dumps(report, indent=2, sort_keys=True) + '\n'
    if len(raw.encode()) > JSON_LIMIT:
        raise ValueError('summary_size_limit')
    path.parent.mkdir(exist_ok=True)
    path.write_text(raw, encoding='utf-8')


def main() -> int:
    root = Path(__file__).resolve().parents[2]
    if sys.argv[1:] == ['--capture-runtime']:
        marker = capture_runtime(root)
        write_report(root / 'ci-evidence/event-spool-runtime.json', marker)
        return 1 if marker['capture_issues'] else 0
    if sys.argv[1:]:
        raise SystemExit('Unsupported summary mode')
    report = summarize(root, os.environ.get('GITHUB_SHA'), {
        key: os.environ.get(key.upper() + '_OUTCOME') for key in STEPS})
    write_report(root / 'ci-evidence/event-spool.json', report)
    print(json.dumps(report, sort_keys=True))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
