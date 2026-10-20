"""Cross-face and config-fingerprint coverage for the shared metadata cache
(issue #97): src/quarry/cache.py's on-disk store is used identically by
gui.py, cli.py, and mcp.py, so a value written by one face is visible to
the others without a second DB round trip. Cache-file isolation is provided
by conftest.py's autouse `_isolated_cache` fixture.
"""

from __future__ import annotations

import contextlib

import pytest

from quarry import cache, cli, core, gui, mcp, tunnel, workspace
from quarry.core import EXIT_OK


def _write_conn(wsdir, key: str, url: str, engine: str = "mysql") -> None:
    (wsdir / "connections.toml").write_text(
        f'[{key}]\nurl = "{url}"\nengine = "{engine}"\n', encoding="utf-8")


def _run_cli(wsdir, *argv):
    args = cli.build_parser().parse_args(["--workspace", str(wsdir), *argv])
    workspace.configure_workspace(args.workspace)
    return args.func(args)


_COLS = [
    {"column_name": "id", "data_type": "int", "is_nullable": "NO",
     "column_default": None, "character_maximum_length": None},
]


# ---------------------------------------------------------------------------
# GUI and CLI (and MCP) read the same cache — cross-face, both directions.
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestCrossFaceSharedCache:
    def test_cli_write_is_read_by_gui_without_a_second_query(self, wsdir, monkeypatch, capsys):
        _write_conn(wsdir, "shop", "mysql://u:p@localhost:3306/shopdb")

        def query_once(conn, sql, **kwargs):
            return core.QueryResult(columns=[{"name": k, "type": None} for k in _COLS[0]],
                                     rows=list(_COLS), row_count=1, truncated=False,
                                     elapsed_ms=1, engine="mysql", sql=sql)

        monkeypatch.setattr(core, "run_query", query_once)
        rc = _run_cli(wsdir, "describe-table", "shop", "widgets", "--format", "json")
        assert rc == EXIT_OK
        capsys.readouterr()  # discard the CLI's own JSON output

        # a real DB query here would prove the GUI kept its own, separate cache
        monkeypatch.setattr(
            core, "run_query",
            lambda *a, **k: pytest.fail("gui.api_columns re-queried instead of reusing the CLI's cache entry"))
        out = gui.api_columns("shop", None, "widgets")
        assert out == {"columns": ["id"], "types": {"id": "int"}}

    def test_gui_write_is_read_by_cli_without_a_second_query(self, wsdir, monkeypatch, capsys):
        _write_conn(wsdir, "shop", "mysql://u:p@localhost:3306/shopdb")
        workspace.configure_workspace(str(wsdir))

        def query_once(conn, sql, **kwargs):
            return core.QueryResult(columns=[{"name": k, "type": None} for k in _COLS[0]],
                                     rows=list(_COLS), row_count=1, truncated=False,
                                     elapsed_ms=1, engine="mysql", sql=sql)

        monkeypatch.setattr(core, "run_query", query_once)
        out = gui.api_columns("shop", None, "widgets")
        assert out == {"columns": ["id"], "types": {"id": "int"}}

        monkeypatch.setattr(
            core, "run_query",
            lambda *a, **k: pytest.fail("cli describe-table re-queried instead of reusing the GUI's cache entry"))
        rc = _run_cli(wsdir, "describe-table", "shop", "widgets", "--format", "json")
        assert rc == EXIT_OK
        assert '"column_name": "id"' in capsys.readouterr().out

    def test_gui_write_is_read_by_mcp_without_a_second_query(self, wsdir, monkeypatch):
        _write_conn(wsdir, "shop", "postgresql://u:p@localhost:5432/shopdb", "postgres")
        workspace.configure_workspace(str(wsdir))

        def query_once(conn, sql, **kwargs):
            return core.QueryResult(columns=[{"name": "table_name", "type": None}],
                                     rows=[{"table_name": "widgets"}], row_count=1,
                                     truncated=False, elapsed_ms=1, engine="postgres", sql=sql)

        monkeypatch.setattr(core, "run_query", query_once)
        out = gui.api_tables("shop", None)
        assert out["tables"] == ["widgets"]

        monkeypatch.setattr(
            core, "run_query",
            lambda *a, **k: pytest.fail("mcp.tool_list_tables re-queried instead of reusing the GUI's cache entry"))
        assert mcp.tool_list_tables("shop") == {"engine": "postgres", "tables": ["widgets"]}


