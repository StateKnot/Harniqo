# M0 真实执行环境准入

这些脚本只操作指定 Harniqo 工作区及自己创建的临时 VM。运行报告、实际主机资源和既有 domain 清单位于被 Git 忽略的 `.private/qualification/`，不得直接发布其中的环境信息。镜像与写入层全部位于工作区的 `images/`。

## 1. 取得可信 Ubuntu 基础镜像

```bash
python3 scripts/qualification/fetch_ubuntu_image.py --root "$HARNIQO_WORKSPACE"
```

`fetch_ubuntu_image.py` 使用 Canonical HTTPS、系统 Ubuntu cloud-image keyring 与固定签名指纹校验 SHA256SUMS，再验证实际镜像 hash；不修改全局 keyring。镜像保存为 `images/vm/base/ubuntu/noble/amd64/<sha256>.qcow2`，旁边有签名、校验单和 provenance，真实清单写入 `images/manifest.local.json`。

需要 `gpgv`、可信系统 keyring、`qemu-img` 和出站 HTTPS。该下载不会启动 VM。不要为了继续验证跳过 GPG/SHA 校验或采用来源不明的镜像。

## 2. 验证两台 sealed KVM VM

使用具有 `qemu:///system` 管理权限的普通 Linux x86_64 用户；不需要 sudo，也不使用 privileged Docker。需要 `virsh`、`qemu-img`、`qemu-system-x86_64`、`cloud-localds`、`getfacl`、`setfacl` 与实际 KVM 权限。脚本不会安装软件、开启宿主 TCP 端口、创建网络或修改系统服务。

```bash
python3 scripts/qualification/qualify_kvm.py \
  --root "$HARNIQO_WORKSPACE" \
  --base-image "$HARNIQO_WORKSPACE/images/vm/base/ubuntu/noble/amd64/<verified-sha256>.qcow2" \
  --timeout 240
```

占用两台 VM，每台 1 vCPU、1 GiB 内存和默认 8 GiB 的稀疏虚拟盘；每轮启动后最多等待 `--timeout` 秒，共两轮冷启动。`--disk-gib` 允许 4–32 GiB，必须容纳基础镜像。运行前检查 `df` 和实际空间，至少保留 50 GiB 加两块虚拟盘容量；不会自动删除其他文件来腾空间。

脚本执行以下真实检查：

1. 验证基础镜像名称、Canonical provenance、实际 SHA-256 和 standalone qcow2 格式；保留并发锁，记录既有 domain UUID/状态。
2. 创建唯一 `harniqo-m0-*` 名称、UUID、独立 overlay 与 NoCloud seed。仅给 QEMU 用户增加缺少的路径遍历/文件权限，保存原 ACL；不使用 `chmod 777`、不关闭 AppArmor/sVirt，不接受用户 QEMU 命令行。
3. 创建两台 transient、无 NIC、无宿主共享文件系统的 q35/KVM VM。每台 VM 使用两个不同角色的私有 UNIX socket，均由宿主绑定、XML 使用 `source mode=connect`：virtio-serial channel 接收有界 JSON 报告，serial 接收原始 console 字节。QEMU 只获 socket 连接权限，不获宿主日志文件路径或读写权限，避免 libvirt/virtlogd 更换文件 inode 导致日志失真。
4. Guest 用已有 Python3/cloud-init 检查 KVM、CPU/内存/虚拟盘、无 NIC/非 loopback 默认路由、无宿主 socket/共享文件系统、其他 VM canary 不可见；写入自己的 canary 并同步磁盘。
5. 宿主只按自己预生成的 UUID 停止 VM，再以同一 UUID/overlay 冷启动。第二轮验证新的 boot ID、原 canary、前一 boot ID 和计数，证明该磁盘上的状态实际保留。
6. 宿主通过同一个 selector 同时读取报告和 console；为每台 VM 的每轮启动创建独立 0600 日志，最多保存 2 MiB，超出后仍消费串口流，并在报告记录 `bytes_seen`、`bytes_saved`、`discarded_bytes` 和 `truncated`。Guest 停止后最多再等待 10 秒读尽 console 至 EOF；每个实际启动的 Guest 都必须有非空连接和 EOF 证据。finally 仅停止自己拥有的 UUID，归档宿主持有的日志 inode 并校验身份、权限与容量，保存报告并恢复未被其他参与者改动的原 ACL。确认 domain 清单/状态和 base hash 不变后，删除本任务 overlay/seed；只有 console 已保存才删除工作目录。日志收集或保存失败保留该诊断目录并报告失败；无法确认已停则保留全部文件，绝不删除仍被使用的镜像。私有报告目录链限定为当前用户所有、无 symlink、权限 0700。

