"""Host filesystem guardrails only; no VM, native libvirt, model or privilege use."""
from pathlib import Path
import fcntl
import json
import os
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

import qualify_resource_broker as operator


class GuestBootstrapTests(unittest.TestCase):
    def test_sealed_probe_runs_early_without_a_second_final_stage_invocation(self):
        instance = {'uuid': 'instance-a', 'nonce': 'nonce-a'}
        other = {'uuid': 'instance-b'}
        root = Path('/qualification/workspace')
        original = json.loads(operator.kvm.guest_config(instance, other, root, operator.DISK_BYTES).split('\n', 1)[1])
        config = json.loads(operator.sealed_guest_config(instance, other, root).split('\n', 1)[1])
        self.assertEqual(config['bootcmd'], [['/usr/bin/python3', '-c', original['write_files'][0]['content']]])
        self.assertNotIn('runcmd', config)
        self.assertNotIn('write_files', config)
        for field in ('package_update', 'package_upgrade', 'resize_rootfs'):
            self.assertIs(config[field], False)


    def test_limited_guest_fixture_keeps_sealed_checks_and_separates_load_from_heartbeat(self):
        instance = {'uuid': 'instance-a', 'nonce': 'nonce-a'}
        other = {'uuid': 'instance-b'}
        root = Path('/qualification/workspace')
        original = json.loads(operator.kvm.guest_config(instance, other, root, operator.DISK_BYTES).split('\n', 1)[1])['write_files'][0]['content']
        programs = []
        for pressure in (True, False):
            config = json.loads(operator.sealed_guest_config(instance, other, root,
                resource_profile='sealed-raw8g-q35-limited-v2', pressure=pressure).split('\n', 1)[1])
            program = config['bootcmd'][0][2]
            self.assertTrue(program.startswith(original))
            compile(program, '<sealed-resource-guest>', 'exec')
            self.assertNotIn('runcmd', config)
            programs.append(program[len(original):])
        self.assertIn('while time.monotonic()<until: pass', programs[0])
        self.assertNotIn('HARNIQO-HEARTBEAT', programs[0])
        self.assertIn('HARNIQO-HEARTBEAT', programs[1])
        self.assertIn("re.fullmatch(b'[0-9a-f]{40}',line)", programs[1])
        self.assertIn('select.select', programs[1])


class WorkerLogCleanupTests(unittest.TestCase):
    def test_sync_failure_is_reported_without_skipping_other_log_closure(self):
        logs = [tempfile.TemporaryFile(mode='w+'), tempfile.TemporaryFile(mode='w+')]
        for log in logs:
            self.addCleanup(log.close)
            log.write('private diagnostic content')
        with patch.object(operator.os, 'fsync', side_effect=[OSError('private filesystem detail'), None]) as sync:
            errors = operator.close_worker_logs(logs)
        self.assertTrue(all(log.closed for log in logs))
        self.assertEqual(sync.call_count, 2)
        self.assertEqual(errors, [{'phase': 'worker_log_sync', 'type': 'OSError'}])


class CoordinationPathTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='hbro-', dir=Path('/private/tmp' if sys.platform == 'darwin' else '/tmp').resolve())
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.state = self.root / '.state'; self.state.mkdir(mode=0o775); self.state.chmod(0o775)
        self.legacy = self.state / 'qualification'; self.legacy.mkdir(mode=0o775); self.legacy.chmod(0o775)
        self.lock = self.legacy / 'kvm.lock'; self.lock.write_bytes(b'coordination-only\n'); self.lock.chmod(0o664)

    def test_legacy_shared_modes_are_kept_without_becoming_private(self):
        descriptor = operator.coordination_lock(self.root)
        self.addCleanup(os.close, descriptor)
        self.assertEqual(stat.S_IMODE(self.legacy.stat().st_mode), 0o775)
        self.assertEqual(stat.S_IMODE(self.lock.stat().st_mode), 0o664)
        self.assertEqual(self.lock.read_bytes(), b'coordination-only\n')
        with self.assertRaisesRegex(RuntimeError, 'Private directory'):
            operator.private_directory(self.legacy)
        competing = os.open(self.lock, os.O_RDWR)
        try:
            with self.assertRaises(BlockingIOError):
                fcntl.flock(competing, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(competing)

    def test_private_authority_parent_is_separate_and_0700(self):
        private = self.state / 'resource-broker-qualification'
        operator.private_directory(private, create=True)
        operator.private_directory(private / 'rb-task', create=True)
        self.assertEqual(stat.S_IMODE(private.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((private / 'rb-task').stat().st_mode), 0o700)
        self.assertNotEqual(private, self.legacy)
        self.assertEqual(stat.S_IMODE(self.legacy.stat().st_mode), 0o775)

    def test_existing_public_authority_parent_is_rejected_without_chmod(self):
        private = self.state / 'resource-broker-qualification'; private.mkdir(mode=0o775); private.chmod(0o775)
        with self.assertRaisesRegex(RuntimeError, 'Private directory'):
            operator.private_directory(private, create=True)
        self.assertEqual(stat.S_IMODE(private.stat().st_mode), 0o775)

    def test_existing_qemu_traverse_parent_does_not_weaken_authority(self):
        runtime = self.state / 'runtime'; runtime.mkdir(mode=0o700); runtime.chmod(0o710)
        acl = 'user::rwx\nuser:64055:--x\ngroup::---\nmask::--x\nother::---\n'
        with patch.object(operator.pwd, 'getpwnam', return_value=SimpleNamespace(pw_uid=64055)), \
                patch.object(operator.kvm, 'run', return_value=acl):
            operator.runtime_directory(runtime, create=True)
        self.assertEqual(stat.S_IMODE(runtime.stat().st_mode), 0o710)
        with self.assertRaisesRegex(RuntimeError, 'Private directory'):
            operator.private_directory(runtime)

    def test_runtime_parent_rejects_unsafe_acl_principals(self):
        runtime = self.state / 'runtime'; runtime.mkdir(mode=0o700); runtime.chmod(0o710)
        for acl in (
            'user::rwx\nuser:64056:--x\ngroup::---\nmask::--x\nother::---\n',
            'user::rwx\ngroup::--x\nmask::--x\nother::---\n',
            'user::rwx\nuser:64055:--x\nuser:1234:--x\ngroup::---\nmask::--x\nother::---\n',
        ):
            with self.subTest(acl=acl), \
                    patch.object(operator.pwd, 'getpwnam', return_value=SimpleNamespace(pw_uid=64055)), \
                    patch.object(operator.kvm, 'run', return_value=acl):
                with self.assertRaisesRegex(RuntimeError, 'named QEMU traverse'):
                    operator.runtime_directory(runtime, create=True)
        self.assertEqual(stat.S_IMODE(runtime.stat().st_mode), 0o710)

    def test_shared_runtime_mode_is_rejected_without_chmod(self):
        with self.assertRaisesRegex(RuntimeError, 'Runtime directory permissions'):
            operator.runtime_directory(self.legacy, create=True)
        self.assertEqual(stat.S_IMODE(self.legacy.stat().st_mode), 0o775)

    def test_symlink_runtime_parent_is_rejected(self):
        runtime = self.state / 'runtime'; runtime.symlink_to(self.legacy, target_is_directory=True)
        with self.assertRaisesRegex(RuntimeError, 'Symlink'):
            operator.runtime_directory(runtime, create=True)
        self.assertEqual(stat.S_IMODE(self.legacy.stat().st_mode), 0o775)


    def test_symlink_coordination_directory_is_rejected(self):
        retained = self.state / 'retained'
        self.legacy.rename(retained); self.legacy.symlink_to(retained, target_is_directory=True)
        with self.assertRaisesRegex(RuntimeError, 'Symlink'):
            operator.coordination_lock(self.root)
        self.assertEqual((retained / 'kvm.lock').read_bytes(), b'coordination-only\n')
        self.assertEqual(stat.S_IMODE(retained.stat().st_mode), 0o775)

    def test_symlink_coordination_lock_is_rejected(self):
        target = self.state / 'untouched'; target.write_bytes(b'do-not-change'); target.chmod(0o600)
        self.lock.unlink(); self.lock.symlink_to(target)
        with self.assertRaisesRegex(RuntimeError, 'Symlink'):
            operator.coordination_lock(self.root)
        self.assertEqual(target.read_bytes(), b'do-not-change')
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)

    def test_hardlinked_coordination_lock_is_rejected(self):
        extra = self.state / 'second-link'; os.link(self.lock, extra)
        with self.assertRaisesRegex(RuntimeError, 'coordination lock'):
            operator.coordination_lock(self.root)
        self.assertEqual(self.lock.stat().st_nlink, 2)
        self.assertEqual(extra.read_bytes(), b'coordination-only\n')

    def test_nonregular_coordination_lock_is_rejected(self):
        self.lock.unlink(); os.mkfifo(self.lock, 0o600)
        with self.assertRaisesRegex(RuntimeError, 'coordination lock'):
            operator.coordination_lock(self.root)
        self.assertTrue(stat.S_ISFIFO(self.lock.lstat().st_mode))

    def test_foreign_owner_coordination_path_is_rejected(self):
        uid = os.geteuid()
        with patch.object(operator.os, 'geteuid', return_value=uid + 1):
            with self.assertRaisesRegex(RuntimeError, 'coordination directory'):
                operator.coordination_lock(self.root)
        self.assertEqual(stat.S_IMODE(self.legacy.stat().st_mode), 0o775)
        self.assertEqual(stat.S_IMODE(self.lock.stat().st_mode), 0o664)

    def test_replaced_coordination_lock_cannot_claim_unchanged(self):
        descriptor = operator.coordination_lock(self.root)
        self.addCleanup(os.close, descriptor)
        original = self.legacy / 'original.lock'; self.lock.rename(original)
        self.lock.write_bytes(b'new-coordinator'); self.lock.chmod(0o664)
        with self.assertRaisesRegex(RuntimeError, 'lock was replaced'):
            operator.verify_coordination_lock(self.root, descriptor)
        self.assertEqual(original.read_bytes(), b'coordination-only\n')
        self.assertEqual(self.lock.read_bytes(), b'new-coordinator')

    def test_foreign_owned_base_still_requires_an_independent_owned_copy(self):
        (self.root / 'images/vm/base').mkdir(parents=True)
        (self.root / 'images/README.md').write_text('fixture')
        base = self.root / 'images/vm/base' / ('a' * 64 + '.qcow2')
        base.write_bytes(b'base'); base.chmod(0o444)
        uid = os.geteuid()
        with patch.object(operator.os, 'geteuid', return_value=uid + 1), patch.object(operator.kvm, 'run') as command:
            with self.assertRaisesRegex(RuntimeError, 'Base must be owned'):
                operator.check_base(self.root, base)
            command.assert_not_called()
        self.assertEqual(base.read_bytes(), b'base')
        self.assertEqual(stat.S_IMODE(base.stat().st_mode), 0o444)


if __name__ == '__main__':
    unittest.main()
