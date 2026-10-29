# 评测与观测条目核验：身份、许可、分类与接入边界

核验日期：2026-10-27。只用官方仓库、作者/项目网站、论文与官方文档。以下是在线资料审阅，没有实际运行框架或验证评分可靠性；代码许可、任务数据许可、第三方站点条款与模型服务条款必须分别处理。

## 先明确四种不同能力

1. **Agent Runtime / Harness**：真正执行模型循环、工具、上下文与任务的系统，是客户端要连接的对象。
2. **Eval Runner**：把候选 Agent 放进规定条件下执行、评分、汇总，是评测后端。
3. **Task Environment / Dataset**：网站、桌面、模拟业务数据库、任务和评分器，是测试内容与环境。
4. **Observability / Eval Analysis**：接收已生成的事件、Trace、指标，辅助分析，不自动拥有启动、审批、恢复 Agent 的能力。

一个项目可以跨几类，但不能因为提供 `run` 命令或包含 Sandbox 就把它认定为“通用多租户 Runtime 控制面”。以下分类是根据实际接口与项目自述形成的产品判断。

## 核验清单

| 条目 | 一手身份与许可 | 已核实范围 | 对本产品的建议 |
|---|---|---|---|
| Harness² / Harness2 | [google-research/harness2](https://github.com/google-research/harness2)，[Apache-2.0](https://raw.githubusercontent.com/google-research/harness2/main/LICENSE) | 固定模型权重，递归优化 Agent 的 prompt、guardrails、memory、tools guidance、subagents、skills、scripts、plugins；支持 OpenCode/Codex executor 以及 JobBench/LAB/WorkBuddy-Bench | 属于 Harness 优化与实验研究工具；后续可作为配置优化插件，不是首版客户端/云会话管理底座 |
| WorkBuddy-Bench | [Tencent/workbuddy-bench](https://github.com/Tencent/workbuddy-bench)，[自定义 Tencent License](https://raw.githubusercontent.com/Tencent/workbuddy-bench/main/LICENSE) | Code、Web、Office、Security 工作任务；Docker 运行待测 CLI、保存产物、评分；基于 Harbor | 值得研究真实任务和 Manifest，但代码与数据内置/分发前须评估许可，不能标注成标准 Apache/MIT |
| AgentCompass | [open-compass/AgentCompass](https://github.com/open-compass/AgentCompass)，[Apache-2.0](https://raw.githubusercontent.com/open-compass/AgentCompass/main/LICENSE) | 解耦 Model、Benchmark、Harness、Environment；调度、隔离执行、持久化、重试/恢复和轨迹分析；当前 README 写 v1.0.0 已发布 | 应加入 Harbor 的直接备选，尤其当非编码、GUI、多领域评测成为核心时；先做相同样本集验证再选一个主 Runner |
| MASEval | [maseval/MASEval](https://github.com/maseval/MASEval)，[MIT](https://raw.githubusercontent.com/maseval/MASEval/main/LICENSE) | framework-agnostic 的多 Agent 系统评测接口；用户提供自己的 Agent 实现；系统整体作为评估单元 | 更适合团队编排/多 Agent 实验。官方 Benchmark 文档目前仅 MACS 完成与原实现对齐验证，其余标 Beta，不能直接声称所有得分复现原榜单 |
| ProofAgent Harness | [ProofAgent-ai/proofagent-harness](https://github.com/ProofAgent-ai/proofagent-harness)，仓库标 Apache-2.0 | 对抗式多轮评估、多 Juror、上下文评估、治理与 CI gate、报告导出 | 可选安全/行为评测插件；其产品评分与治理“认证”不等于外部监管认证，也不代替任务的客观验收 |
| Arnes | 名称不能唯一定位。已核实 [frangelbarrera/agentic-harness](https://github.com/frangelbarrera/agentic-harness) 与 [PyPI arnes](https://pypi.org/project/arnes/)，仓库标 Apache-2.0 | Python/YAML playbook Agent Harness、审计、预算；内置 benchmark 默认执行 seeded mock LLM | 这个候选主要是 Runtime/Workflow，而非通用横向评测基础设施；mock 的成功率不能代表真实模型生产成功率。另有 `andreban/arnes` Rust/ACP 候选，但本次官方 URL 未成功读取，身份待确认 |
| Kensa | [kensa-sh/kensa](https://github.com/kensa-sh/kensa)，当前仓库标 Apache-2.0 | 从 Agent Trace 提取评测线索，生成或编写 pytest eval，在 CI 运行；支持 Trace 导入与 Langfuse 连接 | 适合把生产失败变成回归用例。作者旧博客曾称 MIT，实施时以锁定版本 LICENSE 为准；避免误用同名 Kotlin BDD 或 SSH 合规扫描项目 |
| PawBench | 此处相关候选是 [agentscope-ai/PawBench](https://github.com/agentscope-ai/PawBench)，仓库标 Apache-2.0 | Model × Harness co-evaluation；v1.0 README 为 150 任务，qwenpaw/openclaw/hermes 三种 Harness；任务切片、轨迹、自动检查/LLM Judge | 高度相关，适合扩展通用 Agent 实验数据与分析维度。任务由多来源复用，仍需查各数据源许可；不要与同名吞吐测试或世界模型视频评测混淆 |
| ClawBench | 名称有多个独立项目；浏览器任务候选为 [TIGER-AI-Lab/ClawBench](https://github.com/TIGER-AI-Lab/ClawBench)，仓库标 Apache-2.0 | 真实在线网站任务，HTTP 请求拦截、任务 oracle/Judge、视频和轨迹等证据；V1/V2 任务与评分不同 | 作为外部浏览器任务集；在线站点易变化，重现能力低于固定离线环境，不宜成为首版唯一质量门槛。另有 [clawbench](https://github.com/clawbench) 组织与 [clawbench.club](https://clawbench.club/) 项目，不能混用规模、许可或榜单 |

范围来源：[Harness² README](https://github.com/google-research/harness2)、[Harness² Setup](https://github.com/google-research/harness2/blob/main/docs/SETUP.md)、[WorkBuddy 配置与 Manifest](https://github.com/Tencent/workbuddy-bench/blob/main/configs/README.md)、[AgentCompass README](https://github.com/open-compass/AgentCompass)、[MASEval Benchmark 状态](https://maseval.readthedocs.io/en/latest/benchmark/)、[ProofAgent README](https://github.com/ProofAgent-ai/proofagent-harness)、[Agentic Harness Benchmark](https://github.com/frangelbarrera/agentic-harness#benchmark)、[Kensa 作者旧文](https://satyaborg.com/blog/kensa-agent-evals-harness)、[Kensa 当前仓库](https://github.com/kensa-sh/kensa)、[PawBench README](https://raw.githubusercontent.com/agentscope-ai/PawBench/main/README.md)、[ClawBench 官网](https://www.claw-bench.com/)。

### 需要单独保留的许可与方法限制

- WorkBuddy-Bench LICENSE 的第 0 条明确写有额外地域条款：`IS NOT INTENDED FOR USE WITHIN THE EUROPEAN UNION`，且声明冲突时该条优先。全球分发/托管前需要明确处理，不能把它归为无限制宽松许可。[原文](https://raw.githubusercontent.com/Tencent/workbuddy-bench/main/LICENSE)
- MASEval 的代码 MIT 不覆盖它接入的所有 Benchmark 数据；官方要求按各 Benchmark 单独查看来源与许可。[Benchmark licensing](https://maseval.readthedocs.io/en/latest/benchmark/)
- Harness² 对当前任务进行多次 Propose–Probe–Compose，再更新持久改进 Harness。与固定配置单次运行比较时必须把优化阶段用量与时间算入，区分“开发集优化后冻结”与“测试时自适应”，并避免测试集 oracle 泄漏。[Harness²](https://github.com/google-research/harness2)
- PawBench 使用模型 Judge，其 README 的 v1.0 报告明确列出 Judge。评测产品必须保存 Judge/rubric 版本，不能把其分数当作完全确定性单元测试。[PawBench 方法](https://raw.githubusercontent.com/agentscope-ai/PawBench/main/README.md)
- ClawBench 同时存在真实网站浏览器 benchmark、离线业务任务集和依赖 OpenClaw Gateway 的 orchestration benchmark。只给名称不足以选择正确 Adapter。[浏览器版](https://github.com/TIGER-AI-Lab/ClawBench)、[离线任务组织](https://github.com/clawbench)、[Orchestration 版](https://clawbench.club/)

## WebArena、OSWorld、τ-bench 的准确位置

| 项目 | 类型与核实能力 | 不应据此推导的能力 | 本产品接入方式 |
|---|---|---|---|
| [WebArena](https://github.com/web-arena-x/webarena) | 可自托管网站任务环境与 Web Agent Benchmark；仓库 Apache-2.0。官方指向 AgentLab/BrowserGym 作为更新的导航实验基础设施 | 不是可直接管理任意云端 Harness 的会话服务 | TaskEnvironment Adapter，准备/重置网站与账号状态、把候选 Agent 连接到浏览器接口、调用对应 evaluator |
| [OSWorld](https://github.com/xlang-ai/OSWorld) | 真实桌面/VM 环境与开放任务 Benchmark；`desktop-env` 可独立安装；仓库 Apache-2.0 | VM 的生命周期管理与动作接口不等于完整 SaaS Runtime、租户授权或任意 Harness 原生对话 | Desktop TaskEnvironment Adapter；固定 VM image、OS、显示参数、工具接口和 evaluator；不要从客户端三平台支持推导出远端任意OS等价 |
| [τ-bench](https://github.com/sierra-research/tau-bench) | Tool-Agent-User 交互评测、retail/airline 模拟环境、User Simulator 与业务状态评分；仓库 MIT | 不是云托管通用 Agent 的管理后端，User Simulator 也不是真人对话会话系统 | BusinessSimulation Adapter；固定用户模拟模型/策略、初始DB、规则、工具契约和重复次数，并单列模拟器调用成本 |

OSWorld 的 Docker 路径本质仍涉及 VM/KVM；当前文档另外列出了云 Provider。选中环境后要验证真实的 Kernel/VM 隔离边界，而不能只看包装层叫“Docker”。τ-bench 官方也提示自动错误归因使用 LLM 可能不准确，应保留原始轨迹与状态差异。[OSWorld 安装与评测](https://github.com/xlang-ai/OSWorld)、[τ-bench User Simulator 与 Error Identification](https://github.com/sierra-research/tau-bench)

## OpenLIT / OpenTelemetry 的真实覆盖范围

[OpenLIT](https://github.com/openlit/openlit) 当前仓库是 Apache-2.0，定位已包括 Agent Harness engineering、观测、Evals、Guardrails 等。Python/TS SDK 对受支持的 Provider/Framework 做自动 instrumentation；Coding Agent CLI 可安装 Claude Code/Codex/Cursor/OpenCode 等具体集成，将 Hook 事件映射为 OTel 数据。不能用过时信息声称它“只有 SDK”。

但它也不能给任意黑盒 Harness 自动补出全部内部过程。产品里要区分：

- 原生 SDK/Hook/插件已输出的事件：可采集实际暴露的内容。
- 客户端或 Runner 自己看到的生命周期、工具调用、费用：由 Adapter 补充并注明来源。
- Harness 内部未暴露的推理、隐藏调用、缓存与子 Agent：标为缺失或不可观测，不能填成 0。
- Token/cost：区分实测 provider usage、Harness 自报、网关记账与估算，记录覆盖率。

这是依据 instrumentation 工作边界形成的接入建议。OTel 只定义和传递已产生的遥测，不授予观察未知进程内部状态的能力；原始协议事件和业务状态仍应在自己的事件层保存。GenAI convention 当前已迁到[官方独立仓库](https://github.com/open-telemetry/semantic-conventions-genai)，集成时锁 convention 与 exporter 版本。

## 推荐优先级

1. **优先验证 Harbor 与 AgentCompass 两个 Runner 候选，先选一个主执行器。** 同时接入两个不会自动让产品更完整，反而需要维护两套失败语义。
2. **优先参考 Harness-Bench 的“Model × Native Harness”实验方法。** 保留 native context/tools/recovery，不把不同 Harness 强行改成相同简化 Loop。[Harness-Bench](https://www.harness-bench.ai/)
3. **数据集从自有任务 + 外部参照开始。** 编码用明确版本的 Terminal-Bench/SWE-bench；通用工作用经许可核验的 Harness-Bench/PawBench；Web/桌面/业务仿真再分别接对应任务环境。
4. **Kensa/ProofAgent/OpenLIT 作为插件与分析出口。** 它们补回归、对抗与遥测，不替代会话与权限控制面。

Harbor 与 Harness-Bench 的云端执行、隔离、版本和评分细节见同目录 `evaluation-cloud.md`。Harness-Bench 的当前公开网站/仓库与论文摘要有任务规模差异，本次也未在仓库根目录找到明确 LICENSE；先借鉴方法，正式复制代码/数据前解决许可与版本，不能将公开可见等同于可随意商用。
