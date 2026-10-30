# M0-05 生产执行网络与资源准入评审

初始评审：2026-10-29；状态更新：2026-10-30。**固定 NIC canary 的 82 项检查已通过（第 11 节）；生产 Egress、宿主硬配额及专用执行身份仍未验收。**

评审范围：现有封闭 KVM 基线之后的最小完整实施方案。第 1～9 节中的设备与权限描述属于初始只读评审时点。随后两次 NIC 测试因合法 TLS 路径失败（第 10 节），第三次经明确授权的精确临时规则通过并完整恢复（第 11 节）；原始失败保留。没有安装生产服务、调用模型或重启既有服务。正式生产实施应调用最终要交付的生产 Module，不能把固定 canary 当生产代理验收。

## 1. 决定与现有事实

采用 **KVM + 本任务专用 libvirt isolated network + 每个 NIC 的宿主 nwfilter + guest-only TLS 模型/文件入口**。VM 不直连互联网，不接既有业务 bridge，不共享工作区写入层。允许的外部请求由受限 Egress Module 发起；原生协议、流式行为和供应商身份保持可追溯。

这是现有架构的直接实现，不引入新 VMM、Kubernetes、通用 CONNECT 代理或云厂商适配层。无 NIC 基线继续保留为封闭执行模式；不能把“无 NIC”重命名为“选择性出网已通过”。Docker 可以是 VM 内 Harness 的打包方式，不能承担不可信租户之间的生产隔离声明。

| 证据 | 已核对事实 | 不可据此推出 |
| --- | --- | --- |
| 现有 KVM 私有原报告 | 第三次运行 `passed=true`，2 VM × 2 冷启动，错误/清理错误均为 0；此前两份失败报告仍在 | 选择性网络、生产身份、资源耗尽或 lease/fencing 已通过 |
| `virsh version` | libvirt 10.0.0，QEMU 8.2.2；实际 KVM 基线已存在 | 当前上游最新补丁或所有安全更新已落实 |
| 宿主能力 | cgroup v2；systemd 255；AppArmor 与 DAC；ext4；检查时可用约 2.5 TiB | 控制器已委派、配额已启用、OOM 可控或容量压力已经验收 |
| 操作身份 | 普通账号属于 libvirt、Docker 等组；当前及 ambient capabilities 为空 | 生产 Runner/代理已实现最小授权 |
| 管理权限 | `sudo -n -l` 要求密码；`nft list tables` 被权限拒绝 | 可无条件安装系统服务或读取/修改宿主全局过滤规则 |
| libvirt 配置 | 可读配置中 `auth_unix_rw="none"`，未见启用 `access_drivers`；后续实际创建/删除了本任务 network/nwfilter | 按对象和操作授予权限已启用，或合法 Guest TCP 路径已可用 |

本次实时核对只在工具记录中读取必要字段，没有导出凭据、业务容器命令或其他项目配置。具体主机、网络名、domain UUID 和原始报告位置保留在私有环境记录；公开文档不抄入。`.private/development-environment.md` 的最初设备快照早于 KVM 实测，不能拿其中“尚未启动 VM”否定后续证据。

