"""In-process tests for the Quarry MCP face (quarry.mcp).

Everything here drives the MCP server functions directly (no `qy mcp` subprocess)
so coverage counts. DB-backed tests use the process-wide workspace configured by
the `ws` fixture (connection key 'testpg' -> local Postgres 'quarry_test').

The `_ALLOW_WRITE_FLAG` module global is toggled by several tests; each of those
saves and restores it so ordering never leaks state to other tests.
"""

from __future__ import annotations

import io
import json

import pytest

from conftest import TEST_DB_URL, requires_db
from quarry import core, mcp, workspace


# ---------------------------------------------------------------------------
# _s schema builder + _tools_payload  (pure)
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_s_builds_object_schema_with_no_required():
    schema = mcp._s()
    assert schema == {"type": "object", "properties": {}, "required": []}


@pytest.mark.unit
def test_s_computes_required_from_req_and_pops_marker():
    schema = mcp._s(
        db={"type": "string", "_req": True},
        env={"type": "string"},
        table={"type": "string", "_req": True},
    )
    assert schema["type"] == "object"
    assert set(schema["required"]) == {"db", "table"}
    assert "env" not in schema["required"]
    # the _req marker must be stripped out of the emitted property schema
    for prop in schema["properties"].values():
        assert "_req" not in prop


@pytest.mark.unit
def test_tools_payload_shape_and_required_fields():
    payload = mcp._tools_payload()
    assert len(payload) == 6
    names = [t["name"] for t in payload]
    assert names == [
        "list_connections", "list_tables", "describe_table",
        "exec_sql", "list_saved_queries", "run_saved_query",
    ]
    for t in payload:
        # only the protocol-facing keys are exposed; the internal "fn" is not
        assert set(t.keys()) == {"name", "description", "inputSchema"}
        assert "fn" not in t
        assert t["inputSchema"]["type"] == "object"
    by_name = {t["name"]: t for t in payload}
    assert by_name["exec_sql"]["inputSchema"]["required"] == ["db", "sql"]
    assert by_name["describe_table"]["inputSchema"]["required"] == ["db", "table"]
    assert by_name["run_saved_query"]["inputSchema"]["required"] == ["name"]
    assert by_name["list_connections"]["inputSchema"]["required"] == []


# ---------------------------------------------------------------------------
# _dispatch protocol methods  (pure — no DB needed)
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_dispatch_initialize_echoes_client_protocol_version():
    res = mcp._dispatch("initialize", {"protocolVersion": "1999-01-01"})
    assert res["protocolVersion"] == "1999-01-01"
    assert res["capabilities"] == {"tools": {}}
    assert res["serverInfo"]["name"] == "quarry"
    assert "version" in res["serverInfo"]


@pytest.mark.unit
def test_dispatch_initialize_defaults_protocol_version_when_absent():
    res = mcp._dispatch("initialize", {})
    assert res["protocolVersion"] == mcp.PROTOCOL_VERSION


@pytest.mark.unit
def test_dispatch_ping_returns_empty():
    assert mcp._dispatch("ping", {}) == {}


@pytest.mark.unit
def test_dispatch_tools_list_returns_six_tools():
    res = mcp._dispatch("tools/list", {})
    assert len(res["tools"]) == 6
    assert res["tools"] == mcp._tools_payload()


@pytest.mark.unit
def test_dispatch_resources_and_prompts_lists_are_empty():
    assert mcp._dispatch("resources/list", {}) == {"resources": []}
    assert mcp._dispatch("resources/templates/list", {}) == {"resourceTemplates": []}
    assert mcp._dispatch("prompts/list", {}) == {"prompts": []}


@pytest.mark.unit
def test_dispatch_notifications_return_none():
    assert mcp._dispatch("notifications/initialized", {}) is None
    assert mcp._dispatch("notifications/cancelled", {"requestId": 1}) is None


@pytest.mark.unit
def test_dispatch_unknown_method_raises_method_not_found():
    with pytest.raises(core.QuarryError) as ei:
        mcp._dispatch("does/not/exist", {})
    assert str(ei.value).startswith("method not found")


