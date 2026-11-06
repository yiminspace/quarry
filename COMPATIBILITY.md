# Quarry interface and support contract

Quarry 2.x follows the functional and state-preservation contracts below.
Native crates, dependencies, and migration limits are recorded in
[the Rust guide](docs/rust-preview.md) and
[verification](docs/rust-verification.md). References to Python APIs below
describe the historical 1.x implementation.

This document defines the stable interface contract starting with Quarry 1.0.0. Experimental capabilities are identified separately. Passing local tests is not a statement that every platform or database version is supported.

## Entry points

| Entry | Read queries | Writes | Output |
|---|---|---|---|
| CLI `qy exec` / `qy run` | Default | `--write`; every prod write opt-in also requires confirmation or `--yes` | `json`: array of rows; `ndjson`: one row per line; CSV/table: rendered rows. Diagnostics/truncation/stats go to stderr. |
| GUI query / saved-query API | Default | Not offered | QueryResult object; grid, copy and exports use the active tab's snapshot. Local lifecycle/sync controls are separate operations. |
| MCP `exec_sql` | Default | Server `--write` + per-call `write:true`; prod also requires `confirm_prod:true` | QueryResult object within the MCP tool result. |
| MCP `run_saved_query` | Default | Not offered | QueryResult object. |
| MCP `describe_saved_query` / `save_query` | No SQL execution | `save_query` writes local query files; explicit `overwrite:true` required to replace | SQL/metadata or saved file path; independent of database `--write`. Multiple workspaces require an explicit configured target. |
| MCP `ping_connection` | Fixed engine probe only | Not offered | `ok`, `elapsed_ms`, target identity and sanitized failure category; timeout 1–30 seconds per setup/probe phase. |
| Public Python `run_query` | Default | Explicit `allow_write=True`; this is a trusted library authorization, with no interactive prod prompt | QueryResult object. Applications must obtain authorization before passing this flag. |

QueryResult fields: `columns`, `rows`, `rowCount`, `truncated`, `elapsedMs`, `engine`, `sql`, `downloadBytes`, `sizeIsEstimated`. `rowCount` counts returned rows, **not affected rows**. Non-returning writes yield empty rows after successful execution/commit; PostgreSQL INSERT/UPDATE/DELETE with RETURNING yields returned rows. SQL batches, psql backslash commands and PostgreSQL data-modifying CTEs are rejected; issue a standalone statement instead. New optional result fields may be added; consumers should ignore unknown fields.

Ordinary JSON row arrays remain the CLI default for existing scripts. Do not parse CLI stderr as JSON or assume the CLI stdout has QueryResult metadata. CLI errors use process exit codes; Python raises QuarryError; GUI includes `code` in error responses; MCP sets `isError` and includes the error payload. Stable query codes: 0 success, 1 usage, 2 connection, 3 execution, 8 safety rejection; argparse syntax errors use 2. Other commands have their documented command-specific codes (for example `ping` returns 1 if any probe fails).

## Read-only and result limits

PostgreSQL and MySQL default query execution uses database read-only transactions, in addition to query classification. SQLite opens existing files in read-only mode and restricts unsafe operations; authorized writes open existing files for read/write without creating a missing database. Redis uses command checks; Neptune uses conservative openCypher read classification. Writes requiring authorization include EXPLAIN ANALYZE over mutations. A database account with read-only privileges can provide an additional server-side boundary, especially for graph/Redis access.

SQL read queries and openCypher RETURN queries without an outer LIMIT default to 500 rows. Inner subquery limits do not disable the outer cap. An explicit outer LIMIT/FETCH is preserved. PostgreSQL locking queries and utility statements are not rewritten. Redis result caps are applied after receipt, not inside the server command. `--max-rows 0` / Python `max_rows=None` explicitly disables the cap; Python/MCP also accept 0. CLI emits a truncation notice to stderr. GUI/MCP/Python expose `truncated`. No stable ordering is implied without an explicit ordering clause when paging.

## Result fidelity

- Integers outside JavaScript's safe range (±9007199254740991) are JSON strings.
- PostgreSQL JSON fractional numbers and MySQL DECIMAL values are strings, preserving precision and scale. Ordinary MySQL floating-point types retain their driver-provided numbers.
- Duplicate object-result names are deterministically suffixed (`id`, `id_2`, `id_3`, ...), avoiding collisions rather than discarding columns. Native PostgreSQL CSV/table output retains positional headers, including duplicates. Use explicit SQL aliases when exact names are important.
- Empty relational results retain available column metadata; the GUI shows headers and exports a CSV header.
- Redis requires redis-cli 6+ for JSON responses. Scalars are one `value` row, arrays are multiple `value` rows, nested arrays stay nested, nil is `null`, and empty/multiline strings retain their content. Redis server errors are failures, not successful rows containing error text.
- Redis `--scan` returns one key per row using cursor-based JSON replies, preserving whitespace and newlines in keys. Supported scan options are `--pattern`, `--count`, `--cursor` and `-i`; other redis-cli modes or connection overrides are rejected.
- MySQL datetime values preserve subsecond precision. Invalid UTF-8 binary values use a `base64:`-prefixed string; valid UTF-8 bytes remain text.
- SQLite uses its native integer, real, text and null values, with the same large-integer string rule. BLOB values are tagged objects (`{"$binary": "<base64>"}`), including bytes that could decode as text. Nonfinite reals use `{"$number": "Infinity"}` or `{"$number": "-Infinity"}` (and defensively `"NaN"`) to keep JSON valid and distinct from text. Declared column types do not change SQLite's stored value types. Record links require a primary key or a complete nonpartial unique index; binary, nonfinite, null or ambiguous large-integer keys are skipped in favor of another usable unique key.
- JSON/CSV exports preserve these values in the file. A spreadsheet application may infer types when importing CSV; use its text-column import options for long identifiers.

