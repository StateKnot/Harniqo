# M0-05 有界原始 console 写入

日期：2026-10-31。状态：正式Module、指定服务器180项回归及真实双KVM的22项门槛已通过，本子项 `VERIFIED`；M0-05 保持 `IN_PROGRESS`，M0仍4/8。

本子项把原始 console 的空间准入、写入上限、文件身份与错误处理放进正式 Runner Module，由现有资格收集器调用同一 Interface。它承接[执行网络评审](m0-execution-network-review.md)中日志无界增长的缺口，但只声明**可信宿主写者拥有的有限 console 字节预算**。它不交付通用宿主文件系统配额、SQLite critical-event spool或全部libvirt日志。Module本身不选择/停止VM；本轮固定双VM的宿主停止证据在末节单独登记。

## 核对过的上游语义

### 空间预留与写入上限是两件事

Linux `fallocate(fd, FALLOC_FL_KEEP_SIZE, 0, capacity)` 为范围分配空间，同时保留原 EOF，适合新建 append 日志。成功分配的范围内，后续写入具有不因缺少空间而失败的保证；但分配按块进行，实际范围可能大于请求。**预分配本身不禁止写者继续扩展文件**，容量上限仍须由唯一可信写者在每次写前执行。这里不把 payload 上限等同于文件系统元数据、目录、inode 或整机空间的严格上限。[fallocate(2)](https://man7.org/linux/man-pages/man2/fallocate.2.html)

`posix_fallocate` 会在必要时扩展逻辑文件长度，glibc 在不支持原生调用时还可能采用具有并发风险的模拟路径。选定 Linux Module 因此直接要求原生 KEEP_SIZE；`ENOSYS`、`EOPNOTSUPP` 或空间不足必须拒绝准入，不退回 sparse 文件或通过填零模拟成功。[posix_fallocate(3)](https://man7.org/linux/man-pages/man3/posix_fallocate.3.html)、[fallocate 错误](https://man7.org/linux/man-pages/man2/fallocate.2.html#ERRORS)

`st_size` 是逻辑长度，`st_blocks` 是已分配的512字节块数。新 sink 应记录实际文件系统、设备/inode、capacity、零初始长度和预留块数，再在写入及封存时核对；不能以逻辑容量或 `df` 轮询代替实际分配。块数只作为该文件的观测证据，不把共享块或目录大小的累计值当作整机释放量。[stat(3type)](https://man7.org/linux/man-pages/man3/stat.3type.html)

`RLIMIT_FSIZE` 限制进程所创建文件的最大长度，越界可发送默认致命的 `SIGXFSZ`，或在信号被捕获后返回 `EFBIG`；它不是多文件合计预算，也不预留块。给共享 Broker 进程设置低值还会影响其 journal 等其他写入，因此不能代替独立 console Module 的容量政策。[getrlimit(2)](https://man7.org/linux/man-pages/man2/getrlimit.2.html)

### 文件持久化必须包含目录

成功 `write` 可能仅完成部分字节，也不代表数据已持久；写回错误可在后续写入或 `fsync` 才报告。Module 必须有界重试短写，拒绝零进度，保留实际写入长度及首个失败原因；不能把未完成 chunk 当作完整持久接收。[write(2)](https://man7.org/linux/man-pages/man2/write.2.html)

`fsync(file_fd)` 同步文件数据及所需元数据，但不保证创建它的目录项已落盘；新文件或归档目录项需要显式 `fsync(directory_fd)`。`fsync` 本身仍可能失败，任何成功结论必须等同步完成；正常调用成功也不能替代真实断电恢复验收。[fsync(2)](https://man7.org/linux/man-pages/man2/fsync.2.html)

### flock 不能替代身份与线程串行化

本地 Linux `flock` 是 advisory lock。锁属于 open file description，`dup`/`fork` 产生的描述符共享同一把锁；对同一文件另行 `open` 才产生独立锁请求。最后一个相关描述符关闭后锁才释放。因此需要进程内串行化保护额度检查与写入，并用独立进程真实验证互斥；不能靠同一 FD 的重复 `flock` 证明线程互斥。[flock(2)](https://man7.org/linux/man-pages/man2/flock.2.html)

有文件权限的进程可以忽略 advisory lock。已打开的 FD 在路径被替换后仍指向原文件，也使“先检查路径、后随意 reopen”的方式不可靠。宿主写者及父目录拥有者属于受信身份；Module 要保持 FD、目录及 inode 绑定，拒绝 symlink、hardlink、替换和未知 owner，不宣称防住恶意同 UID 宿主进程。网络文件系统锁语义不同，本项只准入已实际验证的本地文件系统。[flock 约束](https://man7.org/linux/man-pages/man2/flock.2.html#NOTES)、[open(2)](https://man7.org/linux/man-pages/man2/open.2.html)

### 通用宿主配额需要部署权限

Linux quota 支持 user/group/project 限额；设置限额的 `Q_SETQUOTA` 和启用 `Q_QUOTAON` 需要 `CAP_SYS_ADMIN`。ext4 的 `prjquota` 还要求 project 文件系统特性与相应启用步骤。普通获准账号能为自有可写文件预分配，并不意味着它有权为共享宿主启用、重设配额或 remount。本子项不修改这些全局配置。[quotactl(2)](https://man7.org/linux/man-pages/man2/quotactl.2.html)、[Q_SETQUOTA](https://man7.org/linux/man-pages/man2/Q_SETQUOTA.2const.html)、[Q_QUOTAON](https://man7.org/linux/man-pages/man2/Q_QUOTAON.2const.html)、[ext4 project quota](https://man7.org/linux/man-pages/man5/ext4.5.html#Mount_options_for_ext4)

## 正式 Module 的责任

下列是本子项的设计要求，须由实现与实际证据核对，不是上游保证或已有通过结果。

- 可信配置冻结每个 sink 的路径、容量及资源归属，固定实例组限制 sink 数量；Guest 只能贡献原始字节，不能指定宿主路径、配额、FD 或 libvirt UUID。
- sink 在受信私有目录中独占新建，权限0600、普通单链接文件；先完成原生预分配、文件/目录同步及身份核对，再允许接收。既有文件不能被隐式截断、重置额度或当作新的空 sink。
- append 把额度检查与写入作为串行操作，任何输入都不能让文件超过 cap。超额必须返回稳定错误、记录实际保留/未保留字节并锁存失败；原始 console 不是具有事务 ACK 的事件队列，不能静默丢弃后继续报告正常运行。
- 任何容量、写入、同步、身份或封存错误都必须显式暴露。后续成功调用不能清除原失败；归档不把失败前缀包装成完整日志。
- 收集器发现失败后应停止接受该资源正常执行，并进入既有 Broker 撤销/核对流程。日志 flush、fsync、close 或归档失败不能阻止其他资源的停止尝试；无法确认物理停止时仍保留磁盘和恢复材料。
- authority journal 与 console 使用不同预算。归档产生第二份副本时，需要单独准入并记录峰值；原始 sink 的上限不约束报告、base、QEMU stderr、libvirt daemon 日志或其他宿主写者。
- SQLite critical-event spool 仍按[架构](../docs/architecture/ARCHITECTURE.md)另行实现其持久 ACK、游标、重传和容量策略；本原始字节 Module 不作替代。

## 有限验收计划

下表是验收判据；真实结果与组件故障注入在末节分别登记。结果关联源码hash和私有报告，不用模拟结果补填原生证据。

| 门槛 | 必需证据及通过条件 |
| --- | --- |
| 环境及准入 | 指定服务器普通账号；运行前读 `df`，保留至少50GiB；记录内核/文件系统及挂载信息；仅创建本轮私有小文件，不修改宿主挂载、quota、服务或防火墙 |
| 原生分配 | 调用正式 Module 新建小容量 A/B sink；KEEP_SIZE 返回成功，初始 EOF为0，实际预留块已记录；同步及身份核对通过；不以 mock 代替此门槛 |
| 容量与对照 | A写至精确 cap 后尝试越界，明确失败且任何时刻不超 cap；B在A失败之后接收本轮新随机挑战字节并完整封存；分别核对文件长度、内容与摘要 |
| 实际互斥 | 独立进程另行打开同一受信对象并验证锁冲突；同一 FD 的 dup 不计第二写者；线程并发 append 也不能越界或打乱已声明的写入顺序 |
| 真实 socket 集成 | 现有 GuestStreams 通过正式 Module 收集两条原始 console；核对路径/FD归属、不静默吞掉越界错误、B证据完整；它只证明宿主流收集，不称为真实 Guest 或 VM 停止 |
| 故障回归 | 受控注入短写、零进度、EIO、预分配失败、fsync/close失败及身份替换；拒绝 symlink/hardlink/未知 owner/重复新建；首个失败保留且停止尝试不被日志异常短路。明确标记这些是组件故障注入 |
| 归档与回收 | 核对原始与归档副本的身份、实际长度及摘要；失败不能标完整；留存报告在本轮可回收目录之外，只删除已核对身份的自有文件；结束再次读 `df`，按实际变化报告空间 |

单凭原生文件/Socket门槛，只允许登记`bounded_console_writer_verified=true`；VM停止另需真实KVM、已绑定UUID的Broker StopEvidence及B健康证据。本轮固定双VM取得了这些证据，但没有内核文件系统配额与相应耗尽证据，通用宿主硬配额仍未验证。M0-05其余退出条件保持原状态。


## 实现、修正与实际结果

正式[Module与Interface](../apps/runner/CONSOLE.md)由共享GuestStreams调用；不另建模拟存储来通过资格。配额拒绝整块输入，成功append逐次fsync；物理EOF与完整成功append的durable前缀分开。归档使用另一个同容量BoundedConsole，复制最多64KiB/次，回读EOF/长度/SHA、再次核对源摘要和两侧身份，关闭成功后才标saved。任意新归档错误保留源；失败前缀明确不完整。

两轴复核发现的归档独立预留/摘要核对和阻塞stdout期限缺口均已修正后验证：A独立SIGALRM100秒覆盖阻塞写，B控制窗口120秒；manifest分别记录。组件用真实满管道及缩短注入时钟验证alarm，不声称在Guest里实际等满100秒。首轮177组件0失败/错误但1平台跳过，整组不计通过；原报告保留。平台负向用例改为显式不支持分支，最终Linux180项0失败/错误/跳过，包括26存储、11收集器、10故障操作者、56Broker、22既有操作者、7CPU压力、42内存helper及6网络护栏。其中独立Python子进程用新open与dev/inode绑定，实证持有者关闭前LOCK_NB冲突、关闭后获得锁；该case不模拟内核锁。短写/EIO/fsync/同长度归档损坏等均是组件注入，不能写成真实宿主ENOSPC。

真实组`74336c2285a8`于`2026-10-31T07:26:11Z`至`07:28:50Z`通过：两KVM共22项sealed检查和22项必需门槛，错误/清理错误均0。A/B每个source和archive都实际预留2,097,152bytes。三次128KiB输出分别保存完成后，B各有新挑战；最终burst使A在已保存2,097,075bytes时拒绝越过2MiB的94byte输入，后续失败锁存。最终只读排空累计丢弃1,234bytes，失败与截断标识保留，未标为完整日志。B完整保存81,939bytes，无丢弃，并在A超限后/停止前、宿主停止后各有新挑战，共五个B新挑战逐一与归档内容核对。

正式Broker以`console_capacity_exceeded`撤销已认证A，得到`libvirt_destroy_then_absent`；停止前原launcher已SIGKILL且A仍活动。持久重建后终态相同，旧Assignment/Grant分别以`generation_stale`/`resource_terminal`拒绝；B原watch SIGKILL及过期重建停止链也通过。两QEMU在分段窗口的身份及限额不变，样本内host OOM事件为0；不是Host OOM资格。归档源/目标SHA一致，A前缀还与故障时摘要一致；签名journal/anchor/ownership XML在服务器内核验，实际秘密/原始console不导出。

原base、既有libvirt对象和协调锁不变，两份8GiB raw及seed及运行目录仅在宿主停止/归档/身份确认后回收。旧80GiB诊断材料不动。无模型调用、NIC、新TCP监听、sudo、宿主挂载/quota/全局服务或防火墙修改；无新可复用镜像归档。原始报告SHA-256为`88d4738a73ce6d6bfef3fd6097f6014271603f4ec29fa0eb80def583e64adf5d`；[脱敏原生报告](../docs/operations/bounded-console-kvm-2026-10-31.json)包含9份原生源码和15份组件源码摘要。

通过范围为固定v2可信写者console及其超限后的真实VM停止；`host_hard_quotas_verified=false`、`generic_filesystem_quota_verified=false`、`critical_event_spool_verified=false`、`power_loss_recovery_verified=false`。专用UID、完整Egress/授权预算、其他宿主日志、全局容量和宿主OOM仍须单独实现验收。M0仍4/8，M0-05整项不提前接受。
