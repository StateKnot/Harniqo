# KVM NIC 过滤资格验证

`qualify_kvm_network.py` 使用真实两台 KVM、任务专用 libvirt isolated network 和每 NIC 独立 nwfilter。固定 TLS canary 只回传本次合成 nonce，不转发网络、不调用模型、不承载生产代理；供应商凭据不会进入 Guest。报告固定 `production_egress_verified=false`、`milestone_complete=false`。

默认不安装软件、不使用 sudo/特权 Docker、不修改既有网络或系统配置；仅创建本任务 bridge 和过滤规则。另有默认关闭、须明确管理员授权的 `--allow-owned-host-canary-input` 选项，临时放行两条精确宿主入站规则，范围与回收见下文。Docker 仅用于读取 network IPAM，避免地址冲突；不执行/启动容器，不读取容器命令或环境变量。

## 前置条件

- 使用与封闭基线相同的授权普通 Linux x86_64 用户、基础镜像、libvirt/QEMU 和工具；另外需要 `ip`、`ss`、`openssl` 与可只读列出 Docker network IPAM 的权限。
- 独占宿主 10018/10019。脚本只在动态创建的专用 bridge 地址监听；不绑定公网。10018 是允许目标，10019 是实际监听的拒绝目标。Guest 自身也启动 10019 listener，完成 self-check 和双 VM readiness/completion barrier。
- 运行前使用 `df` 检查空间，保留至少 50 GiB 加两台 8 GiB 虚拟盘额度。Guest 每台 1 vCPU/1 GiB；这不是正式生产 slot 的容量验收。
- 必须允许本任务 bridge 到宿主 10018 的合法路径；宿主 INPUT 默认拒绝时需要管理员核对/提供仅限该 bridge、目标 IP 和端口的例外。10019 仍为拒绝目标。默认运行不会创建 UFW 例外；显式管理员选项也不改变默认策略、持久 UFW 配置或 NAT。

## 执行

```bash
PYTHONDONTWRITEBYTECODE=1 python3 scripts/qualification/test_qualify_kvm_network.py -v
PYTHONDONTWRITEBYTECODE=1 python3 scripts/qualification/test_kvm_host_policy.py -v

PYTHONDONTWRITEBYTECODE=1 python3 scripts/qualification/qualify_kvm_network.py \
  --root "$HARNIQO_WORKSPACE" \
  --base-image "$HARNIQO_WORKSPACE/images/vm/base/ubuntu/noble/amd64/<verified-sha256>.qcow2" \
  --timeout 240
```

组件测试不创建 VM/网络/TCP 端口，只证明缺少阳性对照不能通过、资源身份不匹配拒绝清理、子网重叠拒绝和固定策略护栏。真实执行使用 `kvm.lock`，不能与封闭基线同时运行。

脚本从小型候选地址池选择与宿主地址/所有路由、libvirt 网络和 Docker IPAM 不重叠的 `/28`；全部冲突即失败。网络无 forward/DNS/DHCP，每 NIC 仅允许必要 ARP 与固定来源到 bridge:10018 的 TCP。Guest root 在负向探测前主动增加默认路由；探测实际 host/peer canary、metadata、外网直接地址和来源 IP 伪造。IPv6/其他 EtherType 在规则中拒绝，但没有原始帧全面攻击验证，不据此宣称所有网络攻击均已验证。

成功必须包括合法 TLS 请求、host 与 peer 的真实监听对照、全部必需 Guest 检查、canary 未收到被禁入口的 Guest 请求、清理成功、基础镜像未变、既有 domain/network/filter 清单/状态不变。只收到超时不能算隔离通过；合法路径失败则整轮失败，后续负向结果未验证。

## 显式管理员临时入站例外

已取得本次管理员授权时，才可在上述命令追加 `--allow-owned-host-canary-input`。凭据只能预先保存在指定服务器工作区的 `.private/admin-sudo-password`；文件必须为操作账号拥有、普通文件、0600、单硬链接、不经符号链接，长度为 1～4096 bytes。脚本不会代为修改权限。`kvm_host_policy.py` 用 `O_NOFOLLOW` 打开并核对同一 inode，凭据只在服务器内存及 `sudo -S -p ''` 标准输入流转，不进入命令参数、环境变量、报告、本机副本或 Guest。

启动 VM 前核对实际本任务 network UUID/name/bridge、两个固定 Guest IP、无 forward/DNS/DHCP 的拓扑，以及当前 `iptables-nft` 后端、`ufw-user-input` 链和 `INPUT DROP`。先把有效 nft/iptables 规则保存在本轮 0700 私有目录，再插入恰好两条运行期规则：本轮随机 bridge 入站、单个 Guest `/32` 来源、本网关 `/32` 目标、TCP 10018、`NEW,ESTABLISHED`，使用含 network UUID 和 Guest 序号的唯一 comment。规则检查允许内核规范化字段顺序，任何额外 selector、重复 comment、已有同身份规则或添加失败均拒绝启动 VM。

结束时先停止本轮 VM，再逐条核对唯一 comment 与完整 selector，以原始规则参数精确删除；删除返回未知时必须先确认特权写入进程已退出，再读取确认规则不存在。sudo 超时不直接 SIGKILL；仍未退出或由信号终止时，不能把当前规则暂时不存在当作恢复成功，保留本任务 network/filter/写入层及证据。创建与登记 sudo 进程的短临界区屏蔽可处理信号，登记后恢复；避免启动期间中断遗漏写入者。不得按规则序号、前缀、`flush` 或全表恢复来清理。前后有效规则结构必须相同（只忽略计数器的 packets/bytes 和规则 handle，保留命名计数器身份及引用），既有规则或默认策略有差异就报告失败，不回滚其他作业的变更。最后按原流程回收本任务 filter/network。10019、管理接口、其他 Guest、metadata 和直接外网均不增加例外。

