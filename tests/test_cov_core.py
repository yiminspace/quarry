"""Coverage-closing tests for quarry.core.

Targets ONLY the branches/lines that test_core.py, test_core_more.py,
test_safety_rails.py and test_groups.py leave uncovered:

  resolve_psql fallbacks, load_connections dup-key + missing-file skip,
  resolve_connection env-set fallbacks + errors, get_connection missing,
  group_connections branches, _parse_param_spec invalid, parse_query_file
  header edge cases, find_query_file ambiguity, list_all_queries skip,
  compute_fingerprint file/missing/dir, sql_skeleton unterminated-quote /
  dollar-quote / block-comment / doubled-quote branches, run_query redis /
  mysql / neptune / truncation / with_types, import_pymysql ImportError,
  run_psql_capture timeout, _rows_postgres error branches, execute_sql
  redis/mysql/neptune/postgres paths, validate_query all engines.

Engines other than Postgres are mocked (mysql/redis/neptune/ssh are not
available in this environment). Real-Postgres tests are @requires_db.
"""

from __future__ import annotations

import builtins
import contextlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from quarry import core, redis_engine, tunnel, workspace  # noqa: E402
from conftest import TEST_DB_URL, requires_db  # noqa: E402


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _fake_tunnel(expect_url: str | None = None):
    """A contextlib.contextmanager stand-in for tunnel.open_tunnel that yields
    the connection URL unchanged (no real SSH)."""
    @contextlib.contextmanager
    def _open(conn, engine, connect_timeout=None, use_proxy=None):
        yield conn.url
    return _open


def _redis_conn():
    return core.Connection(key="rk", url="redis://127.0.0.1:6379/0", engine="redis")


def _mysql_conn():
    return core.Connection(key="mk", url="mysql://u:p@h/db", engine="mysql")


def _neptune_conn():
    return core.Connection(key="nk", url="https://x.neptune.amazonaws.com:8182", engine="neptune")


# ===========================================================================
# resolve_psql  (88-91)
# ===========================================================================

@pytest.mark.unit
def test_resolve_psql_homebrew_fallback(monkeypatch):
    # workspace psql_bin not on PATH -> falls through to the homebrew path,
    # which Path.exists() reports as present -> returned.
    monkeypatch.setattr(workspace.WS, "psql_bin", "psql-missing", raising=False)
    monkeypatch.setattr(core.shutil, "which", lambda _b: None)
    homebrew = "/opt/homebrew/opt/postgresql@13/bin/psql"
    monkeypatch.setattr(core.Path, "exists", lambda self: str(self) == homebrew)
    assert core.resolve_psql() == homebrew


@pytest.mark.unit
def test_resolve_psql_none_found_errors(monkeypatch):
    monkeypatch.setattr(workspace.WS, "psql_bin", "psql-missing", raising=False)
    monkeypatch.setattr(core.shutil, "which", lambda _b: None)
    monkeypatch.setattr(core.Path, "exists", lambda self: False)
    with pytest.raises(core.QuarryError) as ei:
        core.resolve_psql()
    assert ei.value.exit_code == core.EXIT_CONNECTION_ERROR
    assert "psql not found" in str(ei.value)


# ===========================================================================
# load_connections  (136, 141)  — dup logical key + a missing conn file skipped
# ===========================================================================

@pytest.mark.unit
def test_load_connections_dup_key_earlier_wins_and_skips_missing(tmp_path):
    # w1 has [dup]; w2 also has [dup] (later, so skipped: line 141 continue).
    # w3 has NO connections.toml (line 135-136 continue).
    w1 = tmp_path / "w1"
    w2 = tmp_path / "w2"
    w3 = tmp_path / "w3"
    for w in (w1, w2, w3):
        w.mkdir()
    (w1 / "connections.toml").write_text('[dup]\nurl = "postgres://h/first"\n', encoding="utf-8")
    (w2 / "connections.toml").write_text('[dup]\nurl = "postgres://h/second"\n[only2]\nurl = "postgres://h/x"\n', encoding="utf-8")
    import os as _os
    try:
        workspace.configure_workspace(_os.pathsep.join([str(w1), str(w3), str(w2)]))
        conns = core.load_connections()
        # earlier workspace (w1) wins the dup key
        assert conns["dup"].url == "postgres://h/first"
        assert conns["dup"].source == str(w1)
        # a key only in w2 is still picked up (w3's missing file was skipped)
        assert conns["only2"].url == "postgres://h/x"
    finally:
        workspace.configure_workspace(None)


# ===========================================================================
# resolve_connection  (185, 187, 189, 194)
# ===========================================================================

def _write_conns(dirpath: Path, toml: str):
    (dirpath / "connections.toml").write_text(toml, encoding="utf-8")


@pytest.mark.unit
def test_resolve_connection_single_member_fallback(tmp_path):
    # env-set of one member, no --env, no dev -> single-member fallback (185)
    _write_conns(tmp_path, '[shop_jp]\nurl = "postgres://h/jp"\ndb = "shop"\nenv = "jp"\n')
    try:
        workspace.configure_workspace(str(tmp_path))
        c = core.resolve_connection("shop")   # logical db, no env
        assert c.key == "shop_jp"
    finally:
        workspace.configure_workspace(None)


@pytest.mark.unit
def test_resolve_connection_multi_env_no_dev_picks_stable_first(tmp_path):
    # multi-env set, no dev, no --env -> stable first (sorted) member (187)
    _write_conns(
        tmp_path,
        '[shop_jp]\nurl = "postgres://h/jp"\ndb = "shop"\nenv = "jp"\n'
        '[shop_us]\nurl = "postgres://h/us"\ndb = "shop"\nenv = "us"\n',
    )
    try:
        workspace.configure_workspace(str(tmp_path))
        c = core.resolve_connection("shop")   # no env; sorted -> 'jp' first
        assert c.env == "jp"
    finally:
        workspace.configure_workspace(None)


@pytest.mark.unit
def test_resolve_connection_env_given_not_in_set_falls_back_to_key(tmp_path):
    # a real connection key that is its own env-set; --env not present in the set
    # but `name in conns` -> fall back to the key (189)
    _write_conns(
        tmp_path,
        '[shop_jp]\nurl = "postgres://h/jp"\ndb = "shop"\nenv = "jp"\n'
        '[shop_us]\nurl = "postgres://h/us"\ndb = "shop"\nenv = "us"\n',
    )
    try:
        workspace.configure_workspace(str(tmp_path))
        # name is the key 'shop_jp', which is in the env-set; --env 'zz' not in set
        c = core.resolve_connection("shop_jp", env="zz")
        assert c.key == "shop_jp"
    finally:
        workspace.configure_workspace(None)


