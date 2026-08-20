"""GUI backend helper + branch tests (quarry.gui).

Companion to test_gui_api.py, which drives the endpoints through the live
ThreadingHTTPServer. This file exercises the *internal helpers and branches*
that HTTP-level tests miss — cache persistence, the per-engine branches of
api_health / api_tables / api_columns / api_inspect / _list_tables, and the
port-reclaim / _bind logic — almost entirely with mocks (no server, no DB
except a couple of @requires_db in-process integration checks).

We NEVER call serve() (it blocks) or webbrowser.
"""

from __future__ import annotations

import contextlib
import errno
import os
import signal
import socket
import subprocess

import pytest

from conftest import requires_db


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

class _Conn:
    """Minimal stand-in for a core.Connection. ssh_user/ssh_key/ssh_port are
    read by core.connection_fingerprint (issue #97) even when unset."""

    def __init__(self, url="postgresql://localhost/x", ssh_host=None,
                 ssh_user=None, ssh_key=None, ssh_port=None):
        self.url = url
        self.ssh_host = ssh_host
        self.ssh_user = ssh_user
        self.ssh_key = ssh_key
        self.ssh_port = ssh_port


def _fake_tunnel(expected_url="URL"):
    """Return a function usable as a monkeypatched tunnel.open_tunnel: a
    @contextmanager that yields a fixed effective URL."""

    @contextlib.contextmanager
    def _open(conn, engine):
        yield expected_url

    return _open


class _Res:
    """Stand-in for core.QueryResult with just the .rows attribute _list_tables /
    api_columns read."""

    def __init__(self, rows):
        self.rows = rows


@pytest.fixture()
def isolated_cache():
    """The gui module. Cache-file isolation (and clearing between tests) is
    provided globally by the autouse `_isolated_cache` fixture in conftest.py
    (issue #97) — the shared cache now lives in cache.py, reachable here as
    gui.cache, and health helpers moved to core.py, reachable as gui.core."""
    from quarry import gui

    yield gui


# ---------------------------------------------------------------------------
# cache: put -> save -> load round-trip
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_cache_put_persists_and_reloads(isolated_cache):
    gui = isolated_cache
    ret = gui.cache.put("k1", {"a": 1, "unicode": "héllo"})
    assert ret == {"a": 1, "unicode": "héllo"}          # returns the value
    assert gui.cache.get("k1") == {"a": 1, "unicode": "héllo"}
    assert gui.cache.CACHE_FILE.exists()                 # save() wrote JSON

    # Simulate a fresh process: drop the in-memory cache, then load() reads it back.
    gui.cache._CACHE.clear()
    assert gui.cache.get("k1") is None
    gui.cache.load()
    assert gui.cache.get("k1") == {"a": 1, "unicode": "héllo"}


@pytest.mark.unit
def test_cache_get_missing_returns_none(isolated_cache):
    assert isolated_cache.cache.get("nope") is None


@pytest.mark.unit
def test_load_cache_ignores_bad_file(isolated_cache):
    gui = isolated_cache
    gui.cache.CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    gui.cache.CACHE_FILE.write_text("{not json", encoding="utf-8")
    gui.cache.load()                       # must not raise
    assert gui.cache.get("anything") is None


@pytest.mark.unit
def test_load_cache_ignores_non_dict_json(isolated_cache):
    gui = isolated_cache
    gui.cache.CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    gui.cache.CACHE_FILE.write_text("[1, 2, 3]", encoding="utf-8")  # valid JSON, wrong type
    gui.cache.load()
    assert gui.cache.get("anything") is None


@pytest.mark.unit
def test_save_cache_swallows_errors(isolated_cache, monkeypatch):
    """cache.save() must never raise even if the FS write fails."""
    gui = isolated_cache

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(gui.Path, "mkdir", boom)
    gui.cache.put("k", {"v": 1})           # save() internally catches OSError
    assert gui.cache.get("k") == {"v": 1}  # in-memory put still succeeded


# ---------------------------------------------------------------------------
# health helpers
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_put_health_stamps_ts_and_returns_clean(isolated_cache, monkeypatch):
    gui = isolated_cache
    monkeypatch.setattr(gui.time, "time", lambda: 5000.0)
    out = gui.core._put_health("health:x@dev", "fp1", {"ok": True})
    assert out == {"ok": True}                      # returned value has NO _ts
    stored = gui.cache.get("health:x@dev", fingerprint="fp1")
    assert stored["ok"] is True and stored["_ts"] == 5000.0   # stored value HAS _ts


@pytest.mark.unit
def test_health_fresh_enough_boundary(monkeypatch):
    from quarry import gui

    monkeypatch.setattr(gui.core, "HEALTH_TTL_SEC", 100)
    monkeypatch.setattr(gui.time, "time", lambda: 1000.0)
    assert gui.core._health_fresh_enough({"_ts": 1000.0}) is True          # age 0
    assert gui.core._health_fresh_enough({"_ts": 901.0}) is True           # age 99 < 100
    assert gui.core._health_fresh_enough({"_ts": 900.0}) is False          # age 100, not < 100
    assert gui.core._health_fresh_enough({"_ts": "bad"}) is False          # non-numeric
    assert gui.core._health_fresh_enough({}) is False                      # missing _ts


# ---------------------------------------------------------------------------
# api_health — each engine branch, mocked (no real DB)
# ---------------------------------------------------------------------------

def _patch_health(monkeypatch, gui, engine, conn=None):
    """Wire _resolve/connection_engine/open_tunnel for a mocked api_health probe."""
    conn = conn or _Conn()
    monkeypatch.setattr(gui, "_resolve", lambda db, env: conn)
    monkeypatch.setattr(gui.core, "connection_engine", lambda c: engine)
    monkeypatch.setattr(gui.tunnel, "open_tunnel", _fake_tunnel("EFF_URL"))


@pytest.mark.unit
def test_api_health_redis_ping_ok(isolated_cache, monkeypatch):
    gui = isolated_cache
    _patch_health(monkeypatch, gui, "redis")
    seen = {}
    monkeypatch.setattr(gui.redis_engine, "run_redis",
                        lambda url, cmd, timeout=6: seen.update(url=url, cmd=cmd) or [])
    out = gui.api_health("r", "dev", fresh=True)
    assert out == {"ok": True}
    assert seen == {"url": "EFF_URL", "cmd": "PING"}


@pytest.mark.unit
def test_api_health_mysql_select1_ok(isolated_cache, monkeypatch):
    gui = isolated_cache
    _patch_health(monkeypatch, gui, "mysql")
    calls = []
    monkeypatch.setattr(gui.core, "run_mysql_query",
                        lambda url, sql, timeout=6: calls.append((url, sql)))
    out = gui.api_health("m", "dev", fresh=True)
    assert out == {"ok": True}
    assert calls == [("EFF_URL", "SELECT 1")]


@pytest.mark.unit
def test_api_health_neptune_ok(isolated_cache, monkeypatch):
    gui = isolated_cache
    _patch_health(monkeypatch, gui, "neptune")
    calls = []
    monkeypatch.setattr(gui.core, "run_neptune_cypher",
                        lambda url, cy, timeout=6, **kwargs: calls.append((url, cy)))
    out = gui.api_health("n", "dev", fresh=True)
    assert out == {"ok": True}
    assert calls == [("EFF_URL", "RETURN 1 AS ok")]


