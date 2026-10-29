# 多 Harness 客户端竞品预研

读取日期：2026-10-27（Asia/Shanghai）。范围：官方文档、GitHub 官方仓库、实际发布记录及少量关键实现。未安装产品、未运行真实模型任务；“支持”表示有文档/发布/源码证据，不等于已通过我们的生产验收。OpenDesign 由主报告单独研究。

## 建议先看这五个项目

| 项目 | 对用户设想的相关度 | Harness 的真实语义 | 部署/客户端 | 最值得借鉴 | 关键边界 |
|---|---|---|---|---|---|
| OpenHands Agent Canvas | 很高，优先做 build-vs-extend 验证 | 后端启动外部原生 agent，通过 ACP adapter 通讯；也有自有 OpenHands agent | Web；三端 desktop 早期预览；local/VM/Docker/cloud，多后端切换 | 后端注册、会话与工作区、API/事件、嵌入式 UI、自动化 | 已发布 ACP 桥自动批准工具请求，不能直接满足远程人工审批；桌面仍 preview |
| HAPI | 很高，最贴近“连接各处原生 harness” | wrapper/native app-server/Agent SDK/ACP/RPC，按 harness 分别接入 | Web/PWA、原生 iOS/Android；Hub 与多机器 Runner 可分离 | 原生会话保真、远程创建、审批流、多机器管理、能力矩阵 | AGPL-3.0；个人自托管优先，不能直接当作完整企业多租户控制面 |
| AionUi | 高，最贴近三端统一交互体验 | 自有 agent 与外部原生 CLI/ACP 两类并存 | macOS/Windows/Linux Electron；独立 WebUI/headless | 多 agent UI、文件/产物预览、团队协作、定时任务 | UI 与 AionCore 后端分仓；parallel/team/cron 不等于科学批量评测 |
| Happy | 高，但必须分旧 CLI 与新 Desktop | 旧 CLI 直接包装 Claude/Codex；新 Desktop 使用基于 Pi 的 Happy Agent 自有 harness | Mac/Windows/Linux、移动/Web、团队服务 | 跨设备体验、多人会话、同步、桌面组件拆分 | 新 Desktop 的多厂商体验不能充当 Claude Code/Codex 原生 harness 对照实验 |
| Vibe Kanban | 中高，适合代码复用与交互参考 | 多原生 coding agents executor | 三端本地应用/Web；源码有 remote/self-host | worktree、executor、diff review、任务板 | 公司已关停、托管服务 sunset；代码仍有社区提交，不能写成“项目完全停更” |

表格是以下官方证据的综合判断；关于“企业控制面”和“评测”的不足是本次范围内未见完整证据，并非断言其所有分支永远没有该能力。

## OpenHands：最值得先验证能否复用，但不能原样套壳

