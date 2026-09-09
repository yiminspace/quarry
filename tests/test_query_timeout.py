"""Tests for issue #94: configurable query timeout, connect/execute split,
and the PostgreSQL server-side statement_timeout backstop.

Unit tests mock subprocess/pymysql — no DB required. The two @requires_db
tests exercise the real behavior end to end (fast-fail on an unreachable
host, and a genuine server-side statement_timeout cancellation).
"""

from __future__ import annotations

import contextlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from quarry import core, tunnel  # noqa: E402
from conftest import TEST_DB_URL, requires_db  # noqa: E402


def _pg_conn(**kw) -> core.Connection:
    return core.Connection(key="k", url="postgres://h/d", engine="postgres", **kw)


def _fake_tunnel_capture(captured: dict):
    @contextlib.contextmanager
    def _open(conn, engine, connect_timeout=None, use_proxy=None):
        captured["connect_timeout"] = connect_timeout
        yield conn.url
    return _open


# ===========================================================================
# resolve_timeout — priority chain (pure logic, no DB/mocking needed)
# ===========================================================================

@pytest.mark.unit
def test_resolve_timeout_default_when_nothing_set(monkeypatch):
    monkeypatch.delenv("QUARRY_TIMEOUT", raising=False)
    assert core.resolve_timeout(None, None, default=300) == 300


@pytest.mark.unit
def test_resolve_timeout_conn_setting_wins_over_default(monkeypatch):
    monkeypatch.delenv("QUARRY_TIMEOUT", raising=False)
    conn = _pg_conn(timeout=45)
    assert core.resolve_timeout(conn, None, default=300) == 45


@pytest.mark.unit
def test_resolve_timeout_env_wins_over_conn_setting(monkeypatch):
    monkeypatch.setenv("QUARRY_TIMEOUT", "90")
    conn = _pg_conn(timeout=45)
    assert core.resolve_timeout(conn, None, default=300) == 90


@pytest.mark.unit
def test_resolve_timeout_cli_wins_over_everything(monkeypatch):
    monkeypatch.setenv("QUARRY_TIMEOUT", "90")
    conn = _pg_conn(timeout=45)
    assert core.resolve_timeout(conn, 12, default=300) == 12


@pytest.mark.unit
def test_resolve_timeout_invalid_env_falls_through(monkeypatch):
    monkeypatch.setenv("QUARRY_TIMEOUT", "not-a-number")
    conn = _pg_conn(timeout=45)
    assert core.resolve_timeout(conn, None, default=300) == 45


@pytest.mark.unit
def test_resolve_timeout_nonpositive_env_falls_through(monkeypatch):
    # review r1-3: QUARRY_TIMEOUT=0 must not be accepted at face value — it
    # would collapse PG's statement_timeout to ~1ms and cancel almost every
    # query instead of giving a clear signal that the value is bogus.
    monkeypatch.setenv("QUARRY_TIMEOUT", "0")
    conn = _pg_conn(timeout=45)
    assert core.resolve_timeout(conn, None, default=300) == 45


# ===========================================================================
# load_connections — timeout field validation (review r1-3)
# ===========================================================================

@pytest.mark.unit
def test_load_connections_rejects_nonpositive_timeout(tmp_path):
    from quarry import workspace
    (tmp_path / "connections.toml").write_text(
        '[bad]\nurl = "postgres://h/d"\ntimeout = 0\n', encoding="utf-8")
    try:
        workspace.configure_workspace(str(tmp_path))
        with pytest.raises(core.QuarryError) as ei:
            core.load_connections()
        assert ei.value.exit_code == core.EXIT_USAGE
        assert "timeout" in str(ei.value)
    finally:
        workspace.configure_workspace(None)


# ===========================================================================
# CLI --timeout argument validation (review r1-3)
# ===========================================================================

@pytest.mark.unit
def test_cli_timeout_rejects_zero_and_negative():
    from quarry import cli
    parser = cli.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["exec", "db", "--sql", "select 1", "--timeout", "0"])
    with pytest.raises(SystemExit):
        parser.parse_args(["exec", "db", "--sql", "select 1", "--timeout", "-5"])


@pytest.mark.unit
def test_cli_timeout_accepts_positive():
    from quarry import cli
    parser = cli.build_parser()
    args = parser.parse_args(["exec", "db", "--sql", "select 1", "--timeout", "30"])
    assert args.timeout == 30


# ===========================================================================
# _pg_statement_timeout_prefix
# ===========================================================================

@pytest.mark.unit
def test_pg_statement_timeout_prefix_default():
    assert core._pg_statement_timeout_prefix(300) == "SET statement_timeout = '270000ms';\n"


@pytest.mark.unit
def test_pg_statement_timeout_prefix_small_value():
    assert core._pg_statement_timeout_prefix(10) == "SET statement_timeout = '9000ms';\n"


