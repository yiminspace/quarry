"""Schema sync from a remote env into a local Postgres — `qy local sync`.

Copies structure with `pg_dump --schema-only` (no migration framework), then
swaps it in without ever mutating the live local database in place:

    1. the dump is applied to a fresh `<db>__staging` database — a failure
       there leaves the current database untouched;
    2. the previous `<db>__prev` backup is dropped, `<db>` is renamed to
       `<db>__prev`, and staging is renamed to `<db>` (two millisecond-level
       renames, so the service-facing database name never changes).

The target must resolve to `env=local` AND point at a loopback host without an
SSH tunnel; there is no override for that safety gate.
"""

from __future__ import annotations

import glob
import re
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from . import core, local, tunnel, workspace
from .core import (
    EXIT_CONNECTION_ERROR,
    EXIT_SQL_ERROR,
    EXIT_SYNC_DENIED,
    EXIT_USAGE,
    QuarryError,
    connection_engine,
    run_psql_capture,
)

_PG_VERSION_RE = re.compile(r"(?:PostgreSQL\s+)?(\d+)(?:\.\d+)?")

# Hosts a sync target is allowed to resolve to. `env=local` is just a field in
# connections.toml — a hand-edited entry pointing anywhere else must not pass.
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}

STAGING_SUFFIX = "__staging"
PREV_SUFFIX = "__prev"

# Postgres identifiers cap at 63 bytes; the db name must leave room for the
# longest suffix we append.
_MAX_DB_NAME = 63 - len(STAGING_SUFFIX)


def _keg_major(path: str) -> int:
    m = re.search(r"@(\d+)", path)
    return int(m.group(1)) if m else 0


def pg_dump_candidates() -> list[str]:
    """Every pg_dump this machine plausibly has, in preference order:
    the QUARRY_PSQL bin dir, PATH, then versioned install dirs (Homebrew kegs
    on macOS, /usr/lib/postgresql on Debian/Ubuntu) newest first."""
    cands: list[str] = []
    psql_path = Path(workspace.WS.psql_bin)
    if psql_path.name == "psql":
        c = psql_path.with_name("pg_dump")
        if c.exists():
            cands.append(str(c))
    if shutil.which("pg_dump"):
        cands.append("pg_dump")
    for pattern in ("/opt/homebrew/opt/postgresql@*/bin/pg_dump",
                    "/usr/local/opt/postgresql@*/bin/pg_dump"):
        cands.extend(sorted(glob.glob(pattern), key=_keg_major, reverse=True))
    cands.extend(sorted(glob.glob("/usr/lib/postgresql/*/bin/pg_dump"), reverse=True))
    seen: set[str] = set()
    return [c for c in cands if not (c in seen or seen.add(c))]


def resolve_pg_dump() -> str:
    """The default pg_dump, ignoring version (first candidate found)."""
    cands = pg_dump_candidates()
    if not cands:
        raise QuarryError(
            "pg_dump not found in PATH (install postgresql client tools or set QUARRY_PSQL's bin dir)",
            exit_code=EXIT_CONNECTION_ERROR,
        )
    return cands[0]


def select_pg_dump(server_major: int, *, dump_bin: str | None = None) -> str:
    """Pick a pg_dump whose major version can dump `server_major`.

    An explicit dump_bin is honored but still version-checked. Otherwise every
    candidate on the machine is probed, so having postgresql@17 installed is
    enough — no PATH or QUARRY_PSQL surgery required."""
    if dump_bin:
        client = pg_dump_major_version(dump_bin)
        if client < server_major:
            raise QuarryError(
                f"pg_dump client is PostgreSQL {client} but source server is "
                f"{server_major} — install a pg_dump at least as new as the server "
                f"(e.g. `brew install postgresql@{server_major}`)",
                exit_code=EXIT_CONNECTION_ERROR,
            )
        return dump_bin
    found: list[tuple[str, int]] = []
    for cand in pg_dump_candidates():
        try:
            major = pg_dump_major_version(cand)
        except QuarryError:
            continue
        if major >= server_major:
            return cand
        found.append((cand, major))
    if not found:
        raise QuarryError(
            "pg_dump not found in PATH (install postgresql client tools or set QUARRY_PSQL's bin dir)",
            exit_code=EXIT_CONNECTION_ERROR,
        )
    best = max(v for _, v in found)
    raise QuarryError(
        f"pg_dump client is PostgreSQL {best} but source server is "
        f"{server_major} — install a pg_dump at least as new as the server "
        f"(e.g. `brew install postgresql@{server_major}`)",
        exit_code=EXIT_CONNECTION_ERROR,
    )


