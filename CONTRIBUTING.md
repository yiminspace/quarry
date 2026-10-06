# Contributing to Quarry

Thanks for your interest! Quarry is young and contributions of all sizes are welcome — bug reports, docs, new engine backends, GUI polish.

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
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

### Reinstalling after a worktree is removed

`pip install -e .` records the *exact path* you ran it from. If you installed
from a `git worktree` (e.g. one created for a specific issue/branch) and later
remove that worktree, `qy`/`quarry` will fail to import — you'll see a
`FileNotFoundError` naming the now-missing path. That's expected: re-run
`pip install -e ".[dev]"` from a checkout that still exists (your main clone or
a surviving worktree) to point the install back at real source again.

### Running tests

Unit tests run with no setup:

```bash
python3 -m pytest -q
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
- `python3 -m pytest -q` must pass.
- Describe *why*, not just *what*, in the PR body.
- PR titles must follow Conventional Commits (`feat:`, `fix:`, `refactor:`,
  `chore:`, `docs:`, ...). Quarry uses squash merge, so the PR title is the
  release-driving commit message.

## Release flow (fully automated)

- Versioning and changelog updates are handled by `python-semantic-release` on
  every merge to `main`; do not manually edit `project.version` in
  `pyproject.toml` for routine releases.
- Bump rules follow Conventional Commits:
  - `fix` -> patch
  - `feat` -> minor
  - `BREAKING CHANGE` footer or `!` -> breaking change (while on `0.x`,
    configured to bump **minor**, not `1.0.0`)
- The release workflow updates `pyproject.toml`, writes the new `CHANGELOG.md`
  section, creates a git tag, and publishes a GitHub Release. The existing
  publish workflow then ships that release to PyPI.

## Reporting bugs / proposing features

Use the issue templates. For safety-rail bypasses (a way to execute a write without `--write`), please follow [SECURITY.md](SECURITY.md) instead of opening a public issue.
