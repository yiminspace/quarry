# Quarry

> **The database workbench built for the AI era** — one kernel, many faces (CLI / GUI / MCP / agent skill).

[![CI](https://github.com/yiminspace/quarry/actions/workflows/ci.yml/badge.svg)](https://github.com/yiminspace/quarry/actions/workflows/ci.yml)
[![Coverage ≥95%](https://img.shields.io/badge/coverage-%E2%89%A595%25-brightgreen)](TESTING.md)
[![PyPI](https://img.shields.io/pypi/v/quarry-db)](https://pypi.org/project/quarry-db/)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](https://pypi.org/project/quarry-db/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

[中文文档 →](README.zh-CN.md) · [Website →](https://yiminspace.github.io/quarry/)

![Quarry demo](site/assets/demo.svg)

Every database tool you know — DBeaver, TablePlus, pgAdmin — assumes a *human* at the keyboard. But increasingly, the entity running your queries is an **AI agent**, and agents need different guarantees:

- **Results a machine can parse**, not a screen a human can read
- **Shared query safety policies**, with explicit authorization by entry point
- **Deterministic error contracts** (stable exit codes), not stack traces to scrape
- **Configuration as files**, not clicks — so it can be versioned, diffed, and shared with agents

Quarry inverts the traditional design: it is a **query kernel with an agent-safe contract first**, and the human faces (CLI, GUI) are thin shells grown from the same kernel. Whether a query comes from a person in the browser, a script in CI, or Claude running a skill, it uses shared query policies. CLI formats render rows; the GUI, MCP and Python API expose a structured QueryResult. See the [interface and support contract](COMPATIBILITY.md) for the exact boundaries.

## Philosophy

1. **One core, many faces.** Connection management, query execution, schema introspection, and safety rails live in an importable kernel (`quarry.core`). The CLI (`qy`), the GUI, the MCP server, and agent skills are thin shells. Fix a bug once, every face gets it.

2. **Read-only by default; escalation is explicit and graduated.** CLI writes require `--write`; prod additionally needs confirmation or `--yes`. MCP requires server and per-call authorization, plus `confirm_prod` for prod. GUI queries are read-only; Python callers obtain authorization before passing `allow_write=True`. Read queries without an outer LIMIT default to a 500-row cap; `--max-rows 0` explicitly disables it. PostgreSQL/MySQL query execution also uses database read-only transactions unless writes are authorized; SQLite opens existing files in read-only mode.

3. **A contract machines can trust.** GUI/MCP/Python queries return `{columns, rows, rowCount, truncated, elapsedMs, engine, sql, downloadBytes, sizeIsEstimated}`. CLI JSON remains an array of rows; diagnostics and truncation notices go to stderr. Exit codes are stable API: `0` ok, `2` connection error, `3` SQL error, `8` safety block. CLI argument syntax errors also use `2`; other commands have their own codes (for example `ping` returns `1` on failure). GUI/MCP/Python report structured errors.

4. **Workspace as code.** A workspace is just a directory: `connections.toml` + `queries/**/*.sql` (named queries with `-- @meta` headers). Share query files and credential-free templates through *your* repo; keep actual connection credentials local.

5. **Nearly zero dependencies.** The base package and GUI use Python 3.11+ stdlib, including `sqlite3` for SQLite. PostgreSQL uses system `psql`, Redis needs `redis-cli` 6+, SSH uses system `ssh`, and MySQL uses the optional `quarry-db[mysql]` dependencies. No Electron or cloud service; the optional `qy up` keeper runs in the background.

## Install

```bash
pipx install quarry-db        # or: pip install quarry-db
qy --help
```

SQLite uses Python's built-in `sqlite3`, with no server, Docker container or `sqlite3` CLI required. PostgreSQL uses the system `psql` binary; MySQL needs `pip install "quarry-db[mysql]"`.

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

### SQLite files

Point a connection at an **existing** SQLite file on the machine running `qy`:

```toml
[notes_local]
engine = "sqlite"
url = "sqlite:///data/notes-local.db"
db = "notes"
env = "local"

[notes_prod]
engine = "sqlite"
url = "sqlite:////srv/notes/notes.db"
db = "notes"
env = "prod"
production = true
```

`sqlite:///data/notes-local.db` resolves relative to the workspace containing
that connection; `sqlite:////srv/notes/notes.db` is an absolute path. Plain file
paths are also accepted when `engine = "sqlite"` is explicit. A missing file is
an error, including for authorized writes: Quarry never creates an empty
database because a connection path was mistyped.

```bash
qy connections test notes --env local
qy exec notes --env local --sql "SELECT * FROM notes ORDER BY id"
qy schema notes notes --env local
qy gui
```

The CLI, GUI, MCP and Python API share table/view browsing, schema inspection,
parameters, query limits and exports. The GUI supports paging and SQLite's
`EXPLAIN QUERY PLAN`. Queries default to read-only; writes use the same explicit
authorization as other SQL engines, and the GUI stays read-only.

`local`, `dev` and `prod` are labels; separate files provide data isolation.
Two environments pointing at one file access the same data. Set
`production = true` explicitly for production protection and Manual mode by
default. SQLite does not use `qy local up`, Docker or SSH tunnels; remote files,
`:memory:` databases, SQLite URI options and attached databases are outside
this first implementation. If your application uses PostgreSQL in production,
keep PostgreSQL locally too for matching SQL and data types.

## Workspace

A workspace directory is the source of connections + queries:

```
my-workspace/
├── connections.toml      # [key] url / engine / env / group / notes
└── queries/<db>/*.sql    # named queries (with -- @meta headers)
```

Resolution order: `--workspace PATH` → `~/.config/quarry/config.toml` → current directory.

See [COMPATIBILITY.md](COMPATIBILITY.md) for lossless number/string representations, write support by entry point, stable interfaces and tested environment boundaries.

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
| `qy local up/down/status/sync [--engine postgres\|redis\|neptune\|all]` | Local dev services (see below) |
| `qy gui` | Launch the local GUI |
| `qy mcp [--write]` | Serve the MCP face over stdio (for AI agents) |

## MCP (the agent-native face)

`qy mcp` speaks the Model Context Protocol over stdio — pure stdlib, no SDK dependency. Agents get six tools (`list_connections`, `list_tables`, `describe_table`, `exec_sql`, `list_saved_queries`, `run_saved_query`) with the exact same kernel rails: read-only unless the server was started with `--write` *and* the call passes `write: true`; a prod env additionally requires `confirm_prod: true`.

```bash
# Claude Code
claude mcp add quarry -- qy mcp --workspace ~/my-workspace
```

```json
{ "mcpServers": { "quarry": { "command": "qy", "args": ["mcp", "--workspace", "/path/to/workspace"] } } }
```

Published in the [MCP Registry](https://registry.modelcontextprotocol.io/v0.1/servers/io.github.yiminspace%2Fquarry/versions/latest)
as `io.github.yiminspace/quarry`.
The former personal-namespace listing still needs administrator-assisted migration
or deprecation; use the organization entry above. Direct `qy mcp` configuration is unchanged.

<!-- mcp-name: io.github.yiminspace/quarry -->

## Safety rails (the AI-native moat)

- **CLI read-only default**: writes/DDL blocked with exit code `8`; `--write` to allow
- **Automatic row cap**: read queries without an outer LIMIT default to 500 rows; raise with `--max-rows N`, disable with `--max-rows 0` (utility/locking queries are not rewritten; Redis caps after receipt)
- **CLI prod protection**: all envs default read-only → dev needs `--write` → prod needs `--write` *plus* an interactive confirmation (`--yes` for automation)
- **Query exit codes**: `0` success (rows optional), `1` usage, `2` connection or CLI argument syntax, `3` execution, `8` safety block; other commands have their own documented codes

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

On PostgreSQL, `qy` also sets a server-side `statement_timeout` (~90% of the execute timeout) before running the query; on MySQL/MariaDB it sets the equivalent session variable (`MAX_EXECUTION_TIME` / `max_statement_time`, whichever the server supports) best-effort. PostgreSQL enforces its statement timeout server-side. MySQL support is best-effort and its MAX_EXECUTION_TIME applies to SELECTs; a client timeout is not proof that a write was cancelled. A timeout error always tells you how to raise it (`--timeout`, `QUARRY_TIMEOUT`, or the connection's `timeout` setting). `--timeout` and the `timeout` field must be a positive number of seconds.

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
qy up                    # enable keep-alive; default reconnect on unless explicitly disabled
qy status                # text status: keeper + per-connection tunnel state
qy status --format json  # per-tunnel state (up/reconnecting/down/blocked)
qy down                  # stop keeper
```

`keep_alive=true` + `reconnect=true` are persisted per workspace in
`~/.config/quarry/config.toml`. When reconnect is enabled, dropped tunnels are
re-opened with exponential backoff and reported as `reconnecting` in both `qy
status` and the GUI header badge. If keep-alive is enabled but the keeper is
down, cold `qy exec`/`qy run` still work (legacy behavior) and print a one-line
hint to stderr suggesting `qy up`.

With reconnection explicitly disabled, the keeper makes an initial attempt but
does not retry failed or dropped tunnels. Missing keys, authentication failures,
and invalid SSH configuration report `blocked`; fixing the relevant connection
or SSH files allows another attempt. `qy status` describes tunnel transport,
not database authentication or query health; the GUI's “keeper up” badge means
the background process is running. Use a read-only query to test the full path.

The registry verifies process identities before sharing a forward. Older keeper
records containing only a PID are not trusted; stop the old version's keeper
before upgrading. On proxy changes, active queries retain their existing
forward. Retired keeper-owned forwards stay alive until keeper shutdown because
external clients may still be using them.

For PostgreSQL server identity verification, add `sslmode=verify-full` and a
trusted `sslrootcert` to the database URL. Quarry preserves the database hostname
for certificate verification and supplies `hostaddr=127.0.0.1` plus the forwarded
port to libpq. An SSH tunnel alone does not verify the database server.

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
url = "postgresql://user:password@dev.example.com:5432/shop"
group = "shop"
db = "shop"
env = "dev"
[shop_prod]
url = "postgresql://user:password@prod.example.com:5432/shop"
group = "shop"
db = "shop"
env = "prod"
production = true
```

- Connections with the same `db` fold into one env-set — one saved query runs against any environment: `qy run recent_orders --env prod`
- `qy connections add/set` accepts `--db` and `--group`; when a new key such as `shop_prod --env prod` matches an existing env-set, Quarry inherits that identity automatically
- If one env member omits `group` but every grouped sibling agrees, CLI/GUI/MCP keep the logical DB together in that group instead of creating a duplicate under `OTHER`
- Unspecified env defaults to `dev` (the safest)
- The GUI shows an environment switcher; production styling follows `production = true`

## Multiple workspaces

`qy` aggregates all workspaces listed in `~/.config/quarry/config.toml` — one GUI/CLI over all your projects:

```bash
qy workspace add ~/projects/acme/db-workspace
qy workspace add ~/projects/side-project/db
qy connections    # both projects, grouped
qy gui            # sidebar shows both groups side by side
```

`--workspace a:b` (os.pathsep-separated) works as a temporary override; the first directory is primary for writes.

### Agent skill and saved queries

The portable skill is in [`skills/quarry`](skills/quarry/SKILL.md). Install that
directory in your agent's skill location and use it with the `qy` CLI built
from this version. No personal connections or queries ship with the skill.
English is the default [`SKILL.md`](skills/quarry/SKILL.md); the equivalent
[Simplified Chinese edition](skills/quarry/SKILL.zh-CN.md) is `SKILL.zh-CN.md`.
To use Chinese as the installed entry point, copy that edition to `SKILL.md`
in the installation directory. Install one edition, not two duplicate skills;
maintain both editions together when changing the skill's behavior.

```bash
qy --skill-dir /path/to/installed/quarry list
qy --skill-dir /path/to/installed/quarry --workspace ~/db/acme save customer-count \
  --db customers --sql 'SELECT count(*) FROM customers'
```

`save` owns the file format and writes `<workspace>/queries/<db>/<name>.sql`.
`--skill-dir` creates `<skill>/queries/<workspace-name>` as a symlink to that
query directory before running the command. Credentials remain outside the
link. Pass the option on each call to recreate missing links after updates;
the CLI does not remember or guess skill installation paths. Missing workspace
directories are reported rather than recreated. Existing files
and links to other locations are never replaced. Duplicate workspace directory
names require selecting one with `--workspace`. Linking errors stop the command
with a usage error; query data is not removed. Keep generated `queries/` entries
out of the published skill package.

For legacy workspaces whose `queries/` points into a skill repository, first
back up and move the query files into a real workspace `queries/` directory,
then remove the old skill query entry before using `--skill-dir`. The CLI
does not automatically move existing user data.

## Local dev containers

If the default port is occupied, choose a free port explicitly:

```bash
qy local status --engine postgres
qy local up --engine postgres --port 55434
```

The port is saved in `[local] postgres_port` (or `redis_port`) in Quarry's
config.toml and used by CLI and GUI local setup. Only `env=local` connections
with Quarry's named-volume metadata still pointing to the old loopback port
are updated across all configured workspaces; other connections stay unchanged.
Changed connection files are backed up beside the originals.

A port change requires stopping a running Quarry container first with
`qy local down --engine postgres`. A stopped container is recreated with its
existing image and named volume; custom data mounts require manual handling.
Docker must be running. Query commands only diagnose failures, never launch
Docker or stop conflicting services automatically.


When a locally-running service shares a remote (dev) database, every read/write
crosses the public network — and a test/e2e run that hammers the DB gets flaky
on the round trips. `qy local` runs Postgres/Redis in a docker container so the
service talks only to `localhost`:

```bash
qy local up shop            # start local Postgres + register a shop `local` connection
qy local up                 # start Postgres, Redis, and the empty Neptune endpoint
qy local up neptune --engine neptune  # register neptune@local independently
qy connections              # shop now shows a [local] env alongside [dev]
qy run active_customers --env local

qy local status             # running? which port / image?
qy local sync shop          # copy dev schema into local (staging db + rename swap)
qy local down               # stop, keep the data volume (data survives)
qy local down --purge       # stop + delete the volume (next up is an empty DB)
```

`qy local up <db>` stores a new local connection in the source database's
workspace. Before creating it, Quarry checks every configured workspace and
reuses the one existing local entry for the same project scope (`group`, or the
source workspace when ungrouped); multiple matches in that scope are reported
as a config error. Different projects may use the same logical DB name safely:
their physical Postgres databases are namespaced, for example
`brain_matrix_runtime` and `yiminlab_matrix_runtime`. Managed local entries are
reconciled to the configured port, namespace, and localhost credentials when
the shared container is recreated.

The local Neptune service listens on `https://localhost:18182` and accepts both
Quarry and AWS Neptune Data API openCypher requests. Its default `empty` mode
returns no rows and acknowledges writes without persisting them.

For deterministic tests, start **only** Neptune with `qy local up neptune
--engine neptune --fixture /path/to/responses.json`. A fixture contains an
ordered `responses` array; the first rule whose `query_contains` substring and
optional parameter subset match returns its `results` verbatim:

```json
{"responses": [{"query_contains": "MATCH (n)", "parameters": {"user_id": "test-user"}, "results": [{"n": {"name": "example"}}]}]}
```

Set `query_exact: true` to require the whole query to equal `query_contains`,
and `parameters_exact: true` to require the complete parameter dictionary.
Both flags default to false for existing substring/subset fixtures.
Exact parameters compare JSON types recursively, keeping booleans distinct from numbers.

Mock mode returns HTTP 501 for unmatched queries, including unconfigured writes,
so tests cannot silently pass on an empty response. `qy local neptune-calls
--format json` lists every openCypher call, its parameters, match status, and a
monotonic `next` cursor; `--after <cursor>` isolates a subsequent test window.
The fixture and calls live in process memory and reset when the endpoint stops.
This is a protocol-level response mock, **not** a Cypher graph database. Mock
mode binds to loopback only; changing between `empty` and `mock` (or changing
fixtures) requires explicitly stopping the existing endpoint first. `qy local
status --engine neptune` shows the active backend.

One shared Postgres container hosts a namespaced physical database per scoped
logical connection (fixed port `5433`; redis `6380`), and data lives on a named
docker volume. Requires a docker daemon; the image tag is overridable with
`--image`.

## GUI

![Quarry GUI](site/assets/gui-dark.png)

GUI links use `/quarry/?db=auth_db&env=prod&table=oauth_accounts`.
The address follows the active tab using the configured logical database and
environment; workspace folder names stay out of the URL. Opening a table-focus
link restores selection without running SQL. `/app/` is no longer served.

`qy gui` — a local, zero-build web GUI (Slate & Copper theme, light/dark):

- Chinese by default; the language switch selects English and remembers your choice.
- Grouped sidebar tree with env switcher, explicit production styling, connection health dots
- **Multi-tab editor** — SQL and connection drafts persist when browser storage is available; result snapshots are bounded and large results remain in-session
- SQL highlighting + local autocomplete (keywords / tables / columns)
- **SQL assistant** — explain or change the current SQL in a collapsible editor
  panel, with streaming replies and checkpoints; recover closed conversations
  from History. Query execution stays on the existing Run control.
- **EXPLAIN button** — one click to the query plan
- Type-aware data grid: sorting, column resize, **keyboard navigation** (arrows + Enter), cell inspection with a **collapsible JSON tree**
- CSV/JSON export, **searchable query history** (with connection + time)
- TYPE-aware Redis key browsing with a collapsible namespace tree
- **Update check** — a background thread polls PyPI once every 24h and shows
  a header badge (with the upgrade command + release notes) when a newer
  `quarry-db` is out. Editable/dev installs are skipped automatically; set
  `QUARRY_UPDATE_CHECK=0` to disable it entirely.

### SQL assistant

Run `qy ai configure` for an interactive setup, or configure an
OpenAI-compatible model endpoint with flags:

```bash
qy ai configure --base-url https://model.example/v1 --model your-model \
  --api-key-env QUARRY_AI_API_KEY
qy ai show
qy ai explain --db planning --env dev --file query.sql
qy ai sql --db planning --env dev 'Show the latest record per session'
```

Set the named key environment variable in the environment that starts `qy gui`
or the CLI command. Alternatively, `--api-key-stdin` stores a key read from stdin
in a separate file with owner-only permissions; `--no-key` supports local
endpoints that require no authentication. `--bifrost` selects Bifrost Virtual
Key authentication, and `--thinking disabled` requests non-thinking responses.
Provider selection and fallback order are managed by the configured gateway;
Quarry does not hard-code a second provider.

Settings live in the `[assistant]` section of `~/.config/quarry/config.toml`.
`QUARRY_CONFIG` overrides that path for both GUI and CLI. With stored
authentication, the API key lives separately in `assistant-credentials.json`
beside the config file, with owner-only permissions. `qy ai show` displays the
effective settings with the key masked.
`assistant-credentials.json` is Quarry's internal storage format, created
automatically by `qy ai configure`; you do not need to write JSON yourself.
Quarry does not register or issue API keys for you.

Configuration is CLI-only: if a model is missing, the GUI
keeps your message and shows the command to run. The endpoint must support
Chat Completions streaming and tool calls for metadata discovery.

Open the vertical **SQL 助手 / SQL Assistant** rail at the left of the SQL
editor. Chat opens on the left, with the SQL editor and query controls on the
right. The panel extends to the bottom of the query toolbar, with chat input
pinned at the bottom. Chat begins at the top of the panel; the small arrow at
the top of the divider hides it. The chat scrollbar stays narrow and next to
the divider. Hiding the panel restores a full-width toolbar and gives
the result grid more space. When expanded, the editor keeps
enough height to leave the chat input visible even after resizing it. Drag the
horizontal divider down to use the available height above the status bar;
drag it back up to reveal the results again. The limit follows the window and
toolbar size. Both dividers share grip sizing, colors and hover feedback; the
vertical divider has its thin border on the left, and its bottom edge joins
the toolbar border without a gap.

Opening the panel does not send a model request. When SQL exists and the chat
input is empty, a faint suggestion shows the complete explanation prompt.
Press Tab to accept the displayed text, then Enter to request the explanation.

Reply language is configured only through the CLI. By default it follows the
system language of the machine running Quarry: Chinese resolves to Chinese,
and other languages resolve to English. To inspect or change this GUI preference:

```bash
qy ai language          # Show the GUI preference and resolved reply language
qy ai language zh       # Chinese GUI replies
qy ai language en       # English GUI replies
qy ai language system   # Follow the system language (default)
```

The choice is stored in `[gui.assistant] reply_language` in Quarry's config and
applies to all GUI assistant replies, independently of interface language.
The GUI reads changes when reopened or brought back into focus; it has no
language-setting controls. Existing drafts, SQL and history are preserved.
The legacy `auto` value is an alias for `system`. This preference does not
affect CLI replies or model endpoint settings.

Hand-written messages are sent unchanged. Send other messages
with Enter; use Shift+Enter for a new line. The assistant can inspect the current
connection's PostgreSQL, MySQL or SQLite table structures, including key
relationships, to help with joins. It cannot join independent connections or
run SQL. Use **Run** to execute a query through Quarry's existing read-only
query path.

Unsent messages stay with their tab when you hide the assistant or switch tabs.

Explanations support Markdown headings, lists, emphasis, code blocks and
tables, including streamed replies and reopened history. Headings use the
theme accent, and SQL code blocks distinguish keywords, strings and numbers.
Wide code and tables scroll within the chat panel. SQL shown in an explanation
stays in the message; only a completed SQL edit updates the editor.

Completed edits update the editor and provide **Restore previous SQL**. If you
edit the query while a reply is arriving, the assistant keeps its proposal for
explicit application. Restoring a checkpoint changes only the SQL; it keeps
the conversation and marks that change as restored, without adding another
restore action. It never rolls back database changes. Editing a saved query
creates a draft rather than modifying the query file.

Closing a tab does not delete its conversation. **History → Conversations**
reopens the latest SQL, messages and checkpoints without executing the query.
Conversations are stored on the machine running `qy gui`, independently of the
browser's open tabs, in `assistant/history.sqlite3` next to Quarry's `config.toml`.
`QUARRY_AI_HOME` can select a different conversation directory.

## Privacy and support boundaries

Quarry does not upload connection credentials, queries or results to a Quarry service. Queries travel to the databases you configure. The GUI binds to localhost by default and checks local origins. Installed GUI packages periodically check PyPI for updates; set `QUARRY_UPDATE_CHECK=0` to disable this.

The optional SQL assistant sends your SQL, conversation messages and relevant
schema metadata to the model endpoint you configure. It does not automatically
send query result rows or database/SSH credentials. Model credentials stay on
the local backend; the browser has no model-key entry form. Conversations and
SQL checkpoints are stored locally and may contain the SQL literals and text
you entered.

Neptune/openCypher is **experimental**, outside the stable database support commitment. Its endpoint and local empty-service tests do not verify real AWS/IAM behavior. See [COMPATIBILITY.md](COMPATIBILITY.md) for supported test environments, persistence limits and the final 1.0 acceptance checklist.

## Roadmap

- Column types in the result contract for all engines
- DuckDB engine
- Cross-environment schema/data diff
- Write audit log (who ran what, where, when)
- Single-binary distribution

## Development & testing

```bash
pip install -e ".[dev]"
createdb quarry_test && psql quarry_test -f tests/seed.sql   # or: make seed
make test        # layered run with a per-layer PASS/FAIL summary
```

Tests are classified into four layers; use `python -m pytest --collect-only -q`
for the current count and CI reports for current coverage.

| Layer | Covers | Needs |
|-------|--------|-------|
| `unit` | pure logic and mocked engine behavior | test dependencies |
| `integration` | real database and in-process CLI/GUI/MCP dispatch | relevant database; Docker for local lifecycle/sync |
| `e2e` | real CLI/MCP subprocesses | PostgreSQL |
| `browser` | real built GUI driven by Chromium (also in e2e) | PostgreSQL + Playwright; Redis for key browsing |

Missing dependencies can skip tests; a green run with skips is not a full release
validation. CI provides PostgreSQL 16, MySQL 8.4 and Redis 7. The unit/integration
coverage gate is **≥95%**; frontend behavior is verified separately.

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

### Explicit production connections

Environment names are labels, not a safety policy. Set `production = true` in
that connection's `connections.toml` section, for example `env = "jp"` with
`production = true`. An omitted flag defaults to false, even for `env = "prod"`.
When upgrading, explicitly mark existing production connections before use.

Use `qy connections set CONNECTION_KEY --production --no-test` to mark one,
or `--no-production` to clear the flag (add `--workspace PATH` before
`connections` for a particular workspace). Other updates preserve the flag.
Production navigation prepares previews without executing them; Run remains an
explicit action. CLI writes require confirmation (or `--yes`), and MCP writes
require `confirm_prod: true`, in addition to their normal write opt-ins.
