# 通用、持久 Agent Runtime 短名单

读取日期：2026-10-27（Asia/Shanghai）。新范围：源码开放、可部署到自己的云、可替换模型、有真实 agent 执行循环及长期状态；纯 SDK 不作为独立产品。本文查官方文档、发布元数据与关键源码，未安装或实跑。主仓 main 与正式 release 的能力差异需要在集成前按版本复验。

## 结论与优先级

优先接入 **Hermes Agent、OpenClaw**；再验证 **QwenPaw（原 CoPaw）、Letta App Server**；**Pi** 作为需要服务包装的可控 CLI runtime；**HKUDS OpenHarness/ohmo** 保留观察。这个排序是对“统一客户端接入”的适配成本判断，不是模型效果或绝对安全性排名。

| Runtime | 独立运行形态 | 持久状态 / 模型选择 | 远程交互与审批证据 | 生产接入的主要差距 | 建议 |
|---|---|---|---|---|---|
| Hermes Agent | CLI + 常驻 messaging gateway + HTTP API | sessions、长期记忆、skills；多 provider/自有 endpoint | runs、SSE、stop、approval、clarify、capabilities；会话 REST；共享会话另有 WS | API session 与其他 surface 的会话拥有权不同；profile/home 需隔离；不能假定所有版本具有同一 durable 语义 | 第一批通用 adapter |
| OpenClaw | 常驻 Gateway + 自有 agent/runtime 插件 + Web/客户端 | sessions/memory/agent workspace；云模型、本地 provider、可换 harness | 正式 WS 控制协议，session/chat/events、operator scopes、审批、问题与重连恢复 | Gateway 是受信操作边界；对不互信租户仍需外层隔离与治理；自身已是强客户端/平台竞品 | 第一批；重点验证可复用边界 |
| QwenPaw（原 CoPaw） | Python app + Console + channels + Hub | user/session 历史、memory、skills；14+ provider/本地模型/兼容 API | REST chat + SSE；已发行 approval router；Hub 单入口与每用户 runtime | Hub 官方仍限互信内部团队，不适合未知用户公共多租户；多节点与弹性尚缺 | 第二批，内部/私有部署优先 |
| Letta Code App Server | 常驻网络 runtime，可多 agent 并行 | backend local 可将状态放自己的机器；可换 provider/model | 单 WS，runtime_start、stream、sync、abort、approval_response、能力发现；可选 OpenAI兼容HTTP/ACP | 不能沿用已退休 V1 server；BYOM 默认Cloud与local AppServer不同；最后订阅者断线可能取消运行 | 第二批，重点测长期记忆 |
| Pi | 可执行 CLI；持久子进程 JSONL RPC；另有 SDK | session文件/树、resume、多 provider；skills/extensions | RPC命令与事件；extension UI可传确认对话 | RPC是stdio而非现成网络daemon；需自己的认证、worker、事件持久化、审批策略、隔离和部署控制 | adapter包装后接入 |
| HKUDS OpenHarness + ohmo | 完整CLI agent；ohmo为个人助手/channel gateway | memory/session resume；OpenAI/Anthropic兼容API与其他provider | JSON/stream-json批处理、交互权限；未验证独立通用远程控制API | 发行与主分支更新较早；API目录主要为模型客户端；远程审批与服务契约待验证 | 暂缓第一批 |

“有远程审批”只说明存在可接入路径，不代表所有工具、运行模式和并发条件都自动满足统一审批策略。以下分项目给出一手证据。

## 身份与许可证纠正

