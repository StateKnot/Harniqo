"""Identity lifecycle acceptance through the public Interface and bounded OS edge."""
from contextlib import contextmanager, ExitStack
import copy
import errno
import hashlib
import importlib.util
import json
import os
import stat
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('_broker_identities_test', REPO / 'scripts/deploy/provision_broker_identities.py')
m = importlib.util.module_from_spec(SPEC); sys.modules[SPEC.name] = m; SPEC.loader.exec_module(m)
PLAN = {'schema_version': 1, 'broker_account': {'name': 'harniqo-broker', 'uid': 24001, 'gid': 24001},
        'supervisor_account': {'name': 'harniqo-supervisor', 'uid': 24002, 'gid': 24002},
        'ipc_group': {'name': 'harniqo-ipc', 'gid': 24003}}


class MemoryStore:
    def __init__(self): self.value = None; self.writes = 0
    def read(self): return copy.deepcopy(self.value)
    def write(self, value): self.value = copy.deepcopy(value); self.writes += 1


class NativeFixture:
    def __init__(self):
        self.users = {}; self.groups = {}; self.shadow = {}; self.commands = []; self.fail_once = None; self.lost_reply = None
        self.include_primary = True; self.include_ipc = True; self.extra_initgroups = ()
    def snapshot(self, plan): return copy.deepcopy(list(self.users.values())), copy.deepcopy(list(self.groups.values()))
    def run(self, args, **kw):
        if args[0] == m.GETENT:
            name = args[-1]
            if args[1] == 'shadow': return name + ':' + self.shadow[name] + ':20000:0:99999:7:::\n'
            assert args[1] == 'initgroups'
            ids = ([self.users[name][3]] if self.include_primary else [])
            ids += [row[2] for row in self.groups.values() if self.include_ipc and name in row[3].split(',')]
            return name + ' ' + ' '.join([*ids, *self.extra_initgroups]) + '\n'
        self.commands.append(list(args)); name = args[-1]
        if self.fail_once == name:
            self.fail_once = None; raise m.IdentityError('identity_command_failed')
        gid = args[args.index('--gid') + 1]
        if args[0] == '/usr/sbin/groupadd':
            assert name not in self.groups; self.groups[name] = [name, 'x', gid, '']
        else:
            assert args[0] == '/usr/sbin/useradd' and name not in self.users
            self.users[name] = [name, 'x', args[args.index('--uid') + 1], gid, 'Harniqo service', '/nonexistent', '/usr/sbin/nologin']
            self.shadow[name] = args[args.index('--password') + 1]
            group = self.groups[args[args.index('--groups') + 1]]
            group[3] = ','.join(filter(None, [group[3], name]))
        if self.lost_reply == name: raise m.IdentityError('identity_command_timeout')
        return ''


