# Restricted HTTPS upstream transport

This is the host-internal HTTPS transport used behind the future Model Egress
role. It does not listen for Guest traffic or implement ExecutionCredential
verification, current lease/authorization lookup, model approval, budget
reservation, OpenBao integration, kernel egress policy, or File Broker access.
A trusted host caller must finish admission before calling `exchange` and must
abort the supplied signal when that admission is revoked. A valid TLS request
from this Module does not establish full production Egress qualification.

Node **24.22.1** is the qualification runtime. TypeScript 6.0.3, @types/node
24.19.2 and ipaddr.js 2.5.0 are exact dependencies in the lockfile. Node's native
HTTPS/TLS/HTTP parser and stream pipeline handle wire protocols; there is no
custom HTTP or TLS parser. Node's TypeScript erasure runs the erasable source;
`npm run typecheck` separately checks strict TypeScript.

## Interface

```ts
import { UpstreamTransport } from './transport.ts';

const transport = new UpstreamTransport({
  routes: [{
    id: 'approved-chat',
    origin: 'https://approved-provider.example',
    operations: [{ method: 'POST', path: '/v1/chat/completions' }],
    secretRef: 'provider/approved-version',
  }],
  getSecret: async reference => loadApprovedProviderHeaders(reference),
  deniedCidrs: deploymentManagementAndTranslationNetworks,
});

await transport.exchange({
  routeId: 'approved-chat', method: 'POST', path: '/v1/chat/completions',
  headers: { 'content-type': 'application/json' }, body: frozenOriginalBytes,
}, { onHeaders: writeApprovedResponseHeaders, body: downstreamWritable }, signal);
```

`getSecret` is a trusted host dependency, called anew for each admitted exchange.
It returns only `authorization`, `x-api-key`, `api-key`, `openai-organization` or
`openai-project` string headers (an empty object supports approved anonymous
routes). This Module does not issue a Secret permission or make a mock Secret
provider into OpenBao. Request headers accept only `accept`, `content-type`,
`anthropic-version`, `anthropic-beta` and `openai-beta`. Every other header,
including Guest authorization, Host, hop-by-hop or proxy headers, is rejected
before DNS or Secret access. Native HTTP 4xx/5xx bodies pass through; redirects,
CONNECT and protocol upgrades are unsupported and rejected.

A route freezes an HTTPS DNS-name origin and paired GET/POST + exact origin-form
path/query operations. Arbitrary URLs, credentials in URLs, IP-literal origins,
fragments, trailing-dot hosts, a second socket path and caller-selected dial
options cannot cross this Interface. IDNA processing follows the WHATWG URL
implementation. The request method, path, body, headers and destination are
copied before asynchronous work. A private readonly route catalog binds these
settings. `configurationSha256` records its routes, limits, deny list and trust
bundle digests; it contains no credential value. A new configuration requires a
new Module instance.

The destination is a byte-mode `Writable` and a **synchronous** `onHeaders`
callback. After `onHeaders` begins, failure destroys the downstream stream;
callers must not attempt to send a second HTTP response. Before that point, the
caller can map `EgressTransportError.code` into its own controlled response.
Async header callbacks are unsupported; if supplied accidentally, their permit
remains held until they settle even after an outward timeout.

## Address and TLS policy

Every result from one OS lookup is validated before any connection is created;
one forbidden result rejects the entire set. Only canonical IPv4/IPv6 is
accepted, mapped IPv6 and zone identifiers are rejected. Public routing
conservatively excludes the IANA special-purpose ranges, including some special
allocations marked globally reachable; IPv6 is restricted to `2000::/3` with
special-purpose exclusions. ipaddr.js handles parsing and Node `BlockList`
handles subnet membership. A checked IPv4 is preferred, otherwise a checked
IPv6 is used, with no automatic retry.

`privateAddressExceptions` accepts exact RFC1918, loopback or ULA addresses for
one frozen origin/port. It does not permit CIDRs, link-local metadata, multicast,
unspecified or documentation ranges. `deniedCidrs` always wins over exceptions.
Known local interface addresses are rejected unless covered by an allowed
private exception. Deployments must additionally register their management
networks, public/NAT host addresses, and network-specific NAT64 prefixes in
`deniedCidrs`. Arbitrary network-specific translation cannot be inferred from a
public IPv6 address; this Module does not substitute for kernel network policy.

A request-local Agent disables pooling, TLS session caching and environment
proxies. Dialing uses only the checked address, explicit family and
`autoSelectFamily:false`; original hostname, Host and TLS SNI/certificate checks
remain bound to the route. The connected peer address is also checked. TLS
verification is always enabled, including when ambient
`NODE_TLS_REJECT_UNAUTHORIZED=0` exists. The default CA set is Node's bundled
roots; route `ca` supplies an explicit replacement trust bundle. No caller can
supply `verify=false`, custom certificate-check callbacks or proxy parameters.

