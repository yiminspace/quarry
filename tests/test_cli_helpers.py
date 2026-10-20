"""Unit tests for CLI helpers that manipulate SQL text — `--limit` / `--full`
rewriting (must never corrupt SQL) and heterogeneous-row CSV rendering."""

from __future__ import annotations

import pytest

from quarry.cli import _override_limit, _strip_limit
from quarry.core import _csv_limit, rows_to_csv

pytestmark = pytest.mark.unit


# ---- _override_limit: replace the OUTER limit, else append ----

def test_override_appends_when_no_limit():
    assert _override_limit("SELECT * FROM t", 7) == "SELECT * FROM t\nLIMIT 7"


def test_override_replaces_top_level_limit():
    assert _override_limit("SELECT * FROM t LIMIT 100", 7) == "SELECT * FROM t LIMIT 7"


def test_override_keeps_offset_context_valid():
    # only the count is replaced; OFFSET clause is dropped-and-rewritten as LIMIT 7
    out = _override_limit("SELECT * FROM t LIMIT 100 OFFSET 20", 7)
    assert out == "SELECT * FROM t LIMIT 7"


def test_override_ignores_inner_subquery_limit():
    sql = "SELECT * FROM (SELECT id FROM t ORDER BY id LIMIT 100) s WHERE id > 5"
    out = _override_limit(sql, 10)
    # inner LIMIT 100 untouched; an outer LIMIT is appended
    assert "LIMIT 100) s" in out and out.rstrip().endswith("LIMIT 10")


def test_override_replaces_outer_not_inner():
    sql = "SELECT * FROM (SELECT id FROM t LIMIT 100) s LIMIT 50"
    out = _override_limit(sql, 10)
    assert "LIMIT 100) s" in out and out.rstrip().endswith("LIMIT 10")


# ---- _strip_limit: remove the OUTER limit (--full) ----

def test_strip_removes_top_level_limit():
    assert _strip_limit("SELECT * FROM t LIMIT 100") == "SELECT * FROM t"


def test_strip_leaves_inner_limit_alone():
    sql = "SELECT * FROM (SELECT id FROM t LIMIT 100) s WHERE id > 5"
    assert _strip_limit(sql) == sql  # no outer LIMIT to strip


def test_strip_noop_without_limit():
    assert _strip_limit("SELECT 1") == "SELECT 1"


# ---- rows_to_csv: heterogeneous rows must not crash ----

def test_rows_to_csv_uniform():
    out = rows_to_csv([{"a": 1, "b": 2}, {"a": 3, "b": 4}])
    assert out.splitlines()[0] == "a,b"


def test_rows_to_csv_heterogeneous_rows():
    # a later row with an extra key used to raise ValueError from DictWriter
    rows = [{"a": 1}, {"a": 2, "b": 9}]
    out = rows_to_csv(rows)
    lines = out.splitlines()
    assert lines[0] == "a,b"          # union of keys
    assert lines[1] == "1,"            # missing key -> empty


def test_rows_to_csv_empty():
    assert rows_to_csv([]) == ""


def test_csv_limit_keeps_header_plus_n():
    text = "id,name\n1,a\n2,b\n3,c\n"
    kept = _csv_limit(text, 2).splitlines()
    assert kept[0] == "id,name" and len(kept) == 3  # header + 2 rows


def test_csv_limit_quote_safe():
    # embedded newline inside a quoted field must not be miscounted as a row
    text = 'id,note\r\n1,"a\nb"\r\n2,c\r\n'
    kept = _csv_limit(text, 1)
    rows = kept.strip().splitlines()
    assert rows[0].startswith("id,note")