def parse_pg_major(version_text: str) -> int:
    m = _PG_VERSION_RE.search(version_text.strip())
    if not m:
        raise QuarryError(
            f"cannot parse PostgreSQL version from: {version_text!r}",
            exit_code=EXIT_CONNECTION_ERROR,
        )
    return int(m.group(1))


def pg_dump_major_version(dump_bin: str | None = None) -> int:
    bin_path = dump_bin or resolve_pg_dump()
    try:
        proc = subprocess.run(
            [bin_path, "--version"], capture_output=True, text=True, timeout=10,
        )
    except subprocess.TimeoutExpired:
        raise QuarryError("pg_dump --version timed out", exit_code=EXIT_CONNECTION_ERROR)
    if proc.returncode != 0:
        raise QuarryError(
            f"pg_dump --version failed: {proc.stderr.strip()}",
            exit_code=EXIT_CONNECTION_ERROR,
        )
    return parse_pg_major(proc.stdout or proc.stderr)


def server_pg_major_version(url: str) -> int:
    rc, out, err = run_psql_capture(
        url, "SHOW server_version", timeout=15,
    )
    if rc != 0:
        raise QuarryError(
            f"failed to read server version: {err.strip()}",
            exit_code=EXIT_CONNECTION_ERROR,
        )
    return parse_pg_major(out)


# ---------------------------------------------------------------------------
# safety gate
# ---------------------------------------------------------------------------

def _require_local_target(conn: core.Connection) -> None:
    """Both halves are hard requirements with no override: the destructive part
    of sync (dropping the `__prev` backup, renaming the live db) must never be
    reachable from a shared/remote environment."""
    if (conn.env or "").lower() != local.LOCAL_ENV:
        raise QuarryError(
            f"sync refused: target connection [{conn.key}] has env={conn.env!r}, "
            f"not '{local.LOCAL_ENV}' — this command only runs against local databases",
            exit_code=EXIT_SYNC_DENIED,
        )
    if conn.ssh_host:
        raise QuarryError(
            f"sync refused: target connection [{conn.key}] uses an SSH tunnel "
            f"(ssh_host={conn.ssh_host}) — a tunneled database is not local",
            exit_code=EXIT_SYNC_DENIED,
        )
    host = (urlsplit(conn.url).hostname or "").lower()
    if host not in LOCAL_HOSTS:
        raise QuarryError(
            f"sync refused: target connection [{conn.key}] points at host "
            f"{host!r}, not a loopback address — this command only runs "
            "against local databases",
            exit_code=EXIT_SYNC_DENIED,
        )


def _require_postgres(conn: core.Connection, role: str) -> None:
    eng = connection_engine(conn)
    if eng != "postgres":
        raise QuarryError(
            f"sync only supports postgres (the {role} connection [{conn.key}] is {eng})",
            exit_code=EXIT_USAGE,
        )


# ---------------------------------------------------------------------------
# URL / database-name plumbing
# ---------------------------------------------------------------------------

def _replace_db(url: str, dbname: str) -> str:
    parts = urlsplit(url)
    return urlunsplit(parts._replace(path=f"/{dbname}"))


def _admin_url(url: str) -> str:
    """Same server, maintenance database — CREATE/DROP/RENAME DATABASE cannot
    run while connected to the database being managed."""
    return _replace_db(url, "postgres")


