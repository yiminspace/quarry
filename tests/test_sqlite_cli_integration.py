"""In-process public-interface contracts against temporary SQLite files."""

from __future__ import annotations

import csv
import io
import json
import sqlite3

import pytest

from quarry import cli, core, mcp, workspace
from test_sqlite_interfaces import QUOTED_NAME, sqlite_ws  # noqa: F401 — shared real-file fixture

pytestmark = pytest.mark.integration


@pytest.fixture()
def sqlite_workspace(sqlite_ws, monkeypatch):
    for name in ("WS", "WS_LIST", "_EXPLICIT"):
        monkeypatch.setattr(workspace, name, getattr(workspace, name))
    workspace.configure_workspace(str(sqlite_ws))
    return sqlite_ws


def invoke(ws, *args):
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(cli.sys, "argv", ["qy", "--workspace", str(ws), *args])
        return cli.main()


@pytest.mark.parametrize("fmt", ["json", "ndjson", "csv", "table"])
def test_library_and_cli_share_capped_rows_and_stats(sqlite_workspace, capsys, fmt):
    conn = core.resolve_connection("app")
    sql = "SELECT id, name FROM items ORDER BY id"
    result = core.run_query(conn, sql, max_rows=2, with_types=True)
    stats = {}
    assert core.execute_sql(conn=conn, sql=sql, psql_vars={}, fmt=fmt,
                            max_rows=2, stats=stats) == core.EXIT_OK
    captured = capsys.readouterr()
    assert result.truncated and result.row_count == 2
    assert result.columns == [{"name": "id", "type": None}, {"name": "name", "type": None}]
    assert stats["elapsed_ms"] >= 0 and stats["size_is_estimated"] is True
    assert stats["download_bytes"] == result.download_bytes > 0
    assert "truncated to 2 rows" in captured.err
    if fmt == "json":
        assert json.loads(captured.out) == result.rows
    elif fmt == "ndjson":
        assert [json.loads(line) for line in captured.out.splitlines()] == result.rows
    elif fmt == "csv":
        assert list(csv.DictReader(io.StringIO(captured.out))) == [
            {"id": str(row["id"]), "name": row["name"]} for row in result.rows]
    else:
        assert "dev-first" in captured.out and QUOTED_NAME in captured.out
        assert "第三条" not in captured.out


def test_cli_empty_csv_keeps_headers_and_statistics(sqlite_workspace, capsys):
    assert invoke(sqlite_workspace, "exec", "app", "--sql",
                  "SELECT id, name FROM items WHERE 0", "--format", "csv") == 0
    captured = capsys.readouterr()
    assert list(csv.reader(io.StringIO(captured.out))) == [["id", "name"]]
    assert "downloaded" in captured.err


def test_cli_connection_config_and_probes_validate_files(sqlite_workspace, capsys):
    assert invoke(sqlite_workspace, "connections", "add", "extra", "--engine", "sqlite",
                  "--url", "data/dev.db", "--env", "local") == 0
    assert "connected to SQLite" in capsys.readouterr().out
    assert invoke(sqlite_workspace, "connections", "test", "app", "--env", "local") == 0
    assert "app_local" in capsys.readouterr().out
    assert invoke(sqlite_workspace, "ping", "app", "--env", "jp", "--format", "json") == 0
    assert json.loads(capsys.readouterr().out)["key"] == "app_jp"
    assert invoke(sqlite_workspace, "connections", "add", "missing", "--url", "sqlite:///missing.db",
                  "--no-test") == 0
    capsys.readouterr()
    assert invoke(sqlite_workspace, "connections", "test", "missing") == core.EXIT_CONNECTION_ERROR
    assert "does not exist" in capsys.readouterr().err
    assert invoke(sqlite_workspace, "ping", "--all", "--format", "json") == 1
    results = json.loads(capsys.readouterr().out)
    assert {item["key"] for item in results if not item["ok"]} == {"missing"}
    assert not (sqlite_workspace / "missing.db").exists()