@pytest.mark.unit
def test_resolve_connection_env_given_not_in_set_no_key_errors(tmp_path):
    # env given, not in set, and name is a logical db (not a key) -> error (191)
    _write_conns(
        tmp_path,
        '[shop_jp]\nurl = "postgres://h/jp"\ndb = "shop"\nenv = "jp"\n'
        '[shop_us]\nurl = "postgres://h/us"\ndb = "shop"\nenv = "us"\n',
    )
    try:
        workspace.configure_workspace(str(tmp_path))
        with pytest.raises(core.QuarryError) as ei:
            core.resolve_connection("shop", env="zz")
        assert ei.value.exit_code == core.EXIT_USAGE
        assert "not found for 'shop'" in str(ei.value)
    finally:
        workspace.configure_workspace(None)


@pytest.mark.unit
def test_resolve_connection_env_set_with_no_matching_env_key_fallback(tmp_path):
    # A connection whose env is set, targeted by KEY, with --env that yields no
    # env-set match at all except via `name in conns` fallback (194 path):
    # here 'lonely' has no db, so members search is by key's logical_db (=key).
    _write_conns(tmp_path, '[lonely]\nurl = "postgres://h/l"\n')
    try:
        workspace.configure_workspace(str(tmp_path))
        # env given, single member with no env -> not in members[target], not
        # len==1 (env is not None), name in conns -> return conns[name] (189/194)
        c = core.resolve_connection("lonely", env="prod")
        assert c.key == "lonely"
    finally:
        workspace.configure_workspace(None)


@pytest.mark.unit
def test_resolve_connection_unknown_name_errors(tmp_path):
    _write_conns(tmp_path, '[known]\nurl = "postgres://h/k"\n')
    try:
        workspace.configure_workspace(str(tmp_path))
        with pytest.raises(core.QuarryError) as ei:
            core.resolve_connection("ghost")
        assert ei.value.exit_code == core.EXIT_USAGE
        assert "unknown db 'ghost'" in str(ei.value)
    finally:
        workspace.configure_workspace(None)


# ===========================================================================
# group_connections  (272, 276-281, 287, 295-296, 300, 310) + get_connection
# NOTE: 264-313 are _read/_write_connections_file — several targets sit there.
# ===========================================================================

@pytest.mark.unit
def test_group_connections_group_env_ssh_region(tmp_path):
    _write_conns(
        tmp_path,
        '[api_dev]\nurl = "postgres://h/d"\ngroup = "api"\ndb = "api"\nenv = "dev"\n'
        'region = "us-east-1"\nssh_host = "bastion.example.com"\n'
        '[api_prod]\nurl = "postgres://h/p"\ngroup = "api"\ndb = "api"\nenv = "prod"\n'
        '[loner]\nurl = "postgres://h/x"\n',   # no group, no env -> group None, not env-set
    )
    try:
        workspace.configure_workspace(str(tmp_path))
        groups = core.group_connections()
        by_group = {g["group"]: g for g in groups}
        api = by_group["api"]["items"][0]
        assert api["is_env_set"] is True
        dev_env = next(e for e in api["envs"] if e["env"] == "dev")
        assert dev_env["ssh"] is True
        assert dev_env["region"] == "us-east-1"
        prod_env = next(e for e in api["envs"] if e["env"] == "prod")
        assert prod_env["ssh"] is False
        loner = by_group[None]["items"][0]
        assert loner["is_env_set"] is False
    finally:
        workspace.configure_workspace(None)


@pytest.mark.unit
def test_read_connections_file_parts_header_and_data(tmp_path):
    # header lines before first [table], trailing-blank trim, scalar fields kept
    _write_conns(
        tmp_path,
        "# a header comment\n"
        "\n"
        "\n"
        '[api]\nurl = "postgres://h/d"\nregion = "us"\nssh_port = 22\n',
    )
    try:
        workspace.configure_workspace(str(tmp_path))
        header, data = core._read_connections_file_parts()
        assert header == ["# a header comment"]   # trailing blank lines trimmed
        assert data["api"]["url"] == "postgres://h/d"
        assert data["api"]["region"] == "us"
        # numeric scalars are preserved (not dropped) so a rewrite keeps them intact
        assert data["api"]["ssh_port"] == 22
        assert isinstance(data["api"]["ssh_port"], int)
    finally:
        workspace.configure_workspace(None)


@pytest.mark.unit
def test_read_connections_file_parts_no_table_all_header(tmp_path):
    # a file with only comments (no [table]) -> the header loop runs to the end
    # without hitting `break` (276->280) and there is no table data (287->286
    # never taken because raw is empty; top-level non-dict handled below).
    _write_conns(tmp_path, "# only a comment\n# another\n")
    try:
        workspace.configure_workspace(str(tmp_path))
        header, data = core._read_connections_file_parts()
        assert header == ["# only a comment", "# another"]
        assert data == {}
    finally:
        workspace.configure_workspace(None)


@pytest.mark.unit
def test_read_connections_file_parts_top_level_scalar_skipped(tmp_path):
    # a top-level non-dict TOML value is skipped (287->286 false branch)
    _write_conns(tmp_path, 'toplevel = "scalar"\n[api]\nurl = "postgres://h/d"\n')
    try:
        workspace.configure_workspace(str(tmp_path))
        _header, data = core._read_connections_file_parts()
        assert "toplevel" not in data
        assert data["api"]["url"] == "postgres://h/d"
    finally:
        workspace.configure_workspace(None)


@pytest.mark.unit
def test_read_connections_file_parts_missing_file(tmp_path):
    try:
        workspace.configure_workspace(str(tmp_path))   # no connections.toml
        assert core._read_connections_file_parts() == ([], {})   # 272
    finally:
        workspace.configure_workspace(None)


@pytest.mark.unit
def test_write_connections_file_header_ordering_extras(tmp_path):
    # header emitted (295-296); field_order first then extras (310); round-trips
    try:
        workspace.configure_workspace(str(tmp_path))
        header = ["# top comment"]
        data = {"api": {"url": "postgres://h/d", "notes": "hi", "custom": "z"}}
        core._write_connections_file(header, data)
        text = (tmp_path / "connections.toml").read_text(encoding="utf-8")
        assert text.startswith("# top comment")
        assert "[api]" in text
        assert 'url' in text and 'custom = "z"' in text  # extra field emitted (310)
        # url (ordered) appears before custom (extra)
        assert text.index("url") < text.index("custom")
    finally:
        workspace.configure_workspace(None)


