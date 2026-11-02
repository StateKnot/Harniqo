# M0-05 libvirt 10 逐 API ACL 激活与回滚契约

日期：2026-11-02。研究基线：`9e10b06dd343eca400ada256bd603b3b3d0d36d8`。本文先固定部署及资格契约，随后按文末独立记录执行 bootstrap driver 的真实只读 API 资格；研究推导、组件回归和实际原件分别计量。**没有授予 VM 写委派、验证专用 UID 真实 VM 或激活生产服务包。**

此前的[稳定身份与实际服务上下文](m0-broker-dedicated-identities.md)已证明两个不同服务 UID；[已有服务身份研究](m0-broker-service-identities.md)与[离线部署包](../deploy/runner/README.md)的历史证据保持各自范围。管理员变更按受控步骤与持久原件执行；仓库新增可直接运行的[只读原生资格入口](../deploy/runner/LIBVIRT-ACL.md)。它不提供自动修改或回滚宿主的 CLI，不能靠调用方填写 `verified` 或把现有 `BLOCKED` 标志改成成功。

## 结论与最小可落地选择

本轮操作者的实际只读 preflight 已确认当前 libvirtd 为 `Type=notify`、`KillMode=process`、无 `ExecStop`，RW/RO auth 均为 none；没有 TCP/TLS unit/listener、没有 hooks、virtqemud socket inactive，三个既有 domain 全部 running；Ubuntu 管理身份属于 libvirt 组，nested VMX 开关为 Y。**本单元选定受控共享 daemon 激活及纯只读 driver proof**：两个服务身份明确收窄，全部非服务身份显式 API YES 保留原 none 兼容语义，不安装候选 VM 写委派。root/Ubuntu 管理能力是必须核对的正对照。这些事实尚不证明有效 driver或重启后的 QEMU 连续性；原始私有盘点应由实际部署资格报告绑定，本文不替代该证据。