@pytest.mark.unit
def test_api_health_postgres_ok(isolated_cache, monkeypatch):
    gui = isolated_cache
    _patch_health(monkeypatch, gui, "postgres")
    monkeypatch.setattr(gui.core, "run_psql_capture",
                        lambda url, sql, timeout=6: (0, "1", ""))
    assert gui.api_health("p", "dev", fresh=True) == {"ok": True}


@pytest.mark.unit
def test_api_health_postgres_rc_nonzero_error(isolated_cache, monkeypatch):
    gui = isolated_cache
    _patch_health(monkeypatch, gui, "postgres")
    monkeypatch.setattr(gui.core, "run_psql_capture",
                        lambda url, sql, timeout=6: (2, "", "  FATAL: nope  "))
    out = gui.api_health("p", "dev", fresh=True)
    assert out == {"ok": False, "error": "FATAL: nope"}   # stripped


@pytest.mark.unit
def test_api_health_postgres_rc_nonzero_empty_stderr(isolated_cache, monkeypatch):
    gui = isolated_cache
    _patch_health(monkeypatch, gui, "postgres")
    monkeypatch.setattr(gui.core, "run_psql_capture",
                        lambda url, sql, timeout=6: (1, "", "   "))
    out = gui.api_health("p", "dev", fresh=True)
    assert out == {"ok": False, "error": "connect failed"}  # fallback message


@pytest.mark.unit
def test_api_health_exception_branch(isolated_cache, monkeypatch):
    gui = isolated_cache
    monkeypatch.setattr(gui, "_resolve",
                        lambda db, env: (_ for _ in ()).throw(RuntimeError("x" * 400)))
    out = gui.api_health("boom", "dev", fresh=True)
    assert out["ok"] is False
    assert out["error"] == "x" * 200          # truncated to 200 chars


@pytest.mark.unit
def test_api_health_cached_only_nothing_fresh(isolated_cache, monkeypatch):
    gui = isolated_cache
    _patch_health(monkeypatch, gui, "postgres")
    # No cache entry at all -> cached_only returns {ok: None} without probing.
    assert gui.api_health("never", "dev", cached_only=True) == {"ok": None}


@pytest.mark.unit
def test_api_health_cached_only_returns_fresh_entry(isolated_cache, monkeypatch):
    gui = isolated_cache
    _patch_health(monkeypatch, gui, "postgres")
    monkeypatch.setattr(gui.core, "HEALTH_TTL_SEC", 100)
    monkeypatch.setattr(gui.time, "time", lambda: 2000.0)
    fp = gui.core.connection_fingerprint(_Conn())
    gui.core._put_health("health:c@dev", fp, {"ok": True})          # fresh (_ts=2000)
    # cached_only must serve straight from cache without an actual network probe
    # (resolving the connection to check its fingerprint is cheap/local, unlike a probe).
    monkeypatch.setattr(gui.core, "run_psql_capture",
                        lambda *a, **k: pytest.fail("must not probe"))
    assert gui.api_health("c", "dev", cached_only=True) == {"ok": True}


@pytest.mark.unit
def test_api_health_fresh_bypasses_cache(isolated_cache, monkeypatch):
    gui = isolated_cache
    monkeypatch.setattr(gui.core, "HEALTH_TTL_SEC", 100)
    monkeypatch.setattr(gui.time, "time", lambda: 3000.0)
    gui.core._put_health("health:p@dev", "x", {"ok": False, "error": "old"})  # a fresh cached failure
    _patch_health(monkeypatch, gui, "postgres")
    monkeypatch.setattr(gui.core, "run_psql_capture",
                        lambda url, sql, timeout=6: (0, "1", ""))
    # fresh=True ignores the fresh-but-stale-in-meaning cache and re-probes -> ok.
    assert gui.api_health("p", "dev", fresh=True) == {"ok": True}


@pytest.mark.unit
def test_api_health_uses_fresh_cache_without_probe(isolated_cache, monkeypatch):
    gui = isolated_cache
    _patch_health(monkeypatch, gui, "postgres")
    monkeypatch.setattr(gui.core, "HEALTH_TTL_SEC", 100)
    monkeypatch.setattr(gui.time, "time", lambda: 4000.0)
    fp = gui.core.connection_fingerprint(_Conn())
    gui.core._put_health("health:p@dev", fp, {"ok": True})
    monkeypatch.setattr(gui.core, "run_psql_capture",
                        lambda *a, **k: pytest.fail("must not probe"))
    # Not fresh, cache present + fresh_enough -> served from cache, _ts stripped.
    assert gui.api_health("p", "dev") == {"ok": True}


# ---------------------------------------------------------------------------
# api_tables — engine branches + cache put/hit
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_api_tables_mysql_branch_and_cache(isolated_cache, monkeypatch):
    gui = isolated_cache
    monkeypatch.setattr(gui, "_resolve", lambda db, env: _Conn())
    monkeypatch.setattr(gui.core, "connection_engine", lambda c: "mysql")
    monkeypatch.setattr(gui.core, "_list_tables", lambda conn, **kw: ["t_a", "t_b"])

    out = gui.api_tables("m", "dev", fresh=True)
    assert out == {"tables": ["t_a", "t_b"], "engine": "mysql", "capped": False, "_cached": False}

    # Second call (fresh=False) is served from cache with _cached=True and no _list_tables.
    monkeypatch.setattr(gui.core, "_list_tables",
                        lambda conn, **kw: pytest.fail("should not re-list on cache hit"))
    hit = gui.api_tables("m", "dev")
    assert hit == {"tables": ["t_a", "t_b"], "engine": "mysql", "capped": False, "_cached": True}


@pytest.mark.unit
def test_api_tables_neptune_branch(isolated_cache, monkeypatch):
    gui = isolated_cache
    monkeypatch.setattr(gui, "_resolve", lambda db, env: _Conn())
    monkeypatch.setattr(gui.core, "connection_engine", lambda c: "neptune")
    # neptune _list_tables returns [] (no SQL); assert the real path via _list_tables.
    out = gui.api_tables("n", None, fresh=True)
    assert out == {"tables": [], "engine": "neptune", "capped": False, "_cached": False}


@pytest.mark.unit
def test_api_tables_redis_branch_and_cache(isolated_cache, monkeypatch):
    gui = isolated_cache
    monkeypatch.setattr(gui, "_resolve", lambda db, env: _Conn())
    monkeypatch.setattr(gui.core, "connection_engine", lambda c: "redis")
    monkeypatch.setattr(gui.tunnel, "open_tunnel", _fake_tunnel("REDIS_URL"))
    seen = {}
    monkeypatch.setattr(gui.redis_engine, "keys_with_meta",
                        lambda url, cap=400: seen.update(url=url, cap=cap) or [{"key": "k1"}])
    out = gui.api_tables("r", "dev", fresh=True)
    assert out == {"engine": "redis", "keys": [{"key": "k1"}], "capped": False, "_cached": False}
    assert seen == {"url": "REDIS_URL", "cap": 400}

    # cache hit
    hit = gui.api_tables("r", "dev")
    assert hit["_cached"] is True and hit["keys"] == [{"key": "k1"}]


