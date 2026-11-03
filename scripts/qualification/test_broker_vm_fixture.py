"""Synthetic fixture grammar tests; real VM evidence is recorded separately."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('broker_vm_fixture', Path(__file__).with_name('broker_vm_fixture.py'))
f = importlib.util.module_from_spec(spec); spec.loader.exec_module(f)

class FixtureReportTests(unittest.TestCase):
    def setUp(self):
        self.nonce = 'a'*32
        self.value = {'kind':'harniqo-vm-probe-v1','nonce':self.nonce,'uid':0,'boot_id':'9e10b06d-d343-4ca4-a00a-256bd603b3b3'}
    def test_valid_guest_report(self):
        self.assertEqual(f.guest_report(json.dumps(self.value).encode(), self.nonce), self.value)
    def reject(self, value):
        with self.assertRaises((f.FixtureError, ValueError)): f.guest_report(json.dumps(value).encode(), self.nonce)
    def test_challenge_is_bound(self):
        self.reject(dict(self.value, nonce='b'*32))
    def test_guest_must_be_root_but_not_boolean(self):
        for value in [False, True, 1, '0']:
            with self.subTest(value=value): self.reject(dict(self.value, uid=value))
    def test_boot_id_required(self):
        for value in ['', 'not-a-boot', 1]:
            with self.subTest(value=value): self.reject(dict(self.value, boot_id=value))
    def test_no_extra_guest_fields(self): self.reject(dict(self.value, command='echo never'))
    def test_missing_nonce(self):
        value = dict(self.value); del value['nonce']; self.reject(value)
    def test_other_protocol_not_accepted(self): self.reject(dict(self.value, kind='other'))
    def test_duplicate_members_rejected(self):
        with self.assertRaises(ValueError): f.guest_report(b'{"uid":0,"uid":0}',self.nonce)
    def test_nonfinite_rejected(self):
        with self.assertRaises(ValueError): f.guest_report(b'{"uid":NaN}',self.nonce)
    def test_frame_limit(self):
        with self.assertRaises(ValueError): f.guest_report(b' '*16385+json.dumps(self.value).encode(),self.nonce)
    def test_invalid_utf8(self):
        with self.assertRaises(ValueError): f.guest_report(b'\xff',self.nonce)
    def test_non_root_plan_rejected_before_config(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'plan.json';p.write_text('{}');p.chmod(0o640)
            with self.assertRaises(ValueError): f.load_plan(p,'0'*64)


class FixtureStreamTests(unittest.TestCase):
    def exercise(self, fn):
        import socket
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);p.chmod(0o700)
            console=f.BoundedConsole.create(p/'console',capacity_bytes=65536)
            report=f.BoundedConsole.create(p/'report',capacity_bytes=16384)
            streams=f.StreamCollector(console,report,'a'*32)
            left,right=socket.socketpair()
            try:fn(p,streams,left,right)
            finally:streams.close();left.close();right.close();console.close();report.close()
    def test_drain_keeps_buffered_tail_before_eof(self):
        def fn(p,s,left,right):
            s.attach('console',left);right.sendall(b'x'*24576);right.shutdown(1)
            s.poll(.01);self.assertEqual((p/'console').stat().st_size,8192)
            s.drain(.5);self.assertEqual((p/'console').read_bytes(),b'x'*24576);self.assertIn('console',s.closed)
        self.exercise(fn)
    def test_missing_eof_fails_with_partial_console_preserved(self):
        def fn(p,s,left,right):
            s.attach('console',left);right.sendall(b'partial')
            with self.assertRaisesRegex(f.FixtureError,'fixture_console_eof_missing'):s.drain(.03)
            self.assertEqual((p/'console').read_bytes(),b'partial')
        self.exercise(fn)
    def test_invalid_nonce_frame_remains_raw(self):
        def fn(p,s,left,right):
            s.attach('report',left);raw=b'{"kind":"wrong","nonce":"secret-frame"}\n';right.sendall(raw)
            with self.assertRaises(f.FixtureError):s.poll(.1)
            self.assertEqual((p/'report').read_bytes(),raw)
        self.exercise(fn)


    def test_failed_guest_validation_still_drains_console_tail(self):
        import socket
        def fn(p,s,left,right):
            a,b=socket.socketpair()
            try:
                s.attach('report',a);s.attach('console',left)
                bad=b'{"kind":"wrong"}\n';b.sendall(bad);b.shutdown(1)
                right.sendall(b't'*24576);right.shutdown(1)
                with self.assertRaises(f.FixtureError):s.poll(.1)
                s.drain(.5,validate=False)
                self.assertEqual((p/'console').read_bytes(),b't'*24576)
                self.assertEqual((p/'report').read_bytes(),bad)
            finally:a.close();b.close()
        self.exercise(fn)

    def test_successful_drain_rejects_extra_guest_frame(self):
        import socket
        def fn(p,s,left,right):
            a,b=socket.socketpair()
            try:
                s.attach('report',a);s.attach('console',left)
                value={'kind':'harniqo-vm-probe-v1','nonce':'a'*32,'uid':0,'boot_id':'9e10b06d-d343-4ca4-a00a-256bd603b3b3'}
                raw=json.dumps(value).encode()+b'\n';b.sendall(raw);s.poll(.1)
                b.sendall(raw);right.sendall(b'tail');right.shutdown(1)
                with self.assertRaisesRegex(f.FixtureError,'fixture_duplicate_report'):s.drain(.5)
                s.drain(.5,validate=False)
                self.assertEqual((p/'report').read_bytes(),raw+raw)
                self.assertEqual((p/'console').read_bytes(),b'tail')
            finally:a.close();b.close()
        self.exercise(fn)

class FixtureFailureReceiptTests(unittest.TestCase):
    def test_existing_final_file_preserves_full_stdout_fallback(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);(p/'fixture-result.json').write_bytes(b'original')
            receipt={'event':'fixture_stopped','status':'FAILED','errors':[{'phase':'resources','code':'original_failure'}],'console':{'durable_bytes':123}}
            with patch.object(f,'emit') as emitted:f.finalize_receipt(p,receipt)
            self.assertEqual((p/'fixture-result.json').read_bytes(),b'original')
            self.assertEqual(emitted.call_args.args[0]['console']['durable_bytes'],123)
            self.assertEqual(receipt['errors'][0]['code'],'original_failure')
            self.assertEqual(receipt['errors'][-1]['phase'],'receipt_persistence')
    def test_receipt_fsync_error_retains_console_context(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as d:
            receipt={'status':'PASSED','errors':[],'console':{'durable_bytes':41}}
            with patch.object(f.os,'fsync',side_effect=OSError(28,'full')),patch.object(f,'emit') as emitted:f.finalize_receipt(Path(d),receipt)
            self.assertEqual(emitted.call_args.args[0]['console']['durable_bytes'],41)
            self.assertEqual(receipt['status'],'FAILED')

if __name__ == '__main__': unittest.main()
