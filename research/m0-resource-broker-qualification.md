# M0-05 本地 Resource Broker 生命周期准入

日期：2026-10-30。状态：固定实例组本地生命周期子项 `VERIFIED`；M0-05 保持进行中。

本轮交付固定预登记实例组的本地持久生命周期与期限停止核心；验收调用该 Module 的同一 Interface，不在资格脚本中重写 VM 状态机。整体执行计划仍见[执行网络评审](m0-execution-network-review.md)和[隔离 ADR](../docs/adr/0002-isolated-execution.md)。

## Interface 与信任范围

`apps/runner/resource_broker/` 仅提供固定 libvirt 后端。一个 `state_dir` 对应一个不可变的预登记实例组，最多 64 个资源；写者互斥仅覆盖该组。`Template` 在本模块中表示包含 UUID、磁盘/seed/socket 文件身份的已准备实例配置，区别于平台的可复用镜像/策略模板。组内同一 Workspace 的后续 Attempt 可在停止已确认后按更高代次复用；新增 Workspace/资源、重建 socket 或修改目录需要受控实例组交接。M2 仍须实现资源准备、目录准入和执行目标级分配/全局单写者，不能靠换一个空 `state_dir` 绕过旧组未决执行。`ensure`、`renew`、`observe`、`revoke` 隐藏模板校验、固定 UUID、单写者、持久 intent、期限和停止核对；`reconcile`/`watch` 可在另一个宿主进程运行。

本地签名 Assignment/Grant 是 Resource Broker 自己的内部授权格式，签名区分指派与续约用途；不是模型/文件入口的 ExecutionGrant。可信目录只向管理员开放，运行调用方不能提交 XML、QEMU 参数或宿主文件路径。指派绑定确定 UUID、组织/Workspace/Attempt、模板配置 hash、租约代次、恢复代次和时限。控制面 OIDC/项目授权、权威业务事务、预算和跨宿主协调仍由 M1/M2 整合。

本轮只运行无 NIC 的固定模板：1 vCPU、1024 MiB Guest RAM、独立 8 GiB raw root、只读 NoCloud seed、两个有界 virtio-serial/console 通道；不挂载宿主目录或管理 socket。raw 文件先完整预分配并核对实际块数；这只固定本轮数据盘分配，不替代所有宿主磁盘、日志/spool 配额和资源耗尽验收。

