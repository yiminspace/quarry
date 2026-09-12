# Quarry 如何工作

Quarry 是一个运行在本机的数据库工作台。它把连接管理、查询执行和安全规则放在 Python 内核中，让人和 AI 通过不同入口使用同一套能力。

本文基于 2026-09-12 的仓库代码整理。

## 1. 整体结构：一个内核，多个入口

```text
人 → CLI 命令 / 浏览器 GUI ──┐
AI → MCP / skill 调用 CLI ──┼→ Python 查询内核 → 直连或 SSH 隧道 → 数据库
程序 → Python API ──────────┘
```

- **CLI**：通过 `qy` 执行查询、管理连接和保存查询，适合终端和脚本。
- **GUI**：浏览器里的表列表、SQL 编辑器、结果网格和多标签页。
- **MCP**：向 AI 提供查看连接、查看结构、执行查询等工具。
- **Agent skill**：指导 AI 如何使用 CLI、选择查询和理解结果。
- **Python API**：其他程序直接调用 Quarry 内核。

这些入口共享连接解析、安全检查和数据库适配能力，但授权方式和输出形式有所不同。GUI、MCP、Python 主要使用结构化结果；CLI 支持 JSON 行数组、CSV、NDJSON 和表格。

Quarry 通过用户配置的地址访问数据库，查询和结果不需要经过 Quarry 云服务。

## 2. 配置与保存的查询放在哪里

一个 workspace 就是一个目录：

```text
workspace/
├── connections.toml       # 连接地址、引擎、环境、SSH 等配置
└── queries/
    └── shop/
        └── recent_orders.sql
```

查找 workspace 的优先级是：`--workspace` 指定目录 → `~/.config/quarry/config.toml` 登记的目录 → 当前目录。

可以同时加载多个 workspace。CLI 默认按顺序解析重名连接或查询，前面的优先；新增连接、保存查询默认写入第一个 workspace，因此保存时应明确目标目录。

连接里的三个概念分别是：

| 字段 | 含义 | 示例 |
|---|---|---|
| `group` | 项目分组 | `shop` |
| `db` | 逻辑数据库 | `orders` |
| `env` | 具体环境 | `local`、`dev`、`prod` |

同一个逻辑数据库可以对应多个环境的连接。一份保存的查询绑定逻辑数据库，执行时再选择环境。

保存的查询是带 metadata 注释头的普通 `.sql` 文件，记录用途、目标数据库和参数。查询文件可以放进 Git 共享；实际连接凭据留在本机。skill 可以通过软链接访问 workspace 的查询目录，链接不包含连接配置。

## 3. 一条查询如何执行

以 GUI 执行 `SELECT * FROM orders` 为例：

1. 浏览器把数据库、环境、SQL 和行数上限发送到本机 `/api/query`。
2. Python 后端解析连接，调用查询内核。
3. 内核检查语句、安全授权、参数和超时，并在适用时补充行数限制。
4. 如果需要跳板机，建立或复用 SSH 隧道。
5. 调用对应数据库的执行方式。
6. 整理结果，返回浏览器，显示在发起查询的标签页中。

不同数据库的执行方式如下：

| 数据库 | 执行方式 |
|---|---|
| PostgreSQL | 系统 `psql` |
| MySQL | 可选依赖 PyMySQL |
| Redis | 系统 `redis-cli`，输入为 Redis 命令 |
| Neptune | HTTP 请求，输入为 openCypher；目前属于实验性支持 |

结构化结果包含列、行、返回行数、是否截断、耗时、引擎、执行 SQL，以及传输大小信息。大小在部分引擎中是估算值。大整数和高精度小数会以字符串保真，避免前端数字精度损失。

## 4. 安全规则如何生效

Quarry 默认只读，会检查写入、DDL、多语句输入和带写操作的 CTE。PostgreSQL/MySQL 默认查询还会启用数据库侧只读约束。

各入口的写入规则是：