@pytest.mark.parametrize("flag,value", [("--ssh-host", "bastion"), ("--ssh-user", "alice"),
                                        ("--ssh-key", "id_ed25519"), ("--ssh-port", "22")])
def test_sqlite_connection_rejects_ssh_before_saving(sqlite_workspace, capsys, flag, value):
    path = sqlite_workspace / "connections.toml"
    before = path.read_text()
    assert invoke(sqlite_workspace, "connections", "add", "bad", "--url", "sqlite:///data/dev.db",
                  flag, value, "--no-test") == core.EXIT_USAGE
    assert "SSH" in capsys.readouterr().err
    assert path.read_text() == before


@pytest.mark.parametrize("args", [("app",), ("--engine", "sqlite")])
def test_sqlite_local_up_explains_file_connections(sqlite_workspace, capsys, args):
    assert invoke(sqlite_workspace, "local", "up", *args) == core.EXIT_USAGE
    assert "needs no Docker" in capsys.readouterr().err


def test_cli_saved_query_roundtrip_and_explicit_write(sqlite_workspace, capsys):
    assert invoke(sqlite_workspace, "save", "by_name", "--db", "app", "--param", "name:text:required",
                  "--sql", "SELECT id, name FROM items WHERE name = :'name'") == 0
    capsys.readouterr()
    assert invoke(sqlite_workspace, "run", "by_name", "--strict", "--param", f"name={QUOTED_NAME}") == 0
    assert json.loads(capsys.readouterr().out) == [{"id": 2, "name": QUOTED_NAME}]
    assert invoke(sqlite_workspace, "exec", "app", "--sql", "DELETE FROM items WHERE id=3") == 8
    assert "read-only" in capsys.readouterr().err
    assert invoke(sqlite_workspace, "exec", "app", "--sql",
                  "UPDATE items SET name=:'name' WHERE id=3 RETURNING name", "--write",
                  "--param", f"name={QUOTED_NAME}") == 0
    assert json.loads(capsys.readouterr().out) == [{"name": QUOTED_NAME}]
    with sqlite3.connect(sqlite_workspace / "data/dev.db") as db:
        assert db.execute("SELECT name FROM items WHERE id=3").fetchone()[0] == QUOTED_NAME


@pytest.mark.parametrize("sql,code", [
    ("SELECT :n AS n, :'value' AS value", core.EXIT_OK),
    ("SELECT * FROM missing_table", core.EXIT_SQL_ERROR),
    ("DELETE FROM items", core.EXIT_SAFETY_BLOCKED),
    ("PRAGMA user_version=3", core.EXIT_SAFETY_BLOCKED),
    ("SELECT 1; DELETE FROM items", core.EXIT_SAFETY_BLOCKED),
])
def test_validation_is_side_effect_free(sqlite_workspace, sql, code):
    conn = core.resolve_connection("app")
    query = core.Query(name="candidate", db="app", sql=sql,
                       params=[core.Param("n", "int", True), core.Param("value", "text", default=QUOTED_NAME)])
    assert core.validate_query(query, conn) == code
    with sqlite3.connect(sqlite_workspace / "data/dev.db") as db:
        assert db.execute("SELECT count(*) FROM items").fetchone()[0] == 3
        assert db.execute("PRAGMA user_version").fetchone()[0] == 0


def test_failed_parameterized_execution_has_no_success_stats(sqlite_workspace, capsys):
    stats = {}
    with pytest.raises(core.QuarryError) as caught:
        core.execute_sql(conn=core.resolve_connection("app"), sql="SELECT :value", fmt="json",
                         psql_vars={"value": "1; DELETE FROM items"}, stats=stats)
    assert caught.value.exit_code == core.EXIT_SAFETY_BLOCKED
    assert stats == {} and capsys.readouterr().out == ""


