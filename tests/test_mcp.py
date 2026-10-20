"""MCP face tests — real subprocess speaking JSON-RPC over stdio."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SRC = str(Path(__file__).resolve().parent.parent / "src")

CONNS = """
[blog]
url = "postgresql://u@127.0.0.1:5432/blog"
group = "acme"
env = "prod"
production = true

[shop_dev]
url = "postgresql://u@dev-host/shop"
db = "shop"
env = "dev"

[shop_prod]
url = "postgresql://u@prod-host/shop"
db = "shop"
env = "prod"
production = true
"""


class MCPClient:
    def __init__(self, ws: Path, write: bool = False):
        args = [sys.executable, "-m", "quarry.mcp", "--workspace", str(ws)]
        if write:
            args.append("--write")
        env = {**os.environ, "PYTHONPATH": SRC}
        self.proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, text=True, env=env)
        self._id = 0

    def rpc(self, method: str, params: dict | None = None):
        self._id += 1
        msg = {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params or {}}
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        reply = json.loads(line)
        assert reply["id"] == self._id
        return reply

    def notify(self, method: str):
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": method}) + "\n")
        self.proc.stdin.flush()

    def call_tool(self, name: str, args: dict | None = None):
        reply = self.rpc("tools/call", {"name": name, "arguments": args or {}})
        result = reply["result"]
        payload = json.loads(result["content"][0]["text"])
        return payload, result["isError"]

    def close(self):
        self.proc.stdin.close()
        self.proc.wait(timeout=10)


@pytest.fixture()
def mcp_ws(tmp_path: Path):
    (tmp_path / "connections.toml").write_text(CONNS)
    (tmp_path / "queries").mkdir()
    return tmp_path


@pytest.fixture()
def client(mcp_ws):
    c = MCPClient(mcp_ws)
    yield c
    c.close()


def test_initialize_handshake(client):
    reply = client.rpc("initialize", {"protocolVersion": "2025-06-18",
                                      "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}})
    res = reply["result"]
    assert res["serverInfo"]["name"] == "quarry"
    assert "tools" in res["capabilities"]
    client.notify("notifications/initialized")
    assert client.rpc("ping")["result"] == {}


def test_tools_list(client):
    tools = client.rpc("tools/list")["result"]["tools"]
    names = {t["name"] for t in tools}
    assert names == {"list_connections", "list_tables", "describe_table",
                     "exec_sql", "list_saved_queries", "run_saved_query"}
    for t in tools:
        assert t["inputSchema"]["type"] == "object"
        assert t["description"]


def test_list_connections_tool(client):
    payload, is_err = client.call_tool("list_connections")
    assert not is_err
    groups = {g["group"]: g for g in payload["groups"]}
    assert "acme" in groups
    shop = next(it for g in payload["groups"] for it in g["items"] if it["db"] == "shop")
    assert shop["is_env_set"] is True


def test_exec_sql_write_blocked_by_default(client):
    # safety rail fires before any connection attempt -> works without a live DB
    payload, is_err = client.call_tool("exec_sql", {"db": "shop", "sql": "delete from t"})
    assert is_err
    assert payload["code"] == 8


def test_exec_sql_write_flag_requires_server_write(client):
    payload, is_err = client.call_tool(
        "exec_sql", {"db": "shop", "sql": "delete from t", "write": True})
    assert is_err
    assert payload["code"] == 8
    assert "--write" in payload["error"]


def test_prod_write_needs_confirm(mcp_ws):
    c = MCPClient(mcp_ws, write=True)
    try:
        payload, is_err = c.call_tool(
            "exec_sql", {"db": "shop", "sql": "delete from t", "env": "prod", "write": True})
        assert is_err and payload["code"] == 8
        assert "confirm_prod" in payload["error"]
    finally:
        c.close()


def test_unknown_tool_and_method(client):
    reply = client.rpc("tools/call", {"name": "nope", "arguments": {}})
    assert "error" in reply and "unknown tool" in reply["error"]["message"]
    reply = client.rpc("bogus/method")
    assert reply["error"]["code"] == -32601


def test_exec_sql_live(tmp_path):
    """End-to-end SELECT against the local quarry_test DB (skips if unreachable)."""
    from conftest import TEST_DB_URL, _db_reachable
    if not _db_reachable():
        pytest.skip("quarry_test Postgres not reachable")
    (tmp_path / "connections.toml").write_text(
        f'[testpg]\nurl = "{TEST_DB_URL}"\nengine = "postgres"\nenv = "test"\n')
    (tmp_path / "queries").mkdir()
    c = MCPClient(tmp_path)
    try:
        c.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                             "clientInfo": {"name": "t", "version": "0"}})
        payload, is_err = c.call_tool(
            "exec_sql", {"db": "testpg", "sql": "select name from customers order by id"})
        assert not is_err
        assert payload["rowCount"] == 3
        assert payload["rows"][0]["name"] == "Alice"
        payload, is_err = c.call_tool("list_tables", {"db": "testpg"})
        assert not is_err and "customers" in payload["tables"]
        payload, is_err = c.call_tool("describe_table", {"db": "testpg", "table": "customers"})
        assert not is_err
        cols = {r["column_name"] for r in payload["columns"]}
        assert {"id", "name", "email"} <= cols
    finally:
        c.close()
