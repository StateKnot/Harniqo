# Bounded raw console storage

`console_store.BoundedConsole` is the Runner's Linux raw-console Module. A trusted
owner supplies a fresh file path under its existing 0700 directory and a fixed
4 KiB–256 MiB capacity. Guest bytes cannot select a host path or capacity.

```python
from apps.runner.console_store import BoundedConsole, ConsoleStoreError

log = BoundedConsole.create(approved_new_path, capacity_bytes=2 * 1024**2)
try:
    log.append(received_bytes)  # bytes, at most 64 KiB per call
    receipt = log.snapshot()
finally:
    receipt = log.close()
```

Creation uses native Linux `fallocate(FALLOC_FL_KEEP_SIZE)` on an exclusively
created 0600, single-link regular file. It requires actual allocated blocks and
zero initial EOF, synchronizes the file and parent, and refuses unsupported or
insufficient storage. There is no sparse fallback, permission repair of an old
file, or implicit overwrite. The writer holds a process lock and serializes
threads. The Module verifies the original private parent and file identities
before writes and snapshots. Advisory locking and private DAC assume a trusted
host owner; they do not contain a malicious process with that same identity.

Each successful append completes its `fsync`. A chunk exceeding remaining
capacity is rejected before writing any of it. Capacity, identity, write or
sync failure latches its safe error code; later appends cannot resume normal
collection. The caller must stop or reconcile the authenticated affected
execution. The Module cannot select or terminate a VM itself.

`snapshot()` separates `bytes_saved` (physical EOF), `durable_bytes` (the prefix
from fully successful appends), seen/rejected bytes, capacity, actual reserved
allocation and failure code. A short write followed by an error can leave
physical forensic bytes beyond the durable prefix. Such bytes must not satisfy
an execution probe or become a successfully acknowledged event. Closing cannot
retroactively commit a failed append. `close()` always attempts to release all
owned descriptors, is idempotent, retains historical failure in its receipt,
and raises a new cleanup failure when necessary.

The exposed `fd` is read-only and belongs to the Module. Consumers use bounded
`pread`; they must call `close()` rather than close that descriptor themselves.
The writable descriptor is never supplied to QEMU or a Guest. Existing files
and failed-create evidence remain in place. A new collector process creates a
new unique stream file; this Module does not offer crash resume or an event
acknowledgement/replay protocol.

The shared qualification `GuestStreams` uses this same Module, exposes the first
failure with the already-bound instance UUID, then continues bounded discard
drain so another stream and final EOF can be collected. `save_console` preserves
the verified physical prefix and labels the durable boundary and failure. An
archive copy is a separate allocation: the two 2 MiB sources plus their copies
have at most 8 MiB of payload; filesystem metadata and other writers are outside
that count. An archive failure preserves the source and blocks final deletion.

This is a fixed writer-owned raw-console budget, not a kernel filesystem/project
quota, global tenant storage allocator, libvirt daemon log policy, SQLite
critical-event spool, power-loss test or whole-host storage guarantee. The
event spool retains its separate WAL/committed ACK requirements in the
[architecture](../../docs/architecture/ARCHITECTURE.md). Actual native and VM
acceptance are recorded separately from component failures and mocks.
