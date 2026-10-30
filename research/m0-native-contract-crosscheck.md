# 原生运行证据与平台契约交叉核对

日期：2026-10-30。范围是M0-04候选选择与既有M0-02草稿的语义核对，不是生产Adapter、平台API或M0-02冻结验收。依据为[真实证据索引](../docs/planning/M0-RESULTS.md)、[当前矩阵](m0-runtime-evidence-matrix.md)、[canonical schema](../packages/contracts/schemas/contract.schema.json)与[原生Adapter决策](../docs/adr/0003-native-harness-adapters.md)。

## 已有定义能保留的差异

| 真实差异 | 既有契约表达 | 实现必须保留的约束 |
| --- | --- | --- |
| 平台Run、原生Session、原生turn/message不是同一身份 | `Run.id`与`NativeSessionRef.native_id`分开；绑定Harness发行、工作区、RuntimeInstance及generation | 不把OpenCode message ID、OpenClaw run ID或SDK message ID强行当作平台UUID；原生操作映射与原始事件另存，不在重连时新建平台Run |
| 原生历史可恢复，旧原生Run查询却可能404 | `NativeSessionRef.recovery_kind`区别connection_replay/native_resume/checkpoint_resume；各恢复能力独立声明 | Hermes旧locked-stream Run的404不能抹去已验证的历史/续聊，也不能宣称进行中崩溃续跑；原生ID相同不证明上下文连续 |
| 收到取消、原生turn结束、整个执行资源停止是不同事实 | `StopReceipt`把stopping/reconciling与cancelled分开；`Capabilities.stop_guarantee`分request_acknowledged/logical_turn/process_tree/workspace_vm | 原生abort成功或连接关闭不生成宿主停止证明；未知执行保持reconciling。非可信VM的正式停止按宿主资源核对，不能靠Guest自报 |
| 审批参数与归属必须精确 | `ApprovalResponse`携带Attempt、lease binding、预期request revision和parameters hash | 根据实际pending动作生成参数hash；响应前重新核对调用者授权及动作，原生错误/迟到决定不当成新批准；原生审批不是生产工具出口隔离 |
| 某组合只验证了固定工具/模型/模式 | `Capabilities`绑定Harness/Adapter release、integration_mode；每项有supported/unsupported/unknown、证据和限制 | Deployment的能力解析必须结合当前不可变配置；不得把资格profile的三个工具或合作式取消扩成任意工具支持。配置变化须重新预检，未知必需能力不能放行 |
| 在线事件观察不等于持久游标重放 | `SourceEvent`有受控stream/source_seq；`EventEnvelope`由平台持久提交后分配event_id/seq | OpenCode SSE、Gateway WS或SDK iterator不能直接冒充平台可靠事件日志；重连先核对原生权威状态，平台持久流由Execution模块保证 |
| 原生事件形状未知、消息包含额外字段 | `native.observed`使用有命名域的`RawEventPointer`和受控`FileDescriptor` | 先限大小、按归属持久保存并校验hash，再发布引用；不把任意上游对象展开进平台状态，更不能把原始私有轨迹发到公开仓库 |
| 部分usage缺失，原生成本默认显示0 | `UsagePayload.precision=unknown`时禁止amount；已知amount采用十进制字符串 | OpenClaw缺失usage、取消未结算或原生默认价格0均不能计作免费；币种、价格来源和换算口径来自已确认配置，不能由探针猜测 |

以上是字段和语义检查。Schema不能证明授权、数据库事务、租约fencing、字节实际持久化或宿主已停；这些仍按[验收矩阵](../docs/planning/ACCEPTANCE.md)在M1及以后实现、故障注入和验收。

## Octop组合的身份要求

Octop服务、Octop Harness SDK与Octop委派外部Agent使用不同资格记录。委派组合采用`delegated_composition`，平台的原生Session绑定外层拥有者；被委派的原生Session、模型、输入转换和调用归因另存组合清单与有命名域的原始事件。不得以一个outer session ID覆盖inner session ID，也不能把两层usage当作同一次请求。

当前草稿以配置/输入快照引用携带组合资料，没有声称组合清单、嵌套执行管理和费用聚合API已经实现。M4启用委派前，必须固定其完整配置schema、来源和关联约束，再以实际AC-16/AC-19/AC-21结果验收；本次最小原生委派只为M0提供可行性和边界证据。

## 状态与冻结规则

本轮原生补证分别关联候选记录；成功、失败、不支持及preview都保留具体原因，不通过改写契约把失败变成功。M0-04当前技术选择按任务退出条件已接受，支持范围仍以逐组合证据为准；M0-02仍需结合M0-05的生产执行准入再冻结。后续正式Adapter能力不能仅凭这份静态核对报告变为supported。
