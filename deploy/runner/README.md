# Broker 专用服务部署包

`scripts/deploy/prepare_broker_service.py` 将受控 plan 与同一份运行配置生成可审阅的离线部署包。仅使用 Python 标准库；不连接宿主应用、libvirt、systemd 或网络，不读取 issuer key，不创建账号、不安装文件、不启动服务、不调用 sudo。CLI/API 均拒绝 root 或 real/effective 身份不一致的执行。运行配置语法由实际服务的纯函数 `service.parse_configuration` 共同校验。

**当前包的激活状态始终为 `BLOCKED`。** v1 仅支持 `libvirt_acl_attestation={"status":"unverified"}`；不接受调用方自填的 `verified`、PID、签名或探测成功布尔值来证明 daemon 授权。包同时提供有效的 bootstrap 拒绝规则和完整、精确范围的委派候选；候选文件以 `.candidate` 结尾，不是 polkit 可加载的 `.rules`，没有安装路径。管理员完成实际 daemon/ACL/身份资格后才能审阅替换规则；没有 CLI 激活 override 或自动 apply。

原理和证据门槛见 [服务身份研究](../../research/m0-broker-service-identities.md)、[Broker 文件/目录策略](../../apps/runner/resource_broker/README.md)、[IPC](../../apps/runner/resource_broker/IPC.md)。文档和静态规则测试不能证明生产身份或 VM 经 IPC 已验收。

## 准备输入

先准备与 `apps.runner.resource_broker.service` 相同语法的完整配置 JSON，包含固定 broker UID/GID、各目录、key **路径引用**、固定 Template catalog、租约策略和唯一 Supervisor peer。使用实际发行配置的原始字节摘要；重新排版也会改变 SHA-256。不能把开发账号持有的 journal/磁盘 chown 后当作新专用身份的已准入 catalog。

plan 必须是无重复成员、无额外字段的 UTF-8 JSON，最多 32 KiB。配置最多 64 KiB。输入为 root/当前操作者拥有、单链接普通文件，禁止组/其他可写；所有输入/输出路径均为规范绝对路径，任何 symlink 路径组件都被拒绝。`configuration_path` 指向读取该包的本地输入，不是安装后配置地址。

```json
{
  "schema_version": 1,
  "configuration_path": "/absolute/private/resource-broker.json",
  "configuration_sha256": "<该文件原始字节的64位小写SHA256>",
  "release_root": "/opt/harniqo/releases/m005-<不可变发行标识>",
  "broker_account": {"name": "harniqo-broker", "uid": 24001, "gid": 24001},
  "supervisor_account": {"name": "harniqo-supervisor", "uid": 24002, "gid": 24002},
  "ipc_group": {"name": "harniqo-ipc", "gid": 24003},
  "libvirt_acl_attestation": {"status": "unverified"}
}
```

