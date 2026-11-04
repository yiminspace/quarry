# Quarry 2

<!-- mcp-name: io.github.yiminspace/quarry -->

A native database workbench: one reusable Rust kernel, CLI, browser GUI and MCP.
PostgreSQL, MySQL, SQLite, Redis and experimental Neptune; explicit query/write
policies, SSH tunnels, saved queries, local Docker services and a SQL assistant.

## Install

Version 2.0 is currently a release candidate.

```sh
pip install --upgrade --only-binary=:all: quarry-db==2.0.0rc4
qy --version
qy gui
```

Or download a native archive and SHA256SUMS from [GitHub Releases](https://github.com/yiminspace/quarry/releases).
The native executable needs neither Python nor a Rust toolchain. Supported:
macOS/Linux arm64/x86_64; Linux glibc 2.28+. See [install and upgrade](docs/install.md).

## Use

```sh
qy --workspace /path/to/workspace connections
qy --workspace /path/to/workspace exec shop --env dev --sql 'SELECT 1'
qy --workspace /path/to/workspace mcp
```

Workspaces contain `connections.toml` and reusable SQL query files. Actual
credentials remain local. GUI queries stay read-only; CLI writes require
`--write` and production confirmation. MCP writes require both server and
request authorization. SQLite opens existing files; it never invents a database
when a connection path is wrong. Assistant proposals never execute themselves.

Ordinary queries use native database drivers. SSH needs system ssh; Local
services need Docker, and PostgreSQL schema sync needs pg_dump/psql.

## Rust and Python

Rust owns main and all 2.x distribution. `quarry-core`, `quarry-runtime`,
`quarry-local`, `quarry-assistant` and `quarry-service` can be embedded directly
in host applications. [Architecture](docs/rust-migration.md).

Python 1.11.0 is preserved at its tag and the protected `archive/python-1.x`
branch. Python import APIs do not ship in 2.x. The Python source retained in this
checkout is a development/test baseline, not a runtime dependency or published
Python implementation. Existing configuration, saved queries and histories are
read by Rust. See upgrade instructions before changing installer ownership.

## Build and verify

```sh
cd web && npm ci && npm run build && cd ..
cargo build --release --locked -p quarry-cli
cargo test --workspace --all-targets --locked
```

[GUI feature matrix](TESTING.md) · [Compatibility](COMPATIBILITY.md) ·
[Verification boundaries](docs/rust-verification.md) · [中文](README.zh-CN.md)
