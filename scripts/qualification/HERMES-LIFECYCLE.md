# Hermes 原生取消与审批拒绝验收

`probe_hermes_lifecycle.py` 在固定 Hermes 源码 `a382af23f4c067d23463b5781958684c2cda82bc` 与明确登记的四工具 qualification profile 上，验证**运行中的实际停止**和**原生人工审批拒绝**。它调用真实原生接口、原生工具调度、原生中断标记及人工审批队列。受控工具只操作固定合成文件；结果不代表通用终端、任意文件、生产 VM 隔离或供应商计费停止。

构建和容器生命周期由 [运行包说明](../../deploy/qualification/hermes/README.md) 定义。操作者须使用正确 image ID 和相符 profile provenance，为每个模型使用全新的私有 home/workspace。这里的探针不启动容器，不设置模型，不读取模型 API key，也不接受任意服务 URL。API token 从私有0600文件读入请求头；HTTP 始终是授权端口范围中的字面 `127.0.0.1`，禁代理和重定向。

```bash
df -h .
python3 scripts/qualification/probe_hermes_lifecycle.py \
  --root "$PWD" \
  --container OWNED_QUALIFICATION_CONTAINER \
  --provenance images/containers/hermes/git-a382af23f4c0/sha256-IMAGE_ID.docker.tar.provenance.json \
  --token-file .private/qualification/CASE/api-token \
  --workspace .private/qualification/CASE/workspace \
  --expected-model APPROVED_MODEL_ID \
  --report .private/qualification/CASE/lifecycle-report.json
```

默认依次验证取消和审批，两项使用各自新建的原生 Session/Run。可以用 `--cases cancellation` 或 `--cases approval` 单独执行；不能把单项通过写成另一项也通过。报告文件必须是新路径，四个 lifecycle marker 必须不存在。前一个正常工具闭环产生的 `qualification-result.json` 可以存在；探针会保存并复核其 SHA-256 不变。

## 运行中取消

1. 请求模型真实调用一次 `qualification_wait`，不使用“先发停止、假定任务正在运行”的方式。
2. 工具在实际执行线程写入固定 `qualification-wait-started.json`，包含 nonce、不可预测 receipt、所在 worker 的 PID 和 Linux `/proc/self/stat` start_ticks。模型不能选择文件路径或这些身份值。
3. 宿主在同一容器内独立检查该 PID 的 kernel start_ticks 与非 zombie 状态，确认工具实际开始、该 worker 尚存活且45秒自然完成 marker 尚不存在。读取第一条真实 SSE 事件后主动关闭连接，再确认实际 worker 存活，然后调用原生 `POST /v1/runs/{id}/stop`。接口回复 `stopping` 只代表请求已接受。
4. 工具最多等待45秒，每0.1秒检查上游 `tools.interrupt.is_interrupted()`，发现原生中断后写入相同身份的 `qualification-wait-interrupted.json` 并返回。自然超时只写 `qualification-wait-finished.json`，不会被当作取消。
5. 原生 Run 必须收敛为 `cancelled`；用第一条事件游标作为 `Last-Event-ID` 重连，序号必须连续、无重复，SSE 必须有真实工具开始和最终 `run.cancelled`。中断 marker 必须与开始 marker 全量匹配，自然完成 marker 必须不存在。
6. 原生 Session 历史必须保存唯一真实调用的 ID、原 nonce，以及工具函数完整返回的 `native_interrupt_observed` JSON；不可预测 receipt、PID、start_ticks 必须与两个 marker 全量匹配。固定源码的 `run_agent.py` 对单调用选择 sequential 路径，`agent/tool_executor.py` 在该结果产生前等待 Future 完成，随后在 finally 中 `executor.shutdown(wait=True)`，因此这里验证的是实际工具调用返回和执行器 join。合成的 abandoned/cancelled 文字、部分结果或伪造 receipt 均拒绝。

这条 canonical API 路径可以在长期运行的 gateway 进程中执行。宿主再次记录该 kernel 进程身份的存活观察，但不要求为取消一个 Run 杀掉整个 gateway。native Run 终态、完整函数 Future 返回及执行器 join 与网关进程退出是不同事实。最初把 gateway PID 仍存活判为取消失败的记录应保留，修正判据必须由源码和真实历史共同支撑。

探针不自行 kill native worker。失联、`unknown`、自然完成、缺中断 marker 或缺少真实函数返回都会失败并保留阶段，不把停止请求当作停止确认。对于非配合工具、合成 abandoned 结果或外部副作用，本用例没有证明通用停止保证。

