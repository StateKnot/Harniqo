#!/usr/bin/env python3
"""Operator error/ownership tests; no Docker, listeners or model requests."""
from __future__ import annotations

import errno
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import run_hermes_qualification as operator


def _worker_still_live(identity):
    try:
        fields = (Path("/proc") / str(identity["pid"]) / "stat").read_text().rsplit(")", 1)[1].split()
        return int(fields[19]) == identity["start_ticks"] and fields[0] not in {"Z", "X"}
    except OSError as error:
        if error.errno in (errno.ENOENT, errno.ESRCH):
            return False
        raise


def _stop_worker_if_live(identity):
    """Clean up only the frozen worker; disappearance must not mask assertions."""
    if not _worker_still_live(identity):
        return
    try:
        descriptor = os.pidfd_open(identity["pid"], 0)
    except OSError as error:
        if error.errno == errno.ESRCH:
            return
        raise
    try:
        # A recycled PID may have been opened after the first /proc check.
        # Once rebound, a pidfd cannot signal a later process using that PID.
        if _worker_still_live(identity):
            try:
                signal.pidfd_send_signal(descriptor, signal.SIGKILL, None, 0)
            except OSError as error:
                if error.errno != errno.ESRCH:
                    raise
    finally:
        os.close(descriptor)


class ProcessLivenessTests(unittest.TestCase):
    identity = {"pid": 123, "start_ticks": 456}

    def stat(self, state="R", start_ticks=456):
        return "123 (worker) " + " ".join([state, *(["0"] * 18), str(start_ticks)])

    def test_stat_esrch_means_worker_disappeared(self):
        with patch.object(Path, "read_text", side_effect=ProcessLookupError(errno.ESRCH, "No such process")):
            self.assertFalse(_worker_still_live(self.identity))

    def test_stat_enoent_means_worker_disappeared(self):
        with patch.object(Path, "read_text", side_effect=FileNotFoundError(errno.ENOENT, "No such file")):
            self.assertFalse(_worker_still_live(self.identity))

    def test_stat_permission_failure_is_not_absence(self):
        with patch.object(Path, "read_text", side_effect=PermissionError(errno.EACCES, "Permission denied")):
            with self.assertRaises(PermissionError):
                _worker_still_live(self.identity)

    def test_stat_other_io_failure_is_not_absence(self):
        with patch.object(Path, "read_text", side_effect=OSError(errno.EIO, "Input/output error")):
            with self.assertRaises(OSError):
                _worker_still_live(self.identity)

    def test_matching_live_identity_is_still_live(self):
        with patch.object(Path, "read_text", autospec=True, return_value=self.stat()) as read:
            self.assertTrue(_worker_still_live(self.identity))
            self.assertEqual(read.call_args.args[0], Path("/proc/123/stat"))

    def test_reused_pid_is_not_the_original_worker(self):
        with patch.object(Path, "read_text", return_value=self.stat(start_ticks=457)):
            self.assertFalse(_worker_still_live(self.identity))

    def test_zombie_is_not_running(self):
        for state in ("Z", "X"):
            with self.subTest(state=state):
                with patch.object(Path, "read_text", return_value=self.stat(state=state)):
                    self.assertFalse(_worker_still_live(self.identity))