独立完整 OS 中的 system libvirt 则降低共享 daemon 重启风险：它有独立 polkit、daemon、socket、域状态、存储及 QEMU 生命周期；既有共享宿主三个 domain 不参与内部测试。当前固定 `qemu:///system` 后端可保留，前提是隔离 OS 里有实际 KVM、相同版本及真实普通服务 UID。仅另开一个 root `virtqemud --config` 不能建立这个边界：它仍读取固定 QEMU 配置，运行目录也不能因换一个管理 socket 就认定已隔离。[virtqemud 文件契约](https://libvirt.org/manpages/virtqemud.html#files)

共享 system libvirt 也有可实施路径，但它是**全局授权与 daemon 生命周期变更**。若保留当前 `auth_unix_rw="none"`，可以只新增 `access_drivers=["polkit"]` 和精确规则，不必再切连接认证、不必引入 `org.libvirt.unix.manage=YES`。连接入口与 API driver 是不同层：`none` 连接认证只要求能连 socket；未配置 access driver 时 v10 选 `none`，没有 API 检查。[v10 配置说明](https://github.com/libvirt/libvirt/blob/v10.0.0/src/remote/libvirtd.conf.in#L118)、[v10 access manager 初始化](https://github.com/libvirt/libvirt/blob/v10.0.0/src/remote/remote_daemon.c#L391)

旧离线包的 candidate 面向 polkit 连接认证，含 manage 许可，不能原样作为此路径的 installer。共享宿主激活工具必须绑定已核对的 `auth_unix_rw=none`、现存 socket/DAC、精确服务身份、全量规则及配置原字节；遇到其他认证/模块 daemon/未知入口应拒绝此部署模式，不能自动改成另一方案。

本单元阶段顺序为：只读基线与可恢复 receipt → 安装服务收窄规则及明确的非服务身份兼容规则 → 仅更改 access driver → 核对准确 systemd job 并重启准确 daemon → **无 VM 副作用的 API 正负例证明 driver** → 保持 driver及服务收窄规则。后续独立单元才安装 Broker 精确委派并执行自有 VM 经 IPC 验收；原离线包保持 `BLOCKED`。任何未知 native 效果都留下 intent，确认写入者/重启 job 已结束后读取实际状态；不凭超时或进程退出码重发修改。

## 重启不等于绝对不影响 QEMU

上游手册说明正常重启 libvirtd 后 Guest 继续运行。v10 上游 unit 为 `KillMode=process`，没有 `ExecStop`；QEMU cleanup 不关闭 domain，初始化重连运行中对象。这些解释了为什么可以重启管理 daemon 而通常保留 QEMU PID。[libvirtd 手册](https://libvirt.org/manpages/libvirtd.html#description)、[v10 unit](https://github.com/libvirt/libvirt/blob/v10.0.0/src/remote/libvirtd.service.in#L26)、[v10 cleanup](https://github.com/libvirt/libvirt/blob/v10.0.0/src/qemu/qemu_driver.c#L980)、[v10 reconnect](https://github.com/libvirt/libvirt/blob/v10.0.0/src/qemu/qemu_driver.c#L838)

这不是无条件不宕机保证：v10 的 QEMU 重连遇到某些错误会调用 `qemuProcessStop`，QEMU reconnect hook 失败也进入此路径；初始化还可能处理 autostart。官方 virtqemud 手册因此建议实际可行时避免在有运行中 Guest 时重启。已经发生的 Guest 停止、磁盘写入或生命周期变化不能通过恢复 conf 字节撤销。[v10 reconnect 错误处理](https://github.com/libvirt/libvirt/blob/v10.0.0/src/qemu/qemu_process.c#L8569)、[virtqemud 重启建议](https://libvirt.org/manpages/virtqemud.html#description)

实际宿主必须先保存并审查以下只读事实；上游 unit 和 Ubuntu 常规包装都不能替代它们：

- `systemctl cat/show` 的实际 FragmentPath、DropInPaths、ExecStart、EnvironmentFiles、ExecStop/ExecStopPost、KillMode、KillSignal、TimeoutStopUSec、Restart、Requires/PartOf/BindsTo/Conflicts 和反向依赖；准确 socket activation 与模块/代理关系。
- 实际 executable、启动 argv/config 路径、PID、`/proc/PID/stat` starttime、boot ID、InvocationID、unit 文件与配置原字节摘要；`libvirt-guests`、daemon/QEMU hooks 及发行版附加行为。
- 所有既有 domain 的 UUID、active/persistent 状态、XML 摘要、自启动状态；活动 QEMU 的 PID/starttime/owner/cgroup，必要的业务健康探测及当前 migration/block/backup 等作业。只读基线不收集公开完整业务 XML/argv。

`KillMode=process` 只描述 systemd 对该 unit 的杀进程方式；必须确认 QEMU 实际 cgroup 与所有 stop handler，不能据此推定其他生命周期服务不会介入。`libvirt-guests.service` 有自己的 `ExecStop`，其脚本默认 `ON_SHUTDOWN=suspend`；它不能被混同为普通 libvirtd restart 的关闭语义。[systemd 255 KillMode](https://www.freedesktop.org/software/systemd/man/255/systemd.kill.html#KillMode=)、[v10 libvirt-guests unit](https://github.com/libvirt/libvirt/blob/v10.0.0/tools/libvirt-guests.service.in#L14)、[v10 guests 脚本](https://github.com/libvirt/libvirt/blob/v10.0.0/tools/libvirt-guests.sh.in#L28)

本阶段已有的服务器维护授权覆盖范围不因本文新增确认流程；以上是可执行变更的技术门槛与真实影响说明。执行后必须逐项确认原 QEMU PID **与 starttime**、域状态/配置、网络/过滤器及健康仍符合基线。重启 job 失败或任何既有域变化立即停止后续准入；不得为凑资格再重启既有 VM。

另对 Ubuntu Noble 官方 `10.0.0-2ubuntu8.19` Debian source tar 做了只读核对：`wait-for-qemu-kvm.patch` 对 libvirtd unit 仅增 `After=qemu-kvm.service`；`Debianize-libvirt-guests.patch` 将 guests 脚本默认改为 `ON_SHUTDOWN=shutdown`、`ON_BOOT=ignore`。这说明发行版默认可能与上游不同，不证明实际安装的 patch revision或 `/etc/default/libvirt-guests`。该 tar SHA-256 为 `99450ba43612f5a13f96a7c74d0bc5b5509af5e84c2ae64a7db1eb86bc852f50`。[Ubuntu 官方源码包](https://archive.ubuntu.com/ubuntu/pool/main/libv/libvirt/libvirt_10.0.0-2ubuntu8.19.debian.tar.xz)

## 非 Harniqo 账号的兼容策略必须显式选择

“规则对其他用户返回 NOT_HANDLED”只表示本文件没有作决定，**不表示继续沿用原来的 access driver=none**。polkit API policy 默认允许只读 permission，默认拒绝其他 permission；启用 driver 后，既有普通管理者的写 API 会因此被收窄。libvirt 的名单 API 还按对象 permission 过滤结果。[libvirt 默认 ACL](https://libvirt.org/acl.html#access-control-drivers)、[polkit 默认 permission](https://libvirt.org/aclpolkit.html#permission-names)

本单元规则区分以下身份，绑定两个服务账号实际 NSS 名称/数字 UID/GID 映射并拒绝碰撞：

| 身份 | `org.libvirt.api.*` 决策 | 适用边界 |
| --- | --- | --- |
| Broker | bootstrap 仅 QEMU `connect.getattr` YES；委派阶段精确 permission+对象 YES；其他 libvirt action NO | 不得回落只读默认；对象必须 UUID **且**名称成对匹配，缺失属性 NO |
| Supervisor | 全部 libvirt action NO | namespace 中的 socket 不可达验收另记，不能替代 API 拒绝 |
| 全部非服务身份 | API YES；对其他 action NOT_HANDLED | 显式选择旧 none 兼容模式，包括 root/Ubuntu；它保留原有宽权限，不是最小权限 |

这一全覆盖选择以连接入口/DAC/认证完全未放宽为前提，且服务身份分支必须先于兼容分支，禁止泛组匹配意外将服务身份归为 legacy。备选的显式 legacy UID 集合可以更窄，但其余身份返回 NOT_HANDLED 时只能声明保留已列出的旧管理者；不能承诺全部原 RW 客户端的 none 语义。文档、plan 与证据必须记录实际采用哪一模式，不能暗换。

原生 TCP/TLS/SASL 也是硬边界。polkit driver 需要本地进程身份；官方说明 TCP 连接没有所需身份时会被拒绝，SSH tunnel 则识别目标机上的本地 SSH 用户。v10 在请求 polkit 前就要求 PID/starttime/UNIX UID，所以 legacy YES 规则不能修复这种身份缺失。必须盘点所有 libvirt 监听入口、真实客户端及其连接模式；若需保持原生远程入口，当前共享 daemon 路径不能被宣称兼容。[身份限制](https://libvirt.org/aclpolkit.html#user-identity-attributes)、[v10 caller 核对](https://github.com/libvirt/libvirt/blob/v10.0.0/src/access/viraccessdriverpolkit.c#L62)

规则要审查 `/etc/polkit-1/rules.d` 与 `/usr/share/polkit-1/rules.d` 的全部文件与 basename 排序；首个有结果的函数结束授权，`00-` 不保证绝对最早。同名文件 `/etc` 先于 `/usr`。两目录变更触发规则重载，不要求为切规则再重启 libvirtd；仍必须等待并以真实调用确认新规则已生效。对象属性决策不用 `AUTH_*_KEEP`，因为缓存按同一 action/subject 可复用于不同 details。[polkit 规则排序、重载与缓存](https://www.freedesktop.org/software/polkit/docs/latest/polkit.8.html#polkit-rules)

## 激活工具必须持久化的阶段

以下定义执行阶段契约，实际执行范围与结果见文末；操作者保持 root，而实际 Broker、watcher 和 peer 保持原稳定普通 UID。daemon 激活步骤只管理 plan 固定的配置键与本任务规则文件，禁止重写其他规则、网络、QEMU 配置或既有 domain。只读 qualifier 不修改 socket 权限；若原 DAC 阻止服务身份抵达 RW socket，另由受信任管理员在 driver 启用且服务规则已加载后执行精确的临时命名 UID ACL 准入与 CAS 恢复，步骤见[操作说明](../deploy/runner/LIBVIRT-ACL.md)。它不交付持久生产 socket 授权。

1. **Prepare / check**：核对所有输入祖先、owner/mode/ACL、原始摘要、实际 daemon/config/socket、可用空间、账户和全部规则顺序。冻结既有域基线，保存 root 私有备份与恢复 receipt；本单元不创建测试 domain。固定软件/源码/配置摘要；过期或发生并发变化的 plan 拒绝应用。
2. **Bootstrap**：新服务尚不接受有效授权。原 `auth_unix_rw=none` 保持；服务规则与 legacy 兼容规则先装好，检查 polkit 语法/载入日志并做限定 subject/details 的静态/`pkcheck` 检查。后者仅预检规则，不能记作 daemon ACL 证明。
3. **Driver enable**：先持久 intent，再以 compare-before-write 原子发布只改变 `access_drivers` 的配置；父目录同步并记录新原字节摘要。确认 actual unit/argv 读取此文件后，派发一次准确 daemon restart，绑定 systemd job/InvocationID，未知结果保持恢复态。
4. **Driver proof**：准确新 daemon 就绪、原 VM 全部核对通过后，先执行下节无副作用正负例。负例意外成功即失败；不能放行管理连接或增加权限来“再试”。
5. **本单元停止位置**：driver proof 成功后保持 Broker bootstrap 和 Supervisor全拒绝，不安装 candidate，不签发有效 VM 执行授权。失败回退优先保留 driver+服务收窄，不自动恢复 none。
6. **Commit evidence**：原始失败、未知结果和恢复记录保留。生成可独立校验的 root 私有证据，公开脱敏索引仅引用摘要及范围；只有实际原生资格通过才能新增其对应 VERIFIED 声明。driver proof 不自动激活离线包、不抵扣专用 UID VM 或生产 Supervisor 集成。

后续的 **Scoped delegation / qualify** 单元只可在 driver proof 成功且配置未漂移后原子替换本任务规则为 Broker 精确委派，所有身份与 legacy 决策不变。规则切换无需第二次 daemon restart。核对新规则真实生效后，才签发短期授权并执行自有 VM 的完整 IPC/文件/进程/cgroup 资格；下节第二道探针属于此后续范围。

服务的最小委派仍是 QEMU `connect.getattr/search-domains` 与固定成对 domain `getattr/read/write/start/stop`。这组 permission 不是四条命令白名单，规则看不到 XML/磁盘路径/hash/网络/设备拓扑；`domain.write` 被多个 API 复用。Broker 的固定模板及不接受调用者 XML 的边界继续必须成立。[v10 API permission 标注](https://github.com/libvirt/libvirt/blob/v10.0.0/src/remote/remote_protocol.x#L3860)、[v10 domain details](https://github.com/libvirt/libvirt/blob/v10.0.0/src/access/viraccessdriverpolkit.c#L161)

## 最小真实探针与必须先准备的对象

第一道 driver proof **不需要创建或修改任何 domain**。以实际 Broker UID/GID/组/NNP/capabilities 建立字面 `qemu:///system` 的正常 RW 连接，不用 `--readonly`、RO socket、交互认证或调用者 URI。优先通过已安装 libvirt C API 记录同一连接结果；virsh 的进程级证据则同时记录准确命令与每次连接身份。

| 探针 | 预期及证明 | 前提与误判排除 |
| --- | --- | --- |
| Broker `virConnectGetHostname` | 成功：QEMU `connect.getattr` 正例 | 同一个 RW 连接；不以可能只读本地缓存的 `uri` 代替 |
| 同连接 `virConnectGetCapabilities` | `connect.read` 明确 API ACL 拒绝 | 纯只读、无需对象；原 none / 默认只读 policy 会允许，可安全发现 driver 未启用 |
| Supervisor 正常 `virConnectOpen` | QEMU `connect.getattr` 明确 API 拒绝 | 先在 mount 屏蔽外用实际 UID raw AF_UNIX connect精确 RW socket成功并关闭；sandbox 中 socket 不可达单独验收 |
| Broker/legacy 同时调用纯只读 API | Broker 被拒绝、已声明 legacy 成功 | 核对实际客户端 UID，不把 root 全通当普通 legacy 测试 |

上述 API 对应 v10 `connect:getattr/read` 标注；driver 不允许交互，使用真实 caller PID/starttime/UID 发往 polkit。特别是 `virConnectOpen` 本身检查 `connect.getattr`，因此 Supervisor全NO时**打开连接即被 API ACL 拒绝**，不能期待先 open成功、再 hostname拒绝，两者是同一 permission。[v10 open ACL](https://github.com/libvirt/libvirt/blob/v10.0.0/src/remote/remote_protocol.x#L3813)、[QEMU open 执行位置](https://github.com/libvirt/libvirt/blob/v10.0.0/src/qemu/qemu_driver.c#L1065)、[v10 hostname/capabilities ACL](https://github.com/libvirt/libvirt/blob/v10.0.0/src/remote/remote_protocol.x#L3852)、[v10 hostname](https://github.com/libvirt/libvirt/blob/v10.0.0/src/remote/remote_protocol.x#L4195)、[v10 polkit 调用](https://github.com/libvirt/libvirt/blob/v10.0.0/src/access/viraccessdriverpolkit.c#L106)

拒绝证据至少包括实际 API、flags、固定 URI/target socket、caller credentials、错误 code/domain、对应 action/driver 与安装规则/daemon 的摘要关联。v10 的 `VIR_ERR_ACCESS_DENIED=88` 与 `VIR_FROM_ACCESS=55` 是可核对的结构化信息：ACL generator 将错误域设为 ACCESS，在 polkit NO 返回的 `rv==0` 分支产生 ACCESS_DENIED，消息带 driver，通常不包含完整 action。因此 action 以固定 API/permission 映射与规则/daemon 日志关联，不伪造为客户端直接返回字段。最终以目标发行版真实结果及对应源码路径确定判定，不靠匹配一句 stderr。socket DAC、`AUTH_FAILED`、`NO_DOMAIN`、不支持的参数、XML 校验或普通文件不可读不能替代 API ACL 负例。[v10 错误 ABI](https://github.com/libvirt/libvirt/blob/v10.0.0/include/libvirt/virterror.h#L108)、[ACL 错误域生成](https://github.com/libvirt/libvirt/blob/v10.0.0/src/rpc/gendispatch.pl#L1873)、[ACL 拒绝生成](https://github.com/libvirt/libvirt/blob/v10.0.0/src/rpc/gendispatch.pl#L2035)

第二道对象及 VM 资格先做所有权初始化：创建 root 控制的发行代码/config、新 Broker 私有 key/state/catalog、真实 raw/seed及仅所需 QEMU 文件 ACL、已绑定 channel/console socket，正式 CLI 初始化新 journal。冻结 UUID/name 及所有路径与文件身份并与既有域做冲突检查；不得接管身份阶段不可启动的 canary、旧开发 journal 或已有域。执行对象为本次独占资源，issuer 留在受信任侧，Supervisor 不读取 key。

- 自有 matched 域通过真实 IPC `ensure → observe → renew → revoke`；实际普通 UID Broker 调用 create/dumpxml/destroy，QEMU owner、KVM、固定 XML、资源限额、线程归属、真实 absence 与 receipt 全部核对。
- 自有 matched 域普通 `virDomainGetXMLDesc(flags=0)` 成功；同一连接同一真实 domain handle 的 `VIR_DOMAIN_XML_SECURE` 拒绝。它只可能暴露本次无密钥测试域，即使规则意外放行也不改 VM。`read_secure` 与普通 `read` 是不同 permission。[v10 secure XML ACL](https://github.com/libvirt/libvirt/blob/v10.0.0/src/remote/remote_protocol.x#L3898)、[v10 XML 执行位置](https://github.com/libvirt/libvirt/blob/v10.0.0/src/qemu/qemu_driver.c#L5712)
- 非 catalog 对象由 root/legacy 先只读证明存在，优先使用本次新建、明确 inactive/autostart=false 的无业务哨兵，或只读查询已盘点既有 UUID。Broker 的列表不返回它；直接 lookup 返回明确 ACL 拒绝。lookup 先被 `domain.getattr` 拒绝时，必须记作 lookup 负例，不能声称已执行 dumpxml。不存在对象的拒绝不计分。[v10 lookup/list ACL](https://github.com/libvirt/libvirt/blob/v10.0.0/src/remote/remote_protocol.x#L3945)
- 需要验证 UUID/name 交叉匹配或禁止写 permission 时，只能用本次拥有的无碰撞资源。预先声明其 XML、磁盘、名称与全部潜在成功副作用，意外成功立即停止准入并按 receipt 精确清理。不得对既有业务域发 destroy/undefine/reset/save/migrate/monitor 试探；不得执行宿主 power API。v10 create 解析 XML 后先检查 ACL，再加入域列表/启动，因此错误属性可在对象真正建立前被拒绝，但意外允许仍是一次真正创建，必须具备自有资源清理能力。[v10 create 顺序](https://github.com/libvirt/libvirt/blob/v10.0.0/src/qemu/qemu_driver.c#L1472)

专用 UID 可读 QEMU 事实也必须提前核对。v10 privileged QEMU stateDir 为 `/run/libvirt/qemu`，启动通过 `virCommandSetPidFile` → `virPidFileAcquirePath` 创建 pidfile，请求 mode `0644`，路径和最终权限仍受实际 umask/现存目录/ACL/发行补丁影响。它不同于另一个请求 `0600` 的 `virPidFileWritePath`；不能凭函数名推断。[v10 stateDir](https://github.com/libvirt/libvirt/blob/v10.0.0/src/qemu/qemu_conf.c#L149)、[QEMU pidfile](https://github.com/libvirt/libvirt/blob/v10.0.0/src/qemu/qemu_process.c#L7178)、[command pidfile](https://github.com/libvirt/libvirt/blob/v10.0.0/src/util/vircommand.c#L787)、[AcquirePath mode](https://github.com/libvirt/libvirt/blob/v10.0.0/src/util/virpidfile.c#L343)

实际 Broker 服务上下文应只读验证每个安全祖先、pidfile、`/proc/QEMU/{stat,cmdline,cgroup,task}` 与所需 cgroup 文件。hidepid、ProtectProc、mount namespace及 LSM 可能改变结果。失败时先定位真实机制；不加入 libvirt/kvm 组、不增加 `CAP_SYS_PTRACE`/`CAP_DAC_OVERRIDE`，不全局放宽目录来凑通过。启动前对既有 QEMU 的只读检查仅证明其观察能力，最终仍须对自有新 QEMU 全量验收。

## 回滚必须区分委派回滚与旧 daemon 恢复

**委派回滚**优先保持已证明的 polkit access driver：先关闭新任务准入，仍保留原 watcher/key/state/catalog，按原许可经 IPC 撤销本次 VM 并独立确认停止；所有未决停止保留管理能力和材料。排空后停 listener/watch并确认本任务 unit InvocationID，再切 bootstrap deny、用真实 API 核对。若因安全事件必须先收窄规则，则由已声明的 root/legacy 恢复操作者依据 receipt 对**本次精确 UUID**处理；不能在权限撤走后假定 watcher 仍能完成 destroy。

**恢复原 access driver=none**需要另一次 daemon restart与相同既有 VM 门槛。`auth_unix_rw=none` 下保留 polkit deny 文件也不会保护服务身份：连接认证和 API 层都不再调用它。恢复前必须停止并核对本任务所有服务身份进程和 VM，并证明它们对所有管理入口的可达性符合恢复安全契约；独立 OS 可直接以完整 OS 生命周期隔离。不能宣称“恢复 conf+保留 NO rule”是 fail-closed 回滚。此结论直接来自 [none 的连接语义](https://github.com/libvirt/libvirt/blob/v10.0.0/src/remote/libvirtd.conf.in#L118)与 [none access manager 分支](https://github.com/libvirt/libvirt/blob/v10.0.0/src/remote/remote_daemon.c#L391)。

恢复只写回有基线摘要且仍由本任务拥有的精确文件/键；并发变化、native job未结束、stop outcome未知、证据保存失败或原 QEMU 变化均停止自动推进，保留恢复 receipt及原始材料。不能全表恢复 polkit、按名称前缀杀 VM、删除未知 journal/key/images，或为了恢复文档状态重建既有域。

## 独立 OS / nested KVM 的实际准入

可使用独立执行宿主，或新建独占 L1 OS，在其内部安装相同 libvirt/QEMU/systemd 与两名稳定服务 UID。L0 nested 开关和 CPU feature 必须已经支持；L1 要能看到 VMX/SVM、可用 `/dev/kvm`，L2 明确使用 KVM，不能由 TCG 成功替代。内核文档说明 nested 自 4.20 默认启用但发行版可覆盖，并要求向 L1 暴露相应虚拟化 feature。[Linux nested KVM](https://docs.kernel.org/virt/kvm/x86/running-nested-guests.html)、[nested VMX](https://docs.kernel.org/virt/kvm/x86/nested-vmx.html)

L0 只读检查不足时，不能卸载/重载正在承载既有 VM 的 KVM 模块或修改共享 CPU/网络策略。L1 的来源、摘要、发行版、磁盘/内存/CPU预算、唯一标识、管理通路和销毁 receipt 也必须受控；先以实际 L2 KVM 小实例及所需 `/proc`/cgroup/UNIX socket 语义通过准入，再运行完整 Broker 资格。容器共享宿主 kernel/polkit、仅替换 URI或在现有 root daemon 旁再启动一个 daemon，都不能自动算作独立 OS。

风险排序为：重连或生命周期处理影响既有 VM；全局 driver 切换中断旧客户端；driver未生效/完整回滚后新 UID 仍可宽管理；早期规则或默认只读越权；错误负例归因；permission 复用导致超出固定 XML 的权限。实现与真实验收必须分别消除或接受这些具体风险，不能以静态规则、组件测试或历史专用身份结果代替。

## 实际执行与原件核验

本轮已开发完成并验收限定的 **bootstrap API ACL 资格子项**。只读工具使用固定 system URI、真实降权 worker、父进程 `/proc` 身份握手、零 capabilities/NNP、同一 native FD/peer 核对，以及有限输出、Linux WNOWAIT 清理后 reap；34 项指定 Linux/Python 3.12 回归全部通过（20 纯契约、14 真实进程），0 失败/错误/跳过。双轴评审发现的后代遗留与超时证据缺口已修复并回归，[组件原件索引](../docs/operations/libvirt-acl-components-2026-11-02.json)与真实结果分别计量。

授权管理员保存全部原字节、plan 与现有业务基线，先以活的 PID/start/UID 验证 policy 加载屏障，再只启用 polkit access driver；同步 systemctl 等待准确重启任务完成，一次 daemon restart 后原三个 QEMU 的 PID/starttime/owner/cgroup/XML/state、两个 network 和 24 个 filter 不变。既有 root 与普通管理身份 RW 查询成功；非服务 API YES 明确保留原 none 的宽管理权限，不是全宿主最小权限整改。未切换连接 auth，未安装 VM 写候选或正式服务 unit。

随后临时给两个精确 UID 准入 RW socket，Broker 同一 RW 连接的 hostname 正例、capabilities 的 88/55 拒绝、hostname 再次正例通过；Supervisor 先 raw RW connect 成功、正常 open 在 connect.getattr 返回 88/55。真实 UID/GID/groups、五类 capabilities=0 与 NNP=1 独立核对；所有 worker 已退出，临时 ACL 与 socket 原始身份完整恢复。新独立 collector 读取并核验 707 份原件，重采当前 3/2/24 基线、规则/配置/daemon、绑定版本及 socket，13 组最终核验全部通过，见[原生与独立审计索引](../docs/operations/libvirt-acl-native-2026-11-02.json)。

首次 native worker 在 ready 前因本任务 root 发行目录受 umask 变成 0700 而退出，尚未执行 API 调用；失败与 ACL 恢复原件保留。只对已核对来源/dev/inode 的三个公开代码目录改为 0755，并实际以 Broker 读取固定源摘要；新的独立目录复测通过，未覆盖失败或第二次重启 daemon。首个只读 collector 误把 QEMU 拥有的 runtime pidfile 当作 root 源码目录拒绝；仅观察读取改用有界稳定文件检查，并先绑定冻结 PID/start/owner/cgroup/native UUID，其他源码/配置/规则仍严格 root 控制。失败审计同样保留，宿主目录权限未因此放宽。

当前 driver 与 bootstrap 收窄规则保持启用；socket DAC 已恢复，未交付持久生产 socket 准入，没有 VM 创建/启动/停止、模型调用或新 TCP 监听。固定 UUID/name 写委派、专用 UID 真实 VM 经 IPC、正式 Supervisor 与服务激活仍须后续独立验收，原包保持 BLOCKED，M0 仍 4/8、M0-05 仍 IN_PROGRESS。CI 发布后的实际原件另行登记，不由本轮服务器成功推断。

## 实际 GitHub CI

源码`6115898459b2037804bf153317028afc5d58de6e`的[完整资格CI](https://github.com/StateKnot/Harniqo/actions/runs/41389364911)670 项 Python 回归、0失败/错误/跳过，两组 Node 检查全部成功；[同源三平台契约CI](https://github.com/StateKnot/Harniqo/actions/runs/41389364897)各60定义/18路径/107样例/15生命周期成功。4份实际 artifact 已下载，运行/步骤、报告、源码与锁摘要已核对，见[CI索引](../docs/operations/libvirt-acl-ci-2026-11-02.json)。CI 是组件/进程夹具与契约范围，不启用实际 libvirt driver，不继承 E73 指定服务器原生授权结果，也不是三端桌面构建。

最终增补 E74 仅改变证据与文档；运行代码、测试、工作流、契约均保持此受测提交的字节，不重复管理员变更或 VM 操作。M0 仍4/8、M0-05仍 IN_PROGRESS，服务包继续 BLOCKED。
