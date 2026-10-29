# M0 执行证据索引

日期：2026-10-29 · M0 状态：`IN_PROGRESS`，8 项中 **2 项已验收**。本索引记录已取得的证据及其范围，不表示 M0、应用功能或生产发布已完成。

任务定义见 [BACKLOG](BACKLOG.md)，阶段门槛见 [ROADMAP](ROADMAP.md)，验收规则见 [ACCEPTANCE](ACCEPTANCE.md)。这里只发布脱敏摘要；私有报告位置均为工作区相对路径模式，`<…>` 代表由受控环境记录映射的实际文件名。原始报告、日志、配置、凭据和镜像不随本索引公开。

## 任务状态

| 任务 | 当前状态 | 已取得结果 | 尚需完成 |
| --- | --- | --- | --- |
| M0-01 产品规格 | ✅ `ACCEPTED` | [PRD](../product/PRD.md) 固定两种部署操作者、权限矩阵、导航和六条输入/结果/失败流程；8 项文档验收检查通过 | 应用实现由后续里程碑交付 |
| M0-02 机器契约 | `IMPLEMENTED` | 60 个 Schema 定义、18 条 OpenAPI 路径、107 个正反例、15 个生命周期案例通过验证；干净服务器按完整 hash 锁安装后复跑通过 | 结合真实原生 Harness 与生产隔离准入冻结契约 |
| M0-03 首个 Harness | `IN_PROGRESS` | 两个 Provider 协议探针通过；Hermes 镜像已构建，两个独立模型配置下的原生工具/文件/鉴权/SSE 子项有真实证据；重启历史与一次原生续聊通过 | canonical Run 未报告有效模型 ID，严格准入失败；运行中取消确认、人工审批拒绝及完整可替换模型准入仍待完成 |
| M0-04 多 Harness | `TODO` | 候选及需验证能力已列入计划 | 候选实际运行、能力比较、正式目标和 Octop 分项结论 |
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
| E07 | Hermes 固定运行包构建完成：Python3.14.10、SQLite3.53.4/FTS5、74 个分发包；Docker archive 为 658,780,160 bytes，SHA-256 `404bcc2246c0ada24c3925c95d2579abd8f62f139180fed0500e2d574a70e270`。归档及 provenance 已登记在受控 `images/` 清单；Harness 准入仍为 false | [原生 Hermes 准入组合](../../deploy/qualification/hermes/README.md) | `images/containers/hermes/git-a382af23f4c0/<image-id>.docker.tar` 及 provenance；`.private/validation/<offline-build>.log` |
| E08 | Hermes 受控工具及原生驱动的 14 项组件测试在服务器通过；覆盖临时文件、随机回执/hash、权限/符号链接/非法 Unicode 拒绝、SSE 游标、canonical 终态形状、伪造结果拒绝，以及缺模型元数据时保留真实子项证据 | [原生探针与测试范围](../../scripts/qualification/HERMES-PROBE.md) | `.private/validation/hermes-tools-evidence-tests.json` |
| E09 | Provider 探针 26 项 HTTP/解析/配置组件测试在服务器通过；仅使用受控本机服务，不调用真实模型 | [探针组件测试](../../scripts/qualification/test_probe_model.py) | `.private/validation/model-probe-tests.json` |
| E10 | 离线构建输入：74 个官方 wheel、49,184,573 bytes 全量核对原锁及导出 hash；同 Python3.14 禁网 fresh-cache 精确同步 74 版本通过。上游过滤器核对 1 项已明确 override、0 项非预期冲突；输入复制的 14 项篡改/size/名称/符号链接/竞态组件测试在服务器通过 | [供应链核验](../../research/m0-dependency-verification.md)、[输入边界测试](../../scripts/qualification/test_build_hermes_image.py) | `.private/qualification/hermes-offline-inputs/<closure-id>/`；`.private/validation/hermes-wheelhouse-tests.json` |
| E11 | 两个独立模型配置的真实原生子项通过：新建 Session/Run、缺失和错误 token 被拒、两个工具调用及随机回执、7 条原生历史、文件 SHA、终态和真实 SSE 断线补取。DeepSeek 配置记录 42 事件/8605 tokens，Qwen 配置记录 34 事件/8906 tokens | [原生验收驱动](../../scripts/qualification/probe_hermes.py) | `.private/qualification/hermes-native/<case>/report.json`；首个用例另有只追加的 `subchecks.json` |
| E12 | 两个配置的严格原生准入均失败：canonical Run 的有效模型 metadata 为空；未将请求配置回显当作实际模型。补证不覆盖原始失败报告 | [固定原生 Run 实现](https://github.com/NousResearch/hermes-agent/blob/a382af23f4c067d23463b5781958684c2cda82bc/gateway/platforms/api_server_runs.py)、[探针模型门槛](../../scripts/qualification/HERMES-PROBE.md) | 与 E11 相同，错误码及 `model_evidence` 分别保留 |
| E13 | 两个配置的同一会话历史和 completed Run 均在实际容器重启后恢复；DeepSeek 配置另用不含旧 nonce/receipt 的新提示，在重启后真实续聊回忆随机回执，且没有调用工具/改变文件，报告 3212 tokens | [原生运行包与持久目录](../../deploy/qualification/hermes/README.md) | `.private/qualification/hermes-native/<case>/subchecks.json`、`continuation.json` 或 `recovery-cleanup.json` |

E02/E03 证明当前契约及验证环境可复现，不证明 API 服务、数据库事务、授权或队列已经实现。E04 是受控工具协议的前置检查，执行的是探针自己的固定工具；它没有运行原生 Hermes，不计作 Harness 准入。两次共 1991 tokens 是供应商报告用量，不是已核对账单或费用硬封顶。

E05 的 Guest 没有 NIC。该结果证明真实 KVM 启停、该拓扑下的隔离检查、冷启动磁盘 canary 持久化及安全回收；它不证明生产网络可用。允许模型/工具访问同时拒绝管理网、metadata 和横向访问的选择性出网，仍未验收。原生 Harness 恢复、Runner 租约与 fencing、CPU/内存/磁盘耗尽、故障接管和灾备恢复也未由本次证明。

E06 使用合成 Guest 和真实 UNIX socket，没有启动 VM、创建 TCP 端口或调用模型；测试通过只证明失败留证逻辑。该测试不替代 E05 的硬件证据，也不重新计算此前真实运行的结论。

E08/E09 及 E10 的输入复制测试是组件验证。E11/E13 来自真实原生执行，仅代表所列子项；两个原生配置的有效模型仍未被接口报告，整体准入按 E12 保持失败。上述 token 数均为相应 Provider/Harness 报告口径，不代表已对账费用；不同用例分别记账，不混成单次运行。

## 失败历史与证据保留

前两次 KVM 运行的 Guest 检查已通过，但整体结果均为失败：第一次 console 文件被 libvirt 改变所有者后归档读取失败；第二次发现 libvirt/virtlogd 更换了日志 inode，完整性检查拒绝通过。两次原始失败报告及所保留的诊断材料继续存在于各自的私有运行目录；第三次成功没有覆盖或改写这些记录。

修复后由宿主绑定独立 console UNIX socket，QEMU 只连接 socket，日志文件由宿主以 0600 保存并限制容量；第三次取得四份完整日志和无错误回收证据。随后补充每轮 `status`、`phase`、缺失 Guest、`failed_phase`、`validation_error` 及已收报告快照。超时后补收报告只补充证据，不会把失败轮次改为通过；已确认停止的 overlay 仍按规则回收，JSON 和 console 继续留存。

Hermes 的在线构建先后因官方 Pillow、OpenAI wheel 连接超时失败；离线构建第一次暴露原锁部分 wheel 不带可选 size，修正为已知 size 严格匹配、缺省时有界读取并始终验证完整原锁 SHA 后成功。原始失败及后续成功记录均保留。离线安装实验中的 metadata 重解冲突、低 PID/默认并发超时也独立记录，采用完整 frozen export 的精确同步保留上游 override，未更换包版本或放弃 hash。

首个原生探针在模型门槛前退出，补采的工具/文件/重启证据写入单独报告。之后修正探针，先保存已验证子项，再区分模型未报告与不匹配；没有重写原始失败。一项取消实验在发出停止前已完成，记录为未验证，没有据此声称运行中取消成功；人工审批拒绝也未验收。

## 下一步准入

M0-03 先解决或明确替代 canonical 有效模型证据缺口，再复验可替换模型、运行中取消和审批拒绝；当前 Hermes 组合保留候选身份。继续 M0-04 的候选比较、M0-05 的生产执行边界及 M0-07 的外部资源验证；M0-02 在真实准入后冻结，M0-08 汇总决定与排期。当前没有可安装桌面应用、正式认证的 Harness 集成或已通过的生产 SaaS 隔离声明。
