"""Coverage-closing tests for quarry.cli — the non-Postgres engine branches.

test_cli_integration.py already exercises every command against the real local
Postgres. This file targets the branches that need a *mocked* engine (redis /
mysql / neptune have no live server here) plus a couple of error/abort paths:

  - _connections_test   : redis / mysql / neptune / psql-fail / QuarryError /
                          generic-Exception branches (cli.py 152-185)
  - cmd_describe_table   : mysql json + text render, and the psql text-path
                          timeout / failure handlers (cli.py 304-337)
  - _execute            : the prod-write abort path (cli.py 394)

Everything here is engine-mocked (no live redis/mysql/neptune), so the whole
file is @pytest.mark.unit. tunnel.open_tunnel is replaced with a trivial
contextmanager yielding a dummy URL so no real network/tunnel is touched.

Note on patch targets: cli.py imports run_mysql_query / run_neptune_cypher /
run_psql_capture *by name* from core, so those must be patched on the `cli`
module (patching core.* would not rebind the name the handler calls). redis is
reached via `redis_engine.run_redis` (attribute access), so it's patched on the
redis_engine module. Every test drives through run_cli() so the process-wide
workspace is pointed at the temp `wsdir` (isolating it from the real config.toml
aggregation) exactly as main() does.
"""

from __future__ import annotations

import contextlib
import io
import json

import pytest

from quarry import cli, core, redis_engine, tunnel, workspace
from quarry.core import (
    EXIT_CONNECTION_ERROR,
    EXIT_OK,
    EXIT_SQL_ERROR,
    EXIT_USAGE,
)


# ---------------------------------------------------------------------------
# main()-equivalent dispatcher (mirrors test_cli_integration.run_cli):
# build the real parser, configure the workspace, dispatch to args.func.
# ---------------------------------------------------------------------------

def run_cli(wsdir, *argv):
    args = cli.build_parser().parse_args(["--workspace", str(wsdir), *argv])
    workspace.configure_workspace(args.workspace)
    try:
        return args.func(args)
    except cli.QuarryError as e:
        return e.exit_code
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else 1


def _fake_tunnel(url: str = "engine://dummy/host"):
    """A drop-in for tunnel.open_tunnel: a contextmanager yielding a fixed URL."""
    @contextlib.contextmanager
    def _cm(conn, engine, **kwargs):
        yield url
    return _cm


def _write_conn(wsdir, key: str, url: str, engine: str, extra: str = "") -> None:
    (wsdir / "connections.toml").write_text(
        f'[{key}]\nurl = "{url}"\nengine = "{engine}"\n{extra}', encoding="utf-8")


# ===========================================================================
# _connections_test — engine branches (cli.py 152-185)
# ===========================================================================