# ---------------------------------------------------------------------------
# _handle_tools_call — argument validation / unknown tool  (pure)
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_tools_call_unknown_tool_raises_quarryerror():
    with pytest.raises(core.QuarryError) as ei:
        mcp._handle_tools_call({"name": "nope", "arguments": {}})
    assert "unknown tool: nope" in str(ei.value)


@pytest.mark.unit
def test_tools_call_missing_required_arg_is_tool_error():
    # exec_sql requires db + sql; supply neither -> isError, no crash
    res = mcp._handle_tools_call({"name": "exec_sql", "arguments": {}})
    assert res["isError"] is True
    payload = json.loads(res["content"][0]["text"])
    assert "missing required argument" in payload["error"]
    assert "db" in payload["error"] and "sql" in payload["error"]
    assert payload["code"] == core.EXIT_USAGE


@pytest.mark.unit
def test_tools_call_empty_string_counts_as_missing():
    # an empty-string required arg is treated as missing, not as a valid value
    res = mcp._handle_tools_call(
        {"name": "describe_table", "arguments": {"db": "x", "table": ""}})
    assert res["isError"] is True
    payload = json.loads(res["content"][0]["text"])
    assert "table" in payload["error"]


@pytest.mark.unit
def test_tools_call_non_quarry_error_becomes_tool_error_not_crash(monkeypatch):
    """A tool fn raising a plain Exception is surfaced as isError, never a crash."""
    boom = next(t for t in mcp.TOOLS if t["name"] == "list_connections")
    monkeypatch.setitem(
        boom, "fn", lambda a: (_ for _ in ()).throw(ValueError("kaboom")))
    res = mcp._handle_tools_call({"name": "list_connections", "arguments": {}})
    assert res["isError"] is True
    payload = json.loads(res["content"][0]["text"])
    assert payload["error"] == "ValueError: kaboom"
    assert payload["code"] is None


# ---------------------------------------------------------------------------
# _handle_tools_call — tool happy paths against the real DB
# ---------------------------------------------------------------------------

def _call(name, args=None):
    """Invoke a tool through the dispatcher; return (payload_dict, isError)."""
    res = mcp._handle_tools_call({"name": name, "arguments": args or {}})
    return json.loads(res["content"][0]["text"]), res["isError"]


@requires_db
@pytest.mark.integration
def test_tool_list_connections(ws):
    payload, is_err = _call("list_connections")
    assert is_err is False
    # our workspace defines exactly the 'testpg' connection
    keys = [env["key"]
            for grp in payload["groups"]
            for item in grp["items"]
            for env in item["envs"]]
    assert "testpg" in keys
    assert any(str(ws) == w for w in payload["workspaces"])


@requires_db
@pytest.mark.integration
def test_tool_list_tables(ws):
    payload, is_err = _call("list_tables", {"db": "testpg"})
    assert is_err is False
    assert payload["engine"] == "postgres"
    assert "customers" in payload["tables"]
    assert "orders" in payload["tables"]


@requires_db
@pytest.mark.integration
def test_tool_describe_table(ws):
    payload, is_err = _call("describe_table", {"db": "testpg", "table": "customers"})
    assert is_err is False
    assert payload["table"] == "customers"
    assert payload["engine"] == "postgres"
    cols = {c["column_name"] for c in payload["columns"]}
    assert {"id", "name", "email", "created_at"} <= cols


@requires_db
@pytest.mark.integration
def test_tool_exec_sql_read(ws):
    payload, is_err = _call(
        "exec_sql", {"db": "testpg", "sql": "SELECT 1 AS one, 2 AS two"})
    assert is_err is False
    assert payload["rows"] == [{"one": 1, "two": 2}]
    assert payload["rowCount"] == 1
    assert payload["engine"] == "postgres"
    # to_dict() shape
    assert set(payload) >= {"columns", "rows", "rowCount", "truncated",
                            "elapsedMs", "engine", "sql"}


