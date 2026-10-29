# 开放通用 Agent Runtime 候选核验

核验时间：2026-10-27，Asia/Shanghai。筛选口径：源代码开放、可部署在自有云、可替换模型、包含实际 Agent loop 与工具执行；区分完整服务与嵌入式运行时/SDK。只读检查官方源码、官方文档、npm 发布元数据，未启动服务、未付费调用模型、未压测。

## 先行结论

| 候选 | 身份与许可 | 实际形态 | 目前判断 |
|---|---|---|---|
| DeepSeek Harness / DSH | DeepSeek 官方仓库，MIT | 完整可运行 agent runtime；Web、headless、SDK JSON-RPC、ACP 等 profiles | 符合 runtime 候选，值得优先适配；已确认 pi-ai 自定义端点，developer preview、版本兼容及模型适配语义须验收 |
| TrueForge | TrueFoundry 官方仓库，MIT | 完整 UI/API/loop/session 服务；有独立 core/SDK 包 | 完整自托管平台候选；已有 hosted 拓扑，需把沙箱、身份与默认配置真正收紧 |
| OAO | vectrix-ai/oao，Apache-2.0 | HTTP/SSE API、持久化 worker、Postgres、console | 完整平台候选，但官方明确 early beta，不可直接宣称生产级；任意自建模型端点尚待核实 |
| `@tencent-ai/agent-harness` | npm metadata、顶层LICENSE及内嵌CLI许可证均已读，MIT式授权；尚未找到完整源仓 | 独立JSON-RPC服务，包含完整CodeBuddy runtime、持久会话、审批与资源RPC | 完整可运行发布物已确认，不是薄SDK；完整源仓/可重建性与任意模型API替换仍未确认 |

## 固定研究快照