@pytest.mark.unit
class TestConnectionsTestEngines:
    def test_redis_ok_reports_key_count(self, wsdir, monkeypatch, capsys):
        _write_conn(wsdir, "cache", "redis://localhost:6379/0", "redis")
        monkeypatch.setattr(tunnel, "open_tunnel", _fake_tunnel())
        calls: list[str] = []

        def fake_run_redis(url, command, *, timeout=30):
            calls.append(command)
            if command == "PING":
                return [{"value": "PONG"}], 0
            return [{"value": "12"}], 0  # DBSIZE

        monkeypatch.setattr(redis_engine, "run_redis", fake_run_redis)
        rc = run_cli(wsdir, "connections", "test", "cache")
        assert rc == EXIT_OK
        out = capsys.readouterr().out
        assert "connected to Redis" in out and "12 keys" in out
        assert calls == ["PING", "DBSIZE"]  # PING first, then DBSIZE

    def test_redis_lowercase_pong_still_ok(self, wsdir, monkeypatch, capsys):
        # the PONG check upper()s the value, so a lowercase 'pong' still passes
        _write_conn(wsdir, "cache", "redis://localhost:6379/0", "redis")
        monkeypatch.setattr(tunnel, "open_tunnel", _fake_tunnel())
        monkeypatch.setattr(
            redis_engine, "run_redis",
            lambda url, command, *, timeout=30: ([{"value": "pong"}], 0) if command == "PING"
            else ([{"value": "7"}], 0))
        assert run_cli(wsdir, "connections", "test", "cache") == EXIT_OK
        assert "7 keys" in capsys.readouterr().out

    def test_redis_dbsize_empty_prints_question_mark(self, wsdir, monkeypatch, capsys):
        # DBSIZE returning [] exercises the `size[0]... if size else '?'` else-branch
        _write_conn(wsdir, "cache", "redis://localhost:6379/0", "redis")
        monkeypatch.setattr(tunnel, "open_tunnel", _fake_tunnel())
        monkeypatch.setattr(
            redis_engine, "run_redis",
            lambda url, command, *, timeout=30: ([{"value": "PONG"}], 0) if command == "PING" else ([], 0))
        rc = run_cli(wsdir, "connections", "test", "cache")
        assert rc == EXIT_OK
        assert "? keys" in capsys.readouterr().out

    def test_redis_no_pong_is_connection_error(self, wsdir, monkeypatch, capsys):
        _write_conn(wsdir, "cache", "redis://localhost:6379/0", "redis")
        monkeypatch.setattr(tunnel, "open_tunnel", _fake_tunnel())
        # PING returns something other than PONG -> failure
        monkeypatch.setattr(
            redis_engine, "run_redis",
            lambda url, command, *, timeout=30: ([{"value": "NOPE"}], 0))
        rc = run_cli(wsdir, "connections", "test", "cache")
        assert rc == EXIT_CONNECTION_ERROR
        assert "no PONG" in capsys.readouterr().err

    def test_redis_empty_ping_rows_is_connection_error(self, wsdir, monkeypatch, capsys):
        # PING returning [] makes `rows and ...` falsy -> the not-ok branch
        _write_conn(wsdir, "cache", "redis://localhost:6379/0", "redis")
        monkeypatch.setattr(tunnel, "open_tunnel", _fake_tunnel())
        monkeypatch.setattr(redis_engine, "run_redis", lambda url, command, *, timeout=30: ([], 0))
        assert run_cli(wsdir, "connections", "test", "cache") == EXIT_CONNECTION_ERROR

    def test_neptune_ok(self, wsdir, monkeypatch, capsys):
        _write_conn(wsdir, "graph", "https://neptune.example.com:8182", "neptune")
        monkeypatch.setattr(tunnel, "open_tunnel", _fake_tunnel())
        seen = {}

        def fake_cypher(url, cypher, *, timeout=30, **kwargs):
            seen["cypher"] = cypher
            return [{"ok": 1}]

        # cli.py calls the name imported into its own namespace
        monkeypatch.setattr(cli, "run_neptune_cypher", fake_cypher)
        rc = run_cli(wsdir, "connections", "test", "graph")
        assert rc == EXIT_OK
        out = capsys.readouterr().out
        assert "connected to Neptune" in out
        assert seen["cypher"] == "RETURN 1 AS ok"

    def test_mysql_ok_prints_db_and_version(self, wsdir, monkeypatch, capsys):
        _write_conn(wsdir, "shop", "mysql://u:p@localhost:3306/shopdb", "mysql")
        monkeypatch.setattr(tunnel, "open_tunnel", _fake_tunnel())
        monkeypatch.setattr(
            cli, "run_mysql_query",
            lambda url, sql, *, timeout=30: ([{"db_name": "shopdb", "version": "8.0.34"}], 0))
        rc = run_cli(wsdir, "connections", "test", "shop")
        assert rc == EXIT_OK
        out = capsys.readouterr().out
        assert "connected to shopdb (mysql)" in out
        assert "8.0.34" in out  # the version sub-line

    def test_mysql_ok_no_version_and_no_rows(self, wsdir, monkeypatch, capsys):
        # empty result -> row={} -> '?' for db_name and the version sub-line is skipped
        _write_conn(wsdir, "shop", "mysql://u:p@localhost:3306/shopdb", "mysql")
        monkeypatch.setattr(tunnel, "open_tunnel", _fake_tunnel())
        monkeypatch.setattr(cli, "run_mysql_query", lambda url, sql, *, timeout=30: ([], 0))
        rc = run_cli(wsdir, "connections", "test", "shop")
        assert rc == EXIT_OK
        out = capsys.readouterr().out
        assert "connected to ? (mysql)" in out
        assert "8.0" not in out  # no version line emitted

    def test_postgres_psql_capture_failure(self, wsdir, monkeypatch, capsys):
        # engine=postgres path: run_psql_capture returns nonzero -> connection error
        _write_conn(wsdir, "pg", "postgresql://localhost:5432/x", "postgres")
        monkeypatch.setattr(tunnel, "open_tunnel", _fake_tunnel())
        monkeypatch.setattr(
            cli, "run_psql_capture",
            lambda url, sql, *, timeout=30: (1, "", "FATAL: nope"))
        rc = run_cli(wsdir, "connections", "test", "pg")
        assert rc == EXIT_CONNECTION_ERROR
        assert "connection test failed" in capsys.readouterr().err

    def test_postgres_psql_capture_ok_single_part(self, wsdir, monkeypatch, capsys):
        # a one-field psql result: parts has length 1 so the version sub-line
        # (len(parts) > 1) is skipped — covers the 177->179 false branch.
        _write_conn(wsdir, "pg", "postgresql://localhost:5432/x", "postgres")
        monkeypatch.setattr(tunnel, "open_tunnel", _fake_tunnel())
        monkeypatch.setattr(
            cli, "run_psql_capture",
            lambda url, sql, *, timeout=30: (0, "mydb", ""))
        rc = run_cli(wsdir, "connections", "test", "pg")
        assert rc == EXIT_OK
        out = capsys.readouterr().out
        assert "connected to mydb" in out

    def test_postgres_psql_capture_ok_with_version(self, wsdir, monkeypatch, capsys):
        # a two-part psql result ('db | version') -> the version sub-line at 178
        # (len(parts) > 1 true branch) is printed.
        _write_conn(wsdir, "pg", "postgresql://localhost:5432/x", "postgres")
        monkeypatch.setattr(tunnel, "open_tunnel", _fake_tunnel())
        monkeypatch.setattr(
            cli, "run_psql_capture",
            lambda url, sql, *, timeout=30: (0, "mydb | PostgreSQL 15.2 on x86_64", ""))
        rc = run_cli(wsdir, "connections", "test", "pg")
        assert rc == EXIT_OK
        out = capsys.readouterr().out
        assert "connected to mydb" in out
        assert "PostgreSQL 15.2" in out  # the version sub-line

    def test_open_tunnel_quarry_error_returns_its_exit_code(self, wsdir, monkeypatch, capsys):
        _write_conn(wsdir, "pg", "postgresql://localhost:5432/x", "postgres")

        @contextlib.contextmanager
        def boom(conn, engine):
            raise core.QuarryError("tunnel down", exit_code=EXIT_USAGE)
            yield  # pragma: no cover

        monkeypatch.setattr(tunnel, "open_tunnel", boom)
        rc = run_cli(wsdir, "connections", "test", "pg")
        assert rc == EXIT_USAGE  # the QuarryError's own exit_code, not the generic one
        assert "connection test failed: tunnel down" in capsys.readouterr().err

    def test_open_tunnel_generic_exception_is_connection_error(self, wsdir, monkeypatch, capsys):
        _write_conn(wsdir, "pg", "postgresql://localhost:5432/x", "postgres")

        @contextlib.contextmanager
        def boom(conn, engine):
            raise RuntimeError("kaboom")
            yield  # pragma: no cover

        monkeypatch.setattr(tunnel, "open_tunnel", boom)
        rc = run_cli(wsdir, "connections", "test", "pg")
        assert rc == EXIT_CONNECTION_ERROR
        assert "connection test failed: kaboom" in capsys.readouterr().err