退出码 0 表示此 sealed 基线的两轮检查、其他 domain 不变和清理均通过；非 0 要检查 JSON 报告与 console。超时、Guest 报告失败、ACL/权限不满足、其他 domain 并发变化或清理失败都不能记为通过。`Ctrl-C` 也会进入清理；进程被 `SIGKILL`、宿主断电或 libvirt 永久不可达时不能保证自动清理，需要依据保存/输出的独占资源身份人工核对，不能按名称前缀批量删除。

每轮在启动前登记 `status`、`phase` 和预期 Guest UUID；无论收集、验证或后续步骤成功与否，finally 都保存已收到报告的独立快照、缺失 UUID、失败阶段和 `validation_error`。清理期间 drain 补收的报告也会写入最终快照；补齐报告不会把已超时的轮次改为通过。已确认停止的 overlay/seed 仍按原规则回收，失败诊断以私有 JSON 报告和 console 归档留存，不保留磁盘副本。

失败留证的标准库组件测试可独立运行，无需 KVM、libvirt 或基础镜像，只使用本任务临时 UNIX socket，不创建 TCP 端口：

```bash
PYTHONDONTWRITEBYTECODE=1 python3 scripts/qualification/test_qualify_kvm.py -v
```

测试覆盖部分 Guest 报告后超时、第二轮恢复检查失败、console drain 期间补收报告、快照与原始数据相互独立，以及两轮成功的记录状态。这些合成 Guest 测试不替代真实 KVM 准入证据。

## 3. 证据边界

报告名称为 `M0-05-sealed-kvm-baseline`，其中 `milestone_complete` 始终为 `false`。它能证明该主机真实启动了两台独立、无网络接口的 KVM VM，磁盘 canary 在冷启动后存在，以及脚本未改变原 domain 清单/状态。

它没有证明以下能力，不应因此关闭完整 M0-05 或宣称 SaaS 执行环境已生产可用：

- 允许模型/工具但禁止管理网、metadata 和横向访问的选择性出网。
- Harness 工具闭环、原生会话恢复、审批、取消、Guest 执行授权和预算控制。
- Runner/Broker 租约 watchdog、fencing、失联恢复和生产身份隔离。
- CPU/内存/磁盘耗尽后的宿主限制与回收、负载容量、备份/灾备恢复。

既有 VM 在测试期间被其他人正常启动/停止时，清单比较也会报差异；脚本只报告，不试图把其他人的 VM 改回原状态。基础镜像与本次报告保留，运行写入层不会作为可跨 Trial 复用的基础镜像发布。


## 4. 内部 HTTPS 上游传输资格

使用项目锁定的 Node24.22.1 和 `apps/model-egress` 的 integrity-locked 依赖；先检查 `df`，运行 `npm run typecheck` 与 `npm test`。完整命令、前置条件、固定端口、私有 PKI/报告及失败保留规则见 [Module README](../../apps/model-egress/README.md)；真实操作者为 `qualify_egress_transport.mjs`，仅 Linux 普通用户、loopback TCP10020/10021，不启动 VM、不改防火墙、不使用模型凭据。不要把受控 resolver 分支当作真实公网 DNS/供应商验证。脱敏结果见 [原生报告](../../docs/operations/egress-transport-native-2026-10-31.json)。