@requires_db
@pytest.mark.integration
def test_tool_list_and_run_saved_query(ws):
    # seed a saved query in the active workspace's queries/ dir
    (ws / "queries" / "mcp_cnt.sql").write_text(
        "-- @name: mcp_cnt\n"
        "-- @db: testpg\n"
        "-- @desc: count customers\n"
        "SELECT count(*) AS n FROM customers\n",
        encoding="utf-8",
    )
    listed, is_err = _call("list_saved_queries")
    assert is_err is False
    names = [q["name"] for q in listed["queries"]]
    assert "mcp_cnt" in names
    entry = next(q for q in listed["queries"] if q["name"] == "mcp_cnt")
    assert entry["db"] == "testpg"
    assert entry["desc"] == "count customers"

    run, is_err = _call("run_saved_query", {"name": "mcp_cnt"})
    assert is_err is False
    assert run["rows"][0]["n"] >= 0
    assert run["rowCount"] == 1


@requires_db
@pytest.mark.integration
def test_tool_exec_sql_bad_sql_is_tool_error(ws):
    # a QuarryError from run_query (SQL error) is surfaced as isError, with code
    payload, is_err = _call(
        "exec_sql", {"db": "testpg", "sql": "SELECT * FROM mcp_no_such_table_xyz"})
    assert is_err is True
    assert "error" in payload and payload["code"] is not None


# ---------------------------------------------------------------------------
# engine-specific branches in tool_list_tables / tool_describe_table
# (mock connection resolution so no real redis/neptune is needed)
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_list_tables_neptune_returns_empty_table_list(monkeypatch):
    conn = core.Connection(key="nep", url="https://nep:8182", engine="neptune")
    monkeypatch.setattr(mcp.core, "resolve_connection", lambda db, env: conn)
    monkeypatch.setattr(mcp.core, "connection_engine", lambda c: "neptune")
    assert mcp.tool_list_tables("nep") == {"engine": "neptune", "tables": []}


@pytest.mark.unit
def test_list_tables_redis_projects_key_names_from_the_shared_cache(monkeypatch):
    # issue #97 review: redis now goes through the same core.cached_tables
    # entry the GUI populates (key/type/ttl dicts), rather than its own
    # separate, uncached scan — tool_list_tables just projects out the names.
    import contextlib

    from quarry import redis_engine  # the module core.py imports at call time

    conn = core.Connection(key="r", url="redis://x", engine="redis")
    monkeypatch.setattr(mcp.core, "resolve_connection", lambda db, env: conn)
    monkeypatch.setattr(mcp.core, "connection_engine", lambda c: "redis")

    @contextlib.contextmanager
    def fake_tunnel(c, engine, **kw):
        assert engine == "redis"
        yield "redis://tunneled"
    monkeypatch.setattr(mcp.core.tunnel, "open_tunnel", fake_tunnel)
    monkeypatch.setattr(
        redis_engine, "keys_with_meta",
        lambda url, cap=400: [{"key": "k1", "type": "string", "ttl": -1},
                              {"key": "k2", "type": "string", "ttl": -1}])

    assert mcp.tool_list_tables("r") == {"engine": "redis", "keys": ["k1", "k2"]}

    # a second call is served from the shared cache — no re-scan
    monkeypatch.setattr(
        redis_engine, "keys_with_meta",
        lambda *a, **k: pytest.fail("must not re-scan redis on a cache hit"))
    assert mcp.tool_list_tables("r") == {"engine": "redis", "keys": ["k1", "k2"]}


@pytest.mark.unit
def test_describe_table_unsupported_engine_raises(monkeypatch):
    conn = core.Connection(key="r", url="redis://x", engine="redis")
    monkeypatch.setattr(mcp.core, "resolve_connection", lambda db, env: conn)
    monkeypatch.setattr(mcp.core, "connection_engine", lambda c: "redis")
    with pytest.raises(core.QuarryError) as ei:
        mcp.tool_describe_table("r", "customers")
    assert "not supported for engine=redis" in str(ei.value)