@pytest.mark.unit
def test_write_connections_file_invalid_key_errors(tmp_path):
    try:
        workspace.configure_workspace(str(tmp_path))
        with pytest.raises(core.QuarryError) as ei:
            core._write_connections_file([], {"9bad": {"url": "postgres://h/d"}})
        assert ei.value.exit_code == core.EXIT_USAGE   # 300
        assert "invalid connection key" in str(ei.value)
    finally:
        workspace.configure_workspace(None)


@pytest.mark.unit
def test_get_connection_missing_key_lists_available(tmp_path):
    _write_conns(tmp_path, '[real]\nurl = "postgres://h/d"\n')
    try:
        workspace.configure_workspace(str(tmp_path))
        with pytest.raises(core.QuarryError) as ei:
            core.get_connection("nope")
        assert ei.value.exit_code == core.EXIT_USAGE
        assert "real" in str(ei.value)
    finally:
        workspace.configure_workspace(None)


# ===========================================================================
# _parse_param_spec invalid (366)  +  Query.has_limit (360)
# ===========================================================================

@pytest.mark.unit
def test_parse_param_spec_invalid_errors():
    with pytest.raises(core.QuarryError) as ei:
        core._parse_param_spec("123 not a name")
    assert ei.value.exit_code == core.EXIT_USAGE
    assert "invalid @param spec" in str(ei.value)


@pytest.mark.unit
def test_query_has_limit_property():
    assert core.Query(name="q", db="d", sql="SELECT 1 LIMIT 5").has_limit is True
    assert core.Query(name="q", db="d", sql="SELECT 1").has_limit is False


# ===========================================================================
# parse_query_file header branches (374-377, 386-398, 393-397)
# ===========================================================================

@pytest.mark.unit
def test_parse_query_file_default_param_and_non_meta_comment(tmp_path):
    qdir = tmp_path / "queries"
    qdir.mkdir()
    p = qdir / "rpt.sql"
    p.write_text(
        "-- @name: rpt\n"
        "-- @db: shop\n"
        "-- @desc: line one\n"
        "-- @desc: line two\n"          # multiple @desc joined
        "-- @tags: a, b ,c\n"           # @tags parsing
        "-- @param: n (int, default=7)\n"   # default= branch (374-377)
        "-- @schema-source: schema/a.sql\n"
        "-- @schema-source: schema/b.sql\n"
        "-- a non-meta leading comment\n"   # kept in body (393-395)
        "\n"                                 # blank line in header
        "SELECT *\n"                         # body line 1 -> ends header (397)
        "FROM t\n"                           # body line 2 -> in_header False (386->398)
        "WHERE x = 1;\n",                    # body line 3
    )
    q = core.parse_query_file(p)
    assert q.desc == "line one line two"
    assert q.tags == ["a", "b", "c"]
    assert q.schema_sources == ["schema/a.sql", "schema/b.sql"]
    assert q.params[0].name == "n" and q.params[0].default == "7"
    assert q.params[0].required is False
    assert "non-meta leading comment" in q.sql
    assert "SELECT *" in q.sql
    assert "FROM t" in q.sql
    assert "WHERE x = 1" in q.sql


# ===========================================================================
# find_query_file ambiguous (452) + list_all_queries skip malformed (457, 462)
# ===========================================================================

def _mkq(w: Path, name: str, text: str):
    qd = w / "queries"
    qd.mkdir(exist_ok=True)
    (qd / f"{name}.sql").write_text(text, encoding="utf-8")


@pytest.mark.unit
def test_find_query_file_ambiguous_two_workspaces(tmp_path):
    import os as _os
    w1, w2 = tmp_path / "w1", tmp_path / "w2"
    for w in (w1, w2):
        w.mkdir()
        (w / "connections.toml").write_text('[k]\nurl = "postgres://h/d"\n', encoding="utf-8")
        _mkq(w, "dup", "-- @name: dup\n-- @db: d\nSELECT 1;\n")
    try:
        workspace.configure_workspace(str(w1) + _os.pathsep + str(w2))
        with pytest.raises(core.QuarryError) as ei:
            core.find_query_file("dup")
        assert ei.value.exit_code == core.EXIT_USAGE
        assert "ambiguous" in str(ei.value)
    finally:
        workspace.configure_workspace(None)


@pytest.mark.unit
def test_list_all_queries_skips_malformed_and_dedups(tmp_path, capsys):
    import os as _os
    w1, w2 = tmp_path / "w1", tmp_path / "w2"
    for w in (w1, w2):
        w.mkdir()
        (w / "connections.toml").write_text('[k]\nurl = "postgres://h/d"\n', encoding="utf-8")
    _mkq(w1, "good", "-- @name: good\n-- @db: d\nSELECT 1;\n")
    # malformed: @name mismatch -> parse raises, caught + warn-skipped (458-460)
    _mkq(w1, "bad", "-- @name: MISMATCH\n-- @db: d\nSELECT 1;\n")
    # w2 also has 'good' -> earlier workspace wins, second skipped (461-462)
    _mkq(w2, "good", "-- @name: good\n-- @db: d\nSELECT 2;\n")
    try:
        workspace.configure_workspace(str(w1) + _os.pathsep + str(w2))
        qs = core.list_all_queries()
        names = [q.name for q in qs]
        assert names.count("good") == 1
        assert "MISMATCH" not in names and "bad" not in names
        assert "failed to parse" in capsys.readouterr().err
    finally:
        workspace.configure_workspace(None)


@pytest.mark.unit
def test_list_all_queries_skips_missing_queries_dir(tmp_path):
    # w1 has a queries dir + query; w2 has connections.toml but NO queries dir
    # -> `if not w.queries_dir.exists(): continue` (451-452)
    import os as _os
    w1, w2 = tmp_path / "w1", tmp_path / "w2"
    for w in (w1, w2):
        w.mkdir()
        (w / "connections.toml").write_text('[k]\nurl = "postgres://h/d"\n', encoding="utf-8")
    _mkq(w1, "only", "-- @name: only\n-- @db: d\nSELECT 1;\n")
    try:
        workspace.configure_workspace(str(w1) + _os.pathsep + str(w2))
        qs = core.list_all_queries()
        assert [q.name for q in qs] == ["only"]
    finally:
        workspace.configure_workspace(None)


@pytest.mark.unit
def test_list_all_queries_systemexit_propagates(tmp_path, monkeypatch):
    # a SystemExit raised while parsing must propagate, not be warn-skipped (456-457)
    _mkq(tmp_path, "q1", "-- @name: q1\n-- @db: d\nSELECT 1;\n")

    def boom(_path):
        raise SystemExit(7)

    monkeypatch.setattr(core, "parse_query_file", boom)
    try:
        workspace.configure_workspace(str(tmp_path))
        with pytest.raises(SystemExit):
            core.list_all_queries()
    finally:
        workspace.configure_workspace(None)


