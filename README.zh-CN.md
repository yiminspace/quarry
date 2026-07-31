# Quarry

> **为 AI 时代而生的数据库工作台** —— 一核多脸(CLI / GUI / MCP / agent skill)。

[![CI](https://github.com/Wangggym/quarry/actions/workflows/ci.yml/badge.svg)](https://github.com/Wangggym/quarry/actions/workflows/ci.yml)
[![覆盖率 ≥95%](https://img.shields.io/badge/coverage-%E2%89%A595%25-brightgreen)](TESTING.md)
[![Tests](https://img.shields.io/badge/tests-723-brightgreen)](TESTING.md)
[![PyPI](https://img.shields.io/pypi/v/quarry-db)](https://pypi.org/project/quarry-db/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

[English →](README.md) · [官网 →](https://quarry.yiminlab.site)

![Quarry demo](site/assets/demo.svg)

你熟悉的所有数据库工具 —— DBeaver、TablePlus、pgAdmin —— 都默认键盘前坐着一个*人*。但如今越来越多的查询是 **AI agent** 在跑,而 agent 需要的保障完全不同:

- **机器能解析的结构化结果**,而不是给人看的界面
- **安全栏内置在内核里**,任何客户端都绕不过、忘不掉
- **确定性的错误契约**(稳定退出码),而不是靠爬 stack trace
- **配置即文件**,而不是点点点 —— 能进 git、能 diff、能共享给 agent

Quarry 把传统设计倒了过来:先做一个**带 agent 安全契约的查询内核**,人类用的 CLI、GUI 只是从同一个内核长出来的薄壳。无论查询来自浏览器里的人、CI 里的脚本,还是跑 skill 的 Claude,都走同一套安全栏、返回同一种结构化结果。

## 理念

1. **一核多脸。** 连接管理、查询执行、schema 内省、安全栏都在可 import 的内核(`quarry.core`)里。CLI(`qy`)、GUI、MCP server、agent skill 都是薄壳。修一次 bug,所有脸同时受益。

2. **默认只读;放行是显式且分级的。** 写/DDL 默认拦截(退出码 `8`),`--write` 显式放行;prod 连接在 `--write` 之上还需额外确认;每条查询自动注入 `LIMIT 500`,除非 opt-out。因为安全栏在内核,agent 换任何入口都绕不过去。

3. **机器可信赖的契约。** 每次查询返回 `{columns, rows, rowCount, truncated, elapsedMs, engine, sql}`。退出码是稳定 API:`0` ok / `2` 连接错 / `3` SQL 错 / `8` 安全拦截。agent 可以直接对结果分支,不用解析文字。

4. **Workspace 即代码。** 一个 workspace 就是一个目录:`connections.toml` + `queries/**/*.sql`(带 `-- @meta` 头的命名查询)。它放在*你的*仓库里,git 管理,团队成员和 agent 共用。内核本身零业务、零密钥。

5. **近乎零依赖。** 纯 stdlib。PostgreSQL 走系统 `psql`,Redis 走 `redis-cli`,SSH 隧道走系统 `ssh`;MySQL 只需可选的 `pymysql`。没有 Electron,没有守护进程,没有云。

## 安装

```bash
pipx install quarry-db        # 或 pip install quarry-db
qy --help
```

PostgreSQL 走系统 `psql`;MySQL 需要 `pip install "quarry-db[mysql]"`。

## 快速开始

```bash
mkdir my-workspace && cd my-workspace
cat > connections.toml <<'EOF'
[shop]
url    = "postgresql://user:pass@localhost:5432/shop"
engine = "postgres"
env    = "dev"
EOF

qy connections                       # 列出连接
qy exec shop --sql "select * from customers"
qy schema shop customers             # 表结构(\d+)
qy gui                               # 浏览器数据网格
```

## Workspace

一个 workspace 目录就是"连接 + 查询"的来源:

```
my-workspace/
├── connections.toml      # [key] url / engine / env / group / notes
└── queries/<db>/*.sql    # 命名查询(带 -- @meta 头)
```

解析优先级:`--workspace PATH` → `~/.config/quarry/config.toml` → 当前目录。

## CLI 速查

| 命令 | 作用 |
|------|------|
| `qy connections [list\|add\|set\|remove\|test]` | 管理连接 |
| `qy ping <db>\|--all [--timeout N] [--format text\|json]` | 轻量健康探测(可达/耗时;有失败则 exit 1) |
| `qy exec <db> --sql "..." [--format json\|ndjson\|csv\|table]` | 跑临时 SQL |
| `qy speedtest <db> [--env dev] [--bytes N] [--runs N]` | 测当前 PostgreSQL/MySQL 隧道链路吞吐 |
| `qy schema <db> <table>` | 看表结构 |
| `qy run <name> [k=v ...]` | 跑命名查询 |
| `qy save <name> --db X --sql "..."` | 存命名查询 |
| `qy list / describe / validate / fingerprint / audit` | 管理命名查询 |
| `qy workspace list/add/remove` | 管理聚合 workspace |
| `qy gui` | 启动本地 GUI |
| `qy mcp [--write]` | 起 MCP server(stdio,给 AI agent 用)|

## MCP(agent 原生脸)

`qy mcp` 用纯 stdlib 实现 MCP 协议(stdio),零 SDK 依赖。agent 拿到 6 个工具(`list_connections` / `list_tables` / `describe_table` / `exec_sql` / `list_saved_queries` / `run_saved_query`),安全栏与内核完全一致:server 不带 `--write` 一律只读;带了之后每次调用还要 `write: true`;prod 环境额外要求 `confirm_prod: true`。

```bash
# Claude Code
claude mcp add quarry -- qy mcp --workspace ~/my-workspace
```

## 安全栏(AI 原生护城河)

- **默认只读**:写/DDL 被拦(退出码 `8`),`--write` 显式放行
- **自动行数上限**:`run_query()` 默认注入 `LIMIT 500`,`--max-rows N` 提高
- **分级 prod 保护**:全环境默认只读 → dev 加 `--write` → prod 在 `--write` 之上还需交互确认(自动化用 `--yes`)
- **稳定退出码契约**:`0` ok / `2` 连接错 / `3` SQL 错 / `8` 安全拦截

## 作为库(GUI 和 agent 的用法)

```python
from quarry import configure_workspace, get_connection, run_query

configure_workspace("~/my-workspace")
res = run_query(get_connection("shop"), "select * from customers")
print(res.to_dict())   # {columns, rows, rowCount, truncated, elapsedMs, engine, sql}
```

## SSH 隧道

连只能过 bastion 访问的库:给连接加 `ssh_*` 字段,`qy` 自动开隧道(走系统 `ssh`,零依赖):

```toml
[internal_db]
url      = "postgresql://user:pass@127.0.0.1:5432/appdb"
engine   = "postgres"
ssh_host = "bastion.example.com"
ssh_user = "ubuntu"
ssh_key  = "~/.ssh/id_ed25519"
```

### 代理(隧道被限速时用)

如果 SSH 隧道被限速(比如跨境走 bastion,握手正常但传输很慢),可以让同一条隧道改走系统的 HTTP(S) 代理:

```bash
qy proxy              # 查看探测到的代理 + 各 workspace 的开关状态
qy proxy on           # 为当前 workspace 开启
qy proxy off          # 关闭
qy exec mydb --no-proxy --sql "..."   # 即使已开启,单次调用也跳过代理
```

代理零配置自动探测:优先读 macOS 系统代理设置(`scutil --proxy`),没有则退回 `ALL_PROXY`/`HTTPS_PROXY` 环境变量;开关状态按 workspace 粒度持久化在 `config.toml`(绝不碰 `connections.toml`)。它只对带 `ssh_host` 的连接生效(通过 `ProxyCommand` 走隧道)以及 Neptune 的直连 HTTPS 请求;不走隧道的直连数据库不受影响,`qy connections add/set` 会在代理已开启但连接没有 `ssh_host` 时给出提示。代理端口如果没有监听,`qy` 会自动退回直连而不是报错;系统代理的例外列表(loopback、私有网段)始终生效,命中的目标不会被代理。

## Redis

`engine = "redis"`(走系统 `redis-cli`)。查询即 redis 命令:

```bash
qy exec cache --sql "SCAN 0 COUNT 100"
qy exec cache --sql "HGETALL user:42"
```

只读栏同样生效:`GET/SCAN/TYPE/TTL/HGETALL` 放行,`SET/DEL/FLUSHALL` 拦(`--write` 放行)。GUI 里 key 可点,自动 TYPE-aware 读值。

## 分组与环境集(env-set)

连接可归入**项目文件夹**(`group`)和**环境集**(同 `db`、不同 `env`,共享 schema):

```toml
[shop_dev]
url = "postgresql://…dev…/shop";  group = "shop"; db = "shop"; env = "dev"
[shop_prod]
url = "postgresql://…prod…/shop"; group = "shop"; db = "shop"; env = "prod"
```

- 同 `db` 的连接折叠成 env-set,一份查询跑多环境:`qy exec shop --env prod`
- 未指定 env 默认 `dev`(最安全)
- GUI 提供环境切换器(prod 变红)

## 多 workspace 聚合

`qy` 读 `~/.config/quarry/config.toml` 里的 workspace 列表,一个 GUI/CLI 同屏所有项目:

```bash
qy workspace add ~/projects/acme/db-workspace
qy workspace add ~/projects/side-project/db
qy connections    # 两个项目的连接,按组同屏
qy gui            # 侧栏两组并列
```

也可 `--workspace a:b`(os.pathsep 分隔)临时覆盖,第一个目录为 primary(写操作以它为准)。

## GUI

![Quarry GUI](site/assets/gui-dark.png)

`qy gui` —— 本地零构建 web GUI(Slate & Copper 主题,亮/暗切换):

- 分组侧栏树 + 环境切换器(prod 变红)+ 连接健康状态点
- **多标签编辑器** —— 每个标签记住自己的 SQL + 连接,重启不丢
- SQL 高亮 + 本地补全(关键字/表/列)
- **EXPLAIN 按钮** —— 一键看执行计划
- 类型着色数据网格:排序、列宽拖拽、**键盘导航**(方向键 + Enter)、单元格详情带**可折叠 JSON 树**
- CSV/JSON 导出、**可搜索的查询历史**(带连接名 + 时间)
- Redis key TYPE-aware 浏览

## 路线图

- 全引擎结果契约列类型
- SQLite / DuckDB 引擎(零配置本地体验)
- Redis key 命名空间折叠树
- 跨环境 schema/数据 diff
- 写操作审计日志
- 单二进制分发

## 开发与测试

```bash
pip install -e ".[dev]"
createdb quarry_test && psql quarry_test -f tests/seed.sql   # 或:make seed
make test        # 分层运行,末尾给每层 PASS/FAIL 汇总
```

**723 个测试,分四层**,每个测试按所用 fixture 自动归类,可单独跑任意一层:

| 层 | 数量 | 覆盖 | 依赖 |
|----|-----:|------|------|
| `unit` | 568 | 纯逻辑 + mock 引擎(安全栏、SQL 骨架、参数、格式化、缓存) | 无 |
| `integration` | 110 | 进程内连真库,含 GUI HTTP API 和 CLI/MCP 分发 | Postgres |
| `e2e` | 45 | 真子进程:`qy` CLI 与 `qy mcp` stdio server | Postgres |
| `browser` | 20 | **真实 GUI 前端**,无头 Chromium 驱动(Playwright) | Postgres + Playwright |

连库/引擎的测试在引擎不可达时自动 skip,所以裸机上整套也是绿的;CI 提供引擎、跑全套。

**覆盖率门禁 ≥95%**(单元 + 集成),当前 **99.6%**。

### 如何直观看测试情况

- **GitHub 上:** 顶部 CI 徽章是实时的 —— 任一层或覆盖率门禁挂了它就变红。每次提交、每个 PR 的结果在 **Actions** 页和 PR 检查里可见。
- **本地看通过/失败:** `make test` 打印彩色的分层汇总;单跑一层用 `make test-unit` / `test-integration` / `test-e2e` / `test-browser`。
- **本地看覆盖率:** `make cov` 强制门禁并生成 HTML 报告 —— 打开 `htmlcov/index.html` 就能逐行看到哪些代码被覆盖。

完整架构、fixture、CI 布局见 [TESTING.md](TESTING.md),贡献指南见 [CONTRIBUTING.md](CONTRIBUTING.md)。

Quarry 在 macOS 和 Linux 上开发和测试;Windows 尚未验证(psql/ssh 集成和端口接管是 Unix 风格)—— 欢迎 PR。

## License

[MIT](LICENSE)