该选项不调用 `ufw reload`，也不写持久 UFW 配置。若其他管理员重新加载策略，临时规则可能被移除，本轮应按实际失败留证。SIGKILL/宿主断电后的人工恢复依据 `host-input-policy/receipt.json` 的 UUID、bridge、两条完整规则和私有快照；先核对实际归属，再精确撤销。它仍不能替代生产服务身份、权限委派、宿主 watchdog 或生产 Egress。

## 失败、恢复和证据

报告及完整 console 位于 `.private/qualification/network-<run-id>/`，公开仓库只写脱敏摘要。当前版本保存源码/依赖 helper hash、基础镜像 hash、libvirt/Python 版本、实际 network/filter/domain XML、Guest 网络诊断、canary 审计和前后空间读数。空间变化可能包含共享主机其他作业的同期变化，不按目录大小推算释放量。

普通异常/SIGTERM/SIGHUP 进入精确回收；先按预生成 UUID 停止 VM并核对，撤销已启用的本任务入站例外，再关闭 canary，移除只属于本任务的 filter/network/写入层并恢复 ACL。对象身份/目录 inode 不符或停止无法确认时保留资源并报错。不会按前缀删除，也不会回滚其他人的并发变更。

SIGKILL/宿主断电无法依赖 `finally`。依据启动前保存的 `ownership.json` 核对本次 UUID、目录和进程；先停止确定 VM和 canary，再清理对应网络/filter，最后回收写入层。不能直接重跑并假设旧资源已不存在。TLS canary 自带运行期限和 CPU/内存/文件/描述符上限，但仍是操作账号的受信测试 fixture；不等于生产 service UID/cgroup/watchdog 已合格。

## 2026-10-29 实际结果

前两次真实资格运行均失败，原报告不改写。第三轮在明确授权的精确临时例外下通过，范围限于本节固定 TLS canary 的 NIC 网络资格，不代表完整生产出网通过。

第一轮在合法 Guest TLS 连接阶段超时，未执行负向探测；资源清理完成，但结束端口 bind 检查报错，原报告保持失败。随后读取该两端口的 `ss` 结果无 LISTEN/TIME_WAIT；没有证据可据此重写首次失败。脚本修正为先查 LISTEN，再按 TCP 地址复用语义检查 bind，同时让自身 listener 使用同一语义。

第二轮同样在合法 TLS 阶段超时，Guest 均有正确固定 IP/MAC、直连路由，网关 ARP 为 REACHABLE，宿主存在两 Guest 邻居。Host 对 10018/10019 的 TLS 阳性对照成功，Guest 的 peer listener 创建成功；双方 readiness barrier未完成，负向探测没有执行。`cleanup_errors=0`，临时 VM/network/filter/写入层已回收，端口释放，既有对象和 base hash 不变。

早期普通权限只能确认 UFW 已启用且配置默认 INPUT/FORWARD DROP，实际规则读取被拒绝。当时未据此猜测唯一根因。后续获得管理员只读授权，先核对并按明确授权将凭据文件从 0664 收紧为 0600，再读取有效 nft/iptables/UFW/ebtables 规则。两个原失败时间窗口分别找到 6 条和 3 条 `[UFW BLOCK]`，匹配当轮专用 bridge、两 Guest 之一、本网关、TCP SYN 和目标 10018。这证明合法请求实际到达 UFW 宿主入站拒绝路径；当前有效 INPUT 为 DROP，`ufw-user-input` 无匹配 10018 的允许规则。当前后端为 nft，legacy IPv4/IPv6 无规则；本任务已回收后的 libvirt host-in/ebtables 状态不等于当时逐条 nwfilter 状态。

原始规则与历史日志只保存在服务器 `.private/qualification/network-admin-readonly-20261029T152221Z-cb408865/`，只读诊断没有创建 VM 或改变网络策略，两个失败报告保持原样。新增临时策略的 15 项负向组件和原 6 项网络护栏在本机、指定服务器均通过；这些组件没有调用 sudo、创建 VM/端口或更改真实规则。

第三轮 `network-6bd4f9821341` 于 `2026-10-29T15:46:07Z`～`15:48:05Z` 实际完成，2 台真实 KVM 各 41 项检查、共 82 项通过：每台包含 28 个宿主地址拒绝检查，以及合法 TLS、真实 host/peer listener 阳性对照、host10019/peer/metadata/外网直连/来源 IP 伪造拒绝、负向后合法入口仍通等必需检查。canary 共 12 次请求，禁止端口的 Guest 命中为 0；没有模型调用。

本轮两条临时宿主规则均已核对删除，`preexisting_filter_unchanged=true`、`effective_ruleset_unchanged=true`；`errors=[]`、`cleanup_errors=[]`，原 domain/network/filter 清单和基础镜像未变，临时 VM、network、filter、bridge、写入层回收，10018/10019 释放。随后另一次只读检查再次确认精确 UUID/bridge/监听均不存在、两条规则删除 receipt 完整、源码快照 hash 一致。报告、规则前后快照与完整 console 仅位于服务器 `.private/qualification/network-6bd4f9821341/`；其中 `verification-sanitized.json` 为独立收尾核对，未把原始规则复制到本机。

metadata 和外网拒绝仅证明本轮 isolated network/nwfilter 拓扑中的实际探测结果；本轮没有上游转发、真实模型出网或生产代理身份。报告仍为 `production_egress_verified=false`、`milestone_complete=false`。

生产 Egress/凭据/文件授权、服务身份、宿主硬资源配额、lease/fencing/watchdog 与灾备仍须独立实现验收，见 [生产评审](../../research/m0-execution-network-review.md)。