@pytest.mark.unit
def test_parse_param_spec_typed_no_default_no_required():
    # a param with a type but neither required nor default (374->377 false branch)
    p = core._parse_param_spec("region (text)")
    assert p.name == "region" and p.type == "text"
    assert p.required is False and p.default is None


# ===========================================================================
# compute_fingerprint (485-493)
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


@pytest.mark.unit
def test_compute_fingerprint_missing_source(tmp_path):
    fp, details = core.compute_fingerprint([str(tmp_path / "gone.sql")])
    assert details[0]["exists"] is False
    assert details[0]["size"] is None
    assert fp.startswith("sha256:")


@pytest.mark.unit
def test_compute_fingerprint_directory_raises(tmp_path):
    d = tmp_path / "adir"
    d.mkdir()
    with pytest.raises(IsADirectoryError):
        core.compute_fingerprint([str(d)])


@pytest.mark.unit
def test_compute_fingerprint_relative_source_resolved_via_cwd(tmp_path, monkeypatch):
    # a RELATIVE @schema-source is resolved against the candidate roots
    # (485-493): here it exists relative to CWD.
    f = tmp_path / "rel_schema.sql"
    f.write_text("SELECT 1;", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    fp, details = core.compute_fingerprint(["rel_schema.sql"])
    assert details[0]["exists"] is True
    assert details[0]["resolved"].endswith("rel_schema.sql")
    assert fp.startswith("sha256:")


@pytest.mark.unit
def test_resolve_source_path_relative_missing_returns_asis(tmp_path, monkeypatch):
    # relative path that exists in NONE of the candidate roots -> returned as-is (493)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(core.Path, "home", classmethod(lambda cls: tmp_path))
    out = core._resolve_source_path("does/not/exist_xyz.sql")
    assert str(out) == "does/not/exist_xyz.sql"


# ===========================================================================
# sql_skeleton edge branches (563-593)
# ===========================================================================

@pytest.mark.unit
def test_sql_skeleton_unterminated_single_quote():
    # unterminated string literal: loop runs off the end (563-571), blanked to ''
    out = core.sql_skeleton("SELECT 'abc")
    assert "abc" not in out
    assert "''" in out


@pytest.mark.unit
def test_sql_skeleton_unterminated_dollar_quote():
    # $tag$ opened, never closed -> consumed to end of string (587-592, end==-1)
    out = core.sql_skeleton("SELECT $tag$ body never closed")
    assert "body never closed" not in out


@pytest.mark.unit
def test_sql_skeleton_closed_dollar_quote():
    out = core.sql_skeleton("SELECT $tag$secret$tag$ , 1")
    assert "secret" not in out
    assert "1" in out  # code after the dollar-quote survives


@pytest.mark.unit
def test_sql_skeleton_block_comment():
    out = core.sql_skeleton("SELECT /* hidden DROP TABLE */ 1")
    assert "hidden" not in out
    assert "DROP" not in out
    assert "1" in out


@pytest.mark.unit
def test_sql_skeleton_quoted_identifier_doubled_quote():
    # a quoted identifier with an embedded doubled "" (574-584)
    out = core.sql_skeleton('SELECT "we""ird" FROM t')
    assert "we" not in out          # identifier body blanked to "id"
    assert '"id"' in out
    assert "FROM t" in out


@pytest.mark.unit
def test_sql_skeleton_unterminated_quoted_identifier():
    # quoted identifier that never closes -> while exits on i>=n (575->583)
    out = core.sql_skeleton('SELECT "unterminated ident')
    assert "unterminated" not in out
    assert '"id"' in out


@pytest.mark.unit
def test_sql_skeleton_lone_dollar_not_a_tag():
    # `$1` is a positional param, not a dollar-quote tag -> _DOLLAR_TAG_RE fails,
    # the `$` is emitted verbatim (587->593 false branch).
    out = core.sql_skeleton("SELECT * FROM t WHERE id = $1")
    assert "$1" in out


# ===========================================================================
# run_psql_capture timeout (686-687)  — patch subprocess.run to raise
# ===========================================================================

@pytest.mark.unit
def test_run_psql_capture_timeout(monkeypatch):
    import subprocess as _sp

    def raiser(*a, **k):
        raise _sp.TimeoutExpired(cmd="psql", timeout=1)

    monkeypatch.setattr(core.subprocess, "run", raiser)
    monkeypatch.setattr(core, "resolve_psql", lambda: "psql")
    rc, out, errout = core.run_psql_capture("postgres://h/d", "SELECT 1", timeout=1)
    assert rc == -1
    assert "timed out" in errout


@pytest.mark.unit
def test_run_psql_capture_passes_extra_vars(monkeypatch):
    # exercise the psql_vars loop (682)
    captured = {}

    class _Proc:
        returncode = 0
        stdout = "ok"
        stderr = ""

    def fake_run(cmd, **k):
        captured["cmd"] = cmd
        return _Proc()

    monkeypatch.setattr(core.subprocess, "run", fake_run)
    monkeypatch.setattr(core, "resolve_psql", lambda: "psql")
    core.run_psql_capture("postgres://h/d", "SELECT 1", psql_vars={"limit_n": "5"})
    assert "-v" in captured["cmd"] and "limit_n=5" in captured["cmd"]


# ===========================================================================
# import_pymysql ImportError branch (710-715)
# ===========================================================================

@pytest.mark.unit
def test_import_pymysql_missing_raises(monkeypatch):
    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name == "pymysql":
            raise ImportError("no pymysql")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    # err() with an exit_code raises QuarryError before the unreachable
    # `raise SystemExit` (715) can run.
    with pytest.raises(core.QuarryError) as ei:
        core.import_pymysql()
    assert ei.value.exit_code == core.EXIT_CONNECTION_ERROR
    assert "pymysql not found" in str(ei.value)


# ===========================================================================
# normalize_neptune_endpoint invalid host (812)
# ===========================================================================

@pytest.mark.unit
def test_normalize_neptune_invalid_url_errors():
    with pytest.raises(core.QuarryError) as ei:
        core.normalize_neptune_endpoint("https://:8182/")   # no hostname
    assert ei.value.exit_code == core.EXIT_USAGE


# ===========================================================================
# _rows_postgres error + non-JSON branches (919, 929-930)
# ===========================================================================

@pytest.mark.unit
def test_rows_postgres_explain_rc_nonzero(monkeypatch):
    monkeypatch.setattr(core, "run_psql_capture",
                        lambda *a, **k: (1, "", "boom"))
    with pytest.raises(core.QuarryError) as ei:
        core._rows_postgres("postgres://h/d", "EXPLAIN SELECT 1", {}, 10)
    assert ei.value.exit_code == core.EXIT_SQL_ERROR
    assert "psql failed" in str(ei.value)


@pytest.mark.unit
def test_rows_postgres_nonjson_body(monkeypatch):
    # rc 0 but body is not JSON (929-930)
    monkeypatch.setattr(core, "run_psql_capture",
                        lambda *a, **k: (0, "not-json-at-all", ""))
    with pytest.raises(core.QuarryError) as ei:
        core._rows_postgres("postgres://h/d", "SELECT 1", {}, 10)
    assert ei.value.exit_code == core.EXIT_SQL_ERROR
    assert "non-JSON body" in str(ei.value)


@pytest.mark.unit
def test_rows_postgres_download_bytes_is_stdout_size(monkeypatch):
    # issue #104: download_bytes is psql's stdout text, UTF-8 encoded (an
    # approximation — psql exposes no raw wire-protocol byte count).
    body = '[{"id": 1}, {"id": 2}]'
    monkeypatch.setattr(core, "run_psql_capture", lambda *a, **k: (0, body, ""))
    rows, download_bytes = core._rows_postgres("postgres://h/d", "SELECT 1", {}, 10)
    assert rows == [{"id": 1}, {"id": 2}]
    assert download_bytes == len(body.encode("utf-8")) > 0


@pytest.mark.unit
def test_run_query_postgres_branch_mocked(monkeypatch):
    # exercises run_query's postgres branch (default engine) without a real DB
    monkeypatch.setattr(tunnel, "open_tunnel", _fake_tunnel())
    body = '[{"id": 1}, {"id": 2}]'
    monkeypatch.setattr(core, "run_psql_capture", lambda *a, **k: (0, body, ""))
    conn = core.Connection(key="pk", url="postgres://h/d", engine="postgres")
    res = core.run_query(conn, "SELECT id FROM t")
    assert res.engine == "postgres"
    assert res.rows == [{"id": 1}, {"id": 2}]
    assert res.download_bytes == len(body.encode("utf-8")) > 0
    assert res.size_is_estimated is True


@pytest.mark.unit
def test_pg_column_types_rc_nonzero_returns_empty(monkeypatch):
    # 938 branch
    monkeypatch.setattr(core, "run_psql_capture",
                        lambda *a, **k: (1, "", "err"))
    assert core._pg_column_types("postgres://h/d", "SELECT 1", {}) == {}


@pytest.mark.unit
def test_pg_column_types_parses_gdesc(monkeypatch):
    # 941 branch — parse a `name|type` gdesc line
    monkeypatch.setattr(core, "run_psql_capture",
                        lambda *a, **k: (0, "n|integer\nlabel|text\nnoseparator\n", ""))
    types = core._pg_column_types("postgres://h/d", "SELECT 1", {})
    assert types == {"n": "integer", "label": "text"}


# ===========================================================================
# run_query — redis (686-687 / 969-978)
# ===========================================================================

@pytest.mark.unit
def test_run_query_redis_read(monkeypatch):
    monkeypatch.setattr(tunnel, "open_tunnel", _fake_tunnel())
    monkeypatch.setattr(redis_engine, "is_redis_read_only", lambda cmd: True)
    monkeypatch.setattr(redis_engine, "run_redis",
                        lambda url, cmd, timeout=30: ([{"value": "a"}, {"value": "b"}], 42))
    res = core.run_query(_redis_conn(), "GET foo")
    assert res.engine == "redis"
    assert res.rows == [{"value": "a"}, {"value": "b"}]
    assert res.row_count == 2
    assert res.truncated is False
    assert res.download_bytes == 42
    assert res.size_is_estimated is True


@pytest.mark.unit
def test_run_query_redis_write_blocked(monkeypatch):
    monkeypatch.setattr(tunnel, "open_tunnel", _fake_tunnel())
    monkeypatch.setattr(redis_engine, "is_redis_read_only", lambda cmd: False)
    with pytest.raises(core.QuarryError) as ei:
        core.run_query(_redis_conn(), "SET foo bar")
    assert ei.value.exit_code == core.EXIT_SAFETY_BLOCKED


@pytest.mark.unit
def test_run_query_redis_truncates(monkeypatch):
    monkeypatch.setattr(tunnel, "open_tunnel", _fake_tunnel())
    monkeypatch.setattr(redis_engine, "is_redis_read_only", lambda cmd: True)
    monkeypatch.setattr(redis_engine, "run_redis",
                        lambda url, cmd, timeout=30: ([{"value": str(i)} for i in range(10)], 0))
    res = core.run_query(_redis_conn(), "KEYS *", max_rows=3)
    assert res.row_count == 3 and res.truncated is True


# ===========================================================================
# run_query — mysql (987) + neptune (985) + with_types on non-postgres
# ===========================================================================

@pytest.mark.unit
def test_run_query_mysql_branch(monkeypatch):
    monkeypatch.setattr(tunnel, "open_tunnel", _fake_tunnel())
    monkeypatch.setattr(core, "run_mysql_query",
                        lambda url, sql, params=None, timeout=60, connect_timeout=None, read_only=False: ([{"id": 1}, {"id": 2}], 17))
    res = core.run_query(_mysql_conn(), "SELECT id FROM t", with_types=True)
    assert res.engine == "mysql"
    assert res.rows == [{"id": 1}, {"id": 2}]
    # with_types on a non-postgres engine leaves types null
    assert res.columns[0]["type"] is None
    assert res.download_bytes == 17
    assert res.size_is_estimated is True


@pytest.mark.unit
def test_run_query_neptune_branch(monkeypatch):
    monkeypatch.setattr(tunnel, "open_tunnel", _fake_tunnel())
    monkeypatch.setattr(core, "run_neptune_cypher",
                        lambda url, cypher, params=None, timeout=60, use_proxy=None, workspace_home=None:
                        ([{"n": 1}], 9))
    res = core.run_query(_neptune_conn(), "MATCH (n) RETURN n")
    assert res.engine == "neptune"
    assert res.rows == [{"n": 1}]
    assert res.columns[0]["type"] is None
    assert res.download_bytes == 9
    assert res.size_is_estimated is False


@pytest.mark.unit
def test_run_query_mysql_truncates(monkeypatch):
    monkeypatch.setattr(tunnel, "open_tunnel", _fake_tunnel())
    monkeypatch.setattr(core, "run_mysql_query",
                        lambda url, sql, params=None, timeout=60, connect_timeout=None, read_only=False:
                        ([{"id": i} for i in range(10)], 0))
    res = core.run_query(_mysql_conn(), "SELECT id FROM t", max_rows=3)
    # applied_limit trims and marks truncated (812/995-997)
    assert res.row_count == 3 and res.truncated is True


# ===========================================================================
# execute_sql — redis / mysql / neptune (1150-1160, 1174-1192)
# ===========================================================================

@pytest.mark.unit
def test_execute_sql_redis_json(monkeypatch, capsys):
    monkeypatch.setattr(tunnel, "open_tunnel", _fake_tunnel())
    monkeypatch.setattr(redis_engine, "is_redis_read_only", lambda cmd: True)
    monkeypatch.setattr(redis_engine, "run_redis",
                        lambda url, cmd, timeout=None: ([{"value": "x"}], 5))
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False, raising=False)
    stats = {}
    rc = core.execute_sql(conn=_redis_conn(), sql="GET foo", psql_vars={}, fmt="json", stats=stats)
    assert rc == core.EXIT_OK
    assert json.loads(capsys.readouterr().out) == [{"value": "x"}]
    assert stats["download_bytes"] == 5
    assert stats["elapsed_ms"] >= 0