libvirt 默认连接权限与细粒度 ACL 是两层机制。启用 ACL 需要管理员配置，官方说明其配置变更需要重启 daemon；共享主机不应为本次实验直接改现有 daemon。权限不足时停止受影响项，不能经 Docker socket、特权容器或任意宿主命令绕过。见 [libvirt ACL](https://libvirt.org/acl.html)、[polkit 对象权限](https://libvirt.org/aclpolkit.html)。

## 2. 最小网络拓扑

```text
Guest A (独立磁盘/身份) ─┐                    ┌─ Model Egress ─ TLS ─ 已批准供应商
                       ├─ 隔离 bridge ─ TLS ┤
Guest B (独立磁盘/身份) ─┘                    └─ File Broker ─ 本执行限定对象
    每 NIC: 固定 MAC/IP、port isolation、默认丢弃
    virtio-serial: 独立控制/报告通道，不转发任意宿主命令
```

资格验证使用一个任务专用 transient network、两台 VM、两个独立执行身份。生产可按执行池复用同一份不可变策略；不得连接既有默认网络或业务网络。创建顺序必须是登记资源身份 → 创建隔离网络/过滤器 → 启动受限入口 → 核对生效策略 → 启动 VM。失败时保持关闭；不得退回默认 NAT。

网络配置固定以下不变量：

- 不配置 `<forward>`；开启 Linux bridge 的 `port isolated='yes'`；关闭该网络的 DNS、DHCP，Guest 用固定地址且不配置默认路由。两项只是额外防护，Guest root 可改自己的路由，真正授权由宿主规则决定。
- 从经过冲突检查的私有地址池分配小子网，必须与所有宿主路由、已有 libvirt 网络及容器网络无重叠。地址、MAC、network UUID、bridge 名和 filter UUID 在启动前写入私有 ownership manifest。不能把示例网段硬编码到生产。
- 宿主入口只绑定该专用 bridge 地址；端口从环境允许的 `10000–10100` 中独占登记，排除保留的 SSH 端口及已用端口。模型与文件入口使用分开的监听/授权域；不绑定 `0.0.0.0`、`::` 或管理地址，不新增公网/FRP 转发。
- Guest 静态 hosts 只解析入口证书中的专用名称。无 guest DNS 出口；供应商 DNS 在 Egress 侧解析。不存在“为了 DNS 暂时放开所有 UDP”的步骤。

libvirt isolated network 本身允许访问宿主和同网络 Guest；`port isolated` 也不阻止访问宿主。因此这两个选项不能替代 NIC 过滤器。见 [network XML 与 isolated/port isolation 语义](https://libvirt.org/formatnetwork.html)。

每个 NIC 独立绑定、默认拒绝的 nwfilter 必须覆盖整个二层入口：只允许本 VM 的固定 MAC/IP；ARP 仅限本 VM 与入口地址间的必要请求/回复；IPv4 TCP 仅准入该入口地址与明确端口，返回方向只接受对应已建立连接。明确拒绝 IPv6、VLAN、RARP、广播/多播和其他 EtherType；不依赖 Guest 关闭 IPv6，不接受自动探测的来源 IP。`ESTABLISHED` 规则同样受端点/方向约束，不能因规则顺序允许另一条连接绕过限制。实现需要实际验证 nwfilter 的 `in/out` 是相对 Guest 的方向，不能凭名称猜测。见 [libvirt nwfilter](https://libvirt.org/formatnwfilter.html)。

禁止 Guest 访问任意宿主地址上的其他端口，包含 bridge 地址上已有的通配监听；禁止其他 Guest、管理网、云 metadata、数据库、OpenBao、存储管理入口以及 IPv4-mapped IPv6 绕过。控制面监听不能复用 guest-only 入口。如果宿主已有 INPUT 策略使合法入口不可达，由管理员提供仅限定本 bridge/端口的规则；不改全局默认策略，不 flush 规则集，不覆盖其他 libvirt/Docker 链。

## 3. Egress、证书和凭据

Egress Module 的 Interface 接受 `route_id + 原生请求 + ExecutionCredential`。`route_id` 对应控制面冻结的供应商 origin、模型、允许方法/路径、Secret 引用和预算策略；Guest 不能提交任意 URL、上游认证头、代理地址、Unix socket 路径或 CONNECT 隧道。工具网络访问按实际需要增加显式 route；本轮不承诺任意浏览器/SSH/Git 协议已经支持。

以下条件属于同一个生产 Interface，而不是测试脚本的可选开关：

1. 每次新请求校验 issuer/audience/scope、组织/项目/Workspace/RuntimeInstance/Attempt、恢复代次、租约代次、当前授权与预算。签名有效和 JWT 未过期不足以允许旧 VM 继续调用。授权不可核对时拒绝新请求。
2. 只将已批准的 HTTPS origin 解析为允许的地址；拒绝 localhost、link-local/metadata、私有/管理/容器/Guest 网段、非全球可路由地址及显式宿主地址。固定地址连接仍用原始主机名校验证书/SNI。连接池不得跨 origin 或授权作用域复用错误的认证。
3. DNS 的所有返回地址、实际连接地址和重定向都检查。初版不跟随上游重定向，返回可诊断错误；没有“先检查域名，再交给另一个 resolver 重新连接”的 TOCTOU。控制 URL 的 IDNA、尾点、userinfo、端口及 IPv4/IPv6 表示法走同一规范化入口。
4. 若现有获准模型入口确属私有地址，需显式登记那个 origin/地址/端口的专用例外；不能放开整个 RFC1918 地址空间，也不能借例外访问管理接口。例外进入配置快照与报告。
5. 去除 Guest 的供应商认证与 hop-by-hop 头，由 Egress 读取 Secret 后添加上游认证。请求体、响应体及 SSE 使用背压、并发/大小/空闲/总时长限制；客户端断开与租约撤销关闭流并记录在途调用，不能伪称上游费用已停止。
6. File Broker 使用另一 audience/scope，仅允许本 Attempt 的冻结输入和平台分配的产物上传；不提供任意 URL、bucket/key、目录列表或通用转发。上传可核对 hash/长度；缺真实对象存储时只完成 Module 测试，文件链路验收继续未完成。

Guest → 入口采用受信 TLS，不安装抓包 MITM，不使用 `verify=false`。使用专用内部 CA 签发带 SAN 的服务证书；CA 签名私钥不进入 Guest、cloud-init、镜像或代理运行包。Guest 仅获得 trust bundle 与短期、绑定本执行的 credential；供应商长期 key 不进入 VM。Runner → 控制面继续使用独立 mTLS 身份。

本机生成的测试 CA 可证明相同的 TLS 校验链路，不代表证书签发/轮换/撤销运维已经交付。需实测错误 CA、错 SAN、过期证书拒绝；生产由部署配置提供签发者、轮换和恢复策略。Harness 不支持自定义信任链或端点时应声明该能力不支持，不能关闭验证以继续运行。

TLS/HTTP 实现直接使用既定受支持运行时和成熟服务栈，锁定依赖与版本；不自行实现 TLS/HTTP parser。`http.server` 可作本地测试 target，但不得交付为生产 Egress，Python 官方明确其不推荐用于生产：[Python http.server](https://docs.python.org/3/library/http.server.html)。

## 4. 生产权限与资源限制

| 身份 | 允许 | 必须拒绝 |
| --- | --- | --- |
| Supervisor | 控制面出站 mTLS、自己的有界 spool、Broker 固定 Interface | libvirt/Docker socket、供应商 Secret、任意宿主执行 |
| Resource Broker | 固定镜像/网络模板、已登记 UUID、限定资源值、拥有资源的核对与停止 | 用户 XML/QEMU 参数、任意路径、任意其他 domain 操作 |
| Model Egress / File Broker | 各自只读配置、必要凭据和后端目标、有限审计 | libvirt/Docker/宿主管理 socket、其他角色凭据、宿主业务 HOME |
| Guest 执行身份 | 自己工作区、获准执行 credential、受限入口 | 平台/Runner 管理身份、其他租户数据与 credential |

生产服务采用不同系统 UID、精确目录 ACL、无额外管理组、受约束的 systemd 服务与本地 socket peer 身份检查。操作账号已有的宽权限只可承担受信验收操作者角色，不是生产服务运行身份。AppArmor/DAC 保持开启；不得为安装 convenience 放宽整个 `images/` 或关闭保护。

Egress 除应用层地址校验外，需要自己的内核出口策略：默认拒绝，仅开放必要 DNS、冻结/受控刷新的上游地址和后端入口，排除管理网。可用 systemd cgroup 网络过滤落地，但必须检查本机 BPF 支持并运行负向用例；该机制可能在不支持环境无效果，且不是普通 user service 已有能力。变更只作用于本任务专用服务，不设置 `system.slice` 的全局策略。[systemd resource-control](https://www.freedesktop.org/software/systemd/man/latest/systemd.resource-control.html)

资源验证先使用两台 1 vCPU/1 GiB 的小 VM，保持网络比较轻量；该 profile 不替代正式 `2 vCPU/4 GiB` slot 的后续验收。

- CPU：Guest vCPU 数量与 QEMU 全进程 CPU quota 分别核对；包含 emulator/I/O 线程。读取实际 cgroup 限额和 throttling，不能以 Guest `os.cpu_count()` 当宿主限额证据。
- 内存：Guest RAM 固定；QEMU 与代理分别有硬上限、swap 策略和 OOM 事件证据。QEMU 限额需包含其额外内存，不能直接等于 Guest RAM；正常启动先测余量，再施加受控耗尽。libvirt 对随意设置 `hard_limit` 有明确警告：[domain CPU/memory tuning](https://libvirt.org/formatdomain.html)。
- 磁盘：虚拟盘容量、宿主实际分配空间及日志/spool 配额是三个不同限制。先有任务专用、可核对的硬容量池/文件系统配额，再做写满；不能用 sparse qcow2 的逻辑大小或 `df` 轮询充当宿主硬配额。现有 ext4 输出未证明 quota 已启用，本轮不能自行 remount。Guest 日志写满测试只写任务专用小卷，绝不填满宿主根盘。
- I/O/网络/并发：配置宿主生效的块 I/O 限额、NIC 带宽和代理请求/响应/连接上限；验收时保留未受压 B VM 的可用性数据。时长与停止上限由宿主执行，Guest 时间不可信。

cgroup v2 是实现机制；必须检查实际进程所在 cgroup、祖先/子级限制和 `memory.events/cpu.stat/io.stat`，只有配置文件还不够。[Linux cgroup v2](https://docs.kernel.org/admin-guide/cgroup-v2.html)

## 5. Lease/fencing 的完整最小语义

Resource Broker Module 使用小 Interface：`ensure(Assignment)`、`renew(Grant)`、`observe(ResourceRef)`、`revoke(ResourceRef, cause)`。复杂度留在 Implementation，包括先持久登记再启动、固定 UUID、镜像/路径/配置校验、创建超时后查询同一 UUID、停止证明和清理。首版仅实现 libvirt，不创建仅有一个实现的后端 Adapter seam。

Assignment/Grant 必须带完整归属、确定 UUID、策略 hash、恢复代次、单调递增租约代次、权威截止时间与签名；拒绝客户端提供的资源名称和宿主路径。Workspace 单写者记录持久化；任何超时不能生成新 UUID 试运气。Broker 独立于 Supervisor，由宿主服务监督并维护截止时间，绝不能依赖验收脚本的 `finally`。

租约失效的顺序为拒绝新代理请求 → 撤销既有流/入口 → 停止确定 VM → 读取宿主真实停止状态 → 允许释放工作区写权。使用受约束时钟偏差和单调计时；重启后先核对旧资源与 grant，不自动恢复失效执行。若 libvirt/宿主不可达，状态保留 `reconciling`，禁止启动同一 Workspace 的第二写者。Guest 的“已停止”报告不能转为正式停止证据。

Broker 崩溃也必须有停止策略：生产服务重启后首先对账；代理授权缓存过期即拒绝，不无限保留授权；最终停机窗口和无法核对时的处置进入验收阈值。完整端到端 fencing 需要真实控制面权威状态存储与恢复代次，不能拿内存字典或临时 JSON 模拟它并宣布 AC-07/AC-30 通过。M0 可先证明本地 Broker 期限/宿主停止，不混淆 M2 的持久化执行协调。

## 6. 成功、越界和故障用例

所有用例均记录配置 hash、VM/filter/network UUID、启动/停止时间、预期与实际结果。Guest 报告只能是证据之一；必须和宿主策略、服务日志、受控目标命中计数/审计对应。单次超时不能证明安全拒绝，合法请求失败也不能因为负向请求全失败而算通过。

| 用例 | 实际动作 | 通过条件 |
| --- | --- | --- |
| NET-01 合法出口 | A/B 各自使用本执行 credential 经 TLS 入口请求获准 HTTPS target；再按已有授权进行一个实际模型请求 | 真实上游响应、证书校验、请求归属和流式结束可查；公开 HTTPS 测试只证明出口，不替代模型集成 |
| NET-02 宿主管理网 | Guest root 人工添加路由，访问 bridge 其他端口、宿主管理地址和预登记管理网 target | NIC filter 拒绝；受控宿主目标零命中；不对既有业务做端口扫描 |
| NET-03 metadata/旁路 | 固定目标覆盖 metadata IPv4、IPv6 link-local/ULA、原始公共 IP、外部 DNS/DoH 和禁用端口 | 无直接连接；宿主规则匹配证据；未获准 DoH route 被代理拒绝 |
| NET-04 横向与伪造 | A 访问 B 的受控 listener；交换来源 IP/MAC、ARP 欺骗、IPv6/VLAN、任意默认路由 | B 零命中；A 合法请求恢复后仍可用；B 自身健康请求正常 |
| AUTH-01 执行凭据 | 缺失/伪造/过期、错 audience/scope、跨租户、错误 VM/Attempt/代次请求；含 keep-alive 重放 | 在上游调用前拒绝；旧 token 不能借连接复用绕过逐请求校验 |
| TLS-01 信任校验 | 错 CA、错 SAN、过期服务证书；上游证书错误 | 全部拒绝且无明文/跳过验证降级；真实内部 CA 正向请求成功 |
| SSRF-01 解析/重定向 | 受控 DNS/HTTP target 返回私有/metadata 地址、混合 A/AAAA、重绑定及重定向 | 不建立禁用地址连接；使用真实受控 target，并明确其不替代真实供应商 |
| FILE-01 文件范围 | A 猜 B 的输入/产物 ID、路径穿越、任意 key、超大上传、缺失/错误 hash | 无越权读取/发布；需真实 File Broker/对象后端才能验收完整链路 |
| FAIL-01 代理/策略失败 | 终止本任务代理、授权存储不可用、错误/缺失 filter、刷新失败 | 失败关闭；VM 不降级到直接互联网；不影响其他业务网络 |
| LEASE-01 期限与旧代次 | 终止本任务 Supervisor、停止续约、旧 token 新请求、Guest 伪造停止 ACK | 出口按期限关闭；宿主证实停机；未知不释放单写者 |
| LEASE-02 Broker/调用超时 | 杀本任务 Broker并重启、创建请求超时、重复启动、旧 grant 重放 | 查同一 UUID；无第二写者；无有效 grant 不恢复；保留失败证据 |
| RES-01 受控耗尽 | 逐项施加 CPU、Guest 内存、任务专用磁盘、日志/spool、连接/带宽压力 | 宿主硬限额有效；B 健康基线无不可接受退化；只回收 A 自有资源 |
| CLEAN-01 中断回收 | 正常结束、SIGTERM、启动中失败；独立恢复进程处理 SIGKILL 后遗留 | 只清理 manifest 所有资源；报告保留；既有对象配置与状态未被本任务更改 |

网络首轮阈值建议在运行清单冻结：两台 VM；单目标连接超时 3 秒、至多 2 次；全轮最长 10 分钟；最多 4 个并发代理请求/VM；请求/响应限制按真实协议固定；控制与 console 延用已有有界采集。负向用例不向真实 metadata/管理服务发送敏感请求体；生产禁止地址覆盖由静态规则与受控 canary 共同证明。默认网关不存在不能替代主动加路由后的越界测试。

中止条件包括：合法请求无法通过、过滤器未真正生效、无法识别资源所有权、可用空间低于安全保留、任务总资源超过预留、既有服务指标出现异常。继续保留故障证据，不靠反复放开权限“跑绿”。

## 7. 所需文件及 Interface 落点

下列为计划文件，不表示已经存在或已实现；沿用架构工程布局，资格脚本调用生产 Module 的同一 Interface。

| 落点 | 职责与交付 |
| --- | --- |
| `packages/contracts/` | draft2020-12 的 Assignment、Grant、NetworkPolicy、ResourceObservation、StopEvidence；约束不允许任意 XML/路径/URL；沿用现有生成验证流程 |
| `apps/runner/resource-broker/` | libvirt 具体 Implementation、ownership journal、期限 watchdog、重启核对、宿主停止证据；控制复杂度集中在此 |
| `apps/control-plane/` 的 guest-only 进程角色 | Egress/File Broker、真实执行授权/撤销检查、原生协议流转发、Secret 引用、限额与审计；不增加通用透明代理 |
| `deploy/execution/` | 固定 VM/network/nwfilter 模板、服务 UID/ACL/系统服务、受限端口/证书部署与管理员安装/撤销说明 |
| `scripts/qualification/qualify_execution_network.py` | 正向/负向真实 KVM 用例、阶段留证、精确清理；保留现有 `qualify_kvm.py` 的封闭基线 |
| `tests/acceptance/` | 身份交叉、SSRF、超时/重启、资源耗尽的可复现用例；替身只验证错误路径并单独标记 |
| `.private/qualification/network-<run-id>/` | 0700 原始报告/console/审计/受控目标命中记录；脱敏摘要才进入公开证据索引 |

Module 的 Interface 包含权限、调用顺序、失败关闭、时效与持久性，不只是函数类型。Egress 接受已验证配置和授权依赖，不自行读取操作账号 HOME、全局环境变量或猜测凭据。验证使用相同 seam，避免为“方便测试”添加另一套生产不存在的出口。

## 8. 清理、恢复与证据

1. 运行前读取 `df`，预留基础镜像之外的实际任务预算和至少 50 GiB；只读记录既有 domain/network/filter UUID、配置 hash、状态、宿主地址/路由与目标端口占用。完整宿主过滤规则需要有权限的只读收集；拿不到时明确未核验，不能宣称全局规则未变。
2. 每次创建前 fsync ownership journal，包含唯一 run ID、确定 UUID、配置 hash、目录 dev/inode、PID 与启动身份/服务 unit。持有 task lock；不能只凭进程号或名称前缀判断所有权。
3. 清理先拒绝新请求并关闭流，再按 UUID 核对/停止本任务 VM，确认 QEMU/domain 已停止；随后保存并 drain console/报告。无法确认停止时保留磁盘与网络隔离，不删除正在使用的层。
4. 关闭本任务监听/服务，核对没有其他 Guest 引用网络或 filter，再删除本任务 transient network 和自行定义的 filter。绝不修改系统内置 filter，不执行全局 firewall restore；并发变更不是回滚其他人配置的理由。
5. ACL 恢复使用现有“当前值仍等于本次施加值”的比较保护；只删 inode/manifest/真实路径匹配的本任务文件。失败报告、必要日志和基础镜像保留在缓存外；凭据失效后删除临时私钥/seed，报告只留指纹和引用。
6. SIGKILL/断电后的恢复工具先读持久 manifest，核对实际资源再复用同一撤销流程。不能宣称 Python `finally` 能处理进程硬杀。重启后未知状态仍保持隔离，禁止自动重放有副作用请求。
7. 结束再次读取 `df`，只报告实际可用空间变化；核对既有 domain/network/filter 配置和状态、任务监听已释放、业务健康检查无新增失败。其他人并发变更时记录差异，不强行恢复其状态。保存原始失败，成功重跑不覆盖历史。

## 9. 初始评审时点的执行条件（历史）

| 工作 | 不新增外部资源能否完成 | 当前具体条件 |
| --- | --- | --- |
| 生产 Module/Schema、固定模板、安装/卸载与验收工具 | 能 | 本地/指定工作区即可开发；静态与组件结果单列，不等于真实隔离验收 |
| 专用 libvirt 网络 + 两 VM 的真实过滤验证 | 有条件能，当前合法路径阻塞 | 后续已证实创建权限与回收；Guest ARP 正常、合法 TLS 超时。需管理员只读核对有效 INPUT 规则，并确保本任务专用入口可达；不改既有网络 |
| 真实 HTTPS/现有模型端点的代理访问 | 有条件能 | 复用已经获准的端点/凭据与模型调用授权；正式 TLS/认证/原生协议链路和服务身份须实现，不能由 `curl` 结果代替 |
| 普通账号下 libvirt CPU/内存调度参数小规模验证 | 有条件能 | 需按任务 UUID 确认实际 cgroup生效；既有 capabilities 不证明写权限，不先对共享主机施加压力 |
| 专用生产 UID、系统服务/watchdog、代理内核出口控制、宿主存储硬配额 | 当前权限下不能完成系统部署验收 | 需要管理员提供明确受限部署路径/策略；无免密 sudo，宿主规则不可读，配额未证明。准备可审查配置无需先取得权限 |
| 完整控制面授权撤销、持久 lease/fencing、预算/文件链路 | 现有主机可承载，但当前产品实现尚缺 | 依赖真实权威状态存储及相应 Module；不是购买另一台 VM 就能解决，不能用 mock 关闭任务 |
| 宿主失联、N−1 容量、独立故障域与灾备 | 单机不能证明 | 需要第二故障域和独立备份/恢复资源；禁止中断当前共享宿主制造事故 |

建议根任务先实现可复用的生产网络/授权 Module 和精确资源清单，同时完成管理员部署配置；只有权限与宿主保护门槛实际满足后才执行网络资格验证。可把结果记为 `M0-05-selective-egress-network`，明确通过的范围和未通过项；不因为此子项通过关闭 M0-05。

本评审不改变 [M0 结果](../docs/planning/M0-RESULTS.md)、[任务清单](../docs/planning/BACKLOG.md) 或 [验收状态](../docs/planning/ACCEPTANCE.md)。执行拓扑与生产身份、资源上限、回收语义完成真实验证后，再按任务退出条件决定 M0-05 是否可接受。

## 10. 后续实际 NIC 过滤资格尝试

已交付 [独立资格脚本](../scripts/qualification/qualify_kvm_network.py)、[负向护栏测试](../scripts/qualification/test_qualify_kvm_network.py) 和 [操作/结果说明](../scripts/qualification/KVM-NETWORK.md)。没有修改原封闭 `qualify_kvm.py` 或规划文件。该脚本用固定 TLS canary 检查真实 VM 网络，不实现生产 Egress，不访问上游模型。

两轮都真实创建了两台 KVM、一个无 forward/DNS/DHCP 的专用 transient network 和两个独立 nwfilter。Host 的允许/拒绝端口 listener 阳性对照成功；Guest 到网关 ARP 正常，但合法 TCP/TLS 超时，未进入负向探测。因此 **网络资格仍失败**，不能根据零 forbidden hit 推导安全通过。

第一次另有端口结束 bind 检查错误，历史不改写；第二次修正 listener/检查的地址复用语义并增加 Guest 地址/路由/邻居诊断后，`cleanup_errors=0`，本任务 VM、network、filter、写入层全部回收、10018/10019 释放、原 domain/network/filter 清单和基础镜像不变。原始失败报告与 console 留在私有资格目录；第二轮另存当次实际源码及 hash 旁车。

已只读确认 UFW 启用、配置默认 INPUT/FORWARD DROP；实际 user rules 不可读。这是必须核对的宿主条件，尚无逐规则命中证据证明唯一根因。下一次实际网络测试需要管理员先核对现有有效规则，并确保只针对本任务 bridge 地址的 10018 入站可用；其余拒绝目标维持限制。不得扩大整体入站策略、复用既有业务网络或用 Docker特权绕过。生产服务身份/硬资源限制/watchdog 等原有缺口保持不变。

## 11. 后续管理员只读诊断与临时例外实现

用户随后提供了仅保存在指定服务器的管理员凭据并授权验证。元数据检查发现原文件为 0664；获明确授权后只对该已核对 inode 收紧至 0600，再以 `O_NOFOLLOW`、操作账号 UID、普通文件、单硬链接和长度限制读取，通过 sudo stdin 运行只读诊断。凭据内容没有进入输出、参数、环境、本机、证据文件或 Guest。

有效规则确认 iptables 使用 nft 后端，IPv4 INPUT 默认 DROP，UFW 用户入站链没有适配本任务 TCP10018 的允许规则。更直接的历史证据是：原两轮失败时间窗口分别有 6/3 条 UFW BLOCK，输入接口为当轮专用 bridge，来源为测试 Guest，目标为本网关，协议 TCP SYN/10018。由此确认 UFW 入站拒绝路径实际命中过这两轮合法请求；当前已回收的任务拓扑仍不能作为完整实时 nwfilter trace，不预先排除其余待验证问题。完整快照只留在服务器 `.private/qualification/network-admin-readonly-20261029T152221Z-cb408865/`，历史失败记录不变。

经明确授权，资格工具增加默认关闭的 `--allow-owned-host-canary-input`。管理员能力封装在 [独立 helper](../scripts/qualification/kvm_host_policy.py)，先核对实际 owned network 和当前后端/链，再用两条带唯一 network UUID comment 的运行期规则只允许两个固定 Guest `/32` 到本轮 bridge `/32` 的 TCP10018。完整 selector 与唯一性检查失败或添加结果未知都不启动 VM。结束先停 VM，按原完整规则逐条撤销，核对私有 before/after 规则快照；默认策略、持久 UFW 配置和其他作业规则均不修改。并发策略变化只报失败，不恢复别人规则。操作和故障恢复边界见 [资格说明](../scripts/qualification/KVM-NETWORK.md)。

新增 [15 项负向组件](../scripts/qualification/test_kvm_host_policy.py) 与原 6 项网络护栏均已在本机和服务器通过，覆盖凭据不合格、错误拓扑/后端、规则重排、添加/删除未知结果、重复或被修改的归属、并发规则变化与精确回收；组件不提权、不创建 VM、不修改真实网络。临时固定 canary 例外只服务于本次 NIC 资格验证，不能继承为生产 Egress/凭据隔离/服务身份/cgroup/watchdog 已合格。

独立复核另外关闭了三处边界：有效规则规范化只去除计数器 packets/bytes，保留命名计数器的身份和引用；sudo 超时或被信号终止时保留未决写入者，未确认退出不判恢复成功；创建与登记特权调用时暂时屏蔽可处理信号，登记后恢复原 mask，子进程也恢复原 mask。未决时保留本任务网络资源供精确恢复，仍不宣称 SIGKILL/断电自动恢复已实现。

之后按授权仅重跑一次真实双 VM。`network-6bd4f9821341` 在 `2026-10-29T15:46:07Z`～`15:48:05Z` 通过 2×41=82 项 Guest 检查，每台含 28 个宿主地址拒绝检查。合法 TLS 与 host/peer listener 阳性对照、必需拒绝用例、负向后合法访问均通过；canary 12 次请求、禁端口 Guest 命中 0、模型调用 0。两条临时 INPUT 规则均精确撤销，原 filter 与全有效 nft 规则结构一致，原 libvirt 对象与 base 不变，临时 VM/filter/network/bridge/写入层及端口已回收，`errors=[]`、`cleanup_errors=[]`。服务器 `.private/qualification/network-6bd4f9821341/verification-sanitized.json` 又独立核对了 UUID/bridge/端口不存在及源码 hash；原始规则、console 和凭据不传本机，前两次失败证据保留。

该通过结论仅覆盖固定 TLS canary 的真实 NIC 网络资格；isolated 拓扑中的 metadata/外网拒绝不等于生产上游路由和 Egress 授权已过。`production_egress_verified=false`、`milestone_complete=false` 保持不变，M0-05 仍须完成前文生产身份、凭据/文件授权、cgroup/磁盘硬配额、lease/fencing/watchdog 与故障域验证。