@pytest.mark.unit
def test_api_tables_cache_miss_when_not_fresh(isolated_cache, monkeypatch):
    """fresh=False but no cache entry yet -> falls through to a real list (branch 126->128)."""
    gui = isolated_cache
    monkeypatch.setattr(gui, "_resolve", lambda db, env: _Conn())
    monkeypatch.setattr(gui.core, "connection_engine", lambda c: "mysql")
    monkeypatch.setattr(gui.core, "_list_tables", lambda conn, **kw: ["only"])
    out = gui.api_tables("m", "dev")            # fresh defaults to False, cache empty
    assert out == {"tables": ["only"], "engine": "mysql", "capped": False, "_cached": False}


@pytest.mark.unit
def test_api_tables_fresh_bypasses_existing_cache(isolated_cache, monkeypatch):
    """fresh=True must re-list even when a cache entry already exists (branch 126->128)."""
    gui = isolated_cache
    gui.cache.put("tables:m@dev", {"tables": ["stale"], "engine": "mysql"})
    monkeypatch.setattr(gui, "_resolve", lambda db, env: _Conn())
    monkeypatch.setattr(gui.core, "connection_engine", lambda c: "mysql")
    monkeypatch.setattr(gui.core, "_list_tables", lambda conn, **kw: ["fresh1", "fresh2"])
    out = gui.api_tables("m", "dev", fresh=True)
    assert out == {"tables": ["fresh1", "fresh2"], "engine": "mysql", "capped": False, "_cached": False}


@pytest.mark.unit
def test_api_tables_capped_flag_at_5000(isolated_cache, monkeypatch):
    """A table list that hits _list_tables' 5000-row cap is flagged so the UI
    can say 'showing only the first N tables' instead of silently truncating."""
    gui = isolated_cache
    monkeypatch.setattr(gui, "_resolve", lambda db, env: _Conn())
    monkeypatch.setattr(gui.core, "connection_engine", lambda c: "postgres")
    monkeypatch.setattr(gui.core, "_list_tables", lambda conn, **kw: [f"t{i}" for i in range(5000)])
    out = gui.api_tables("p", "dev", fresh=True)
    assert out["capped"] is True and len(out["tables"]) == 5000


# ---------------------------------------------------------------------------
# _req / _max_rows / api_query / api_run  (pure POST-handler helpers)
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_req_present_and_missing(isolated_cache):
    from quarry.core import QuarryError

    gui = isolated_cache
    assert gui._req({"db": "x"}, "db") == "x"
    for body in ({}, {"db": None}, {"db": ""}):
        with pytest.raises(QuarryError, match="missing required field 'db'"):
            gui._req(body, "db")


@pytest.mark.unit
def test_max_rows_default_int_and_bad(isolated_cache):
    from quarry.core import QuarryError

    gui = isolated_cache
    assert gui._max_rows({}) == 500                 # default when absent
    assert gui._max_rows({"maxRows": 0}) == 500     # falsy 0 -> default (0 or 500)
    assert gui._max_rows({"maxRows": "25"}) == 25   # numeric string coerced
    assert gui._max_rows({"maxRows": 42}) == 42
    with pytest.raises(QuarryError, match="maxRows must be an integer"):
        gui._max_rows({"maxRows": "abc"})


@pytest.mark.unit
def test_offset_default_int_and_bad(isolated_cache):
    from quarry.core import QuarryError

    gui = isolated_cache
    assert gui._offset({}) == 0                    # default when absent
    assert gui._offset({"offset": 0}) == 0
    assert gui._offset({"offset": "200"}) == 200   # numeric string coerced
    assert gui._offset({"offset": 42}) == 42
    with pytest.raises(QuarryError, match="offset must be an integer"):
        gui._offset({"offset": "abc"})


@pytest.mark.unit
def test_api_query_wires_resolve_and_run_query(isolated_cache, monkeypatch):
    gui = isolated_cache
    seen = {}

    class _R:
        def to_dict(self):
            return {"rows": [{"n": 1}], "columns": []}

    def fake_resolve(db, env):
        seen["resolve"] = (db, env)
        return _Conn()

    monkeypatch.setattr(gui, "_resolve", fake_resolve)

    def fake_run(conn, sql, *, max_rows, offset, with_types):
        seen["run"] = (sql, max_rows, offset, with_types)
        return _R()

    monkeypatch.setattr(gui.core, "run_query", fake_run)
    out = gui.api_query({"db": "d", "env": "e", "sql": "SELECT 1", "maxRows": "7", "offset": "20"})
    assert out == {"rows": [{"n": 1}], "columns": []}
    assert seen["resolve"] == ("d", "e")
    assert seen["run"] == ("SELECT 1", 7, 20, True)


@pytest.mark.unit
def test_api_run_loads_named_query(isolated_cache, monkeypatch):
    gui = isolated_cache

    class _Q:
        db = "reports"
        sql = "SELECT * FROM t WHERE x = :x"

    class _R:
        def to_dict(self):
            return {"rows": [], "columns": []}

    seen = {}

    def fake_load_query(name):
        seen["name"] = name
        return _Q()

    def fake_resolve(db, env):
        seen["resolve"] = (db, env)
        c = _Conn()
        c.logical_db = db      # the producing connection reported back to the client
        c.env = env
        return c

    def fake_resolve_params(q, p):
        seen["params"] = p
        return {"x": "1"}

    monkeypatch.setattr(gui.core, "load_query", fake_load_query)
    monkeypatch.setattr(gui, "_resolve", fake_resolve)
    monkeypatch.setattr(gui.core, "resolve_params", fake_resolve_params)

    def fake_run(conn, sql, *, params, max_rows, offset, with_types):
        seen["run"] = (sql, params, max_rows, offset, with_types)
        return _R()

    monkeypatch.setattr(gui.core, "run_query", fake_run)
    out = gui.api_run({"name": "rep", "env": "prod", "params": {"x": "1"}, "maxRows": 9})
    assert out == {"rows": [], "columns": [], "db": "reports", "env": "prod"}
    assert seen["name"] == "rep"
    assert seen["resolve"] == ("reports", "prod")
    assert seen["params"] == {"x": "1"}
    assert seen["run"] == ("SELECT * FROM t WHERE x = :x", {"x": "1"}, 9, 0, True)


# ---------------------------------------------------------------------------
# api_columns — sanitizer, caching, redis/neptune, empty
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_api_columns_empty_table_no_db(isolated_cache, monkeypatch):
    gui = isolated_cache
    monkeypatch.setattr(gui, "_resolve",
                        lambda db, env: pytest.fail("must not resolve for empty table"))
    assert gui.api_columns("db", "dev", "") == {"columns": [], "types": {}}
    assert gui.api_columns("db", "dev", "   ") == {"columns": [], "types": {}}


@pytest.mark.unit
def test_api_columns_binds_table_name_as_param_not_string_concat(isolated_cache, monkeypatch):
    """The table name must travel as a bound `:'table'` query parameter, not be
    spliced into the SQL text — a prior character-stripping sanitizer silently
    dropped legal quoted/special-char table names (e.g. `qy-review-weird`) that
    /api/tables had just listed, showing an empty schema for a real table."""
    gui = isolated_cache
    captured = {}

    def fake_run_query(conn, sql, params=None, max_rows=2000, **kwargs):
        captured["sql"] = sql
        captured["params"] = params
        return _Res([{"column_name": "id", "data_type": "integer"},
                     {"column_name": "name", "data_type": "text"},
                     {"column_name": None, "data_type": "text"}])

    monkeypatch.setattr(gui, "_resolve", lambda db, env: _Conn())
    monkeypatch.setattr(gui.core, "connection_engine", lambda c: "postgres")
    monkeypatch.setattr(gui.core, "run_query", fake_run_query)

    weird = "qy-review weird$1"                            # dash + space + $, all legal in postgres
    out = gui.api_columns("db", "dev", weird)
    assert out == {"columns": ["id", "name"],
                    "types": {"id": "integer", "name": "text"}}  # None column filtered out
    assert captured["params"] == {"table": weird}          # exact name, unmangled
    assert weird not in captured["sql"]                    # never spliced into the SQL text
    assert ":'table'" in captured["sql"]                   # bound placeholder instead

    # mysql uses DATABASE() schema
    captured.clear()
    monkeypatch.setattr(gui.core, "connection_engine", lambda c: "mysql")
    gui.api_columns("db", "dev2", "orders")
    assert "DATABASE()" in captured["sql"]