@pytest.mark.unit
def test_execute_sql_redis_ndjson(monkeypatch, capsys):
    # _emit_rows ndjson branch (1153)
    monkeypatch.setattr(tunnel, "open_tunnel", _fake_tunnel())
    monkeypatch.setattr(redis_engine, "is_redis_read_only", lambda cmd: True)
    monkeypatch.setattr(redis_engine, "run_redis",
                        lambda url, cmd, timeout=None: ([{"value": "a"}, {"value": "b"}], 0))
    rc = core.execute_sql(conn=_redis_conn(), sql="KEYS *", psql_vars={}, fmt="ndjson")
    assert rc == core.EXIT_OK
    lines = [json.loads(x) for x in capsys.readouterr().out.splitlines()]
    assert lines == [{"value": "a"}, {"value": "b"}]


@pytest.mark.unit
def test_execute_sql_redis_table(monkeypatch, capsys):
    # _emit_rows table branch (1157)
    monkeypatch.setattr(tunnel, "open_tunnel", _fake_tunnel())
    monkeypatch.setattr(redis_engine, "is_redis_read_only", lambda cmd: True)
    monkeypatch.setattr(redis_engine, "run_redis",
                        lambda url, cmd, timeout=None: ([{"value": "a"}], 0))
    rc = core.execute_sql(conn=_redis_conn(), sql="KEYS *", psql_vars={}, fmt="table")
    assert rc == core.EXIT_OK
    out = capsys.readouterr().out
    assert "value" in out and "a" in out


