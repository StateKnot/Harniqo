# OpenClaw native qualification

This profile runs the official native Gateway with its own WebSocket protocol,
session store and file tools. It is a trusted qualification fixture, not a
production execution backend or the Harniqo Adapter. Current actual results are
recorded separately in the [M0 evidence index](../../../docs/planning/M0-RESULTS.md).

## Fixed upstream input

- OpenClaw `2026.10.8`, source commit
  `dc2523e5a5c7a309517fd7fa834add938231d526`.
- Official `2026.10.8-slim` release index:
  `sha256:82b6ce63048e75ca749dbc250e872ee21f4b1c098fc6bff94636e357bcb64bf5`.
- Linux amd64 manifest:
  `sha256:eba17fa04b4ac5616496c2ce374a95afb6db6ff52d8bf73bf87dc8c7b8656cad`.
- Image configuration:
  `sha256:27b4542156d3701cbfd013c538ec2548545973c13cc714144350ae80125d425c`.
- The same official manifest/configuration bytes were independently obtained
  from GHCR and Docker Hub. Image layers total 674,089,475 compressed bytes.
  Source and runtime dependencies retain their upstream licenses; Harniqo's MIT
  license does not replace them.

The [acquirer](../../../scripts/qualification/prepare_openclaw.py) records failed
downloads independently. `--official-oci` accepts an offline export only after
all physical tar members, the fixed official manifest, configuration and every
blob size/hash match. It copies input to a private read-only snapshot before
loading; it never extracts archive paths into the host. The saved Docker archive
and provenance live under `images/containers/openclaw/2026.10.8/`. Acquisition
success is not native qualification success.

## Native profile and execution

The Gateway runs as the non-root operator UID with a read-only root filesystem,
no capabilities, no privilege escalation, 128 PIDs, 4 GiB RAM and 2 CPUs. Three
private writable directories hold state, workspace and evidence. Only the
original server-side credential file is mounted. The generated configuration
contains native SecretRefs; secrets enter the child Gateway process environment,
not Docker image metadata, launch arguments or public reports.

The profile fixes one configured model, disables fallback selection and automatic
catalog/update discovery, and allows only native `read` and `write` tools within
the workspace. Shell, browser, arbitrary plugins and unrelated service features
are not part of this fixture. Native DNS/SSRF guards and TLS validation are kept.
This Docker fixture does not provide hostile tenant isolation.

Run the [operator](../../../scripts/qualification/run_openclaw.py) with explicit
`--root`, `--config`, `--endpoint-id`, `--provenance`, `--case` and `--mode` values.
Each case directory must be new. The default host port is 10016, loopback only.
All accepted host ports are 10000–10100, excluding 10023.

1. `validate`: native `openclaw config validate` with networking disabled.
2. `preflight`: actual Gateway protocol 4 handshake, missing/wrong token rejection,
   Ed25519 device proof and granted operator scopes. No chat submission.
3. `smoke`: one fresh native session and one idempotent chat submission; native
   read → write → read over two fixed files with unpredictable input values.
4. `cancellation`: a separate native text-generation task; require actual text
   and active state before abort, then matched run IDs, native aborted and idle.

The probe does not retry uncertain submissions. It saves native events, session,
history, terminal response and submission identity privately. It requires an
explicit native idle state, paired successful file tool calls, matching random
artifact content and observed model transport. Configuration or preflight failure
must be resolved before starting a paid model case. Native pairing requirements
must be met using the native mechanism; device authentication is never disabled.

The operator captures bounded private logs, stops only its labelled container,
verifies the original image/container identity and PID 0, removes it, then checks
the loopback port is released. A stopped worker does not by itself prove native
run cancellation or that upstream billing stopped. The service remains dependent
on the external model endpoint's own limits.

## Actual transport observation

The Node preload wraps the actual global `fetch` and the fixed image's
`require('undici/index.js').fetch` export before native code loads it. This covers
the upstream dispatcher-specific path without changing its dispatcher, DNS,
request arguments, AbortSignal, redirect policy or Response/Error identity.
Identical first nested transport calls share a record; independent retries and
additional sends consume independent records.

Private JSONL records contain request IDs, URL/model hashes, status, response URL
hash and redirect state, never raw keys, prompts or endpoint URLs. The probe
requires exactly one successful terminal record per observed request, the fixed
target and body model hash, HTTP 200, no redirect and no unknown/missing/error
records. An unreadable request body stays unknown instead of consuming/cloning a
native stream. The shared six-request limit applies to matching instrumented
fetch calls, not every possible network operation, provider billing or redirects.

Component tests exercise actual Undici dispatcher behavior without network or
credentials. They do not prove the native Gateway selected that path: a real
Gateway report must supply its own observed transport evidence.

## Primary sources

- [Official Docker deployment and registries](https://github.com/openclaw/openclaw/blob/dc2523e5a5c7a309517fd7fa834add938231d526/docs/install/docker.md).
- [Gateway handshake](https://github.com/openclaw/openclaw/blob/dc2523e5a5c7a309517fd7fa834add938231d526/docs/gateway/protocol/handshake.md).
- [Native chat methods](https://github.com/openclaw/openclaw/blob/dc2523e5a5c7a309517fd7fa834add938231d526/src/gateway/server-methods/chat.ts).
- [Native fetch guard](https://github.com/openclaw/openclaw/blob/dc2523e5a5c7a309517fd7fa834add938231d526/src/infra/net/fetch-guard.ts).
- [Native dispatcher loader](https://github.com/openclaw/openclaw/blob/dc2523e5a5c7a309517fd7fa834add938231d526/src/infra/net/undici-dispatcher-options.ts).
