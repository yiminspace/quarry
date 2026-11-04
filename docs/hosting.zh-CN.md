# 把 Quarry 集成到你的平台

[English](hosting.md) · [独立安装](install.md) · [Rust 架构](rust-preview.md)

Quarry 可以独立提供 CLI、GUI 和 MCP，也可以成为已有平台中的数据库工作台。
平台负责账号、权限、资源登记、凭据与部署；Quarry 提供 Rust 执行类库、只读托管 API、
工作台界面，以及需要明确配对的本机服务。

本文的托管和本机服务接口由当前源码 SDK 提供，与独立的 2.0.0 发布包分别管理。
目前这是类库接入方式，没有通用的一键托管安装器。公开的 `quarry-platform` 后端接受
宿主提供的 IAM 适配器；现有网页适配器使用 Ylab 会话约定，其他平台还需要适配网页会话。
Ylab 的服务可执行文件与部署配置由宿主管理，不包含在公开 Quarry 安装包中。

## 选择接入层

| 你的需求 | 使用方式 |
| --- | --- |
| 在自己的电脑上使用工作台 | 安装 `qy`，配置 workspace，再运行 `qy gui` |
| 让 agent 使用数据库 | 通过 stdio 启动 `qy mcp` |
| 在已有 Rust 应用中执行查询 | 嵌入 `quarry-service::QuarryService` |
| 在自己的 CLI 中提供 Quarry 命令 | 嵌入 `quarry-cli::run_from`，提供宿主的守护进程入口 |
| 平台 IAM 工作台与本机个人记录 | 接入 `quarry-platform`、托管网页适配器和 CLI 本机服务 |

依赖固定的 Quarry 源码版本，类库就在本仓库中。宿主不需要另外安装 `qy`，也不调用
Python 子进程。编译内嵌 GUI 的程序前，需要先构建 `web/`。可先运行公开的
[类库示例](../crates/quarry-service/examples/embedded_host.rs)，构建步骤见
[Rust 架构](rust-preview.md)。

## 1. 在服务端登记资源

用明确的配置文件、workspace 和状态目录创建 `QuarryService`。workspace 与数据库
凭据保留在服务端。通过 `HostedResource` 登记每个开放目标：稳定 ID、项目、逻辑库、
环境、服务端连接 key，以及明确的 `readers` 授权列表。列表可包含账号 ID 或
`role:<角色名>`；空列表代表无人可访问。生产连接需要明确设置 `production = true`。

网页只选择已登记的资源 ID，不能传入 workspace 路径、连接 URL 或数据库密码。
远程查询保持只读，结构查看、取消查询和 schema 导出也只能操作已授权目标。

## 2. 接入自己的 IAM

实现异步 `Authorizer`。**每次调用**都验证 Bearer 会话仍有效、所请求权限与 token scope
允许该操作，再返回稳定账号 ID 和当前角色组成的 `Principal`。不能相信浏览器传来的
身份头，也不能只解码 token 而不检查会话有效性。

当前类库使用下列权限名。平台命名不同时，在 `Authorizer` 内映射到自己的权限策略。

| 权限 | 能力 |
| --- | --- |
| `lab:db:read` | 远程只读查询、结构查看与取消 |
| `lab:db:schema:export` | PostgreSQL 结构导出，同时需要读取权限 |
| `lab:db:local` | 初次本机配对，以及需要云端授权的 Local 操作 |
| `lab:ai:invoke` | 助手模型请求，同时需要相应目标访问权限 |

一次操作必须同时满足 IAM 权限和资源授权。给账号分配数据库权限，不代表开放全部资源。
首次接入可先只给一个账号和一份明确的资源列表。

把执行服务、IAM 实现和资源表传给公开路由：

```rust
use std::sync::Arc;
use axum::Router;
use quarry_platform::{Authorizer, HostedResource, HostedState, hosted_router};
use quarry_service::QuarryService;

fn database_api(
    service: Arc<QuarryService>,
    iam: Arc<dyn Authorizer>,
    resources: Vec<HostedResource>,
) -> quarry_platform::Result<Router> {
    let state = HostedState::new(service, iam, resources)?;
    Ok(hosted_router(state))
}
```

这段代码只提供 API 路由，监听端口、HTTPS 网关、IAM 实现和网页托管仍由你的应用负责。
路由接收 `/api/platform`、`/api/query`、`/api/schema/export` 等路径。已有后端不是 Rust
时，也可以在网关后运行一个薄 Rust 执行服务，通过适配器向现有 IAM 逐次核验权限。

