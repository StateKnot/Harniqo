# Host-local Resource Broker

This is the reusable Python standard-library implementation of the sealed KVM
lifecycle. It drives the fixed local `qemu:///system` connection. Its Interface is
`ensure`, `renew`, `observe`, `revoke`, and a single-cycle `reconcile`; `watch`
repeatedly reconciles from an independently supervised process. The qualification
operator calls these same methods with real libvirt. Component tests replace only
the libvirt command edge and do not establish VM isolation.

## Trusted configuration

The host administrator supplies an immutable catalog of `Template` objects:

```python
from pathlib import Path
from apps.runner.resource_broker import Template, ResourceBroker, prepare_seed_access

# After creating the new seed and its QEMU read ACL, before catalog signing:
prepare_seed_access(readonly_seed, images_root=approved_images_directory)

template = Template(
    template_id="sealed-a", resource_id=approved_uuid, name=approved_name,
    disk=raw_disk, seed=readonly_seed, channel=report_socket,
    console=console_socket, source_base_sha256=verified_base_sha256,
)
broker = ResourceBroker(
    state_dir=private_state_directory, templates=[template],
    issuer_key_file=private_32_byte_key_file,
    images_root=approved_images_directory,
    channel_root=approved_channel_directory,
    max_lease_seconds=300,
)
```

The roots must exist, be owned by the broker account, and have no group/world
write permission. State and issuer-key parent directories are private (0700);
the key is a regular, single-link, 0600 file containing exactly 32 random bytes.
The state directory may be created under an already trusted parent. No method
changes existing permissions to make a bad configuration pass.

Image files are regular, single-link files. Report/console paths are existing
UNIX sockets. No symbolic links, lexical parent escapes, cross-template path or
inode aliases, or out-of-root paths are accepted. The ownership policy is role-specific and included in the configuration hash:
raw/seed owners may be the broker UID or the system's named `libvirt-qemu` UID;
report/console socket owners may be the broker UID, that same known QEMU UID
while running, or root after the libvirt driver stops/restores the resource.
No arbitrary owner is accepted. This recognizes the actual driver lifecycle;
it does not chown a file or grant any new access. QEMU's named socket `rw-`
access is already required by the sealed profile. Physical file/inode/capacity/seed/ancestor
identities remain frozen across these observed DAC transitions; mutable owner
and ACL-mask metadata is not used to invent a new configuration.

Named QEMU ACLs are exactly `r--` on seeds and `rw-` on raw disks/sockets; seeds
also require a named broker UID `r--` entry with an effective read mask. Owning
and named groups and other principals receive no access. Parent directories
remain broker-owned and reject group/other write permission (including ACL-mask
write); QEMU traversal does not permit writing those directories.

`prepare_seed_access(seed, images_root=...)` is the explicit reusable preparation
Interface for a **new broker-owned seed**, after its QEMU ACL is set and before
Template hashing/signing. It adds only the broker's named read entry using the
fixed `setfacl` executable, then checks the actual ACL and unchanged
hash/device/inode/size. That named entry survives libvirt's seed chown, permitting
ordinary-permission live and cold-restart hash validation. The helper does not
sudo, chown, grant group/other access, skip hashing, or repair an existing
QEMU-owned failed case. Unsupported driver ownership changes still fail closed.
The first native run's stricter policy and unreadable-seed failure remain a
separate failed configuration, not a success retroactively changed by this policy.

Each template fixes one UUID, raw 8 GiB root disk, raw read-only seed, q35/KVM,
1 vCPU, 1024 MiB guest RAM, one virtio report channel named `org.harniqo.m0`, one
serial console, and **no network or host filesystem device**. The source image
checksum is an administrator-approved provenance reference; source/signature
verification must happen before catalog admission. The broker does not claim to
independently re-establish that provenance from a checksum label.

`template.configuration_sha256` binds this profile, source reference, full paths,
file and ancestor-directory identities, raw-disk capacity, seed bytes and socket
identities. It excludes mutable root-disk contents, mtime and ctime. Prepare the
files, sockets and their ACLs before computing the hash. Every host observation
revalidates this frozen description. Changing the catalog of an existing state
directory is an error; there is no implicit migration or empty-state fallback.

The catalog and state directory define one **fixed instance group** of at most
64 pre-registered resources. Workspace exclusion is scoped to this state
directory, not every process, state directory or host in a deployment. Configure
one authoritative broker/state owner for the group; callers must not create a
second group to bypass that ownership. Dynamic allocation, socket replacement,
catalog migration, global target-level writer coordination and image preparation
are separate M2 integration responsibilities. This Module is the group's reusable
local guard, not a complete production Runner allocator.

## Signed local envelopes

These `Assignment` and `Grant` objects are **private Resource Broker envelopes**.
They are not the canonical control-plane Assignment or the model/file
ExecutionGrant. They do not accept guest execution credentials. The complete
ProjectScope, RuntimeInstance/InputSnapshot mapping, control-plane authorization,
revocation and budget transaction integration remains a separate M2 task.

