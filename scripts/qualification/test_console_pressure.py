"""Fault-operator decisions; no libvirt, VM, model, network or host quota changes."""
from dataclasses import replace
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import subprocess
import sys
import unittest
from unittest.mock import patch

import console_pressure as pressure
import qualify_resource_broker as operator
from apps.runner.resource_broker import Observation, StopEvidence, BrokerError


class ConsoleFixtureTests(unittest.TestCase):
    def test_fixture_is_explicit_bounded_and_separate_from_other_faults(self):
        a={'uuid':'a','nonce':'nonce-a'}; b={'uuid':'b'}
        def generate(**kwargs):
            return json.loads(operator.sealed_guest_config(a,b,Path('/workspace'),
                resource_profile='sealed-raw8g-q35-limited-v2',**kwargs).split('\n',1)[1])['bootcmd'][0][2]
        default=generate(pressure=True)
        fault=generate(pressure=True,console_pressure_mode=True)
        control=generate(pressure=False,console_pressure_mode=True)
        self.assertNotIn('HARNIQO-CONSOLE-READY',default)
        self.assertNotIn('HARNIQO-CONSOLE-READY',control)
        self.assertIn('HARNIQO-CONSOLE-READY',fault)
        self.assertIn('until=time.monotonic()+120',control)
        self.assertNotIn('guest_identity_guard',fault)
        self.assertIn('range(3)',fault)
        compile(fault,'<console-fault>','exec')

    def test_fault_modes_and_profile_reject_before_preparation(self):
        for memory,console,profile in [(True,True,'sealed-raw8g-q35-limited-v2'),
                                      (False,True,'sealed-raw8g-q35-v1')]:
            args=SimpleNamespace(timeout=120,guest_memory_pressure=memory,
                                 console_pressure=console,resource_profile=profile)
            with patch.object(operator.sys,'platform','linux'),patch.object(operator.platform,'machine',return_value='x86_64'),\
                 patch.object(operator.os,'geteuid',return_value=1000),patch.object(operator.sys,'version_info',(3,12)),\
                 patch.object(operator,'check_base') as prepare:
                with self.assertRaises(RuntimeError):operator.execute(args)
                prepare.assert_not_called()

    def test_frozen_console_budget_is_distinct_and_hash_bound(self):
        with tempfile.TemporaryDirectory() as folder:
            result={};root=Path(folder)
            operator.freeze_qualification_limits(root,result,console_mode=True)
            manifest=root/'qualification-limits.json'
            self.assertEqual(json.loads(manifest.read_text()),pressure.qualification_limits())
            self.assertNotEqual(result['qualification_limits'],operator.memory_pressure.qualification_limits())
            self.assertEqual(result['qualification_limits_sha256'],hashlib.sha256(manifest.read_bytes()).hexdigest())

    def test_complete_or_intentionally_truncated_archive_is_explicit(self):
        good={'saved':True,'failure_code':None,'truncated':False,'archive_sha256':'a'*64,
              'source_sha256':'a'*64,'archive_reserved_bytes':4096,'limit_bytes':4096}
        quota={**good,'failure_code':'console_capacity_exceeded','truncated':True}
        self.assertTrue(pressure.validate_archives([good],False))
        self.assertFalse(pressure.validate_archives([good,quota],False))
        self.assertTrue(pressure.validate_archives([good,quota],True))
        for invalid in ({**quota,'saved':False},{**quota,'failure_code':'console_io_failed'},
                        {**quota,'truncated':False}):
            self.assertFalse(pressure.validate_archives([good,invalid],True))
        self.assertFalse(pressure.validate_archives([{**good,'archive_sha256':'b'*64}],False))
        self.assertFalse(pressure.validate_archives([{**good,'archive_reserved_bytes':0}],False))

    @unittest.skipUnless(sys.platform=='linux','blocked pipe alarm uses Linux fixture target')
    def test_blocked_guest_stdout_has_an_independent_timeout(self):
        # Run the actual generated program with a shorter injected clock alarm.
        # Its report fd gets valid tokens, while its stdout pipe has no reader.
        # No /dev device, guest or host listener is created by this component.
        program=pressure.guest_program()
        bootstrap="""import os,time,signal
C={'nonce':'fixture'}
readfd,writefd=os.pipe()
os.write(writefd,(('a'*40+'\\n')*4).encode());os.close(writefd)
actual_open=os.open
os.open=lambda *args,**kwargs:readfd if args[0]=='/dev/virtio-ports/org.harniqo.m0' else actual_open(*args,**kwargs)
signal.alarm=lambda seconds:signal.setitimer(signal.ITIMER_REAL,.2 if seconds else 0)
"""
        with tempfile.TemporaryFile() as errors:
            child=subprocess.Popen([sys.executable,'-c',bootstrap+'\n'+program],stdout=subprocess.PIPE,stderr=errors)
            try:
                code=child.wait(timeout=3)
                self.assertNotEqual(code,0)
                errors.seek(0)
                self.assertIn(b'TimeoutError: console fixture deadline',errors.read())
            finally:
                if child.poll() is None:child.kill();child.wait()
                child.stdout.close()

    def test_late_predicate_and_poll_cannot_satisfy_deadline(self):
        for phase in ('predicate','poll'):
            clock=[0.0]
            def predicate():
                if phase=='predicate':clock[0]=2
                return phase=='predicate' or clock[0]>0
            def poll(_):clock[0]=2
            streams=SimpleNamespace(_poll=poll)
            with patch.object(pressure.time,'monotonic',side_effect=lambda:clock[0]):
                with self.assertRaises(TimeoutError):pressure._wait(predicate,streams,1,'deadline')


