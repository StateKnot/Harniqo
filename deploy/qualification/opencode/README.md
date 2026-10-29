# OpenCode 原生服务准入组合

此组合用于 M0-04 的原生接口验证；当前文件提供公开输入准备、禁网构建和原生启动配方。**源码检查、离线测试和镜像构建均不等于 OpenCode 已通过真实模型或生产隔离验收。** 使用不修改的 OpenCode `1.19.5` 原生服务与工具，Harniqo 只控制模型、权限、启动环境和凭据装载。正式运行必须进入单独验收的执行目标/KVM 边界。

## 固定发行身份

| 输入 | 官方来源与 pin |
| --- | --- |
| Release | [v1.19.5](https://github.com/anomalyco/opencode/releases/tag/v1.19.5)，官方 API 读取发布时间 `2026-10-28T06:17:49Z`，`immutable:true`；[tag ref](https://api.github.com/repos/anomalyco/opencode/git/ref/tags/v1.19.5) 直接指向 `810c0eba1a7348036f8bdf61e6632d0c36c6876c` |
| GitHub 默认二进制 | [Linux x64 baseline/glibc tar](https://github.com/anomalyco/opencode/releases/download/v1.19.5/opencode-linux-x64-baseline.tar.gz)，79,651,821 bytes，SHA-256 `17c200fd4b2289954cc01552559f9b6d03ec157b2ddfb5eb54144f491656333d`，来自[官方 release API](https://api.github.com/repos/anomalyco/opencode/releases/tags/v1.19.5) 的 asset digest |
| 显式 npm 格式 | [opencode-linux-x64-baseline@1.19.5](https://registry.npmjs.org/opencode-linux-x64-baseline/1.19.5)，[固定 tarball](https://registry.npmjs.org/opencode-linux-x64-baseline/-/opencode-linux-x64-baseline-1.19.5.tgz)，完整 SHA-512 SRI `sha512-9kA+st3XXtCXMPx5f3Ld1u19uoHTO2u2Btt8a2cqhJ4GIqVbD1mXLevj3LHFEmtuQoFGk+UstGnl38sCqXj5sQ==`；仅标准库解包，不执行 npm/Node 或 postinstall |
| 源码材料 | 固定 commit 的 [LICENSE](https://github.com/anomalyco/opencode/blob/810c0eba1a7348036f8bdf61e6632d0c36c6876c/LICENSE)、[原 package.json](https://github.com/anomalyco/opencode/blob/810c0eba1a7348036f8bdf61e6632d0c36c6876c/packages/opencode/package.json)、[bun.lock](https://github.com/anomalyco/opencode/blob/810c0eba1a7348036f8bdf61e6632d0c36c6876c/bun.lock) 均核对官方 tree 的 Git blob 身份，并记录本次 SHA-256 |
| ripgrep | 与固定 OpenCode [原生查找器](https://github.com/anomalyco/opencode/blob/810c0eba1a7348036f8bdf61e6632d0c36c6876c/packages/core/src/ripgrep/binary.ts) 相同的 15.1.0；[官方 musl release tar](https://github.com/BurntSushi/ripgrep/releases/download/15.1.0/ripgrep-15.1.0-x86_64-unknown-linux-musl.tar.gz)，2,263,077 bytes，SHA-256 `1c9297be4a084eea7ecaedf93eb03d058d6faae29bbc57ecdaf5063921491599`，保留 COPYING/LICENSE-MIT/UNLICENSE |
| 基镜像 | Docker Official Python 的固定 Linux amd64 manifest `python@sha256:faf7953db05ee8e060aa9fda8a389e5779cdba26a22e1363379861023e910830`；本组合复用已缓存的 Hermes 公共输入基镜像，不使用 Python 模型 SDK，不安装宿主 Node |

GitHub 与 npm 是显式选择的两种官方发布格式，各自核对完整 hash。npm registry 返回 `opencode-ai@1.19.5` 的同版平台包 optional dependency；上游固定 [binary dispatcher](https://github.com/anomalyco/opencode/blob/810c0eba1a7348036f8bdf61e6632d0c36c6876c/packages/opencode/bin/opencode) 与 [postinstall](https://github.com/anomalyco/opencode/blob/810c0eba1a7348036f8bdf61e6632d0c36c6876c/packages/opencode/script/postinstall.mjs) 也明确选择平台包的 `bin/opencode`。npm metadata 声明 GitHub Actions trusted publisher 和 maintainer `thdxr`；本脚本校验 SRI，不宣称额外完成 registry 签名或 provenance attestation 验证。尚未实际逐字节比较两种 archive 中的可执行文件，因此不能写成同 hash。

该 binary 由上游 [Bun compile 配方](https://github.com/anomalyco/opencode/blob/810c0eba1a7348036f8bdf61e6632d0c36c6876c/packages/opencode/script/build.ts) 生成。基线版避免要求目标 CPU 具有 AVX2。原生 SDK 包 `@ai-sdk/openai-compatible` 已[捆绑](https://github.com/anomalyco/opencode/blob/810c0eba1a7348036f8bdf61e6632d0c36c6876c/packages/opencode/src/provider/provider.ts)，不需要额外安装 provider 包。

## 公开输入准备与离线构建

在指定服务器的仓库根目录执行。默认只下载和核验公开输入，不执行下载的程序、不访问模型、不监听端口。默认来源失败不会自动切换；GitHub 传输较慢时可显式选 npm。

```bash
df -h .
python3 scripts/qualification/prepare_opencode.py --root "$PWD" --binary-source npm-package
```

需要归档时显式加 `--build`。该分支要求原生 Linux amd64、已缓存的固定 base、工作区和 Docker 实际文件系统均有 60 GiB 空闲；打印 `df` 并以字节阈值复核。构建无 apt、npm、pip 或远程 shell，使用 `docker build --network=none --pull=false`。Dockerfile 只执行 `--version`、`rg --version` 和动态库检查，不启动 server 或模型。

```bash
python3 scripts/qualification/prepare_opencode.py --root "$PWD" --binary-source npm-package --build
```

公开缓存和输入 manifest 位于 `.state/qualification/opencode/`，临时 context 结束即清理。context 只包含选定可执行文件、上游许可/依赖声明、Dockerfile、entrypoint 和输入 manifest；不复制仓库根目录、`.private`、`.env` 或既有安装目录。禁止 archive 中的链接、路径穿越、重复条目、错误 ELF 架构和超量展开；npm 路径仅选 `package/bin/opencode` 与核对后的 manifest。脚本不安装平台包，也不执行其生命周期脚本。

归档输出为 `images/containers/opencode/1.19.5/sha256-<实际 image store ID>.docker.tar`，另存 `.provenance.json`。原始 `build_result_id` 与 `docker inspect` 实际 `.Id` 分开记录，避免在 Docker 29 containerd store 上混淆 descriptor/config/可运行身份。归档 SHA-256 与 registry digest 分开；没有 registry 发布验证时 `registry_manifest_digest` 为 null。归档 fsync 后设 0444，provenance 同样只读。既有证据不覆盖。

构建开始前在 `.private/qualification/opencode-build/` 建 attempt；构建输出直接保存在该目录，超时仍保留日志。`runtime-inventory.json` 含离线实测版本、Python 版本和 ldd 结果。读取 inventory 只创建未启动容器，复制文件后删除该精确容器；没有 `docker run`。所有输入、配方/启动器/preparer SHA 和 inventory 一并进入 provenance；始终 `qualification_passed:false`。

此轮缓存仅供该发行组合复建。清理只允许确认未被使用的本任务 context/下载缓存，先保留 attempt/provenance/报告；Docker 层使用其现有 GC，不做全局 prune。达到用户的缓存/空间阈值时先评估，清理后报告实际 df 差值。

离线边界检查：

```bash
PYTHONDONTWRITEBYTECODE=1 python3 scripts/qualification/test_prepare_opencode.py
```

## 私有配置与原生启动

每个获准组合使用独立的 0700 home/workspace，属执行 UID；模型配置、API key 与服务密码分别使用 UID 所有的 0600 只读文件。模型配置只接受下列字段；`base_url` 必须 HTTPS，不含用户名、密码、query 或 fragment。`context` 是显式保守上下文容量，`output` 是 128–1024 的准入输出限额，必须符合真实端点。

```json
{"base_url":"https://api.example.invalid/v1","model":"approved-model-id","context":8192,"output":1024}
```

只有真实协议要求时显式加 `"interleaved":"reasoning_content"`；不根据厂商或模型名自动猜测。本组合选择 Chat Completions：`npm:"@ai-sdk/openai-compatible"`、`provider.options.baseURL`、`provider.options.apiKey:"{env:HARNIQO_MODEL_API_KEY}"`，模型 key 对应真实 API model ID。Responses 端点需要独立发行配置和真实准入，不能把仅替换 URL 当成转换。[原生 provider 配置源码](https://github.com/anomalyco/opencode/blob/810c0eba1a7348036f8bdf61e6632d0c36c6876c/packages/opencode/src/provider/provider.ts)

`entrypoint.py` 在内存读取 secrets，`os.execve` 原生 `opencode serve --hostname 0.0.0.0 --port 4096`；服务密码至少32个 base64url 字符。API 是原生 HTTP Basic（用户名 `opencode`）。不把 key 放进 Docker `-e`、命令参数、构建参数或持久配置。[原生 ServeCommand](https://github.com/anomalyco/opencode/blob/810c0eba1a7348036f8bdf61e6632d0c36c6876c/packages/opencode/src/cli/cmd/serve.ts)、[认证源码](https://github.com/anomalyco/opencode/blob/810c0eba1a7348036f8bdf61e6632d0c36c6876c/packages/opencode/src/server/auth.ts)

下例要求先设置获准的宿主环回端口；镜像 ID 取本次 provenance。文件路径表示约定位置，需要事先按上述权限准备，命令不包含真实凭据。

```bash
docker run --detach --name harniqo-opencode-1195-qualification \
  --user "$(id -u):$(id -g)" --init --read-only \
  --cap-drop ALL --security-opt no-new-privileges \
  --pids-limit 128 --memory 4g --cpus 2 \
  --tmpfs /tmp:rw,noexec,nosuid,nodev,size=256m,mode=1777 \
  --publish "127.0.0.1:${HARNIQO_OPENCODE_PORT:?Set an authorized port}:4096" \
  --mount "type=bind,src=$PWD/.private/qualification/opencode/home,dst=/data/opencode" \
  --mount "type=bind,src=$PWD/.private/qualification/opencode/workspace,dst=/data/workspace" \
  --mount "type=bind,src=$PWD/.private/qualification/opencode/model.json,dst=/run/config/model.json,readonly" \
  --mount "type=bind,src=$PWD/.private/secrets/model-api-key,dst=/run/secrets/model-api-key,readonly" \
  --mount "type=bind,src=$PWD/.private/qualification/opencode/api-password,dst=/run/secrets/api-password,readonly" \
  sha256:REPLACE_WITH_PROVENANCE_IMAGE_ID
```

容器网络尚需执行目标授权的模型出口策略；普通 Docker bridge 不是租户出口隔离。禁止挂载宿主 Docker/libvirt socket、SSH、云控制凭据或其他项目目录。与 Hermes 一样，此处仅适用于可信合成任务的接口准入；同 UID shell/native code 仍可能读进程环境或 secret 挂载，因此生产必须使用已验收的执行隔离与凭据代理。

## 配置、成本与真实验收边界

关闭更新检查、在线模型目录刷新、默认插件、外部 skills、Claude Code 配置发现、项目配置、LSP 下载、LSP/formatter、MCP、文件快照和分享。固定 `enabled_providers`、`model`、`small_model`；禁 title/summary Agent 和自动 compaction，避免额外小模型调用。原生 `@ai-sdk/openai-compatible` 已打包，系统 `rg` 已备好。相关 knobs 分别来自[静态 Flag](https://github.com/anomalyco/opencode/blob/810c0eba1a7348036f8bdf61e6632d0c36c6876c/packages/core/src/flag/flag.ts)、[RuntimeFlags](https://github.com/anomalyco/opencode/blob/810c0eba1a7348036f8bdf61e6632d0c36c6876c/packages/opencode/src/effect/runtime-flags.ts)、[Config schema](https://github.com/anomalyco/opencode/blob/810c0eba1a7348036f8bdf61e6632d0c36c6876c/packages/core/src/v1/config/config.ts)。

原生 config 仍会为发现的 config directory 调用 npm installer。固定的只读 XDG_CONFIG_HOME 中预建真实目录和 .gitignore，原生 [Npm.install](https://github.com/anomalyco/opencode/blob/810c0eba1a7348036f8bdf61e6632d0c36c6876c/packages/core/src/npm.ts) 检查不可写后直接返回。不要把 HOME 指向既有个人 OpenCode 目录；不接受其中的 `.opencode` 或远程账号配置。仅初次鲜净和同一可信准入组合续接允许复用状态，后续需核对实际无附加安装请求。

权限默认 deny；允许 read，write/edit 请求原生审批，shell/web/委派/外部目录/skills 保持拒绝。OpenCode 将 write/apply_patch 归在 edit 权限族，审批以原生实际请求为准。该 profile 验证原生文件工具与审批，不使用自造结果代替工具。工作区、secret 与 HOME 的进程内权限并不构成强文件沙箱。

每轮 build Agent 最多4步，输出最多1024 tokens，请求 timeout60s；这些均不等于账单硬上限。模型可能有重试、初始化上下文和供应商隐藏用量；provider usage 缺失记 unknown，不将 native 默认价格0当成免费。首轮仅用一个合成工具任务，确认模型 ID、工具、文件和审批后再做第二模型与恢复/取消用例。

| 原生路径 | 需要取得的实际证据 |
| --- | --- |
| `GET /global/health`、`GET /doc` | 发行版本、真实 OpenAPI schema；另验错误 Basic 凭据拒绝 |
| `POST /session` | 固定非默认 title，保留返回的原生 session ID |
| `POST /session/{id}/prompt_async` | 显式 `model:{providerID:"harniqo",modelID:"<真实 model ID>"}`；不能凭接受204当成完成 |
| `GET /event`、`GET /session/{id}/message` | 实际工具调用/回执、内容、message/model/usage；断线后从权威历史重建，SSE不默认承诺 durable replay |
| `GET /permission`、`POST /permission/{requestID}/reply` | 原生 pending request；allow-once/reject 实际行为和无副作用拒绝；固定版 schema 的枚举见 `/doc` |
| `POST /session/{id}/abort`、`GET /session/status` | abort 接受与真正执行停止分开，检查终态/工具副作用/后续续聊，不把 boolean 当成所有资源停止 |
| 重建同 home 的原生服务后继续相同 session | 原生 SQLite/history、配置及模型一致、文件可追溯；不从 SQLite 文件存在推断恢复通过 |

官方 [Server 文档](https://opencode.ai/docs/server/) 是入口，固定源码及本次实际 `/doc` 优先。旧 `/session/{id}/permissions/{permissionID}` 在固定版已 deprecated；应按 `/permission` 族处理。[固定 permission API](https://github.com/anomalyco/opencode/blob/810c0eba1a7348036f8bdf61e6632d0c36c6876c/packages/opencode/src/server/routes/instance/httpapi/groups/permission.ts)

上游 MIT 源码和 ripgrep 许可材料保留在 `/opt/harniqo/upstream/`。编译 binary、Python 基镜像和其 native/bundled 第三方组件仍需完整 SBOM、NOTICE 及许可证/源码提供义务检查；本私有准入产物不冒充已通过对外分发审查。SRI/asset checksum 保证本次字节匹配公开 pin，不证明从源码可复现同一 binary。真实模型、拒绝、取消、续聊、升级回退、VM 边界和正式支持地位均需独立记录证据。

## 2026-10-29 实施结果与范围

新镜像 `sha256:ef392b1148602b61ad4ea1e9dcca9b05337adc3c7ef9fc97434d632c69b8373c` 已在指定 Linux amd64 服务器完成禁网构建和 Docker archive 归档：499,582,464 bytes，archive SHA-256 `c50dd9d32477a4fc70a8e4ba0a985e6bb41688b633d77990869f7078e9e194bc`。运行清单实测 OpenCode1.19.5、ripgrep15.1.0（上游revision后缀保留）、Python3.14.10、所有动态库可用，并增加最终非root启动材料可读/配置目录不可写检查。

原始失败均保留：服务器对官方ripgrep URL两次读取超时后，只将本机从相同官方URL取得并核完整SHA的公开输入传入任务缓存；首轮构建对ripgrep版本行的断言误把revision后缀当成版本不符；首个已归档镜像 `bfc9e8b6…` 的COPY权限令 `/opt/harniqo` 为0444，非root启动失败、未提交模型。修复搜索权限后生成上述新镜像，旧镜像和日志未覆盖。

实际原生smoke保留两个付费任务的独立状态：

| 用例 | 确认结果 |
| --- | --- |
| `20261029T124409Z-832c652e` | health1.19.5、缺失/错误Basic均401、OpenAPI3.1、session和prompt204；原生read完成、write等待审批。探针错误地将patterns视为cwd相对，而nongit项目的原生worktree是`/`，因此拒绝了正确固定输出。未批准、未产生输出，abort200后容器确认PID0并移除。该原始失败不改写为成功 |
| `20261029T125157Z-fef1fe98` | 唯一追加任务使用权威`/project/current.worktree`、metadata.filepath和callID关联；仅批准固定输出的原生edit请求once，HTTP200。所有原生工具调用均为固定输入read→固定输出write→固定输出read且completed，实际文件与不在提示词中出现的随机challenge完全一致 |

第二个任务的**原自动探针仍记超时未通过**：探针曾错误等待assistant的`time.end`，该字段属于工具；assistant实际使用`time.completed`。修复后的组件回归通过，但没有再调用模型重跑。只用超时处理时、abort之前已保存的原生history和实际文件追加 `offline-supplement.json`：最后assistant `finish:stop`、有completed时间、无error；完成于提交标记后11.294秒，至少早于120秒deadline下界108.706秒，也早于预abort历史快照和abort请求。原timeout report SHA保持不变，不把补证覆盖为整轮自动通过。

完整原始证据在私有 `.private/qualification/opencode-native-smoke/<上述用例>/`，包括请求、原生历史、随机输入与实际输出、补证、资源配置、容器日志和停止记录；公开索引见M0证据文档。最终artifact SHA-256为 `ce29d9239e11b2651e940f48af44903185bfc13f287f2c8a1d9c017647c3164d`。配置模型metadata为 `harniqo/qwen3.8-flash`；未取得独立served-model传输证明。

实际运行使用执行UID、只读根/secret挂载、128pids/4GiB/2CPU、cap-drop/no-new-privileges及临时/tmp；各容器均确认PID0后移除，私有home和证据保留。总计12项离线组件测试在本机与服务器通过，包括archive/完整性、模型配置、nongit worktree审批、完整工具路径/顺序和assistant终态字段。

当前只确认上述原生子项。当时的session idle响应未保存，不能由abort后的状态倒推；SSE、重连/重启续聊、原生Run取消、拒绝后继续、第二模型、生产KVM隔离与完整第三方分发审查均未在此通过。M0-04继续IN_PROGRESS。
