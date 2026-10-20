"""SQLite files remain bound to their owning workspace across CLI/MCP entrypoints."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from quarry import core, row_link, sqlite_engine, workspace

SRC = Path(__file__).resolve().parents[1] / "src"


def seed(path: Path, marker: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE records(id INTEGER PRIMARY KEY, marker TEXT NOT NULL)")
        db.execute("INSERT INTO records VALUES(1, ?)", (marker,))


@pytest.fixture()
def multi_workspace(tmp_path, monkeypatch):
    homes = {name: tmp_path / name for name in ("alpha", "beta")}
    for name, home in homes.items():
        (home / "queries").mkdir(parents=True)
        blocks = []
        for env in ("local", "dev", "prod"):
            seed(home / "data" / f"{env}.db", f"{name}/{env}")
            with sqlite3.connect(home / "data" / f"{env}.db") as db:
                db.execute(f'CREATE TABLE "only_{name}" (id INTEGER)')
            blocks.append(
                f'[{name}_{env}]\nurl="sqlite:///data/{env}.db"\ndb="{name}"\n'
                f'env="{env}"\nproduction={str(env == "prod").lower()}\n')
        blocks.append(f'[{name}_missing]\nurl="sqlite:///data/missing.db"\n')
        (home / "connections.toml").write_text("\n".join(blocks))
        (home / "queries" / f"{name}_records.sql").write_text(
            f"-- @name: {name}_records\n-- @db: {name}\nSELECT marker FROM records\n")
    # A file with the same path in a sibling workspace or cwd must never be
    # silently substituted for the missing file owned by alpha.
    seed(homes["beta"] / "data/missing.db", "beta/missing")
    foreign = tmp_path / "unrelated-cwd"
    for env in ("local", "dev", "prod", "missing"):
        seed(foreign / "data" / f"{env}.db", f"wrong-cwd/{env}")
    config = tmp_path / "isolated-config.toml"
    config.write_text("workspaces = " + json.dumps([str(p) for p in homes.values()]) + "\n")
    monkeypatch.setenv("QUARRY_CONFIG", str(config))
    for name in ("QUARRY_CONNECTIONS_FILE", "QUARRY_QUERIES_DIR", "QUARRY_WORKSPACE"):
        monkeypatch.delenv(name, raising=False)
    for name in ("WS", "WS_LIST", "_EXPLICIT"):
        monkeypatch.setattr(workspace, name, getattr(workspace, name))
    monkeypatch.chdir(foreign)
    return homes, foreign


def cli_call(state, *args, explicit: bool = True, stdin: str = ""):
    homes, cwd = state
    command = [sys.executable, "-m", "quarry.cli"]
    if explicit:
        command += ["--workspace", os.pathsep.join(str(p) for p in homes.values())]
    return subprocess.run(command + list(args), cwd=cwd, input=stdin, capture_output=True,
                          text=True, timeout=20, env={**os.environ, "PYTHONPATH": str(SRC)})


def mcp_call(state, calls, *, write: bool = False, explicit: bool = True):
    homes, cwd = state
    command = [sys.executable, "-m", "quarry.mcp"]
    if explicit:
        command += ["--workspace", os.pathsep.join(str(p) for p in homes.values())]
    if write:
        command.append("--write")
    payload = "".join(json.dumps({"jsonrpc": "2.0", "id": index, "method": "tools/call",
        "params": {"name": name, "arguments": args}}) + "\n"
        for index, (name, args) in enumerate(calls, 1))
    proc = subprocess.run(command, cwd=cwd, input=payload, capture_output=True, text=True,
                          timeout=20, env={**os.environ, "PYTHONPATH": str(SRC)})
    assert proc.returncode == 0, proc.stderr
    replies = [json.loads(line) for line in proc.stdout.splitlines()]
    assert len(replies) == len(calls), proc.stderr
    output = []
    for index, reply in enumerate(replies, 1):
        assert reply["id"] == index and "error" not in reply, reply
        result = reply["result"]
        output.append((json.loads(result["content"][0]["text"]), result["isError"]))
    return output


@pytest.mark.integration
@pytest.mark.parametrize("reverse", [False, True])
def test_core_metadata_and_row_links_keep_workspace_origin(multi_workspace, reverse):
    homes, _ = multi_workspace
    order = list(homes.values())[::-1] if reverse else list(homes.values())
    workspace.configure_workspace(os.pathsep.join(str(p) for p in order))
    for owner, home in homes.items():
        for env in ("local", "dev", "prod"):
            conn = core.resolve_connection(owner, env)
            assert conn.source == str(home)
            assert conn.production is (env == "prod")
            assert sqlite_engine.database_path(conn) == home / "data" / f"{env}.db"
            result = core.run_query(conn, "SELECT * FROM records")
            assert result.rows == [{"id": 1, "marker": f"{owner}/{env}"}]
            # Reuse the same cache alias deliberately: the file fingerprint
            # must still bind metadata to this connection's actual database.
            assert core.cached_tables(conn, "shared-alias", env)["tables"] == [f"only_{owner}", "records"]
            assert core.cached_columns(conn, "shared-alias", env, "records")["rows"][0]["primary_key"] == 1
            record = row_link.record_query(conn, result.sql, result.rows[0])
            assert core.run_query(conn, record["sql"]).rows == result.rows


@pytest.mark.e2e
@pytest.mark.parametrize("explicit", [True, False])
def test_cli_from_foreign_cwd_resolves_all_workspace_environments(multi_workspace, explicit):
    for owner in ("alpha", "beta"):
        for env in ("local", "dev", "prod"):
            proc = cli_call(multi_workspace, "run", f"{owner}_records", "--env", env, explicit=explicit)
            assert proc.returncode == 0, proc.stderr
            assert json.loads(proc.stdout) == [{"marker": f"{owner}/{env}"}]


@pytest.mark.e2e
def test_cli_production_opt_in_writes_only_selected_workspace(multi_workspace):
    homes, cwd = multi_workspace
    args = ("exec", "beta", "--env", "prod", "--sql",
            "UPDATE records SET marker='confirmed' WHERE id=1 RETURNING marker", "--write")
    proc = cli_call(multi_workspace, *args, stdin="n\n")
    assert proc.returncode == core.EXIT_USAGE and "PROD" in proc.stderr
    with sqlite3.connect(homes["beta"] / "data/prod.db") as db:
        assert db.execute("SELECT marker FROM records").fetchone()[0] == "beta/prod"
    proc = cli_call(multi_workspace, *args, "--yes")
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == [{"marker": "confirmed"}]
    for path, expected in [(homes["beta"] / "data/prod.db", "confirmed"),
                           (homes["beta"] / "data/dev.db", "beta/dev"),
                           (homes["alpha"] / "data/prod.db", "alpha/prod"),
                           (cwd / "data/prod.db", "wrong-cwd/prod")]:
        with sqlite3.connect(path) as db:
            assert db.execute("SELECT marker FROM records").fetchone()[0] == expected


@pytest.mark.e2e
@pytest.mark.parametrize("explicit", [True, False])
def test_mcp_from_foreign_cwd_resolves_workspace_and_production_policy(multi_workspace, explicit):
    calls = [("run_saved_query", {"name": f"{owner}_records", "env": env})
             for owner in ("alpha", "beta") for env in ("local", "dev", "prod")]
    results = mcp_call(multi_workspace, calls, explicit=explicit)
    expected = [f"{owner}/{env}" for owner in ("alpha", "beta") for env in ("local", "dev", "prod")]
    assert all(not failed for _, failed in results), results
    assert [payload["rows"][0]["marker"] for payload, _ in results] == expected
    args = {"db": "beta", "env": "prod", "sql": "UPDATE records SET marker='mcp' RETURNING marker", "write": True}
    blocked, allowed, other = mcp_call(multi_workspace, [
        ("exec_sql", args), ("exec_sql", {**args, "confirm_prod": True}),
        ("exec_sql", {"db": "alpha", "env": "prod", "sql": "SELECT marker FROM records"}),
    ], write=True, explicit=explicit)
    assert blocked[1] and blocked[0]["code"] == core.EXIT_SAFETY_BLOCKED
    assert "confirm_prod" in blocked[0]["error"]
    assert not allowed[1] and allowed[0]["rows"] == [{"marker": "mcp"}]
    assert not other[1] and other[0]["rows"] == [{"marker": "alpha/prod"}]


@pytest.mark.e2e
@pytest.mark.parametrize("write", [False, True])
def test_missing_owner_file_never_falls_back_or_is_created(multi_workspace, write):
    homes, cwd = multi_workspace
    workspace.configure_workspace(os.pathsep.join(str(p) for p in reversed(homes.values())))
    conn = core.resolve_connection("alpha_missing")
    with pytest.raises(core.QuarryError) as caught:
        core.run_query(conn, "SELECT * FROM records", allow_write=write)
    assert caught.value.exit_code == core.EXIT_CONNECTION_ERROR
    proc = cli_call(multi_workspace, "exec", "alpha_missing", "--sql", "SELECT * FROM records",
                    *(('--write',) if write else ()))
    assert proc.returncode == core.EXIT_CONNECTION_ERROR, proc.stderr
    [(payload, failed)] = mcp_call(multi_workspace, [
        ("exec_sql", {"db": "alpha_missing", "sql": "SELECT * FROM records", "write": write})], write=write)
    assert failed and payload["code"] == core.EXIT_CONNECTION_ERROR
    assert not (homes["alpha"] / "data/missing.db").exists()
    for path, expected in [(homes["beta"] / "data/missing.db", "beta/missing"),
                           (cwd / "data/missing.db", "wrong-cwd/missing")]:
        with sqlite3.connect(path) as db:
            assert db.execute("SELECT marker FROM records").fetchone()[0] == expected
