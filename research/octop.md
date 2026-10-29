# Octop 完整产品补充核验

核验日期：2026 年 10 月 27 日。范围为腾讯云官方 `TencentCloud/Octop`，与底层 `TencentCloud/octop-harness` 分别记录；不等同于 `@tencent-ai/agent-harness`。本轮只读文档、源码与发行元数据，未安装运行。

## 身份与实际发行

Octop 主仓 MIT，定位为面向家庭和小团队的自托管多用户、多 Agent 助手。读取源码快照为 `890111b7879665840414ca28ab168b05b0db0921`。GitHub latest 指向 2026-10-25 发布的 `v1.0.3b1`，实际包含 macOS、Windows、Linux 的 amd64/arm64 桌面与 portable 资产；名称含 b1，而 GitHub prerelease 标记为 false，不能单独由任一标记推导出企业生产成熟度。[README](https://github.com/TencentCloud/Octop/blob/890111b7879665840414ca28ab168b05b0db0921/README.md)、[MIT](https://github.com/TencentCloud/Octop/blob/890111b7879665840414ca28ab168b05b0db0921/LICENSE)、[发行页](https://github.com/TencentCloud/Octop/releases/tag/v1.0.3b1)

桌面壳是 Wails v3，捆绑 Python 运行包并启动 Octop，不能把“三端安装包”直接理解成无本地后端的远程薄客户端。构建文档要求匹配平台的原生 CI，并列出备份、迁移与升级职责。[Desktop](https://github.com/TencentCloud/Octop/blob/890111b7879665840414ca28ab168b05b0db0921/desktop/README.md)

## 对本产品最相关的能力

完整产品包含 Web 工作台、多用户 JWT/角色/资源可见性、独立专家工作区、模型配置、MCP/OAuth connectors、skills、知识库、定时任务与远程操作。后端为 FastAPI/uvicorn 单进程，控制面数据库默认 SQLite，已提供 PostgreSQL 选择；不应沿用早期 fork 中“只有 SQLite、依赖库尚未开放”的描述。[架构](https://github.com/TencentCloud/Octop/blob/890111b7879665840414ca28ab168b05b0db0921/docs/architecture.md)、[当前依赖](https://github.com/TencentCloud/Octop/blob/890111b7879665840414ca28ab168b05b0db0921/pyproject.toml)

原生聊天当前主要使用 WebSocket，文档提供订阅活动 turn、显式取消、HITL resume、轨迹 ledger、按序号续接和 JSONL 导出；断开订阅连接本身不取消 turn。这些是文档契约，进程崩溃后的在途副作用仍须另测。[API](https://github.com/TencentCloud/Octop/blob/890111b7879665840414ca28ab168b05b0db0921/docs/api.md)

## 多 Harness 接入的真实语义

ACP 有两个方向：`octop acp` 把 Octop 专家暴露给外部客户端；`acp_runner` 让 Octop 专家向外部 Agent 委派任务。内置 runner 包括 OpenCode、CodeBuddy、Claude Code、Codex、Kimi、Cursor、Pi；外部 CLI 在运行 Octop 的机器上安装。当前 ACP 使用 stdio JSON-RPC，没有 HTTP ACP endpoint；主产品自身 HTTP/WS API 是另一层协议。[ACP](https://github.com/TencentCloud/Octop/blob/890111b7879665840414ca28ab168b05b0db0921/docs/acp.md)

外部任务提供 start/message/status/respond/close 等动作与权限响应，因此值得研究。不过“Octop 主 Agent 通过工具委派另一个 Agent”与“客户端直连外部 Harness 的全部原生会话”不同。横向评测应明确测 Octop 自身、外部原生 Harness，还是两层委派组合，并记录新增 prompt、模型调用、工具转换、费用和会话身份。

跨实例 Bridge 允许连接另一 Octop 并使用对端专家，对话在专家所在实例执行；设计文档仍标实现中，发行记录已有相关修复。它是跨实例协同，不能推导出已交付通用 Harness 的云端资源编排与弹性调度。[Bridge](https://github.com/TencentCloud/Octop/blob/890111b7879665840414ca28ab168b05b0db0921/docs/bridge.md)、[发行记录](https://github.com/TencentCloud/Octop/releases/tag/v1.0.3b1)

## 选型位置与生产验收

建议将 Octop 升入“重点直接竞品与私有部署复用候选”，将 Octop Harness 另列为底层 runtime 候选。优先考察多用户工作台、专家/模型/技能配置、原生 API、工作区、远程协同和桌面打包。

现有多用户资源授权不能替代不互信租户的执行隔离。当前架构是单进程；README 的 Managed Agents、项目级工作区、云边卸载仍属规划项。面向公开 SaaS，仍需本平台的执行池、租户资源边界、配额/预算、可靠调度和故障对账；本轮也未核实完整的 Dataset/Trial/Verifier 批量评测系统。[Roadmap](https://github.com/TencentCloud/Octop/blob/890111b7879665840414ca28ab168b05b0db0921/README.md#-roadmap)、[架构](https://github.com/TencentCloud/Octop/blob/890111b7879665840414ca28ab168b05b0db0921/docs/architecture.md)

新增验收项：外部 runner 的原生 session/审批/取消是否可保留；断线和重启后任务状态是否可核对；跨用户文件、记忆与执行权限是否隔离；Bridge 与本地两层身份是否一致；批量试验是否能冻结外层 Octop 与内层 Harness 的完整配置。

