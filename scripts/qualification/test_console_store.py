#!/usr/bin/env python3
"""Bounded raw-console storage tests, without a VM, network or sparse fallback.

Run: PYTHONDONTWRITEBYTECODE=1 python3 scripts/qualification/test_console_store.py -v
Allocation-dependent tests execute only on Linux, against real fallocate.
"""
from __future__ import annotations

import errno
import fcntl
import os
from pathlib import Path
import sys
import subprocess
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "apps" / "runner"))
import console_store
from console_store import BoundedConsole, ConsoleStoreError


class ConsoleTestCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="console-store-", dir=Path("/tmp").resolve())
        self.work = Path(self.temporary.name)
        self.parent = self.work / "console-logs"
        self.parent.mkdir(mode=0o700)
        self.path = self.parent / "console.log"

    def tearDown(self):
        self.temporary.cleanup()

    def error_code(self, code, action):
        with self.assertRaises(ConsoleStoreError) as raised:
            action()
        self.assertEqual(raised.exception.code, code)
        self.assertEqual(str(raised.exception), code)


class ConsoleCreateTests(ConsoleTestCase):
    def test_invalid_capacity_creates_nothing(self):
        for value in (0, 4095, 256 * 1024 * 1024 + 1, 4096.0, True):
            self.error_code("console_invalid_capacity", lambda: BoundedConsole.create(
                self.path, capacity_bytes=value))
        self.assertFalse(self.path.exists())

    def test_public_parent_is_refused(self):
        for mode in (0o755, 0o777, 0o500):
            self.parent.chmod(mode)
            self.error_code("console_parent_unsafe", lambda: BoundedConsole.create(
                self.path, capacity_bytes=4096))
        self.parent.chmod(0o700)
        self.assertFalse(self.path.exists())

    def test_symlink_ancestor_is_refused_without_touching_destination(self):
        link = self.work / "shortcut"
        link.symlink_to(self.parent, target_is_directory=True)
        self.error_code("console_create_failed", lambda: BoundedConsole.create(
            link / self.path.name, capacity_bytes=4096))
        self.assertFalse(self.path.exists())

    def test_existing_file_and_symlink_are_never_overwritten(self):
        destination = self.work / "preserved"
        destination.write_bytes(b"original")
        self.path.symlink_to(destination)
        self.error_code("console_create_failed", lambda: BoundedConsole.create(
            self.path, capacity_bytes=4096))
        self.assertEqual(destination.read_bytes(), b"original")
        self.path.unlink()
        self.path.write_bytes(b"existing")
        self.error_code("console_create_failed", lambda: BoundedConsole.create(
            self.path, capacity_bytes=4096))
        self.assertEqual(self.path.read_bytes(), b"existing")

    def test_unsupported_reservation_preserves_empty_private_evidence_and_closes_fds(self):
        # This failure edge must never pretend successful allocation on macOS.
        reserve_error = ConsoleStoreError("console_reservation_unsupported")
        original_open = os.open
        opened = []

        def observed_open(*args, **kwargs):
            fd = original_open(*args, **kwargs)
            opened.append(fd)
            return fd

        with mock.patch.object(console_store, "_reserve", side_effect=reserve_error):
            with mock.patch.object(console_store.os, "open", side_effect=observed_open):
                self.error_code("console_reservation_unsupported", lambda: BoundedConsole.create(
                    self.path, capacity_bytes=4096))
        self.assertEqual(self.path.read_bytes(), b"")
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        for fd in set(opened):
            with self.assertRaises(OSError):
                os.fstat(fd)

    def test_non_linux_has_no_allocation_fallback(self):
        with mock.patch.object(console_store.sys, "platform", "darwin"):
            self.error_code("console_reservation_unsupported", lambda: BoundedConsole.create(
                self.path, capacity_bytes=4096))
        self.assertTrue(self.path.exists())
        self.assertEqual(self.path.stat().st_size, 0)