class ConsoleBrokerStopTests(unittest.TestCase):
    def invoke(self,*,method='libvirt_destroy_then_absent',cause='console_capacity_exceeded',
               mismatch=False,resource='a',revived=False,old_accepted=False):
        with tempfile.TemporaryDirectory() as folder:
            disk=Path(folder)/'owned.raw';disk.write_bytes(b'owned')
            stopped=Observation(resource,'stopped',3,1,StopEvidence(resource,101,method,'f'*64),stop_cause=cause)
            rebuilt=replace(stopped,generation=4) if mismatch else stopped
            calls=[]
            class Broker:
                def revoke(self,identity,why):calls.append((identity,why));return stopped
            class Rebuilt:
                def observe(self,_):return rebuilt
                def ensure(self,_):
                    if old_accepted:return stopped
                    raise BrokerError('generation_stale')
                def renew(self,_):raise BrokerError('resource_terminal')
            result={'checks':{},'observations':{}}
            with patch.object(operator.time,'time',return_value=100),\
                 patch.object(operator,'broker_from',return_value=Rebuilt()),\
                 patch.object(operator.kvm,'domain_ids',return_value={'a'} if revived else set()):
                returned=operator.stop_console_resource(Broker(),{}, {'uuid':'a','overlay':disk},result,object(),object())
            self.assertEqual(calls,[('a','console_capacity_exceeded')])
            self.assertEqual(returned[1],stopped)
            self.assertTrue(all(result['checks'].values()))
            self.assertEqual(result['console_old_grant']['code'],'resource_terminal')
            return result

    def test_formal_stop_and_reconstruction_reject_old_authority(self):self.invoke()

    def test_absence_without_broker_destroy_is_insufficient(self):
        with self.assertRaises(RuntimeError):self.invoke(method='libvirt_absent_after_confirmed_start')

    def test_wrong_resource_or_stop_reason_cannot_satisfy_quota_stop(self):
        for kwargs in ({'resource':'foreign'},{'cause':'lease_expired_or_clock_changed'}):
            with self.assertRaises(RuntimeError):self.invoke(**kwargs)

    def test_tombstone_change_revival_and_old_authority_each_fail(self):
        for kwargs in ({'mismatch':True},{'revived':True},{'old_accepted':True}):
            with self.assertRaises(RuntimeError):self.invoke(**kwargs)


if __name__=='__main__':unittest.main()
