# Quarry 2

Rust 原生数据库工作台，共用内核提供 CLI、GUI、MCP 与类库接口。
支持 PostgreSQL、MySQL、SQLite、Redis 及实验性的 Neptune。

## 安装与升级

当前稳定版：

```sh
curl -fsSL https://yiminspace.github.io/quarry/install.sh | sh
qy --version
qy gui
```

也可从 [GitHub Releases](https://github.com/yiminspace/quarry/releases) 下载原生安装包及校验和。
二进制运行不依赖 Python 或 Rust 工具链。支持 macOS/Linux 的 arm64/x86_64；
Linux 需要 glibc 2.28+。安装脚本管理的版本会在后台下载并校验更新，下次启动时启用。
也可以运行 `qy update` 立即更新。完整步骤见 [安装和升级](docs/install.md)。

main 及 2.x 发布由 Rust 接管；Python 1.11.0 保存在原标签和受保护的
`archive/python-1.x` 分支。2.x 不再提供 Python import API；源码中的旧实现仅用于开发验收。
配置、保存查询和助手历史继续沿用。GUI 默认只读，CLI/MCP 写入需要显式授权。

在另一个终端运行 `qy gui stop` 可以正常关闭独立运行的本机 GUI。
如果启动时指定了 `--config`、`--workspace`、`--host` 或 `--port`，关闭时也传入相同参数。
如果用 `--port 0` 启动，关闭时传入 GUI 启动网址中显示的实际端口。

SSH、Docker 和 PostgreSQL schema sync 分别按需使用系统工具；普通查询使用原生驱动。
类库按 core/runtime/local/assistant/service 分层，供宿主 CLI 直接共用。

平台可通过当前源码 SDK 的 `quarry-platform` 接入自己的 IAM、明确的资源授权和配对本机服务。
远程查询只读，个人记录保存在电脑 SQLite；宿主负责网页会话、CLI 与部署。
YLab 内嵌的 Quarry 随 YLab 发布和升级，独立安装的 `qy` 从 Quarry Releases 更新。
公开接口、存储与离线行为见
[托管接入指南](docs/hosting.zh-CN.md)。

[英文文档](README.md) · [GUI 功能矩阵](TESTING.md) · [验收边界](docs/rust-verification.md)
