"""CLI and MCP contracts against real SQLite files, without external services."""

from __future__ import annotations

import csv
import io
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.e2e
SRC = Path(__file__).resolve().parents[1] / "src"
QUOTED_NAME = "O'Reilly\\folder"


@pytest.fixture()
def sqlite_ws(tmp_path: Path) -> Path:
    (tmp_path / "data").mkdir()
    (tmp_path / "queries").mkdir()
    for env in ("dev", "local", "jp"):
        with sqlite3.connect(tmp_path / "data" / f"{env}.db") as db:
            db.executescript('''
                CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT NOT NULL, payload BLOB);
                CREATE VIEW item_names AS SELECT id, name FROM items;
                CREATE TABLE "order details" ("line id" INTEGER PRIMARY KEY, description TEXT);
            ''')
            db.executemany("INSERT INTO items VALUES (?, ?, ?)", [
                (1, f"{env}-first", b"\x00\xff"),
                (2, QUOTED_NAME, None),
                (3, "第三条", None),
            ])
    (tmp_path / "connections.toml").write_text("\n".join(
        f'[app_{env}]\nurl = "sqlite:///data/{env}.db"\ndb = "app"\n'
        f'env = "{env}"\nproduction = {str(env == "jp").lower()}\n'
        for env in ("dev", "local", "jp")
    ))
    return tmp_path


def qy(ws: Path, *args: str, input_text: str = "") -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PYTHONPATH": str(SRC)}
    env.pop("QUARRY_WORKSPACE", None)
    return subprocess.run(
        [sys.executable, "-m", "quarry.cli", "--workspace", str(ws), *args],
        input=input_text, capture_output=True, text=True, env=env, timeout=20,
    )


