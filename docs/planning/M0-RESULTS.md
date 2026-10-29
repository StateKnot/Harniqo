# M0 执行证据索引

日期：2026-10-29 · M0 状态：`IN_PROGRESS`，8 项中 **3 项已验收**。本索引记录已取得的证据及其范围，不表示 M0、应用功能或生产发布已完成。

任务定义见 [BACKLOG](BACKLOG.md)，阶段门槛见 [ROADMAP](ROADMAP.md)，验收规则见 [ACCEPTANCE](ACCEPTANCE.md)。这里只发布脱敏摘要；私有报告位置均为工作区相对路径模式，`<…>` 代表由受控环境记录映射的实际文件名。原始报告、日志、配置、凭据和镜像不随本索引公开。

## 任务状态

| 任务 | 当前状态 | 已取得结果 | 尚需完成 |
| --- | --- | --- | --- |
| M0-01 产品规格 | ✅ `ACCEPTED` | [PRD](../product/PRD.md) 固定两种部署操作者、权限矩阵、导航和六条输入/结果/失败流程；8 项文档验收检查通过 | 应用实现由后续里程碑交付 |
| M0-02 机器契约 | `IMPLEMENTED` | 60 个 Schema 定义、18 条 OpenAPI 路径、107 个正反例、15 个生命周期案例通过验证；干净服务器按完整 hash 锁安装后复跑通过 | 结合真实原生 Harness 与生产隔离准入冻结契约 |
| M0-03 首个 Harness | ✅ `ACCEPTED` · 已开发完成 | 固定四工具组合：两模型实际身份与 HTTP 目标、工具/文件/历史、原生取消、人工拒绝、重启后原生续聊均已验证 | 范围限于本次组合；locked-stream 的断线中断、重启后旧 Run 查询404按实声明，通用工具与生产隔离由后续任务验收 |
| M0-04 多 Harness | `IN_PROGRESS` | 候选源码已复核；OpenCode 修复镜像启动、原生读写与精确审批已有子项证据，12项组件测试通过 | 原自动探针超时保留；完整原生能力、候选实际比较、正式目标和 Octop 分项结论继续验证 |
| M0-05 执行隔离 | `IN_PROGRESS` | 第三次封闭 KVM 基线完整通过；此前失败记录保留 | 选择性出网、生产租户策略、lease/fencing、资源耗尽和故障恢复等完整准入 |
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

OpenCode 当前修复镜像的 image store ID 为 `sha256:ef392b1148602b61ad4ea1e9dcca9b05337adc3c7ef9fc97434d632c69b8373c`；归档499,582,464 bytes，SHA-256 `c50dd9d32477a4fc70a8e4ba0a985e6bb41688b633d77990869f7078e9e194bc`。构建新增非root用户的目录遍历、文件可读及配置目录不可写检查；原有只读根文件系统、非root用户和凭据权限要求保留。实际原生服务已启动，健康检查报告1.19.5，缺失及错误Basic认证均401，原生OpenAPI为3.1。该镜像与旧归档分别保存，功能测试结果独立记录。

E02/E03 证明当前契约及验证环境可复现，不证明 API 服务、数据库事务、授权或队列已经实现。E04 是受控工具协议的前置检查，执行的是探针自己的固定工具；它没有运行原生 Hermes，不计作 Harness 准入。两次共 1991 tokens 是供应商报告用量，不是已核对账单或费用硬封顶。

E05 的 Guest 没有 NIC。该结果证明真实 KVM 启停、该拓扑下的隔离检查、冷启动磁盘 canary 持久化及安全回收；它不证明生产网络可用。允许模型/工具访问同时拒绝管理网、metadata 和横向访问的选择性出网，仍未验收。原生 Harness 恢复、Runner 租约与 fencing、CPU/内存/磁盘耗尽、故障接管和灾备恢复也未由本次证明。

E06 使用合成 Guest 和真实 UNIX socket，没有启动 VM、创建 TCP 端口或调用模型；测试通过只证明失败留证逻辑。该测试不替代 E05 的硬件证据，也不重新计算此前真实运行的结论。

E08/E09 及 E10 的输入复制测试是组件验证。E11/E13 来自真实原生执行，仅代表所列子项；早期 canonical 配置的有效模型未被该接口报告，E12 的原始失败保持不变；新增 locked-session 与传输观察证据另见 E17。上述 token 数均为相应 Provider/Harness 报告口径，不代表已对账费用；不同用例分别记账，不混成单次运行。