```python
import time
from apps.runner.resource_broker import sign_assignment, sign_grant

claims = dict(
    resource_id=template.resource_id, organization_id="organization-a",
    workspace_id="workspace-a", attempt_id="attempt-a",
    template_id=template.template_id,
    configuration_sha256=template.configuration_sha256,
    generation=1, recovery_generation=1,
    issued_at=time.time(), expires_at=time.time() + 120,
)
assignment = sign_assignment(trusted_issuer_key_bytes, **claims)
observation = broker.ensure(assignment)

claims.update(generation=2, issued_at=time.time(), expires_at=time.time() + 30)
observation = broker.renew(sign_grant(trusted_issuer_key_bytes, **claims))
```

The issuer key stays on the trusted host, never in a VM, image, environment
variable, command argument or emitted observation. HMAC-SHA256 is purpose-bound
(`assignment`, `renewal`, ownership, anchor, journal). Renewal binds the same
resource, organization, workspace, attempt, template, configuration and recovery
generation. Its lease generation must increase; shortening a deadline is allowed.
Expired/stopped executions cannot be renewed. A fresh assignment may start a
higher generation only after the previous writer is confirmed stopped, and a
resource UUID can never switch organization or workspace, even after stopping.
Malformed scalar values are rejected before signature serialization and cannot
stop another legitimate workspace.

This is a trusted local library Interface, not a network authentication endpoint.
A future Supervisor IPC listener must enforce its own exact peer identity and
operation ACL. Separate production UIDs, libvirt ACL/polkit delegation, platform
registration and system service installation are not established by constructing
this object as the development account.

## Persistence, observation and failure

The broker uses a per-directory exclusive `flock` and a stable lock-file inode.
A separately authenticated durable anchor distinguishes first initialization
from a deleted journal and binds the lock file’s device/inode across process
restarts. Replacing the lock file is rejected by existing and newly constructed
brokers; it cannot establish a second lock domain over the same state. Journal replacement uses a private temporary file,
file `fsync`, atomic rename and directory `fsync`. Catalog/state integrity failure
never creates an empty replacement database. Generation tombstones are retained.
The intent and workspace reservation are durable **before** dispatching a fixed
UUID. A timeout never causes a new UUID or an automatic create retry. A revoke first
persists a distinct stop intent; observation, restart and renewal cannot clear
it or return the execution to running while a host stop is still pending.

A successful `ensure` has verified libvirt XML, signed ownership metadata and
actual presence. A running observation remains conditional on a valid lease.
Lease deadlines use host UTC together with Linux `CLOCK_BOOTTIME` and boot ID;
reboot or excessive wall/elapsed-clock divergence conservatively expires the
lease. Each resource and critical dispatch/observation uses a fresh clock sample;
slow earlier libvirt calls cannot extend another resource’s deadline. Stop
evidence time is sampled after host absence has actually been observed. Guest clocks and guest “stopped” ACKs are never consulted.

`revoke` and expired-lease reconciliation issue `destroy` only after exact
UUID/name/ownership/profile verification, then require successful host observation
of absence. The resulting `StopEvidence` records the configuration and host
confirmation time. Natural disappearance after a confirmed start can separately
produce `libvirt_absent_after_confirmed_start`. If final admission expires before
the create command is invoked, the broker durably records `not_sent` and only
records `libvirt_absent_without_dispatch` after a fresh successful host absence
query. This is distinct from an RPC with an unknown outcome. Unknown create/stop dispatches
remain `reconciling` and retain the writer even if a later list is empty; there is
no unsafe automatic repair. An administrator must resolve such ambiguous calls
against actual resources before a separate recovery procedure changes the state.

Configuration refusal diagnostics are available through the read-only
`broker.last_configuration_error` property (a defensive copy), and through
`BrokerError.configuration_error` when no constructed instance is available.
Wrapped descriptor errors preserve `__cause__.code`. The bounded record contains
only phase, catalog index, role, cause code and failed/changed **field names**;
it contains no filesystem paths, actual metadata values, seed bytes, key or
request body. A qualification worker may serialize it into its private error
JSON. The broker never prints it to stdout. It is observation only: none of the
owner/ACL/inode/hash or lifecycle acceptance decisions are relaxed.

A running watcher keeps a trusted in-memory ownership snapshot. If the journal
becomes unreadable/corrupt, it attempts exact ownership-checked stop and keeps
retrying, including late-arriving resources from pending creation. It records
`last_error` and `emergency_observations`; these remain `reconciling` even when
an emergency host stop succeeds, because persistence has not recovered. A fresh
process with an authenticated readable anchor can also identify its own catalog
domains through ownership MACs; it refuses startup with a bad journal after this
bounded stop attempt. Deployment supervision must retry startup and alert rather
than treating an initialization error as a healthy watcher. Missing/untrusted
anchors or unreachable libvirt may prevent safe identification/stop; those
conditions require operator intervention and do not establish a stopped result.

Run the watcher independently of the caller submitting assignments:

```python
broker.watch(interval_seconds=0.25)
```

Its service supervisor must restart it after failure. This library alone cannot
stop a VM while the broker host/libvirt is unreachable or while every independent
watcher is dead. Qualification records these process/failure cases separately.

The broker never deletes disks, seeds, channels or evidence. Retention and exact
cleanup are separate authorized operations after host stop confirmation. Guest
RAM/vCPU/raw capacity are profile facts, **not** proof of QEMU cgroup limits,
host storage hard quotas, tenant fairness, production egress, native Harness
recovery or global M2 fencing. Those capabilities require their own real
acceptance evidence.