def mcp_calls(ws: Path, calls: list[tuple[str, dict]], *, write: bool = False) -> list[tuple[dict, bool]]:
    args = [sys.executable, "-m", "quarry.mcp", "--workspace", str(ws)]
    if write:
        args.append("--write")
    requests = "\n".join(json.dumps({
        "jsonrpc": "2.0", "id": index, "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }) for index, (name, arguments) in enumerate(calls, 1)) + "\n"
    proc = subprocess.run(args, input=requests, capture_output=True, text=True,
                          env={**os.environ, "PYTHONPATH": str(SRC)}, timeout=20)
    assert proc.returncode == 0, proc.stderr
    replies = [json.loads(line) for line in proc.stdout.splitlines()]
    assert len(replies) == len(calls), proc.stderr
    results = []
    for index, reply in enumerate(replies, 1):
        assert reply["id"] == index
        assert "error" not in reply, reply
        result = reply["result"]
        results.append((json.loads(result["content"][0]["text"]), result["isError"]))
    return results


@pytest.mark.parametrize("fmt", ["json", "ndjson", "csv", "table"])
def test_cli_sqlite_query_formats(sqlite_ws, fmt):
    proc = qy(sqlite_ws, "exec", "app", "--sql", "SELECT id, name FROM items ORDER BY id",
              "--format", fmt, "--max-rows", "2")
    assert proc.returncode == 0, proc.stderr
    expected = [{"id": 1, "name": "dev-first"}, {"id": 2, "name": QUOTED_NAME}]
    if fmt == "json":
        assert json.loads(proc.stdout) == expected
    elif fmt == "ndjson":
        assert [json.loads(line) for line in proc.stdout.splitlines()] == expected
    elif fmt == "csv":
        assert list(csv.DictReader(io.StringIO(proc.stdout))) == [
            {"id": str(row["id"]), "name": row["name"]} for row in expected]
    else:
        assert "dev-first" in proc.stdout and QUOTED_NAME in proc.stdout
    assert "第三条" not in proc.stdout
    assert "truncat" in proc.stderr.lower() or "capped" in proc.stderr.lower()


def test_cli_sqlite_connection_configuration_and_probes(sqlite_ws):
    # Relative SQLite URLs are resolved against the owning workspace, not cwd.
    proc = qy(sqlite_ws, "connections", "add", "extra", "--engine", "sqlite",
              "--url", "data/dev.db", "--env", "local")
    assert proc.returncode == 0, proc.stderr
    assert "connected to SQLite" in proc.stdout
    proc = qy(sqlite_ws, "connections", "set", "extra", "--engine", "sqlite",
              "--url", "sqlite:///data/local.db")
    assert proc.returncode == 0, proc.stderr
    proc = qy(sqlite_ws, "ping", "app", "--env", "local", "--format", "json")
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout)
    assert result["ok"] and result["key"] == "app_local" and result["engine"] == "sqlite"
    proc = qy(sqlite_ws, "local", "up", "app_local")
    assert proc.returncode != 0 and "needs no Docker" in proc.stderr
    proc = qy(sqlite_ws, "local", "up", "--engine", "sqlite")
    assert proc.returncode != 0 and "needs no Docker" in proc.stderr


def test_cli_sqlite_environments_and_explicit_production(sqlite_ws):
    for env in ("dev", "local", "jp"):
        proc = qy(sqlite_ws, "exec", "app", "--env", env, "--sql",
                  "SELECT name FROM items WHERE id=1")
        assert proc.returncode == 0, proc.stderr
        assert json.loads(proc.stdout) == [{"name": f"{env}-first"}]
    proc = qy(sqlite_ws, "exec", "app", "--env", "jp", "--sql",
              "UPDATE items SET name='changed' WHERE id=1", "--write", input_text="n\n")
    assert proc.returncode != 0 and "PROD" in proc.stderr
    proc = qy(sqlite_ws, "exec", "app", "--env", "jp", "--sql",
              "UPDATE items SET name='changed' WHERE id=1 RETURNING name", "--write", "--yes")
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == [{"name": "changed"}]
    with sqlite3.connect(sqlite_ws / "data" / "jp.db") as db:
        assert db.execute("SELECT name FROM items WHERE id=1").fetchone()[0] == "changed"
    with sqlite3.connect(sqlite_ws / "data" / "dev.db") as db:
        assert db.execute("SELECT name FROM items WHERE id=1").fetchone()[0] == "dev-first"


@pytest.mark.parametrize("sql", [
    "DELETE FROM items", "CREATE TABLE accidental (id INTEGER)",
    "PRAGMA user_version=9", "PRAGMA query_only=OFF",
    "SELECT 1; DELETE FROM items",
])
def test_cli_sqlite_readonly_blocks_writes(sqlite_ws, sql):
    proc = qy(sqlite_ws, "exec", "app", "--sql", sql)
    assert proc.returncode == 8, proc.stderr
    with sqlite3.connect(sqlite_ws / "data" / "dev.db") as db:
        assert db.execute("SELECT count(*) FROM items").fetchone()[0] == 3
        assert db.execute("PRAGMA user_version").fetchone()[0] == 0


def test_cli_sqlite_metadata_saved_query_and_params(sqlite_ws):
    proc = qy(sqlite_ws, "schema", "app", "order details", "--format", "json")
    assert proc.returncode == 0, proc.stderr
    columns = json.loads(proc.stdout)["columns"]
    assert {row["column_name"] for row in columns} == {"line id", "description"}
    proc = qy(sqlite_ws, "save", "by_name", "--db", "app", "--param", "name:text:required",
              "--sql", "SELECT id, name FROM items WHERE name = :'name'")
    assert proc.returncode == 0, proc.stderr
    proc = qy(sqlite_ws, "validate", "by_name")
    assert proc.returncode == 0, proc.stderr
    proc = qy(sqlite_ws, "run", "by_name", "--param", f"name={QUOTED_NAME}", "--strict")
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == [{"id": 2, "name": QUOTED_NAME}]


def test_cli_sqlite_missing_file_and_sql_error(sqlite_ws):
    proc = qy(sqlite_ws, "exec", "app", "--sql", "SELECT * FROM unknown_table")
    assert proc.returncode == 3 and "unknown_table" in proc.stderr
    with (sqlite_ws / "connections.toml").open("a") as stream:
        stream.write('\n[missing]\nurl="sqlite:///does-not-exist.db"\n')
    for flags in ((), ("--write",)):
        proc = qy(sqlite_ws, "exec", "missing", "--sql", "SELECT 1", *flags)
        assert proc.returncode == 2, proc.stderr
        assert not (sqlite_ws / "does-not-exist.db").exists()
    proc = qy(sqlite_ws, "ping", "missing", "--format", "json")
    assert proc.returncode == 1 and not json.loads(proc.stdout)["ok"]


@pytest.mark.parametrize("sql,expected", [
    ("VALUES (1), (2), (3)", [{"column1": 1}, {"column1": 2}]),
    ("WITH c AS (SELECT 1) VALUES (1), (2), (3)", [{"column1": 1}, {"column1": 2}]),
    ("SELECT id, name AS id FROM items ORDER BY items.id", [
        {"id": 1, "id_2": "dev-first"}, {"id": 2, "id_2": QUOTED_NAME}]),
])
def test_cli_sqlite_saved_limit(sqlite_ws, sql, expected):
    proc = qy(sqlite_ws, "save", "values_query", "--db", "app", "--sql", sql)
    assert proc.returncode == 0, proc.stderr
    proc = qy(sqlite_ws, "run", "values_query", "--limit", "2")
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == expected


def test_mcp_sqlite_metadata_and_lossless_results(sqlite_ws):
    results = mcp_calls(sqlite_ws, [
        ("list_tables", {"db": "app"}),
        ("describe_table", {"db": "app", "table": "order details"}),
        ("exec_sql", {"db": "app", "sql": "SELECT * FROM items ORDER BY id", "max_rows": 2}),
        ("exec_sql", {"db": "app", "sql": "SELECT 9223372036854775807 AS big"}),
        ("exec_sql", {"db": "app", "env": "local", "sql": "SELECT name FROM items WHERE id=1"}),
        ("exec_sql", {"db": "app", "sql": "PRAGMA table_info('items')"}),
    ])
    assert all(not failed for _, failed in results), results
    assert {"items", "item_names", "order details"} <= set(results[0][0]["tables"])
    assert results[1][0]["table"] == "order details"
    assert {col["column_name"] for col in results[1][0]["columns"]} == {"line id", "description"}
    query = results[2][0]
    assert query["rowCount"] == 2 and query["truncated"]
    assert query["rows"][0]["payload"] == {"$binary": "AP8="}
    assert results[3][0]["rows"] == [{"big": "9223372036854775807"}]
    assert results[4][0]["rows"] == [{"name": "local-first"}]
    assert len(results[5][0]["rows"]) == 3


def test_mcp_sqlite_write_permissions_and_persistence(sqlite_ws):
    args = {"db": "app", "sql": "DELETE FROM items WHERE id=3"}
    results = mcp_calls(sqlite_ws, [("exec_sql", args), ("exec_sql", {**args, "write": True})])
    assert all(failed and payload["code"] == 8 for payload, failed in results)
    results = mcp_calls(sqlite_ws, [
        ("exec_sql", {**args, "env": "jp", "write": True}),
        ("exec_sql", {**args, "env": "jp", "write": True, "confirm_prod": True}),
        ("exec_sql", {**args, "write": True}),
        ("exec_sql", {"db": "app", "sql": "SELECT count(*) AS n FROM items"}),
    ], write=True)
    assert results[0][1] and results[0][0]["code"] == 8
    assert "confirm_prod" in results[0][0]["error"]
    assert all(not failed for _, failed in results[1:]), results
    assert results[3][0]["rows"] == [{"n": 2}]
    for env in ("dev", "jp"):
        with sqlite3.connect(sqlite_ws / "data" / f"{env}.db") as db:
            assert db.execute("SELECT count(*) FROM items").fetchone()[0] == 2


def test_mcp_sqlite_saved_params_and_sql_error(sqlite_ws):
    (sqlite_ws / "queries" / "by_name.sql").write_text(
        "-- @name: by_name\n-- @db: app\n-- @param: name (text, required)\n"
        "SELECT id, name FROM items WHERE name = :'name'\n")
    results = mcp_calls(sqlite_ws, [
        ("run_saved_query", {"name": "by_name", "params": {"name": QUOTED_NAME}}),
        ("exec_sql", {"db": "app", "sql": "SELECT * FROM unknown_table"}),
    ])
    assert not results[0][1], results
    assert results[0][0]["rows"] == [{"id": 2, "name": QUOTED_NAME}]
    assert results[1][1] and results[1][0]["code"] == 3


def test_mcp_sqlite_table_limit_is_visible(sqlite_ws):
    with sqlite3.connect(sqlite_ws / "data" / "dev.db") as db:
        db.execute("BEGIN")
        for index in range(5001):
            db.execute(f"CREATE TABLE t{index} (id INTEGER)")
    [(payload, failed)] = mcp_calls(sqlite_ws, [("list_tables", {"db": "app"})])
    assert not failed, payload
    assert len(payload["tables"]) == 5000
    assert payload["capped"] is True
