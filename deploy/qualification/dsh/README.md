# 固定 DSH SDK 资格配方

范围：`deepseek-ai/deepseek-harness@f3803c96974ad4a94195fb97e5c637e9c2a3afe3`，官方 npm CLI `0.2.2-alpha.2`，developer preview。单独 native SDK 运行，模型证据不从其他 Harness 继承。

在指定 Linux 服务器、仓库根目录执行，先 `df -h .`。准备器只使用小型公开 npm context；不挂仓库或凭据，不安装宿主 Node，不执行 npm lifecycle scripts。

```sh
python3 scripts/qualification/prepare_dsh.py --root "$PWD"
python3 scripts/qualification/prepare_dsh.py --root "$PWD" --build
```

第一步保留 npm lock、安装日志、官方版本/tag 元数据，第二步禁网 build，核固定 base tag 前后 image ID，保存 Docker archive 与 SHA-256。目标目录为 `images/containers/dsh/0.2.2-alpha.2`；源码、npm payload 与构建 context 留在 ignored `images/sources/dsh`。镜像继承已经核验的官方 OpenClaw slim image，仅使用其 Node 24.22.1 和系统依赖作 carrier，入口明确改为 DSH，关闭继承的 healthcheck；这不是可分发的最小生产镜像。

冻结的 npm lock 与上游源码 pnpm lock 分别记录，不能把本次 semver 解析后的 npm 树称为上游 pnpm workspace tree。`runtime-inputs.json` 的 `resolved_package_count` 包括未安装的跨平台 optional 记录；实际安装数单列。

0 模型检查分三次，`--case` 每次使用新私有目录，不能覆盖旧失败：

```sh
python3 scripts/qualification/run_dsh.py --root "$PWD" \
  --case "$PWD/.private/qualification/dsh-component-UNIQUE" \
  --config "$PWD/.private/model-endpoints.json" \
  --provenance "$PWD/deploy/qualification/dsh/runtime-inputs.json" --mode component
# 再分别用全新 case 执行 --mode preflight 与 --mode transport。
```

组件检查用假 fetch 验证模型/URL/输出上限和六次预算，实际 HTTP 为零。preflight 在 `network none` 下运行真实 SDK initialize、native working-directory、shutdown、第二进程同 ID resume。transport 在同样禁网条件下让真实原生 SDK 准备一次 provider 请求，observer 记录实际 body 的 model/cap 后有意抛错；这是预算 seam 检查，不能算模型任务成功。

得到本轮明确模型任务授权后才执行 `--mode smoke --admission-report /absolute/private/transport-case/operator.json`，仍使用新 case。固定模型 `secondary/qwen3.8-flash`，至多六次 HTTP、每次输出 ≤1024、整容器 120 秒。API key 只挂服务器原始文件；不复制到构建 context、仓库或本机。prompt 一次提交，未知结果不重试。成功需要原生终态、不可从 prompt 猜测的工具文件 challenge、实际出站身份与原生工具事件证据，随后检查同 ID 的新进程恢复。

所有 runtime 启动由任务 label、image ID、完整 container ID 对账；停止核 PID 0 后保存完整日志，再移除 container，保留私有工作区、history 和证据。清理失败时保留原 container。`shutdown` 是整实例停止；逐 prompt 取消、实际审批、运行中故障恢复和生产 VM 隔离未因此通过。结果见[原生资格研究](../../../research/dsh-native-qualification.md)。

实际通过组合使用官方 `NARB_DISABLE_NATIVE_CACHE=1`，从只读镜像直接加载原生 prebuild，避免默认缓存进入 noexec `/tmp`。不需要开启生命周期脚本或增加容器 capability。真实任务已通过4次HTTP200/原生三工具/随机产物及正常重启恢复；完整逐项结果与失败留存见 [qualification-results](qualification-results.json)。历史只读补证脚本为 [inspect_dsh_history.mjs](../../../scripts/qualification/inspect_dsh_history.mjs)，不发送模型请求。

审阅后准备器与执行器均固定本机 Unix Docker daemon，清除 Docker host/context/TLS 环境覆盖；builder 使用随机 attempt，并复用已有完整日志、身份与 PID0 回收逻辑。只接受本轮资格镜像的完整固定 ID，核对安装 ID/Linux/amd64。后续镜像归档采用 partial、fsync、SHA 与不可覆盖的同目录原子发布；原已完成归档不重写。11 项 fake-runner 组件验证拒绝/未知派发/清理失败/归档中断和并发覆盖，不重新发送模型任务。

`smoke` 现在强制消费明确提供的 `--admission-report`。该报告必须来自当前用户所有、无 symlink、无组/其他权限的私有 transport case；operator/probe 均成功，并有唯一 `component_intercepted`、`actual_http_sent:false` 的实际 native fetch 自检记录。门槛核同一固定镜像、probe/observer SHA、实际只读源码 snapshot、canonical endpoint/model hash，以及固定 sdk profile/6请求/1024输出/120秒（native watchdog110秒）预算；凭据内容与引用不参与报告绑定。缺失或不匹配时，在读取模型 key、挂载 key、建立 smoke case和 Docker 派发前拒绝。实际使用的 probe/observer 字节保存为私有0400 snapshot后只读挂载。

旧的真实任务由人工先后完成前置门槛，历史成功保持原样；旧 transport 报告缺少绑定字段，不能授权新的 smoke。应先以同一源码和路由运行一次无 key、禁网、0实际HTTP 的 `--mode transport`，然后显式提供新生成的 operator.json。前置成功是执行技术条件；它不替代新的付费任务授权。本次审阅仅补门槛、执行22项 fake-runner 回归和新的0HTTP admission自检，没有新增付费任务。
