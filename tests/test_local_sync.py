"""Unit tests for `qy local sync` — version checks, safety gates, swap plumbing.

Real schema copy + information_schema assertions live in test_local_sync_docker.py.
"""

from __future__ import annotations

import argparse
from dataclasses import replace

import pytest

from quarry import cli, core, local, local_sync


# ---------------------------------------------------------------------------
# version parsing / compatibility
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text,major", [
    ("pg_dump (PostgreSQL) 16.2", 16),
    ("PostgreSQL 13.15", 13),
    ("server_version\n15.4", 15),
])
def test_parse_pg_major(text, major):
    assert local_sync.parse_pg_major(text) == major


def test_parse_pg_major_invalid():
    with pytest.raises(core.QuarryError) as ei:
        local_sync.parse_pg_major("not a version")
    assert ei.value.exit_code == core.EXIT_CONNECTION_ERROR


def test_select_pg_dump_explicit_bin_ok(monkeypatch):
    monkeypatch.setattr(local_sync, "pg_dump_major_version", lambda *a, **kw: 16)
    assert local_sync.select_pg_dump(16, dump_bin="/x/pg_dump") == "/x/pg_dump"


def test_select_pg_dump_explicit_bin_too_old(monkeypatch):
    monkeypatch.setattr(local_sync, "pg_dump_major_version", lambda *a, **kw: 13)
    with pytest.raises(core.QuarryError) as ei:
        local_sync.select_pg_dump(16, dump_bin="/x/pg_dump")
    assert ei.value.exit_code == core.EXIT_CONNECTION_ERROR
    assert "pg_dump client is PostgreSQL 13" in str(ei.value)
    assert "server is 16" in str(ei.value)


def test_select_pg_dump_scans_candidates_for_a_compatible_one(monkeypatch):
    """A too-old PATH pg_dump must not fail the sync when a newer keg exists —
    no QUARRY_PSQL / PATH surgery required of the user."""
    versions = {"pg_dump": 13, "/opt/homebrew/opt/postgresql@17/bin/pg_dump": 17}
    monkeypatch.setattr(local_sync, "pg_dump_candidates", lambda: list(versions))
    monkeypatch.setattr(local_sync, "pg_dump_major_version", lambda b=None: versions[b])
    assert local_sync.select_pg_dump(17) == "/opt/homebrew/opt/postgresql@17/bin/pg_dump"
    assert local_sync.select_pg_dump(13) == "pg_dump"   # first candidate wins when enough


def test_select_pg_dump_reports_best_available_when_all_too_old(monkeypatch):
    versions = {"pg_dump": 13, "/opt/homebrew/opt/postgresql@14/bin/pg_dump": 14}
    monkeypatch.setattr(local_sync, "pg_dump_candidates", lambda: list(versions))
    monkeypatch.setattr(local_sync, "pg_dump_major_version", lambda b=None: versions[b])
    with pytest.raises(core.QuarryError) as ei:
        local_sync.select_pg_dump(17)
    assert "PostgreSQL 14" in str(ei.value) and "postgresql@17" in str(ei.value)


def test_select_pg_dump_no_candidates(monkeypatch):
    monkeypatch.setattr(local_sync, "pg_dump_candidates", lambda: [])
    with pytest.raises(core.QuarryError) as ei:
        local_sync.select_pg_dump(16)
    assert "pg_dump not found" in str(ei.value)


def test_keg_major_sorting():
    paths = ["/opt/homebrew/opt/postgresql@13/bin/pg_dump",
             "/opt/homebrew/opt/postgresql@17/bin/pg_dump",
             "/opt/homebrew/opt/postgresql@14/bin/pg_dump"]
    assert "@17" in sorted(paths, key=local_sync._keg_major, reverse=True)[0]


