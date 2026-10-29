# M0 Harness 候选准入复核与可执行顺序

复核日期：2026-10-29。此报告使用官方源码、发行 API、官方 npm registry 和官方文档；研究阶段没有启动候选服务、没有调用模型、没有安装宿主依赖。随报告交付的 OpenCode 准备/构建配方及离线测试是工程材料，不将候选写成已通过 M0-04。后续实际构建、运行和失败证据仍以私有 attempt、provenance 与 M0 证据索引为准。

## 当下决策

**受限准入预算优先用于 OpenCode 原生服务，其次补齐 Hermes 两模型闭环，再做 OpenClaw。** 建议正式目标仍为 Hermes、OpenCode、OpenClaw，但只能在各自运行证据齐全后锁定。Octop 完整服务另列产品接入项，Octop Harness 另列嵌入运行时；DSH 暂保留 preview，不能因为 SDK 文档丰富而宣称已具备逐任务原生取消。

OpenCode 的本次优势是已有约80 MB的官方自包含 Linux 二进制、原生 HTTP/会话/审批接口、捆绑的自定义模型 adapter，以及可核实的 MIT 源码和固定发行身份。目标服务器没有 Node 不构成阻塞。采用固定 Python 基镜像只承担凭据读取和启动，不安装 Python 模型 SDK，也不重新实现 Agent loop。这是当前时间/带宽/模型额度限制下的实施排序判断，并非效果、安全或成熟度排名。[官方发行](https://github.com/anomalyco/opencode/releases/tag/v1.19.5)、[固定构建脚本](https://github.com/anomalyco/opencode/blob/810c0eba1a7348036f8bdf61e6632d0c36c6876c/packages/opencode/script/build.ts)、[原生 provider](https://github.com/anomalyco/opencode/blob/810c0eba1a7348036f8bdf61e6632d0c36c6876c/packages/opencode/src/provider/provider.ts)

## 身份重新固定

| 对象 | 本次身份 | 许可与定位 |
| --- | --- | --- |
| OpenCode | 正式 immutable release `v1.19.5`，2026-10-28；tag 直接指向 `810c0eba1a7348036f8bdf61e6632d0c36c6876c` | [MIT](https://github.com/anomalyco/opencode/blob/810c0eba1a7348036f8bdf61e6632d0c36c6876c/LICENSE)，独立 CLI/HTTP runtime；本轮优先 |
| OpenClaw | 正式 `v2026.10.8`，2026-10-27；`dc2523e5a5c7a309517fd7fa834add938231d526` | [MIT](https://github.com/openclaw/openclaw/blob/dc2523e5a5c7a309517fd7fa834add938231d526/LICENSE)，Gateway/完整助手 runtime；不沿用旧报告的 v2026.10.7 身份 |
| Octop 服务 | `v1.0.3b1`；`890111b7879665840414ca28ab168b05b0db0921` | [MIT](https://github.com/TencentCloud/Octop/blob/890111b7879665840414ca28ab168b05b0db0921/LICENSE)，FastAPI/工作台/身份/会话等完整产品 |
| Octop Harness | `v1.0.5`；`76cd38d7b8ffc66c28ba24ad60bbab206a95d3ac` | [MIT](https://github.com/TencentCloud/octop-harness/blob/76cd38d7b8ffc66c28ba24ad60bbab206a95d3ac/LICENSE)，Python 原生 loop 组合库/CLI/stdio ACP，独立于 Octop 服务 |
| DSH | 本次 master 快照 `f3803c96974ad4a94195fb97e5c637e9c2a3afe3`，2026-10-27；不冒充固定正式 release | [官方 README/MIT](https://github.com/deepseek-ai/deepseek-harness/blob/f3803c96974ad4a94195fb97e5c637e9c2a3afe3/README.md)，官方明确 developer preview 和兼容破坏预告 |
| HarnessRouter | `v0.37.10`；`3fa39ad1947f9a3d1526e7d1814399498f7b1be0`，2026-10-29 | [Apache-2.0](https://github.com/HarnessRouter/harnessrouter/blob/3fa39ad1947f9a3d1526e7d1814399498f7b1be0/LICENSE)，网关/适配复用候选；不能把其行为计成外部 Harness 原生能力 |

各 tag 身份来自官方 Git ref，annotated tag 已 peel：[OpenCode](https://api.github.com/repos/anomalyco/opencode/git/ref/tags/v1.19.5)、[OpenClaw](https://api.github.com/repos/openclaw/openclaw/git/ref/tags/v2026.10.8)、[Octop](https://api.github.com/repos/TencentCloud/Octop/git/ref/tags/v1.0.3b1)、[Octop Harness](https://api.github.com/repos/TencentCloud/octop-harness/git/ref/tags/v1.0.5)、[HarnessRouter](https://api.github.com/repos/HarnessRouter/harnessrouter/git/ref/tags/v0.37.10)。许可结论仅针对对应源码，捆绑依赖、镜像、模型服务条款和品牌应分别核对。

## OpenCode：立即能准备的原生路径

### 公开二进制与供应链

官方 [release metadata](https://api.github.com/repos/anomalyco/opencode/releases/tags/v1.19.5) 的 `opencode-linux-x64-baseline.tar.gz` 为79,651,821 bytes，SHA-256 `17c200fd4b2289954cc01552559f9b6d03ec157b2ddfb5eb54144f491656333d`；immutable release 的 tag 直接指向上述 commit。baseline/glibc 可在目标 Linux amd64 上避免 AVX2 要求。原生 [Dockerfile](https://github.com/anomalyco/opencode/blob/810c0eba1a7348036f8bdf61e6632d0c36c6876c/packages/opencode/Dockerfile) 自身也是复制已编译 binary，另加系统库和 ripgrep，并不要求服务端运行 Node。

考虑 GitHub asset 实际带宽不稳定，提供显式 `--binary-source npm-package`。官方 [opencode-ai@1.19.5 metadata](https://registry.npmjs.org/opencode-ai/1.19.5) 列出同版 `opencode-linux-x64-baseline` optional dependency；固定 [dispatcher](https://github.com/anomalyco/opencode/blob/810c0eba1a7348036f8bdf61e6632d0c36c6876c/packages/opencode/bin/opencode) 和 [postinstall](https://github.com/anomalyco/opencode/blob/810c0eba1a7348036f8bdf61e6632d0c36c6876c/packages/opencode/script/postinstall.mjs) 也指向平台包的 `bin/opencode`。本组合只下载 [固定 npm tarball](https://registry.npmjs.org/opencode-linux-x64-baseline/-/opencode-linux-x64-baseline-1.19.5.tgz)，核[registry metadata](https://registry.npmjs.org/opencode-linux-x64-baseline/1.19.5)的完整 SRI `sha512-9kA+st3XXtCXMPx5f3Ld1u19uoHTO2u2Btt8a2cqhJ4GIqVbD1mXLevj3LHFEmtuQoFGk+UstGnl38sCqXj5sQ==`，解出ELF及包声明；不执行 Node、npm install 或 postinstall。metadata 显示 GitHub Actions trusted publisher 和 maintainer thdxr。该事实不等于本轮独立核验了 npm 签名或供应链 attestation。

两个 archive 采用各自完整 pin；除非实际比较解出的binary，否则不声称两种来源逐字节相同。源码仅通过固定 commit 及其 LICENSE/package.json/bun.lock Git blob 核验关联，不能由 tag、版本字符串或上游 CI 脚本存在推导二进制可复现构建。

原生 [ripgrep finder](https://github.com/anomalyco/opencode/blob/810c0eba1a7348036f8bdf61e6632d0c36c6876c/packages/core/src/ripgrep/binary.ts) 会优先用 PATH 中 `rg`，否则下载15.1.0；其fallback没有本组合的完整SHA校验。因此提前固定 [BurntSushi 官方15.1.0 musl tar](https://github.com/BurntSushi/ripgrep/releases/download/15.1.0/ripgrep-15.1.0-x86_64-unknown-linux-musl.tar.gz)，SHA-256 `1c9297be4a084eea7ecaedf93eb03d058d6faae29bbc57ecdaf5063921491599`，保留三份上游许可材料，避免运行中动态安装。

2026-10-29实施前置记录：服务器首轮已下载并按SRI缓存官方npm二进制，随后对上述GitHub ripgrep输入的读取超时；第二次仅复用既有npm缓存、延长socket界限后，仍在该ripgrep输入超时，尚未进入Docker构建。两个原始失败日志均保留。随后在本机从完全相同的官方URL取得2,263,077bytes、完整SHA匹配的ripgrep，以及Git对象匹配的三份源码材料，仅将这些公开字节传入服务器任务缓存；来源、版本和pin均未改变。这是下载传输恢复，不是模型、工具或运行准入成功证据；真实构建结果另记provenance。

### 精确模型语义与成本控制

`provider.<id>.npm:"@ai-sdk/openai-compatible"` 对应 `/chat/completions`；`options.baseURL` 是 API 前缀，`options.apiKey` 支持 env 引用；`models`的 key/可选`id`决定实际模型，`model`采用`provider/model`。该 SDK 已捆绑。`@ai-sdk/openai`走另一套原生 OpenAI 适配，不能与兼容 Chat Completions 混配。源码保留 providerID、内部 modelID、api.id 等不同身份，验收要比对请求与原始模型响应，而不只看 UI 名称。[固定 provider 配置与加载源码](https://github.com/anomalyco/opencode/blob/810c0eba1a7348036f8bdf61e6632d0c36c6876c/packages/opencode/src/provider/provider.ts)

本组合模型配置只含`base_url/model/context/output/interleaved`，key放独立只读文件，仅进原生进程环境；配置本身使用`{env:HARNIQO_MODEL_API_KEY}`。固定`model/small_model/enabled_providers`，关闭标题、summary和自动compaction，最多4步、1024输出token、每模型HTTP请求timeout60秒。title是实际额外模型调用，固定非默认session title和禁title Agent有源码依据。[标题生成](https://github.com/anomalyco/opencode/blob/810c0eba1a7348036f8bdf61e6632d0c36c6876c/packages/opencode/src/session/prompt.ts)、[Agent配置](https://github.com/anomalyco/opencode/blob/810c0eba1a7348036f8bdf61e6632d0c36c6876c/packages/opencode/src/agent/agent.ts)

关闭更新、模型目录fetch、默认插件、外部skills、LSP下载和项目配置；原生config仍尝试初始化插件包，因此用真正不可写的XDG config目录触发原生[Npm.install的不可写退出](https://github.com/anomalyco/opencode/blob/810c0eba1a7348036f8bdf61e6632d0c36c6876c/packages/core/src/npm.ts)。不能只设置`plugin:[]`就承诺零运行下载。环境knobs分别在[Flag](https://github.com/anomalyco/opencode/blob/810c0eba1a7348036f8bdf61e6632d0c36c6876c/packages/core/src/flag/flag.ts)和[RuntimeFlags](https://github.com/anomalyco/opencode/blob/810c0eba1a7348036f8bdf61e6632d0c36c6876c/packages/opencode/src/effect/runtime-flags.ts)。

输出/步数/超时减少一次准入的成本，不是账单硬封顶；重试、系统上下文、usage缺失与实际价格仍需记录。首个合成工具任务通过后再进行第二模型，不在基础启动失败时消耗模型额度。

### 控制接口和交付

原生`opencode serve`提供HTTP/OpenAPI，默认4096/loopback；服务端 Basic密码来自`OPENCODE_SERVER_PASSWORD`。本组合容器内绑定4096，仅在获准宿主环回端口映射。[原生serve命令](https://github.com/anomalyco/opencode/blob/810c0eba1a7348036f8bdf61e6632d0c36c6876c/packages/opencode/src/cli/cmd/serve.ts)、[认证](https://github.com/anomalyco/opencode/blob/810c0eba1a7348036f8bdf61e6632d0c36c6876c/packages/opencode/src/server/auth.ts)

可按原生session/prompt_async/event/message接口接入；abort boolean仅说明调用返回，停止确认需要状态、事件与实际副作用证据。`GET /permission`、`POST /permission/{id}/reply`是固定版入口，旧session permission路径已deprecated。SSE不默认提供与Harniqo持久事件等价的重放契约，应从权威历史对账。[session API](https://github.com/anomalyco/opencode/blob/810c0eba1a7348036f8bdf61e6632d0c36c6876c/packages/opencode/src/server/routes/instance/httpapi/groups/session.ts)、[permission API](https://github.com/anomalyco/opencode/blob/810c0eba1a7348036f8bdf61e6632d0c36c6876c/packages/opencode/src/server/routes/instance/httpapi/groups/permission.ts)

材料落在[准备脚本](../scripts/qualification/prepare_opencode.py)、[构建/启动说明](../deploy/qualification/opencode/README.md)、[离线边界测试](../scripts/qualification/test_prepare_opencode.py)。使用既有固定Linux amd64基镜像、公开输入小context、Docker`--network=none`构建；不在宿主安装Node，不用浮动apt或远程shell。构建只验证版本和动态链接，归档与provenance分别保存实际image store ID、build result ID及archive SHA。完整许可证/SBOM、真实工具循环、审批、取消、续聊和VM隔离须另行验收。

## OpenClaw：第二个原生远程控制参照

新release v2026.10.8 的[package.json](https://github.com/openclaw/openclaw/blob/dc2523e5a5c7a309517fd7fa834add938231d526/package.json)要求Node`>=24.22.1 <25 || >=26.12.1`、pnpm12.15.0。官方[Dockerfile](https://github.com/openclaw/openclaw/blob/dc2523e5a5c7a309517fd7fa834add938231d526/Dockerfile)已经固定Node24.22.1镜像digest；应在容器/VM内使用锁定运行包，不能因宿主缺Node而临时全局安装不明版本。本轮未解析其正式registry镜像manifest或下载完整依赖闭包，因此没有准备好可运行镜像的证据。

自定义模型定义在`models.providers.<provider>`：`baseUrl`（注意小写l）、`apiKey`/SecretRef、`api:"openai-completions"`、`models:[{id,...}]`，Agent选择`provider/model`。Responses须明确`openai-responses`。默认merge会与agent models.json、插件目录交互；本轮宜单独HOME并用`models.mode:"replace"`固定实际catalog。远程compatible端点默认不请求streaming usage，需要其真实支持时显式声明`compat.supportsUsageInStreaming:true`；缺失usage不能算0。[固定custom provider文档](https://github.com/openclaw/openclaw/blob/dc2523e5a5c7a309517fd7fa834add938231d526/docs/gateway/config-tools/custom-providers.md)

接统一客户端优先Gateway WS，保留session/run标识与角色scope。官方客户指南列`operator.read/write/approvals`；exec approvals有list/requested/resolved与resolve，断线后重新订阅并读取chat.history。不是简单包一个OpenAI兼容聊天endpoint。[客户端契约](https://github.com/openclaw/openclaw/blob/dc2523e5a5c7a309517fd7fa834add938231d526/docs/gateway/clients.md)、[原生审批](https://github.com/openclaw/openclaw/blob/dc2523e5a5c7a309517fd7fa834add938231d526/docs/gateway/protocol/operator-methods.md)

建议用独立Gateway进程和工作区作为最小运行实体；首轮禁channels/browser/cron等额外面，仅查建session、工具、审批、原生停止和重连。Gateway是受信操作边界，其token/scope不自动提供Harniqo跨组织执行隔离；正式支持仍要求运行证据。

## Octop必须拆成三个身份

1. **完整服务**：`TencentCloud/Octop`的FastAPI/工作台/用户系统。官方[README](https://github.com/TencentCloud/Octop/blob/890111b7879665840414ca28ab168b05b0db0921/README.md)给出`octop run --host ... --port ...`及Docker路径；本轮若准备则用固定wheel+完整冻结闭包，不照抄浮动`pip install octop`或安装shell。Python≥3.12，单进程in-process处理器，控制面SQLite/Postgres与工作区存储分开。服务API支持`POST /providers`的`kind/base_url/api_key/model`；chat WebSocket有explicit cancel，断线本身不取消；HITL resume提供独立恢复入口。[固定API](https://github.com/TencentCloud/Octop/blob/890111b7879665840414ca28ab168b05b0db0921/docs/api.md)
2. **底层Harness**：`TencentCloud/octop-harness`实际装配DeepAgents/LangGraph，提供stream/cancel/history/resume_hitl；与服务用户/JWT/调度无关。自定义`ProviderConfig(id,base_url,api_key,protocol:"openai")`，default_model是`provider/model`；每模型`api_mode`显式Chat Completions或Responses，默认无endpoint fallback。README同时说明HITL暂停上下文process-local，宿主负责授权resume caller。[README](https://github.com/TencentCloud/octop-harness/blob/76cd38d7b8ffc66c28ba24ad60bbab206a95d3ac/README.md)、[模型factory](https://github.com/TencentCloud/octop-harness/blob/76cd38d7b8ffc66c28ba24ad60bbab206a95d3ac/src/octop_harness/llm/factory.py)。`pyproject.toml`仍是Alpha、DeepAgents`>=0.7.16,<0.8`；必须冻结闭包，不以project版本号替代依赖锁。[元数据](https://github.com/TencentCloud/octop-harness/blob/76cd38d7b8ffc66c28ba24ad60bbab206a95d3ac/pyproject.toml)
3. **外部ACP委派**：入站`octop acp`和出站`acp_runner`均是stdio。Octop明确当前不提供HTTP ACP endpoint。经Octop委派OpenCode须同时登记外层会话/模型与被委派CLI身份、额外费用、权限桥和恢复限制，不计成OpenCode原生HTTP准入。[固定ACP文档](https://github.com/TencentCloud/Octop/blob/890111b7879665840414ca28ab168b05b0db0921/docs/acp.md)

官方本次release API仍为Octop1.0.3b1与Harness1.0.5。它们值得做完整服务复用与通用任务能力验证；基础准入阶段先采用已编译OpenCode，可以更快取得可比较的原生工具证据，再投入完整工作台与多依赖服务的验证。这是实施顺序判断，不是淘汰结论。

## DSH：保留preview，并纠正取消推断

官方README明确developer preview；自定义pi-ai provider可用`apiKeyEnv`、`api:"openai-completions"`/`openai-responses`、`baseURL`、显式models，并按实际兼容字段配置。模型与协议组合有明确语义，不能只写“兼容OpenAI所以都一样”。[固定模型指南](https://github.com/deepseek-ai/deepseek-harness/blob/f3803c96974ad4a94195fb97e5c637e9c2a3afe3/docs/user/guide/providers.md)

**headless和SDK的能力不能互相拼接成不存在的契约。** headless的`dsh --profile headless --json ...`提供一次性执行、持久`--session-id`续接、`--approval-policy never`拒绝需人工批准的动作，但官方明确没有live approval-response channel；JSON是受限投影而非无损session log，final/exitstatus必须同时检查。[headless bundle](https://github.com/deepseek-ai/deepseek-harness/blob/f3803c96974ad4a94195fb97e5c637e9c2a3afe3/packages/bundle/headless/README.md)

SDK通过`dsh --profile sdk`提供stdio JSON-RPC，初始化`approvalRequests:true`可桥接审批，错误/断线fail closed；`session/resume`有原生持久恢复语义。可是固定版server文档明确 **wire没有per-session close或prompt-cancel方法**，agents保留至process shutdown。关闭整个专用runtime可作为外层停止机制候选，必须保留它与原生逐Run取消的区别；共享进程内的多session不能假装单会话已停止。[SDK bundle](https://github.com/deepseek-ai/deepseek-harness/blob/f3803c96974ad4a94195fb97e5c637e9c2a3afe3/packages/bundle/sdk-app/README.md)、[SDK server及限制](https://github.com/deepseek-ai/deepseek-harness/blob/f3803c96974ad4a94195fb97e5c637e9c2a3afe3/packages/sdk/server/README.md)

因此本次不凭stdio接口存在将DSH锁成第三个正式目标；后续选定真实发行包、完整依赖、单session进程归属、停止与审批策略，再据实决定支持面。

## HarnessRouter：可复用参考，暂不插入这轮原生准入链

CE当前v0.37.10提供统一API、provider连接/自定义API格式、session/workspace/checkpoint、事件归一化。其[自托管指南](https://github.com/HarnessRouter/harnessrouter/blob/3fa39ad1947f9a3d1526e7d1814399498f7b1be0/docs/self-hosting-guide.md)仍明确一台单owner、SQLite+单volume、每session OS user而非每session container/VM，没有按费用强停，使用max_step/timeout及provider预算。不能直接当成Harniqo多租户执行面。

本次更具体的复用风险来自固定[runner/server.py](https://github.com/HarnessRouter/harnessrouter/blob/3fa39ad1947f9a3d1526e7d1814399498f7b1be0/runner/server.py)：OpenCode走`run --format json --auto --pure --thinking`，其`--auto`事先授予权限；请求经过relay做格式修复和credential替换；缺失native session时可在同workspace新建会话继续。随后事件被映成canonical Claude stream-json。这些是该网关有意的产品语义，与Harniqo要求的原生审批/身份连续性/原始事件保留不同。

建议复用其provider兼容案例、原始测试任务、失败处理知识和可独立隔离的解析函数；正式拷贝代码前逐件登记Apache来源/NOTICE并增加差异测试。不要在本轮原生模型兼容探针前插入relay，否则无法区分端点原始协议能力和网关修复效果。服务自身后续可作为单独UHP/Gateway Adapter候选，与OpenCode直接Adapter并存。[官方支持矩阵](https://github.com/HarnessRouter/harnessrouter/blob/3fa39ad1947f9a3d1526e7d1814399498f7b1be0/docs/support-matrix.md)是上游运行证据，只能作为测试线索，不替代本产品目标版本/模型/执行环境验证。

## M0-04未解除的验收条件

OpenCode下一轮最小真实任务应至少产生不可预先猜测的工具侧文件回执，并核原生工具事件、模型ID、用量和文件hash；原生审批分别允许/拒绝，拒绝无副作用；abort后核真实停止与继续；同home重启后续聊并检查历史。第二个获准模型使用独立工作区/状态，保留全部失败与重试。OpenClaw、Octop两层、DSH及HarnessRouter均需自身运行证据，不能用OpenCode成功替代。

## 2026-10-29 后续实施补证

本报告研究结束后，已执行受限OpenCode准入准备。固定官方npm/SRI、ripgrep/SHA和源码Git对象全部校验；服务器禁网构建得到镜像 `sha256:ef392b1148602b61ad4ea1e9dcca9b05337adc3c7ef9fc97434d632c69b8373c`，Docker archive为499,582,464bytes、SHA-256 `c50dd9d32477a4fc70a8e4ba0a985e6bb41688b633d77990869f7078e9e194bc`。旧镜像非root目录权限故障和原始下载/构建失败均保留；新镜像增加非root读取检查。[实际实施范围](../deploy/qualification/opencode/README.md#2026-10-29-实施结果与范围)

真实服务确认health1.19.5、缺失/错误Basic401、OpenAPI3.1与原生会话。第一付费任务因探针误解nongit项目worktree相对pattern而拒绝正确固定输出，原始失败保留；经显式授权的唯一追加任务使用权威worktree+metadata.filepath+callID核对，只批准单一输出once。其原生工具轨迹是固定输入read→固定输出write→固定输出read，全部completed；实际输出保留不在prompt中的随机challenge。

**完整自动smoke仍未通过。** 追加任务原探针超时，原因是错误等待assistant.time.end；保存的预abort历史证明其实际assistant.time.completed、finish:stop且无error，并在提交标记后11.294秒完成，至少早于deadline下界108.706秒。后续只读补证保存为 `offline-supplement.json`，原timeout report不改写；修复终态谓词与顺序检查后，12项本机/服务器组件回归通过，没有再调用模型。独立审阅还补齐了所有异常路径的best-effort abort；实际外层控制器始终有硬期限并确认本任务容器PID0后移除。

配置模型metadata为`harniqo/qwen3.8-flash`，属于native configured metadata证据，不冒充独立served-model传输证明。当时session idle快照没有保存，不能由abort后状态推导；SSE/恢复/原生Run取消、拒绝、第二模型、生产VM隔离与对外发行合规仍未完成。本轮验证的是实际原生子项，未锁定3个正式Runtime，M0-04继续IN_PROGRESS。

当前报告不关闭任何M0-04任务。源码/许可研究、可下载固定artifact、离线构建、真实模型工具回合和生产VM准入是五类不同证据。
