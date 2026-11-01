# Broker 服务入口

`apps.runner.resource_broker.service` 是固定实例组的 Linux 服务入口，复用原
Broker、租约、持久 journal 与 [Supervisor IPC](IPC.md)。运行前先准备并准入
[固定 catalog、文件和通道](README.md)，再生成不可变配置；入口不分配镜像、
创建通道、修复权限或迁移旧状态。

配置是最多 64 KiB 的 UTF-8 JSON，拒绝重复或未知字段。顶层字段为：

| 字段 | 内容 |
| --- | --- |
| `schema_version` | 固定 `1` |
| `broker_identity` | 实际非 root `uid`、`gid` |
| `state_dir`, `images_root`, `channel_root`, `ipc_directory` | 规范绝对目录路径 |
| `issuer_key_file` | 私有 32 字节 issuer key 的路径引用，不嵌入密钥 |
| `lease` | `max_seconds`（1–600）、`max_clock_skew_seconds`（0–30） |
| `templates` | 1–64 个固定 Template；显式包含 `resource_profile` |
| `peers` | `uid`、`gid` 和 `resources`；每项绑定 UUID、组织、工作区与允许的四操作子集 |

Template 字段为 `template_id`、`resource_id`、`name`、`disk`、`seed`、`channel`、
`console`、`source_base_sha256`、`resource_profile`。peer 的资源项使用
`resource_id`、`organization_id`、`workspace_id`、`operations`。完整输入形状见
[部署包说明](../../../../deploy/runner/README.md)及
[服务回归夹具](../../../../scripts/qualification/test_broker_service.py)。

配置须为 root 或实际 Broker UID 拥有的单链接普通文件，模式 `0600` 或 `0640`；
全部祖先归 root 或该 UID、不可组/其他写、无 symlink。实际身份必须与配置相符，
不能从可组写的源码目录加载发行配置。原始配置字节的 SHA-256 由可信发行路径提供，
不能将同时被不可信调用方控制的配置和摘要视为授权。配置每次构造服务时重新读取核对；
运行实例使用已准入的不可变配置，没有在线 reload。

```sh
python3 -B -m apps.runner.resource_broker.service check \
  --config "$HARNIQO_BROKER_CONFIG" --sha256 "$HARNIQO_BROKER_CONFIG_SHA256"

# 仅对全新的、尚无状态的实例组执行一次；不能重置已有 journal。
python3 -B -m apps.runner.resource_broker.service initialize \
  --config "$HARNIQO_BROKER_CONFIG" --sha256 "$HARNIQO_BROKER_CONFIG_SHA256"
```

`serve` 和 `watch` 使用相同参数，在两个独立受监督进程中运行。`serve` 提供四操作
IPC；`watch` 独立核对租约和真实资源，listener 退出不应停止 watcher。正常启动拒绝
缺失初始化状态；损坏或不完整的已认证 journal 仍走原 Broker 的保守恢复及停机路径，
不能改用空状态。Python Interface 为 `load_configuration(path, expected_sha256=...)`、
`initialize(config)`、`serve(config, ...)`、`watch(config, ...)`；纯
`parse_configuration(bytes)` 仅校验语法，不能直接授权运行。

CLI 在构造完成后输出有限 JSON `ready` 事件，包含角色、PID 和
`status=operational|degraded`，可能附稳定错误码。这是进程准入状态；`check`、
ready 和 systemd `Type=exec` 都不能证明 VM 或业务健康。SIGTERM/SIGINT 只设置无锁
退出标记；listener 关闭新请求准入并等待已进入的请求结束，watcher 独立退出。
等待期间不能将已接收操作的结果推断为取消；仍按 IPC 的结果未知规则处理。

专用账号、发行目录、双 systemd unit、目录权限及 polkit 候选由
[离线部署包](../../../../deploy/runner/README.md)生成。生成不安装或激活权限；必须先
实际验收 libvirt daemon 的细粒度 ACL、两个服务身份及 Supervisor 权限隔离。
当前宿主尚未完成这些门槛，部署包保持 `BLOCKED`。
