# 通用 Agent 框架与服务化边界核验

核验日期：2026 年 10 月 27 日。以下基于官方文档与仓库当前公开内容；未安装、未执行任务，也没有据此认定某个项目整体已经通过生产验收。

## 许可和项目状态需要更新

| 用户清单条目 | 本次核验 | 对选型的影响 |
| --- | --- | --- |
| Mastra 核心 MIT | 当前根许可证将非 `ee/` 内容列为 Apache 2.0；`ee/` 采用企业许可，生产使用需满足其条款 | 把核心与企业功能分开列入物料清单，不能沿用旧 MIT 标签 |
| AutoGen 和 AG2 合并为 MIT | 两者是不同项目。Microsoft AutoGen README 宣布归档及迁移建议；AG2 当前 README 和仓库标为 Apache 2.0 | 分别记录来源和版本；新增微软栈项目优先考察 Microsoft Agent Framework |
| Semantic Kernel 作为新建企业 Agent 底座 | 当前官方 README 将 Microsoft Agent Framework 列为后继项目；SK 仍标为 MIT | 区分现有系统接入与新系统选型 |
| Dify 为 Apache 2.0 附加 | 根许可证明确，多租户服务需另获书面授权；使用其前端还需保留规定的标识与版权信息 | 自托管单企业实例与将源码作为对外多租户服务基础，是不同使用场景 |

来源：[Mastra LICENSE](https://github.com/mastra-ai/mastra/blob/main/LICENSE.md)、[AutoGen README](https://github.com/microsoft/autogen/blob/main/README.md)、[AG2 README](https://github.com/ag2ai/ag2/blob/main/README.md)、[Semantic Kernel README](https://github.com/microsoft/semantic-kernel/blob/main/README.md)、[Dify LICENSE](https://github.com/langgenius/dify/blob/main/LICENSE)。

AutoGen 的“归档”在此指 README 的维护声明；本次 GitHub API 的 `archived` 字段仍为 false，不能把它表述成已经禁止推送的仓库状态。GitHub 自动识别的单个 license 字段也不能替代根许可证与各子目录的条款。

## 框架和可服务化组合

| 项目 | 已核验的实际形态 | 服务化边界 | 本产品中的位置 |
| --- | --- | --- | --- |
| Strands Harness | AWS Strands 团队的 Python 与 TypeScript 组合式 harness，包含工具、会话、记忆、上下文管理和审批钩子；Apache 2.0 | `create_harness` 在调用者进程内运行，不自带一个通用托管控制面 | 通过固定配置的 Runner 包装接入；不能把库直接算成现成云服务 |
| deepagents 和 LangGraph | MIT harness，规划、文件系统后端、子 Agent、HITL，借助 LangGraph 的状态与 checkpoint | 云端 API、鉴权、执行沙箱、持久 checkpointer 需要明确选型；LangSmith 部署能力不能自动等同免费开源自托管产品 | 以明确的部署包作为一个接入对象；保留 graph 和配置版本 |
| tRPC-Agent-Go | Apache 2.0 Go 框架，有 streaming runner、session、memory、A2A、AG-UI、MCP 和 gateway 路径 | 需要选定实际 Agent 程序、服务端状态存储及部署配置 | 适合已有 Go 服务团队，不能只对一个空框架做有效效果排名 |
| tRPC-Agent-Python | 官方框架包含 FastAPI、A2A、AG-UI serving；会话有 Redis/SQL 等后端 | 用户工具、权限、状态、执行器和服务部署仍需应用配置；Apache 2.0 允许范围与三方依赖分开 | 可作为有声明配置的 Python Runner |
| Mastra | 当前提供独立 server、工作流暂停恢复、存储、端点与 Studio | 开发 Studio、开源核心和企业认证功能分开核实；多租户仍是应用责任 | 可接既有 Mastra 服务，或提供经验证的部署模板 |
| CrewAI | MIT 核心框架，Crews 与 Flows；商业 AMP 提供另一层控制面 | MIT 核心不自动包含 AMP 的部署、治理、团队后台 | 以用户提供的 crew/flow 应用接入，不将 AMP 宣称为全部开源 |
| AG2 | 当前 AgentOS 和工具循环、知识持久化、历史压缩等组件 | 库和编排能力不等于任意用户应用已有稳定远程服务 | 后续支持固定应用封装，不作为首批完整服务候选 |
| Semantic Kernel 和 Microsoft Agent Framework | 模型无关的 SDK/编排框架，新项目官方推荐后者 | 须选择具体 Agent、托管程序和存储后端 | 企业既有应用接入项 |
| Haystack 与 Hayhooks | Haystack 是 Apache 2.0 AI 编排框架；官方指向 Hayhooks 将 pipeline/agent 暴露为 REST 或 MCP | HTTP 接口不必然包含完整会话、审批、取消与进程恢复，需按实际部署验证 | RAG/知识任务专项 Runner，优先级取决于目标用户 |
| SOAT | 官方站描述为 Apache 2.0 自托管 Node 加 PostgreSQL/pgvector 服务，包含 REST、MCP、CLI、SDK、会话和治理 | 当前仍 pre-1.0；官网能力比较承认不提供内置代码执行，文件/执行能力通过客户端工具等外接 | 可以作为服务候选评估；不要把平台治理能力等同完整云端 OS/代码执行沙箱 |

来源：[Strands 根 README](https://github.com/strands-agents/harness-sdk/blob/main/README.md)、[Strands harness](https://github.com/strands-agents/harness-sdk/blob/main/harness-py/README.md)、[deepagents](https://github.com/langchain-ai/deepagents)、[生产部署指南](https://docs.langchain.com/oss/python/deepagents/going-to-production)、[tRPC Go](https://github.com/trpc-group/trpc-agent-go)、[tRPC Python](https://github.com/trpc-group/trpc-agent-python)、[Python 许可证](https://github.com/trpc-group/trpc-agent-python/blob/main/LICENSE)、[Mastra](https://github.com/mastra-ai/mastra)、[CrewAI](https://github.com/crewAIInc/crewAI)、[AG2](https://github.com/ag2ai/ag2)、[Microsoft Agent Framework](https://github.com/microsoft/agent-framework)、[Haystack](https://github.com/deepset-ai/haystack)、[SOAT](https://soat.ttoss.dev/)。

## 统一接入应描述可运行配置

同名框架可以搭出完全不同的 Agent。一个可复现接入对象至少包含：源码版本、应用代码和配置 hash、Agent 定义、模型与采样参数、工具清单、存储后端、镜像 digest、权限策略、网络策略及数据保留规则。“LangGraph 对比 CrewAI”的结果只有在这些条件都明确后才有意义。

对用户清单中的“全部模型无关且支持 OpenAI 兼容接口”，应逐项核实。多模型 provider 抽象、允许自定义 base URL、兼容 OpenAI Chat Completions、兼容 Responses API、支持工具调用与流式工具增量，是不同能力。认证成功或能输出文字，只能证明最基础连接，不能证明 harness 可以完成工具闭环。

建议把 SDK 类接入限定成由发布者提供的版本化运行包，而不是让桌面端任意执行未定义的 Python/Node SDK。服务封装可以复用现成框架能力，但产品应承担自己公开承诺的 API、会话恢复、鉴权和隔离责任。
