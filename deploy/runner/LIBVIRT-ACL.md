# 有效 libvirt API ACL 资格

本入口验证已安装的授权策略；不安装规则、不启动或停止 VM、不更改 socket 权限或 daemon。
适用 Linux 的 system QEMU、原生 Python libvirt 绑定及已建立的 Broker/Supervisor 专用账号。
共享宿主的部署、现有客户端兼容性与回退契约见[研究](../../research/m0-libvirt-acl-activation.md)。
原[离线服务包](README.md)继续保持 `BLOCKED`；本资格不授予 VM 写权限或激活正式服务。

## 实际验证

将经过审阅的 `scripts/qualification/qualify_libvirt_acl.py` 安装在 root 拥有且所有祖先不可由组/其他身份写入的发行目录。
使用系统 `/usr/bin/python3 -I -B`，冻结脚本 SHA-256；报告父目录必须 root 拥有、0700，报告路径必须未存在。
不要以开发者可写目录中的脚本运行 root 验收。

```sh
/usr/bin/python3 -I -B /opt/harniqo/releases/RELEASE/scripts/qualification/qualify_libvirt_acl.py \
  --broker-uid BROKER_UID --broker-gid BROKER_GID \
  --supervisor-uid SUPERVISOR_UID --supervisor-gid SUPERVISOR_GID \
  --ipc-gid IPC_GID --source-sha256 REVIEWED_SHA256 \
  --report /var/lib/harniqo/ACL_RUN/proof.json --timeout 60
```

以上大写参数由已核对的部署清单替换。实际 UID 的 NSS 主组及完整组集合必须与输入一致；不得给账号加入 libvirt、kvm 或 sudo 组。
父进程以 `setpriv` 清除全部 capabilities、设置 NNP、切换真实及有效 UID/GID；worker 等待父进程读取实际 `/proc` 身份后才调用 libvirt。

期望已生效的 bootstrap 策略：Broker 仅允许 QEMU `connect.getattr`，其他 libvirt action 拒绝；Supervisor 全部拒绝。
Broker 在同一个固定 `qemu:///system` RW 连接执行 hostname 成功、capabilities 明确返回 `VIR_ERR_ACCESS_DENIED=88`/`VIR_FROM_ACCESS=55`、hostname 再次成功。
worker 还检查真实 native FD 的 peer 路径和凭据与目标 RW socket 一致。
Supervisor 必须先成功 raw-connect 真实 RW socket，再在正常 `virConnectOpen` 的 `connect.getattr` 检查取得同样的结构化 ACL 拒绝。
`openReadOnly`、socket DAC 拒绝、认证失败、默认只读允许、空对象或普通异常均不能通过。

## socket 与共享宿主

工具不修改 DAC。实际 API 资格需要两个 UID 能到达 RW socket；它与生产 Supervisor 的 mount/DAC 隔离分别计量。
如现有 DAC 阻止连接，管理员只能在 driver 已启用、服务收窄规则已安装且账号无活动进程后，临时给精确两个 UID 添加 RW socket 的命名 ACL。
先保存 socket 的 device/inode、属主/组/mode 和完整 `getfacl --absolute-names --numeric` 原件；不更改其他用户/组权限，不加入管理组，不暴露其他管理 socket。
验收后确认 worker 已退出，并以 socket 身份与本次 after-ACL 摘要做 CAS，再恢复原件、读回比对。
发生并发 ACL/socket 变化时停止自动覆盖，保留原件和恢复 intent 由受信任管理员核对。
临时 ACL 不能当作已交付的持久生产 socket 授权；当前生产服务仍不激活。

共享 daemon 启用 driver 前后另行保存并比较现有 domain XML/state、QEMU PID/starttime/cgroup、网络及过滤器，保留准确 restart job/InvocationID、配置与规则原字节。
非服务身份采用何种兼容策略必须明确记录；API YES 保留旧 none 权限属于兼容模式，不是全宿主最小权限。
恢复旧 `access_drivers=none` 会使 API NO 规则失效，不能作为自动 fail-closed 回退。

## 结果与限制

报告使用 O_EXCL、0600、fsync 保存，包含两个 worker 的实际父进程观察、调用结果、socket/peer、绑定来源与摘要。
超时、身份不符、输出超限、禁止 API 意外成功、弱错误或任何缺少证据均失败；保留失败摘要，不重发修改。
原始报告含身份、路径与 hostname 摘要，应留在受控存储；公开索引仅发布脱敏结果。

通过只证明此时的有效 bootstrap API ACL。它不证明固定 UUID/name 的对象授权、XML 约束、专用 UID 下 VM 启停/IPC、QEMU `/proc` 观察、持久 socket 授权或正式 Supervisor 集成。
这些后续资格独立记录；不能填写一个 `verified` 字段来跳过。

组件回归（Linux/Python 3.12，不连接真实 daemon）：

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -B scripts/qualification/test_libvirt_acl.py -v
```