@unittest.skipUnless(sys.platform == "linux", "requires native Linux fallocate KEEP_SIZE")
class ConsoleNativeTests(ConsoleTestCase):
    def create(self, capacity=4096):
        sink = BoundedConsole.create(self.path, capacity_bytes=capacity)
        self.addCleanup(self.close_quietly, sink)
        return sink

    @staticmethod
    def close_quietly(sink):
        try:
            sink.close()
        except ConsoleStoreError:
            pass

    def test_real_allocation_natural_eof_private_mode_reader_and_exclusive_lock(self):
        sink = self.create()
        receipt = sink.snapshot()
        self.assertGreaterEqual(self.path.stat().st_blocks * 512, 4096)
        self.assertGreaterEqual(receipt["reserved_bytes"], 4096)
        self.assertEqual(receipt["reservation_mode"], "linux_fallocate_keep_size")
        self.assertEqual(self.path.read_bytes(), b"")
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.path.stat().st_uid, os.geteuid())
        self.assertEqual(self.path.stat().st_nlink, 1)
        self.assertEqual(sink.identity, (self.path.stat().st_dev, self.path.stat().st_ino))
        self.assertEqual(sink.fileno(), sink.fd)
        self.assertEqual(fcntl.fcntl(sink.fd, fcntl.F_GETFL) & os.O_ACCMODE, os.O_RDONLY)
        with self.assertRaises(OSError):
            os.write(sink.fd, b"unauthorized")
        with self.path.open("rb") as competing:
            with self.assertRaises(BlockingIOError):
                fcntl.flock(competing.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        sink.close()
        with self.path.open("rb") as competing:
            fcntl.flock(competing.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_append_exact_binary_bytes_and_durable_counters(self):
        sink = self.create()
        chunks = [b"one\n", b"\x00\xff\r\n", b""]
        for chunk in chunks:
            sink.append(chunk)
        expected = b"".join(chunks)
        self.assertEqual(os.pread(sink.fd, len(expected) + 1, 0), expected)
        snapshot = sink.snapshot()
        self.assertEqual(snapshot["bytes_saved"], len(expected))
        self.assertEqual(snapshot["durable_bytes"], len(expected))
        self.assertEqual(snapshot["bytes_seen"], len(expected))
        self.assertEqual(snapshot["rejected_bytes"], 0)
        self.assertIsNone(snapshot["failure_code"])
        self.assertFalse(snapshot["closed"])

    def test_independent_process_is_excluded_until_writer_owner_closes(self):
        sink = self.create()
        sink.append(b"native-independent-process-lock\n")
        expected = self.path.read_bytes()
        program = """import os,fcntl,sys
fd=os.open(sys.argv[1],os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC)
try:
    info=os.fstat(fd)
    assert (info.st_dev,info.st_ino)==(int(sys.argv[2]),int(sys.argv[3]))
    try:
        fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        outcome='acquired'
    except BlockingIOError:
        outcome='blocked'
    print(outcome)
finally:
    os.close(fd)
"""
        def probe():
            child = subprocess.run([sys.executable,"-c",program,str(self.path),
                                    *(str(v) for v in sink.identity)],
                                   close_fds=True,capture_output=True,text=True,timeout=5)
            self.assertEqual(child.returncode,0)
            self.assertEqual(child.stderr,"")
            return child.stdout.strip()
        self.assertEqual(probe(),"blocked")
        sink.close()
        self.assertEqual(probe(),"acquired")
        self.assertEqual(self.path.read_bytes(),expected)

    def test_reader_offset_never_affects_append_and_normal_close_is_idempotent(self):
        sink = self.create()
        sink.append(b"first")
        self.assertEqual(os.read(sink.fd, 5), b"first")
        os.lseek(sink.fd, 0, os.SEEK_SET)
        sink.append(b"second")
        self.assertEqual(self.path.read_bytes(), b"firstsecond")
        reader = sink.fd
        receipt = sink.close()
        self.assertEqual(receipt, sink.close())
        self.assertEqual(receipt, sink.snapshot())
        self.assertEqual(receipt["bytes_saved"], 11)
        self.assertEqual(receipt["durable_bytes"], 11)
        self.assertTrue(receipt["closed"])
        with self.assertRaises(OSError):
            os.fstat(reader)
        self.error_code("console_closed", lambda: sink.append(b"later"))

    def test_quota_rejects_entire_chunk_and_latches(self):
        sink = self.create()
        sink.append(b"a" * 4090)
        self.error_code("console_capacity_exceeded", lambda: sink.append(b"b" * 7))
        self.error_code("console_capacity_exceeded", lambda: sink.append(b"c"))
        self.assertEqual(self.path.read_bytes(), b"a" * 4090)
        snapshot = sink.snapshot()
        self.assertEqual(snapshot["bytes_saved"], 4090)
        self.assertEqual(snapshot["durable_bytes"], 4090)
        self.assertEqual(snapshot["bytes_seen"], 4098)
        self.assertEqual(snapshot["rejected_bytes"], 8)
        self.assertEqual(snapshot["failure_code"], "console_capacity_exceeded")
        receipt = sink.close()  # Historical quota is valid bounded forensic evidence.
        self.assertEqual(receipt["failure_code"], "console_capacity_exceeded")
        self.assertTrue(receipt["closed"])
        self.assertEqual(sink.snapshot(), receipt)
        self.error_code("console_capacity_exceeded", lambda: sink.append(b"d"))

    def test_exact_capacity_succeeds_then_next_byte_is_rejected(self):
        sink = self.create()
        sink.append(b"a" * 4096)
        self.error_code("console_capacity_exceeded", lambda: sink.append(b"b"))
        self.assertEqual(sink.bytes_saved, 4096)
        self.assertEqual(sink.durable_bytes, 4096)

    def test_chunk_limit_rejects_without_prefix_even_with_space(self):
        sink = self.create(128 * 1024)
        self.error_code("console_chunk_too_large", lambda: sink.append(b"a" * (64 * 1024 + 1)))
        self.assertEqual(self.path.read_bytes(), b"")
        self.assertEqual(sink.rejected_bytes, 64 * 1024 + 1)

    def test_short_writes_are_completed_before_fsync(self):
        sink = self.create()
        original = os.pwrite
        calls = []

        def short_write(fd, data, offset):
            calls.append(offset)
            return original(fd, data[:3], offset)

        with mock.patch.object(console_store.os, "pwrite", side_effect=short_write):
            sink.append(b"1234567890")
        self.assertEqual(calls, [0, 3, 6, 9])
        self.assertEqual(self.path.read_bytes(), b"1234567890")
        self.assertEqual(sink.durable_bytes, 10)

    def test_partial_eio_keeps_uncommitted_prefix_and_prior_durable_count(self):
        sink = self.create()
        sink.append(b"prior")
        original = os.pwrite
        calls = 0

        def partial_then_eio(fd, data, offset):
            nonlocal calls
            calls += 1
            if calls == 1:
                return original(fd, data[:3], offset)
            raise OSError(errno.EIO, "private path and raw content must not escape")

        with mock.patch.object(console_store.os, "pwrite", side_effect=partial_then_eio):
            self.error_code("console_io_failed", lambda: sink.append(b"failed"))
        self.assertEqual(self.path.read_bytes(), b"priorfai")
        snapshot = sink.snapshot()
        self.assertEqual(snapshot["bytes_saved"], 8)
        self.assertEqual(snapshot["durable_bytes"], 5)
        self.assertEqual(snapshot["bytes_seen"], 11)
        self.assertEqual(snapshot["rejected_bytes"], 6)
        self.error_code("console_io_failed", lambda: sink.append(b"later"))
        receipt = sink.close()
        self.assertEqual(receipt["bytes_saved"], 8)
        self.assertEqual(receipt["durable_bytes"], 5)  # Close does not commit the failed chunk.

    def test_fsync_failure_never_reports_failed_chunk_durable(self):
        sink = self.create()
        sink.append(b"prior")
        with mock.patch.object(console_store.os, "fsync", side_effect=OSError(errno.EIO, "fsync failed")):
            self.error_code("console_io_failed", lambda: sink.append(b"uncommitted"))
        self.assertEqual(sink.bytes_saved, 16)
        self.assertEqual(sink.durable_bytes, 5)
        self.assertEqual(sink.rejected_bytes, 11)
        self.assertEqual(sink.close()["durable_bytes"], 5)

    def test_no_progress_and_repeated_interruptions_have_finite_failure(self):
        for result in (0, -1, InterruptedError(errno.EINTR, "interrupt")):
            path = self.parent / str(len(list(self.parent.iterdir())))
            sink = BoundedConsole.create(path, capacity_bytes=4096)
            self.addCleanup(self.close_quietly, sink)
            options = {"side_effect": result} if isinstance(result, BaseException) else {"return_value": result}
            with mock.patch.object(console_store.os, "pwrite", **options) as write:
                self.error_code("console_write_failed", lambda: sink.append(b"data"))
            self.assertLessEqual(write.call_count, 8)
            self.assertEqual(sink.bytes_saved, 0)
            self.assertEqual(sink.durable_bytes, 0)

    def test_single_interruption_is_retried(self):
        sink = self.create()
        original = os.pwrite
        calls = 0

        def interrupted(fd, data, offset):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise InterruptedError(errno.EINTR, "interrupted")
            return original(fd, data, offset)

        with mock.patch.object(console_store.os, "pwrite", side_effect=interrupted):
            sink.append(b"retry")
        self.assertEqual(self.path.read_bytes(), b"retry")
        self.assertEqual(sink.durable_bytes, 5)

    def test_concurrent_appends_remain_whole_and_accounted(self):
        sink = self.create(128 * 1024)
        chunks = [bytes([index]) * 512 for index in range(32)]
        barrier = threading.Barrier(len(chunks))
        errors = []

        def append(chunk):
            try:
                barrier.wait(timeout=10)
                sink.append(chunk)
            except BaseException as error:
                errors.append(error)

        threads = [threading.Thread(target=append, args=(chunk,)) for chunk in chunks]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
            self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        actual = self.path.read_bytes()
        self.assertCountEqual([actual[start:start + 512] for start in range(0, len(actual), 512)], chunks)
        self.assertEqual(sink.bytes_saved, 32 * 512)
        self.assertEqual(sink.durable_bytes, 32 * 512)
        self.assertEqual(sink.bytes_seen, 32 * 512)

    def test_file_replacement_symlink_permissions_and_hardlink_are_rejected(self):
        mutations = ("replacement", "symlink", "permissions", "hardlink", "truncate")
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                path = self.parent / mutation
                sink = BoundedConsole.create(path, capacity_bytes=4096)
                self.addCleanup(self.close_quietly, sink)
                sink.append(b"saved")
                if mutation in {"replacement", "symlink"}:
                    path.rename(path.with_suffix(".original"))
                    if mutation == "replacement":
                        path.touch(mode=0o600)
                    else:
                        path.symlink_to(path.with_suffix(".original"))
                elif mutation == "permissions":
                    path.chmod(0o640)
                elif mutation == "hardlink":
                    os.link(path, path.with_suffix(".linked"))
                else:
                    with path.open("wb"):
                        pass
                code = "console_size_changed" if mutation == "truncate" else "console_file_changed"
                self.error_code(code, lambda: sink.append(b"blocked"))
                self.assertEqual(sink.durable_bytes, 5)
                self.error_code(code, sink.snapshot)
                self.error_code(code, sink.close)
                self.assertTrue(sink.snapshot()["closed"])

    def test_parent_permission_and_identity_changes_are_rejected(self):
        for mutation in ("permissions", "replacement", "symlink"):
            with self.subTest(mutation=mutation):
                # Separate private parents avoid restoring a tampered identity.
                parent = self.work / mutation
                parent.mkdir(mode=0o700)
                path = parent / "console.log"
                sink = BoundedConsole.create(path, capacity_bytes=4096)
                self.addCleanup(self.close_quietly, sink)
                if mutation == "permissions":
                    parent.chmod(0o755)
                else:
                    original = parent.with_suffix(".original")
                    parent.rename(original)
                    if mutation == "replacement":
                        parent.mkdir(mode=0o700)
                    else:
                        parent.symlink_to(original, target_is_directory=True)
                code = "console_io_failed" if mutation == "symlink" else "console_parent_changed"
                self.error_code(code, lambda: sink.append(b"blocked"))
                self.assertEqual(sink.bytes_saved, 0)

    def test_duplicate_creation_cannot_take_existing_sink(self):
        sink = self.create()
        sink.append(b"original")
        self.error_code("console_create_failed", lambda: BoundedConsole.create(self.path, capacity_bytes=4096))
        sink.append(b" still owned")
        self.assertEqual(self.path.read_bytes(), b"original still owned")

    def test_close_releases_all_descriptors_on_new_sync_failure_and_is_idempotent(self):
        sink = self.create()
        sink.append(b"saved")
        descriptors = (sink.fd, sink._write_fd, sink._parent_fd)
        with mock.patch.object(console_store.os, "fsync", side_effect=OSError(errno.EIO, "failed")):
            self.error_code("console_sync_failed", sink.close)
        for fd in descriptors:
            with self.assertRaises(OSError):
                os.fstat(fd)
        receipt = sink.close()
        self.assertTrue(receipt["closed"])
        self.assertEqual(receipt["failure_code"], "console_sync_failed")
        self.assertEqual(receipt["durable_bytes"], 5)
        self.error_code("console_closed", lambda: sink.fd)

    def test_new_close_failure_surfaces_without_replacing_first_failure(self):
        sink = self.create()
        sink.append(b"a" * 4096)
        self.error_code("console_capacity_exceeded", lambda: sink.append(b"overflow"))
        with mock.patch.object(console_store.os, "fsync", side_effect=OSError(errno.EIO, "failed")):
            self.error_code("console_sync_failed", sink.close)
        self.assertEqual(sink.failure_code, "console_capacity_exceeded")
        self.assertEqual(sink.snapshot()["failure_code"], "console_capacity_exceeded")

    def test_constructor_fsync_failure_closes_every_descriptor_and_preserves_file(self):
        original_open = os.open
        opened = []

        def observed_open(*args, **kwargs):
            fd = original_open(*args, **kwargs)
            opened.append(fd)
            return fd

        with mock.patch.object(console_store.os, "open", side_effect=observed_open):
            with mock.patch.object(console_store.os, "fsync", side_effect=OSError(errno.EIO, "failed")):
                self.error_code("console_create_failed", lambda: BoundedConsole.create(
                    self.path, capacity_bytes=4096))
        self.assertTrue(self.path.exists())
        self.assertEqual(self.path.stat().st_size, 0)
        for fd in set(opened):
            with self.assertRaises(OSError):
                os.fstat(fd)

    def test_allocator_errors_fail_without_sparse_fallback(self):
        for error in ("console_reservation_unsupported", "console_reservation_failed"):
            path = self.parent / error
            with mock.patch.object(console_store, "_reserve", side_effect=ConsoleStoreError(error)):
                self.error_code(error, lambda: BoundedConsole.create(path, capacity_bytes=4096))
            self.assertEqual(path.stat().st_size, 0)
            self.assertEqual(path.stat().st_blocks, 0)


if __name__ == "__main__":
    unittest.main()
