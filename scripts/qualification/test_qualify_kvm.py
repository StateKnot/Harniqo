#!/usr/bin/env python3
"""Failure-evidence component tests with synthetic guests over real UNIX sockets.

Run: PYTHONDONTWRITEBYTECODE=1 python3 scripts/qualification/test_qualify_kvm.py -v
These tests start no VM, create no TCP port and do not qualify KVM hardware.
"""
from __future__ import annotations

import copy
import errno
import hashlib
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

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
            if not log["closed"]:
                try:
                    log.close()
                except qualification.ConsoleStoreError:
                    # Fault tests intentionally invalidate their own log. The
                    # sink still owns and releases both of its descriptors.
                    pass
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

    def poll_until(self, streams, predicate, timeout=2):
        deadline = time.monotonic() + timeout
        while not predicate():
            if time.monotonic() >= deadline:
                self.fail("Expected socket evidence was not received before its deadline")
            streams._poll(min(0.02, deadline - time.monotonic()))

    def reports_directory(self):
        reports = self.work / "reports"
        reports.mkdir(mode=0o700)
        return reports

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

    def test_console_quota_is_exposed_once_and_other_guest_keeps_fresh_evidence(self):
        first, second = self.instances
        evidence = qualification.RoundEvidence(self.rounds, 1, self.instances)
        with patch.object(qualification, "MAX_CONSOLE_BYTES", 8192):
            streams = self.start_streams(evidence, 1)
        a = self.connect(first, "console")
        prefix = b"preserved-A-" * 512
        a.sendall(prefix)
        self.poll_until(streams, lambda: streams.logs[first["uuid"]]["bytes_saved"] == len(prefix))
        a.sendall(b"x" * 8192)
        with self.assertRaises(qualification.GuestConsoleError) as failure:
            self.poll_until(streams, lambda: streams.logs[first["uuid"]]["failure_exposed"])
        log = streams.logs[first["uuid"]]
        self.assertEqual(failure.exception.instance_uuid, first["uuid"])
        self.assertEqual(failure.exception.failure_code, log["failure_code"])
        self.assertEqual(log["failure_code"], "console_capacity_exceeded")
        self.assertNotIn(str(self.work), str(failure.exception))
        self.assertEqual(log["bytes_saved"], len(prefix))
        self.assertEqual(log["durable_bytes"], len(prefix))
        self.assertEqual(log["source_path"].stat().st_size, len(prefix))
        self.assertGreaterEqual(log["reserved_bytes"], log["limit_bytes"])
        a.sendall(b"discarded-after-first-failure")
        a.close()
        b = self.connect(second, "console")
        fresh = b"B-fresh-after-A-quota\n"
        b.sendall(fresh)
        b.close()
        self.send_report(first, self.guest(first, 1))
        self.send_report(second, self.guest(second, 1))
        # Neither repeated A drain nor EOF rethrows its first failure. B gets
        # a newly generated console and report after A's failure was observed.
        received = streams.collect(2)
        streams.finish()
        self.assertEqual(set(received), {first["uuid"], second["uuid"]})
        self.assertEqual(os.pread(streams.logs[second["uuid"]]["fd"], len(fresh), 0), fresh)
        self.assertEqual(log["bytes_seen"], len(prefix) + 8192 + len(b"discarded-after-first-failure"))
        self.assertEqual(log["discarded_bytes"], log["bytes_seen"] - len(prefix))
        self.assertTrue(log["truncated"])
        self.assertTrue(all(item["stream_closed"] for item in streams.logs.values()))
        reports = self.reports_directory()
        for item in self.logs:
            qualification.save_console(item, reports)
        self.assertEqual((reports / log["source_path"].name).read_bytes(), prefix)
        self.assertTrue(log["saved"])
        self.assertTrue(log["closed"])
        self.assertTrue(log["failure_code"])
        self.assertEqual(log["source_sha256"], hashlib.sha256(prefix).hexdigest())
        self.assertEqual(log["archive_sha256"], log["source_sha256"])
        self.assertGreaterEqual(log["archive_reserved_bytes"], log["limit_bytes"])
        json.dumps([{k: str(v) if isinstance(v, Path) else v for k, v in item.items()
                     if k not in {"fd", "inode"}} for item in self.logs])

    def test_console_io_failure_is_resource_bound_and_does_not_block_another_stream(self):
        first, second = self.instances
        evidence = qualification.RoundEvidence(self.rounds, 1, self.instances)
        streams = self.start_streams(evidence, 1)
        a = self.connect(first, "console")
        a.sendall(b"committed-prefix")
        self.poll_until(streams, lambda: streams.logs[first["uuid"]]["bytes_saved"] == len(b"committed-prefix"))
        a.sendall(b"rejected-write")
        with patch("apps.runner.console_store.os.pwrite", side_effect=OSError(errno.EIO, "private filesystem detail")):
            with self.assertRaises(qualification.GuestConsoleError) as failure:
                self.poll_until(streams, lambda: streams.logs[first["uuid"]]["failure_exposed"])
        log = streams.logs[first["uuid"]]
        self.assertEqual(failure.exception.instance_uuid, first["uuid"])
        self.assertNotIn("private filesystem detail", str(failure.exception))
        self.assertEqual(log["bytes_saved"], len(b"committed-prefix"))
        a.close()
        self.send_console(second)
        self.send_report(second, self.guest(second, 1))
        self.poll_until(streams, lambda: second["uuid"] in streams.received)
        streams.finish()
        self.assertTrue(log["stream_closed"])
        self.assertTrue(streams.logs[second["uuid"]]["bytes_saved"])

    def test_console_archive_rejects_replaced_identity_and_releases_its_owner(self):
        evidence = self.passing_round(1)
        log = evidence.streams.logs[self.instances[0]["uuid"]]
        original = log["source_path"]
        original.rename(original.with_suffix(".preserved"))
        original.write_bytes(b"foreign replacement")
        original.chmod(0o600)
        reports = self.reports_directory()
        with self.assertRaises(qualification.ConsoleStoreError):
            qualification.save_console(log, reports)
        self.assertFalse(log["saved"])
        self.assertTrue(log["closed"])
        self.assertTrue(log["archive_failure_code"])
        self.assertEqual(original.read_bytes(), b"foreign replacement")
        self.assertFalse((reports / original.name).exists())

    def test_failed_partial_write_is_archived_with_its_durable_boundary(self):
        first, second = self.instances
        evidence = qualification.RoundEvidence(self.rounds, 1, self.instances)
        streams = self.start_streams(evidence, 1)
        a = self.connect(first, "console")
        committed = b"committed-A-prefix"
        a.sendall(committed)
        self.poll_until(streams, lambda: streams.logs[first["uuid"]]["bytes_saved"] == len(committed))
        actual_pwrite = os.pwrite
        calls = []

        def partial_then_error(fd, data, offset):
            calls.append(offset)
            if len(calls) == 1:
                return actual_pwrite(fd, data[:5], offset)
            raise OSError(errno.EIO, "private partial-write error")

        a.sendall(b"uncommitted-trailing-data")
        with patch("apps.runner.console_store.os.pwrite", side_effect=partial_then_error):
            with self.assertRaises(qualification.GuestConsoleError):
                self.poll_until(streams, lambda: streams.logs[first["uuid"]]["failure_exposed"])
        a.close()
        self.send_console(second)
        streams.finish()
        log = streams.logs[first["uuid"]]
        self.assertEqual(log["bytes_saved"], len(committed))
        self.assertEqual(log["durable_bytes"], len(committed))
        reports = self.reports_directory()
        qualification.save_console(log, reports)
        self.assertEqual((reports / log["source_path"].name).read_bytes(), committed + b"uncom")
        self.assertEqual(log["archive_bytes"], len(committed) + 5)
        self.assertEqual(log["archive_durable_bytes"], len(committed))
        self.assertEqual(log["source_sha256"], hashlib.sha256(committed + b"uncom").hexdigest())
        self.assertEqual(log["archive_sha256"], log["source_sha256"])
        self.assertGreaterEqual(log["archive_reserved_bytes"], log["limit_bytes"])
        self.assertTrue(log["failure_code"])
        self.assertTrue(log["saved"])

    def test_archive_digest_readback_detects_same_size_corruption_and_next_archive_succeeds(self):
        evidence = self.passing_round(1)
        first, second = [evidence.streams.logs[item["uuid"]] for item in self.instances]
        reports = self.reports_directory()
        original = first["source_path"].read_bytes()
        create = qualification.BoundedConsole.create
        append = qualification.BoundedConsole.append
        archives = []
        corrupted = []

        def capture_archive(path, *, capacity_bytes):
            sink = create(path, capacity_bytes=capacity_bytes)
            if path.parent == reports:
                archives.append(sink)
            return sink

        def corrupt_after_append(sink, data):
            append(sink, data)
            if sink in archives and not corrupted:
                # Keep inode/size/mode/allocation unchanged so a metadata-only
                # archive check would incorrectly report successful evidence.
                fd = os.open(reports / first["source_path"].name,
                             os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
                try:
                    os.pwrite(fd, b"!", 0)
                    os.fsync(fd)
                finally:
                    os.close(fd)
                corrupted.append(True)

        with patch.object(qualification.BoundedConsole, "create", side_effect=capture_archive):
            with patch.object(qualification.BoundedConsole, "append", new=corrupt_after_append):
                with self.assertRaisesRegex(qualification.ConsoleStoreError, "console_archive_digest_mismatch"):
                    qualification.save_console(first, reports)
        self.assertEqual(corrupted, [True])
        self.assertFalse(first["saved"])
        self.assertTrue(first["closed"])
        self.assertEqual(first["archive_failure_code"], "console_archive_digest_mismatch")
        self.assertTrue(archives[0].snapshot()["closed"])
        self.assertEqual(first["source_path"].read_bytes(), original)
        self.assertEqual((reports / first["source_path"].name).stat().st_size, len(original))
        self.assertNotEqual(first["source_sha256"], first["archive_sha256"])
        qualification.save_console(second, reports)
        self.assertTrue(second["saved"])
        self.assertEqual(second["source_sha256"], second["archive_sha256"])
        self.assertGreaterEqual(second["archive_reserved_bytes"], second["limit_bytes"])

    def test_console_archive_sync_error_does_not_mark_saved_or_prevent_next_archive(self):
        evidence = self.passing_round(1)
        first, second = [evidence.streams.logs[item["uuid"]] for item in self.instances]
        reports = self.reports_directory()
        with patch("apps.runner.console_store.os.fsync", side_effect=OSError(errno.EIO, "private sync error")):
            with self.assertRaises((OSError, qualification.ConsoleStoreError)):
                qualification.save_console(first, reports)
        self.assertFalse(first["saved"])
        self.assertTrue(first["closed"])
        qualification.save_console(second, reports)
        self.assertTrue(second["saved"])
        self.assertEqual((reports / second["source_path"].name).read_bytes(), b"synthetic boot console\n")

    def test_constructor_failure_closes_prepared_sinks_and_keeps_private_directory_modes(self):
        original_create = qualification.BoundedConsole.create
        created = []

        def create(path, *, capacity_bytes):
            if created:
                raise qualification.ConsoleStoreError("console_reservation_failed")
            sink = original_create(path, capacity_bytes=capacity_bytes)
            created.append(sink)
            return sink

        with patch.object(qualification.BoundedConsole, "create", side_effect=create):
            with self.assertRaises(qualification.ConsoleStoreError):
                qualification.GuestStreams(self.instances, self.work, 1, self.logs)
        self.assertTrue(created[0].snapshot()["closed"])
        self.assertTrue(self.logs[0]["closed"])
        directory = self.work / "console-logs"
        self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
        directory.chmod(0o750)
        with self.assertRaisesRegex(RuntimeError, "mode 0700"):
            qualification.GuestStreams(self.instances, self.work, 2, [])
        self.assertEqual(directory.stat().st_mode & 0o777, 0o750)


if __name__ == "__main__":
    unittest.main()