原生依据：[工具线程中断](https://github.com/NousResearch/hermes-agent/blob/a382af23f4c067d23463b5781958684c2cda82bc/tools/interrupt.py)、[单调用调度](https://github.com/NousResearch/hermes-agent/blob/a382af23f4c067d23463b5781958684c2cda82bc/run_agent.py#L1307-L1332)、[调度器中断传播及 Future 收敛](https://github.com/NousResearch/hermes-agent/blob/a382af23f4c067d23463b5781958684c2cda82bc/agent/tool_executor.py#L904-L1004)、[canonical Run 停止](https://github.com/NousResearch/hermes-agent/blob/a382af23f4c067d23463b5781958684c2cda82bc/gateway/platforms/api_server_authority_runs.py)、[managed worker 生命周期](https://github.com/NousResearch/hermes-agent/blob/a382af23f4c067d23463b5781958684c2cda82bc/gateway/session_managed_worker.py)。

## 人工审批拒绝

`qualification_approved_write` 在产生固定合成 marker 前调用上游 `tools.approval.request_tool_approval`，使用其人工队列、通知、选择和拒绝语义；Harniqo 没有替换原生审批运输或写自己的自动审批器。只有上游明确返回 `approved is True` 才允许写入 `qualification-approved-write.json`。profile 固定 manual 模式、45秒审批超时及 unattended deny；本用例只提交 deny，不提交 once/session/always。

实际验证：

- 模型真实调用该工具，Run 进入 `waiting_for_approval`；canonical `pending_controls` 必须报告唯一 approval、非空原因、精确 request ID、当前 execution generation 和 deny 选择。marker 此时必须不存在。
- 用错误 request ID 发 deny，接口必须拒绝；原请求仍待审批且 marker 不存在。再用正确 request ID 和错误 generation 发 deny，也必须拒绝且不改变当前请求。
- 用当前原生 request ID 和 execution generation 提交 deny，必须确认该请求 resolved。
- 原生 Run 正常完成拒绝后的回复。SSE 包含原生 `approval.request`，历史包含真实工具调用的 ID、原 nonce 及工具返回的 denial；没有其他工具调用。固定 marker 仍不存在，原正常工具产物 SHA 不变。
- Run 完成后重发旧审批必须被拒绝，不能把旧权限用于新执行。

canonical 请求体必须恰好包含 `request_id`、`execution_generation` 和 `choice`；legacy 形状不能用于 canonical 分支。未出现真正待审批请求、无人模式自动拒绝、鉴权失败或工具参数非法都不计作本项通过。

原生依据：[request_tool_approval 人工闸门](https://github.com/NousResearch/hermes-agent/blob/a382af23f4c067d23463b5781958684c2cda82bc/tools/approval.py)、[API ask bridge 测试](https://github.com/NousResearch/hermes-agent/blob/a382af23f4c067d23463b5781958684c2cda82bc/tests/tools/test_request_tool_approval.py)、[generation 绑定的 canonical 响应](https://github.com/NousResearch/hermes-agent/blob/a382af23f4c067d23463b5781958684c2cda82bc/gateway/platforms/api_server_authority_runs.py)、[待审批队列与响应](https://github.com/NousResearch/hermes-agent/blob/a382af23f4c067d23463b5781958684c2cda82bc/gateway/session_pending_controls.py)。

## 留证、截止与测试范围

执行前还会验证相同原生接口拒绝缺失和错误的 API token。宿主逐阶段原子保存私有报告；只记录身份、阶段、计数、hash、类型化失败、经过筛选的用量与模型观察，不保存模型原始消息、原始 SSE、URL、密钥或 Authorization。实际私有容器日志由启动器限量保存。`milestone_complete` 和 `saas_isolation_passed` 始终 false。

父进程默认240秒截止（可设30–300秒）；它结束自身的 HTTP worker 进程组，不确认远端 Run、工具、模型请求或计费已经停止。失败或超时后仍须由启动器核对 Run/实际进程并完成所属容器清理，不能删除未知资源或别人的容器。单个 HTTP/SSE 响应沿用 [原生探针](HERMES-PROBE.md) 的容量和 socket inactivity 上限。

组件测试用真实临时文件与原生接口替身验证本 profile 的约束和留证判断，不调用模型、原生 gateway、Docker 或 VM：

```bash
df -h .
PYTHONDONTWRITEBYTECODE=1 python3 scripts/qualification/test_hermes_lifecycle.py -v
```

组件通过不能替代实际原生用例通过；模型 ID/端点一致性也由独立模型证据验证，本探针只记录观察，不用请求配置填充缺失的实际模型元数据。

## 2026-10-29 实测记录

在指定 Linux 服务器、固定源码及四工具 image store ID `sha256:75bb26db4828c79a92565bcf00b61ea40d4da35d807007f50a9f91fe7ffb7e52` 上，两次独立的真实原生用例通过：审批拒绝覆盖错误 request ID、错误 generation、终态后重放（均409）、精确拒绝和无副作用，原生报告5751 tokens；运行中取消覆盖真实工具开始、原生中断、完整函数 Future 返回、终态和真实 SSE 游标补取，原生报告2747 tokens。该次停止到确认耗时0.286秒，只是单次观察。网关进程在取消后仍存活，未将它当作未停止的 Run；容器最终均另行确认停止、回收，限量日志保留，所用宿主端口释放。11项组件测试也在服务器通过。

首次真实取消运行因旧判据要求长期网关 PID 退出而记录失败；原始失败保留，另追加源码与持久原生工具结果的核对记录，修正后再用全新用例通过。本段只声明上述两个用例，审批后的原生续聊、具体模型/端点一致性及完整 M0-03 状态仍以 [M0 证据索引](../../docs/planning/M0-RESULTS.md) 的独立证据为准。
