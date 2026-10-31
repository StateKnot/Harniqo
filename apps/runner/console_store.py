"""Bounded, reserved raw console storage owned by the Runner.

This Linux-only Module reserves actual blocks without extending natural EOF. A
successful append is fsynced; a failed append can leave an uncommitted physical
prefix, which is retained for private forensic archival. This is not an event
spool: it supplies no message acknowledgement, replay, or crash-resume protocol.
The owner must keep the private parent directory and use a fresh filename for
an independently created stream. The exposed descriptor is read-only and must
not be closed or passed to a guest; close() alone owns descriptor cleanup.
"""
from __future__ import annotations

import ctypes
import errno
import fcntl
import os
from pathlib import Path
import stat
import sys
import threading

MIN_CAPACITY_BYTES = 4 * 1024
MAX_CAPACITY_BYTES = 256 * 1024 * 1024
MAX_CHUNK_BYTES = 64 * 1024


class ConsoleStoreError(RuntimeError):
    """A safe error code, without console contents or host paths."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _reserve(fd: int, capacity: int) -> None:
    """Use Linux fallocate KEEP_SIZE, never a sparse or truncate substitute."""
    if sys.platform != "linux":
        raise ConsoleStoreError("console_reservation_unsupported")
    libc = ctypes.CDLL(None, use_errno=True)
    try:
        fallocate = libc.fallocate
    except AttributeError:
        raise ConsoleStoreError("console_reservation_unsupported") from None
    fallocate.argtypes = (ctypes.c_int, ctypes.c_int, ctypes.c_longlong, ctypes.c_longlong)
    fallocate.restype = ctypes.c_int
    for _ in range(8):
        if fallocate(fd, 1, 0, capacity) == 0:  # FALLOC_FL_KEEP_SIZE
            return
        error = ctypes.get_errno()
        if error != errno.EINTR:
            code = ("console_reservation_unsupported" if error in
                    {errno.ENOSYS, errno.EOPNOTSUPP, errno.EINVAL} else "console_reservation_failed")
            raise ConsoleStoreError(code)
    raise ConsoleStoreError("console_reservation_failed")


def _open_parent(path: Path) -> int:
    """Walk every ancestor with NOFOLLOW, retaining only the direct parent FD."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    current = os.open(path.anchor, flags)
    try:
        for name in path.parts[1:]:
            following = os.open(name, flags, dir_fd=current)
            previous, current = current, following
            os.close(previous)
        return current
    except BaseException:
        os.close(current)
        raise


