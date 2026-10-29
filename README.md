# Harniqo

**The open-source control center for agent harnesses.**

开源的 Agent Harness 控制中心：通过 macOS、Windows、Linux 客户端，在自有云部署、使用和比较不同 Agent Harness。

## 项目状态

目前处于开发规划阶段，尚无可安装应用或已认证的 Harness 集成。生产目标同时覆盖团队私有部署和多租户 SaaS；执行环境首版限定为 Linux，客户端覆盖三端。

| 文档 | 用途 |
| --- | --- |
| [技术架构](docs/architecture/ARCHITECTURE.md) | 技术栈、七个业务 Module、数据与接口、执行隔离、双部署和关键决策 |
| [开发方案与里程碑](docs/planning/ROADMAP.md) | 产品范围、架构、阶段依赖与发布门槛 |
| [开发任务清单](docs/planning/BACKLOG.md) | 67 项任务，含交付物和验收条件 |
| [验收与执行规则](docs/planning/ACCEPTANCE.md) | 真实环境验收、失败场景与证据要求 |
| [领域术语](GLOSSARY.md) | 后续文档、API 与代码采用的统一概念 |
| [预研报告](research/multi-harness-client-research.md) | 候选运行时、协议、复用边界和资料来源 |

## 命名

Harniqo 读作「哈尼科」，取意于 Harness 与 IQ。建议仓库名 `harniqo`，未来命令名使用同一拼写，界面品牌使用 `Harniqo`。

2026-10-29 对 GitHub 仓库名称及公开搜索进行了重名初筛，未发现明显同名软件项目；域名、包名与商标尚未核验或注册。当前文档采用此项目名。

正式仓库：[StateKnot/Harniqo](https://github.com/StateKnot/Harniqo)。项目采用 **MIT** 许可证；第三方依赖、数据和集成运行时分别遵循各自许可。镜像存放规范见 [images](images/README.md)。