# ===========================================================================
# _pg_url_with_connect_timeout (review r1-1)
# ===========================================================================

@pytest.mark.unit
def test_pg_url_with_connect_timeout_overrides_existing_value():
    url = core._pg_url_with_connect_timeout("postgres://h/d?connect_timeout=60&sslmode=require", 15)
    assert "connect_timeout=15" in url
    assert "connect_timeout=60" not in url
    assert "sslmode=require" in url  # other query params are preserved


@pytest.mark.unit
def test_pg_url_with_connect_timeout_adds_when_absent():
    url = core._pg_url_with_connect_timeout("postgres://h/d", 15)
    assert "connect_timeout=15" in url


# ===========================================================================
# _psql_error_message
# ===========================================================================

@pytest.mark.unit
def test_psql_error_message_rc2_is_connection_error():
    msg, code = core._psql_error_message(2, "could not connect to server")
    assert code == core.EXIT_CONNECTION_ERROR
    assert "postgres connection failed" in msg


@pytest.mark.unit
def test_psql_error_message_statement_timeout_gets_hint():
    msg, code = core._psql_error_message(3, "ERROR: canceling statement due to statement timeout")
    assert code == core.EXIT_SQL_ERROR
    assert "--timeout" in msg


@pytest.mark.unit
def test_psql_error_message_other_sql_error_no_hint():
    msg, code = core._psql_error_message(3, "ERROR: syntax error at or near \"bogus\"")
    assert code == core.EXIT_SQL_ERROR
    assert "--timeout" not in msg


# ===========================================================================
# run_psql_capture — PGCONNECT_TIMEOUT wiring + timeout-expired hint
# ===========================================================================

@pytest.mark.unit
def test_run_psql_capture_sets_pgconnect_timeout_env(monkeypatch):
    captured = {}

    class _Proc:
        returncode = 0
        stdout = "[]"
        stderr = ""

    def fake_run(cmd, input, capture_output, text, timeout, env):
        captured["timeout"] = timeout
        captured["env"] = env
        return _Proc()

    monkeypatch.setattr(core.subprocess, "run", fake_run)
    core.run_psql_capture("postgres://h/d", "select 1", timeout=315, connect_timeout=15)
    assert captured["timeout"] == 315
    assert captured["env"]["PGCONNECT_TIMEOUT"] == "15"


@pytest.mark.unit
def test_run_psql_capture_overrides_connect_timeout_already_in_url(monkeypatch):
    # review r1-1: libpq's connection-string `connect_timeout` param takes
    # precedence over the PGCONNECT_TIMEOUT env var, so a URL that already
    # carries its own (larger) connect_timeout must have it overridden, not
    # merely shadowed by the env var.
    captured = {}

    class _Proc:
        returncode = 0
        stdout = "[]"
        stderr = ""

    def fake_run(cmd, input, capture_output, text, timeout, env):
        captured["url"] = cmd[1]
        return _Proc()

    monkeypatch.setattr(core.subprocess, "run", fake_run)
    core.run_psql_capture("postgres://h/d?connect_timeout=60", "select 1",
                          timeout=16, connect_timeout=15)
    assert "connect_timeout=15" in captured["url"]
    assert "connect_timeout=60" not in captured["url"]


@pytest.mark.unit
def test_run_psql_capture_no_connect_timeout_leaves_env_unset(monkeypatch):
    captured = {}

    class _Proc:
        returncode = 0
        stdout = "[]"
        stderr = ""

    def fake_run(cmd, input, capture_output, text, timeout, env):
        captured["env"] = env
        return _Proc()

    monkeypatch.setattr(core.subprocess, "run", fake_run)
    core.run_psql_capture("postgres://h/d", "select 1", timeout=15)
    assert captured["env"] is None


@pytest.mark.unit
def test_run_psql_capture_timeout_expired_hints_increase(monkeypatch):
    import subprocess as sp

    def fake_run(*a, **k):
        raise sp.TimeoutExpired(cmd="psql", timeout=5)

    monkeypatch.setattr(core.subprocess, "run", fake_run)
    rc, out, errout = core.run_psql_capture("postgres://h/d", "select pg_sleep(9)", timeout=5)
    assert rc == -1
    assert "--timeout" in errout


# ===========================================================================
# run_query / execute_sql — postgres timeout wiring (mocked psql + tunnel)
# ===========================================================================

@pytest.mark.unit
def test_run_query_postgres_default_timeouts(monkeypatch):
    captured = {}
    monkeypatch.setattr(tunnel, "open_tunnel", _fake_tunnel_capture(captured))

    def fake_capture(url, sql, *, psql_vars=None, timeout=60, connect_timeout=None):
        if "\\gdesc" in sql:
            return (0, "", "")
        captured["timeout"] = timeout
        captured["connect_timeout"] = connect_timeout
        captured["sql"] = sql
        return (0, "[]", "")

    monkeypatch.setattr(core, "run_psql_capture", fake_capture)
    core.run_query(_pg_conn(), "select 1")
    # 15s connect + 300s execute default
    assert captured["timeout"] == 15 + 300
    assert captured["connect_timeout"] == 15
    assert captured["sql"].startswith("SET statement_timeout = '270000ms';\n")