## Environment and dependency scope

Native releases support macOS 11+ and Linux glibc 2.28+ on arm64 and x86_64.
The executable embeds its database drivers and web assets. SSH connections use
the system `ssh` executable, Local services use Docker, and PostgreSQL schema
sync uses `pg_dump` and `psql`. The keeper started by `qy up` is optional.

SQLite support covers existing database files accessible on the machine running Quarry. URLs use `sqlite:///relative/path.db` for a path relative to the connection's workspace, and `sqlite:////absolute/path.db` for an absolute path; a plain path requires explicit `engine = "sqlite"`. Connection tests, table/view metadata, queries, parameters, exports and GUI paging share the normal interfaces. Missing files are errors. SQLite URI options, `:memory:`, remote URLs, SSH tunnels and attached databases are unsupported. Environment labels use the existing `env` field; different files provide isolation and `production = true` explicitly enables production classification. Docker lifecycle and PostgreSQL schema sync do not apply to SQLite.

Each SQLite request uses its own connection and transaction with foreign-key enforcement enabled. Authorized writes commit on success and roll back on failure; transactions cannot span requests. Read-only metadata PRAGMAs are supported, while explicit transaction control, extension loading and `VACUUM` are unavailable.

Native CI exercises Linux with PostgreSQL 16, MySQL 8.4 and Redis 7, plus
Chromium browser tests. The release workflow verifies the archive on each
supported OS and architecture.

Neptune/openCypher is **experimental**, outside the stable database support commitment. The HTTP(S) endpoint integration is available; the local empty endpoint accepts requests but does not store or execute graph data. It is not an AWS Neptune emulator and does not verify real AWS authentication, IAM signing or query semantics. Promoting Neptune to stable support requires real-service acceptance tests.

## Local state and network behavior

The GUI stores SQL/connection drafts and bounded result snapshots in browser localStorage when it is available. Results with reported download size above 512 KiB stay in-session; the combined persisted-result budget is 1.5 MiB, with the active tab prioritized. Clearing or disabling browser storage, storage exhaustion, or changing the GUI origin can prevent restoration. Export results that must be retained.

Quarry does not upload connection credentials, queries or results to a Quarry service. Queries travel to configured databases; MCP returns results to the chosen client, whose own data-handling policy applies. The GUI binds to localhost by default and checks local Host/Origin headers. Standalone Quarry checks GitHub Releases for updates. Set
`QUARRY_AUTO_UPDATE=0` to disable automatic staging and activation in
installer-managed copies.

## 1.0 evolution policy

Published CLI command names/options, workspace configuration keys, saved-query metadata, MCP tool names/arguments and names exported by `quarry.__all__` form the public interface. Private helpers and GUI layout are implementation details. Compatible optional fields/features may be added; removals or meaning/type changes require a documented migration and major version after 1.0. The lossless numeric representation was introduced in 0.25.1 and is retained in 1.0.0.

An explicit version tag starts the native release workflow. The workflow
requires successful CI for the exact main commit before publishing native
archives and checksums to GitHub Releases.

## Release acceptance checklist

- Set the Cargo workspace version to match the tag and prepare release notes.
- Verify the native archive, fresh installation, retained workspace, CLI, GUI and MCP on each supported platform. Rehearse updating the previous stable version and confirm that drafts and bounded results remain usable without rerunning queries on reload.
- Verify the published archive and both landing-page languages against the released contract.

## 中文摘要

- CLI 保持 JSON 行数组，诊断、统计和截断提示走 stderr；GUI/MCP/Python 返回 QueryResult。`rowCount` 是返回行数，不是写入影响行数。
- GUI 和 MCP 的已存查询执行只读；MCP `save_query` 只保存本地文件，不执行或验证 SQL，覆盖需显式 `overwrite:true`，多工作区需指定目标。CLI prod 的每次 `--write` 都需确认；MCP 写入需服务端、调用和 prod 确认三层授权。Python 的 `allow_write=True` 是调用方已经取得授权的声明，不另外弹出 prod 确认。
- 默认查询上限为 500；显式外层 LIMIT 保留；CLI `--max-rows 0` 取消限制。Redis 在取回结果后截断，工具语句/PG 锁定查询不改写。SQL 批处理、psql 反斜杠命令、PG 修改型 CTE 不支持，请逐条执行；普通 INSERT/UPDATE/DELETE 可显式授权，RETURNING 保留返回结果。
- 大整数、高精度小数按上述规则返回字符串，重复列名自动加后缀，空查询保留可用列信息；Redis 保留 null、空字符串、多行文本和嵌套数组，执行错误不再伪装成数据。
- 原生程序内嵌 GUI 和数据库驱动。`qy up` 可启动后台 keeper。
- CI 的 Linux/数据库版本矩阵和本机 macOS 验证范围如上；未验证平台不能视为支持承诺。Neptune 为实验性支持，不纳入稳定数据库支持承诺；本地空服务不验证真实 AWS 能力。
- 浏览器存储可用时保留草稿和有限的结果快照；单结果报告的下载大小超过 512 KiB 时仅留在会话，总持久化预算为 1.5 MiB。清理/禁用存储、空间不足或更换 GUI origin 会影响恢复。
- 查询发送到配置的数据库，MCP 结果返回所选客户端。独立版 GUI 默认绑定 localhost 并检查本地来源，安装脚本管理的版本可用 `QUARRY_AUTO_UPDATE=0` 关闭自动更新。
- 公开接口的破坏性变更需主版本和迁移说明。发布时用显式标签触发 CI，验收各平台的原生安装包后上传至 GitHub Releases。升级验收覆盖连接、保存的查询、草稿和缓存结果恢复。
