# 专用服务身份的创建与验收

身份创建与 libvirt 授权分开执行。`provision_broker_identities.py` 负责两名固定服务用户和三个组；
`qualify_broker_identities.py` 用实际 systemd 进程验证不同 UID、文件权限与生产 IPC。
两者都不能激活当前 [BLOCKED 部署包](README.md)，不会修改现有 libvirt、polkit、sudoers 或 SSH 配置。

身份 plan 是最多 32 KiB 的 JSON，只接受 `schema_version=1`、`broker_account`、
`supervisor_account`、`ipc_group`。账号项为 `name/uid/gid`，IPC 组为 `name/gid`；
名称、两个 UID 和三个 GID 必须满足角色间的独立要求。数字须经目标宿主 NSS 检查，
不能直接把文档示例当作预留号段。未知/重复字段和保留管理身份均拒绝。

先以普通账号做新身份预检：

```sh
python3 -I -B scripts/deploy/provision_broker_identities.py plan \
  --plan "$HARNIQO_IDENTITY_PLAN" --sha256 "$HARNIQO_IDENTITY_PLAN_SHA256"
```

正式 `apply` 与完整 `check` 必须由管理员从 root 拥有、服务账号不可写的发行目录调用。
使用隔离 Python 模式，避免在模块自己的检查前加载不可信 `PYTHONPATH` 或用户 site 包。
plan 文件及全部祖先须由 root 控制，不允许 symlink 或组/其他写权限。

```sh
python3 -I -B "$HARNIQO_RELEASE/scripts/deploy/provision_broker_identities.py" apply \
  --plan "$HARNIQO_IDENTITY_PLAN" --sha256 "$HARNIQO_IDENTITY_PLAN_SHA256"
python3 -I -B "$HARNIQO_RELEASE/scripts/deploy/provision_broker_identities.py" check \
  --plan "$HARNIQO_IDENTITY_PLAN" --sha256 "$HARNIQO_IDENTITY_PLAN_SHA256"
```

工具只通过固定的 `groupadd`、`useradd` 创建新身份。账号锁定密码、使用 nologin、
不创建 home，仅拥有独立主组与 IPC 补充组；不加入 libvirt、Docker 等管理组。
同名、数字身份、外来主组引用或预先存在的成员关系冲突均拒绝，不接管已有账号。
这些检查不等于审查宿主所有认证、sudoers 或外部身份策略。

`/var/lib/harniqo/identities` 保存 root 私有锁和创建 receipt。每步先持久化 intent，
再执行命令，核对实际 NSS 后记录完成。完成的同一计划可重复核对；一旦命令效果未知，
后续执行保持拒绝，等待管理员核对原始日志及账号数据库并明确恢复。工具不自动重放、
删除账号、修补漂移或把“当前查询不到账号”当作没有产生任何效果。

管理员已独立核对某个已创建步骤时，可使用显式确认入口；此命令不创建、修改或删除 NSS
身份。它要求原始 receipt 字节摘要、指定的唯一有序未知步骤和完整后置检查，先排他保存
原件及恢复审计，再确认该步骤。过期摘要、漂移或重复确认均拒绝；普通 apply/check
不会调用此恢复入口。随后 apply 只继续尚未尝试的步骤，check 才能确认全部完成。

```sh
python3 -I -B "$HARNIQO_RELEASE/scripts/deploy/provision_broker_identities.py" confirm-created \
  --plan "$HARNIQO_IDENTITY_PLAN" --sha256 "$HARNIQO_IDENTITY_PLAN_SHA256" \
  --receipt-sha256 "$HARNIQO_PRIOR_RECEIPT_SHA256" --step "$HARNIQO_CONFIRM_CREATED_STEP"
```

主组由 passwd 单独严格核对；initgroups 查询可能只列补充组。工具合并已核对的主 GID
后要求恰为主组与 IPC 组，仍拒绝缺失 IPC 或任何额外组，不能通过这个归一化扩大权限。

资格验收要求已经创建的两个专用账号、root 控制的发行代码及配置、全新的私有 key/state、
Broker:IPC `0750` 目录、Broker 私有 channel 根，以及 `images/` 下新建的 raw/seed。
报告路径的父目录须 root-owned `0700`，报告排他创建 `0600`。原件含真实身份与路径，
只保存至私有存储。

```sh
python3 -I -B "$HARNIQO_RELEASE/scripts/qualification/qualify_broker_identities.py" \
  --config "$HARNIQO_IDENTITY_CONFIG" --sha256 "$HARNIQO_IDENTITY_CONFIG_SHA256" \
  --report "$HARNIQO_IDENTITY_REPORT"
```

资格工具以普通 Broker UID 启动正式 CLI 的 initialize、watch、serve，以独立 peer UID
执行 IPC。模板只是不可启动的身份 canary：raw 仅用于满足固定容量契约，可以是明确登记的
稀疏文件；不发有效 Assignment、不启动 VM。空 journal 的重新打开不能代替有运行资源时的恢复验收。

DAC 探针保留私有路径可见，并允许 IPC 所在 mount 写入，以实际 `0700/0750` 拒绝读写；
受限探针再验证 mount 屏蔽、能力清空、NoNewPrivileges、网络 namespace 和 AF_UNIX 限制。
只读 mount 不能代替 UNIX socket 访问控制；PrivateNetwork 隔离 abstract UNIX socket，
文件路径型 IPC 仍可跨这些网络 namespace 工作。[systemd 255 执行环境](https://www.freedesktop.org/software/systemd/man/255/systemd.exec.html)

结果只证明所测服务进程上下文和明确列出的文件、socket、IPC 策略。它不证明整个 UID 在
namespace 外没有管理权限，也不交付生产 Supervisor 常驻程序。libvirt 的有效细粒度 ACL、
专用 UID 下实际 VM 生命周期和正式服务激活仍须分别验收；配置文件或 JS 规则测试不能证明
daemon 已加载 access driver。[libvirt ACL](https://libvirt.org/acl.html)

2026-11-02指定服务器的两个稳定账号、三个组、显式恢复与10个实际unit/172项探针已通过
独立核验；64项组件回归另行通过。见[身份原生证据](../../docs/operations/broker-identities-native-2026-11-02.json)
与[组件证据](../../docs/operations/broker-identities-components-2026-11-02.json)。生产激活仍BLOCKED，
专用UID下的真实VM/libvirt有效授权及生产Supervisor尚未验收。
