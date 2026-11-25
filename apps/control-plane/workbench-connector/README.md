# Hermes connector transport

`loadHermesConnector` supplies the existing `OpenAuthorizedExecution` interface over a dedicated HTTPS endpoint. The trusted worker config fixes the connector origin, CA, client certificate and private key paths. The private key is an absolute, non-symlink regular file owned by the worker account with mode `0600`. CA and certificate files must be regular files protected from group or public writes.

Each operation is a single POST to `/connector/v1/hermes` with `{version:1, operation, assignment, payload}`. The server returns `{version:1, assignment, result}`. `protocol.ts` validates the complete assignment, exact operation payload and exact result. Every response must retain the organization, project, principal, Run, Command, Attempt, workspace, target, immutable revisions, runtime identity, platform epoch and lease generation sent by the caller.

`open`, `assert_live` and `has_native_session` return the current grant and session facts. `attach`, `start`, `inspect` and `read_reply` carry the existing Hermes Adapter operations. `confirm_terminal` returns a matching native Run, observed writer exit and optional served-model evidence. The trusted connector rechecks its authority on each call. Native credentials remain at that connector.

Requests use direct mutually authenticated TLS connections with normal hostname and CA validation. Per-operation deadlines, cancellation and message byte limits apply to the whole response. Redirects and non-JSON results fail. The transport performs no automatic retry. The execution coordinator retains uncertain submission state and decides whether any follow-up is safe.

The loopback test exercises an actual mTLS server and the public transport interface. It verifies the operation round trip, exact identity, certificate rejection, absent client authentication, cancellation, response bounds and unsafe secret-file permissions. It uses controlled operation results. Production acceptance additionally requires the actual isolated connector, its current authorization and independent writer/model evidence.

```sh
node --test apps/control-plane/workbench-connector/index.test.ts
```
