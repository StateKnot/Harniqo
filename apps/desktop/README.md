# Desktop engineering entry — M1-01

The Electron/React desktop displays its build identity and lets the user test a
platform HTTP or HTTPS origin. A successful probe displays the public identity issuer.
Platform login and Harness sessions follow M1-03 and M2.

From the repository root, use the pinned Node.js/pnpm versions and install with
`pnpm install --frozen-lockfile`, then prepare Electron's native runtime and run:

```sh
pnpm --filter @harniqo/desktop exec install-electron
pnpm --filter @harniqo/desktop typecheck
pnpm --filter @harniqo/desktop test
pnpm --filter @harniqo/desktop build
pnpm --filter @harniqo/desktop start
pnpm --filter @harniqo/desktop smoke -- --receipt /absolute/path/source-smoke.json
pnpm --filter @harniqo/desktop package
pnpm --filter @harniqo/desktop smoke -- --packaged --receipt /absolute/path/package-smoke.json
```

Electron 45 provides `install-electron` explicitly rather than a postinstall lifecycle.
It uses the pinned package's built-in checksums; retain TLS and checksum validation.

Build uses local installed tooling and emits `dist/main/main.js`, a sandboxed
CommonJS preload, bundled local renderer assets and `dist/build-info.json`.
It does not start an HTTP development server. Node.js 24.22.1 is required.
`HARNIQO_SOURCE_SHA`, when supplied, must equal the actual checkout's Git commit.
Every build records the commit, whether Git sees uncommitted changes, and a
SHA-256 digest of source/lock/toolchain inputs. `dist/source-inputs.json` lists
the individual input hashes. CI refuses a dirty checkout. Build metadata contains
no generated timestamp. A commit identifier alone is not evidence of a clean
working tree; read `sourceDirty` together with `sourceInputHash`.

Packaging uses only the current native OS/architecture. It copies the compiled
application into a temporary staging directory, includes the project MIT license
and bundled React/react-dom/scheduler licenses, and retains Electron/Chromium's
upstream notices. `artifacts/package.json` records the native bundle directory,
application and executable paths and build identity. `appPath` points to the
actual `.app` on macOS and the application directory on Windows/Linux.
These unsigned internal bundles do not establish signed release or installation
support; M8 owns signing, installer/update channels and release verification.

Native smoke launches the real Electron process, mounts React, invokes the actual
preload, compares renderer/main/build identity, checks renderer `require` and
`process` absence and verifies sandbox/context isolation/Node settings. Packaged
smoke additionally requires `app.isPackaged === true`. It times out after at most
35 seconds, returns nonzero on failure and writes a machine-readable receipt.
`--app /absolute/path/to/executable` can select an explicit native package;
its embedded identity must still equal the current build. Each run uses and
removes a caller-created private temporary profile, leaving ordinary app data
alone. A GUI session is required; use an actual native desktop or Xvfb on Linux.

The preload exposes `getRuntimeInfo()` and `probePlatform(origin)`. Main checks
the exact window, top frame and document URL on every call. The probe requires
transient user activation and performs one bounded discovery request at a time.
The renderer receives the issuer or a typed error. Main bundles the shared
discovery client and canonical schema, with dependency notices in
`dist/main/THIRD-PARTY-MAIN.md`.

Renderer requests are limited to verified assets served by `harniqo://app`.
CSP denies renderer networking, frames, inline/eval scripts and native form
navigation. Main denies navigation, popups, webviews, permissions and downloads.
Native smoke covers preload activation denial. When both
`HARNIQO_SMOKE_DISCOVERY_ORIGIN` and `HARNIQO_SMOKE_BAD_CERTIFICATE_ORIGIN` are
provided, it submits the actual form against a live HTTPS endpoint and verifies
successful discovery and certificate rejection.
Set `HARNIQO_SMOKE_HTTP_DISCOVERY_ORIGIN` to exercise the same form against a
live HTTP discovery endpoint.
