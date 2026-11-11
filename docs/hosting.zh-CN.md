# 托管 Quarry

[English](hosting.md) · [本地安装](install.md) · [Rust 架构](rust-preview.md)

Quarry 托管版由一个官方 Docker 镜像提供网页、登录、只读数据库 API 和查询执行。
部署者提供数据库与权限配置，可以使用镜像内置账号，也可以接入自己的身份系统。

## 选择工作台

| 入口 | 数据库连接 | 个人记录 |
| --- | --- | --- |
| 托管网页 `/quarry/` | 当前账号有权访问的服务端连接 | 草稿和 SQL 历史按账号保存在当前浏览器 |
| 内嵌本机工作台 | 本机工作区和已授权的服务端连接 | 工作台状态和执行历史保存在本机 SQLite |
| 独立 `qy gui` | Quarry 工作区和已登记的云端来源 | 受保护的工作台使用本机 SQLite，纯本地模式使用浏览器存储 |

三种入口使用同一套查询内核。内嵌客户端在本机执行本地连接的查询，并使用选定账号转发云端请求。
托管服务保管数据库连接凭据并逐请求校验权限。

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

## 构建和更新托管镜像

[Dockerfile](../Dockerfile.hosted) 先用 Vite 构建 React 网页资源，再在编译时将资源嵌入
Rust 的 `quarry-server` 二进制。最终镜像包含该二进制及其运行依赖。CI 验证 Linux amd64
和 arm64 镜像后，以源码提交 SHA 为标签发布到 GHCR。

工作台会为更新的稳定托管版本显示发布说明入口，每个浏览器可以关闭该版本的提醒。
从发布说明选择新镜像，更新 Compose 环境中的
`QUARRY_IMAGE_TAG`，运行 `docker compose pull quarry` 和 `docker compose up -d quarry`，
然后通过 `/health` 确认正在运行的版本。

支持自更新的独立安装使用 [`qy update`](install.md#update)。内嵌工作台随宿主应用打包，
通过宿主的发布渠道更新。

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

Quarry 每次授权时向 `verify_url` 发送 GET 请求，携带 Bearer 令牌以及 `X-Required-Permission`
和 `X-Required-Scope`。身份服务验证会话和权限，通过时返回 HTTP 200。`json` 模式读取响应中的
`id`、`roles` 和 `permissions`，`headers` 模式读取 `X-User-Id`、`X-User-Roles` 和
`X-User-Permissions`，多个角色或权限以逗号分隔。Quarry 再对目标资源检查 `readers`。
不能把浏览器传来的账号头当作已验证身份，也不能只解码 token 就认为会话有效。
权限名为 `lab:db:read`、`lab:db:local`、`lab:db:schema:export`、`lab:ai:invoke`。

浏览器登录、刷新及可选的 `logout_path` 都是同源路径，由平台已有服务提供，必要时通过
反代挂载。外部会话格式为：

```json
{"access_token":"...","refresh_token":"...","expires_in":3600,"user":{"id":"stable-account-id"}}
```

网页从 `/api/hosted/config` 读取公开的运行时配置。平台在登录后将会话写入配置的存储键，
再把浏览器带回 `/quarry/`。反向代理需将 `/quarry/`、`/api/hosted/*` 和 `/api/db/*` 路由到托管服务，
并将平台自己的登录、续期和退出路径路由到对应服务。
内部私网 HTTP 身份接口需要显式 `allow_insecure_http: true`；公网接口保持 HTTPS。

## 网页和本机工作台

托管网页展示当前账号有权访问的云端连接。草稿和 SQL 历史保存在当前浏览器，
按账号隔离。查询结果保留在当前页面。首次成功加载并保留缓存后，网页也能断网打开编辑。

在 Rust 客户端嵌入本机工作台时，先构建网页资源，再调用 `quarry_cli::run_gui_from`，
并传入宿主实现的 `CloudBackend` 来提供云端目录和操作。宿主负责选择账号和管理会话凭据。

## 助手与验收

配置 `assistant_base_url` 和 `assistant_model` 可启用 SQL 助手，两者同时省略即可禁用。
模型网关必须接受当前用户的 Bearer 认证；建议可应用、恢复，不会自动运行。
远程查询会把 SQL 发到服务器，助手会把必要的上下文发到模型网关；平台日志保留策略由部署者管理。

发布验收分别覆盖：真实容器启动和持久卷、管理员登录与刷新/退出、外部 IAM 与资源越权拒绝、
真实查询/写入拦截/取消、本机 SQLite 保存和离线重启，以及原有本地 CLI/GUI/MCP 回归。
GUI 模拟预览或编译成功不能代替这些验收。

在应用中嵌入 Quarry 的方式见 [Rust 架构](rust-preview.md)。
