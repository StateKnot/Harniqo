# macOS arm64 内部安装

适用范围是 M1-01 的 macOS 26 arm64 开发验收。使用仓库本机原生构建留下的 `.app`、构建清单和锁定依赖。可信的本机打包产物来自[桌面工程入口](../../apps/desktop/README.md)。安装入口先核对清单、实际 Git 提交和当前文件摘要，拒绝过期或脏工作区构建。

在原生 macOS 图形会话中，从仓库根目录执行。Node 必须为项目固定版本；收据目录须已存在，收据路径须为新的绝对路径。

```sh
df -h .
node scripts/desktop/install-macos-arm64.mjs /absolute/path/to/new-install-receipt.json
```

脚本将应用和随附许可材料复制到当前用户 Applications 下按版本和源码提交区分的目录。目录已存在即拒绝。脚本仅在本次安装副本上使用锁定的 Electron 签名工具施加本机 ad-hoc 封签，并用系统工具严格验证签名和 arm64 架构。安装前后 `app.asar` 摘要一致。原始包继续用作构建来源。

安装后从用户 Applications 目录直接启动实际包内可执行文件。冒烟使用独立临时配置，核对原生进程、React 渲染、preload 隔离和来源标识。安装及冒烟出错时，收据记录原因和本轮目录清理结果。移除内测版时，退出应用并在 Finder 删除该安装目录。

本机签名使用 ad-hoc 身份和工程运行配置。正式发行的 Developer ID、Hardened Runtime、公证、Gatekeeper 评估、更新和完整用户流程在 M8 验收。该冒烟覆盖已安装包的直接启动，Finder 交互和普通用户配置另行验证。[Apple 签名验证](https://developer.apple.com/library/archive/technotes/tn2206/)
