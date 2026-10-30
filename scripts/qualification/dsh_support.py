"""Local Docker and immutable artifact seams for trusted DSH qualification."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
from urllib.parse import urlsplit
from probe_hermes import (IMAGE, require, write_report, no_links, private_directory,
                          read_file, json_value)
from run_octop import docker_args, docker_env
from run_hermes_qualification import cleanup, docker, inspect, owned, TASK_LABEL

DOCKER_VARIABLES=('DOCKER_HOST','DOCKER_CONTEXT','DOCKER_TLS_VERIFY','DOCKER_CERT_PATH')
QUALIFIED_IMAGE='sha256:664991f530811b5390640625ea9fc585e2e279350170eb0101447aa56946c997'
PROBE_SOURCE='scripts/qualification/probe_dsh.mjs'
OBSERVER_SOURCE='deploy/qualification/dsh/transport_audit.mjs'
QUALIFICATION_PROFILE={'sdk_profile':'sdk','max_http_requests':6,'max_output_tokens':1024,
  'execution_deadline_seconds':120,'native_watchdog_seconds':110,'provider_retries':0,
  'approval_requests':True,'native_tools':['read','write']}

def canonical_digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()

def endpoint_configuration(path, endpoint_id='secondary'):
    """Read model routing only. Do not open or return the credential file."""
    path=no_links(path);private_directory(path.parent)
    config=json_value(read_file(path,65536))
    require(isinstance(config,dict) and config.get('schema_version')==1
            and isinstance(config.get('endpoints'),list),'invalid_endpoint_configuration')
    selected=[r for r in config['endpoints'] if isinstance(r,dict) and r.get('id')==endpoint_id]
    require(len(selected)==1,'endpoint_identity_missing_or_duplicate')
    model,base=selected[0].get('model'),selected[0].get('base_url')
    require(model=='qwen3.8-flash' and isinstance(base,str) and len(base)<=2048,'model_not_authorized')
    parsed=urlsplit(base)
    require(parsed.scheme=='https' and parsed.hostname and not parsed.username and not parsed.password
            and not parsed.query and not parsed.fragment,'invalid_endpoint_url')
    return {'model':model,'base_url':base}

def qualification_binding(image_id, model, sources):
    """Bind the native code, exact route (never its key), and fixed budget."""
    profile=json.loads(json.dumps(QUALIFICATION_PROFILE))
    return {'schema_version':1,'image_id':image_id,
      'probe_sha256':hashlib.sha256(sources[PROBE_SOURCE]).hexdigest(),
      'observer_sha256':hashlib.sha256(sources[OBSERVER_SOURCE]).hexdigest(),
      'endpoint_config_sha256':canonical_digest({'endpoint_id':'secondary',**model}),
      'qualification_profile':profile,
      'qualification_profile_sha256':canonical_digest(profile)}

def validate_admission(report_path, binding, model):
    """Require an owned private, successful, zero-HTTP native transport case.

    The operator implementation may change. Its native probe, observer, route,
    immutable image and budget must still match. Legacy reports without this
    binding remain historical evidence and cannot authorize a new submission.
    """
    require(report_path is not None,'admission_report_required')
    report_path=no_links(report_path);private_directory(report_path.parent)
    require(report_path.name=='operator.json','admission_operator_report_required')
    raw=read_file(report_path,65536);operator=json_value(raw)
    require(isinstance(operator,dict) and operator.get('mode')=='transport'
      and operator.get('status')=='passed' and operator.get('native_preflight_passed') is True,
      'admission_transport_not_passed')
    require(operator.get('image_id')==binding['image_id'],'admission_image_mismatch')
    require(operator.get('admission_binding')==binding,'admission_binding_mismatch')
    require(all(operator.get(name)==QUALIFICATION_PROFILE[name] for name in
      ('max_http_requests','max_output_tokens','execution_deadline_seconds')),'admission_budget_mismatch')
    stopped=operator.get('cleanup',{})
    require(isinstance(stopped,dict) and stopped.get('execution_stop_confirmed') is True and stopped.get('logs_complete') is True
      and stopped.get('container_removed') is True,'admission_cleanup_unconfirmed')
    case=report_path.parent;evidence=private_directory(case/'evidence')
    snapshots=private_directory(case/'native-sources')
    recorded_sources=operator.get('source_sha256',{})
    require(isinstance(recorded_sources,dict),'admission_native_source_mismatch')
    for source,name in ((PROBE_SOURCE,'probe_sha256'),(OBSERVER_SOURCE,'observer_sha256')):
      require(recorded_sources.get(source)==binding[name]
        and hashlib.sha256(read_file(snapshots/Path(source).name,262144)).hexdigest()==binding[name],
        'admission_native_source_mismatch')
    probe_raw=read_file(evidence/'probe.json',65536);probe=json_value(probe_raw)
    require(isinstance(probe,dict) and probe.get('mode')=='transport' and probe.get('status')=='passed'
      and probe.get('native_preflight_passed') is True and probe.get('native_transport_path_observed_without_http') is True
      and probe.get('component_only_intentional_provider_interception') is True
      and probe.get('model')==model['model'],'admission_native_transport_not_passed')
    require(json_value(read_file(case/'model.json',65536))==model,'admission_case_model_mismatch')
    audit_raw=read_file(evidence/'transport.jsonl',65536)
    require(audit_raw.endswith(b'\n'),'admission_incomplete_audit')
    markers=[json_value(line) for line in audit_raw.splitlines()]
    require(len(markers)==1,'admission_unexpected_http_records')
    marker=markers[0]
    require(isinstance(marker,dict) and marker.get('schema')==1 and marker.get('kind')=='component_intercepted'
      and marker.get('actual_http_sent') is False and marker.get('model_matches_expected') is True
      and marker.get('output_cap')==QUALIFICATION_PROFILE['max_output_tokens']
      and marker.get('model_sha256')==hashlib.sha256(model['model'].encode()).hexdigest()
      and marker.get('url_sha256')==hashlib.sha256((model['base_url'].rstrip('/')+'/chat/completions').encode()).hexdigest(),
      'admission_intercept_marker_mismatch')
    return {'operator_sha256':hashlib.sha256(raw).hexdigest(),
      'probe_report_sha256':hashlib.sha256(probe_raw).hexdigest(),
      'audit_sha256':hashlib.sha256(audit_raw).hexdigest(),'binding':binding}

@contextmanager
def local_docker_environment():
    """The inherited helpers are synchronous within this single CLI process."""
    saved={key:os.environ.pop(key) for key in DOCKER_VARIABLES if key in os.environ}
    try:yield
    finally:
        for key in DOCKER_VARIABLES:os.environ.pop(key,None)
        os.environ.update(saved)

def command(*args, runner=None, **kwargs):
    kwargs.setdefault('check',True)
    return (runner or subprocess.run)(docker_args(list(args)),env=docker_env(),**kwargs)

def validate_image(identity, *, expected=None, runner=None):
    require(isinstance(identity,str) and IMAGE.fullmatch(identity),'immutable_image_required')
    require(expected is None or identity==expected,'pinned_image_mismatch')
    result=command('docker','image','inspect',identity,runner=runner,capture_output=True,text=True,timeout=20)
    value=json.loads(result.stdout)[0]
    require(value.get('Id')==identity and value.get('Os')=='linux' and value.get('Architecture')=='amd64','installed_image_identity_mismatch')
    return value

def builder(command_args, attempt_directory, image, *, runner=None):
    """Always reconcile a dispatch, including a timeout before acknowledgement.

    Reuse the existing complete-log/PID-zero/immutable-ID cleanup implementation.
    A mismatched resource is retained; incomplete logs forbid removal.
    """
    token=secrets.token_hex(16);name='harniqo-dsh-prepare-'+token
    report={'schema_version':1,'kind':'dsh_credential_free_builder','attempt_id':token,
      'container_name':name,'image_id':image,'status':'running','dispatch':'intent','model_requests':0}
    identity=None
    write_report(attempt_directory/'operator.json',report)
    with local_docker_environment():
        try:
            args=['run','--detach','--name',name,'--label',TASK_LABEL+'='+token,*command_args]
            if runner is None:docker(*args,timeout=30)
            else:runner(*args,timeout=30)
            report['dispatch']='acknowledged'
            identity=owned(inspect(name),token,image);report['container_id']=identity
            result=docker('wait',identity,timeout=900)
            require(result.stdout.strip()==b'0','npm_runtime_install_failed')
            report['status']='passed'
        except BaseException as error:
            report.update(status='failed',failure={'code':getattr(error,'code',type(error).__name__)})
        finally:
            try:
                report['cleanup']=cleanup(name,token,image,identity,attempt_directory)
                require(report['cleanup'].get('execution_stop_confirmed') and report['cleanup'].get('logs_complete'),'builder_cleanup_not_confirmed')
            except BaseException as error:
                report.update(status='failed',cleanup_failure={'code':getattr(error,'code',type(error).__name__)})
            write_report(attempt_directory/'operator.json',report)
    require(report['status']=='passed','builder_attempt_failed')
    return report

def archive_image(image, archive, report, *, runner=None):
    """A failed save retains its partial; it never acquires the final name."""
    require(not archive.exists() and not archive.is_symlink(),'archive_already_exists')
    partial=archive.with_name(archive.name+'.'+secrets.token_hex(16)+'.partial')
    report['partial_archive_name']=partial.name
    try:
        command('docker','save','-o',str(partial),image,runner=runner,timeout=300)
        require(partial.is_file() and not partial.is_symlink() and partial.stat().st_size>0,'invalid_archive')
        with partial.open('rb') as stream:
            os.fsync(stream.fileno());digest=hashlib.file_digest(stream,'sha256').hexdigest()
        size=partial.stat().st_size;partial.chmod(0o444)
        os.link(partial,archive);partial.unlink()
        descriptor=os.open(archive.parent,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(descriptor)
        finally:os.close(descriptor)
        report.pop('partial_archive_name',None)
        return digest,size
    except BaseException:
        if partial.is_file() and not partial.is_symlink():
            report['partial_archive_bytes']=partial.stat().st_size
            report['partial_archive_sha256']=hashlib.sha256(partial.read_bytes()).hexdigest()
        raise
