# OpenCode 1.19.5 原生模型发送验证

2026-10-30，在指定服务器完成M0-04的独立发送证据补充。一个获准的`qwen3.8-flash`任务经未改动的OpenCode原生SDK产生4次fetch发送，4个HTTP200均与发送记录逐项配对，实际URL及JSON请求的model摘要与批准配置相等；原生read→write→read、精确once审批、stop/completed、idle和实际产物均通过。没有执行第二个付费任务。

该结果证明固定组合实际发出了批准的模型请求，不证明供应商执行的权重身份、账单金额或生产租户隔离。此前DeepSeek/Qwen两模型工具验收、重启续聊、拒绝和取消证据保留；本报告只补新的Qwen发送证据，不追溯修改旧结果。

## 固定来源与发送seam

OpenCode `1.19.5`，固定commit `810c0eba1a7348036f8bdf61e6632d0c36c6876c`；沿用原image `sha256:ef392b1148602b61ad4ea1e9dcca9b05337adc3c7ef9fc97434d632c69b8373c`，没有重编译或覆盖归档。原Docker archive 499582464 bytes，SHA-256 `c50dd9d32477a4fc70a8e4ba0a985e6bb41688b633d77990869f7078e9e194bc`，仍位于`images/containers/opencode/1.19.5/`。新增资格脚本按只读文件挂载并独立记SHA。

