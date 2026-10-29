# Harniqo 架构技术栈核验

核验日期：2026-10-29。仅使用项目官方文档、规范与源码；本轮未安装、构建、调用模型或执行生产压测。下述版本是架构基线建议，M1 实施时锁定受支持的具体补丁、依赖锁与镜像 digest，并运行兼容验收。

## 1. 推荐基线与已确认约束

| 项目 | 基线建议 | 已确认事实与适用范围 |
| --- | --- | --- |
| 服务端 Node.js | 24.x 的受支持补丁 | 官方计划中 24 于 2026-10-20 进入 Maintenance，EOL 为 2028-04-30；仍可作为生产基线。26 于 2026-10-28 进入 LTS，不能把 24 称为最新 Active LTS。应在 EOL 前完成升级验收。 |
| Fastify | 5.x | 官方 LTS 表列出 Node 20/22/24/26；5.x 的 LTS 结束日期当前为 TBD。兼容关系按实际依赖锁验证。 |
| PostgreSQL | 17.x 的受支持补丁 | 官方列 17 受支持至 2029-11-08；当前政策是每个 major 支持五年。无需为采用可维护数据库而追逐最新 major。 |
| pg-boss | 12.x，锁定实际 schema/补丁 | 当前包要求 Node >=22.12.0；官网列 PostgreSQL >=13。Node24 + PG17 满足所述最低要求，实际工作负载仍需验收。 |
| Desktop Electron | 实施时最新受支持稳定 major；当前可选 45.x | 官方仅支持最新三个稳定 major；当前 45 已于 2026-10-20 稳定发布，43/44 也在支持窗口内。桌面内置 Node 由 Electron 管理，服务端 Node 版本不替代它。 |
| 私有 S3 对象存储 | SeaweedFS 社区版，或客户现有受验收 S3 服务 | SeaweedFS 主仓 Apache-2.0；S3 gateway、Filer 元数据和 Volume 数据都有持久化/恢复职责，不是只备份业务 PostgreSQL 就能恢复全部对象。 |
| OIDC | 企业现有 issuer；独立部署默认 Keycloak | Keycloak Apache-2.0，官方提供容器与生产部署文档；当前支持 PostgreSQL17。使用生产模式、固定版本、TLS/hostname 和持久数据库。 |
| Secret | 独立部署默认 OpenBao | OpenBao 主仓 MPL-2.0；支持自托管 KV-v2 与 Raft 集成存储。Harniqo 的 MIT 不改变该依赖许可。 |

