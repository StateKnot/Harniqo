# Harniqo contracts

M0-02 的机器可读契约初稿，版本 `0.1.0`。它定义接口形状和确定的生命周期，不代表已经存在 API 服务、已通过 Harness/VM 准入或具备生产运行能力。当前状态见 [manifest](manifest.json)。

## 文件与单一来源

| 文件 | 作用 |
| --- | --- |
| [schemas/contract.schema.json](schemas/contract.schema.json) | JSON Schema draft2020-12 的 canonical definitions，所有对象均有封闭属性集 |
| [openapi.routes.json](openapi.routes.json) | OpenAPI 路径、认证和响应源文件，引用 canonical schema 名称 |
| [openapi.json](openapi.json) | 由定义和路由生成的自包含 OpenAPI3.1 文档；不能单独手工改 Schema |
| [run-lifecycle.json](run-lifecycle.json) | Run 状态迁移表、终态与必须满足的业务前置条件 |
| [fixtures/cases.json](fixtures/cases.json) | 人工构造的正向/负向一致性样例；没有真实业务内容或凭据 |
| [../../scripts/contracts/validate.py](../../scripts/contracts/validate.py) | Schema、OpenAPI、样例、迁移表、引用及生成产物一致性校验 |

OpenAPI API 路径仍为 `/v1`，它与契约草稿 SemVer 是不同维度。Schema 采用同文档 `$ref`；生成的 OpenAPI 也只使用同文档引用，不允许网络、任意本机文件或动态引用。`$schema`/`jsonSchemaDialect` 是标准方言标识，不是运行时网络下载指令。

所有路径的认证都显式声明。只有 `/v1/auth/discovery` 是公开的 TLS issuer/client discovery；它不包含机密，桌面仍必须通过系统浏览器、Authorization Code + PKCE 登录。用户 API 用配置 audience 的 Access Token，Runner Interface 用受控注册的 mTLS 身份，两者不能互换。

## 运行验证

锁定与正式验收环境为 Python3.12/Linux x86_64，目前仅此组合经过实测。锁中部分传递依赖要求 Python3.11+，不能根据两个直接工具包的最低版本推导整个环境支持 Python3.10。其他 Python/OS/架构需独立兼容验收。

从仓库根目录执行；先检查磁盘空间。使用项目虚拟环境，不依赖 Codex 特有路径或全局 Python 包。

```bash
df -h .
python3 -m venv .venv/contracts
.venv/contracts/bin/python -m pip install --require-hashes -r scripts/contracts/requirements.lock
.venv/contracts/bin/python scripts/contracts/validate.py --report .private/validation/contracts.json
```

验证默认不改源码、无须网络，显式启用 UUID、真实日历日期和 URI 格式校验。缺少依赖时 exit 2，校验不通过时非零退出；不会把跳过验证报告为成功。报告写入调用者指定位置，并区分契约验证与尚未通过的 live Harness/VM 验证。

某些精简 Ubuntu 环境没有 `ensurepip`，普通 `venv` 命令因此无法安装 pip。若系统已有支持 `--python` 的 pip（本项目验收机为 pip24.0），可使用以下无需 sudo、不会修改系统 Python 包的路径：

```bash
python3 -m venv --without-pip .venv/contracts
python3 -m pip --python .venv/contracts install --require-hashes -r scripts/contracts/requirements.lock
.venv/contracts/bin/python scripts/contracts/validate.py --report .private/validation/contracts.json
```

使用该路径前核对 `python3 -m pip --version` 和 `--help` 是否包含 `--python`。不要改用系统全局安装、`--break-system-packages` 或下载后直接执行未校验脚本。

`requirements.txt` 保留两个直接工具依赖的源需求；[requirements.lock](../../scripts/contracts/requirements.lock) 固定该隔离验收环境解析出的全部 20 个依赖版本及官方 PyPI SHA-256，排除 bootstrap pip。锁包含每版已发布发行文件的 hash，安装采用 wheel-only，避免源码构建临时拉取未锁定的 build dependency；没有 wheel 的目标应拒绝并单独处理，不静默退回源码安装。[来源记录](../../scripts/contracts/requirements.lock.provenance.json) 保存各版本的官方 metadata URL、metadata hash 与文件数量。

每次正式验收记录 Python 版本、`pip freeze --all` 和锁文件 hash。`pip freeze` 仅作环境证据，不直接覆盖公开 lock，以免把本机 bootstrap 或私有文件路径带入仓库。升级直接依赖时重新解析干净环境、从官方发行 metadata 更新完整锁并重跑验收，不能只更改某个 pin。安装需要访问获准 PyPI/镜像，验证本身不需要访问远程 `$ref`。

变更定义或路由后，用标准库生成再验证：

```bash
python3 scripts/contracts/validate.py --write-openapi
.venv/contracts/bin/python scripts/contracts/validate.py
```

生成操作会明确说明尚未执行验证。正向/负向样例不能代替真实 API、数据库事务、队列重复投递、VM 停止及原生协议测试。