class IdentityTests(unittest.TestCase):
    def setUp(self):
        self.data = json.dumps(PLAN).encode(); self.digest = hashlib.sha256(self.data).hexdigest()
        self.store = MemoryStore(); self.native = NativeFixture(); self.lock_modes = []
    @contextmanager
    def root(self):
        @contextmanager
        def locked(*, create): self.lock_modes.append(create); yield self.store
        with ExitStack() as stack:
            for name in ('getuid', 'geteuid', 'getgid', 'getegid'): stack.enter_context(patch.object(m.os, name, return_value=0))
            stack.enter_context(patch.object(m.sys, 'platform', 'linux'))
            stack.enter_context(patch.object(m, '_ancestors'))
            stack.enter_context(patch.object(m, '_read', return_value=self.data))
            stack.enter_context(patch.object(m, '_locked', locked))
            stack.enter_context(patch.object(m, '_snapshot', self.native.snapshot))
            stack.enter_context(patch.object(m, '_run', self.native.run))
            yield
    @contextmanager
    def filesystem_root(self, state):
        # Keep real open/write/fchmod/fsync/rename/lock operations. Only privilege
        # ownership and NSS edges differ from this non-root test process.
        with ExitStack() as stack:
            for name in ('getuid', 'geteuid', 'getgid', 'getegid'): stack.enter_context(patch.object(m.os, name, return_value=0))
            stack.enter_context(patch.object(m.sys, 'platform', 'linux'))
            stack.enter_context(patch.object(m, '_ancestors'))
            stack.enter_context(patch.object(m, '_root_owned', return_value=True))
            if not hasattr(m.os, 'getxattr'):
                # macOS CLT Python lacks Linux xattr APIs; Linux tests use the real edge.
                stack.enter_context(patch.object(m.os, 'getxattr', side_effect=OSError(errno.ENODATA, 'no fixture ACL'), create=True))
            stack.enter_context(patch.object(m, 'STATE_ROOT', state))
            stack.enter_context(patch.object(m, '_snapshot', self.native.snapshot))
            stack.enter_context(patch.object(m, '_run', self.native.run))
            yield
    def call(self, action='apply', **kwargs):
        return m.execute(Path('/root/identities.json'), expected_sha256=self.digest, action=action, **kwargs)
    def test_fresh_apply_and_reentry_keep_exact_locked_identities_without_recreating(self):
        with self.root():
            result = self.call(); repeated = self.call(); checked = self.call('check')
        self.assertTrue(result['verified'] and repeated['verified'] and checked['verified'])
        self.assertFalse(result['activation_authorized'] or result['libvirt_acl_verified'])
        self.assertEqual(len(self.native.commands), 5)
        self.assertEqual(self.lock_modes, [True, True, False])
        self.assertEqual(set(self.native.users), {'harniqo-broker', 'harniqo-supervisor'})
        for command in self.native.commands[3:]:
            self.assertIn('--no-create-home', command); self.assertIn('--no-user-group', command)
            self.assertIn('/usr/sbin/nologin', command); self.assertNotIn('--non-unique', command)
        self.assertEqual({row['status'] for row in self.store.value['steps']}, {'complete'})
    def test_initgroups_supplementary_only_output_uses_verified_passwd_primary(self):
        self.native.include_primary = False
        with self.root():
            try: result = self.call()
            except m.IdentityError as error: result = {'verified': False, 'code': str(error)}
        self.assertTrue(result['verified'], result)
    def test_initgroups_including_primary_remains_valid(self):
        self.native.include_primary = True
        with self.root(): self.assertTrue(self.call()['verified'])
    def test_initgroups_normalization_still_requires_ipc_and_denies_extra_groups(self):
        for include_ipc, extra in ((False, ()), (True, ('0',)), (True, ('998',))):
            with self.subTest(include_ipc=include_ipc, extra=extra):
                self.native = NativeFixture(); self.store = MemoryStore()
                self.native.include_primary = False; self.native.include_ipc = include_ipc; self.native.extra_initgroups = extra
                with self.root(), self.assertRaises(m.IdentityError): self.call()
                self.assertEqual(self.store.value['steps'][3]['status'], 'unconfirmed')
    def test_no_receipt_never_adopts_preexisting_name_or_numeric_identity(self):
        variants = [('foreign', 'x', '24001', ''), ('harniqo-broker', 'x', '25000', '')]
        for row in variants:
            with self.subTest(row=row):
                self.native.groups = {row[0]: list(row)}
                with self.root(), self.assertRaises(m.IdentityError): self.call()
                self.assertEqual(self.store.writes, 0); self.assertEqual(self.native.commands, [])
    def test_dangling_privileged_membership_refuses_fresh_user_before_mutation(self):
        self.native.groups['docker'] = ['docker', 'x', '998', 'harniqo-broker']
        with self.root(), self.assertRaises(m.IdentityError): self.call()
        self.assertEqual(self.native.commands, []); self.assertEqual(self.store.writes, 0)
    def test_foreign_primary_gid_collision_is_not_hidden_by_missing_group(self):
        self.native.users['foreign'] = ['foreign', 'x', '25000', '24001', '', '/srv', '/bin/sh']
        with self.root(), self.assertRaises(m.IdentityError): self.call()
        self.assertEqual(self.native.commands, [])
    def test_failed_attempt_cannot_resume_from_nss_absence_or_repair_completed_steps(self):
        self.native.fail_once = 'harniqo-ipc'
        with self.root():
            with self.assertRaisesRegex(m.IdentityError, 'identity_provision_failed'): self.call()
            self.assertEqual([r['status'] for r in self.store.value['steps']], ['complete', 'complete', 'unconfirmed', 'new', 'new'])
            attempted = len(self.native.commands)
            with self.assertRaisesRegex(m.IdentityError, 'identity_creation_unconfirmed'): self.call()
        self.assertEqual(len(self.native.commands), attempted)
        self.assertFalse(any(a[0].endswith(('userdel', 'groupdel', 'usermod')) for a in self.native.commands))
    def test_interruption_before_dispatch_can_continue_only_new_unattempted_steps(self):
        original = self.native.snapshot
        def interrupted(plan):
            if self.store.value is not None and [r['status'] for r in self.store.value['steps']][:3] == ['complete', 'complete', 'new']:
                raise KeyboardInterrupt()
            return original(plan)
        with self.root(), patch.object(m, '_snapshot', interrupted), self.assertRaises(KeyboardInterrupt): self.call()
        self.assertEqual(len(self.native.commands), 2)
        self.assertEqual([r['status'] for r in self.store.value['steps']], ['complete', 'complete', 'new', 'new', 'new'])
        with self.root(): self.assertTrue(self.call()['verified'])
        self.assertEqual(len(self.native.commands), 5)
    def test_effect_then_lost_reply_stays_unconfirmed_without_adoption_or_retry(self):
        self.native.lost_reply = 'harniqo-ipc'
        with self.root():
            with self.assertRaises(m.IdentityError): self.call()
            self.assertEqual(self.store.value['steps'][2]['status'], 'unconfirmed')
            count = len(self.native.commands)
            with self.assertRaisesRegex(m.IdentityError, 'identity_creation_unconfirmed'): self.call()
        self.assertEqual(len(self.native.commands), count)
    def test_pending_crash_intent_blocks_reentry_even_when_identity_is_absent(self):
        with self.root(): self.call()
        self.store.value['steps'][0]['status'] = 'pending'; self.native.groups.clear(); count = len(self.native.commands)
        with self.root(), self.assertRaisesRegex(m.IdentityError, 'identity_creation_unconfirmed'): self.call()
        self.assertEqual(len(self.native.commands), count)
    def test_managed_account_drift_is_rejected_without_repair(self):
        with self.root(): self.call()
        self.native.shadow['harniqo-broker'] = '$6$example_not_locked'; count = len(self.native.commands)
        with self.root(), self.assertRaisesRegex(m.IdentityError, 'identity_password_unlocked'): self.call()
        self.assertEqual(len(self.native.commands), count)
    def test_managed_extra_group_drift_is_rejected_without_changing_membership(self):
        with self.root(): self.call()
        self.native.groups['docker'] = ['docker', 'x', '998', 'harniqo-broker']; count = len(self.native.commands)
        with self.root(), self.assertRaises(m.IdentityError): self.call('check')
        self.assertEqual(len(self.native.commands), count)
    def test_check_without_receipt_is_read_only(self):
        with self.root(), self.assertRaisesRegex(m.IdentityError, 'identity_not_provisioned'): self.call('check')
        self.assertEqual(self.store.writes, 0); self.assertEqual(self.native.commands, []); self.assertEqual(self.lock_modes, [False])
    def test_receipt_mismatch_refuses_reusing_managed_identities(self):
        with self.root(): self.call()
        self.store.value['plan_sha256'] = '0' * 64; count = len(self.native.commands)
        with self.root(), self.assertRaisesRegex(m.IdentityError, 'identity_receipt_mismatch'): self.call()
        self.assertEqual(len(self.native.commands), count)
    def test_malformed_receipt_status_fails_finitely_before_mutation(self):
        with self.root(): self.call()
        self.store.value['steps'][0]['status'] = {}; count = len(self.native.commands)
        with self.root(), self.assertRaisesRegex(m.IdentityError, 'identity_receipt_mismatch'): self.call()
        self.assertEqual(len(self.native.commands), count)
    def test_nonroot_check_is_diagnostic_without_shadow_or_state_access(self):
        with patch.object(m.os, 'getuid', return_value=1000), patch.object(m.os, 'geteuid', return_value=1000), patch.object(m.os, 'getgid', return_value=1000), patch.object(m.os, 'getegid', return_value=1000), patch.object(m, '_read', return_value=self.data), patch.object(m, '_snapshot', return_value=([], [])), patch.object(m, '_locked') as locked:
            report = self.call('check')
            self.assertFalse(report['verified']); self.assertTrue(report['privileged_verification_required']); locked.assert_not_called()
            with self.assertRaisesRegex(m.IdentityError, 'identity_root_required'): self.call()
    def test_plan_schema_and_role_scopes_are_strict(self):
        cases = [b'{}', b'{"schema_version":1,"schema_version":1}', b'null', b'x' * 32769]
        for edit in ({'extra': True}, {'schema_version': True}, {'broker_account': {'name': 'root', 'uid': 1, 'gid': 1}}, {'ipc_group': {'name': 'harniqo-ipc', 'gid': True}}, {'ipc_group': {'name': 'harniqo-ipc', 'gid': 24001}}, {'supervisor_account': {'name': 'harniqo-supervisor', 'uid': 24001, 'gid': 24002}}):
            cases.append(json.dumps({**PLAN, **edit}).encode())
        for body in cases:
            with self.subTest(body=body[:50]), self.assertRaises(m.IdentityError): m.parse_plan(body)
    def test_digest_and_root_source_trust_are_required_before_mutation(self):
        with self.root(), patch.object(m, '_read', return_value=b'{}'), self.assertRaisesRegex(m.IdentityError, 'identity_digest_mismatch'): self.call()
        with self.root(), patch.object(m, '_ancestors', side_effect=m.IdentityError('identity_path_untrusted')), self.assertRaisesRegex(m.IdentityError, 'identity_path_untrusted'): self.call()
        self.assertEqual(self.native.commands, [])
    def test_actual_input_symlink_and_writable_file_are_refused(self):
        with tempfile.TemporaryDirectory(dir=Path(tempfile.gettempdir()).resolve()) as directory:
            path = Path(directory) / 'plan.json'; path.write_bytes(self.data); path.chmod(0o600)
            self.assertEqual(m._read(path, 32768), self.data)
            link = Path(directory) / 'link'; link.symlink_to(path)
            with self.assertRaises(m.IdentityError): m._read(link, 32768)
            path.chmod(0o666)
            with self.assertRaises(m.IdentityError): m._read(path, 32768)
    def test_dangling_receipt_symlink_cannot_be_adopted(self):
        with tempfile.TemporaryDirectory(dir=Path(tempfile.gettempdir()).resolve()) as directory:
            root = Path(directory); (root / 'receipt.json').symlink_to(root / 'missing')
            with self.assertRaisesRegex(m.IdentityError, 'identity_receipt_untrusted'): m._Store(root).read()
    def test_untrusted_parent_is_rejected_before_creating_a_state_directory(self):
        with tempfile.TemporaryDirectory(dir=Path(tempfile.gettempdir()).resolve()) as directory:
            target = Path(directory) / 'not-created'
            with self.assertRaises(m.IdentityError): m._directory(target, 0o700)
            self.assertFalse(target.exists())
    def test_new_directory_parent_names_are_synced_before_first_account_mutation(self):
        with tempfile.TemporaryDirectory(dir=Path(tempfile.gettempdir()).resolve()) as directory:
            base = Path(directory); state = base / 'harniqo' / 'identities'
            plan = base / 'plan.json'; plan.write_bytes(self.data); plan.chmod(0o600)
            syncs = []; first_mutation_syncs = []; real_sync = os.fsync
            def synced(fd):
                real_sync(fd); info = os.fstat(fd); syncs.append((info.st_dev, info.st_ino))
            def dispatched(args, **kwargs):
                if args[0] != m.GETENT and not first_mutation_syncs: first_mutation_syncs.extend(syncs)
                return self.native.run(args, **kwargs)
            previous = os.umask(0o077)
            try:
                with self.filesystem_root(state), patch.object(m.os, 'fsync', side_effect=synced), patch.object(m, '_run', dispatched):
                    result = m.execute(plan, expected_sha256=self.digest, action='apply')
            finally: os.umask(previous)
            self.assertTrue(result['verified'])
            for path in (base, state.parent, state):
                info = path.stat(); self.assertIn((info.st_dev, info.st_ino), first_mutation_syncs)
            self.assertEqual(stat.S_IMODE(state.parent.stat().st_mode), 0o755)
            self.assertEqual(stat.S_IMODE(state.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE((state / 'provision.lock').stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE((state / 'receipt.json').stat().st_mode), 0o600)
    def test_new_private_files_ignore_restrictive_umask_without_repairing_old_lock(self):
        with tempfile.TemporaryDirectory(dir=Path(tempfile.gettempdir()).resolve()) as directory:
            base = Path(directory); parent = base / 'harniqo'; parent.mkdir(mode=0o755); parent.chmod(0o755)
            state = parent / 'identities'; state.mkdir(mode=0o700); state.chmod(0o700)
            plan = base / 'plan.json'; plan.write_bytes(self.data); plan.chmod(0o600)
            previous = os.umask(0o777)
            try:
                with self.filesystem_root(state): result = m.execute(plan, expected_sha256=self.digest, action='apply')
            finally: os.umask(previous)
            self.assertTrue(result['verified'])
            self.assertEqual(stat.S_IMODE((state / 'provision.lock').stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE((state / 'receipt.json').stat().st_mode), 0o600)
            (state / 'provision.lock').chmod(0o640); attempted = len(self.native.commands)
            with self.filesystem_root(state), self.assertRaisesRegex(m.IdentityError, 'identity_lock_untrusted'):
                m.execute(plan, expected_sha256=self.digest, action='apply')
            self.assertEqual(stat.S_IMODE((state / 'provision.lock').stat().st_mode), 0o640)
            self.assertEqual(len(self.native.commands), attempted)
    def test_directory_sync_failure_prevents_all_identity_commands(self):
        with tempfile.TemporaryDirectory(dir=Path(tempfile.gettempdir()).resolve()) as directory:
            base = Path(directory); state = base / 'harniqo' / 'identities'
            plan = base / 'plan.json'; plan.write_bytes(self.data); plan.chmod(0o600)
            with self.filesystem_root(state), patch.object(m, '_sync_directory', side_effect=OSError('synthetic fsync failure')), self.assertRaises(OSError):
                m.execute(plan, expected_sha256=self.digest, action='apply')
            self.assertEqual(self.native.commands, [])
            self.assertFalse((state / 'receipt.json').exists())
    def test_reentry_syncs_existing_directory_parent_before_any_native_attempt(self):
        with tempfile.TemporaryDirectory(dir=Path(tempfile.gettempdir()).resolve()) as directory:
            base = Path(directory); state = base / 'harniqo' / 'identities'
            plan = base / 'plan.json'; plan.write_bytes(self.data); plan.chmod(0o600)
            original_sync = m._sync_directory; fail_parent = True; successful_parents = []; first_native = []
            def synced(path):
                if path == base and fail_parent: raise OSError('synthetic parent sync failure')
                original_sync(path); successful_parents.append(path)
            def dispatched(args, **kw):
                if args[0] != m.GETENT and not first_native: first_native.extend(successful_parents)
                return self.native.run(args, **kw)
            with self.filesystem_root(state), patch.object(m, '_sync_directory', side_effect=synced), patch.object(m, '_run', dispatched):
                with self.assertRaises(OSError): m.execute(plan, expected_sha256=self.digest, action='apply')
                self.assertTrue(state.parent.is_dir()); self.assertEqual(self.native.commands, [])
                self.assertEqual(stat.S_IMODE(state.parent.stat().st_mode), 0o755)
                # A previous mkdir survived, but its name was never durably synced.
                # Reusing it must not bypass that same failed durability barrier.
                with self.assertRaises(OSError): m.execute(plan, expected_sha256=self.digest, action='apply')
                self.assertEqual(self.native.commands, [])
                fail_parent = False
                result = m.execute(plan, expected_sha256=self.digest, action='apply')
                self.assertTrue(result['verified']); self.assertIn(base, first_native)
    def _unknown_broker_receipt(self, plan, state):
        # Actual file/receipt history, with only native account changes represented
        # by this deterministic NSS edge. The effect happened before reply loss.
        original = self.native.run
        def lost(args, **kwargs):
            value = original(args, **kwargs)
            if args[0] == '/usr/sbin/useradd' and args[-1] == 'harniqo-broker':
                raise m.IdentityError('identity_command_timeout')
            return value
        with patch.object(m, '_run', lost), self.assertRaisesRegex(m.IdentityError, 'identity_provision_failed'):
            m.execute(plan, expected_sha256=self.digest, action='apply')
        raw = (state / 'receipt.json').read_bytes()
        self.assertEqual([row['status'] for row in json.loads(raw)['steps']], ['complete', 'complete', 'complete', 'unconfirmed', 'new'])
        self.assertEqual(len(self.native.commands), 4)
        return raw
    def test_explicit_confirmation_keeps_original_and_audit_before_only_new_step_resumes(self):
        with tempfile.TemporaryDirectory(dir=Path(tempfile.gettempdir()).resolve()) as directory:
            base = Path(directory); state = base / 'harniqo' / 'identities'; plan = base / 'plan.json'
            plan.write_bytes(self.data); plan.chmod(0o600); self.native.include_primary = False
            with self.filesystem_root(state):
                raw = self._unknown_broker_receipt(plan, state); before = hashlib.sha256(raw).hexdigest()
                for action in ('apply', 'check'):
                    with self.assertRaisesRegex(m.IdentityError, 'identity_creation_unconfirmed'):
                        m.execute(plan, expected_sha256=self.digest, action=action)
                result = m.execute(plan, expected_sha256=self.digest, action='confirm-created', receipt_sha256=before, step='user:harniqo-broker')
                self.assertEqual(result['status'], 'confirmed'); self.assertFalse(result['verified'])
                self.assertEqual(len(self.native.commands), 4)
                expected = json.loads(raw); expected['steps'][3]['status'] = 'complete'
                self.assertEqual((state / 'receipt.json').read_bytes(), m._encoded(expected))
                backup = state / result['backup_file']; audit = state / result['audit_file']
                self.assertEqual(backup.read_bytes(), raw)
                record = json.loads(audit.read_bytes())
                self.assertEqual(record['action'], 'confirm-created'); self.assertEqual(record['step'], {'kind': 'user', 'name': 'harniqo-broker'})
                self.assertEqual(record['receipt_sha256_before'], before)
                self.assertEqual(record['receipt_sha256_after'], hashlib.sha256(m._encoded(expected)).hexdigest())
                self.assertEqual(record['plan_sha256'], self.digest)
                self.assertEqual((record['operator_real_uid'], record['operator_effective_uid'], record['operator_gid']), (0, 0, 0))
                self.assertEqual(result['audit_sha256'], hashlib.sha256(audit.read_bytes()).hexdigest())
                for path in (backup, audit): self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                self.assertTrue(m.execute(plan, expected_sha256=self.digest, action='apply')['verified'])
                self.assertEqual(len(self.native.commands), 5)
                self.assertEqual(self.native.commands[-1][0:1] + self.native.commands[-1][-1:], ['/usr/sbin/useradd', 'harniqo-supervisor'])
                self.assertEqual(backup.read_bytes(), raw)
    def test_confirmation_stale_receipt_hash_changes_neither_files_nor_nss(self):
        with tempfile.TemporaryDirectory(dir=Path(tempfile.gettempdir()).resolve()) as directory:
            base = Path(directory); state = base / 'harniqo' / 'identities'; plan = base / 'plan.json'
            plan.write_bytes(self.data); plan.chmod(0o600)
            with self.filesystem_root(state):
                raw = self._unknown_broker_receipt(plan, state); files = sorted(p.name for p in state.iterdir())
                with self.assertRaisesRegex(m.IdentityError, 'identity_receipt_changed'):
                    m.execute(plan, expected_sha256=self.digest, action='confirm-created', receipt_sha256='0' * 64, step='user:harniqo-broker')
                self.assertEqual((state / 'receipt.json').read_bytes(), raw)
                self.assertEqual(sorted(p.name for p in state.iterdir()), files); self.assertEqual(len(self.native.commands), 4)
    def test_confirmation_refuses_mismatched_native_account_without_audit_or_repair(self):
        with tempfile.TemporaryDirectory(dir=Path(tempfile.gettempdir()).resolve()) as directory:
            base = Path(directory); state = base / 'harniqo' / 'identities'; plan = base / 'plan.json'
            plan.write_bytes(self.data); plan.chmod(0o600)
            with self.filesystem_root(state):
                raw = self._unknown_broker_receipt(plan, state)
                self.native.users['harniqo-broker'][6] = '/bin/sh'
                with self.assertRaisesRegex(m.IdentityError, 'managed_identity_drift'):
                    m.execute(plan, expected_sha256=self.digest, action='confirm-created', receipt_sha256=hashlib.sha256(raw).hexdigest(), step='user:harniqo-broker')
                self.assertEqual((state / 'receipt.json').read_bytes(), raw); self.assertEqual(len(self.native.commands), 4)
                self.assertEqual(list(state.glob('confirm-created-*')), [])
    def test_confirmation_is_root_only_and_cannot_be_smuggled_into_apply(self):
        with patch.object(m.os, 'getuid', return_value=1000), patch.object(m.os, 'geteuid', return_value=1000), patch.object(m.os, 'getgid', return_value=1000), patch.object(m.os, 'getegid', return_value=1000), patch.object(m, '_locked') as locked:
            with self.assertRaisesRegex(m.IdentityError, 'identity_root_required'):
                self.call('confirm-created', receipt_sha256='0' * 64, step='user:harniqo-broker')
            locked.assert_not_called()
        with self.root(), self.assertRaisesRegex(m.IdentityError, 'identity_arguments_invalid'):
            self.call('apply', receipt_sha256='0' * 64, step='user:harniqo-broker')
        self.assertEqual(self.native.commands, [])
    def test_confirmation_repeated_completed_step_is_refused(self):
        with tempfile.TemporaryDirectory(dir=Path(tempfile.gettempdir()).resolve()) as directory:
            base = Path(directory); state = base / 'harniqo' / 'identities'; plan = base / 'plan.json'
            plan.write_bytes(self.data); plan.chmod(0o600)
            with self.filesystem_root(state):
                raw = self._unknown_broker_receipt(plan, state)
                result = m.execute(plan, expected_sha256=self.digest, action='confirm-created', receipt_sha256=hashlib.sha256(raw).hexdigest(), step='user:harniqo-broker')
                files = sorted(p.name for p in state.iterdir()); current = (state / 'receipt.json').read_bytes()
                with self.assertRaisesRegex(m.IdentityError, 'identity_confirmation_invalid'):
                    m.execute(plan, expected_sha256=self.digest, action='confirm-created', receipt_sha256=result['receipt_sha256_after'], step='user:harniqo-broker')
                self.assertEqual((state / 'receipt.json').read_bytes(), current)
                self.assertEqual(sorted(p.name for p in state.iterdir()), files); self.assertEqual(len(self.native.commands), 4)
    def test_confirmation_refuses_more_than_one_pending_ordered_step(self):
        with tempfile.TemporaryDirectory(dir=Path(tempfile.gettempdir()).resolve()) as directory:
            base = Path(directory); state = base / 'harniqo' / 'identities'; plan = base / 'plan.json'
            plan.write_bytes(self.data); plan.chmod(0o600)
            with self.filesystem_root(state):
                raw = self._unknown_broker_receipt(plan, state); value = json.loads(raw)
                value['steps'][4]['status'] = 'pending'; m._Store(state).write(value)
                raw = (state / 'receipt.json').read_bytes()
                with self.assertRaisesRegex(m.IdentityError, 'identity_confirmation_invalid'):
                    m.execute(plan, expected_sha256=self.digest, action='confirm-created', receipt_sha256=hashlib.sha256(raw).hexdigest(), step='user:harniqo-broker')
                self.assertEqual((state / 'receipt.json').read_bytes(), raw); self.assertEqual(list(state.glob('confirm-created-*')), [])
    def test_confirmation_audit_failure_keeps_unknown_receipt_and_durable_original_backup(self):
        with tempfile.TemporaryDirectory(dir=Path(tempfile.gettempdir()).resolve()) as directory:
            base = Path(directory); state = base / 'harniqo' / 'identities'; plan = base / 'plan.json'
            plan.write_bytes(self.data); plan.chmod(0o600)
            with self.filesystem_root(state):
                raw = self._unknown_broker_receipt(plan, state); original = m._Store.exclusive
                def failed(store, name, data):
                    if name.endswith('.audit.json'): raise OSError('synthetic audit failure')
                    return original(store, name, data)
                with patch.object(m._Store, 'exclusive', failed), self.assertRaises(OSError):
                    m.execute(plan, expected_sha256=self.digest, action='confirm-created', receipt_sha256=hashlib.sha256(raw).hexdigest(), step='user:harniqo-broker')
                self.assertEqual((state / 'receipt.json').read_bytes(), raw)
                backups = list(state.glob('confirm-created-*.before.json')); self.assertEqual(len(backups), 1)
                self.assertEqual(backups[0].read_bytes(), raw); self.assertEqual(stat.S_IMODE(backups[0].stat().st_mode), 0o600)
                self.assertEqual(len(self.native.commands), 4)
    def test_confirmation_rechecks_receipt_cas_after_audit_before_replacement(self):
        with tempfile.TemporaryDirectory(dir=Path(tempfile.gettempdir()).resolve()) as directory:
            base = Path(directory); state = base / 'harniqo' / 'identities'; plan = base / 'plan.json'
            plan.write_bytes(self.data); plan.chmod(0o600)
            with self.filesystem_root(state):
                raw = self._unknown_broker_receipt(plan, state); original = m._Store.confirmation_files
                def changed(store, before, audit):
                    result = original(store, before, audit)
                    (state / 'receipt.json').write_bytes(raw + b' ')
                    return result
                with patch.object(m._Store, 'confirmation_files', changed), self.assertRaisesRegex(m.IdentityError, 'identity_receipt_changed'):
                    m.execute(plan, expected_sha256=self.digest, action='confirm-created', receipt_sha256=hashlib.sha256(raw).hexdigest(), step='user:harniqo-broker')
                self.assertEqual((state / 'receipt.json').read_bytes(), raw + b' '); self.assertEqual(len(self.native.commands), 4)
    def _hidden_native_effect(self, mode):
        real_run = m._run; original_popen = subprocess.Popen; children = []
        def started(*args, **kwargs):
            child = original_popen(*args, **kwargs); children.append(child); return child
        with tempfile.TemporaryDirectory(dir=Path(tempfile.gettempdir()).resolve()) as directory:
            hidden = Path(directory) / 'effect-not-visible-in-nss'
            code = 'from pathlib import Path;Path(' + repr(str(hidden)) + ').write_text("partial")\n'
            code += 'import time;time.sleep(30)' if mode == 'timeout' else 'import sys;sys.stdout.write("x"*8192);sys.stdout.flush()'
            def dispatch(args, **kw):
                if args[0] != m.GETENT and args[-1] == 'harniqo-ipc':
                    self.native.commands.append(list(args))
                    with patch.object(m, 'COMMAND_SECONDS', 1), patch.object(m, 'LIMIT', 1024):
                        return real_run([sys.executable, '-B', '-c', code])
                return self.native.run(args, **kw)
            with self.root(), patch.object(m, '_run', dispatch), patch.object(m.subprocess, 'Popen', side_effect=started):
                with self.assertRaisesRegex(m.IdentityError, 'identity_provision_failed'): self.call()
                self.assertTrue(hidden.exists())
                self.assertNotIn('harniqo-ipc', self.native.groups)
                self.assertEqual(self.store.value['steps'][2]['status'], 'unconfirmed')
                attempted = len(self.native.commands)
                with self.assertRaisesRegex(m.IdentityError, 'identity_creation_unconfirmed'): self.call()
                self.assertEqual(len(self.native.commands), attempted)
            self.assertEqual(len(children), 1); self.assertIsNotNone(children[0].poll())
    def test_real_timeout_with_nss_absence_keeps_hidden_effect_unconfirmed(self):
        self._hidden_native_effect('timeout')
    def test_real_output_limit_with_nss_absence_keeps_hidden_effect_unconfirmed(self):
        self._hidden_native_effect('output')
    def test_actual_native_output_and_timeout_bounds_reap_owned_children(self):
        children = []; original = subprocess.Popen
        def started(*args, **kwargs):
            child = original(*args, **kwargs); children.append(child); return child
        with patch.object(m.subprocess, 'Popen', side_effect=started):
            with patch.object(m, 'LIMIT', 1024), self.assertRaisesRegex(m.IdentityError, 'identity_command_output_limit'):
                m._run([sys.executable, '-B', '-c', 'import sys;sys.stdout.write("x"*8192)'])
            with patch.object(m, 'COMMAND_SECONDS', .1), self.assertRaisesRegex(m.IdentityError, 'identity_command_timeout'):
                m._run([sys.executable, '-B', '-c', 'import time;time.sleep(10)'])
        self.assertEqual(len(children), 2); self.assertTrue(all(p.poll() is not None for p in children))


if __name__ == '__main__': unittest.main()