版本事实来源：[Node 发布与生产支持](https://nodejs.org/en/about/previous-releases)、[Node 精确日程](https://raw.githubusercontent.com/nodejs/Release/main/schedule.json)、[Fastify LTS](https://github.com/fastify/fastify/blob/main/docs/Reference/LTS.md)、[PostgreSQL 支持周期](https://www.postgresql.org/support/versioning/)、[pg-boss 要求](https://pgboss.io/)、[pg-boss package.json](https://github.com/timgit/pg-boss/blob/master/package.json)、[Electron 日程](https://releases.electronjs.org/schedule)、[Electron 支持政策](https://www.electronjs.org/docs/latest/tutorial/electron-timelines)。

## 2. 队列、事务与租户数据

pg-boss 明确采用至少一次交付：Worker 崩溃或执行超出期限后，处理函数可能再次运行。它允许将 job 操作放进调用者的数据库事务，官方提供 Kysely 等事务 Adapter。Harniqo 可以把 Run 接受、预算预留与任务登记放在同一数据库事务/连接中；在业务事务外单独 `send()` 不具有该原子性。[交付语义](https://pgboss.io/introduction)、[事务 Adapter](https://pgboss.io/api/adapters)。

架构含义：job 只携带不含凭据的资源身份。处理 job 前读取 Run/Attempt 与真实进程身份，进行幂等和租约核对；外部模型调用、文件修改、第三方工具副作用不因数据库事务而变成恰好一次。队列重试不是自动重启 Agent 的授权。

PostgreSQL RLS 在启用后无匹配 policy 时默认拒绝，但超级用户、`BYPASSRLS` 角色始终绕过，表 owner 通常也绕过。`FORCE ROW LEVEL SECURITY` 可以约束 owner；`TRUNCATE` 和引用完整性检查另有边界。[PG17 RLS](https://www.postgresql.org/docs/17/ddl-rowsecurity.html)。

架构建议：运行账号与迁移 owner 分开，运行账号无 superuser/BYPASSRLS；带租户的表启用并强制 RLS，业务接口仍做资源授权。连接池的租户上下文在显式事务内用 `SET LOCAL`/等价参数化调用设置，缺失上下文拒绝；不使用会污染下一次借用连接的长期 `SET SESSION`。`SET LOCAL` 只在当前事务有效。[SET LOCAL 语义](https://www.postgresql.org/docs/17/sql-set.html)。预算并发扣减用行锁/条件更新与唯一记录保证，涉及跨表一致性的并发用例按所选隔离级别测试。[事务隔离](https://www.postgresql.org/docs/17/transaction-iso.html)。

## 3. Electron 安全、凭据与更新

官方建议对 renderer 启用 context isolation 和 sandbox，禁用 remote content 的 Node integration，限制导航/新窗口，校验 IPC sender，限制外部 URL，采用 CSP。Harniqo 的本地 UI 可以调用有限 preload Interface；模型结果、Markdown、HTML 与产物预览是独立的不可信输入，不能获得任意 Electron/文件系统能力。[安全清单](https://www.electronjs.org/docs/latest/tutorial/security)。

`safeStorage` 的 OS 保护不同：macOS Keychain、Windows DPAPI、Linux 桌面 Secret Service 等。Linux 无 secret store 时可能进入弱 fallback；旧同步路径的 `basic_text` 使用硬编码密码，不能把“加密 API 可用”视为已取得安全持久存储。当前 Electron45 文档把同步 API 标为弃用并提供异步 API。[safeStorage](https://www.electronjs.org/docs/latest/api/safe-storage)。

Harniqo 应将凭据管理收敛到主进程中的一个 Module，按最终 Electron 版本验证实际 provider；没有满足策略的安全存储时，刷新凭据仅保留内存并要求后续重新登录，提示用户启用受支持 keyring。不能调用 plaintext fallback 来兑现“记住登录”。macOS 签名一致性和 Windows 同用户空间的保护边界需纳入真实系统验收。

Electron 内置 autoUpdater 只支持 macOS/Windows，Linux 建议走发行版包管理。macOS 的该更新机制要求签名。Harniqo 应明确三端发行/更新渠道、版本兼容、包校验与失败恢复，不声明一个内置 API 覆盖三端。[autoUpdater](https://www.electronjs.org/docs/latest/api/auto-updater)。

## 4. 私有部署基础依赖

SeaweedFS 的 `weed s3` 是连接到 Filer 的无状态 S3 gateway；Filer 维护文件目录元数据，Volume 保存对象数据。它可独立部署，社区主仓使用 Apache-2.0。Harniqo 的 S3 Interface 只依赖被验收的子集，例如 Put/Get、Range、multipart、元数据与删除，不能根据“S3-compatible”推断所有 AWS 语义均相同。[项目与架构](https://github.com/seaweedfs/seaweedfs)、[许可证](https://github.com/seaweedfs/seaweedfs/blob/master/LICENSE)、[S3 gateway](https://github.com/seaweedfs/seaweedfs/wiki/Amazon-S3-API)、[Filer metadata stores](https://github.com/seaweedfs/seaweedfs/wiki/Filer-Stores)。

部署建议：私有版提供经过测试的 SeaweedFS 配置，生产禁用匿名写入；SaaS 可以用同一 S3 Interface 连接已验证托管对象存储。数据恢复清单必须覆盖 Volume 数据、Filer 元数据、相关集群状态与配置；“单进程快速启动”不等同跨节点高可用。API/执行身份按 bucket/prefix/对象范围授权；受限产物下载的最终授权仍由 Harniqo 执行。

Keycloak 提供可自托管的容器，主仓 Apache-2.0；官方支持配置列出 PG17。生产 `start` 与开发 `start-dev` 的安全默认不同，生产不能照抄开发模式。私有版可以接客户已有 OIDC，不依赖 Harniqo 官方云身份服务。[许可](https://github.com/keycloak/keycloak/blob/main/LICENSE.txt)、[容器部署](https://www.keycloak.org/server/containers)、[生产模式](https://www.keycloak.org/server/configuration)、[支持配置](https://www.keycloak.org/server/supported-configurations)。

OpenBao 主仓 MPL-2.0；Raft 集成存储有官方生产与 HA 说明。KV-v2 提供版本化 Secret 与 ACL，软删除不等于永久销毁。Harniqo 数据库只存秘密引用与版本；执行端按工作负载领取所需秘密，不把 OpenBao 管理令牌交给 VM 中的 Agent。[许可](https://github.com/openbao/openbao/blob/main/LICENSE)、[Raft 存储](https://openbao.org/docs/configuration/storage/raft/)、[KV-v2](https://openbao.org/docs/secrets/kv/kv-v2/)。

生产配置还须落实初始化/unseal、密钥保管、TLS、权限、备份与恢复顺序。单节点开发实例不证明高可用；多节点 Raft 也不替代独立备份。这里是架构建议，实际部署结果在 M0/M1/M7 验收记录中给出。

## 5. 实施时锁定与验收

Runner 本地 spool 的 SQLite WAL 必须使用本地文件系统，并明确同步与 checkpoint 策略。`synchronous=FULL` 每次提交同步 WAL，不能把 `NORMAL` 的掉电持久性假设等同于 FULL；官方另披露 WAL-reset 问题已在 3.51.3 及部分维护分支修复，需核对实际绑定的 SQLite 版本，而不只看 Node 包版本。[SQLite WAL](https://www.sqlite.org/wal.html)

Kysely 是类型化 SQL builder，架构选择它用于显式事务、查询与迁移，不将其当成绕过数据库约束的实体映射层。[官方项目](https://github.com/kysely-org/kysely)。JSON Schema 与 Fastify 官方 type provider 可以收敛请求验证和 TypeScript 推导；服务端只注册来自受控发行物的 Schema，不接受用户内容作为动态执行代码。[Fastify Type Providers](https://fastify.dev/docs/latest/Reference/Type-Providers/)

- 固定实际软件版本、运行镜像 digest、归档 SHA-256、Schema 与依赖锁；记录上游支持窗口，定期安排安全补丁和 major 升级验证。
- 版本门槛以官方当前发布/支持信息重新核对，不能把本文日期的观察值当成永久“最新版”。
- 最少验证：三端安全存储/IPC/更新，真实 PostgreSQL RLS 与连接池上下文，业务与 job 同事务回滚，重复交付与失联执行，S3 子集与完整恢复，OIDC 撤权，Secret 领取/轮换/unseal/恢复。
- 所有拟分发依赖保持自身 LICENSE/NOTICE 和来源，Harniqo MIT 仅覆盖 Harniqo 有权授予的代码与文档。