@pytest.mark.unit
def test_api_columns_caches_result(isolated_cache, monkeypatch):
    gui = isolated_cache
    n = {"calls": 0}

    def fake_run_query(conn, sql, params=None, max_rows=2000, **kwargs):
        n["calls"] += 1
        return _Res([{"column_name": "id", "data_type": "integer"}])

    monkeypatch.setattr(gui, "_resolve", lambda db, env: _Conn())
    monkeypatch.setattr(gui.core, "connection_engine", lambda c: "postgres")
    monkeypatch.setattr(gui.core, "run_query", fake_run_query)

    first = gui.api_columns("db", "dev", "t")
    second = gui.api_columns("db", "dev", "t")
    assert first == second == {"columns": ["id"], "types": {"id": "integer"}}
    assert n["calls"] == 1                                # second served from cache


@pytest.mark.unit
def test_api_columns_redis_neptune_return_empty_and_cache(isolated_cache, monkeypatch):
    gui = isolated_cache
    monkeypatch.setattr(gui, "_resolve", lambda db, env: _Conn())
    for eng in ("redis", "neptune"):
        monkeypatch.setattr(gui.core, "connection_engine", lambda c, e=eng: e)
        monkeypatch.setattr(gui.core, "run_query",
                            lambda *a, **k: pytest.fail("no DB call for redis/neptune"))
        out = gui.api_columns("db", eng, "tbl")
        assert out == {"columns": [], "types": {}}
        # cached: the key is stored (a later hit returns the same object, no resolve).
        assert gui.cache.get(f"columns:db@{eng}:tbl") == {"rows": []}


@pytest.mark.unit
def test_api_columns_swallows_exception(isolated_cache, monkeypatch):
    gui = isolated_cache
    monkeypatch.setattr(gui, "_resolve",
                        lambda db, env: (_ for _ in ()).throw(RuntimeError("boom")))
    assert gui.api_columns("db", "dev", "t") == {"columns": [], "types": {}}


# ---------------------------------------------------------------------------
# api_inspect — redis happy path + non-redis rejection
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_api_inspect_redis_happy_path(isolated_cache, monkeypatch):
    gui = isolated_cache
    monkeypatch.setattr(gui, "_resolve", lambda db, env: _Conn())
    monkeypatch.setattr(gui.core, "connection_engine", lambda c: "redis")
    monkeypatch.setattr(gui.tunnel, "open_tunnel", _fake_tunnel("REDIS_URL"))
    rows = [{"field": "a", "value": "1"}, {"field": "b", "value": "2"}]
    seen = {}
    monkeypatch.setattr(gui.redis_engine, "inspect_key",
                        lambda url, key: seen.update(url=url, key=key) or rows)

    out = gui.api_inspect("r", "dev", "user:1")
    assert seen == {"url": "REDIS_URL", "key": "user:1"}
    assert out["rows"] == rows
    assert out["rowCount"] == 2
    assert out["engine"] == "redis"
    assert out["truncated"] is False
    assert out["sql"] == "# inspect user:1"
    assert out["columns"] == [{"name": "field", "type": None}, {"name": "value", "type": None}]


@pytest.mark.unit
def test_api_inspect_missing_key_raises(isolated_cache):
    from quarry.core import QuarryError

    with pytest.raises(QuarryError, match="requires a 'key'"):
        isolated_cache.api_inspect("r", "dev", "")


@pytest.mark.unit
def test_api_inspect_non_redis_raises(isolated_cache, monkeypatch):
    from quarry.core import QuarryError

    gui = isolated_cache
    monkeypatch.setattr(gui, "_resolve", lambda db, env: _Conn())
    monkeypatch.setattr(gui.core, "connection_engine", lambda c: "postgres")
    with pytest.raises(QuarryError, match="redis-only"):
        gui.api_inspect("p", "dev", "somekey")


# ---------------------------------------------------------------------------
# _list_tables — mysql / neptune / redis / postgres branches
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_list_tables_mysql(isolated_cache, monkeypatch):
    gui = isolated_cache
    monkeypatch.setattr(gui.core, "connection_engine", lambda c: "mysql")
    captured = {}

    def fake_run_query(conn, sql, max_rows=5000, **kwargs):
        captured["sql"] = sql
        return _Res([{"table_name": "t1"}, {"table_name": None}, {"table_name": "t2"}])

    monkeypatch.setattr(gui.core, "run_query", fake_run_query)
    assert gui.core._list_tables(_Conn()) == ["t1", "t2"]     # None filtered
    assert "DATABASE()" in captured["sql"]


@pytest.mark.unit
def test_list_tables_postgres(isolated_cache, monkeypatch):
    gui = isolated_cache
    monkeypatch.setattr(gui.core, "connection_engine", lambda c: "postgres")
    captured = {}

    def fake_run_query(conn, sql, max_rows=5000, **kwargs):
        captured["sql"] = sql
        return _Res([{"table_name": "customers"},
                     {"table_name": "app_metadata.app_metadata_app"}])

    monkeypatch.setattr(gui.core, "run_query", fake_run_query)
    assert gui.core._list_tables(_Conn()) == ["customers", "app_metadata.app_metadata_app"]
    assert "quote_ident(table_schema)" in captured["sql"]
    assert "information_schema" in captured["sql"]


@pytest.mark.unit
def test_list_tables_neptune_is_empty_no_query(isolated_cache, monkeypatch):
    gui = isolated_cache
    monkeypatch.setattr(gui.core, "connection_engine", lambda c: "neptune")
    monkeypatch.setattr(gui.core, "run_query",
                        lambda *a, **k: pytest.fail("neptune must not run a query"))
    assert gui.core._list_tables(_Conn()) == []


@pytest.mark.unit
def test_list_tables_redis_scans_keys(isolated_cache, monkeypatch):
    gui = isolated_cache
    monkeypatch.setattr(gui.core, "connection_engine", lambda c: "redis")
    monkeypatch.setattr(gui.tunnel, "open_tunnel", _fake_tunnel("RURL"))
    seen = {}
    monkeypatch.setattr(gui.redis_engine, "scan_keys",
                        lambda url, count=1000: seen.update(url=url, count=count) or ["k1", "k2"])
    assert gui.core._list_tables(_Conn()) == ["k1", "k2"]
    assert seen == {"url": "RURL", "count": 1000}


# ---------------------------------------------------------------------------
# _resolve delegates to core.resolve_connection
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_resolve_delegates(isolated_cache, monkeypatch):
    gui = isolated_cache
    calls = []
    sentinel = _Conn()
    monkeypatch.setattr(gui.core, "resolve_connection",
                        lambda db, env: calls.append((db, env)) or sentinel)
    assert gui._resolve("mydb", "prod") is sentinel
    assert calls == [("mydb", "prod")]


