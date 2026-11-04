# Rust architecture and migration

Quarry 2.x uses a native Rust runtime. Python 1.11.0 is preserved in its release
tag and the locked `archive/python-1.x` branch as a rollback release and behavior
baseline; it is not called by the Rust binary or libraries. Python files retained
in this checkout support development and regression tests, not production
execution. See [installation and upgrade](install.md) for published artifacts.

## Build and run without replacing the installed CLI

Cargo declares Rust 1.89 or later. Node is needed to build the existing React
assets, not to run the workbench. Build the assets before compiling because the
binary embeds `src/quarry/web_dist`.

```sh
cd web
npm ci
npm run build
cd ..
cargo build -p quarry-cli --release --locked
./target/release/qy --help
./target/release/qy --config /path/to/config.toml --workspace /path/to/workspace gui --no-browser
```

Use a disposable copy of a workspace first. Keep the same browser origin
(`http://127.0.0.1:8765`) when checking existing drafts/results/preferences; a
different port or hostname uses different browser storage. Stop an older GUI
before binding that port. Restarting does not run saved SQL automatically.

Queries support PostgreSQL, MySQL, SQLite, Redis and experimental Neptune.
PostgreSQL/MySQL/Redis use native Rust clients; SQLite is bundled. System SSH
is retained for its configuration/agent semantics. Docker is needed only for
managed local services; schema sync requires a compatible `pg_dump` and `psql`.
`QUARRY_PSQL` and `QUARRY_PG_DUMP` configure those tools at the standalone CLI
boundary. Normal queries do not need those command-line database clients.

## Code ownership and embedding

| Crate | Responsibility |
| --- | --- |
| `quarry-core` | Positional values/results, engine adapters, metadata, query policies, budgets, cancellation, shared bounded admission |
| `quarry-runtime` | Explicit configuration/catalogs, connection/query files, SSH leases, proxies, keeper/daemon launch contracts |
| `quarry-local` | Docker lifecycle, local registration, Neptune fixtures, staged PostgreSQL schema sync and recovery |
| `quarry-assistant` | Model transport, metadata-only tools, proposals, historical conversations and revision CAS |
| `quarry-service` | Shared facade and connection resolution used by entry points and embedding applications |
| `apps/qy` | Standalone CLI, MCP stdio and local HTTP adapters; embedded unchanged workbench assets |

`ylab`/`bcli` can depend on these crates directly. No separately installed `qy`
or Python subprocess is required. Both examples perform actual SQLite work:

```sh
cargo run -p quarry-core --example embedded_sqlite --locked
cargo run -p quarry-service --example embedded_host --locked
```

The facade accepts explicit config/workspace/state paths. Identity and resource
authorization belong to the host; query write/production restrictions remain
in the kernel. A hosted adapter must resolve only its granted resources and
must deliberately select which local/configuration/Docker capabilities to
expose. Quarry itself does not provide IAM authorization or change ylab/bcli repositories.

A host that wants a durable local Neptune process supplies a `DaemonLauncher`
pointing to its own hidden dispatch command; a keeper also accepts an explicit
launcher. Libraries do not assume the current executable is `qy`. Alternatively,
a host can own the runtime and call `serve_neptune`/`run_keeper` itself.

## Existing state

The implementation reads the existing TOML configuration and connections,
query headers, schema-source fingerprints, local registration naming/scope,
assistant credential file and SQLite conversation documents. It preserves the
old assistant connection identity hash, omitted environment values and SQLite
file identity. Saved-query IDs and displayed workspace paths retain the forms
used by persisted browser tabs. SQL assistant prompts were copied unchanged;
proposals are never automatically executed.

Docker keeps the existing container/volume names by default, respects configured
ports (including a nondefault PostgreSQL port), and only publishes new managed
services on loopback. `down` retains volumes; purge requires explicit authority.
Testing uses separate namespaces and temporary state, never user volumes.

An existing wildcard-bound container is reported as a binding conflict. The
candidate preserves it and its volumes. Review its actual image, mounts and
published port before explicitly recreating only the container with a loopback
binding; do not remove its data volume. Custom mounts must be preserved. Status
reports actual published bindings and configured-port drift instead of claiming
an older container is loopback-only.

Runtime files are isolated beside the selected config under `rust-runtime/`.
The candidate does not trust or kill processes merely because an old Python
PID/keeper file names them. Stop old keepers or local Neptune processes through
their owning version before starting replacements on the same port. The old
Python release remains available for rollback; do not run two local managers
concurrently against the same resource.

Schema sync imports schema into staging, verifies the intended local target,
locks a target across processes, journals the rename sequence and retains the
previous database. Compensation/recovery is tested; the rename sequence is not
advertised as an atomic database swap. Client key passwords should use a libpq
service file, rather than an argument containing a secret.

## Validation and release gates

See [Rust verification](rust-verification.md) for the exact test commands and
verification boundaries. CI must pass for the exact main commit before the
release workflow builds four native platform wheels and matching archives. Each
platform must pass fresh-install, Python 1.11 upgrade and Python-free execution
checks before publication to PyPI and GitHub Releases. Stable versions also
publish the pinned MCP manifest. Historical Python tags remain available for
rollback. A configured workflow alone is not proof that a remote run has passed.