# ===========================================================================
# ping (issue #110) — engine branches + --all aggregation (cli.py cmd_ping /
# _ping_one). Unlike `connections test`, a failure here is *always* exit 1
# regardless of the underlying engine exit code (EXIT_CONNECTION_ERROR/
# EXIT_SQL_ERROR/etc.) — that's the whole point of a plain reachability probe.
# ===========================================================================

@pytest.mark.unit
class TestPingEngines:
    def test_redis_ok(self, wsdir, monkeypatch, capsys):
        _write_conn(wsdir, "cache", "redis://localhost:6379/0", "redis")
        monkeypatch.setattr(tunnel, "open_tunnel", _fake_tunnel())
        monkeypatch.setattr(
            redis_engine, "run_redis",
            lambda url, command, *, timeout=30: ([{"value": "PONG"}], 0))
        rc = run_cli(wsdir, "ping", "cache")
        assert rc == EXIT_OK
        assert "✓ cache (redis): ok" in capsys.readouterr().out

    def test_redis_no_pong_is_a_failure(self, wsdir, monkeypatch, capsys):
        _write_conn(wsdir, "cache", "redis://localhost:6379/0", "redis")
        monkeypatch.setattr(tunnel, "open_tunnel", _fake_tunnel())
        monkeypatch.setattr(
            redis_engine, "run_redis",
            lambda url, command, *, timeout=30: ([{"value": "NOPE"}], 0))
        rc = run_cli(wsdir, "ping", "cache")
        assert rc == 1
        assert "no PONG" in capsys.readouterr().out

    def test_neptune_ok(self, wsdir, monkeypatch, capsys):
        _write_conn(wsdir, "graph", "https://neptune.example.com:8182", "neptune")
        monkeypatch.setattr(tunnel, "open_tunnel", _fake_tunnel())
        seen = {}

        def fake_cypher(url, cypher, *, timeout=30, **kwargs):
            seen["cypher"] = cypher
            return [{"ok": 1}], 3

        # cli.py calls the name imported into its own namespace
        monkeypatch.setattr(cli, "run_neptune_cypher", fake_cypher)
        rc = run_cli(wsdir, "ping", "graph")
        assert rc == EXIT_OK
        assert seen["cypher"] == "RETURN 1 AS ok"  # reuses `connections test`'s probe, not a new one
        assert "✓ graph (neptune): ok" in capsys.readouterr().out

    def test_mysql_ok_uses_select_1(self, wsdir, monkeypatch, capsys):
        _write_conn(wsdir, "shop", "mysql://u:p@localhost:3306/shopdb", "mysql")
        monkeypatch.setattr(tunnel, "open_tunnel", _fake_tunnel())
        seen = {}

        def fake_query(url, sql, *, timeout=30):
            seen["sql"] = sql
            return [], 0

        monkeypatch.setattr(cli, "run_mysql_query", fake_query)
        rc = run_cli(wsdir, "ping", "shop")
        assert rc == EXIT_OK
        assert seen["sql"] == "SELECT 1"
        assert "✓ shop (mysql): ok" in capsys.readouterr().out

    def test_mysql_connection_failed_exits_1_with_reason(self, wsdir, monkeypatch, capsys):
        _write_conn(wsdir, "shop", "mysql://u:p@localhost:3306/shopdb", "mysql")
        monkeypatch.setattr(tunnel, "open_tunnel", _fake_tunnel())

        def fake_query(url, sql, *, timeout=30):
            raise core.QuarryError("mysql connection failed: nope", exit_code=EXIT_CONNECTION_ERROR)

        monkeypatch.setattr(cli, "run_mysql_query", fake_query)
        rc = run_cli(wsdir, "ping", "shop")
        assert rc == 1  # ping's own 0/1 contract, not the wrapped EXIT_CONNECTION_ERROR
        out = capsys.readouterr().out
        assert "✗ shop (mysql): fail" in out
        assert "mysql connection failed" in out

    def test_postgres_ok_uses_select_1(self, wsdir, monkeypatch, capsys):
        _write_conn(wsdir, "pg", "postgresql://localhost:5432/x", "postgres")
        monkeypatch.setattr(tunnel, "open_tunnel", _fake_tunnel())
        seen = {}

        def fake_capture(url, sql, *, timeout=30):
            seen["sql"] = sql
            return (0, "1", "")

        monkeypatch.setattr(cli, "run_psql_capture", fake_capture)
        rc = run_cli(wsdir, "ping", "pg")
        assert rc == EXIT_OK
        assert seen["sql"] == "SELECT 1"

    def test_postgres_capture_failure_surfaces_reason(self, wsdir, monkeypatch, capsys):
        _write_conn(wsdir, "pg", "postgresql://localhost:5432/x", "postgres")
        monkeypatch.setattr(tunnel, "open_tunnel", _fake_tunnel())
        monkeypatch.setattr(
            cli, "run_psql_capture",
            lambda url, sql, *, timeout=30: (2, "", "FATAL: password authentication failed"))
        rc = run_cli(wsdir, "ping", "pg")
        assert rc == 1
        out = capsys.readouterr().out
        assert "✗ pg (postgres): fail" in out
        assert "password authentication failed" in out

    def test_open_tunnel_generic_exception_is_a_failure_not_a_crash(self, wsdir, monkeypatch, capsys):
        _write_conn(wsdir, "pg", "postgresql://localhost:5432/x", "postgres")

        @contextlib.contextmanager
        def boom(conn, engine, **kwargs):
            raise RuntimeError("kaboom")
            yield  # pragma: no cover

        monkeypatch.setattr(tunnel, "open_tunnel", boom)
        rc = run_cli(wsdir, "ping", "pg")
        assert rc == 1
        assert "kaboom" in capsys.readouterr().out

    def test_all_aggregates_mixed_results_and_exits_1(self, wsdir, monkeypatch, capsys):
        (wsdir / "connections.toml").write_text(
            '[good]\nurl = "postgresql://localhost:5432/x"\nengine = "postgres"\n'
            '[bad]\nurl = "postgresql://localhost:5432/y"\nengine = "postgres"\n',
            encoding="utf-8")
        monkeypatch.setattr(tunnel, "open_tunnel", _fake_tunnel())
        calls = {"n": 0}

        def fake_capture(url, sql, *, timeout=30):
            calls["n"] += 1
            return (0, "1", "") if calls["n"] == 1 else (2, "", "connection refused")

        monkeypatch.setattr(cli, "run_psql_capture", fake_capture)
        rc = run_cli(wsdir, "ping", "--all")
        assert rc == 1
        out = capsys.readouterr().out
        assert "✓ good" in out
        assert "✗ bad" in out
        assert "1/2 reachable" in out

    def test_all_with_no_configured_connections(self, wsdir, capsys):
        (wsdir / "connections.toml").write_text("", encoding="utf-8")
        rc = run_cli(wsdir, "ping", "--all")
        assert rc == EXIT_OK
        assert "no configured connections" in capsys.readouterr().out

    def test_json_format_single_connection_is_a_dict(self, wsdir, monkeypatch, capsys):
        _write_conn(wsdir, "pg", "postgresql://localhost:5432/x", "postgres")
        monkeypatch.setattr(tunnel, "open_tunnel", _fake_tunnel())
        monkeypatch.setattr(cli, "run_psql_capture", lambda url, sql, *, timeout=30: (0, "1", ""))
        rc = run_cli(wsdir, "ping", "pg", "--format", "json")
        assert rc == EXIT_OK
        obj = json.loads(capsys.readouterr().out)
        assert obj["key"] == "pg" and obj["ok"] is True and obj["error"] is None

    def test_json_format_all_is_a_list(self, wsdir, monkeypatch, capsys):
        _write_conn(wsdir, "pg", "postgresql://localhost:5432/x", "postgres")
        monkeypatch.setattr(tunnel, "open_tunnel", _fake_tunnel())
        monkeypatch.setattr(cli, "run_psql_capture", lambda url, sql, *, timeout=30: (0, "1", ""))
        rc = run_cli(wsdir, "ping", "--all", "--format", "json")
        assert rc == EXIT_OK
        obj = json.loads(capsys.readouterr().out)
        assert isinstance(obj, list) and obj[0]["key"] == "pg"

    def test_timeout_flag_reaches_the_engine_probe(self, wsdir, monkeypatch, capsys):
        _write_conn(wsdir, "pg", "postgresql://localhost:5432/x", "postgres")
        monkeypatch.setattr(tunnel, "open_tunnel", _fake_tunnel())
        seen = {}

        def fake_capture(url, sql, *, timeout=30):
            seen["timeout"] = timeout
            return (0, "1", "")

        monkeypatch.setattr(cli, "run_psql_capture", fake_capture)
        rc = run_cli(wsdir, "ping", "pg", "--timeout", "3")
        assert rc == EXIT_OK
        assert seen["timeout"] == 3

    def test_default_timeout_used_when_flag_omitted(self, wsdir, monkeypatch, capsys):
        _write_conn(wsdir, "pg", "postgresql://localhost:5432/x", "postgres")
        monkeypatch.setattr(tunnel, "open_tunnel", _fake_tunnel())
        seen = {}

        def fake_capture(url, sql, *, timeout=30):
            seen["timeout"] = timeout
            return (0, "1", "")

        monkeypatch.setattr(cli, "run_psql_capture", fake_capture)
        rc = run_cli(wsdir, "ping", "pg")
        assert rc == EXIT_OK
        assert seen["timeout"] == core.DEFAULT_PING_TIMEOUT_SEC

    def test_rejects_connection_and_all_together(self, wsdir):
        _write_conn(wsdir, "pg", "postgresql://localhost:5432/x", "postgres")
        assert run_cli(wsdir, "ping", "pg", "--all") == EXIT_USAGE

    def test_requires_connection_or_all(self, wsdir):
        _write_conn(wsdir, "pg", "postgresql://localhost:5432/x", "postgres")
        assert run_cli(wsdir, "ping") == EXIT_USAGE