## 关键语义

- 身份来自认证结果，不允许用户在 SubmitRun/审批响应中自选组织或 approver。路由 scope 还须与 Access、RLS 和记录归属核对。
- Command 幂等键的作用域为组织/项目、调用者和 operation；请求摘要由服务端对语义规范化后的输入计算。重放前重新授权，同键异内容返回 `IDEMPOTENCY_CONFLICT`。
- Trial 是计划单位，准入前可没有 Run；准入后关联一个逻辑 Run，重试增加其 Attempt。取消未获准 Trial 可以没有 Run。Attempt 不同时持有 trial_id/run_id 两个执行归属。
- 排队时 Run 可以还没有原生会话；进入 running/waiting 状态时必须已有原生绑定和活动 Attempt。终态必须有 ended_at；终态不再向运行态迁移。重跑创建新的逻辑执行或按明确可重试规则创建新 Attempt，不改写历史成功/失败。
- `Run.succeeded` 表示 Agent 执行生命周期完成，业务成功由独立 VerificationResult 决定。Verifier 错误不能同时给业务成功分数。
- SourceEvent 不接受 guest 自选平台 seq。EventEnvelope 在持久提交后添加平台 event_id/seq；原生未识别事件通过受限原始对象引用保留，不把任意 JSON 注入平台状态机。
- committed ACK 是每 `(attempt_id, stream_id)` 的最高连续提交序号。Schema 只能限制其整数形状，不能证明它没有越过缺口；平台必须根据持久记录计算。
- 相同 Workspace 的连续 Run 可以保留状态且只允许一个写者；Assignment/Grant 绑定平台恢复 epoch、工作区、实例、owner、租约代次和 scope。新操作逐次核对撤权与当前代次。
- `StopReceipt` 的 stopping/reconciling 不得声明 stop_confirmed；cancelled 必须声明停止确认。Attempt 的 stop_evidence_id 仅为引用，真实证据必须由受信的资源检查取得，guest ACK 不充分。
- 输入和产物上传收到字节不等于 ready，必须再次完成对象存在/hash/大小校验。用户不能选内部 object key。下载由代理逐次授权，不对用户暴露内部长期签名直链。
- 所有金额使用十进制字符串；unknown 用量不得填 `0`。Server 提交的实际费用不因旧租约拒绝状态写入而丢弃。
- 草稿响应只接受所协商版本的字段，未协商新字段不默默生效；新增稳定字段的协商/降级在 M0 冻结。Handshake 可携带语法正确的未知版本，服务端无交集时返回 `VERSION_INCOMPATIBLE`，不能把它误判为坏 JSON。

## 不能由 Schema 证明的条件

以下是 M1/M2 与准入验证的必选测试，本轮格式校验不宣称它们通过：

| 必须实现的行为 | 对应证明 |
| --- | --- |
| 资源归属、角色、撤权、复合 FK 与连接池隔离 | 真实 Access/RLS 与跨组织/项目负向测试 |
| 授权复核、Run/Command、预留、job 同事务接受 | 真实 PostgreSQL/pg-boss 提交与回滚及重复投递 |
| Assignment 与 Grant 的 scope/owner/epoch/代次匹配，时间窗有效 | 当前平台记录及受信时钟检查，不能只验 JWT 过期 |
| Approval 参数摘要、有效期、当前 Attempt/授权版本、响应唯一性 | 重新查当前动作并原子 CAS；审批者来自认证身份 |
| InputResponse 与当前问题 kind/choices 对应、无重复 question_id | 当前 InputRequest 和业务约束检查 |
| SourceEvent/Envelope 的 run/attempt/scope 一致 | 当前 Assignment 校验及状态事务，guest 不能直接决定终态 |
| contiguous ACK、批次字节限制、快照/last_seq 一致 | 持久流与缺口重传测试、同一数据库快照、入口字节配额 |
| 停止、旧写者 fencing、恢复后的新 epoch | 真 VM/原生服务核对；失联不自动启动第二写者 |
| 对象真实 hash/大小、同一 owner、孤儿回收与撤权下载 | 真对象存储及 DB 失败/回滚、活动流撤权测试 |
| Trial 唯一矩阵、Agent Run 与 Verifier 归属、180 项不丢失 | 实验 manifest 与持久约束、独立评分和预算对账 |

## 冻结条件与资料

M0-02 的初稿和离线一致性验证可以独立完成；正式冻结仍须结合 M0-03/04 的真实原生能力和 M0-05 的真实隔离验证。对外支持状态不能从 Schema 的字段存在推导。

规范和依赖来源：[JSON Schema draft2020-12](https://json-schema.org/draft/2020-12)、[OpenAPI3.1](https://spec.openapis.org/oas/v3.1.0)、[jsonschema4.26.0/MIT](https://pypi.org/project/jsonschema/4.26.0/)、[openapi-spec-validator0.9.0/Apache-2.0](https://pypi.org/project/openapi-spec-validator/0.9.0/)。