- DSH：[`22ddee3792402c3e70ec5f86a63c3e3e895a2f60`](https://github.com/deepseek-ai/deepseek-harness/tree/22ddee3792402c3e70ec5f86a63c3e3e895a2f60)，默认分支 `master`。
- TrueForge：[`cf786ee874f6d1b7901a34a2faa2c0798117a725`](https://github.com/truefoundry/trueforge/tree/cf786ee874f6d1b7901a34a2faa2c0798117a725)。
- OAO：[`a0f6f658ee1ae0923dbd7fac6166206446137806`](https://github.com/vectrix-ai/oao/tree/a0f6f658ee1ae0923dbd7fac6166206446137806)。
- Tencent npm：`0.1.6`，官方 registry 返回发布时间 `2026-09-04T10:45:09.860Z`，tarball SHA-1 `11459eb10c161cbd4dc9f5f5ffee67e3f1bef173`，SHA-512 integrity `sha512-2ibfCwXaWBl0TjsRM8ufP5/Apv5N3Ls3jmJvfxF3btMVNFu0eWvCj7c5sFGCIXkpC392BCX3F0kNSoNaq9og6Q==`。Registry 当时无 repository/homepage，maintainers 为 `muxiangqiu`、`code_faker`；这些 npm 身份不单独构成腾讯企业身份的完整证明。[官方 npm 元数据](https://registry.npmjs.org/@tencent-ai%2fagent-harness)

## DSH

官方 README 明示由 DeepSeek AI 开发，MIT，现为 developer preview，未来会有破坏性变更；可直接运行 `npx @deepseek-ai/dsh web` 或源码构建后运行。[README](https://github.com/deepseek-ai/deepseek-harness/blob/22ddee3792402c3e70ec5f86a63c3e3e895a2f60/README.md)、[LICENSE](https://github.com/deepseek-ai/deepseek-harness/blob/22ddee3792402c3e70ec5f86a63c3e3e895a2f60/LICENSE)

架构不是薄 SDK：agent loop、model adapter、tool registry、session log 均为可替换 Cordis 插件。`dsh-base` 提供工具、持久化、sandbox/approval 等，Web、一次性 headless、JSON-RPC SDK server、automation ACP 在其上组装。[Architecture](https://github.com/deepseek-ai/deepseek-harness/blob/22ddee3792402c3e70ec5f86a63c3e3e895a2f60/docs/architecture.md)

有真实 API Gateway，生成 typed Remote 方法，复用 `/api` HTTP 与 multiplexed WebSocket；业务方法需显式暴露与访问策略。不能按普通 OpenAI chat endpoint 接入，但有实现自有桌面 client 的接口基础。[API Gateway](https://github.com/deepseek-ai/deepseek-harness/blob/22ddee3792402c3e70ec5f86a63c3e3e895a2f60/docs/api-gateway.md)

持久化目录有结构化 header/event schema；approval/asked、approval/decided、tool/call、tool/result、turn 等均进入 Session event catalog。可恢复会话不代表 worker 崩溃时所有外部动作都 exactly-once；需另测执行结果不明、重启、审批等待与副作用恢复。[持久化目录](https://github.com/deepseek-ai/deepseek-harness/blob/22ddee3792402c3e70ec5f86a63c3e3e895a2f60/docs/persistence-catalog.md)

模型无关性有具体实现证据：`dsh-llm-pi-ai` 支持内置provider、自声明OpenAI-compatible gateway及self-hosted server，可配置 `api`、`baseURL`、`models`、compat。工具schema与调用关联通过adapter进入模型上下文。兼容配置本身不探测端点真实能力；developer/system role、max_tokens字段、reasoning与图片支持均有明确差异，因此必须用真实工具回合验证目标模型，不能只做“能回复你好”的测试。[pi-ai adapter](https://github.com/deepseek-ai/deepseek-harness/blob/22ddee3792402c3e70ec5f86a63c3e3e895a2f60/packages/llm/llm-pi-ai/README.md)、[Provider guide](https://github.com/deepseek-ai/deepseek-harness/blob/22ddee3792402c3e70ec5f86a63c3e3e895a2f60/docs/user/guide/providers.md)

自有云已有官方反向代理部署说明，覆盖public URL、trusted Host、TLS、WebSocket upgrade、cookie路径与Secure标记；认证涉及launch token与signed session cookie。此为独立部署路径，不等于已有组织级多租户平台。[Public deployment](https://github.com/deepseek-ai/deepseek-harness/blob/22ddee3792402c3e70ec5f86a63c3e3e895a2f60/docs/user/guide/public-deployments.md)

SDK profile含文件读写、present、ask_user_question、schedule及Office资源，能承担通用任务；但默认profile仍带coding persona，ACP profile也明确automation/coding定位。产品若主打通用办公/研究，应显式配置persona、工具、skills和资源，而非假定更名即得到完整通用产品。[SDK bundle](https://github.com/deepseek-ai/deepseek-harness/blob/22ddee3792402c3e70ec5f86a63c3e3e895a2f60/packages/bundle/sdk-app/README.md)

## TrueForge

MIT 许可；不是仅 SDK，仓库含完整 hosted server、Web UI、core runtime、session store、HTTP/OpenAPI 与客户端 SDK。[仓库](https://github.com/truefoundry/trueforge/tree/cf786ee874f6d1b7901a34a2faa2c0798117a725)、[LICENSE](https://github.com/truefoundry/trueforge/blob/cf786ee874f6d1b7901a34a2faa2c0798117a725/LICENSE)

官方区分 local 与 hosted：local 用 SQLite、默认无登录，不用于公网生产；hosted 采用 Postgres 持久化、Redis 支持副本间 stream/cancellation peering，提供 Docker Compose、Helm、Railway 源码部署说明。Helm 的 OIDC 默认关闭，并有众所周知的 placeholder API key，必须在实际部署清除这些默认项。[Quickstart](https://github.com/truefoundry/trueforge/blob/cf786ee874f6d1b7901a34a2faa2c0798117a725/docs/quickstart.mdx)

自定义模型可配置 `custom` provider、base URL、API key、model IDs；文档明确例举 vLLM、Ollama、公司 gateway。但 OpenAI-compatible 只能证明接入面可配置，不证明每种模型都能可靠完成完整工具循环。[Models](https://github.com/truefoundry/trueforge/blob/cf786ee874f6d1b7901a34a2faa2c0798117a725/docs/models.mdx)

session/turn/event/delta 是完整会话模型；支持 MCP tool approval、澄清问题与 auth-required 后续 turn，事件有 sequence 用于断线恢复，历史读取合并 live deltas。SDK 是调用该服务的客户端，不能误当整个项目只有 SDK。[API concepts](https://github.com/truefoundry/trueforge/blob/cf786ee874f6d1b7901a34a2faa2c0798117a725/docs/api/overview.mdx)

官方Sandbox文档称当前只支持Daytona，skills/code mode需要sandbox；agent loop与模型/MCP凭证在harness，sandbox只负责执行文件/代码操作。源码同时存在 `TFYSandboxProvider`，需要TrueFoundry serverUrl、NATS bridge、tenant，且 `envSupported=false`。这是文档与源码覆盖面的差异：不能把该类的存在当成通用开源Docker沙箱控制面已交付，也不能笼统说源码里只有Daytona。若要求所有执行基础设施都在自有云，应核实实际自托管sandbox部署路径与许可证/依赖。[Sandbox文档](https://github.com/truefoundry/trueforge/blob/cf786ee874f6d1b7901a34a2faa2c0798117a725/docs/sandbox.mdx)、[TFY provider源码](https://github.com/truefoundry/trueforge/blob/cf786ee874f6d1b7901a34a2faa2c0798117a725/packages/trueforge-core/src/core/sandbox/provider/TFYSandboxProvider.ts)

自定义模型实际调用 `createOpenAICompatible`、`baseURL`，并把function tools转换为Vercel AI SDK ToolSet参与streamText。代码证明有完整工具协议路径，未证明任意端点的工具调用质量；其harness只消费function tool calls，不能默认兼容所有非标准tool类型。[LLM adapter源码](https://github.com/truefoundry/trueforge/blob/cf786ee874f6d1b7901a34a2faa2c0798117a725/packages/trueforge-core/src/core/llm/VercelAILLM.ts)

## OAO

Apache-2.0，完整 managed-agent 服务，包含 Hono HTTP/SSE、Flue worker、Postgres 状态/事件持久化、console。官方 README 的 durable execution 描述包含租约、fencing tokens、幂等控制，但同页明确 early beta，适合 evaluation/local development，无 production-readiness、LTS 或 SLA 保证。[README](https://github.com/vectrix-ai/oao/blob/a0f6f658ee1ae0923dbd7fac6166206446137806/README.md)、[LICENSE](https://github.com/vectrix-ai/oao/blob/a0f6f658ee1ae0923dbd7fac6166206446137806/LICENSE)

当前有 OpenRouter、OpenAI、Anthropic、xAI 的 provider 与 catalog，不能把宣传的 any model 直接解释成任意 vLLM/Ollama/自有 OpenAI-compatible URL 已可用。模型会按能力与定价经过 runtimeSupported 检查，部分条目无法运行；这与纯透传 base URL 不同。[Adding models](https://github.com/vectrix-ai/oao/blob/a0f6f658ee1ae0923dbd7fac6166206446137806/docs/models/adding-models.mdx)

API/worker 可部署在自己基础设施，但启用沙箱的 agent 当前依赖 Daytona；S3-compatible 存储用于文件与备份。其自带 SDK 目前是 private workspace package，外部客户端应按文档用 HTTP/SSE，不能承诺已有稳定公开 npm SDK。[MVP limitations](https://github.com/vectrix-ai/oao/blob/a0f6f658ee1ae0923dbd7fac6166206446137806/docs/reference/mvp-limitations.mdx)

限制还有：每个 thread 同时一个 run；并行委派共享 workspace，未提供文件锁/合并；没有 project budget、quota 或 spend alert；MCP 是 HTTPS/static credential，stdio、OAuth refresh、每用户 delegated credential 尚未实现。若作为统一客户端后端，仍需要平台层能力。[MVP limitations](https://github.com/vectrix-ai/oao/blob/a0f6f658ee1ae0923dbd7fac6166206446137806/docs/reference/mvp-limitations.mdx)

HITL有实际实现：tool broker记录pending/approved/denied/expired、设置approval TTL、持久化approval.requested/approval.resolved事件，并把run置为waiting_for_approval。它不只是文档声称有“人工介入”。[Tool broker源码](https://github.com/vectrix-ai/oao/blob/a0f6f658ee1ae0923dbd7fac6166206446137806/packages/tool-broker/src/index.ts)

## Tencent `@tencent-ai/agent-harness`

只读下载固定版tarball并核对：顶层LICENSE为MIT、Copyright Tencent CodeBuddy；内嵌 `cli/package.json` 为CodeBuddy Code 2.143.1，指向 `cli/LICENSE.txt`，该文件同样为MIT式授权。不能断言“内嵌CLI必然闭源许可”，也不能仅凭许可推断完整原始源码树已经公开。[固定发布物](https://registry.npmjs.org/@tencent-ai/agent-harness/-/agent-harness-0.1.6.tgz)、[顶层LICENSE](https://unpkg.com/@tencent-ai/agent-harness@0.1.6/LICENSE)、[内嵌CLI LICENSE](https://unpkg.com/@tencent-ai/agent-harness@0.1.6/cli/LICENSE.txt)、[内嵌CLI manifest](https://unpkg.com/@tencent-ai/agent-harness@0.1.6/cli/package.json)

它确实是可运行服务：包自带CLI完整运行时，主进程作为supervisor，Resource child管理durable sessions/skills/plugins/config，executing session使用独立Agent CLI process。公开API含ACP v2 new/list/delete/fork/resume/close/prompt/cancel、checkpoint、session/request_permission和资源RPC；持久会话由Agent CLI持有。因此“只有SDK、不含完整loop”不准确，但也不是一套自带团队云管理UI的产品。[固定版README](https://unpkg.com/@tencent-ai/agent-harness@0.1.6/README.md)

须分清公网与私有transport：README开头“UDS/Named Pipe、不监听TCP”描述父进程访问private Resource child的边界；公开protocol第9节同时明确stdio、Streamable HTTP、WebSocket、Unix socket、Windows Named Pipe，network transport有detach/grace与重连语义。不能把“UDS”概括成整个服务不支持网络接入；自有云仍需外部TLS与授权网关，Windows Node主包还不能承诺全部stdio MCP containment能力。[协议文档，固定版](https://unpkg.com/@tencent-ai/agent-harness@0.1.6/docs/protocol.md)

模型控制的边界比“任意OpenAI-compatible”更窄：Product管理endpoint、认证路由与resolved model catalog；session只选择已协商的model，workspace `.codebuddy/models.json`在harness child中只是selector，不能注入URL/API key/headers/协议或新模型；需要不同endpoint的workspace必须用不同harness process。本次未找到明确、自包含的任意OpenAI-compatible模型服务配置与完整tool round-trip示例，因此模型无关性暂标待核实，不把Product endpoint误当模型baseURL。[Product文档](https://unpkg.com/@tencent-ai/agent-harness@0.1.6/docs/product.md)

原始源码与维护者还需补证：npm无repository/homepage，README构建链引用同checkout内的 `@genie/agent-cli`等workspace。官方CodeBuddy公开CNB仓库当前可见根目录是docs/samples/README/CHANGELOG/许可证等，未定位能从头重建agent-harness与内嵌loop的完整源仓。这是“本次未定位”，不是证明不存在。公开版本为0.1.6，使用ACP v2及Effect 4 RC依赖，建议固定发布物和能力握手后评估，不能从丰富的协议文档直接推导生产成熟度。[npm元数据](https://registry.npmjs.org/@tencent-ai%2fagent-harness)、[固定manifest](https://unpkg.com/@tencent-ai/agent-harness@0.1.6/package.json)、[CodeBuddy官方CNB仓库](https://cnb.cool/codebuddy/codebuddy-code)

## OpenWorkBuddy：同名项目必须用owner区分

`CatCatUncle/openworkbuddy`固定快照为[`90e3585db5ac67d232d3242feabb6d870e1c7e13`](https://github.com/CatCatUncle/openworkbuddy/tree/90e3585db5ac67d232d3242feabb6d870e1c7e13)。README明确独立社区项目、与腾讯及腾讯WorkBuddy无关；整体许可证为PolyForm Noncommercial 1.0.0，公司真实业务使用（包括内部）和个人商用需另获商业许可，部分部署脚本、评测集、文档示例另给MIT例外。不能把局部MIT扩展到整个runtime。按“开放源码并可自由商业部署”的产品基线不进入默认推荐；若允许购买商业许可可单列。[README](https://github.com/CatCatUncle/openworkbuddy/blob/90e3585db5ac67d232d3242feabb6d870e1c7e13/README.en.md)、[LICENSE](https://github.com/CatCatUncle/openworkbuddy/blob/90e3585db5ac67d232d3242feabb6d870e1c7e13/LICENSE)

另有`chenin0931/OpenWorkbuddy`等不同owner同名项目；搜索名称时不能混用其MIT/macOS Electron描述与CatCatUncle项目的功能、许可或部署情况。本次仅将其作为名称歧义提醒，未做实现级选型。[同名仓库](https://github.com/chenin0931/OpenWorkbuddy)

## 入围建议与下一步验收（工程判断）

1. **DSH：第一批完整runtime适配对象。** 官方来源、MIT、custom provider、会话/API/审批闭环可定位，但需承担preview版本兼容。
2. **TrueForge：第一批完整平台参照与适配对象。** 模型自定义与对话/审批/恢复API完善；自有云sandbox路径、认证默认和存储/升级需明确实装。
3. **OAO：架构参照与候补。** Postgres持久执行值得借鉴；以官方early beta和当前provider清单为边界，不能直接充当任意自托管模型的生产底座。
4. **Tencent：等源码/模型准入补齐后决定。** 已确认可运行runtime与MIT式发布许可；“完整可构建源码”和“任意模型工具循环”尚不能勾选。

模型工具兼容验收至少覆盖：流式function参数组装、工具schema限制、多工具并发、工具结果续聊、拒绝/审批后继续、取消、重试与outcome-unknown、长上下文压缩、图片/文件可选能力、usage/cost缺失处理。用相同任务、固定工具与资源比较不同harness；不能把“同一OpenAI-compatible接口”当成行为一致证明。

本报告没有证明任何候选已经通过生产验收；许可标识、代码存在、协议文档和稳定性实测是四种不同证据。实现前需要按最终版本完成构建复现、故障恢复、隔离和真实模型协议测试。
