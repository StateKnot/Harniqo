# Local durable event spool

`EventSpool` stores one immutable attempt/stream/execution binding in a private
local SQLite database. It is a reusable Runner Module; the stdio worker under
`scripts/qualification/` is a private acceptance fixture, not a production RPC
endpoint, uploader or control plane.

The qualified runtime is Linux x64, Node **24.22.1**, embedded SQLite **3.53.4**
with the exact source ID checked by the implementation, and a local ext4 mount.
Admission checks the actual mount type, runtime, compile options, private file
ownership and connection settings. No npm runtime dependency is required.

## Interface and ownership

Trusted configuration supplies an existing absolute 0700 directory, organization,
project, Run, Attempt, RuntimeInstance, stream and platform-epoch UUIDs, and a
positive lease generation. Guest input cannot choose any of these values.
Every ancestor must also pass the trusted-owner and non-writable-path checks.
A private leaf inside a group-writable checkout does not satisfy admission.
Provision a private data root outside such a checkout; keep source and diagnostic
reports in their designated workspace. Existing unsafe permissions are rejected,
never silently repaired by this Module.

```ts
import { EventSpool } from './spool.ts';

// Use create only for a new empty directory; open never initializes missing data.
const spool = EventSpool.open(trustedOptions);
try {
  const local = spool.append({ source_seq, body: serializedSourceEvent });
  const pending = spool.pendingBatch();
  // Only after the authenticated uploader verifies a real committed receipt:
  spool.applyCommittedAck(committedAck);
} finally {
  spool.close();
}
```

`append` accepts exact UTF-8 JSON bytes and checks their identity header and
sequence against the fixed scope. Full business `SourceEvent` schema validation
belongs to ingress. A retry of a pending key must have identical bytes; a
different payload is rejected. Returned bodies are detached materialized copies.
No database, statement, iterator or SQL execution handle escapes the Module.

The local receipt has level **`durable_received`**. It does not authorize the
producer to discard critical events. `applyCommittedAck` accepts the canonical
`committed` cursor only from a trusted authenticated uploader; it validates the
fixed attempt/stream/binding and cannot cross a local sequence gap. Cursor
advancement and payload deletion commit in one transaction. Network identity,
outer batch-ID correlation and actual control-plane commit are uploader duties.
This Module supplies none of those proofs.

Pending out-of-order events and missing ranges remain visible. After confirmed
payload reclamation, replay at or below the committed cursor returns
`event_already_committed`; it does not pretend a retired payload digest still
exists. A changed binding needs a separately registered stream/store; reopening
cannot migrate ownership or rewrite pending events.

## Capacity and failure behavior

Defaults are 2,048 database pages of 4,096 bytes, 4 MiB/1,024 pending events,
64 KiB per event, and a batch of at most 256 events/1 MiB of stored body bytes.
Trusted limits are finite and frozen in the database configuration. The batch
byte budget excludes a transport JSON envelope and HTTP headers; the uploader
must budget those separately. The configurable record ceiling is also 256.

The single connection uses EXCLUSIVE locking, WAL, FULL synchronization, no
cache spill, a cache sized for the finite page count, memory temporary storage,
and a successful TRUNCATE checkpoint before every mutation. One committed WAL
may remain until the next mutation or normal close. For the pinned SQLite
source, the conservative WAL envelope is
`32 + (max_pages + 32) * (4096 + 24)` bytes, including extra sector-padding
frames. Actual file sizes, settings, counters and event digests are checked.
The [source derivation](../../../research/m0-event-spool.md) is part of this
runtime restriction, not a guarantee for arbitrary future SQLite builds.

Logical `queue_full` and a reconciled pre-COMMIT `storage_full` reject the new
event and preserve accepted payloads. A valid contiguous committed prefix can
then be reclaimed. A full store with an unresolved leading gap may remain
backpressured; the caller must not fabricate an ACK or evict unconfirmed events.
The execution owner must pause or stop admission/execution and expose the reason.

I/O, identity, checkpoint and uncertain commit failures latch mutations.
`mayHaveCommitted` distinguishes an unknown append/ACK outcome; close and reopen
for reconciliation, then retry the same bytes. A failed operation never becomes
successful merely because cleanup succeeds. Statements are explicitly finalized;
close always attempts database and descriptor cleanup while retaining the first
failure. Application code does not delete failed stores. SQLite itself may
checkpoint or remove its WAL during close.

The directory and database identities remain bound; symlinks, hardlinks,
unexpected files/SHM, mode/owner changes, foreign schemas and incompatible
configuration fail closed. The host owner is trusted. This is not isolation
against another malicious process with that same UID.

## Verification scope

Unit, native SIGKILL/SQLite-capacity and actual VM-stop evidence are separate.
The [standalone operator](../../../scripts/qualification/qualify_event_spool.py)
retains raw private stores and ledgers. The VM operator's explicit
`--event-spool-pressure` mode uses the same Module and formal Resource Broker.
Current results and status belong in the [M0 evidence index](../../../docs/planning/M0-RESULTS.md).

Both operators require a trusted explicit `--spool-data-root`. For the standalone
process recovery checks:

```sh
python3 scripts/qualification/qualify_event_spool.py \
  --root "$HARNIQO_WORKSPACE" --spool-data-root "$HARNIQO_PRIVATE_SPOOL_ROOT"
```

The 2026-11-01 designated-server checks passed 34 SQLite tests and 210 Python
regressions, nine native-process gates, and 27 host/22 Guest checks in two actual
VMs. [Process evidence](../../../docs/operations/event-spool-native-2026-11-01.json)
and [VM evidence](../../../docs/operations/event-spool-kvm-2026-11-01.json) retain
separate scopes; original failed runs remain recorded.

Database/WAL bounds do not implement a kernel filesystem/project quota, aggregate
host storage admission, libvirt log policy, dedicated service identity, power-loss
recovery or universal physical corruption detection. Loss of an already-committed
WAL tail due to a storage-contract violation is not independently detected by a
second receipt ledger. Those deployment and recovery gates remain separate.
