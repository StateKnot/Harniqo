# Harniqo 依赖、来源与分发基线

版本：1.0 · 核验日期：2026-10-29 · 对应任务：M0-06。

Harniqo 自有源码、契约、原创文档和合成测试样本采用根目录 [MIT LICENSE](../../LICENSE)。StateKnot 已明确选择 MIT。引用的规范、第三方代码、工具、运行包、操作系统、模型、数据和标识仍保持各自权利与条款，不能因为它们被 Harniqo 调用或一起安装就改标 MIT。

本文是当前来源清点与依赖准入基线，不是尚未构建的发行物 SBOM。M1 解析完整依赖树，M2 构建运行镜像，M8 分发时仍须按实际版本、包内容和交付方式核对。新增或更换依赖必须更新本清单及相应锁文件；不能拿本次主仓许可核验覆盖未知的未来版本或插件。

## 1. 当前自有交付与第三方材料

| 内容 | 当前来源与处理 | 后续准入要求 |
| --- | --- | --- |
| 设计、PRD、契约和校验器实现 | 由 Harniqo 为本项目编写，按 MIT 发布；不拷贝上游 Harness 实现 | 变更追溯到提交，引用规范保留出处；引入第三方片段时单独登记 |
| 研究笔记 | 对官方资料的归纳和链接，未把整份第三方文档收为 Harniqo 原创源码 | 保留来源与核验日期，不把引用项目的商标/许可归为 Harniqo 所有 |
| 字体、图标、图片、Logo | 当前未从第三方项目复制或内置这些发行资产 | 引入前登记具体资产、作者、许可、修改及归属；系统字体的使用不等于获准重新打包字体文件 |
| 任务数据与示例 | 当前测试应使用本项目生成的合成输入，不包含用户真实会话、私有代码或第三方基准数据 | 每个 Dataset 版本登记数据权利、来源及可否再分发，代码 LICENSE 不能代替数据条款 |
| Harness 源码和二进制 | 当前没有将候选 Harness 的实现 vendor 到 Harniqo；正式集成尚待准入 | 固定实际仓库/版本、运行包及依赖，保存对应 LICENSE/NOTICE 和来源；不能仅复制一个主仓许可文件就认证整个镜像 |
| VM/容器镜像 | 镜像规范已定义，构建和准入证据单独登记；本文不声明已有可发行镜像 | 遵循 [images 规范](../../images/README.md)，按镜像实际组件生成 SBOM、保存归档校验值与所需源码/许可材料 |
| 开发验证包 | M0 契约验证的两个直接包及18个传递包已形成20包完整解析锁，以依赖引用使用，不把实现改标为 MIT | 使用已核验官方 hash 的完整锁；direct pins 仅表达依赖意图，不能替代完整锁或包内容分发核验 |

上述“未内置”是对本轮来源范围的记录，不表示将来无需做分发检查。没有安装/发行的拟用组件在下一节明确标为计划依赖。

## 2. 实际 M0 开发验证依赖

直接依赖定义位于 `scripts/contracts/requirements.txt`，用途是校验本项目 JSON Schema/OpenAPI 与正负样例。它们不代表已部署的生产运行时。

