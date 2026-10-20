"""CLI end-to-end regression tests — run the real `qy` binary as a subprocess.

Uses the shared `qy` fixture (from conftest) which targets a temp Postgres
workspace. Exit codes are part of the CLI's stable contract, so they're asserted
explicitly.
"""

from __future__ import annotations

import json

import pytest

from conftest import TEST_DB_URL, requires_db

# EXIT contract (mirrors core.py)
EXIT_OK, EXIT_USAGE, EXIT_CONN, EXIT_SQL, EXIT_NODATA = 0, 1, 2, 3, 4
EXIT_SAFETY = 8


# ---- usage / argument errors (no DB needed) ----

@requires_db
@pytest.mark.e2e
def test_exec_missing_sql_and_file(qy):
    p = qy("exec", "testpg")
    assert p.returncode == EXIT_USAGE
    assert "must provide --sql or --file" in p.stderr


@requires_db
@pytest.mark.e2e
def test_exec_missing_file_is_clean_error(qy):
    p = qy("exec", "testpg", "--file", "/no/such/file.sql")
    assert p.returncode == EXIT_USAGE
    assert "cannot read --file" in p.stderr
    assert "Traceback" not in p.stderr  # a clean message, not a crash


@requires_db
@pytest.mark.e2e
def test_unknown_db_exit_code(qy):
    p = qy("exec", "ghostdb", "--sql", "SELECT 1")
    assert p.returncode == EXIT_USAGE
    assert "unknown db 'ghostdb'" in p.stderr


# ---- read-only rail ----

@requires_db
@pytest.mark.e2e
def test_write_blocked_exit_8(qy):
    p = qy("exec", "testpg", "--sql", "DELETE FROM customers")
    assert p.returncode == EXIT_SAFETY
    assert "read-only" in p.stderr


@requires_db
@pytest.mark.e2e
def test_multi_statement_blocked_exit_8(qy):
    p = qy("exec", "testpg", "--sql", "SELECT 1; DROP TABLE customers")
    assert p.returncode == EXIT_SAFETY


# ---- formats + max_rows ----

@requires_db
@pytest.mark.e2e
def test_json_output_shape(qy):
    p = qy("exec", "testpg", "--sql", "SELECT 1 AS x, NULL AS y", "--format", "json")
    assert p.returncode == EXIT_OK, p.stderr
    data = json.loads(p.stdout)
    assert data == [{"x": 1, "y": None}]


@requires_db
@pytest.mark.e2e
def test_max_rows_is_exact(qy):
    """--max-rows N must emit exactly N rows, not N+1 (the truncation-probe row)."""
    p = qy("exec", "testpg", "--sql", "SELECT generate_series(1,10) AS n",
           "--max-rows", "4", "--format", "ndjson")
    assert p.returncode == EXIT_OK, p.stderr
    lines = [ln for ln in p.stdout.splitlines() if ln.strip()]
    assert len(lines) == 4


@requires_db
@pytest.mark.e2e
def test_csv_output(qy):
    p = qy("exec", "testpg", "--sql", "SELECT 1 AS a, 2 AS b", "--format", "csv")
    assert p.returncode == EXIT_OK, p.stderr
    assert p.stdout.splitlines()[0] == "a,b"


# ---- saved-query lifecycle ----

@requires_db
@pytest.mark.e2e
def test_save_run_limit_roundtrip(qy):
    save = qy("save", "cust", "--db", "testpg",
              "--sql", "SELECT id, name FROM customers ORDER BY id", "--no-validate")
    assert save.returncode == EXIT_OK, save.stderr

    run = qy("run", "cust", "--format", "json")
    assert run.returncode == EXIT_OK, run.stderr
    assert len(json.loads(run.stdout)) == 3

    # --limit caps the result and stays valid SQL
    limited = qy("run", "cust", "--limit", "1", "--format", "json")
    assert limited.returncode == EXIT_OK, limited.stderr
    assert len(json.loads(limited.stdout)) == 1


@requires_db
@pytest.mark.e2e
def test_run_limit_does_not_corrupt_nested_query(qy):
    qy("save", "nested", "--db", "testpg", "--no-validate",
       "--sql", "SELECT * FROM (SELECT id FROM customers ORDER BY id LIMIT 3) s")
    p = qy("run", "nested", "--limit", "2", "--format", "json")
    assert p.returncode == EXIT_OK, p.stderr
    assert len(json.loads(p.stdout)) == 2


@requires_db
@pytest.mark.e2e
def test_validate_resolves_logical_db(tmp_path, qy):
    """`validate` must resolve a logical env-set db (resolve_connection), matching `run`."""
    # env-set: two keys share db=shop; a query targets @db: shop
    (tmp_path / "connections.toml").write_text(
        f'[shop_dev]\nurl = "{TEST_DB_URL}"\nengine="postgres"\ndb="shop"\nenv="test"\n',
        encoding="utf-8")
    qdir = tmp_path / "queries"
    qdir.mkdir(exist_ok=True)
    (qdir / "ping.sql").write_text("-- @name: ping\n-- @db: shop\nSELECT 1 AS ok\n", encoding="utf-8")
    p = qy("validate", "ping")
    assert p.returncode == EXIT_OK, (p.stdout + p.stderr)
