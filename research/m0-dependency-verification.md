# M0 依赖许可与验收外部条件核验

日期：2026-10-29。只使用上游 LICENSE、官方发行页及平台支持文档。本文是 [M0-06 清单](../docs/compliance/DEPENDENCIES.md) 与 [M0-07 基线](../docs/operations/ACCEPTANCE-BASELINE.md) 的来源记录，不是运行、签名或完整供应链认证报告。

## 许可事实

| 范围 | 本轮确认 | 原始来源 |
| --- | --- | --- |
| 桌面 | Electron、React、Vite、TanStack Query 主项目 MIT；TypeScript Apache-2.0 | [Electron](https://github.com/electron/electron/blob/main/LICENSE)、[React](https://github.com/facebook/react/blob/main/LICENSE)、[Vite](https://github.com/vitejs/vite/blob/main/LICENSE)、[Query](https://github.com/TanStack/query/blob/main/LICENSE)、[TypeScript](https://github.com/microsoft/TypeScript/blob/main/LICENSE.txt) |
| Node/数据库客户端 | Node 主许可MIT且完整LICENSE包含内嵌组件；Fastify、Kysely、node-postgres、pg-boss MIT | [Node完整许可](https://github.com/nodejs/node/blob/main/LICENSE)、[Fastify](https://github.com/fastify/fastify/blob/main/LICENSE)、[Kysely](https://github.com/kysely-org/kysely/blob/master/LICENSE)、[pg](https://github.com/brianc/node-postgres/blob/master/LICENSE)、[pg-boss](https://github.com/timgit/pg-boss/blob/master/LICENSE) |
| 数据与独立依赖 | PostgreSQL采用自身许可；SeaweedFS、Keycloak为Apache-2.0；OpenBao为MPL-2.0 | [PostgreSQL](https://www.postgresql.org/about/licence/)、[SeaweedFS](https://github.com/seaweedfs/seaweedfs/blob/master/LICENSE)、[Keycloak](https://github.com/keycloak/keycloak/blob/main/LICENSE.txt)、[OpenBao](https://github.com/openbao/openbao/blob/main/LICENSE) |
| 虚拟化 | QEMU整体GPLv2，部分源码与固件分项；libvirt C API LGPL-2.1-or-later，部分非库代码GPL-2.0-or-later | [QEMU说明](https://www.qemu.org/docs/master/about/license.html)、[QEMU完整分项](https://github.com/qemu/qemu/blob/master/LICENSE)、[libvirt README](https://github.com/libvirt/libvirt/blob/master/README.rst) |
| Python/SQLite/观测 | CPython许可包含历史与内嵌条款；SQLite核心交付代码public domain，binding另查；OTel JS/Prometheus Apache-2.0 | [CPython](https://github.com/python/cpython/blob/main/LICENSE)、[SQLite](https://sqlite.org/copyright.html)、[OTel](https://github.com/open-telemetry/opentelemetry-js/blob/main/LICENSE)、[Prometheus](https://github.com/prometheus/prometheus/blob/main/LICENSE) |
| 首批Harness | Hermes、OpenClaw、OpenCode、Octop、Octop Harness和DSH主仓均MIT；HarnessRouter CE为Apache-2.0 | [Hermes](https://github.com/NousResearch/hermes-agent/blob/main/LICENSE)、[OpenClaw](https://github.com/openclaw/openclaw/blob/main/LICENSE)、[OpenCode](https://github.com/anomalyco/opencode/blob/dev/LICENSE)、[Octop](https://github.com/TencentCloud/Octop/blob/main/LICENSE)、[Harness](https://github.com/TencentCloud/octop-harness/blob/main/LICENSE)、[DSH](https://github.com/deepseek-ai/deepseek-harness/blob/main/LICENSE)、[Router](https://github.com/HarnessRouter/harnessrouter/blob/main/LICENSE) |
| M0验证依赖 | jsonschema4.26.0为MIT，openapi-spec-validator0.9.0为Apache-2.0；均要求Python≥3.10 | [jsonschema固定发行](https://pypi.org/project/jsonschema/4.26.0/)、[OpenAPI validator固定发行](https://pypi.org/project/openapi-spec-validator/0.9.0/) |

核验方式是读原文，而非只信 GitHub 页面许可证标签。GitHub Connector 读取 React LICENSE 时未返回可读文件，改读官方仓库 raw LICENSE 成功；不据此推断许可缺失。文中浮动分支来源标记的是本次观察，真正发行必须重验锁定版本与嵌入内容。

部分本轮成功读取的许可文件 Git blob 标识如下，可用来核对本次文本；blob SHA 不是软件发行版本，也不能代替包/镜像内容核验。

| 文件 | 本轮 blob SHA |
| --- | --- |
| TypeScript LICENSE.txt | `8746124b277914d0f0fd9cf4aef2ed3b587143d9` |
| Vite LICENSE | `b7e97ecb6aa4dd70b3e3a637afa2ae16feb7cd12` |
| Kysely LICENSE | `d64909141c304cc9dc908e897e501dd9909fe95a` |
| pg-boss LICENSE | `e37b5797983fa2c3bbab44c9aa4291956253e142` |
| libvirt COPYING.LESSER | `e5ab03e1238af66de157fae2e6270d7e8f967f93` |
| Hermes LICENSE | `75410e73319c72cd3e991a501c5455eb78f38375` |
| OpenClaw LICENSE | `ed064819ab537575d0cad902f7e5af4a3488b1d0` |
| OpenCode LICENSE | `6439474beed8e0271df9862eff97ffd70ec2464c` |
| Octop / Octop Harness LICENSE | `10baa764eb9e15629dde4b775ec5967765f40e8d` |
| DSH LICENSE | `c1f7a78e89e4e4dc7b86664c3b3c76eb5eee1785` |

## 分发边界的依据

MIT对应原文中的版权与许可保留要求，Apache-2.0第4节规定许可、修改及NOTICE处理；MPL2第3节规定源码/可执行形式分发，Mozilla FAQ说明文件级copyleft。项目选择不把上游实现改标MIT，并按实际交付方式保存完整材料。[Apache原文](https://www.apache.org/licenses/LICENSE-2.0)、[MPL原文](https://www.mozilla.org/en-US/MPL/2.0/)、[MPL FAQ](https://www.mozilla.org/en-US/MPL/2.0/FAQ/)

QEMU官方明确其发行包含许可独立的固件，libvirt官方区分C库与其他代码；因此“宿主需要这些工具”和“Harniqo给用户分发它们的镜像”不能共用一个未经清点的许可结论。本文没有审查任何尚未构建的guest镜像或完整传递依赖图。

## OS、签名和当前未实证的条件

- Apple在2026-10-26列出macOS15.8.2、26.7.2和27.2安全更新；首批正式测试按项目决定只覆盖macOS26 arm64、macOS15 x64。这个公开列表不承诺未来更新期限，也不等于本项目已在这些系统运行。[Apple官方安全更新](https://support.apple.com/en-us/100100)
- Microsoft生命周期页列Windows11 Home/Pro 25H2结束日期2027-10-13；24H2结束日期2026-10-14。首批选择25H2 x64，未自动把Windows Server CI构建视作Windows11桌面验收。[官方生命周期](https://learn.microsoft.com/en-us/lifecycle/products/windows-11-home-and-pro)
- Ubuntu24.04 LTS处于受支持维护范围；首批选择24.04 x64，后续补丁、内核与架构按实际验收记录。[Canonical维护周期](https://ubuntu.com/about/release-cycle)
- Apple Developer ID与公证、Windows代码签名需要相应身份、密钥和可用流程。只读文档不证明项目持有这些资源；本轮未取得成功签名产物。Windows签名服务有账户/地区/权限条件，未替用户购买或开通。[Apple Developer ID](https://developer.apple.com/help/account/certificates/create-developer-id-certificates/)、[Electron签名](https://www.electronjs.org/docs/latest/tutorial/code-signing)、[Microsoft Artifact Signing](https://learn.microsoft.com/en-us/azure/artifact-signing/overview)

## 结论与后续动作

当前可以形成自有MIT与拟用直接依赖的来源/许可基线，不能据此认证传递依赖、全部镜像与最终安装包。M0-06按当前基线评审，M0-07因真实资源、签名、模型及备份证据缺失保持未完成。M1补完整解析锁与原生CI，M2补镜像清单，M7/M8补完整恢复、发行物SBOM与实际分发资料。

## 本轮开发更新

同日后续已按指定服务器的实际隔离环境生成 `scripts/contracts/requirements.lock`：20 个精确包版本、434 个官方 PyPI SHA-256，并附 metadata 来源/hash；旧段落中“待补完整解析锁”是生成前状态。哈希解析与契约验证已运行，源码/镜像与后续发行仍须分别履约。Bootstrap pip 留在私有工具缓存，不随项目分发。