# ---------------------------------------------------------------------------
# _display_path
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_display_path(monkeypatch, tmp_path):
    from pathlib import Path

    from quarry import gui

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert gui._display_path(tmp_path / "proj" / "ws") == "~/proj/ws"
    assert gui._display_path(tmp_path) == "~"                    # exactly home
    assert gui._display_path("/somewhere/else") == "/somewhere/else"


# ---------------------------------------------------------------------------
# port management: _port_pids / _is_quarry_gui / _reclaim_port / _next_free_port
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_port_pids_parses_lsof(monkeypatch):
    from quarry import gui

    def fake_run(cmd, **kw):
        assert cmd[0] == "lsof"
        return subprocess.CompletedProcess(cmd, 0, stdout="123\n456\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert gui._port_pids(8765) == [123, 456]


@pytest.mark.unit
def test_port_pids_swallows_errors(monkeypatch):
    from quarry import gui

    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("lsof missing")))
    assert gui._port_pids(8765) == []


@pytest.mark.unit
def test_is_quarry_gui_ours_vs_foreign(monkeypatch):
    from quarry import gui

    table = {
        "10": "python -m quarry.gui --port 8765",   # ours (-m quarry + gui)
        "11": "/usr/local/bin/qy gui",              # ours (/qy + gui)
        "12": "node server.js gui",                 # foreign (no quarry marker)
        "13": "postgres: writer process",           # foreign (no 'gui')
    }

    def fake_run(cmd, **kw):
        pid = cmd[cmd.index("-p") + 1]
        return subprocess.CompletedProcess(cmd, 0, stdout=table.get(pid, ""), stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert gui._is_quarry_gui(10) is True
    assert gui._is_quarry_gui(11) is True
    assert gui._is_quarry_gui(12) is False
    assert gui._is_quarry_gui(13) is False


@pytest.mark.unit
def test_is_quarry_gui_swallows_errors(monkeypatch):
    from quarry import gui

    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("ps missing")))
    assert gui._is_quarry_gui(1) is False


@pytest.mark.unit
def test_reclaim_port_kills_only_ours(monkeypatch):
    """Only a pid that is ours AND not our own process gets SIGTERM'd."""
    from quarry import gui

    monkeypatch.setattr(gui, "_port_pids", lambda port: [111, 222, os.getpid()])
    # 111 is ours (kill), 222 is foreign (skip), our own pid is skipped by the guard.
    monkeypatch.setattr(gui, "_is_quarry_gui", lambda pid: pid == 111)
    monkeypatch.setattr(gui.time, "sleep", lambda s: None)
    killed = []
    monkeypatch.setattr(gui.os, "kill", lambda pid, sig: killed.append((pid, sig)))

    assert gui._reclaim_port(8765) is True
    assert killed == [(111, signal.SIGTERM)]             # ONLY 111


@pytest.mark.unit
def test_reclaim_port_nothing_to_kill(monkeypatch):
    from quarry import gui

    monkeypatch.setattr(gui, "_port_pids", lambda port: [999])
    monkeypatch.setattr(gui, "_is_quarry_gui", lambda pid: False)   # all foreign
    monkeypatch.setattr(gui.os, "kill",
                        lambda *a: pytest.fail("must not kill a foreign pid"))
    assert gui._reclaim_port(8765) is False


@pytest.mark.unit
def test_reclaim_port_kill_failure_is_swallowed(monkeypatch):
    from quarry import gui

    monkeypatch.setattr(gui, "_port_pids", lambda port: [111])
    monkeypatch.setattr(gui, "_is_quarry_gui", lambda pid: True)
    monkeypatch.setattr(gui.os, "kill",
                        lambda *a: (_ for _ in ()).throw(ProcessLookupError()))
    # killed stays False because os.kill raised -> no sleep, returns False.
    monkeypatch.setattr(gui.time, "sleep",
                        lambda s: pytest.fail("should not sleep when nothing killed"))
    assert gui._reclaim_port(8765) is False


@pytest.mark.unit
def test_next_free_port_finds_open(monkeypatch):
    from quarry import gui

    class FakeSock:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def bind(self, addr):
            host, port = addr
            if port < 9003:                 # first two candidates "in use"
                raise OSError("in use")
            # else succeeds

    monkeypatch.setattr(gui.socket, "socket", lambda *a, **k: FakeSock())
    assert gui._next_free_port("127.0.0.1", 9000) == 9003


@pytest.mark.unit
def test_next_free_port_exhausted_returns_start(monkeypatch):
    from quarry import gui

    class AlwaysBusy:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def bind(self, addr):
            raise OSError("in use")

    monkeypatch.setattr(gui.socket, "socket", lambda *a, **k: AlwaysBusy())
    assert gui._next_free_port("127.0.0.1", 8000, tries=5) == 8000


# ---------------------------------------------------------------------------
# _bind — clean bind, reclaim-and-takeover, foreign->next-free, re-raise
# ---------------------------------------------------------------------------

class _FakeServer:
    """Records the (host, port) it was constructed with."""

    instances = []

    def __init__(self, addr, handler):
        self.addr = addr
        self.handler = handler
        _FakeServer.instances.append(self)


@pytest.mark.unit
def test_bind_clean(monkeypatch):
    from quarry import gui

    _FakeServer.instances = []
    monkeypatch.setattr(gui, "ThreadingHTTPServer", _FakeServer)
    server, port = gui._bind("127.0.0.1", 8765)
    assert port == 8765
    assert isinstance(server, _FakeServer) and server.addr == ("127.0.0.1", 8765)


@pytest.mark.unit
def test_bind_reraises_non_addrinuse(monkeypatch):
    from quarry import gui

    def ctor(addr, handler):
        raise OSError(errno.EACCES, "permission denied")   # not 48/98

    monkeypatch.setattr(gui, "ThreadingHTTPServer", ctor)
    with pytest.raises(OSError) as ei:
        gui._bind("127.0.0.1", 80)
    assert ei.value.errno == errno.EACCES


@pytest.mark.unit
def test_bind_reclaim_and_takeover(monkeypatch, capsys):
    """errno 48 once, then _reclaim_port True -> rebind SAME port and take over."""
    from quarry import gui

    _FakeServer.instances = []
    state = {"raised": False}

    def ctor(addr, handler):
        if not state["raised"]:
            state["raised"] = True
            raise OSError(48, "address already in use")
        return _FakeServer(addr, handler)

    monkeypatch.setattr(gui, "ThreadingHTTPServer", ctor)
    monkeypatch.setattr(gui, "_reclaim_port", lambda port: True)
    monkeypatch.setattr(gui, "_next_free_port",
                        lambda *a, **k: pytest.fail("must not move ports when we reclaimed"))

    server, port = gui._bind("127.0.0.1", 8765)
    assert port == 8765                                   # SAME port
    assert server.addr == ("127.0.0.1", 8765)
    assert "took over" in capsys.readouterr().out


