"""Gap-filling tests for quarry.core.

Focus: output emitters, serialization, URL parsers (mysql/neptune), engine
inference, connection loading error paths, query-file metadata parsing,
fingerprinting, param resolution, and the mysql/neptune engine drivers (via
fakes/mocks). A few @requires_db tests exercise the real psql path in-process.

Existing test_core.py / test_safety_rails.py / test_groups.py already cover the
safety rails and resolve_connection basics — those are not repeated here.
"""

from __future__ import annotations

import io
import json
import ssl
import sys
import types
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from quarry import core, workspace  # noqa: E402
from conftest import TEST_DB_URL, requires_db  # noqa: E402


# ===========================================================================
# Output emitters
# ===========================================================================

@pytest.mark.unit
def test_emit_rows_json_non_tty(capsys, monkeypatch):
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False, raising=False)
    core.emit_rows_json([{"a": 1}, {"a": 2}])
    out = capsys.readouterr().out
    assert json.loads(out) == [{"a": 1}, {"a": 2}]
    assert out.endswith("\n")
    # compact (non-tty) form has no indentation newlines inside the array
    assert out.count("\n") == 1


@pytest.mark.unit
def test_emit_rows_json_tty_is_indented(capsys, monkeypatch):
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True, raising=False)
    core.emit_rows_json([{"a": 1}])
    out = capsys.readouterr().out
    assert json.loads(out) == [{"a": 1}]
    assert "  " in out  # indent=2 pretty-print


@pytest.mark.unit
def test_emit_rows_ndjson(capsys):
    core.emit_rows_ndjson([{"a": 1}, {"b": 2}])
    lines = capsys.readouterr().out.splitlines()
    assert [json.loads(x) for x in lines] == [{"a": 1}, {"b": 2}]


@pytest.mark.unit
def test_emit_csv_adds_trailing_newline(capsys):
    core.emit_csv("a,b\n1,2")  # no trailing newline
    out = capsys.readouterr().out
    assert out == "a,b\n1,2\n"


@pytest.mark.unit
def test_emit_csv_keeps_existing_newline(capsys):
    core.emit_csv("a,b\n1,2\n")
    assert capsys.readouterr().out == "a,b\n1,2\n"


@pytest.mark.unit
def test_emit_table_alignment_and_separator(capsys):
    core.emit_table("name,age\nalice,30\nbo,7\n")
    lines = capsys.readouterr().out.splitlines()
    # header padded to widest cell in each column ('name'/'alice' -> 5, 'age' -> 3)
    assert lines[0] == "name   age"
    assert lines[1] == "-----  ---"
    assert lines[2] == "alice  30 "
    assert lines[3] == "bo     7  "


@pytest.mark.unit
def test_emit_table_empty_is_noop(capsys):
    core.emit_table("")
    assert capsys.readouterr().out == ""


@pytest.mark.unit
def test_emit_table_short_row_padded(capsys):
    # a data row with fewer cells than the header must be padded, not crash
    core.emit_table("a,b\nx\n")
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "a  b"
    assert lines[2] == "x   "  # missing 2nd cell -> blank padded


@pytest.mark.unit
def test_emit_rows_csv(capsys):
    core.emit_rows_csv([{"a": 1, "b": 2}])
    assert capsys.readouterr().out == "a,b\r\n1,2\r\n"


@pytest.mark.unit
def test_emit_rows_table(capsys):
    core.emit_rows_table([{"x": "hi", "y": "yo"}])
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].startswith("x")
    assert "hi" in lines[2] and "yo" in lines[2]


@pytest.mark.unit
def test_emit_json_valid(capsys, monkeypatch):
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False, raising=False)
    core.emit_json('[{"a": 1}]')
    assert json.loads(capsys.readouterr().out) == [{"a": 1}]


@pytest.mark.unit
def test_emit_json_empty_becomes_empty_list(capsys, monkeypatch):
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False, raising=False)
    core.emit_json("   ")
    assert json.loads(capsys.readouterr().out) == []


@pytest.mark.unit
def test_emit_json_non_json_fallback(capsys):
    # not valid JSON -> printed verbatim with a trailing newline
    core.emit_json("ERROR: boom")
    assert capsys.readouterr().out == "ERROR: boom\n"


@pytest.mark.unit
def test_emit_json_tty_indented(capsys, monkeypatch):
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True, raising=False)
    core.emit_json('[{"a":1}]')
    assert "  " in capsys.readouterr().out


@pytest.mark.unit
def test_emit_ndjson_list(capsys):
    core.emit_ndjson('[{"a":1},{"b":2}]')
    lines = capsys.readouterr().out.splitlines()
    assert [json.loads(x) for x in lines] == [{"a": 1}, {"b": 2}]


@pytest.mark.unit
def test_emit_ndjson_wraps_scalar_object(capsys):
    # a single (non-list) JSON object gets wrapped into one ndjson row
    core.emit_ndjson('{"a":1}')
    lines = capsys.readouterr().out.splitlines()
    assert [json.loads(x) for x in lines] == [{"a": 1}]


@pytest.mark.unit
def test_emit_ndjson_non_json_fallback(capsys):
    core.emit_ndjson("not json")
    assert capsys.readouterr().out == "not json\n"


# ===========================================================================
# rows_to_csv / _csv_limit
# ===========================================================================

@pytest.mark.unit
def test_rows_to_csv_uniform():
    assert core.rows_to_csv([{"a": 1, "b": 2}, {"a": 3, "b": 4}]) == "a,b\r\n1,2\r\n3,4\r\n"


@pytest.mark.unit
def test_rows_to_csv_heterogeneous_keys():
    # union of keys, in first-seen order; missing cells become empty (restval="")
    out = core.rows_to_csv([{"a": 1, "b": 2}, {"a": 3, "c": 4}])
    assert out == "a,b,c\r\n1,2,\r\n3,,4\r\n"


@pytest.mark.unit
def test_rows_to_csv_empty():
    assert core.rows_to_csv([]) == ""


@pytest.mark.unit
def test_csv_limit_header_plus_n():
    text = "h1,h2\r\n1,2\r\n3,4\r\n5,6\r\n"
    assert core._csv_limit(text, 2) == "h1,h2\r\n1,2\r\n3,4\r\n"