| 入口 | 写入条件 |
|---|---|
| GUI 查询 | 始终只读 |
| CLI | 需要 `--write`；生产连接额外要求确认或 `--yes` |
| MCP 的 `exec_sql` | 服务启动时启用 `--write`，调用时传 `write: true`；生产连接还需 `confirm_prod: true` |
| Python API | 调用方先取得授权，再传 `allow_write=True` |

生产保护依据是连接配置中的 **`production = true`**。`prod`、`jp` 等环境名称只是标签。

对于没有外层 `LIMIT`、且允许追加限制的读查询，默认最多交付 500 行。实际多取一行，用来判断是否还有结果。例如上限为 500 时执行 `LIMIT 501`，若拿到 501 行，则返回前 500 行并标记截断。GUI 可以调整上限；“Load more”在适用时通过 `OFFSET` 获取后续结果。

已有外层 `LIMIT` 的查询不会被自动替换限制；`EXPLAIN` 等工具语句也不会随意追加 `LIMIT`。Redis 则在取回结果后截断。返回行数限制不等于数据库执行成本限制。

连接和执行有独立超时。执行超时可通过参数、环境变量或连接配置调整。CLI 同时提供稳定错误码，让脚本区分连接错误、查询错误和安全拦截。

## 5. 隧道池与 keeper：减少重复建连

访问内网数据库时，SSH 隧道提供这条通路：

```text
数据库客户端 → 本机转发端口 → SSH 跳板机 → 内网数据库
```

建立 SSH 需要握手和认证。如果查看表、读取字段和执行 SQL 都重新建连，等待时间会反复叠加。

**隧道池**会在当前进程内复用已经建立的 SSH 通路。是否可以复用，取决于跳板机、用户、密钥、目标数据库主机与端口，以及代理路径。同一数据库实例中的多个数据库可以共享通路。

进程内的池随进程结束而释放。单次 `qy exec` 退出后，下次执行可能仍需重新建立隧道。

**keeper** 是独立的后台进程，用于让通路跨 CLI 调用长期保持：

- `qy up`：启动 keeper，提前建立 workspace 的 SSH 隧道。
- CLI、GUI、MCP：通过共享登记信息复用 keeper 的转发端口。
- 开启重连时：断线按逐步延长的间隔重试；永久性配置错误等待修正。
- `qy status`：查看状态；`qy down`：停止 keeper。

隧道池减少同一进程内的重复建连，keeper 进一步解决跨进程复用和后台重连。它们保持的是 SSH 通路，数据库会话仍由查询创建；不会自动重放失败的 SQL。普通查询不依赖 keeper，也能自行建立隧道。

## 6. Proxy：改善远程传输速度