固定原生[provider.ts](https://github.com/anomalyco/opencode/blob/810c0eba1a7348036f8bdf61e6632d0c36c6876c/packages/opencode/src/provider/provider.ts)先等待plugin config hook，再读取provider配置；`resolveSDK`复制provider.options，把`timeoutFetch(options)`交给捆绑的`@ai-sdk/openai-compatible`。`timeoutFetch`保留自定义fetch，继续执行上游signal组合、header/chunk timeout与SSE包装。因此观察器放在官方`provider.options.fetch` seam，底下调用原Bun.fetch；不替换Agent主循环，不架设模型代理，不改TLS、认证、URL、body或原生超时处理。

固定[plugin loader](https://github.com/anomalyco/opencode/blob/810c0eba1a7348036f8bdf61e6632d0c36c6876c/packages/opencode/src/plugin/loader.ts)支持本地file模块；[plugin初始化](https://github.com/anomalyco/opencode/blob/810c0eba1a7348036f8bdf61e6632d0c36c6876c/packages/opencode/src/plugin/index.ts)执行其config hook，[Plugin Interface](https://github.com/anomalyco/opencode/blob/810c0eba1a7348036f8bdf61e6632d0c36c6876c/packages/plugin/src/index.ts)支持tuple options。上游可能记录后忽略plugin错误，操作者因此先要求实际Bun运行生成的ready收据，再允许发prompt。固定[config.ts](https://github.com/anomalyco/opencode/blob/810c0eba1a7348036f8bdf61e6632d0c36c6876c/packages/opencode/src/config/config.ts)的离线安装配置与file模块加载已在镜像内实测。

下载原文缓存位于`.state/qualification/opencode-transport/source/`；可重建缓存不进入Git。原文字节SHA-256：

| 固定源码 | SHA-256 |
| --- | --- |
| `packages/opencode/src/provider/provider.ts` | `8d31296a58bc7955a1adb8b6d60da53a34ab7540d85644bab212a8ee27b8b69d` |
| `packages/opencode/src/plugin/index.ts` | `47c62b7cfae891d268e6b239edb0f1c46df5cb35eb11ccfd8bd4186c156024e9` |
| `packages/opencode/src/plugin/loader.ts` | `a7eba2d328a36a2486b50245ad98ef0daa769d4109c9e50470d8493b0936c4c0` |
| `packages/plugin/src/index.ts` | `7ce98ddab759903e6618b40a1c8beb1b621f0691f8f508ad2c07dba2041744e1` |
| `packages/opencode/src/config/config.ts` | `839c934f014c29a40f06cafd8bf2da1f0b2976ecd9e3e0f71b859792837ab14d` |

## Module与Interface

[transport_plugin.mjs](../deploy/qualification/opencode/transport_plugin.mjs)只导出原生plugin函数，通过`config` hook给指定provider装入[transport_audit.mjs](../deploy/qualification/opencode/transport_audit.mjs)。它们的Interface供独立服务与Octop的内层OpenCode复用：

```json
["file:///opt/harniqo/deploy/qualification/opencode/transport_plugin.mjs", {
  "auditFile": "/data/evidence/transport.jsonl",
  "providerId": "harniqo",
  "expectedModel": "qwen3.8-flash",
  "maxRequests": 6
}]
```

`maxRequests`仅接受1–6；配置必须只有指定provider及model，且使用`@ai-sdk/openai-compatible`。每个case和每层委派分别使用独立0700目录，不能把外层和内层发送预算混合。ready收据同目录、0600，包含runtime版本、UID、批准配置摘要及发送上限；同配置多次初始化复用原wrapper，异配置拒绝。没有改动全局fetch。

模块传递原参数对象、`this`、Response和异常对象；不读取response body。只检查有界JSON string/Uint8Array请求body中的model；Request stream不消费、不clone，无法确认时明确unknown。原始URL、model、headers、key、提示和响应内容均不写入审计日志；URL/model以SHA-256比较。日志使用O_NOFOLLOW、UID/权限/nlink/inode与目录检查、256KiB上限、排他锁及fsync；异常或丢失的结果不能形成通过报告。

schema 1的`request_started`包含唯一request_id、transport、method、url_sha256、expected_model_sha256、model_known及已知model摘要/匹配结果。`response`用同request_id关联status、最终response_url_sha256、redirected、network_error。网络异常记录`network_error`；超限和已持久化401/403后的后续尝试记录`request_blocked`。原生SDK重试每次都计入；没有删除或隐藏重试。

资格profile仅限制匹配配置base的路径段内POST SDK fetch调用。其他URL、GET、类似`/v1evil`的前缀不计入；底层重传、原生fetch内部redirect和供应商计费不是该预算的硬边界。调用者若更换发送实现，需要重新验证。未知模型字段仍原样发送并记录unknown，最终验收拒绝unknown；该观察器不是不可信Agent的安全隔离设施。

## 实际验证与保留证据

以下路径均相对指定服务器项目目录，原始材料保持私有。容器非root、只读根文件系统、cap-drop、no-new-privileges、2CPU/4GiB/128pid；真实模式只发布环回10012。模型key仅引用已有只读文件，未复制到本机或公开材料。

| 证据 | 结果与范围 |
| --- | --- |
| `.private/qualification/opencode-transport/baseline-20261030-a/` | 原服务health/provider成功但无observer ready，自动拒绝提交；保留红信号，未发模型 |
| `components-20261030-a/`、`components-20261030-b/` | 原生umask077使“不私有文件”测试夹具实际为0600，初次16/17和最小诊断失败保留；显式chmod建立正确夹具，没有放宽日志权限规则 |
| `components-20261030-c/`、`components-20261030-d/` | 分别17/17、增加鉴权锁止后的19/19；作为中间版本记录保留 |
| `.private/qualification/opencode-transport/components-20261030-e/operator.json` | **最终21/21**，固定binary内Bun1.4.4 / revision `38afee6c84fcf48c0edb2466aa4b27d287a59c30`，Node兼容v26.3.0、UID1001；network-none，零模型任务，审计业务日志空 |
| `.private/qualification/opencode-transport/offline-send-20261030-a/operator.json` | 禁网实际原生SDK向关闭的loopback端口发送一次；请求URL/body model摘要与配对网络异常均取得。没有模型服务或模型响应；仅证明实际SDK发送seam |
| `.private/qualification/opencode-transport/qwen-smoke-20261030-a/operator.json` | **一个真实任务PASS**；4次发送/4个HTTP200，12.547秒，原生报告15864 tokens；精确一次审批、read/write/read、产物与stop/completed/idle均通过 |

最终21项覆盖参数/this/Response/异常透明、秘密不入日志、不可消费Request stream、base匹配、redirect记录、累计预算与跨安装、鉴权失败锁止、目录/文件替换/链接/权限/大小/锁异常、结果记录失败后的拒绝、原生hook重入与配置拒绝。本机Node26.4.0也通过21项；[报告判据的3项检查](../scripts/qualification/test_opencode_transport_report.py)在本机与服务器通过。组件不代替表内的真实SDK/模型事实。

真实任务`evidence/transport.jsonl`含8条记录，逐request_id唯一配对；全部request model摘要为`da666201edb53dbb6e359b4d32e09ca8b87a976359f7cf576196f29d4adbcf93`（批准的Qwen ID），实际发送与响应URL摘要均为`32c8ee6b45d802a7eaf3f7a13a7ca7a2537a9792fa6f0a9b17970291889631c9`（服务器私有base拼接原生`/chat/completions`）。四个status均200、redirected=false、network_error=false，无缺失/多余/blocked记录。报告不公开模型端点原文。

所有上述原生容器都按精确image/attempt/container ID收回：停止后宿主PID0，前后限量日志完整保留，再移除容器；10012已释放。私有home、workspace、失败证据、key文件均保留，未做全局清理。共享cleanup若无法完整归档日志会保留已停止容器，不把证据丢失当成功。

最终真实任务记录了全部运行源SHA。复用Interface的关键文件为：

- `transport_audit.mjs`：`ef3ccbc02853ee254534e7375dfc91ed3dba551850eee103a99e52b88ad069a7`
- `transport_plugin.mjs`：`fd651e8674b50b4fd4da0b19ae96e79d74f542253a7c458cc454a93ea826b696`
- `run_opencode_transport.py`：`4836ee7462d58be597f33da878d6a51fdc571b05461d2cc5495d3fbc7fbfe864`

重跑命令与输入约束见[配方README](../deploy/qualification/opencode/README.md#2026-10-30-实际模型发送验证)。需要新的批准才能扩展本轮两次任务额度之外的真实调用；本次仅使用一次，结果充分后未重复测试。正式Adapter、durable重放、执行中崩溃恢复、非合作工具取消、升级回退与生产执行隔离仍按各自里程碑验收。