# ===========================================================================
# cmd_describe_table — mysql render + psql text-path error handlers
# (cli.py 304-337)
# ===========================================================================

_MYSQL_COLS = [
    {"column_name": "id", "data_type": "int", "is_nullable": "NO", "column_default": None},
    {"column_name": "name", "data_type": "varchar", "is_nullable": "YES", "column_default": "''"},
]


@pytest.mark.unit
class TestDescribeTableMysql:
    def test_mysql_text_render(self, wsdir, monkeypatch, capsys):
        _write_conn(wsdir, "shop", "mysql://u:p@localhost:3306/shopdb", "mysql")
        monkeypatch.setattr(tunnel, "open_tunnel", _fake_tunnel())
        monkeypatch.setattr(
            core, "run_mysql_query",
            lambda url, sql, *, params=None, timeout=15, connect_timeout=None: (list(_MYSQL_COLS), 0))
        rc = run_cli(wsdir, "describe-table", "shop", "widgets", "--format", "text")
        assert rc == EXIT_OK
        out = capsys.readouterr().out
        # header row + separator + both columns rendered
        assert "column_name" in out and "data_type" in out
        assert "id" in out and "name" in out
        assert "----" in out  # the dashed separator line

    def test_mysql_text_empty_table(self, wsdir, monkeypatch, capsys):
        _write_conn(wsdir, "shop", "mysql://u:p@localhost:3306/shopdb", "mysql")
        monkeypatch.setattr(tunnel, "open_tunnel", _fake_tunnel())
        monkeypatch.setattr(
            core, "run_mysql_query",
            lambda url, sql, *, params=None, timeout=15, connect_timeout=None: ([], 0))
        rc = run_cli(wsdir, "describe-table", "shop", "ghost", "--format", "text")
        assert rc == EXIT_OK
        assert "not found or has no columns" in capsys.readouterr().out

    def test_mysql_json_render(self, wsdir, monkeypatch, capsys):
        _write_conn(wsdir, "shop", "mysql://u:p@localhost:3306/shopdb", "mysql")
        monkeypatch.setattr(tunnel, "open_tunnel", _fake_tunnel())
        monkeypatch.setattr(
            core, "run_mysql_query",
            lambda url, sql, *, params=None, timeout=15, connect_timeout=None: (list(_MYSQL_COLS), 0))
        rc = run_cli(wsdir, "describe-table", "shop", "widgets", "--format", "json")
        assert rc == EXIT_OK
        obj = json.loads(capsys.readouterr().out)
        assert obj["table"] == "widgets"
        assert [c["column_name"] for c in obj["columns"]] == ["id", "name"]

    def test_mysql_query_error_maps_to_sql_error(self, wsdir, monkeypatch):
        # core.run_mysql_query already wraps driver errors into a
        # QuarryError(EXIT_SQL_ERROR) itself (see core.py); cmd_describe_table
        # (via core.cached_columns(raise_errors=True)) just lets that propagate,
        # same as every other mysql-backed CLI command.
        _write_conn(wsdir, "shop", "mysql://u:p@localhost:3306/shopdb", "mysql")
        monkeypatch.setattr(tunnel, "open_tunnel", _fake_tunnel())

        def boom(url, sql, *, params=None, timeout=15, connect_timeout=None):
            raise core.QuarryError("mysql error: mysql exploded", exit_code=EXIT_SQL_ERROR)

        monkeypatch.setattr(core, "run_mysql_query", boom)
        args = cli.build_parser().parse_args(
            ["--workspace", str(wsdir), "describe-table", "shop", "widgets", "--format", "json"])
        workspace.configure_workspace(args.workspace)
        with pytest.raises(core.QuarryError) as ei:
            args.func(args)
        assert ei.value.exit_code == EXIT_SQL_ERROR

    def test_cache_hit_skips_the_db_query(self, wsdir, monkeypatch, capsys):
        # issue #97: `qy describe-table` metadata now goes through the shared
        # cache (core.cached_columns), so a second lookup for the same
        # db@env:table is served without ever touching the database again.
        _write_conn(wsdir, "shop", "mysql://u:p@localhost:3306/shopdb", "mysql")
        monkeypatch.setattr(tunnel, "open_tunnel", _fake_tunnel())
        calls = []

        def counting_query(url, sql, *, params=None, timeout=15, connect_timeout=None):
            calls.append(sql)
            return list(_MYSQL_COLS), 0

        monkeypatch.setattr(core, "run_mysql_query", counting_query)
        rc1 = run_cli(wsdir, "describe-table", "shop", "widgets", "--format", "json")
        assert rc1 == EXIT_OK
        first = json.loads(capsys.readouterr().out)
        assert len(calls) == 1

        # a second call for the same table must be served from cache, not the DB
        monkeypatch.setattr(
            core, "run_mysql_query",
            lambda *a, **k: pytest.fail("must not query the DB on a cache hit"))
        rc2 = run_cli(wsdir, "describe-table", "shop", "widgets", "--format", "json")
        assert rc2 == EXIT_OK
        second = json.loads(capsys.readouterr().out)
        assert second == first
        assert len(calls) == 1


