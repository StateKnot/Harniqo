# DSH 原生 SDK 固定版资格

本报告只记录 DSH，M0-04 的整体状态与正式 Adapter 选择由统一能力矩阵维护。源码研究、发行包安装、组件检查、真实模型工具任务和生产隔离分别计证据；成功安装不能代表原生工具或恢复已经验证。

## 固定来源与运行包

固定 `deepseek-ai/deepseek-harness` 的 `f3803c96974ad4a94195fb97e5c637e9c2a3afe3`，官方 tag `dsh-v0.2.2-alpha.2` 直接指向该 commit。[官方 tag 对象](https://api.github.com/repos/deepseek-ai/deepseek-harness/git/ref/tags/dsh-v0.2.2-alpha.2)。根元数据声明 Node ≥24.9.0、pnpm 11.28.5；CLI 发布包为 `@deepseek-ai/dsh@0.2.2-alpha.2`。[根 package.json](https://github.com/deepseek-ai/deepseek-harness/blob/f3803c96974ad4a94195fb97e5c637e9c2a3afe3/package.json)、[CLI 元数据](https://github.com/deepseek-ai/deepseek-harness/blob/f3803c96974ad4a94195fb97e5c637e9c2a3afe3/apps/cli/package.json)

官方 README 明确 developer preview，并预告不兼容变更。本轮不静默更新，也不把预览改称正式支持。[固定 README](https://github.com/deepseek-ai/deepseek-harness/blob/f3803c96974ad4a94195fb97e5c637e9c2a3afe3/README.md)

官方 Python runtime-bin 文档描述封闭 executable、原生 sidecar 和可选显式下载；但 2026-10-30 实查 PyPI `deepseek-harness-runtime-bin`、`deepseek-harness-sdk` 只有至 `0.2.1rc2` 的公开包，目标 `0.2.2a2` 返回 404。该发行路径在固定版本下没有可获取的 wheel，故改走同版官方 npm 发布物，不降级。[官方 runtime-bin 设计](https://github.com/deepseek-ai/deepseek-harness/blob/f3803c96974ad4a94195fb97e5c637e9c2a3afe3/python/sdk-runtime/README.md)、[PyPI runtime 元数据](https://pypi.org/pypi/deepseek-harness-runtime-bin/json)、[PyPI SDK 元数据](https://pypi.org/pypi/deepseek-harness-sdk/json)

源 archive SHA-256 为 `798cab9a2acbc886a40ad9f7916e52f9380fbd4b51b41f685f3ed681f77e0ccb`。CLI 发布 tgz 的官方 SRI 固定在[准备脚本](../scripts/qualification/prepare_dsh.py)。该 npm 包没有 `gitHead`，tag→commit、包版本和官方 tgz SRI 是分别核对的关联证据，不能宣称有发布包到源码的可重现构建证明。[官方 npm 版本元数据](https://registry.npmjs.org/@deepseek-ai%2fdsh/0.2.2-alpha.2)

依赖使用 npm 12.2.1 在无密钥、非 root、只读根文件系统、无 cap 的可信构建容器解析并冻结；根输入仅包含生产依赖 `@deepseek-ai/dsh`，不安装源码 monorepo 的 devDependencies。保留完整 resolved URL、integrity 和平台 optional 条目。此 lock 是 **Harniqo 本次冻结的 npm 发布物组合**，不是上游 pnpm workspace tree；同时记录上游 `pnpm-lock.yaml` SHA 供审查差异。禁生命周期脚本后，必要原生 prebuild 是否实际可用由 SDK 启动证明。配方和全部固定输入见[DSH 资格目录](../deploy/qualification/dsh/README.md)。

## 原生 Interface 与预算 seam

采用真实 `dsh --profile sdk`，只通过官方 JSON-RPC stdio Interface 驱动。`initialize` 传 cwd、显式 provider/model、`maxTokens:1024` 和 `approvalRequests:true`。模型选择必须在握手时解析成功；prompt 返回 durable `messageId` 只表示入队。`session/wait` 等待该原生会话及 managed descendant idle，终态仍核 `turn/end`。`session/resume` 恢复同一持久 ID，拒绝不存在或已占用的会话，且不会重放历史为新通知。[固定协议](https://github.com/deepseek-ai/deepseek-harness/blob/f3803c96974ad4a94195fb97e5c637e9c2a3afe3/packages/sdk/protocol/README.md)、[固定 server](https://github.com/deepseek-ai/deepseek-harness/blob/f3803c96974ad4a94195fb97e5c637e9c2a3afe3/packages/sdk/server/README.md)

本组合保留原生 read/write 工具，关闭 edit、read_image、其他模型可调用工具和遥测；任何意外原生审批均明确拒绝。`approvalRequests:true` 的配置证据不代表已经做过实际允许/拒绝任务。使用固定 `llm-pi-ai` provider、`openai-completions`、显式模型目录和 `retryPolicy.maxRetries:0`，不让上游默认五次重试扩大资格预算。[provider 原生配置](https://github.com/deepseek-ai/deepseek-harness/blob/f3803c96974ad4a94195fb97e5c637e9c2a3afe3/packages/llm/llm-pi-ai/README.md)、[SDK profile](https://github.com/deepseek-ai/deepseek-harness/blob/f3803c96974ad4a94195fb97e5c637e9c2a3afe3/packages/bundle/sdk-app/README.md)

预算 seam 在真实 Node CLI 启动前 `--import` 的 global fetch observer：只接受精确配置 URL 的 Chat Completions POST、可解析 string JSON body、指定模型与 ≤1024 的输出参数，先写入持久请求记录再发出；最多六次，未知 body 不放行。记录 URL/model/body hash 和响应状态，不记录 key、headers、endpoint 或 payload。该 observer 不提供模型回答、不修复协议。另以 native SDK 的请求准备路径做 0 HTTP 自检：观察到实际 model/cap 后有意抛出组件错误，并使用 `--network none` 保证不能提交。只有该自检通过才允许付费任务。组件 fixture 与实际模型能力分别记账。[观察器](../deploy/qualification/dsh/transport_audit.mjs)、[原生探针](../scripts/qualification/probe_dsh.mjs)、[组件检查](../scripts/qualification/test_dsh_transport.mjs)

## 停止、恢复与生产边界

固定 SDK wire 没有逐 prompt cancel 或 per-session close。`shutdown` 释放整棵 runtime、持久化并退出；外层强制停止专用容器后需要核 Docker `Running:false` 和 `Pid:0`，不能从 HTTP 连接关闭推断停止。生产方案应采用单工作区/单原生会话专用 runtime 归属，公开能力保持“整实例停止”，不能宣称共享进程内逐 Run 取消。[固定协议限制](https://github.com/deepseek-ai/deepseek-harness/blob/f3803c96974ad4a94195fb97e5c637e9c2a3afe3/packages/sdk/protocol/README.md#known-limitations-and-deferred-work)、[server shutdown](https://github.com/deepseek-ai/deepseek-harness/blob/f3803c96974ad4a94195fb97e5c637e9c2a3afe3/packages/sdk/server/README.md#shutdown-and-exit)

本次是可信 Docker 资格 profile：只挂专用 home、工作区、证据和显式配置，smoke 才只读挂原服务器 API key 文件；不挂仓库、Docker socket 或宿主 HOME。容器只读根、非 root、cap-drop ALL、no-new-privileges、4GiB/2CPU/128 PID、120 秒外层期限。它不提供生产多租户 VM 隔离、租约/fencing 或供应商停止计费保证。首次构建前实查本机 73GiB、服务器 2.5TiB 可用。

## 执行记录

2026-10-30，Ubuntu Linux amd64，Node 24.22.1/npm 12.2.1。官方 CLI 真实 `--version` 为 `0.2.2-alpha.2`；SDK handshake 的 wire version 为 `0.0.1`，两者分别记录，不混为一项发行版本。724 条依赖锁记录、653 个实际安装包，CLI 全部运行路径使用同一禁生命周期脚本安装树。禁网 build 得到 image `sha256:664991f530811b5390640625ea9fc585e2e279350170eb0101447aa56946c997`；Docker archive 845,569,536 bytes，SHA-256 `114387849bc262119de2d8cf32a99357a77e4c5c8ebf6366393c3eb0c6614b94`。完整来源见 [runtime-inputs](../deploy/qualification/dsh/runtime-inputs.json)。

| 子项 | 实际结果与范围 |
| --- | --- |
| 预算/隐私组件 | 8 项组件检查通过，fake fetch，0 实际 HTTP；不能算原生模型成功 |
| 首次 SDK preflight | 失败并保留：官方 native loader 默认将已有 prebuild 复制到 noexec 的 `/tmp`，因此 dlopen 失败；不是缺失原生包 |
| 修正后的 SDK preflight | 官方 `NARB_DISABLE_NATIVE_CACHE=1` 直接从只读镜像加载固定 prebuild，保留 noexec；initialize、原生 cwd、shutdown、第二进程同 ID resume 均通过，0 HTTP |
| native transport 组件 | 真实 SDK→pi-ai→fetch 路径观察到指定模型和 cap1024 后主动抛出明确组件错误；network none，1 条 `component_intercepted`、0 实际发送 |
| 唯一真实 Qwen 任务 | `qwen3.8-flash`，1 prompt 不重试；4 次实际 HTTP200，实际 URL/body model hash 与配置一致、每次 cap1024；原生 read→write→read 三个不同 callId 均有成功结果；32 原生事件、`turn/end` completed |
| 随机产物 | `result.json` 52 bytes，SHA-256 `2c30f8e6e1f534cfbb644041bcd21e0509c1bb2c14bfa9df74f2fc84c09617c5`；challenge 来自工具读取的私有 fixture，没有放进 prompt |
| 正常停机与恢复 | 两个原生进程分别收到 shutdown 并 exit0；第二进程 `session/resume` 恢复同一 ID，无新 prompt、无新 turn；外层核 `Running:false`/PID0、保存完整日志后移除容器 |
| 历史只读补证 | 使用固定 Node 公共 zstd decoder，恢复后 9,303 bytes 原生 v4 历史内 32 个事件与初始 live wire 逐对象完全一致，含3个 tool/call；压缩文件 SHA-256 `997c16e88e40d503f74ec6b5f7158271599e6ef129ae2267f50d4bf1bd7aef61`，0 模型请求 |

历史检查最初误用固定 UID1000 而非服务器执行 UID，读取私有历史被正确拒绝。该只读检查失败保留，后续以实际 UID 修正并验证；没有放宽文件权限，也没有覆盖失败。所有原始失败与成功保持独立目录。公开索引见 [qualification-results](../deploy/qualification/dsh/qualification-results.json)，原始记录只留服务器 `.private/qualification/`。

模型发送证据是 actual outbound URL/body 的 model 身份；响应体 `model`/实际服务端后端身份未采集，仍为 unknown。4 条 provider usage 保留 input/output/cache/total 原值，不把 cache 命中与 aggregate total 简单混加，金额未知。未执行第二模型、真实审批允许/拒绝、运行中外层 kill、异常崩溃恢复或重启后新 prompt 的上下文续聊；只证明正常 shutdown 后同 ID 和全部历史恢复。SDK wire 仍无逐 prompt cancel，DSH 保持 developer preview。Docker 资格不关闭生产 VM 准入。

当前结果补齐 M0-04 的 DSH 实际 SDK 最小运行子项，不单独关闭 M0-04，也不改变正式目标选择。

## 审阅修正与证据版本

真实任务之后的静态审阅指出 Docker 环境可覆盖、builder 派发失败的回收与归档中断行为、provenance 使用可变镜像引用的风险。已收紧为固定本机 Unix daemon/清理环境覆盖、随机身份及既有完整日志回收逻辑、固定完整 image ID 与实际 OS/架构核对、partial/fsync/hash/不可覆盖发布。服务器 11 项 fake-runner 回归通过，0 Docker 调度、0 模型请求。原镜像、归档、付费任务报告及之前的源码证据索引全部保留；最终代码冻结和真实任务时点的源码索引分别记账，不重写原记录，不重复付费。

## Smoke 前置报告门槛

后续 Standards 审阅指出：原先“只有 native transport 自检通过才允许付费”的要求在操作者代码中依赖人工顺序，尚无输入门槛。实际已执行任务满足该先后顺序，原始报告与成功结果不改写。现 `smoke` 必须提供 `--admission-report`，在取 key、挂 key 和派发前核验私有完整路径、operator/probe成功和唯一0HTTP拦截标记；绑定固定 image、实际执行的 probe/observer SHA及0400源码snapshot、canonical endpoint/model hash和固定profile预算。操作脚本自身的无关修订不要求旧operator SHA相同，native运行代码、路由和预算必须保持一致。 key内容及文件引用不进入绑定。[执行器](../scripts/qualification/run_dsh.py)、[门槛实现](../scripts/qualification/dsh_support.py)、[22项组件检查](../scripts/qualification/test_dsh_operator.py)

旧 transport case缺少新绑定字段，因此被保留为历史证据而不充当新任务许可。新增 `.private/qualification/dsh-transport-admission-20261030T0702Z` 是同一固定 native probe/observer 的无key、network none、0实际HTTP自检；报告已通过新的校验函数只读消费，没有执行新的smoke。新门槛不追溯改变原任务，不把0HTTP组件测试写成原生模型成功。公开索引仅更新最终源码hash，原始operator/probe对象始终只留私有存储。