@pytest.mark.unit
def test_describe_table_invalid_name_raises(monkeypatch):
    conn = core.Connection(key="pg", url="postgresql://x/y", engine="postgres")
    monkeypatch.setattr(mcp.core, "resolve_connection", lambda db, env: conn)
    monkeypatch.setattr(mcp.core, "connection_engine", lambda c: "postgres")
    # a name with no [A-Za-z0-9_$] survivors sanitizes to "" -> invalid
    with pytest.raises(core.QuarryError) as ei:
        mcp.tool_describe_table("pg", "!!!")
    assert "invalid table name" in str(ei.value)


# ---------------------------------------------------------------------------
# shared metadata cache (issue #97) — tool_list_tables / tool_describe_table
# go through core.cached_tables / core.cached_columns, so a repeat call for
# the same db@env(:table) is served from cache without a DB round trip.
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_list_tables_cache_hit_skips_the_db_query(monkeypatch):
    conn = core.Connection(key="pg", url="postgresql://x/y", engine="postgres")
    monkeypatch.setattr(mcp.core, "resolve_connection", lambda db, env: conn)
    monkeypatch.setattr(mcp.core, "connection_engine", lambda c: "postgres")
    calls = []

    def counting_run_query(conn, sql, **kwargs):
        calls.append(sql)
        return core.QueryResult(columns=[{"name": "table_name", "type": None}],
                                 rows=[{"table_name": "widgets"}], row_count=1,
                                 truncated=False, elapsed_ms=1, engine="postgres", sql=sql)

    monkeypatch.setattr(mcp.core, "run_query", counting_run_query)
    first = mcp.tool_list_tables("pg")
    assert first == {"engine": "postgres", "tables": ["widgets"]}
    assert len(calls) == 1

    monkeypatch.setattr(
        mcp.core, "run_query",
        lambda *a, **k: pytest.fail("must not query the DB on a cache hit"))
    second = mcp.tool_list_tables("pg")
    assert second == first
    assert len(calls) == 1


@pytest.mark.unit
def test_describe_table_cache_hit_skips_the_db_query(monkeypatch):
    conn = core.Connection(key="pg", url="postgresql://x/y", engine="postgres")
    monkeypatch.setattr(mcp.core, "resolve_connection", lambda db, env: conn)
    monkeypatch.setattr(mcp.core, "connection_engine", lambda c: "postgres")
    calls = []
    col = {"column_name": "id", "data_type": "int", "is_nullable": "NO",
           "column_default": None, "character_maximum_length": None}

    def counting_run_query(conn, sql, **kwargs):
        calls.append(sql)
        return core.QueryResult(columns=[{"name": k, "type": None} for k in col],
                                 rows=[col], row_count=1, truncated=False,
                                 elapsed_ms=1, engine="postgres", sql=sql)

    monkeypatch.setattr(mcp.core, "run_query", counting_run_query)
    first = mcp.tool_describe_table("pg", "widgets")
    assert first == {"table": "widgets", "engine": "postgres", "columns": [col]}
    assert len(calls) == 1

    monkeypatch.setattr(
        mcp.core, "run_query",
        lambda *a, **k: pytest.fail("must not query the DB on a cache hit"))
    second = mcp.tool_describe_table("pg", "widgets")
    assert second == first
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# write policy — _check_write_policy + exec_sql
# ---------------------------------------------------------------------------

@pytest.fixture()
def restore_write_flag():
    saved = mcp._ALLOW_WRITE_FLAG
    yield
    mcp._ALLOW_WRITE_FLAG = saved


@pytest.mark.unit
def test_check_write_policy_read_returns_false(restore_write_flag):
    mcp._ALLOW_WRITE_FLAG = True
    conn = core.Connection(key="k", url="postgresql://x/y", env="dev")
    assert mcp._check_write_policy(conn, write=False, confirm_prod=False) is False


@pytest.mark.unit
def test_check_write_policy_write_without_server_flag_blocks(restore_write_flag):
    mcp._ALLOW_WRITE_FLAG = False
    conn = core.Connection(key="k", url="postgresql://x/y", env="dev")
    with pytest.raises(core.QuarryError) as ei:
        mcp._check_write_policy(conn, write=True, confirm_prod=False)
    assert ei.value.exit_code == core.EXIT_SAFETY_BLOCKED
    assert "without --write" in str(ei.value)