def test_pg_dump_major_version_from_mocked_subprocess(monkeypatch):
    class Proc:
        returncode = 0
        stdout = "pg_dump (PostgreSQL) 14.1"
        stderr = ""

    monkeypatch.setattr(local_sync.subprocess, "run", lambda *a, **k: Proc())
    assert local_sync.pg_dump_major_version("/usr/bin/pg_dump") == 14


def test_server_pg_major_version(monkeypatch):
    monkeypatch.setattr(
        local_sync, "run_psql_capture",
        lambda url, sql, **kw: (0, "16.4", ""),
    )
    assert local_sync.server_pg_major_version("postgresql://x/db") == 16


# ---------------------------------------------------------------------------
# safety gates — env AND loopback host AND no tunnel; no override for any
# ---------------------------------------------------------------------------

def _conn(**kw):
    base = core.Connection(key="shop", url="postgresql://h/db", env="dev", engine="postgres")
    return replace(base, **kw)


def _local_conn(**kw):
    base = _conn(key="shop_local", env="local",
                 url="postgresql://quarry:quarry@localhost:5433/shop")
    return replace(base, **kw)


def test_require_local_target_accepts_local_loopback():
    local_sync._require_local_target(_local_conn())
    local_sync._require_local_target(_local_conn(url="postgresql://127.0.0.1:5433/shop"))
    local_sync._require_local_target(_local_conn(url="postgresql://[::1]:5433/shop"))


def test_require_local_target_rejects_dev():
    with pytest.raises(core.QuarryError) as ei:
        local_sync._require_local_target(_conn(env="dev"))
    assert ei.value.exit_code == core.EXIT_SYNC_DENIED
    assert "sync refused" in str(ei.value)


def test_require_local_target_rejects_prod():
    with pytest.raises(core.QuarryError) as ei:
        local_sync._require_local_target(_conn(key="shop_prod", env="prod"))
    assert ei.value.exit_code == core.EXIT_SYNC_DENIED


def test_require_local_target_rejects_remote_host_despite_local_env():
    # env=local is just a connections.toml field — a hand-edited entry pointing
    # at a remote host must not pass the gate.
    with pytest.raises(core.QuarryError) as ei:
        local_sync._require_local_target(
            _local_conn(url="postgresql://db.internal.example.com:5432/shop"))
    assert ei.value.exit_code == core.EXIT_SYNC_DENIED
    assert "loopback" in str(ei.value)


def test_require_local_target_rejects_ssh_tunnel():
    # localhost + ssh_host is a tunnel to some remote machine's loopback.
    with pytest.raises(core.QuarryError) as ei:
        local_sync._require_local_target(_local_conn(ssh_host="bastion.example.com"))
    assert ei.value.exit_code == core.EXIT_SYNC_DENIED
    assert "tunnel" in str(ei.value).lower()


def test_sync_schema_rejects_non_local_target(monkeypatch):
    def resolve(name, env=None):
        # env-set has no local member — resolve falls back to the dev connection
        return _conn(key="only_dev", env="dev")

    monkeypatch.setattr(core, "resolve_connection", resolve)
    with pytest.raises(core.QuarryError) as ei:
        local_sync.sync_schema("only_dev", from_env="dev")
    assert ei.value.exit_code == core.EXIT_SYNC_DENIED


def test_sync_schema_rejects_non_postgres_source(monkeypatch):
    def resolve(name, env=None):
        if env == local.LOCAL_ENV:
            return _local_conn(key="cache_local", engine="postgres")
        return _conn(key="cache", env="dev", engine="redis",
                     url="redis://dev-host:6379/1")
    monkeypatch.setattr(core, "resolve_connection", resolve)
    with pytest.raises(core.QuarryError) as ei:
        local_sync.sync_schema("cache", from_env="dev")
    assert ei.value.exit_code == core.EXIT_USAGE
    assert "postgres" in str(ei.value)


# ---------------------------------------------------------------------------
# URL / database-name plumbing
# ---------------------------------------------------------------------------

def test_target_db_name():
    assert local_sync.target_db_name("postgresql://localhost:5433/shop") == "shop"