class WorkerCleanupTests(unittest.TestCase):
    identity = {"pid": 123, "start_ticks": 456}

    def test_absent_worker_never_opens_or_signals_a_pid(self):
        with patch(__name__ + '._worker_still_live', return_value=False), \
                patch.object(os, 'pidfd_open', create=True) as opened, \
                patch.object(signal, 'pidfd_send_signal', create=True) as sent:
            _stop_worker_if_live(self.identity)
            opened.assert_not_called()
            sent.assert_not_called()

    def test_same_identity_is_signaled_through_pidfd_and_descriptor_is_closed(self):
        with patch(__name__ + '._worker_still_live', side_effect=[True, True]), \
                patch.object(os, 'pidfd_open', create=True, return_value=99) as opened, \
                patch.object(signal, 'pidfd_send_signal', create=True) as sent, \
                patch.object(os, 'close') as closed, patch.object(os, 'kill') as numeric_kill:
            _stop_worker_if_live(self.identity)
            opened.assert_called_once_with(123, 0)
            sent.assert_called_once_with(99, signal.SIGKILL, None, 0)
            closed.assert_called_once_with(99)
            numeric_kill.assert_not_called()

    def test_reused_pid_after_open_is_not_signaled(self):
        with patch(__name__ + '._worker_still_live', side_effect=[True, False]), \
                patch.object(os, 'pidfd_open', create=True, return_value=99), \
                patch.object(signal, 'pidfd_send_signal', create=True) as sent, \
                patch.object(os, 'close') as closed:
            _stop_worker_if_live(self.identity)
            sent.assert_not_called()
            closed.assert_called_once_with(99)

    def test_worker_disappearing_before_pidfd_open_needs_no_cleanup(self):
        with patch(__name__ + '._worker_still_live', return_value=True), \
                patch.object(os, 'pidfd_open', create=True, side_effect=ProcessLookupError(errno.ESRCH, 'gone')), \
                patch.object(signal, 'pidfd_send_signal', create=True) as sent, \
                patch.object(os, 'close') as closed:
            _stop_worker_if_live(self.identity)
            sent.assert_not_called()
            closed.assert_not_called()

    def test_worker_disappearing_after_identity_check_does_not_mask_result(self):
        with patch(__name__ + '._worker_still_live', side_effect=[True, True]), \
                patch.object(os, 'pidfd_open', create=True, return_value=99), \
                patch.object(signal, 'pidfd_send_signal', create=True, side_effect=ProcessLookupError(errno.ESRCH, 'gone')), \
                patch.object(os, 'close') as closed:
            _stop_worker_if_live(self.identity)
            closed.assert_called_once_with(99)

    def test_permission_and_io_errors_at_open_or_signal_remain_errors(self):
        for stage in ('open', 'signal'):
            for code in (errno.EACCES, errno.EPERM, errno.EIO):
                with self.subTest(stage=stage, errno=code):
                    with patch(__name__ + '._worker_still_live', return_value=True), \
                            patch.object(os, 'pidfd_open', create=True, return_value=99,
                                         side_effect=OSError(code, 'fixture error') if stage == 'open' else None), \
                            patch.object(signal, 'pidfd_send_signal', create=True,
                                         side_effect=OSError(code, 'fixture error') if stage == 'signal' else None), \
                            patch.object(os, 'close') as closed:
                        with self.assertRaises(OSError) as caught:
                            _stop_worker_if_live(self.identity)
                        self.assertEqual(caught.exception.errno, code)
                        if stage == 'signal':
                            closed.assert_called_once_with(99)
                        else:
                            closed.assert_not_called()

    def exercise_parent_death_call_site(self, liveness, *, times=None):
        response = subprocess.CompletedProcess([], 0, json.dumps(self.identity), '')
        with patch.object(subprocess, 'run', return_value=response), \
                patch(__name__ + '._worker_still_live', side_effect=liveness), \
                patch.object(os, 'pidfd_open', create=True, return_value=99), \
                patch.object(signal, 'pidfd_send_signal', create=True,
                             side_effect=ProcessLookupError(errno.ESRCH, 'gone')), \
                patch.object(os, 'close') as closed, \
                patch.object(os, 'kill', side_effect=AssertionError('numeric PID signal is unsafe')), \
                patch.object(time, 'monotonic', side_effect=times or [0.0]), \
                patch.object(time, 'sleep', side_effect=AssertionError('unexpected fixture wait')):
            ParentDeathTests().test_detached_worker_is_not_left_running_when_probe_parent_dies()
            closed.assert_called_once_with(99)

    def test_actual_call_site_accepts_only_a_successful_assertion_despite_cleanup_esrch(self):
        self.exercise_parent_death_call_site([False, False, True, True])

    def test_actual_call_site_keeps_a_surviving_worker_assertion_failed(self):
        with self.assertRaisesRegex(AssertionError, 'probe worker survived its parent'):
            self.exercise_parent_death_call_site([True, True, True, True], times=[0.0, 4.0])


