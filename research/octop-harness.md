# Octop / Octop Harness 核验补充

核验日期：2026-10-27（Asia/Shanghai）。目标口径：源码开放、可部署自有云、模型可替换，面向通用 Agent；同时区分底层 runtime 与带管理界面的完整产品。以下基于固定官方源码与官方发布记录，只做静态核验，未启动服务、运行测试或调用模型。

## 结论

**应把 Octop 加入重点竞品和完整服务候选，把 Octop Harness 单列为可嵌入的运行时基础。** 两者都在腾讯云官方 `TencentCloud` 组织，MIT；它们与此前的 npm `@tencent-ai/agent-harness` 是不同项目、不同技术栈与协议实现，不能混称。[Harness仓库](https://github.com/TencentCloud/octop-harness)、[产品仓库](https://github.com/TencentCloud/Octop)

| 项目 | 当前核验快照 | 实际形态 |
|---|---|---|
| `TencentCloud/octop-harness` | GitHub latest `v1.0.5`，2026-10-24；commit `76cd38d7b8ffc66c28ba24ad60bbab206a95d3ac` | Python library + terminal CLI + stdio ACP，装配并执行真实 DeepAgents/LangGraph loop；不是自带团队管理台的完整服务 |
| `TencentCloud/Octop` | GitHub latest `v1.0.3b1`，2026-10-25；commit `890111b7879665840414ca28ab168b05b0db0921` | 完整自托管产品：FastAPI、React管理台、认证、聊天、任务、连接器、双向ACP，以及Wails三端桌面壳 |

发布证据：[Harness v1.0.5](https://github.com/TencentCloud/octop-harness/releases/tag/v1.0.5)、[Octop v1.0.3b1](https://github.com/TencentCloud/Octop/releases/tag/v1.0.3b1)。版本号不代替生产验收。

## 1. 官方身份、许可与成熟度

两仓库均直接归属 `TencentCloud`，源码LICENSE为MIT；Harness的项目URL与包元数据也指回同一官方仓库。[Harness LICENSE](https://github.com/TencentCloud/octop-harness/blob/76cd38d7b8ffc66c28ba24ad60bbab206a95d3ac/LICENSE)、[Octop LICENSE](https://github.com/TencentCloud/Octop/blob/890111b7879665840414ca28ab168b05b0db0921/LICENSE)

Harness README称production-grade，但其v1.0.5 `pyproject.toml`仍列 `Development Status :: 3 - Alpha`，且DeepAgents依赖为 `>=0.7.16,<0.8`。应同时保留这两项证据，不能直接用README措辞得出“已达到多租户SaaS生产标准”。[包元数据](https://github.com/TencentCloud/octop-harness/blob/76cd38d7b8ffc66c28ba24ad60bbab206a95d3ac/pyproject.toml)

## 2. Runtime 与完整 App 的关系

Harness不是单纯远程client SDK：`HarnessAgent`实际调用 `deepagents.create_deep_agent`，把模型、tools、skills、memory、middleware、checkpointer装配为CompiledStateGraph，提供 `call`、`stream`、`stream_events`、history、cancel、HITL resume。其loop来自DeepAgents/LangGraph，Octop增加工程与产品能力。[agent.py](https://github.com/TencentCloud/octop-harness/blob/76cd38d7b8ffc66c28ba24ad60bbab206a95d3ac/src/octop_harness/agent.py)

Octop组合Harness、Memory、Browser、Gateway等，提供完整HTTP/SSE/WebSocket产品接口、Web管理台、JWT多用户、SQLite或Postgres控制面。它已覆盖通用办公、研究、浏览器、IM和定时任务，不能只归为coding工具。[产品README](https://github.com/TencentCloud/Octop/blob/890111b7879665840414ca28ab168b05b0db0921/README.md)

三端客户端为Wails v3壳，内置portable runtime、启动Octop本地服务；可用 `OCTOP_DESKTOP_URL` 开发连接既有服务。它是现有产品的重要参考，但不等于已提供用户设想的“多个独立harness原生控制台+统一批量评测”。[Desktop实现说明](https://github.com/TencentCloud/Octop/blob/890111b7879665840414ca28ab168b05b0db0921/desktop/README.md)

## 3. 模型可替换与工具调用

`ProviderConfig`公开 `id/base_url/api_key/protocol/headers/models`，OpenAI协议通过LangChain `ChatOpenAI`构建；每模型可明确选择Chat Completions或Responses，默认Chat Completions。不是只能调用腾讯模型或云产品API。[配置类型](https://github.com/TencentCloud/octop-harness/blob/76cd38d7b8ffc66c28ba24ad60bbab206a95d3ac/src/octop_harness/config/__init__.py)、[Model factory](https://github.com/TencentCloud/octop-harness/blob/76cd38d7b8ffc66c28ba24ad60bbab206a95d3ac/src/octop_harness/llm/factory.py)

README给出自建OpenAI-compatible base URL和function-calling model的配置；默认client工具搜索使用普通function tool，不要求托管Responses工具搜索。实际loop仍要求目标模型能处理工具schema、流式参数、工具结果续聊与相应上下文格式；“支持base URL”不证明所有端点/模型行为一致。[Harness README](https://github.com/TencentCloud/octop-harness/blob/76cd38d7b8ffc66c28ba24ad60bbab206a95d3ac/README.md)

评测时应固定DeepAgents、LangChain及provider依赖的完整lock/environment，不能只固定octop-harness版本；其依赖范围允许下游库升级改变行为。这是基于包声明的工程判断。

## 4. Session、Checkpoint、HITL 与恢复

Harness有真实持久化：默认AsyncSqliteSaver，memory启用时可复用memory saver，也允许注入checkpointer；history按thread_id读取。重要边界是：sessions JSONL、memory数据库和checkpoints.sqlite属于宿主本地runtime持久化，**S3/Postgres workspace backend不自动把这些状态一起远程化**。[agent.py持久化与checkpointer](https://github.com/TencentCloud/octop-harness/blob/76cd38d7b8ffc66c28ba24ad60bbab206a95d3ac/src/octop_harness/agent.py)

源码在checkpointer缺失或初始化失败时允许警告后无持久化继续运行。生产部署应把“恢复必需的checkpointer不可用”做成启动/任务准入失败，避免界面承诺可恢复但实际退化；此为对上层实现的建议，并非已经存在的强制策略。

HITL通过 `interrupt_on`、`hitl_required`和LangGraph `Command(resume={decisions: ...})`实现；`ask_user_question`使用respond决策。`resume_hitl`明确要求宿主先授权调用者；暂停请求scope还保存在进程内字典，进程重建后需由宿主恢复权限与上下文，不能假设checkpoint自动恢复全部授权范围。[agent.py](https://github.com/TencentCloud/octop-harness/blob/76cd38d7b8ffc66c28ba24ad60bbab206a95d3ac/src/octop_harness/agent.py)、[LangGraph协议](https://github.com/TencentCloud/octop-harness/blob/76cd38d7b8ffc66c28ba24ad60bbab206a95d3ac/src/octop_harness/protocols/langgraph.py)

Octop App额外提供带当前用户/agent访问检查的HITL SSE resume、pending审批存储与刷新后卡片重建。它是完整App层能力，不能全部归给独立Harness library。[产品HITL路由](https://github.com/TencentCloud/Octop/blob/890111b7879665840414ca28ab168b05b0db0921/src/octop/api/routers/chat/routes.py)

## 5. ACP：原生Loop仍在，但当前是委派，不是完整透明接管

官方文档明确inbound `octop acp` 与outbound `acp_runner` 当前都是stdio JSON-RPC，**没有HTTP ACP endpoint**。Octop普通HTTP聊天API与ACP不是同一个协议。Outbound在运行Octop的宿主spawn外部CLI；外部CLI自身原生loop仍在运行，Octop没有重写那个loop，但它成为主DeepAgents的一次工具委派。[产品ACP文档](https://github.com/TencentCloud/Octop/blob/890111b7879665840414ca28ab168b05b0db0921/docs/acp.md)、[ACPService](https://github.com/TencentCloud/octop-harness/blob/76cd38d7b8ffc66c28ba24ad60bbab206a95d3ac/src/octop_harness/acp/service.py)

### trusted 与审批

- `ACPRunnerConfig.trusted`默认False，内置runner模板设True，但内置runner默认未启用。[models.py](https://github.com/TencentCloud/octop-harness/blob/76cd38d7b8ffc66c28ba24ad60bbab206a95d3ac/src/octop_harness/acp/models.py)
- 本次已读ACP client/service/permissions/tool_adapter路径，`trusted`没有参与permission决策；不能看到True就宣称它会自动批准。[client.py](https://github.com/TencentCloud/octop-harness/blob/76cd38d7b8ffc66c28ba24ad60bbab206a95d3ac/src/octop_harness/acp/client.py)
- 请求保留options，硬拒绝后，其余请求持有Future等待respond；回复必须精确匹配选项ID。[permissions.py](https://github.com/TencentCloud/octop-harness/blob/76cd38d7b8ffc66c28ba24ad60bbab206a95d3ac/src/octop_harness/acp/permissions.py)
- `acp_runner(action='respond', message='<option id>')`是模型可调用工具；工具描述要求先询问用户，但精确option ID校验本身不证明人类授权。产品若要求所有审批由真实用户决定，应由宿主绑定并验证用户、run/tool-call、参数与有效期，不把“模型传回正确ID”当用户签署。[acp_runner.py](https://github.com/TencentCloud/octop-harness/blob/76cd38d7b8ffc66c28ba24ad60bbab206a95d3ac/src/octop_harness/builtin/tools/acp_runner.py)

### 保真与恢复边界

当前outbound client把工具进展主要投影为call_id/title/kind/detail，assistant结果聚合为文本；thought、plan、mode、commands、user updates直接忽略。原生工具raw I/O、完整timeline和扩展消息不能据此承诺无损展示。面向统一harness客户端，应增加专用ACP adapter及原始事件保留层。[client.py](https://github.com/TencentCloud/octop-harness/blob/76cd38d7b8ffc66c28ba24ad60bbab206a95d3ac/src/octop_harness/acp/client.py)、[文本tool adapter](https://github.com/TencentCloud/octop-harness/blob/76cd38d7b8ffc66c28ba24ad60bbab206a95d3ac/src/octop_harness/acp/tool_adapter.py)

`ACPService`为每个`(thread_id,runner)`建立内存conversation、async turn_lock和pending permission Future；已有外部进程退出时要求start重新建立，不把外部session映射持久化。宿主重启后不能仅凭主Agent checkpoint承诺外部CLI原会话/待审批精确恢复。[service.py](https://github.com/TencentCloud/octop-harness/blob/76cd38d7b8ffc66c28ba24ad60bbab206a95d3ac/src/octop_harness/acp/service.py)

Inbound adapter也需单独验收：其load_session当前主要登记session_id/cwd，未在该方法重放历史；cancel设本层flag并在下一chunk检查，不能把它与Harness直接cancel中断等待的实现混为一谈。[server.py](https://github.com/TencentCloud/octop-harness/blob/76cd38d7b8ffc66c28ba24ad60bbab206a95d3ac/src/octop_harness/acp/server.py)

## 6. 自有云执行、并发与SaaS边界

有Docker与OpenSandbox执行backend，非强依赖Daytona托管：Docker文件与exec经Docker SDK、默认不自动bind宿主workspace；named sandbox持久复用，可按agent/user/fixed scope。代码默认allow_network=False、512MB、1CPU、256 PID，属于实际执行隔离基础。[DockerSandbox](https://github.com/TencentCloud/octop-harness/blob/76cd38d7b8ffc66c28ba24ad60bbab206a95d3ac/src/octop_harness/backends/docker_sandbox.py)、[OpenSandbox](https://github.com/TencentCloud/octop-harness/blob/76cd38d7b8ffc66c28ba24ad60bbab206a95d3ac/src/octop_harness/backends/opensandbox_sandbox.py)

但本地shell也是可选/默认路径：Linux满足bwrap条件才用bubblewrap，否则使用宿主subprocess shell；workspace/root_dir的路径翻译不自动成为OS权限边界。Docker named容器close默认保持，user/fixed scope会共享执行环境；文件/容器生命周期、租户资源归属、清理和秘密注入仍需产品层定义。不能拿配置中的root_dir、工具审批或PII中间件替代多租户强隔离。[backend解析](https://github.com/TencentCloud/octop-harness/blob/76cd38d7b8ffc66c28ba24ad60bbab206a95d3ac/src/octop_harness/backends/__init__.py)、[local shell](https://github.com/TencentCloud/octop-harness/blob/76cd38d7b8ffc66c28ba24ad60bbab206a95d3ac/src/octop_harness/backends/local_shell.py)

Octop文档仍是单Python进程、in-process路由、无独立worker/外部队列的架构；控制面可以改用Postgres，但这不等于分布式执行调度、worker租约、跨副本事件恢复和配额已具备。ADR明确最初接受vertical scaling取舍；当前是否存在额外scale-out路径需实现前按版本核对。团队私有部署可先实测；面向互不信任租户的SaaS应外置runner/调度/隔离和持久事件，而非只在前面加登录。[当前产品README](https://github.com/TencentCloud/Octop/blob/890111b7879665840414ca28ab168b05b0db0921/README.md)、[单进程ADR](https://github.com/TencentCloud/Octop/blob/890111b7879665840414ca28ab168b05b0db0921/docs/adr/001-single-process-model.md)

## 纳入现有预研的定位（工程判断）

- **重点竞品**：Octop已同时覆盖三端壳、云端服务、多用户、通用Agent、模型配置和ACP委派，应优先对照，避免重复构建已有基础体验。
- **完整服务候选**：可评估独立部署Octop并由自有客户端接HTTP/WebSocket API；必须验证资源授权、升级、恢复、事件保真和商用多租户隔离。
- **底层runtime候选**：可选Octop Harness装配自有runner；它比纯provider SDK完整，但需要自己构建团队服务、调度与租户控制面。
- **不能直接复用为统一harness协议层**：现有acp_runner偏Agent委派，非全保真原生会话控制；批量评测仍需独立的隔离工作区、固定版本、预算与结果记录。

本次结论是选型准入依据，不是安全审计或生产认证；未验证性能上限、备份恢复实效、目标模型工具质量或跨平台发行包行为。
