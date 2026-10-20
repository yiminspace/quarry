"""Safety-rail regression tests — the read-only guarantee and auto-LIMIT.

These lock down bugs found in the audit:
  * multi-statement bypass    `EXPLAIN SELECT 1; DROP TABLE t` used to run the DROP
  * data-modifying CTE bypass `WITH d AS (DELETE ... RETURNING *) SELECT ...`
  * `LIMIT` inside a string literal falsely counted as "already limited"
  * `FETCH FIRST` / `FOR UPDATE` corrupted by a blindly-appended `LIMIT`
  * `substitute_params` re-substituting a value that contained another `:param`
"""

from __future__ import annotations

import pytest

from quarry import core
from quarry.core import (
    QuarryError,
    enforce_safety,
    has_limit,
    is_read_only,
    sql_skeleton,
    substitute_params,
)
from conftest import requires_db

# Pure tests below are auto-marked `unit` (they use no DB fixtures); the
# DB-backed tests at the bottom carry an explicit `integration` marker.


# ---------------------------------------------------------------------------
# is_read_only — the core guarantee
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("sql", [
    "select * from t",
    "  SELECT 1",
    "with x as (select 1) select * from x",
    "-- a comment\nselect 2",
    "/* block */ SELECT 3",
    "SHOW tables",
    "EXPLAIN select 1",
    "VALUES (1),(2)",
    "SELECT 1;",                                  # single trailing semicolon is fine
    "SELECT * FROM t WHERE note = 'DELETE FROM x'",  # write word only inside a string
    "SELECT * FROM t WHERE note = 'a; b; c'",     # semicolons only inside a string
])
def test_reads_allowed(sql):
    assert is_read_only(sql) is True


@pytest.mark.parametrize("sql", [
    "insert into t values (1)",
    "UPDATE t SET x=1",
    "delete from t",
    "drop table t",
    "ALTER TABLE t ADD COLUMN c int",
    "truncate t",
    "-- sneaky\nDELETE FROM t",
    "/* c */ /* c2 */ drop table t",
    "COPY t FROM '/etc/passwd'",
])
def test_writes_blocked(sql):
    assert is_read_only(sql) is False


@pytest.mark.parametrize("sql", [
    "EXPLAIN SELECT 1; DROP TABLE t",
    "SELECT 1; DROP TABLE t",
    "SHOW search_path; DELETE FROM t",
    "SELECT 1 ; ; UPDATE t SET x = 1",
])
def test_multi_statement_blocked(sql):
    """Multiple top-level statements can never be read-only (psql runs them all)."""
    assert is_read_only(sql) is False


@pytest.mark.parametrize("sql", [
    "WITH d AS (DELETE FROM t RETURNING id) SELECT * FROM d",
    "with x as (insert into t values (1) returning id) select * from x",
    "WITH a AS (SELECT 1), b AS (UPDATE t SET x=1 RETURNING *) SELECT * FROM b",
])
def test_data_modifying_cte_blocked(sql):
    assert is_read_only(sql) is False


def test_read_only_cte_still_allowed():
    assert is_read_only("WITH d AS (SELECT 1 AS n) SELECT * FROM d") is True


# ---------------------------------------------------------------------------
# has_limit — must ignore LIMIT inside strings, recognise FETCH FIRST
# ---------------------------------------------------------------------------

def test_has_limit_true_for_real_limit():
    assert has_limit("SELECT * FROM t LIMIT 10") is True


def test_has_limit_true_for_fetch_first():
    assert has_limit("SELECT * FROM t FETCH FIRST 5 ROWS ONLY") is True
    assert has_limit("SELECT * FROM t FETCH NEXT 5 ROWS ONLY") is True


def test_has_limit_false_for_limit_in_string():
    assert has_limit("SELECT * FROM logs WHERE level = 'LIMIT'") is False


# ---------------------------------------------------------------------------
# enforce_safety — blocks writes, injects a valid LIMIT, never corrupts SQL
# ---------------------------------------------------------------------------

def test_enforce_blocks_write_by_default():
    with pytest.raises(QuarryError) as ei:
        enforce_safety("DELETE FROM t", allow_write=False, max_rows=500)
    assert ei.value.exit_code == core.EXIT_SAFETY_BLOCKED


def test_enforce_allows_write_with_flag():
    sql, applied = enforce_safety("DELETE FROM t", allow_write=True, max_rows=500)
    assert sql == "DELETE FROM t" and applied is None


def test_enforce_injects_limit():
    sql, applied = enforce_safety("SELECT * FROM t", allow_write=False, max_rows=100)
    assert sql.endswith("LIMIT 101") and applied == 100


