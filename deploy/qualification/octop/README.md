# Octop 独立资格组合

这里只验证可信合成任务的原生 Interface；不宣布生产租户隔离或正式支持。

| Scope | 固定发行身份 | 当前准入边界 |
| --- | --- | --- |
| `octop-harness-sdk-native` | octop-harness 1.0.5 / `76cd38d7b8ffc66c28ba24ad60bbab206a95d3ac` | SDK 的 stream / HITL / history / cancel，必须有独立真实报告 |
| `octop-service-native` | Octop 1.0.3b1 / `890111b7879665840414ca28ab168b05b0db0921` | HTTP/WebSocket 服务、用户认证与运行状态，不能继承 SDK 的资格 |
| `octop-acp-delegated` | 外层服务或 SDK + 指定外部 CLI 版本 | 另行绑定外层与被委派方的原生会话、模型、权限、费用、取消；本配方不启动外部 CLI |

官方 source archive 从固定 codeload commit 获取，必须保存到 `images/sources/octop/`。Harness archive SHA-256 为 `75fcfd10b9d7a654e3092a875d931deb935177f577195f339f895210f09db9e8`；固定源码的 uv.lock SHA-256 为 `1766085847229271984a3f3080fd9d2c7958f843f84c4a185b32ab4240d1c992`。完整服务的部分下载文件不是合格源码，不得用于构建。

## Harness 构建

指定 Linux amd64 服务器已缓存官方 Python3.14.10 镜像，完整 digest 为 `sha256:faf7953db05ee8e060aa9fda8a389e5779cdba26a22e1363379861023e910830`。所有依赖与容器 archive 保存在 `images/`；临时 context/cache 位于 `.state/qualification/octop/`；报告和完整日志位于 `.private/qualification/octop/`。

1. `freeze_octop_lock.py` 在固定基镜像中按 marker、extras、Python 和平台 wheel tags 选择上游 lock 的核心闭包，只下载官方 PyPI wheel 并核对完整 hash/size；不会执行 wheel。
2. `prepare_octop.py --root "$PWD" --scope harness --build` 核对每个 wheel 的官方 PyPI release metadata、完整 hash/size；对照固定 commit 源码逐 Python 文件核对根 Harness wheel。
3. Docker build 使用 `--network=none --pull=false`，`pip install --no-index --require-hashes` 后执行 `pip check`。拒绝缺失依赖、错误 hash 和意外新增包；Harness闭包不需要sdist，完整服务的隔离sdist配方见下节；不安装宿主包。
4. archive 以实际 Docker image ID 命名，保存独立 archive SHA、大小、配方 hash、锁 hash。`qualification_passed` 始终 false，直到独立原生运行证据完成。

示例参数中的 `$PWD` 应为获准服务器仓库根目录。不要把整个仓库或 `.private` 作为 build context。`--download` 是独立的在线元数据诊断入口；发布/复建使用已核验的上游锁闭包，不以其浮动解析结果覆盖锁。

```bash
df -h .
PYTHONDONTWRITEBYTECODE=1 python3 scripts/qualification/test_prepare_octop.py
python3 scripts/qualification/prepare_octop.py --root "$PWD" --scope harness --build
```

## 原生探针

`probe_octop.py` 仅在已冻结的 Harness 镜像中运行。配置/密钥由服务器只读挂载，配置结构为 `{"base_url":"https://example.invalid/v1","model":"qwen3.8-flash"}`；真实配置不得复制到本机。输出目录须为新的私有目录。

探针使用原生 `HarnessAgent` + filesystem backend + SQLite checkpointer；关闭辅助记忆模型、自动重试、搜索、ACP、团队、自动加载 subagent、skills 和额外工具。显式传入官方 `ChatModelFactory.get_chat_model(..., max_retries=0)`；每个合成任务最多 4 个实际模型 HTTP 请求，每次输出限512 tokens，HTTP固定原目标/模型，不跟随重定向；401/403 后同进程阻止后续请求。外层操作者仍须设置总时长上限；供应商在途请求费用不由客户端停止承诺。

