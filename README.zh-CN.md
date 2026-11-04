# Quarry 2

Rust 原生数据库工作台，共用内核提供 CLI、GUI、MCP 与类库接口。
支持 PostgreSQL、MySQL、SQLite、Redis 及实验性的 Neptune。

## 安装与升级

当前为 2.0 候选版：

```sh
pip install --upgrade --only-binary=:all: quarry-db==2.0.0rc1
qy --version
qy gui
```

也可从 [GitHub Releases](https://github.com/yiminspace/quarry/releases) 下载原生安装包及校验和。
二进制运行不依赖 Python 或 Rust 工具链。支持 macOS/Linux 的 arm64/x86_64；
Linux 需要 glibc 2.28+。完整步骤见 [安装和升级](docs/install.md)。

main 及 2.x 发布由 Rust 接管；Python 1.11.0 保存在原标签和受保护的
`archive/python-1.x` 分支。2.x 不再提供 Python import API；源码中的旧实现仅用于开发验收。
配置、保存查询和助手历史继续沿用。GUI 默认只读，CLI/MCP 写入需要显式授权。

SSH、Docker 和 PostgreSQL schema sync 分别按需使用系统工具；普通查询使用原生驱动。
类库按 core/runtime/local/assistant/service 分层，供宿主 CLI 直接共用。

[英文文档](README.md) · [GUI 功能矩阵](TESTING.md) · [验收边界](docs/rust-verification.md)
