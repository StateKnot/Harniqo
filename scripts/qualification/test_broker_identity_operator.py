#!/usr/bin/env python3
"""Operator safety checks; native UID/systemd qualification runs separately.

These checks do not provision accounts, contact systemd/libvirt or start VMs.
Real subprocesses substantiate only the bounded operator command interface.
"""
from __future__ import annotations

import copy
import errno
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import struct
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('qualify_broker_identities',
    REPO / 'scripts/qualification/qualify_broker_identities.py')
operator = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(operator)


class CommandBudgetTests(unittest.TestCase):
    def test_real_child_output_and_environment_are_bounded(self):
        code = 'import json,os;print(json.dumps({k:os.environ.get(k) for k in ("LC_ALL","PYTHONDONTWRITEBYTECODE","UNTRUSTED_OPERATOR_VALUE")}))'
        with patch.dict(os.environ, {'UNTRUSTED_OPERATOR_VALUE': 'must-not-be-inherited'}):
            result, output = operator._command([sys.executable, '-B', '-c', code])
        self.assertEqual(result, 0)
        self.assertEqual(json.loads(output), {'LC_ALL': 'C', 'PYTHONDONTWRITEBYTECODE': '1',
            'UNTRUSTED_OPERATOR_VALUE': None})

    def test_shell_metacharacters_are_passed_literally(self):
        value = '$(printf bad)`printf bad`; never-a-shell'
        result, output = operator._command([sys.executable, '-B', '-c',
            'import sys;print(sys.argv[1])', value])
        self.assertEqual(result, 0)
        self.assertEqual(output.strip(), value)

    def test_real_output_overflow_kills_and_reaps_only_the_spawned_child(self):
        children = []; real_popen = subprocess.Popen
        def spawn(*args, **kwargs):
            child = real_popen(*args, **kwargs); children.append(child); return child
        with patch.object(operator.subprocess, 'Popen', side_effect=spawn):
            with self.assertRaisesRegex(operator.QualificationError, 'command_output_limit'):
                operator._command([sys.executable, '-B', '-c',
                    'import sys,time;sys.stdout.write("x"*8192);sys.stdout.flush();time.sleep(2)'], output_limit=1024)
        self.assertEqual(len(children), 1)
        self.assertIsNotNone(children[0].poll())

    def test_real_timeout_is_finite_and_reaps_the_child(self):
        before = time.monotonic()
        with self.assertRaisesRegex(operator.QualificationError, 'command_timeout'):
            operator._command([sys.executable, '-B', '-c', 'import time;time.sleep(2)'], timeout=.1)
        self.assertLess(time.monotonic() - before, 1.5)

    def test_native_failure_is_a_finite_code_without_stderr_disclosure(self):
        with self.assertRaisesRegex(operator.QualificationError, '^command_failed$'):
            operator._command([sys.executable, '-B', '-c',
                'import sys;print("private-native-diagnostic",file=sys.stderr);sys.exit(3)'])
        result, output = operator._command([sys.executable, '-B', '-c', 'raise SystemExit(3)'], allowed_failure=True)
        self.assertEqual((result, output), (3, ''))