Guest 的停止消息不能签发宿主停止证明。libvirt 的 `destroy` 是强制终止，而 `shutdown` 依赖 Guest 配合；因此由 Module 执行精确归属核对及宿主停止确认，保留工作区磁盘。对于调用超时或无法核对的状态，继续 `reconciling` 并阻止第二写者，不能按“目前没看到 UUID”释放未确认启动。[libvirt destroy/shutdown](https://www.libvirt.org/manpages/virsh.html#destroy)

## 真实验收设计

资格入口为 [qualify_resource_broker.py](../scripts/qualification/qualify_resource_broker.py)。合成组件检查使用 [test_resource_broker.py](../scripts/qualification/test_resource_broker.py)，两类证据分别记录。

| 用例 | 实际动作与成功判据 |
| --- | --- |
| 固定身份与单写者 | 通过 Module 启动 A/B，重复同一指派仍为同两个 UUID；签名正确的同 Workspace 第二写者在启动前拒绝 |
| Guest 阳性对照 | 两台真实 KVM 各完成已封闭基线的 11 项 Guest 检查，带随机 nonce；普通安装、Schema 或模拟后端不计此项 |
| 签名与代次 | 真实实例的错误签名、旧代次续约被拒；拒绝原因与原生运行分别保留 |
| 启动方被 SIGKILL | 确认启动进程退出；独立 watch 仍在。只读观察 A 到期后实际消失，再读取持久停止证明；B 的实际域仍运行 |
| 期限进程重建 | 确认 watch 已被 SIGKILL、B 到期后仍为实际活动域；新建 Broker 从磁盘恢复并停止 B，不能启动替代 UUID |
| 禁止复活 | 过期续约及已停止的原指派不能复活 VM；停止前磁盘保留，确认后才能清理 |
| 恢复与归属 | 全部实际控制台留证，基础镜像 hash、已有 VM/network/filter 配置与状态不变；未知状态保留磁盘、ACL 与诊断材料 |

原始配置、签名、资源身份、journal、控制台和失败记录仅存服务器受控目录。公开材料只保留脱敏索引、源码版本、结果及限制。

## 尚不能由本轮证明的内容

- 本轮启动/watch 是不同进程；专用生产 UID、Supervisor 本地 IPC、polkit/对象 ACL 与服务安装需要另行验证。
- 完整 Model Egress/File Broker、逐请求执行撤销及内核出口策略没有从 sealed 模式继承。
- 本地签名和持久代次不证明控制面数据库事务、全局 fencing、跨宿主接管或全部 AC-07/AC-30。
- Guest RAM/vCPU、raw 数据盘分配不证明 QEMU 全进程 CPU/内存硬限额、OOM、日志/spool 和宿主配额。
- 进程恢复不等于断电、宿主永久失联或 SaaS 独立故障域通过。

## 实测记录

当前组件与独立复审已覆盖撤销 intent 持久性、阻塞操作后的时限复核、停止确认时刻及不可发送的过期指派。真实 KVM 主流程已由下列独立新组完成；M0-05 不关闭。

启动前已有两份独立拒绝记录，均未创建 VM：

1. 共享 Canonical base 为只读、SHA 正确，但归 QEMU 账号；操作者没有改变原文件，另生成 621,284,352-byte、相同 SHA 的操作者只读副本。副本位于 `images/vm/base`，旁车保留同一认证来源。
2. 既有 `.state/qualification` 是共享目录，不满足私有 authority 状态目录要求。操作者保留其权限，仅将旧 `kvm.lock` 用于与既有资格脚本协调；新任务的权威状态和通道使用独立私有目录。共享协调路径不充当权限或秘密边界。

最终原生检查、退出与回收结论将在相应独立运行后回填；这些前置拒绝不回填为成功。

第一次实际启动流程在固定来源 `f088a7ad` / 操作者 `67b35e78` 下失败：libvirt 的 DAC 把 seed 交给 QEMU、socket 交给 root，原 owner-only 校验触发 `state_catalog_changed`。当时可见测试域为0，base及既有对象未变；指派保持未知、完整停止证明缺失，所以诊断磁盘与 ACL 继续保留，未用暂时 absence 强行删除。后续配置将角色转换纳入 hash，并使用正式准备 Interface 保留 Broker 的只读 seed ACL；将以新实例组和新私有签名键重新验收，不改写原失败。

后续分开的实际记录：

- `01e311a8fe51`：新 seed ACL 已在 DAC 后保留、单模板与整体 catalog 的停止后摘要仍匹配；启动期间配置保护失败，原始报告保持 FAILED。
- `bdbdeb8d5073`：有限诊断定位到运行中 channel 的 owner 白名单遗漏。官方 DAC 路径会把 connect socket 交给 QEMU，停止后又可能是 root；新策略只补同一已授予 socket 读写的系统 QEMU UID，策略仍进入 hash，未放宽 inode、ACL 或 seed 校验。
- `28a00b28918f`：两台实际 VM、原生去重/排他/签名门槛与运行中 QEMU socket 阳性均通过；Guest 报告在120秒窗口超时。控制台明确停在 networkd-wait-online，未执行期限故障步骤。该轮停止、console及精确回收均通过，`cleanup_errors=[]`；自动主结果继续 FAILED。

无 NIC 的 stock image 仍会运行网络在线等待。systemd255 的该服务默认120秒并要求至少一个链路在线；独立实例组 `e1623ca39230` 使用150秒 Guest 窗口仍失败：网络等待约120秒结束，config阶段约127秒完成，但未进入Final，未收到Guest报告；本轮精确停止与回收通过，原FAILED保留。[systemd255 文档](https://www.freedesktop.org/software/systemd/man/255/systemd-networkd-wait-online.service.html)

新的资格引导仅将原封闭探针的完整Python程序改在 `bootcmd` 执行，去掉晚期 `write_files/runcmd`，避免重复探针。11项检查、随机nonce、60秒virtio设备等待、原8秒/5秒期限和停止判据不变。cloud-init官方说明bootcmd在早期阶段执行，runcmd生成的脚本由Final阶段运行；报告只证明探针执行时的sealed检查，不证明cloud-final或全部OS服务ready。[cloud-init启动阶段](https://docs.cloud-init.io/en/latest/explanation/boot.html)、[bootcmd](https://docs.cloud-init.io/en/25.1/reference/yaml_examples/boot_cmds.html)。

## 最终真实结果

独立新组 `f30e1ba509be` 于2026-10-30T10:48:12Z–2026-10-30T10:50:42Z通过，`errors=0`、`cleanup_errors=0`。生产core SHA-256 `feae211210922048f0e88d453cf490fe8f7fd46278769b47163f4b7f002b0bb2`，资格入口 `9e08308a7eea0f532581be8223ecb98f4e9f1bbabe577db426c3a7c6a513edee`；原始私有报告 SHA-256 `4639152cef3cce978c196cc14db705729e807079b9f5146f8a9791383a0b9a67`。[脱敏机器摘要](../docs/operations/resource-broker-kvm-2026-10-30.json)。

- 两台实际KVM各11项sealed Guest检查通过；运行中两台disk/seed/channel/console均观察到已知QEMU owner。
- 同一指派重复ensure保留原两个UUID；同Workspace第二写者、错误签名、旧续约代次、过期续约和已停止原指派均按预期拒绝。
- launcher实际SIGKILL退出后，先只读确认A仍活动，独立watch仍存活；随后只读观察A消失，再取得`libvirt_destroy_then_absent`持久证据，B仍实际running。
- watch实际SIGKILL退出后，B超过期限仍实际运行；新Broker从磁盘恢复并停止B，方法仍为`libvirt_destroy_then_absent`。两次确认时刻均不早于相应期限。
- 两份console连接、完整drain至EOF、归档分别92,327/92,330 bytes，零截断。所有worker退出、两VM宿主停止确认后，才删除本轮raw/seed/socket；base、全部既有libvirt配置/状态、源码和协调lock身份保持不变。

本机operator16项及服务器完整233项纯组件通过、零失败/跳过，核心34项包含DAC三态/冷恢复回归；两路独立代码复核通过。这些合成组件与真实22项Guest/故障因果链分别记账，GitHub CI结果另行关联。

前五份实际FAILED及两个前置拒绝均未改写。前三份未知派发的诊断写入层合计48GiB、ACL与journal继续保留，不能把后续新组通过当作旧组停止证明。后两份Guest超时组及最终通过组均按各自宿主证明精确回收。本轮无NIC、无宿主TCP监听、不提权、模型调用为0；未使用模型测试凭据。

这里的 `VERIFIED` 仅覆盖固定预登记实例组的本地生命周期与上述两类进程故障。完整M0-05仍缺生产Egress、专用身份/服务授权、宿主硬配额与耗尽、控制面全局fencing及更广宿主故障恢复，不将其标为已开发完成。

加强因果门槛前的独立组 `34865c5acbb5` 按当时条件通过，原始PASSED未改写；但没有直接记录launcher退出后A仍活动，因而不单独承担“退出后才停止”的严格时序证明。上面的最终组增加此必需阳性门槛，缺失即失败，两个退出确认时间与前后宿主读数均独立留证。

中间独立组 `c9bff4e4a396` 已通过加强的A存活阳性门槛，原PASSED保留；随后修正日志I/O异常不能跳过宿主撤销及journal缺失禁止删除路径，并由最终组同源重验。最终归档的anchor/journal及两份ownership XML签名均在服务器内核验，终态代次为A2/B3，Workspace epoch与签名输入/停止绑定一致。归档是法证快照，不能直接作为新state_dir恢复（锁inode已变）；签名完整性不单独代表宿主物理停止，物理因果仍以本轮libvirt读数和停止链核对。
