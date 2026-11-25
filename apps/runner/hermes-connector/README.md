# Dedicated Hermes Connector

This connector accepts the control worker's mTLS requests for one administrator-provisioned native Hermes runtime. The server checks the controller certificate's SHA-256 fingerprint, the exact full assignment, a finite live grant and the fixed runtime binding before invoking its private native Adapter. Requests cannot select a native origin, credential or filesystem path.

`initializeConnectorState(directory)` creates the durable ledger in a new, empty, caller-owned `0700` directory. Normal startup opens this existing ledger with an exclusive SQLite connection, WAL and FULL synchronization. Retain it with the runtime's private state. Do not reset it after a failed request. A persisted `attaching` or `starting` intent without its receipt prevents another native mutation. Reopening the connector preserves that decision. Up to 128 assignments are retained, and a full ledger rejects new admissions until an administrator archives and reconciles it.

`openHermesConnector(options)` returns a Node HTTPS server and an asynchronous `close()` method. Supply the server TLS key, certificate and controller CA, the authorized controller fingerprint, a fixed `ConnectorBinding`, an opened native Hermes Adapter and a trusted `ConnectorAuthority`. The authority must verify the live assignment and finite grant, check existing native sessions and independently confirm the native writer has stopped. The connector persists the actual Run projection and matching native reply before requesting terminal proof.

## Dedicated Docker authority

`createDockerHermesAuthority(options)` provides a concrete Linux host authority for an administrator-provisioned, dedicated container through the local `/var/run/docker.sock` daemon. It pins the complete container ID and immutable image ID, verifies a read-only root filesystem, private process and IPC namespaces, dropped capabilities and no-new-privileges, refuses root or socket mounts, and checks the following labels against the assigned scope.

- `org.harniqo.organization-id`
- `org.harniqo.project-id`
- `org.harniqo.workspace-id`
- `org.harniqo.runtime-instance-id`

The native API must publish container port `8642/tcp` exactly once to a fixed `127.0.0.1` host port in `10000–10100`. The authority holds the native bearer credential. It never sends that credential to the control worker. Its grant contains the complete immutable assignment and expires within 120 seconds. A controller request cannot extend it.

The Docker authority also requires `deadlineDirectory`, normally the existing connector state directory. Before admission it persists the immutable container identity, assignment and original expiry and starts a separate detached Node deadline process. The helper checks the exact local container before acknowledging readiness. Its environment contains no model credentials. It survives a connector or worker exit, stops the pinned container at the original deadline and records the actual stopped state. An existing deadline cannot be replaced with a later expiry. A running container requires a live guard for further authorization. Container restart policy must be `no`.

For terminal confirmation, the authority reads the actual native Run, requires `completed` or `failed`, stops the pinned container ID and checks `State.Running=false` and `State.Pid=0`. Disable container auto-removal so the stopped identity remains available for inspection. Docker control stays in the trusted connector process. The Agent receives no Docker socket.

An optional `servedModelReceipt` callback can provide independently correlated model evidence. The pinned Hermes canonical Run route may omit served-model metadata. Omitted model information remains unknown.

This dedicated-container profile requires an administrator-controlled trust domain. Shared untrusted tenant execution requires the separately qualified workspace VM and credential egress policy. Production deployment must additionally provision and renew current assignment grants, maintain immutable runtime identity, and reconcile a stopped runtime before authorizing its next assignment.

## Verification

The standalone entry point reads `HARNIQO_CONNECTOR_FILE` as an absolute path to an administrator-provisioned `0600` JSON file. Its exact fields are `assignment`, `expiresAt`, `containerId`, `imageId`, `nativeOrigin`, `nativeTokenFile`, `tls`, `controllerFingerprint256`, `stateDirectory`, `host` and `port`. `assignment` contains the full persisted assignment, `expiresAt` is the finite grant deadline in Unix milliseconds, and `tls` contains `keyFile`, `certFile` and `caFile` absolute paths. The native token and TLS private key must be caller-owned `0600` files. The listener uses the explicit IP address and an approved port in `10000–10100`, excluding SSH port `10023`.

Initialize the empty ledger directory explicitly before the first start. Preserve the existing directory for every subsequent start.

```sh
node --input-type=module -e \
  'import { initializeConnectorState } from "./apps/runner/hermes-connector/index.ts"; initializeConnectorState(process.argv[1]);' \
  /absolute/private/connector-state
HARNIQO_CONNECTOR_FILE=/absolute/private/connector.json node apps/runner/hermes-connector/main.ts
```

Startup checks the live container identity and current finite grant before listening. This process runs on the trusted Linux Docker host. The Agent container receives only its native runtime files and workspace.

Run `node --test apps/runner/hermes-connector/index.test.ts` using the repository's pinned Node 24 toolchain. The test establishes a real mTLS connection and verifies assignment scope, grant revocation, persistent unknown-start handling and workspace exclusion across connector restarts. Its native Adapter is a controlled fixture. A live deployment must also exercise the pinned native Harness and actual lifecycle authority.

On the designated Linux host, set `HARNIQO_DEADLINE_TEST_IMAGE` to an approved immutable image ID with `/bin/sh` and run `node --test apps/runner/hermes-connector/deadline.integration.ts`. This no-model test uses a temporary hardened container on loopback port `10098`, exits the authorizing parent, confirms deadline stop and rejects a later deadline for the same container. It cleans up its container and private files.