def target_db_name(url: str) -> str:
    """Database name from the target URL (parsed, not queried — the target may
    not exist yet on a fresh container)."""
    name = urlsplit(url).path.lstrip("/")
    if not name:
        raise QuarryError(
            f"target URL has no database name: {url}", exit_code=EXIT_USAGE,
        )
    if not local.SAFE_DB_RE.match(name):
        raise QuarryError(
            f"'{name}' is not a safe database name (letters, digits, underscore; "
            "must start with a letter)", exit_code=EXIT_USAGE,
        )
    if len(name) > _MAX_DB_NAME:
        raise QuarryError(
            f"database name '{name}' is too long for sync "
            f"(max {_MAX_DB_NAME} chars — room is needed for the "
            f"'{STAGING_SUFFIX}' suffix)", exit_code=EXIT_USAGE,
        )
    return name


def check_local_reachable(admin_url: str, conn: core.Connection) -> None:
    rc, _, err = run_psql_capture(admin_url, "SELECT 1", timeout=15)
    if rc != 0:
        raise QuarryError(
            f"cannot reach the local postgres behind [{conn.key}] "
            f"({err.strip() or 'connection failed'}) — is the container "
            "running? try `qy local up`",
            exit_code=EXIT_CONNECTION_ERROR,
        )


def database_exists(admin_url: str, dbname: str) -> bool:
    rc, out, err = run_psql_capture(
        admin_url, f"SELECT 1 FROM pg_database WHERE datname = '{dbname}'", timeout=15)
    if rc != 0:
        raise QuarryError(
            f"failed to list databases: {err.strip()}", exit_code=EXIT_SQL_ERROR)
    return out.strip() == "1"


def current_database_name(url: str) -> str:
    rc, out, err = run_psql_capture(url, "SELECT current_database()", timeout=15)
    if rc != 0:
        raise QuarryError(
            f"failed to read database name: {err.strip()}",
            exit_code=EXIT_CONNECTION_ERROR,
        )
    name = out.strip()
    if not name:
        raise QuarryError(
            "failed to read database name: empty result",
            exit_code=EXIT_CONNECTION_ERROR,
        )
    return name


# ---------------------------------------------------------------------------
# dump / sanitize / apply
# ---------------------------------------------------------------------------

def run_pg_dump_schema(url: str, *, dump_bin: str | None = None) -> str:
    bin_path = dump_bin or resolve_pg_dump()
    cmd = [bin_path, "--schema-only", "--no-owner", "--no-privileges", url]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        raise QuarryError("pg_dump timed out", exit_code=EXIT_CONNECTION_ERROR)
    if proc.returncode != 0:
        raise QuarryError(
            f"pg_dump failed: {proc.stderr.strip() or proc.stdout.strip()}",
            exit_code=EXIT_SQL_ERROR,
        )
    return proc.stdout


def _db_name_pattern(db: str) -> str:
    """Match a database identifier as pg_dump may quote it."""
    return rf'(?:"{re.escape(db)}"|{re.escape(db)})'


def sanitize_schema_dump(dump: str, *, source_db: str, target_db: str) -> str:
    """Rewrite source-database references and drop connect/create-database lines."""
    if source_db != target_db:
        ref = _db_name_pattern(source_db)
        dump = re.sub(
            rf"COMMENT ON DATABASE {ref}",
            f'COMMENT ON DATABASE "{target_db}"',
            dump,
            flags=re.IGNORECASE,
        )
        dump = re.sub(
            rf"ALTER DATABASE {ref}",
            f'ALTER DATABASE "{target_db}"',
            dump,
            flags=re.IGNORECASE,
        )
    kept: list[str] = []
    for line in dump.splitlines():
        stripped = line.strip()
        if stripped.startswith("\\connect"):
            continue
        # pg_dump ≥ 17.6/16.10 emits \restrict/\unrestrict guard meta-commands
        # that only equally-new psql understands; the apply-side psql may be
        # older, and the guards protect against hostile object names we don't
        # face (the source is the user's own database) — strip them.
        if stripped.startswith("\\restrict") or stripped.startswith("\\unrestrict"):
            continue
        # New clients emit this harmless session setting even when dumping
        # older servers. PG <17 cannot parse it; it is not schema content.
        if re.fullmatch(r"SET\s+transaction_timeout\s*=\s*0;", stripped, re.IGNORECASE):
            continue
        if re.match(r"CREATE\s+DATABASE\b", stripped, re.IGNORECASE):
            continue
        kept.append(line)
    out = "\n".join(kept)
    if dump.endswith("\n"):
        out += "\n"
    return out