def test_target_db_name_missing():
    with pytest.raises(core.QuarryError) as ei:
        local_sync.target_db_name("postgresql://localhost:5433")
    assert ei.value.exit_code == core.EXIT_USAGE


def test_target_db_name_unsafe():
    with pytest.raises(core.QuarryError) as ei:
        local_sync.target_db_name("postgresql://localhost:5433/shop;drop")
    assert ei.value.exit_code == core.EXIT_USAGE


def test_target_db_name_too_long():
    long = "a" * 60
    with pytest.raises(core.QuarryError) as ei:
        local_sync.target_db_name(f"postgresql://localhost:5433/{long}")
    assert ei.value.exit_code == core.EXIT_USAGE
    assert "too long" in str(ei.value)


def test_replace_db_and_admin_url():
    url = "postgresql://quarry:quarry@localhost:5433/shop?sslmode=disable"
    assert local_sync._replace_db(url, "shop__staging") == (
        "postgresql://quarry:quarry@localhost:5433/shop__staging?sslmode=disable")
    assert local_sync._admin_url(url) == (
        "postgresql://quarry:quarry@localhost:5433/postgres?sslmode=disable")


def test_database_exists(monkeypatch):
    monkeypatch.setattr(local_sync, "run_psql_capture", lambda *a, **k: (0, "1\n", ""))
    assert local_sync.database_exists("postgresql://localhost/postgres", "shop") is True
    monkeypatch.setattr(local_sync, "run_psql_capture", lambda *a, **k: (0, "", ""))
    assert local_sync.database_exists("postgresql://localhost/postgres", "shop") is False


def test_check_local_reachable_error(monkeypatch):
    monkeypatch.setattr(
        local_sync, "run_psql_capture", lambda *a, **k: (2, "", "connection refused"))
    with pytest.raises(core.QuarryError) as ei:
        local_sync.check_local_reachable("postgresql://localhost/postgres", _local_conn())
    assert ei.value.exit_code == core.EXIT_CONNECTION_ERROR
    assert "qy local up" in str(ei.value)


# ---------------------------------------------------------------------------
# staging + swap
# ---------------------------------------------------------------------------

def test_swap_script_with_existing_main():
    script = local_sync.swap_script(
        "shop", staging="shop__staging", prev="shop__prev", main_exists=True)
    assert "pg_terminate_backend" in script
    assert "'shop', 'shop__prev'" in script
    assert 'DROP DATABASE IF EXISTS "shop__prev" WITH (FORCE);' in script
    assert 'ALTER DATABASE "shop" RENAME TO "shop__prev";' in script
    assert 'ALTER DATABASE "shop__staging" RENAME TO "shop";' in script
    # drop-prev must come before the renames
    assert script.index("DROP DATABASE") < script.index('RENAME TO "shop__prev"')


def test_swap_script_first_sync_has_no_backup_rename():
    script = local_sync.swap_script(
        "shop", staging="shop__staging", prev="shop__prev", main_exists=False)
    assert 'ALTER DATABASE "shop" RENAME' not in script
    assert 'ALTER DATABASE "shop__staging" RENAME TO "shop";' in script


def test_prepare_staging_sql(monkeypatch):
    captured: dict = {}

    def fake_psql(url, sql, **kw):
        captured["url"] = url
        captured["sql"] = sql
        return 0, "", ""

    monkeypatch.setattr(local_sync, "run_psql_capture", fake_psql)
    local_sync.prepare_staging("postgresql://localhost/postgres", "shop__staging")
    assert captured["url"] == "postgresql://localhost/postgres"
    assert 'DROP DATABASE IF EXISTS "shop__staging" WITH (FORCE);' in captured["sql"]
    assert 'CREATE DATABASE "shop__staging";' in captured["sql"]


def test_drop_database_failure(monkeypatch):
    monkeypatch.setattr(local_sync, "run_psql_capture", lambda *a, **k: (3, "", "boom"))
    with pytest.raises(core.QuarryError) as ei:
        local_sync.drop_database("postgresql://localhost/postgres", "shop__staging")
    assert ei.value.exit_code == core.EXIT_SQL_ERROR