@pytest.mark.unit
def test_bind_foreign_moves_to_next_free(monkeypatch, capsys):
    """errno 98 once, _reclaim_port False (foreign) -> bind next free port."""
    from quarry import gui

    _FakeServer.instances = []
    state = {"raised": False}

    def ctor(addr, handler):
        if not state["raised"]:
            state["raised"] = True
            raise OSError(98, "address already in use")   # linux EADDRINUSE
        return _FakeServer(addr, handler)

    monkeypatch.setattr(gui, "ThreadingHTTPServer", ctor)
    monkeypatch.setattr(gui, "_reclaim_port", lambda port: False)
    monkeypatch.setattr(gui, "_next_free_port", lambda host, start, tries=30: 8899)

    server, port = gui._bind("127.0.0.1", 8765)
    assert port == 8899                                   # moved
    assert server.addr == ("127.0.0.1", 8899)
    assert "not Quarry" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# a couple of in-process integration checks against the real Postgres
# (prove the mocked branches match real behavior end-to-end, coverage counts)
# ---------------------------------------------------------------------------

@requires_db
@pytest.mark.integration
def test_api_health_real_postgres_ok(ws, isolated_cache):
    out = isolated_cache.api_health("testpg", "test", fresh=True)
    assert out == {"ok": True}
    # and it was cached with a timestamp
    stored = isolated_cache.cache.get("health:testpg@test")
    assert stored["ok"] is True and "_ts" in stored


@requires_db
@pytest.mark.integration
def test_list_tables_real_postgres(ws, isolated_cache):
    conn = isolated_cache._resolve("testpg", "test")
    tables = isolated_cache.core._list_tables(conn)
    assert "customers" in tables and "orders" in tables


@requires_db
@pytest.mark.integration
def test_list_and_describe_non_public_postgres_table(ws, isolated_cache, pg_exec):
    pg_exec("DROP SCHEMA IF EXISTS qy_multischema CASCADE")
    rc, _, err = pg_exec("CREATE SCHEMA qy_multischema")
    assert rc == 0, err
    rc, _, err = pg_exec("CREATE TABLE qy_multischema.events (id integer, label text)")
    assert rc == 0, err
    try:
        conn = isolated_cache._resolve("testpg", "test")
        assert "qy_multischema.events" in isolated_cache.core._list_tables(conn)
        out = isolated_cache.api_columns("testpg", "test", "qy_multischema.events")
        assert out["columns"] == ["id", "label"]
        assert out["types"] == {"id": "integer", "label": "text"}
    finally:
        pg_exec("DROP SCHEMA IF EXISTS qy_multischema CASCADE")


@requires_db
@pytest.mark.integration
def test_api_columns_real_postgres(ws, isolated_cache):
    out = isolated_cache.api_columns("testpg", "test", "customers")
    assert "id" in out["columns"] and "email" in out["columns"]
    assert out["types"]["id"] == "integer" and out["types"]["email"] == "text"
    # second call served from cache -> identical
    assert isolated_cache.api_columns("testpg", "test", "customers") == out


@requires_db
@pytest.mark.integration
def test_api_columns_quoted_special_char_table_name(ws, isolated_cache, pg_exec):
    """A table whose name needs quoting (dash + space) must still resolve its
    columns — regression for the sanitizer silently stripping such names to a
    non-existent identifier and returning an empty schema."""
    pg_exec('DROP TABLE IF EXISTS "qy-review weird"')
    rc, _, err = pg_exec('CREATE TABLE "qy-review weird" (id serial PRIMARY KEY, note text)')
    assert rc == 0, err
    try:
        assert "qy-review weird" in isolated_cache.core._list_tables(
            isolated_cache._resolve("testpg", "test"))
        out = isolated_cache.api_columns("testpg", "test", "qy-review weird")
        assert out["columns"] == ["id", "note"]
        assert out["types"] == {"id": "integer", "note": "text"}
    finally:
        pg_exec('DROP TABLE IF EXISTS "qy-review weird"')


# ---------------------------------------------------------------------------
# events: SSE framing, publish/subscribe, the workspace watcher
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_sse_format_frames_event_as_data_line():
    import json

    from quarry import gui

    raw = gui._sse_format({"type": "workspace_changed", "ts": 1.5})
    assert raw.startswith(b"data: ") and raw.endswith(b"\n\n")
    assert json.loads(raw[len(b"data: "):].decode()) == {"type": "workspace_changed", "ts": 1.5}


@pytest.mark.unit
def test_publish_event_reaches_subscribers_and_survives_full_queue():
    import queue

    from quarry import gui

    q = queue.Queue(maxsize=1)
    with gui._SUB_LOCK:
        gui._SUBSCRIBERS.add(q)
    try:
        gui.publish_event("workspace_changed")
        gui.publish_event("workspace_changed")  # queue full -> dropped, no raise
        evt = q.get_nowait()
        assert evt["type"] == "workspace_changed" and isinstance(evt["ts"], float)
        assert q.empty()
    finally:
        with gui._SUB_LOCK:
            gui._SUBSCRIBERS.discard(q)


@pytest.mark.unit
def test_apply_workspace_change_drops_health_cache_and_publishes(ws, isolated_cache):
    import queue

    from quarry import gui

    gui.cache.put("health:testpg@test", {"ok": True, "_ts": 1.0})
    gui.cache.put("tables:testpg@test", {"tables": ["t"], "engine": "postgres", "capped": False})
    q = queue.Queue()
    with gui._SUB_LOCK:
        gui._SUBSCRIBERS.add(q)
    try:
        gui._apply_workspace_change()
        assert gui.cache.get("health:testpg@test") is None          # probes may now lie
        assert gui.cache.get("tables:testpg@test") is not None      # table cache survives
        assert q.get_nowait()["type"] == "workspace_changed"
    finally:
        with gui._SUB_LOCK:
            gui._SUBSCRIBERS.discard(q)


@pytest.mark.unit
def test_watch_tick_fires_only_on_fingerprint_change(ws, isolated_cache, monkeypatch):
    import os as _os

    from quarry import gui

    calls = []
    monkeypatch.setattr(gui, "_apply_workspace_change", lambda: calls.append(1))
    fp = gui._ws_fingerprint()
    conn_file = ws / "connections.toml"
    assert str(conn_file) in fp                       # the workspace file is watched

    fp = gui._watch_tick(fp)
    assert calls == []                                # unchanged -> no-op

    st = conn_file.stat()
    _os.utime(conn_file, (st.st_atime, st.st_mtime + 5))
    fp2 = gui._watch_tick(fp)
    assert calls == [1] and fp2 != fp                 # mtime bump -> one apply

    (ws / "queries" / "new.sql").write_text("select 1", encoding="utf-8")
    assert len(gui._watch_tick(fp2)) == len(fp2) + 1  # new .sql file is picked up
    assert calls == [1, 1]


@pytest.mark.unit
def test_watch_tick_survives_apply_failure(ws, isolated_cache, monkeypatch):
    import os as _os

    from quarry import gui

    def boom():
        raise RuntimeError("bad config")
    monkeypatch.setattr(gui, "_apply_workspace_change", boom)
    fp = gui._ws_fingerprint()
    conn_file = ws / "connections.toml"
    st = conn_file.stat()
    _os.utime(conn_file, (st.st_atime, st.st_mtime + 5))
    assert gui._watch_tick(fp) != fp                  # no raise; fingerprint advances


@pytest.mark.unit
def test_ensure_watcher_starts_one_daemon_thread(monkeypatch):
    from quarry import gui

    started = []

    class FakeThread:
        def __init__(self, **kw):
            started.append(kw)

        def start(self):
            pass

    monkeypatch.setattr(gui.threading, "Thread", FakeThread)
    monkeypatch.setattr(gui, "_WATCHER_STARTED", False)
    gui._ensure_watcher()
    gui._ensure_watcher()
    assert len(started) == 1 and started[0]["daemon"] is True


