# Octop 原生资格：服务、Harness 与 ACP 委派

研究日期：2026-10-29。范围：M0-04；只使用官方固定源码、官方 PyPI metadata 和真实受控运行。研究、构建、组件、模型、生产隔离结论分别记录。当前还不能将任何 Octop scope 列为正式支持。

## 发行和接口

| Scope | 固定身份 | 原生边界 |
| --- | --- | --- |
| 完整服务 | Octop1.0.3b1，commit `890111b7879665840414ca28ab168b05b0db0921` | FastAPI、用户与workspace服务、WebSocket chat；完整应用依赖 `octop-harness[all]>=1.0.5`、memory/browser/gateway等，不是单个无状态模型代理 |
| Harness SDK | octop-harness1.0.5，commit `76cd38d7b8ffc66c28ba24ad60bbab206a95d3ac` | 装配 DeepAgents/LangGraph 的 SDK；独立的 `HarnessAgent`、stream、history/checkpointer、HITL/cancel |
| ACP委派 | 外层 Octop身份 + 被委派的具体CLI身份 | 入站/出站stdio，不自动得到平台租户授权、凭据、计费或外部CLI停止保证 |

来源：[完整服务 pyproject](https://github.com/TencentCloud/Octop/blob/890111b7879665840414ca28ab168b05b0db0921/pyproject.toml)、[Harness pyproject](https://github.com/TencentCloud/octop-harness/blob/76cd38d7b8ffc66c28ba24ad60bbab206a95d3ac/pyproject.toml)、[服务 API](https://github.com/TencentCloud/Octop/blob/890111b7879665840414ca28ab168b05b0db0921/docs/api.md)、[ACP](https://github.com/TencentCloud/Octop/blob/890111b7879665840414ca28ab168b05b0db0921/docs/acp.md)。两个源码仓库的 MIT 许可不自动覆盖所有二进制、传递依赖、模型服务条款；镜像对外分发仍需单独材料。

固定 Harness 源码的 `ProviderConfig` 支持自定义 HTTPS OpenAI-compatible provider，每个 `ModelConfig` 显式选择 Chat Completions 或 Responses，默认不fallback。`max_output_tokens` 是模型描述信息，实际生成限额须由 host 的 `configurable.model_settings.max_tokens` 传入；SDK和运行时重试是两层，资格任务必须分别关闭。来源：[config](https://github.com/TencentCloud/octop-harness/blob/76cd38d7b8ffc66c28ba24ad60bbab206a95d3ac/src/octop_harness/config/__init__.py)、[model factory](https://github.com/TencentCloud/octop-harness/blob/76cd38d7b8ffc66c28ba24ad60bbab206a95d3ac/src/octop_harness/llm/factory.py)、[model settings](https://github.com/TencentCloud/octop-harness/blob/76cd38d7b8ffc66c28ba24ad60bbab206a95d3ac/src/octop_harness/middleware/model_settings.py)。

`cancel(thread_id)` 设置原生取消事件，`stream` 的迭代器竞争该事件与下一异步chunk，取消时取消并等待 `__anext__`，随后关闭iterator。这是协作式异步终止机制，不保证工具自行派生的外部进程均已停止；实际资格需观察stream结束及工具退出。`aget_history` 从checkpointer读历史。`resume_hitl` 保留thread的请求scope，但这份用户/工具选择上下文是process-local，官方要求host授权resume caller。来源：[agent.py](https://github.com/TencentCloud/octop-harness/blob/76cd38d7b8ffc66c28ba24ad60bbab206a95d3ac/src/octop_harness/agent.py)、[README](https://github.com/TencentCloud/octop-harness/blob/76cd38d7b8ffc66c28ba24ad60bbab206a95d3ac/README.md)。

默认 backend 指向主机根的 local_shell；不能直接继承为多租户运行配置。本组合显式采用 workspace-rooted filesystem backend、只暴露固定文件工具、SDK无监听端口。此配置仍只是可信合成准入，生产隔离要由独立执行目标和凭据代理验证。来源：[backend factory](https://github.com/TencentCloud/octop-harness/blob/76cd38d7b8ffc66c28ba24ad60bbab206a95d3ac/src/octop_harness/backends/__init__.py)。

## 固定构建材料

配方：[README](../deploy/qualification/octop/README.md)、[Dockerfile](../deploy/qualification/octop/Dockerfile)、[上游锁导出](../scripts/qualification/freeze_octop_lock.py)、[完整性与离线构建](../scripts/qualification/prepare_octop.py)、[原生探针](../scripts/qualification/probe_octop.py)。

- 官方 Python3.14.10 linux/amd64：`sha256:faf7953db05ee8e060aa9fda8a389e5779cdba26a22e1363379861023e910830`。
- Harness官方codeload固定commit归档：SHA-256 `75fcfd10b9d7a654e3092a875d931deb935177f577195f339f895210f09db9e8`。
- 同commit `uv.lock`：SHA-256 `1766085847229271984a3f3080fd9d2c7958f843f84c4a185b32ab4240d1c992`。按实际Python/platform marker导出核心闭包，不安装dev/optional extras；锁中包含DeepAgents0.7.21和MCP1.27.1。来源：[上游锁](https://github.com/TencentCloud/octop-harness/blob/76cd38d7b8ffc66c28ba24ad60bbab206a95d3ac/uv.lock)。
- 完整服务配方只接受完整依赖闭包，不能用`--no-deps`、删除`[all]`或强改Python约束来冒充原版运行。

首次服务器执行UID写错造成下载目录PermissionError，未调用模型，已保留原失败日志；修正为实际执行UID后继续。旧浮动解析日志保留，但Harness后续改为上游固定锁，不将动态解析当作发布闭包。完整服务源码archive下载超时，部分字节不能作为有效source输入。

## 验证记录

准备器6项组件测试在本机通过，分别验证官方metadata命中、SHA篡改、size篡改、域名伪装、文件名不符和多METADATA拒绝；仅测试输入校验，不是模型或Harness真实运行证据。

以下记录固定构建与实际执行结果。原始证据保存在服务器 `.private/qualification/octop/`，只发布脱敏hash与范围；真实模型地址、密钥、完整配置不复制到本机。

## 已通过的 Harness SDK 子项（固定93包组合）

服务器从固定上游锁选择93个官方wheel，共31,330,542 bytes；逐项核官方PyPI release metadata、完整SHA/size，根wheel的216个Python文件与固定commit源码一致。Python3.14.10下禁网`--require-hashes`安装和`pip check`通过。发布的完整锁为 [harness.requirements.lock](../deploy/qualification/octop/harness.requirements.lock)，每个发行文件的官方URL/hash见 [wheel provenance](../deploy/qualification/octop/harness.wheel-provenance.json)。

| 材料 | 完整身份 |
| --- | --- |
| 依赖锁 SHA-256 | `f566ec85a38feda0529dd1a9313f5d0289af2aa86207fab81765531e91ed3d71` |
| 实际 Docker image ID | `sha256:714e015d1c1281e1c91bd2ae67f39191b99c408eb7523b62a409831e912a73d5` |
| Docker archive | 496,659,456 bytes；SHA-256 `f5fa07a06006c9f1dca7471bb3135ba22834eb22a7914c1754731b9d2334c470` |
| Dockerfile SHA-256 | `95ffd35d97f4046291f02e17295396fdacdf5d82e5844f58718b7a7830657511` |
| 构建原始证据 | `.private/qualification/octop/20261029T141727Z-b4943e23/` |

服务器6项完整性组件通过；同镜像无网、无凭据的3项真实SDK组件通过：创建Harness与空history、HITL配置、真实被阻塞异步iterator取消后finally。组件容器确认PID0后移除，未触发模型。

Harness部分实际Qwen共4个独立合成任务、9次HTTP请求；首例失败保留，后3例通过。每个任务实际请求数≤4，均无重试；请求目标hash固定、请求model与历史中供应商返回的`model_name`均为`qwen3.8-flash`。返回名称是供应商metadata证据，不独立证明后台权重。

| 用例 | 真实证据 | 原始probe SHA-256 |
| --- | --- | --- |
| `native-allow-20261029T142157Z-4d3cab08` | 2 HTTP200；原生read后write HITL省略输入末尾换行，精确校验拒绝。未批准、无输出；保持FAILED | `4f79da011d21e1ce68a1ded7cc116a0fbd87482e8e0fc38e5ca1544e9ef03d81` |
| `native-allow-20261029T143041Z-70dc17db` | 新用例无尾换行随机输入，3 HTTP200；原生read/write完成、精确HITL approve；6条history、末条assistant stop、零error事件；供应商usage合计6388 tokens | `cc88d563a3556b2cfb60c2924707df5aeeb7ea5dec6a21d0646ea4e1540f3a52` |
| `native-deny-20261029T142601Z-f48b938e` | 3 HTTP200；精确HITL reject后原生write_file结果为error/rejected，输出不存在；6条history、assistant stop、零error事件；6378 tokens | `52ddc8fb82e3452c4c5be060e2ed386403bd219a7beb73d895b984aee9538388` |
| `native-cancel-20261029T142846Z-c11d5767` | 1 HTTP200；显式合成wait工具确已开始，原生cancel后stream在0.006415275秒返回，handler finally执行，无自然完成marker；2条history、零error事件；1388 tokens | `dde77d37e1c9ff899c0d78a7d2e0f6ad5e795e3c4c9328d11614c7ea17b37bc8` |

成功allow输出SHA-256为 `c756c346c19c70c31d01dff353ecbf0acb009980bfdd83242cd2ccaf3eba6923`。随机内容在模型提示中不存在，只从实际原生read工具取得。3个成功用例均关闭原生Agent并重新构造，SQLite历史逐条一致；该检查没有新的模型调用，**不等于容器/进程重启后真实续聊**。取消后的history停在tool_calls，不伪造assistant completed或原生Run终态。

所有运行使用实际执行UID、只读根、4GiB/2CPU/128pids、cap-drop/no-new-privileges和限量tmpfs；SDK没有TCP监听。4个容器均保存日志/state、确认PID0后移除；秘密配置只在服务器从原文件引用并只读挂载，未复制本机。

追加的`offline-supplement.json`只读取原报告/原生history/events，保存其hash、模型、工具结果和usage，未覆盖原始报告。首例保存的实际input/HITL通过7项零模型精确审批回归，后3例保存history通过served-model/工具结果/终态回归。最终探针将这些已验证判定纳入复跑，并为未来运行保存脚本快照；当前4例的原始operator在运行前尚未保存独立脚本hash，不能追补成当时已记录。

## 完整服务的独立状态

固定服务官方uv.lock已完整获取，SHA-256 `c6b4c1a8f96c37351731b7eab79772fccffbc1ae7aadd501df28be954b52c907`。核心服务及Harness[all]选择201个发行物，共201,851,107 bytes。其中5个需从上游固定sdist构建：evdev1.9.3、oss2 2.19.1、crcmod1.7、aliyun-python-sdk-core2.16.0、esdk-obs-python3.26.6。来源：[完整服务固定锁](https://github.com/TencentCloud/Octop/blob/890111b7879665840414ca28ab168b05b0db0921/uv.lock)。

最初`--only-binary=:all:`解析因`esdk-obs-python`没有适配wheel而失败，原日志保留。这个失败只说明**纯wheel配方不足**，不是上游不可部署。已增加 [隔离sdist builder](../scripts/qualification/build_octop_sdists.py)：官方Python固定镜像，setuptools80.9.0/wheel0.45.1/packaging26.2及其完整hash，无网、无凭据、非root、只读根；产出wheel按原源码与构建工具链单独登记。服务完整闭包构建与零模型启动结果见后文；独立原生API模型证据不能继承上述SDK成功。

ACP外部委派没有实际启动。它必须独立绑定外层与被委派CLI的release、native session、model、工具权限、事件、用量和终止证据；本轮任何结果都不能计为被委派Harness自身的原生API资格。

## 独立审阅后的验收工具修复

独立审阅发现旧探针记录`checkpoint_reopen_matches`却未将其并入最终判定，以及旧operator的stop/inspect/log/rm异常可能中断最终报告；prepare helper在name冲突时也缺少归属校验。这些是验收工具缺陷，已修复，不回写前4个模型用例的原始报告。

最终operator在dispatch前原子保存intent，启动结果未知仍进行核对；固定`unix:///var/run/docker.sock`并清除远程daemon/TLS环境覆盖。清理核随机attempt label、原始完整image引用、实际image ID和容器64位ID，后续操作使用容器ID并重复验证。无法确认归属时不停止/删除；抓日志失败时保留已停容器。各步骤异常进入`cleanup_errors`并保留报告；最终成功还要求原生报告成功、wait exit0、PID0、资源已移除且无operator/cleanup错误。重开历史失配现在直接撤销成功。

本机及服务器最终回归：13项operator（含真实main seam的dispatch未知和stop超时仍落报告）、8项准备器（含同名外来容器零stop/rm与setuptools vendor元数据）、11项探针判定。它们均为零模型组件测试；服务器日志分别为`operator-components-final.log`、`component-tests-final-2.log`、`probe-components-final.log`。此前3项真实SDK禁网组件及4个真实模型原报告保持独立，不以这些组件冒充新模型验收。

服务首次201包下载出现TLS EOF，保留原日志；完整SHA通过的已有文件复用，未完成文件重新从同一官方锁定URL取得。5个sdist离线builder已全部成功，输出位于`images/sources/octop/service-built-20261029T144119Z-3799f3f1/`，完整来源/输出hash保存在`.private/qualification/octop/sdist-build-20261029T144119Z-3799f3f1/`。builder本身不等于服务镜像、启动或模型资格。


## 完整服务固定镜像与零模型原生API

196个官方wheel与5个离线构建wheel组成201包完整闭包，在固定Python3.14.10镜像中禁网安装、`pip check`通过。完整服务使用官方PyPI `octop-1.0.3b1-py3-none-any.whl`，19,076,875 bytes，SHA-256 `3801d38b586ff6e3c9aae48f95d3adf8042c8df9d514b3caa7005366aff2f454`；固定commit的完整源码归档未取得，因此不声称服务wheel像Harness一样经过逐Python文件源码等价核对。服务依赖版本选择来自固定commit的官方uv.lock，根wheel与每个发行物均核官方PyPI release metadata。

| 材料 | 身份与服务器相对路径 |
| --- | --- |
| 201包完整锁 | `deploy/qualification/octop/service.requirements.lock`；SHA-256 `37df5b07f0e74e7d9bf996a9f00fd271ab8982ed7c2cdead7390ca6a24f5f4c1` |
| 实际image ID | `sha256:8dbb51439f4ff7597352035e30e7d0099e3ba6e143fd538c9a2f4a4cd0f932c7` |
| archive | `images/containers/octop/service/sha256-8dbb51439f4ff7597352035e30e7d0099e3ba6e143fd538c9a2f4a4cd0f932c7.docker.tar`；867,920,384 bytes；SHA-256 `1506f719e58af360203a113d51c11a2ccae9709fb62d1400efc1f94812547f70` |
| 构建来源 | `.private/qualification/octop/service-build-20261029T152537Z-c207d9b8/report.json`及相邻wheel-provenance、锁、日志；archive旁无独立provenance文件 |
| 公开来源清单 | `deploy/qualification/octop/service.wheel-provenance.json`；明确区分官方wheel与从固定sdist离线构建的wheel |

零模型用例`service-zero-20261029T153632Z-4d4e9692`通过：原生`octop run`、OpenAPI、health、SQLite绑定、初始admin201、无token401/错误token401/有效token200、空providers列表及最终db=true/users_loaded=1。容器`--network none`、非root只读根、无凭据、仅容器环回10014、无宿主端口；operator成功、PID0及删除真、清理错误0。原始证据`.private/qualification/octop/service-zero-20261029T153632Z-4d4e9692/`。此前两个零模型探针失败（OpenAPI路径错误、provider后缀路由歧义）保留，不改写为成功。

服务零模型成功只覆盖真实HTTP初始化/鉴权。其WebSocket协议依据同一固定官方wheel的`octop/api/routers/chat/ws.py`、`models.py`、`history.py`核对；错误路径观察到`turn.ended`，后续真实成功路径为`done`；探针接受这两种终止事件，并另行核对无error、历史与turn inactive，不能仅因收到终止事件判成功。首次模型准备attempt `service-model-20261029T154830Z-bc4b8001`设置`max_iters=4`触发内部图节点上限，实际模型HTTP请求0，PID0/删除真。原始失败保留；更正节点预算32与真实结束事件后，实际模型请求预算仍由独立发送边界限制为4。


## 完整服务原生模型用例与最终冻结

`service-model-20261029T155306Z-2f02540c`实际通过。它通过原生HTTP创建用户、provider和filesystem工作区，原生WebSocket发送单个合成echo请求；1次实际HTTP200，固定请求模型及原生WS `response_metadata`均为`qwen3.8-flash`。收到40个原生WS事件、成功`done`，REST历史2条（user1/assistant1）；随机回执精确匹配、无工具历史、无error、turn inactive。此echo的随机值在提示中公开，与Harness原生文件读取挑战明确不同；不把echo当文件工具证明。

| 最终服务证据 | SHA-256 / 路径 |
| --- | --- |
| 原始探针报告 | `.private/qualification/octop/service-model-20261029T155306Z-2f02540c/service-probe.json`；`a9e4fe349696689a5710aec1cebcea49089c07d4c5d79dc7c16e4471b63f051c` |
| 原始operator报告 | 同目录`operator-report.json`；`53f339afa03b93913fb1448483efd967353fbd69dc3c1dcd046b2dcf30e10998`；qualified/PID0/removed均true，清理错误0 |
| 原始WS事件 | 同目录`ws-events.json`；`5f251e31926780a94191552a38c09702cc8b4d42e45be9ae415103e89a54ad2f` |
| 原始执行probe | 同目录`probe.py`；`cbdbeafdeed7710b3f7b0e3bda2eda2ee388b8302c74b889fc6ae46129274214` |
| 原始发送guard | 同目录`guard.py`；`683fe693f7162176cac71c3e4e904614dc0360a3f8788df44eb03a9130903c5f` |
| 响应内容 | SHA-256 `e3ab3d4423939f7bea1a0eac01b75402849c6d1f54006245cf50ce7c9e41fb5c` |
| 零模型追加校验 | 同目录`offline-service-supplement.json`：记录原报告/WS事件hash、最终probe hash，served_model_matches=true，new_model_calls=0；未覆盖原报告 |

最终探针把served-model精确集合检查并入未来成功判定，SHA-256为`4b840eb8713e7db9306cf4b5f6a55f16e91821b84de5f75ab4cd2aec09530b93`；原用例通过保存事件进行同一检查，不重跑模型。最终全部公开脚本、Dockerfile、锁、来源说明及本文hash列在 [qualification-recipes.sha256](../deploy/qualification/octop/qualification-recipes.sha256)，清单不包含自身。

最终零模型组件计数为38：operator13、准备器8、SDK探针11、service发送guard4、service metadata探针2；本机与服务器均通过。另有固定镜像中3项真实SDK组件已通过；不将组件测试或保存证据的7项审批回归再计入真实模型任务。新增日志为`.private/qualification/octop/service-guard-components.log`及`service-probe-components.log`。本轮实际付费任务总计5、实际请求10（Harness4任务9请求、服务1任务1请求），各任务≤4请求；0请求准备失败不计入付费任务，原失败保留。

最终服务器只读核对：带`io.harniqo.qualification=octop`标签的剩余容器0，宿主10014监听0，可用空间约2.5TiB。保留原始报告、SQLite状态、镜像归档和已验证依赖cache；不删除用户数据、不做全局prune。

仍未验收：服务HITL/服务原生取消、跨进程真实续聊、ACP外部委派、桌面工作台、多租户隔离与完整依赖再分发许可证材料。SDK已有的HITL/cancel证据不能转移给服务或被委派CLI。服务完整源码archive未取得，PyPI根wheel有固定官方发行hash但没有逐文件commit等价证明。生产级发布需按这些独立gate继续验收；本轮仅证明可重建的固定候选组合及上述原生接口范围。
