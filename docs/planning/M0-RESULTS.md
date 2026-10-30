# M0 执行证据索引

日期：2026-10-30 · M0 状态：`IN_PROGRESS`，8 项中 **4 项已验收**。本索引记录已取得的证据及其范围，不表示 M0、应用功能或生产发布已完成。

任务定义见 [BACKLOG](BACKLOG.md)，阶段门槛见 [ROADMAP](ROADMAP.md)，验收规则见 [ACCEPTANCE](ACCEPTANCE.md)。这里只发布脱敏摘要；私有报告位置均为工作区相对路径模式，`<…>` 代表由受控环境记录映射的实际文件名。原始报告、日志、配置、凭据和镜像不随本索引公开。

## 任务状态

| 任务 | 当前状态 | 已取得结果 | 尚需完成 |
| --- | --- | --- | --- |
| M0-01 产品规格 | ✅ `ACCEPTED` | [PRD](../product/PRD.md) 固定两种部署操作者、权限矩阵、导航和六条输入/结果/失败流程；8 项文档验收检查通过 | 应用实现由后续里程碑交付 |
| M0-02 机器契约 | `IMPLEMENTED` | 60 个 Schema 定义、18 条 OpenAPI 路径、107 个正反例、15 个生命周期案例通过验证；干净服务器按完整 hash 锁安装后复跑通过 | 结合真实原生 Harness 与生产隔离准入冻结契约 |
| M0-03 首个 Harness | ✅ `ACCEPTED` · 已开发完成 | 固定四工具组合：两模型实际身份与 HTTP 目标、工具/文件/历史、原生取消、人工拒绝、重启后原生续聊均已验证 | 范围限于本次组合；locked-stream 的断线中断、重启后旧 Run 查询404按实声明，通用工具与生产隔离由后续任务验收 |
| M0-04 多 Harness | ✅ `ACCEPTED` · 已开发完成 | 正式开发目标Hermes/OpenCode/OpenClaw均有运行证据；OpenCode实际发送、DSH SDK与Octop三身份试验补齐；原版失败、独立修改组合和preview均有具体结论 | 正式Adapter、完整能力套件、生产隔离及更多组合在后续阶段验收，不从技术准入推导已交付支持 |
| M0-05 执行隔离 | `IN_PROGRESS` | 封闭KVM基线与第三轮真实NIC82项检查通过；临时规则、资源及原有效策略恢复均确认；固定实例组真实期限/进程故障恢复通过 | 生产Egress代理、专用身份、硬配额、控制面全局fencing、资源耗尽及更广宿主故障恢复等完整准入 |
| M0-06 来源与依赖基线 | ✅ `ACCEPTED` | 当前 MIT 自有范围、第三方来源及分发边界已清点；验证工具依赖已精确锁定和校验 | 新增依赖、原生运行包及镜像分发仍须继续核对来源、许可和材料 |
| M0-07 验收与资源 | `IN_PROGRESS` | [验收基线](../operations/ACCEPTANCE-BASELINE.md) 已形成 | 原生三端 CI/签名、容量、备份和相关外部资源逐项验证 |
| M0-08 排期与准入 | `TODO` | 67 项任务及阶段依赖已形成 | M0-01～07 全部证据和关键可行性结论到位后，冻结具体投入与排期 |

## 已取得证据

