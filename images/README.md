# Harniqo 镜像目录

镜像根目录为工作区根目录下的 `images/`。指定服务器的绝对路径记录在 `.private/development-environment.md`。

```text
images/
  README.md                    本规范，进入 Git
  manifest.example.json        脱敏清单格式，进入 Git
  manifest.local.json          实际镜像、校验值和本机路径，不进入 Git
  containers/                  容器 OCI/Docker 镜像归档
  vm/base/                     校验后的只读 VM 基础镜像
  vm/instances/                各工作区/Trial 独立 VM 写入层
```

公开仓库只包含规范、构建配方、锁和脱敏来源记录。指定验收环境已保存并校验 Ubuntu KVM 基础镜像、Hermes 的两工具与四工具组合归档，以及 OpenCode1.19.5 修复前后两份运行包；本轮新增Octop Harness1.0.5归档。镜像二进制位于本目录下的受控子目录；来源证明按各配方保存在归档旁或私有构建报告中，不进入 Git。构建、运行及验收状态分别见 [M0 证据索引](../docs/planning/M0-RESULTS.md)，构建时的 `qualification_passed:false` 不由后续用例覆盖，追加验收报告另行关联 image ID 与 recipe SHA。

Octop Harness实际image ID为`sha256:714e015d1c1281e1c91bd2ae67f39191b99c408eb7523b62a409831e912a73d5`，Docker archive位于`containers/octop/harness/`，496,659,456 bytes，SHA-256为`f5fa07a06006c9f1dca7471bb3135ba22834eb22a7914c1754731b9d2334c470`；93包固定锁与来源见 [配方](../deploy/qualification/octop/README.md)。Octop完整服务另有867,920,384 bytes归档，SHA-256 `1506f719e58af360203a113d51c11a2ccae9709fb62d1400efc1f94812547f70`；OpenClaw官方固定归档674,162,688 bytes，SHA-256 `adab4f92ac77655b55573eb1560eac9d77c99199e09a496d78148bdc91f23804`。三份新增归档均已再次核验并追加到私有清单，合计8项，原5项不改写。两轮NIC资格使用既有Ubuntu base，临时网络/VM写入层已回收，不产生可复用生产基础镜像。

## 命名与来源

- 容器归档：`containers/<component>/<version>/<image-digest>.oci.tar`，登记 registry 引用、不可变 digest、平台、源码提交与归档 SHA-256。使用 Docker archive 时显式标明格式。
- VM 基础镜像：`vm/base/<distribution>/<version>/<architecture>/<sha256>.qcow2`，登记原始发行来源、系统/内核/依赖、构建脚本版本、磁盘格式和校验值。使用 raw 等格式时按真实格式命名。
- 交互工作区写入层：`vm/instances/<workspace-id>/active.qcow2`，绑定稳定工作区身份，同一获准工作区的连续 Run 可保留状态且仅允许单写者。批量试验写入层：`vm/instances/<trial-id>/<attempt-id>.qcow2`，每个 Trial 独立；Attempt 记录复用、快照或分叉关系，旧执行确认终止后才可继续写。不得跨组织、未授权工作区、不同 Trial 或并行候选复用。清单记录 backing image digest、所属资源和生命周期。
- 移动或导入前验证 SHA-256，加载后另查实际容器 image ID 及可用的 manifest descriptor。Docker image ID 的含义随镜像存储后端而异，不能直接当作 config digest 或已发布 registry digest。归档校验值、镜像存储身份和 registry 内容摘要分别记录，不互相替代。

## 存储与权限

镜像二进制、工作区状态和真实清单由 `.gitignore` 排除；源码、Dockerfile、镜像构建脚本和脱敏清单应进入 Git。Docker daemon 的内部数据目录遵循服务配置，本目录保存可归档/重新加载的镜像文件；不擅自迁移系统 Docker 数据。

基础镜像不得写入密钥、会话或业务文件。执行写入层可能含敏感数据，须限权访问和按保留策略销毁。该目录不得通过静态 Web 服务公开；Agent 无权访问其他镜像或实例路径。镜像发布到 registry 时仍保留本地已验收版本归档和清单。

构建前检查实际可用空间，记录预计体积和配额。回收前核对真实路径、活动 VM/容器、引用关系、发布回退和备份保留；有活跃引用的基础镜像与写入层不得删除。回收后用 `df` 记录实际可用空间变化，不能把 `du` 总量当成释放量。
