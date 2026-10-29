# Hermes 原生接口准入组合

这是 M0-03 的 **可信原生接口准入容器组合**，身份固定为源码 `a382af23f4c067d23463b5781958684c2cda82bc` 加 Harniqo 受控工具 profile；不是上游完整产品镜像，也不是 SaaS 隔离通过的证据。生产时相同运行包仍须进入已授权 KVM，另验身份、网络、文件、资源、取消和恢复边界。

本目录仅提供构建与启动材料。写入材料、静态检查、合成工具自测、成功构建、健康接口、真实模型工具回执是不同证据；不能用前几项替代真实 Harness 准入。未运行前不填 passed。

## 固定身份与 primary 核验

| 项目 | 核验及处理 |
| --- | --- |
| 源码 | [固定 commit](https://github.com/NousResearch/hermes-agent/tree/a382af23f4c067d23463b5781958684c2cda82bc)，GitHub codeload HTTPS archive 的本次观测 SHA-256 为 `e4f29f1b0c6aecc9c8867166fc620835047bfc5d76d2c634b823ccc795238a1e`；脚本严格校验。该校验是固定输入记录，不冒充上游签名证明 |
| tag 差异 | [v0.22.5 tag ref](https://api.github.com/repos/NousResearch/hermes-agent/git/ref/tags/v0.22.5) 指向 annotated tag `56b6a31fededdf7364e1a0a79548d144e7fd81a3`；[tag 对象](https://api.github.com/repos/NousResearch/hermes-agent/git/tags/56b6a31fededdf7364e1a0a79548d144e7fd81a3) 实际 peel 到 `bf0203a794a07aac3ff764972ab9087baa4e4f4e`，并非本轮 commit。因此仅命名 `git-a382af23f4c0`，不称 v0.22.5 原版 |
| Python | 固定源码 [pyproject.toml](https://github.com/NousResearch/hermes-agent/blob/a382af23f4c067d23463b5781958684c2cda82bc/pyproject.toml) 注明仅正式支持3.14，3.11–3.13声明仅为旧安装升级桥；[uv.lock](https://github.com/NousResearch/hermes-agent/blob/a382af23f4c067d23463b5781958684c2cda82bc/uv.lock) supported marker 为 Python≥3.14且<3.15 |
| 基镜像 | 仅 Linux amd64 原生构建。先从 Docker Official `python:3.14-trixie` index 解析 amd64 manifest digest、核对响应内容 SHA，再使用不可变 `python@sha256:…`；解析结果存在 `.state/qualification/hermes/python-base-resolved.json`，复建不悄悄更新 |
| 依赖 | 在线分支沿用原始 `pyproject.toml` 与 `uv.lock` 执行 frozen/wheels-only sync；显式 `--wheelhouse` 分支由同一原锁冻结导出完整闭包，再执行 hash-required offline pip sync，保留原锁override。两者都选core+sms、no-default-groups并省略根项目安装。`sms` extra 在该源码仅引入 `aiohttp==3.14.5`，供原生API adapter使用，不启用短信平台；不开all/messaging/desktop extras；缺wheel时失败，不解锁或降Python绕过 |
| 安装器 | 默认 [pm/lock.json](https://github.com/NousResearch/hermes-agent/blob/a382af23f4c067d23463b5781958684c2cda82bc/pm/lock.json) 的 uv0.13.5 Linux x64 官方 release tar，SHA `f68d291c75783122dad58ab77def0e13f3233f358731db5d6d7e8715603b928f`。也可明确选同版官方PyPI wheel，独立URL/SHA见下文；无自动换源、无远程安装shell；安装后移除uv可执行文件 |
| SQLite | 保留上游 [Dockerfile](https://github.com/NousResearch/hermes-agent/blob/a382af23f4c067d23463b5781958684c2cda82bc/Dockerfile) 的 SQLite3.53.4源码、SHA及编译选项，避免旧系统库的 WAL-reset 问题；构建断言 linked version 和 FTS5 trigram。完整 Python base 提供编译器，本配方不执行浮动 apt install |

与上游完整 Docker 配方的差异：省略桌面、Chromium、Node/TUI、IM、s6和包管理自动准备；只运行原生 gateway/API/managed worker。Python lock不改。上游主循环、授权、审批、会话、SSE、取消实现不改；不会构造假审批回执。

上游源树仅增加只读 `tools/harniqo_qualification.py`（内容与本目录 `entrypoint.py` 相同），并用上游 `scripts/write_install_stamp.py` 生成显式 commit、dirty、external-update 的组合身份。源码工具注册使用原生 `tools.registry.register`；不修改其发现器。每个原生 managed worker 都会重新 exec，因此只在父进程注册工具无效，模块必须随运行包可发现。配方、工具、原始锁 SHA、安装分发版本清单都进入镜像 provenance。

原生 `file` toolset 的读操作并不提供此轮所需的固定工作区读取边界；即使设置 `HERMES_WRITE_SAFE_ROOT`，同 UID 的工具仍可能读取secret。早期两工具组合仅注册 `qualification_write_file(nonce, content)` 与 `qualification_read_file(nonce)`，不接收模型路径，不执行 shell；两者只读写 `/data/workspace/qualification-result.json`，内容最多2048UTF-8字节。写入后随机生成48hex receipt，读回核对 nonce、receipt 和 SHA。验收记录必须带该组合的 profile/工具 SHA，不能声称验证了原生通用 file、terminal 或审批能力。

后续四工具组合保留上述写读工具，并新增 `qualification_wait` 与 `qualification_approved_write`：前者最多等待45秒、检查上游线程中断标志并记录实际 PID/start_ticks；后者通过上游 `request_tool_approval` 人工闸门，只在明确批准后写固定合成文件。它们验证原生中断和审批协议，仍不代表通用 shell/file 工具策略已验收。新旧组合按 image ID、recipe SHA 分开，原始失败不覆盖。[生命周期探针](../../../scripts/qualification/HERMES-LIFECYCLE.md)

四工具组合同时在每个原生 Gateway/Worker 的实际 httpx send 处安装只记录 hash 的传输观察器，保持原请求与主循环实现。模型锁接口、canonical 接口的差异以及实际 endpoint 证据见 [调查结论](../../../research/hermes-native-model-evidence.md)。`transport_audit.py` 与入口文件均记录在 recipe SHA 中；它属于明确版本化的 Harniqo 验收组合。完整启动、私有凭据挂载、进程截止、日志留证和归属核对后的回收使用 [验收操作者](../../../scripts/qualification/HERMES-OPERATOR.md)。

## 构建

在指定 Linux x86_64 Docker 宿主工作区运行；系统 Python3.12只执行标准库 builder，容器运行时为3.14。需要已安装 Docker、可访问官方源、工作区和 Docker 实际存储文件系统均≥60GiB可用。执行前读取真实 `df`。脚本不安装宿主软件、不接触模型或凭据、不监听端口。

```bash
cd /path/to/harniqo-workspace
df -h .
python3 scripts/qualification/build_hermes_image.py --root "$PWD"
```

可先用 `--prepare-only` 下载和校验公开输入、解析基镜像；它不构建镜像。源 archive、uv/SQLite公开缓存和临时 context只位于 `.state/qualification/hermes/`；不将源码 vendor 到 Git，不复制工作区根目录、`.private`、`.env` 或现有虚拟环境进 context。context结束后清理，公开下载缓存保留复建。

依赖安装要求现有 builder 支持 BuildKit `RUN --mount=type=cache`；不支持则停止，不切回忽略挂载的构建方式，也不自动改 daemon 或新建 builder。固定源码、`uv.lock`、uv0.13.5 和原有 frozen/wheels-only 依赖选择保持不变。[Docker cache mount](https://docs.docker.com/reference/dockerfile/#run---mounttypecache)。

uv 缓存挂载 ID 为 `harniqo-hermes-a382af23f4c067d23463b5781958684c2cda82bc-linuxamd64-uv0.13.5`，包含源码 commit、平台和安装器版本，`sharing=locked` 使同 ID 的并发构建等待当前使用者。挂载仅在依赖 `RUN` 内映射到 `/tmp/uv-cache`，使用 `UV_LINK_MODE=copy` 将安装文件复制到虚拟环境；缓存内容不进入镜像层，不再对挂载执行 `rm`。完成的下载可供同组合失败重试复用；缓存可能被 builder 的 GC 回收，冷缓存仍须正常完成安装，不能把缓存命中当作来源或 hash 验证的替代。[uv Docker 缓存用法](https://docs.astral.sh/uv/guides/integration/docker/#caching)。

物理缓存由所选 builder 的 BuildKit 存储管理，不是宿主 `/tmp/uv-cache`，也不在工作区 `images/` 或 `.state/`。本地 Docker driver 使用 daemon 存储；`docker-container` driver 使用其 BuildKit 存储卷，位置不能互相假定。用 `docker info --format '{{.DockerRootDir}}'`、`docker buildx ls` 和 `docker buildx inspect "$HARNIQO_BUILDER"`（已存在的 builder 名称，不加 `--bootstrap`）核对实际后端，把确切位置、cache ID 和用途记入私有环境记录。[Builder drivers](https://docs.docker.com/build/builders/drivers/)。

本任务缓存保留供当前准入和同组合重试；连续 7 天未用、本任务缓存超过 20 GiB，或可用空间低于构建要求的 60 GiB 时先核对并评估清理。清理前保存 attempt/日志/provenance，确认无活动构建，再用 `docker buildx du --builder "$HARNIQO_BUILDER" --filter type=exec.cachemount --verbose` 核对归属、record ID、使用时间、共享和可回收状态。只对确认属于本任务且已不在使用的 record 做精确 `id` 过滤清理；挂载 ID 不等于 BuildKit 的 record ID。无法确认映射或仍在使用则保留，不直接删除 daemon 存储，不使用全局 prune，不碰其他任务缓存、镜像、容器、卷或原失败记录。清理后重新读取实际文件系统 `df`，不把共享缓存累计大小当成释放量。[du](https://docs.docker.com/reference/cli/docker/buildx/du/)、[prune 过滤](https://docs.docker.com/reference/cli/docker/buildx/prune/#provide-filter-values---filter)。

当官方GitHub release传输不可用时，可显式加 `--uv-source pypi-wheel`。该选项只选择同版本安装器的另一种官方发布格式，不修改Hermes的 `uv.lock`、`pm/lock.json` 或uv版本，也不宣称两个archive或其可执行文件逐字节相同。默认仍为 `--uv-source release-tar`，失败不会自动切换。

```bash
python3 scripts/qualification/build_hermes_image.py --root "$PWD" --uv-source pypi-wheel
```

备用输入通过 [官方PyPI0.13.5 metadata](https://pypi.org/pypi/uv/0.13.5/json) 及 [版本文件页](https://pypi.org/project/uv/0.13.5/#files) 核验，为 `uv-0.13.5-py3-none-manylinux_2_17_x86_64.manylinux2014_x86_64.whl`（Linux glibc≥2.17、x86_64），17,211,825bytes、未撤回。固定 [wheel下载源](https://files.pythonhosted.org/packages/3a/ad/3c2a81d66d1839063662e5b6c0002ec31f8ee987dbc5295747627ca6160a/uv-0.13.5-py3-none-manylinux_2_17_x86_64.manylinux2014_x86_64.whl) SHA-256为 `fa8d26e08e34c29c0c4e820cf3181757086d547240b4f4d31ff1d8bd810ca946`，这是wheel自身的官方hash，不能冒用原release tar的SHA。

builder将wheel缓存在 `.state/qualification/hermes/<原始wheel文件名>`，校验其完整SHA、大小和version/platform metadata，再只解出固定 `uv-0.13.5.data/scripts/uv`；不运行wheel中的Python、安装钩子或pip。拒绝重复/越界/链接/加密条目和超出大小限制的archive。provenance及私有attempt同时记录实际选择、格式、来源、archive SHA、解出binary SHA和原上游tar pin。它仍须在真实容器构建中验证能运行，获取或解包成功不等于Harness通过。

### 显式离线依赖输入

已准备好目标平台的官方wheel闭包时，显式传入依赖wheelhouse；`--uv-source`只决定安装器自身的格式，与这74个运行依赖分开记录。wheelhouse不要混入uv安装器wheel、其他项目包、凭据或虚拟环境。路径是占位符，替换为本次已验证目录。

```bash
python3 scripts/qualification/build_hermes_image.py \
  --root "$PWD" --uv-source pypi-wheel \
  --wheelhouse /path/to/verified-hermes-wheelhouse
```

不传 `--wheelhouse` 仍走在线frozen sync，不会在网络失败时自动改模式。[builder](../../../scripts/qualification/build_hermes_image.py)逐一按原 `uv.lock` 校验输入wheel文件名、大小和完整SHA，拒绝符号链接/来源不在锁中的文件及超限输入，将只读副本带入context；`--prepare-only`在此阶段可核验输入，但不证明目标闭包安装成功。Docker依赖阶段以只读bind挂载wheelhouse，按下面顺序使用同一锁和选择，不修改包版本或上游override：

```bash
uv export --frozen --offline --all-packages --no-default-groups --extra sms \
  --no-emit-project --no-emit-package hermes-agent --format requirements-txt \
  --output-file /tmp/harniqo-frozen-requirements.txt
uv venv --python /usr/local/bin/python3 .venv
UV_CONCURRENT_DOWNLOADS=4 UV_CONCURRENT_INSTALLS=4 \
uv pip sync --offline --require-hashes --no-index \
  --find-links=/opt/harniqo-wheelhouse --only-binary=:all: \
  --python=.venv/bin/python /tmp/harniqo-frozen-requirements.txt
```

requirements是 `pip sync` 的位置参数，不能照搬 `pip install -r`。`pip sync`把输入视为完整闭包，不重新补齐传递依赖；导出仍含跨环境markers，必须在真实目标Python环境求值，并用该环境的 `sys_tags()` 从原锁选择兼容wheel。任何激活的direct URL/Git或缺失兼容wheel都须单独处理或失败退出，不静默漏包、改源或构建sdist。[uv0.13.5 export定义](https://github.com/astral-sh/uv/blob/0.13.5/crates/uv-cli/src/lib.rs#L4482)、[pip sync闭包语义](https://github.com/astral-sh/uv/blob/0.13.5/crates/uv-cli/src/lib.rs#L795)、[markers](https://packaging.pypa.io/en/stable/markers.html)、[tags](https://packaging.pypa.io/en/stable/tags.html)

2026-10-29的独立前置实验使用固定base digest、uv0.13.5、Python3.14.10/Linux amd64及同一core+sms选择。86条导出要求中12条marker不适用，74个目标包均有兼容wheel；全部49,184,573bytes下载自原锁的官方 `files.pythonhosted.org` URL，按原锁与导出hash双重匹配并校验完整文件。慢文件可经有界8段Range获取，最终仍必须核完整SHA，不能把分段大小或下载完成视作完整性证明。清单与每文件来源、大小、SHA记录在wheelhouse manifest及私有报告；当前原锁SHA为 `ae748e8063f636357ee3c5a623f95eeb1dab388b13a199f62c174388901f5a32`，本次wheelhouse manifest SHA为 `3a144732d405b1fe0387f4f9e2f22171db19ad6eeb7a79cf7eb6fbc174ea4977`。

| 实际用例 | 结果及含义 |
| --- | --- |
| 原registry锁、空缓存，`uv sync --frozen --offline --find-links`，tiny six用例 | 失败，仍要求原registry URL的缓存；同hash本地wheel不是该锁来源的自动替代。uv的registry缓存匹配包含index URL和文件名。[固定实现](https://github.com/astral-sh/uv/blob/0.13.5/crates/uv-distribution/src/index/registry_wheel_index.rs#L53) |
| 真实Hermes冻结导出后 `uv pip install --require-hashes` | 失败；重新求解包metadata时，browser-harness0.1.13要求Pillow12.3.0，与上游明确override的13.0.0冲突。不能降Pillow或改锁躲过 |
| 相同74包闭包，fresh-cache/venv、同Python3.14 base、容器禁网，`uv pip sync --require-hashes` | 成功，74个安装分发版本逐一与目标选择一致，原锁SHA不变；native API与本组合工具模块import成功，未启动gateway或模型。首次64pids/2GiB/默认并发的实验90秒超时；128pids/4GiB/2CPU并限制安装/下载并发为4的独立重试成功。多项条件同时改变，不把它归因为单一条件 |
| 原始 `uv pip check` +上游override过滤 | raw返回1，1项冲突；原 `pm/pip_check.py` 按锁内Pillow13 override完整识别，unexpected=0。只能记“已知override冲突1、非预期0”，不能记raw检查全过。[上游helper及原理](https://github.com/NousResearch/hermes-agent/blob/a382af23f4c067d23463b5781958684c2cda82bc/pm/pip_check.py) |

冻结导出默认header会记录命令及output-file路径。同一requirements内容换输出路径后，原始文件SHA可能变化；追踪原文件时保存其SHA，比较依赖选择时另核规范化的requirement、marker与hash集合及实际目标wheel，不用header路径差异推断版本改变。清单必须保存原锁SHA、target markers/tags、选择、每文件来源与hash、实际安装版本和失败记录，不能只保存一个目录名。

这些结论仅构成离线依赖与构建前置证据。独立import实验尚使用Python base的SQLite3.46.1；最终配方的固定SQLite3.53.4、完整镜像构建、归档、原生会话/工具/流式/取消/恢复，以及KVM执行边界仍由对应真实用例验收，不从74包同步成功推导Harness通过。原始失败与成功报告保存在私有 `.private/qualification/hermes-offline-inputs/<closure-sha256>/`，公开文档不写实际宿主路径或凭据名。

输出为 `images/containers/hermes/git-a382af23f4c0/sha256-<docker-image-store-id>.docker.tar` 及 `.docker.tar.provenance.json`。格式明确为 Docker archive；`image_id` 保留Docker构建返回的可运行引用，兼容字段 `image_digest` 采用同值，`digest_kind=docker-image-store-id`。不同image store的ID含义可能不同；不能一概认定为config或已发布registry manifest的摘要。归档文件SHA-256另行记录，不与该ID混用。按provenance的精确 `image_id` 启动，不依赖可变本地tag。

provenance只从现有inspect结果复制经过形态/长度校验的 `inspect_image_id`、`image_descriptor`（mediaType、digest、size及可选platform）和 `repo_digests`，不复制Config/Env、annotations或其余字段。`image_config_digest` 与 `registry_manifest_digest` 均留null，直到有独立的manifest/config字节或registry核验；RepoDigests存在本身也不证明本地组合已发布到registry。[Docker containerd image store](https://docs.docker.com/engine/storage/containerd/)支持manifest/index目标，[官方构建元数据](https://docs.docker.com/reference/cli/docker/buildx/build/#write-build-result-metadata-to-a-file---metadata-file)也将config digest与descriptor digest分开。provenance继续记录原始源码、base index/platform digest、lock SHA、recipe/tool SHA、实际Python/SQLite/分发依赖版本；构建成功仍 `qualification_passed:false`。

如 SHA、版本、TLS、wheel、SQLite、导入或空间检查失败，保留准确失败原因，停止准入；不要换成 master、`pip install` 最新包、TCG 或预构建的不同镜像。若归档已存在，脚本拒绝覆盖证据。

## 私有配置与启动

为**每个准入组合**准备独立、当前执行 UID 所有的0700 HERMES_HOME和workspace。首次为全新目录；重启会保留同一目录的原生会话。模型 JSON 只含 `base_url` 与 `model`，本轮写入用户批准的真实端点和模型；不要把占位符当成通过证据。模型key/API token各在独立0600文件；API token由安全随机数生成，至少32个base64url或hex字符。文件不得以参数、Docker `-e`、build-arg、镜像层或提交传递。

```json
{"base_url":"https://api.example.invalid/v1","model":"model-id"}
```

下面文件路径均为私有输入的约定位置，配置、模型key和API token必须事先安全准备；镜像ID须替换为本次provenance值。命令不包含真实secret，`--user`使0600宿主secret能被同UID进程读取；目录需同UID0700。secret/config单文件bind必须只读，启动器会检查 `ST_RDONLY`，不会放宽权限或写`.env`/auth key文件。

```bash
docker run --detach --name harniqo-hermes-a382-qualification \
  --user "$(id -u):$(id -g)" --init --read-only \
  --cap-drop ALL --security-opt no-new-privileges \
  --pids-limit 128 --memory 4g --cpus 2 \
  --tmpfs /tmp:rw,noexec,nosuid,nodev,size=256m,mode=1777 \
  --publish 127.0.0.1:10010:8642 \
  --mount "type=bind,src=$PWD/.private/qualification/hermes/home,dst=/data/hermes" \
  --mount "type=bind,src=$PWD/.private/qualification/hermes/workspace,dst=/data/workspace" \
  --mount "type=bind,src=$PWD/.private/qualification/hermes/model.json,dst=/run/config/model.json,readonly" \
  --mount "type=bind,src=$PWD/.private/secrets/model-api-key,dst=/run/secrets/model-api-key,readonly" \
  --mount "type=bind,src=$PWD/.private/qualification/hermes/api-token,dst=/run/secrets/api-token,readonly" \
  sha256:REPLACE_WITH_PROVENANCE_IMAGE_ID
```

API仅在容器内 `0.0.0.0:8642`，宿主映射环回10010；不要挂载Docker/libvirt socket、宿主目录、SSH目录或开放公网bind。容器共享宿主内核，普通bridge出网也不构成严格租户出口策略。只读挂载和进程内环境变量阻止凭据进入 `docker inspect Config.Env`；它们不能隔离恶意同UID代码、daemon管理员、内核或进程内存。仅在可信受控准入中使用，生产沿用KVM边界和凭据代理。

启动器在进程内读取模型key到 `HARNIQO_MODEL_API_KEY`，由原生 `model.key_env` 绑定精确 `model.base_url`，不写明文key到Hermes配置；API token读入 `API_SERVER_KEY`。固定custom/chat_completions/streaming，禁 multiplex、plugins（`HERMES_SAFE_MODE=1`）、MCP、lazy install、tool_search、coding context和自动标题。单profile原生managed worker继承启动器环境；不能把这个profile直接改成多profile复用密钥。

原生API是 [gateway/platforms/api_server.py](https://github.com/NousResearch/hermes-agent/blob/a382af23f4c067d23463b5781958684c2cda82bc/gateway/platforms/api_server.py)，启动器调用原生 `gateway.run.start_gateway()`。不是自造OpenAI聊天代理。`GET /health`无鉴权，只能证明监听；其余准入请求用客户端在内存中从文件读取token构造 `Authorization: Bearer …`，不要把token放进curl参数、公共trace或日志。

| 原生路径 | 验收用途 |
| --- | --- |
| `POST /api/sessions`，`{id,title}` | 创建固定ID原生会话，记录返回ID |
| `POST /v1/runs`，`{session_id,input}` | 新建真实Agent执行；可带Idempotency-Key，记录run_id |
| `GET /v1/runs/{id}` | 轮询真实状态，记录usage/runtime模型metadata |
| `GET /v1/runs/{id}/events` | SSE轨迹；`Last-Event-ID`/`last_seq`续传检查需真实断线，不凭接口存在断言通过 |
| `GET /api/sessions/{id}/messages` | 核对工具事件、真实随机回执和原生持久历史；同home容器重启后再查 |
| `POST /v1/runs/{id}/stop` | 停止请求回stopping仍需等待终态，并核实际worker/tool停止证据；不冒充VM已停 |

本profile限制 `agent.max_turns=4`、`run_budget_seconds=90`、`api_max_retries=1`、`auto_recovery_cycles=0`。这些不等于输出token或费用硬封顶；该commit没有可据此宣称生效的通用 `model.max_tokens` 用户配置。外层验收执行器还应给每个用例总时限、记录终止及报告完整性，超时/无完整报告不通过。只用获准模型和小规模合成任务，provider-reported usage不替代最终账单。

本轮必须分别记录：真实会话/模型交互、两个受控工具回执、usage、SSE续传、重启恢复、取消实际终态、认证拒绝及工具拒绝。任何缺项继续保持进行中；原生API该能力不存在或组合不兼容时如实登记。

## 源码和许可材料

Hermes源码按上游MIT许可；镜像保留 `/opt/hermes/LICENSE` 和完整固定源码（含原锁、依赖声明及上游Dockerfile），运行时只增加上述Harniqo MIT工具组合。公开source archive不含凭据；构建provenance列明来源和未修改原始锁SHA。SQLite来源及校验、Python基镜像digest、uv bootstrap来源均可追溯。

实际Python分发清单在 `/opt/hermes-runtime-inventory.json` 并复制到provenance。它不是完整镜像SBOM，也不自动满足所有wheel、系统库及native组件的NOTICE/源码提供义务。本轮产物只保存在被Git忽略的 `images/containers/`；对外发布镜像前仍须完成完整SBOM、嵌入许可、NOTICE和native wheel材料检查，不能仅凭项目MIT给第三方组件重许可。
