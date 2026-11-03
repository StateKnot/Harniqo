# 开发主线与偏离检查

用户指令（2026-11-03）：交付 macOS/Windows/Linux 客户端，连接和管理云端 Harness、持续对话、单任务比较与批量评测。按原里程碑推进，生产级要求在对应阶段验收。

1. 每轮开始读取 [开发主线](docs/planning/MAINLINE.md)，选定一个原 BACKLOG 任务 ID，写清本轮交付、必要前置、完成条件及它解锁的下一项。主线是 M0 有限技术准入 → M1 客户端/控制面底座 → M2 首个 Harness 真实闭环 → M3/M4 工作台与多 Harness → M5/M6 比较与批测 → M7/M8 运营与发行。
2. 每完成一个子任务、出现新失败或准备扩大范围时，重新核对原任务退出条件。满足后推进下一任务；需要后续阶段实现的能力，登记到对应原任务。基础设施工作必须说明它解除的当前阻塞，完整生产 Runner 的成熟度不自动成为 M0 的新增前置。
3. 在 VM、模型、管理员操作、全量 CI、大构建或额外评审前，明确待解决的具体问题和最小充分验证。复用同源已通过的证据；仅在相关源码/环境改变、真实失败或未决风险需要时补测。任务与所有代理共用同一范围和退出条件。
4. 若连续两批工作仍只增加同一底层主题的子项或测试数量，且未关闭原任务条件、交付产品能力或解除明确阻塞，先纠正任务分配与范围，再继续执行。
5. 每轮进度报告列出原任务 ID、实际交付、验收结果、父任务状态和下一项。测试/证据数量单独计量；子项完成不改变父任务或阶段的未通过状态。外部资源缺失明确记录影响，只推进依赖已满足的独立工作。

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
