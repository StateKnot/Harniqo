#!/usr/bin/env python3
"""Operator error/ownership tests; no Docker, listeners or model requests."""
from __future__ import annotations

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
        def still_live():
            try:
                fields = (Path("/proc") / str(identity["pid"]) / "stat").read_text().rsplit(")", 1)[1].split()
                return int(fields[19]) == identity["start_ticks"] and fields[0] != "Z"
            except FileNotFoundError:
                return False
        try:
            deadline = time.monotonic() + 3
            while still_live() and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertFalse(still_live(), "probe worker survived its parent")
        finally:
            if still_live():
                os.kill(identity["pid"], signal.SIGKILL)

if __name__ == "__main__":
    unittest.main()