## 失败历史与证据保留

前两次 KVM 运行的 Guest 检查已通过，但整体结果均为失败：第一次 console 文件被 libvirt 改变所有者后归档读取失败；第二次发现 libvirt/virtlogd 更换了日志 inode，完整性检查拒绝通过。两次原始失败报告及所保留的诊断材料继续存在于各自的私有运行目录；第三次成功没有覆盖或改写这些记录。

修复后由宿主绑定独立 console UNIX socket，QEMU 只连接 socket，日志文件由宿主以 0600 保存并限制容量；第三次取得四份完整日志和无错误回收证据。随后补充每轮 `status`、`phase`、缺失 Guest、`failed_phase`、`validation_error` 及已收报告快照。超时后补收报告只补充证据，不会把失败轮次改为通过；已确认停止的 overlay 仍按规则回收，JSON 和 console 继续留存。

Hermes 的在线构建先后因官方 Pillow、OpenAI wheel 连接超时失败；离线构建第一次暴露原锁部分 wheel 不带可选 size，修正为已知 size 严格匹配、缺省时有界读取并始终验证完整原锁 SHA 后成功。原始失败及后续成功记录均保留。离线安装实验中的 metadata 重解冲突、低 PID/默认并发超时也独立记录，采用完整 frozen export 的精确同步保留上游 override，未更换包版本或放弃 hash。

首个原生探针在模型门槛前退出，补采的工具/文件/重启证据写入单独报告。之后修正探针，先保存已验证子项，再区分模型未报告与不匹配；没有重写原始失败。早期一项取消实验在发出停止前已完成，保留为未验证；该批次当时也没有人工审批拒绝证据。后续受控四工具组合的原生拒绝与运行中取消分别按 E18/E19 验证，不以早期实验充抵。

当前组合的首次 DeepSeek locked-session 原生探针通过，但外层 operator 的旧端口 bind 检查失败，原报告保持 failed；其原生 PID 为0、容器移除和日志完整已在该报告确认。补充报告随后证明端口无 LISTEN，普通 bind 与 SO_REUSEADDR bind 均成功。没有直接捕获失败当时的 TIME_WAIT，不能将这一推测写成历史实测事实。修正后的检查用于新的 Qwen 用例，原生执行与停止、移除、日志和端口释放均通过；没有重复付费重跑已通过的 DeepSeek 用例。

当前组合的首次运行中取消，因旧判据要求常驻网关 PID 退出而记录失败。核对固定上游调度器及真实持久工具结果后，明确受控合作式 handler 的完整 Future 返回、执行器 join、原生 cancelled 与整个网关进程退出是不同事实；原失败及追加纠正报告保留，新的独立取消用例按 E19 通过。该结果没有扩展为非合作工具、任意外部副作用或供应商计费停止保证。

当前 locked-session 的第一次恢复检查要求旧 Run 可继续轮询，实际HTTP404导致失败，原报告保留。随后分别核对原生历史、实际重启和获准续聊，在新报告中记录 E20 的已验证子项与404限制；两份恢复报告均不把旧 Run 轮询恢复标为通过。平台 Run 持久化、原生历史续接与崩溃断点续跑仍按不同能力实现和验收。

OpenCode 的公开输入准备因官方下载连接超时失败后，仍按原始官方pin核对输入，没有替换包版本或跳过hash；离线构建的rg版本断言随后按实际版本行修正。首份镜像虽通过版本与链接检查，实际非root启动发现COPY创建的目录缺少遍历权限；修复目录权限并增加非root检查后形成独立新归档，旧镜像与失败保留。首次真实任务又因验收探针把原生相对worktree的权限路径误当作相对工作目录而拒绝固定输出请求；当次没有授予权限，原始失败与原生轨迹保留。修复仅允许权威worktree解析后的精确目标，并关联metadata.filepath及原生write callID，不能据此批准任意路径。

## 下一步准入

M0-03 的固定受控组合技术验证已完成；M2/M3仍须实现真正的 Adapter、通用工具与用户工作流，不能把准入实验当作已交付集成。继续 M0-04 的三目标实际比较、M0-05 的生产执行边界及 M0-07 的外部资源验证；M0-02 在完整准入后冻结，M0-08 汇总决定与排期。当前没有可安装桌面应用或已通过的生产 SaaS 隔离声明。