| 用户清单称谓 | 核实后的官方身份 | 许可证与证据 |
|---|---|---|
| Hermes / NousResearch | [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent)；不是同名模型API本身 | [MIT](https://github.com/NousResearch/hermes-agent/blob/a382af23f4c067d23463b5781958684c2cda82bc/LICENSE) |
| OpenClaw | [openclaw/openclaw](https://github.com/openclaw/openclaw) | **[MIT](https://github.com/openclaw/openclaw/blob/a218de7ac2355fa962833cc8720fbdbdb4d131d2/LICENSE)，不是Apache-2.0** |
| CoPaw | 旧 `agentscope-ai/CoPaw` 已跳转 [agentscope-ai/QwenPaw](https://github.com/agentscope-ai/QwenPaw) | [Apache-2.0](https://github.com/agentscope-ai/QwenPaw/blob/3d35ad12476da0b8f88b9b51cec121d4c0c52b3c/LICENSE) |
| Letta | 活跃完整runtime在 [letta-ai/letta-code](https://github.com/letta-ai/letta-code)；旧 `letta-ai/letta` 提供迁移说明 | [当前runtime Apache-2.0](https://github.com/letta-ai/letta-code/blob/b995585d42313c9ddb8873986de05e01a595d52a/LICENSE) |
| Pi / mariozechner/pi-mono | 旧官方为 `badlogic/pi-mono`，现迁 [earendil-works/pi](https://github.com/earendil-works/pi)；旧 `@mariozechner/*` 包已deprecated | [MIT](https://github.com/earendil-works/pi/blob/6c384aa30f619d7b28963f2bab24d8dacbe73cb6/LICENSE)；[官方迁移公告](https://pi.dev/changelog/2026/5/7/pi-has-a-new-home) |
| OpenHarness | [HKUDS/OpenHarness](https://github.com/HKUDS/OpenHarness)，包含 `oh` 与 `ohmo`；需和其他同名项目分开 | [MIT](https://github.com/HKUDS/OpenHarness/blob/9b2efd795c6aa09f88b0c257d269a9e518da6ae7/LICENSE) |

这些许可证不自动授予模型订阅额度共享、品牌、第三方服务或闭源依赖的权利。部署应独立提供模型凭据和权限策略。

## Hermes Agent

它是完整通用agent，内建工具、记忆、技能和多平台gateway，可运行在VPS或Docker/SSH/Modal/Daytona等工具执行后端。provider可用自有 endpoint，无需绑定 Nous 模型或 Nous Portal。[官方README](https://github.com/NousResearch/hermes-agent/blob/a382af23f4c067d23463b5781958684c2cda82bc/README.md)

API不仅是转发模型聊天：已发行 v0.22.5 文档提供 `/v1/capabilities`、runs、事件、停止、approval/clarify、session及job控制；Responses有server-side历史。durable admission、幂等和终止状态按canonical/legacy模式区分，不能概括成全局exactly-once。API session不能直接绑定CLI/Desktop/IM拥有的session，访问同一现有会话需走gateway WebSocket。统一客户端应发现能力，再选择对应控制协议。[v0.22.5 API Server契约](https://github.com/NousResearch/hermes-agent/blob/v0.22.5/website/docs/user-guide/features/api-server.md)

长期memory与可搜索旧会话使它适合作为记忆型runtime的首批样本；批量评测应给每个实验独立profile/home和可恢复快照，避免学习结果串入另一实验。隔离是本产品的集成建议，需与当前profile/session authority配置一起验证。[Memory官方文档](https://hermes-agent.nousresearch.com/docs/user-guide/features/memory)

## OpenClaw

OpenClaw当前不只是聊天channel转发器，Gateway承载会话、工具、事件和客户端控制；model/provider与agent harness以插件替换，支持通用助手场景。自身已有跨平台客户端，因此同时是可接入runtime与直接产品竞品。[官方README](https://github.com/openclaw/openclaw/blob/a218de7ac2355fa962833cc8720fbdbdb4d131d2/README.md)

其官方客户端集成指南给出了 `operator.read/write/approvals`，问题处理另加 `operator.questions`。断线后需要重新订阅、读权威history、按run/seq对账；文档细化了快照与事件交错，不能靠“重新打开SSE”保证恢复。[Building a Gateway client](https://docs.openclaw.ai/gateway/clients)

远程审批有真实生命周期：gateway广播 `exec.approval.requested`，客户端调用 `exec.approval.resolve`，还存在automation专用standing grants。该能力值得接入，但与普通会话、node、定时任务的行为并不相同，必须逐模式测试。[Exec approvals](https://docs.openclaw.ai/tools/exec-approvals)

部署时把Gateway当受信操作端，模型执行与租户隔离另作边界，不能用一枚共享operator token给所有客户端用户。官方Gateway文档明确默认鉴权、默认loopback和远程隧道模式。[Gateway运行指南](https://github.com/openclaw/openclaw/blob/a218de7ac2355fa962833cc8720fbdbdb4d131d2/docs/gateway/index.md)

## QwenPaw（原CoPaw）

它有独立app/Console、channel、memory、技能、模型配置、容器部署，当前并非只能使用Qwen模型。README列本地模型及多云provider，也提供通用办公、自动化与多agent能力。[官方README](https://github.com/agentscope-ai/QwenPaw/blob/3d35ad12476da0b8f88b9b51cec121d4c0c52b3c/README.md)

HTTP入口 `/api/console/chat` 支持SSE，使用 `session_id/user_id` 自动保存和加载历史。远程Web authentication是可配置功能，本机请求有旁路语义，因此不能让本产品的反向代理无差别把所有外部用户伪装成本机请求。[官方REST契约](https://github.com/agentscope-ai/QwenPaw/blob/v2.2.2.post1/website/public/docs/api-tutorial.en.md)

工具批准并非只存在UI文案：发行版有 `approval.py` router、approve/deny与pending记录，并按root session校验；集成可复用其请求标识，但仍需核对路由挂载和实际身份模式。[v2.2.2.post1审批源码](https://github.com/agentscope-ai/QwenPaw/blob/v2.2.2.post1/src/qwenpaw/app/routers/approval.py)

Hub能为用户分配独立进程/容器、工作区与凭据，但官方明确“early release，仅互信内部团队”，不能作为未知用户公共多租户服务。文档同时说明尚无per-user资源配额、多节点容量调度和autoscaling。适合自有团队验证，面向公众需外部控制面与强隔离。[已发行Hub文档](https://github.com/agentscope-ai/QwenPaw/blob/v2.2.2.post1/website/public/docs/hub.en.md)

安全文档还区分interactive approval与无session非交互运行；后者部分guard finding只记录而仍可继续。批量任务的硬拒绝应配置 `denied_tools` 等确定性策略，不能依赖有人点UI。[官方安全文档](https://github.com/agentscope-ai/QwenPaw/blob/v2.2.2.post1/website/public/docs/security.en.md)

## Letta：必须采用当前 App Server 路径

旧 `letta-ai/letta` README明确V1 API server已经retired、移到archive，活跃源码在letta-code；不能把旧Docker服务文档当当前默认架构。[迁移说明](https://github.com/letta-ai/letta/blob/main/README.md)

当前App Server是常驻服务，可并行管理多个agent，通过WebSocket直接连接；`backend local`使状态在自己的服务器，无需把状态托管给Letta Cloud。[App Server](https://docs.letta.com/self-hosting/app-server)；[Self-hosting](https://docs.letta.com/self-hosting)

部署命令应明确使用 `letta server --backend local --listen ...`；单独 `letta server` 的Cloud BYOM模式会向Letta Cloud建出站连接，不等价于全自托管。HTTP兼容入口是可选功能，Responses不支持stored response与`previous_response_id`，会话连续性需`X-Letta-Chat-Key`或原生协议。[App Server Quickstart](https://docs.letta.com/self-hosting/app-server/quickstart)；[BYOM差异](https://docs.letta.com/platform/computers/byom)

原生协议支持runtime/conversation、approval_response、event_seq、sync与abort。最后一个订阅客户端断线且没有其他客户端接手时，App Server会请求取消active turn。因此桌面关闭后仍要继续的任务，应由云端常驻controller拥有连接，桌面只连接控制面。[Protocol lifecycle](https://docs.letta.com/self-hosting/app-server/protocol-lifecycle)

它有memory/identity、skills、自我反思等长期agent能力，也可配置自己的模型provider；适合长期记忆对比。stateful不代表默认分布式调度或多租户隔离，仍需验收。[当前runtime README](https://github.com/letta-ai/letta-code/blob/b995585d42313c9ddb8873986de05e01a595d52a/README.md)

## Pi：完整 CLI Runtime，不是只有 SDK；但还不是服务产品

Pi官方区分interactive、print/JSON、RPC与TypeScript SDK。RPC是长驻子进程，stdin/stdout传JSONL命令、响应和流事件；可以跨语言集成，并非纯SDK框架。[CLI README](https://github.com/earendil-works/pi/blob/6c384aa30f619d7b28963f2bab24d8dacbe73cb6/packages/coding-agent/README.md)；[RPC契约](https://github.com/earendil-works/pi/blob/v1.4.0/packages/coding-agent/docs/rpc.md)

session可保存和resume，支持树形分支；`--no-session`是临时运行，退出后不可恢复。评测与持续对话应分别选择明确模式。[Sessions](https://github.com/earendil-works/pi/blob/v1.4.0/packages/coding-agent/docs/sessions.md)

权限可通过extension `tool_call` gate和确认UI实现，RPC能转发支持的确认对话；这些hook/示例不是开箱即用的企业远程审批服务。若接入，worker包装层应负责网络鉴权、进程生命期、事件恢复、审批超时与拒绝、资源与工具边界。[Extensions](https://github.com/earendil-works/pi/blob/v1.4.0/packages/coding-agent/docs/extensions.md)

## HKUDS OpenHarness / ohmo：真实完整 Agent，但暂缓首批

`oh`有agent loop、tools、skills、memory、权限、resume及JSON/stream-json模式；`ohmo`在其上提供个人workspace和Slack/Feishu/Telegram/Discord gateway。它不是纯SDK，但其“支持Claude/Codex订阅”不自动代表保真运行原版Claude/Codex harness。[官方README](https://github.com/HKUDS/OpenHarness/blob/9b2efd795c6aa09f88b0c257d269a9e518da6ae7/README.md)

本次检查到的 `src/openharness/api` 是模型调用客户端/registry，而不是面向统一客户端的完整服务端控制API；未核实独立session/run/remote approval网络契约。因此先按CLI/stream-json可包装、service能力待验处理。[API目录](https://github.com/HKUDS/OpenHarness/tree/9b2efd795c6aa09f88b0c257d269a9e518da6ae7/src/openharness/api)

最新正式release v0.1.9（2026-05-07），main最近提交2026-06-04；这不证明项目已停用，但相对当前多变harness协议，接入维护风险更高。[v0.1.9](https://github.com/HKUDS/OpenHarness/releases/tag/v0.1.9)；[main取证提交](https://github.com/HKUDS/OpenHarness/commit/9b2efd795c6aa09f88b0c257d269a9e518da6ae7)

## 另两项源流纠正

- **OpenHands原名OpenDevin，与SWE-agent是不同项目。** 作者原始论文摘要明确 `OpenHands (f.k.a. OpenDevin)`。[OpenHands论文](https://arxiv.org/abs/2407.16741)
- **Kilo需按时代区分。** Kilo官方说明2025年从Roo fork起步；2026年新版extension已经重建在OpenCode server上，当前仓库README也明确Kilo CLI是OpenCode fork。因此“始终只是Roo/Cline分支”会误导架构和许可证选择。[官方迁移文章](https://kilo.ai/articles/roo-to-kilo-migration-guide)；[当前官方仓库](https://github.com/Kilo-Org/kilocode)

## 已读取版本

| Runtime | 最新正式 release | 读取到的 main SHA |
|---|---|---|
| Hermes Agent | v0.22.5，2026-10-23发布（notes写10-22） | `a382af23f4c067d23463b5781958684c2cda82bc` |
| OpenClaw | v2026.10.7，2026-10-24发布 | `a218de7ac2355fa962833cc8720fbdbdb4d131d2` |
| QwenPaw | v2.2.2.post1，2026-10-22发布 | `3d35ad12476da0b8f88b9b51cec121d4c0c52b3c` |
| Letta Code | v0.34.24，2026-10-25发布 | `b995585d42313c9ddb8873986de05e01a595d52a` |
| Pi | v1.4.0，2026-10-21发布 | `6c384aa30f619d7b28963f2bab24d8dacbe73cb6` |
| HKUDS OpenHarness | v0.1.9，2026-05-07发布 | `9b2efd795c6aa09f88b0c257d269a9e518da6ae7` |

元数据通过官方GitHub connector读取。版本名中的日期不等同于publish时间；官网文档可能领先发行。首轮验收应固定镜像/CLI/adapter/model与permissions配置，覆盖：建session、流事件、工具拒绝与批准、请求取消、重连/重启、客户端关闭后继续、会话归属、产物与成本归因、从空白或快照开始批量实验。
