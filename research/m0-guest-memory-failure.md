# M0-05 Guest 内存耗尽与外部终止资格

日期：2026-10-31。状态：固定v2 Guest全局OOM及外部终止恢复子项 `VERIFIED`。本文约束本轮资格流程，不提前声明通过；M0-05 仍为 `IN_PROGRESS`，M0 已验收任务数不变。

## 本轮交付与范围

复用正式 [Resource Broker Module](../apps/runner/resource_broker/README.md) 的固定 `sealed-raw8g-q35-limited-v2`，新增 Guest 内存耗尽和确定 VM 被外部终止后的真实资格。操作者使用相同 `ensure`、`resource_limits`、`observe`、`renew`、`reconcile` 与持久停止证据；不另外实现一个供测试使用的 Broker，不用临时 JSON 替代控制面授权或预算。

两台 VM 仍各为 1 vCPU、Guest RAM1GiB、8GiB raw 工作盘、无 NIC/宿主文件共享。v2 仍以宿主 cgroup 约束整个 QEMU：CPU100ms/100ms、`memory.max=2GiB`、额外 `memory.swap.max=2GiB`。v1/v2 的配置描述、hash 和正常启动余量不为触发故障而修改。既有 [v2 来源及结果](m0-host-resource-limits.md)不因本轮新增记录被改写。

本轮分别验证以下事件，不能合并原因：

1. **Guest 内部 OOM**：A 中的受控分配进程被 A 自己的 Linux OOM killer 终止；A 的监控父进程、QEMU/VM 及 B 仍存活。
2. **操作者注入的 VM 外部终止**：在上述独立证据之后，资格操作者只终止已核对归属的 A；正式 Broker 根据宿主事实记录停止并持久防止旧授权复活。这是故障注入，不能写成 Guest OOM 自动导致 QEMU 退出，也不能写成宿主内核杀掉 QEMU。

Host QEMU memcg OOM、宿主整体 OOM、全局容量、磁盘/日志/spool 硬配额、专用服务 UID、生产 Egress、控制面权威授权/预算及全局 fencing 均不在本子项的通过范围。

## Linux OOM 证据为何需要分层

### Guest OOM 与宿主 cgroup OOM 是不同内核的事件

