# 多 Harness 客户端：接入协议与运行时预研

调研日期：2026-10-27（Asia/Shanghai）。范围是 Claude Code、Codex、OpenCode、Gemini CLI、goose，以及 ACP/A2A/MCP 的职责边界。以下“支持”表示官方当前文档或源码提供接口，不表示已完成端到端实测、压测或商业授权确认。未安装、启动任何 harness，也未使用用户的模型凭证或产生模型调用费用。

## 结论

建议采用“自有远程管理协议 + 云端 Runner + 多种 Adapter”，而非让桌面端直接暴露和连接每个 CLI 的内部端口。ACP 是有用的通用接入层，但目前不能替代生产环境的身份、租户授权、事件持久化、审批审计和任务调度。原生 SDK/API 在能力更丰富时应保留，不能强行压成最低共同能力。这是基于下文接口边界得出的架构建议。

第一批接入优先考察 OpenCode 原生 Server API、Claude Agent SDK；Codex 使用独立适配器，并把其官方远程支持警告、认证路径和版本兼容列为上线门槛；Gemini CLI 与 goose 用于验证通用 ACP 适配器。用户所说“各种 harness”应在产品承诺中落成一个个经过测试的“harness × 版本 × 认证方式 × 能力”组合，而非无限兼容。

## 1. 接口能力矩阵

| Harness | 首选集成面 | 会话/事件/审批/取消 | 远程连接边界与保真程度 |
|---|---|---|---|
| Claude Code | Python/TypeScript Agent SDK；批作业可用 `claude -p` JSON/stream-json | SDK 提供 resume/fork、消息/工具事件、`canUseTool` 审批与用户问题回调；`interrupt()` 结束当前轮次 | SDK 管理本地 CLI 子进程；需要自建 HTTP/WebSocket 服务。原生能力高，主会话 token delta 与子 agent 完整消息有不同语义。 |
| Codex | 富交互用 app-server JSON-RPC；自动化用 Codex SDK/非交互执行 | thread/start/resume/fork/read/list、turn/start/steer/interrupt；事件与审批是双向 RPC | 优先把 stdio 留在 Runner 内部。官方提供 WebSocket/Unix socket，但远程说明有明确生产支持警告；不能把封装接口等同厂商支持承诺。 |
| OpenCode | `opencode serve` HTTP/OpenAPI + SSE；也支持 `opencode acp` | 原生 API 有 session 列表、消息、fork、abort、diff、权限回复与事件流 | 多客户端架构，原生 API 最容易接入管理 UI；服务密码是 Basic Auth，不是多租户 RBAC。ACP 部分内置 slash 命令不支持。 |
| Gemini CLI | `gemini --acp`；批作业 `-p --output-format stream-json` | ACP 提供初始化、认证、新建/加载、prompt/cancel；CLI 自动保存工具执行与 token 数据，可按 ID resume | ACP 是 stdio JSON-RPC；remote 通过 Runner 桥接，不能当它已有官方统一云端会话服务。 |
| goose | `goose acp` stdio；`goose serve` HTTP/WebSocket ACP | 以标准 ACP 能力协商为准；不要猜测未公开的扩展 API | 已有远程 Desktop 指南、共享 secret、TLS、证书指纹。HTTP/WS 是 goose 的实现能力，不代表所有 ACP agent 都支持同一网络 transport。 |