@pytest.mark.unit
class TestDescribeTableUnsupportedEngines:
    @pytest.mark.parametrize("engine,url", [
        ("redis", "redis://localhost:6379/0"),
        ("neptune", "https://neptune.example.com:8182"),
    ])
    def test_unsupported_engine_is_usage_error(self, wsdir, engine, url):
        # redis/neptune reject describe-table before any tunnel access via
        # err(..., EXIT_USAGE), which raises QuarryError carrying the message.
        _write_conn(wsdir, "c", url, engine)
        args = cli.build_parser().parse_args(
            ["--workspace", str(wsdir), "describe-table", "c", "t"])
        workspace.configure_workspace(args.workspace)
        with pytest.raises(core.QuarryError) as ei:
            args.func(args)
        assert ei.value.exit_code == EXIT_USAGE
        assert f"not supported for engine={engine}" in str(ei.value)


@pytest.mark.unit
class TestDescribeTablePsqlTextErrors:
    """The engine=postgres text path shells out to psql; cover its timeout and
    non-zero-exit handlers with a mocked subprocess (no real psql invoked).
    err(..., exit_code=...) raises QuarryError, so run_cli reports the exit code
    (the message rides on the exception, not stderr)."""

    def test_psql_text_timeout(self, wsdir, monkeypatch):
        import subprocess as _sp
        _write_conn(wsdir, "pg", "postgresql://localhost:5432/x", "postgres")
        monkeypatch.setattr(tunnel, "open_tunnel", _fake_tunnel())

        def fake_run(cmd, **kw):
            raise _sp.TimeoutExpired(cmd, kw.get("timeout", 15))

        monkeypatch.setattr(cli.subprocess, "run", fake_run)
        rc = run_cli(wsdir, "describe-table", "pg", "customers", "--format", "text")
        assert rc == EXIT_CONNECTION_ERROR

    def test_psql_text_nonzero_exit(self, wsdir, monkeypatch):
        import subprocess as _sp
        _write_conn(wsdir, "pg", "postgresql://localhost:5432/x", "postgres")
        monkeypatch.setattr(tunnel, "open_tunnel", _fake_tunnel())

        def fake_run(cmd, **kw):
            return _sp.CompletedProcess(cmd, returncode=1, stdout="", stderr="psql: boom")

        monkeypatch.setattr(cli.subprocess, "run", fake_run)
        rc = run_cli(wsdir, "describe-table", "pg", "customers", "--format", "text")
        assert rc == EXIT_SQL_ERROR

    def test_psql_text_ok(self, wsdir, monkeypatch, capsys):
        import subprocess as _sp
        _write_conn(wsdir, "pg", "postgresql://localhost:5432/x", "postgres")
        monkeypatch.setattr(tunnel, "open_tunnel", _fake_tunnel())

        def fake_run(cmd, **kw):
            return _sp.CompletedProcess(
                cmd, returncode=0, stdout="Table widgets\n id | int\n", stderr="")

        monkeypatch.setattr(cli.subprocess, "run", fake_run)
        rc = run_cli(wsdir, "describe-table", "pg", "widgets", "--format", "text")
        assert rc == EXIT_OK
        assert "Table widgets" in capsys.readouterr().out


