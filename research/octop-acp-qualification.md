# M0-04 / AC-16 — Octop Harness 出站 ACP 委派

日期：2026-10-30。范围是固定 Octop Harness SDK 的原生 `acp_runner` → OpenCode 原生 stdio ACP。完整 Octop 服务、旧 SDK 原生文件/HITL/cancel 资格与此组合分别登记；本文件不修改它们的既有结果。

## 原生路径与身份

| 层 | 固定身份 | 原生协议身份与会话 |
| --- | --- | --- |
| 外层 | octop-harness 1.0.5 / `76cd38d7b8ffc66c28ba24ad60bbab206a95d3ac`；SDK核心image `sha256:714e015d1c1281e1c91bd2ae67f39191b99c408eb7523b62a409831e912a73d5` | `HarnessAgent` thread；其ACP实现发出的clientInfo是`octop-harness-acp` / `0.1.0`，这个协议实现字符串不等于包版本 |
| 内层 | OpenCode 1.19.5 / `810c0eba1a7348036f8bdf61e6632d0c36c6876c`；固定image `sha256:ef392b1148602b61ad4ea1e9dcca9b05337adc3c7ef9fc97434d632c69b8373c` | 实际 `opencode acp` 子进程，ACP agentInfo为OpenCode/1.19.5，独立`ses_*` |
| 组合 | 新独立可信资格镜像，包含固定SDK的`[acp]` extras及同一OpenCode二进制 | scope=`octop-harness-acp-delegated`；无完整Octop服务进程，无HTTP提交替代ACP |