@pytest.mark.unit
def test_csv_limit_quote_safe():
    # a quoted field containing an embedded newline is ONE logical row
    text = 'h\r\n"a\nb"\r\nc\r\n'
    out = core._csv_limit(text, 1)
    parsed = list(__import__("csv").reader(io.StringIO(out)))
    assert parsed == [["h"], ["a\nb"]]


@pytest.mark.unit
def test_csv_limit_empty_returns_input():
    assert core._csv_limit("", 5) == ""


# ===========================================================================
# serialize_row
# ===========================================================================

@pytest.mark.unit
def test_serialize_row_datetime():
    out = core.serialize_row({"dt": datetime(2021, 1, 2, 3, 4, 5)})
    assert out["dt"] == "2021-01-02 03:04:05"


@pytest.mark.unit
def test_serialize_row_bare_date():
    # regression: a bare `date` must serialize to ISO (used to crash — date.isoformat
    # rejects the sep/timespec kwargs that only datetime accepts).
    out = core.serialize_row({"d": date(2021, 6, 7)})
    assert out["d"] == "2021-06-07"


@pytest.mark.unit
def test_serialize_row_decimal_becomes_float():
    out = core.serialize_row({"amount": Decimal("12.50")})
    assert out["amount"] == 12.5
    assert isinstance(out["amount"], float)


@pytest.mark.unit
def test_serialize_row_bytes_decoded():
    out = core.serialize_row({"b": b"hello"})
    assert out["b"] == "hello"


@pytest.mark.unit
def test_serialize_row_bytes_invalid_utf8_replaced():
    out = core.serialize_row({"b": b"\xff\xfe"})
    assert out["b"] == "��"  # errors="replace"


@pytest.mark.unit
def test_serialize_row_memoryview_decoded():
    # regression: memoryview/bytearray (pymysql BLOB/BINARY) are now decoded like bytes
    assert core.serialize_row({"b": memoryview(b"hi")})["b"] == "hi"
    assert core.serialize_row({"b": bytearray(b"yo")})["b"] == "yo"


@pytest.mark.unit
def test_serialize_row_nested_passthrough():
    row = {"d": {"x": 1}, "l": [1, 2], "n": None, "i": 5}
    out = core.serialize_row(row)
    assert out == {"d": {"x": 1}, "l": [1, 2], "n": None, "i": 5}


# ===========================================================================
# parse_mysql_url
# ===========================================================================

@pytest.mark.unit
def test_parse_mysql_url_basic():
    cfg = core.parse_mysql_url("mysql://user:pass@dbhost:3307/shop")
    assert cfg == {
        "host": "dbhost", "port": 3307, "user": "user",
        "password": "pass", "database": "shop",
    }


@pytest.mark.unit
def test_parse_mysql_url_driver_normalized():
    cfg = core.parse_mysql_url("mysql+pymysql://user:pass@h/db")
    assert cfg["host"] == "h"
    assert cfg["database"] == "db"


@pytest.mark.unit
def test_parse_mysql_url_default_port_and_percent_decode():
    cfg = core.parse_mysql_url("mysql://us%40er:p%40ss@h/mydb")
    assert cfg["port"] == 3306
    assert cfg["user"] == "us@er"
    assert cfg["password"] == "p@ss"


@pytest.mark.unit
def test_parse_mysql_url_missing_database_errors():
    with pytest.raises(core.QuarryError) as ei:
        core.parse_mysql_url("mysql://user:pass@host/")
    assert ei.value.exit_code == core.EXIT_USAGE


@pytest.mark.unit
def test_parse_mysql_url_wrong_scheme_errors():
    with pytest.raises(core.QuarryError) as ei:
        core.parse_mysql_url("postgres://h/db")
    assert ei.value.exit_code == core.EXIT_USAGE


# ===========================================================================
# Neptune endpoint / helpers
# ===========================================================================

@pytest.mark.unit
def test_normalize_neptune_bare_host_defaults_https_8182():
    assert core.normalize_neptune_endpoint("h.example.com") == "https://h.example.com:8182"


@pytest.mark.unit
def test_normalize_neptune_explicit_http():
    assert core.normalize_neptune_endpoint("http://h.example.com") == "http://h.example.com:8182"


@pytest.mark.unit
def test_normalize_neptune_custom_port_and_path():
    assert core.normalize_neptune_endpoint("https://h:9999/graph/") == "https://h:9999/graph"


@pytest.mark.unit
def test_normalize_neptune_empty_errors():
    with pytest.raises(core.QuarryError) as ei:
        core.normalize_neptune_endpoint("   ")
    assert ei.value.exit_code == core.EXIT_USAGE


@pytest.mark.unit
def test_normalize_neptune_bad_scheme_errors():
    with pytest.raises(core.QuarryError) as ei:
        core.normalize_neptune_endpoint("ftp://h:8182")
    assert ei.value.exit_code == core.EXIT_USAGE


@pytest.mark.unit
def test_neptune_cypher_url_appends_and_is_idempotent():
    assert core._neptune_cypher_url("https://h:8182") == "https://h:8182/openCypher"
    assert core._neptune_cypher_url("https://h:8182/openCypher") == "https://h:8182/openCypher"


@pytest.mark.unit
def test_extract_neptune_rows_list():
    assert core._extract_neptune_rows([{"a": 1}, 2]) == [{"a": 1}, {"value": 2}]


@pytest.mark.unit
def test_is_loopback_host():
    assert core._is_loopback_host("127.0.0.1")
    assert core._is_loopback_host("localhost")
    assert core._is_loopback_host("::1")
    assert not core._is_loopback_host("h.example.com")
    assert not core._is_loopback_host("10.0.0.1")
    assert not core._is_loopback_host(None)
    assert not core._is_loopback_host("")


