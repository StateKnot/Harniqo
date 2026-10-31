# M0-05 QEMU 宿主 CPU 与内存限额

日期：2026-10-31。状态：固定v2宿主CPU/内存/swap边界及CPU压力子项 `VERIFIED`；M0-05 保持 `IN_PROGRESS`。

本轮在[已验证的固定实例组生命周期](m0-resource-broker-qualification.md)上增加固定资源策略。资格流程必须调用正式 Resource Broker Module，由其冻结策略、创建精确 UUID 并核对真实资源；不能在测试脚本另造一套不进入正式配置 hash 的限额。整体隔离选择及剩余要求沿用[执行网络评审](m0-execution-network-review.md)和[隔离 ADR](../docs/adr/0002-isolated-execution.md)。

## 已核对的上游行为

### CPU 必须限制整个 domain

libvirt 的 `cputune.period/quota` 按 vCPU 生效，`global_period/global_quota` 才约束整个 domain 的 CPU 带宽。固定模板使用后者，覆盖 QEMU 的 vCPU、emulator 和 I/O 线程合计；不能以 Guest 仅看到一个 CPU 代替宿主限额。v10.0.0 的启动路径在设置各线程调度前应用全域 CPU 策略，控制器缺失且请求了限制时返回错误。[domain XML](https://libvirt.org/formatdomain.html#cpu-tuning)、[全域设置实现](https://raw.githubusercontent.com/libvirt/libvirt/v10.0.0/src/hypervisor/domain_cgroup.c)、[QEMU 启动调用](https://raw.githubusercontent.com/libvirt/libvirt/v10.0.0/src/qemu/qemu_process.c)。

例如 `global_period=100000`、`global_quota=100000` 表示整个 domain 每100ms获得100ms CPU 时间；这是最多一个逻辑 CPU 的带宽，不是独占物理核。固定 profile 不启用 realtime 调度。内核 CFS 带宽机制限制的是普通公平调度任务；其配额分配和节流具有周期、层级及调度粒度，不能用一次瞬时采样要求绝无偏差。[CFS 带宽控制](https://docs.kernel.org/scheduler/sched-bwc.html)。

### Guest RAM 与 QEMU 内存上限分别配置

`memory/currentMemory=1024MiB` 规定 Guest RAM；`memtune.hard_limit` 是另外的宿主限制。libvirt 明确警告：QEMU 还有运行开销，错误估计的 hard limit 可导致内核杀掉 domain。候选固定 profile 为 Guest1GiB、QEMU hard limit2GiB，留出的1GiB仅是本次 sealed 模板待实测的余量。它不是任意 Harness、设备、热插拔、HugeTLB 或正式2vCPU/4GiB slot 的通用公式；先保存实际启动及运行峰值，再决定是否足够。[内存调优及警告](https://libvirt.org/formatdomain.html#memory-tuning)。

### libvirt10 的 v2 swap 映射需要按实际值解释

上游 XML 文档把 `swap_hard_limit` 描述为 memory+swap；但已核对的 v10.0.0 实现存在不同映射：公共层直接转发该值，v2 后端把该 KiB 数值乘1024直接写入 `memory.swap.max`，没有减去 hard limit。相同后端将 hard limit 写入 `memory.max`。因此，在这条固定 v2 路径上，2GiB hard limit 加2GiB swap hard limit 的实际边界是 **计费 RAM2GiB、swap2GiB，合计至多4GiB**，不是禁用 swap；必须以本机读回数值核验，不能仅根据 XML 字段名断言。[公共调用层](https://raw.githubusercontent.com/libvirt/libvirt/v10.0.0/src/util/vircgroup.c)、[v2 内存与 swap 实现](https://raw.githubusercontent.com/libvirt/libvirt/v10.0.0/src/util/vircgroupv2.c)。

动态 memtune 还检查 hard limit不得大于swap hard limit；不应把零值配置当作已实现禁止 swap。首版明确限定已验证的 cgroup v2 语义，发现控制器、文件或读回值不符就拒绝准入；不静默兼容 v1，也不为本次实验直接更改宿主的全局 swap、cgroup 或服务配置。[v10 参数校验及设置顺序](https://raw.githubusercontent.com/libvirt/libvirt/v10.0.0/src/hypervisor/domain_cgroup.c)。

## 如何获取有意义的宿主证据

限额与计数读取必须定位到本次确定 UUID 所属的 domain 父 cgroup，核对其 QEMU 进程及线程归属；不能读取包含其他 VM 的 `machine.slice`，也不能只看 `emulator` 子级而遗漏 vCPU。libvirt v2 本身会处理 emulator/libvirt 子级，说明“主 PID 所在路径”等同“全域限额路径”的推断不成立。祖先限额也需要保存，以解释更严格的有效限制。[v2 路径检测及嵌套实现](https://raw.githubusercontent.com/libvirt/libvirt/v10.0.0/src/util/vircgroupv2.c)、[systemd255 层级控制](https://www.freedesktop.org/software/systemd/man/255/systemd.resource-control.html)。

具体到 v10 的 systemd 创建路径：`virCgroupNewMachineSystemd` 在 scope 下创建名为 `libvirt` 的 nested cgroup；`virCgroupGetNested` 优先返回它，CPU period/quota、memory hard limit及swap setter都调用这个选择器。因此应分别核对 **scope祖先** 与 **scope/libvirt全域控制层**；scope上的 `cpu.max=max` 不能否定子层实际有限配额，也不能据此只选择一个“看起来匹配”的任意后代。应确认已知层级的全部目标线程 containment、固定控制值及祖先限制。`emulator/vcpuN/iothreadN` 又是全域层下的线程子级。[nested选择器](https://raw.githubusercontent.com/libvirt/libvirt/v10.0.0/src/util/vircgrouppriv.h)、[创建与setter实现](https://raw.githubusercontent.com/libvirt/libvirt/v10.0.0/src/util/vircgroup.c)。

每台 VM 至少记录以下值，保留单调时间及采样前后身份核对：

| 文件 | 目的 |
| --- | --- |
| `cpu.max`、`cpu.stat` | 读回配额/周期，保存 `usage_usec`、`nr_periods`、`nr_throttled`、`throttled_usec` 的前后变化 |
| `memory.max`、`memory.current`、可用时的 `memory.peak` | 核对硬上限和实际计费占用；峰值文件缺失不能编造峰值 |
| `memory.events`、可用时的 `memory.events.local` | 分清层级事件、触及max、OOM与OOM kill；零事件仅说明观察窗口内未发生 |
| `memory.swap.max/current/events` | 分别记录 swap 限额、实际使用与失败事件；配置上限不等于已制造过 swap 压力 |
| `cgroup.procs`、相关线程的 cgroup 归属 | 确认所有目标 QEMU 线程均在所核对的限额层级内 |

内核 `memory.max` 与 `memory.swap.max` 分别限制计费内存和 swap；前者在无法回收时可触发 cgroup 内 OOM。`cpu.stat` 的用量包含后代任务。采样和压力结果须分别登记，不把读取到了有限数值当作完整耗尽验证。[Linux cgroup v2](https://docs.kernel.org/admin-guide/cgroup-v2.html)。

## 本轮真实用例与发布口径

1. 经同一 Resource Broker Interface 新建独立 A/B 两台 KVM，固定策略进入配置 hash 和归属核对。正常启动、原11项 sealed Guest检查及已有期限/故障停止判据继续要求通过。
2. 两台都核对实际父 cgroup、CPU全域配额和有限内存/swap值。若控制器未生效、对象不符或不可读取，不得把结果计为通过。
3. 只在 A 的 Guest 内运行有固定截止时间的 CPU 饱和 pulse，保存 A 的真实节流计数增量；B 只回复采样窗口内新发出的随机挑战，返回绑定本执行 nonce 的有界应答，确认对照 VM 仍可用。Guest 不接收宿主 PID、cgroup 路径或管理凭据。
4. 停止后仍依赖正式 Module 的宿主 StopEvidence；未知创建/停止不释放写入层，日志或归档失败继续保留材料。不能以 Guest“压力已结束”代替宿主停止证明。

普通获准用户可向 `qemu:///system` 的特权 daemon 提交域配置，由 daemon 设置其管理的 domain cgroup；这不要求用户直接获得 cgroup 写权，也不是 `qemu:///session`。v10明确拒绝 session 模式的内存调优。能否在本机生效仍以真实 domain 和控制器证据为准，不因此声称专用生产 UID 或最小 libvirt 授权已经交付。[QEMU 调优与权限检查](https://raw.githubusercontent.com/libvirt/libvirt/v10.0.0/src/qemu/qemu_driver.c)。

本轮不制造宿主 QEMU 的 memcg OOM。Guest 内分配失败或 Guest OOM 只能证明 Guest 自身事件，不能替代宿主 `memory.max` 触发 OOM 的证据。因此实际 CPU 节流与读回的内存/swap硬边界可以独立成为已验证子项，**宿主 OOM、内存耗尽恢复、磁盘/日志/spool硬配额、块I/O与网络带宽限制、专用服务身份及生产 Egress 仍未验收**。压力对照也不代表整机 N−1 容量或跨故障域灾备通过。

## 实测结果

独立新组 `f8cea05fd378` 于2026-10-31T03:31:32Z至2026-10-31T03:34:09Z完成同源真实验收，全部16个必需门槛通过，errors/cleanup_errors均0。[脱敏报告](../docs/operations/resource-limits-kvm-2026-10-31.json)记录原始报告SHA-256 `bfa5fb8df20fdf75d4ceefd993388811b9f142ceb2a132c548a41963e5c11766` 及全部源码摘要；原始private报告及签名键留服务器。


### 前置真实失败记录（保持原结果）

- `b15e7a68055f`：第一版实际读回失败后A已由正式Module停止，B尚未登记；当时终态未保留精确限额失败原因。原自动FAILED及诊断材料保留。
- `b089d8ad7276`：新增安全诊断后明确为`resource_limits_cpu_mismatch/control_values`；A安全停止，未留下本轮活动VM，base和全部既有libvirt对象不变。官方GetNested路径随后确认实际控制在`scope/libvirt`，没有把scope的无限值当作已设限。
- `6e58a694f5a7`：两台VM的实际限额读取及22项Guest检查通过；原一次性GuestStreams在收完报告后关闭通道，导致随机挑战阶段缺少连接，保持FAILED。该轮宿主停止、console及精确清理通过，错误1、清理错误0，本轮资源已回收。

前两组因B未登记而缺少整个实例组的两份正式停止凭据，均保留各16GiB写入层与相关ACL/journal；不凭当前domain清单为空删除。既有更早生命周期三组48GiB未知派发材料仍保留，合计保留80GiB（不同原因分别记账）。原始私有报告、凭据及路径不进入公开摘要。


### 最终实测

两台KVM各11项sealed Guest检查通过。实际CPU每100ms/100ms、memory.max和memory.swap.max各2,147,483,648 bytes，所有QEMU线程归属已核。A的实际CPU采样间隔6.137秒，CPU使用增量6132349微秒，节流次数增加8、节流时间增加3159微秒；6秒挑战窗口内B完成3个新随机挑战应答。采样前后PID/starttime/CPU控制组相同；内存峰值分别463011840/471539712 bytes、OOM/OOM kill计数0，仅说明本正常窗口未发生OOM。

原launcher和watch的两个SIGKILL、期限与恢复停止、签名/旧代次/过期/复活拒绝继续通过；停止证据均为libvirt_destroy_then_absent。2份console完整归档，journal/anchor/ownership签名和终态代次在服务器内核验，base及原有3个domain/network/filter配置状态不变。worker退出且宿主停止确认后，本轮临时镜像/socket全部精确回收；旧失败及80GiB保留材料不借新组通过删除。

服务器262项纯组件通过（55核心、17操作者、7压力门槛、183原套件），零失败/错误/跳过，模型调用0；原生VM证据与组件及GitHub CI分别登记。v1默认关闭报告通道的行为保留，v2显式保留经nonce校验的通道用于挑战，真实socketpair集成覆盖该完整过程。首次限额失败不可通过重读解停，失败原因/phase在终态保留。

本子项不是完整宿主硬配额或生产隔离通过：宿主OOM与耗尽恢复、磁盘/日志/spool硬配额、块I/O/网络带宽、专用服务身份、生产Egress及控制面全局fencing仍未验收，M0保持4/8。

独立 Standards 与 Spec 复核分别对冻结快照 `dd75c86214e653ec7498182f917d048cd14cb864` / 基线 `0151f541b90b53100ebdfac7b500f8705503853b` 完整diff检查，均无未关闭发现。前轮发现的初次失败重读解停、采样时钟开销、旧心跳误计与一次性collector断开已修正，并由7项合成压力/通道回归及最终同源VM试验分别覆盖。