# ---------------------------------------------------------------------------
# Config-fingerprint invalidation (issue #97 / #98): a health probe is keyed
# by connection_fingerprint(conn), so a URL (or SSH/proxy) change invalidates
# it automatically — no hand-enumerated purge needed. tables:*/columns:*
# entries carry no fingerprint (schema metadata doesn't change just because
# the connection string did) and so are left alone by the same change.
# ---------------------------------------------------------------------------

class _FpConn:
    def __init__(self, url):
        self.url = url
        self.engine = "postgres"
        self.ssh_host = self.ssh_user = self.ssh_key = self.ssh_port = None


@pytest.mark.unit
def test_url_change_invalidates_health_but_not_tables(monkeypatch):
    monkeypatch.setattr(core.workspace, "is_proxy_enabled", lambda home: False)

    @contextlib.contextmanager
    def fake_tunnel(conn, engine, **kw):
        yield conn.url

    monkeypatch.setattr(core.tunnel, "open_tunnel", fake_tunnel)
    probes = []

    def fake_psql_capture(url, sql, timeout=6):
        probes.append(url)
        return 0, "", ""

    monkeypatch.setattr(core, "run_psql_capture", fake_psql_capture)

    conn_a = _FpConn("postgresql://host-a/shopdb")
    assert core.cached_health(conn_a, "shop", "dev") == {"ok": True}
    assert len(probes) == 1

    # a table list has no fingerprint at all — seed it directly, as
    # core.cached_tables would after a real query.
    cache.put("tables:shop@dev", {"tables": ["orders"], "engine": "postgres", "capped": False})

    # same cache key, but the connection URL changed -> a different fingerprint
    conn_b = _FpConn("postgresql://host-b/shopdb")
    assert core.cached_health(conn_b, "shop", "dev") == {"ok": True}
    assert len(probes) == 2  # the stale, differently-fingerprinted entry forced a re-probe

    # the unrelated tables:* entry was never touched by the URL change
    assert cache.get("tables:shop@dev") == {
        "tables": ["orders"], "engine": "postgres", "capped": False}


# ---------------------------------------------------------------------------
# Backward compatibility with a pre-#97, GUI-only gui-cache.json (issue #97
# acceptance criteria: "existing gui-cache.json still readable"). The old GUI
# stored columns:* entries as {"columns": [...], "types": {...}}; the new
# core.cached_columns canonical shape is {"rows": [...]}.
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_cached_columns_reads_legacy_gui_only_cache_entry(monkeypatch):
    legacy = {"columns": ["id", "name"], "types": {"id": "int", "name": "varchar"}}
    cache.put("columns:shop@dev:widgets", legacy)

    monkeypatch.setattr(
        core, "run_query",
        lambda *a, **k: pytest.fail("a legacy cache entry must not be treated as a miss"))
    res = core.cached_columns(_FpConn("mysql://x/shopdb"), "shop", "dev", "widgets")
    assert res == {"rows": [
        {"column_name": "id", "data_type": "int", "is_nullable": None,
         "column_default": None, "character_maximum_length": None},
        {"column_name": "name", "data_type": "varchar", "is_nullable": None,
         "column_default": None, "character_maximum_length": None},
    ]}
    # the entry is upgraded to the canonical shape on read, in place
    assert "rows" in cache.get("columns:shop@dev:widgets")