@pytest.mark.unit
class TestCheckConnectionWrite:
    """issue #76: deterministic guardrails for `connections add`/`set`."""

    def test_port_conflict_blocks_without_force(self):
        existing = {"shop_local": {"url": "postgresql://127.0.0.1:5433/shop",
                                    "local_image": "postgres:16-alpine"}}
        with pytest.raises(core.QuarryError) as ei:
            core.check_connection_write(
                "shop", {"url": "postgresql://localhost:5433/shop", "engine": "postgres"}, existing)
        assert ei.value.exit_code == core.EXIT_USAGE
        assert "shop_local" in str(ei.value) and "postgres:16-alpine" in str(ei.value)

    def test_port_conflict_prefers_notes_over_local_image(self):
        existing = {"other": {"url": "postgresql://localhost:5432/x",
                               "notes": "shared staging db", "local_image": "img"}}
        with pytest.raises(core.QuarryError) as ei:
            core.check_connection_write("new", {"url": "postgresql://localhost:5432/y"}, existing)
        assert "shared staging db" in str(ei.value)

    def test_port_conflict_force_warns_instead_of_raising(self, capsys):
        existing = {"other": {"url": "postgresql://localhost:5432/x"}}
        core.check_connection_write("new", {"url": "postgresql://localhost:5432/y"}, existing, force=True)
        assert "already used by connection [other]" in capsys.readouterr().err

    def test_no_conflict_on_different_port(self):
        existing = {"other": {"url": "postgresql://localhost:5432/x"}}
        core.check_connection_write("new", {"url": "postgresql://localhost:5433/y"}, existing)  # no raise

    def test_self_key_excluded_from_conflict_scan(self):
        existing = {"self": {"url": "postgresql://localhost:5432/old"}}
        core.check_connection_write("self", {"url": "postgresql://localhost:5432/new"}, existing)  # no raise

    def test_loopback_alias_conflicts_with_127_0_0_1(self):
        existing = {"shop_local": {"url": "postgresql://127.0.0.1:5433/shop"}}
        with pytest.raises(core.QuarryError):
            core.check_connection_write("shop", {"url": "postgresql://localhost:5433/shop"}, existing)

    def test_neptune_bare_endpoint_port_conflict(self):
        # Neptune connections accept bare `host:port` (no scheme); the same
        # host:port guardrail must still catch it (see normalize_neptune_endpoint).
        existing = {"graph_local": {"url": "https://127.0.0.1:8182", "engine": "neptune",
                                     "local_image": "amazon/neptune"}}
        with pytest.raises(core.QuarryError) as ei:
            core.check_connection_write(
                "graph", {"url": "localhost:8182", "engine": "neptune"}, existing)
        assert ei.value.exit_code == core.EXIT_USAGE
        assert "graph_local" in str(ei.value)

    def test_neptune_bare_endpoint_loopback_warns(self, capsys):
        core.check_connection_write("graph", {"url": "localhost:8182", "engine": "neptune"}, {})
        err = capsys.readouterr().err
        assert "loopback" in err and "--ssh-host" in err

    def test_loopback_without_ssh_warns(self, capsys):
        core.check_connection_write("new", {"url": "postgresql://localhost:5555/x"}, {})
        err = capsys.readouterr().err
        assert "loopback" in err and "--ssh-host" in err

    def test_loopback_with_ssh_host_no_warning(self, capsys):
        core.check_connection_write(
            "new", {"url": "postgresql://localhost:5555/x", "ssh_host": "bastion"}, {})
        assert capsys.readouterr().err == ""

    def test_remote_host_no_loopback_warning(self, capsys):
        core.check_connection_write("new", {"url": "postgresql://db.internal:5432/x"}, {})
        assert capsys.readouterr().err == ""

    def test_env_local_naming_hint(self, capsys):
        core.check_connection_write(
            "shop", {"url": "postgresql://db.internal:5432/x", "env": "local"}, {})
        assert "shop_local" in capsys.readouterr().err

    def test_env_local_key_with_suffix_no_hint(self, capsys):
        core.check_connection_write(
            "shop_local", {"url": "postgresql://db.internal:5432/x", "env": "local"}, {})
        assert capsys.readouterr().err == ""

    def test_proxy_enabled_no_ssh_host_warns(self, monkeypatch, capsys):
        monkeypatch.setattr(core.workspace, "is_proxy_enabled", lambda home: True)
        core.check_connection_write(
            "shop", {"url": "postgresql://db.internal:5432/x"}, {})
        err = capsys.readouterr().err
        assert "proxy enabled" in err and "shop" in err and "ssh_host" in err

    def test_proxy_enabled_with_ssh_host_no_warning(self, monkeypatch, capsys):
        monkeypatch.setattr(core.workspace, "is_proxy_enabled", lambda home: True)
        core.check_connection_write(
            "shop", {"url": "postgresql://db.internal:5432/x", "ssh_host": "bastion"}, {})
        assert "proxy enabled" not in capsys.readouterr().err

    def test_proxy_enabled_neptune_no_ssh_host_no_warning(self, monkeypatch, capsys):
        # Neptune reaches its endpoint directly over HTTPS — the proxy still
        # applies there (see run_neptune_cypher), so no ssh_host is needed.
        monkeypatch.setattr(core.workspace, "is_proxy_enabled", lambda home: True)
        core.check_connection_write(
            "graph", {"url": "https://graph.example.com:8182", "engine": "neptune"}, {})
        assert "proxy enabled" not in capsys.readouterr().err

    def test_proxy_disabled_no_ssh_host_no_warning(self, monkeypatch, capsys):
        monkeypatch.setattr(core.workspace, "is_proxy_enabled", lambda home: False)
        core.check_connection_write(
            "shop", {"url": "postgresql://db.internal:5432/x"}, {})
        assert "proxy enabled" not in capsys.readouterr().err


@pytest.mark.unit
def test_neptune_ssl_context_loopback_skips_verification():
    ctx = core._neptune_ssl_context("127.0.0.1")
    assert ctx is not None and ctx.verify_mode == ssl.CERT_NONE


@pytest.mark.unit
def test_neptune_ssl_context_real_host_verifies():
    assert core._neptune_ssl_context("h.example.com") is None


@pytest.mark.unit
def test_neptune_ssl_context_env_override(monkeypatch):
    monkeypatch.setattr(core, "NEPTUNE_INSECURE", True)
    ctx = core._neptune_ssl_context("h.example.com")
    assert ctx is not None and ctx.verify_mode == ssl.CERT_NONE


