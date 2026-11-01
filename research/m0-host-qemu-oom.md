# M0-05 Host QEMU memcg OOM 与持久恢复：来源及准入

日期：2026-11-01。状态：**限定Host QEMU OOM及持久恢复子项已开发完成并验证（VERIFIED）；M0-05整体仍IN_PROGRESS**。起始基线 main `de97622bf052dc1925147bb0382887c13c1d2807`。本文核对上游 Linux **v7.0**、libvirt **v10.0.0** 与 systemd255；实际宿主组合、权限与运行证据见末节，不由上游tag推定发行补丁的全部行为。

目标仅为新建、显式选择的 sealed 故障模板：Guest1GiB、全域CPU100ms/100ms、QEMU计账内存768MiB、额外swap0。保留v1/v2定义与hash；正常B使用原v2（QEMU内存2GiB、额外swap2GiB）。不修改现有VM、全局cgroup/swap/sysctl或管理员服务，不在宿主制造内存压力。768MiB是**故障注入值**，不是生产工作负载的正常容量建议；libvirt明确提示hard limit包含Guest内存与QEMU开销，过低会使内核杀掉域。[官方内存调优](https://libvirt.org/formatdomain.html#memory-tuning)

## libvirt10：显式XML零值与动态memtune不能混用

固定v2后端将`hard_limit`写入`memory.max`，把`swap_hard_limit`直接乘1024写入`memory.swap.max`，**没有减去hard limit**。因此额外swap0须实际读到`memory.swap.max=0`，不能只按XML文档的memory+swap描述解释。[v10后端](https://github.com/libvirt/libvirt/blob/v10.0.0/src/util/vircgroupv2.c)

已逐层核对的新建XML路径：

1. `virParseScaledValue`区分元素缺失与字符串`0`：缺失返回0；显式零成功返回1。`virScaleInteger`允许数值0。[XML解析器](https://github.com/libvirt/libvirt/blob/v10.0.0/src/util/virxml.c#L1867)、[单位缩放](https://github.com/libvirt/libvirt/blob/v10.0.0/src/util/virutil.c#L172)
2. `virDomainParseMemoryLimit`仅对“缺失”设置unlimited；显式零保留为0。格式化使用`virMemoryLimitIsSet`，该函数认为0是已设置值，故不会把它当作省略项。[limit解析与格式化](https://github.com/libvirt/libvirt/blob/v10.0.0/src/conf/domain_conf.c)、[IsSet实现](https://github.com/libvirt/libvirt/blob/v10.0.0/src/util/virutil.c)
3. `virDomainCgroupSetupMemtune`对该已设置值调用swap setter；公共setter不改写0，经nested层转发给上述v2后端写入0。新建域的公共/QEMU验证器中没有动态接口的hard≤swap检查。[启动应用](https://github.com/libvirt/libvirt/blob/v10.0.0/src/hypervisor/domain_cgroup.c)、[公共转发](https://github.com/libvirt/libvirt/blob/v10.0.0/src/util/vircgroup.c#L1947)、[公共验证器](https://github.com/libvirt/libvirt/blob/v10.0.0/src/conf/domain_validate.c)、[QEMU验证器](https://github.com/libvirt/libvirt/blob/v10.0.0/src/qemu/qemu_validate.c)

**推论与实现约束：**候选新建XML可明确写`hard_limit unit='KiB'=786432`及`swap_hard_limit unit='KiB'=0`。禁止用运行中`virsh memtune --swap-hard-limit 0`替代：动态`virDomainCgroupSetMemoryLimitParameters`会拒绝hard大于swap的值。这是版本限定的启动路径结论，尚不是本机实测；发行补丁若拒绝、忽略或改变零值映射，应失败并保留记录，不能改为任意小swap或直接写内核文件凑结果。普通已获授权用户通过`qemu:///system`交给daemon设置，不能转用明确不支持该内存调优的session路径。[动态参数检查](https://github.com/libvirt/libvirt/blob/v10.0.0/src/hypervisor/domain_cgroup.c)、[QEMU权限与session检查](https://github.com/libvirt/libvirt/blob/v10.0.0/src/qemu/qemu_driver.c)

压力前由正式Module读取实际CPU、memory/swap控制层，确认768MiB/0与全线程containment，保存scope、`scope/libvirt`及祖先限制。主PID的emulator子路径不等于全域memory层；沿用既有[层级准入](m0-host-resource-limits.md)，不靠名称搜索挑选任意cgroup。

## Linux7.0强归因：约束、victim与新鲜度必须同时成立

`constrained_alloc`在`oc->memcg`存在时给出`CONSTRAINT_MEMCG`。OOM摘要包含constraint、victim的task/comm、pid和uid；`mem_cgroup_print_oom_context`分别输出触发限制的`oom_memcg`和victim所在`task_memcg`。两者可因层级不同而不相等。实际kill路径先增加OOM计数，再向victim线程组发送SIGKILL，另记录被杀PID/comm。[v7.0 OOM选择与日志](https://github.com/torvalds/linux/blob/v7.0/mm/oom_kill.c)、[memcg上下文](https://github.com/torvalds/linux/blob/v7.0/mm/memcontrol.c#L1549)

**本项目验收规则，非内核稳定日志API：**

- 压力前保存Broker核属的A UUID、QEMU PID、`/proc/PID/stat` starttime、实际comm/UID、全部线程与cgroup、有效cap；压力窗口持续关联同身份。victim PID/comm须精确绑定预先记录的QEMU，`oom_memcg`精确等于A的已准入全域memory层，`task_memcg`位于该层或其后代。按路径组件比较，不能用字符串前缀容忍同名前缀的邻居。
- 要求新增kernel摘要的`constraint=CONSTRAINT_MEMCG`，并要求对应`Memory cgroup out of memory: Killed process …`记录匹配同一victim。拒绝`global_oom`、`CONSTRAINT_NONE`、其他VM/祖先memcg、只有任意“Killed process”字样或单个SIGKILL。摘要受内核ratelimit影响，缺失时本轮强归因失败，不能降级为“信号大概来自OOM”。
- 简单fixture可保守要求victim为QEMU主PID。若实现接受非主线程，须事前保存该TID/starttime、Tgid等于Broker主PID、comm与cgroup；不能事后把同名线程认作原QEMU。内核`find_lock_task_mm`可能改选仍有mm的线程，日志pid不保证总是线程组leader。[OOM线程处理](https://github.com/torvalds/linux/blob/v7.0/mm/oom_kill.c)、[/proc身份字段](https://www.kernel.org/doc/html/v7.0/filesystems/proc.html)

Guest内部OOM、`MemoryError`、连接断开、QEMU不见或libvirt shutoff理由都不独立证明上述原因。`cgroup_oom_kill`只能作为**通过这些宿主证据验证后**的资格归因字段；Broker现有“宿主absence”停止原因不自动升级成OOM原因，除非正式接口显式接纳并校验这些证据。

## Journal证据范围与可消失的cgroup计数

压力前在当前boot以`journalctl --dmesg --boot=<boot-id> --no-pager -o json -n 1`取得实际最后一条kernel记录，完整保存其`__CURSOR`、`_BOOT_ID`、`__MONOTONIC_TIMESTAMP`和原始JSON。实现以`--cursor=<cursor>`读取包含anchor的批次，要求首条精确匹配后只消费后续记录，再推进游标；同时保存压力开始/截止的单调时间。仅接受`_TRANSPORT=kernel`；`_PID/_COMM`是日志来源而非OOM victim，victim字段来自按本版源码核对的`MESSAGE`。JSON允许字段数组、二进制数组及长字段null，解析器须拒绝缺失/歧义，不能把它们转成可信字符串。[journalctl255](https://www.freedesktop.org/software/systemd/man/255/journalctl.html)、[journal字段](https://www.freedesktop.org/software/systemd/man/255/systemd.journal-fields.html)

cursor是opaque位置，不能解析或按字典序比较。seek在原记录已不存在时可能落到最近位置；严格采集器须验证anchor仍可精确定位（libsystemd用seek、next、test_cursor），或采用同等明确anchor核对，拒绝不可证明的缺口。当前用户读不到宿主kernel journal、boot改变、输出截断/超预算或范围无法确认时，保留失败；不修改日志权限/组或安装管理员服务绕过。[cursor API](https://www.freedesktop.org/software/systemd/man/255/sd_journal_get_cursor.html)、[seek语义](https://www.freedesktop.org/software/systemd/man/255/sd_journal_seek_cursor.html)

`memory.events`通常含后代计数，`memory.events.local`限定本层；`oom_kill`计数包含该组被任一种OOM killer杀掉的进程，不能单独区分memcg/global。压力前基线与压力中样本必须保存；若同一cgroup还在，可记录增量辅助交叉核对。libvirt停止流程会删除domain cgroup，故**不要求不存在目录产生“after计数”**，不将ENOENT改写成0或正增量。记录最后成功样本、首次ENOENT及各自时间，用完整kernel归因和正式停止证据闭合。[Linux7 cgroup接口及删除语义](https://www.kernel.org/doc/html/v7.0/admin-guide/cgroup-v2.html)、[libvirt停止回收](https://github.com/libvirt/libvirt/blob/v10.0.0/src/qemu/qemu_process.c#L8588)

## 有界真实资格流程

下列是设计要求，所有数值须由操作者在运行前冻结到源码与报告，不在失败后调整同一轮：两台sealed检查；A实际768MiB/0与B原v2边界；压力总字节/步长/触页方式/期限；阶段闸门；B各挑战期限；journal、console、采样与回收预算。1GiB Guest不是保证Host OOM的充分条件，启动失败、Guest先OOM、无目标kernel证据或超时都必须保持FAILED。

1. A/B同源启动并完成原sealed阳性；保存实际宿主版本、身份、cap与新journal anchor。初始A必须存活且未发生目标OOM。
2. 只在A Guest用有界worker分段真实写触匿名页并保留已分配页；报告累计触页进度，不能用仅保留虚拟地址或宿主allocator。设置早期阶段暂停闸门，宿主收到进度且确认A此时仍live后，才向B发送本轮新随机challenge；B及时返回新应答才允许A继续。至少三次压力中挑战，并在A死亡后再次挑战B。早期门槛应低于预期768MiB故障点，具体值事前冻结，不能把压力前缓存心跳当并行可用。
3. 等待A的目标kernel OOM和独立宿主absence；在归因窗内禁止操作者destroy/kill、Broker撤销/期限到期或SysRq触发。超时仍由正式Broker精确回收，但该cleanup不能变成“OOM成功”。B在压力前后保持同QEMU身份/限额，额外记录B事件样本。
4. 真实瞬时域停止后会消失。正式Broker必须沿已确认启动后的宿主absence分支，持久产生本次资源/配置的`StopEvidence`（期望方法`libvirt_absent_after_confirmed_start`）。不能用Guest自报、EOF、单次查PID或资格脚本自己构造停止证明。[libvirt瞬时域生命周期](https://wiki.libvirt.org/VM_lifecycle.html)
5. 重建Broker核对同一终态/代次/停止证明，拒绝旧Assignment和Grant；B完成后续挑战与独立到期恢复链。证据完整归档且正式停止确认后才能精确清理本轮raw/seed/socket/ACL；不动历史失败材料或现有实例。

本子项即使后续通过，也仅覆盖固定故障profile、该宿主/Guest组合及观测窗口的QEMU memcg OOM后本地持久处理；整机global OOM、任意工作负载容量、生产服务权限、控制面全局fencing、存储/网络/Egress和完整M0-05仍分别验收。

## 实现与实际结果（2026-11-01）

已实现独立、有限且不可变的 HostLimits 策略及显式 `sealed-raw8g-q35-host-oom-v1`，配置摘要、XML与实际控制文件核验使用同一策略。v1/v2配置摘要的回归保持兼容，不能原位迁移活动实例；普通v2仍是2GiB内存及额外2GiB swap。故障profile不是正常部署的默认值，不能把其通过外推为默认v2或全宿主OOM已测试。

源码复核发现的三处门槛已在实际运行前关闭：精确journal anchor连续性、三个B挑战前后A的宿主存活、压力中真实memory.events及cgroup消失时间线。52项helper回归包含完整采集编排与A在第三进度后/第三挑战期间死亡的负例，66项核心含v1/v2配置摘要金样本，29项操作者含预检失败先于raw分配。[指定服务器受影响回归](../docs/operations/host-qemu-oom-components-2026-11-01.json)共278项，0失败/错误/跳过；合成组件不替代下列真实故障。

[真实双KVM验收](../docs/operations/host-qemu-oom-kvm-2026-11-01.json)通过24项宿主门槛和22项sealed Guest检查，错误与清理错误均0。原始报告SHA-256为 `8354228a27024b8f4dc0e684a12e7259c14057881ca4657509ec488287d6fdc3`。实际Host为Linux7.0.0-38-generic、libvirt10.0.0、QEMU8.2.2、Python3.12.3；A的768MiB/0 swap与B的原v2均由正式Module实际读回，未使用动态memtune或修改全局swap。

A最后确认11步/352MiB真实触页，继续下一步时发生Host memcg OOM。31份有界journal批次共130161字节，精确anchor链和同boot元数据已重放；新的约束记录与Killed记录相隔约13微秒，精确绑定冻结的QEMU leader PID/starttime/comm/UID、实际oom_memcg及其task子组。pidfd退出与libvirt UUID absence独立确认。压力与首个死亡后B健康段约1.404秒，不把该单次耗时当通用SLO。

前三个32/64/96MiB阶段各在B新挑战前后保存A的真实身份与限额样本，共6份；B在这三阶段、A死亡后和正式停止后共5个新挑战均通过，B身份、限额及OOM计数保持。A原始事件时间线有12个成功样本、2个真实ENOENT；最后成功读取的oom/oom_kill仍为0，之后cgroup已消失。归因由独立内核记录成立，不能把未读到的after计数编为1或0。

正式Broker沿 `libvirt_absent_after_confirmed_start` 持久记录停止；其原有停止原因为 `host_absence_after_confirmed_dispatch`，未伪造OOM终态或stop intent。重建后停止凭据、代次及配置一致，旧Assignment/Grant双拒绝；B的watch SIGKILL与持久期限重建停止链通过。HMAC journal/anchor/ownership XML、console归档、基础镜像、既有对象及源码均由独立只读收集器另行核验，本组raw/seed/socket已精确回收，旧96GiB诊断层未动。收集器按当前内核实际字段保留nice_usec，原生报告未改。

本子项已开发完成并验证（VERIFIED），M0-05整体仍IN_PROGRESS、M0仍4/8。无模型调用、新NIC/TCP监听、管理员或全局服务/挂载/防火墙更改；不继承Guest OOM、断电、全宿主OOM、通用FS quota、专用服务UID、完整Egress、控制面授权/提交或桌面构建能力。实际GitHub CI在本轮源码提交后单独记录。


## 首轮实际资格CI与夹具修正

源码`37e1bd7408233dd159e5fd0ed5a3e55feb04001e`的[首轮资格CI](https://github.com/StateKnot/Harniqo/actions/runs/41282918646)保持FAILED：Python3.12.15共451项，0失败、2错误、0跳过；两组后续Node检查因前序失败而跳过。错误均来自旧OCTOP启动测试：模拟了df输出，却没有模拟shutil.disk_usage，托管runner不足50GiB时在预检提前退出，未走到预定的停止超时/未知结果对账场景。Host OOM新增测试未失败。[三平台契约](https://github.com/StateKnot/Harniqo/actions/runs/41282918652)该轮成功，另行留证。

已仅调整测试夹具：足量场景明确60GiB，增加独立49GiB拒绝、无Docker派发/无attempt目录的负例。生产run_octop.py与Host OOM运行代码均未变，50GiB准入不降低。先在外层模拟49GiB复现原2错误，再于本机和指定Linux服务器以相同低空间外层复验14项OCTOP测试全部通过；没有启动监听、Docker或模型调用。首次CI原始日志/产物及复现报告保持原样，不能改写为通过。修正后的完整CI待实际运行，不用本次14项代替完整套件。
