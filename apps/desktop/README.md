# Desktop engineering entry — M1-01

This is an executable Electron/React/TypeScript engineering entry. It displays the
actual local application identity and an explicit disconnected state. Platform
login, environment configuration and real Harness sessions remain M1-03/06/07 and
M2 work. No cloud or Harness capability is simulated.

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

The only renderer capability is `window.harniqo.getRuntimeInfo():
Promise<RuntimeInfo>`, defined in `src/shared/interface.ts`. It accepts no
arguments. Main checks the exact window, main frame and document URL on every
call. No generic IPC, shell, URL opening, file access or credential channel is
exposed. Renderer requests are limited to exact build-manifest assets served by
`harniqo://app`; hashes are checked before delivery. CSP denies network, frames,
inline/eval scripts and forms. Main denies navigation, redirects, popup windows,
webviews, permissions and downloads. Security regressions exercise hostile
senders, subframes, extra arguments, protocols and path traversal; they do not
substitute for actual native smoke or the later M1-07 malicious-content suite.
