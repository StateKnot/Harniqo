# Harniqo 镜像目录

镜像根目录为工作区根目录下的 `images/`。指定服务器的绝对路径记录在 `.private/development-environment.md`。

```text
images/
  README.md                    本规范，进入 Git
  manifest.example.json        脱敏清单格式，进入 Git
  manifest.local.json          实际镜像、校验值和本机路径，不进入 Git
  containers/                  容器 OCI/Docker 镜像归档
  toolchains/                  固定运行/构建工具链的上游发行归档
  vm/base/                     校验后的只读 VM 基础镜像
  vm/instances/                各工作区/Trial 独立 VM 写入层
```

公开仓库只包含规范、构建配方、锁和脱敏来源记录。指定验收环境已保存并校验 Ubuntu KVM 基础镜像、Hermes 的两工具与四工具组合归档，以及 OpenCode1.19.5 修复前后两份运行包；本轮新增Octop Harness1.0.5归档。镜像二进制位于本目录下的受控子目录；来源证明按各配方保存在归档旁或私有构建报告中，不进入 Git。构建、运行及验收状态分别见 [M0 证据索引](../docs/planning/M0-RESULTS.md)，构建时的 `qualification_passed:false` 不由后续用例覆盖，追加验收报告另行关联 image ID 与 recipe SHA。

Octop Harness实际image ID为`sha256:714e015d1c1281e1c91bd2ae67f39191b99c408eb7523b62a409831e912a73d5`，Docker archive位于`containers/octop/harness/`，496,659,456 bytes，SHA-256为`f5fa07a06006c9f1dca7471bb3135ba22834eb22a7914c1754731b9d2334c470`；93包固定锁与来源见 [配方](../deploy/qualification/octop/README.md)。Octop完整服务另有867,920,384 bytes归档，SHA-256 `1506f719e58af360203a113d51c11a2ccae9709fb62d1400efc1f94812547f70`；OpenClaw官方固定归档674,162,688 bytes，SHA-256 `adab4f92ac77655b55573eb1560eac9d77c99199e09a496d78148bdc91f23804`。三份新增归档均已再次核验并追加到私有清单，合计8项，原5项不改写。两轮NIC资格使用既有Ubuntu base，临时网络/VM写入层已回收，不产生可复用生产基础镜像。

## 命名与来源

- 容器归档：`containers/<component>/<version>/<image-digest>.oci.tar`，登记 registry 引用、不可变 digest、平台、源码提交与归档 SHA-256。使用 Docker archive 时显式标明格式。
- 工具链归档：`toolchains/<component>/<version>/<upstream-filename>`，登记原始HTTPS来源、平台、精确版本、归档大小/SHA、官方manifest来源及实际核验方式；不是VM或容器镜像。发布签名未验证时明确记为`false`，不得以同站点checksum代替签名声明。解包运行目录属于项目`.state/`，不覆盖系统运行时。
- VM 基础镜像：`vm/base/<distribution>/<version>/<architecture>/<sha256>.qcow2`，登记原始发行来源、系统/内核/依赖、构建脚本版本、磁盘格式和校验值。使用 raw 等格式时按真实格式命名。
- 交互工作区写入层：`vm/instances/<workspace-id>/active.qcow2`，绑定稳定工作区身份，同一获准工作区的连续 Run 可保留状态且仅允许单写者。批量试验写入层：`vm/instances/<trial-id>/<attempt-id>.qcow2`，每个 Trial 独立；Attempt 记录复用、快照或分叉关系，旧执行确认终止后才可继续写。不得跨组织、未授权工作区、不同 Trial 或并行候选复用。清单记录 backing image digest、所属资源和生命周期。
- 移动或导入前验证 SHA-256，加载后另查实际容器 image ID 及可用的 manifest descriptor。Docker image ID 的含义随镜像存储后端而异，不能直接当作 config digest 或已发布 registry digest。归档校验值、镜像存储身份和 registry 内容摘要分别记录，不互相替代。

## 存储与权限

镜像二进制、工作区状态和真实清单由 `.gitignore` 排除；源码、Dockerfile、镜像构建脚本和脱敏清单应进入 Git。Docker daemon 的内部数据目录遵循服务配置，本目录保存可归档/重新加载的镜像文件；不擅自迁移系统 Docker 数据。

基础镜像不得写入密钥、会话或业务文件。执行写入层可能含敏感数据，须限权访问和按保留策略销毁。该目录不得通过静态 Web 服务公开；Agent 无权访问其他镜像或实例路径。镜像发布到 registry 时仍保留本地已验收版本归档和清单。

构建前检查实际可用空间，记录预计体积和配额。回收前核对真实路径、活动 VM/容器、引用关系、发布回退和备份保留；有活跃引用的基础镜像与写入层不得删除。回收后用 `df` 记录实际可用空间变化，不能把 `du` 总量当成释放量。

2026-10-30新增主要资格输入：DSH0.2.2-alpha.2（845,569,536 bytes）、Octop原版ACP（580,660,736 bytes）、独立修正ACP（580,673,024 bytes），完整SHA与来源见各[DSH](../deploy/qualification/dsh/README.md)/[ACP](../deploy/qualification/octop/acp-README.md)记录。三份归档重新核验后追加私有清单，合计11项，原8项不改写。其他失败构建候选保留其原构建报告，不能默认计为已通过组合。


2026-10-31新增Node.js v24.22.1 Linux x64工具链archive：`toolchains/node/v24.22.1/node-v24.22.1-linux-x64.tar.xz`，32,026,916 bytes，SHA-256 `6c172a9ab6718f7b801fb5a7bd486af2ee5e32758086a04b84fcce2aa77095e6`，旁车保留官方HTTPS manifest与provenance。已核同站点[SHASUMS256.txt](https://nodejs.org/dist/v24.22.1/SHASUMS256.txt)；`release_signature_verified=false`，未宣称发布签名、源码可复现构建或完整二进制分发SBOM通过。实际完整`LICENSE` SHA-256为`5888dbb9a1d2b18f2c3e6c5f6af1b39de658372b402a0577b002777f14c62ace`，不能用Node顶层MIT覆盖所带组件。

该项类型为`runtime-toolchain-archive`；新增后私有清单共12条，原11条保持不变。实际解释器在项目`.state/toolchains/node-v24.22.1-linux-x64`，不是全局安装。上游归档校验与最终transport资格分开：Node24.22.1严格typecheck/14项Module回归及30门槛TLS/HTTP资格已通过，后者仍不代表完整生产Egress、Guest VM模型链路或执行权限/预算/内核策略通过。归档与运行时元数据见[工具链来源](../docs/operations/node-toolchain-2026-10-31.json)，流式/拒绝/清理证据见[传输结果](../docs/operations/egress-transport-native-2026-10-31.json)。二进制、测试CA/私钥及原始报告继续留受控存储，不进入Git。


2026-10-31 Guest内存故障资格复用同一已核Canonical base与固定v2，不新增可复用镜像或工具链归档，私有manifest仍为12项。三组FAILED及最终通过组各自使用`vm/instances/`下独立8GiB raw/seed；均按本组宿主停止/日志归档证据精确回收，原始失败报告与旧未知派发材料保留。Guest临时监督脚本与log只写各自Guest/run，未写入只读base或其他实例；不能把已回收运行写层登记成生产基础镜像。[实际范围与结果](../research/m0-guest-memory-failure.md)。
