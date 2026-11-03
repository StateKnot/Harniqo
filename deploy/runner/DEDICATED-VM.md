# 专用身份 VM/IPC 资格

本单元把已实现的 Broker CLI、独立 watch、四操作 SDK 与固定 libvirt 模板放到真实的不同服务身份中验证。
它不交付生产 Supervisor，也不自动激活[离线服务包](README.md)。原 [bootstrap ACL](LIBVIRT-ACL.md) 的证据不能代替固定对象写权限或真实 VM 验收。

## 执行身份与输入

受信任管理员先冻结代码发行目录、普通 Broker/Supervisor 账号、root 配置、独占 UUID/name、只读 base 摘要、新 raw/seed 的物理身份和权限、key/state、IPC 与通道目录。启动器必须在执行前后核对全部源文件摘要，确认发行目录及祖先为 root 控制且组/其他不可写；worker 自报的源码身份不能替代这一检查。
VM 镜像必须留在指定项目的 `images/`；新 raw 固定 8 GiB 并实际预留，不接管旧诊断层或身份阶段不可启动的 canary。
启动前分别以 Broker 和 QEMU 的真实身份核对镜像树各级目录的遍历权限，不能用 QEMU 的成功代替 Broker。若受控的 `instances` 父目录仅缺 Broker 遍历权限，只可在预先记录的精确目录上增加 named-user execute，保留原 mask 与其他 entry；先持久保存目录身份和原 ACL，停止所有写者后按身份及完整 ACL 比较恢复。不得增加目录读取/写入权限或递归放宽镜像树。
issuer 留在受信任侧，只给 peer 已签名的 Assignment/Grant；Supervisor 不读取 key、state、images、libvirt 或管理 socket。

`broker_peer.py --input ABS --sha256 SHA --report ABS` 是有限资格调用者。输入 root 拥有、0640、组为 Supervisor 主组；所有祖先 root 控制且不可由组/其他身份写入。
报告目录为 Supervisor 私有 0700，文件排他 0600，原件保留。输入顶层字段为：

- `schema_version: 1`、`peer: {uid,gid,ipc_gid}`、`broker: {uid,gid,ipc_directory}`。
- `resource: {resource_id,organization_id,workspace_id,template_id,configuration_sha256}`；摘要是固定 Template 的摘要。
- `timeout_seconds: 1..60`、`operations` 最多八项：ensure/renew 带完整已签 envelope，observe 无额外参数，revoke 带明确 cause。

每个 envelope 都须匹配固定 resource scope。真实 UID/GID/groups、五类 capabilities=0 与 NNP=1 必须符合输入；整个 phase 有限期限，SDK 调用使用剩余时间。
任何错误停止后续操作，保留先前响应及 IPC outcome，不重试未知结果。`PASSED` 仅表示收到有效响应；管理员仍须分别检查 running、停止凭据、实际 QEMU absence 与持续不可复活。
该程序不签名、不读取 key、不构造 ResourceBroker；它的输入不接受 XML、宿主命令或任意资源路径。

## Guest 通道与资源观察

`broker_vm_fixture.py --plan ABS --sha256 SHA` 在真实 Broker 身份中持有一个 Template 的 report/console socket，准备精确 QEMU 文件 ACL，并使用现有 BoundedConsole 实际预留 2 MiB。
plan 包含 `schema_version:1`、`configuration_path`、`configuration_sha256`、`nonce`（32位小写hex）、`report_directory`、`timeout_seconds`（30..300）。plan 同样要求 root0640/组为 Broker；报告根为 Broker0700。
它不读 issuer key，不创建、启动、停止 VM，不新增网络接口。

程序发出 `fixture_ready`（绑定实际模板摘要）、`guest_report`、`limits_sample` 和通道关闭事件；有界保存原始 Guest 帧，包含验证失败的帧。
Guest 只提交新 nonce、实际 boot ID、uid 与固定协议名；单帧最多16 KiB，重复/跨资源报告失败。console 有界落盘，停止时继续有限排空，缺少 EOF 明确失败，不能用关闭描述符代替完整归档。成功或失败均保留阶段、错误分类、原始帧与 console 收据；`fixture-result.json` 及 unit 正常退出还须由管理员交叉核验。
资源样本由 Broker UID 调用正式 `resources.read_limits`，核对真实 QEMU PID/start/UUID、所有线程与 CPU/内存/swap cgroup；root 的读取成功不能替代它。

## 受控运行与清理

先证明实际 driver、规则/daemon 代次和已有业务基线未漂移；确认没有遗留服务身份进程或未决写者，才为本次精确 UUID **且** name 安装窄授权。
Broker 只得到必要 connect 和固定 domain 权限，Supervisor 全部 libvirt action 继续拒绝。临时 RW socket ACL 只准入 Broker，原件及 inode 先保存。
这些临时操作不称作持久 socket 授权；后续持久启动准入的顺序和每代证明独立交付。

fixture 绑定文件后才计算 Template 摘要并签发短期授权。新状态显式 initialize，然后独立 watch、serve，peer 经真实 SDK 执行 ensure/observe/renew/revoke。
watch 的运行时限必须覆盖全部租约和停止余量，不能沿用较短的身份 canary 默认值。正常 daemon、socket、规则或源变更必须先排空再重验。
额外只读 ACL 探针要求自有域普通 XML 成功、secure XML 拒绝、已证明存在的外来域 lookup 拒绝；不得对业务域试探 destroy/undefine。

停止通过真实 stop receipt 和精确 UUID 的 absence 确认。确认后归档 console、停本任务单位、恢复有身份/hash CAS 的 socket/规则/祖先 ACL，再核对原业务基线。
失败与未知结果保留原件；VM 尚活或写者未决时保留管理/期限停止能力，不盲目撤权、重试创建、删除写层或按名称前缀批量清理。
此范围只证明指定 profile 与已执行场景，完整故障恢复、多 VM 抗干扰、生产 Supervisor 与服务激活不能继承为成功。

组件检查不连接真实 VM：

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -B scripts/qualification/test_broker_peer.py -v
PYTHONDONTWRITEBYTECODE=1 python3 -B scripts/qualification/test_broker_vm_fixture.py -v
```
