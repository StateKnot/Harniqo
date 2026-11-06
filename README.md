# Harniqo

**The open-source control center for agent harnesses.**

开源的 Agent Harness 控制中心：通过 macOS、Windows、Linux 客户端，在自有云部署、使用和比较不同 Agent Harness。

## 项目状态

开发主线是 **M0 技术准入 → M1 客户端与控制面底座 → M2 首个 Harness 真实闭环 → 多 Harness、比较与批量评测**。[会话执行规则](AGENTS.md)要求每轮核对原任务、交付、退出条件和最小验证；[当前主线](docs/planning/MAINLINE.md)列出有限的 M0 收尾项与本轮范围。

M0 的 8 项中 4 项已验收，完整生产执行准入仍在进行。M1-01 的独立工程工作面已开始：固定 workspace/工具链、Electron/React/TypeScript 可构建入口、只读桌面 Interface 与原生打包检查。本机 macOS arm64 与四组合[实际原生 CI](https://github.com/StateKnot/Harniqo/actions/runs/41595744654)的打包启动/渲染已验证，指定服务器构建通过；[工程结果](docs/operations/m1-engineering-2026-11-03.json)和[CI范围](docs/operations/m1-desktop-ci-2026-11-03.json)分别记录。登录、平台连接及真实 Harness 会话尚未实现，工程入口不代表可用产品或正式支持。

既有 Harness、VM、网络、限额和故障回收的结果、失败历史及范围见 [M0 执行证据索引](docs/planning/M0-RESULTS.md)。已有 Runner/transport/spool 实现按原 M2/M7 任务复用，整项和阶段状态以 [BACKLOG](docs/planning/BACKLOG.md) 为准。

## 客户端工程

使用 [.node-version](.node-version) 和 [package.json](package.json) 中固定的 Node/pnpm，运行：

```sh
pnpm install --frozen-lockfile
pnpm --filter @harniqo/desktop exec install-electron
pnpm --filter @harniqo/desktop typecheck
pnpm --filter @harniqo/desktop test
pnpm build:desktop
pnpm --filter @harniqo/desktop start
```

打包、原生冒烟和构建来源见 [桌面工程说明](apps/desktop/README.md)。macOS arm64 的[内部安装流程](docs/operations/DESKTOP-INTERNAL-INSTALL.md)已在真实 Mac 验证。正式签名、公证、其他目标系统安装及完整用户流程按里程碑验收。

生产目标同时覆盖团队私有部署和多租户 SaaS；执行环境首版限定为 Linux，客户端覆盖三端。

| 文档 | 用途 |
| --- | --- |
| [产品规格](docs/product/PRD.md) | 首版权限、导航、六条完整用户流程和验收口径 |
| [机器契约](packages/contracts/README.md) | JSON Schema、OpenAPI、状态迁移和可复现验证 |
| [技术架构](docs/architecture/ARCHITECTURE.md) | 技术栈、七个业务 Module、数据与接口、执行隔离、双部署和关键决策 |
| [开发主线与收尾清单](docs/planning/MAINLINE.md) | 每轮任务选择、退出条件、资源约束及已有代码复用归属 |
| [开发方案与里程碑](docs/planning/ROADMAP.md) | 产品范围、架构、阶段依赖与发布门槛 |
| [开发任务清单](docs/planning/BACKLOG.md) | 67 项任务，含交付物和验收条件 |
| [M0 执行证据索引](docs/planning/M0-RESULTS.md) | 已验收状态、真实验证摘要、失败历史和未覆盖边界 |
| [验收与执行规则](docs/planning/ACCEPTANCE.md) | 真实环境验收、失败场景与证据要求 |
| [领域术语](GLOSSARY.md) | 后续文档、API 与代码采用的统一概念 |
| [预研报告](research/multi-harness-client-research.md) | 候选运行时、协议、复用边界和资料来源 |

## 命名

Harniqo 读作「哈尼科」，取意于 Harness 与 IQ。建议仓库名 `harniqo`，未来命令名使用同一拼写，界面品牌使用 `Harniqo`。

2026-10-29 对 GitHub 仓库名称及公开搜索进行了重名初筛，未发现明显同名软件项目；域名、包名与商标尚未核验或注册。当前文档采用此项目名。

正式仓库：[StateKnot/Harniqo](https://github.com/StateKnot/Harniqo)。项目采用 **MIT** 许可证；第三方依赖、数据和集成运行时分别遵循各自许可。镜像存放规范见 [images](images/README.md)。
