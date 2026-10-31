# Resource Broker 真实 KVM 生命周期验收

`qualify_resource_broker.py` 调用最终使用的 `apps.runner.resource_broker.ResourceBroker` Interface。验收操作者准备两份独立全分配 raw8GiB 磁盘；固定预登记实例组的 Module 负责签名/配置绑定、单工作区写者、持久 journal、固定 libvirt UUID、期限、核对与真实停止。操作者不自行模拟租约结论，不提供可执行任意 XML/宿主路径的远程接口。

当前文档说明可执行流程，**不是通过报告**。实际结果须由指定服务器的私有报告及公开脱敏索引关联；不能仅依据组件测试或本文将 M0-05 标为完成。

## 前置条件与执行

- 普通获准账号，Linux x86_64、Python3.12、可使用 `qemu:///system` 的 KVM/libvirt；`qemu-img`、`qemu-system-x86_64`、`cloud-localds`、`getfacl`、`setfacl` 和非root `libvirt-qemu` 账号已存在。
- 使用既有 Canonical 签名核对过的 standalone qcow2；文件位于 `images/vm/base`，以 SHA-256 命名、由操作账号拥有且无写权限，旁车 provenance 的 hash/签名指纹与既有获取工具一致。共享源若归QEMU等其他账号所有，先准备操作账号拥有、同一已核对SHA的独立只读副本与来源旁车；操作者不会放宽base校验、chown或chmod共享源。目录和文件不经符号链接。
- 实际空闲至少 50GiB 加两台各8GiB磁盘；运行前后保存 `df`。用 convert/resize/`posix_fallocate` 与 `st_blocks` 核对实际分配，不以 sparse 文件虚拟容量冒充已分配空间。它不证明宿主文件系统、日志或任意 qcow2 的通用硬配额已交付。
- 与现有封闭/NIC资格共用 `.state/qualification/kvm.lock`，禁止同时运行。该路径仅协调受信操作者，不储存签名authority。既有目录0775、lock0664可保留原mode；校验目录/文件归属、无符号链接、普通单硬链接lock及实际flock互斥，并在启动前/结束时核对lock inode未被替换。world-writable路径拒绝；不chmod共享目录或lock。启动前保存全部既有domain/network/filter UUID、配置与状态；只管理本轮两个确定UUID。
- 所有新增磁盘、seed 位于 `images/vm/instances/rb-<run-id>`；socket/受限console与独立authority子目录位于 `.state/resource-broker-qualification/rb-<run-id>`；报告、issuer随机32byte key及配置位于 `.private/qualification/resource-broker-<run-id>`。新建专用父目录和任务目录初始0700；既有专用父目录若不是本账号拥有的无符号链接0700目录则拒绝，不自动chmod。为QEMU临时添加的外层traverse ACL可能使mode mask显示为0710，Broker authority子目录仍为0700。私钥/报告0600；不会读取模型或管理员凭据。

```bash
PYTHONDONTWRITEBYTECODE=1 python3 scripts/qualification/qualify_resource_broker.py \
  --root "$HARNIQO_WORKSPACE" \
  --base-image "$HARNIQO_WORKSPACE/images/vm/base/ubuntu/noble/amd64/<verified-sha256>.qcow2" \
  --timeout 120
```

`--timeout` 为30～150秒的 Guest 收集上限。每台1vCPU/1GiB、无NIC、无共享宿主文件系统、不挂 Docker/libvirt socket；不开放宿主TCP端口，不创建网络/过滤器，不提权、不更改防火墙、不调用模型。它沿用已验收的 Guest 配置与 virtio-serial 报告、独立宿主 console 接收和11项 Guest 检查，不从本轮推导生产出网或原生 Harness 已在VM内运行。

`--worker launcher|watch --config <private-path>` 是本工具的子进程入口；配置仅接受当前工作区私有 qualification 路径。正常操作用上面的主命令，不手工制造其他 worker。worker不输出密钥，禁用core dump，stdout/stderr受2MiB限制，完整输出只保存私有文件。

文件系统护栏可独立运行，不创建VM、连接libvirt或调用模型：

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover \
  -s scripts/qualification -p test_resource_broker_operator.py -v
