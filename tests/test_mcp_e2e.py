"""MCP face e2e — a real `qy mcp` subprocess speaking JSON-RPC over stdio,
bound to the temp Postgres workspace (via the shared `mcp` fixture)."""

from __future__ import annotations

import pytest

from conftest import requires_db


@requires_db
@pytest.mark.e2e
def test_list_tables(mcp):
    payload, is_err = mcp.call_tool("list_tables", {"db": "testpg", "env": "test"})
    assert is_err is False
    assert "customers" in payload["tables"] and "orders" in payload["tables"]


@requires_db
@pytest.mark.e2e
def test_describe_table(mcp):
    payload, is_err = mcp.call_tool("describe_table", {"db": "testpg", "table": "customers", "env": "test"})
    assert is_err is False
    cols = {c.get("column_name") for c in payload["columns"]}
    assert "email" in cols


@requires_db
@pytest.mark.e2e
def test_exec_sql_read(mcp):
    payload, is_err = mcp.call_tool("exec_sql", {"db": "testpg", "env": "test", "sql": "SELECT 1 AS one"})
    assert is_err is False and payload["rows"] == [{"one": 1}]


@requires_db
@pytest.mark.e2e
def test_exec_sql_write_blocked(mcp):
    payload, is_err = mcp.call_tool(
        "exec_sql", {"db": "testpg", "env": "test", "sql": "DELETE FROM customers", "write": True})
    assert is_err is True and payload["code"] == 8


@requires_db
@pytest.mark.e2e
def test_exec_sql_missing_args_is_tool_error(mcp):
    """A malformed call is a tool error (isError), never a -32603 protocol crash."""
    payload, is_err = mcp.call_tool("exec_sql", {})
    assert is_err is True
    assert "missing required argument" in payload["error"]


@requires_db
@pytest.mark.e2e
def test_unknown_tool_is_protocol_error(mcp):
    reply = mcp.rpc("tools/call", {"name": "does_not_exist", "arguments": {}})
    assert "error" in reply and "unknown tool" in reply["error"]["message"]


@requires_db
@pytest.mark.e2e
def test_notifications_get_no_reply_and_loop_survives(mcp):
    # a notification (no id) must not produce a response; the server keeps serving
    mcp.notify("notifications/some_event")
    payload, is_err = mcp.call_tool("exec_sql", {"db": "testpg", "env": "test", "sql": "SELECT 2 AS n"})
    assert is_err is False and payload["rows"] == [{"n": 2}]