@pytest.mark.unit
def test_extract_neptune_rows_results_key():
    assert core._extract_neptune_rows({"results": [{"a": 1}]}) == [{"a": 1}]


@pytest.mark.unit
def test_extract_neptune_rows_result_key():
    assert core._extract_neptune_rows({"result": ["x"]}) == [{"value": "x"}]


@pytest.mark.unit
def test_extract_neptune_rows_scalar_and_plain_dict():
    assert core._extract_neptune_rows(42) == [{"value": 42}]
    # a dict without results/result is normalized as a single row (already a dict)
    assert core._extract_neptune_rows({"other": 1}) == [{"other": 1}]


@pytest.mark.unit
def test_normalize_row():
    assert core._normalize_row({"a": 1}) == {"a": 1}
    assert core._normalize_row("z") == {"value": "z"}


# ===========================================================================
# infer_engine / connection_engine / get_connection
# ===========================================================================

@pytest.mark.unit
def test_infer_engine_postgres_default():
    assert core.infer_engine("postgresql://h/db") == "postgres"
    assert core.infer_engine("anything-else") == "postgres"


@pytest.mark.unit
def test_infer_engine_mysql_variants():
    assert core.infer_engine("mysql://h/db") == "mysql"
    assert core.infer_engine("mysql+pymysql://h/db") == "mysql"
    assert core.infer_engine("MYSQL://H/DB") == "mysql"  # case-insensitive


@pytest.mark.unit
def test_infer_engine_redis_variants():
    assert core.infer_engine("redis://h") == "redis"
    assert core.infer_engine("rediss://h") == "redis"


@pytest.mark.unit
def test_infer_engine_neptune_host_heuristic():
    assert core.infer_engine("wss://x.cluster.neptune.amazonaws.com:8182") == "neptune"


@pytest.mark.unit
def test_infer_engine_explicit_wins_and_normalizes():
    assert core.infer_engine("mysql://h/db", "postgres") == "postgres"
    assert core.infer_engine("x", "  MySQL ") == "mysql"


@pytest.mark.unit
def test_infer_engine_explicit_junk_rejected():
    with pytest.raises(core.QuarryError) as ei:
        core.infer_engine("x", "sqlite")
    assert ei.value.exit_code == core.EXIT_USAGE


@pytest.mark.unit
def test_connection_engine_uses_url_and_explicit():
    c = core.Connection(key="k", url="mysql://h/db", engine="postgres")
    assert core.connection_engine(c) == "postgres"  # explicit engine field wins
    c2 = core.Connection(key="k", url="mysql://h/db", engine="")
    assert core.connection_engine(c2) == "mysql"


@pytest.mark.unit
def test_get_connection_missing_key_errors(ws):
    with pytest.raises(core.QuarryError) as ei:
        core.get_connection("does_not_exist")
    assert ei.value.exit_code == core.EXIT_USAGE
    assert "testpg" in str(ei.value)  # lists available keys


@pytest.mark.unit
def test_get_connection_hit(ws):
    c = core.get_connection("testpg")
    assert c.key == "testpg"
    assert c.engine == "postgres"


# ===========================================================================
# load_connections error paths + group_connections
# ===========================================================================

def _configure(dirpath: Path):
    workspace.configure_workspace(str(dirpath))


@pytest.mark.unit
def test_load_connections_missing_file_errors(tmp_path):
    # a workspace dir with NO connections.toml
    try:
        _configure(tmp_path)
        with pytest.raises(core.QuarryError) as ei:
            core.load_connections()
        assert ei.value.exit_code == core.EXIT_USAGE
        assert "connections file not found" in str(ei.value)
    finally:
        workspace.configure_workspace(None)


@pytest.mark.unit
def test_load_connections_missing_url_errors(tmp_path):
    (tmp_path / "connections.toml").write_text('[bad]\nregion = "us"\n', encoding="utf-8")
    try:
        _configure(tmp_path)
        with pytest.raises(core.QuarryError) as ei:
            core.load_connections()
        assert ei.value.exit_code == core.EXIT_USAGE
        assert "missing required 'url'" in str(ei.value)
    finally:
        workspace.configure_workspace(None)


@pytest.mark.unit
def test_group_connections_env_set_grouping(tmp_path):
    (tmp_path / "connections.toml").write_text(
        '[shop_dev]\nurl = "postgres://h/d1"\ngroup = "shop"\ndb = "shop"\nenv = "dev"\n'
        '[shop_prod]\nurl = "postgres://h/d2"\ngroup = "shop"\ndb = "shop"\nenv = "prod"\n'
        '[solo]\nurl = "mysql://h/s"\ngroup = "misc"\n',
        encoding="utf-8",
    )
    try:
        _configure(tmp_path)
        groups = core.group_connections()
        by_group = {g["group"]: g for g in groups}
        shop = by_group["shop"]["items"][0]
        assert shop["db"] == "shop"
        assert shop["is_env_set"] is True
        assert {e["env"] for e in shop["envs"]} == {"dev", "prod"}
        misc = by_group["misc"]["items"][0]
        # single member, no env -> not an env-set
        assert misc["is_env_set"] is False
        assert misc["engine"] == "mysql"
    finally:
        workspace.configure_workspace(None)


# ===========================================================================
# Query metadata: parse_query_file / find_query_file / list_all_queries
# ===========================================================================

def _write_query(dirpath: Path, name: str, text: str) -> Path:
    qdir = dirpath / "queries"
    qdir.mkdir(exist_ok=True)
    p = qdir / f"{name}.sql"
    p.write_text(text, encoding="utf-8")
    return p


