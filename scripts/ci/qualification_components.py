"""Run only synthetic, standard-library qualification regression suites.

This command never launches Docker, libvirt or a real model. Native acceptance
uses the designated server and remains a separate evidence category.
"""
import json
from pathlib import Path
import platform
import shutil
import sys
import time
import unittest

ROOT = Path(__file__).resolve().parents[2]
TESTS = (
    'test_probe_model.py',
    'test_qualify_kvm.py',
    'test_qualify_kvm_network.py',
    'test_kvm_host_policy.py',
    'test_prepare_openclaw.py',
    'test_run_hermes_qualification.py',
    'test_prepare_opencode.py',
    'test_opencode_lifecycle.py',
    'test_prepare_octop.py',
    'test_run_octop.py',
    'test_probe_octop.py',
    'test_probe_octop_service.py',
    'test_guard_octop_service.py',
    'test_opencode_transport_report.py',
    'test_octop_acp.py',
    'test_dsh_operator.py',
    'test_resource_broker.py',
    'test_resource_broker_operator.py',
    'test_resource_pressure.py',
    'test_guest_memory_pressure.py',
)


def main():
    if sys.platform != 'linux' or sys.version_info[:2] != (3, 12):
        raise SystemExit('This qualification regression job requires Linux/Python 3.12')
    if shutil.disk_usage(ROOT).free < 5*1024**3:
        raise SystemExit('Insufficient CI workspace space')
    folder = ROOT/'ci-evidence'
    folder.mkdir(exist_ok=True)
    suite = unittest.TestSuite()
    for name in TESTS:
        selected = unittest.defaultTestLoader.discover(str(ROOT/'scripts/qualification'), pattern=name)
        if selected.countTestCases() == 0:
            raise SystemExit('Required regression suite missing: '+name)
        suite.addTests(selected)
    started = time.monotonic()
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    report = {
        'scope': 'synthetic_qualification_components_only',
        'python': platform.python_version(),
        'system': platform.system(),
        'suites': list(TESTS),
        'tests': result.testsRun,
        'failures': len(result.failures),
        'errors': len(result.errors),
        'skipped': len(result.skipped),
        'unexpected_successes': len(result.unexpectedSuccesses),
        'expected_failures': len(result.expectedFailures),
        'seconds': time.monotonic()-started,
        'passed': result.wasSuccessful() and not result.skipped and not result.expectedFailures,
        'native_harness_verified': False,
        'vm_isolation_verified': False,
        'real_model_calls': 0,
    }
    (folder/'qualification-python.json').write_text(json.dumps(report, indent=2)+'\n', encoding='utf-8')
    print(json.dumps(report))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
