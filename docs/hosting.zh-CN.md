# 托管 Quarry

[English](hosting.md) · [本地安装](install.md) · [Rust 架构](rust-preview.md)

Quarry 托管版由一个官方 Docker 镜像提供网页、登录、只读数据库 API 和查询执行。
部署者提供数据库与权限配置，无需编写 Rust 适配器或重新编译网页。
YiminLab 使用同一个镜像，通过运行时配置复用现有 IAM。

原有本地模式保持独立：安装 `qy` 后，CLI、`qy gui`、`qy mcp` 不依赖这个服务器、
平台登录或 `ylab`。两种模式使用相同的查询内核，但不会强制迁移已有本地配置和记录。

## 用镜像启动

使用 [Compose 配置](../deploy/hosted/compose.yaml)，将
[hosted.example.json](../deploy/hosted/hosted.example.json) 复制到它旁边的
`config/hosted.json`。这一个文件包含账号、数据库连接和访问权限：

```json
{
  "listen": "0.0.0.0:8080",
  "state_dir": "/var/lib/quarry",
  "auth": {
    "mode": "builtin",
    "username": "admin",
    "password_hash": "替换为生成的 Argon2id 哈希"
  },
  "databases": [{
    "id": "notes",
    "project": "demo",
    "db": "notes",
    "env": "production",
    "url": "postgresql://readonly-user:替换密码@database:5432/notes",
    "production": true,
    "readers": ["admin"]
  }]
}
```

先生成管理员密码哈希，填入配置，再启动：

```sh
docker run --rm -it ghcr.io/yiminspace/quarry:hosted hash-password
docker compose up -d
```

打开 `http://127.0.0.1:8080/quarry/`，使用配置的管理员账号登录。
`username` 是登录账号；可选的 `user_id` 默认 `admin`，是稳定的权限和个人记录标识。
资源必须有明确的 `readers`；空列表不允许任何人访问。
数据库连接的 `production = true` 保持生产环境默认手动执行。

Compose 默认只绑定回环地址；对外部署时通过你的 HTTPS 反代暴露服务。
生产部署建议固定 `QUARRY_IMAGE_TAG=sha-<完整源码SHA>`，避免漂移到不同版本。
配置只读挂载，需可由镜像用户 UID 10001 读取；`quarry-state` 卷保留登录会话等服务状态。
`/health` 返回服务与版本，镜像的 `check` 命令可验证配置。
数据库连接密码、管理员密码哈希和部署私钥都不应打进镜像或提交到公开仓库。
已有独立 workspace 配置仍可通过高级的 `config_path`、`workspaces`、`resources` 接入。

## 接入已有账号体系

镜像无需变化，把 `auth` 改为 `external`：

```json
{
  "mode": "external",
  "verify_url": "https://iam.example.com/api/authorize",
  "response": "headers",
  "session_key": "company.session.v1",
  "login_url": "/login?return=/quarry/",
  "refresh_path": "/api/auth/refresh"
}
```

`verify_url` 是服务端逐次核验身份和权限的入口；`headers` 模式读取可信 IAM 返回的身份头，
`json` 模式读取 `id`、`roles` 和 `permissions`。访问同时需要相应权限和资源 `readers` 授权。
不能把浏览器传来的账号头当作已验证身份，也不能只解码 token 就认为会话有效。
权限名为 `lab:db:read`、`lab:db:local`、`lab:db:schema:export`、`lab:ai:invoke`。

浏览器登录、刷新及可选的 `logout_path` 都是同源路径，由平台已有服务提供，必要时通过
反代挂载。外部会话格式为：

```json
{"access_token":"...","refresh_token":"...","expires_in":3600,"user":{"id":"stable-account-id"}}
```

网页从 `/api/hosted/config` 读取非敏感的运行时配置，然后使用配置的会话存储键和认证路径。
YiminLab 配置 `ylab.console.session.v1`、现有 Console 登录页和 IAM 刷新接口即可，
不再维护另一套数据库执行镜像或定制构建。
内部私网 HTTP 身份接口需要显式 `allow_insecure_http: true`；公网接口保持 HTTPS。

## 用户电脑上的个人记录

服务器负责数据库连接、资源授权和查询；个人草稿、标签、历史、有限结果缓存、助手对话、
生成 SQL 和恢复点保存在用户电脑的 SQLite 中，与 CLI 共用，不存进托管服务器。

用户运行 `ylab db connect` 后，网页自动连接本机服务并保存，不显示地址、配对码或
数据库管理表单。独立托管使用一次性登录交接，已有平台使用现有账号确认流程。
交接码仅通过 URL fragment 传递，网页在发请求前清除；长期 Bearer token 不进入 URL。
本机授权仍绑定平台、账号和来源，有期限且可撤销，不开放任意文件访问。

本机暂时未连接时，网页仍可查询远程数据库和编辑，使用有容量上限的浏览器恢复副本。
恢复连接后自动重试保存；遇到版本冲突时保留当前草稿，由用户选择导出和恢复，
不会静默覆盖另一方记录。重连不会运行 SQL、启动数据库或同步结构。

网页首次成功加载并保留离线缓存后，可以断网重新打开和编辑；第一次访问仍需要加载网页。
共享 SQL、云端个人历史和跨设备同步不在当前范围内。

## 助手与验收

配置 `assistant_base_url` 和 `assistant_model` 可启用 SQL 助手，两者同时省略即可禁用。
模型网关必须接受当前用户的 Bearer 认证；建议可应用、恢复，不会自动运行。
远程查询会把 SQL 发到服务器，助手会把必要的上下文发到模型网关；平台日志保留策略由部署者管理。

发布验收分别覆盖：真实容器启动和持久卷、管理员登录与刷新/退出、外部 IAM 与资源越权拒绝、
真实查询/写入拦截/取消、本机 SQLite 自动保存和断线恢复，以及原有本地 CLI/GUI/MCP 回归。
GUI 模拟预览或编译成功不能代替这些验收。

Rust 类库嵌入仍可作为高级接入方式使用；相关边界见 [Rust 架构](rust-preview.md)
和 [YiminLab 接入约定](ylab-integration.md)。