[Issue #96](https://github.com/yiminspace/quarry/issues/96) 记录了这个功能的起因：跨境直连跳板机时，小查询可以成功，但大字段传输很慢，最终触发超时。

当时记录的直连吞吐约 15 KB/s，一行约 2.6 MB 的 JSONB 就会撞上当时的 60 秒超时；经本机 HTTP 代理后，吞吐约 700 KB/s，同一查询约 7 秒完成。这些是历史测量，不代表当前网速或当前超时默认值。

代理改变的是到跳板机的网络路径：

```text
直连：Quarry → SSH 跳板机 → 数据库
代理：Quarry → 本机 HTTP 代理 → SSH 跳板机 → 数据库
```

Quarry 使用已有的代理服务，通过 Python 标准库实现 HTTP CONNECT，并作为 SSH 的 `ProxyCommand`。这样无需逐台机器手工修改 SSH 配置，也无需依赖不同平台语法不一致的 `nc`。

使用方式与边界：

- `qy proxy` 查看代理发现结果和 workspace 开关；`qy proxy on/off` 切换。
- 优先从 macOS 系统设置发现地址，再回退到环境变量。
- 开关按 workspace 保存，查询可用 `--no-proxy` 单次覆盖。
- 尊重系统代理例外列表；代理端口未监听时退回直连。
- 已经尝试代理后的 SSH 失败不会盲目重试直连，以免掩盖认证或配置问题。
- 对 SSH 隧道和 Neptune HTTPS 请求生效；普通直连 PostgreSQL/MySQL 不会因此自动走代理。

这里采用显式开关，是因为握手快不代表大数据传输快。自动比较握手时间无法可靠选路，而每次额外传输大量数据测速也有成本。

隧道池的标识包含代理路径，避免切换代理后继续复用旧路径。已有 SSH 配置也可能影响实际路由，诊断时需要结合通路状态判断。

## 7. Local：准备本地开发数据库

local 环境让开发者拥有结构接近远端、数据由自己控制的数据库，便于运行后端、测试读写和反复调试，减少共享 dev 数据和远程网络的影响。

仅写 `env = "local"` 不会创建数据库。以 PostgreSQL 为例：

```bash
qy local up shop
qy local sync shop --from dev
```

第一条启动或复用本地 PostgreSQL Docker 容器，创建 `shop` 数据库，并登记 `shop@local` 连接。第二条从 dev 同步结构，不复制业务数据。

结构同步先导入临时数据库，成功后再切换为正式本地库，原本地库保留为上一份备份。当前只支持 PostgreSQL，目标必须是 local、回环地址且无 SSH 隧道。同步会替换本地结构，不是向已有库增量补表。

| 引擎 | local 实现 | 能做什么 |
|---|---|---|
| PostgreSQL | Docker 中的真实数据库 | 运行后端、真实 SQL 读写、同步结构 |
| Redis | Docker 中的真实 Redis | 提供缓存和状态依赖 |
| Neptune | Python HTTPS 空响应服务 | 支持不依赖真实图数据的启动和流程调试 |

Neptune 空服务不执行或持久化真实图数据，图查询正确性需要真实 Neptune 验证。

多个 PostgreSQL 逻辑数据库可以共享一个本地容器。PostgreSQL 和 Redis 使用命名数据卷，普通 `qy local down` 停止后数据仍保留；清除数据需要显式 purge。查询不会自动启动 Docker。

应用需要配置为使用这些本地地址，Quarry 不会自动修改应用连接。

## 8. GUI 如何维护工作状态

`qy gui` 启动本机 Python HTTP 服务：`/app/` 提供构建好的 React 页面，`/api/*` 处理请求，SSE 通知前端配置变化。运行已安装的 GUI 无需 Node.js；Node.js 用于前端开发和构建。

前端使用 Zustand 管理连接、界面和标签页状态：

- 标签页按数据库和环境组织，分别保存 SQL 草稿与结果快照。
- 切回已有环境时恢复该环境的标签和结果，不必重新查询。
- 空目标环境的初始化可以带入 SQL 并在非生产环境自动运行；生产连接保持手动执行。
- 小结果可保存到浏览器本地存储；大结果只保留在当前会话。
- 每个标签页用请求序号判断结果是否仍有效，旧响应不能覆盖新查询结果。
- 结果网格、状态栏和导出跟随当前标签页，避免混用其他查询的结果。
- 手写 SQL 通过草稿和历史机制保护，表预览等操作避免静默覆盖。

## 9. 代码入口

| 文件 | 职责 |
|---|---|
| `src/quarry/core.py` | 连接解析、安全检查、引擎执行、结果格式 |
| `src/quarry/workspace.py` | workspace 发现、配置、查询目录链接 |
| `src/quarry/cli.py` | `qy` 命令入口 |
| `src/quarry/gui.py` | 本地 HTTP API 和静态文件服务 |
| `src/quarry/mcp.py` | AI 工具接口和 MCP 授权 |
| `src/quarry/tunnel.py` / `keepalive.py` | SSH 隧道复用和后台保活 |
| `src/quarry/proxy.py` / `proxycommand.py` | 代理发现、路径选择和 HTTP CONNECT |
| `src/quarry/local.py` / `local_sync.py` | 本地服务创建和 PostgreSQL 结构同步 |
| `web/src/ResultWorkbench.tsx` / `store/` | 查询交互、标签页和结果状态 |

进一步阅读：[README](README.zh-CN.md)、[支持边界](COMPATIBILITY.md)、[测试说明](TESTING.md)。
