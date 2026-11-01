# M0-05 Broker / Supervisor 专用服务身份与 libvirt 最小委派核验

日期：2026-11-01。状态：**严格服务入口、独立 watch、离线部署产物及开发 UID 下真实 VM 经 IPC 的限定子项已开发完成并验证（VERIFIED）；生产专用 UID、有效 libvirt ACL 与系统服务安装仍 PENDING**。M0-05 仍 IN_PROGRESS。以下研究与初始资格清单保留原范围；本轮实际执行、失败、修正与证据见末节。

范围依据：[领域词汇](../GLOSSARY.md)、[架构 §2](../docs/architecture/ARCHITECTURE.md#2-进程网络与信任关系)、[Broker](../apps/runner/resource_broker/README.md)、[IPC](../apps/runner/resource_broker/IPC.md)。沿用固定实例组，不引入动态分配或第二套业务状态。已有 [Host OOM 证据](../docs/operations/host-qemu-oom-kvm-2026-11-01.json) 记录 libvirt **10.0.0**、QEMU **8.2.2**、Python **3.12.3**、glibc **2.39**，且 `dedicated_service_identity_verified=false`。本文查阅 libvirt `v10.0.0` 源码；systemd 取 255 文档及 Ubuntu 24.04 noble 手册，实际安装补丁、编译选项与生效配置仍须单独记录。

## 1. 不能跳过的准入条件

1. **连接认证与每 API 授权是两层配置。** `auth_unix_rw="polkit"` 控制 RW 连接认证，`org.libvirt.unix.manage` 是完整管理连接权限。真正逐对象限制需运行中的正确 daemon 启用 `access_drivers=["polkit"]`。仅写 `org.libvirt.api.*` 规则、限制 socket mode 或加入 `libvirt` 组，都不能把默认无 ACL 的 RW 连接变成最小权限连接。[libvirt 认证](https://libvirt.org/auth.html#unix-socket-policykit-auth)、[libvirt ACL](https://libvirt.org/acl.html#access-control-drivers)
2. **未证明有效 ACL driver 时，不能给 Broker 放行 manage。** v10.0.0 启动代码在 `access_drivers` 未设置或为空时明确选择 `none`。配置文件一行、安装了 polkit、`virsh -V` 有 polkit 编译支持，都不是运行中的访问管理器证明；ACL 配置需重启对应 daemon 才生效。[v10 daemonSetupAccessManager](https://github.com/libvirt/libvirt/blob/v10.0.0/src/remote/remote_daemon.c#L391)、[ACL 配置与重启要求](https://libvirt.org/acl.html#access-control-drivers)
3. **polkit 的对象限制不是模板验证。** `domain.createXML` 可按精确 UUID 与名称授权，但 polkit 看不到磁盘路径、XML、镜像摘要、配置 hash、NIC 或 hostdev。允许 `domain:write` 也允许其他复用此 permission 的 API。固定模板、签名、持久状态及不接受调用者 XML 的 Broker 仍是受信任边界，不能宣称“Broker 被攻陷后也只能执行这四条 libvirt 命令”。[v10 polkit Domain 属性](https://github.com/libvirt/libvirt/blob/v10.0.0/src/access/viraccessdriverpolkit.c#L161)、[v10 API 权限标注](https://github.com/libvirt/libvirt/blob/v10.0.0/src/remote/remote_protocol.x#L3860)
4. **实际部署需两个不同且稳定的非 root UID。** Broker/listener 与独立 watcher 属于受信任资源管理域；Supervisor 没有 issuer key、Broker 状态、libvirt/Docker 管理权限。Supervisor 仅持四操作 IPC 客户端与后续控制面身份。当前开发 UID 的进程证据不能替代不同 UID 的文件隔离证据。[当前 IPC 身份约束](../apps/runner/resource_broker/IPC.md#内核身份及权限)
5. **不能用 sudo/整服务 root 或“先给管理组跑通”降级。** Broker 服务的 real/effective UID/GID 必须分别相等，配置精确 peer UID/主 GID；无 setuid helper、环境继承的任意 libvirt URI、任意命令或 guest 可达宿主 socket。默认生成的部署产物必须保持未放行；单独完成有效 ACL driver 的审查、配置与真实负向验收后才允许部署管理连接许可。

## 2. libvirt 10 权限与实际调用边界

现有 `resource_broker/__init__.py` 固定 `/usr/bin/virsh --connect qemu:///system`，原生命令环境仅固定 PATH 与 `LC_ALL=C`；调用方不能用 `LIBVIRT_DEFAULT_URI` 重定向。其命令边是 `list --all --uuid`、`dumpxml UUID`、`create 固定生成的文件 --validate`、`destroy UUID`。下表是该源码与 v10 上游的最小候选集合，**不是实际目标 daemon 已通过的权限清单**。

| Broker 命令/阶段 | libvirt API / permission | 所需作用范围 |
| --- | --- | --- |
| 连接 QEMU driver | `virConnectOpen` / `connect:getattr` | `connect_driver == "QEMU"` |
| `list --all --uuid` | `virConnectListAllDomains` / `connect:search_domains`，每结果 `domain:getattr` 过滤 | 连接可搜索，域仅固定 catalog 的 UUID+名称 |
| UUID 查找与 `dumpxml` | lookup / `domain:getattr`；`virDomainGetXMLDesc` / `domain:read` | 固定 UUID+名称；不允许 `read_secure` |
| `create --validate` | `virDomainCreateXML` / `domain:write` **且** `domain:start` | 新 XML 中的固定 UUID+名称 |
| `destroy` | `virDomainDestroy[Flags]` / `domain:stop` | 固定 UUID+名称 |

这些权限来自 [v10 remote_protocol.x](https://github.com/libvirt/libvirt/blob/v10.0.0/src/remote/remote_protocol.x#L3813)，包含 create、lookup、XML 读取、destroy 和 list 的标注。不能误写成 `connect:write` 或不存在的 `domain:create`。下划线在 polkit action 中转成连字符，例如 `connect:search_domains` 是 `org.libvirt.api.connect.search-domains`。[libvirt action 命名](https://libvirt.org/aclpolkit.html#permission-names)

v10 QEMU driver 先将新 XML 解析成 `virDomainDef`，然后执行 `virDomainCreateXMLEnsureACL(conn, def)`，最后才加入域列表、启动 QEMU。因此不存在“create 时完全没有 UUID，只能给所有域 write”的必要性。规则应把 **UUID 与名称作为一对同时匹配**，不能只检查名称前缀、单独 UUID 集合/名称集合的任意组合，也不能从 Supervisor 输入生成许可。缺少属性直接拒绝。[v10 qemuDomainCreateXML](https://github.com/libvirt/libvirt/blob/v10.0.0/src/qemu/qemu_driver.c#L1472)

不过同一许可会覆盖多 API：v10 `domain:write` 也用于 attach/detach、部分内存/CPU 参数等。`--validate` 只验证 libvirt Schema，不证明符合 Harniqo 固定拓扑。服务 UID 被控制后的授权上界必须如实写为“固定对象及 libvirt permission 集合”，不可写为“仅允许创建该固定 XML”。如果产品要求抵抗恶意 Broker，应另建更窄的受信任特权中介或采用经完整验证的 MAC/独立执行宿主边界；不能靠此 polkit 规则冒充该能力。[v10 write 权限复用](https://github.com/libvirt/libvirt/blob/v10.0.0/src/remote/remote_protocol.x#L3860)

规则设计必须对目标服务账号覆盖整个 `org.libvirt.api.*`：先允许上表确切动作/对象，其他返回 **NO**；不能默认 `NOT_HANDLED`，因为 libvirt 的只读 permission 默认允许。否则 Broker 仍可读其他租户 XML、枚举非 catalog 资源或访问无关 driver。Supervisor 对 `org.libvirt.unix.manage`、`monitor` 和全部 `org.libvirt.api.*` 均明确 NO。规则不改变其他账号的策略。[只读默认策略与对象属性](https://libvirt.org/aclpolkit.html)

部署必须核对全量 polkit 规则排序；先返回结果的规则结束授权，不能假定某个 `00-*.rules` 必然先于所有已安装规则。不使用 `AUTH_*_KEEP` 或交互认证作为 headless 服务通行条件，避免相同 action 的缓存跨对象属性复用。测试 JS rule 函数的返回值只验证规则逻辑；不证明 libvirt 调用了它。[polkit rules 顺序与 KEEP 语义](https://www.freedesktop.org/software/polkit/docs/latest/polkit.8.html#polkit-rules)

运行最小集合还需以目标发行版真实 virsh 进程核对：CLI 附加 API、daemon 拆分时的 secondary driver 连接若被拒绝，应先定位并证明必要性，不自动放开 `org.libvirt.api.*` 或全部 QEMU action。libvirt 明确说明 secondary API 连接可能使用 `network`、`storage` 等不同 `connect_driver`。[secondary driver 语义](https://libvirt.org/aclpolkit.html#hypervisor-driver-connect_driver)

## 3. 普通用户能确认什么

普通用户可以收集以下**只读线索**。下列保留研究阶段的执行模板，末节另外记录本轮实际41项只读盘点；管理员配置/授权操作尚未执行。所有输出仅保存至受控证据，不公开完整现有域 XML、业务路径或进程命令行：

```sh
id
getent passwd harniqo-broker harniqo-supervisor libvirt-qemu
systemctl --version
systemctl show libvirtd.service virtqemud.service virtproxyd.service -p FragmentPath -p DropInPaths -p ExecStart -p MainPID
systemctl cat libvirtd.service libvirtd.socket virtqemud.service virtqemud.socket virtproxyd.service virtproxyd.socket
/usr/bin/virsh --no-pkttyagent --readonly --connect qemu:///system uri
/usr/bin/virsh --no-pkttyagent --readonly --connect qemu:///system version --daemon
```

读取有权限访问的 `/etc/libvirt/libvirtd.conf` 或 `virtqemud.conf`、socket 路径/owner/mode、polkit policy/rules，以及 daemon 的进程启动参数，有助于定位实际部署模式；不要为读取失败增加权限或猜测内容。socket activation 下，socket 权限/目录可能由对应 `.socket` unit 而非 daemon 配置控制，proxy 还可能把请求转给模块 daemon。[libvirt daemon 与 socket 配置](https://libvirt.org/daemons.html)、[virsh readonly/禁止认证代理](https://libvirt.org/manpages/virsh.html#options)

**这些命令不能可靠返回有效 `access_drivers`。** 已查阅的公开 host API 与 virt-admin API 没有“查询当前 access driver”的契约；版本、capabilities、宿主安全模型以及成功的只读调用都不等于 per-API ACL 生效。可读磁盘配置也可能尚未加载，或者实际进程使用了另一配置路径。这是依据公开接口与启动实现作出的限制判断，不是本轮对指定服务器的检查结果。[host API](https://libvirt.org/html/libvirt-libvirt-host.html)、[virt-admin 接口](https://libvirt.org/manpages/virt-admin.html)、[v10 access manager 初始化](https://github.com/libvirt/libvirt/blob/v10.0.0/src/remote/remote_daemon.c#L391)

有效证明需把**管理员提供的启动配置、实际目标 socket/daemon、启动时间/版本/配置 hash**与专用 UID 的真实 API 正负例关联。需要证明拒绝来自 `org.libvirt.api.*` 检查，而不是连接就失败、客户端 readonly 限制、对象不存在或普通文件不可读。`pkcheck` 成功仅代表给定 subject/details 的 polkit 判断，不能证明 daemon 使用了该 driver；普通账号下全拒绝也不能证明之后给 manage 会安全。

在禁止改动现有全局 daemon/polkit 的当前约束下，若已有可信有效配置与 API 证据不足，应把这一项记为 **PENDING / 需要独立隔离验收环境或一次受限管理员部署窗口**。不能通过安装 manage-YES 规则来试探 ACL 是否存在。

## 4. 可落地的部署选择

| 选择 | 可接受前提 | 本轮能完成的工作 / 剩余条件 |
| --- | --- | --- |
| 专用隔离执行宿主或独立完整 OS 的验收宿主，使用自己的 system libvirt | 独立 VM 状态、配置、socket、polkit 和 QEMU；实际 KVM 可用；不影响现有宿主全局服务 | 当前硬编码 `qemu:///system` 可保留。在隔离 OS 内受控部署 ACL 与两个 UID，并执行真实 IPC/VM 门槛。这是现有模块假设下优先的资格路径；嵌套 KVM 不可用时不能用 TCG 冒充 KVM |
| 复用现有 system libvirt | 管理员证明当前有效 `access_drivers` 含 polkit；审查已有规则优先级/其他租户行为；在授权窗口安装仅匹配新服务账号的规则，并验收 | 当前只能收集允许读取的线索、生成离线产物。若需开启 driver 或重启全局 daemon，超出本轮范围，不执行 |
| 同宿主再起一个所谓“私有 root daemon” | 必须真正隔离配置、运行状态、pidfile、锁、helper daemon、cgroup 及存储，而非只换管理 socket | `virtqemud --config` **不能**重定向 QEMU driver 的 `qemu.conf`；默认 runtime 路径仍存在冲突。需独立 OS，或先设计并验收完整 namespace/状态隔离。不能把第二个 daemon 当作无风险捷径 |
| 每用户 `qemu:///session` / embed | 需另行修改受信任连接配置并重新验收整个后端 | 当前固定 `libvirt-qemu` UID、`/run/libvirt/qemu` PID 文件及 `machine.slice` 结构不能直接成立。不能用于完成当前 system 后端的专用身份门槛 |

system/session 的不同运行方式见 [libvirt daemon 文档](https://libvirt.org/daemons.html)。`virtqemud` 明确列出无法用命令行切换的 QEMU 配置位置及 root/session runtime 路径。[virtqemud 文件契约](https://www.libvirt.org/manpages/virtqemud.html#files)。独立 namespace 或 embed 不在本轮实现/验证范围；不因支持自定义 URI 就推定其具有 system 后端同等隔离、ACL 与恢复语义。

## 5. 服务身份、路径与权限

建议固定专用账号 `harniqo-broker`、`harniqo-supervisor`，各自独立主组，另建只含获准服务的 `harniqo-ipc` 补充组。不得使用动态 UID：catalog hash/owner、IPC peer 策略与持久状态绑定稳定数字身份；轮换 UID 需受控迁移和重新准入。账号创建、目录初始化及规则安装只由后续管理员在已授权隔离环境中执行，下面仅为经审阅的命令形态：

```sh
groupadd --system harniqo-ipc
useradd --system --user-group --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin harniqo-broker
useradd --system --user-group --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin harniqo-supervisor
```

命令前须拒绝已有同名但来源/数字 UID/GID 不符的账号；不静默接管。`useradd --system` 的 ID 范围受宿主配置决定，实际 UID/GID 要写入部署清单和 peer policy。无 `--password` 的账号默认锁定密码；还需确认无 SSH key、sudoers、额外认证通道及非预期 group membership。[Ubuntu noble useradd](https://manpages.ubuntu.com/manpages/noble/man8/useradd.8.html)

| 数据/目录 | 所属与实际许可 | Supervisor 权限 |
| --- | --- | --- |
| 已发布代码、启动器和部署配置 | 管理员拥有、服务账号不可写；不从可写 cwd/PYTHONPATH 导入 | 只读它需要的发行代码/配置 |
| Broker issuer key | Broker UID 的单链接普通文件，精确 `0600`、32 字节，独立 `0700` 父目录；服务 mount 中只读 | 目录不可遍历、文件不可读；不得放环境/命令行/IPC响应 |
| Broker journal/lock/catalog 状态 | Broker UID，`0700` 目录/`0600` 私有文件；listener 与 watcher 共用同一权威组状态 | 无访问；不能另建空状态目录绕过未决执行 |
| 不可变 catalog 配置 | 管理员发布、Broker 可读、Supervisor 不可写；真实路径/UUID/配置 hash 固定 | 只给 IPC 资源映射，不给密钥或构造模板的能力 |
| IPC 目录 | Broker UID、`harniqo-ipc` GID、`0750`；无额外 access/default ACL；socket `0660` | 以补充组遍历/连接，不能写目录或替换 socket/lock |
| images/channel 根 | Broker 拥有，无组/其他写权限；专用 QEMU UID 仅必要 traverse 与文件 ACL | 无访问，不共享 IPC 父目录 |
| raw/seed/channel 文件 | 完全沿用现有 role-specific Broker/QEMU/root owner 策略；seed QEMU `r--`、raw/socket `rw-`，seed 保留 Broker named read ACL | 无访问；不把 Supervisor 加入 QEMU 组 |
| Supervisor spool/配置/后续控制面凭据 | Supervisor 私有状态目录；精确 audience/scope 单独验收 | 仅本身份资源 |

这些权限必须满足 [现有 Broker 文件策略](../apps/runner/resource_broker/README.md#trusted-configuration)，不能改成共享 `0770` 私有目录来解决启动失败。启动器在创建 **新** IPC 目录时可设置其获准补充组；已有路径必须原样验证，不使用启动前通用 chown/chmod 修复。`RuntimeDirectory=` 的 owner/group 取服务的 User/Group，不能假定它自动创建具有不同共享 GID 的 IPC 目录；管理员或受信任的新目录初始化流程要处理这一点。

`SupplementaryGroups=harniqo-ipc` 可以给两个 unit 赋予共享组。它会**扩展** NSS 中已有补充组；空值仅清空 unit 内前面的配置，不会剥离账号已有 `libvirt`、`docker`、`kvm` 等组。因此验收必须检查实际进程 `Groups`，同时确认账号数据库，不能只看 unit 文本。[systemd 255 SupplementaryGroups](https://www.freedesktop.org/software/systemd/man/255/systemd.exec.html#SupplementaryGroups=)

## 6. 与现有模块兼容的 systemd 限制

这是待实际 unit 启动验证的策略，不把静态 key/value 当成已生效 sandbox。实际生成的两份 unit 使用系统 manager 的 `User=`/`Group=` 与 `Type=exec`；后者只核对 exec，业务准入另读 CLI ready，并未实现 `Type=notify` 或 watchdog 通知；不使用 setuid、socket activation、listen 后 fork 换进程或 PID/user namespace。不同权限身份之间的 real/effective IDs 与补充组有不同含义，IPC 仍只接受固定的 UID/主 GID pair。[Linux credentials](https://man7.org/linux/man-pages/man7/credentials.7.html)、[Linux socket credentials](https://man7.org/linux/man-pages/man7/unix.7.html)

| 设置/策略 | Broker listener 与 watcher | Supervisor |
| --- | --- | --- |
| `User` / `Group` / `SupplementaryGroups` | Broker 专用 UID/主 GID；只增加 IPC 组 | Supervisor 专用 UID/主 GID；只增加 IPC 组 |
| `NoNewPrivileges=yes`；`CapabilityBoundingSet=`；`AmbientCapabilities=` | 开启、清空 capabilities；libvirt 权限由 daemon 代行 | 同左；无 `CAP_SETUID/SETGID/SYS_ADMIN/SYS_PTRACE` |
| `ProtectSystem=strict` | 只对 Broker state 与自有 IPC 目录开必要 `ReadWritePaths`；catalog/key/images 只读；libvirt socket 与 PID 文件可访问 | 只给本 spool/state 写路径，IPC 目录保留不可写 |
| `PrivateDevices=yes` | 不直接访问 `/dev/kvm`；libvirt daemon/QEMU 在自身服务上下文处理 | 开启，无直接设备管理 |
| `ProtectKernelTunables=yes`、`ProtectKernelModules=yes`、`ProtectControlGroups=yes` | 保留 `/proc/sys/kernel/random/boot_id`、`/proc/$pid/{stat,cmdline,cgroup,task}` 与 cgroup 控制文件的必要只读访问；不设置 `Delegate=yes` | 开启，不能写宿主 cgroup |
| `ProtectProc=default`、`ProcSubset=all` | **必须保留**；跨 QEMU UID 的进程核对及 boot_id 会受隐藏/裁剪影响 | 可选 `ProtectProc=invisible`，实际功能与凭据证据单独确认 |
| `PrivateUsers=no` | 保留宿主 UID/owner 语义；否则 QEMU/peer UID 可能映射为 nobody | 同左，不能推定 namespace 与现有 IPC 身份协议兼容 |
| `RestrictAddressFamilies=AF_UNIX` | 当前本地生命周期服务可用；真实 NSS/virsh 进程需验证 | IPC 专项资格可用；生产 Supervisor 需另准入 AF_INET/AF_INET6 以连接控制面，不能宣称其 egress 已验收 |
| `ProtectHome=yes`、`PrivateTmp=yes` | 仅当发行代码、images 和状态迁至明确的 `/opt`/`var/lib`/`run` 受控路径后采用；不能把当前开发 home 路径隐藏再声称可运行 | 同样按实际发布路径设置 |
| `InaccessiblePaths=` | 隐藏 Docker/containerd 管理 socket、无关密钥/业务目录；保持实际 libvirt socket 可达 | 隐藏 Broker key/state/images 和全部已发现 libvirt RW/RO/admin、Docker/containerd socket；路径不存在的条目按设计显式处理 |

systemd 的 `ProtectSystem`/`ReadOnlyPaths` 限制文件写入，**不会阻止连接只读 mount 上的 AF_UNIX socket**；因此 Supervisor 的 libvirt socket 必须用 `InaccessiblePaths`/真实 ACL 拒绝，不能仅依赖 `ProtectSystem=strict`。无 libvirt 管理组也不保证没有默认 world-readable RO socket。[systemd 255 sandbox 边界](https://www.freedesktop.org/software/systemd/man/255/systemd.exec.html#Sandboxing)

`ProtectProc=invisible/noaccess/ptraceable` 会隐藏或限制其他 UID 进程；`ProcSubset=pid` 会裁剪非进程 `/proc` 内容。对目前读取 `libvirt-qemu` 进程、boot_id 的 Broker，不能为提高 security 分数启用这些限制，再靠授予 `CAP_SYS_PTRACE` 补救。[systemd 255 ProtectProc/ProcSubset](https://www.freedesktop.org/software/systemd/man/255/systemd.exec.html#ProtectProc=)

不直接把 `LoadCredential=` 的文件路径交给现有 `_read_private`：后者要求精确 `0600`、Broker owner 和私有父目录，而 systemd 提供只读 credential copy。若采用该机制，须明确修改受信任密钥装载接口并验证权限/生命周期；当前兼容路径是管理员准备的 Broker-owned `0600` 私有 key，再通过 unit mount 只读。环境变量不用于传密钥。[systemd credential 语义](https://www.freedesktop.org/software/systemd/man/255/systemd.exec.html#LoadCredential=)

listener 退出不意味着 QEMU 退出；VM 由 libvirt 管理，不能依赖 unit `KillMode` 完成 VM 清理。watcher 作为独立 Broker 身份的长期服务，启动与恢复均读取相同 catalog/journal；不向 Supervisor 暴露 `watch/reconcile/resource_limits`。停止意图与持久 receipt 继续由原模块产生。独立 watcher 被 Broker 锁阻塞的既有限制必须保留，不能把 `Restart=on-failure` 当作硬停止时限证明。[Broker watch 与恢复边界](../apps/runner/resource_broker/README.md)

## 7. 最小隔离资格与证据分层

### A. 现在可交付的离线产物

在仓库或任务目录生成部署 manifest、两个服务 unit、watcher unit、deny-by-default polkit 候选规则、路径/用户/组清单和只读 preflight。每个配置字段固定来源与 scope；拒绝未知字段、重复 UUID/名称/路径、UID 0、重用服务 UID 和不匹配 owner。不包含真实私钥，不生成改变现有全局 daemon 的安装步骤，也不执行命令式 root installer。静态规则测试须覆盖目标与外来身份、缺失/错配对象属性、只读默认回落及未知 action。

若 Linux 255 工具已可用，可在未安装的产物路径运行：

```sh
systemd-analyze verify /absolute/rendered/harniqo-broker.service /absolute/rendered/harniqo-broker-watch.service /absolute/rendered/harniqo-supervisor.service
```

先按仓库要求读取 `df`；这类检查无须创建编译缓存。需保存 systemd 版本、完整 stderr、源码与产物摘要。命令检查语法、依赖和可执行文件等，**不**创建账号、启动 sandbox、验证 socket ACL 或运行 VM。缺真实入口程序不能用 `/usr/bin/true` 替换后报告完整 unit 通过；可以分开记录语法检查与安装布局待验证。[Ubuntu noble systemd-analyze verify](https://manpages.ubuntu.com/manpages/noble/man1/systemd-analyze.1.html#systemd-analyze-verify-file)

### B. 两个真实 UID 的无 VM 验收

在独立验收 OS 中，管理员只执行明确审查后的新账号/新目录/unit 初始化；不动现有全局服务。以实际 User/Group 启动 listener、watcher、Supervisor，记录 PID、`/proc/PID/status` 的 real/effective/saved/fs UID/GID、Groups、capabilities、NoNewPrivs，以及 unit 的实际 sandbox。真实 UID 的 acceptance 必须覆盖：

- Supervisor 可连接预期 Broker，四操作到达原模块；错误 UID、错误主 GID、额外/错误组、伪装的 Broker socket、连接 FD 转交继续拒绝。
- Supervisor 无法读 key/catalog 私有材料/journal/raw/seed，无法创建、替换或删除 IPC socket、稳定 lock 及其父目录；Broker key 的内容不进入输出或日志。
- 在实际 Supervisor 服务上下文中，直接连接 libvirt RW、RO、admin 与 Docker/containerd socket 均失败；不是只测试 `virsh` 不在 PATH。
- Broker 能核对文件身份/seed ACL，能读实际系统 boot_id；未知 owner、过宽目录、symlink、额外 IPC access/default ACL 不被自动修复。
- 重启、异常退出、陈旧 endpoint、未知 bind 窗口保留现有精确 inode/ctime 规则；服务管理器不能在重启时自动删除用于诊断的 socket 后宣布恢复。

此阶段后端若仍为持久夹具，只能标记“专用 UID 的 IPC/DAC”，不能标记“最小 libvirt 或真实 VM 通过”。

### C. 有效 libvirt ACL 的真实 VM 经 IPC 验收

仅在隔离 system libvirt 已由管理员完成明确配置、正确 daemon 使用 polkit access driver 且规则已审查后执行。受信任的资格 issuer 生成短期 Assignment/Grant；**不能让 Supervisor 读取签名 key**。先准备 catalog 固定的测试资源和一个不同 UID/对象授权域的外来哨兵资源，记录所有原始状态，以整个真实 IPC 链执行：

- `ensure → observe → renew → revoke`，实际一个确定 UUID、KVM QEMU owner、固定磁盘/无 NIC/设备拓扑、限额控制文件和所有 thread membership 均通过现有观察器；停止 receipt 来自真实 libvirt absence 和持久状态。
- Broker 专用 UID 能 list 到 catalog 资源并读取其非 secure XML；外来 UUID/name、错配二者、外来 driver/网络/storage/secret/node-device 被拒绝。list 过滤不是“外来对象已不存在”；管理员侧独立证明哨兵仍存在/运行。
- Supervisor 的直接 libvirt API 调用被实际拒绝；其合法 IPC 仍可用。Broker 的 forbidden permission（secure XML、console/open-device、host power、domain delete/save/migrate 等）失败，拒绝不能仅来自 CLI readonly 模式。
- 新 XML 的非 catalog UUID/名称在创建前被拒绝，不能产生 QEMU；对已获准对象的同 permission 变体如 `domain:write` **不伪造拒绝保证**，由上述明确的受信任 Broker 边界说明其能力上界。
- listener/watcher 单独重启、期限停止、旧 envelope 重放、跨组织/Workspace 操作、未知 stop outcome、恢复单写者均走原持久语义。实际 QEMU 由 libvirt 创建的 socket/file chown 后 Broker 仍能校验种子与资源；不能以开发 UID 路径替代。
- 拒绝证据包含 daemon/API/polkit action 层次、固定 UUID、实际进程身份和核对结果；保存原始敏感日志在受控存储，公开仅脱敏索引。没有执行或权限无法核对的一项保持 PENDING。

外来写入负向测试只可针对管理员明确创建的隔离哨兵及测试 UUID，不以现有用户 VM 测试 destroy/attach/修改 XML。结束后先保存产物与日志，使用同一 Broker IPC 撤销自己的测试资源并独立核对，保留失败和不确定结果；不得清理非本任务域/磁盘。

## 8. 当前结论与下一步门槛

已确认可以实现的生产部署模型是：稳定普通权限 Supervisor，经双向内核凭据 IPC 调用独立受信任 Broker；Broker 在固定实例组内使用管理员准入的 libvirt permission 集合，独立 watcher 维持原有期限与恢复核对。完整生产能力仍取决于实际服务身份、有效 daemon ACL、路径与 process/cgroup 观察权限和真实 VM 的正负例，不能用研究、静态 rule 测试或同 UID Linux 进程测试代替。

在“不改变现有全局 daemon/polkit、不执行 root”的当前范围内，下一步可以完成代码、离线部署产物和普通权限 preflight；真实跨 UID/system-libvirt 资格需要独立完整 OS，或管理员明确提供已生效 ACL 的运行证据与受限部署窗口。未具备时保留 **PENDING**，并列出所缺的 daemon/账号/目录/权限证据；不给 manage 宽授权兜底，也不把 M0-05 勾选为已开发完成。

## 8. 实现与指定服务器实际验收

[服务入口](../apps/runner/resource_broker/SERVICE.md)将固定配置的语法、来源、SHA、实际身份与文件准入收在同一 Interface。初始化显式且拒绝已有非空状态；正常启动使用 `initialize_state=False`，不能创建丢失的 lock/anchor/journal，也不能把损坏状态当作新组。原 Broker 的认证恢复及 ownership 核对继续生效。独立 `serve` 与 `watch` 分开监督，ready 的 operational/degraded 只说明进程准入状态，不能证明 VM 健康。

[指定服务器组件证据](../docs/operations/broker-service-components-2026-11-01.json)189项全部通过：70核心、32IPC、20服务、26部署、41操作者，0失败/错误/跳过。首次186项有1个 Linux-only 夹具错误：peer 工作区与复用核心签名不一致；只修正测试输入，未放宽 ACL。独立审查确认旧 CLI 在 signal handler 调用 Event.set 可重入 main wait 的非重入锁；服务器旧实现的真实 SIGTERM/SIGINT 子进程回归均超时，2个失败子进程均精确回收。修正为 handler 仅布尔置位和锁外有限轮询，最终20项服务测试通过。首份错误报告、RED、旧通过及新通过各自保留，不覆盖历史。

离线 renderer 使用实际服务纯语法解析，已生成并保留9份产物、逐字节/摘要/权限与 manifest 核对；实际 Node24.22.1通过101个正反例，两份真实 Type=exec unit通过 systemd-analyze verify。plan身份和catalog为合成输入；active规则仅拒绝，委派候选不以.rules加载，激活状态仍BLOCKED。41条普通权限只读宿主盘点确认既有3个domain和专用服务账号尚不存在；磁盘配置投影不能证明运行中daemon的细粒度ACL。未创建账号、加入管理组、安装unit或改变daemon/polkit；不能用本次开发UID结果抵扣专用生产身份的 real_vm_ipc_pending。

[实际 KVM / 服务 IPC 证据](../docs/operations/broker-service-kvm-2026-11-01.json)的26项必需宿主门槛、额外console归档检查及初始两boot共22项Guest检查全部通过，错误/清理0。正式CLI初始化、watch和serve就绪；四操作经真实IPC进入正式Broker/libvirt。listener被SIGKILL后watch与两个原QEMU身份保持，重启后从原journal继续观察；SIGTERM关闭准入而B继续运行。原launcher/watch故障、A租约期限停止、B在watch死亡后仍活动、可信宿主重建后的期限停止与旧授权拒绝链继续验收。限额诊断与watch死亡后的显式恢复为可信本地操作，报告单列，不宣称所有动作走IPC。

共2个资源UUID、3次实际VM启动。A初次期限停止后以新签Assignment、generation3/recovery2和新attempt重启同UUID，核对不同QEMU进程身份、固定v2限额与宿主在场，再IPC revoke(cancelled)；必须取得更新的libvirt_destroy_then_absent和宿主absence，B/watch保持活动。第二次A有独立完整有界console，未计作第二轮Guest健康通过。独立收集器另验11份源码快照、配置原字节/安全祖先、签名Assignment/anchor/journal及XML、三份console、ready/退出、限额与压力挑战；只读重查既有对象和base原字节，本组raw/seed与临时通道已精确回收。旧96GiB诊断材料不在本轮清理范围；不声称已为旧层追加逐字节前后对照。

本轮无模型、TCP/NIC或管理员/全局服务修改，不依赖测试模型凭据。M0仍4/8、M0-05仍IN_PROGRESS；专用UID/有效ACL、Supervisor生产集成、完整Egress与控制面权威授权/预算/提交等继续按各自门槛推进。历史E62/E64/E65源码绑定保留，不改写成当前实现的证明。实际GitHub CI在发布后单列登记。