@pytest.mark.unit
def test_check_write_policy_prod_needs_confirm(restore_write_flag):
    mcp._ALLOW_WRITE_FLAG = True
    conn = core.Connection(key="k", url="postgresql://x/y", env="prod", production=True)
    with pytest.raises(core.QuarryError) as ei:
        mcp._check_write_policy(conn, write=True, confirm_prod=False)
    assert ei.value.exit_code == core.EXIT_SAFETY_BLOCKED
    assert "prod" in str(ei.value)


@pytest.mark.unit
def test_check_write_policy_prod_with_confirm_allows(restore_write_flag):
    mcp._ALLOW_WRITE_FLAG = True
    conn = core.Connection(key="k", url="postgresql://x/y", env="prod", production=True)
    assert mcp._check_write_policy(conn, write=True, confirm_prod=True) is True


@pytest.mark.unit
def test_check_write_policy_dev_write_allows(restore_write_flag):
    mcp._ALLOW_WRITE_FLAG = True
    conn = core.Connection(key="k", url="postgresql://x/y", env="dev")
    assert mcp._check_write_policy(conn, write=True, confirm_prod=False) is True


@requires_db
@pytest.mark.integration
def test_exec_sql_write_without_server_flag_is_blocked(ws, restore_write_flag):
    mcp._ALLOW_WRITE_FLAG = False
    payload, is_err = _call(
        "exec_sql",
        {"db": "testpg", "sql": "CREATE TABLE mcp_tmp_never (id int)", "write": True})
    assert is_err is True
    assert payload["code"] == core.EXIT_SAFETY_BLOCKED
    assert "without --write" in payload["error"]


@requires_db
@pytest.mark.integration
def test_exec_sql_prod_env_blocked_without_confirm(wsdir, restore_write_flag):
    """A prod-env connection + write=True + no confirm_prod -> code 8 (blocked).

    Uses its own on-disk workspace with a prod connection, configured
    process-wide here and torn down after, so it doesn't disturb other tests.
    """
    (wsdir / "connections.toml").write_text(
        f'[prodpg]\nurl = "{TEST_DB_URL}"\nengine = "postgres"\nenv = "prod"\nproduction = true\n',
        encoding="utf-8",
    )
    workspace.configure_workspace(str(wsdir))
    mcp._ALLOW_WRITE_FLAG = True
    try:
        payload, is_err = _call(
            "exec_sql",
            {"db": "prodpg", "sql": "SELECT 1", "write": True})
        assert is_err is True
        assert payload["code"] == core.EXIT_SAFETY_BLOCKED
        assert "prod" in payload["error"]
    finally:
        workspace.configure_workspace(None)


@requires_db
@pytest.mark.integration
def test_exec_sql_prod_env_with_confirm_allowed(wsdir, restore_write_flag):
    """Prod env + write + confirm_prod passes the policy; we run a SELECT so
    nothing is actually written."""
    (wsdir / "connections.toml").write_text(
        f'[prodpg]\nurl = "{TEST_DB_URL}"\nengine = "postgres"\nenv = "prod"\nproduction = true\n',
        encoding="utf-8",
    )
    workspace.configure_workspace(str(wsdir))
    mcp._ALLOW_WRITE_FLAG = True
    try:
        payload, is_err = _call(
            "exec_sql",
            {"db": "prodpg", "sql": "SELECT 42 AS answer",
             "write": True, "confirm_prod": True})
        assert is_err is False
        assert payload["rows"] == [{"answer": 42}]
    finally:
        workspace.configure_workspace(None)


# ---------------------------------------------------------------------------
# serve() loop in-process
# ---------------------------------------------------------------------------