_CREATE_PUBLIC_RE = re.compile(
    r'^\s*CREATE\s+SCHEMA\s+(?:IF\s+NOT\s+EXISTS\s+)?(?:"public"|public)\s*;',
    re.IGNORECASE | re.MULTILINE,
)


def source_has_public_schema(source_url: str) -> bool:
    rc, out, err = run_psql_capture(
        source_url, "SELECT 1 FROM pg_namespace WHERE nspname = 'public'", timeout=15)
    if rc != 0:
        raise QuarryError(
            f"failed to inspect source schemas: {err.strip()}",
            exit_code=EXIT_SQL_ERROR,
        )
    return out.strip() == "1"


def align_public_schema(staging_url: str, dump: str, source_url: str) -> None:
    """A fresh CREATE DATABASE ships a `public` schema; the dump replicates the
    source truthfully only if we reconcile the two: drop staging's default
    public when the dump recreates it (name collision) or when the source
    genuinely has no public schema (would be an invented extra)."""
    if not _CREATE_PUBLIC_RE.search(dump) and source_has_public_schema(source_url):
        return
    rc, _, err = run_psql_capture(
        staging_url, "DROP SCHEMA IF EXISTS public CASCADE", timeout=30)
    if rc != 0:
        raise QuarryError(
            f"failed to prepare staging database: {err.strip()}",
            exit_code=EXIT_SQL_ERROR,
        )


def apply_schema_dump(url: str, dump: str) -> None:
    rc, _, err = run_psql_capture(url, dump, timeout=120)
    if rc != 0:
        raise QuarryError(
            f"failed to apply schema dump: {err.strip()}",
            exit_code=EXIT_SQL_ERROR,
        )


# ---------------------------------------------------------------------------
# staging lifecycle + swap
# ---------------------------------------------------------------------------

def drop_database(admin_url: str, dbname: str) -> None:
    """Force-drop (terminates sessions); PG13+ — the local containers are 16."""
    rc, _, err = run_psql_capture(
        admin_url, f'DROP DATABASE IF EXISTS "{dbname}" WITH (FORCE);', timeout=60)
    if rc != 0:
        raise QuarryError(
            f"failed to drop database {dbname}: {err.strip()}",
            exit_code=EXIT_SQL_ERROR,
        )


def prepare_staging(admin_url: str, staging: str) -> None:
    """Fresh staging database (clearing any leftover from an interrupted run)."""
    rc, _, err = run_psql_capture(
        admin_url,
        f'DROP DATABASE IF EXISTS "{staging}" WITH (FORCE);\n'
        f'CREATE DATABASE "{staging}";',
        timeout=60,
    )
    if rc != 0:
        raise QuarryError(
            f"failed to create staging database {staging}: {err.strip()}",
            exit_code=EXIT_SQL_ERROR,
        )


def swap_script(db: str, *, staging: str, prev: str, main_exists: bool) -> str:
    """One psql script so the terminate→drop→rename window stays minimal.
    Sessions on the live db are terminated (a dev server holding its pool must
    not block the sync); the previous backup gives way to the new one."""
    stmts = [
        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity\n"
        f"WHERE datname IN ('{db}', '{prev}') AND pid <> pg_backend_pid();",
        f'DROP DATABASE IF EXISTS "{prev}" WITH (FORCE);',
    ]
    if main_exists:
        stmts.append(f'ALTER DATABASE "{db}" RENAME TO "{prev}";')
    stmts.append(f'ALTER DATABASE "{staging}" RENAME TO "{db}";')
    return "\n".join(stmts) + "\n"