# ---------------------------------------------------------------------------
# update check: PyPI polling — throttle, semver compare, disable/editable
# skips, silent network failure (see the module docstring in gui.py)
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_version_gt_compares_numeric_segments_not_strings():
    from quarry import gui

    assert gui._version_gt("0.10.0", "0.9.0") is True     # string compare would get this backwards
    assert gui._version_gt("0.9.0", "0.10.0") is False
    assert gui._version_gt("1.0.0", "1.0.0") is False
    assert gui._version_gt("2.0.0", "1.99.99") is True


@pytest.mark.unit
def test_update_check_disabled_env_var(monkeypatch):
    from quarry import gui

    monkeypatch.delenv("QUARRY_UPDATE_CHECK", raising=False)
    assert gui._update_check_disabled() is False
    monkeypatch.setenv("QUARRY_UPDATE_CHECK", "0")
    assert gui._update_check_disabled() is True


@pytest.mark.unit
def test_check_for_update_skips_fetch_when_disabled(isolated_cache, monkeypatch):
    gui = isolated_cache
    monkeypatch.setenv("QUARRY_UPDATE_CHECK", "0")
    calls = []
    monkeypatch.setattr(gui, "_fetch_latest_version", lambda: calls.append(1) or "9.9.9")
    gui._check_for_update()
    assert calls == []
    assert gui.cache.get(gui._UPDATE_CACHE_KEY) is None


@pytest.mark.unit
def test_check_for_update_skips_fetch_when_editable_install(isolated_cache, monkeypatch):
    gui = isolated_cache
    monkeypatch.delenv("QUARRY_UPDATE_CHECK", raising=False)
    monkeypatch.setattr(gui, "_is_editable_install", lambda: True)
    calls = []
    monkeypatch.setattr(gui, "_fetch_latest_version", lambda: calls.append(1) or "9.9.9")
    gui._check_for_update()
    assert calls == []
    assert gui.cache.get(gui._UPDATE_CACHE_KEY) is None


@pytest.mark.unit
def test_check_for_update_throttles_within_24h_window(isolated_cache, monkeypatch):
    gui = isolated_cache
    monkeypatch.delenv("QUARRY_UPDATE_CHECK", raising=False)
    monkeypatch.setattr(gui, "_is_editable_install", lambda: False)
    calls = []
    monkeypatch.setattr(gui, "_fetch_latest_version", lambda: calls.append(1) or "9.9.9")

    gui._check_for_update()
    assert len(calls) == 1
    gui._check_for_update()  # still inside the interval -> no second HTTP call
    assert len(calls) == 1

    c = gui.cache.get(gui._UPDATE_CACHE_KEY)
    assert c["latest"] == "9.9.9" and c["available"] is True


@pytest.mark.unit
def test_check_for_update_force_bypasses_throttle(isolated_cache, monkeypatch):
    gui = isolated_cache
    monkeypatch.setattr(gui, "_is_editable_install", lambda: False)
    calls = []
    monkeypatch.setattr(gui, "_fetch_latest_version", lambda: calls.append(1) or "0.0.1")

    gui._check_for_update()
    gui._check_for_update(force=True)
    assert len(calls) == 2


@pytest.mark.unit
def test_check_for_update_publishes_event_only_when_newer(isolated_cache, monkeypatch):
    import queue

    gui = isolated_cache
    monkeypatch.setattr(gui, "_is_editable_install", lambda: False)
    monkeypatch.setattr(gui, "_fetch_latest_version", lambda: gui.__version__)  # already current
    q = queue.Queue()
    with gui._SUB_LOCK:
        gui._SUBSCRIBERS.add(q)
    try:
        gui._check_for_update()
        assert q.empty()  # not newer -> no update_available event
        c = gui.cache.get(gui._UPDATE_CACHE_KEY)
        assert c["available"] is False
    finally:
        with gui._SUB_LOCK:
            gui._SUBSCRIBERS.discard(q)


@pytest.mark.unit
def test_fetch_latest_version_network_failure_returns_none(monkeypatch):
    from quarry import gui

    def boom(req, timeout=5.0):
        raise OSError("network down")

    monkeypatch.setattr(gui, "urlopen", boom)
    assert gui._fetch_latest_version() is None


@pytest.mark.unit
def test_check_for_update_network_failure_is_silent(isolated_cache, monkeypatch):
    """A PyPI outage must never raise or leave a stale 'available' flag —
    only checked_at advances, so the throttle still resets the retry window."""
    gui = isolated_cache
    monkeypatch.setattr(gui, "_is_editable_install", lambda: False)
    monkeypatch.setattr(gui, "_fetch_latest_version", lambda: None)

    gui._check_for_update()  # must not raise

    c = gui.cache.get(gui._UPDATE_CACHE_KEY)
    assert c is not None and "checked_at" in c and "latest" not in c


@pytest.mark.unit
def test_fetch_latest_version_parses_pypi_json(monkeypatch):
    from quarry import gui

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"info": {"version": "1.2.3"}}'

    monkeypatch.setattr(gui, "urlopen", lambda req, timeout=5.0: _Resp())
    assert gui._fetch_latest_version() == "1.2.3"


@pytest.mark.unit
def test_is_editable_install_true_when_direct_url_flags_editable(monkeypatch):
    import json as _json

    from quarry import gui

    class _Dist:
        def read_text(self, name):
            assert name == "direct_url.json"
            return _json.dumps({"dir_info": {"editable": True}})

    monkeypatch.setattr("importlib.metadata.distribution", lambda name: _Dist())
    assert gui._is_editable_install() is True


@pytest.mark.unit
def test_is_editable_install_false_for_normal_pypi_install(monkeypatch):
    from quarry import gui

    class _Dist:
        def read_text(self, name):
            return None  # no direct_url.json -> a regular (non-editable) install

    monkeypatch.setattr("importlib.metadata.distribution", lambda name: _Dist())
    assert gui._is_editable_install() is False


@pytest.mark.unit
def test_is_editable_install_swallows_lookup_errors(monkeypatch):
    from quarry import gui

    def boom(name):
        raise ModuleNotFoundError("not installed")

    monkeypatch.setattr("importlib.metadata.distribution", boom)
    assert gui._is_editable_install() is False


@pytest.mark.unit
def test_api_update_reads_cache_without_triggering_a_check(isolated_cache, monkeypatch):
    gui = isolated_cache
    monkeypatch.delenv("QUARRY_UPDATE_CHECK", raising=False)
    monkeypatch.setattr(gui, "_is_editable_install", lambda: False)
    assert gui.api_update() == {"current": gui.__version__, "latest": None, "available": False}

    gui.cache.put(gui._UPDATE_CACHE_KEY, {"checked_at": 1.0, "latest": "9.9.9", "available": True})
    assert gui.api_update() == {"current": gui.__version__, "latest": "9.9.9", "available": True}


@pytest.mark.unit
def test_api_update_recomputes_availability_after_upgrade(isolated_cache, monkeypatch):
    """A cache written while running an older version (available=True) must
    not keep showing the badge once the current process is already on
    `latest` — the flag has to be re-derived from `latest` vs __version__,
    never trusted as-is."""
    gui = isolated_cache
    monkeypatch.delenv("QUARRY_UPDATE_CHECK", raising=False)
    monkeypatch.setattr(gui, "_is_editable_install", lambda: False)
    gui.cache.put(
        gui._UPDATE_CACHE_KEY,
        {"checked_at": 1.0, "latest": gui.__version__, "available": True},
    )
    assert gui.api_update() == {"current": gui.__version__, "latest": gui.__version__, "available": False}


