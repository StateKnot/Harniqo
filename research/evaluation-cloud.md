# 多 Harness 客户端预研：云端执行与批量评测

调研日期：2026-10-27（Asia/Shanghai）。本文是一手文档与源码的可行性预研，没有创建云资源、运行付费模型、执行 Benchmark 或做性能实测。引用的是读取时的在线文档；落地时需要把依赖、镜像和协议版本锁定到具体发行版与 digest。文中标为“建议”的内容是据此形成的架构判断，不是上游产品对你的系统作出的保证。

## 结论

最值得复用的是 **Harbor 的评测任务格式、Agent/Environment 适配、Trial/Job 调度和 ATIF 轨迹**，上层自行做统一客户端、交互会话、权限、成本、版本和实例管理。不要先从零写一套 Benchmark 执行框架。Harbor 已能把 Agent、模型、任务和 Sandbox 组成并行 Job，提供本地与云端环境适配；当前文档也已包含 Hosted Jobs、Web UI 和 Secret 管理，因此它同时是基础设施候选与产品竞品。[Harbor 核心概念](https://docs.harborframework.com/core-concepts)、[运行 Job](https://docs.harborframework.com/jobs/run-a-job)

但 Harbor 不能直接等同于你的统一交互后端：其 `--stream` 文档当前仅明确支持 Claude Code/Codex 以及部分环境，且要求浏览器与 Viewer 在同一机器运行。需要单独验证交互控制、远程鉴权、审批往返和断线恢复。[Harbor Stream](https://docs.harborframework.com/jobs/stream)

首个正式可用版本建议采用“云端 Linux 执行池 + 三平台客户端 + 独立会话与评测控制面”。客户端能在三种系统运行，不等于 Agent 已能在三种远端系统执行；例如 E2B 主文档明确把核心 Sandbox 描述为 Linux VM。[E2B 文档](https://docs.e2b.dev/)

## 1. 已有能力与选型

| 项目 | 已核实能力 | 适合复用的部分 | 边界与建议 |
|---|---|---|---|
| Harbor | Agent/模型/任务/Sandbox 的 Trial 与并行 Job；内置多种 CLI Agent 和云 Sandbox；自定义接口；ACP Agent 接入 | 批量评测执行、任务与结果交换、适配器实现参考 | 不是默认完整的多租户交互控制面；逐 Agent 检查 capability 与版本。先集成，不深度 Fork |
| Harness-Bench | 对模型与原生 Harness 组合做共享任务/预算评测，保留轨迹与产物，用隐藏任务 oracle 评分 | 实验方法、真实工作任务和评分思路；与本产品高度相邻 | 官网、仓库与论文任务规模存在版本差异；本次根目录未查到 LICENSE，复用代码/数据前核实授权 |
| Terminal-Bench | 官方当前已有 4.0，官网列出 2026-08-28 发布；版本之间任务与环境可变化 | 通用终端任务的外部参照集 | 固定版本与任务 revision；不要将旧版本跑分与新版本混排，也不能把小子集当完整榜单 |
| SWE-bench | 在容器中应用生成补丁并运行项目测试，区分可评分结果和环境错误 | 真实仓库修复任务与 Patch verifier | 需保留原始 tests、评测日志与结果；不是统一对话协议 |
| Inspect | 通用评测、Sandbox 扩展、错误恢复与重跑；支持本地 Docker 及云 Sandbox 扩展 | 后续非编码任务、安全/多轮 Agent 评估 | 第一阶段同时维护两种主 Runner 会增加工作量，建议先 Harbor，场景需要时再添加 Inspect 插件 |
| E2B | Linux VM Sandbox、生命周期与持久化、命令/文件/PTY/SSH、访问认证 | 托管执行池候选 | 核实运行时长、并发、区域、暂停成本与网络限制；不要把暂停中的 VM 当永久唯一存档 |
| Daytona | Sandbox SDK/API、OCI/Docker 兼容、网络控制、Secret 代理；不同 Sandbox class 有容器和 VM 区别 | 托管/BYOC 执行池候选 | 旧仓库 2026-10-03 归档，README 明确核心开发自 2026 年 6 月转私有，旧版不再更新/修复/发行；不能据旧开源版承诺持续维护 |
| Modal Sandboxes | 独立执行、镜像、文件、快照、资源与网络控制 | 大并发/批处理/GPU 需求时的托管执行池候选 | 不把平台 SDK 泄漏进业务领域接口；域名允许列表页面标有 Beta，DNS 限制有明确边界 |
| Langfuse / OTel | 实验、数据集、评分；可接入基础观测与轨迹 | 可选分析后端与通用遥测输出 | 不能替代执行状态机、任务隔离或可复现的评测定义 |

表格来源：[Harbor Agents](https://docs.harborframework.com/agents/pre-integrated-agents)、[ACP](https://docs.harborframework.com/agents/acp)、[Sandboxes](https://docs.harborframework.com/sandboxes/pre-integrated-sandboxes)、[Terminal-Bench 版本](https://www.tbench.ai/benchmarks)、[4.0 公告](https://www.tbench.ai/news/terminal-bench-4-0)、[SWE-bench Evaluation](https://www.swebench.com/SWE-bench/guides/evaluation/)、[Inspect Sandboxing](https://inspect.aisi.org.uk/sandboxing.html)、[Inspect 错误处理](https://inspect.aisi.org.uk/handling-errors.html)、[E2B Sandbox](https://docs.e2b.dev/sandbox)、[E2B 持久化](https://docs.e2b.dev/sandbox/persistence)、[Daytona 文档](https://www.daytona.io/docs/en/)、[Daytona 运行隔离](https://www.daytona.io/docs/en/isolation/)、[Daytona 仓库](https://github.com/daytonaio/daytona)、[Modal Sandboxes](https://modal.com/docs/guide/sandboxes)、[Modal 网络](https://modal.com/docs/guide/sandbox-networking)、[Langfuse 概念](https://langfuse.com/docs/evaluation/core-concepts)。

Harbor 仓库许可证是 Apache-2.0。这个结论仅针对该框架；其中调用的商业 Harness、模型 API、托管服务和任务数据各有自己的许可与服务条款，不能由框架许可推导出转售、代理登录或共享订阅的权利。[Harbor LICENSE](https://raw.githubusercontent.com/harbor-framework/harbor/main/LICENSE)

补充一手来源：[Harness-Bench 官网](https://www.harness-bench.ai/)、[官方仓库](https://github.com/Qihoo360/harness-bench)、[论文 v2](https://arxiv.org/abs/2605.27922v2)、[评分契约](https://github.com/Qihoo360/harness-bench/blob/main/docs/EVALUATION_AND_SCORING.md)、[Daytona README 顶部维护声明](https://raw.githubusercontent.com/daytonaio/daytona/main/README.md)。截至本次读取，Harness-Bench 官网/仓库是 300 任务，而论文 v2 摘要是 106 任务；不将两组数字混用。官网称无 LLM Judge，但仓库评分契约允许受限 semantic judging，且不能决定结构化核心结果，实施时以锁定版本的评分脚本为准。

## 2. 产品里的“批量测试”应分成两种明确模式

**受控实验：比较 Harness 本身。** 固定模型完整 ID 与配置、任务、仓库 commit、基础环境、CPU/内存/磁盘、网络、可见资料、工具预算与审批策略，再改变 Harness 及其必要适配。只在共同支持的配置区间比较；某个 Harness 无法使用指定模型时标为“不兼容”，不能偷偷换模型后仍声称是 Harness 对比。推理预算、模型调用格式、系统提示词和内置工具即便名义相同也可能不等价，应把实际差异写入实验 Manifest。

**产品组合实验：比较用户实际会购买的方案。** 比较 Claude Code 默认推荐组合、Codex 默认推荐组合等，允许各自最佳设置，但结果标题应是“Agent+模型+配置组合对比”。用户最关心它的成功率、交付时间、总成本和人工干预次数；不把结果归因给单独 Harness。

以上是建议的实验设计。它基于 Harbor 已将 Agent、Model、Task、Trial 和 Job 区分建模，且模型 slug 与 Credential/Endpoint 的解析可由适配器改变；统一显示名并不保证底层请求完全相同。[Harbor 核心概念](https://docs.harborframework.com/core-concepts)、[Agent auth resolution](https://docs.harborframework.com/agents/agent-auth-resolution)

一个正式可复现的 `ExperimentSpec` 至少包含：

- 任务集 ID、版本、内容哈希、筛选子集及抽样方法。
- 仓库 URL、commit SHA、子模块/LFS 状态和初始文件内容哈希。
- Harness 名称、发行版/commit、安装包哈希、Adapter 版本与协议版本。
- 模型 provider、完整模型 ID、API 格式、实际 Endpoint、采样和推理参数；不记录密钥正文。
- Base image digest、平台架构、依赖锁、工具链版本、资源、时钟/网络策略与执行区域。
- Skills、MCP Server、System Prompt、工具定义及其版本；注明原生内置工具的不可控差异。
- Timeout、token/cost/tool-call 预算、审批模式、重试规则、重复次数、随机种子（可用时）。
- Verifier 镜像 digest、测试脚本哈希、Judge 模型与 rubric 版本。

**建议：可复现应解释为“配置、环境、输入和产物可追溯并能重新执行”，不要承诺模型输出逐字一致。** API 模型更新、不可见后端实现与随机生成都会限制确定性。每一次重跑应产生独立 Trial，并保留父子关系。

## 3. 指标应展示多个维度，避免一张总分掩盖失败

以下为建议的最小指标契约：

| 维度 | 建议指标 | 计算与展示要求 |
|---|---|---|
| 可交付结果 | resolved、隐藏测试通过率、回归测试、构建/类型/静态检查 | 尽量由可执行 Verifier 给出；保留原始报告 |
| Patch 质量 | 修改文件/行数、约束违反、人工审阅接受率 | 不以代码越少越好作为通用质量指标 |
| 效率 | 端到端时长、排队/启动/Agent/Verifier 分段、p50/p95 | 明确冷启动与重试时间是否计入 |
| 成本 | 模型调用、缓存输入/输出、Sandbox compute、存储/网络、Judge | 区分 provider 实测账单、按价目表估算与缺失；保留币种和价目表日期 |
| 自动化程度 | 审批次数、人工输入轮数、接管耗时 | 全自动和有人辅助的样本分组比较 |
| 可靠性 | Agent task failure、预算耗尽、认证失败、限流、Sandbox/Adapter/Verifier 错误 | Infra error 不悄悄计成模型失败，也不能从界面上消失 |
| 稳定性 | 每任务多次试验均值、波动、置信区间 | 有效样本数、未完成数、排除原因同时显示；不要只挑一次最佳结果 |

Harbor 当前已有 `failure_cause` 与 `reward_valid` 分类：Agent 超时/预算耗尽可仍属于有效评分，而 Sandbox/Provider/Verifier 错误通常会影响评分有效性。应保留并映射这些上游语义，另加产品自己的错误域。[Harbor Error classification](https://docs.harborframework.com/jobs/error-classification)

建议并列显示两个口径：`任务质量 = 有效评分中的成功率` 与 `系统交付率 = 所有计划试验中的成功完成率`，旁边明确分母。前者帮助研究，后者反映真实服务体验。若部分试验重试，显示首试与最终结果、总尝试数和累计成本；不能用悄悄无限重试提高榜单。

例如 `30 个任务 × 3 个 Harness × 2 个模型 × 3 次重复 = 540 个 Trial`，尚未包括 infra retry。提交前显示预计上下限与硬预算，执行中按组织、项目、Provider 和 Experiment 分别限制并发与累计花费。这是建议，不是对模型调用单价的报价。

## 4. 首版即可投入使用的云端架构建议

建议从模块化单体控制面起步，执行面独立进程/机器部署，而不是从第一天拆成大量微服务。

```text
Mac / Windows / Linux 客户端
    │ HTTPS：命令、配置、查询；SSE/WS：带游标事件
    ▼
控制面 API + 身份/项目权限 + 会话/实验调度
    ├─ PostgreSQL：元数据、状态、审批、命令、事件索引、持久队列
    ├─ 对象存储：原始轨迹、代码 diff、文件、测试报告与日志
    ├─ Secret/KMS：项目/租户隔离的凭据与短期令牌
    └─ 调度器 / Worker Registry / 对账与清理任务
             │ Worker 主动建立带身份的连接
             ▼
执行面 Worker / Sandbox Provider Adapter
    ├─ 交互 Session Runner + Harness Adapter
    ├─ Batch Runner（优先集成 Harbor）
    └─ 每 Session / Trial 独立 Sandbox 与文件系统
             ▼
Harness（Codex / Claude Code / OpenCode 等）→ 允许的模型/工具服务
```

队列可以先放 PostgreSQL，短事务领取任务并更新租约；官方明确 `SKIP LOCKED` 可供多个消费者消费队列表。需要配套索引、租约续期、超时回收、fencing token、重试退避与积压监控，不能只写一条 SELECT 就宣称有可靠队列。数据库事务不应跨整个 Agent 执行周期持有。[PostgreSQL SELECT](https://www.postgresql.org/docs/current/sql-select.htm)

建议起步组件是：两个无状态 API 实例、受控的 Worker 池、托管 PostgreSQL、对象存储、云 KMS/Secret 管理、反向代理/TLS 与现有 OIDC。单组织私有部署可减至单 API，但备份、恢复、版本管理和审计仍要有明确方案。需要 HA 还是允许短时中断，应由目标客户的 SLO 决定，不能从“生产级”推导出必须购买多区域集群。

**Kubernetes 与 Temporal 都不是必要前提。** 当前规模可用成熟部署工具与数据库队列承担；当执行池跨多节点/租户、调度约束和长流程恢复明显复杂后再引入。领域契约保持不变：`Session`、`Run`、`Trial`、`Approval`、`Artifact`、`CredentialRef`、`ExecutionTarget`。生产能力来自清楚的失败恢复契约和验证，不来自组件数量。

## 5. 断线、重试与副作用的生产语义

以下是建议的系统契约：

1. **客户端断线只断开观察通道。** 云端 Session 继续运行；客户端按 `session_id + last_event_seq` 补取持久事件。每条事件带唯一 ID、顺序、Harness 原始类型和版本。增量文本可批量持久化，但必须明确允许丢失的窗口；完整结果与状态转移必须落盘。
2. **执行租约不等于可以安全重跑。** Worker 超时后先向 Sandbox/Runner 对账，判断进程仍活跃、已完成、已取消或状态不明。接管用新的 fencing token 拒绝旧 Worker 写状态；不能在不确定时启动第二个 Agent 同时修改同一 workspace。
3. **命令接受幂等，外部副作用不承诺 exactly-once。** 发消息、开始 Run、审批回复可用 request ID/command ID 去重；Git push、外部 API 写入和支付等不一定有上游幂等能力。状态不明时记为 `needs_reconciliation` 并先核对结果。
4. **取消是状态机。** `cancel_requested → stopping → cancelled`；先发 Harness 原生 cancel，再结束进程组，最后由 Sandbox 控制 API 强制停止。上传已存在产物，保留实际费用。Worker 心跳丢失不能直接显示“取消成功”。
5. **审批需要绑定具体动作。** 保存工具、参数摘要、会话、调用 ID、发起者、权限、有效期与答复。只由有权限的主体答复；超时默认不执行。Batch 的无人值守审批策略必须成为实验的一部分。
6. **重试分层。** 只读状态查询可安全退避重试；创建 Sandbox 用幂等键或查询标签对账；可确认无执行的启动失败可新建 Trial；已经运行的任意 shell 命令不能因 HTTP 超时而盲目重发。

Inspect 官方 Sandbox 扩展文档也明确指出：非幂等命令的超时重试可能使第一次尝试的副作用影响后续结果，可用 `timeout_retry=False` 禁用；Inspect 的 eval-retry 则保留原始日志并新建重试日志。可参考它对“评测恢复”和“命令重放”的区分。[Inspect Sandbox Extension](https://inspect.aisi.org.uk/extensions-sandboxes.html)、[Inspect 错误恢复](https://inspect.aisi.org.uk/handling-errors.html)

## 6. 隔离、Secret 与评测可信度

**建议：默认按 Session 或 Trial 分配执行环境。** 同一容器里只创建几个工作目录，不能形成租户隔离。Agent 不应访问宿主 Docker socket、控制面数据库、云管理凭据或其他项目的 Secret；控制面与不可信 Agent 执行面使用不同身份与网络。Docker 官方文档明确提示，能够控制 daemon 的主体可获得非常强的宿主访问能力。[Docker Engine Security](https://docs.docker.com/engine/security/)

单组织可信内部使用可以从独立 VM 中的容器池起步；对互不信任的客户代码或公开上传任务，应采用受管理的强化 Sandbox 或自建 VM/microVM 边界。gVisor 是可与 Docker/Kubernetes 集成的用户态内核隔离层，并非完整 VM；Firecracker 是 microVM VMM，也不是现成的完整 Sandbox SaaS。选择后都要实测工具兼容、文件/网络行为与资源开销。[gVisor](https://gvisor.dev/docs/)、[Firecracker](https://github.com/firecracker-microvm/firecracker)

建议对每个环境设置 CPU/RAM/PID/磁盘/时长硬限额；禁用不必要的宿主挂载与特权；网络默认只允许项目要求的模型 API、Git 和依赖仓库，并为纯评分阶段禁网。云厂商默认策略并不相同，例如 Modal 文档说明默认可出网，CIDR/域名允许列表可配置，且 DNS 有明确例外；不能把“Sandbox”自动理解成“无网络”。[Modal 网络与安全](https://modal.com/docs/guide/sandbox-networking)

Secret 建议采用项目级引用、静态加密、短期作用域、运行时按需取得、日志脱敏和轮换；能用出口代理替换凭据时，优先让 Sandbox 只持有代理用的占位令牌。Daytona 官方文档当前已提供 HTTPS 出口占位符替换机制与 Host allowlist，但明确没有 allowlist 的 Secret 可被替换到任意 host；必须主动配置允许目标，且验证 Harness 的请求方式兼容。[Daytona Secrets](https://www.daytona.io/docs/en/secrets/)

E2B Controller 访问必须使用认证机制；官方文档说明历史旧模板需要重建以兼容 secure access，SDK 版本也会改变 `secure` 参数行为。实际接入要锁 SDK、模板与 Controller 版本，不能只验证 SDK 构造函数调用成功。[E2B Secured Access](https://docs.e2b.dev/sandbox/secured-access)

评测用 Verifier 建议与 Agent 环境隔离，只导出允许的 Patch/Artifact 后在干净环境复验，测试和评分规则不提前暴露给 Agent，避免 Agent 修改测试/奖励文件。Harbor v0.25 起新建任务默认使用 separate verification，已有任务仍保留原行为，所以必须显式记录并检查任务配置。[Harbor Separate Verifier](https://docs.harborframework.com/tasks/separate-verifier)

## 7. 轨迹、评分与成本的存储边界

建议同时保留三份有明确用途的数据：

- Harness 原始事件与原始产物，作为排障证据，不被统一 schema 丢弃。
- 产品规范化事件，用于统一 UI、会话状态与权限检查；允许保留 provider-specific extension。
- 评测交换格式，优先兼容 ATIF，并在能力表里区分“可产出轨迹”和“可由轨迹恢复”。

Harbor 当前 ATIF 文档明确：轨迹产出与加载是两个不同 capability，能写 `trajectory.json` 不代表能把它无损续接到另一个 Agent。当前页面列出的格式是 `ATIF-v1.7`；落地应锁 schema 版本。[Harbor ATIF](https://docs.harborframework.com/agents/atif)

生产事件先进入自己的数据库/对象存储，再异步导出 OTel/Langfuse；遥测系统故障不应阻止 Agent 结束和保存代码。代码测试适合确定性 evaluator；LLM Judge 用于说明质量、计划清晰度、用户体验等难以程序化的维度，必须保存 rubric、Judge 模型、输入、原始输出与版本，并抽样由人审阅。Judge 成本独立计入，不能反向改写客观测试结果。Langfuse 支持数据集、实验与多种评分方法，可作为可选的观测/分析出口。[Langfuse Evaluation](https://langfuse.com/docs/evaluation/core-concepts)

OTel 的旧 GenAI 页面当前已迁移到独立官方仓库，集成时跟随该仓库并锁 convention 版本，不硬编码旧文档中的字段为永久稳定契约。[OTel 迁移说明](https://opentelemetry.io/docs/specs/semconv/gen-ai/)、[官方 GenAI conventions 仓库](https://github.com/open-telemetry/semantic-conventions-genai)

## 8. 账号与商业化边界

建议首版使用用户/组织提供的正式 API Credential 或上游明确支持的企业授权流程，按组织隔离 Provider 账户、额度与账单。平台自己的云执行费用和模型费用分账显示；统一密钥入口不等于可以任意代理个人订阅。

这部分不是从开源许可得出的法律结论。对 Claude Code、Codex、Gemini CLI 等，必须分别核对实际采用的登录方式、可服务对象、是否允许托管第三方用户、自动化与转售限制；未验证条款时将“共享订阅池”“代登个人账号”“出售统一无限额度”排除在产品承诺之外。Harbor 文档提供凭据透传技术能力，也明确不同 Agent 的认证规则不同，但它并不授予商业使用权。[Harbor ACP Auth](https://docs.harborframework.com/agents/acp)、[Harbor Auth Resolution](https://docs.harborframework.com/agents/agent-auth-resolution)

## 9. 进入开发前的验收门槛建议

建议用三种 Harness、一个正式云执行池和一套自己的小任务集做集成验证。下列是上线门槛，不要求第一天支持所有厂商：

- 创建、审批、取消、续聊、产物导出都有真实端到端流程；Unsupported capability 在 UI 明示。
- 关闭客户端后任务继续，重新登录能恢复日志、状态和审批。
- Kill Worker、重启 API、断开模型 API、触发限流后，不会双重执行同一 workspace，也不会丢失已接受的命令。
- 相同 Manifest 能重跑；各 Harness 的原始日志、Patch、测试结果、失败原因与花费可追溯。
- 租户间文件、会话、对象存储 URL、凭据和执行权限隔离通过验证。
- 硬预算、并发限额、资源限额、超时和孤儿 Sandbox 回收真正生效。
- 备份可恢复；已结束任务可在 Sandbox 删除后继续查看报告和产物。
- Harness 升级先跑契约用例与回归任务集，再按版本灰度；能回退 Adapter/镜像。

这个切片已经构成可长期演进的正式底座。后续增加 Harness、执行厂商、Windows/macOS 远端环境和更多 Benchmark，应通过 capability 与 Adapter 接口扩展，而不是更换会话与实验的数据模型。
