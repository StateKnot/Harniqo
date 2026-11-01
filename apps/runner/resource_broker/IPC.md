# Supervisor 与 Resource Broker 本地 Interface

本 Module 为 Linux 上的 Supervisor 提供四个获准操作：`ensure`、`renew`、`observe`、`revoke`。现有 Resource Broker 继续负责签名、固定模板、工作区单写者、租约、持久停止事实及实际资源核对。监听器不接收镜像目录、XML、命令、密钥、PID、cgroup 路径或任意配额，不公开全资源枚举、`reconcile`、`watch` 或宿主诊断 `resource_limits`。

这是 Runner 的宿主本地 Interface，不是桌面或 Guest 的入口，也不是平台 Assignment/ExecutionGrant。控制面登记、远端授权与全局 fencing 继续单独实现和验收。

## 配置及调用

可信启动代码先构造 `ResourceBroker`，再构造 `BrokerServer`；IPC 目录与 Broker 的私有状态/密钥目录分开。`ResourceAccess` 固定资源 UUID、组织、工作区和允许的操作；`PeerPolicy` 固定调用进程实际主 UID/GID 及其资源列表。`ensure` 和 `renew` 的签名 envelope 还必须匹配该组织和工作区，首次使用资源也不能越过这个限制。查询与撤销只使用已准入的 UUID，不接受调用者重新指定查询归属。

```python
from resource_broker.ipc import (
    BrokerClient, BrokerServer, PeerPolicy, ResourceAccess,
)

access = ResourceAccess(resource_id, organization_id, workspace_id,
                        frozenset({'ensure', 'renew', 'observe', 'revoke'}))
policy = PeerPolicy(supervisor_uid, supervisor_primary_gid, (access,))
with BrokerServer(directory=ipc_directory, broker=broker, peers=(policy,)) as server:
    server.serve(stop_event)

# 在独立 Supervisor 进程中；受信任配置给出预期 Broker UID/GID。
client = BrokerClient(directory=ipc_directory,
                      server_uid=broker_uid, server_gid=broker_primary_gid)
observation = client.ensure(signed_local_assignment)
```

`observe` 会进行现有 Broker 的实际核对；过期、限额不可验证等情况可能触发停止。它不是无副作用的数据库查询。撤销原因只接受本 Interface 明确列出的有限值，不能由调用者写入任意错误或日志内容。

## 内核身份及权限

通信使用文件系统中的 `AF_UNIX/SOCK_SEQPACKET`，每个连接只处理一份完整请求和一份响应。两端在通信前启用 `SO_PASSCRED`，同时检查 `SO_PEERCRED` 与每个 packet 唯一的 `SCM_CREDENTIALS`，核对 PID/UID/GID。已连接 FD 被其他进程继承或转交后，新的发送 PID 不能沿用原连接授权。客户端也核对真实 Broker 身份，不只依赖 socket 文件名。

正常服务必须使用相等的 real/effective UID/GID；监听、接收及响应来自同一 Broker 进程，连接与发送来自同一 Supervisor 进程。不支持监听后 fork 并由子进程响应，也不把 socket activation、PID namespace 或 setuid 程序推定为已兼容。

IPC 目录必须预先创建，由 Broker UID 拥有，采用 `0700` 或 `0750`，禁止组/其他用户写入。跨 UID 部署使用 Broker 拥有、Supervisor 所在组可遍历的目录及 `0660` socket；Broker 状态与密钥仍保持独立 `0700`。目录、稳定 lock 和 socket 均核对类型、权限及 inode，不跟随符号链接，不通过 chmod 放宽已有目录。本版本只采用 mode/group 授权，拒绝额外 POSIX access/default ACL，避免目录默认 ACL 绕过 bind 时的 umask。目录组必须是 Broker 能设置到自己 socket 上的获准组；调用者的 UID/GID 策略仍独立检查。