@pytest.mark.unit
def test_parse_query_file_full_meta(tmp_path):
    p = _write_query(
        tmp_path, "top_customers",
        "-- @name: top_customers\n"
        "-- @db: shop\n"
        "-- @desc: top customers\n"
        "-- @desc: by amount\n"
        "-- @tags: sales, weekly\n"
        "-- @param: limit_n (int, default=10)\n"
        "-- @param: region (text, required)\n"
        "-- @schema-source: schema/shop.sql\n"
        "-- a plain leading comment (not meta)\n"
        "SELECT * FROM customers LIMIT :limit_n;\n",
    )
    q = core.parse_query_file(p)
    assert q.name == "top_customers"
    assert q.db == "shop"
    assert q.desc == "top customers by amount"
    assert q.tags == ["sales", "weekly"]
    assert q.schema_sources == ["schema/shop.sql"]
    assert {pp.name: (pp.type, pp.required, pp.default) for pp in q.params} == {
        "limit_n": ("int", False, "10"),
        "region": ("text", True, None),
    }
    # a non-meta leading comment is kept as part of the body (only recognized
    # `-- @key:` meta lines are stripped from the header).
    assert "plain leading comment" in q.sql
    assert "SELECT * FROM customers" in q.sql


@pytest.mark.unit
def test_parse_query_file_name_mismatch_errors(tmp_path):
    p = _write_query(tmp_path, "actual",
                     "-- @name: other\n-- @db: shop\nSELECT 1;\n")
    with pytest.raises(core.QuarryError) as ei:
        core.parse_query_file(p)
    assert ei.value.exit_code == core.EXIT_USAGE
    assert "does not match filename stem" in str(ei.value)


@pytest.mark.unit
def test_parse_query_file_missing_name_errors(tmp_path):
    p = _write_query(tmp_path, "q1", "-- @db: shop\nSELECT 1;\n")
    with pytest.raises(core.QuarryError) as ei:
        core.parse_query_file(p)
    assert ei.value.exit_code == core.EXIT_USAGE
    assert "missing @name or @db" in str(ei.value)


@pytest.mark.unit
def test_parse_query_file_missing_db_errors(tmp_path):
    p = _write_query(tmp_path, "q1", "-- @name: q1\nSELECT 1;\n")
    with pytest.raises(core.QuarryError) as ei:
        core.parse_query_file(p)
    assert ei.value.exit_code == core.EXIT_USAGE


@pytest.mark.unit
def test_has_limit_property():
    assert core.Query(name="q", db="d", sql="SELECT 1 LIMIT 5").has_limit is True
    assert core.Query(name="q", db="d", sql="select * from t limit 3").has_limit is True
    assert core.Query(name="q", db="d", sql="SELECT 1").has_limit is False


@pytest.mark.unit
def test_find_query_file_missing_errors(tmp_path):
    try:
        _configure(tmp_path)
        (tmp_path / "queries").mkdir(exist_ok=True)
        with pytest.raises(core.QuarryError) as ei:
            core.find_query_file("nope")
        assert ei.value.exit_code == core.EXIT_USAGE
        assert "not found" in str(ei.value)
    finally:
        workspace.configure_workspace(None)


@pytest.mark.unit
def test_find_query_file_ambiguous_across_dirs_errors(tmp_path):
    # two workspaces each containing dup.sql -> ambiguous
    w1 = tmp_path / "w1"
    w2 = tmp_path / "w2"
    for w in (w1, w2):
        w.mkdir()
        (w / "connections.toml").write_text('[k]\nurl = "postgres://h/d"\n', encoding="utf-8")
        _write_query(w, "dup", "-- @name: dup\n-- @db: d\nSELECT 1;\n")
    import os as _os
    try:
        workspace.configure_workspace(str(w1) + _os.pathsep + str(w2))
        with pytest.raises(core.QuarryError) as ei:
            core.find_query_file("dup")
        assert ei.value.exit_code == core.EXIT_USAGE
        assert "ambiguous" in str(ei.value)
    finally:
        workspace.configure_workspace(None)


@pytest.mark.unit
def test_list_all_queries_warn_skips_malformed(tmp_path, capsys):
    _write_query(tmp_path, "good", "-- @name: good\n-- @db: d\nSELECT 1;\n")
    # malformed: @name mismatch -> parse raises QuarryError, caught + warn-skipped
    _write_query(tmp_path, "bad", "-- @name: WRONG\n-- @db: d\nSELECT 1;\n")
    try:
        _configure(tmp_path)
        qs = core.list_all_queries()
        names = {q.name for q in qs}
        assert "good" in names
        assert "WRONG" not in names and "bad" not in names
        assert "failed to parse" in capsys.readouterr().err
    finally:
        workspace.configure_workspace(None)


@pytest.mark.unit
def test_param_to_meta_value_roundtrip():
    assert core.Param("n", "int", required=True).to_meta_value() == "n (int, required)"
    assert core.Param("n", "text", default="hi").to_meta_value() == "n (text, default=hi)"
    assert core.Param("n").to_meta_value() == "n (text)"


# ===========================================================================
# compute_fingerprint
# ===========================================================================

@pytest.mark.unit
def test_compute_fingerprint_existing_file(tmp_path):
    f = tmp_path / "schema.sql"
    f.write_text("CREATE TABLE t (id int);", encoding="utf-8")
    fp, details = core.compute_fingerprint([str(f)])
    assert fp.startswith("sha256:")
    assert len(fp) == len("sha256:") + 16
    assert details[0]["exists"] is True
    assert details[0]["size"] == len("CREATE TABLE t (id int);")
    # deterministic
    assert core.compute_fingerprint([str(f)])[0] == fp


@pytest.mark.unit
def test_compute_fingerprint_missing_marks_missing(tmp_path):
    fp, details = core.compute_fingerprint([str(tmp_path / "gone.sql")])
    assert details[0]["exists"] is False
    assert details[0]["size"] is None
    # a missing source still contributes a stable <MISSING> marker to the hash
    assert fp.startswith("sha256:")


@pytest.mark.unit
def test_compute_fingerprint_directory_raises(tmp_path):
    # a resolved directory is read_bytes()'d -> IsADirectoryError (not globbed)
    d = tmp_path / "adir"
    d.mkdir()
    with pytest.raises(IsADirectoryError):
        core.compute_fingerprint([str(d)])


# ===========================================================================
# parse_kv_args / resolve_params
# ===========================================================================