class IdentityGateTests(unittest.TestCase):
    def setUp(self):
        self.host = {'net': 'net:[10]', 'user': 'user:[20]'}
        self.evidence = {'pid': 123, 'start_ticks': '100', 'status': {
            'Uid': ['24002'] * 4, 'Gid': ['24002'] * 4,
            'Groups': ['24002', '24003'], 'CapInh': ['0'], 'CapPrm': ['0'],
            'CapEff': ['0'], 'CapBnd': ['0'], 'CapAmb': ['0'],
            'NoNewPrivs': ['1'], 'Seccomp': ['2']},
            'namespaces': {'net': 'net:[11]', 'user': 'user:[20]', 'mnt': 'mnt:[30]'}}

    def check(self, evidence):
        operator._identity(evidence, 24002, 24002, [24002, 24003], self.host)

    def test_exact_real_effective_saved_and_filesystem_ids_are_required(self):
        self.check(self.evidence)
        for field in ('Uid', 'Gid'):
            for position in range(4):
                value = copy.deepcopy(self.evidence); value['status'][field][position] = '0'
                with self.subTest(field=field, position=position), self.assertRaises(operator.QualificationError):
                    self.check(value)

    def test_unexpected_management_groups_or_missing_ipc_group_fail(self):
        for groups in (['24002', '24003', '998'], ['24002'], []):
            value = copy.deepcopy(self.evidence); value['status']['Groups'] = groups
            with self.subTest(groups=groups), self.assertRaisesRegex(operator.QualificationError, 'groups_mismatch'):
                self.check(value)

    def test_every_capability_set_nnp_and_seccomp_are_observed(self):
        for field in ('CapInh', 'CapPrm', 'CapEff', 'CapBnd', 'CapAmb', 'NoNewPrivs', 'Seccomp'):
            value = copy.deepcopy(self.evidence); value['status'][field] = ['1' if field.startswith('Cap') else '0']
            with self.subTest(field=field), self.assertRaisesRegex(operator.QualificationError, 'privileges_mismatch'):
                self.check(value)

    def test_host_network_or_remapped_user_namespace_cannot_pass(self):
        for field, value in (('net', self.host['net']), ('user', 'user:[different]')):
            evidence = copy.deepcopy(self.evidence); evidence['namespaces'][field] = value
            with self.subTest(field=field), self.assertRaisesRegex(operator.QualificationError, 'namespaces_mismatch'):
                self.check(evidence)

    def test_worker_rejects_root_before_reading_proc(self):
        with patch.object(operator.os, 'getuid', return_value=0), patch.object(operator, '_process') as process:
            with self.assertRaisesRegex(operator.QualificationError, 'worker_privileged'):
                operator._worker_evidence(24002, 24002, [24002, 24003], self.host)
        process.assert_not_called()

    def test_success_and_an_unconfirmed_error_cannot_masquerade_as_predispatch_refusal(self):
        def call(code, outcome):
            def fail():
                raise operator.ipc.IPCError(code, outcome=outcome)
            return fail
        self.assertTrue(operator._expected_error(call('ipc_peer_denied', 'not_dispatched'),
            'ipc_peer_denied', 'not_dispatched'))
        self.assertFalse(operator._expected_error(call('ipc_peer_denied', 'unconfirmed'),
            'ipc_peer_denied', 'not_dispatched'))
        self.assertFalse(operator._expected_error(lambda: object(), 'ipc_peer_denied', 'not_dispatched'))


class FilesystemGateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(tempfile.gettempdir()).resolve())
        self.addCleanup(self.temp.cleanup); self.root = Path(self.temp.name)

    def test_path_parser_rejects_aliases_and_control_characters(self):
        for value in (None, '', 'relative', '/', '//run/harniqo', '/run/a/../b', '/run//b', '/run/x\nsecret'):
            with self.subTest(value=value), self.assertRaises(operator.QualificationError):
                operator._path(value)

    def test_real_symlink_is_rejected_before_following_it(self):
        target = self.root / 'target'; target.mkdir()
        link = self.root / 'link'; link.symlink_to(target, target_is_directory=True)
        with self.assertRaisesRegex(operator.QualificationError, 'symlink_rejected'):
            operator._ancestors(link)

    def test_private_input_reader_rejects_hardlinks_group_write_and_wrong_modes(self):
        path = self.root / 'input'; path.write_bytes(b'fixed'); path.chmod(0o600)
        with patch.object(operator, '_ancestors'):
            self.assertEqual(operator._read(path, owner=os.getuid(), modes={0o600}), b'fixed')
            os.link(path, self.root / 'alias')
            with self.assertRaises(operator.QualificationError):
                operator._read(path, owner=os.getuid())
            (self.root / 'alias').unlink(); path.chmod(0o620)
            with self.assertRaises(operator.QualificationError):
                operator._read(path, owner=os.getuid())
            path.chmod(0o644)
            with self.assertRaises(operator.QualificationError):
                operator._read(path, owner=os.getuid(), modes={0o600})

    def test_readonly_mount_is_not_accepted_as_a_private_read_denial(self):
        with patch.object(operator.os, 'open', side_effect=OSError(errno.EROFS, 'read-only')):
            self.assertFalse(operator._denied_open('/private', os.O_RDONLY))
            self.assertTrue(operator._denied_open('/immutable', os.O_WRONLY))
        with patch.object(operator.os, 'open', side_effect=OSError(errno.ENOENT, 'missing')):
            self.assertFalse(operator._denied_open('/missing', os.O_RDONLY))

    def test_read_probe_never_reads_or_modifies_an_unexpectedly_accessible_file(self):
        path = self.root / 'canary'; path.write_bytes(b'unchanged')
        self.assertFalse(operator._denied_open(path, os.O_RDONLY))
        self.assertEqual(path.read_bytes(), b'unchanged')

    def test_json_rejects_duplicate_and_nonfinite_members(self):
        for raw in (b'{"x":1,"x":2}', b'{"x":NaN}', b'{"x":Infinity}'):
            with self.subTest(raw=raw), self.assertRaises(operator.QualificationError):
                operator._decode(raw)

    def test_named_ancestor_acl_cannot_grant_peer_directory_write(self):
        def acl(tag, principal, rights, mask=7):
            return struct.pack('<I', 2) + struct.pack('<HHI', tag, rights, principal) + struct.pack('<HHI', 16, mask, 0xffffffff)
        for tag, principal in ((2, 24002), (8, 24003)):
            with self.subTest(tag=tag), patch.object(operator.os, 'getxattr', return_value=acl(tag, principal, 7), create=True):
                with self.assertRaisesRegex(operator.QualificationError, 'peer_can_modify_data_ancestor_acl'):
                    operator._peer_acl(Path('/trusted/developer'), 24002, {24002, 24003})
        with patch.object(operator.os, 'getxattr', return_value=acl(2, 24002, 7, mask=5), create=True):
            self.assertRegex(operator._peer_acl(Path('/trusted/developer'), 24002, {24003}), '^[0-9a-f]{64}$')
        with patch.object(operator.os, 'getxattr', side_effect=OSError(errno.EACCES, 'unreadable'), create=True):
            with self.assertRaisesRegex(operator.QualificationError, 'ancestor_acl_unavailable'):
                operator._peer_acl(Path('/trusted/developer'), 24002, {24003})