矩阵证据：[Claude SDK 概览](https://code.claude.com/docs/en/agent-sdk/overview)、[Claude 流式输出](https://code.claude.com/docs/en/agent-sdk/streaming-output)、[Claude 审批](https://code.claude.com/docs/en/agent-sdk/user-input)、[Claude CLI 运行与停止](https://code.claude.com/docs/en/headless)、[Codex app-server](https://learn.chatgpt.com/docs/app-server)、[Codex SDK](https://learn.chatgpt.com/docs/codex-sdk)、[OpenCode Server](https://opencode.ai/docs/server/)、[OpenCode ACP](https://opencode.ai/docs/acp/)、[Gemini ACP](https://geminicli.com/docs/cli/acp-mode/)、[Gemini headless](https://geminicli.com/docs/cli/headless/)、[Gemini 会话](https://geminicli.com/docs/cli/session-management/)、[goose ACP](https://goose.vcorp.ai/docs/gdk/acp/)、[goose 远程服务](https://goose.vcorp.ai/docs/guides/remote-goose-server/)。

## 2. 关键差异，不能被“统一聊天”隐藏

### Claude Code

- Agent SDK 提供的是 Claude Code 的运行时，包括工具循环、权限、会话、hooks 和上下文管理；直接调用 Claude Client SDK 不是同一层次，自己重写工具循环也不能称为原 harness。[官方概览](https://code.claude.com/docs/en/agent-sdk/overview)
- 会话历史持久化不等于工作目录持久化。多人/多会话系统必须显式保存并使用 session ID；“继续最近一次会话”容易把不同用户串到一起。[会话文档](https://code.claude.com/docs/en/agent-sdk/sessions)
- 长期运行不是无状态 API：SDK 和 CLI 子进程通过 stdio 连接。官方部署说明覆盖容器、会话存储、跨宿主恢复和隔离；工作目录与 memory 文件仍需要独立持久化策略。[部署文档](https://code.claude.com/docs/en/agent-sdk/hosting)
- 许可回调只覆盖需要走到询问的动作，已经自动批准的工具不会经过 `canUseTool`。强制对所有调用执行策略需理解 hooks 和权限规则顺序，不能把 UI 审批当作系统隔离。[权限文档](https://code.claude.com/docs/en/agent-sdk/permissions)
- token streaming 主会话与 subagent 的信息粒度不同，后者不能默认承诺逐 token 展示。原始事件应保留，统一事件另做投影。[输出文档](https://code.claude.com/docs/en/agent-sdk/streaming-output)
- 进程退出、轮次结束、任务完成是不同概念；SIGTERM 会留下未完成 turn，优雅中断应先走 SDK interrupt/SIGINT，再按超时策略停止进程。续聊成本可能是会话累计值，不能简单累加每次返回的总额。[CLI 文档](https://code.claude.com/docs/en/headless)

### Codex

- app-server 是富客户端集成接口；SDK 官方建议用于 CI/自动化。当前 Python SDK 已是 stable release，基于 app-server JSON-RPC，发布包携带固定版本的 CLI runtime；不能沿用“只有 TS 包”这一旧认知。[SDK 文档](https://learn.chatgpt.com/docs/codex-sdk)
- 官方远程 Code Mode host 说明有一句：“The app-server command and WebSocket transport are experimental and aren’t supported for production workloads.” 同一页又有 stable API surface/`experimentalApi` 的分层，因此不能把“所有 API 都实验性”或“只要 stdio 就获得官方生产支持”任一说法当成已证实事实。安全的产品判断是：将受控版本和兼容测试作为自己的支持范围，远程连接不直接依赖裸露的 WebSocket listener。[远程说明与协议](https://learn.chatgpt.com/docs/app-server#connect-a-remote-code-mode-host)
- 同页支持生成与 CLI 版本精确匹配的 TS/JSON schema。适配器应绑定运行时版本和 schema，处理不支持的方法/字段，默认不启用实验字段。[schema 与实验 API](https://learn.chatgpt.com/docs/app-server#message-schema)

### OpenCode

- `/doc` 提供 OpenAPI 3.1；SSE `/event` 与 `/global/event` 是事件入口，session REST API 提供 abort、diff、历史和权限回复。文档没有在本次读取内容中证明 SSE 具备持久游标重放保证，因此断线恢复不能只靠重新订阅 SSE；Runner 应保存自己的事件序号，并结合原生会话查询补齐。[Server 文档](https://opencode.ai/docs/server/)
- `OPENCODE_SERVER_PASSWORD` 开启 HTTP Basic Auth；生产入口还需要 TLS、用户/租户授权、资源范围检查。ACP 能力较全，但 `/undo`、`/redo` 等内置命令在文档中被注明不支持，不能宣称界面完全一致。[Server](https://opencode.ai/docs/server/)、[ACP 支持](https://opencode.ai/docs/acp/)

### Gemini CLI

- `gemini --acp` 的官方说明列出 session load、prompt/cancel，以及文件系统代理。具体 RPC 字段和能力应按所固定版本的实现与握手判断，不要照着文档里的简写方法名自行发包。[ACP 文档](https://geminicli.com/docs/cli/acp-mode/)
- headless 的 stream-json 含 init/message/tool_use/tool_result/error/result，适合批作业采集；它不自动等同完整双向审批 RPC。[Headless 文档](https://geminicli.com/docs/cli/headless/)
- 服务器环境可选 Gemini API key 或 Vertex AI。Google 账号登录、免费额度、订阅额度的产品使用规则另行核实，不以可在本机登录推导可做商业托管凭证共享。[认证文档](https://geminicli.com/docs/get-started/authentication/)

### goose

- 当前原生远程入口已有 secret 保护，HTTP 通过 `X-Secret-Key`；浏览器 WebSocket 的 query token 存在代理访问日志等暴露面，建议客户端走自有网关，网关到 Runner 使用受控凭证。此为基于官方认证形态的工程推论。[ACP 文档](https://goose.vcorp.ai/docs/gdk/acp/)
- 远程部署指南提供 TLS 与证书 pinning；不应复制其连接测试里的 `curl -k` 到产品配置。[远程指南](https://goose.vcorp.ai/docs/guides/remote-goose-server/)
- goose ACP Reference 当前明确说其标准之外的方法表尚未公布，因此不把管理 provider、安装扩展、计划任务等猜成稳定通用 API。[Reference 当前状态](https://goose.vcorp.ai/docs/gdk/acp/reference/)

## 3. ACP、MCP、A2A 的位置

| 协议 | 主要连接对象 | 对本产品的用途 | 不能默认解决 |
|---|---|---|---|
| ACP = Agent Client Protocol | IDE/客户端 ↔ coding agent | 通用 prompt、事件、工具展示、审批、会话能力协商 | 云端多租户平台、运行时调度、厂商订阅授权、所有 native 能力一致性 |
| MCP = Model Context Protocol | AI 应用/agent ↔ 工具和数据源 | 给不同 harness 注入相近的工具配置；配置工具与连接状态 | 完整 harness 会话控制与调度 |
| A2A = Agent2Agent | 独立 agent 系统 ↔ 远程 agent 系统 | 后续接入企业 agent、跨系统任务委派与产物交换 | 逐个 shell/file 动作的完整可见性；底层 agent 可保持黑盒 |

证据：[ACP 概览](https://agentclientprotocol.com/get-started/introduction)、[ACP 工具调用](https://agentclientprotocol.com/protocol/v1/tool-calls)、[MCP 概览](https://modelcontextprotocol.io/docs/2026-07-28/getting-started/intro)、[A2A 核心概念](https://a2a-protocol.org/latest/topics/key-concepts/)。注意 ACP 在其他资料中也可能指 Agent Communication Protocol；本报告明确指 Agent Client Protocol。

ACP v1 当前 transport 规范列出 stdio，Streamable HTTP 仍是 draft proposal；允许自定义 transport。goose 已做 HTTP/WS 不能推导这些 transport 在所有 ACP agent 上互通。ACP v2 仍标为 draft，首版不应依赖 draft 才成立。[官方 transports](https://agentclientprotocol.com/protocol/v1/transports)

ACP 会话加载/恢复必须看握手 capabilities；未声明不能调用。即使协议支持 resume，也不意味着能把 harness A 的原生会话状态转成 harness B 的状态。[Session Setup](https://agentclientprotocol.com/protocol/v1/session-setup)

ACP 的 terminal capability 代表 agent 可以要求“client 环境”执行命令。在云架构中，这个 ACP client 必须是沙箱内 Runner 的适配器，不能无意把云端 agent 的 terminal/file 请求转回用户桌面执行。桌面只接收展示数据与显式授权流程。[Terminals](https://agentclientprotocol.com/protocol/v1/terminals)

取消不保证所有嵌套活动已经停止：ACP 通用取消有 optional 语义，权限请求在 session/cancel 时还必须返回 cancelled。平台仍需定义取消确认、宽限期、进程树终止、沙箱销毁与产物保存顺序。[Cancellation](https://agentclientprotocol.com/protocol/v1/cancellation)、[工具权限](https://agentclientprotocol.com/protocol/v1/tool-calls)

## 4. 认证与许可证：上线前必须分开核实

| 项目 | 已验证的代码/包许可事实 | 已验证的服务使用边界 |
|---|---|---|
| OpenCode | 仓库 LICENSE 为 MIT | 模型 provider 的 OAuth/API 与托管规则单独适用，不能由 MIT 推导额度转售许可。 |
| Gemini CLI | 仓库 LICENSE 为 Apache-2.0 | API key/Vertex 有官方服务器认证路径；消费级 Google 订阅复用不在本次已确认范围。 |
| goose | 仓库 LICENSE 为 Apache-2.0 | 模型服务条款、扩展许可与用户私有工具凭证另外适用。 |
| Codex | 仓库 LICENSE 为 Apache-2.0 | 内置 app-server authentication 有商业/hosted 限制；应独立设计 API-key 与获准 SIWC 路径。 |
| Claude Agent SDK | Python SDK wrapper 为 MIT；TypeScript 仓库 LICENSE.md 为 Anthropic all rights reserved/Commercial Terms | 不能把 Python wrapper 的 MIT 扩大为闭源 Claude Code runtime 和服务的许可。 |

许可原文：[OpenCode](https://github.com/anomalyco/opencode/blob/dev/LICENSE)、[Gemini CLI](https://github.com/google-gemini/gemini-cli/blob/main/LICENSE)、[goose](https://github.com/aaif-goose/goose/blob/main/LICENSE)、[Codex](https://github.com/openai/codex/blob/main/LICENSE)、[Claude Python](https://github.com/anthropics/claude-agent-sdk-python/blob/main/LICENSE)、[Claude TypeScript](https://github.com/anthropics/claude-agent-sdk-typescript/blob/main/LICENSE.md)。这里只记录已读文件的许可标识，不代替对最终打包物及全部依赖的许可审核。

Claude 官方 SDK 概览明确：未经事先批准，第三方产品不能提供 claude.ai 登录或相应额度；应使用 API-key 认证。首版商业云托管不能把用户 Claude Pro/Max 订阅登录当作默认凭证方案。[官方概览](https://code.claude.com/docs/en/agent-sdk/overview)

Codex 官方认证段说明 app-server authentication 不允许商业或 hosted 服务；这不能扩大成“开源协议禁止商用”，也不能单凭这段推导所有 API-key 使用都被禁止。官方 SIWC 专门给出使用自行持有的 OAuth token、自定义 Responses provider 和 stdio 的配置，且当前 client ID 仅向部分商业合作伙伴开放，需申请。产品应区分 API-key/BYOK、内置 Codex managed ChatGPT 登录、获准的 SIWC 三条路径；商业场景各自适用条款仍需按实际模式确认。[认证段](https://learn.chatgpt.com/docs/app-server#auth-endpoints)、[SIWC app-server 配置](https://developers.openai.com/siwc/token-sharing-open-source/codex-app-server)、[SIWC 准入](https://developers.openai.com/siwc/request-client-id)

SIWC preview 不是通用付费 API 的同等能力：有 Responses 字段/工具限制，store:false 下本地 thread/resume 仍可用；按 env_key 配置刷新 token 时需重启 app-server 再 resume。不能在产品里仅切换 credential 类型而忽略这些差异。[Preview limitations](https://developers.openai.com/siwc/token-sharing-open-source/preview-limitations)

## 5. 生产架构建议与验收门槛（设计判断）

1. Adapter 和运行时一同 pin 版本，镜像按 digest 固定。启动时记录 harness version、adapter version、原生 capability、认证类型、工具/skills 配置哈希与 workspace ID。
2. Runner 负责本机 stdio/native API、文件与 terminal 能力、原生状态持久化；自有控制面负责用户授权、预算、调度、租约、审批状态与重连。
3. 统一事件采用信封：tenant/project/session/run、adapter/harness version、sequence、time、type、normalized payload、raw event 引用。统一层保留 extension 字段；UI 按 capability 显示 resume/fork/diff/usage/approval 等功能。
4. 每次会话同时持久化“展示事件”和“原生会话恢复所需状态”，另存工作目录快照；统一聊天历史不是原生运行状态的替代品。跨 harness 的“继续任务”创建新 run，附带可审计的摘要/产物，不假装原生无损迁移。
5. 审批绑定 tenant/session/run/tool call/native request ID、规范化参数哈希、有效期与当前权限版本；请求过期、重放、跨租户、已取消 run 一律拒绝。多个桌面在线时决策只接受一次。
6. 单个任务的 cancellation 必须有最终可观测状态；先协议级取消，宽限期后停止进程树，最后回收沙箱；区分 canceled、failed、timed out、infra error、awaiting approval。
7. UI 关闭与网络断开不等于取消云端任务。重连使用持久序号补发，动作发送采用 idempotency key；不要自动重发可能已经执行的工具动作。
8. 回归用例覆盖：双会话不串历史、断网/重连、不重复审批、审批中重启、取消后的孤儿进程、Runtime 升级后的历史恢复、超时/额度不足、事件重复/乱序/截断、工作目录丢失、原始 usage 与累计 cost 口径。

这些是正式实现前的接口/故障验收清单；本次研究没有验证任何组合已经通过上述门槛。