@pytest.mark.unit
def test_parse_kv_args_ok():
    assert core.parse_kv_args(["a=1", "b=x=y", "c="]) == {"a": "1", "b": "x=y", "c": ""}


@pytest.mark.unit
def test_parse_kv_args_missing_equals_errors():
    with pytest.raises(core.QuarryError) as ei:
        core.parse_kv_args(["nokey"])
    assert ei.value.exit_code == core.EXIT_USAGE


@pytest.mark.unit
def test_resolve_params_defaults_and_extra():
    q = core.Query(
        name="q", db="d",
        params=[core.Param("a", default="10"), core.Param("b", required=False)],
    )
    # 'a' defaulted; 'a' not provided but has default; extra 'z' passes through;
    # 'b' has no default and is not required -> omitted
    resolved = core.resolve_params(q, {"z": "extra"})
    assert resolved == {"a": "10", "z": "extra"}


@pytest.mark.unit
def test_resolve_params_provided_overrides_default():
    q = core.Query(name="q", db="d", params=[core.Param("a", default="10")])
    assert core.resolve_params(q, {"a": "99"}) == {"a": "99"}


@pytest.mark.unit
def test_resolve_params_required_missing_errors():
    q = core.Query(name="q", db="d", params=[core.Param("r", required=True)])
    with pytest.raises(core.QuarryError) as ei:
        core.resolve_params(q, {})
    assert ei.value.exit_code == core.EXIT_USAGE
    assert "missing required param 'r'" in str(ei.value)


# ===========================================================================
# run_mysql_query with a fake pymysql
# ===========================================================================

class _FakeMySQLError(Exception):
    pass


class _FakeCursor:
    def __init__(self, description, rows, raise_on_execute=None):
        self.description = description
        self._rows = rows
        self._raise = raise_on_execute
        self.executed = None
        self.executed_all: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql):
        self.executed = sql
        self.executed_all.append(sql)
        if self._raise is not None:
            raise self._raise

    def fetchall(self):
        return self._rows


class _FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor
        self.closed = False
        self.committed = False

    def cursor(self):
        return self._cursor

    def commit(self):
        self.committed = True

    def close(self):
        self.closed = True


def _make_fake_pymysql(*, cursor=None, connect_error=None):
    mod = types.ModuleType("pymysql")
    err_mod = types.ModuleType("pymysql.err")
    err_mod.MySQLError = _FakeMySQLError
    cursors_mod = types.ModuleType("pymysql.cursors")
    cursors_mod.DictCursor = object
    mod.err = err_mod
    mod.cursors = cursors_mod
    captured = {}

    def connect(**kwargs):
        captured.update(kwargs)
        if connect_error is not None:
            raise connect_error
        connection = _FakeConn(cursor)
        mod._connection = connection
        return connection

    mod.connect = connect
    mod._captured = captured
    mod._connection = None
    return mod


@pytest.mark.unit
def test_run_mysql_query_serializes_rows(monkeypatch):
    cur = _FakeCursor(
        description=[("id",), ("amount",), ("when",)],
        rows=[{"id": 1, "amount": Decimal("9.99"), "when": datetime(2020, 1, 1, 0, 0, 0)}],
    )
    fake = _make_fake_pymysql(cursor=cur)
    monkeypatch.setattr(core, "import_pymysql", lambda: fake)
    rows, download_bytes = core.run_mysql_query("mysql://u:p@h/db", "SELECT * FROM t")
    assert rows == [{"id": 1, "amount": 9.99, "when": "2020-01-01 00:00:00"}]
    assert download_bytes == len(json.dumps(rows, default=str).encode("utf-8"))
    # connection config was derived from the URL
    assert fake._captured["host"] == "h"
    assert fake._captured["database"] == "db"
    assert cur.executed == "SELECT * FROM t"


@pytest.mark.unit
def test_run_mysql_query_commits_statement_without_result(monkeypatch):
    cur = _FakeCursor(description=None, rows=[{"x": 1}])
    fake = _make_fake_pymysql(cursor=cur)
    monkeypatch.setattr(core, "import_pymysql", lambda: fake)
    rows, download_bytes = core.run_mysql_query("mysql://u:p@h/db", "INSERT INTO t VALUES (1)")
    assert rows == []
    assert download_bytes == len(json.dumps([]).encode("utf-8"))
    assert fake._connection.committed is True


@pytest.mark.unit
def test_run_mysql_query_connect_error_is_connection_error(monkeypatch):
    fake = _make_fake_pymysql(connect_error=_FakeMySQLError("cannot connect"))
    monkeypatch.setattr(core, "import_pymysql", lambda: fake)
    with pytest.raises(core.QuarryError) as ei:
        core.run_mysql_query("mysql://u:p@h/db", "SELECT 1")
    assert ei.value.exit_code == core.EXIT_CONNECTION_ERROR
    assert "connection failed" in str(ei.value)


@pytest.mark.unit
def test_run_mysql_query_execute_error_is_sql_error(monkeypatch):
    cur = _FakeCursor(description=None, rows=[], raise_on_execute=_FakeMySQLError("bad sql"))
    fake = _make_fake_pymysql(cursor=cur)
    monkeypatch.setattr(core, "import_pymysql", lambda: fake)
    with pytest.raises(core.QuarryError) as ei:
        core.run_mysql_query("mysql://u:p@h/db", "SELECT bogus")
    assert ei.value.exit_code == core.EXIT_SQL_ERROR
    assert "mysql error" in str(ei.value)


# ===========================================================================
# run_mysql_query connect/execute timeout split (issue #94)
# ===========================================================================

@pytest.mark.unit
def test_run_mysql_query_connect_timeout_independent_of_execute_timeout(monkeypatch):
    # connect_timeout (tunnel/dial budget) must reach pymysql.connect as
    # connect_timeout, while read/write timeouts stay pinned to `timeout`
    # (query execution budget) — the two must NOT collapse into one value.
    cur = _FakeCursor(description=[("id",)], rows=[{"id": 1}])
    fake = _make_fake_pymysql(cursor=cur)
    monkeypatch.setattr(core, "import_pymysql", lambda: fake)
    core.run_mysql_query("mysql://u:p@h/db", "SELECT 1", timeout=300, connect_timeout=15)
    assert fake._captured["connect_timeout"] == 15
    assert fake._captured["read_timeout"] == 300
    assert fake._captured["write_timeout"] == 300