# ===========================================================================
# _execute — prod-write abort path (cli.py 394 / the confirm-abort guard)
# ===========================================================================

@pytest.mark.unit
class TestExecuteProdAbort:
    def test_prod_write_declined_aborts(self, wsdir, monkeypatch):
        # A write against a prod connection with --write but not --yes prompts; a
        # 'n' answer makes _confirm_prod_write return False, so _execute calls
        # err('aborted', EXIT_USAGE) — which raises QuarryError (err with an
        # exit_code raises, it does not return) — and execute_sql is never reached.
        import argparse
        conn = core.Connection(key="prodpg", url="postgresql://localhost/x", env="prod")

        called = {"execute": False}
        monkeypatch.setattr(
            core, "execute_sql",
            lambda **kw: called.__setitem__("execute", True) or EXIT_OK)  # pragma: no cover
        monkeypatch.setattr(cli.sys, "stdin", io.StringIO("n\n"))

        args = argparse.Namespace(write=True, yes=False, format="json", max_rows=None)
        with pytest.raises(core.QuarryError) as ei:
            cli._execute(conn, "DELETE FROM t", {}, args)
        assert ei.value.exit_code == EXIT_USAGE
        assert "aborted" in str(ei.value)
        assert called["execute"] is False  # write blocked before reaching the engine


