# Hermes 原生有效模型证据：两条 HTTP 路径的差别

日期：2026-10-29。固定源码 `a382af23f4c067d23463b5781958684c2cda82bc`，结论仅适用于这个快照及本仓库明确标识的受控工具 profile。此前两次 canonical Run 的严格模型验收失败仍保留，不能由另一个入口的通过改写。

## 已定位的原因

`POST /v1/runs` 在存在 SessionAuthority 时走 canonical admission。它把 `requested_model`、`requested_provider`、`model_options` 写入执行设置，未传入 `confirmed_runtime_lock` 和 `requested_runtime`。[Run admission 实现](https://github.com/NousResearch/hermes-agent/blob/a382af23f4c067d23463b5781958684c2cda82bc/gateway/platforms/api_server_runs.py#L819)

canonical Worker 在 `_settle_api_runtime_lock` 中只有遇到 confirmed lock 才调用实际 Agent 的 runtime 核对；没有 lock 时不填充该 metadata。因此仅给 `/v1/runs` 增加请求模型名，不能补足实际模型证据。这里是执行设置与 metadata 生成条件的差别；请求回显不是有效模型。[执行时的 lock 判断](https://github.com/NousResearch/hermes-agent/blob/a382af23f4c067d23463b5781958684c2cda82bc/gateway/session_api_turn.py#L542)、[Worker settlement](https://github.com/NousResearch/hermes-agent/blob/a382af23f4c067d23463b5781958684c2cda82bc/gateway/run_turn_runner.py#L123)

诊断先用固定源码实际函数的独立 AST 执行形成红色反馈：一个具有确定 `agent.model` 的 global turn，finished result/usage 没有 runtime.model。随后对同一实际 Worker settlement 函数比较三种设置：未 lock 的显式模型仍省略；confirmed lock 保存实际 Agent 模型；实际模型与 lock 不符则使结果失败、`completed:false`。没有启动模型或原生 Gateway，故这三项只定位条件分支，不计入真实 Harness 验收。复现脚本、源码 hash 和结果保存在受控诊断目录。

## 使用上游已有的 confirmed-lock 入口

`POST /api/sessions/{session_id}/chat/stream` 支持 `model`、`provider`、`require_model_lock:true`。`_prepare_session_chat` 生成并传递 confirmed runtime lock；结束时 `_turn_runtime_metadata` 从真实 Agent 的 `model/provider` 获取观察值，与锁定选择比较，不匹配会拒绝成功。[请求准备](https://github.com/NousResearch/hermes-agent/blob/a382af23f4c067d23463b5781958684c2cda82bc/gateway/platforms/api_server.py#L2821)、[实际 runtime 核对](https://github.com/NousResearch/hermes-agent/blob/a382af23f4c067d23463b5781958684c2cda82bc/gateway/platforms/api_server.py#L3601)

该入口的最终 `run.completed` SSE 带有效 runtime；开始事件的 `accepted` 只表示接收选择，不能通过验收。最终值必须为预期模型、`custom` provider 和 `model_lock:confirmed`，同时核对真实工具闭环、随机回执、历史、文件及终态。[结束事件实现](https://github.com/NousResearch/hermes-agent/blob/a382af23f4c067d23463b5781958684c2cda82bc/gateway/platforms/api_server.py#L3170)

两条流的行为不同。session-chat stream 的事件名在 SSE `event:` 行，JSON seq 从 1 开始，没有 Last-Event-ID 重放接口；连接断开会中断该执行。canonical `/v1/runs/{id}/events` 的现有补取验证继续保留独立身份。生产 Adapter 不应把两条路径的能力拼成一条不存在的原生接口；断线后先核对执行，不盲目重新提交。[session 事件队列](https://github.com/NousResearch/hermes-agent/blob/a382af23f4c067d23463b5781958684c2cda82bc/gateway/platforms/api_server.py#L957)、[断线处理](https://github.com/NousResearch/hermes-agent/blob/a382af23f4c067d23463b5781958684c2cda82bc/gateway/platforms/api_server.py#L3206)

## 实际端点需要独立观察

原生 runtime metadata 只保留 provider/model 等标识，不包含实际 base URL。配置文件 hash 能证明期望值；它不能证明真正发送了哪个 URL。[metadata 清洗](https://github.com/NousResearch/hermes-agent/blob/a382af23f4c067d23463b5781958684c2cda82bc/gateway/platforms/api_server.py#L1990)

本仓库的受控 profile 增加 [transport_audit.py](../deploy/qualification/hermes/transport_audit.py)，在实际 httpx sync/async send 观察响应对应的真实 request：仅保存目标 URL 的 SHA-256、实际 JSON model 的 SHA-256、method、响应状态、重定向计数和网络错误布尔值。它不改写、转发或重试请求，不记录 key、headers、prompt、body 全文或 URL 原文。每个 Gateway/Worker 进程安装一次；文件为固定工作区内的 0600、有界、加锁追加记录，并受 image recipe hash 约束。它属于 Harniqo 资格验证 profile，不是上游原生 endpoint-reporting 能力。

[独立 model-lock 探针](../scripts/qualification/probe_hermes_model_lock.py) 在一个新工作区执行一次不断线的原生两工具任务。只在最终实际 metadata 匹配，且全部传输记录匹配期望 endpoint/model、至少三次 HTTP200、零重定向/网络错误时通过。配置回显、未报告、异常值和部分留证均不放宽为通过。原始 canonical 报告仍为失败；两个模型的实际新用例由 [M0 证据索引](../docs/planning/M0-RESULTS.md) 另行登记。

组件测试使用内存 SSE、真实临时文件，以及安装了 SDK 时的真实 httpx MockTransport；不启动 HTTP listener、不调用模型。SDK 项在没有 httpx 的环境中会明确跳过，必须在固定运行包复跑零跳过后才能声称 SDK 观察器已验证。组件结果不能替代实际模型、通用工具审批、取消执行确认或生产 VM 隔离。

## 2026-10-29 真实运行结果

当前受控运行包 image ID 为 `sha256:75bb26db4828c79a92565bcf00b61ea40d4da35d807007f50a9f91fe7ffb7e52`，保留同一上游源码，明确包含 Harniqo 工具 profile 与传输观察器。14 项组件测试在该 image 内以 Python3.14.10/httpx0.28.1 运行，全部通过、零跳过；容器禁网，只挂载四个公开源文件的只读副本，没有挂载凭据。

| 独立原生用例 | 实际最终模型 / provider / lock | 两工具、回执、文件、历史及终态 | 实际传输观察 | 原生报告用量 |
| --- | --- | --- | --- | --- |
| DeepSeek 配置 | `deepseek-v4.1-flash` / `custom` / `confirmed` | 通过；37 个有序 session SSE 事件 | 3 次 HTTP200；目标与实际 body.model hash 均匹配；零重定向/网络错误 | 9031 tokens |
| Qwen 配置 | `qwen3.8-flash` / `custom` / `confirmed` | 通过；39 个有序 session SSE 事件 | 3 次 HTTP200；目标与实际 body.model hash 均匹配；零重定向/网络错误 | 9512 tokens |

上述为两个新工作区的真实原生执行，不是模型协议探针。DeepSeek 的原生探针通过后，外层 operator 的旧端口检查失败；它已确认原生 PID 为 0、容器移除及日志完整。后续补充观察证明端口无 LISTEN，普通 bind 和 SO_REUSEADDR bind 均成功。没有直接捕获失败当时的 TIME_WAIT 状态，不能把该原因写为实测事实；原失败报告保留。Qwen 在修正后的检查下，原生用例和外层停止、移除、日志、端口释放均通过。

DeepSeek 配置在重建同 image 的容器并保留原 home 后，7 条原生历史与原报告的完整 hash 一致；实际 `docker restart` 后再次一致，产物 hash 不变。之后用不含旧 nonce/receipt 值的新提示发起一次真实 locked-session 续聊，正确复述原随机回执和 nonce，没有调用工具，旧历史前缀及产物保持不变；本次原生报告 3333 tokens。恢复与续聊只向各自新报告追加证据，不覆盖此前用例。

Qwen 配置也在重建容器及随后实际 `docker restart` 后，分别核对原 7 条历史的完整 hash 和产物 hash，均保持一致。此项没有再调用模型；没有据此声称第二次真实续聊通过。

同时发现一个必须声明的能力限制：locked-session 流结束后，它的 Run 可以在原 Gateway 进程中轮询；Gateway 重建或重启后，旧 Run ID 的轮询实测 HTTP404，而会话历史恢复和上述续聊可用。第一次把旧 Run 轮询当作恢复必要条件的报告保持失败，后续报告分别记录历史恢复通过与旧 Run 轮询不可恢复，不能统称全部恢复通过。

**源码推断**：该 session-stream 入口只给 `_run_owners` 写内存归属，调用执行时未传 `run_owner_scope`；重启后缺失内存归属，`owns_api_run` 的持久归属检查要求 admission payload 中有此字段，否则拒绝。`/v1/runs` 的 admission 路径会传入它。该推断解释了 404，尚未对上游源码做补丁，不把推断当作数据库现场证明。[stream 启动与执行参数](https://github.com/NousResearch/hermes-agent/blob/a382af23f4c067d23463b5781958684c2cda82bc/gateway/platforms/api_server.py#L3120)、[持久归属检查](https://github.com/NousResearch/hermes-agent/blob/a382af23f4c067d23463b5781958684c2cda82bc/gateway/session_api_turn.py#L257)、[Run 授权回退](https://github.com/NousResearch/hermes-agent/blob/a382af23f4c067d23463b5781958684c2cda82bc/gateway/platforms/api_server_runs.py#L1162)

生产 Adapter 必须把平台自己的 Run、最终状态和原始 native ID 持久保存；恢复原生历史、创建新的原生续聊、恢复旧 Run 的轮询是三种分别声明的能力。平台能够保留历史结果，并不表示旧 native Run 在上游也可重新轮询或续跑。具体任务状态及私有证据索引见 [M0 结果](../docs/planning/M0-RESULTS.md)。
