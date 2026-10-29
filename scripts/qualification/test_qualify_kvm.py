#!/usr/bin/env python3
"""Failure-evidence component tests with synthetic guests over real UNIX sockets.

Run: PYTHONDONTWRITEBYTECODE=1 python3 scripts/qualification/test_qualify_kvm.py -v
These tests start no VM, create no TCP port and do not qualify KVM hardware.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest

import qualify_kvm as qualification


class RoundEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="hkvm-", dir=Path("/tmp").resolve())
        self.work = Path(self.temporary.name)
        self.rounds = []
        self.logs = []
        self.streams = []
        self.clients = []
        self.instances = []
        for index, label in enumerate(["a", "b"], 1):
            instance = {"uuid": f"00000000-0000-4000-8000-{index:012d}", "name": "fixture-" + label,
                        "nonce": "fixture-nonce-" + label, "socket": self.work / (label + ".sock"),
                        "console_socket": self.work / (label + "-console.sock")}
            instance["listener"] = qualification.listener_at(instance["socket"])
            instance["console_listener"] = qualification.listener_at(instance["console_socket"])
            self.instances.append(instance)

    def tearDown(self):
        for streams in self.streams:
            if not streams.closed:
                streams._close()
        for client in self.clients:
            client.close()
        for instance in self.instances:
            instance["listener"].close()
            instance["console_listener"].close()
        for log in self.logs:
            os.close(log["fd"])
        self.temporary.cleanup()

    def guest(self, instance, round_index):
        return {"schema_version": 1, "instance_uuid": instance["uuid"], "nonce": instance["nonce"],
                "passed": True, "boot_count": round_index, "canary_preexisting": round_index == 2,
                "boot_id": f"{instance['name']}-boot-{round_index}",
                "previous_boot_id": f"{instance['name']}-boot-1" if round_index == 2 else None,
                "checks": {"synthetic_fixture": True}, "details": {"values": ["retained", round_index]}}

    def connect(self, instance, role):
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.clients.append(client)
        client.connect(str(instance["socket" if role == "report" else "console_socket"]))
        return client

    def send_report(self, instance, report):
        client = self.connect(instance, "report")
        client.sendall((json.dumps(report) + "\n").encode())
        client.close()

    def send_console(self, instance, *, close=True):
        client = self.connect(instance, "console")
        client.sendall(b"synthetic boot console\n")
        if close:
            client.close()
        return client

    def start_streams(self, evidence, round_index):
        streams = qualification.GuestStreams(self.instances, self.work, round_index, self.logs)
        self.streams.append(streams)
        evidence.streams = streams
        for instance in self.instances:
            streams.mark_start(instance)
        return streams

    def persist(self):
        path = self.work / "report.json"
        qualification.write(path, json.dumps({"rounds": self.rounds}, indent=2) + "\n")
        return json.loads(path.read_text())["rounds"]

    def passing_round(self, round_index):
        with qualification.RoundEvidence(self.rounds, round_index, self.instances) as evidence:
            self.assertIs(self.rounds[-1], evidence.record)
            self.assertEqual(evidence.record["status"], "running")
            streams = self.start_streams(evidence, round_index)
            for instance in self.instances:
                self.send_console(instance)
                self.send_report(instance, self.guest(instance, round_index))
            evidence.record["phase"] = "collect"
            received = streams.collect(2)
            evidence.record["phase"] = "validate"
            qualification.validate_round(received, self.instances, round_index,
                                         self.rounds[0]["guests"] if round_index == 2 else None)
            evidence.record["phase"] = "console_drain"
            streams.finish()
        return evidence

    def test_partial_report_survives_collection_timeout_and_serialization(self):
        first, second = self.instances
        raw_report = self.guest(first, 1)
        expected = copy.deepcopy(raw_report)
        with self.assertRaisesRegex(TimeoutError, "Guest stream deadline"):
            with qualification.RoundEvidence(self.rounds, 1, self.instances) as evidence:
                streams = self.start_streams(evidence, 1)
                for instance in self.instances:
                    self.send_console(instance)
                self.send_report(first, raw_report)
                evidence.record["phase"] = "collect"
                streams.collect(0.5)
        streams.finish()
        evidence.snapshot()  # Same refresh performed by the production cleanup finally.
        record = self.rounds[0]
        self.assertEqual(record["status"], "failed")
        self.assertEqual(record["phase"], "collect")
        self.assertEqual(record["failed_phase"], "collect")
        self.assertIn("TimeoutError", record["validation_error"])
        self.assertEqual(record["missing_guest_uuids"], [second["uuid"]])
        self.assertEqual(record["guests"], {first["uuid"]: expected})
        streams.received[first["uuid"]]["details"]["values"].append("later mutation")
        self.assertEqual(self.persist()[0]["guests"], {first["uuid"]: expected})

    def test_second_round_validation_failure_keeps_both_original_reports(self):
        self.passing_round(1)
        first_round = copy.deepcopy(self.rounds[0])
        reports = {item["uuid"]: self.guest(item, 2) for item in self.instances}
        reports[self.instances[1]["uuid"]]["previous_boot_id"] = "incorrect-prior-boot"
        with self.assertRaisesRegex(RuntimeError, "Cold restart/persistent canary"):
            with qualification.RoundEvidence(self.rounds, 2, self.instances) as evidence:
                streams = self.start_streams(evidence, 2)
                for instance in self.instances:
                    self.send_console(instance)
                    self.send_report(instance, reports[instance["uuid"]])
                evidence.record["phase"] = "collect"
                received = streams.collect(2)
                evidence.record["phase"] = "validate"
                qualification.validate_round(received, self.instances, 2, self.rounds[0]["guests"])
        streams.finish()
        evidence.snapshot()
        persisted = self.persist()
        self.assertEqual(persisted[0], first_round)
        self.assertEqual(persisted[1]["guests"], reports)
        self.assertEqual(persisted[1]["status"], "failed")
        self.assertEqual(persisted[1]["failed_phase"], "validate")
        self.assertEqual(persisted[1]["missing_guest_uuids"], [])
        self.assertIn("Cold restart/persistent canary proof failed", persisted[1]["validation_error"])

    def test_cleanup_snapshot_includes_report_received_while_draining_console(self):
        first, second = self.instances
        with self.assertRaises(TimeoutError):
            with qualification.RoundEvidence(self.rounds, 1, self.instances) as evidence:
                streams = self.start_streams(evidence, 1)
                held_console = self.send_console(first, close=False)
                self.send_console(second)
                self.send_report(first, self.guest(first, 1))
                evidence.record["phase"] = "collect"
                streams.collect(0.5)
        self.assertEqual(evidence.record["missing_guest_uuids"], [second["uuid"]])
        failures = []

        def late_report():
            try:
                self.send_report(second, self.guest(second, 1))
                deadline = time.monotonic() + 5
                while second["uuid"] not in streams.received and time.monotonic() < deadline:
                    time.sleep(0.005)
                if second["uuid"] not in streams.received:
                    failures.append("Late report was not consumed during console drain")
            except BaseException as error:
                failures.append(repr(error))
            finally:
                held_console.close()

        worker = threading.Thread(target=late_report, daemon=True)
        worker.start()
        try:
            streams.finish()
        finally:
            worker.join(timeout=6)
        self.assertFalse(worker.is_alive())
        self.assertEqual(failures, [])
        evidence.snapshot()
        persisted = self.persist()[0]
        self.assertEqual(set(persisted["guests"]), {first["uuid"], second["uuid"]})
        self.assertEqual(persisted["missing_guest_uuids"], [])
        self.assertEqual(persisted["status"], "failed")
        self.assertEqual(persisted["failed_phase"], "collect")
        self.assertIn("TimeoutError", persisted["validation_error"])

    def test_two_successful_rounds_keep_completion_status(self):
        self.passing_round(1)
        self.passing_round(2)
        for record in self.persist():
            self.assertEqual(record["status"], "passed")
            self.assertEqual(record["phase"], "complete")
            self.assertIsNone(record["validation_error"])
            self.assertEqual(record["missing_guest_uuids"], [])
            self.assertEqual(len(record["guests"]), 2)


if __name__ == "__main__":
    unittest.main()