示例数字不是预留号段或宿主事实。正式应用前必须盘点 NSS 与本地账号、同名/同 UID/GID、补充组和关联文件；拒绝碰撞及接管现有账号。Broker/Supervisor 的 UID、主 GID 和 IPC GID 必须分别非零、两个 UID 不同、三个 GID 不同；账号名和组名不同，禁止 root、libvirt、Docker 等保留身份。sysusers 不是碰撞检查器，也不覆盖 LDAP/NIS。[systemd 255 sysusers 契约](https://www.freedesktop.org/software/systemd/man/255/sysusers.d.html)

部署布局有意限定：

- 发布代码位于 `/opt/harniqo/releases/<安全发行标识>`，实际模块在其 `apps/runner/resource_broker/service.py`。全部祖先及发行文件由管理员控制、服务账号不可写；配置安装到 `/etc/harniqo/resource-broker.json`，root:Broker 主 GID、`0640`，父目录 root-owned、无组/其他写。
- state 与 key 父目录分别是 `/var/lib/harniqo` 下不同的直接子目录；两者均 Broker-owned `0700`。key 由管理员另行提供为 Broker-owned、单链接 `0600` 的 32 字节文件，renderer 不创建或读取它。
- IPC 与 channel 根分别是 `/run/harniqo` 下不同的直接子目录。IPC 为 Broker:IPC 组 `0750`，无额外 access/default ACL；socket 由实际 Broker 创建为 `0660`。channel 为 Broker:Broker `0710`，其 QEMU traverse ACL、已绑定的报告/console socket 由独立准入准备。
- `images_root` 保留配置指定的绝对物理存储位置，包括既有 `design/images`。包不创建/chown images、不修改 home 祖先 ACL、不复制 VM 磁盘、不新增 bind mount。catalog 中 raw/seed 必须位于该根下并满足现有 inode、owner、容量、ACL 与种子摘要策略；QEMU 文件/祖先遍历须实际验证。私有/运行根彼此及 images 根不能重叠。

目录格式限制不覆盖实际磁盘内容准入；运行服务仍核对文件身份和权限。`/run` 重建、通道 socket 替换、代码/配置/UID 变化均需依原模块的恢复与 catalog 交接规则处理，不能通过初始化空状态绕过未决 VM。

## 生成并审阅

按仓库要求先用 `df` 核对空间。输出目录须**已存在、为空、当前操作者拥有且精确 `0700`**；renderer 不创建、放宽或清理它。Linux 支持相应 xattr API 时还拒绝额外 POSIX access/default ACL。每个产物以排他创建和 `0600` 写入，fsync 后返回；不得在共享 ACL 目录发布私有配置。验证失败在首次写入前结束；写入阶段 I/O 故障保留部分产物作为诊断，不自动删除或宣布成功。

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -B scripts/deploy/prepare_broker_service.py \
  --plan /absolute/private/plan.json \
  --output /absolute/private/empty-bundle
```

API：`prepare_bundle(plan_path, output_dir)`，返回与 `manifest.json` 相同的字典。CLI 退出码 0 表示完整**离线生成成功**，摘要同时明确 `activation_authorized=false`、`status=BLOCKED`；不代表可激活。失败输出固定分类，不回显输入、密钥路径或 native traceback。

| 产物 | 审阅/后续安装语义 |
| --- | --- |
| `manifest.json` | 原始 plan/config SHA-256、全部产物摘要/目标 mode/owner、身份、阻塞条件、激活/回滚前置条件；不安装到系统 |
| `resource-broker.json` | 原始配置字节，安装为固定 `/etc/harniqo/resource-broker.json`，不重新编码 |
| `harniqo-sysusers.conf` | 两个固定 UID/主 GID、一个 IPC GID，nologin、无 home；仅加入 IPC 组 |
| `harniqo-tmpfiles.conf` | 受限 state/key/IPC/channel 目录；无 image 项，无 key 文件、ACL 变更、age、删除规则 |
| `harniqo-broker.service` | `Type=exec`，专用 Broker UID，直接运行同一模块 `serve`；Requires/After watcher |
| `harniqo-broker-watch.service` | 相同受信任 Broker UID，运行 `watch`；Before listener，无反向 Requires/PartOf/BindsTo |
| `00-harniqo-libvirt.rules` | 对 Broker/Supervisor 的全部 libvirt action 返回 NO，其他账号保持未处理 |
| `00-harniqo-libvirt.rules.candidate` | 非激活候选；Broker 仅 QEMU `connect.getattr/search-domains` 和 catalog 精确 UUID **且**名称的 `domain.getattr/read/write/start/stop`；Supervisor 全部拒绝，其余 libvirt 动作拒绝 |
| `required-libvirt-permissions.json` | 同一冻结对象/permission 集合及能力边界，供真实 API 正负例核对 |

候选必须放行 `org.libvirt.unix.manage` 才能建立需要的 RW 连接；这行在未验证 `access_drivers=["polkit"]` 时将造成宽管理授权，所以它**只出现在无安装目标的 candidate**。`auth_unix_rw="polkit"` 与 API ACL 不是一回事。规则看不到完整 XML/hash/磁盘/拓扑，`domain:write` 也被其他 API 复用；固定 XML 的信任边界仍是 Broker。[libvirt ACL](https://libvirt.org/acl.html)、[v10 API permissions](https://github.com/libvirt/libvirt/blob/v10.0.0/src/remote/remote_protocol.x)

没有真实 Supervisor 常驻程序时不生成假服务；该集成显式 PENDING。后续 Supervisor unit 必须以其专用身份运行，隔离 issuer key/state/images 和所有 libvirt RW/RO/admin、Docker/containerd socket；仅依赖只读 mount 或未加入 libvirt 组不足以拒绝直接 socket 连接。

## 激活前的真实门槛

仅在独立隔离执行宿主，或另行明确授权的管理员窗口执行安装。每一步都保留原文件身份/hash、现有 domain 清单及回滚证据；这个工具不执行下面的管理员动作。

1. 盘点账号/数字 ID/全部补充组，核对目标路径不存在或已满足确切 owner/mode/ACL。`tmpfiles d` 会调整现有目录的 owner/mode，不能当作“不修改已有路径”的命令盲跑；禁止用它修复陌生目录。真实 daemon/socket/规则顺序与所有其他管理入口必须独立盘点。
2. 关联当前 daemon PID/启动时间、实际加载配置路径/摘要、target socket、版本与真实 API 正负例，证明 polkit access driver 正在生效。普通 `virsh version/capabilities`、编译了 polkit、读取磁盘 conf、单独 `pkcheck` 或 rule 函数测试都不构成该证明。不能为了试探而先安装 candidate。
3. 审阅独立普通权限单位、实际安装布局与完整 scoped candidate，核对没有更早规则越权放行。仅在前述证据齐备后，由管理员采用受控变更替换 bootstrap deny。不要改现有全局 daemon、UFW、QEMU 配置或给两个服务加 libvirt/docker/kvm 管理组来降级绕过。
4. 发布 root-owned 不可写 release/config，准备新 catalog 的文件、QEMU ACL、已绑定 channel/console socket、私有 issuer key 与目录；运行既有服务 CLI 的 `initialize` 仅限明确首次初始化。正常 unit 不执行 initialize、不创建空 journal。
5. 在实际安装布局执行 `systemd-analyze verify` 检查两个真实 unit。它不启动服务、不验证 UID/ACL/VM；缺可执行入口不能替换成 `/usr/bin/true` 后宣称通过。[systemd verify](https://www.freedesktop.org/software/systemd/man/255/systemd-analyze.html#systemd-analyze%20verify%20FILE%E2%80%A6)
6. 用真实两个 UID 验收 `/proc/PID/status` 的 real/effective IDs、全部 Groups/capabilities、NoNewPrivs、实际 sandbox、Supervisor 文件/管理入口拒绝和 IPC 正负例。再经真实 IPC 执行 VM ensure/observe/renew/revoke、期限停止、进程重启、旧 envelope/外来域拒绝和持久 receipt。issuer 运行在受信任侧，不让 Supervisor 读 key。所有未执行项保持 PENDING。

units 使用 `ProtectControlGroups=yes` 而不隐藏宿主 `/proc`：Broker 需要读取另一 UID 的 QEMU cmdline/task/cgroup 和 boot_id。capabilities 清空、NoNewPrivileges 开启、PrivateUsers 关闭，AF_UNIX 限制只作用于本地 Broker 服务；production Supervisor 出站网络另行验收。`ProtectHome=no` 保持 home-backed images 可见；既有 DAC 和只读路径继续约束访问。两个 unit 均不创建/清理 RuntimeDirectory、无 socket activation、无进度看门狗。

`Type=exec` 仅证明 exec 成功；不是应用 ready、VM 健康或硬停止保证。监听器故障不会反向停止 watcher；watcher 与 listener 共用原 Broker 锁，锁阻塞等既有限制没有消失。QEMU 位于 libvirt 管理上下文，unit 的 KillMode 不能替代 VM 停止事实。[systemd service Type](https://www.freedesktop.org/software/systemd/man/255/systemd.service.html#Type=)

## 回滚与验收范围

先停止新任务准入，对本次已准入测试 UUID 经 IPC 撤销并独立核对实际停止和 receipt；未决时保持 watcher 和所有 key/catalog/state/images/socket/lock 诊断材料。确认排空后才先停 listener、再停 watcher，恢复有基线身份/hash 的本任务 unit/规则；不删除旧文件、复用 UID、重置状态或按名称前缀批量清理域。规则替换与账号撤销均需要独立管理员审查，工具没有 rollback/apply 命令。

组件检查：

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -B scripts/qualification/test_broker_deployment.py -v
```

包含真实文件安全失败路径、配置摘要/身份约束、格式边界、产物权限/哈希、模板作用域与实际生成 JS rule 的正负例（本机存在 Node 时运行）。Node VM 中的规则计算不代表真实 polkit/daemon 验收；没有 systemd 255 环境时不能把文本检查记为 `systemd-analyze verify`，也不能把离线 bundle 标为实际部署完成。
