# Development workflow

1. Select the next task whose prerequisites are satisfied in `docs/planning/BACKLOG.md`. Read `GLOSSARY.md` for domain terms and `docs/architecture/ARCHITECTURE.md` when changing module interfaces, data ownership, execution or deployment.
2. Implement the complete task and run its relevant checks from `docs/planning/ACCEPTANCE.md`. Before building, testing or packaging, read `df`; preserve evidence outside generated build directories. Use the designated server workspace for server-side acceptance; its connection details and allowed host ports are in `.private/development-environment.md`.
3. Record the task ID, source/version, actual environment, checks, evidence and remaining limitations in the backlog execution record. Set the per-task status to match the evidence. `IMPLEMENTED` means code exists; `VERIFIED` means its declared verification passed; `ACCEPTED` means every task exit condition passed.
4. As each task becomes `ACCEPTED`, mark it **已开发完成** in the backlog and check the matching task in its existing GitHub stage Issue. Keep unverified tasks unchecked. Close the stage Issue and milestone only after every required task is accepted; synchronize source and evidence indexes before reporting completion.

## Verification branches

- Product/permission/workflow changes: use `docs/product/PRD.md`; test the affected roles and cross-project cases.
- Contract changes: use `packages/contracts/README.md`; regenerate OpenAPI from canonical definitions and validate the positive, negative and lifecycle fixtures. Respect the draft2020-12 compiler requirement in the architecture.
- Harness, VM or model qualification: use `scripts/qualification/README.md` and `MODEL-PROBE.md`. Distinguish model protocol, native Harness integration and production VM isolation; each needs its own evidence. Preserve failed runs and recovery evidence.
- Dependency, image or release changes: use `docs/compliance/DEPENDENCIES.md`, `docs/operations/ACCEPTANCE-BASELINE.md` and `images/README.md`; update pinned sources and checksums before acceptance.

## Workspace and data

Use `images/` for container archives, VM bases and task write layers, and `.state/` for rebuildable local runtime state. Keep credentials, real infrastructure, raw qualification evidence and business inputs under access-controlled private storage. Only publish sanitized evidence indexes; `.private/`, `.state/` and runtime image binaries remain outside Git.

Use actual native systems to substantiate desktop support and actual execution resources to substantiate isolation/recovery. A successful mock, schema validation, package installation or UI render establishes only its stated scope. Continue independent work when an external prerequisite is missing, and record the affected task and concrete unblock condition.
