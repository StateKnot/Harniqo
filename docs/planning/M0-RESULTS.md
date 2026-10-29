# M0 执行证据索引

日期：2026-10-29 · M0 状态：`IN_PROGRESS`，8 项中 **2 项已验收**。本索引记录已取得的证据及其范围，不表示 M0、应用功能或生产发布已完成。

任务定义见 [BACKLOG](BACKLOG.md)，阶段门槛见 [ROADMAP](ROADMAP.md)，验收规则见 [ACCEPTANCE](ACCEPTANCE.md)。这里只发布脱敏摘要；私有报告位置均为工作区相对路径模式，`<…>` 代表由受控环境记录映射的实际文件名。原始报告、日志、配置、凭据和镜像不随本索引公开。

## 任务状态

| 任务 | 当前状态 | 已取得结果 | 尚需完成 |
| --- | --- | --- | --- |
| M0-01 产品规格 | ✅ `ACCEPTED` | [PRD](../product/PRD.md) 固定两种部署操作者、权限矩阵、导航和六条输入/结果/失败流程；8 项文档验收检查通过 | 应用实现由后续里程碑交付 |
| M0-02 机器契约 | `IMPLEMENTED` | 60 个 Schema 定义、18 条 OpenAPI 路径、107 个正反例、15 个生命周期案例通过验证；干净服务器按完整 hash 锁安装后复跑通过 | 结合真实原生 Harness 与生产隔离准入冻结契约 |
| M0-03 首个 Harness | `IN_PROGRESS` | 两个获准模型的两轮受控工具协议探针通过；原生 Hermes 构建验证进行中 | 尚无已完成的原生 Hermes 构建或运行证据；需两个获准可替换模型配置的原生工具、拒绝、取消和续聊证据 |
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
| E07 | 固定官方 Python 基础镜像已拉取，SQLite3.53.4/FTS5 构建检查通过；首次原生运行包构建因官方 Pillow wheel 连接超时失败，证据保留。已加入任务专用下载缓存复验；尚无已完成的 Hermes 镜像或原生运行结果 | [原生 Hermes 准入组合](../../deploy/qualification/hermes/README.md) | `.private/qualification/hermes-build/<build-attempt>/`、`.private/validation/<build-console>.log`；不登记准入通过 |
| E08 | Hermes 受控工具及原生驱动的 10 项组件测试在服务器通过；覆盖真实临时文件、随机回执/hash、权限/符号链接/非法 Unicode 拒绝、SSE 游标及伪造结果拒绝 | [原生探针与测试范围](../../scripts/qualification/HERMES-PROBE.md) | `.private/validation/hermes-tools-final-tests.json` |
| E09 | Provider 探针 26 项 HTTP/解析/配置组件测试在服务器通过；仅使用受控本机服务，不调用真实模型 | [探针组件测试](../../scripts/qualification/test_probe_model.py) | `.private/validation/model-probe-tests.json` |

E02/E03 证明当前契约及验证环境可复现，不证明 API 服务、数据库事务、授权或队列已经实现。E04 是受控工具协议的前置检查，执行的是探针自己的固定工具；它没有运行原生 Hermes，不计作 Harness 准入。两次共 1991 tokens 是供应商报告用量，不是已核对账单或费用硬封顶。

E05 的 Guest 没有 NIC。该结果证明真实 KVM 启停、该拓扑下的隔离检查、冷启动磁盘 canary 持久化及安全回收；它不证明生产网络可用。允许模型/工具访问同时拒绝管理网、metadata 和横向访问的选择性出网，仍未验收。原生 Harness 恢复、Runner 租约与 fencing、CPU/内存/磁盘耗尽、故障接管和灾备恢复也未由本次证明。

E06 使用合成 Guest 和真实 UNIX socket，没有启动 VM、创建 TCP 端口或调用模型；测试通过只证明失败留证逻辑。该测试不替代 E05 的硬件证据，也不重新计算此前真实运行的结论。

E08/E09 是组件验证，不是原生 Harness 或真实模型集成结果。真实 Provider 结果单列在 E04；原生 Harness 结果须由其独立运行报告补充。

## 失败历史与证据保留

前两次 KVM 运行的 Guest 检查已通过，但整体结果均为失败：第一次 console 文件被 libvirt 改变所有者后归档读取失败；第二次发现 libvirt/virtlogd 更换了日志 inode，完整性检查拒绝通过。两次原始失败报告及所保留的诊断材料继续存在于各自的私有运行目录；第三次成功没有覆盖或改写这些记录。

修复后由宿主绑定独立 console UNIX socket，QEMU 只连接 socket，日志文件由宿主以 0600 保存并限制容量；第三次取得四份完整日志和无错误回收证据。随后补充每轮 `status`、`phase`、缺失 Guest、`failed_phase`、`validation_error` 及已收报告快照。超时后补收报告只补充证据，不会把失败轮次改为通过；已确认停止的 overlay 仍按规则回收，JSON 和 console 继续留存。

## 下一步准入

继续完成 M0-03 的真实原生 Harness 构建和运行、M0-04 的候选比较、M0-05 的生产执行边界，以及 M0-07 的外部资源验证；M0-02 在这些结果到位后冻结。M0-08 负责汇总剩余结论与排期。当前没有可安装桌面应用、正式认证的 Harness 集成或已通过的生产 SaaS 隔离声明。