class BoundedConsole:
    """One exclusively created console file; append and snapshots serialize.

    Capacity is 4 KiB..256 MiB and each append is at most 64 KiB. Any storage,
    integrity, or quota failure latches its first safe code and refuses future
    appends. snapshot() verifies current identity and reports both actual EOF
    and the prefix committed by successful append fsyncs. Historical errors do
    not prevent snapshot/close; a new integrity or cleanup failure still raises.
    close() always releases all descriptors and is idempotent, including after
    an error. Files are never automatically removed, including failed creates.
    """

    @classmethod
    def create(cls, path: Path, *, capacity_bytes: int) -> BoundedConsole:
        if (type(capacity_bytes) is not int or
                not MIN_CAPACITY_BYTES <= capacity_bytes <= MAX_CAPACITY_BYTES):
            raise ConsoleStoreError("console_invalid_capacity")
        path = Path(path)
        if ".." in path.parts or not path.name or path.name in {".", ".."}:
            raise ConsoleStoreError("console_invalid_path")
        path = Path(os.path.abspath(path))
        self = cls.__new__(cls)
        self._path = path
        self._capacity_bytes = capacity_bytes
        self._lock = threading.RLock()
        self._write_fd = self._read_fd = self._parent_fd = -1
        self._closed = False
        self._failure_code = None
        self._bytes_seen = self._rejected_bytes = self._durable_bytes = 0
        self._expected_size = self._observed_size = self._reserved_bytes = 0
        self._final_snapshot = None
        try:
            self._parent_fd = _open_parent(path.parent)
            parent = os.fstat(self._parent_fd)
            if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.geteuid() or
                    stat.S_IMODE(parent.st_mode) != 0o700):
                raise ConsoleStoreError("console_parent_unsafe")
            self._parent_identity = (parent.st_dev, parent.st_ino, parent.st_mode, parent.st_uid)
            self._write_fd = os.open(path.name, os.O_RDWR | os.O_CREAT | os.O_EXCL |
                                     os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=self._parent_fd)
            # An unusual process umask must not silently weaken/alter the contract.
            os.fchmod(self._write_fd, 0o600)
            file_stat = os.fstat(self._write_fd)
            self._identity = (file_stat.st_dev, file_stat.st_ino)
            self._file_owner = file_stat.st_uid
            fcntl.flock(self._write_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            _reserve(self._write_fd, capacity_bytes)
            self._reserved_bytes = os.fstat(self._write_fd).st_blocks * 512
            if self._reserved_bytes < capacity_bytes:
                raise ConsoleStoreError("console_reservation_incomplete")
            self._read_fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                                    dir_fd=self._parent_fd)
            self._verify()
            os.fsync(self._write_fd)
            os.fsync(self._parent_fd)
            self._verify()
            return self
        except BaseException as error:
            self._close_descriptors()
            if isinstance(error, ConsoleStoreError):
                raise
            if isinstance(error, OSError):
                raise ConsoleStoreError("console_create_failed") from None
            raise

    @property
    def fd(self) -> int:
        with self._lock:
            if self._closed:
                raise ConsoleStoreError("console_closed")
            return self._read_fd

    def fileno(self) -> int:
        return self.fd

    @property
    def identity(self) -> tuple[int, int]:
        return self._identity

    @property
    def capacity_bytes(self) -> int:
        return self._capacity_bytes

    @property
    def bytes_saved(self) -> int:
        with self._lock:
            if not self._closed:
                self._observed_size = os.fstat(self._write_fd).st_size
            return self._observed_size

    @property
    def durable_bytes(self) -> int:
        with self._lock:
            return self._durable_bytes

    @property
    def bytes_seen(self) -> int:
        with self._lock:
            return self._bytes_seen

    @property
    def rejected_bytes(self) -> int:
        with self._lock:
            return self._rejected_bytes

    @property
    def failure_code(self) -> str | None:
        with self._lock:
            return self._failure_code

    def _latch(self, code: str) -> None:
        if self._failure_code is None:
            self._failure_code = code

    def _verify(self) -> None:
        parent = os.fstat(self._parent_fd)
        parent_identity = (parent.st_dev, parent.st_ino, parent.st_mode, parent.st_uid)
        if parent_identity != self._parent_identity:
            raise ConsoleStoreError("console_parent_changed")
        current = _open_parent(self._path.parent)
        try:
            linked = os.fstat(current)
            if (linked.st_dev, linked.st_ino, linked.st_mode, linked.st_uid) != self._parent_identity:
                raise ConsoleStoreError("console_parent_changed")
        finally:
            os.close(current)
        actual = os.fstat(self._write_fd)
        self._observed_size = actual.st_size
        linked = os.stat(self._path.name, dir_fd=self._parent_fd, follow_symlinks=False)
        reader = os.fstat(self._read_fd)
        for item in (actual, linked, reader):
            if (not stat.S_ISREG(item.st_mode) or (item.st_dev, item.st_ino) != self._identity or
                    item.st_uid != self._file_owner or item.st_uid != os.geteuid() or
                    stat.S_IMODE(item.st_mode) != 0o600 or item.st_nlink != 1):
                raise ConsoleStoreError("console_file_changed")
        if actual.st_size != self._expected_size or actual.st_size > self._capacity_bytes:
            raise ConsoleStoreError("console_size_changed")
        if actual.st_blocks * 512 < self._capacity_bytes:
            raise ConsoleStoreError("console_reservation_lost")

    def append(self, data: bytes) -> None:
        if not isinstance(data, bytes):
            raise TypeError("console append requires bytes")
        with self._lock:
            if self._closed:
                raise ConsoleStoreError(self._failure_code or "console_closed")
            self._bytes_seen += len(data)
            if self._failure_code is not None:
                self._rejected_bytes += len(data)
                raise ConsoleStoreError(self._failure_code)
            try:
                self._verify()
                if len(data) > MAX_CHUNK_BYTES:
                    raise ConsoleStoreError("console_chunk_too_large")
                if self._expected_size + len(data) > self._capacity_bytes:
                    raise ConsoleStoreError("console_capacity_exceeded")
                written = 0
                interruptions = 0
                while written < len(data):
                    self._verify()  # Each physical write, including short-write continuations.
                    try:
                        count = os.pwrite(self._write_fd, data[written:], self._expected_size)
                    except InterruptedError:
                        interruptions += 1
                        if interruptions >= 8:
                            raise ConsoleStoreError("console_write_failed") from None
                        continue
                    if count <= 0 or count > len(data) - written:
                        raise ConsoleStoreError("console_write_failed")
                    written += count
                    self._expected_size += count
                self._verify()
                os.fsync(self._write_fd)
                self._verify()
                self._durable_bytes = self._expected_size
            except (ConsoleStoreError, OSError) as error:
                code = error.code if isinstance(error, ConsoleStoreError) else "console_io_failed"
                self._latch(code)
                self._rejected_bytes += len(data)
                raise ConsoleStoreError(code) from None

    def _receipt(self) -> dict:
        if self._write_fd >= 0:
            try:
                self._observed_size = os.fstat(self._write_fd).st_size
            except OSError:
                pass  # Preserve the last observed EOF if the descriptor itself failed.
        return {"capacity_bytes": self._capacity_bytes, "reserved_bytes": self._reserved_bytes,
                "reservation_mode": "linux_fallocate_keep_size", "bytes_saved": self._observed_size,
                "durable_bytes": self._durable_bytes, "bytes_seen": self._bytes_seen,
                "rejected_bytes": self._rejected_bytes, "failure_code": self._failure_code,
                "closed": self._closed, "device": self._identity[0], "inode": self._identity[1]}

    def snapshot(self) -> dict:
        with self._lock:
            if self._closed:
                return dict(self._final_snapshot)
            try:
                self._verify()
            except (ConsoleStoreError, OSError) as error:
                code = error.code if isinstance(error, ConsoleStoreError) else "console_integrity_failed"
                self._latch(code)
                raise ConsoleStoreError(code) from None
            return self._receipt()

    def _close_descriptors(self) -> OSError | None:
        failure = None
        for attribute in ("_read_fd", "_write_fd", "_parent_fd"):
            fd = getattr(self, attribute)
            setattr(self, attribute, -1)
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError as error:
                    if failure is None:
                        failure = error
        return failure

    def close(self) -> dict:
        with self._lock:
            if self._closed:
                return dict(self._final_snapshot)
            failure = None
            try:
                self._verify()
                os.fsync(self._write_fd)
                self._verify()
            except (ConsoleStoreError, OSError) as error:
                failure = error.code if isinstance(error, ConsoleStoreError) else "console_sync_failed"
                self._latch(failure)
            finally:
                receipt = self._receipt()
                close_error = self._close_descriptors()
                self._closed = True
                if close_error is not None:
                    failure = failure or "console_close_failed"
                    self._latch(failure)
                receipt.update(closed=True, failure_code=self._failure_code)
                self._final_snapshot = receipt
            if failure is not None:
                raise ConsoleStoreError(failure) from None
            return dict(self._final_snapshot)
