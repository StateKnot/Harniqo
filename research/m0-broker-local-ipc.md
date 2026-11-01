# M0-05 Supervisor → Resource Broker 本地 IPC 核验

日期：2026-11-01。状态：**限定 Linux 本地 IPC 子项已开发完成并验证（VERIFIED）；M0-05 整体仍 IN_PROGRESS**。初始上游研究不计作执行证据；下方保留原始资格清单，实际覆盖范围、失败、修正与最终结果见末节。未安装生产服务、创建生产 UID、修改管理员权限、启动 VM、调用模型或新增 TCP 端口。

范围依据：[架构 §2/§3](../docs/architecture/ARCHITECTURE.md#3-七个业务-module)、[领域词汇](../GLOSSARY.md)和 [Broker 现有 Interface 与限制](../apps/runner/resource_broker/README.md#signed-local-envelopes)。IPC 只把受限本地进程边界接到既有 ResourceBroker；控制面继续拥有 Run/Attempt、授权与预算，Broker 的物理观察、停止证据和代次 tombstone 继续拥有原来的含义。IPC 不能自造另一套业务 Run 状态。

## 1. 适用版本和结论

建议使用 Linux pathname `AF_UNIX/SOCK_SEQPACKET`，Python 标准库 `socket`、`struct`、`array`、`json`、`fcntl`、`os`、`selectors` 和 `time` 即可实现。一个连接只处理一个请求和一个响应，不复用连接、不多路复用、不传 FD。SEQPACKET 保留记录边界及发送顺序，因此不需要再做 STREAM 长度头/拼包协议。[Linux unix(7)](https://man7.org/linux/man-pages/man7/unix.7.html)、[Python 3.12.3 socket 文档源](https://github.com/python/cpython/blob/v3.12.3/Doc/library/socket.rst#L1661)

已有同宿主资格摘要记录的是 Python **3.12.3**、Linux **7.0.0-38-generic**、glibc2.39，不是本轮 IPC 运行结果。[已有环境证据](../docs/operations/host-qemu-oom-kvm-2026-11-01.json)。本文固定阅读 CPython `v3.12.3` 与 Linux `v7.0` 源码；Python 3.12 在线文档现显示更后的维护补丁。上游 tag 说明所依赖的实现路径，不代表已逐行审计发行版内核补丁；资格仍须记录真实 `uname`、Python 版本和待测源码摘要。

`unix(7)` 的 `SO_PEERCRED` 适用说明没有列出 SEQPACKET，不能据此宣称跨 Unix 可移植。Linux v7.0 的 `unix_seqpacket_ops` 明确复用 `unix_stream_connect`、`unix_listen` 和 `unix_accept`；connect 为双方建立 peer credentials，通用 `SO_PEERCRED` 返回该快照。选型因此是有源码依据的 **Linux 专用**路径，启动时必须检查必需常量/能力，真实 Linux 测试失败应拒绝启动，不能静默退回无认证或 TCP。[Linux AF_UNIX 操作表与 connect](https://github.com/torvalds/linux/blob/v7.0/net/unix/af_unix.c#L961)、[Linux SO_PEERCRED](https://github.com/torvalds/linux/blob/v7.0/net/core/sock.c#L1796)

## 2. 双向认证：连接身份和逐报文发送者都检查

`SO_PEERCRED` 是 connect/listen/socketpair 时的身份快照，不证明后来向相同已连接 FD 写入的仍是那个进程。`SO_PASSCRED` 则要求内核随每个收到的记录给出 `SCM_CREDENTIALS`；默认包含实际发送进程 PID、real UID/GID。因此只在 accept 时检查 UID，会让 fork 继承或 SCM_RIGHTS 转交的连接成为未预期的授权通道。[Linux unix(7) credentials](https://man7.org/linux/man-pages/man7/unix.7.html#DESCRIPTION)

Linux v7.0 的 peer snapshot 导出的是 effective UID/GID，默认报文 credentials 用 current real UID/GID，PID 使用 TGID。显式 SCM_CREDENTIALS 仍经过内核身份/capability 核对，不能将它理解为应用自行可信地声明 PID。具备相应 capability 的特权进程属于另外的宿主信任边界。[peer effective IDs](https://github.com/torvalds/linux/blob/v7.0/net/core/sock.c#L1606)、[current message credentials](https://github.com/torvalds/linux/blob/v7.0/net/unix/af_unix.c#L1892)、[SCM credential 核验](https://github.com/torvalds/linux/blob/v7.0/net/core/scm.c#L45)

从这些机制推导的实现约束：

- Broker 在 listen 前启用 `SO_PASSCRED`，accept 后再次确认已启用。Supervisor 在 connect 前启用；不要等首条记录已经排队后才开启。Broker 必须由实际响应的进程 bind/listen，Supervisor 由实际发送请求的进程 connect；不支持 listen 后 fork 出另一进程响应。
- 双方都读取 `SO_PEERCRED`，要求合法正 PID，UID/GID **同时精确匹配**宿主可信配置。ACL 使用完整 `(uid, gid)` 对，不用独立 UID 集合与 GID 集合做笛卡尔积，不把组成员资格、`uid==0`、当前调用者身份或 JSON 字段作为隐式放行规则。
- 每条记录必须恰好一个 `SOL_SOCKET/SCM_CREDENTIALS`、长度恰好一个 Linux `ucred`（按本机 ABI 解包 PID/UID/GID），且三者与本连接 peer snapshot 完全相同。缺失、重复、畸形、PID/UID/GID 任一不同均拒绝；从而拒绝普通 fork child 或持有转交 FD 的另一进程。线程共享 TGID，按同一个服务进程处理。
- 同时落实有限 operation/resource ACL；每个 resource policy 固定 `resource_id`、`organization_id`、`workspace_id` 和获准操作。ensure/renew 的签名 envelope 还须精确匹配这三个归属字段，首次准入也适用。通过 UID/GID 检查不等于获准调用所有方法或操作所有目录中的资源。client 必须核对 Broker 身份；仅相信 socket 文件存在或文件 owner 不能完成服务端认证。
- 部署前提为两端固定 real/effective/saved UID/GID、无 set-ID 身份切换、使用已验收宿主 namespace。数值 PID 比较用于本次短连接，不是永久进程授权标识；它不提供防同 UID 调试/注入、特权进程或任意 namespace 映射的安全声明。

**证据边界**：同一开发 UID 下，另一个普通进程可以新建自己的连接而满足该 UID/GID ACL；0700 目录与0600文件也不能把同 UID 的进程彼此隔离。逐报文 PID 检查能验证普通继承/转交负例，不能把开发账号变成隔离的生产身份。生产必须配置不同的 Supervisor/Broker 专用 UID、精确 DAC/ACL 和 libvirt 最小委派，并实际验收；本轮不创建这些身份、不修改 polkit，既有 `dedicated_service_identity_verified=false` 不因 IPC 组件通过而变化。[现有 Broker 部署限制](../apps/runner/resource_broker/README.md#signed-local-envelopes)、[Linux DAC 解析](https://man7.org/linux/man-pages/man7/path_resolution.7.html#DESCRIPTION)

## 3. 截断、FD 和解码顺序

`recvmsg` 返回 data、ancdata、msg_flags。`MSG_TRUNC` 表示正文被截断，`MSG_CTRUNC` 表示辅助数据被截断；Linux SEQPACKET 经 datagram receive 路径丢弃本记录未读部分。不能把下一次 read 当作前一请求的续片，也不能先解析被截断的合法 JSON 前缀。[recvmsg(2)](https://man7.org/linux/man-pages/man2/recvmsg.2.html#DESCRIPTION)、[Linux record receive](https://github.com/torvalds/linux/blob/v7.0/net/unix/af_unix.c#L2394)

必须把 `MSG_CMSG_CLOEXEC` 传给 **双方**每次 `recvmsg`，让意外收到的 FD 从安装到进程表的那一刻就 close-on-exec。它防止接收与随后设置 FD_CLOEXEC 之间的 exec 泄漏，但不关闭描述符，也不替代拒绝 FD 的协议规则。返回 flags 可能包含该标志，不能用 `msg_flags != 0` 直接拒绝所有正常报文。[recvmsg CLOEXEC](https://man7.org/linux/man-pages/man2/recvmsg.2.html#DESCRIPTION)、[Linux 安装 FD](https://github.com/torvalds/linux/blob/v7.0/net/core/scm.c#L329)

接收侧应使用固定上限缓冲，按以下顺序处理：

1. 无论正文、身份、flags 是否有效，先扫描**全部** ancdata，收集每个 SCM_RIGHTS 里所有完整 `int` FD 并关闭；在 finally 覆盖后续每一条异常分支。不得因第一个未知 cmsg、畸形 credentials、空正文或截断标志而提前跳过其余 FD。FD 0/1/2 也是真实接收值，不能以 `fd > 2` 过滤。无法解析完整剩余数据时拒绝，已得到的 FD 仍全部关闭。
2. 任意 SCM_RIGHTS 均拒绝；再检查 `MSG_TRUNC|MSG_CTRUNC`、空记录、报文长度、未知 cmsg，以及上一节恰好一个 credentials 的要求。建议仅分配 `CMSG_SPACE(sizeof(ucred))` 所需的有限辅助空间；padding 仍可能装入意外 FD，因此空间小不等于不必清理 FD。
3. 通过身份和包完整性后，再做 UTF-8 strict 解码、有限 JSON 解码与 exact-field 校验；不使用 pickle/eval/动态 import，不把输入映射成任意属性调用。

Linux 自动释放未能装入控制缓冲区或超过接收方 FD 上限的**多余**描述符；应用仍须关闭成功交付的那些。CPython3.12.3 仅在 `recvmsg` 自己构造返回值时异常的路径尝试清理；正常返回后的 JSON/ACL/异常处理属于调用者责任。CPython 对部分 ancillary 截断还可能发 `RuntimeWarning` 并返回部分内容，必须作为拒绝条件处理，且仍执行已交付 FD 的清理。[Linux SCM detach/destroy](https://github.com/torvalds/linux/blob/v7.0/net/core/scm.c#L329)、[CPython recvmsg 清理路径](https://github.com/python/cpython/blob/v3.12.3/Modules/socketmodule.c#L3850)、[Python recvmsg 部分辅助数据](https://docs.python.org/3.12/library/socket.html#socket.socket.recvmsg)

Python JSON 默认接受重复键、NaN/Infinity，且没有本协议所需的层级/容器限额。因此要显式拒绝重复键、非有限数、bool 冒充 int、额外字段、错误类型和过深结构；异常映射到有限安全 code，不能回传 JSON 原文、原生 exception/stderr、路径或密钥。编码响应也必须检查上限。[Python3.12 JSON 行为](https://docs.python.org/3.12/library/json.html#standard-compliance-and-interoperability)

发送方一次 `sendmsg([packet])` 对应一条记录，并核对返回长度；不使用会循环发送的 `sendall` 来完成 SEQPACKET 协议记录。发送成功只说明本地协议接受数据，不能证明 Broker 完成或响应已被调用者收到；`EMSGSIZE` 需要在发送前有界编码和实际 Linux 消息限额测试来避免。[Python3.12.3 sendall/sendmsg](https://github.com/python/cpython/blob/v3.12.3/Doc/library/socket.rst#L1629)、[Linux send(2)](https://man7.org/linux/man-pages/man2/send.2.html#DESCRIPTION)

## 4. pathname、DAC 与 listener 单例的完整生命周期

监听地址只能来自可信宿主配置中的固定绝对路径，不由请求选择。Linux pathname socket 受父目录遍历及 socket write 权限约束；abstract namespace 没有同等文件权限保护，因此本方案不使用 abstract **监听地址**。connect 前启用 PASSCRED 可能给未绑定 client 自动分配 abstract 名称；这不是新增的 abstract 服务监听，也不能把该地址当认证依据。[Linux unix(7)](https://man7.org/linux/man-pages/man7/unix.7.html#DESCRIPTION)

冻结配置将 IPC root 与私密 state/key root 分开。IPC root 预先创建、由 Broker UID 拥有，采用0700或0750，无 symlink 和 group/other write；state 与 key 的0700/0600约束原样保留。生产跨 UID 时使用 Broker 拥有、Supervisor 所在获准组可遍历的0750目录及0660 socket；目录组须是 Broker 能设置到自己 socket 上的组，两端精确 UID/GID ACL 仍独立核对。非预期 access/default POSIX ACL 必须拒绝，不能只凭 mode 位推定实际授权；不能把私密 state root 放宽。启动检查坏权限时拒绝，不自行 chmod/chown“修好”任意现有目录。

可信服务入口建议在单线程启动阶段设置受限 umask（例如077）；目录 default ACL 会覆盖 umask，因此仅凭 umask 不能替代真实 ACL/mode/owner 检查。切换 umask 是进程级操作，Module 不在可能已有其他线程的进程里临时修改它。本实现先校验不可由 client 写入的0700/0750目录并拒绝额外 access/default ACL，bind 后设置并读回 socket 的最终 owner/group/0660 权限、保存身份，最后才 listen；最终检查前没有可接受连接的监听端点。实际保证是 **listen 前完成最终 DAC 核验**，不是声称库自行建立了 bind 瞬间的077模式。[umask(2) 及 default ACL](https://man7.org/linux/man-pages/man2/umask.2.html#DESCRIPTION)、[Linux listen(2)](https://man7.org/linux/man-pages/man2/listen.2.html#DESCRIPTION)

listener 需要一把**独立于现有 per-operation broker.lock** 的 lifetime lock。现有锁保护 Broker 每次操作；若 listener 永久持有同一把排他锁，会阻塞实际业务方法和独立 watcher。`flock` 绑定 open-file-description，fork/dup 可以共同持有，最后一个引用关闭才释放；它是 advisory lock，不抵御有目录写权限且不遵守协议的进程。[现有持久性约束](../apps/runner/resource_broker/README.md#persistence-observation-and-failure)、[flock(2)](https://man7.org/linux/man-pages/man2/flock.2.html#DESCRIPTION)

从这些语义推导的安全顺序：

1. 校验并固定父目录身份；用 `O_NOFOLLOW|O_CLOEXEC` 打开固定 basename 的 regular、single-link、严格权限 lock 文件，取得 `LOCK_EX|LOCK_NB`，并比较 fd 与路径 dev/inode。拿不到锁就退出，**不得先 unlink socket**。
2. lock 文件不得在正常退出/清理时删除。若要声明“锁被替换后新旧进程仍不能形成两个锁域”，须把 lock dev/inode 冻结到跨重启可验证的可信 anchor/配置；仅当次 fstat/lstat 相等不足以证明后启动者没锁住新 inode。现有 Broker 已有类似 anchor，IPC 的新 lifetime lock 需要独立落实，不能借用一句“用了 flock”替代。
3. 持锁后，仅当既有路径是本 listener 配置预期 owner/权限的真实 socket、父目录/锁身份仍匹配、且没有存活的既有 listener 时，才允许清理本固定 basename 的 stale inode。文件、目录、symlink、owner/身份不符、权限错误、connect 超时或响应异常应拒绝并保留现场。不能把任何 connect 失败当 stale；如探测发现活 listener，拒绝启动，即使自己刚锁到了一个新 lock inode。
4. bind 后保存 socket 文件的实际 dev/inode，监听整个生命周期保持 lock FD；停止时先停止 accept、结束/核对在途调用，关闭 listener，并在仍持锁时只 unlink 与保存身份相同的 socket。发现路径已替换则保留新对象并报错；最后关闭 lock FD。失败构造路径也只清理自己已经创建且身份相符的 socket。
5. restart 必须等旧 owner 完成上述顺序或真实退出后重新取得同一锁域；禁止新进程通过直接 unlink 一个活 socket 夺取路径。UNIX socket unlink 不会立即消灭仍被引用的连接，所以仅看到路径消失不能证明旧服务已退出。[unlink(2)](https://man7.org/linux/man-pages/man2/unlink.2.html#DESCRIPTION)

这些 inode 检查依赖父目录不受非信任者写入；不能把一组 lstat+unlink 描述成在恶意同 UID 目录替换下原子的防护。listener/lock FD 都不得传给子进程；Python 的 non-inheritable 默认保护的是 exec，单纯 fork 仍需要关闭继承引用。[Python FD inheritance](https://docs.python.org/3.12/library/os.html#inheritance-of-file-descriptors)

## 5. 最小协议、有限资源和未知结果

当前冻结范围见 [IPC Interface](../apps/runner/resource_broker/IPC.md)，不是新的控制面公共契约：固定版本、有限 request_id、operation 和 exact payload；response 回显版本/request_id，只含当前方法的明确 DTO 或有限错误 code。request_id 只关联响应，不是新建“恰好一次执行”账本。operation 用静态 dispatch 表；按 `(uid,gid)` 冻结资源的 UUID、组织、工作区和允许操作，不用反射调用。实际实现与分项验证结果见第7节。

| 操作 | 唯一允许输入 | 保留的既有边界 |
| --- | --- | --- |
| ensure | 原私有签名 Assignment 的完整精确字段 | 同时匹配 policy 的资源 UUID/组织/工作区；原签名、配置 hash、scope、代次、TTL、writer 与 durable intent 检查全部保留 |
| renew | 原私有签名 Grant 的完整精确字段 | 同时匹配 policy 的资源 UUID/组织/工作区；不以本地 UID 替代签名、不自增 generation、不自行延长期限 |
| observe | 固定 catalog 的 resource_id | 可能触发过期停止；不能当成绝无副作用的普通 GET |
| revoke | 固定 resource_id 与 Interface 明确列出的有限安全原因 | 不接受任意错误或日志标签；只经原方法产生真实 StopEvidence |

四个操作接到 [Broker 正式 Interface](../apps/runner/resource_broker/README.md)。`reconcile`、`resource_limits` 明确排除在本 IPC 授权面之外，不是承诺后续开放的待实现 RPC；`watch` 仍是独立监督职责，也不上 wire。不允许全资源枚举、任意 host 扫描、clear/migrate 状态或 UUID 重分配。配置、catalog、XML、宿主路径、libvirt URI/参数、签名 key、raw/seed bytes、命令行和任意 kwargs 都不上请求 wire。已有签名 envelope 仍是本地私有结构，不能宣称等于权威控制面 Assignment/ExecutionGrant。

冻结限额为请求最多 **16384字节**、响应最多 **8192字节**、最多 **16个 peer**、策略资源合计最多 **64个**。listener 顺序处理请求，使用有限 backlog、JSON 层级/标量限额与读写期限，达到上限立即拒绝/背压；不建立无界线程、队列或响应缓存。`SO_RCVBUF` 不构成 AF_UNIX 应用报文上限，必须在应用层检查这些固定大小。[Python JSON 限额责任](https://docs.python.org/3.12/library/json.html#implementation-limitations)、[Linux UNIX buffer 语义](https://man7.org/linux/man-pages/man7/unix.7.html#DESCRIPTION)

请求携带宿主本地 `CLOCK_BOOTTIME` 派发截止时间，最长准入窗口 **5秒**；dispatch 前再次核对，排队过久的请求不得调用 Broker。BOOTTIME 是计入系统 suspend 时间的单调时钟，不是可任意修改的墙钟。client 总等待默认 **30秒**、上限 **60秒**；服务端 socket 读取、写入各限 **1秒**。client 分阶段计算总预算 remaining，不能每次 I/O 重新给满预算；不要把来自 client 的任意巨大 timeout 当服务端限额。这些派发/传输期限不修改 Broker 已签名 expires_at 或已有租约时钟职责。[Python CLOCK_BOOTTIME](https://docs.python.org/3.12/library/time.html#time.CLOCK_BOOTTIME)、[Python monotonic](https://docs.python.org/3.12/library/time.html#time.monotonic)、[既有 Broker 租约时钟](../apps/runner/resource_broker/README.md#persistence-observation-and-failure)

通信截止**不等于**已 dispatch 的资源操作取消。既有单条 libvirt edge 有10秒预算，四个获准方法仍可能做多条调用、等待状态锁或文件系统；不能把 socket timeout 包装成整个方法的硬执行上限。dispatch 后等待原方法完成并保留其持久状态，不能通过杀 worker 或回滚 JSON 来假装取消。文件锁/文件系统/宿主调用阻塞仍需外部服务监督及恢复，独立 watcher 也不能绕过另一个持锁进程。IPC 不建立新的硬停止时延保证。[当前正式实现](../apps/runner/resource_broker/__init__.py)

若请求可能已经发送，而连接丢失、调用者超时、响应被截断/无法认证或服务进程重启，client 应报告 `unconfirmed`：**本次调用结果未知、需要核对**，保持原资源 UUID、writer、租约与证据；禁止自动重发或生成新 UUID。只有在确认的传输/身份/格式/策略拒绝点结束且未进入 Broker，才可报告 `not_dispatched`。即使 server 返回 BrokerError，也不能一律断言“未产生副作用”：例如 observe、renew 的失败路径可能已请求停止。恢复使用原身份调用获准 observe，并遵循原 `reconciling`/StopEvidence 与独立监督规则；不为此增加 reconcile RPC。未决 create/stop 仍按既有运维边界处理。通信错误不得写成 stopped、释放预算或向控制面声称 Run 已完成。[Broker 未知 dispatch 与停止语义](../apps/runner/resource_broker/README.md#persistence-observation-and-failure)、[send 不承诺完成](https://man7.org/linux/man-pages/man2/send.2.html#DESCRIPTION)

## 6. 待实施资格与结果登记

下表保留初始设计时的 **PENDING** 清单，不回填为全部通过。末节按实际用例单独登记结果；特定 FD 转交方式、生产身份及真实 VM 等未由现有证据证明的部分继续待验，M0-05 整体不变。

| 资格组 | 必须保存的实际证据 | 当前结果 |
| --- | --- | --- |
| 协议/ACL | 真实 SEQPACKET 请求响应、精确 UID/GID 对、资源 UUID/组织/工作区及四操作策略拒绝、client 拒绝错误 server 身份；明确拒绝 reconcile/resource_limits | PENDING |
| 逐报文身份 | accept 身份允许后，普通 fork child 和 SCM_RIGHTS 接收进程用原连接发送均拒绝；正常父进程可用 | PENDING |
| 报文/FD | 正文/辅助截断、空记录、额外 cmsg、credentials 错误、未知 operation、重复键、非有限数、超深/超大 JSON；所有路径 FD 数恢复 | PENDING |
| 双向响应 | client 收到恶意 FD、截断、错误 request_id/版本/DTO 或错误发送者时清理并拒绝 | PENDING |
| 生命周期 | 第二 listener 拒绝且不改变首个路径；stale 恢复；锁/socket 替换、symlink、错误 owner/mode/default ACL；旧退出不删除新 inode | PENDING |
| 期限/不重放 | 16384/8192字节、16peer/64资源、BOOTTIME最多5秒准入、client默认30/上限60秒和服务端各1秒读写；无请求/不读响应/发送后断开/超时/重启，一次请求最多一次方法调用且无自动重试 | PENDING |
| 正式方法组件链 | 在现有真实 ResourceBroker 上仅替换 libvirt edge，跨进程经 IPC 调用 ensure/renew/observe/revoke；持久重建、旧签名/代次拒绝及既有 core/limits 回归 | PENDING；合成 libvirt 不证明真实 VM 启停 |
| 原生 IPC 资格 | 指定 Linux/Python3.12 的真实内核 UDS/进程/FD/锁/超时与重启；原生操作者使用确定性持久后端夹具，保存环境、源码与报告摘要 | PENDING；夹具只验证传输/策略/进程恢复，不替代正式 Broker 组件链或真实 VM 资格 |
| 生产隔离 | 专用 UID、精确 filesystem/libvirt ACL/polkit、服务安装与独立 watch 真实权限验收 | 未建立；不得随组件或开发 UID 资格升级 |


## 7. 实现、失败与实际结果

正式 [IPC Module](../apps/runner/resource_broker/IPC.md) 已实现冻结的四操作、双向连接/逐消息凭据、资源/组织/工作区策略、有界报文及客户端总预算。listener 的单例锁与 Broker 每次操作的锁分离；实际 socket 身份记录 dev/inode/ctime_ns，正常关闭在锁内清除旧记录。关闭与最终准入同步，已接受但未准入的请求不能在关闭后进入 Broker；已经准入的操作保留既有持久化和核对职责。结果未知时不自动重发，不把未知停止原因编造成 requested revoke。

首轮指定 Linux 回归共102项（66核心/20IPC/16操作者），1失败、0错误、0跳过：正常关闭后新建的同账号外来 socket 复用了旧 dev/inode，被旧记录误判为可回收。独立诊断重放确认新 socket 与旧记录 dev/inode 完全相同，原始失败和诊断保留。修正增加最终权限设置后的 ctime_ns 绑定，并在正常清理后清空旧 socket 记录；另有强制旧 ctime 负例。ctime 不是抵抗恶意同 UID 操作者的不可伪造身份，本 Module 的不可由 client 写入目录和宿主 UID 信任前提保持不变。

独立复审还提出连接阶段必须使用剩余总预算，原代码在文件系统检查后给 connect 一份新期限；Root 同时发现未知停止原因不应映射成 requested_revoke。两项公开 Interface 负例在指定 Linux 的旧源码上均实际 FAIL（2项/2失败/0错误），随后才修改实现。修正后只保留固定安全原因，未知值为 None；connect 在预算耗尽时不建立连接、明确 not_dispatched。源码复核的 umask 文字按实际 listen 前最终 DAC 保证修正，实际系统/内核/架构/Python/CPU 信息已进入报告及有限公开投影。

[最终服务器回归](../docs/operations/broker-ipc-components-2026-11-01.json)共116项：66既有核心、32 IPC、18操作者，0失败/错误/跳过。IPC 组件包含真实 ResourceBroker 与合成 libvirt 的签名四操作、持久停止、重构及旧授权拒绝；这不是实际 libvirt/VM 验收。最初114项通过和此前原生14项通过保留历史源绑定，最终环境与错误修正后的结果单独记录，不覆盖原件。

[最终真实进程资格](../docs/operations/broker-ipc-processes-2026-11-01.json)在 Linux7.0.0-38-generic/x86_64、Python3.12.3、72逻辑CPU上通过14项门槛，33个实际独立进程全部退出。生产 IPC listener/client 处理实际内核 credentials；后端明确为确定性持久夹具，核对4操作、越权前无调用、重复 request_id 不作传输去重、实际收到 FD 后关闭、继承的已连接 FD 换 PID 拒绝、idle peer期限、持久 effect 后丢响应/客户端不重发、SIGKILL后同 socket恢复、活动/外来/替换端点保护。该轮没有单独执行通过 SCM_RIGHTS 把已连接 socket 转交给接收进程再发送的变体，不把继承 FD 用例扩写为所有转交方式均已实测。

独立只读收集器不导入生产者的 summary 函数，另核5份源码快照、122个原始文件及222476字节、33份退出记录、7份ready记录、8次拒绝的FD零增长、正常3次与故障恢复1次持久effect、越权0调用及实际报告摘要；组件原始unittest日志和7个当前源摘要另核。最终原生报告SHA-256为 `a6e1126ae2b54c373f48e472ce8ac5468344172bbd6a2f4257b156babb5806d1`。首次收集器把既有0775共享validation父目录当作私有证据目录而拒绝；仅修正只读父目录前提并固定其身份，严格0700/0600证据子项保持不变，没有chmod共享目录，也不是原生资格失败。

本子项已开发完成并验证（VERIFIED），M0仍4/8、M0-05仍IN_PROGRESS。专用生产 UID、libvirt/polkit 委派与服务部署、实际 VM 经 IPC 生命周期、完整 Egress/控制面授权提交、断电及跨 namespace 仍未由本轮验收。镜像manifest保持12项，无新增VM/容器/工具链归档；原有96GiB诊断写层不动。实际 GitHub CI 见第8节，与指定服务器用例分别计量。

## 8. 实际 GitHub CI

源码`287d61bfc073e0a5a1f3e651a46729796510570b`的[完整资格CI](https://github.com/StateKnot/Harniqo/actions/runs/41288334278)成功：Python3.12.15共510项，0失败/错误/跳过，两组Node检查也全部通过。[真实IPC进程CI](https://github.com/StateKnot/Harniqo/actions/runs/41288334191)在Linux7.0.0-1017-azure/x86_64、Python3.12.15、4逻辑CPU上完成14项门槛/33进程，实际环境与指定服务器分别记录；后端仍是同一确定性持久夹具，没有生产UID或VM声明。

同提交的[三平台契约CI](https://github.com/StateKnot/Harniqo/actions/runs/41288334289)成功，每平台60定义、18路径、107样例、15生命周期案例通过；这不是三端桌面原生构建。完整资格的单份报告、进程资格唯一脱敏summary及三份契约artifact均已实际下载，逐项核对run/job/step、源码、完整锁及SHA-256。[公开CI索引](../docs/operations/broker-ipc-ci-2026-11-01.json)分别保存这三种范围。原始进程配置/身份、完整日志和服务器原件不上传公开CI产物。

最终证据提交仅补本节、E65和CI索引，受测运行代码与工作流保持不变；不为文档重复运行故障或模型任务。E64的实际内核/持久夹具与正式Broker组件、E65的CI、此前真实VM报告各自保留，M0仍4/8、M0-05仍IN_PROGRESS。