@requires_db
@pytest.mark.integration
def test_serve_loop_processes_stream(wsdir, monkeypatch, restore_write_flag):
    """Drive serve() with a fabricated stdin stream and capture stdout.

    The stream mixes: an initialize request, a notification (no id -> no reply),
    a tools/list request, a malformed JSON line (-> skipped, no reply), and an
    exec_sql request. We assert replies line up with request ids, that the
    notification and the malformed line produce NO reply, and the loop returns 0
    without raising.
    """
    lines = [
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {"protocolVersion": "2025-06-18"}}),
        json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
        json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
        "{bad json here",
        json.dumps({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                    "params": {"name": "exec_sql",
                               "arguments": {"db": "testpg", "sql": "SELECT 7 AS s"}}}),
        "",  # blank line -> skipped
    ]
    monkeypatch.setattr("sys.stdin", io.StringIO("\n".join(lines) + "\n"))
    out = io.StringIO()
    monkeypatch.setattr("sys.stdout", out)

    rc = mcp.serve(str(wsdir), allow_write=False)
    assert rc == 0

    replies = [json.loads(ln) for ln in out.getvalue().splitlines() if ln.strip()]
    # exactly three replies (initialize, tools/list, exec_sql); the notification
    # and the malformed/blank lines produced none.
    ids = [r["id"] for r in replies]
    assert ids == [1, 2, 3]

    by_id = {r["id"]: r for r in replies}
    assert by_id[1]["result"]["protocolVersion"] == "2025-06-18"
    assert len(by_id[2]["result"]["tools"]) == 6
    exec_payload = json.loads(by_id[3]["result"]["content"][0]["text"])
    assert exec_payload["rows"] == [{"s": 7}]


@requires_db
@pytest.mark.integration
def test_serve_loop_reports_method_not_found_and_notification_errors(
        wsdir, monkeypatch, restore_write_flag):
    """An unknown method on a request id -> JSON-RPC error -32601; the same
    unknown method sent as a NOTIFICATION (no id) produces no reply."""
    lines = [
        json.dumps({"jsonrpc": "2.0", "id": 10, "method": "bogus/method"}),
        json.dumps({"jsonrpc": "2.0", "method": "bogus/method"}),  # notification
    ]
    monkeypatch.setattr("sys.stdin", io.StringIO("\n".join(lines) + "\n"))
    out = io.StringIO()
    monkeypatch.setattr("sys.stdout", out)

    rc = mcp.serve(str(wsdir), allow_write=False)
    assert rc == 0

    replies = [json.loads(ln) for ln in out.getvalue().splitlines() if ln.strip()]
    assert len(replies) == 1
    assert replies[0]["id"] == 10
    assert replies[0]["error"]["code"] == -32601
    assert "method not found" in replies[0]["error"]["message"]


@requires_db
@pytest.mark.integration
def test_serve_loop_internal_error_wrapped_as_minus_32603(
        wsdir, monkeypatch, restore_write_flag):
    """A non-QuarryError bubbling out of _dispatch on a request is caught and
    returned as JSON-RPC error -32603; the same failure on a notification (no id)
    yields no reply and never crashes the loop."""
    def boom(method, params):
        raise RuntimeError("unexpected boom")
    monkeypatch.setattr(mcp, "_dispatch", boom)

    lines = [
        json.dumps({"jsonrpc": "2.0", "id": 5, "method": "tools/list"}),
        json.dumps({"jsonrpc": "2.0", "method": "tools/list"}),  # notification
    ]
    monkeypatch.setattr("sys.stdin", io.StringIO("\n".join(lines) + "\n"))
    out = io.StringIO()
    monkeypatch.setattr("sys.stdout", out)

    rc = mcp.serve(str(wsdir), allow_write=False)
    assert rc == 0

    replies = [json.loads(ln) for ln in out.getvalue().splitlines() if ln.strip()]
    assert len(replies) == 1
    assert replies[0]["id"] == 5
    assert replies[0]["error"]["code"] == -32603
    assert "internal error" in replies[0]["error"]["message"]


@requires_db
@pytest.mark.integration
def test_serve_sets_allow_write_flag(wsdir, monkeypatch, restore_write_flag):
    """serve(..., allow_write=True) flips the module global so writes may pass."""
    monkeypatch.setattr("sys.stdin", io.StringIO(""))  # empty stream -> loop exits
    monkeypatch.setattr("sys.stdout", io.StringIO())
    rc = mcp.serve(str(wsdir), allow_write=True)
    assert rc == 0
    assert mcp._ALLOW_WRITE_FLAG is True