```

这组组件区分共享协调路径与私有authority，检查symlink/hardlink/FIFO、foreign owner、lock替换、私有目录拒绝自动chmod，以及共享base owner校验；它不替代真实生命周期验收。

## 验收因果链

1. 新建两台确定UUID与独立 raw/seed/socket；临时精确 ACL 仅允许 QEMU 访问所需路径，保留原ACL及恢复比较保护。生成 host-only issuer key，并冻结包含 `configuration_sha256` 的 Assignment/Grant。配置 hash 来自生产 Template，绑定基础来源、raw设备/inode/大小、seed内容/身份、socket身份与固定VM策略。
2. 保存 ownership、生产 Module 的全部 Python 源快照/hash 和配置后，先启动独立 watch 进程并核对 ready，再启动 launcher。watch覆盖启动和Guest检查阶段，不依赖 launcher 或 Guest 时间。
3. launcher先 `ensure(A)`，再用正确签名请求第二资源占用同组织/工作区，必须得到 `workspace_busy`；之后 `ensure(B)`。根操作者重复相同 Assignment，仍只能存在这两个 UUID。错误签名不能被接受。
4. 两 Guest 各完成11项真实封闭检查并返回独立 nonce。续约A至约8秒、B至约120秒，代次递增；旧代次必须拒绝。向launcher发送SIGKILL并确认其退出；紧接着只读核对A仍实际活动，缺此阳性门槛直接失败。
5. 在已确认launcher退出且A仍活动之后，只使用只读 libvirt UUID 清单等待A消失，期间不调用可能主动停止VM的 `observe/reconcile`。之后读取持久 StopEvidence，核对watch仍活着、B的实际UUID仍在并且观察为running；StopEvidence还必须为`libvirt_destroy_then_absent`，确认时间不早于A期限（允许0.25秒采样误差），排除Guest自行退出被误认作watch停止。这才证明独立watch在launcher死亡后继续执行期限停止。
6. 将B续约缩短至约5秒，再SIGKILL watch并确认其退出。超过期限后先只读确认B仍实际运行，再重建 ResourceBroker、执行 `reconcile()`，取得`libvirt_destroy_then_absent`宿主停止证明，确认时间不早于B期限。旧renew/原Assignment不得复活已停止资源；无新UUID，原磁盘在宿主停止确认前保留。

两个角色是不同宿主进程，但本次同属获准操作账号。它不代表专用服务UID、系统服务安装、正式Supervisor IPC、控制面持久fencing或全链路执行授权已通过。

## 留证、失败与回收

成功必须同时具备：上述正向/负向判据、两次SIGKILL确认、独立watch/重建因果链、宿主 StopEvidence、完整console归档、源与base hash不变、全部既有libvirt对象配置/状态不变，以及精确清理成功。报告固定 `milestone_complete=false`、`dedicated_service_identity_verified=false`、`production_egress_verified=false`、`global_fencing_verified=false`、`real_model_calls=0`。

普通异常/SIGTERM/SIGHUP进入 finally：先终止并确认本轮worker退出，再分别通过 Module `revoke` 核对两个固定资源。只有每台都为stopped且具备StopEvidence、没有未决创建者/运行UUID，才归档console、恢复ACL并删除身份匹配的本轮磁盘/socket目录；持久journal另存私有报告。不能凭一次`virsh list`暂时为空就释放资源。存在reconciling、未知创建或未确认worker退出时，保留镜像、socket目录、ACL、issuer key和journal供恢复，不回滚其他作业。

SIGKILL主操作者/宿主断电不靠finally自动恢复；按私有 ownership、配置和worker ready记录核对实际进程/资源，使用相同ResourceBroker及签名配置执行对账/撤销。主操作者本身不是生产常驻服务，此类硬杀恢复的完整自动化仍需单独验收。原始失败和后续成功保留不同目录，禁止覆写失败报告。

公开输出只含通过布尔、错误数量和相对报告位置。原始console、libvirt XML、真实路径、journal和签名材料均留在私有存储；公开仓库只登记经核对的脱敏结论。

运行期权限按明确角色验证：disk/seed 可归 Broker 或系统 QEMU UID，socket 可归 Broker、系统 QEMU UID 或 root；任意其他 UID 仍拒绝。新 seed 在 QEMU ACL 后必须调用正式 `prepare_seed_access`，再冻结配置和签名；它仅保留 named Broker 的只读 ACL，不改 group/other，不跳过 seed hash。旧失败用例不补改权限或报告。外层 IPC 目录只允许 0700 或精确的 named QEMU `--x` ACL（mode0710），权威 journal 子目录、报告与 issuer key 仍严格私有；0775 的旧协调目录不充当此边界。

原生正向还会在实际运行时保存owner类别（broker/QEMU/root），要求观察到QEMU持有连接socket，覆盖运行中与停止后的不同DAC角色；没有记录真实UID、路径或内容到公开摘要。

sealed Guest fixture沿用完整原探针程序，但由cloud-init `bootcmd` 直接执行，移除晚期 `write_files/runcmd`，避免无NIC环境的Final阶段依赖导致探针未启动，也避免重复执行。报告只说明检查发生时的Guest状态，不作为OS全部服务ready的证据；缺失或失败报告仍拒绝。默认120秒、`--timeout 150`是Guest报告窗口，生产租约/故障停止判据独立。

日志flush/fsync/close失败按stream记录有限错误后继续宿主revoke，不允许提前中断物理停止尝试；任何清理错误都会阻止删除本轮文件。确认停止后，持久journal目录必须完整归档成功才可释放临时目录，缺失或归档失败保留文件并判失败。


## 显式宿主限额 profile（M0-05 子项）

默认 `sealed-raw8g-q35-v1` 保持原固定组配置摘要与恢复语义；只有新建实例组显式使用 `--resource-profile sealed-raw8g-q35-limited-v2` 才采用新策略。已登记组不能原位切换 profile。v2 的签名配置绑定 QEMU 整个 domain 每100ms/100ms CPU带宽、2GiB计费内存及2GiB swap上限；Guest仍为1vCPU/1GiB。

正式 Module通过 `resource_limits(resource_id)` 和运行观察核对固定PID文件、QEMU UUID、进程starttime、所有线程与实际父cgroup；调用方不能传入PID、任意路径或配额。内核读回不符或不可确认会停止已认证归属的资源，不能继续作为正常running续约。停止核对只跳过已坏的资源限额检查，保留签名归属、文件身份和拓扑保护。

资格Guest先完成原11项报告，随后A执行45秒有期限CPU pulse、B仅对宿主在采样窗口新生成的随机挑战输出绑定nonce的应答。宿主只读采样6秒，要求A真实节流计数增长、CPU用量按同份cpu.stat的单调采样时间差落在有负载及周期容差范围内，B至少3次匹配窗口内新挑战的应答、两台正常内存峰值低于硬上限且无OOM事件。原两个SIGKILL、期限停止、拒绝复活、完整console及精确回收判据全部保留；缺少任何v2门槛即失败。

此子项不制造宿主OOM或swap耗尽，不证明磁盘/spool/块I/O等完整硬配额；`host_hard_quotas_verified=false`、`memory_exhaustion_verified=false`。2GiB余量只用于本sealed模板，非任意Harness通用参数。libvirt10/v2 swap映射和官方依据见[宿主资源限额研究](../../research/m0-host-resource-limits.md)。


## 显式 Guest 内存耗尽与外部终止资格

在既有固定 `limited-v2` 下运行独立用例，不修改 v1/v2 的 CPU、内存、swap 或配置摘要。此模式要求普通 Linux x86_64 / Python3.12 操作者及实际 KVM/libvirt 权限，复用本文件的已验证基础镜像、目录 ACL、空间与私有证据前置条件。运行前检查 `df`，至少保留50GiB加两块8GiB raw盘；镜像和本轮写入层仍位于项目 `images/`。

```sh
python3 scripts/qualification/qualify_resource_broker.py \
  --root "$HARNIQO_WORKSPACE" \
  --base-image "$HARNIQO_VERIFIED_BASE_IMAGE" \
  --resource-profile sealed-raw8g-q35-limited-v2 \
  --guest-memory-pressure --timeout 150