当前主仓已经是 Agent Canvas，不能沿用早期“只有 OpenHands 自有 agent”的印象。它连接一个或多个 Agent Server，提供聊天、终端、文件、设置和自动化；UI 本身不负责执行工具或实现隔离，且有可供宿主嵌入的 library entrypoints。[官方架构](https://github.com/OpenHands/OpenHands/blob/311dc1be63758e51d61e43d6574d5c03c7a30688/docs/architecture.md)

多个 backend 可以是本机、VM、Docker 或托管云。backend 配置包括 host 与 API key，设置和会话归属随 active backend 切换。这证明“同一入口连接不同环境”已有现成实现；不等于已经有跨全部后端的统一批量调度和租户治理。[Backends 文档](https://docs.openhands.dev/openhands/usage/agent-canvas/backends)

外部 Claude Code、Codex、Gemini、Pi、OpenCode 由 Agent Server 启动其 CLI 或 ACP adapter，由外部 agent 保持自己的工具和执行循环；不是把模型塞进 OpenHands loop。官方托管 Cloud 当前只列 Claude/Codex/Gemini，Pi/OpenCode 可在本机或自托管服务器使用，不应把自托管支持范围直接套给托管云。[ACP Agents 文档](https://docs.openhands.dev/openhands/usage/agent-canvas/acp-agents)

不是纯路线愿景：官方 v1.33.0 已于 2026-10-23 发布，实际附 macOS universal DMG、Windows EXE、Linux AppImage/DEB；但安装文档明确将桌面端称作 early preview，macOS 是 ad-hoc 签名。[v1.33.0 发布页](https://github.com/OpenHands/OpenHands/releases/tag/v1.33.0)；[桌面预览安装说明](https://docs.openhands.dev/openhands/usage/agent-canvas/setup#desktop-app-preview-build)

发布版 Canvas v1.33.0 锁定 Agent Server 1.58.1。[发布配置](https://github.com/OpenHands/OpenHands/blob/v1.33.0/config/defaults.json) 对应 SDK v1.58.1 的 ACP 桥在 `request_permission` 中直接选取第一个选项返回批准；`create_elicitation` 拒绝询问，注释写明 headless bridge 没有用户。它能驱动原生 agent，但目前这条路径不能直接作为人工审批与交互提问的保真实现。若选择复用，要改造此桥或使用独立 adapter。[已发布 SDK 源码 L1606](https://github.com/OpenHands/software-agent-sdk/blob/v1.58.1/openhands-sdk/openhands/sdk/agent/acp_agent.py#L1606)

自托管官方文档还记录：内置编辑器与 Canvas 同源，页面脚本可读浏览器 localStorage 内保存的各 backend key（链接 issue #16492）。面向企业部署时不能照搬其浏览器保存长期 backend secret 的信任模型。[自托管文档](https://github.com/OpenHands/OpenHands/blob/311dc1be63758e51d61e43d6574d5c03c7a30688/docs/SELF_HOSTING.md#L83)

主仓当前 MIT，可作为复用候选；仍须逐仓核对 SDK、自动化、扩展及第三方依赖许可证。[LICENSE](https://github.com/OpenHands/OpenHands/blob/311dc1be63758e51d61e43d6574d5c03c7a30688/LICENSE)

## HAPI：原生 Harness 适配和远程控制的参考价值最高

官方结构为 agent CLI/Runner → Socket.IO → Hub+SQLite → REST/SSE → Web/原生客户端。Hub 可以在 VPS/云主机运行，Runner 负责在指定机器创建会话；界面有历史、审批、文件、Git diff、终端与远程 spawn。[How it Works](https://github.com/tiann/hapi/blob/7bf0e5bc35240d718a33bd1045f3ca369173fa5b/docs/guide/how-it-works.md)

多台机器可连接同一 Hub，Runner 与 Hub 可以分机；workspace-root 能限制允许浏览和创建会话的目录。Hub 生成共享访问 token；这类个人自托管模式不能直接视为已具备企业 SSO、项目 RBAC、租户隔离和审计治理，需要另做设计。[Installation](https://github.com/tiann/hapi/blob/7bf0e5bc35240d718a33bd1045f3ca369173fa5b/docs/guide/installation.md)

接入能力按 agent 分别说明：Claude 本地包装 TUI、远程用 Agent SDK；Codex 使用 native terminal + app-server（文档要求 0.154.0+）；OpenCode/Copilot 等走 ACP；Pi 走 JSON-line RPC。是否有本地 TUI、remote、resume、permission modes 都逐项列出。这个能力矩阵方式值得复制，不能只承诺“任何 harness 都一样”。[官方 support matrix](https://github.com/tiann/hapi/blob/7bf0e5bc35240d718a33bd1045f3ca369173fa5b/docs/guide/agents.md)

v0.31.2（2026-10-27）附 Mac ARM/x64、Linux ARM/x64、Windows x64 CLI/Hub 二进制；这些是运行组件，不是三端原生桌面 GUI。[发布页](https://github.com/tiann/hapi/releases/tag/v0.31.2)

许可证为 AGPL-3.0，应把修改后网络提供服务时的对应源码义务纳入商业复用评估；不要简单复制代码进计划闭源的产品。[LICENSE](https://github.com/tiann/hapi/blob/7bf0e5bc35240d718a33bd1045f3ca369173fa5b/LICENSE)

## AionUi：三端产品体验与 ACP 集成的重要参考

官方列出三端安装、外部 CLI 自动检测、多会话独立上下文、统一 MCP 管理、ACP 团队协作，以及可独立运行 HTTP server 的 WebUI/headless 模式。它还包含自己的内置 agent，因而对比时必须区分“外部 CLI”与“内置 engine”。[README](https://github.com/iOfficeAI/AionUi/blob/11cd2a9a20d1e033157636efc570b41384c66d85/readme.md)

当前开发架构是 AionUi Electron frontend + AionCore local backend 双仓，不能仅 fork UI 就得到全部 agent 能力。[开发文档](https://github.com/iOfficeAI/AionUi/blob/11cd2a9a20d1e033157636efc570b41384c66d85/docs/contributing/development.md)

最新正式 release 元数据为 v2.2.2（2026-09-09），release notes 指定 AionCore v0.2.2，且明确安装包不再发布在 GitHub、改从官方站下载。GitHub assets 为空不能被误读成没有三端产品，但本次没有逐一下载安装包验证。[v2.2.2 发布页](https://github.com/iOfficeAI/AionUi/releases/tag/v2.2.2)

UI 与 Core 当前均可读取到 Apache-2.0 LICENSE，是可进一步做源码复用验证的候选。[UI LICENSE](https://github.com/iOfficeAI/AionUi/blob/11cd2a9a20d1e033157636efc570b41384c66d85/LICENSE)；[AionCore LICENSE](https://github.com/iOfficeAI/AionCore/blob/main/LICENSE)

## Happy：产品直接相关，但新旧运行时语义不同

旧 `happy` CLI 直接包装 Claude Code/Codex，支持从手机续接；当前 Desktop 则依赖 Happy Agent。主仓还包括移动/Web客户端和加密同步 relay。[仓库边界说明](https://github.com/slopus/happy/blob/main/README.md)

Happy Agent 明确基于 Pi 基础构建一个统一的自有 harness，重现 Codex、Claude Code、Grok 的 prompts/tools 特点。它是多模型体验和团队产品的对标对象；用它跑“Claude vs Codex”不能自动解释成“原版 Claude Code harness vs 原版 Codex harness”。[Happy Agent README](https://github.com/slopus/happy-agent/blob/main/README.md)

v0.0.102（2026-10-25，非 prerelease）实际有 Mac ARM/x64 DMG、Windows x64 EXE、Linux ARM/x64 AppImage，另有 Nightly 资产。[发布页](https://github.com/slopus/happy-desktop/releases/tag/v0.0.102) 桌面开发文档拆分 state、UI、app、Electron、web、验证工具包，适合参考“交互共享、本地边界独立”的代码组织。[DEVELOPMENT](https://github.com/slopus/happy-desktop/blob/db12dbeb1c6c522b6e388a4ce359b08b53e57d07/DEVELOPMENT.md)

当前主仓声明 MIT，并提供对应 LICENSE。厂商订阅可用性仍受各厂商条款约束，不能把 Happy 的产品文案当成第三方产品转售/共享额度的授权。[MIT LICENSE](https://github.com/slopus/happy/blob/main/LICENSE)

## Vibe Kanban：保留为参考实现，注意维护与托管状态

官方于 2026-04-10 宣布 bloop 关停、托管 remote services 30 天后移除，项目转开源社区维护，本地 workspaces 继续。撤销对象包括 kanban issues、comments、projects、organisations。[官方关停公告](https://www.vibekanban.com/blog/shutdown)

截至读取时 main 仍含 sunsetting 标识，但源码仍在更新：2026-10-26 提交修正 Copilot session namespace。因此准确表述是“商业运营与托管服务 sunset，开源代码仍有社区维护”；没有查到官方托管服务恢复公告。[当前 README](https://github.com/BloopAI/vibe-kanban/blob/a019cebf0693f787ef0f8dc2bb8d37edd35527ac/README.md)；[2026-10-26 提交](https://github.com/BloopAI/vibe-kanban/commit/a019cebf0693f787ef0f8dc2bb8d37edd35527ac)

其 workspaces、10+ agent executor、diff 反馈、预览和 PR 工作流有借鉴价值；许可证 Apache-2.0。最新正式 release 元数据仍是 v0.1.44-20260424091429，而不是按 main 新提交推断已有新发行版。[发布页](https://github.com/BloopAI/vibe-kanban/releases/tag/v0.1.44-20260424091429)；[LICENSE](https://github.com/BloopAI/vibe-kanban/blob/a019cebf0693f787ef0f8dc2bb8d37edd35527ac/LICENSE)

## 对产品定位的启示（研究判断）

1. “三端 UI + 多 agent 聊天 + 云主机连接”已有密集竞争。应优先验证 OpenHands、AionUi 能否满足基础交互，再决定自研范围。
2. 上述产品的 parallel sessions、task board、team、cron 解决“安排多个 agent 工作”；本次没有在这些产品的用户工作流中验证到完整的固定数据集、环境快照、预算、重复次数、客观评分和统计比较闭环。不能把“没发现完整闭环”写成绝对市场空白，但它仍是很值得验证的差异化方向。
3. 原生 harness 评测必须保留各自执行 loop，保存 CLI/adapter 版本与配置。自有统一 runtime、多厂商模型切换是另一种实验，应该分别标注。
4. 企业价值可能来自可复现评测、环境与成本治理、私有云/自托管、多租户权限、故障恢复，而不是再增加一个聊天窗口。Vibe Kanban 的官方关停原因提示：大量免费使用并不自动形成有效商业模式。

## 取证版本

| 官方仓库 | 读取到的 main SHA / release | 时间 |
|---|---|---|
| OpenHands/OpenHands | `311dc1be63758e51d61e43d6574d5c03c7a30688`；正式 release `v1.33.0` | main 2026-10-27；release 2026-10-23 |
| tiann/hapi | `7bf0e5bc35240d718a33bd1045f3ca369173fa5b`；`v0.31.2` | 2026-10-27 |
| iOfficeAI/AionUi | `11cd2a9a20d1e033157636efc570b41384c66d85`；`v2.2.2` | main 2026-10-23；release 2026-09-09 |
| slopus/happy-desktop | `db12dbeb1c6c522b6e388a4ce359b08b53e57d07`；`v0.0.102` | main 2026-10-26 UTC；release 2026-10-25 |
| BloopAI/vibe-kanban | `a019cebf0693f787ef0f8dc2bb8d37edd35527ac`；`v0.1.44-20260424091429` | main 2026-10-26；release 2026-04-24 |

main SHA 和 release 元数据通过官方 GitHub connector 的仓库 commits/main 与 releases/latest 读取；动态官网文档未固定版本。下一阶段若做兼容性验收，应锁定明确 release、adapter 与模型配置，重新验证其接口行为。