@pytest.mark.parametrize("sql,expected", [
    ("VALUES (1),(2),(3); -- trailing comment", [{"column1": 1}, {"column1": 2}]),
    ("WITH c AS (SELECT 1) VALUES (1),(2),(3); /* trailing ; */", [{"column1": 1}, {"column1": 2}]),
    ('SELECT id AS "LIMIT 1" FROM items ORDER BY id; -- final', [{"LIMIT 1": 1}, {"LIMIT 1": 2}]),
    ("SELECT id AS [LIMIT 1] FROM items ORDER BY id", [{"LIMIT 1": 1}, {"LIMIT 1": 2}]),
    ("SELECT id AS `LIMIT 1` FROM items ORDER BY id LIMIT -1", [{"LIMIT 1": 1}, {"LIMIT 1": 2}]),
    ("SELECT id FROM items ORDER BY id LIMIT 1,2; -- reset offset", [{"id": 1}, {"id": 2}]),
    ("SELECT id FROM items ORDER BY id LIMIT '1'", [{"id": 1}, {"id": 2}]),
    ("SELECT id FROM items ORDER BY id LIMIT (SELECT 1)", [{"id": 1}, {"id": 2}]),
])
def test_saved_limit_understands_sqlite_tokens(sqlite_workspace, capsys, sql, expected):
    query = sqlite_workspace / "queries" / "limited.sql"
    query.write_text(f"-- @name: limited\n-- @db: app\n{sql}\n")
    assert invoke(sqlite_workspace, "run", "limited", "--limit", "2") == 0
    assert json.loads(capsys.readouterr().out) == expected
    assert query.read_text().endswith(sql + "\n")


@pytest.mark.parametrize("sql", [
    'SELECT id AS "LIMIT 1" FROM items ORDER BY id LIMIT 1; -- last comment',
    "SELECT id AS [LIMIT 1] FROM items ORDER BY id LIMIT '1'",
    "SELECT id AS [LIMIT 1] FROM items ORDER BY id",
])
def test_saved_full_keeps_quoted_column_names(sqlite_workspace, capsys, sql):
    (sqlite_workspace / "queries" / "full.sql").write_text(f"-- @name: full\n-- @db: app\n{sql}\n")
    assert invoke(sqlite_workspace, "run", "full", "--full") == 0
    assert json.loads(capsys.readouterr().out) == [{"LIMIT 1": n} for n in (1, 2, 3)]


@pytest.mark.parametrize("sql", ["SELECT :limit AS n FROM items", "SELECT id AS n FROM items ORDER BY id LIMIT :limit"])
def test_saved_limit_does_not_confuse_parameter_names(sqlite_workspace, capsys, sql):
    (sqlite_workspace / "queries" / "parameter_limit.sql").write_text(
        "-- @name: parameter_limit\n-- @db: app\n-- @param: limit (int, default=1)\n" + sql + "\n")
    assert invoke(sqlite_workspace, "run", "parameter_limit", "--limit", "2") == 0
    rows = json.loads(capsys.readouterr().out)
    assert len(rows) == 2 and rows[0] == {"n": 1}


def test_mcp_metadata_and_structured_rows_match_library(sqlite_workspace):
    assert mcp.tool_list_tables("app")["tables"] == ["item_names", "items", "order details"]
    columns = mcp.tool_describe_table("app", "order details")["columns"]
    assert columns[0]["column_name"] == "line id" and columns[0]["primary_key"] == 1
    sql = "SELECT id, payload FROM items ORDER BY id"
    result = mcp.tool_exec_sql("app", sql, max_rows=1)
    library = core.run_query(core.resolve_connection("app"), sql, max_rows=1).to_dict()
    assert result["columns"] == library["columns"]
    assert result["rows"] == library["rows"] == [{"id": 1, "payload": {"$binary": "AP8="}}]
    assert result["truncated"] and result["rowCount"] == 1


def test_mcp_table_metadata_reports_truncation(sqlite_workspace):
    with sqlite3.connect(sqlite_workspace / "data/dev.db") as db:
        db.execute("BEGIN")
        for n in range(5000):
            db.execute(f"CREATE TABLE t{n}(id)")
    result = mcp.tool_list_tables("app")
    assert result["capped"] is True and len(result["tables"]) == 5000
