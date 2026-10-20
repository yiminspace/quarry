"""Core engine tests: safety rails (no DB) + run_query (needs DB)."""

from __future__ import annotations

import pytest

from quarry import core
from quarry.core import QuarryError, enforce_safety, is_read_only, run_query
from conftest import requires_db


# ---- safety: read-only detection (pure, no DB) ----

@pytest.mark.parametrize("sql", [
    "select * from t",
    "  SELECT 1",
    "with x as (select 1) select * from x",
    "-- a comment\nselect 2",
    "/* block */ SELECT 3",
    "SHOW tables",
    "EXPLAIN select 1",
    "VALUES (1),(2)",
])
def test_read_only_allows_reads(sql):
    assert is_read_only(sql) is True


@pytest.mark.parametrize("sql", [
    "insert into t values (1)",
    "UPDATE t SET x=1",
    "delete from t",
    "drop table t",
    "ALTER TABLE t ADD COLUMN c int",
    "truncate t",
    "-- sneaky\nDELETE FROM t",
])
def test_read_only_blocks_writes(sql):
    assert is_read_only(sql) is False


def test_enforce_safety_blocks_write_by_default():
    with pytest.raises(QuarryError) as e:
        enforce_safety("delete from t", allow_write=False, max_rows=None)
    assert e.value.exit_code == core.EXIT_SAFETY_BLOCKED


def test_enforce_safety_allows_write_with_flag():
    sql, lim = enforce_safety("delete from t", allow_write=True, max_rows=None)
    assert sql == "delete from t" and lim is None


def test_enforce_safety_injects_limit():
    sql, lim = enforce_safety("select * from t", allow_write=False, max_rows=100)
    assert lim == 100 and sql.rstrip().endswith("LIMIT 101")


def test_enforce_safety_keeps_existing_limit():
    sql, lim = enforce_safety("select * from t limit 5", allow_write=False, max_rows=100)
    assert lim is None and "LIMIT 101" not in sql


def test_enforce_safety_skips_limit_for_explain_and_show():
    # EXPLAIN/SHOW don't accept LIMIT — appending one would break them
    for stmt in ("explain select * from t", "SHOW work_mem"):
        sql, lim = enforce_safety(stmt, allow_write=False, max_rows=100)
        assert lim is None and "LIMIT" not in sql.replace(stmt, "")


def test_enforce_safety_injects_offset_alongside_limit():
    sql, lim = enforce_safety("select * from t", allow_write=False, max_rows=100, offset=200)
    assert lim == 100 and sql.rstrip().endswith("LIMIT 101 OFFSET 200")


def test_enforce_safety_offset_zero_omits_offset_clause():
    sql, lim = enforce_safety("select * from t", allow_write=False, max_rows=100, offset=0)
    assert lim == 100 and "OFFSET" not in sql


def test_enforce_safety_ignores_offset_when_sql_has_own_limit():
    # grid "load more" never rewrites hand-written SQL that already has a LIMIT
    sql, lim = enforce_safety("select * from t limit 5", allow_write=False, max_rows=100, offset=10)
    assert lim is None and "OFFSET" not in sql


# ---- run_query: real Postgres ----

@requires_db
def test_run_query_returns_rows(ws):
    conn = core.get_connection("testpg")
    res = run_query(conn, "select id, name from customers order by id")
    assert res.row_count == 3
    assert res.rows[0]["name"] == "Alice"
    assert [c["name"] for c in res.columns] == ["id", "name"]
    assert res.engine == "postgres"
    assert res.elapsed_ms >= 0
    assert res.truncated is False
    assert res.download_bytes > 0
    assert res.size_is_estimated is True


@requires_db
def test_run_query_truncates(ws):
    conn = core.get_connection("testpg")
    res = run_query(conn, "select id from customers order by id", max_rows=2)
    assert res.row_count == 2 and res.truncated is True


@requires_db
def test_run_query_offset_pages_through_results(ws):
    # grid "load more": same SQL, growing offset, until the tail page isn't truncated
    conn = core.get_connection("testpg")
    page1 = run_query(conn, "select id from customers order by id", max_rows=2, offset=0)
    assert [r["id"] for r in page1.rows] == [1, 2] and page1.truncated is True
    page2 = run_query(conn, "select id from customers order by id", max_rows=2, offset=2)
    assert [r["id"] for r in page2.rows] == [3] and page2.truncated is False


@requires_db
def test_run_query_blocks_write(ws):
    conn = core.get_connection("testpg")
    with pytest.raises(QuarryError) as e:
        run_query(conn, "delete from orders")
    assert e.value.exit_code == core.EXIT_SAFETY_BLOCKED


@requires_db
def test_run_query_explain_returns_plan(ws):
    conn = core.get_connection("testpg")
    res = run_query(conn, "explain select * from orders")
    assert res.row_count > 0
    assert res.columns[0]["name"] == "QUERY PLAN"
    assert any("Seq Scan" in str(r["QUERY PLAN"]) for r in res.rows)


@requires_db
def test_run_query_sql_error_raises(ws):
    # regression: without ON_ERROR_STOP psql swallowed errors into empty results
    conn = core.get_connection("testpg")
    with pytest.raises(QuarryError) as e:
        run_query(conn, "select nonexistent_col from orders")
    assert e.value.exit_code == core.EXIT_SQL_ERROR
