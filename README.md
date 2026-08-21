# Quarry

> **The database workbench built for the AI era** — one kernel, many faces (CLI / GUI / MCP / agent skill).

[![CI](https://github.com/Wangggym/quarry/actions/workflows/ci.yml/badge.svg)](https://github.com/Wangggym/quarry/actions/workflows/ci.yml)
[![Coverage ≥95%](https://img.shields.io/badge/coverage-%E2%89%A595%25-brightgreen)](TESTING.md)
[![Tests](https://img.shields.io/badge/tests-723-brightgreen)](TESTING.md)
[![PyPI](https://img.shields.io/pypi/v/quarry-db)](https://pypi.org/project/quarry-db/)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](https://pypi.org/project/quarry-db/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

[中文文档 →](README.zh-CN.md) · [Website →](https://quarry.yiminlab.site)

![Quarry demo](site/assets/demo.svg)

Every database tool you know — DBeaver, TablePlus, pgAdmin — assumes a *human* at the keyboard. But increasingly, the entity running your queries is an **AI agent**, and agents need different guarantees:

- **Results a machine can parse**, not a screen a human can read
- **Safety rails that live in the kernel**, so no client can forget them
- **Deterministic error contracts** (stable exit codes), not stack traces to scrape
- **Configuration as files**, not clicks — so it can be versioned, diffed, and shared with agents

Quarry inverts the traditional design: it is a **query kernel with an agent-safe contract first**, and the human faces (CLI, GUI) are thin shells grown from the same kernel. Whether a query comes from a person in the browser, a script in CI, or Claude running a skill, it passes through the exact same safety rails and returns the exact same structured result.

## Philosophy

1. **One core, many faces.** Connection management, query execution, schema introspection, and safety rails live in an importable kernel (`quarry.core`). The CLI (`qy`), the GUI, the MCP server, and agent skills are thin shells. Fix a bug once, every face gets it.

2. **Read-only by default; escalation is explicit and graduated.** Writes and DDL are blocked (exit code `8`) unless you pass `--write`. Production connections require an *additional* confirmation on top of `--write`. Every query gets an automatic `LIMIT 500` unless you opt out. Because the rails are in the kernel, an agent cannot bypass them by picking a different entry point.

3. **A contract machines can trust.** Every query returns `{columns, rows, rowCount, truncated, elapsedMs, engine, sql}`. Exit codes are stable API: `0` ok, `2` connection error, `3` SQL error, `8` safety block. An agent can branch on outcomes without parsing prose.

4. **Workspace as code.** A workspace is just a directory: `connections.toml` + `queries/**/*.sql` (named queries with `-- @meta` headers). It lives in *your* repo, versioned by git, shared between teammates and agents alike. The kernel itself carries zero business logic and zero secrets.

5. **Nearly zero dependencies.** Pure stdlib. PostgreSQL goes through your system `psql`, Redis through `redis-cli`, SSH tunnels through system `ssh`. MySQL is one optional `pymysql`. No Electron, no daemon, no cloud.

## Install

```bash
pipx install quarry-db        # or: pip install quarry-db
qy --help
```

PostgreSQL uses the system `psql` binary; MySQL needs `pip install "quarry-db[mysql]"`.

## Quickstart

```bash
mkdir my-workspace && cd my-workspace
cat > connections.toml <<'EOF'
[shop]
url    = "postgresql://user:pass@localhost:5432/shop"
engine = "postgres"
env    = "dev"
EOF

qy connections                       # list connections
qy exec shop --sql "select * from customers"
qy schema shop customers             # table structure (\d+)
qy gui                               # browser data grid
```

## Workspace

A workspace directory is the source of connections + queries:

```
my-workspace/
├── connections.toml      # [key] url / engine / env / group / notes
└── queries/<db>/*.sql    # named queries (with -- @meta headers)
```

Resolution order: `--workspace PATH` → `~/.config/quarry/config.toml` → current directory.

## CLI reference

| Command | Purpose |
|---------|---------|
| `qy connections [list\|add\|set\|remove\|test]` | Manage connections |
| `qy ping <db>\|--all [--timeout N] [--format text\|json]` | Reachability probe (ok/fail + latency; exit 1 if any fail) |
| `qy exec <db> --sql "..." [--format json\|ndjson\|csv\|table] [--timeout N]` | Run ad-hoc SQL |
| `qy speedtest <db> [--env dev] [--bytes N] [--runs N]` | Benchmark the current PostgreSQL/MySQL tunnel path |
| `qy schema <db> <table>` | Live table structure |
| `qy run <name> [k=v ...]` | Run a saved named query |
| `qy save <name> --db X --sql "..."` | Save a named query |
| `qy list / describe / validate / fingerprint / audit` | Manage named queries |
| `qy workspace list/add/remove` | Manage aggregated workspaces |
| `qy up/down/status [--format text\|json]` | Workspace tunnel keep-alive keeper |
| `qy local up/down/status/sync [--engine postgres\|redis\|all]` | Local dev containers (see below) |
| `qy gui` | Launch the local GUI |
| `qy mcp [--write]` | Serve the MCP face over stdio (for AI agents) |

## MCP (the agent-native face)

`qy mcp` speaks the Model Context Protocol over stdio — pure stdlib, no SDK dependency. Agents get six tools (`list_connections`, `list_tables`, `describe_table`, `exec_sql`, `list_saved_queries`, `run_saved_query`) with the exact same kernel rails: read-only unless the server was started with `--write` *and* the call passes `write: true`; a prod env additionally requires `confirm_prod: true`.

```bash
# Claude Code
claude mcp add quarry -- qy mcp --workspace ~/my-workspace
```

```json
// or any MCP client (.mcp.json)
{ "mcpServers": { "quarry": { "command": "qy", "args": ["mcp", "--workspace", "/path/to/workspace"] } } }
```

Published in the [MCP Registry](https://registry.modelcontextprotocol.io/) as `mcp-name: io.github.Wangggym/quarry`.

## Safety rails (the AI-native moat)

- **Read-only by default**: writes/DDL blocked with exit code `8`; `--write` to allow
- **Automatic row cap**: `run_query()` injects `LIMIT 500`; raise with `--max-rows N`
- **Graduated prod protection**: all envs default read-only → dev needs `--write` → prod needs `--write` *plus* an interactive confirmation (`--yes` for automation)
- **Stable exit-code contract**: `0` ok / `2` connection / `3` SQL / `8` safety block

## Timeouts

Query execution and connection establishment (including SSH tunnel setup) are capped independently, so an unreachable host fails fast instead of eating the whole query budget:

- **Connect timeout**: 15s, fixed — bounds tunnel/dial only.
- **Execute timeout**: 300s default for the CLI/GUI, 120s for MCP (agents should converge faster).

The effective execute timeout is resolved in priority order:

1. `--timeout N` (CLI, on `qy exec`/`qy run`)
2. `QUARRY_TIMEOUT` env var
3. the connection's `timeout` field in `connections.toml` (set via `qy connections add/set --timeout N`)
4. the default above

```toml
[shop_prod]
url     = "postgresql://…prod…/shop"
timeout = 600   # this connection alone gets 10 minutes
```

On PostgreSQL, `qy` also sets a server-side `statement_timeout` (~90% of the execute timeout) before running the query; on MySQL/MariaDB it sets the equivalent session variable (`MAX_EXECUTION_TIME` / `max_statement_time`, whichever the server supports) best-effort. Either way, the database itself cancels a runaway query and reports the real reason — instead of the client giving up and leaving the query running server-side. A timeout error always tells you how to raise it (`--timeout`, `QUARRY_TIMEOUT`, or the connection's `timeout` setting). `--timeout` and the `timeout` field must be a positive number of seconds.

## As a library (what the GUI and agents use)

```python
from quarry import configure_workspace, get_connection, run_query

configure_workspace("~/my-workspace")
res = run_query(get_connection("shop"), "select * from customers")
print(res.to_dict())   # {columns, rows, rowCount, truncated, elapsedMs, engine, sql}
```

## SSH tunnels

For databases only reachable via a bastion, add `ssh_*` fields and `qy` opens the tunnel automatically (system `ssh`, zero dependencies):

```toml
[internal_db]
url      = "postgresql://user:pass@127.0.0.1:5432/appdb"
engine   = "postgres"
ssh_host = "bastion.example.com"
ssh_user = "ubuntu"
ssh_key  = "~/.ssh/id_ed25519"
```

Neptune participates in the same tunnel path now: if a Neptune connection has
`ssh_host` (plus optional `ssh_user`/`ssh_key`/`ssh_port`), it joins tunnel
pooling/keep-alive the same way as Postgres/MySQL/Redis.

### Workspace keep-alive (`qy up/down/status`)

If you query the same SSH-backed connections repeatedly (CLI + GUI + MCP), run
the workspace keeper once and reuse warm forwards across processes:

```bash
qy up                    # start keeper for current workspace (also turns keep-alive on)
qy status                # text status: keeper + per-connection tunnel state
qy status --format json  # machine-readable state (up/reconnecting/down)
qy down                  # stop keeper
```

`keep_alive=true` + `reconnect=true` are persisted per workspace in
`~/.config/quarry/config.toml`. When reconnect is enabled, dropped tunnels are
re-opened with exponential backoff and reported as `reconnecting` in both `qy
status` and the GUI header badge. If keep-alive is enabled but the keeper is
down, cold `qy exec`/`qy run` still work (legacy behavior) and print a one-line
hint to stderr suggesting `qy up`.

### Proxy (for throttled tunnels)

If an SSH tunnel's throughput is throttled (a cross-border bastion, for example — the handshake connects fine but data crawls), route it through your machine's HTTP(S) proxy instead:

```bash
qy proxy              # show the discovered proxy + each workspace's toggle state
qy proxy on           # enable for the current workspace
qy proxy off          # disable it again
qy exec mydb --no-proxy --sql "..."   # skip the proxy for one call, even if enabled
```

The proxy is auto-discovered — macOS system proxy settings first (`scutil --proxy`), falling back to `ALL_PROXY`/`HTTPS_PROXY` — and the toggle is persisted per workspace in `config.toml` (never `connections.toml`). It only affects connections with `ssh_host` (tunneled via `ProxyCommand`) and Neptune's direct HTTPS requests; a direct (non-tunneled) DB connection is unaffected, and `qy connections add/set` warns if you enable the proxy for a connection with no `ssh_host`. If the proxy is enabled but nothing is listening on its port, `qy` falls back to a direct connection instead of erroring; targets covered by the system proxy's exceptions list (loopback, private CIDR ranges) are never proxied.

#### Confirming the proxy is actually in effect

Because the fallback-to-direct behavior above is silent by design (a query still has to run), it's worth knowing how to check whether a given call actually went through the proxy:

- **`qy` output**: if a workspace has the proxy enabled but a call still ran direct, `qy exec`/`qy run` print a one-line reason to stderr — no proxy discovered, discovered but nothing listening on its port, or the target is covered by the proxy's exceptions list. `--no-proxy` suppresses this (you asked for direct, so there's nothing to report).
- **`qy proxy`**: besides the discovered proxy and each workspace's toggle, it lists every pooled SSH tunnel — ssh target, local port, whether it's actually routed through the proxy (and which address), and whether the underlying `ssh` process is still alive. Add `--format json` for a `tunnels` array with the same fields, handy for scripting.
- **GUI**: an env pill in the sidebar shows a small badge when that connection's tunnel is routed through the proxy; the workspace manager shows each workspace's proxy toggle alongside the currently discovered proxy address. Both are computed server-side from the same logic `qy` uses, not guessed in the browser.

## Redis

`engine = "redis"` (uses system `redis-cli`). Queries are redis commands:

```bash
qy exec cache --sql "SCAN 0 COUNT 100"
qy exec cache --sql "HGETALL user:42"
```

Read-only rail applies here too: `GET/SCAN/TYPE/TTL/HGETALL` pass; `SET/DEL/FLUSHALL` are blocked without `--write`. In the GUI, redis keys are clickable with TYPE-aware value display.

## Groups & env-sets

Connections can be organized into **project folders** (`group`) and **env-sets** (same `db`, different `env`, shared schema):

```toml
[shop_dev]
url = "postgresql://…dev…/shop";  group = "shop"; db = "shop"; env = "dev"
[shop_prod]
url = "postgresql://…prod…/shop"; group = "shop"; db = "shop"; env = "prod"
```

- Connections with the same `db` fold into one env-set — one saved query runs against any environment: `qy exec shop --env prod`
- `qy connections add/set` accepts `--db` and `--group`; when a new key such as `shop_prod --env prod` matches an existing env-set, Quarry inherits that identity automatically
- If one env member omits `group` but every grouped sibling agrees, CLI/GUI/MCP keep the logical DB together in that group instead of creating a duplicate under `OTHER`
- Unspecified env defaults to `dev` (the safest)
- The GUI shows an environment switcher (prod turns red)

## Multiple workspaces

`qy` aggregates all workspaces listed in `~/.config/quarry/config.toml` — one GUI/CLI over all your projects:

```bash
qy workspace add ~/projects/acme/db-workspace
qy workspace add ~/projects/side-project/db
qy connections    # both projects, grouped
qy gui            # sidebar shows both groups side by side
```

`--workspace a:b` (os.pathsep-separated) works as a temporary override; the first directory is primary for writes.

## Local dev containers

When a locally-running service shares a remote (dev) database, every read/write
crosses the public network — and a test/e2e run that hammers the DB gets flaky
on the round trips. `qy local` runs Postgres/Redis in a docker container so the
service talks only to `localhost`:

```bash
qy local up shop            # start local Postgres + register a shop `local` connection
qy connections              # shop now shows a [local] env alongside [dev]
qy run active_customers --env local

qy local status             # running? which port / image?
qy local sync shop          # copy dev schema into local (staging db + rename swap)
qy local down               # stop, keep the data volume (data survives)
qy local down --purge       # stop + delete the volume (next up is an empty DB)
```

One shared Postgres container hosts a logical database per connection key (fixed
port `5433`; redis `6380`), and data lives on a named docker volume. Requires a
docker daemon; the image tag is overridable with `--image`.

## GUI

![Quarry GUI](site/assets/gui-dark.png)

`qy gui` — a local, zero-build web GUI (Slate & Copper theme, light/dark):

- Grouped sidebar tree with env switcher (prod turns red), connection health dots
- **Multi-tab editor** — each tab remembers its SQL + connection, across restarts
- SQL highlighting + local autocomplete (keywords / tables / columns)
- **EXPLAIN button** — one click to the query plan
- Type-aware data grid: sorting, column resize, **keyboard navigation** (arrows + Enter), cell inspection with a **collapsible JSON tree**
- CSV/JSON export, **searchable query history** (with connection + time)
- TYPE-aware Redis key browsing
- **Update check** — a background thread polls PyPI once every 24h and shows
  a header badge (with the upgrade command + release notes) when a newer
  `quarry-db` is out. Editable/dev installs are skipped automatically; set
  `QUARRY_UPDATE_CHECK=0` to disable it entirely.

## Roadmap

- Column types in the result contract for all engines
- SQLite & DuckDB engines (zero-setup local demo)
- Redis key-namespace folding tree
- Cross-environment schema/data diff
- Write audit log (who ran what, where, when)
- Single-binary distribution

## Development & testing

```bash
pip install -e ".[dev]"
createdb quarry_test && psql quarry_test -f tests/seed.sql   # or: make seed
make test        # layered run with a per-layer PASS/FAIL summary
```

**723 tests in four layers**, each auto-classified so you can run any slice:

| Layer | Count | Covers | Needs |
|-------|------:|--------|-------|
| `unit` | 568 | pure logic + mocked engines (safety rails, SQL skeleton, params, formatters, cache) | nothing |
| `integration` | 110 | in-process against a real DB, incl. the GUI HTTP API and CLI/MCP dispatch | Postgres |
| `e2e` | 45 | the real `qy` CLI and `qy mcp` stdio server as subprocesses | Postgres |
| `browser` | 20 | the **real GUI frontend** driven in headless Chromium (Playwright) | Postgres + Playwright |

DB/engine-backed tests skip automatically when the engine is unreachable, so the
suite stays green on a bare machine; CI provides the engines and runs everything.

**Coverage is gated at ≥95%** (unit + integration) and currently sits at **99.6%**.

### Seeing test status at a glance

- **On GitHub:** the CI badge above is live — it goes red if any layer *or* the
  coverage gate fails. Per-commit and per-PR results show under the **Actions** tab
  and as PR checks.
- **Locally, pass/fail:** `make test` prints a colored per-layer summary; run one
  layer with `make test-unit` / `test-integration` / `test-e2e` / `test-browser`.
- **Locally, coverage:** `make cov` enforces the gate and writes an HTML report —
  open `htmlcov/index.html` for a line-by-line view of exactly what's covered.

See [TESTING.md](TESTING.md) for the full architecture, fixtures, and CI layout,
and [CONTRIBUTING.md](CONTRIBUTING.md) for contribution guidelines.

Quarry is developed and tested on macOS and Linux. Windows is currently untested (the psql/ssh integration and port takeover are Unix-flavored) — PRs welcome.

## License

[MIT](LICENSE)