@pytest.mark.unit
def test_api_update_returns_unavailable_when_disabled(isolated_cache, monkeypatch):
    gui = isolated_cache
    monkeypatch.setenv("QUARRY_UPDATE_CHECK", "0")
    gui.cache.put(gui._UPDATE_CACHE_KEY, {"checked_at": 1.0, "latest": "9.9.9", "available": True})
    assert gui.api_update() == {"current": gui.__version__, "latest": None, "available": False}


@pytest.mark.unit
def test_api_update_returns_unavailable_for_editable_install(isolated_cache, monkeypatch):
    gui = isolated_cache
    monkeypatch.setattr(gui, "_is_editable_install", lambda: True)
    gui.cache.put(gui._UPDATE_CACHE_KEY, {"checked_at": 1.0, "latest": "9.9.9", "available": True})
    assert gui.api_update() == {"current": gui.__version__, "latest": None, "available": False}


@pytest.mark.unit
def test_ensure_update_checker_starts_one_daemon_thread(monkeypatch):
    from quarry import gui

    started = []

    class FakeThread:
        def __init__(self, **kw):
            started.append(kw)

        def start(self):
            pass

    monkeypatch.setattr(gui.threading, "Thread", FakeThread)
    monkeypatch.setattr(gui, "_UPDATE_CHECKER_STARTED", False)
    gui._ensure_update_checker()
    gui._ensure_update_checker()
    assert len(started) == 1 and started[0]["daemon"] is True


# ---------------------------------------------------------------------------
# What's New: CHANGELOG.md parsing (GET /api/changelog) — see the module
# docstring above `_parse_changelog` in gui.py
# ---------------------------------------------------------------------------

_SAMPLE_CHANGELOG = """\
# Changelog

## [Unreleased]

### Added

- Some in-progress work not yet released — must not appear anywhere.

<!-- version list -->

## v0.6.0 (2026-07-16)

### Features

- **gui**: What's New panel shows changelog entries after an upgrade
  ([#80](https://github.com/Wangggym/quarry/pull/80), [`abc1234`](https://github.com/Wangggym/quarry/commit/abc1234))

### Bug Fixes

- Fix a thing (#48 note) ([#81](https://github.com/Wangggym/quarry/pull/81),
  [`def5678`](https://github.com/Wangggym/quarry/commit/def5678))

## v0.5.1 (2026-07-15)

### Bug Fixes

- **release**: __version__ 常量纳入 semantic-release 同步
  ([`89d330b`](https://github.com/Wangggym/quarry/commit/89d330bb2aaf25640d678145e412217259f95ee6))

## [0.2.2] — 2026-07-02

### Fixed

- Legacy hand-written heading format still parses
  ([`aaaaaaa`](https://github.com/Wangggym/quarry/commit/aaaaaaa))
"""


@pytest.mark.unit
def test_parse_changelog_multiple_versions_with_dates_and_entries():
    from quarry import gui

    versions = gui._parse_changelog(_SAMPLE_CHANGELOG)
    assert [v["version"] for v in versions] == ["0.6.0", "0.5.1", "0.2.2"]
    assert [v["date"] for v in versions] == ["2026-07-16", "2026-07-15", "2026-07-02"]

    assert versions[0]["entries"] == [
        "gui: What's New panel shows changelog entries after an upgrade",
        "Fix a thing (#48 note)",
    ]
    assert versions[1]["entries"] == ["release: __version__ 常量纳入 semantic-release 同步"]
    assert versions[2]["entries"] == ["Legacy hand-written heading format still parses"]


@pytest.mark.unit
def test_parse_changelog_skips_unreleased_section():
    from quarry import gui

    versions = gui._parse_changelog(_SAMPLE_CHANGELOG)
    all_entries = " ".join(e for v in versions for e in v["entries"])
    assert "in-progress work" not in all_entries


@pytest.mark.unit
def test_parse_changelog_empty_or_headerless_text_returns_empty_list():
    from quarry import gui

    assert gui._parse_changelog("") == []
    assert gui._parse_changelog("# Changelog\n\nnothing here\n") == []


@pytest.mark.unit
def test_parse_changelog_caps_at_max_versions(monkeypatch):
    from quarry import gui

    monkeypatch.setattr(gui, "CHANGELOG_MAX_VERSIONS", 2)
    versions = gui._parse_changelog(_SAMPLE_CHANGELOG)
    assert [v["version"] for v in versions] == ["0.6.0", "0.5.1"]


@pytest.mark.unit
def test_api_changelog_returns_empty_list_when_file_missing(monkeypatch, tmp_path):
    from quarry import gui

    monkeypatch.setattr(gui, "_changelog_path", lambda: tmp_path / "does-not-exist.md")
    assert gui.api_changelog() == []


@pytest.mark.unit
def test_api_changelog_reads_and_parses_from_changelog_path(monkeypatch, tmp_path):
    from quarry import gui

    f = tmp_path / "CHANGELOG.md"
    f.write_text(_SAMPLE_CHANGELOG, encoding="utf-8")
    monkeypatch.setattr(gui, "_changelog_path", lambda: f)
    versions = gui.api_changelog()
    assert versions[0]["version"] == "0.6.0"


@pytest.mark.unit
def test_changelog_path_prefers_bundled_copy_next_to_gui_py(monkeypatch, tmp_path):
    """Installed-wheel layout: CHANGELOG.md sits next to gui.py, via the
    build hook in hatch_build.py."""
    from quarry import gui

    bundled = tmp_path / "CHANGELOG.md"
    bundled.write_text("# Changelog\n", encoding="utf-8")
    monkeypatch.setattr(gui, "__file__", str(tmp_path / "gui.py"))
    assert gui._changelog_path() == bundled


@pytest.mark.unit
def test_changelog_ships_in_standard_wheels_only():
    """The CHANGELOG bundling must not apply to editable wheels: a bundled
    quarry/CHANGELOG.md materializes a real site-packages/quarry/ directory
    (a namespace package) that shadows the editable install's redirect to the
    source tree, breaking every `quarry.*` import (see hatch_build.py)."""
    import importlib.util

    pytest.importorskip("hatchling")
    from conftest import REPO

    spec = importlib.util.spec_from_file_location("hatch_build", REPO / "hatch_build.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    hook = mod.ChangelogBuildHook.__new__(mod.ChangelogBuildHook)

    build_data = {"force_include": {}}
    hook.initialize("editable", build_data)
    assert build_data["force_include"] == {}
    hook.initialize("standard", build_data)
    assert build_data["force_include"] == {"CHANGELOG.md": "quarry/CHANGELOG.md"}


@pytest.mark.unit
def test_changelog_path_falls_back_to_repo_root_for_editable_installs(monkeypatch, tmp_path):
    """Editable/source-checkout layout: no CHANGELOG.md next to gui.py, so
    fall back two levels up (src/quarry/gui.py -> repo root)."""
    from quarry import gui

    src_quarry = tmp_path / "src" / "quarry"
    src_quarry.mkdir(parents=True)
    monkeypatch.setattr(gui, "__file__", str(src_quarry / "gui.py"))
    assert gui._changelog_path() == tmp_path / "CHANGELOG.md"