## 3. 接入网页会话

在平台自己的 HTTPS 来源下提供工作台。独立页面不会读取平台身份；托管副本需要包含：

```html
<meta name="quarry-mode" content="platform">
```

以 [`web/src/platform.ts`](../web/src/platform.ts) 为当前适配器，接入你的登录、续期、
切换账号和退出行为。现有 Ylab 约定为：

- 会话保存在 `ylab.console.session.v1`，包含 `access_token`、`refresh_token`、
  `expires_in` 与 `user.id`。
- 通过 `/api/iam/auth/refresh` 续期。
- 网页请求 `/api/db/*`，网关转发成执行服务的 `/api/*`，例如
  `/api/db/platform` → `/api/platform`。
- 远程请求使用 Bearer 授权，访问回环地址的本机服务必须另行配对。保留来源校验，
  处理浏览器的本地网络访问许可流程。

只添加 meta 标记还不够。会话适配和网关路径需要配套，切换账号时必须隔离个人恢复数据
和配对数据。平台登录不能自动授予对用户电脑的访问权。

## 4. 在自己的 CLI 中加入本机服务

使用 `quarry-cli::run_from` 嵌入独立命令能力，再用 `CompanionConfig`、`CompanionState`
和 `companion_router` 提供自己的本机服务命令。指定可信平台的准确 origin、当前 IAM
账号、SQLite 绝对路径和回环端口。CLI 自己管理账号/profile、打包、进程生命周期，以及
需要的 keeper/Neptune 隐藏分发入口。

一次性配对码只在用户终端显示，网页由用户明确配对到这台电脑、平台和账号。配对后使用
有有效期且可撤销的本机授权，不开放任意文件访问、连接凭据或通用 Docker 控制接口。
容器、卷、端口和 Local 资源状态按平台、账号和源资源隔离。

| 数据或操作 | 保存位置与行为 |
| --- | --- |
| 连接凭据、资源表 | 服务端，不返回网页 |
| 草稿、标签、查询历史、有限结果缓存、助手对话、生成 SQL、恢复点 | 电脑上的 SQLite，按平台、账号和目标隔离，网页与 CLI 共用 |
| 本机服务断开时的编辑 | 有大小限制且按账号隔离的浏览器恢复副本；不冒充已写入 SQLite |
| 同时修改个人记录 | 通过版本检查报告冲突，不静默覆盖另一方 |
| Local PostgreSQL / Redis | 通过配对服务明确执行 Docker 生命周期操作 |
| PostgreSQL 结构迁移 | 本机服务获取已授权源，导入临时库并保留旧 Local 库；不复制业务行或源密码 |

首次成功加载且浏览器仍保留缓存时，页面可离线重新打开和编辑。远程与本机连接分别显示
状态。已配对的本机操作在本机授权有效期内可离线使用；远程查询、模型调用和远程结构
获取仍需要实时 IAM。重连不会自动执行 SQL、启动 Docker、导入结构或替换未保存草稿。

## 5. 配置助手并验收访问边界

可通过 `AssistantOptions` 配置可信模型网关和模型。网关需要支持调用者的 IAM 授权。
助手建议可以应用和恢复，不会自动执行。个人记录持久化在本机 SQLite；实际远程查询会
把 SQL 发到平台，模型请求会把所需上下文发到网关。平台、数据库和模型日志的保留时间
由你的平台策略决定。

开放工作台前，用真实 IAM 与网关验证：匿名、跨账号和跨资源拒绝，token scope 与撤销，
取消操作归属，写入拦截，配对过期/撤销，错误来源，账号切换，远程与本机分别断连，
SQLite 重启持久化与并发修改冲突。验证 PostgreSQL 迁移不含业务行、保留旧 Local 库，
失败时可恢复；验证部署后的 HTTPS 页面能在支持的桌面浏览器中连接本机服务。

托管共享 SQL、云端个人历史、跨设备同步尚未实现；独立模式的本地 `.sql` 查询文件仍可
使用。结构迁移仅支持 PostgreSQL。Neptune/openCypher 仍为实验性支持，本地空端点不
等于图数据库。其他边界见 [兼容性](../COMPATIBILITY.md) 与
[YiminLab 接入约定](ylab-integration.md)。
