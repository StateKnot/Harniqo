# M0 原生运行证据与接入决定

更新：2026-10-29。此矩阵用于选择后续正式 Adapter 的开发顺序；真实技术资格与产品实现分别记账。M0-04仍为IN_PROGRESS，AC-05/AC-16的未验范围继续保留，不能用本表替代完整验收。

## 首批开发顺序

1. **Hermes**：已有两模型实际出站身份、工具、拒绝、合作式取消、历史恢复和重启续聊证据，先实现最完整的端到端平台路径。
2. **OpenCode**：原生HTTP/会话/审批/流式取消/恢复接口已真实验证，作为独立协议和权限语义的第二种实现。
3. **OpenClaw**：官方Gateway协议4、设备签名、作用域、两模型实际工具任务和运行中取消已真实验证，覆盖WebSocket和长驻Gateway接口。

上述是正式开发目标与先后顺序，尚不是已交付的Harniqo支持声明。真正的Adapter、托管工作区、统一事件、平台授权及故障恢复由M2–M4实现并通过AC-15后发布。

## 已验证范围

| 候选与固定身份 | 本轮及既有真实证据 | 模型身份口径 | 尚不能继承的能力 |
| --- | --- | --- | --- |
| Hermes `a382af23f4c0`，受控四工具组合 | 两模型工具闭环；原生人工拒绝；合作式工具取消；两模型历史/产物重启保留，primary原生续聊 | 原生模型锁及实际HTTP目标/body模型hash均匹配 | 通用工具、非合作副作用停止、平台断点续跑；旧locked-stream Run重启查询404，断流会中断 |
| OpenCode1.19.5 / `810c0eba1a73` | 两模型read→write→read；Qwen实际重启续聊；精确原生拒绝；观察到真实SSE输出后的取消，共5例自动通过 | 原生配置metadata已核对；独立实际HTTP目标/served-model仍unknown | 原生SSE持久重放、执行中崩溃续跑、生产隔离；必须补独立模型发送证据 |
| OpenClaw2026.10.8 / `dc2523e5a5c7` | 原生配置、缺失/错误token拒绝、设备签名与scope；两模型read→write→read；Qwen真实文本后取消、原生aborted和idle | 两smoke各4次HTTP200；实际Undici路径的URL/body模型hash和对应响应逐条匹配；取消1次HTTP200 | 原生exec审批尚未运行；重启续聊、断线补取、非合作工具停止及生产隔离未验 |
| Octop Harness1.0.5 / `76cd38d7b8ff` | 固定93包SDK组合的allow/deny/合作式cancel；SQLite关闭后重构实例，历史一致；首个精确审批失败保留 | Qwen实际HTTP记录；只代表该SDK组合 | 进程重启续聊、第二模型、完整服务用户/会话层、外部ACP委派均不继承 |
| Octop服务1.0.3b1 / `890111b78796` | 固定201包禁网镜像、原生SQLite/setup/admin/认证/HTTP已通过；原生WS模型任务1HTTP200、40事件、随机echo和REST历史通过 | 与Harness分别记录，不从SDK用例推导 | 服务级审批、取消、重启、外部委派要按各自原生路径验收 |

对应证据与失败保留见[M0索引](../docs/planning/M0-RESULTS.md)、[OpenCode配方](../deploy/qualification/opencode/README.md)、[OpenClaw配方](../deploy/qualification/openclaw/README.md)、[Octop分层资格](octop-native-qualification.md)。生成取消仅证明本次原生运行终止；不承诺供应商停止计费。缺失usage与原生默认零成本不作为免费或已对账结论。

## 预览与复用决定

**DSH保留preview。** 固定官方快照明确developer preview和破坏性兼容预告；SDK wire没有per-session close/prompt-cancel，headless也没有live approval-response通道。当前只有固定源码与协议核对，没有Harniqo实际运行包/付费任务证据。后续需固定发行闭包、单会话进程归属和外层停止策略，再做真实SDK验证，不能标成已支持。[固定官方SDK限制](https://github.com/deepseek-ai/deepseek-harness/blob/f3803c96974ad4a94195fb97e5c637e9c2a3afe3/packages/sdk/server/README.md)

**Octop三个身份分别交付。** 完整服务走服务API，Harness走嵌入SDK，ACP委派另保留外层与被委派Agent的独立身份、调用、费用、转换和恢复证据。ACP实际委派尚未运行，AC-16没有整体通过；该项仍留在M0-04未关闭范围，并在M4启用及M6比较时再次验收。不得用iframe、外链或SDK成功替代完整服务/委派集成。

**HarnessRouter不作为本轮控制底座。** 可借鉴可独立隔离的协议解析和兼容案例；其自动授予OpenCode权限、relay格式修复/凭据转换、缺失原会话时新建会话等语义，会改变本项目的审批、模型身份及恢复证据。直接复用整套服务需要重做权限、事件原样留存、身份连续性和VM隔离，当前选择自建小接口的原生Adapter，避免先接入再绕过这些语义。若以后拷贝源码，逐件保留Apache来源/NOTICE并做差异测试。[固定版本研究与来源](m0-harness-candidate-qualification.md)

## 关闭M0-04前剩余工作

保留独立served-model验证缺口、Octop ACP实际委派、DSH实际SDK最小运行，以及统一能力矩阵与契约的最终交叉核对；相应失败、预览或不支持结果须明确，不能默认通过。生产VM选择性代理、租约/fencing和资源耗尽由M0-05独立负责，不能由Docker资格替代。阶段状态只按[任务退出条件](../docs/planning/BACKLOG.md)和[验收矩阵](../docs/planning/ACCEPTANCE.md)更新。