@pytest.mark.unit
def test_execute_sql_redis_write_blocked(monkeypatch):
    monkeypatch.setattr(tunnel, "open_tunnel", _fake_tunnel())
    monkeypatch.setattr(redis_engine, "is_redis_read_only", lambda cmd: False)
    with pytest.raises(core.QuarryError) as ei:
        core.execute_sql(conn=_redis_conn(), sql="DEL foo", psql_vars={}, fmt="json")
    assert ei.value.exit_code == core.EXIT_SAFETY_BLOCKED


@pytest.mark.unit
def test_execute_sql_redis_unknown_format(monkeypatch):
    # exercise _emit_rows unknown-format branch (1159)
    monkeypatch.setattr(tunnel, "open_tunnel", _fake_tunnel())
    monkeypatch.setattr(redis_engine, "is_redis_read_only", lambda cmd: True)
    monkeypatch.setattr(redis_engine, "run_redis", lambda url, cmd, timeout=None: ([{"value": "x"}], 0))
    with pytest.raises(core.QuarryError) as ei:
        core.execute_sql(conn=_redis_conn(), sql="GET foo", psql_vars={}, fmt="bogus")
    assert ei.value.exit_code == core.EXIT_USAGE


@pytest.mark.unit
def test_execute_sql_mysql_json(monkeypatch, capsys):
    monkeypatch.setattr(tunnel, "open_tunnel", _fake_tunnel())
    monkeypatch.setattr(core, "run_mysql_query",
                        lambda url, sql, params=None, timeout=None, connect_timeout=None, read_only=False:
                        ([{"id": 1}, {"id": 2}], 11))
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False, raising=False)
    stats = {}
    rc = core.execute_sql(conn=_mysql_conn(), sql="SELECT id FROM t", psql_vars={}, fmt="json", stats=stats)
    assert rc == core.EXIT_OK
    assert json.loads(capsys.readouterr().out) == [{"id": 1}, {"id": 2}]
    assert stats["download_bytes"] == 11


@pytest.mark.unit
def test_execute_sql_mysql_json_truncates(monkeypatch, capsys):
    monkeypatch.setattr(tunnel, "open_tunnel", _fake_tunnel())
    monkeypatch.setattr(core, "run_mysql_query",
                        lambda url, sql, params=None, timeout=None, connect_timeout=None, read_only=False:
                        ([{"id": i} for i in range(10)], 0))
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False, raising=False)
    rc = core.execute_sql(conn=_mysql_conn(), sql="SELECT id FROM t",
                          psql_vars={}, fmt="json", max_rows=3)
    assert rc == core.EXIT_OK
    assert json.loads(capsys.readouterr().out) == [{"id": 0}, {"id": 1}, {"id": 2}]


@pytest.mark.unit
def test_execute_sql_neptune_json(monkeypatch, capsys):
    monkeypatch.setattr(tunnel, "open_tunnel", _fake_tunnel())
    monkeypatch.setattr(core, "run_neptune_cypher",
                        lambda url, sql, params=None, timeout=None, use_proxy=None, workspace_home=None:
                        ([{"n": 1}], 0))
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False, raising=False)
    rc = core.execute_sql(conn=_neptune_conn(), sql="MATCH (n) RETURN n",
                          psql_vars={}, fmt="json")
    assert rc == core.EXIT_OK
    assert json.loads(capsys.readouterr().out) == [{"n": 1}]


# ===========================================================================
# validate_query — all engines (1209-1267)
# ===========================================================================

@pytest.mark.unit
def test_validate_query_not_read_only_blocked():
    # err() with exit_code raises before the (unreachable) return on 1249.
    q = core.Query(name="q", db="d", sql="DELETE FROM t")
    conn = core.Connection(key="k", url="postgres://h/d", engine="postgres")
    with pytest.raises(core.QuarryError) as ei:
        core.validate_query(q, conn)
    assert ei.value.exit_code == core.EXIT_SAFETY_BLOCKED
    assert "not read-only" in str(ei.value)