# ===========================================================================
# _execute — the elapsed/download-size/avg-speed stderr summary (issue #105)
# ===========================================================================

@pytest.mark.unit
class TestExecuteStatsLine:
    def _args(self, **overrides):
        import argparse
        base = dict(write=False, yes=False, format="json", max_rows=None, no_proxy=True)
        base.update(overrides)
        return argparse.Namespace(**base)

    def test_estimated_engine_gets_stats_line_with_approx_marker(self, monkeypatch, capsys):
        # redis/mysql/postgres approximate download size -> '≈' marker.
        conn = core.Connection(key="cache", url="redis://localhost:6379/0", engine="redis")
        monkeypatch.setattr(tunnel, "open_tunnel", _fake_tunnel())
        monkeypatch.setattr(redis_engine, "is_redis_read_only", lambda cmd: True)
        monkeypatch.setattr(redis_engine, "run_redis",
                            lambda url, cmd, timeout=None: ([{"value": "x"}], 4300))

        rc = cli._execute(conn, "GET foo", {}, self._args())
        assert rc == EXIT_OK

        captured = capsys.readouterr()
        assert json.loads(captured.out) == [{"value": "x"}]  # stdout stays data-only
        assert "downloaded ≈4.2 KB" in captured.err
        assert "avg speed ≈" in captured.err and "/s" in captured.err
        assert "ms · " in captured.err

    def test_neptune_exact_size_has_no_approx_marker(self, monkeypatch, capsys):
        conn = core.Connection(key="graph", url="https://neptune.example.com:8182", engine="neptune")
        monkeypatch.setattr(tunnel, "open_tunnel", _fake_tunnel())
        monkeypatch.setattr(core, "run_neptune_cypher",
                            lambda url, sql, params=None, timeout=None, use_proxy=None, workspace_home=None:
                            ([{"n": 1}], 100))

        rc = cli._execute(conn, "MATCH (n) RETURN n", {}, self._args())
        assert rc == EXIT_OK

        captured = capsys.readouterr()
        assert json.loads(captured.out) == [{"n": 1}]
        assert "≈" not in captured.err
        assert "downloaded 100 B" in captured.err

    def test_no_stats_line_when_write_is_blocked(self, monkeypatch, capsys):
        # execute_sql raises before filling stats -> _execute must not print a
        # (bogus, empty) stats line alongside the safety error.
        conn = core.Connection(key="cache", url="redis://localhost:6379/0", engine="redis")
        monkeypatch.setattr(tunnel, "open_tunnel", _fake_tunnel())
        monkeypatch.setattr(redis_engine, "is_redis_read_only", lambda cmd: False)

        with pytest.raises(core.QuarryError) as ei:
            cli._execute(conn, "DEL foo", {}, self._args())
        assert ei.value.exit_code == core.EXIT_SAFETY_BLOCKED

        captured = capsys.readouterr()
        assert "ms · downloaded" not in captured.err

    def test_stdout_stays_clean_for_ndjson_pipe(self, monkeypatch, capsys):
        conn = core.Connection(key="cache", url="redis://localhost:6379/0", engine="redis")
        monkeypatch.setattr(tunnel, "open_tunnel", _fake_tunnel())
        monkeypatch.setattr(redis_engine, "is_redis_read_only", lambda cmd: True)
        monkeypatch.setattr(redis_engine, "run_redis",
                            lambda url, cmd, timeout=None: ([{"value": "a"}, {"value": "b"}], 42))

        rc = cli._execute(conn, "KEYS *", {}, self._args(format="ndjson"))
        assert rc == EXIT_OK

        captured = capsys.readouterr()
        lines = [json.loads(x) for x in captured.out.splitlines()]
        assert lines == [{"value": "a"}, {"value": "b"}]
        assert "ms · downloaded" in captured.err


@pytest.mark.unit
class TestKeepAliveCommands:
    def test_up_down_and_status_json(self, wsdir, monkeypatch, capsys):
        from quarry import keepalive

        monkeypatch.setattr(keepalive, "start", lambda ws: (True, 43210))
        monkeypatch.setattr(keepalive, "stop", lambda ws: True)
        monkeypatch.setattr(keepalive, "status", lambda ws: {
            "workspace": str(ws),
            "enabled": True,
            "reconnect": True,
            "keeper": {"running": True, "pid": 43210},
            "tunnels": [{"connection": "shop", "env": "dev", "state": "up", "localPort": 55123}],
            "updatedAt": 0.0,
        })

        assert run_cli(wsdir, "up") == EXIT_OK
        assert "started" in capsys.readouterr().out
        assert run_cli(wsdir, "status", "--format", "json") == EXIT_OK
        body = json.loads(capsys.readouterr().out)
        assert body["keeper"]["pid"] == 43210
        assert body["tunnels"][0]["state"] == "up"
        assert run_cli(wsdir, "down") == EXIT_OK
        assert "stopped" in capsys.readouterr().out