class OwnershipTests(unittest.TestCase):
    image = "sha256:" + "a" * 64
    identity = "b" * 64

    def value(self, **changes):
        return {"id": self.identity, "image": self.image, "attempt": "ours",
                "running": True, "pid": 123, **changes}

    def test_foreign_label_is_never_stopped(self):
        with patch.object(operator, "inspect", return_value=self.value(attempt="other")), \
                patch.object(operator, "docker") as docker:
            with self.assertRaisesRegex(operator.Rejected, "ownership"):
                operator.cleanup("case", "ours", self.image)
            docker.assert_not_called()

    def test_replaced_name_is_never_stopped(self):
        with patch.object(operator, "inspect", return_value=self.value(id="c" * 64)), \
                patch.object(operator, "docker") as docker:
            with self.assertRaisesRegex(operator.Rejected, "ownership"):
                operator.cleanup("case", "ours", self.image, self.identity)
            docker.assert_not_called()

    def test_removal_requires_observed_zero_pid(self):
        with patch.object(operator, "inspect", side_effect=[self.value(), self.value(running=False, pid=0), None]), \
                patch.object(operator, "docker") as docker:
            result = operator.cleanup("case", "ours", self.image, self.identity)
            self.assertTrue(result["execution_stop_confirmed"])
            self.assertEqual([c.args[:2] for c in docker.call_args_list], [("stop", "--time"), ("rm", self.identity)])

    def test_running_false_with_live_pid_never_counts_as_stopped(self):
        with patch.object(operator, "inspect", side_effect=[self.value(), self.value(running=False, pid=123)]), \
                patch.object(operator, "docker") as docker:
            with self.assertRaisesRegex(operator.Rejected, "stop_not_confirmed"):
                operator.cleanup("case", "ours", self.image)
            self.assertEqual(len(docker.call_args_list), 1)

    def test_incomplete_log_archive_preserves_stopped_container(self):
        with patch.object(operator, 'inspect', side_effect=[self.value(), self.value(running=False, pid=0), None]), \
                patch.object(operator, 'docker') as docker, \
                patch.object(operator, 'capture_logs', side_effect=[{'complete': False}, {'complete': True}]):
            result = operator.cleanup('case', 'ours', self.image, self.identity, Path('/private/case'))
            self.assertTrue(result['execution_stop_confirmed'])
            self.assertFalse(result['logs_complete'])
            self.assertFalse(result['container_removed'])
            self.assertEqual(result['container_id'], self.identity)
            self.assertEqual([call.args[0] for call in docker.call_args_list], ['stop'])

    def test_absence_is_not_stop_acknowledgement(self):
        with patch.object(operator, "inspect", return_value=None), patch.object(operator, "docker") as docker:
            result = operator.cleanup("case", "ours", self.image, self.identity)
            self.assertFalse(result["execution_stop_confirmed"])
            docker.assert_not_called()

    def test_mounts_have_only_two_writable_scopes_and_no_secret_environment(self):
        command = operator.launch_arguments("case", "ours", self.image,
                                            Path("/workspace/.private/case"),
                                            Path("/workspace/.private/secrets/key"), 10010)
        mounts = [command[i + 1] for i, item in enumerate(command) if item == "--mount"]
        self.assertEqual(len(mounts), 5)
        self.assertEqual(len([v for v in mounts if not v.endswith(",readonly")]), 2)
        self.assertIn("127.0.0.1:10010:8642", command)
        self.assertNotIn("--env", command)
        self.assertNotIn("--privileged", command)
        self.assertIn("--read-only", command)

    def test_mount_delimiter_injection_rejected(self):
        with self.assertRaisesRegex(operator.Rejected, "mount_path"):
            operator.launch_arguments("case", "ours", self.image, Path("/x,readonly"), Path("/key"), 10010)


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.private = Path(self.temporary.name).resolve() / ".private"
        self.private.mkdir(mode=0o700)
        self.key = self.private / "key"
        operator.create_private(self.key, b"synthetic-test-key")
        self.config = self.private / "endpoints.json"

    def write(self, endpoints):
        operator.create_private(self.config, json.dumps({"schema_version": 1, "endpoints": endpoints}).encode())

    def item(self, **changes):
        return {"id": "allowed", "base_url": "https://models.example.test/v1", "model": "test-model",
                "api_key_file": "key", **changes}

    def test_relative_key_path_resolves_against_config_directory(self):
        self.write([self.item()])
        configuration, key = operator.select_endpoint(self.config, "allowed")
        self.assertEqual(key, self.key)
        self.assertEqual(set(configuration), {"base_url", "model"})

    def test_duplicate_selected_identity_rejected(self):
        self.write([self.item(), self.item()])
        with self.assertRaisesRegex(operator.Rejected, "duplicate"):
            operator.select_endpoint(self.config, "allowed")

    def test_credential_in_endpoint_rejected(self):
        self.write([self.item(base_url="https://user:password@models.example.test/v1")])
        with self.assertRaisesRegex(operator.Rejected, "endpoint_url"):
            operator.select_endpoint(self.config, "allowed")

    def test_public_key_file_rejected(self):
        self.write([self.item()])
        self.key.chmod(0o644)
        with self.assertRaisesRegex(operator.Rejected, "permissions"):
            operator.select_endpoint(self.config, "allowed")

    def test_key_symlink_rejected(self):
        self.write([self.item(api_key_file="link")])
        (self.private / "link").symlink_to(self.key)
        with self.assertRaisesRegex(operator.Rejected, "symlink"):
            operator.select_endpoint(self.config, "allowed")


@unittest.skipUnless(sys.platform == "linux", "Linux kernel parent-death verification")
class ParentDeathTests(unittest.TestCase):
    def test_detached_worker_is_not_left_running_when_probe_parent_dies(self):
        directory = str(Path(__file__).resolve().parent)
        worker = ("import os,json,time;from pathlib import Path;"
                  "s=Path('/proc/self/stat').read_text().rsplit(')',1)[1].split();"
                  "print(json.dumps({'pid':os.getpid(),'start_ticks':int(s[19])}),flush=True);time.sleep(30)")
        parent = ("import sys,subprocess,os;sys.path.insert(0,sys.argv[1]);import probe_hermes as n;"
                  "p=subprocess.Popen([sys.executable,'-c',sys.argv[2]],stdout=subprocess.PIPE,"
                  "text=True,**n.worker_process_options());print(p.stdout.readline().strip(),flush=True);os._exit(0)")
        result = subprocess.run([sys.executable, "-c", parent, directory, worker],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0)
        identity = json.loads(result.stdout)
        try:
            deadline = time.monotonic() + 3
            while _worker_still_live(identity) and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertFalse(_worker_still_live(identity), "probe worker survived its parent")
        finally:
            _stop_worker_if_live(identity)

if __name__ == "__main__":
    unittest.main()