同一操作系统 UID 下的进程拥有共同的宿主权限。本 Module 不声称能抵抗恶意同 UID 管理进程修改代码、密钥、目录或状态；部署时必须另行配置和验收专用服务 UID、libvirt/polkit 授权及 Guest 不可达策略。开发账号下的真实进程测试只能证明实际内核凭据核对和策略拒绝，不能冒充专用 UID 的 DAC 隔离验收。

请求和响应收到文件描述符或其他未知 ancillary data 时均拒绝，先关闭所有已交付 FD。使用 `MSG_CMSG_CLOEXEC`，检查 `MSG_TRUNC` 与 `MSG_CTRUNC`；超长、截断、重复 JSON 成员、非法标量、未知字段、未知操作、越权资源均不能调用 Broker。

## 有限资源与结果语义

请求上限 16384 字节，响应上限 8192 字节；不拼接被截断的 packet。策略最多 16 个 peer、合计最多 64 个资源。监听器顺序处理请求，使用有限 backlog 和读写期限，不为连接建立无限线程。请求携带本机 `CLOCK_BOOTTIME` 的派发截止时间；最长准入窗口为 5 秒，调用 Broker 前再次检查，排队过久的请求不能继续派发。`close` 与最终准入检查同步：已经接受连接但尚未准入的请求会拒绝，已准入的操作允许完成，生命周期锁保持到处理退出。

客户端默认总等待 30 秒，上限 60 秒；服务端 socket 读写分别限 1 秒。它们约束传输及排队，不会安全中断已经进入 Broker 的持久化或 libvirt 操作。现有文件锁、文件系统或宿主调用阻塞时，需要外部服务监督与恢复；本 Interface 不建立新的硬停止时延保证，独立 watcher 也不能绕过另一个持有 Broker 锁的进程。

`IPCError.outcome` 区分：

- `not_dispatched`：本次调用在已确认的传输、身份、格式或策略拒绝处结束，没有进入 Broker。
- `unconfirmed`：请求可能已进入 Broker，或 Broker 返回错误；需要按相同资源核对实际状态。Broker 错误也可能发生在持久 intent 或停止之后，不能被标记为没有副作用。

客户端不自动重发。`request_id` 只关联响应，不是持久幂等键。`ensure`、`renew`、`revoke` 仍遵守现有 Broker 的签名、代次和持久化规则；不把续约重放拒绝改成成功，不创造第二个 UUID，也不由断线推断执行已经停止。响应只公开受限 Observation 字段和固定错误分类，不回显异常文本、请求 envelope、宿主路径、发行密钥或原始限额诊断。

## 监听器生命周期

独立的稳定 listener lock 在监听器整个生命周期保持排他锁，不能使用现有 Broker 每次操作的锁代替。锁文件保留；运行中在接收和派发前核对其身份。只有取得该锁、已记录的 socket dev/inode/ctime_ns 与当前路径一致、且确认原 socket 不再监听时，才允许回收旧 endpoint。ctime_ns 在最终权限设置后记录，用于拒绝立即复用相同 inode 的新 socket；正常关闭核对删除后仍在锁内清除旧记录。活动、外来、被替换或元数据不明的路径保持原样并拒绝启动。关闭只删除本监听器自己核对过的 socket 身份。

如果进程在首次 bind 创建 socket 后、持久保存身份前退出，身份不明的 socket 会保留并拒绝自动恢复。操作者须先核对该目录的独占归属、原服务已退出、实际 socket 无监听及文件身份，再按部署恢复流程处理；不提供“启动失败就 unlink”的兜底行为。正常就绪后的进程退出与重启另有实际进程验收，不由此推导任意断电恢复。

## 验证范围

运行代码、Linux 进程验收及实际 CI 结果由 [M0 证据索引](../../../docs/planning/M0-RESULTS.md) 分别记录。纯组件、真实内核 IPC、确定性持久后端夹具和真实 VM 是不同证据；本地通信通过不意味着专用服务身份、实际 VM 经 IPC 启停、完整生产 Egress、控制面授权/提交或整个 M0-05 已验收。
