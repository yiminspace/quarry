# Contributing to Quarry

Rust 2.x is the current runtime. The retained Python implementation is a
development baseline for tests. See [installation](docs/install.md) for native
releases and [CI](.github/workflows/ci.yml) for the verification steps.

## Ground rules (design invariants)

Quarry has a few invariants that PRs must not break. If your change needs to bend one, open an issue first so we can discuss.

1. **The kernel stays dependency-free.** `quarry.core` is pure stdlib. Engine backends may shell out to system binaries (`psql`, `redis-cli`, `ssh`) or use an *optional* extra (like `pymysql`), but nothing gets added to the required dependencies.
2. **Safety rails live in the kernel, not in the faces.** Read-only enforcement, row caps, and prod confirmation must work identically through the CLI, the GUI, and library calls. Never implement a safety check only in one face.
3. **The result and exit-code contracts are stable API.** `{columns, rows, rowCount, truncated, elapsedMs, engine, sql}` and exit codes `0/2/3/8` — additive changes only.
4. **Faces stay thin.** If logic could be useful to more than one face, it belongs in `quarry.core` (or a sibling kernel module), not in `cli.py`/`gui.py`.
5. **The kernel carries no secrets and no business logic.** Connections and queries always come from a user workspace.

## Development setup

```bash
git clone https://github.com/yiminspace/quarry && cd quarry
npm --prefix web ci && npm --prefix web run build
cargo build --locked -p quarry-cli
```

For the Python test and browser harness, activate an isolated environment and
install its dependencies:

```bash
python3 -m venv .venv && source .venv/bin/activate
make install
make browser-install
```

### Manual hosted GUI preview

Run `make preview-hosted` to build the current frontend and open
`http://127.0.0.1:18765/quarry/` in your browser. Leave the terminal running;
press Ctrl+C to stop. Run the command again after editing the frontend.
This preview uses demo resources and fixed query results, requires no Console
login and does not execute SQL. It defaults to a connected demo companion,
writing personal documents to a temporary SQLite file whose path is printed.
The file is deleted when the preview stops. Use
`make preview-hosted PREVIEW_ARGS='--disconnected'` to inspect browser-only
recovery. Real pairing and Assistant acceptance require the real services.
Use `make preview-hosted PREVIEW_ARGS='--port 18767 --no-open'` to change the
port or skip opening a browser. It needs Node/npm and Python 3.10+, but no Rust
build, Playwright or database.

### Running tests

Run the native tests after building the web assets:

```bash
cargo test --workspace --all-targets --locked
```

DB-backed tests need a local PostgreSQL with a seeded `quarry_test` database (they skip automatically if it's unreachable):

```bash
createdb quarry_test
psql quarry_test -f tests/seed.sql
python3 -m pytest -q          # QUARRY_TEST_DB_URL overrides the default URL
```

## Adding an engine backend

An engine implements: URL parsing, query execution returning the standard result shape, read-only classification for its command/statement set, and (optionally) schema introspection. Look at `redis_engine.py` for the smallest complete example, and how `core.py` dispatches on `engine`. Requirements:

- Prefer shelling out to the engine's standard client binary over adding a Python driver; if a driver is unavoidable, make it an optional extra.
- Define the read-only command set conservatively — when in doubt, a command is a write.
- Add unit tests that don't require a live server (parsing, safety classification) plus live tests guarded by a reachability skip.

## Pull requests

- Keep PRs focused; separate refactors from behavior changes.
- Add or update tests for what you change.
- Run the relevant Cargo and browser tests. See [the GUI matrix](TESTING.md).
- Describe *why*, not just *what*, in the PR body.
- PR titles must follow Conventional Commits (`feat:`, `fix:`, `refactor:`,
  `chore:`, `docs:`, ...). Quarry uses squash merge.

## Release flow

Update the Cargo workspace version and release notes, then verify the explicit
tag with `python3 scripts/validate_release.py vX.Y.Z`. The release workflow
requires CI for the exact main commit before it builds and checks all four
native archives. GitHub Releases hosts the archives and their SHA-256
checksums. The stable release also carries the verified hosted image descriptor.

## Reporting bugs / proposing features

Use the issue templates. For safety-rail bypasses (a way to execute a write without `--write`), please follow [SECURITY.md](SECURITY.md) instead of opening a public issue.