| 编号 | 证据及结果 | 可复现材料 | 私有证据位置模式 |
| --- | --- | --- | --- |
| E01 | 产品规格的 8 项文档验收检查通过 | [PRD](../product/PRD.md) | 文档本身为交付物 |
| E02 | 契约离线一致性验证通过：60 定义、18 路径、107 样例、15 生命周期案例；指定服务器干净安装并复跑通过 | [机器契约及验证命令](../../packages/contracts/README.md) | `.private/validation/<contract-run>.json`；对应安装环境和命令记录由受控环境索引关联 |
| E03 | 当前依赖来源基线：20 个精确包版本、434 个官方发行文件 SHA-256，完整 hash 锁的干净服务器安装验证通过 | [来源与许可](../compliance/DEPENDENCIES.md)、[依赖锁](../../scripts/contracts/requirements.lock)、[锁来源](../../scripts/contracts/requirements.lock.provenance.json) | 与 E02 关联的安装日志、Python/pip 版本及实际分发清单 |
| E04 | 两个真实 Provider 探针通过：`deepseek-v4.1-flash`、`qwen3.8-flash` 各两轮 HTTP 200，工具 nonce、随机 receipt、固定产物文件及 hash 核对通过；供应商分别报告 920、1071 tokens | [模型探针范围与方法](../../scripts/qualification/MODEL-PROBE.md) | `.private/qualification/<model-probe-run>/report.json` 及相邻 `report.artifacts/probe-result.json` |
| E05 | 第三次封闭 KVM 基线通过：2 台 VM、每台 2 次冷启动，共 44 项 Guest 检查；4 份完整 console；`errors=0`、`cleanup_errors=0`；基础镜像及 3 个既有 domain 的清单/状态不变；本任务临时 VM 和写入层已回收 | [KVM 验证方法](../../scripts/qualification/README.md)、[验证脚本](../../scripts/qualification/qualify_kvm.py) | `.private/qualification/kvm-<run-id>/report.json` 及同目录的 `*-round-*-console.log` |
| E06 | 失败留证组件 `VERIFIED`：本机 4 项通过，指定服务器 4 项复跑通过（1.025 秒）。覆盖部分报告后超时、第二轮恢复校验失败、drain 期间补收报告、独立快照及成功轮次状态 | [标准库测试](../../scripts/qualification/test_qualify_kvm.py) | `.private/validation/kvm-failure-evidence-tests.json` |
| E07 | 早期两工具 Hermes 运行包构建完成：Python3.14.10、SQLite3.53.4/FTS5、74 个分发包；Docker archive 为 658,780,160 bytes，SHA-256 `404bcc2246c0ada24c3925c95d2579abd8f62f139180fed0500e2d574a70e270`。该旧归档的 Harness 准入标记保持 false；当前四工具组合另见 E14、E17–20 | [原生 Hermes 准入组合](../../deploy/qualification/hermes/README.md) | `images/containers/hermes/git-a382af23f4c0/<image-id>.docker.tar` 及 provenance；`.private/validation/<offline-build>.log` |
| E08 | Hermes 受控工具及原生驱动的 14 项组件测试在服务器通过；覆盖临时文件、随机回执/hash、权限/符号链接/非法 Unicode 拒绝、SSE 游标、canonical 终态形状、伪造结果拒绝，以及缺模型元数据时保留真实子项证据 | [原生探针与测试范围](../../scripts/qualification/HERMES-PROBE.md) | `.private/validation/hermes-tools-evidence-tests.json` |
| E09 | Provider 探针 26 项 HTTP/解析/配置组件测试在服务器通过；仅使用受控本机服务，不调用真实模型 | [探针组件测试](../../scripts/qualification/test_probe_model.py) | `.private/validation/model-probe-tests.json` |
| E10 | 离线构建输入：74 个官方 wheel、49,184,573 bytes 全量核对原锁及导出 hash；同 Python3.14 禁网 fresh-cache 精确同步 74 版本通过。上游过滤器核对 1 项已明确 override、0 项非预期冲突；输入复制的 14 项篡改/size/名称/符号链接/竞态组件测试在服务器通过 | [供应链核验](../../research/m0-dependency-verification.md)、[输入边界测试](../../scripts/qualification/test_build_hermes_image.py) | `.private/qualification/hermes-offline-inputs/<closure-id>/`；`.private/validation/hermes-wheelhouse-tests.json` |
| E11 | 早期两工具组合的两个独立模型配置真实子项通过：新建 Session/Run、缺失和错误 token 被拒、两个工具调用及随机回执、7 条原生历史、文件 SHA、终态和真实 SSE 断线补取。DeepSeek 配置记录 42 事件/8605 tokens，Qwen 配置记录 34 事件/8906 tokens | [原生验收驱动](../../scripts/qualification/probe_hermes.py) | `.private/qualification/hermes-native/<case>/report.json`；首个用例另有只追加的 `subchecks.json` |
| E12 | 早期两个配置的 canonical 严格元数据门槛均失败：canonical Run 的有效模型 metadata 为空；未将请求配置回显当作实际模型。补证不覆盖原始失败报告 | [固定原生 Run 实现](https://github.com/NousResearch/hermes-agent/blob/a382af23f4c067d23463b5781958684c2cda82bc/gateway/platforms/api_server_runs.py)、[探针模型门槛](../../scripts/qualification/HERMES-PROBE.md) | 与 E11 相同，错误码及 `model_evidence` 分别保留 |
| E13 | 早期两工具组合、canonical 路径的两个配置，同一会话历史和 completed Run 均在实际容器重启后恢复；DeepSeek 配置另用不含旧 nonce/receipt 的新提示，在重启后真实续聊回忆随机回执，且没有调用工具/改变文件，报告 3212 tokens | [原生运行包与持久目录](../../deploy/qualification/hermes/README.md) | `.private/qualification/hermes-native/<case>/subchecks.json`、`continuation.json` 或 `recovery-cleanup.json` |
| E14 | 四工具组合离线构建通过，新增上游人工审批闸门、合作式中断和实际 HTTP 发送观察；归档 SHA-256 `b3ac9b86496400c944b46a1c94039a403ded7773f3dbebf93a5547712641565d`，与早期两工具镜像分别保存 | [组合及构建配方](../../deploy/qualification/hermes/README.md) | `images/containers/hermes/git-a382af23f4c0/<new-image-id>.docker.tar`、provenance 及 `.private/validation/<profile-build>.log` |
| E15 | 固定新镜像内14项模型锁/传输观察组件全部通过、零跳过，包含真实 httpx0.28.1 的 sync/async/redirect/error/idempotence 检查；禁网、无凭据挂载。服务器11项生命周期组件、14项既有工具组件通过 | [模型锁根因与接口区别](../../research/hermes-native-model-evidence.md)、[生命周期探针](../../scripts/qualification/HERMES-LIFECYCLE.md) | `.private/validation/hermes-model-lock-components-<run>/report.json` 及相邻日志；对应组件报告 |
| E16 | 服务器13项启动/回收组件全部通过、零跳过，包含 Linux 内核 parent-death signal 的真实子进程退出检查；无 Docker 和模型调用。启动器已增加固定端口锁、归属检查、停止确认与限量私有日志 | [验收操作者](../../scripts/qualification/HERMES-OPERATOR.md) | `.private/validation/hermes-operator-components-<run>/report.json` |
| E17 | 新镜像两个 locked-session 真实用例通过：DeepSeek 9031 tokens/37事件、Qwen 9512 tokens/39事件；每例三个HTTP200、实际请求模型/目标hash匹配、零重定向/错误；native actual model/custom provider/confirmed lock、两工具随机回执、文件与历史一致 | [模型锁探针](../../scripts/qualification/probe_hermes_model_lock.py)、[HTTP观察范围](../../research/hermes-native-model-evidence.md) | `.private/qualification/hermes-native-model-lock/<model-case>/probe.json`、workspace中的私有传输记录及operator日志 |
| E18 | 人工拒绝通过：原生待审批及原因、精确request/generation拒绝，wrong request/stale generation/late replay均409；真实denied工具结果、无文件副作用；Qwen报告5751 tokens | [原生生命周期验收](../../scripts/qualification/HERMES-LIFECYCLE.md) | `.private/qualification/hermes-lifecycle/<approval-case>/probe.json` 及operator/私有日志 |
| E19 | 运行中取消通过：工具确已开始，停止后0.286秒确认匹配随机回执的真实handler Future返回、原生cancelled和无自然完成marker；SSE实际断开后从seq0补取seq1–2；Qwen报告2747 tokens。常驻网关继续存活，任务结束与网关退出分别判断 | [取消确认依据](../../scripts/qualification/HERMES-LIFECYCLE.md) | `.private/qualification/hermes-lifecycle/<cancellation-case>/probe.json`；此前不合适的进程退出断言失败及追加纠正报告保留 |
| E20 | 当前四工具镜像的 primary、secondary 各7条历史在新容器启动及实际 `docker restart` 后，均与各自原报告的完整hash一致、产物hash不变。primary 另发不含旧nonce/receipt的新提示，真实续聊正确回忆、无工具、原历史前缀和产物不变；actual模型锁及一次新HTTP200匹配，报告3333 tokens；secondary 恢复没有新增模型调用。两配置的旧 locked-stream Run 查询均HTTP404，报告分别记历史恢复通过、旧Run轮询恢复未通过 | [路径与恢复限制](../../research/hermes-native-model-evidence.md) | 对应model-case中的独立 `recovery-<run>/report.json`；primary 另有 `continuation.json`；失败和补证均保留 |
| E21 | OpenCode 1.19.5 首个固定运行包的离线版本/rg/动态库检查通过，但非root实际启动因镜像目录缺少遍历权限失败，尚未请求模型。源码 `810c0eba1a7348036f8bdf61e6632d0c36c6876c`，官方 npm 完整 SRI 与 ripgrep/source pins 已核对；当时7项准备工具组件通过。旧image store ID `sha256:bfc9e8b6bb477f3ff0beb61e1a8f060ded16284651ea77bdf210bf14ceea0b7b`；归档499,581,440 bytes，SHA-256 `80b29c2154d850ac1ab20902457bd16f3e0c3c26f78b5f542537e79ecf8ee603`。原归档及失败保留，不能作为可运行或正式集成准入证据 | [OpenCode 运行包](../../deploy/qualification/opencode/README.md)、[候选核验](../../research/m0-harness-candidate-qualification.md) | `images/containers/opencode/1.19.5/<image-id>.docker.tar` 及 provenance；`.private/validation/opencode-prepare-localinputs-<run>.log`；对应启动失败记录 |
| E22 | OpenCode 修复镜像实际服务启动：health1.19.5、缺失/错误Basic认证401、原生OpenAPI3.1；一个真实任务记录精确目标edit审批once200及原生read→write→read完成。原自动探针错误读取assistant.time.end而非completed，等待超时并abort200，报告保持失败。独立离线补证11个子项通过：全部固定路径、顺序、随机challenge与实际文件一致；原生assistant的stop/completed早于截止下界108.706秒、早于预abort历史快照109.161秒且无error。未重复模型调用。12项组件回归在本机与服务器通过。尚无当时session idle快照、独立served-model传输、SSE、恢复、原生运行中取消或生产隔离证据，M0-04未验收 | [原生探针](../../scripts/qualification/probe_opencode.py)、[组件测试](../../scripts/qualification/test_prepare_opencode.py)、[运行包与结果](../../deploy/qualification/opencode/README.md) | `.private/qualification/opencode-native-smoke/<run>/probe/report.json`、`offline-supplement.json`及独立清理记录；`.private/validation/<opencode-components>.log` |
| E23 | OpenCode沿用固定1.19.5修复镜像，两个新独立真实smoke自动通过：DeepSeek报告15148 tokens、Qwen报告16201 tokens；原生读写/精确审批、随机输入与产物、idle及stop/completed均通过。早期失败未改写 | [本轮固定组合与结果](../../deploy/qualification/opencode/README.md)、[操作者](../../scripts/qualification/run_opencode_qualification.py) | `.private/qualification/opencode-admission/<primary-or-secondary-case>/operator.json`、`probe/`、`source-sha256.json`；汇总 `final-summary-20261029.json` |
| E24 | OpenCode Qwen恢复用例通过：以同home/workspace重建容器并实际restart，完整原生历史相等；同原生Session用不含旧随机值的新提示续聊，正确回忆nonce/challenge，原历史前缀及产物不变、无工具，新增回答报告5066 tokens | [恢复方法与边界](../../deploy/qualification/opencode/README.md) | `.private/qualification/opencode-admission/<recovery-case>/operator.json`及原生history/status材料 |
| E25 | OpenCode Qwen拒绝与执行中取消分别自动通过：reject200后原生write error/idle、无待执行工具/审批且无文件，迟到重复reject404，3677 tokens；取消前已观察同session/message真实SSE文本delta与busy，abort200后MessageAbortedError/completed/idle及无运行工具，0.016秒确认原生终态，用量unknown。原12项与新增7项组件在本机/服务器通过；本轮全部5个容器PID0后移除、日志保存、端口释放 | [生命周期组件](../../scripts/qualification/test_opencode_lifecycle.py)、[完整结果](../../deploy/qualification/opencode/README.md) | `.private/qualification/opencode-admission/<denial-or-cancellation-case>/operator.json`及审批/SSE/原生状态；汇总与组件日志 |
| E26 | Octop Harness1.0.5固定commit `76cd38d7b8ffc66c28ba24ad60bbab206a95d3ac`：93个官方wheel、31,330,542 bytes逐项核验SHA/size；根wheel的216个Python文件与固定源码一致，Python3.14.10禁网hash安装/pip check通过。Docker image ID `sha256:714e015d1c1281e1c91bd2ae67f39191b99c408eb7523b62a409831e912a73d5`；归档496,659,456 bytes，SHA-256 `f5fa07a06006c9f1dca7471bb3135ba22834eb22a7914c1754731b9d2334c470` | [固定配方](../../deploy/qualification/octop/README.md)、[完整锁](../../deploy/qualification/octop/harness.requirements.lock)、[来源记录](../../research/octop-native-qualification.md) | `images/containers/octop/harness/<image-id>.docker.tar`；来源由 `.private/qualification/octop/<build-run>/report.json`、lock/wheel-provenance/build/save日志关联，归档相邻无provenance文件 |
| E27 | Octop Harness本次4个Qwen任务共9次HTTP200、无重试；首allow因末尾换行不符而精确拒绝，保持失败。3个独立后续用例通过：allow原生read/write/精确HITL approve，6条history、6388 tokens；deny原生write_file error/rejected、无输出，6条history、6378 tokens；cancel确认合成wait真实开始后0.006415275秒stream返回、handler finally、无自然完成marker，2条history、1388 tokens。目标hash/request model与原生供应商model_name匹配；4个容器均PID0后移除 | [独立原生资格记录](../../research/octop-native-qualification.md)、[原生探针](../../scripts/qualification/probe_octop.py) | `.private/qualification/octop/native-<case>/result/report.json`、`operator-report.json`、state/events及只追加的 `offline-supplement.json`；具体原始hash见研究记录 |
| E28 | Octop早期准备器6项、探针9项通过，独立审阅后最终38项组件在本机/服务器通过：operator13、prepare8、SDK probe11、service guard4、service probe2；镜像内禁网、无凭据的3项真实SDK组件通过，覆盖原生构造/history、HITL配置及被阻塞异步iterator的取消/finally。成功真实用例关闭并重构Agent后SQLite历史逐条相等，未新增模型调用；首失败HITL另有7项离线精确审批回归 | [准备器测试](../../scripts/qualification/test_prepare_octop.py)、[探针测试](../../scripts/qualification/test_probe_octop.py)、[SDK组件](../../scripts/qualification/test_octop_native_components.py) | `.private/qualification/octop/component-tests-final.log`、`probe-components.log`、`history-regression.json`、`approval-regression.json`及原生case；组件与真实用例证据分别记录 |
| E29 | Octop完整服务早期独立准备记录：1.0.3b1固定commit `890111b7879665840414ca28ab168b05b0db0921`，官方锁选201个发行物；纯wheel解析因缺适配wheel失败保留，5个锁定sdist已在无网、非root固定builder中构建成功。该记录时完整服务镜像/启动尚未完成，后续构建/原生证据见E36；ACP委派尚未执行；均不继承Harness SDK结果 | [完整服务状态](../../research/octop-native-qualification.md)、[sdist构建器](../../scripts/qualification/build_octop_sdists.py)、[服务准备器](../../scripts/qualification/prepare_octop_service.py) | `.private/qualification/octop/<service-build-or-sdist-run>/`；未完成项没有通过标记 |
| E30 | 前两轮真实NIC合法TLS失败保留。授权只读诊断在对应窗口取得6+3条精确匹配的UFW BLOCK，确认Guest SYN至获准10018被宿主INPUT路径拒绝。第三轮仅临时加入本任务两条精确规则后，2VM各41项、共82项全部通过：合法TLS、host10019/peer/metadata/外网/其他host/source-spoof拒绝、负向后合法仍通。canary12次请求，禁止端口Guest命中0；零模型。两规则、所有VM/network/filter/写入层回收，原有效nft/filter、libvirt对象及base不变，端口释放 | [真实NIC与精确临时例外](../../scripts/qualification/KVM-NETWORK.md)、[执行评审](../../research/m0-execution-network-review.md) | `.private/qualification/network-<third-run>/report.json`、`verification-sanitized.json`与`host-input-policy/`；早期失败和授权只读诊断目录独立保留 |
| E31 | 21项组件本机/服务器通过：6项网络护栏、15项私有凭据/规则归属/恢复边界。独立审阅修复named counter身份不能被归一化抹掉、未决sudo writer不得声明恢复、启动信号窗口必须先登记pending；默认不变更策略，只有显式opt-in为本任务10018添加精确例外 | [策略边界](../../scripts/qualification/kvm_host_policy.py)、[组件测试](../../scripts/qualification/test_kvm_host_policy.py) | 授权只读诊断目录中的递增component/source receipts；生产egress、身份、配额、watchdog/fencing仍未验收 |
| E32 | OpenClaw2026.10.8官方linux/amd64镜像：GHCR/DockerHub固定manifest/config字节相同，42个OCI blob全部匹配官方SHA/size。Docker归档674,162,688 bytes，SHA-256 `adab4f92ac77655b55573eb1560eac9d77c99199e09a496d78148bdc91f23804`；image store/manifest为`sha256:eba17fa04b4ac5616496c2ce374a95afb6db6ff52d8bf73bf87dc8c7b8656cad`。两次在线下载失败及OCI标签解析失败保留，按同一固定manifest完成离线载入与归档 | [官方固定配方](../../deploy/qualification/openclaw/README.md)、[获取与校验](../../scripts/qualification/prepare_openclaw.py) | `images/containers/openclaw/2026.10.8/<manifest>.docker.tar`及provenance；`.private/qualification/openclaw-image/<attempt>/` |
| E33 | 最终固定镜像内30项禁网组件通过，0跳过：22实际Undici观察、4私有配置、4原生协议判据；Node24.22.1、Undici8.11.2、非rootUID。另5项OCI边界本机/服务器通过。原生config validate通过；Gateway协议4、Ed25519设备签名、operator scopes及missing/wrong token拒绝通过 | [真实操作者](../../scripts/qualification/run_openclaw.py)、[协议探针](../../scripts/qualification/probe_openclaw.mjs) | `.private/validation/openclaw-image-components-<final-run>/report.json`及TAP；`.private/qualification/openclaw-native/<validate-or-preflight>/` |
| E34 | OpenClaw两个独立真实smoke通过：DeepSeek/Qwen各原生read→write→read、随机产物/工具配对/native idle与final；各4条实际Undici HTTP200请求/响应，目标/model hash匹配、无重定向/未知/错误。Qwen报告14949 tokens；DeepSeek部分assistant无usage，完整总用量保持unknown。Qwen生成取消通过：同run实际文本delta与active在先，chat.abort匹配runIds，约0.153秒确认aborted+idle、无工具调用/工作区条目变化，1条匹配HTTP200，用量unknown。3任务共9次匹配HTTP，容器PID0后回收 | [原生组合范围](../../deploy/qualification/openclaw/README.md)、[当前能力矩阵](../../research/m0-runtime-evidence-matrix.md) | `.private/qualification/openclaw-native/<model-smoke-or-cancellation>/operator.json`、`evidence/`；所有原始轨迹只在私有目录 |
| E35 | 独立审阅修复共享清理逻辑：日志归档不完整时仍停止并确认PID0，但保留已停止容器和身份供证据恢复，不再先rm丢日志。回归先红后绿，Linux服务器14项全通过；本机13项通过、Linux父进程死亡用例显式跳过，已由服务器补验 | [共享操作者](../../scripts/qualification/run_hermes_qualification.py)、[回归](../../scripts/qualification/test_run_hermes_qualification.py) | 本轮组件输出及源码；已有真实用例日志完整，原历史结果不改写 |
| E36 | Octop完整服务201固定包及5个sdist完成禁网构建/pip check，归档867,920,384 bytes，SHA-256 `1506f719e58af360203a113d51c11a2ccae9709fb62d1400efc1f94812547f70`；image ID `sha256:8dbb51439f4ff7597352035e30e7d0099e3ba6e143fd538c9a2f4a4cd0f932c7`。原生SQLite/setup/admin201、缺失/错误认证401及已认证HTTP200通过；独立原生WS模型用例通过：1次实际Qwen HTTP200、40个原生事件、随机echo精确、REST历史2条/assistant1条，无工具/错误且turn inactive；操作者PASS、PID0/移除与清理0错误。此前零请求准备失败保留。与Harness SDK分开登记，ACP仍未验 | [服务固定锁](../../deploy/qualification/octop/service.requirements.lock)、[服务来源](../../deploy/qualification/octop/service.wheel-provenance.json)、[分层结果](../../research/octop-native-qualification.md) | `.private/qualification/octop/<service-build-or-native-case>/`；服务原始失败/成功分别保存 |
| E37 | 三平台契约CI配置已实现：Ubuntu24.04、Windows Server2025、macOS15；Python3.12、完整hash锁、只读权限及固定Actions提交；首次run的Linux/macOS通过，Windows因checkout CRLF与canonical LF字节不同失败；保留[原run](https://github.com/StateKnot/Harniqo/actions/runs/40907696364)。新增.gitattributes固定LF后[重跑全部通过](https://github.com/StateKnot/Harniqo/actions/runs/40908412252)，三个job均验证60定义/18路径/107样例/15生命周期且JSON源hash相同，见[可复核摘要](../operations/contract-ci-2026-10-30.json)。此项不等同桌面构建、安装、签名或GUI验收 | [工作流](../../.github/workflows/contracts.yml)、[资源基线](../operations/ACCEPTANCE-BASELINE.md) | 实际GitHub run/job与公开合成报告在首次执行后关联 |
| E38 | 固定OpenCode/Bun原镜像新增只读本地plugin，经上游config hook透明观察实际SDK发送。21项实际Bun禁网组件及3项报告判据通过，native bundled SDK无模型负向发送证明该seam实际生效。一个新Qwen任务通过：4次SDK fetch/4匹配HTTP200、实际URL/model hash、read/write/read、once审批和completed/idle，15864报告tokens；PID0、全日志、移除和端口释放。无第二次付费重复 | [独立发送证据](../../research/opencode-model-transport.md)、[插件](../../deploy/qualification/opencode/transport_plugin.mjs) | `.private/qualification/opencode-transport/<case>/operator.json`及私有observer/原生API材料；原红信号及修正记录保留 |
| E39 | DSH固定0.2.2-alpha.2/f3803c9，官方npm包完整SRI；Harniqo冻结724解析/653安装的生产CLI组合，未冒充官方pnpm tree。845,569,536-byte归档SHA `114387849bc262119de2d8cf32a99357a77e4c5c8ebf6366393c3eb0c6614b94`；官方版本PyPI缺包及noexec预检失败保留，固定native-cache配置后SDK初始化通过。8项无网预算组件、审阅后22项操作者/admission边界回归通过，smoke前强制绑定同image/probe/observer/预算/配置的0HTTP自检报告 | [来源与边界](../../research/dsh-native-qualification.md)、[锁与摘要](../../deploy/qualification/dsh/qualification-results.json) | `images/containers/dsh/0.2.2-alpha.2/<image-id>.docker.tar`及原provenance；新admission与review原报告私有保存 |
| E40 | DSH唯一真实Qwen任务通过：4次HTTP200、每次cap1024，native read→write→read、随机52-byte产物、成功turn/end、32事件；两个正常退出的native进程均exit0，重启后resume同ID，完整持久事件与原wire逐对象相等，无新prompt/HTTP。SDK serverInfo0.0.1与CLI包0.2.2-alpha.2分别登记；response served-model unknown，逐prompt cancel不支持，真实审批/崩溃续跑/后续prompt未验。DSH保持preview | [最小SDK资格](../../deploy/qualification/dsh/README.md)、[真实与限制](../../research/dsh-native-qualification.md) | `.private/qualification/dsh-smoke-<run>/`、`dsh-history-verified-<run>/`；公开索引只含计数、状态与文件hash，不嵌入原始报告 |
| E41 | Octop ACP独立组合：原Harness1.0.5+[acp]固定extras与OpenCode1.19.5，真实stdio initialize/new/close通过。断线后的require_existing为具体ConnectionResetError，未重建child/session、无模型；不称持久恢复。源码/镜像/两层ID分别记录，补充HTTP只读内层history/status，不替代ACP提交 | [ACP固定配方](../../deploy/qualification/octop/acp-README.md)、[三身份与转换](../../research/octop-acp-qualification.md) | `.private/qualification/octop-acp/<bootstrap-or-build>/`；原版归档/原始失败保留 |
| E42 | 原版ACP真实用例FAILED：outer2/inner2 HTTP200，inner已返回完整32字receipt，outer工具/最终回答仅98字摘要。无网真实trace回放16链路断言+8格式fixtures定位原acp_runner的body-or-final返回分支丢final；没有把失败改通过。最小MIT来源patch仅合并原工具摘要与final，wire/权限/Agent loop不变。独立修正版128输出任务仍FAILED：final已穿过ACP，outer128含114 reasoning而截断；各自reservation/原报告不变 | [原版失败与源码修正](../../research/octop-acp-qualification.md)、[最小补丁](../../deploy/qualification/octop/acp-final-text.patch) | 原版model与patched128 case、replay目录分别留证；补丁前后SHA及LICENSE在独立profile中 |
| E43 | 独立patched-outer512组合真实PASS：只提高outer输出限额至512，outer/inner各2 HTTP200，身份/工具/转换/精确receipt/终态与停止回收全通过；inner仍4请求/1024，整体180秒；第三个且最后模型任务。原版与修正版128仍FAILED。原/改镜像分开，当前成功image为`sha256:2da776a4603d6cc979bb6fcd99dd5f07eec641c0c5810ba515299f0387f55eb0`；归档580,673,024 bytes，SHA `82f221fb84c9c4cb744a740e8eb6c3741395c2a71c5c90c5d2f92742f8343223` | [独立512配置](../../deploy/qualification/octop/acp-patched512-profile.json)、[实际分层结论](../../research/octop-acp-qualification.md) | `.private/qualification/octop-acp/<patched512-case>/`及独立reservation；两层usage/源SHA/实际请求/提示转换各自保存 |
| E44 | 规范与需求二轴复核、[原生契约交叉核对](../../research/m0-native-contract-crosscheck.md)和[能力矩阵](../../research/m0-runtime-evidence-matrix.md)完成当前M0-04退出核对：三个正式开发目标有证据；Octop各层与修改组合分身份；DSH preview具体原因；HarnessRouter不作控制底座的复用成本与来源明确。正式选择验收不等于M4 Adapter或生产VM能力已实现 | [任务定义](BACKLOG.md)、[验收矩阵](ACCEPTANCE.md) | 独立source review快照与整改回归私有保存；M0-02/05/07状态不提前接受 |
| E45 | 新增无模型资格组件CI，[实际运行全部通过](https://github.com/StateKnot/Harniqo/actions/runs/41020704213)：Linux/Python3.12共183项，0失败/错误/跳过；两组Node协议/透明观察器检查也通过。[首次失败](https://github.com/StateKnot/Harniqo/actions/runs/41019478232)保留，定位为父进程退出时/proc读取ESRCH竞态；只将ENOENT/ESRCH当作进程消失，权限/EIO及原PID/start_ticks/Z判据保留。7项确定性回归先红后绿，服务器21目标/183全量通过，没有追加模型调用 | [工作流](../../.github/workflows/qualification.yml)、[公开合成摘要](../operations/qualification-ci-2026-10-30.json)、[检查器修正](../../scripts/qualification/test_run_hermes_qualification.py) | 实际run/job及下载的合成报告关联；真实Harness/VM/模型准入独立计量，组件CI不证明生产隔离或桌面构建 |
| E46 | 新增固定预登记实例组的本地 Resource Broker Module：签名指派/续约、先持久intent再派发、组内Workspace单写者、不可逆stop intent、固定UUID/配置/文件身份、BOOTTIME/bootID/时钟异常、独立watch、重建核对及宿主停止证据。规范/需求复审的取消丢失、陈旧时刻、过期启动和分裂锁缺口已先红后绿修正。当前34项Module/16项操作者护栏；日志I/O故障继续撤销，journal归档缺失保留资源；服务器233项纯组件通过、零跳过 | [Module](../../apps/runner/resource_broker/README.md)、[用例](../../scripts/qualification/RESOURCE-BROKER.md)、[范围与记录](../../research/m0-resource-broker-qualification.md) | 仅固定最多64实例组，锁不跨state_dir；不是完整资源分配器、控制面Assignment或Model/File ExecutionGrant；真实VM结果另记 |
| E47 | 真实准入保留两个前置拒绝（共享base的QEMU owner、旧共享目录0775），不改变原对象；新readonly同SHA副本及严格私有/协调目录分离。三次实际启动流程因运行期DAC owner保护失败保持FAILED；诊断定位QEMU运行中socket与停止后root两态。新role policy与named Broker只读seed ACL仍验hash；第四组真实启动/排他/签名/owner阳性通过，但Guest120秒网络在线等待超时，完整停止和回收0错误；独立150秒窗口也因Final未启动探针而失败且清理通过；后续仅调整相同探针启动阶段，结果单独记录 | [失败与配置分别留证](../../research/m0-resource-broker-qualification.md) | 原失败源码、报告、journal、console及不确定派发写入层保留；不以组件或当前absence回填通过，不关闭M0-05 |
| E48 | 固定实例组正式Module真实验收通过：2KVM共22项Guest检查；launcher SIGKILL确认且A仍活动后独立watch按期停A，watch SIGKILL后从持久journal重建Broker停B，两次均为宿主destroy_then_absent；错误签名/旧代次/过期/复活拒绝，原UUID未替换。2份console完整、本轮资源全回收、base及既有对象不变，错误与清理错误0；服务器233项纯组件通过 | [真实脱敏报告](../operations/resource-broker-kvm-2026-10-30.json)、[全部失败与最终结论](../../research/m0-resource-broker-qualification.md) | runf30e1ba509be；只确认本地固定组子项VERIFIED，M0-05仍IN_PROGRESS；前三次未知派发48GiB写入层保留，未借新组通过删除 |
| E49 | [本轮实际GitHub组件CI](https://github.com/StateKnot/Harniqo/actions/runs/41043954665)成功：源码`a0eae677a290`，Python3.12共233项、0失败/错误/跳过，两组Node检查也通过；实际run/job和下载artifact hash已核对 | [合成CI摘要](../operations/qualification-ci-m005-2026-10-30.json)、[工作流](../../.github/workflows/qualification.yml) | 合成组件、无模型/VM；E48真实KVM单独计量；不代表桌面构建/签名或完整M0-05通过 |

OpenCode 当前修复镜像的 image store ID 为 `sha256:ef392b1148602b61ad4ea1e9dcca9b05337adc3c7ef9fc97434d632c69b8373c`；归档499,582,464 bytes，SHA-256 `c50dd9d32477a4fc70a8e4ba0a985e6bb41688b633d77990869f7078e9e194bc`。构建新增非root用户的目录遍历、文件可读及配置目录不可写检查；原有只读根文件系统、非root用户和凭据权限要求保留。实际原生服务已启动，健康检查报告1.19.5，缺失及错误Basic认证均401，原生OpenAPI为3.1。该镜像与旧归档分别保存，功能测试结果独立记录。

E02/E03 证明当前契约及验证环境可复现，不证明 API 服务、数据库事务、授权或队列已经实现。E04 是受控工具协议的前置检查，执行的是探针自己的固定工具；它没有运行原生 Hermes，不计作 Harness 准入。两次共 1991 tokens 是供应商报告用量，不是已核对账单或费用硬封顶。

E05 的 Guest 没有 NIC。该结果证明真实 KVM 启停、该拓扑下的隔离检查、冷启动磁盘 canary 持久化及安全回收；它不证明生产网络可用。允许模型/工具访问同时拒绝管理网、metadata 和横向访问的选择性出网，仍未验收。原生 Harness 恢复、Runner 租约与 fencing、CPU/内存/磁盘耗尽、故障接管和灾备恢复也未由本次证明。

E06 使用合成 Guest 和真实 UNIX socket，没有启动 VM、创建 TCP 端口或调用模型；测试通过只证明失败留证逻辑。该测试不替代 E05 的硬件证据，也不重新计算此前真实运行的结论。

E08/E09 及 E10 的输入复制测试是组件验证。E11/E13 来自真实原生执行，仅代表所列子项；早期 canonical 配置的有效模型未被该接口报告，E12 的原始失败保持不变；新增 locked-session 与传输观察证据另见 E17。上述 token 数均为相应 Provider/Harness 报告口径，不代表已对账费用；不同用例分别记账，不混成单次运行。

E23–25补齐OpenCode本轮真实用例，不回填E22当时不存在的证据。该批模型身份限原生configured metadata；本轮Qwen独立实际发送证据新增于E38，供应商物理模型身份不从请求推断。SSE仅证明在线事件观察，没有durable游标重放；已完成Session重启续聊不等于执行中崩溃断点续跑；0.016秒也不是供应商计费或任意外部副作用停止保证。

E26–28仅覆盖固定Octop Harness SDK组合。供应商返回model_name不独立证明后台权重；SDK重构后history一致不等于容器/进程重启后续聊；取消后的history停在tool_calls，不伪造assistant completed/Run终态。完整服务与ACP委派继续按E29分别记录。OpenClaw本轮结果见E32–34；DSH本轮最小SDK与限制另见E39–40；HarnessRouter仍仅作来源参考。

OpenClaw配置首轮因已退役`logging.redactSensitive`被拒，按固定上游强制脱敏语义删除旧开关后通过；两次零模型握手在原生`startup-sidecars`未就绪状态失败，随后仅对该明确无提交状态进行有界等待，缺失/错误token门槛保持不变。所有旧报告保留，未在启动失败时消耗模型任务。

## 失败历史与证据保留

前两次 KVM 运行的 Guest 检查已通过，但整体结果均为失败：第一次 console 文件被 libvirt 改变所有者后归档读取失败；第二次发现 libvirt/virtlogd 更换了日志 inode，完整性检查拒绝通过。两次原始失败报告及所保留的诊断材料继续存在于各自的私有运行目录；第三次成功没有覆盖或改写这些记录。

修复后由宿主绑定独立 console UNIX socket，QEMU 只连接 socket，日志文件由宿主以 0600 保存并限制容量；第三次取得四份完整日志和无错误回收证据。随后补充每轮 `status`、`phase`、缺失 Guest、`failed_phase`、`validation_error` 及已收报告快照。超时后补收报告只补充证据，不会把失败轮次改为通过；已确认停止的 overlay 仍按规则回收，JSON 和 console 继续留存。

Hermes 的在线构建先后因官方 Pillow、OpenAI wheel 连接超时失败；离线构建第一次暴露原锁部分 wheel 不带可选 size，修正为已知 size 严格匹配、缺省时有界读取并始终验证完整原锁 SHA 后成功。原始失败及后续成功记录均保留。离线安装实验中的 metadata 重解冲突、低 PID/默认并发超时也独立记录，采用完整 frozen export 的精确同步保留上游 override，未更换包版本或放弃 hash。

首个原生探针在模型门槛前退出，补采的工具/文件/重启证据写入单独报告。之后修正探针，先保存已验证子项，再区分模型未报告与不匹配；没有重写原始失败。早期一项取消实验在发出停止前已完成，保留为未验证；该批次当时也没有人工审批拒绝证据。后续受控四工具组合的原生拒绝与运行中取消分别按 E18/E19 验证，不以早期实验充抵。

当前组合的首次 DeepSeek locked-session 原生探针通过，但外层 operator 的旧端口 bind 检查失败，原报告保持 failed；其原生 PID 为0、容器移除和日志完整已在该报告确认。补充报告随后证明端口无 LISTEN，普通 bind 与 SO_REUSEADDR bind 均成功。没有直接捕获失败当时的 TIME_WAIT，不能将这一推测写成历史实测事实。修正后的检查用于新的 Qwen 用例，原生执行与停止、移除、日志和端口释放均通过；没有重复付费重跑已通过的 DeepSeek 用例。

当前组合的首次运行中取消，因旧判据要求常驻网关 PID 退出而记录失败。核对固定上游调度器及真实持久工具结果后，明确受控合作式 handler 的完整 Future 返回、执行器 join、原生 cancelled 与整个网关进程退出是不同事实；原失败及追加纠正报告保留，新的独立取消用例按 E19 通过。该结果没有扩展为非合作工具、任意外部副作用或供应商计费停止保证。

当前 locked-session 的第一次恢复检查要求旧 Run 可继续轮询，实际HTTP404导致失败，原报告保留。随后分别核对原生历史、实际重启和获准续聊，在新报告中记录 E20 的已验证子项与404限制；两份恢复报告均不把旧 Run 轮询恢复标为通过。平台 Run 持久化、原生历史续接与崩溃断点续跑仍按不同能力实现和验收。

OpenCode 的公开输入准备因官方下载连接超时失败后，仍按原始官方pin核对输入，没有替换包版本或跳过hash；离线构建的rg版本断言随后按实际版本行修正。首份镜像虽通过版本与链接检查，实际非root启动发现COPY创建的目录缺少遍历权限；修复目录权限并增加非root检查后形成独立新归档，旧镜像与失败保留。首次真实任务又因验收探针把原生相对worktree的权限路径误当作相对工作目录而拒绝固定输出请求；当次没有授予权限，原始失败与原生轨迹保留。修复仅允许权威worktree解析后的精确目标，并关联metadata.filepath及原生write callID，不能据此批准任意路径。

Octop Harness首次allow实际输入含末尾换行，模型提出的write内容没有该换行，精确HITL校验拒绝且无输出；没有放宽审批或改写失败。新的独立用例采用无尾换行随机输入后成功。4个历史用例的operator运行前没有保存独立脚本hash，不能追补成当时已记录；追加补证保留原报告hash，最终探针为后续运行保存源码快照。完整服务的下载和纯wheel失败也不表示其不可部署，后续sdist构建及服务资格独立推进。

首轮NIC资格的合法路径失败之外，结束端口bind检查也报错；随后确认两端口无LISTEN/TIME_WAIT，不重写原报告，也不猜测首次报错一定由TIME_WAIT导致。第二轮使用一致的TCP地址复用语义、读取LISTEN并增加网络诊断，清理通过；合法TLS仍失败。因此两次均不能算网络隔离通过，更不能关闭M0-05。

## 网络资格的管理员核对与解除条件

本轮已完成授权诊断和精确临时例外下的复测：实际UFW日志命中9条对应Guest/bridge/10018的拒绝；第三轮全部82检查通过，原策略已恢复。以下保留可复现的只读诊断步骤，由有权限的管理员在实际任务资源存在期间执行；`HARNIQO_VM_UUID`、`HARNIQO_BRIDGE`、`HARNIQO_GUEST_IP`、`HARNIQO_GATEWAY`必须来自该次私有ownership/实际XML，不复用已回收资源的名称。

只读诊断，不添加或删除规则：

```bash
sudo ufw status verbose
sudo ufw show raw
sudo nft list ruleset
sudo iptables-save -c
sudo ip6tables-save -c
sudo ebtables-save
virsh --connect qemu:///system dumpxml "$HARNIQO_VM_UUID"
ip -j link show dev "$HARNIQO_BRIDGE"
ip -j neigh show dev "$HARNIQO_BRIDGE"
ss -H -ltn '( sport = :10018 or sport = :10019 )'
sudo tcpdump -nn -i "$HARNIQO_BRIDGE" -c 40 -s 96 \
  "arp or (host $HARNIQO_GUEST_IP and host $HARNIQO_GATEWAY and tcp port 10018)"
```

完整规则/抓包只保存到0700私有证据目录，不粘贴进公开报告。结合canary实际监听、SYN/返回包与现有计数器判断阻断位置；若系统使用其他后端，管理员读取相应有效规则，不能因某一命令空输出推断没有过滤。

**变更与上述只读步骤分开。** 确认规则原因后，管理员只为该次/生产固定策略登记的专用bridge、Guest来源、网关目标和TCP10018提供最小入站例外；10019、管理端口、metadata、peer和直接外网继续拒绝。变更应记录精确规则身份与回滚方法；不得flush规则集、放开全局INPUT、修改既有业务网络或借特权Docker绕过。资格脚本默认不变更规则；显式`--allow-owned-host-canary-input`才允许两条完全绑定本任务身份的临时例外，停止VM后按完整selector精确撤销并核对有效策略。特权writer结果未决时不得宣称已恢复或删除其网络。第三轮已按此方式通过，旧失败保留；生产UID、资源硬配额与watchdog/fencing仍各自验收。

## 下一步准入

M0-03 的固定受控组合技术验证已完成；M2/M3仍须实现真正的 Adapter、通用工具与用户工作流，不能把准入实验当作已交付集成。M0-04当前技术选择已完成，继续M0-05生产执行边界及M0-07外部资源验证；M0-02 在完整准入后冻结，M0-08 汇总决定与排期。当前没有可安装桌面应用或已通过的生产 SaaS 隔离声明。
