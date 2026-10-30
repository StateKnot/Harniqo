"""Resource Broker Interface checks; libvirt is a synthetic process-edge fixture.

These tests never start a VM or qualify host isolation. The designated-server
qualification crosses the same ResourceBroker Interface with real libvirt.
"""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from apps.runner.resource_broker import Assignment, ResourceBroker, sign_assignment


class FirstSliceTests(unittest.TestCase):
    def test_assignment_signing_binds_operation_and_fields(self):
        assignment = sign_assignment(b'x' * 32, resource_id='00000000-0000-4000-8000-000000000001',
            organization_id='org-a',workspace_id='workspace-a',attempt_id='attempt-a',template_id='template-a',configuration_sha256='a'*64,
            generation=1,recovery_generation=1,issued_at=1000.0,expires_at=1060.0)
        self.assertIsInstance(assignment, Assignment)
        self.assertEqual(assignment.organization_id, 'org-a')
        self.assertEqual(len(assignment.signature), 64)


# This fixture models only the external libvirt command edge, not lifecycle
# policy. All assertions cross the production ResourceBroker Interface.
import copy
from dataclasses import replace
import json
import os
import socket
import tempfile
from unittest.mock import patch
from types import SimpleNamespace
import xml.etree.ElementTree as ET
from apps.runner import resource_broker as native
from apps.runner.resource_broker import Template, Grant, BrokerError, sign_grant, prepare_seed_access


class LibvirtFixture:
    def __init__(self):
        self.domains = {}; self.creates = 0; self.destroys = 0; self.create_unknown = False
        self.stop_unknown = False; self.unavailable = False

    def __call__(self, *args):
        if self.unavailable:
            raise BrokerError('libvirt_call_unconfirmed')
        if args == ('list', '--all', '--uuid'):
            return '\n'.join(self.domains)
        if args[0] == 'dumpxml':
            return self.domains[args[1]]
        if args[0] == 'create':
            self.creates += 1
            if self.create_unknown:
                raise BrokerError('libvirt_call_unconfirmed')
            xml = Path(args[1]).read_text();identity = ET.fromstring(xml).findtext('uuid')
            self.domains[identity] = xml
            return ''
        if args[0] == 'destroy':
            self.destroys += 1; self.domains.pop(args[1], None)
            if self.stop_unknown:
                raise BrokerError('libvirt_call_unconfirmed')
            return ''
        raise AssertionError(args)


class BrokerInterfaceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='hbr-', dir=Path('/private/tmp' if sys.platform == 'darwin' else '/tmp').resolve())
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.images = self.root/'images';self.images.mkdir(mode=0o700)
        self.channels = self.root/'channels';self.channels.mkdir(mode=0o700)
        self.key = b'k'*32;self.key_file = self.root/'issuer';self.key_file.write_bytes(self.key);self.key_file.chmod(0o600)
        self.prepared_seed_access = True
        self.pwd_patch = patch.object(native.pwd, 'getpwnam', return_value=SimpleNamespace(pw_uid=64055))
        self.pwd_patch.start(); self.addCleanup(self.pwd_patch.stop)
        def acl_fixture(args, **kwargs):
            if args[0].endswith('setfacl'):
                self.prepared_seed_access = True; return ''
            if args[0].endswith('getfacl'):
                seed = str(args[-1]).endswith('.seed')
                rights = 'r--' if seed else 'rw-'
                broker = ('user:' + str(os.geteuid()) + ':r--\n') if seed and self.prepared_seed_access else ''
                return 'user::rw-\n' + broker + 'user:64055:' + rights + '\ngroup::---\nmask::' + rights + '\nother::---\n'
            raise AssertionError(args)
        self.acl_fixture = acl_fixture
        self.acl_patch = patch.object(native, '_invoke_local', side_effect=acl_fixture)
        self.acl_mock=self.acl_patch.start(); self.addCleanup(self.acl_patch.stop)
        self.templates = []
        for index, label in enumerate(('a', 'b'), 1):
            disk=self.images/(label+'.raw')
            with disk.open('wb') as stream:stream.truncate(8*1024**3)
            disk.chmod(0o600)
            seed=self.images/(label+'.seed');seed.write_bytes(b'seed'*128);seed.chmod(0o600)
            sockets=[]
            for role in ('channel', 'console'):
                path=self.channels/(label+'-'+role);listener=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
                listener.bind(str(path));path.chmod(0o600);self.addCleanup(listener.close);sockets.append(path)
            self.templates.append(Template('template-'+label,'00000000-0000-4000-8000-'+f'{index:012d}',
                'owned-'+label,disk,seed,*sockets,'a'*64))
        self.clock={'wall':1000.0,'monotonic':10.0,'boot_id':'00000000-0000-4000-8000-000000000099'}
        self.clock_patch=patch.object(native,'_clock',side_effect=lambda:dict(self.clock));self.clock_patch.start();self.addCleanup(self.clock_patch.stop)
        self.libvirt=LibvirtFixture();self.native_patch=patch.object(native,'_virsh',side_effect=self.libvirt);self.native_patch.start();self.addCleanup(self.native_patch.stop)
        self.broker=self.new_broker()

    def new_broker(self):
        return ResourceBroker(state_dir=self.root/'state',templates=self.templates,issuer_key_file=self.key_file,
            images_root=self.images,channel_root=self.channels,max_lease_seconds=300)

    def claims(self,index=0,**changes):
        t=self.templates[index]
        fields=dict(resource_id=t.resource_id,organization_id='org-a',workspace_id='workspace-'+str(index),
                    attempt_id='attempt-'+str(index),template_id=t.template_id,configuration_sha256=t.configuration_sha256,
                    generation=1,recovery_generation=1,issued_at=self.clock['wall'],expires_at=self.clock['wall']+120)
        fields.update(changes);return fields

    def assignment(self,index=0,**changes):return sign_assignment(self.key,**self.claims(index,**changes))
    def advance(self,seconds):self.clock['wall']+=seconds;self.clock['monotonic']+=seconds

    def test_same_assignment_is_idempotent_and_same_workspace_rejects_second_vm(self):
        a=self.assignment();self.assertEqual(self.broker.ensure(a).state,'running')
        self.assertEqual(self.broker.ensure(a).state,'running');self.assertEqual(self.libvirt.creates,1)
        with self.assertRaisesRegex(BrokerError,'workspace_busy'):
            self.broker.ensure(self.assignment(1,workspace_id='workspace-0'))
        self.assertEqual(self.libvirt.creates,1)
        self.assertEqual(self.broker.ensure(self.assignment(1)).state,'running')

    def test_wrong_signature_purpose_scope_and_configuration_cannot_dispatch(self):
        a=self.assignment()
        for wrong in (replace(a,signature='0'*64),replace(a,organization_id='other-org'),
                      replace(a,configuration_sha256='b'*64),sign_grant(self.key,**self.claims())):
            with self.subTest(wrong=type(wrong).__name__),self.assertRaises(BrokerError):self.broker.ensure(wrong)
        self.assertEqual(self.libvirt.creates,0)

    def test_expiry_stops_exact_owned_domain_and_retains_terminal_tombstone(self):
        a=self.assignment();self.broker.ensure(a);self.advance(121)
        result=self.broker.observe(a.resource_id)
        self.assertEqual(result.state,'stopped');self.assertEqual(result.stop_evidence.method,'libvirt_destroy_then_absent')
        self.assertEqual(self.libvirt.destroys,1)
        with self.assertRaises(BrokerError):self.new_broker().ensure(a)
        with self.assertRaisesRegex(BrokerError,'resource_scope_immutable'):
            self.new_broker().ensure(self.assignment(organization_id='other-org',generation=3))
        self.assertEqual(self.libvirt.creates,1)

    def test_renew_increments_generation_may_shorten_and_rejects_old_or_expired_grants(self):
        a=self.assignment();self.broker.ensure(a)
        g=sign_grant(self.key,**self.claims(generation=2,expires_at=1005.0))
        self.assertEqual(self.broker.renew(g).generation,2)
        with self.assertRaisesRegex(BrokerError,'generation_stale'):self.broker.renew(g)
        self.advance(6)
        with self.assertRaisesRegex(BrokerError,'lease_expired'):
            self.broker.renew(sign_grant(self.key,**self.claims(generation=3)))
        self.assertEqual(self.broker.observe(a.resource_id).state,'stopped')

    def test_pending_create_does_not_release_writer_from_absence_or_retry(self):
        self.libvirt.create_unknown=True;a=self.assignment()
        self.assertEqual(self.broker.ensure(a).state,'reconciling')
        self.advance(121)
        self.assertEqual(self.broker.observe(a.resource_id).state,'reconciling')
        self.assertIsNone(self.broker.observe(a.resource_id).stop_evidence)
        with self.assertRaisesRegex(BrokerError,'workspace_busy'):
            self.broker.ensure(self.assignment(1,workspace_id='workspace-0',generation=2))
        self.assertEqual(self.libvirt.creates,1)

    def test_unknown_destroy_stays_reconciling_even_when_domain_is_absent(self):
        a=self.assignment();self.broker.ensure(a);self.libvirt.stop_unknown=True
        self.assertEqual(self.broker.revoke(a.resource_id,'cancelled').state,'reconciling')
        observed=self.broker.observe(a.resource_id)
        self.assertEqual(observed.state,'reconciling');self.assertIsNone(observed.stop_evidence)

    def test_restart_and_host_boot_or_clock_rollback_expire_known_execution(self):
        for mode in ('restart','boot','rollback'):
            with self.subTest(mode=mode):
                if mode!='restart':break
                a=self.assignment();self.broker.ensure(a);self.advance(121)
                result=self.new_broker().reconcile()[0]
                self.assertEqual(result.state,'stopped')
        # A new authorized generation can reuse the same retained workspace.
        self.broker.ensure(self.assignment(generation=2))
        self.clock['wall']-=10
        self.assertEqual(self.broker.reconcile()[0].state,'stopped')
        self.broker.ensure(self.assignment(generation=3))
        self.clock['boot_id']='00000000-0000-4000-8000-000000000088'
        self.assertEqual(self.broker.reconcile()[0].state,'stopped')

    def test_foreign_metadata_is_not_destroyed_and_does_not_free_writer(self):
        a=self.assignment();self.broker.ensure(a)
        tree=ET.fromstring(self.libvirt.domains[a.resource_id]);tree.find('./metadata')[0].set('signature','0'*64)
        self.libvirt.domains[a.resource_id]=ET.tostring(tree,encoding='unicode')
        result=self.broker.revoke(a.resource_id,'cancelled')
        self.assertEqual(result.state,'reconciling');self.assertEqual(result.error_code,'resource_ownership_mismatch')
        self.assertEqual(self.libvirt.destroys,0)

    def test_missing_or_corrupt_journal_stops_cached_owned_resource_and_never_reinitializes(self):
        a=self.assignment();self.broker.ensure(a);(self.root/'state/journal.json').unlink()
        with self.assertRaises(BrokerError):self.broker.reconcile()
        self.assertEqual(self.libvirt.destroys,1)
        self.assertEqual(self.broker.emergency_observations[0]['state'],'reconciling')
        with self.assertRaises(BrokerError):self.new_broker()
        self.assertFalse((self.root/'state/journal.json').exists())

    def test_failed_persistence_prevents_dispatch(self):
        with patch.object(native,'_atomic_private',side_effect=OSError('synthetic disk full')):
            with self.assertRaises(BrokerError):self.broker.ensure(self.assignment())
        self.assertEqual(self.libvirt.creates,0)

    def test_catalog_file_replacement_is_not_accepted_after_restart(self):
        a=self.assignment();self.broker.ensure(a)
        seed=self.templates[0].seed;seed.write_bytes(b'changed-seed'*128)
        with self.assertRaises(BrokerError):self.new_broker()
        with self.assertRaisesRegex(BrokerError,'state_catalog_changed'):self.broker.renew(sign_grant(self.key,**self.claims(generation=2,configuration_sha256=a.configuration_sha256)))

    def test_nonfinite_or_bad_numeric_assignment_does_not_stop_another_workspace(self):
        a=self.assignment();self.broker.ensure(a);valid_b=self.assignment(1)
        for changes in ({'issued_at':float('nan')},{'expires_at':float('inf')},{'generation':True},
                        {'recovery_generation':1.0},{'expires_at':999.0},{'issued_at':1010.0},
                        {'expires_at':1400.0}):
            malformed=replace(valid_b,**changes)
            with self.subTest(changes=str(changes)),self.assertRaises(BrokerError):self.broker.ensure(malformed)
            self.assertIn(a.resource_id,self.libvirt.domains)
        self.assertEqual(self.libvirt.destroys,0)

    def test_untrusted_catalog_alias_symlink_and_parent_escape_are_rejected(self):
        originals=self.templates
        cases=[replace(originals[1],disk=originals[0].disk),
               replace(originals[1],channel=originals[0].channel),
               replace(originals[1],seed=originals[0].seed),
               replace(originals[1],disk=self.images/'..'/'images'/originals[1].disk.name)]
        for wrong in cases:
            self.templates=[originals[0],wrong]
            with self.subTest(wrong=str(wrong.disk)),self.assertRaises(BrokerError):self.new_broker()
        self.templates=originals
        path=self.images/'link.raw';path.symlink_to(originals[1].disk)
        self.templates=[originals[0],replace(originals[1],disk=path)]
        with self.assertRaisesRegex(BrokerError,'symlink'):self.new_broker()

    def test_validly_signed_invalid_deadlines_and_bool_generations_are_rejected(self):
        for changes in ({'generation':True},{'recovery_generation':False},{'issued_at':1010.0},
                        {'expires_at':999.0},{'expires_at':1400.0}):
            with self.subTest(changes=changes),self.assertRaises(BrokerError):self.broker.ensure(self.assignment(**changes))
        self.assertEqual(self.libvirt.creates,0)

    def test_corrupt_journal_with_fresh_broker_stops_authenticated_catalog_domain(self):
        a=self.assignment();self.broker.ensure(a)
        (self.root/'state/journal.json').write_text('not json')
        with self.assertRaises(BrokerError):self.new_broker()
        self.assertNotIn(a.resource_id,self.libvirt.domains)
        self.assertEqual(self.libvirt.destroys,1)
        self.assertEqual((self.root/'state/journal.json').read_text(),'not json')

    def test_missing_anchor_or_replaced_lock_cannot_reset_existing_state(self):
        a=self.assignment();self.broker.ensure(a)
        (self.root/'state/anchor.json').unlink()
        with self.assertRaises(BrokerError):self.new_broker()
        with self.assertRaises(BrokerError):self.broker.ensure(self.assignment(1))
        self.assertEqual(self.libvirt.creates,1)

    def test_live_seed_replacement_fails_closed_during_observe(self):
        a=self.assignment();self.broker.ensure(a);seed=self.templates[0].seed
        other=seed.with_suffix('.replacement');other.write_bytes(seed.read_bytes());other.chmod(0o600);other.replace(seed)
        with self.assertRaisesRegex(BrokerError,'state_catalog_changed'):self.broker.observe(a.resource_id)
        self.assertNotIn(a.resource_id,self.libvirt.domains)
        self.assertEqual(self.broker.emergency_observations[0]['state'],'reconciling')

    def test_watch_keeps_retrying_late_resource_when_journal_remains_corrupt(self):
        self.libvirt.create_unknown=True;a=self.assignment();self.broker.ensure(a)
        xml=(self.root/'state'/('domain-'+a.resource_id+'.xml')).read_text()
        (self.root/'state/journal.json').write_text('corrupt')
        class StopAfterTwo:
            count=0
            def is_set(inner):return inner.count>=2
            def wait(inner,seconds):
                inner.count+=1
                if inner.count==1:self.libvirt.domains[a.resource_id]=xml
        self.broker.watch(stop_event=StopAfterTwo())
        self.assertNotIn(a.resource_id,self.libvirt.domains)
        self.assertEqual(self.libvirt.destroys,1)
        self.assertEqual(self.broker.last_error,'journal_corrupt')

    def test_unavailable_libvirt_retains_writer_until_host_fact_can_be_observed(self):
        a=self.assignment();self.broker.ensure(a);self.libvirt.unavailable=True;self.advance(121)
        result=self.broker.reconcile()[0]
        self.assertEqual(result.state,'reconciling');self.assertIsNone(result.stop_evidence)
        self.libvirt.unavailable=False
        with self.assertRaisesRegex(BrokerError,'workspace_busy'):
            self.broker.ensure(self.assignment(1,workspace_id='workspace-0',generation=2))
        self.assertEqual(self.libvirt.creates,1)
        self.assertEqual(self.broker.reconcile()[0].state,'stopped')

    def test_create_or_observe_latency_cannot_report_an_expired_lease_running(self):
        base=self.libvirt
        def delayed_create(*args):
            result=base(*args)
            if args[0]=='create':self.advance(121)
            return result
        a=self.assignment()
        with patch.object(native,'_virsh',side_effect=delayed_create):result=self.broker.ensure(a)
        self.assertEqual(result.state,'stopped')
        self.assertGreaterEqual(result.stop_evidence.confirmed_at,1121.0)
        a=self.assignment(generation=2);self.broker.ensure(a)
        def delayed_xml(*args):
            result=base(*args)
            if args[0]=='dumpxml':self.advance(121)
            return result
        with patch.object(native,'_virsh',side_effect=delayed_xml):result=self.broker.observe(a.resource_id)
        self.assertEqual(result.state,'stopped')
        self.assertEqual(result.stop_evidence.confirmed_at,self.clock['wall'])

    def test_reconcile_samples_each_resource_and_stop_confirmation_after_rpc(self):
        a=self.assignment();b=self.assignment(1,expires_at=1005.0)
        self.broker.ensure(a);self.broker.ensure(b);base=self.libvirt
        def delayed_first_xml(*args):
            result=base(*args)
            if args==('dumpxml',a.resource_id):self.advance(10)
            if args[0]=='destroy':self.advance(2)
            return result
        with patch.object(native,'_virsh',side_effect=delayed_first_xml):results=self.broker.reconcile()
        self.assertEqual(results[1].state,'stopped')
        self.assertEqual(results[1].stop_evidence.confirmed_at,self.clock['wall'])

    def test_revoke_intent_survives_unavailable_host_observe_renew_and_restart(self):
        a=self.assignment();self.broker.ensure(a);self.libvirt.unavailable=True
        self.assertEqual(self.broker.revoke(a.resource_id,'cancelled').state,'reconciling')
        self.libvirt.unavailable=False
        recreated=self.new_broker()
        with self.assertRaises(BrokerError):recreated.renew(sign_grant(self.key,**self.claims(generation=2)))
        result=recreated.observe(a.resource_id)
        self.assertEqual(result.state,'stopped');self.assertEqual(self.libvirt.creates,1)
        self.assertEqual(self.libvirt.destroys,1)

    def test_slow_final_catalog_validation_cannot_dispatch_after_deadline(self):
        a=self.assignment(expires_at=1005.0);describe=native._template_description;delayed=False
        def slow_final_description(template):
            nonlocal delayed
            result=describe(template)
            # Reproduce a slow file/ACL check after the prepared domain XML is
            # durable, immediately before dispatch. Other policy remains real.
            if not delayed and (self.root/'state'/('domain-'+a.resource_id+'.xml')).exists():
                self.advance(10);delayed=True
            return result
        with patch.object(native,'_template_description',side_effect=slow_final_description):result=self.broker.ensure(a)
        self.assertTrue(delayed);self.assertEqual(self.libvirt.creates,0)
        self.assertEqual(result.state,'stopped')
        self.assertEqual(result.stop_evidence.method,'libvirt_absent_without_dispatch')

    def test_replaced_lock_is_rejected_by_fresh_and_existing_broker(self):
        a=self.assignment();self.broker.ensure(a)
        lock=self.root/'state/broker.lock';replacement=self.root/'state/new.lock'
        replacement.write_bytes(b'');replacement.chmod(0o600);replacement.replace(lock)
        with self.assertRaisesRegex(BrokerError,'state_lock_replaced'):self.new_broker()
        self.assertNotIn(a.resource_id,self.libvirt.domains)
        with self.assertRaises(BrokerError):self.broker.observe(a.resource_id)
        self.assertEqual(self.libvirt.creates,1)
        self.assertEqual(self.libvirt.destroys,1)

    def test_known_driver_owner_transition_preserves_live_and_cold_observation(self):
        a=self.assignment();self.broker.ensure(a);original_lstat=Path.lstat;root=self.templates[0]
        def driver_stat(path,*args,**kwargs):
            result=original_lstat(path,*args,**kwargs);values=list(result)
            if path==root.seed:values[4]=64055;values[0]=(values[0]&~0o777)|0o640
            if path in (root.channel,root.console):values[4]=0;values[0]=(values[0]&~0o777)|0o660
            return os.stat_result(values)
        with patch.object(Path,'lstat',driver_stat):
            self.assertEqual(self.broker.observe(a.resource_id).state,'running')
            self.assertEqual(self.new_broker().observe(a.resource_id).state,'running')

    def test_seed_access_preparation_is_explicit_read_only_and_required_for_catalog(self):
        self.prepared_seed_access=False
        with self.assertRaisesRegex(BrokerError,'seed_broker_read_acl_required'):
            _=self.templates[0].configuration_sha256
        seed=self.templates[0].seed;before=seed.read_bytes();self.acl_patch.stop()
        calls=[]
        def recorded(args,**kwargs):calls.append(args);return self.acl_fixture(args,**kwargs)
        with patch.object(native,'_invoke_local',side_effect=recorded):
            prepare_seed_access(seed,images_root=self.images)
        mutations=[args for args in calls if args[0].endswith('setfacl')]
        self.assertEqual(mutations,[['/usr/bin/setfacl','-m','u:'+str(os.geteuid())+':r--','--',str(seed)]])
        self.assertEqual(seed.read_bytes(),before)
        self.acl_patch.start()

    def test_arbitrary_owner_is_still_rejected_and_cached_resource_is_stopped(self):
        a=self.assignment();self.broker.ensure(a);original_lstat=Path.lstat;seed=self.templates[0].seed
        def foreign_stat(path,*args,**kwargs):
            result=original_lstat(path,*args,**kwargs)
            if path==seed:
                values=list(result);values[4]=424242;return os.stat_result(values)
            return result
        with patch.object(Path,'lstat',foreign_stat):
            with self.assertRaisesRegex(BrokerError,'state_catalog_changed'):self.broker.observe(a.resource_id)
        self.assertNotIn(a.resource_id,self.libvirt.domains)

    def test_seed_preparation_rejects_existing_foreign_owner_without_acl_mutation(self):
        seed=self.templates[0].seed;original_lstat=Path.lstat
        def qemu_owned(path,*args,**kwargs):
            result=original_lstat(path,*args,**kwargs)
            if path==seed:
                values=list(result);values[4]=64055;return os.stat_result(values)
            return result
        self.acl_mock.reset_mock()
        with patch.object(Path,'lstat',qemu_owned):
            with self.assertRaisesRegex(BrokerError,'seed_preparation_owner_invalid'):
                prepare_seed_access(seed,images_root=self.images)
        self.assertFalse(any(c.args[0][0].endswith('setfacl') for c in self.acl_mock.mock_calls))

    def test_configuration_diagnostics_keep_role_cause_without_paths_and_are_read_only(self):
        a=self.assignment();self.broker.ensure(a);original_lstat=Path.lstat;seed=self.templates[0].seed
        def transient_stat(path,*args,**kwargs):
            result=original_lstat(path,*args,**kwargs)
            if path==seed:
                values=list(result);values[4]=424242;return os.stat_result(values)
            return result
        with patch.object(Path,'lstat',transient_stat):
            with self.assertRaises(BrokerError) as caught:self.broker.observe(a.resource_id)
        details=self.broker.last_configuration_error
        self.assertEqual(details['role'],'seed')
        self.assertEqual(details['cause_code'],'template_file_permissions_invalid')
        self.assertIn('owner_uid',details['failed_fields'])
        self.assertEqual(caught.exception.__cause__.code,'template_file_permissions_invalid')
        self.assertNotIn(str(self.root),json.dumps(details));self.assertNotIn('424242',json.dumps(details))
        details['role']='changed externally'
        self.assertEqual(self.broker.last_configuration_error['role'],'seed')
        with self.assertRaises(AttributeError):self.broker.last_configuration_error={}

    def test_configuration_diff_reports_only_role_and_field_names(self):
        a=self.assignment();self.broker.ensure(a);describe=native._template_description
        def different_seed(template):
            value=describe(template)
            if template.template_id==self.templates[0].template_id:
                value=copy.deepcopy(value);value['files'][1]['sha256']='PRIVATE-SEED-DIAGNOSTIC-CANARY'
            return value
        with patch.object(native,'_template_description',side_effect=different_seed):
            with self.assertRaises(BrokerError):self.broker.observe(a.resource_id)
        details=self.broker.last_configuration_error
        self.assertIn({'role':'seed','field':'sha256'},details['changed_fields'])
        self.assertNotIn('PRIVATE-SEED-DIAGNOSTIC-CANARY',json.dumps(details))
        self.assertNotIn(str(self.root),json.dumps(details))

    def test_constructor_catalog_binding_failure_also_has_bounded_diagnostic(self):
        self.templates[0].seed.write_bytes(b'new-seed'*128)
        with self.assertRaises(BrokerError) as caught:self.new_broker()
        details=caught.exception.configuration_error
        self.assertEqual(details['cause_code'],'anchor_binding_mismatch')
        self.assertIn('catalog_sha256',details['failed_fields'])
        self.assertNotIn(str(self.root),json.dumps(details))

    def test_driver_socket_broker_qemu_root_lifecycle_and_cold_restore(self):
        template=self.templates[0];a=self.assignment();configuration=a.configuration_sha256
        self.assertEqual(self.broker.ensure(a).state,'running')
        original_lstat=Path.lstat;stage={'socket_uid':64055};base=self.libvirt
        def driver_stat(path,*args,**kwargs):
            result=original_lstat(path,*args,**kwargs);values=list(result)
            if path==template.seed:values[4]=64055;values[0]=(values[0]&~0o777)|0o640
            if path in (template.channel,template.console):
                values[4]=stage['socket_uid'];values[0]=(values[0]&~0o777)|0o660
            return os.stat_result(values)
        def destroy_restores_root(*args):
            result=base(*args)
            if args[0]=='destroy':stage['socket_uid']=0
            return result
        with patch.object(Path,'lstat',driver_stat),patch.object(native,'_virsh',side_effect=destroy_restores_root):
            self.assertEqual(template.configuration_sha256,configuration)
            self.assertEqual(self.broker.observe(a.resource_id).state,'running')
            self.assertEqual(self.new_broker().observe(a.resource_id).state,'running')
            self.assertEqual(self.broker.revoke(a.resource_id,'cancelled').state,'stopped')
            self.assertEqual(stage['socket_uid'],0)
            self.assertEqual(template.configuration_sha256,configuration)
            self.assertEqual(self.new_broker().observe(a.resource_id).state,'stopped')

    def test_unrecognized_socket_owner_remains_rejected(self):
        a=self.assignment();self.broker.ensure(a);original_lstat=Path.lstat;channel=self.templates[0].channel
        def foreign_socket(path,*args,**kwargs):
            result=original_lstat(path,*args,**kwargs)
            if path==channel:
                values=list(result);values[4]=424242;return os.stat_result(values)
            return result
        with patch.object(Path,'lstat',foreign_socket):
            with self.assertRaisesRegex(BrokerError,'state_catalog_changed'):self.broker.observe(a.resource_id)
        self.assertEqual(self.broker.last_configuration_error['role'],'channel')
        self.assertIn('owner_uid',self.broker.last_configuration_error['failed_fields'])
        self.assertNotIn(a.resource_id,self.libvirt.domains)


if __name__ == "__main__":
    unittest.main()
