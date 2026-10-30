"""No-Docker/no-model regressions for review findings in the DSH operator."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace
import dsh_support as support
from probe_hermes import Rejected

class OperatorTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root=Path(self.directory.name)

    def image_runner(self, **overrides):
        value={'Id':support.QUALIFIED_IMAGE,'Os':'linux','Architecture':'amd64',**overrides}
        return Mock(return_value=subprocess.CompletedProcess([],0,stdout=json.dumps([value])))

    def test_explicit_local_daemon_and_clean_environment(self):
        runner=self.image_runner()
        with patch.dict(os.environ,{key:'malicious-remote' for key in support.DOCKER_VARIABLES}):
            support.validate_image(support.QUALIFIED_IMAGE,runner=runner)
            args,kwargs=runner.call_args
            self.assertEqual(args[0][:3],['docker','--host','unix:///var/run/docker.sock'])
            self.assertFalse(set(support.DOCKER_VARIABLES)&set(kwargs['env']))

    def test_helper_scope_restores_environment_on_failure(self):
        with patch.dict(os.environ,{'DOCKER_CONTEXT':'original'}):
            with self.assertRaises(ValueError):
                with support.local_docker_environment():
                    self.assertNotIn('DOCKER_CONTEXT',os.environ)
                    raise ValueError()
            self.assertEqual(os.environ['DOCKER_CONTEXT'],'original')

    def test_tag_rejected_before_inspection(self):
        runner=Mock()
        with self.assertRaises(Rejected):support.validate_image('harniqo-dsh:latest',runner=runner)
        runner.assert_not_called()

    def test_unqualified_digest_rejected_before_inspection(self):
        runner=Mock()
        with self.assertRaises(Rejected):support.validate_image('sha256:'+'a'*64,expected=support.QUALIFIED_IMAGE,runner=runner)
        runner.assert_not_called()

    def test_installed_identity_and_platform_are_required(self):
        for mutation in ({'Id':'sha256:'+'b'*64},{'Os':'windows'},{'Architecture':'arm64'}):
            with self.subTest(mutation=mutation),self.assertRaises(Rejected):
                support.validate_image(support.QUALIFIED_IMAGE,runner=self.image_runner(**mutation))

    def test_partial_save_does_not_publish_final(self):
        archive=self.root/'runtime.tar';report={}
        def fail(args,**kwargs):
            Path(args[args.index('-o')+1]).write_bytes(b'incomplete')
            raise subprocess.CalledProcessError(1,args)
        with self.assertRaises(subprocess.CalledProcessError):support.archive_image(support.QUALIFIED_IMAGE,archive,report,runner=fail)
        self.assertFalse(archive.exists())
        self.assertEqual(report['partial_archive_bytes'],10)
        self.assertEqual(report['partial_archive_sha256'],hashlib.sha256(b'incomplete').hexdigest())
        self.assertTrue((self.root/report['partial_archive_name']).is_file())

    def test_successful_save_promotes_exclusively(self):
        archive=self.root/'runtime.tar';report={}
        def succeed(args,**kwargs):Path(args[args.index('-o')+1]).write_bytes(b'complete')
        digest,size=support.archive_image(support.QUALIFIED_IMAGE,archive,report,runner=succeed)
        self.assertEqual((digest,size),(hashlib.sha256(b'complete').hexdigest(),8))
        self.assertEqual(archive.stat().st_mode&0o777,0o444)
        self.assertNotIn('partial_archive_name',report)

    def test_existing_archive_cannot_be_replaced(self):
        archive=self.root/'runtime.tar';archive.write_bytes(b'original');runner=Mock()
        with self.assertRaises(Rejected):support.archive_image(support.QUALIFIED_IMAGE,archive,{},runner=runner)
        self.assertEqual(archive.read_bytes(),b'original');runner.assert_not_called()

    def test_concurrent_archive_cannot_be_replaced(self):
        archive=self.root/'runtime.tar';report={}
        def race(args,**kwargs):
            Path(args[args.index('-o')+1]).write_bytes(b'ours');archive.write_bytes(b'other')
        with self.assertRaises(FileExistsError):support.archive_image(support.QUALIFIED_IMAGE,archive,report,runner=race)
        self.assertEqual(archive.read_bytes(),b'other')
        self.assertTrue((self.root/report['partial_archive_name']).exists())

    def test_unknown_builder_dispatch_always_reconciles_and_reports(self):
        stopped={'execution_stop_confirmed':True,'logs_complete':True}
        with patch.object(support,'cleanup',return_value=stopped) as cleanup:
            def unknown(*args,**kwargs):raise subprocess.TimeoutExpired('docker run',30)
            with self.assertRaises(Rejected):support.builder([],self.root,support.QUALIFIED_IMAGE,runner=unknown)
        report=json.loads((self.root/'operator.json').read_text())
        self.assertEqual(report['dispatch'],'intent');self.assertEqual(report['status'],'failed')
        self.assertEqual(report['cleanup'],stopped);self.assertEqual(len(report['attempt_id']),32)
        self.assertEqual(cleanup.call_args.args[2],support.QUALIFIED_IMAGE)
        self.assertIsNone(cleanup.call_args.args[3])

    def test_cleanup_failure_is_preserved(self):
        with patch.object(support,'cleanup',side_effect=Rejected('container_ownership_mismatch')):
            with self.assertRaises(Rejected):support.builder([],self.root,support.QUALIFIED_IMAGE,runner=Mock(side_effect=OSError()))
        report=json.loads((self.root/'operator.json').read_text())
        self.assertEqual(report['cleanup_failure']['code'],'container_ownership_mismatch')
        self.assertEqual(report['status'],'failed')

class AdmissionTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory();self.addCleanup(self.directory.cleanup)
        self.root=Path(self.directory.name)
        self.case=self.root/'.private/transport';(self.case/'evidence').mkdir(parents=True,mode=0o700)
        (self.root/'.private').chmod(0o700);self.case.chmod(0o700)
        (self.case/'native-sources').mkdir(mode=0o700)
        self.model={'model':'qwen3.8-flash','base_url':'https://example.invalid/v1'}
        self.sources={support.PROBE_SOURCE:b'fixed native SDK probe',support.OBSERVER_SOURCE:b'fixed observer'}
        self.binding=support.qualification_binding(support.QUALIFIED_IMAGE,self.model,self.sources)
        self.operator={'mode':'transport','status':'passed','native_preflight_passed':True,
          'image_id':support.QUALIFIED_IMAGE,'admission_binding':self.binding,
          **{k:support.QUALIFICATION_PROFILE[k] for k in ('max_http_requests','max_output_tokens','execution_deadline_seconds')},
          'source_sha256':{p:hashlib.sha256(b).hexdigest() for p,b in self.sources.items()},
          'cleanup':{'execution_stop_confirmed':True,'logs_complete':True,'container_removed':True}}
        self.probe={'mode':'transport','status':'passed','model':self.model['model'],'native_preflight_passed':True,
          'native_transport_path_observed_without_http':True,'component_only_intentional_provider_interception':True}
        self.marker={'schema':1,'kind':'component_intercepted','actual_http_sent':False,'model_matches_expected':True,'output_cap':1024,
          'model_sha256':hashlib.sha256(self.model['model'].encode()).hexdigest(),
          'url_sha256':hashlib.sha256((self.model['base_url']+'/chat/completions').encode()).hexdigest()}
        self.report=self.case/'operator.json'
        self.write(self.report,self.operator);self.write(self.case/'evidence/probe.json',self.probe)
        self.write(self.case/'model.json',self.model);self.write(self.case/'evidence/transport.jsonl',self.marker)
        for source,data in self.sources.items():
            p=self.case/'native-sources'/Path(source).name;p.write_bytes(data);p.chmod(0o400)

    def write(self,path,value):
        path.write_text(json.dumps(value)+'\n');path.chmod(0o600)

    def validate(self,path=None,**overrides):
        return support.validate_admission(path or self.report,overrides.get('binding',self.binding),overrides.get('model',self.model))

    def test_passed_bound_transport_receipt_has_hashes_without_endpoint(self):
        result=self.validate()
        self.assertEqual(result['operator_sha256'],hashlib.sha256(self.report.read_bytes()).hexdigest())
        self.assertNotIn(self.model['base_url'],json.dumps(result))
        self.assertNotIn('api_key',json.dumps(result))

    def test_missing_report_is_rejected(self):
        with self.assertRaisesRegex(Rejected,'admission_report_required'):support.validate_admission(None,self.binding,self.model)

    def test_wrong_modes_status_and_legacy_binding_are_rejected(self):
        for mutation in ({'mode':'smoke'},{'status':'failed'},{'native_preflight_passed':False},{'admission_binding':None}):
            with self.subTest(mutation=mutation):
                self.write(self.report,{**self.operator,**mutation})
                with self.assertRaises(Rejected):self.validate()

    def test_image_source_route_and_budget_changes_require_new_transport(self):
        changes=[{'image_id':'sha256:'+'a'*64},{'probe_sha256':'a'*64},{'observer_sha256':'b'*64},
          {'endpoint_config_sha256':'c'*64},{'qualification_profile_sha256':'d'*64},
          {'qualification_profile':{**support.QUALIFICATION_PROFILE,'execution_deadline_seconds':121}}]
        for mutation in changes:
            with self.subTest(mutation=mutation),self.assertRaises(Rejected):self.validate(binding={**self.binding,**mutation})

    def test_native_source_snapshot_must_match(self):
        source=self.case/'native-sources'/Path(support.PROBE_SOURCE).name
        source.chmod(0o600);source.write_bytes(b'changed executable');source.chmod(0o400)
        with self.assertRaisesRegex(Rejected,'admission_native_source_mismatch'):self.validate()

    def test_malformed_private_report_fields_are_rejected(self):
        for mutation in ({'cleanup':[]},{'source_sha256':[]},{'admission_binding':[]}):
            with self.subTest(mutation=mutation):
                self.write(self.report,{**self.operator,**mutation})
                with self.assertRaises(Rejected):self.validate()

    def test_different_case_config_and_unconfirmed_cleanup_are_rejected(self):
        self.write(self.case/'model.json',{**self.model,'base_url':'https://different.invalid/v1'})
        with self.assertRaisesRegex(Rejected,'admission_case_model_mismatch'):self.validate()
        self.write(self.case/'model.json',self.model)
        self.write(self.report,{**self.operator,'cleanup':{'execution_stop_confirmed':False}})
        with self.assertRaisesRegex(Rejected,'admission_cleanup_unconfirmed'):self.validate()

    def test_intercept_marker_must_prove_zero_http_and_exact_cap(self):
        for mutation in ({'kind':'request_started'},{'actual_http_sent':True},{'output_cap':2048},
                         {'model_sha256':'a'*64},{'url_sha256':'b'*64}):
            with self.subTest(mutation=mutation):
                self.write(self.case/'evidence/transport.jsonl',{**self.marker,**mutation})
                with self.assertRaises(Rejected):self.validate()

    def test_symlink_ancestor_and_public_or_foreign_private_report_are_rejected(self):
        alias=self.root/'alias';alias.symlink_to(self.root/'.private',target_is_directory=True)
        with self.assertRaisesRegex(Rejected,'symlink_path_rejected'):self.validate(alias/'transport/operator.json')
        self.report.chmod(0o644)
        with self.assertRaisesRegex(Rejected,'private_file_permissions'):self.validate()
        self.report.chmod(0o600)
        with patch('probe_hermes.os.geteuid',return_value=os.getuid()+1),self.assertRaises(Rejected):self.validate()

    def test_key_reference_is_not_read_or_bound(self):
        config=self.root/'.private/model-endpoints.json'
        data={'schema_version':1,'endpoints':[{'id':'secondary',**self.model,'api_key_file':'does-not-exist'}]}
        self.write(config,data);before=support.endpoint_configuration(config)
        data['endpoints'][0]['api_key_file']='another-nonexistent-file';self.write(config,data)
        after=support.endpoint_configuration(config)
        self.assertEqual(before,after)
        self.assertEqual(support.qualification_binding(support.QUALIFIED_IMAGE,before,self.sources),
                         support.qualification_binding(support.QUALIFIED_IMAGE,after,self.sources))

    def test_missing_admission_stops_smoke_before_key_and_dispatch(self):
        if os.geteuid()==0:self.skipTest('qualification entry point requires nonroot')
        import run_dsh
        from prepare_dsh import COMMIT,VERSION
        for source,data in self.sources.items():
            path=self.root/source;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(data)
        provenance=self.root/'provenance.json';self.write(provenance,{'image_id':support.QUALIFIED_IMAGE,'source_commit':COMMIT,'version':VERSION})
        config=self.root/'.private/model-endpoints.json';self.write(config,{'schema_version':1,'endpoints':[{'id':'secondary',**self.model,'api_key_file':'missing'}]})
        args=SimpleNamespace(root=self.root,case=self.root/'.private/smoke',config=config,provenance=provenance,mode='smoke',admission_report=None)
        with patch.object(run_dsh.shutil,'disk_usage',return_value=SimpleNamespace(free=100*1024**3)),\
             patch.object(run_dsh,'select_endpoint') as key,patch.object(run_dsh,'docker') as docker,\
             patch.object(run_dsh,'validate_image') as image:
            with self.assertRaisesRegex(Rejected,'admission_report_required'):run_dsh.execute(args)
            key.assert_not_called();docker.assert_not_called();image.assert_not_called()
        self.assertFalse(args.case.exists())

if __name__=='__main__':unittest.main()
