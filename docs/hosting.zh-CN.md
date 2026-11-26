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

如需从 CLI 打开远程单库工作台，在独立 external-auth 服务上启用 `auth.cli_handoff`。
CLI 携带外部 Bearer 和 `resourceId` 请求 `POST /api/db/browser-ticket`，再通过
`/quarry/#quarry_handoff=<code>` 打开浏览器。票据 60 秒内仅可兑换一次，
浏览器用限定路径的 Secure、HttpOnly Cookie 访问单个库，会话 30 分钟到期。
票据和会话仅存在当前进程内存中。启用时使用 HTTPS 和单副本，浏览器每次访问仍检查当前 IAM 授权。

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

## 云端 Local

托管服务可以在服务器上为每个用户单独起 PostgreSQL 和 Redis 调试库，体验和本机 Local 一样：
可以选择镜像版本，有完整权限，本机服务也能直连。数据库跑在一个独立的 Docker-in-Docker
服务里，端口只对 Quarry 开放，不在宿主机上发布。

```sh
cp hosted.cloud-local.example.json config/hosted.json   # 补全账号和数据库后
docker compose -f compose.yaml -f compose.cloud-local.yaml up -d
```

`hosted.json` 里的 `cloud_local` 段：

| 字段 | 说明 |
| --- | --- |
| `docker_host` / `docker_cert_path` | Quarry 访问 dind 的地址和 TLS 客户端证书目录，Compose 叠加文件里分别是 `tcp://docker:2376` 和 `/certs/client` |
| `connect_host` | Quarry 连接数据库容器时使用的主机名，Compose 里是 `docker` |
| `ports` | dind 内部分配给 Local 的端口段，默认 `[20000, 20999]` |
| `max_per_user` | 每人最多几个 Local，默认 5 |
| `memory` / `cpus` | 每个容器的资源上限，例如 `"1g"`、`"1"` |
| `images` | 允许的镜像前缀；省略时为 `postgres:`、`postgis/postgis:`、`pgvector/pgvector:`、`redis:`、`redis/redis-stack-server:`、`valkey/valkey:` |
| `ticket_hours` | 隧道凭证有效期，默认 12 小时 |

使用方式：

1. 在网页里打开某个 PostgreSQL 或 Redis 库的连接详情，点“创建云端 Local”，可选填镜像版本。
2. 新的 Local 会作为这个库下的“云端 Local”环境出现，在网页和 CLI 里查询它和查询其他云端库一样。
3. 要让本机服务连接，在它的连接详情里点“从本机连接”，复制命令运行：

   ```sh
   qy local connect https://quarry.example.com/quarry/ --port 5433 --ticket qt_...
   ```

   之后服务使用网页上显示的连接地址即可（如 `postgresql://quarry:<该 Local 的密码>@127.0.0.1:5433/<库名>`）。每个 Local 有独立的 Docker 网络和随机密码，只有创建者能拿到，其他用户的容器既连不到它，也登录不了。隧道把字节原样转发，
   事务、预编译语句、扩展、迁移工具都可以正常使用。

需要 `lab:db:local` 权限。每个 Local 只属于创建它的账号；凭证按账号和 Local 绑定，删除 Local 时一并作废。
网页查询仍然只读，写入通过隧道完成，和本机 Local 的规则一致。PostgreSQL Local 可以从来源库同步结构，
只传结构，不复制业务数据。

Docker-in-Docker 需要 privileged 容器，请部署在可信主机上，或改用 Sysbox 运行时。
云端 Local 里的数据保存在部署者的服务器（`cloud-local-docker` 卷）上，备份和保留策略由部署者负责。

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