| case | 真实验证 |
| --- | --- |
| allow | 原生 read_file 读取提示中没有的随机值；原生 write_file 产生精确 HITL；核对单个 action 的 name、args、内容、无先前副作用后 approve；核输出hash、原生history、SQLite关闭/重开后的历史一致 |
| deny | 同样固定文件操作，以原生 reject 恢复；确认没有输出文件；保留真实工具结果与history |
| cancel | 模型调用本配方显式注册的异步等待工具；确认真实handler开始后调用原生 cancel；等待原生stream返回和handler finally，确认自然完成marker不存在。该范围不等于任意shell/后台进程必停 |

注册的边界 middleware 只限制暴露/允许工具及固定路径，不替换原生文件工具或 Agent loop。等待工具是明确披露的合成取消负载。持久历史重开检查不调用模型，不冒充重启后续聊。

运行必须使用执行用户 UID/GID、只读根、cap-drop ALL、no-new-privileges、2CPU/4GiB/128pids和受限 tmpfs；挂载范围仅本用例工作区、探针、必要配置/key。SDK 无需开放 TCP 端口。完整服务若启动只允许分配的环回端口。每例结束先保存报告/日志，再确认精确任务容器PID0后移除；不做全局prune。

源码与本次证据结论见 [研究记录](../../../research/octop-native-qualification.md)。

## 完整服务的源码依赖构建

服务的固定uv.lock SHA为 `c6b4c1a8f96c37351731b7eab79772fccffbc1ae7aadd501df28be954b52c907`。在固定Python镜像中运行`freeze_octop_lock.py --scope service --allow-sdist`，得到201个发行物（201,851,107 bytes）；脚本对每个目标验证官方来源、完整hash/size和目标平台。下载大文件时可用`finish_octop_download.py`对同一官方URL做最多8段HTTP Range；必须收到精确206/Content-Range且重组后完整SHA匹配，不切换镜像源。

`build_octop_sdists.py --root "$PWD"` 在无网、无凭据的固定基镜像内构建5个源码依赖：evdev1.9.3、oss2 2.19.1、crcmod1.7、aliyun-python-sdk-core2.16.0、esdk-obs-python3.26.6。构建环境固定setuptools80.9.0/wheel0.45.1/packaging26.2，下载时核官方release metadata SHA。源码hash必须属于服务固定锁。每次独立输出目录、完整构建日志和built-wheel SHA，不伪称它们是PyPI发布wheel。

随后使用`prepare_octop_service.py --root "$PWD" --sdist-report <本次私有builder报告>`，将196个官方wheel和5个自建wheel组成完整hash锁，禁网安装及`pip check`后归档。该步骤不会调用模型或启动服务。

`run_octop_service.py --root "$PWD" --image sha256:<实际image-id>` 执行独立的零模型服务HTTP检查：原生`octop run`单worker，在`--network none`容器中仅绑定自身环回10014；无宿主映射、无模型凭据。探针从实际OpenAPI选择路由，核health、SQLite初始化、初始admin、无token/错误token拒绝及有权限访问。服务数据与完整日志保留，确认PID0后仅移除本次容器。该范围不证明真实服务聊天、WebSocket、审批、运行取消或桌面工作台功能。


增加`--model`执行一个独立的原生WebSocket echo与REST历史检查：新建私有服务状态与用户，通过原生API登记唯一获准模型、filesystem工作区和工具禁用设置。使用公开的`octop`入口，qualification wrapper只限制实际HTTP发送，不替换模型响应或服务运行。单worker确保进程级计数；最多4次实际请求、128输出token、固定HTTPS目标和模型、拒绝相同请求重试和重定向、401/403后锁止。请求必须包含本用例随机challenge，背景模型工作被拒绝。LangGraph `max_iters=32`是内部节点预算，不能当作模型请求计数；真实请求上限由发送边界独立执行。

模型用例使用bridge出站但服务仍只监听容器环回10014，宿主无端口映射。最大240秒操作者时限；原生WS收到`done`或`turn.ended`终止事件（本次成功实际为`done`；错误路径出现`turn.ended`），并且REST历史中单个精确assistant响应、无工具历史、无error事件、turn inactive、每个真实请求HTTP200及容器PID0/删除全部满足才判定成功。服务HITL、服务取消、跨进程续聊、ACP委派和生产多租户隔离仍未验收。原始报告与脚本快照只在服务器私有目录，公开完整配方hash见`qualification-recipes.sha256`。