@pytest.mark.unit
def test_run_query_postgres_cli_timeout_overrides_conn_setting(monkeypatch):
    captured = {}
    monkeypatch.setattr(tunnel, "open_tunnel", _fake_tunnel_capture(captured))

    def fake_capture(url, sql, *, psql_vars=None, timeout=60, connect_timeout=None):
        if "\\gdesc" in sql:
            return (0, "", "")
        captured["timeout"] = timeout
        captured["sql"] = sql
        return (0, "[]", "")

    monkeypatch.setattr(core, "run_psql_capture", fake_capture)
    core.run_query(_pg_conn(timeout=45), "select 1", timeout=10)
    assert captured["timeout"] == 15 + 10
    assert captured["sql"].startswith("SET statement_timeout = '9000ms';\n")


@pytest.mark.unit
def test_run_query_mcp_default_timeout_is_120s(monkeypatch):
    captured = {}
    monkeypatch.setattr(tunnel, "open_tunnel", _fake_tunnel_capture(captured))

    def fake_capture(url, sql, *, psql_vars=None, timeout=60, connect_timeout=None):
        if "\\gdesc" in sql:
            return (0, "", "")
        captured["timeout"] = timeout
        return (0, "[]", "")

    monkeypatch.setattr(core, "run_psql_capture", fake_capture)
    core.run_query(_pg_conn(), "select 1", default_timeout=core.MCP_EXECUTE_TIMEOUT_SEC)
    assert captured["timeout"] == 15 + 120


@pytest.mark.unit
def test_execute_sql_postgres_default_timeouts(monkeypatch, capsys):
    captured = {}
    monkeypatch.setattr(tunnel, "open_tunnel", _fake_tunnel_capture(captured))

    def fake_capture(url, sql, *, psql_vars=None, timeout=60, connect_timeout=None):
        if "\\gdesc" in sql:
            return (0, "", "")
        captured["timeout"] = timeout
        captured["sql"] = sql
        return (0, "[]", "")

    monkeypatch.setattr(core, "run_psql_capture", fake_capture)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False, raising=False)
    core.execute_sql(conn=_pg_conn(), sql="select 1", psql_vars={}, fmt="json")
    assert captured["timeout"] == 15 + 300
    assert captured["sql"].startswith("SET statement_timeout = '270000ms';\n")


# ===========================================================================
# MCP wiring — server exec/list/describe/run_saved_query use the 120s default
# ===========================================================================

@pytest.mark.unit
def test_mcp_tool_exec_sql_uses_120s_default(monkeypatch):
    from quarry import mcp
    captured = {}

    def fake_run_query(conn, sql, **kw):
        captured.update(kw)
        return core.QueryResult(engine="postgres", columns=[], rows=[], row_count=0,
                                truncated=False, elapsed_ms=0, sql=sql)

    monkeypatch.setattr(core, "run_query", fake_run_query)
    monkeypatch.setattr(mcp.core, "resolve_connection", lambda db, env=None: _pg_conn())
    mcp.tool_exec_sql("db", "select 1")
    assert captured["default_timeout"] == core.MCP_EXECUTE_TIMEOUT_SEC


# ===========================================================================
# DB-backed: real connect/execute split + real statement_timeout cancellation
# ===========================================================================

@requires_db
def test_connect_timeout_independent_of_execute_timeout():
    import time as _time
    # An address that silently drops packets (TEST-NET-1, RFC 5737) so the
    # connect phase actually blocks until PGCONNECT_TIMEOUT trips — proving a
    # short connect_timeout fails fast without waiting out a large execute budget.
    conn = core.Connection(key="k", url="postgresql://192.0.2.1:5432/db", engine="postgres")
    start = _time.monotonic()
    with pytest.raises(core.QuarryError) as ei:
        core.run_query(conn, "select 1", timeout=300, connect_timeout=1)
    elapsed = _time.monotonic() - start
    assert ei.value.exit_code == core.EXIT_CONNECTION_ERROR
    assert elapsed < 10  # nowhere near the 300s execute budget


@requires_db
def test_pg_statement_timeout_cancels_serverside():
    conn = core.Connection(key="testpg", url=TEST_DB_URL, engine="postgres")
    with pytest.raises(core.QuarryError) as ei:
        core.run_query(conn, "select pg_sleep(3)", timeout=1)
    assert ei.value.exit_code == core.EXIT_SQL_ERROR
    msg = str(ei.value).lower()
    assert "statement timeout" in msg
    assert "--timeout" in str(ei.value)