外层官方工具接受`start/message/respond/close`等动作，以outer thread与runner名持有连接。`start`调用官方ACPService，经Python ACP SDK `spawn_agent_process`启动CLI、`initialize`协商版本、`new_session`创建内层会话，随后调用`session/prompt`。它不调用OpenCode的HTTP提交接口。来源：[工具](https://github.com/TencentCloud/octop-harness/blob/76cd38d7b8ffc66c28ba24ad60bbab206a95d3ac/src/octop_harness/builtin/tools/acp_runner.py)、[服务](https://github.com/TencentCloud/octop-harness/blob/76cd38d7b8ffc66c28ba24ad60bbab206a95d3ac/src/octop_harness/acp/service.py)、[配置](https://github.com/TencentCloud/octop-harness/blob/76cd38d7b8ffc66c28ba24ad60bbab206a95d3ac/src/octop_harness/acp/models.py)。

OpenCode官方`acp`命令创建其内部loopback HTTP服务供自身SDK使用，再以`ndJsonStream`和`AgentSideConnection`承载stdin/stdout ACP。这个内部实现不是本探针对外层传输的替换。实际`session/new`返回的ACP sessionId就是其native session ID；load/resume/close能力由原生ACP service分别实现。来源：[固定CLI入口](https://github.com/anomalyco/opencode/blob/810c0eba1a7348036f8bdf61e6632d0c36c6876c/packages/opencode/src/cli/cmd/acp.ts)、[固定ACP service](https://github.com/anomalyco/opencode/blob/810c0eba1a7348036f8bdf61e6632d0c36c6876c/packages/opencode/src/acp/service.ts)、[ACP初始化规范](https://agentclientprotocol.com/protocol/v1/initialization)。

## 构建材料与边界

原SDK93包核心不含ACP extras。按同一上游`uv.lock`（SHA `1766085847229271984a3f3080fd9d2c7958f843f84c4a185b32ab4240d1c992`）新增`agent-client-protocol==0.10.1`与`psutil==7.2.2`，核官方PyPI metadata、完整hash/size后无网安装并`pip check`。不是从完整服务镜像借用另一个ACP库版本。来源：[上游extras](https://github.com/TencentCloud/octop-harness/blob/76cd38d7b8ffc66c28ba24ad60bbab206a95d3ac/pyproject.toml)、[固定上游锁](https://github.com/TencentCloud/octop-harness/blob/76cd38d7b8ffc66c28ba24ad60bbab206a95d3ac/uv.lock)。

公开材料为`deploy/qualification/octop/acp.Dockerfile`、`acp-extra.requirements.lock`、`acp-extra-provenance.json`、`acp-source-provenance.json`与`acp-profile.json`。镜像只从两个已核对ID的本地来源复制OpenCode/rg与只读配置，携带其LICENSE与输入来源；构建验证真实版本、动态库、Python包和协议版本。源码哈希清单记录本次实际阅读的官方文件，不把OpenCode运行二进制等同于自行重建的源码产物。

最终组合image为`sha256:59ecf44fd2fb4c968bf6227f9da79b30331172a4fa0bce4d99711c5356ffd4cd`。archive在`images/containers/octop/acp/sha256-59ecf44fd2fb4c968bf6227f9da79b30331172a4fa0bce4d99711c5356ffd4cd.docker.tar`，580,660,736 bytes，SHA `094e20c6d765b3d3d37f76c823f4f04210c80eb5fa41f03b8d004279a94ddca5`。Dockerfile SHA `0b7096ab9a6a001e66c93e2640dff3b57c900b4cfb1e915fe781fc8337f0e88e`，extras锁SHA `26c7594904b9adf334560a78abd9d08e49647f91746f908f977cda21fdc78767`。

首次BuildKit把裸`sha256:ID`当registry标签，构建失败并保留；后续为已验证本地ID加本任务完整hash标签，构建前后核对ID。原始归档已完整成功。最终准备器另加唯一partial、非空/hash/size、fsync、只读444及不覆盖的同目录原子提升；中断partial不成为完整归档。相关失败通过零模型组件验证，未破坏真实归档或旧scope镜像。

## 无模型真实预检

最终`bootstrap-20261030T062714Z-0257e135`在无网络、无真实凭据的固定组合容器通过：真实initialize协议1与OpenCode/1.19.5、两个不同的positive `ses_*`、session关闭与native子进程退出；observer实际在ACP子进程加载、发送日志空，未发送ACP prompt。PID0、容器移除均true。

断线检查先确认对应第二个子进程已退出，再调用`require_existing`；本固定组合抛出`ConnectionResetError`，未新增initialize/new_session/child，也未发模型。它只证明断线后的请求失败且没有重建副作用，不等于`SESSION_NOT_FOUND`语义、重启恢复或续聊成功。首次bootstrap因只预期`ACPSessionError`而失败，原记录保留；后续按实际transport错误和零副作用条件独立验证。

Octop ACP session映射保存在进程内字典；该工具的重建路径调用new_session，没有自动调用内层load_session。内层声称支持load/resume不意味着外层已实现持久映射或自动恢复。来源：[外层会话所有权](https://github.com/TencentCloud/octop-harness/blob/76cd38d7b8ffc66c28ba24ad60bbab206a95d3ac/src/octop_harness/acp/service.py)、[内层会话实现](https://github.com/anomalyco/opencode/blob/810c0eba1a7348036f8bdf61e6632d0c36c6876c/packages/opencode/src/acp/service.ts)。

## 真实用例与发送预算

任务：外层只能调用一次原生acp_runner，把读取`input.txt`并返回精确随机receipt的指令委派给内层。随机值不出现在外层提示或委派提示，只在inner workspace文件中。外层暴露工具仅acp_runner，参数精确校验runner、message、cwd及最大90秒；内层禁用附加插件、skills、MCP、title/summary、自动压缩，允许native read并拒绝外部目录/其他工具。未测试内层写审批，不把read成功当approve/reject证明。

两层Qwen分别独立计数：原版与首个修补版outer最多4次httpx SDK发送、128输出token；最后独立修补版只把outer输出提高到512。inner始终最多4次配置base路径POST SDK fetch发送、1024输出token。outer关闭SDK/运行时重试、拒绝重复body、非固定目标/模型/输出限额及401/403后发送。inner只读复用已在OpenCode1.19.5/Bun1.4.4中通过21项零模型测试的原生provider.options.fetch观察插件：保留原fetch参数与Response、持久计数及401/403锁止。其scope是配置base的POST SDK发送，不宣称底层重定向wire或供应商收费硬上限；验收要求无redirect、每条发送/响应关联一致且HTTP200。冻结plugin/audit SHA写入acp-profile。

外层流130秒；容器内GNU timeout在175秒发送TERM、再5秒KILL，运行硬上限180秒。宿主操作者创建持久exclusive reservation后才dispatch；结果未知不能自动重跑。运行非root、只读根、cap-drop/no-new-privileges、4GiB/2CPU/128pids；无宿主端口映射。清理只读复用固定SHA的旧ownership helper，按label/image/immutable ID核对，捕获完整可用日志、确认PID0后移除。真实配置/key只从服务器既有私有文件读挂载。

原版唯一真实任务 `model-20261030T063019Z-b98f90f5` **FAILED**，约7.943秒：outer 2次HTTP200、inner 2次HTTP200；outer history 4条/tool 1个、inner history 3条/native read 1个，独立thread/session和请求model/target均匹配。inner读出随机32字receipt并正确返回，outer却只得到98字工具摘要，最终也输出同样98字。预算与正常终止条件通过不改变精确receipt失败。native owner teardown记录child returncode `-9`；cleanup_errors为空、容器PID0及removed为true。原报告、全日志及reservation保留，不覆盖、不自动重试。

## 转换、额外成本与能力限制

观察器只包裹真实Python ACP SDK方法并记录原参数/返回，不替换连接、peer、协议循环或模型回复。证据分别保留outer输入、工具args、ACP text block、inner native user message和两层native history。任务结束后只读查询OpenCode自己的loopback native history/status作为补充；这两次读取不是任务提交，也不是模型请求。

官方工具把message包装为text块，不额外生成一个隐藏委派prompt；内层仍使用其自身Harness系统提示。ACPHostedClient积累assistant文本，把工具事件映成title/kind等摘要，再由tool_adapter转换成带`[assistant]`/`[tool_call]`等标记的外层工具字符串。它丢弃thought chunk，并不会把所有原生工具结构、权限、usage或错误都无损映射成外层Agent事件。来源：[client](https://github.com/TencentCloud/octop-harness/blob/76cd38d7b8ffc66c28ba24ad60bbab206a95d3ac/src/octop_harness/acp/client.py)、[文本转换](https://github.com/TencentCloud/octop-harness/blob/76cd38d7b8ffc66c28ba24ad60bbab206a95d3ac/src/octop_harness/acp/tool_adapter.py)。因此委派额外外层模型调用、内层调用与tokens分别保留，不能把外层usage当总费用。内层未暴露的provider response模型metadata明确unknown；configured model/实际请求model不推断物理权重。

此可信Docker组合不证明生产租户隔离、完整Octop服务ACP入口、委派写审批、执行中取消、崩溃续跑、durable事件重放或重启续聊。原生close的owner teardown会结束CLI子进程，实际signal/returncode需如实保留，不能仅因close返回就声称所有进程停止。依赖再分发许可证材料仍需独立完整审阅。

## 原版文本丢失定位与独立最小补丁

真实记录的零模型回放 `replay-20261030T064537Z-bfae9815` 在原固定镜像中禁网、无凭据、无ACP连接/子Agent，实际调用原 `ACPHostedClient` 与原 `_run_streaming_action`；服务返回值由**保存事件的离线fixture**提供。这是转换组件补证，不冒充新真实ACP任务。16条链路断言与8项格式fixture全部通过；operator退出0、PID0/removed true。回放报告SHA `d1ea76b85d718d6bcf0457e44c9670ca6a77fd21dc129d37e508a5e67701b01c`，原FAILED报告hash未变。

| 边界 | 字符数 | 与receipt关系 / 邻接匹配 |
| --- | ---: | --- |
| outer user | 238 | 不含receipt；含完整委派指令 |
| acp_runner message参数 → ACP text → inner user | 67 / 67 / 67 | 三者逐字相等，不含receipt |
| inner final assistant → 9个agent_message_chunk合并 → 原client.finish_prompt | 32 / 32 / 32 | 三者与receipt逐字相等 |
| 原acp_runner ToolMessage → outer final | 98 / 98 | 两者逐字相等，均不含receipt |
| 补丁组件回放 ToolMessage | 144 | 原98字摘要保留，附最终receipt且仅一次 |

原通知计数：available_commands_update 1、agent_thought_chunk 15、tool_call 1、tool_call_update 2、agent_message_chunk 9、usage_update 1。原client只向工具callback发出tool_start 1与tool_update 2；最终assistant通过 `finish_prompt()` 单独返回。原工具的 `return body or format_final_response(...)` 在3个摘要使body非空时忽略final event。回放精确证明98字等于这3个摘要拼接。因此失败是固定官方工具的文本转换丢失，已排除probe误读内容类型及inner模型/输出预算不精确。来源：[固定工具最后返回分支](https://github.com/TencentCloud/octop-harness/blob/76cd38d7b8ffc66c28ba24ad60bbab206a95d3ac/src/octop_harness/builtin/tools/acp_runner.py#L302)、[固定client](https://github.com/TencentCloud/octop-harness/blob/76cd38d7b8ffc66c28ba24ad60bbab206a95d3ac/src/octop_harness/acp/client.py)。

候选最小补丁为 `deploy/qualification/octop/acp-final-text.patch`，仅在已有body之后追加原renderer生成且尚未在chunks中的最终文本。权限暂停分支、Agent loop、模型、权限、ACP wire和client回调均未改。原源码SHA `fd03c837bca1aefb6629a094a0ee783b823af06832cc371633e55d11c41ecac9`；patched源码SHA `5408b2c1485d681269955d7b29feaa9a2b8ce32302dbb3ae5d3fafad399a8b6a`；patch SHA `80b00725949cd106ba4b76639ded4614dca1347829a5aedb3c40a2e38296a147`；MIT来源和LICENSE hash见 `acp-patch-profile.json`。8项fixture覆盖text-only、空输出、已发final去重、tool+final、tool+error、tool-only、permission suspension、callback final fallback。原版仍为失败/preview；补丁版scope独立为 `octop-harness-acp-delegated-patched`，首个outer128真实任务仍失败；最后独立outer512 scope的实际结果见下节，原版结论不变。

## 修补版独立镜像、预算失败与最终成功

最小来源补丁由root逐行审阅后单独构建，原版image `59ec...` 与失败保持。最终patched image为 `sha256:2da776a4603d6cc979bb6fcd99dd5f07eec641c0c5810ba515299f0387f55eb0`；archive `images/containers/octop/acp-patched/sha256-2da776a4603d6cc979bb6fcd99dd5f07eec641c0c5810ba515299f0387f55eb0.docker.tar`，580,673,024 bytes，SHA `82f221fb84c9c4cb744a740e8eb6c3741395c2a71c5c90c5d2f92742f8343223`。provenance为 `.private/qualification/octop-acp/patched-build-20261030T065607Z-0f6b368f/report.json`；recipe SHA `d6b8d405e129fb5ae9c0bcb40400be632e52fe83ee58e17526696eb5b97b4133`。构建无网、应用前后及加载后均核源码SHA，并携带原MIT LICENSE。`acp-patch-profile.json`是不可追写的构建输入（其中null image/false native qualification表示构建时没有运行资格）；实际镜像与预算分别固定在两个runtime profiles，不以旧构建输入充当最新验收报告。

首个patched候选image `0bcce...` 的零模型bootstrap因公开patch文件继承600权限而失败；仅公开patch/license文件改644后重建上述最终image，旧候选/archive/失败仍保留。原始receipt是0644且位于0700目录；一次回放准备因新严格读取而在dispatch前拒绝。后续只对该合成挑战核owner、完整无symlink父链及有界常规文件读取，不改原文件，也不放宽配置/密钥检查。新probe以umask077创建文件。

最终image的 `patched-bootstrap-20261030T065704Z-def8220f` 与 `patched-replay-20261030T065710Z-ae21671e` 均通过；后者实际加载patched模块后重放旧trace，报告SHA `cf98a2c11f0e75a031ad15eb612c017dccd186409bfa9f26f1f747ce00b1d218`。这是无模型补证，未改变原版失败。

| 真实case（各一次；未重放未知结果） | Scope / outer输出上限 | 实际outer / inner发送 | 结果 |
| --- | --- | --- | --- |
| `model-20261030T063019Z-b98f90f5` | 原版 `octop-harness-acp-delegated` / 128 | 2 / 2；全部HTTP200 | FAILED：原工具丢最终文本，outer得到98字摘要 |
| `patched-model-20261030T065837Z-bcd5f66f` | 修补版 `octop-harness-acp-delegated-patched` / 128 | 2 / 2；全部HTTP200 | FAILED：ToolMessage完整144字并含receipt；outer以length结束，只返回receipt前11字 |
| `patched512-model-20261030T070607Z-eda145be` | 修补版 `octop-harness-acp-delegated-patched-outer512` / 512 | 2 / 2；全部HTTP200 | PASS：inner/ACP/ToolMessage/outer最终精确传递32字receipt，outer stop、inner idle |

第二次失败的原生usage明确：outer第二响应output_tokens=128，reasoning=114，11字可见文本是receipt精确前缀。实际请求为max_completion_tokens=128，未传enable_thinking；不能把这个截断算成功。独立只读补证 `.private/qualification/octop-acp/analysis-20261030T070319Z-6b3b9400/report.json` SHA `c8bea1f8c9bc2c81bcd26c701e74f2764a0ae3bf56c264b586cf36f2ae955ec7`，原FAILED报告hash不变。

第三次经明确追加授权，仅outer输出上限提高到512（含reasoning），不改prompt结构、Agent loop、协议、思考参数、内层限制或精确判据。`acp-patched512-profile.json` SHA `e74fd4c2762dd3f457799200bc79ad739997d9794a75aa003a97fbb049e8ce16`；先通过 `patched512-bootstrap-20261030T070501Z-3939cae2` 的零模型预算快照，再创建独立 `patched512-model-reservation.json`。原版及patched128 profiles/reservations均保留。最后任务native报告SHA `1be1e4872eca852a6e27ddb8743ae01313d3dfa6a112462a8429725d4f5e9b64`，operator报告SHA `321938ef59dc3150189d3afd1cbd7d8f52f81a4b0a829740b9150804b35f0775`，全部15项原判据通过，实际8.39秒，outer history4/内history3、原生acp_runner1/native read1。outer两次output为110/56，reasoning为15/23；外层最后stop。inner响应model metadata仍未观察到，不能提升为已验证。

最后完整链路：outer user238字内含67字委派指令；tool args、ACP text、inner user均67字且逐字相等；inner final与11个agent_message_chunk拼接均32字且等于receipt；patched ToolMessage144字保留摘要和receipt；outer final32字与receipt逐字相等。只读补证 `.private/qualification/octop-acp/analysis-20261030T070751Z-a5322f38/report.json` SHA `1eb997165f156482a9c3dcf02484cee645738116d786253ff451cc493e7251ad`。各case的真实native IDs分别存原生报告，未把外thread和内session合并；补充HTTP仅history/status各1次，主委派仍stdio ACP。

三次真实任务共outer6/inner6次观察器范围内SDK发送，每个任务不超过各4次；未发生鉴权失败或重定向。三个任务均保存完整日志、native owner teardown child returncode -9，容器PID0、removed true、cleanup_errors空；不声称-9是优雅退出。最终仅查本任务label，剩余容器0；始终没有宿主端口映射。

## 冻结、回归与保留边界

新增本轮24项纯组件已在本机/服务器通过：原11项、patch完整性3项、私有路径链7项、512预算3项；16条真实trace链路断言和8个格式fixture在原image与实际patched image分别通过（同一断言集合不重复计数）。21项OpenCode observer组件是只读复用的独立既有证据，不算新增24项。不是将fixture成功当真实ACP成功；只有最后patched512模型case的scope获得本次最小委派运行资格。

独立审阅的路径链问题已修于本轮新prepare/run脚本：只读复用 `probe_hermes.no_links/private_directory/read_file`（SHA `6317083c61ef334b3527dd34b1dfb38f3fdf6912efee5c4a5eae259d680e7d7c`），在resolve前检查完整symlink链、私有目录owner和0700，existing错误权限拒绝且不静默chmod；配置/密钥/bootstrap输入分别校验。旧Docker清理helper SHA不变。运行源码快照与构建时准备器SHA分开保留，不把修正后的源码hash回填旧报告。

本次成功仅是**Harniqo最小补丁 + 固定Octop Harness SDK + 固定OpenCode + outer512/inner1024资格组合**。上游未修补版本仍有已复现缺陷；outer128预算仍有已复现截断。完整Octop服务ACP、生产租户隔离、委派写审批、运行中取消、崩溃恢复、durable重放及重启续聊均未因此通过。原始委派的工具/权限/usage转换仍有损；native子进程退出已观察，不等于供应商停止在途计费。M0候选决策与后续完整产品AC-16验收分别由共享规划记录，本文件不代替后续阶段验收。


最终补充脱敏分析（含内层request_id唯一性与逐发送/响应匹配）为 `.private/qualification/octop-acp/analysis-20261030T071241Z-e452657f/report.json`，SHA `d811c093c43ab31b8b00b1e88af1f23f8d4302e0a6d78fb82f77b387170f7794`。三个case只读核对均为2个唯一request_id与2个对应response，未修改原报告。公开文件完整性清单为 `deploy/qualification/octop/acp-recipes.sha256`；这里只冻结本轮ACP源码/文档/配方，既有共享Docker/私有路径/observer实现以profile内SHA只读引用。