@pytest.mark.unit
def test_validate_query_redis(monkeypatch):
    monkeypatch.setattr(tunnel, "open_tunnel", _fake_tunnel())
    monkeypatch.setattr(redis_engine, "is_redis_read_only", lambda cmd: True)
    called = {}
    monkeypatch.setattr(redis_engine, "run_redis",
                        lambda url, cmd, timeout=20: called.setdefault("ran", True) or [])
    q = core.Query(name="q", db="d", sql="GET foo")
    rc = core.validate_query(q, _redis_conn())
    assert rc == core.EXIT_OK
    assert called["ran"] is True


@pytest.mark.unit
def test_validate_query_redis_not_read_only(monkeypatch):
    monkeypatch.setattr(redis_engine, "is_redis_read_only", lambda cmd: False)
    q = core.Query(name="q", db="d", sql="SET foo bar")
    with pytest.raises(core.QuarryError) as ei:
        core.validate_query(q, _redis_conn())
    assert ei.value.exit_code == core.EXIT_SAFETY_BLOCKED


@pytest.mark.unit
def test_validate_query_neptune(monkeypatch):
    monkeypatch.setattr(tunnel, "open_tunnel", _fake_tunnel())
    called = {}
    monkeypatch.setattr(core, "run_neptune_cypher",
                        lambda url, cypher, params=None, timeout=20, use_proxy=None, workspace_home=None:
                        called.setdefault("ran", True) or [])
    q = core.Query(name="q", db="d", sql="MATCH (n) RETURN n")
    rc = core.validate_query(q, _neptune_conn())
    assert rc == core.EXIT_OK
    assert called["ran"] is True


@pytest.mark.unit
def test_validate_query_mysql(monkeypatch):
    monkeypatch.setattr(tunnel, "open_tunnel", _fake_tunnel())
    captured = {}
    monkeypatch.setattr(core, "run_mysql_query",
                        lambda url, sql, params=None, timeout=20, read_only=False:
                        captured.update(sql=sql, read_only=read_only) or [])
    q = core.Query(name="q", db="d", sql="SELECT 1")
    rc = core.validate_query(q, _mysql_conn())
    assert rc == core.EXIT_OK
    assert captured["sql"].startswith("EXPLAIN ")
    assert captured["read_only"] is True


@pytest.mark.unit
def test_validate_query_postgres_rc_nonzero(monkeypatch):
    monkeypatch.setattr(tunnel, "open_tunnel", _fake_tunnel())
    monkeypatch.setattr(core, "run_psql_capture",
                        lambda *a, **k: (1, "", "syntax error"))
    q = core.Query(name="q", db="d", sql="SELECT bogus")
    conn = core.Connection(key="k", url="postgres://h/d", engine="postgres")
    rc = core.validate_query(q, conn)
    assert rc == core.EXIT_SQL_ERROR


@pytest.mark.unit
def test_validate_query_exception_is_sql_error(monkeypatch):
    # the try/except wraps engine failures -> EXIT_SQL_ERROR (1265-1267)
    @contextlib.contextmanager
    def _boom(conn, engine):
        raise RuntimeError("tunnel exploded")
        yield  # pragma: no cover
    monkeypatch.setattr(tunnel, "open_tunnel", _boom)
    q = core.Query(name="q", db="d", sql="SELECT 1")
    conn = core.Connection(key="k", url="postgres://h/d", engine="postgres")
    rc = core.validate_query(q, conn)
    assert rc == core.EXIT_SQL_ERROR


@pytest.mark.unit
def test_validate_query_params_dummy_values(monkeypatch):
    # params without defaults get dummy values via _dummy_value_for (1240)
    monkeypatch.setattr(tunnel, "open_tunnel", _fake_tunnel())
    captured = {}

    def fake_capture(url, sql, *, psql_vars=None, timeout=60):
        captured["vars"] = psql_vars
        return (0, "", "")

    monkeypatch.setattr(core, "run_psql_capture", fake_capture)
    q = core.Query(
        name="q", db="d", sql="SELECT :id, :n, :amt, :flag, :ts, :other",
        params=[core.Param("id", "uuid"), core.Param("n", "int"),
                core.Param("amt", "numeric"),          # float family (1229)
                core.Param("flag", "bool"), core.Param("ts", "timestamp"),
                core.Param("other", "jsonb")],          # unknown type -> "" (1234)
    )
    conn = core.Connection(key="k", url="postgres://h/d", engine="postgres")
    rc = core.validate_query(q, conn)
    assert rc == core.EXIT_OK
    assert captured["vars"]["id"] == "00000000-0000-0000-0000-000000000000"
    assert captured["vars"]["n"] == "0"
    assert captured["vars"]["amt"] == "0"
    assert captured["vars"]["flag"] == "false"
    assert captured["vars"]["ts"] == "1970-01-01"
    assert captured["vars"]["other"] == ""


# ===========================================================================
# @requires_db — real Postgres, execute_sql applied_limit trimming (1175-1192)
# ===========================================================================

@requires_db
@pytest.mark.integration
def test_execute_sql_postgres_json_applied_limit(ws, capsys, monkeypatch):
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False, raising=False)
    conn = core.get_connection("testpg")
    rc = core.execute_sql(conn=conn, sql="SELECT generate_series(1,10) AS n",
                          psql_vars={}, fmt="json", max_rows=3)
    assert rc == core.EXIT_OK
    data = json.loads(capsys.readouterr().out)
    assert len(data) == 3
    assert [r["n"] for r in data] == [1, 2, 3]


@requires_db
@pytest.mark.integration
def test_execute_sql_postgres_ndjson_applied_limit(ws, capsys, monkeypatch):
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False, raising=False)
    conn = core.get_connection("testpg")
    rc = core.execute_sql(conn=conn, sql="SELECT generate_series(1,10) AS n",
                          psql_vars={}, fmt="ndjson", max_rows=3)
    assert rc == core.EXIT_OK
    lines = [json.loads(x) for x in capsys.readouterr().out.splitlines()]
    assert len(lines) == 3
    assert [r["n"] for r in lines] == [1, 2, 3]


@requires_db
@pytest.mark.integration
def test_execute_sql_postgres_csv_applied_limit(ws, capsys):
    conn = core.get_connection("testpg")
    rc = core.execute_sql(conn=conn, sql="SELECT generate_series(1,10) AS n",
                          psql_vars={}, fmt="csv", max_rows=3)
    assert rc == core.EXIT_OK
    out = capsys.readouterr().out.strip().splitlines()
    # header + 3 data rows
    assert out[0] == "n"
    assert out[1:] == ["1", "2", "3"]


