# Rust verification

All database tests use disposable loopback targets. No business database is a
test fixture. Browser tests selected with `QUARRY_RUST_BINARY` start an actual
native HTTP process; Python is only the test runner/baseline.

## Reproducible commands

The Docker suite expects a disposable schema-sync source at this exact fixture
name/port. Create it in a test environment; do not replace any existing resource
with the same name. The source password below is fixture data only.

```sh
docker run --detach --rm --name quarry-rust-core-pg \
  --publish 127.0.0.1:55322:5432 --env POSTGRES_PASSWORD=fixture-only \
  --env POSTGRES_DB=quarry_fixture postgres:16-alpine
docker run --detach --rm --name quarry-rust-core-redis \
  --publish 127.0.0.1:55337:6379 redis:7-alpine
```

```sh
cargo fmt --all --check
cargo clippy --workspace --all-targets --locked -- -D warnings
cargo build -p quarry-cli --locked
QUARRY_TEST_BINARY="$PWD/target/debug/qy" QUARRY_TEST_DOCKER=1 \
  cargo test --workspace --all-targets --locked

# Supply only explicit disposable databases; these tests write test fixtures.
QUARRY_TEST_POSTGRES_URL=postgres://USER:PASS@127.0.0.1:PORT/FIXTURE \
QUARRY_TEST_MYSQL_URL=mysql://USER:PASS@127.0.0.1:PORT/FIXTURE \
QUARRY_TEST_REDIS_URL=redis://127.0.0.1:PORT \
  cargo test -p quarry-core --test remote_contract --locked -- --ignored

QUARRY_TEST_DB_URL=postgresql://USER:PASS@127.0.0.1:PORT/SEEDED_BROWSER_FIXTURE \
QUARRY_TEST_REDIS_URL=redis://127.0.0.1:PORT \
QUARRY_RUST_BINARY="$PWD/target/debug/qy" \
  python3 -m pytest tests/test_gui_browser.py tests/test_gui_react_app.py \
  tests/test_gui_browser_features.py tests/test_gui_visual.py \
  -m browser -k 'not wheel and not sdist'
```

Build `web/` before Cargo; initialize the disposable browser database from
`tests/seed.sql`. The three Python wheel/sdist packaging tests remain baseline
checks, not native acceptance. Native process tests never import Python.

## Evidence collected locally

- Actual PostgreSQL and Redis containers, actual SQLite files: values, metadata,
  parameters, read-only restrictions, empty/duplicate columns, limits, record
  links and cancellation. PostgreSQL cancellation also checks server activity.
- Strict TLS through forwarded endpoints: actual PostgreSQL and Redis servers,
  a MySQL TLS protocol fixture, and a Neptune HTTPS fixture. Original hostname
  verification remains active; incorrect hostname/CA are negative cases.
- Actual isolated Docker start/idempotence/down/restart/data retention/purge;
  actual `pg_dump`/`psql` schema sync from a source constrained to read-only,
  schema/type/view/index verification, empty imported data and previous local
  database retention. Failed second rename and interrupted journal recovery
  have separate deterministic tests.
- Assistant real loopback SSE/JSON provider requests, headers, tools, proposals,
  interruptions, timeout/cancellation/close, old histories and SQLite CAS.
- Native CLI/MCP/HTTP entry points, local origin guard, persistent metadata
  health cache and two direct library consumers.

The full GUI matrix remains in [TESTING.md](../TESTING.md). Backend-specific
Python monkeypatch fixtures have been replaced by transport-boundary stubs or
real native process/configuration changes where needed; a UI stub verifies
rendering and interaction, while native library/process tests verify execution.

## Remaining release evidence

- MySQL 8.4 real query/type/metadata acceptance is configured in Linux CI but
  could not run locally because server-image/dependency downloads failed. A TLS protocol fixture
  is not MySQL query acceptance.
- macOS arm64 has actual local execution. Linux and macOS x64 artifacts and
  fresh-install/upgrade rehearsals must pass on their intended platforms before
  release. Non-Unix forwarded SQL transport returns Unsupported explicitly.
- Production AWS Neptune/IAM behavior is not proven by local fixtures and remains
  experimental, as in the prior implementation.
- Comparative release-binary latency, peak memory and large-result measurements
  are required before publishing performance claims. No speedup is promised. Result budgets limit accepted output; individual
  driver rows/frames may be read before that check, so they do not establish
  a hard process-memory ceiling.
