# M0-05 Runner critical-event spool 研究与实现范围

更新日期：2026-11-01；研究起始基线：`a1344d30a0263867123a673379e8ddb84da4faaa`。
状态：**本地 SQLite Module、原生进程恢复与固定双 VM 容量故障停止子项已开发完成并验证（VERIFIED）；M0-05 仍为 IN_PROGRESS**。
本文件记录官方依据、已实现行为和待验收项。Module 检查不等于控制面提交、宿主文件系统配额或 M0-05 全项完成；任务状态以 [M0 证据索引](../docs/planning/M0-RESULTS.md)为准。

## 已有边界与契约

- [架构第8节](../docs/architecture/ARCHITECTURE.md#8-持久事件状态与恢复)要求本地文件系统上的有限 SQLite WAL spool、`synchronous=FULL`、有界 checkpoint；guest critical event 保留至控制面 committed ACK。
- 来源去重键是 `(attempt_id, stream_id, source_seq)`；控制面在 Run 行锁下分配 `seq`，事件/业务状态/seq 同事务提交；ACK 只能确认最高**连续**提交的来源序号，不能越过缺口。Runner 本地“落盘接收”和控制面“业务提交”是两个层级。[同上](../docs/architecture/ARCHITECTURE.md#8-持久事件状态与恢复)
- [canonical schema](../packages/contracts/schemas/contract.schema.json)的 `SourceEvent` 带 scope、run/attempt/runtime、lease binding、stream、来源序号、时间、type/payload；序号为 `1..9007199254740991`。`EventBatch` 为1–256事件；该数量限制不提供请求字节上限。
- 同一 schema 的 `CommittedAck` 必含 attempt/stream/binding、`level="committed"` 和 `committed_through`（可为0）；receipt 还带 batch_id 和逐事件拒绝。JSON Schema 不能证明连续提交、当前授权、批次实际字节数或数据库事务。[契约语义](../packages/contracts/README.md#关键语义)
- [生命周期契约](../packages/contracts/run-lifecycle.json)禁止 guest 直接决定平台终态；guest ACK/租约到期不证明 VM 停止。[console Module](../apps/runner/CONSOLE.md)只负责原始字节预算，不替代事件 ACK/重传/崩溃恢复。

## 官方资料核验

| 问题 | 核验结论与一手来源 |
| --- | --- |
| WAL-reset 修复 | 官方说明受影响范围为3.7.0–3.51.2，修复为3.51.3+；明确列出的旧分支回补为3.44.6与3.50.7。触发涉及同文件多个连接在不同线程/进程并发写入/checkpoint。单连接设计仍须通过项目修复版本门槛。[WAL §11](https://sqlite.org/wal.html#walreset)、[3.51.3发行记录](https://sqlite.org/releaselog/3_51_3.html) |
| Node24.22.1 内嵌版本 | 官方该 tag 的 `deps/sqlite/sqlite3.h` 定义 SQLite **3.53.4**；`SQLITE_SOURCE_ID` 为 `2026-07-24 19:02:57 bf7c7f30031888f4e796e429ab3978879485813aaca6f641c7b33e4e09459bcc`。这是上游源码事实，不能代替实际发行物查询。[固定版本源码](https://raw.githubusercontent.com/nodejs/node/v24.22.1/deps/sqlite/sqlite3.h) |
| 固定 Node API | v24.22.1 的 `node:sqlite` 为 Stability1.2（release candidate）；使用 `DatabaseSync`，全部操作同步；busy `timeout` 默认0。`statement.close()` 自24.22.0可显式 finalize；`database.isTransaction` 可检查事务状态；`limits` 可收紧行/SQL等资源限制。不能照搬当前其他 major 的 API。[固定版本文档](https://raw.githubusercontent.com/nodejs/node/v24.22.1/doc/api/sqlite.md) |
| 错误识别 | 固定 Node 源码中的 SQLite 数据库错误可含 `code=ERR_SQLITE_ERROR`、数值 `errcode`、`errstr`；部分构造路径可能没有后两项。分类应核验属性并处理未知错误，不能解析本地化消息字符串。[Node实现](https://raw.githubusercontent.com/nodejs/node/v24.22.1/src/node_sqlite.cc)；SQLite 扩展码低8位为主码。[SQLite错误码](https://sqlite.org/rescode.html) |
| 主文件容量 | `max_page_count` 限单个数据库文件的页数；越界增长返回 `SQLITE_FULL`。它不是 DB+WAL+SHM 的总容量限制。[SQLite limits §12](https://sqlite.org/limits.html#max_page_count) |
| checkpoint 与 WAL | 同时只有一个 SQLite 写事务；长读事务会阻挡 WAL 回收。自动 checkpoint 是阈值触发，不能当硬上限；大事务也可能放大 WAL。[WAL §2/§6](https://sqlite.org/wal.html) |
| checkpoint 结果 | PASSIVE 不等待忙读写者且可能未完成；TRUNCATE 在成功返回前把 WAL 截到0；其他 checkpoint 竞争可直接 BUSY。必须检查返回状态和帧数，不能只因 SQL 未抛异常就宣称已清空。[官方 checkpoint API](https://sqlite.org/c3ref/wal_checkpoint_v2.html) |
| 留存大小与同步 | `journal_size_limit` 控制事务结束/重置后的残留 journal/WAL，不能限制活跃 WAL 的增长。WAL+FULL 在每次提交后追加同步；NORMAL 可能在断电/系统崩溃后丢已提交尾部；未知 PRAGMA 可被静默忽略，因此须读回配置。[PRAGMA](https://sqlite.org/pragma.html) |
| spill与重复帧 | 固定SQLite3.53.4的 `walFrames()` 通常覆写本事务已spill的同页，但COMMIT最后一页可再次追加；FULL还可能重复末帧以跨过VFS sector边界。因此不能直接断言每个数据库页严格只占一帧。[固定版wal.c](https://raw.githubusercontent.com/sqlite/sqlite/version-3.53.4/src/wal.c) |
| 禁spill及sector上限 | 同版 `pager.c` 的 `SPILLFLAG_OFF` 阻止pagerStress写盘；COMMIT处理dirty list并排除大于最终DB页数的页；`sqlite3SectorSize()` 将sector上限夹到65536。[固定版pager.c](https://raw.githubusercontent.com/sqlite/sqlite/version-3.53.4/src/pager.c)。`cache_spill`默认开启，可关闭，但须另计内存。[PRAGMA](https://sqlite.org/pragma.html#pragma_cache_spill) |
| EXCLUSIVE与SHM | 在第一次WAL访问前进入EXCLUSIVE可用进程heap替代SHM；该优化只适合确保唯一连接的所有者，不代表其他进程访问也安全。[WAL文件/锁模式](https://sqlite.org/walformat.html#variations) |
| 外部单文件限制 | Linux `RLIMIT_FSIZE`限制该进程能扩展的**单文件**，并非目录总量；越界默认SIGXFSZ终止，也可捕获后令write/truncate返回EFBIG。限额继承fork/exec，线程共享，可从 `/proc/PID/limits`核对；不能在同一Runner进程中误伤其他文件。[Linux man-pages维护者文档](https://man7.org/linux/man-pages/man2/getrlimit.2.html) |
| SQLite 重试 | BEGIN IMMEDIATE 提前取得写事务；BUSY 的 COMMIT 可保持事务活动。FULL/IOERR/NOMEM 等错误可能回滚语句或整个事务，调用方须检查并显式清理事务。[事务与错误响应](https://sqlite.org/lang_transaction.html) |
| 冲突处理 | `INSERT OR REPLACE` 会删除旧行再写入；`OR IGNORE` 可静默跳过违规行。二者均不能直接证明“同键同内容”的幂等成功。[ON CONFLICT](https://sqlite.org/lang_conflict.html) |
| 控制面 durable commit | PostgreSQL 异步提交可在返回成功后因崩溃丢失尾部；关键接收事务不能使用 `synchronous_commit=off`，且要求 `fsync=on`。是否容忍主机/主库损毁及复制故障另行验收。[PG17异步提交](https://www.postgresql.org/docs/17/wal-async-commit.html) |
| 真实持久性边界 | SQLite 依赖文件系统锁及存储兑现 sync；错误复制/删除热 WAL、活动数据库改名、故障存储可破坏恢复。禁止用删除 `-wal` 恢复容量。[官方损坏原因](https://sqlite.org/howtocorrupt.html) |

## 已实现的本地 Module

实现为 [EventSpool](../apps/runner/event-spool/README.md)，只暴露 create/open、append、pendingBatch、applyCommittedAck、stats 和 close。每个 store 固定一个 organization/project/run/attempt/runtime/stream 及完整 lease binding；变更归属必须使用新 store。SQLite 表、statement、iterator 和 SQL 执行能力不向调用者开放。

| 行为 | 已实现范围与限制 |
| --- | --- |
| 固定运行时与路径准入 | Linux、Node24.22.1、SQLite3.53.4及精确source ID；读取实际compile_options，拒绝已知不安全构建标志。核验实际本地ext4挂载、可信祖先、0700目录和0600普通单链接文件；目录和主DB身份冻结。不是同UID恶意进程隔离。 |
| 单一所有者 | 首次WAL访问前进入EXCLUSIVE，唯一同步连接串行执行；statement显式finalize，无开放读iterator。排除第二连接依赖实际SQLite锁，独立进程锁拒绝已通过原生验收。正式Runner须把同步写入放在独立worker/进程，不能阻塞租约或停止监督。验收stdio worker只是私有fixture。 |
| 冻结容量 | 默认2048×4096字节主DB页、4MiB/1024条待确认事件、64KiB单事件、256条/1MiB批次body字节，以及有界forward gap。配置冻结并写入摘要；256为可配置批次条数的最高值。批次body上限不包括传输JSON封装和HTTP头。 |
| SQLite实际设置 | 读回11项连接PRAGMA，使用WAL、FULL、4096页、有限max_page_count、cache_spill=OFF、有限cache、temp_store=MEMORY、mmap关闭和自动checkpoint关闭。EXCLUSIVE模式不使用SHM，异常SHM拒绝。 |
| checkpoint与WAL | 每次mutation之前要求TRUNCATE成功并确认0帧/0字节。COMMIT之后允许保留一段有界热WAL，至下次mutation或正常close回收，从而支持真实进程被杀后的WAL重放。不是“每次写入后也清空WAL”。文件大小和身份在操作中复核。 |
| 精确幂等append | 接受精确UTF-8 JSON字节，校验来源头/归属/序号；完整业务schema由ingress验证。待确认同键同内容幂等，不同内容拒绝。所有已接收未确认payload保留；不使用REPLACE或按优先级淘汰。 |
| 乱序与重传 | 保留缺口，有限物化pendingBatch按source_seq返回独立字节副本。方法本身不实现网络重试、退避、上传并发或断线恢复；这些属于受信uploader。 |
| ACK与回收 | append仅返回本地durable_received。applyCommittedAck验证固定attempt/stream/binding、committed层级、范围和连续性，游标与删除同事务。受信uploader仍须验证控制面身份、外层batch_id关联及真实提交；Guest同形JSON没有ACK权限。 |
| 已回收键 | seq≤committed游标返回event_already_committed，不谎报已不存在payload的内容摘要相同；不保留无限增长的历史摘要表。不允许旧stream身份复用。 |
| 容量拒绝 | 逻辑queue_full或经过回滚、checkpoint及完整状态核对的提交前storage_full拒绝新事件并保留原行；存在有效连续committed前缀时可ACK回收。未补齐的前导缺口可能使满store持续背压，不能伪造ACK或保证总能排空。 |
| 故障处理 | I/O、checkpoint、身份或不确定COMMIT故障锁存后续mutation；mayHaveCommitted表示结果未知。close保留首个错误并始终尝试释放句柄，应用不删失败store；SQLite close自身仍可能checkpoint或删除WAL。重开核对持久事实后按相同键重试。 |
| 停止接线 | 原生验收operator已有spool容量拒绝→精确Resource Broker撤销/停止→独立B写入的路径；双VM真实验收已通过，见下方证据。该接线不能由Module测试替代。 |

### 限定单事务WAL长度的推导

固定SQLite3.53.4、唯一EXCLUSIVE连接、写前成功TRUNCATE到0、禁cache spill、无额外flush/backup/回调SQL/嵌套savepoint路径，且每次mutation只有一次提交时，COMMIT的dirty页集合至多为 `N=max_page_count` 页。固定版[page cache实现](https://raw.githubusercontent.com/sqlite/sqlite/version-3.53.4/src/pcache.c)为每个cache页维护dirty list；上述pager/wal实现提供过滤和写帧路径。这是基于受限调用面的推导，不能推广为SQLite的一般保证。

令 `P=4096`、`F=P+24`。WAL header为32字节，FULL同步可能为跨sector边界重复末帧。实现额外保守预留32帧，采用 `WAL_length <= 32 + (N + 32) * (4096 + 24)`；不采用只算N帧或只在写后stat的证明。单次COMMIT后留下的WAL已经计入该上限，下一次mutation先成功TRUNCATE，不能在遗留WAL上继续累积新事务。启动遗留WAL先核验/恢复并完成checkpoint；任何失败不能绕过前置条件再次写入。

该约束同时依赖固定SQLite源代码和有限调用面。新增spill、reader、backup、savepoint、回调或改变checkpoint策略时必须重审推导并重验。内存须能容纳最坏dirty页和额外SQLite状态；DB/WAL文件长度不等于文件系统记账空间、通用目录配额或宿主总空间准入。原生operator给专用fixture worker设置的RLIMIT_FSIZE只是独立单文件保护，也不能扩大为这些声明。

## 本轮实际结果（2026-11-01）

[最终服务器回归](../docs/operations/event-spool-components-2026-11-01.json)包含严格TypeScript、**34项真实Node SQLite测试、210项Python回归**，全部0失败/错误/跳过。最终Node JUnit SHA-256为 `837a5f0c375dcc5403a4c7bb62c6fa706f4b52ff6d205f5b5c81e06b94500aba`。原34/200结果保留为较早源码检查；新目录配置对应的210项首轮有1个权限夹具失败：operator的umask077将mkdir0750收紧为0700。只在测试夹具显式chmod0750后重跑通过，运行Module和目录准入未放宽。原失败日志继续保留。

[独立SQLite原生验收](../docs/operations/event-spool-native-2026-11-01.json)的9项门槛全部通过，原报告SHA-256为 `10f4fc8f834f9fd999db6cf665b36975fbf603c0076c22ddb79cc2ebf3f76cf3`。12个已登记进程全部退出：7个正常退出、4个SIGKILL、1个第二进程锁拒绝。append和fixture ACK提交后各保留8272字节WAL，再SIGKILL并重开核对全部字节/游标；append和ACK提交前各SIGKILL，重开证明回滚及重试。乱序缺口、精确幂等/冲突拒绝、已回收键拒绝均通过。实际SQLITE_FULL时13条/114287字节仍保留，独立B的新写入通过，有效连续fixture ACK可回收并复用容量。这些ACK来自可信验收fixture，不是真实控制面提交。

[双KVM容量停止验收](../docs/operations/event-spool-kvm-2026-11-01.json)的27项宿主门槛和22项sealed Guest检查全部通过，原报告SHA-256为 `b0bd7628e022a075e2e21f1bc9bc98bc4b69b695b249457cc3f981c42cdd9ba2`。A保留15条/107487字节，第16条8792字节触发真实storage_full、mayHaveCommitted=false，早于1MiB/1024条逻辑预算；全部未确认字节及摘要不变。B在三个阶段、满库后及物理停止后共5次新挑战与独立落盘通过。正式Broker确认精确A的destroy/absence，持久停止终态重构一致、旧Assignment/Grant双拒绝；B watch SIGKILL与持久期限重建链通过。console归档、HMAC journal/anchor/ownership XML、真实SQLite存留内容和数据目录身份均由服务器只读收集器另行核对。基础镜像、既有对象与源码不变，本组VM层精确回收；数据原件分别保留180224/135168字节。两类原生验收均错误/清理错误0，无模型、NIC、TCP监听或管理员改动。

首轮独立SQLite与双VM验收曾因checkout祖先0775被正确拒绝，原报告SHA分别为 `4b5d1dab3a6d46351c65f5a971215a3c976b7ebb9f1304d931a511cdac500842`、`3f20afa25acbe2b32661f3e7671eba7d1e96f857d1fba5993533f026afda8fd0`，保持FAILED。修正为可信operator显式配置安全祖先下的独立0700数据根，冻结根/运行/store目录的身份及配置，退出后对原始数据库生成有限清单；源码、镜像、报告仍在规定位置。共享checkout权限未改，准入检查未放宽，现有不安全目录不自动修复。失败组两块8GiB raw层连同此前80GiB诊断层继续保留；新通过组不替旧失败组作停止/删除证明。

CI工作流固定Node24.22.1和开发依赖锁，保留实际SQLite版本/source ID/compile_options、源码摘要、类型检查标记和实际JUnit计数；缺失、过期、失败、取消或跳过证据均不能通过。摘要验证器12项本地故障回归已通过。**实际GitHub CI证据待本轮源码提交后另行登记**，不能把验证器测试当作已发生的CI运行。

## 尚未通过的生产门槛

| 层级 | 所需真实证据 |
| --- | --- |
| 受信uploader | 控制面身份认证、当前Assignment授权、batch关联、网络重试和有界队列；Guest无法调用ACK入口。Module的固定scope与本地Broker代次检查不能替代这些证明。 |
| 控制面提交 | 真PG17事务rollback/duplicate/并发Run锁、缺口、DB commit后HTTP响应丢失、对象成功但DB回滚；核对事件/投影/seq/ACK一致和有效同步提交配置。 |
| 部署与宿主资源 | 专用服务身份、正式服务的私有数据根配置、宿主总存储准入、通用目录/项目配额、Host OOM与完整egress分别验收；本Module及RLIMIT不能替代这些控制。 |
| 持久性声明 | 独立系统崩溃/断电与存储同步路径验证才可声明对应范围；宿主永久损毁的未提交尾部标轨迹不完整。存储违约导致已提交WAL尾部消失时，本Module没有第二独立receipt ledger检测，不能声称覆盖所有物理损坏。 |

本次接口未改变canonical schema；真正新增或修改对外契约时，仍按[契约工作流](../packages/contracts/README.md)生成并验证OpenAPI和样例。所有验收按[AGENTS.md](../AGENTS.md)先检查实际磁盘空间，日志保存在生成目录之外；私有原始证据保留，公开仅脱敏索引。只在声明范围的验收真实通过后更新任务状态；本记录不将M0-05或整体M0标为ACCEPTED。