def test_enforce_keeps_existing_limit():
    sql, applied = enforce_safety("SELECT * FROM t LIMIT 5", allow_write=False, max_rows=100)
    assert sql == "SELECT * FROM t LIMIT 5" and applied is None


def test_enforce_skips_limit_for_explain_and_show():
    for stmt in ("EXPLAIN SELECT 1", "SHOW all"):
        sql, applied = enforce_safety(stmt, allow_write=False, max_rows=100)
        assert applied is None and "LIMIT" not in sql.upper()


def test_enforce_does_not_append_limit_after_fetch_first():
    sql, applied = enforce_safety("SELECT * FROM t FETCH FIRST 5 ROWS ONLY",
                                  allow_write=False, max_rows=100)
    assert "LIMIT" not in sql.upper() and applied is None


def test_enforce_does_not_append_limit_after_for_update():
    sql, applied = enforce_safety("SELECT * FROM t WHERE id = 1 FOR UPDATE",
                                  allow_write=False, max_rows=100)
    assert "LIMIT" not in sql.upper() and applied is None


# ---------------------------------------------------------------------------
# sql_skeleton — the primitive the rails are built on
# ---------------------------------------------------------------------------

def test_skeleton_blanks_strings_and_comments():
    sk = sql_skeleton("SELECT 'a; DROP' /* c */ -- x\nFROM t")
    assert ";" not in sk and "DROP" not in sk and "FROM t" in sk


def test_skeleton_handles_escaped_quote():
    # doubled '' is an escaped quote, not a string terminator
    sk = sql_skeleton("SELECT 'it''s; ok' FROM t")
    assert ";" not in sk and "FROM t" in sk


def test_skeleton_handles_dollar_quotes():
    sk = sql_skeleton("SELECT $$a; DROP TABLE t$$ FROM t")
    assert "DROP" not in sk and ";" not in sk


# ---------------------------------------------------------------------------
# substitute_params — single left-to-right pass, proper escaping
# ---------------------------------------------------------------------------

def test_quoted_param_is_escaped():
    out = substitute_params("WHERE x = :'v'", {"v": "O'Brien"})
    assert out == "WHERE x = 'O''Brien'"


def test_quoted_param_escapes_backslash():
    out = substitute_params("WHERE x = :'v'", {"v": "a\\b"})
    assert out == "WHERE x = 'a\\\\b'"


def test_value_containing_param_token_not_resubstituted():
    # value of :name is "a:id" — the trailing :id must NOT be re-expanded
    out = substitute_params("WHERE a = :'name' AND b = :id", {"name": "a:id", "id": "999"})
    assert out == "WHERE a = 'a:id' AND b = 999"


def test_unknown_params_left_intact():
    assert substitute_params("WHERE x = :missing", {}) == "WHERE x = :missing"


# ---------------------------------------------------------------------------
# integration: prove the blocked statements really never execute
# ---------------------------------------------------------------------------

@requires_db
@pytest.mark.integration
def test_multi_statement_drop_is_not_executed(ws, pg_exec):
    pg_exec("DROP TABLE IF EXISTS qy_zap; CREATE TABLE qy_zap(id int);")
    conn = core.resolve_connection("testpg", "test")
    with pytest.raises(QuarryError) as ei:
        core.run_query(conn, "EXPLAIN SELECT 1; DROP TABLE qy_zap")
    assert ei.value.exit_code == core.EXIT_SAFETY_BLOCKED
    rc, out, _ = pg_exec("SELECT count(*) FROM information_schema.tables WHERE table_name='qy_zap';")
    assert out.strip() == "1", "table qy_zap must still exist (DROP must not have run)"
    pg_exec("DROP TABLE IF EXISTS qy_zap;")


@requires_db
@pytest.mark.integration
def test_cte_delete_is_not_executed(ws, pg_exec):
    pg_exec("DROP TABLE IF EXISTS qy_zap2; CREATE TABLE qy_zap2(id int); "
            "INSERT INTO qy_zap2 VALUES (1),(2),(3);")
    conn = core.resolve_connection("testpg", "test")
    with pytest.raises(QuarryError):
        core.run_query(conn, "WITH d AS (DELETE FROM qy_zap2 RETURNING id) SELECT * FROM d")
    rc, out, _ = pg_exec("SELECT count(*) FROM qy_zap2;")
    assert out.strip() == "3", "rows must be intact (CTE DELETE must not have run)"
    pg_exec("DROP TABLE IF EXISTS qy_zap2;")


@requires_db
@pytest.mark.integration
def test_fetch_first_runs_without_corruption(ws):
    conn = core.resolve_connection("testpg", "test")
    res = core.run_query(conn, "SELECT * FROM customers FETCH FIRST 2 ROWS ONLY")
    assert len(res.rows) == 2