class OwnedUnitTests(unittest.TestCase):
    def setUp(self):
        self.report = {'units': [], 'logs': {}}
        self.units = operator.Units('harniqo-idq-0123456789abcdef', self.report)

    def test_existing_unit_is_never_adopted_or_started(self):
        with patch.object(self.units, 'show', return_value={'LoadState': 'loaded'}), patch.object(self.units, 'command') as command:
            with self.assertRaisesRegex(operator.QualificationError, 'unit_exists'):
                self.units.start('serve', ['/usr/bin/python3'], [])
        command.assert_not_called(); self.assertEqual(self.units.owned, {})

    def test_only_generated_role_names_can_be_registered(self):
        for name in ('../libvirtd', 'x.service;stop', 'x' * 33, ''):
            with self.subTest(name=name), self.assertRaises(operator.QualificationError):
                self.units.start(name, ['/usr/bin/python3'], [])
        self.assertEqual(self.units.owned, {})

    def test_unknown_or_replaced_unit_never_receives_stop(self):
        with patch.object(self.units, 'command') as command:
            with self.assertRaisesRegex(operator.QualificationError, 'unit_not_owned'):
                self.units.stop('libvirtd.service')
        command.assert_not_called()
        name = self.units.prefix + '-serve.service'; self.units.owned[name] = 'expected'
        with patch.object(self.units, 'show', return_value={'Id': name, 'Transient': 'yes',
                'LoadState': 'loaded', 'Description': 'another owner'}), patch.object(self.units, 'command') as command:
            with self.assertRaisesRegex(operator.QualificationError, 'ownership_changed'):
                self.units.stop(name)
        command.assert_not_called()

    def test_short_lived_workers_remain_identifiable_after_successful_exit(self):
        state = {'ActiveState': 'active', 'SubState': 'exited', 'MainPID': '0',
            'ExecMainStatus': '0', 'Result': 'success'}
        with patch.object(self.units, 'check_owned', return_value=state):
            self.assertEqual(self.units.completed('own'), state)
        for edit in ({'ExecMainStatus': '1'}, {'Result': 'exit-code'}, {'MainPID': '123'}):
            with self.subTest(edit=edit), patch.object(self.units, 'check_owned', return_value={**state, **edit}):
                with self.assertRaises(operator.QualificationError):
                    self.units.completed('own')

    def test_same_named_unit_with_a_different_invocation_never_receives_stop(self):
        name = self.units.prefix + '-serve.service'; self.units.owned[name] = 'expected'
        state = {'Id': name, 'Transient': 'yes', 'LoadState': 'loaded',
            'Description': 'expected', 'MainPID': '123', 'InvocationID': 'a' * 32}
        with patch.object(self.units, 'show', return_value=state):
            self.units.check_owned(name)
        with patch.object(self.units, 'show', return_value={**state, 'InvocationID': 'b' * 32}), patch.object(self.units, 'command') as command:
            with self.assertRaisesRegex(operator.QualificationError, 'unit_invocation_changed'):
                self.units.stop(name)
        command.assert_not_called()

    def test_log_failure_still_stops_the_identified_unit_and_cannot_pass_cleanup(self):
        name = self.units.prefix + '-serve.service'; self.units.owned[name] = 'expected'
        self.report['status'] = 'VERIFIED'
        running = {'Id': name, 'Transient': 'yes', 'LoadState': 'loaded',
            'Description': 'expected', 'MainPID': '123', 'InvocationID': 'a' * 32}
        stopped = {**running, 'MainPID': '0'}
        with patch.object(self.units, 'logs', side_effect=operator.QualificationError('command_output_limit')), patch.object(self.units, 'show', side_effect=[running, stopped]), patch.object(self.units, 'command', return_value=(0, '')) as command:
            failures = self.units.cleanup()
        self.assertEqual(failures, [name])
        command.assert_called_once_with(['/usr/bin/systemctl', 'stop', name])
        self.assertEqual(self.report['cleanup_errors'], [
            {'unit': name, 'phase': 'logs', 'code': 'command_output_limit'}])
        self.assertEqual(self.report['status'], 'FAILED')

    def test_privilege_and_management_isolation_are_explicit_in_actual_unit_request(self):
        properties = operator._properties(24002, 24002, 24003, writable=(Path('/run/harniqo/own ipc'),),
            inaccessible=(Path('/var/lib/harniqo/private key'),))
        for expected in ('User=24002', 'Group=24002', 'SupplementaryGroups=24003',
                'PrivateUsers=no', 'PrivateNetwork=yes', 'RestrictAddressFamilies=AF_UNIX',
                'CapabilityBoundingSet=', 'AmbientCapabilities=', 'NoNewPrivileges=yes',
                'ProtectProc=default', 'ProcSubset=all', 'ProtectSystem=strict', 'MemoryMax=256M'):
            self.assertIn(expected, properties)
        hidden = next(value for value in properties if value.startswith('InaccessiblePaths='))
        for path in ('/run/libvirt', '/run/docker.sock', '/run/containerd', '/run/dbus/system_bus_socket'):
            self.assertIn('"-' + path + '"', hidden)
        self.assertIn('"-/var/lib/harniqo/private key"', hidden)
        self.assertIn('ReadWritePaths="/run/harniqo/own ipc"', properties)
        with patch.object(self.units, 'show', return_value={'LoadState': 'not-found'}), patch.object(self.units, 'command', return_value=(0, '')) as command:
            name = self.units.start('probe', ['/usr/bin/python3', '-B', '/opt/immutable.py'], properties)
        invocation = command.call_args[0][0]
        self.assertIn('--unit=' + name, invocation)
        self.assertIn('--property=NoNewPrivileges=yes', invocation)
        self.assertIn('--', invocation)
        self.assertEqual(len(self.units.owned), 1)

    def test_nonroot_parent_fails_before_opening_output_or_contacting_systemd(self):
        with patch.object(operator.os, 'getuid', return_value=1000), patch.object(operator.os, 'geteuid', return_value=1000), patch.object(operator, '_ancestors') as paths, patch.object(operator, '_command') as command:
            with self.assertRaisesRegex(operator.QualificationError, 'root_linux_parent_required'):
                operator.qualify(Path('/etc/fixture.json'), 'a' * 64, Path('/private/report.json'))
        paths.assert_not_called(); command.assert_not_called()


