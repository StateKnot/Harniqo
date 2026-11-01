#!/usr/bin/env python3
"""Offline deployment admission, filesystem safety and rendered polkit behavior."""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import selectors
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
SPEC = importlib.util.spec_from_file_location('prepare_broker_service', REPO / 'scripts/deploy/prepare_broker_service.py')
deployment = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(deployment)


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        # Canonical macOS /private/tmp avoids the system's /tmp -> /private/tmp symlink.
        self.temp = tempfile.TemporaryDirectory(dir=Path(tempfile.gettempdir()).resolve())
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.configuration = {
            'schema_version': 1, 'broker_identity': {'uid': 24001, 'gid': 24001},
            'state_dir': '/var/lib/harniqo/broker-state', 'images_root': '/srv/design/images',
            'channel_root': '/run/harniqo/channels', 'ipc_directory': '/run/harniqo/broker-ipc',
            'issuer_key_file': '/var/lib/harniqo/keys/issuer.key',
            'lease': {'max_seconds': 300, 'max_clock_skew_seconds': 5},
            'templates': [{
                'template_id': 'sealed-a', 'resource_id': '00000000-0000-4000-8000-000000000001',
                'name': 'harniqo-a', 'disk': '/srv/design/images/a.raw', 'seed': '/srv/design/images/a.seed',
                'channel': '/run/harniqo/channels/a.report', 'console': '/run/harniqo/channels/a.console',
                'source_base_sha256': 'a' * 64, 'resource_profile': 'sealed-raw8g-q35-limited-v2'}],
            'peers': [{'uid': 24002, 'gid': 24002, 'resources': [{
                'resource_id': '00000000-0000-4000-8000-000000000001', 'organization_id': 'org-a',
                'workspace_id': 'workspace-a', 'operations': ['ensure', 'renew', 'observe', 'revoke']}]}]}
        self.config_path = self.root / 'source-config.json'
        self.plan_path = self.root / 'plan.json'
        self.output = self.root / 'bundle'; self.output.mkdir(mode=0o700)
        self.plan = {'schema_version': 1, 'configuration_path': str(self.config_path),
            'configuration_sha256': '', 'release_root': '/opt/harniqo/releases/m005-0123456789ab',
            'broker_account': {'name': 'harniqo-broker', 'uid': 24001, 'gid': 24001},
            'supervisor_account': {'name': 'harniqo-supervisor', 'uid': 24002, 'gid': 24002},
            'ipc_group': {'name': 'harniqo-ipc', 'gid': 24003},
            'libvirt_acl_attestation': {'status': 'unverified'}}
        self.write()

    def write(self):
        data = json.dumps(self.configuration).encode()
        self.config_path.write_bytes(data); self.config_path.chmod(0o600)
        self.plan['configuration_sha256'] = hashlib.sha256(data).hexdigest()
        self.write_plan()

    def write_plan(self):
        self.plan_path.write_bytes(json.dumps(self.plan).encode()); self.plan_path.chmod(0o600)

    def reject(self):
        with self.assertRaises(deployment.DeploymentError):
            deployment.prepare_bundle(self.plan_path, self.output)
        self.assertEqual(list(self.output.iterdir()), [])

    def bundle(self):
        return deployment.prepare_bundle(self.plan_path, self.output)

    def test_bundle_is_pinned_private_blocked_and_no_key_material_read(self):
        # No configured production path exists in this local fixture. Rendering remains offline.
        manifest = self.bundle()
        self.assertEqual(manifest['status'], 'BLOCKED')
        self.assertEqual(manifest['polkit']['candidate'], 'candidate_not_authorized')
        self.assertIsNone(manifest['polkit']['candidate_install_destination'])
        self.assertEqual(len(list(self.output.iterdir())), 9)
        self.assertEqual((self.output / 'resource-broker.json').read_bytes(), self.config_path.read_bytes())
        for record in manifest['files']:
            path = self.output / record['name']
            self.assertEqual(record['sha256'], hashlib.sha256(path.read_bytes()).hexdigest())
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(json.loads((self.output / 'manifest.json').read_bytes()), manifest)

    def test_candidate_is_not_installable_and_active_policy_only_denies(self):
        self.bundle()
        active = (self.output / '00-harniqo-libvirt.rules').read_text()
        candidate = (self.output / '00-harniqo-libvirt.rules.candidate').read_text()
        self.assertNotIn('polkit.Result.YES', active)
        self.assertIn('polkit.Result.NO', active)
        self.assertIn("action.id === 'org.libvirt.unix.manage'", candidate)
        self.assertIn("action.lookup('domain_uuid') === domains[i][0] &&", candidate)
        self.assertIn("action.lookup('domain_name') === domains[i][1]", candidate)
        permissions = json.loads((self.output / 'required-libvirt-permissions.json').read_bytes())
        self.assertEqual(set(permissions['connect_actions']), {
            'org.libvirt.api.connect.getattr', 'org.libvirt.api.connect.search-domains'})
        self.assertEqual(set(permissions['domain_actions']), {
            'org.libvirt.api.domain.getattr', 'org.libvirt.api.domain.read',
            'org.libvirt.api.domain.write', 'org.libvirt.api.domain.start', 'org.libvirt.api.domain.stop'})

    @unittest.skipUnless(shutil.which('node'), 'Node is required to execute actual generated rule functions')
    def test_native_javascript_rule_behavior(self):
        second = copy.deepcopy(self.configuration['templates'][0])
        second.update(template_id='sealed-b', resource_id='00000000-0000-4000-8000-000000000002',
                      name='harniqo-b', disk='/srv/design/images/b.raw', seed='/srv/design/images/b.seed',
                      channel='/run/harniqo/channels/b.report', console='/run/harniqo/channels/b.console')
        self.configuration['templates'].append(second)
        self.write()
        self.bundle()
        script = r'''
const fs = require('node:fs'); const vm = require('node:vm'); const assert = require('node:assert/strict');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
function compile(text) { let fn; vm.runInNewContext(text, {polkit:{Result:{YES:'yes',NO:'no'},addRule:f=>fn=f}}); return fn; }
function check(fn,user,id,details={}) { return fn({id,lookup:k=>details[k]}, {user}); }
const candidate=compile(input.candidate), deny=compile(input.deny);
const broker='harniqo-broker', supervisor='harniqo-supervisor';
const a={connect_driver:'QEMU',domain_uuid:'00000000-0000-4000-8000-000000000001',domain_name:'harniqo-a'};
assert.equal(check(candidate,broker,'org.libvirt.unix.manage'),'yes');
const b={connect_driver:'QEMU',domain_uuid:'00000000-0000-4000-8000-000000000002',domain_name:'harniqo-b'};
assert.equal(check(candidate,broker,'org.libvirt.api.domain.start',b),'yes');
assert.equal(check(candidate,broker,'org.libvirt.api.domain.start',{...a,domain_name:b.domain_name}),'no');
assert.equal(check(candidate,broker,'org.libvirt.api.domain.start',{...b,domain_name:a.domain_name}),'no');
for (const op of ['getattr','read','write','start','stop']) {
  assert.equal(check(candidate,broker,'org.libvirt.api.domain.'+op,a),'yes');
  for (const wrong of [{...a,domain_uuid:'foreign'},{...a,domain_name:'foreign'},{...a,connect_driver:'LXC'},{}])
    assert.equal(check(candidate,broker,'org.libvirt.api.domain.'+op,wrong),'no');
}
for (const op of ['getattr','search-domains']) assert.equal(check(candidate,broker,'org.libvirt.api.connect.'+op,a),'yes');
for (const id of ['org.libvirt.api.domain.read-secure','org.libvirt.api.domain.delete',
 'org.libvirt.api.domain.open-device','org.libvirt.api.domain.migrate','org.libvirt.api.connect.write',
 'org.libvirt.api.connect.search-storage-pools','org.libvirt.api.secret.getattr','org.libvirt.unix.monitor',
 'org.libvirt.api.future.new-action']) assert.equal(check(candidate,broker,id,a),'no');
for (const id of ['org.libvirt.unix.manage','org.libvirt.unix.monitor','org.libvirt.api.domain.read',
 'org.libvirt.api.domain.stop','org.libvirt.api.future.permission']) {
  assert.equal(check(candidate,supervisor,id,a),'no');
  assert.equal(check(deny,broker,id,a),'no'); assert.equal(check(deny,supervisor,id,a),'no');
  assert.equal(check(candidate,'unrelated-account',id,a),undefined);
  assert.equal(check(deny,'unrelated-account',id,a),undefined);
}
assert.equal(check(candidate,broker,'org.example.unrelated',a),undefined);
process.stdout.write('native-rule-behavior-passed\n');
'''
        data = {'candidate': (self.output / '00-harniqo-libvirt.rules.candidate').read_text(),
                'deny': (self.output / '00-harniqo-libvirt.rules').read_text()}
        result = subprocess.run([shutil.which('node'), '-e', script], input=json.dumps(data),
                                text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, 'native-rule-behavior-passed\n')

    def test_units_preserve_watcher_role_and_process_observation(self):
        self.bundle()
        listener = (self.output / 'harniqo-broker.service').read_text()
        watcher = (self.output / 'harniqo-broker-watch.service').read_text()
        self.assertIn('Requires=harniqo-broker-watch.service', listener)
        self.assertIn('After=harniqo-broker-watch.service', listener)
        self.assertIn('Before=harniqo-broker.service', watcher)
        for unit in (listener, watcher):
            for expected in ('Type=exec', 'User=harniqo-broker', 'Group=harniqo-broker',
                'NoNewPrivileges=yes', 'CapabilityBoundingSet=\n', 'AmbientCapabilities=\n',
                'PrivateUsers=no', 'ProtectProc=default', 'ProcSubset=all', 'ProtectControlGroups=yes',
                'RestrictAddressFamilies=AF_UNIX', 'ProtectHome=no', '--sha256 ' + self.plan['configuration_sha256']):
                self.assertIn(expected, unit)
            for forbidden in ('WatchdogSec=', 'RuntimeDirectory=', 'ExecStartPre=', 'initialize',
                              'PrivatePIDs=', 'Delegate=yes', 'User=root', 'sudo', '.socket'):
                self.assertNotIn(forbidden, unit)
        for forbidden in ('Requires=', 'PartOf=', 'BindsTo='):
            self.assertNotIn(forbidden, watcher)

    @unittest.skipUnless(sys.platform.startswith('linux'), 'systemd unit syntax verification requires Linux')
    def test_generated_units_pass_systemd_analyze_verify(self):
        """Parse the actual bundle with systemd; this does not start either service."""
        binary = shutil.which('systemd-analyze')
        self.assertIsNotNone(binary, 'Supported Ubuntu backend/CI must provide systemd-analyze')
        self.bundle()
        units = [str(self.output / name) for name in ('harniqo-broker.service', 'harniqo-broker-watch.service')]
        # A trailing colon appends installed defaults for normal system targets.
        # No generators, manager contact, install, start, path creation or NSS changes.
        environment = {'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'LC_ALL': 'C',
                       'SYSTEMD_UNIT_PATH': str(self.output) + ':', 'SYSTEMD_LOG_COLOR': '0'}
        command = [binary, 'verify', '--man=no', '--generators=no', '--recursive-errors=no', *units]
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, env=environment, close_fds=True)
        output = bytearray(); deadline = time.monotonic() + 20
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while selector.get_map():
                    remaining = deadline - time.monotonic()
                    self.assertGreater(remaining, 0, 'systemd-analyze verify exceeded 20 seconds')
                    for key, _ in selector.select(min(remaining, 0.2)):
                        chunk = os.read(key.fd, 8192)
                        if not chunk:
                            selector.unregister(key.fileobj)
                        else:
                            self.assertLessEqual(len(output) + len(chunk), 65536,
                                                 'systemd-analyze verify exceeded 64 KiB combined output')
                            output.extend(chunk)
            result = process.wait(timeout=max(0.01, deadline - time.monotonic()))
            self.assertEqual(result, 0, output.decode('utf-8', errors='replace'))
        finally:
            if process.poll() is None:
                process.kill(); process.wait(timeout=2)
            process.stdout.close()

    def test_tmpfiles_never_prepares_images_key_bytes_or_cleans_runtime(self):
        self.bundle()
        tmpfiles = (self.output / 'harniqo-tmpfiles.conf').read_text()
        self.assertNotIn('/srv/design/images', tmpfiles)
        self.assertNotIn('issuer.key', tmpfiles)
        self.assertIn('d /run/harniqo/broker-ipc 0750 harniqo-broker harniqo-ipc - -', tmpfiles)
        self.assertIn('d /var/lib/harniqo/keys 0700 harniqo-broker harniqo-broker - -', tmpfiles)
        for line in tmpfiles.splitlines():
            self.assertTrue(line.startswith('d '))
            self.assertTrue(line.endswith(' - -'))
        users = (self.output / 'harniqo-sysusers.conf').read_text()
        self.assertIn('24001:24001', users)
        self.assertIn('/usr/sbin/nologin', users)
        self.assertNotIn(' libvirt', users)
        self.assertNotIn(' docker', users)

    def test_configuration_digest_mismatch_leaves_output_empty(self):
        self.plan['configuration_sha256'] = '0' * 64; self.write_plan(); self.reject()

    def test_schema_unknown_duplicate_and_nonfinite_plan_rejected(self):
        original = self.plan_path.read_bytes()
        for data in (b'[]', b'{}', b'\xff', b'{"schema_version":1,"schema_version":1}',
                     original[:-1] + b',"override":true}', original.replace(b'24001', b'NaN')):
            with self.subTest(data=data[:30]):
                self.plan_path.write_bytes(data); self.reject()

    def test_oversized_and_empty_inputs_rejected(self):
        for data in (b'', b' ' * (deployment.MAX_PLAN_BYTES + 1)):
            self.plan_path.write_bytes(data); self.reject()
        self.write(); self.config_path.write_bytes(b'x' * (65536 + 1)); self.reject()

    def test_forged_acl_attestation_and_activation_overrides_rejected(self):
        for value in ({'status': 'verified'}, {'status': 'unverified', 'effective_driver': 'polkit'},
                      {'status': 'verified', 'daemon_pid': 123, 'negative_probe': True}, True, None):
            with self.subTest(attestation=value):
                self.plan['libvirt_acl_attestation'] = value; self.write_plan(); self.reject()

    def test_missing_acl_attestation_rejected(self):
        del self.plan['libvirt_acl_attestation']; self.write_plan(); self.reject()

    def test_root_reserved_and_injected_names_rejected(self):
        original = copy.deepcopy(self.plan)
        for field in ('broker_account', 'supervisor_account', 'ipc_group'):
            for name in ('root', 'libvirt', 'docker', 'x\nUser=root', 'x";return polkit.Result.YES;', 'x%u'):
                with self.subTest(field=field, name=name):
                    self.plan = copy.deepcopy(original); self.plan[field]['name'] = name
                    self.write_plan(); self.reject()

    def test_nonroot_distinct_numeric_identities_required(self):
        original = copy.deepcopy(self.plan)
        edits = [(field, key, value) for field in ('broker_account', 'supervisor_account')
                 for key in ('uid', 'gid') for value in (0, True, -1, 2147483647)]
        edits += [('supervisor_account', 'uid', 24001), ('supervisor_account', 'gid', 24001),
                  ('ipc_group', 'gid', 24001), ('ipc_group', 'gid', 24002)]
        for field, key, value in edits:
            with self.subTest(field=field, key=key, value=value):
                self.plan = copy.deepcopy(original); self.plan[field][key] = value
                self.write_plan(); self.reject()

    def test_pinned_configuration_must_match_both_service_identities(self):
        self.plan['supervisor_account']['uid'] += 1; self.write_plan(); self.reject()
        self.plan['supervisor_account']['uid'] -= 1
        self.plan['broker_account']['gid'] += 10; self.write_plan(); self.reject()

    def test_release_and_paths_cannot_escape_into_system_directories(self):
        for release in ('/opt/harniqo/releases/a/../b', '/opt/harniqo/releases/a/b', '/etc',
                        '/opt/harniqo/releases/a\nExecStart=/bin/sh', '/opt/harniqo/releases/a%u'):
            with self.subTest(release=release):
                self.plan['release_root'] = release; self.write_plan(); self.reject()

    def test_tmpfiles_ownership_is_confined(self):
        original = copy.deepcopy(self.configuration)
        for field, value in [('state_dir', '/etc'), ('state_dir', '/var/lib/harniqo'),
            ('issuer_key_file', '/etc/issuer.key'), ('ipc_directory', '/run/libvirt'),
            ('channel_root', '/run/libvirt'), ('state_dir', '/var/lib/harniqo/a/b')]:
            with self.subTest(field=field, value=value):
                self.configuration = copy.deepcopy(original); self.configuration[field] = value
                self.write(); self.reject()

    def test_configuration_grammar_and_frozen_template_scope_reused(self):
        self.configuration['templates'][0]['disk'] = '/etc/shadow'; self.write(); self.reject()
        self.configuration['templates'][0]['disk'] = '/srv/design/images/a.raw'
        self.configuration['issuer_key'] = 'not-a-configuration-field'; self.write(); self.reject()

    def test_images_in_home_are_preserved_and_unit_paths_are_escaped(self):
        root = '/home/operator/design/images space%literal'
        self.configuration['images_root'] = root
        self.configuration['templates'][0]['disk'] = root + '/a.raw'
        self.configuration['templates'][0]['seed'] = root + '/a.seed'; self.write(); self.bundle()
        text = (self.output / 'harniqo-broker.service').read_text()
        self.assertIn('"/home/operator/design/images space%%literal"', text)
        self.assertNotIn(root, (self.output / 'harniqo-tmpfiles.conf').read_text())

    def test_symlink_and_hardlink_inputs_rejected(self):
        real = self.root / 'real-plan.json'; self.plan_path.rename(real)
        self.plan_path.symlink_to(real); self.reject(); self.plan_path.unlink()
        os.link(real, self.plan_path); self.reject()

    def test_symlink_parent_inputs_rejected(self):
        parent = self.root / 'alias'; parent.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(deployment.DeploymentError):
            deployment.prepare_bundle(parent / 'plan.json', self.output)
        self.assertEqual(list(self.output.iterdir()), [])

    def test_world_writable_input_is_rejected(self):
        self.plan_path.chmod(0o666); self.reject()

    def test_fifo_configuration_is_rejected_without_waiting_for_a_writer(self):
        self.config_path.unlink(); os.mkfifo(self.config_path, 0o600)
        self.reject()

    def test_output_requires_preexisting_empty_owned_private_directory(self):
        missing = self.root / 'missing'
        with self.assertRaises(deployment.DeploymentError):
            deployment.prepare_bundle(self.plan_path, missing)
        self.assertFalse(missing.exists())
        self.output.chmod(0o750)
        with self.assertRaises(deployment.DeploymentError): self.bundle()
        self.output.chmod(0o700)
        marker = self.output / 'preserve'; marker.write_bytes(b'untouched')
        with self.assertRaises(deployment.DeploymentError): self.bundle()
        self.assertEqual(marker.read_bytes(), b'untouched')
        self.assertEqual(list(self.output.iterdir()), [marker])

    def test_output_symlink_does_not_write_target(self):
        alias = self.root / 'alias'; alias.symlink_to(self.output, target_is_directory=True)
        with self.assertRaises(deployment.DeploymentError):
            deployment.prepare_bundle(self.plan_path, alias)
        self.assertEqual(list(self.output.iterdir()), [])

    def test_privileged_or_mixed_execution_rejected(self):
        with patch.object(os, 'getuid', return_value=0), self.assertRaises(deployment.DeploymentError):
            self.bundle()
        with patch.object(os, 'geteuid', return_value=os.getuid() + 1), self.assertRaises(deployment.DeploymentError):
            self.bundle()
        self.assertEqual(list(self.output.iterdir()), [])

    def test_cli_rejection_has_no_native_exception_or_input_echo(self):
        self.plan['supervisor_account']['name'] = 'SECRET-MUST-NOT-ECHO'; self.write_plan()
        result = subprocess.run([sys.executable, '-B', str(REPO / 'scripts/deploy/prepare_broker_service.py'),
            '--plan', str(self.plan_path), '--output', str(self.output)], text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 1)
        self.assertNotIn('SECRET-MUST-NOT-ECHO', result.stderr)
        self.assertNotIn('Traceback', result.stderr)
        self.assertEqual(json.loads(result.stderr)['status'], 'rejected')
        self.assertEqual(list(self.output.iterdir()), [])


if __name__ == '__main__':
    unittest.main()