@pytest.mark.unit
def test_run_mysql_query_no_connect_timeout_reuses_timeout(monkeypatch):
    # pre-#94 behavior preserved: omitting connect_timeout reuses `timeout`
    # for the connect phase too.
    cur = _FakeCursor(description=[("id",)], rows=[{"id": 1}])
    fake = _make_fake_pymysql(cursor=cur)
    monkeypatch.setattr(core, "import_pymysql", lambda: fake)
    core.run_mysql_query("mysql://u:p@h/db", "SELECT 1", timeout=45)
    assert fake._captured["connect_timeout"] == 45
    assert fake._captured["read_timeout"] == 45


@pytest.mark.unit
def test_run_mysql_query_sets_serverside_execution_caps(monkeypatch):
    # review r1-2: MySQL must also get a server-side cancellation backstop
    # (not just the client-side socket timeout), mirroring PG's
    # SET statement_timeout. Both dialect variants are attempted best-effort.
    cur = _FakeCursor(description=[("id",)], rows=[{"id": 1}])
    fake = _make_fake_pymysql(cursor=cur)
    monkeypatch.setattr(core, "import_pymysql", lambda: fake)
    core.run_mysql_query("mysql://u:p@h/db", "SELECT 1", timeout=30)
    assert "SET SESSION MAX_EXECUTION_TIME = 30000" in cur.executed_all
    assert "SET SESSION max_statement_time = 30" in cur.executed_all
    assert cur.executed_all[-1] == "SELECT 1"  # real query always runs last


@pytest.mark.unit
def test_run_mysql_query_serverside_cap_failure_is_non_fatal(monkeypatch):
    # e.g. MariaDB rejects MAX_EXECUTION_TIME (unknown system variable) —
    # that must not abort the real query.
    class _PartiallyPickyCursor(_FakeCursor):
        def execute(self, sql):
            self.executed = sql
            self.executed_all.append(sql)
            if sql.startswith("SET SESSION MAX_EXECUTION_TIME"):
                raise _FakeMySQLError("unknown system variable 'MAX_EXECUTION_TIME'")

    cur = _PartiallyPickyCursor(description=[("id",)], rows=[{"id": 1}])
    fake = _make_fake_pymysql(cursor=cur)
    monkeypatch.setattr(core, "import_pymysql", lambda: fake)
    rows, _ = core.run_mysql_query("mysql://u:p@h/db", "SELECT 1", timeout=30)
    assert rows == [{"id": 1}]
    assert cur.executed_all[-1] == "SELECT 1"


@pytest.mark.unit
def test_run_mysql_query_execute_timeout_hints_increase(monkeypatch):
    cur = _FakeCursor(description=None, rows=[],
                      raise_on_execute=_FakeMySQLError("query execution was interrupted, timeout"))
    fake = _make_fake_pymysql(cursor=cur)
    monkeypatch.setattr(core, "import_pymysql", lambda: fake)
    with pytest.raises(core.QuarryError) as ei:
        core.run_mysql_query("mysql://u:p@h/db", "SELECT slow()", timeout=5)
    assert ei.value.exit_code == core.EXIT_SQL_ERROR
    assert "--timeout" in str(ei.value)


# ===========================================================================
# run_neptune_cypher with mocked urlopen
# ===========================================================================

class _FakeResp:
    def __init__(self, body: bytes):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._body


@pytest.mark.unit
def test_run_neptune_cypher_success(monkeypatch):
    calls = {}

    def fake_urlopen(req, timeout=None, context=None):
        calls["url"] = req.full_url
        calls["data"] = req.data
        return _FakeResp(json.dumps({"results": [{"n": 1}, {"n": 2}]}).encode())

    monkeypatch.setattr(core, "urlopen", fake_urlopen)
    body = json.dumps({"results": [{"n": 1}, {"n": 2}]}).encode()
    rows, download_bytes = core.run_neptune_cypher("h.example.com", "MATCH (n) RETURN n")
    assert rows == [{"n": 1}, {"n": 2}]
    assert download_bytes == len(body)
    assert calls["url"].endswith("/openCypher")
    assert b"query=" in calls["data"]


@pytest.mark.unit
def test_connection_workspace_home_prefers_source_over_primary(tmp_path):
    """PR #98 review (r1-2): callers must resolve the proxy toggle against the
    workspace a Connection was actually loaded from, not always the primary
    workspace — this is what run_query/execute_sql/validate_query rely on."""
    other = str(tmp_path / "other-ws")
    conn = core.Connection(key="k", url="https://h:8182", engine="neptune", source=other)
    assert core._connection_workspace_home(conn) == other


@pytest.mark.unit
def test_connection_workspace_home_falls_back_to_primary_when_no_source():
    conn = core.Connection(key="k", url="https://h:8182", engine="neptune")
    assert core._connection_workspace_home(conn) == core.workspace.WS.home


@pytest.mark.unit
def test_run_neptune_cypher_workspace_home_defaults_to_primary(monkeypatch):
    """PR #98 review (r1-2): with no explicit workspace_home, should_use_proxy
    must be called with the primary workspace (workspace.WS.home), matching
    the pre-fix behavior for callers that don't know about multi-workspace."""
    calls = {}
    monkeypatch.setattr(core, "urlopen", lambda *a, **k: _FakeResp(b"[]"))
    monkeypatch.setattr(core.proxy_mod, "should_use_proxy",
                         lambda host, *, workspace_home, override: calls.update(workspace_home=workspace_home) or None)
    core.run_neptune_cypher("h.example.com", "MATCH (n) RETURN n")
    assert calls["workspace_home"] == core.workspace.WS.home