| 包 | 直接版本 | 官方许可与来源 | 当前处理 |
| --- | --- | --- | --- |
| jsonschema | 4.26.0 | MIT；[固定 PyPI 发行页](https://pypi.org/project/jsonschema/4.26.0/) | 保留版本引用；若随工具包分发则纳入第三方许可目录 |
| openapi-spec-validator | 0.9.0 | Apache-2.0；[固定 PyPI 发行页](https://pypi.org/project/openapi-spec-validator/0.9.0/) | 收集该版本许可/NOTICE（如有），核验完整解析图后才可称依赖已完整锁定 |

本轮已根据实际 Python3.12/Linux x86_64 解析环境生成 [完整锁](../../scripts/contracts/requirements.lock) 和 [来源记录](../../scripts/contracts/requirements.lock.provenance.json)：20包、434个官方发行文件 SHA-256，安装限定 wheel。锁中保存多个发行文件的 hash 不代表所有 OS/ABI 都已验收；当前可验证目标只限上述环境。完整安装、hash校验和契约测试结果由执行报告记录。

以下为20个固定发行的官方 PyPI metadata 许可核对；不能据此省略发行时对实际 wheel 内 LICENSE/NOTICE 的检查。

| 固定包 | 许可记录 | 官方固定发行 metadata |
| --- | --- | --- |
| annotated-types 0.8.0 | MIT | [来源](https://pypi.org/pypi/annotated-types/0.8.0/json) |
| attrs 26.1.0 | MIT | [来源](https://pypi.org/pypi/attrs/26.1.0/json) |
| jsonschema 4.26.0 | MIT | [来源](https://pypi.org/pypi/jsonschema/4.26.0/json) |
| jsonschema-path 0.5.0 | Apache-2.0 | [来源](https://pypi.org/pypi/jsonschema-path/0.5.0/json) |
| jsonschema-specifications 2025.9.1 | MIT | [来源](https://pypi.org/pypi/jsonschema-specifications/2025.9.1/json) |
| lazy-object-proxy 1.13.0 | BSD-2-Clause | [来源](https://pypi.org/pypi/lazy-object-proxy/1.13.0/json) |
| openapi-schema-validator 0.9.0 | BSD-3-Clause | [来源](https://pypi.org/pypi/openapi-schema-validator/0.9.0/json) |
| openapi-spec-validator 0.9.0 | Apache-2.0 | [来源](https://pypi.org/pypi/openapi-spec-validator/0.9.0/json) |
| pathable 0.6.0 | Apache-2.0 | [来源](https://pypi.org/pypi/pathable/0.6.0/json) |
| pydantic 2.14.1 | MIT | [来源](https://pypi.org/pypi/pydantic/2.14.1/json) |
| pydantic-core 2.50.1 | MIT | [来源](https://pypi.org/pypi/pydantic_core/2.50.1/json) |
| pydantic-settings 2.16.2 | MIT | [来源](https://pypi.org/pypi/pydantic-settings/2.16.2/json) |
| python-dotenv 1.2.4 | BSD-3-Clause | [来源](https://pypi.org/pypi/python-dotenv/1.2.4/json) |
| PyYAML 6.0.3 | MIT | [来源](https://pypi.org/pypi/PyYAML/6.0.3/json) |
| referencing 0.37.0 | MIT | [来源](https://pypi.org/pypi/referencing/0.37.0/json) |
| rfc3339-validator 0.1.4 | MIT | [来源](https://pypi.org/pypi/rfc3339-validator/0.1.4/json) |
| rpds-py 2026.9.1 | MIT | [来源](https://pypi.org/pypi/rpds-py/2026.9.1/json) |
| six 1.17.0 | MIT | [来源](https://pypi.org/pypi/six/1.17.0/json) |
| typing-extensions 4.16.0 | PSF-2.0 | [来源](https://pypi.org/pypi/typing_extensions/4.16.0/json) |
| typing-inspection 0.4.4 | MIT | [来源](https://pypi.org/pypi/typing-inspection/0.4.4/json) |

bootstrap pip 是隔离安装工具，保留在私有工具缓存并单独记录其版本、来源和校验；不混入这20个业务验证包，也不随当前 Harniqo 交付再分发。锁不包含临时缓存或 `file://` 路径。解释器、安装器和依赖环境分别记录，不能把“排除了 bootstrap”误写为没有使用安装器。

包括 `format` 等 optional extra 在内，新增解析范围必须重新生成锁并清点；凭据、虚拟环境和下载缓存不提交到仓库。

## 3. 已决定架构依赖的准入清单

下表核验主项目官方许可；“基线”指本项目的架构选择，未固定的补丁在 M1 实施时锁定。直接使用、静态/动态链接、独立进程、下载脚本和将二进制/镜像交给用户，是不同的交付形态，实际发行记录必须区分。

| 组件 | 架构基线 / 使用位置 | 已核验许可与原始来源 | 分发处理 |
| --- | --- | --- | --- |
| Electron | 当前受支持稳定 major，当前架构候选 45；桌面发行 | MIT；[LICENSE](https://github.com/electron/electron/blob/main/LICENSE) | 桌面包保存 Electron 及内嵌 Chromium/Node 等完整第三方许可资料，不能只保留顶层 MIT |
| React | 受支持稳定版本，M1 固定；renderer | MIT；[LICENSE](https://github.com/facebook/react/blob/main/LICENSE) | 包含对应版本版权与许可 |
| TypeScript | M1 固定；编译与类型检查 | Apache-2.0；[LICENSE](https://github.com/microsoft/TypeScript/blob/main/LICENSE.txt) | 若发行中包含其代码/工具，保留许可及适用 NOTICE；记录修改 |
| Vite | M1 固定；桌面前端构建 | MIT；[LICENSE](https://github.com/vitejs/vite/blob/main/LICENSE) | 开发依赖也进入构建来源记录；发行物按实际包含内容核对 |
| TanStack Query | M1 固定；客户端服务端缓存 | MIT；[LICENSE](https://github.com/TanStack/query/blob/main/LICENSE) | 包含对应版本版权与许可 |
| Node.js | 24.x 受支持补丁；控制面/Runner | MIT 主许可及内嵌第三方许可；[完整 LICENSE](https://github.com/nodejs/node/blob/main/LICENSE) | 分发 Node 运行包时保留完整许可集合；不能把 Node 内嵌依赖统一写成 MIT |
| Fastify | 5.x；控制面 | MIT；[LICENSE](https://github.com/fastify/fastify/blob/main/LICENSE) | 实际插件单独锁定、核验，不以 Fastify 许可覆盖插件市场 |
| Kysely | M1 固定；SQL 和迁移 | MIT；[LICENSE](https://github.com/kysely-org/kysely/blob/master/LICENSE) | 保留许可；不替代数据库本身许可 |
| node-postgres / pg | M1 固定；数据库驱动 | MIT；[LICENSE](https://github.com/brianc/node-postgres/blob/master/LICENSE) | 保留许可并清点所选包的传递依赖 |
| pg-boss | 12.x；持久队列与事务 Adapter | MIT；[LICENSE](https://github.com/timgit/pg-boss/blob/master/LICENSE) | 固定包及 schema 版本，保留许可 |
| PostgreSQL | 17.x；业务与队列 | PostgreSQL License；[官方许可](https://www.postgresql.org/about/licence/) | 数据库二进制/镜像保留对应版权、许可和扩展清单 |
| SeaweedFS 社区版 | M1/M7 固定；私有 S3 默认实现 | Apache-2.0；[LICENSE](https://github.com/seaweedfs/seaweedfs/blob/master/LICENSE) | 区分社区发行与额外服务，保留许可、适用 NOTICE 及修改记录 |
| Keycloak | M1/M7 固定；私有默认 OIDC | Apache-2.0；[LICENSE](https://github.com/keycloak/keycloak/blob/main/LICENSE.txt) | 二进制/容器及其 Java 运行时按实际内容清点 |
| OpenBao | M1/M7 固定；Secret 服务 | MPL-2.0；[LICENSE](https://github.com/openbao/openbao/blob/main/LICENSE) | 保留 MPL；分发受覆盖代码及修改时提供相应源码取得方式，不能以 Harniqo MIT 替代 |
| QEMU | 按宿主受支持发行固定；VM 进程 | 整体 GPLv2，部分源码和固件另有许可；[官方说明](https://www.qemu.org/docs/master/about/license.html)、[LICENSE](https://github.com/qemu/qemu/blob/master/LICENSE) | 默认通过独立进程/API 使用；若打包再分发，保存实际构建的许可、对应源码/取得方式、固件和构建材料 |
| libvirt | 按宿主受支持发行固定；Resource Broker 调用 | C API 为 LGPL-2.1-or-later，部分非 C 库代码为 GPL-2.0-or-later；[官方 README](https://github.com/libvirt/libvirt/blob/master/README.rst) | 不能把整个 libvirt 发行包简写为一个 LGPL 标签；库、daemon、工具和语言 binding 分项登记 |
| Linux/KVM 与 guest OS | 受支持 Linux；宿主内核和 guest 镜像 | 由实际发行组件各自定义 | 操作系统镜像按包清单清点，不以“Ubuntu/Linux”名称赋予单一许可；固件、内核、用户态分别记录 |
| Python | 3.12+ 的已验证组合，实际 Harness 可自带固定版本 | PSF 及历史/内嵌条款；[完整 CPython LICENSE](https://github.com/python/cpython/blob/main/LICENSE) | 固定实际解释器、构建与包，不能用系统 Python 的许可概括所有 pip 包 |
| SQLite | Runner spool；需包含已修复 WAL 问题的版本 | 核心交付代码 public domain；[官方说明](https://sqlite.org/copyright.html) | Node/Python binding、构建脚本和扩展另外核验；记录实际嵌入 SQLite 版本 |
| OpenTelemetry JS | M1 固定；观测 instrumentation | Apache-2.0；[LICENSE](https://github.com/open-telemetry/opentelemetry-js/blob/main/LICENSE) | Collector、exporter 与插件按实际使用单独清点 |
| Prometheus | M1/M7 固定；指标收集 | Apache-2.0；[LICENSE](https://github.com/prometheus/prometheus/blob/main/LICENSE) | 容器及附带组件按固定发行物保留许可 |

OpenBao 的处理依据其许可原文与 [Mozilla MPL FAQ](https://www.mozilla.org/en-US/MPL/2.0/FAQ/)。不因为组件可以在独立进程运行，就推导它的二进制再分发无需履行原许可；也不因为依赖采用不同许可，就把 Harniqo 自有文件改标成该许可。

## 4. Harness 与复用候选

“许可已识别”和“功能通过生产准入”是不同结果，以下都不表示集成已完成。Harniqo 自己编写原生协议 Adapter；没有决定 fork 或拷贝任何完整产品。

| 候选 | 当前主项目许可 / 官方证据 | 准入与分发结论 |
| --- | --- | --- |
| Hermes Agent | MIT；[LICENSE](https://github.com/NousResearch/hermes-agent/blob/main/LICENSE) | M0-03 首选验证对象；实际运行包、工具和模型条款单独固定 |
| OpenClaw | MIT；[LICENSE](https://github.com/openclaw/openclaw/blob/main/LICENSE) | 正式接入候选；不得沿用原收集表的 Apache-2.0 标注 |
| OpenCode | MIT；[LICENSE](https://github.com/anomalyco/opencode/blob/dev/LICENSE) | 正式接入候选；锁定实际版本、目标模型和运行依赖 |
| Octop 完整服务 | MIT；[LICENSE](https://github.com/TencentCloud/Octop/blob/main/LICENSE) | 单独服务连接对象，不将底层包或外部委派一并计为同一个支持项 |
| Octop Harness | MIT；[LICENSE](https://github.com/TencentCloud/octop-harness/blob/main/LICENSE) | 独立候选；核对 deepagents 等实际依赖与元数据 |
| DeepSeek Harness | MIT；[LICENSE](https://github.com/deepseek-ai/deepseek-harness/blob/main/LICENSE) | 固定开发预览版本验证；不自动进入稳定目录 |
| HarnessRouter CE | Apache-2.0；[LICENSE](https://github.com/HarnessRouter/harnessrouter/blob/main/LICENSE) | 只作适配代码复用评估，尚未拷贝；上游 CLI、Starter Kits 不由 CE 主仓许可统一覆盖 |

AGPL、非商业、附加 SaaS 条件、自定义地区/数据条款、缺失许可或无法追溯来源的组件，不能因出现在研究清单就自动进入默认发行。它们必须另有明确采用决定与履约记录。未纳入的项目继续保留在研究资料中；研究链接不是再分发许可。

模型 API 权限、个人订阅登录、工具 SaaS、浏览器/办公软件及数据许可也分别准入；本项目开源不能代替这些服务的使用授权。首版采用获准的 API 端点与用户/组织自有凭据，不内置共享商业模型账户。

### M0 运行包的增量来源记录

Hermes 固定源码 `a382af23f4c067d23463b5781958684c2cda82bc` 的四工具资格验证组合保留原有74包依赖锁，仅增加本项目工具与只记录 hash 的传输观察代码；按新 image ID 和 recipe SHA 区分旧组合。来源、改动和实际验收见 [配方](../../deploy/qualification/hermes/README.md) 与 [证据索引](../planning/M0-RESULTS.md)。

OpenCode 固定 `1.19.5` / `810c0eba1a7348036f8bdf61e6632d0c36c6876c` 的官方 Linux x64 baseline 二进制；GitHub release SHA-256 和显式选择的同版官方 npm SHA-512 SRI 分别记录，不冒用彼此的归档身份。保留固定源码 MIT LICENSE、package.json 与 bun.lock；同时固定官方 ripgrep15.1.0 musl 二进制及其 MIT/Unlicense/COPYING 文件，避免运行时下载。本轮5个原生用例复用已有修复镜像，没有更换依赖或新增归档。完整输入、解包边界与当前能力限制见 [配方](../../deploy/qualification/opencode/README.md) 和 [候选核验](../../research/m0-harness-candidate-qualification.md)。完整镜像系统包/Bun嵌入依赖 SBOM及正式二进制分发材料仍须按后续门槛验收，真实用例通过不代替分发清点。

Octop Harness固定 `1.0.5` / `76cd38d7b8ffc66c28ba24ad60bbab206a95d3ac`，使用官方Python3.14.10固定基镜像及该commit的上游uv.lock选择93个wheel，共31,330,542 bytes。逐发行物核验官方PyPI metadata、SHA/size；根wheel的216个Python文件与固定源码一致，禁网hash安装/pip check通过。完整锁SHA-256为`f566ec85a38feda0529dd1a9313f5d0289af2aa86207fab81765531e91ed3d71`，材料见 [锁](../../deploy/qualification/octop/harness.requirements.lock)、[官方wheel来源](../../deploy/qualification/octop/harness.wheel-provenance.json) 和 [独立资格记录](../../research/octop-native-qualification.md)。新增Docker归档与原生SDK证据分别登记；顶层MIT不覆盖DeepAgents、MCP、LangGraph、解释器与镜像全部组件，完整SBOM/NOTICE及各实际wheel许可材料仍须发行前核验。

Octop完整服务固定 `1.0.3b1` / `890111b7879665840414ca28ab168b05b0db0921`，其官方uv.lock SHA-256为`c6b4c1a8f96c37351731b7eab79772fccffbc1ae7aadd501df28be954b52c907`，选择201个发行物。纯wheel配方失败后，固定sdist的evdev1.9.3、oss2 2.19.1、crcmod1.7、aliyun-python-sdk-core2.16.0、esdk-obs-python3.26.6已用无网非root builder构建；构建工具setuptools80.9.0、wheel0.45.1、packaging26.2及其官方hash独立登记。服务201包完整镜像及零模型原生HTTP/认证已完成，服务模型资格仍独立记录，不继承Harness的93包锁、镜像或原生资格；本地构建wheel的源码hash、工具链与产出hash需同时保留，见 [sdist构建器](../../scripts/qualification/build_octop_sdists.py)。

OpenClaw2026.10.8固定commit `dc2523e5a5c7a309517fd7fa834add938231d526`的官方slim镜像已按manifest/config及42blob完整核验，两官方registry字节相同；Node24.22.1/Undici8.11.2实际镜像验证及归档/真实用例见[配方](../../deploy/qualification/openclaw/README.md)和E32–34。此为上游官方二进制输入核验，未宣称源码可复现构建或已完成所有系统/传递组件的发行SBOM；正式分发仍需完整许可材料。网络资格新增脚本仅使用Python标准库和既有宿主工具，没有安装新的宿主依赖；固定TLS canary不是正式代理发行物，不增加生产隔离通过声明。

三平台契约CI仅使用官方Actions不可变提交：checkout v7.0.1 `3d3c42e5aac5ba805825da76410c181273ba90b1`、setup-python v7.1.0 `6d4fd22b20465ccc0f7354476588e8df7c9ca0e0`、upload-artifact v7.0.2 `cf430e030ddbb5b0abf93d22962f4752f3646cd9`，从各官方仓库release/tag解析确认；它们是CI执行依赖，不捆入客户端。运行时Python补丁版本、锁hash与安装包清单由各job记录。

## 5. 版本、来源和 NOTICE 的执行规则

1. **新增时登记。** 名称、用途、直接/传递、开发/运行、精确版本、源码 commit/tag、原始来源、许可表达式、实际许可文件、修改情况和是否随发行包交付。
2. **获取时固定。** Node/pnpm 依赖锁、Python 完整解析锁与下载 hash、宿主包版本、镜像 digest、归档 SHA-256 分别保存；`latest` 和未固定 git branch 不是发布版本。首次锁定后重新核对同一固定版本的许可，不能只保留本次浮动主分支 URL。
3. **构建时清点。** 生成包含实际传递依赖和镜像包的 SPDX JSON SBOM，并与源码提交、构建工具、artifact digest 关联。文件/动态包/系统层无法自动识别的内容人工补项。
4. **分发时保留。** MIT/同类组件保留其要求的版权与许可；Apache-2.0 保留许可、适用 NOTICE 和修改标记；MPL/GPL/LGPL 等组件按实际发行方式提供相应许可和源码/取得方式，不用 Harniqo MIT 代替。发行工具生成 `THIRD_PARTY_NOTICES`/许可证目录时必须检查内容，不只是生成一个空文件。
5. **升级时重验。** 支持周期、关键 API、许可证、数据条款、原生能力和镜像依赖一起复核。安全补丁走兼容回归；破坏性变更先更新 ADR、能力与恢复策略。
6. **交付前核对。** 二进制、安装包、镜像、样本、截图、日志与源码各自检查；`.private/`、凭据、私有任务、执行写入层不得进入开源提交或公共发行包。

每个发行组件的最少记录字段为：`name / exact_version / source_commit / source_url / usage_kind / license_expression / license_files / modified / redistributed / artifact_digest / source_availability / verification_report`。没有实际 artifact 的条目填“未构建”，不填写虚构 hash。

## 6. M0-06 验收范围

本轮完成：自有 MIT 范围、当前内容来源、已选择架构依赖与 Harness 候选的主许可核验、实际 M0 验证环境20包完整锁及其官方许可 metadata、分发与版本策略、关键 ADR 的引用关联。来源证据见 [核验笔记](../../research/m0-dependency-verification.md)、上述锁来源记录和 [架构 ADR](../adr/0001-modular-control-plane.md)。

M0-06 的文档交付可以针对**当前基线**验收；它不宣称未来传递依赖、运行包、操作系统镜像或签名发行物已获完整分发认证。M1 依赖解析、M2 镜像与 M8 发行核对是持续门槛，新增未核验组件时必须先补清单，不能引用本次状态跳过。

## 2026-10-30 多 Harness 增量

OpenCode保持1.19.5及原镜像不变，新增只读本地plugin通过固定上游config hook注入`provider.options.fetch`观察器，记录源码SHA及21项实际Bun/3项判据测试；不引入npm依赖、不改变原生Agent loop。Qwen单任务的实际4次请求目标/模型已核验，语义与计数范围见[补证记录](../../research/opencode-model-transport.md)。

DSH固定`dsh-v0.2.2-alpha.2`→`f3803c96974ad4a94195fb97e5c637e9c2a3afe3`。该版PyPI runtime-bin不存在，采用同版官方npm包及完整SRI；本次Harniqo冻结的npm lock有724个解析条目、653个实际安装包，Node24.22.1/npm12.2.1。它不是官方pnpm workspace依赖树，也未声称二进制可复现构建；原上游锁hash、实际组合锁、发行包与镜像SHA分别保留。见[来源与版本](../../deploy/qualification/dsh/runtime-inputs.json)、[组合锁](../../deploy/qualification/dsh/package-lock.json)、[研究与限制](../../research/dsh-native-qualification.md)。DSH顶层MIT不替代其所有传递组件许可，正式分发仍需完整SBOM/NOTICE。

Octop出站ACP在原Harness1.0.5的固定上游锁中补`agent-client-protocol0.10.1`和`psutil7.2.2`，新组合携带原OpenCode二进制/许可证并单独归档，不改核心SDK与完整服务旧归档。外层工具最终文本的原版失败与最小源码修正组合分别登记；修正保留上游MIT/LICENSE、原/新源码与patch SHA，不能把修改后行为写成原版通过。见[来源](../../deploy/qualification/octop/acp-extra-provenance.json)和[分层记录](../../research/octop-acp-qualification.md)。

无模型资格组件CI新增官方`actions/setup-node v7.1.0`固定提交`949feb2413d6458794dcd2491c4babbbce0c15c1`，Node24.22.1；其余Actions沿用上述不可变提交。CI只运行明确选择的标准库/纯Node夹具，不分发或启动真实Harness，不持有模型凭据。

## 2026-10-30 Resource Broker 增量

本地 Resource Broker 使用 Python3.12 标准库、系统 libvirt/virsh 与 QEMU，不新增 pip/npm 依赖或镜像来源。模板基于已有 Canonical 签名验证的 Ubuntu base，raw 写入层及 NoCloud seed 是本次运行资源，不作为新的上游基础发行包。HMAC 指派与续约签名是宿主私有授权格式，issuer key 不进入 Guest、镜像或公开报告。具体源版本、原始失败与准入范围见[本轮记录](../../research/m0-resource-broker-qualification.md)。

2026-10-31固定limited-v2增加Linux cgroup v2实际读回，仍仅Python标准库/libvirt/QEMU，无新增pip/npm依赖。明确核验libvirt10的GetNested及swap direct映射；1GiB Guest使用QEMU记账内存2GiB+额外swap2GiB只是本sealed模板验证值，不是任意Harness通用余量。[结果与primary来源](../../research/m0-host-resource-limits.md)。
