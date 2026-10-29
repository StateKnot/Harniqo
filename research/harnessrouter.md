# HarnessRouter：直接竞品与可复用 Gateway 候选

调研日期：2026-10-27。审阅了官方文档、协议、源码与 Releases；没有启动服务或调用付费模型，以下功能不是实测验收结论。

**结论：应放进第一梯队验证。** 它已经提供多 Harness 的统一 API、Web Console、会话、事件流、文件、取消与结构化失败，覆盖用户计划中的相当一部分。建议优先验证“复用它的适配/网关 + 自己的桌面客户端、SaaS 控制面与评测”，而不是默认从零重写所有 Harness Adapter。[官方仓库](https://github.com/HarnessRouter/harnessrouter)

## 核实结果

| 事项 | 读取时的证据与判断 |
|---|---|
| 实现是否真实 | 仓库包含 Python Gateway/Runner、Web UI、存储适配与测试；不是只有协议文档。Gateway 源码存在 `/v1/responses`、取消、上传/下载、Session、Harness CRUD 和对象归属检查。尚未做安全审计或运行验证。[Gateway 源码](https://raw.githubusercontent.com/HarnessRouter/harnessrouter/main/gateway/app.py) |
| 许可 | CE 的 [LICENSE](https://raw.githubusercontent.com/HarnessRouter/harnessrouter/main/LICENSE) 是 Apache-2.0。上游 CLI 各自许可，Starter Kits 单独条款，不能一并当成 Apache-2.0；商业托管仍需逐一检查上游使用方式。[README License](https://github.com/HarnessRouter/harnessrouter#license) |
| 版本成熟度 | 官方当前最新标记 `v0.37.2`，发布时间 2026-10-26；仍是 0.x 且适配持续变化。Release 与支持矩阵记录了真实失败案例，但项目自报 pass 不等于本产品组合已验收。[Releases](https://github.com/HarnessRouter/harnessrouter/releases)、[支持矩阵](https://raw.githubusercontent.com/HarnessRouter/harnessrouter/main/docs/support-matrix.md) |
| Harness 覆盖 | 当前 README 声称30种 base，包含 Codex、Claude Code、Hermes、Pi、DSH、OpenCode、Gemini CLI、Goose、OpenHands 等。具体可用的 Harness×Model×Provider×操作以运行实例与矩阵为准；“支持某Harness”不等于每模型/能力都可用。[README](https://raw.githubusercontent.com/HarnessRouter/harnessrouter/main/README.md) |
| CE 部署 | 一个容器运行 Console/Gateway/Runner，SQLite、文件和 Secret 位于单 volume；Gateway/Runner 在容器内 loopback。每 Session 独立工作区与 OS user，**不是每 Session 独立容器/VM**。[Dockerfile](https://raw.githubusercontent.com/HarnessRouter/harnessrouter/main/Dockerfile)、[Compose](https://raw.githubusercontent.com/HarnessRouter/harnessrouter/main/docker-compose.yml) |
| 多租户与弹性 | 官方 CE 文档明确只有一个登录者，所有 Session 属于此人；没有云版多人/组织 Workspace、Billing Ledger、跨副本实时事件；CE 不能像托管版那样按需扩展 Sandbox。不能把“同代码库有org字段”当成CE多租户交付承诺。[CE差异说明](https://raw.githubusercontent.com/HarnessRouter/harnessrouter/main/docs/self-hosting-guide.md) |
| 费用预算 | CE 文档明确没有按费用中断能力，`max_cost_usd` 会被拒绝；以 `max_step`、`timeout_seconds` 限制，费用上限依靠 Provider。若产品要硬预算与统一账单，需要补全控制面和记账对账。[CE差异说明](https://raw.githubusercontent.com/HarnessRouter/harnessrouter/main/docs/self-hosting-guide.md) |
| 安全/凭据 | CE 按 owner trust 场景允许直传 Provider credential；`HR_SANDBOX_TRUST=owner` 在源码有明确分支。对SaaS不可信租户，需独立执行边界、凭据代理、网络策略和资源限制，不能只加账号表。[凭据分支](https://raw.githubusercontent.com/HarnessRouter/harnessrouter/main/gateway/app.py)、[Security Policy](https://raw.githubusercontent.com/HarnessRouter/harnessrouter/main/SECURITY.md) |
| 人工审批 | 有工具权限相关配置，但 Runner 的部分无交互 CLI 明确使用 YOLO/full 模式；不能由“支持permissions”推导出所有 Harness 都可逐工具暂停、由客户端批准后继续。[Runner 源码](https://raw.githubusercontent.com/HarnessRouter/harnessrouter/main/runner/server.py) |
| 实验与评测 | 有官方成本/延时对比与兼容性测试矩阵；本次未找到等价 Harbor 的完整 Dataset/Trial/重复实验/隐藏Verifier/统计契约。建议把它当运行 Gateway，与专门评测 Runner 组合，不以营销对比替代实验系统。[官方Benchmark](https://harnessrouter.ai/benchmarks)、[支持矩阵](https://raw.githubusercontent.com/HarnessRouter/harnessrouter/main/docs/support-matrix.md) |

## UHP 到底是什么

UHP 是公开、Apache-2.0、HTTP 的 Harness-to-Application 契约，提供 OpenAI Responses-compatible 任务表面，并补充 Harness 选择、Session、文件、取消等执行语义。当前协议 README 标注版本 `2026-10-19`、状态 **Draft standard**，有 OpenAPI/JSON Schema 和一致性测试。[UHP](https://raw.githubusercontent.com/HarnessRouter/harnessrouter/main/protocol/README.md)

治理文件明确由 HarnessRouter 仓库维护者主导，变更要求规范、参考实现与 conformance suite 同步更新。本次没有查到独立标准组织治理或广泛厂商原生采纳的证据，因此宜称“开放的、新兴协议候选”，不称“行业通用标准”。[治理](https://raw.githubusercontent.com/HarnessRouter/harnessrouter/main/protocol/GOVERNANCE.md)

也不能说它仅有一个实现：官方实现列表包含 SuperQode，而 [SuperQode 自己的 README](https://github.com/SuperagenticAI/superqode#serve-a-harness-over-uhp) 独立确认 `superqode serve uhp`，宣称通过 Core class，支持 `2026-09-12` 与 `2026-08-11`。这与 UHP 当前版本存在差异，客户端必须协商版本与 capability，不能只判断“都叫 UHP”。[实现列表](https://raw.githubusercontent.com/HarnessRouter/harnessrouter/main/protocol/IMPLEMENTATIONS.md)

协议要求对象归属隔离、认证、失败与资源语义，但规范要求并不自动证明某部署符合全部条件。Conformance 是验收起点；安全章节自己说明单凭一个 Credential 不能完全验证跨主体权限。[UHP安全](https://raw.githubusercontent.com/HarnessRouter/harnessrouter/main/protocol/versions/2026-10-19/security.md)、[一致性测试](https://raw.githubusercontent.com/HarnessRouter/harnessrouter/main/protocol/conformance/README.md)

## 对用户产品的落地判断

- **私有部署/个人与可信团队：** 很适合作为首个集成对象和运行网关候选，复用速度可能明显优于自研全部CLI适配。正式决定前验证目标3种Harness的续聊、取消、事件重连、文件与升级回退。
- **SaaS：** CE不能直接作为完整多租户后端交付。可以研究替换存储与执行层的接口，保留其适配器；需要把租户身份/配额、持久队列/事件、Sandbox池、凭据代理、费用和审计补成可验收系统。
- **协议策略：** 内部领域模型保持独立；将UHP作为Gateway Adapter，另保留原生SDK/ACP接入，避免客户端绑定某个Draft版本及单厂商扩展。
- **评测策略：** 由Harbor/AgentCompass或自定义Trial Worker调用Gateway，确保固定实际模型、原生Harness版本和环境；验证网关是否修改提示词、工具或默认配置，避免把网关行为混当Harness效果。

以上是架构建议。不要只因有Apache许可、Docker quickstart或通过协议测试，就跳过不可信代码隔离与多人权限的真实验收。