class FourOperationProbeTests(unittest.TestCase):
    def test_probe_checks_ensure_and_renew_bad_signatures_without_issuing_a_valid_envelope(self):
        resource = {'resource_id': '00000000-0000-4000-8000-000000000001',
            'organization_id': 'org-a', 'workspace_id': 'workspace-a'}
        value = {'broker': {'uid': 24001, 'gid': 24001},
            'supervisor': {'uid': 24002, 'gid': 24002}, 'ipc_gid': 24003,
            'host_namespaces': {}, 'ipc_directory': '/run/harniqo/ipc',
            'resource': resource, 'foreign_resource_id': '00000000-0000-4000-8000-000000000002',
            'template_id': 'fixed', 'template_digest': 'a' * 64,
            'private_read_paths': [], 'immutable_write_paths': [],
            'create_canary': '.create', 'replace_source': '.source', 'replace_target': '.target'}
        raw = operator._json(value); calls = []
        class Client:
            def __init__(self, **unused):
                pass
            def observe(self, identity):
                calls.append(('observe', identity))
                if identity != resource['resource_id']:
                    raise operator.ipc.IPCError('ipc_resource_denied', outcome='not_dispatched')
                raise operator.ipc.IPCError('resource_not_registered', outcome='unconfirmed')
            def revoke(self, identity):
                calls.append(('revoke', identity))
                raise operator.ipc.IPCError('resource_not_registered', outcome='unconfirmed')
            def ensure(self, assignment):
                calls.append(('ensure', assignment))
                raise operator.ipc.IPCError('signature_invalid', outcome='unconfirmed')
            def renew(self, grant):
                calls.append(('renew', grant))
                raise operator.ipc.IPCError('signature_invalid', outcome='unconfirmed')
        with patch.object(operator, '_read', return_value=raw), patch.object(operator, '_worker_evidence', return_value={}), patch.object(operator, '_network_checks', return_value={}), patch.object(operator.ipc, 'BrokerClient', Client), patch.object(operator.os, 'open', side_effect=OSError(errno.EACCES, 'denied')), patch.object(operator.os, 'replace', side_effect=OSError(errno.EACCES, 'denied')), patch.object(operator, '_emit') as emit:
            result = operator._probe(Path('/run/harniqo/control/probe.json'), hashlib.sha256(raw).hexdigest(), 'supervisor-dac')
        self.assertEqual(result, 0)
        self.assertEqual([call[0] for call in calls], ['observe', 'revoke', 'observe', 'ensure', 'renew'])
        assignment = calls[-2][1]; grant = calls[-1][1]
        self.assertIs(type(assignment), operator.Assignment)
        self.assertIs(type(grant), operator.Grant)
        self.assertEqual(assignment.signature, '0' * 64)
        self.assertEqual(operator.asdict(assignment), operator.asdict(grant))
        checks = emit.call_args[0][0]['checks']
        self.assertTrue(checks['invalid_signature_rejected_by_real_core'])
        self.assertTrue(checks['invalid_renewal_signature_rejected_by_real_core'])


if __name__ == '__main__':
    unittest.main()
