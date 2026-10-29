# OpenDesign 对多 harness 客户端的参考价值

核验日期：2026 年 10 月 27 日。用户已确认项目为 `nexu-io/open-design`。源码阅读固定在 `26d60298f8129e229a1c10aab4df7ca25b1e9ad3`；官网的发布信息与这个源码快照分别描述，不能当作同一发布版本。以下是文档与选定源码审阅，未安装或运行项目。

## 产品边界

OpenDesign 当前定位为协作式 AI 设计工作区，主要围绕原型、演示文稿、图片和动效。官网列出本地 CLI、自带 API Key 和托管模型三类入口。它已经有桌面产品：官网明确提供 macOS 和 Windows 安装方式，Linux 目前从源码运行。将它描述为“只有 Web 端”已不准确。[官网](https://open-design.ai/)

公开主仓采用 Apache 2.0；这不自动包含所有第三方 CLI、模型服务、商标和云端账号使用的权利。[LICENSE](https://github.com/nexu-io/open-design/blob/26d60298f8129e229a1c10aab4df7ca25b1e9ad3/LICENSE)

## 可以借鉴的实现

架构由 Web UI、daemon、持久化和 agent 子进程组成。文档明确当前使用 HTTP 与 SSE、SQLite，以及 Electron 桌面壳；生产 daemon 可在同一来源提供静态前端和 API。CLI 与 UI 复用 daemon API。这说明“桌面客户端加远端执行服务”是可落实的结构，但并不证明这个部署已经提供独立运行沙箱或完整多租户隔离。[架构](https://github.com/nexu-io/open-design/blob/26d60298f8129e229a1c10aab4df7ca25b1e9ad3/docs/architecture.md)

适配器以声明式 `RuntimeAgentDef` 描述可执行文件、参数、能力探测和事件格式。通用引擎处理启动和输出归一化；不同协议仍有专门 transport/parser。文档与源码中可见 Claude 的 stream JSON、Codex app-server、OpenCode JSON 事件、ACP、Pi RPC 等路径。应借鉴其“复用上游 agent loop”的职责边界。[适配器说明](https://github.com/nexu-io/open-design/blob/26d60298f8129e229a1c10aab4df7ca25b1e9ad3/docs/agent-adapters.md)、[Codex 定义](https://github.com/nexu-io/open-design/blob/26d60298f8129e229a1c10aab4df7ca25b1e9ad3/apps/daemon/src/runtimes/defs/codex.ts)、[Claude 定义](https://github.com/nexu-io/open-design/blob/26d60298f8129e229a1c10aab4df7ca25b1e9ad3/apps/daemon/src/runtimes/defs/claude.ts)

运行模型区分 Project、Conversation、Message 和 Run，执行状态由 daemon 持有，前端通过运行身份重新附着。这个分层适合借鉴：聊天界面关闭不应成为取消执行的信号。生产产品还需验证 daemon 重启后的进程接管、事件耐久性与取消确认，不能从“可重新连接”推导出“任意崩溃自动恢复”。[Run 模型](https://github.com/nexu-io/open-design/blob/26d60298f8129e229a1c10aab4df7ca25b1e9ad3/specs/current/run.md)

## 云端部署的实际条件

仓库已有 Compose、镜像与部署说明，包含 daemon token、来源限制、反向代理和 SSE 配置。公开镜像并不附带所有 agent CLI；服务端需要在容器内安装可用二进制或准备私有运行镜像。示例也包含挂载宿主 CLI 与凭据的单用户部署方式，不适合直接作为面向不可信代码的共享租户架构。[Docker 部署](https://github.com/nexu-io/open-design/blob/26d60298f8129e229a1c10aab4df7ca25b1e9ad3/deploy/README.md)

外部 orchestrator 可以提供 scratch workspace 并标记来源，OpenDesign 负责对给定目录读写和产出结果；源代码 checkout、写回、PR 与发布策略仍由外部系统持有。这为把 OpenDesign 作为一个“设计任务工作区集成”提供了明确边界。[Externally Prepared Workspaces](https://github.com/nexu-io/open-design/blob/26d60298f8129e229a1c10aab4df7ca25b1e9ad3/docs/orchestrator-workspaces.md)

Headless BYOK 的公开契约允许通过 `POST /api/runs` 提交运行，并带 `clientRequestId` 处理同一逻辑提交的网络重试。daemon 凭据和模型提供方凭据是两类身份，返回 202 只是接受运行，并不代表生成成功。[Headless BYOK](https://github.com/nexu-io/open-design/blob/26d60298f8129e229a1c10aab4df7ca25b1e9ad3/docs/headless-byok.md)

## 不宜照搬的边界

适配器文档明确，多种 CLI 使用无需交互审批的权限参数，cwd 不构成统一沙箱。ACP 集成文档还明确在收到权限请求时自动选择允许选项。这种模式需要外部执行边界兜底；若产品要求用户逐项审批，就必须重新实现审批通道并验证每个适配器，不能只复制其解析器后宣称支持审批。[权限边界](https://github.com/nexu-io/open-design/blob/26d60298f8129e229a1c10aab4df7ca25b1e9ad3/docs/agent-adapters.md#10-authorization-boundaries)、[ACP 集成](https://github.com/nexu-io/open-design/blob/26d60298f8129e229a1c10aab4df7ca25b1e9ad3/docs/new-agent-runtime-acp.md)

产品判断：OpenDesign 适合研究 agent 发现、能力探测、结构化流解析、设计产物预览和本地 daemon。对于通用 harness 管理产品，优先复用经审查的模块或契约；是否整仓 fork，要由设计工作流在目标产品中的占比决定。云端沙箱、可靠调度、跨版本兼容、原生会话保留和评测体系仍应作为独立职责设计。