def test_align_public_schema_keeps_default_public(monkeypatch):
    # the common case: dump has no CREATE SCHEMA public, source has public →
    # staging's default public stays; nothing is executed against staging.
    monkeypatch.setattr(local_sync, "source_has_public_schema", lambda url: True)
    monkeypatch.setattr(
        local_sync, "run_psql_capture",
        lambda *a, **k: pytest.fail("should not touch staging"))
    local_sync.align_public_schema(
        "postgresql://localhost/shop__staging", "CREATE TABLE t(id int);", "src")


def test_align_public_schema_drops_when_dump_recreates_it(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(
        local_sync, "run_psql_capture",
        lambda url, sql, **k: (calls.append(sql), (0, "", ""))[1])
    local_sync.align_public_schema(
        "postgresql://localhost/shop__staging",
        "CREATE SCHEMA public;\nCREATE TABLE public.t(id int);", "src")
    assert calls == ["DROP SCHEMA IF EXISTS public CASCADE"]


def test_align_public_schema_drops_when_source_has_no_public(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(local_sync, "source_has_public_schema", lambda url: False)
    monkeypatch.setattr(
        local_sync, "run_psql_capture",
        lambda url, sql, **k: (calls.append(sql), (0, "", ""))[1])
    local_sync.align_public_schema(
        "postgresql://localhost/shop__staging", "CREATE TABLE t(id int);", "src")
    assert calls == ["DROP SCHEMA IF EXISTS public CASCADE"]


# ---------------------------------------------------------------------------
# orchestration seams (mocked subprocess / tunnel)
# ---------------------------------------------------------------------------

@pytest.fixture()
def local_ws(tmp_path):
    from quarry import workspace

    (tmp_path / "connections.toml").write_text(
        '[shop_dev]\nurl = "postgresql://dev-host/shop"\nengine = "postgres"\n'
        'env = "dev"\ndb = "shop"\n\n'
        '[shop_local]\nurl = "postgresql://localhost:5433/shop"\nengine = "postgres"\n'
        'env = "local"\ndb = "shop"\n',
        encoding="utf-8",
    )
    (tmp_path / "queries").mkdir()
    workspace.configure_workspace(str(tmp_path))
    yield tmp_path
    workspace.configure_workspace(None)


def _mock_pipeline(monkeypatch, calls, *, apply_fails=False, main_exists=True):
    monkeypatch.setattr(local_sync, "server_pg_major_version", lambda url: 16)
    monkeypatch.setattr(local_sync, "select_pg_dump", lambda major, dump_bin=None: "/mock/pg_dump")
    monkeypatch.setattr(local_sync, "current_database_name", lambda url: "shop")
    monkeypatch.setattr(
        local_sync, "run_pg_dump_schema",
        lambda url, **kw: calls.append(f"dump:{url}") or "CREATE TABLE t(id int);")
    monkeypatch.setattr(
        local_sync, "sanitize_schema_dump",
        lambda dump, *, source_db, target_db:
            calls.append(f"sanitize:{source_db}->{target_db}") or dump)
    monkeypatch.setattr(
        local_sync, "check_local_reachable",
        lambda url, conn: calls.append(f"reach:{url}"))
    monkeypatch.setattr(
        local_sync, "prepare_staging",
        lambda url, staging: calls.append(f"staging:{staging}"))
    monkeypatch.setattr(
        local_sync, "align_public_schema",
        lambda url, dump, src: calls.append(f"align:{url}"))

    def fake_apply(url, dump):
        calls.append(f"apply:{url}")
        if apply_fails:
            raise core.QuarryError("failed to apply schema dump: boom",
                                   exit_code=core.EXIT_SQL_ERROR)

    monkeypatch.setattr(local_sync, "apply_schema_dump", fake_apply)
    monkeypatch.setattr(
        local_sync, "database_exists",
        lambda url, db: calls.append(f"exists:{db}") or main_exists)
    monkeypatch.setattr(
        local_sync, "swap_databases",
        lambda url, db, *, staging, prev, main_exists:
            calls.append(f"swap:{db}<-{staging} prev={prev} main={main_exists}"))
    monkeypatch.setattr(
        local_sync, "drop_database",
        lambda url, db: calls.append(f"drop:{db}"))


def test_sync_schema_orchestration(monkeypatch, local_ws):
    calls: list[str] = []
    _mock_pipeline(monkeypatch, calls)
    res = local_sync.sync_schema("shop", from_env="dev")
    assert calls == [
        "dump:postgresql://dev-host/shop",
        "sanitize:shop->shop__staging",
        "reach:postgresql://localhost:5433/postgres",
        "staging:shop__staging",
        "align:postgresql://localhost:5433/shop__staging",
        "apply:postgresql://localhost:5433/shop__staging",
        "exists:shop",
        "swap:shop<-shop__staging prev=shop__prev main=True",
    ]
    assert res == {"db": "shop", "prev": "shop__prev"}


def test_sync_schema_first_sync_reports_no_prev(monkeypatch, local_ws):
    calls: list[str] = []
    _mock_pipeline(monkeypatch, calls, main_exists=False)
    res = local_sync.sync_schema("shop", from_env="dev")
    assert res == {"db": "shop", "prev": None}
    assert "swap:shop<-shop__staging prev=shop__prev main=False" in calls


def test_sync_schema_apply_failure_drops_staging_and_never_swaps(monkeypatch, local_ws):
    calls: list[str] = []
    _mock_pipeline(monkeypatch, calls, apply_fails=True)
    with pytest.raises(core.QuarryError) as ei:
        local_sync.sync_schema("shop", from_env="dev")
    assert ei.value.exit_code == core.EXIT_SQL_ERROR
    assert "drop:shop__staging" in calls
    assert not any(c.startswith("swap:") for c in calls)


def test_run_pg_dump_schema_invocation(monkeypatch):
    captured: dict = {}

    class Proc:
        returncode = 0
        stdout = "-- schema"
        stderr = ""

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        return Proc()

    monkeypatch.setattr(local_sync.subprocess, "run", fake_run)
    out = local_sync.run_pg_dump_schema("postgresql://u/pw@h/db", dump_bin="/bin/pg_dump")
    assert out == "-- schema"
    assert captured["cmd"] == [
        "/bin/pg_dump", "--schema-only", "--no-owner", "--no-privileges",
        "postgresql://u/pw@h/db",
    ]


def test_run_pg_dump_schema_failure(monkeypatch):
    class Proc:
        returncode = 1
        stdout = ""
        stderr = "connection refused"

    monkeypatch.setattr(local_sync.subprocess, "run", lambda *a, **k: Proc())
    with pytest.raises(core.QuarryError) as ei:
        local_sync.run_pg_dump_schema("postgresql://h/db", dump_bin="/bin/pg_dump")
    assert ei.value.exit_code == core.EXIT_SQL_ERROR


def test_cmd_local_sync(monkeypatch, capsys):
    monkeypatch.setattr(
        local_sync, "sync_schema",
        lambda key, from_env="dev": {"db": "shop", "prev": "shop__prev"})
    args = argparse.Namespace(key="shop", from_env="dev")
    assert cli.cmd_local_sync(args) == core.EXIT_OK
    out = capsys.readouterr().out
    assert "synced schema" in out
    assert "shop__prev" in out


def test_cmd_local_sync_first_run_no_prev_line(monkeypatch, capsys):
    monkeypatch.setattr(
        local_sync, "sync_schema",
        lambda key, from_env="dev": {"db": "shop", "prev": None})
    args = argparse.Namespace(key="shop", from_env="dev")
    assert cli.cmd_local_sync(args) == core.EXIT_OK
    out = capsys.readouterr().out
    assert "synced schema" in out
    assert "__prev" not in out


def test_fetch_schema_columns_parses_psql_output(monkeypatch):
    body = "public|customers|id|integer|||32|NO|int4\n"
    monkeypatch.setattr(local_sync, "run_psql_capture", lambda *a, **k: (0, body, ""))
    rows = local_sync.fetch_schema_columns("postgresql://h/db")
    assert rows == [("public", "customers", "id", "integer", "", "", "32", "NO", "int4")]


def test_assert_schemas_match_ok(monkeypatch):
    snap = [("public", "t", "id", "integer", "", "", "32", "NO", "int4")]
    monkeypatch.setattr(local_sync, "fetch_schema_columns", lambda url: snap)
    local_sync.assert_schemas_match("postgresql://a", "postgresql://b")


def test_assert_schemas_match_fails(monkeypatch):
    monkeypatch.setattr(
        local_sync, "fetch_schema_columns",
        lambda url: [("public", "a", "id", "integer", "", "", "32", "NO", "int4")]
        if "a" in url else [],
    )
    with pytest.raises(AssertionError, match="schema mismatch"):
        local_sync.assert_schemas_match("postgresql://a", "postgresql://b")


def test_resolve_pg_dump_from_quarry_psql_dir(monkeypatch, tmp_path):
    from quarry import workspace

    bindir = tmp_path / "pg16" / "bin"
    bindir.mkdir(parents=True)
    dump = bindir / "pg_dump"
    dump.write_text("#!/bin/sh\n", encoding="utf-8")
    dump.chmod(0o755)
    psql = bindir / "psql"
    psql.write_text("#!/bin/sh\n", encoding="utf-8")
    psql.chmod(0o755)
    (tmp_path / "connections.toml").write_text("", encoding="utf-8")
    (tmp_path / "queries").mkdir()
    monkeypatch.setenv("QUARRY_PSQL", str(psql))
    workspace.configure_workspace(str(tmp_path))
    try:
        assert local_sync.resolve_pg_dump() == str(dump)
    finally:
        workspace.configure_workspace(None)


def test_sanitize_schema_dump_rewrites_database_name():
    dump = (
        "\\connect remote_db\n"
        "CREATE DATABASE remote_db;\n"
        "COMMENT ON DATABASE remote_db IS 'note';\n"
        "ALTER DATABASE remote_db SET timezone TO 'UTC';\n"
        "CREATE TABLE t(id int);\n"
    )
    out = local_sync.sanitize_schema_dump(
        dump, source_db="remote_db", target_db="local_db",
    )
    assert "\\connect" not in out
    assert "CREATE DATABASE" not in out
    assert 'COMMENT ON DATABASE "local_db"' in out
    assert 'ALTER DATABASE "local_db"' in out
    assert "CREATE TABLE t(id int);" in out


def test_sanitize_schema_dump_noop_when_names_match():
    dump = "CREATE TABLE t(id int);\n"
    assert local_sync.sanitize_schema_dump(
        dump, source_db="same", target_db="same",
    ) == dump


def test_sanitize_schema_dump_strips_restrict_guards():
    """pg_dump ≥ 17.6 emits \\restrict/\\unrestrict; an older apply-side psql
    dies on them ('invalid command \\restrict') — they must not survive."""
    dump = (
        "\\restrict abc123\n"
        "CREATE TABLE t(id int);\n"
        "\\unrestrict abc123\n"
    )
    out = local_sync.sanitize_schema_dump(dump, source_db="a", target_db="b")
    assert "\\restrict" not in out and "\\unrestrict" not in out
    assert "CREATE TABLE t(id int);" in out


def test_current_database_name(monkeypatch):
    monkeypatch.setattr(
        local_sync, "run_psql_capture",
        lambda url, sql, **kw: (0, "mydb\n", ""),
    )
    assert local_sync.current_database_name("postgresql://h/mydb") == "mydb"