- The installed CLI and publication channel remain on the previous release until
  the candidate and migration rehearsal are accepted.

## Local candidate artifact

The optimized macOS arm64 binary was built and executed with an empty `PATH`
against a disposable SQLite workspace. Its query path does not need Python,
Node or a database CLI. A local tar archive and SHA-256 file live under
`target/release/`; this is a local candidate, not a published release.

## 2026-11-03 local acceptance snapshot

- Optimized packaged macOS arm64 candidate: **316 browser tests passed**; only
  the three Python wheel/sdist packaging tests were deselected. The UI source
  and built assets are unchanged.
- Workspace library/process contracts, real disposable Docker lifecycle and
  read-only-source schema sync passed. Real PostgreSQL/Redis contracts and
  strict TLS positive/negative cases passed.
- Additional native regressions cover malformed-query inventory, saved-query
  parameter privacy, related-workspace MCP execution and CLI shutdown with an
  open SSE stream. Strict Clippy, formatting and publication guards passed.
- Real MySQL query/type acceptance and remote platform/MSRV workflow runs remain
  pending; they are not represented as passing evidence.


## 2026-11-03 daily GUI cutover trial

The installed macOS arm64 candidate now serves the existing loopback GUI origin.
The previous Python GUI process was stopped after its identity was checked;
the Python package and terminal entry point remain installed for rollback.
A private backup and an explicit rollback script are retained locally.

- All 28 configured connection environments, 31 saved queries and 9 assistant
  conversation IDs were preserved. Configuration, query and credential files
  remained byte-identical. The two old Local containers, their volumes and
  port bindings were not changed.
- Real configured PostgreSQL, MySQL, Redis and Neptune connections passed
  read-only constant probes. PostgreSQL BIGINT/NUMERIC and MySQL unsigned
  BIGINT/DECIMAL preserved their exact values. The GUI query API also completed
  a real PostgreSQL constant query. These probes do not claim full metadata,
  write/cancel or AWS IAM coverage for those remote services.
- A live migration regression in the proxy badge was fixed: a configured proxy
  is insufficient; SSH connections require a matching live tunnel. Rust does
  not claim Python-owned tunnels. Runtime/service regressions and the rebuilt
  release candidate passed targeted tests, formatting and Clippy.
- The real assistant request was rejected by the configured model endpoint's
  authentication. The original Python CLI, using identical copied credentials
  and an isolated history, was rejected as well. The assistant end-to-end
  criterion therefore remains failed; this trial does not claim a successful
  real-model reply. Original history and business databases were not modified.

Earlier failed probe attempts and environment-start interruptions remain in
local verification evidence; they were not overwritten. The retained native
GUI is a local trial, not a PyPI/native release. Platform wheels and other OS/CPU
artifacts still require distribution work and their own install validation.


## Daily GUI feedback repairs

Later testing resolved the model-authentication failure described above: the
old model origin now serves a browser-only management route. The workbench now
uses the inference API through IAM, obtaining fresh credentials from its
trusted host CLI before each request instead of saving a short-lived token.
Two real model replies succeeded, including a reply after an explicit session
refresh, using isolated history and an unchanged SQLite file.

The installed GUI also fixes SSH setup holding a global pool lock, workspace
status checks on unresolved home-relative paths, and overlapping/unbounded
health-button requests. Workspace status reports both real directories and
connection files correctly; a confirmed missing test-only registration was
removed. Original connection/query/credential files and legacy Local Docker
containers remain unchanged. Production Redis returned PONG and a positive
fresh health check; the refusal shown in the sidebar belonged to stopped Local
Redis, not the production target.

PostgreSQL and MySQL health probes now use native protocol ping over the same
strict TLS and SSH transport. Ordinary query transactions and write protections
are unchanged. The remote batch of 16 health probes completed with terminal
responses and positive results for known-working PostgreSQL/MySQL while a real
PostgreSQL query and connection-list request also completed. Targeted strict-TLS,
wrong-hostname, handshake-timeout, SSH-concurrency and command-credential tests
passed. Three rebuilt-native browser regressions passed, covering health-button
reentrancy/timeout recovery and workspace status. These are targeted repairs,
not a repeat of the earlier complete browser run or a cross-platform release.