```

`HARNIQO_VERIFIED_BASE_IMAGE` 必须是工作区内已核来源、由操作者拥有且只读的 Canonical base；不是可由 Guest 提交的任意路径。`--guest-memory-pressure` 只接受显式v2，默认v1及不带此flag的v2仍走原有生命周期/CPU压力流程。

A先完成原sealed检查，再在其Guest内用nonce命名的transient service运行监控器和有界触页worker。该unit显式`Type=exec`、`DefaultDependencies=no`、`OOMPolicy=continue`、`OOMScoreAdjust=-1000`、185秒运行/3秒停止限额，外层等待190秒；worker仍覆写+1000。START/RESULT均读回实际MainPID、unit、cgroup、score、`memory.oom.group`及限额，变化即拒绝。不会修改已有cloud-init unit或manager默认策略；子进程身份、随机短comm、实际OOM分值、新内核victim记录、Guest OOM计数和SIGKILL退出必须关联。B在分配进度增长期间回答三次新挑战；OOM后的A需给出绑定本轮token和相同boot ID的健康应答，B也有新增健康检查。宿主通过正式`resource_limits`核对双方QEMU身份、实际限额及OOM事件前后样本。超时人为回收、MemoryError、旧日志或无匹配victim均不能计为OOM通过。

完成上述证据后，受信资格操作者才对已由正式Broker核对的A精确UUID注入一次外部`virsh destroy`；该操作只用于故障注入。正式Module必须通过真实宿主absence产生`libvirt_absent_after_confirmed_start`，重构后同一停止证据仍在，旧Assignment/Grant均不能复活A。B继续执行watch SIGKILL与持久期限重建停止。两类停止原因、console、journal/anchor及源码快照独立保留；不能把外部终止写成Guest OOM杀掉了QEMU。

此用例无NIC、无新增TCP监听、无模型调用，不更改宿主sysctl/cgroup/swap、全局服务或防火墙。公开证据仅表示固定v2的Guest耗尽和外部终止恢复；不证明宿主QEMU OOM、完整存储/spool硬配额、专用服务身份、Egress或控制面授权/预算。细化门槛与来源见[本轮研究](../../research/m0-guest-memory-failure.md)。