## Bounds and failure behavior

Defaults are 8 global / 4 per-route exchanges, 1 MiB request bodies, 16 MiB
responses, 16 KiB headers, 10 second idle and 120 second total deadlines. Limits
are finite, validated at construction and frozen. Saturation rejects immediately
without an unbounded waiting queue. Header parsing has a byte ceiling rather
than silent count truncation. Response bytes pass through a counting Transform
and `pipeline`, preserving backpressure and native bytes/SSE without decompressing
or normalizing a provider protocol. High-water marks are buffering thresholds,
not a claimed hard process-RSS ceiling.

The total timer starts before DNS and Secret lookup. Idle timeout covers TCP,
TLS, response headers and streaming/downstream stalls. Abort, timeout, size,
stream or destination failures actively close the request, response and Agent.
Uncancellable DNS/Secret work can outlive an outward rejection: it keeps its
internal concurrency permit until settlement, and a late result is forbidden
from dialing. Operators must supply nonblocking bounded dependencies; a hung
external dependency can exhaust this Module's finite availability, rather than
create unlimited background tasks. There is no transparent automatic retry.

Completed exchanges return an immutable receipt. Failures throw
`EgressTransportError` with a stable code and immutable failure receipt. Receipts
contain route ID, supplied request-body length, observed response-body bytes,
upstream status and `not_sent` / `may_have_been_sent` exposure. They contain no
request/response body, credential, headers, native exception or upstream URL.
These counters do not establish supplier billing. Cancellation never proves an
upstream operation or charge stopped.

## Verification

```sh
npm ci --ignore-scripts
npm run typecheck
npm test
```

The suite uses real TLS on loopback TCP **10040**, temporary private certificates
and a controlled upstream. Run with test concurrency 1; it fails if that fixed
allowed port is occupied. No model is called. Dependency and DNS fixture tests
are synthetic and do not establish kernel egress restrictions, VM execution
identity, control-plane revocation or a complete production proxy. Source,
version, explicit private exceptions and actual designated-server results must
be recorded separately before this subitem is marked verified.

### Designated-server qualification

Run the native operator from the repository root as the ordinary approved Linux
account. It requires this project-scoped **Node v24.22.1**, installed lockfile
dependencies, the existing `openssl` CLI and private qualification directories.
It rejects root execution, another Node version or a root path different from
the repository. No global Node installation, firewall change, model credential,
VM/NIC creation or public port forwarding is needed.

After verifying the official archive and its checksum as recorded in the
[toolchain provenance](../../docs/operations/node-toolchain-2026-10-31.json), the
project runtime can be used explicitly:

```sh
# Start in the repository root; preserve at least the recorded disk-space floor.
df -h .
TASK_NODE="$PWD/.state/toolchains/node-v24.22.1-linux-x64/bin/node"
"$TASK_NODE" --version
PATH="$PWD/.state/toolchains/node-v24.22.1-linux-x64/bin:$PATH" \
  npm --prefix apps/model-egress ci --ignore-scripts
"$TASK_NODE" apps/model-egress/node_modules/typescript/bin/tsc \
  --project apps/model-egress/tsconfig.json --noEmit
"$TASK_NODE" --test --test-concurrency=1 apps/model-egress/transport.test.ts
NODE_USE_ENV_PROXY=1 HTTPS_PROXY=http://127.0.0.1:10021 NO_PROXY='' \
  "$TASK_NODE" scripts/qualification/qualify_egress_transport.mjs "$PWD"
```

The last argument is positional; the operator has no `--root` flag. Ports
**10020 and 10021** must both be free. The operator binds them only on loopback,
uses fixture-only exact `127.0.0.1`/`::1` exceptions for frozen localhost origins,
and verifies their release at the end. The environment-proxy settings above are
required negative-fixture inputs: they test that the request-local Agent ignores
that ambient proxy. They are not a production proxy configuration. CA/key files,
random authentication and payload material, source snapshots and the original
report remain in a fresh private evidence directory; retain earlier reports.

The final source produced **30 passing qualification gates**, zero errors and
cleanup errors on Node24.22.1, alongside strict typecheck and **14 passing Module
tests**. [Native result](../../docs/operations/egress-transport-native-2026-10-31.json)
and [scope/history](../../research/m0-egress-transport.md) distinguish real
TLS/HTTP traffic from configuration rejection and controlled-resolver/dependency
cases. No real provider model request was made. The operator is verification
code, not a Guest-facing Egress listener; passing it does not complete execution
authorization, budget, Secret infrastructure, kernel egress, VM networking,
dedicated service identity or M0-05.