def swap_databases(admin_url: str, db: str, *, staging: str, prev: str,
                   main_exists: bool) -> None:
    script = swap_script(db, staging=staging, prev=prev, main_exists=main_exists)
    rc, _, err = run_psql_capture(admin_url, script, timeout=60)
    if rc != 0:
        raise QuarryError(
            f"failed to swap staging database into place: {err.strip()}",
            exit_code=EXIT_SQL_ERROR,
        )


# ---------------------------------------------------------------------------
# programmatic parity check (used by the integration tests)
# ---------------------------------------------------------------------------

SCHEMA_COLUMNS_SQL = """
SELECT table_schema, table_name, column_name,
       data_type, character_maximum_length, numeric_precision,
       numeric_scale, is_nullable, udt_name
FROM information_schema.columns
WHERE table_schema NOT IN ('pg_catalog', 'information_schema')
  AND table_name NOT LIKE 'pg_%'
ORDER BY table_schema, table_name, ordinal_position;
"""


def fetch_schema_columns(url: str) -> list[tuple[str, ...]]:
    rc, out, err = run_psql_capture(url, SCHEMA_COLUMNS_SQL, timeout=30)
    if rc != 0:
        raise QuarryError(
            f"failed to read information_schema.columns: {err.strip()}",
            exit_code=EXIT_SQL_ERROR,
        )
    rows: list[tuple[str, ...]] = []
    for line in out.splitlines():
        if not line.strip():
            continue
        rows.append(tuple(part.strip() for part in line.split("|")))
    return rows


def assert_schemas_match(source_url: str, target_url: str) -> None:
    """Programmatic schema equality check via information_schema.columns."""
    src = fetch_schema_columns(source_url)
    tgt = fetch_schema_columns(target_url)
    if src != tgt:
        src_tables = sorted({r[1] for r in src})
        tgt_tables = sorted({r[1] for r in tgt})
        raise AssertionError(
            f"schema mismatch: source tables {src_tables!r} vs target {tgt_tables!r} "
            f"({len(src)} source columns vs {len(tgt)} target columns)"
        )


# ---------------------------------------------------------------------------
# orchestration
# ---------------------------------------------------------------------------

def sync_schema(
    key: str,
    *,
    from_env: str = "dev",
    dump_bin: str | None = None,
) -> dict:
    """Copy schema from `key`@from_env into `key`@local via a staging database
    and a rename swap. Returns {'db': ..., 'prev': ...} where 'prev' is the name
    the previous copy was kept under (None on a first sync). Raises QuarryError
    on failure; the live database is only ever replaced atomically."""
    source = core.resolve_connection(key, from_env)
    target = core.resolve_connection(key, local.LOCAL_ENV)
    _require_local_target(target)
    _require_postgres(source, "source")
    _require_postgres(target, "target")

    db = target_db_name(target.url)
    staging = f"{db}{STAGING_SUFFIX}"
    prev = f"{db}{PREV_SUFFIX}"
    admin_url = _admin_url(target.url)
    staging_url = _replace_db(target.url, staging)

    with tunnel.open_tunnel(source, "postgres") as source_url:
        server_major = server_pg_major_version(source_url)
        dump_bin = select_pg_dump(server_major, dump_bin=dump_bin)
        source_db = current_database_name(source_url)
        dump = run_pg_dump_schema(source_url, dump_bin=dump_bin)
        dump = sanitize_schema_dump(dump, source_db=source_db, target_db=staging)

        check_local_reachable(admin_url, target)
        prepare_staging(admin_url, staging)
        try:
            align_public_schema(staging_url, dump, source_url)
            apply_schema_dump(staging_url, dump)
        except BaseException:
            try:
                drop_database(admin_url, staging)
            except QuarryError:
                pass  # the original failure matters more than cleanup
            raise

    main_exists = database_exists(admin_url, db)
    swap_databases(admin_url, db, staging=staging, prev=prev, main_exists=main_exists)
    return {"db": db, "prev": prev if main_exists else None}