@requires_db
@pytest.mark.integration
def test_execute_sql_postgres_table_applied_limit(ws, capsys):
    conn = core.get_connection("testpg")
    rc = core.execute_sql(conn=conn, sql="SELECT generate_series(1,10) AS n",
                          psql_vars={}, fmt="table", max_rows=3)
    assert rc == core.EXIT_OK
    lines = capsys.readouterr().out.splitlines()
    # header + separator + 3 data rows = 5 lines
    data_lines = [ln for ln in lines if ln.strip() and not set(ln.strip()) <= {"-", " "}]
    assert data_lines[0].strip() == "n"
    assert [ln.strip() for ln in data_lines[1:]] == ["1", "2", "3"]


@requires_db
@pytest.mark.integration
def test_execute_sql_postgres_json_no_limit(ws, capsys, monkeypatch):
    # applied_limit is None branch (1203-1204): a query that already has LIMIT
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False, raising=False)
    conn = core.get_connection("testpg")
    stats = {}
    rc = core.execute_sql(conn=conn, sql="SELECT generate_series(1,3) AS n LIMIT 3",
                          psql_vars={}, fmt="json", max_rows=None, stats=stats)
    assert rc == core.EXIT_OK
    data = json.loads(capsys.readouterr().out)
    assert [r["n"] for r in data] == [1, 2, 3]
    # issue #104: the CLI execute path also has timing/size available internally
    assert stats["elapsed_ms"] >= 0
    assert stats["download_bytes"] > 0


@requires_db
@pytest.mark.integration
def test_execute_sql_postgres_csv_no_limit(ws, capsys):
    conn = core.get_connection("testpg")
    rc = core.execute_sql(conn=conn, sql="SELECT 42 AS n LIMIT 1",
                          psql_vars={}, fmt="csv", max_rows=None)
    assert rc == core.EXIT_OK
    out = capsys.readouterr().out.strip().splitlines()
    assert out == ["n", "42"]


@requires_db
@pytest.mark.integration
def test_execute_sql_postgres_rc_error(ws):
    conn = core.get_connection("testpg")
    with pytest.raises(core.QuarryError) as ei:
        core.execute_sql(conn=conn, sql="SELECT nonexistent_col FROM (SELECT 1) x",
                         psql_vars={}, fmt="json", max_rows=None)
    assert ei.value.exit_code == core.EXIT_SQL_ERROR


@requires_db
@pytest.mark.integration
def test_execute_sql_postgres_csv_rc_error(ws):
    # the csv/table COPY path rc!=0 branch (1208-1209)
    conn = core.get_connection("testpg")
    with pytest.raises(core.QuarryError) as ei:
        core.execute_sql(conn=conn, sql="SELECT nonexistent_col FROM (SELECT 1) x",
                         psql_vars={}, fmt="csv", max_rows=None)
    assert ei.value.exit_code == core.EXIT_SQL_ERROR


@requires_db
@pytest.mark.integration
def test_validate_query_postgres_happy(ws):
    conn = core.get_connection("testpg")
    q = core.Query(name="q", db="d", sql="SELECT 1")
    assert core.validate_query(q, conn) == core.EXIT_OK


@requires_db
@pytest.mark.integration
def test_run_query_postgres_truncation_applied_limit(ws):
    # exercises the postgres run_query truncation path (812/995-997) on real DB
    conn = core.get_connection("testpg")
    res = core.run_query(conn, "SELECT generate_series(1,10) AS n", max_rows=3)
    assert res.row_count == 3
    assert res.truncated is True
    assert [r["n"] for r in res.rows] == [1, 2, 3]


# ===========================================================================
# execute_sql csv/table error branch on non-postgres unknown format (1214)
# ===========================================================================

@pytest.mark.unit
def test_execute_sql_mysql_csv(monkeypatch, capsys):
    monkeypatch.setattr(tunnel, "open_tunnel", _fake_tunnel())
    monkeypatch.setattr(core, "run_mysql_query",
                        lambda url, sql, params=None, timeout=None, connect_timeout=None, read_only=False: ([{"id": 1}], 0))
    rc = core.execute_sql(conn=_mysql_conn(), sql="SELECT id FROM t",
                          psql_vars={}, fmt="csv")
    assert rc == core.EXIT_OK
    assert "id" in capsys.readouterr().out


@pytest.mark.unit
def test_execute_sql_postgres_unknown_format_errors(monkeypatch):
    # postgres engine + a format matching neither json/ndjson nor csv/table
    # falls out of the `with` block to the final err() (1214-1215).
    monkeypatch.setattr(tunnel, "open_tunnel", _fake_tunnel())
    conn = core.Connection(key="k", url="postgres://h/d", engine="postgres")
    with pytest.raises(core.QuarryError) as ei:
        core.execute_sql(conn=conn, sql="SELECT 1", psql_vars={}, fmt="bogus")
    assert ei.value.exit_code == core.EXIT_USAGE
    assert "unknown format" in str(ei.value)


# ---------------------------------------------------------------------------
# connections.toml: list-of-scalars fields + unsupported-type warning
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_toml_value_list_of_scalars():
    assert core._toml_value(["a", "b"]) == '["a", "b"]'
    assert core._toml_value([1, 2, 3]) == "[1, 2, 3]"


@pytest.mark.unit
def test_toml_value_bool():
    assert core._toml_value(True) == "true"
    assert core._toml_value(False) == "false"


@pytest.mark.unit
def test_read_connections_file_parts_preserves_list_field(tmp_path):
    _write_conns(tmp_path, '[api]\nurl = "postgres://h/d"\ntags = ["a", "b"]\n')
    try:
        workspace.configure_workspace(str(tmp_path))
        _, data = core._read_connections_file_parts()
        assert data["api"]["tags"] == ["a", "b"]
        header, data = core._read_connections_file_parts()
        core._write_connections_file(header, data)
        _, data2 = core._read_connections_file_parts()
        assert data2["api"]["tags"] == ["a", "b"]
    finally:
        workspace.configure_workspace(None)


@pytest.mark.unit
def test_read_connections_file_parts_warns_on_unsupported_field(tmp_path, capsys):
    # a nested table field (not a scalar or list-of-scalars) can't round-trip
    # through the hand-rolled TOML writer; it must be dropped loudly, not silently.
    _write_conns(tmp_path, '[api]\nurl = "postgres://h/d"\n\n[api.meta]\nk = "v"\n')
    try:
        workspace.configure_workspace(str(tmp_path))
        _, data = core._read_connections_file_parts()
        assert "meta" not in data["api"]
        assert "unsupported" in capsys.readouterr().err
    finally:
        workspace.configure_workspace(None)


@pytest.mark.unit
def test_connections_file_lock_serializes_and_releases(tmp_path):
    try:
        workspace.configure_workspace(str(tmp_path))
        with core.connections_file_lock():
            pass
        # the lock must be released — a second acquisition must not hang
        with core.connections_file_lock():
            pass
    finally:
        workspace.configure_workspace(None)