@pytest.mark.unit
def test_run_neptune_cypher_workspace_home_uses_explicit_override(monkeypatch, tmp_path):
    """PR #98 review (r1-2): a caller holding a Connection loaded from a
    non-primary workspace (conn.source) must be able to route the proxy
    toggle lookup through that workspace instead of always the primary one."""
    calls = {}
    other = tmp_path / "other-ws"
    monkeypatch.setattr(core, "urlopen", lambda *a, **k: _FakeResp(b"[]"))
    monkeypatch.setattr(core.proxy_mod, "should_use_proxy",
                         lambda host, *, workspace_home, override: calls.update(workspace_home=workspace_home) or None)
    core.run_neptune_cypher("h.example.com", "MATCH (n) RETURN n", workspace_home=other)
    assert calls["workspace_home"] == other


@pytest.mark.unit
def test_run_neptune_cypher_empty_body_is_empty_rows(monkeypatch):
    monkeypatch.setattr(core, "urlopen", lambda *a, **k: _FakeResp(b"   "))
    rows, download_bytes = core.run_neptune_cypher("h.example.com", "MATCH (n) RETURN n")
    assert rows == []
    assert download_bytes == 3


@pytest.mark.unit
def test_run_neptune_cypher_http_error_is_sql_error(monkeypatch):
    from urllib.error import HTTPError

    def raiser(*a, **k):
        raise HTTPError("http://h", 400, "Bad", {}, io.BytesIO(b"detail"))

    monkeypatch.setattr(core, "urlopen", raiser)
    with pytest.raises(core.QuarryError) as ei:
        core.run_neptune_cypher("h.example.com", "X")
    assert ei.value.exit_code == core.EXIT_SQL_ERROR
    assert "neptune HTTP 400" in str(ei.value)


@pytest.mark.unit
def test_run_neptune_cypher_url_error_is_connection_error(monkeypatch):
    from urllib.error import URLError

    def raiser(*a, **k):
        raise URLError("name resolution failed")

    monkeypatch.setattr(core, "urlopen", raiser)
    with pytest.raises(core.QuarryError) as ei:
        core.run_neptune_cypher("h.example.com", "X")
    assert ei.value.exit_code == core.EXIT_CONNECTION_ERROR


@pytest.mark.unit
def test_run_neptune_cypher_timeout_is_connection_error(monkeypatch):
    def raiser(*a, **k):
        raise TimeoutError("slow")

    monkeypatch.setattr(core, "urlopen", raiser)
    with pytest.raises(core.QuarryError) as ei:
        core.run_neptune_cypher("h.example.com", "X")
    assert ei.value.exit_code == core.EXIT_CONNECTION_ERROR
    assert "timed out" in str(ei.value)


@pytest.mark.unit
def test_run_neptune_cypher_non_json_is_sql_error(monkeypatch):
    monkeypatch.setattr(core, "urlopen", lambda *a, **k: _FakeResp(b"<html>oops</html>"))
    with pytest.raises(core.QuarryError) as ei:
        core.run_neptune_cypher("h.example.com", "X")
    assert ei.value.exit_code == core.EXIT_SQL_ERROR
    assert "non-JSON body" in str(ei.value)


# ===========================================================================
# @requires_db — real psql path, in-process
# ===========================================================================

@requires_db
@pytest.mark.integration
def test_pg_column_types_real_select():
    types_map = core._pg_column_types(TEST_DB_URL, "SELECT 1 AS n, 'x'::text AS label", {})
    assert types_map.get("n") == "integer"
    assert types_map.get("label") == "text"


@requires_db
@pytest.mark.integration
def test_run_query_with_types(ws):
    conn = core.get_connection("testpg")
    res = core.run_query(conn, "SELECT 1 AS n, 'hi'::text AS label", with_types=True)
    assert res.row_count == 1
    assert res.rows == [{"n": 1, "label": "hi"}]
    by_name = {c["name"]: c["type"] for c in res.columns}
    assert by_name["n"] == "integer"
    assert by_name["label"] == "text"


@requires_db
@pytest.mark.integration
def test_wrap_for_json_roundtrip():
    wrapped = core.wrap_for_json("SELECT 1 AS n;")
    rc, out, errout = core.run_psql_capture(TEST_DB_URL, wrapped, timeout=15)
    assert rc == 0, errout
    assert json.loads(out.strip()) == [{"n": 1}]


@requires_db
@pytest.mark.integration
def test_wrap_for_csv_roundtrip():
    wrapped = core.wrap_for_csv("SELECT 1 AS n, 2 AS m")
    rc, out, errout = core.run_psql_capture(TEST_DB_URL, wrapped, timeout=15)
    assert rc == 0, errout
    assert out.strip() == "n,m\n1,2"


@requires_db
@pytest.mark.integration
def test_wrap_for_csv_no_header():
    wrapped = core.wrap_for_csv("SELECT 42 AS n", with_header=False)
    rc, out, errout = core.run_psql_capture(TEST_DB_URL, wrapped, timeout=15)
    assert rc == 0, errout
    assert out.strip() == "42"


# ===========================================================================
# format_bytes / format_query_stats (issue #105)
# ===========================================================================

@pytest.mark.unit
@pytest.mark.parametrize("n, expected", [
    (0, "0 B"),
    (1, "1 B"),
    (1023, "1023 B"),
    (1024, "1.0 KB"),
    (4300, "4.2 KB"),
    (1024 * 1024 - 1, "1024.0 KB"),
    (1024 * 1024, "1.0 MB"),
    (5 * 1024 * 1024, "5.0 MB"),
])
def test_format_bytes(n, expected):
    assert core.format_bytes(n) == expected


@pytest.mark.unit
def test_format_query_stats_estimated_gets_approx_marker():
    line = core.format_query_stats(123, 4300, True)
    assert line.startswith("123ms · ")
    assert "downloaded ≈4.2 KB" in line
    assert "avg speed ≈" in line and "/s" in line


@pytest.mark.unit
def test_format_query_stats_exact_has_no_approx_marker():
    line = core.format_query_stats(100, 1024, False)
    assert "≈" not in line
    assert "downloaded 1.0 KB" in line


@pytest.mark.unit
def test_format_query_stats_zero_elapsed_does_not_raise():
    # A sub-millisecond query rounds elapsed_ms to 0 — must not divide by zero.
    line = core.format_query_stats(0, 0, True)
    assert line.startswith("0ms · ")
    assert "downloaded ≈0 B" in line