cgroup v2 的 `memory.max` 限制该层级记账内存，无法回收时可进入 cgroup OOM；`memory.events` 是层级计数，`memory.events.local` 才限定本层。`oom` 表示达到限额并面临分配失败的事件，`oom_kill` 记录该 cgroup 中被任一种 OOM killer 杀掉的进程数，二者不能互换。宿主 QEMU 的计数与 Guest 内 `/proc/vmstat` 不属于同一个内核或同一计数器。[Linux cgroup v2 官方文档](https://docs.kernel.org/admin-guide/cgroup-v2.html#memory-interface-files)。

因此，本轮在 A 内看到 OOM，最多证明这个受控 Guest 的内核事件；还须独立保存正式 Module 读取的宿主 A/B QEMU 身份、全部线程归属、实际 cap 与前后 `memory.events`，才能说明所观察窗口内宿主 QEMU 边界仍有效。宿主计数为0仅说明该采样窗口没有观察到相应事件，不构成宿主 OOM 恢复已经通过。

### `oom_kill` 增长和 SIGKILL 仍不能独自确定 victim

Linux v6.8 的公开 `__oom_kill_process` 路径先增加 OOM 事件计数，再向所选进程发送 SIGKILL，并记录包含 victim PID 和进程名的内核消息；`vmstat` 导出表包含 `oom_kill`。这说明计数、信号退出与 victim 日志可交叉核对，但计数本身没有目标 PID，单个 SIGKILL 也不携带“OOM 导致”这一原因。[OOM 实现](https://raw.githubusercontent.com/torvalds/linux/v6.8/mm/oom_kill.c)、[vmstat 导出](https://raw.githubusercontent.com/torvalds/linux/v6.8/mm/vmstat.c)。

据此采用以下**本项目验收推论**：只有“随机 nonce + 子进程 SIGKILL + Guest `oom_kill` 增量”仍不足以唯一排除同窗口其他进程发生 OOM 或其他主体杀掉该子进程。本轮强归因还要求 Guest 的新增内核 victim 记录精确匹配该 child PID 与本轮随机短进程名。进程开始前保存 PID、`/proc/<pid>/stat` 的 starttime、实际 comm、Guest boot ID 与计数基线；日志必须来自本轮压力之后，不能用旧日志或只匹配 `python3`。这是受控 fixture 中“worker 被 Guest OOM killer 选择并发送 SIGKILL”的强绑定，不是所有并发主体下唯一死因的证明，也不将文本格式提升为稳定内核 API。源码还存在 SysRq 人工触发 OOM 的路径；本 fixture 不使用该入口，须结合实际触页进度与内核记录，不能用人为触发伪装成真实分配压力。

所引 v6.8 源码用于解释机制；它不等于已核实实际 Guest 的发行补丁。真实报告须保存 Guest `uname`/发行版本和实际日志来源；不同内核格式或日志不可读时，该强归因条件应失败并保留诊断，不能通过放宽为任意 `Killed process` 字样取得通过。

### OOM 选择权与受保护监控

`oom_score_adj` 的范围是 -1000 到1000；该值影响 victim 选择，-1000 使对应进程免于 OOM killer 选择。降低到受限值可能需要 `CAP_SYS_RESOURCE`。它是 Guest 内受控 fixture 的偏好设置，不替代宿主限额，也不保证分配一定以某种形式失败。[Linux /proc 官方文档](https://docs.kernel.org/filesystems/proc.html#proc-pid-oom-adj-proc-pid-oom-score-adj-adjust-the-oom-killer-score)。

监控父进程可在该新 Guest 内保护自己，压力 child 必须显式设置并读回自己的高 OOM 选择分值；不能让 child 继承父进程的保护值后误以为压力未触发。分配必须逐页实际触碰，并有固定字节总上限、总期限及小而有界的进度记录。只创建虚拟映射、Python `MemoryError`、正常退出、截止后人为 SIGKILL 或监控丢失均不满足真正 OOM victim 条件。超时回收仍必须执行，但其结果单列为失败与清理，不改变故障原因。

## VM 外部终止与正式停止事实

libvirt 将 transient 与 persistent domain 分开；transient domain 运行结束后不再保留其持久定义。`virsh destroy` 终止运行，不删除 Guest 使用的存储；它是强制停止入口，可能造成 Guest 未落盘数据丢失。底层 destroy API 可先请求终止再按需强制结束，不能把命令名字当成特定 SIGKILL 已被观测的证明。[官方 VM 生命周期](https://wiki.libvirt.org/VM_lifecycle.html)、[virsh destroy](https://www.libvirt.org/manpages/virsh.html#destroy)、[destroy API](https://www.libvirt.org/html/libvirt-libvirt-domain.html#virDomainDestroy)。

故障注入前要经正式 Broker 验证 A 当前运行及冻结归属，再以 manifest 的精确 UUID 为唯一 selector 发出终止。随后必须由正式 Module 成功查询宿主 absence，得到与本次资源/配置对应的 `StopEvidence`。CLI 退出码、Guest ACK、串口断开或丢失一次 `/proc` 都不能独立充当停止事实。此流程预期验证 `libvirt_absent_after_confirmed_start` 这条已有正式分支，而不是先由 Broker 发 stop intent 再把它包装为“外部崩溃”。

Broker 重构后仍须保留该停止证据和代次 tombstone，拒绝旧 Assignment/Grant 复活。B 的独立 watch 被 SIGKILL 后，仍按已实现的持久期限与重建路径停止；A 的 Guest OOM、A 的外部终止、B 的 watch 故障分别保存时间线与原因，不能合为一次原因未分明的“恢复成功”。

## 本轮必须通过的门槛

资格操作者需在执行前固定全部等待/字节/并发边界并写入本轮报告；下表定义结果判据，不预填耗时或实测值。

| 门槛 | 必须记录并满足 | 缺证据时的处理 |
| --- | --- | --- |
| G01 源码与准入 | 正式 Module、操作者、Guest fixture、固定v2配置及基础镜像摘要；两台原 sealed Guest 检查通过 | 不运行压力或不计通过；失败原样留存 |
| G02 宿主实际限额 | A/B 的真实 QEMU UUID/PID/starttime、全线程归属、全域CPU及内存/swap实际cap；压力前后同身份 | 身份/限额不符即停止本轮，不能通过另读一次覆盖故障 |
| G03 有界压力开始 | 显式Guest transient监督配置和实际unit/cgroup/score读回；A 独立 child PID/starttime/随机短comm/nonce、触页分配进度、Guest OOM基线及明确deadline/字节上限；child非受保护分值 | 没有触页或child继承保护均不能算耗尽 |
| G04 OOM强归因 | child确实以SIGKILL结束、Guest `oom_kill` 正增量、新kernel victim记录精确匹配该PID/comm，且没有操作者先人工kill | `MemoryError`、无victim关联或人工kill均失败 |
| G05 A局部存活 | OOM后监控父进程给出本轮应答；Guest boot ID不变，宿主QEMU身份稳定且正式observed running | Guest失联不能写成已恢复；宿主停止另行确认 |
| G06 B并行可用 | 压力窗口内主机才发出的3个新随机挑战全部在冻结期限内由B回应，并绑定B本轮nonce | 压力前旧心跳不能计数；超时不靠延长已运行窗口补成通过 |
| G07 宿主OOM分离 | 宿主A/B QEMU全域层的实际memory事件和正常/压力采样分别保留；只按该身份与观察窗口解释增量 | 不将Guest计数代替host事件、不将缺失计数编成0，不外推为宿主整体没有OOM |
| G08 外部终止 | 完成上述证据后只对已核属A做明确故障注入；正式Broker确认absence并产生已确认启动后的StopEvidence | 终止调用未决且不能取得独立宿主停止事实，或宿主查询失败时，保持reconciling及写层归属 |
| G09 持久防复活 | 重构Broker后证据和停止代次保留，旧Assignment/Grant拒绝，绝不另造UUID或自动重放启动 | 不通过当前VM列表为空推断旧授权已安全失效 |
| G10 独立故障及回收 | B原watch SIGKILL/重构期限链仍通过；两份console、journal/anchor归档；原对象/base不变，精确停止后再回收本组镜像/ACL | creator或停止未决、日志/journal未完整时保留诊断与写入层，不动旧80GiB材料 |

全部 Guest 报告仅是此受控 fixture 的证据之一，宿主生命周期与回收仍以正式 Module 真实观测为准；不能据此允许恶意 Guest 自报“OOM/已停止”释放工作区写权。无新模型请求、NIC、监听端口、全局 sysctl/cgroup 修改、全局 swap 改动或 libvirt daemon 重启。研究与资格无需新的宿主第三方依赖；如实现需要新增包，应先按仓库依赖规则登记。

## 首轮失败与收集器修正

首个真实组 `923213c1cd44` 保持 FAILED：两台VM原22项sealed检查通过，Host等待内存事件超时；错误1、清理错误0，base和既有对象未变，本轮临时文件已回收。原始私有报告SHA-256为`f66eea40cebc441b1185ce755096c8d87d8bca30b2307fadeca0da12c8d4d75e`。实际console给fixture标记附加了内核时间戳和`cloud-init[PID]`前缀，原收集器严格匹配行首，因而没有接受START。已归档的分配/内核迹象不追补为当时资格通过。

修正只剥除一次已确认格式的有界前缀，仍要求完整LF/CRLF帧、精确nonce/token及字段绑定；未知、嵌套前缀、内嵌marker和其他Unicode/控制字符行界均不能成为协议帧。新收集器对原归档只读回放，恢复1个START和5个PROGRESS，原报告仍为FAILED；回放不等于重新执行VM或OOM资格。

独立复核还发现两类期限缺口，均先红后绿修正：Guest外层窗口结束时不能绕过子进程35秒期限进入健康循环；Host在poll/谓词/写完成后必须再次核对绝对期限，迟到的匹配证据仍失败。Guest worker另有独立32秒alarm，内核日志EOF和读取循环也有失败/截止门槛。边界在创建VM前落盘为`qualification-limits.json`，最终报告和manifest内容/SHA须一致。

前缀修正版本在本机Python3.9.6的36项新helper、22项操作者、7项既有压力测试通过，共65项；本机Broker套件的42次pathname UNIX socket创建被sandbox拒绝，未计通过。指定Linux/Python3.12.3实际运行55核心、22操作者、7既有压力、36新helper，共120项通过，0失败/错误/跳过；包含真实UNIX/socketpair和合成Guest，不启动VM或TCP服务。完整资格CI与真实VM另计，不用这120项填充原生退出门槛。

第二个冻结版本的helper SHA-256为`4fb00699046c25dd24c9c3faeb357a53468449fb2ab367aa7d89b6d13ed73fef`，操作者为`20bb9419edbf4b7a8e93b981f8f83cb937ad331a361aff10dea33a214c67e87c`；正式Broker两个生产文件与基线`5cb5fb9`逐字节相同。不可变快照`da67322684b42fe6a5b07cf22a705a913762d116`的Standards与Spec增量复核均无未关闭问题；该版本随后实际运行仍失败，不能据此声明原生通过。

## 第二轮与显式 Guest 监督配置

独立组`780ed2087741`仍保持FAILED：Host已正确接收START及128/256/384MiB的三次A存活回执，之后看到更高触页进度和内核victim迹象，但没有收到RESULT，最终事件收集超时。两台sealed22项通过，错误1、清理错误0，原对象/base不变，本组临时文件已回收；原始私有报告SHA-256为`bb628ba04968ce45cbab9cdb9637dc558aed81abc91e21f582fde795b6d41213`。没有独立证据确定监控器停止原因，不能把`OOMPolicy=stop`假说写成该轮已证根因，也不追补该轮为OOM资格通过。

systemd255官方语义区分内核victim选择与service manager行为：`OOMPolicy=continue`允许unit在成员被OOM选择后继续；`stop`由manager终止unit，`kill`还有组级处理。`oom_score_adj=-1000`不能代替明确的服务监督策略。因此后续fixture把监控器和受控worker放进Guest自己的、nonce命名的transient service，明确`Type=exec`、`OOMPolicy=continue`、`OOMScoreAdjust=-1000`与185秒`RuntimeMaxSec`，并读回实际unit/cgroup/score/`memory.oom.group`。worker仍覆写+1000，所有Guest全局OOM、精确victim、boot及A/B健康门槛不变。这是可复验的监督配置选择，不是对旧故障原因的推断。[systemd255 service](https://www.freedesktop.org/software/systemd/man/255/systemd.service.html#OOMPolicy=)。

该fixture处于早期bootcmd，专用unit显式`DefaultDependencies=no`避免依赖尚未完成的基础启动阶段；不修改已有cloud-init unit或manager默认策略。第三轮曾选择`systemd-run --pipe --wait --collect`保留stdout/stderr，但实际早期启动的完整bus连接失败，因此该选择不列为已通过。后续采用不请求pipe/wait的专用unit启动与有界文件输出中继，保留`Type=exec`、unit运行/停止时限和190秒launcher总期限；相关上限写入预冻结manifest。此配置只属于新Guest内的受控资格进程，不安装宿主服务、不给宿主增加UID/组权限，也不推导任意Harness自己的OOM策略已通过。[systemd255自动依赖](https://www.freedesktop.org/software/systemd/man/255/systemd.service.html#Default%20Dependencies)、[systemd255 transient/pipe](https://www.freedesktop.org/software/systemd/man/255/systemd-run.html#--pipe)。

## 独立监督版本的冻结与回归

独立监督版helper SHA-256为`667ba6bb95ec3d2ebcba562d9c4396e091d5e7dd3691b33952151a812f32a7ef`，操作者为`77b3bf25a397fa7cf3b03e25852568319185948fce70188bb509cef782b5dbf9`。本机42 helper/22操作者/7既有压力，共71项通过；指定Linux/Python3.12.3的55核心/22操作者/7既有压力/42 helper，共126项通过，0失败/错误/跳过。Guest程序仍只在模拟环境编译或以受控替身执行，真实Guest行为按独立原生运行另计。

监督读回还通过确定性迁移负例：每次`monitor_facts()`都重新读取实际`/proc/self/cgroup`，核对本轮nonce unit，再据新位置读取组事实和生成结果；不能复用启动时缓存来证明RESULT期间实际归属。源码快照`781b89055e3a60a72e376556ecf1d8d833e791ae`的Standards和Spec增量复核均无未关闭问题。第三轮实际启动失败见下节，后续relay版本的同源结果独立关联。

## 第三轮早期总线前置失败

组`5ab0317d42f5`保持FAILED，尚未进入内存压力：两台原sealed22项与limits冻结检查通过，Guest launcher报告`guest_supervisor_failed`/`CalledProcessError`，受控console明确出现`Failed to connect to bus: No such file or directory`；没有监控器START或OOM事件。本轮错误1、清理错误0、原对象/base不变且临时文件回收；原始私有报告SHA-256为`4c45817a5fe9304dbc37925825efc1ffa24a1079aede635b8d4ee49662700860`。不得将这次启动失败当作OOM恢复证据。

官方systemd v255 `run.c`明确：使用`--wait`或非默认stdio模式（含`--pipe`）时选择完整bus transport，否则可选择systemd manager transport。这与本轮完整bus连接失败一致，不据此推断manager的私有控制连接也不可用。后续unit保留185秒运行/3秒停止限额，使用仅本Guest私有目录中的受控脚本与输出文件；launcher用固定190秒总限额、完整行和有限字节中继，避免为了同步输出而依赖尚未可达的完整bus。具体可用性仍需新的真实运行证明，不把源码推论写成已测通过。[systemd v255连接选择实现](https://raw.githubusercontent.com/systemd/systemd/v255/src/run/run.c#L1800)、[manager transport](https://raw.githubusercontent.com/systemd/systemd/v255/src/shared/bus-util.c#L321)。

## 实际结果与后续门槛

最终同源真实组与组件证据见下；实际GitHub源码`fbb975b5f7886c4f9cde1bf819493c80112f6ab9`的[资格CI](https://github.com/StateKnot/Harniqo/actions/runs/41177744267)通过309项Python及两组Node检查，零失败/错误/跳过；[三平台契约CI](https://github.com/StateKnot/Harniqo/actions/runs/41177744311)同源成功。下载artifact后按run/job/source/报告hash及契约源摘要核对，见[独立CI索引](../docs/operations/qualification-ci-guest-memory-2026-10-31.json)。这些组件无VM/模型，不能替代下列原生结果。原有CPU压力/内存限额报告与此前失败保持不变，不因本轮结果追补为当时已证明Guest耗尽。

本子项通过也只确认固定v2受控 Guest 内存耗尽的隔离和外部终止后的本地持久处理。宿主QEMU OOM与宿主整体内存压力、任意Harness负载/设备/Guest组合、生产服务权限、网络/存储资源边界及完整M0-05仍须各自实现和验收。


输出中继版的冻结源码helper SHA-256为`8d0398ba447799d55bce41f738d80559341e5f8e1ad072483345f33d192b4479`，操作者仍为`77b3bf25a397fa7cf3b03e25852568319185948fce70188bb509cef782b5dbf9`。本机71项及指定服务器126项相关回归均通过；新增launcher测试使用本任务临时文件真实I/O和假manager命令覆盖明确成功、未知状态及残缺终行，不执行Guest或宿主systemd。`918d7590508d7b5b9f940d76bf9fcb11c25e02d3`的Standards/Spec relay增量复核无未关闭问题；同源第四轮已通过；完整结果见下。


## 第四轮最终真实结果

独立组`424d7c09d3e1`于2026-10-31 06:32:34–06:35:07 UTC通过，21个必需门槛全true，错误/清理错误0。[脱敏机器报告](../docs/operations/guest-memory-kvm-2026-10-31.json)关联原始报告SHA-256 `72a23f4f3fd86e9bcc7610e7855571aff286a4ee363aab1cd871d24b7df222c9`、7份实际源码摘要、预冻结limits、内核victim摘要及签名归档核验。前三组原始FAILED和各自源码均保持原样，没有按本次成功回填通过。

实际Guest为Ubuntu24.04、kernel `6.8.0-147-generic`，可见内存1,007,144,960 bytes、swap0。A受控worker触页771,751,936 bytes（736MiB），确实以SIGKILL退出；Guest `oom_kill`由0增至1，新kernel victim精确绑定该PID、随机comm及starttime，未超时人为杀掉。该压力与健康观测段10.302秒。B在A分配128/256/384MiB时分别完成新挑战与A因果live确认，OOM后A以本轮token和原boot ID给出新健康回执，B也有新增回应。

本Guest的plain manager启动路径与文件输出中继实际可用；读回`OOMPolicy=continue`、monitor score -1000、worker1000、`memory.oom.group=0`、实际MainPID/unit/cgroup/文件身份、185秒runtime/3秒stop及2MiB RLIMIT。START/RESULT的完整监督身份相同；不将其外推为任意Guest或Harness自己的服务策略已通过，也不据此认定第二轮旧故障根因。

宿主通过正式Module读取前后A/B QEMU同PID/start/cgroup身份及各样本全线程归属；CPU100ms/100ms、记账memory.max2GiB+额外swap2GiB保持。A宿主记账内存峰值1,114,431,488 bytes，B峰值476,782,592 bytes，双方该窗口OOM/oom_kill计数均为0。此事实与Guest OOM属于不同内核；Host QEMU OOM恢复和全宿主OOM仍未验证。

完成以上独立证据后，launcher SIGKILL已确认且A仍活动；操作者对核属A注入一次外部`virsh destroy`，正式Broker以`libvirt_absent_after_confirmed_start`持久记录generation2终态，重构后完全一致，旧Assignment/Grant分别被generation_stale/resource_terminal拒绝。B在A停止后继续新挑战；watch SIGKILL后B超过期限仍活动，新Broker从持久状态停止B，generation3方法`libvirt_destroy_then_absent`，确认时刻晚于租约期限。外部终止不写成Guest OOM杀掉QEMU。

两份console完整保存86,818/81,274 bytes、零截断；anchor/journal/ownership XML HMAC和epoch/配置绑定在服务器内核验。VM、worker退出及宿主停止确认后才回收本组raw/seed/socket；base、原有对象及协调锁身份不变。无新增NIC/TCP监听、模型调用或宿主权限/服务策略更改。原80GiB旧未知派发/诊断材料不借本组通过回收。当前仅此Guest耗尽与外部停止子项VERIFIED，M0-05仍IN_PROGRESS、M0仍4/8。
