"""SQLite adapter contracts against independent temporary database files."""
from __future__ import annotations

import json
import builtins
import sqlite3
import time
from dataclasses import replace
from pathlib import Path

import pytest

from quarry import core, sqlite_engine, workspace


@pytest.fixture
def sqlite_conn(tmp_path):
    path = tmp_path / "database.db"
    with sqlite3.connect(path) as db:
        db.executescript('''
            CREATE TABLE entries(id INTEGER PRIMARY KEY, note TEXT NOT NULL, binary BLOB);
            CREATE VIEW entry_view AS SELECT id, note FROM entries;
            CREATE TABLE "daily.events" ("a b" TEXT, n INTEGER, derived AS (n + 1));
            INSERT INTO entries VALUES (1,'first',X'00ff'),(2,'second',NULL),(3,'third',NULL);
        ''')
    return core.Connection(key="sample", engine="sqlite", url="sqlite:///database.db", source=str(tmp_path))


@pytest.mark.unit
@pytest.mark.parametrize("url", ["sqlite:///data.db", "SQLITE:////tmp/data.db"])
def test_sqlite_engine_inference(url):
    assert core.infer_engine(url) == "sqlite"
    assert core.infer_engine(url, " SQLite ") == "sqlite"


@pytest.mark.unit
def test_sqlite_paths_relative_absolute_and_url_characters(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path.parent)
    for url in ["sqlite:///数据%20%23%3F.db", "数据 #?.db",
                "sqlite:///" + str(tmp_path / "数据 #?.db").replace(' ', '%20').replace('#', '%23').replace('?', '%3F')]:
        conn = core.Connection(key="x", engine="sqlite", url=url, source=str(tmp_path))
        assert sqlite_engine.database_path(conn) == tmp_path / "数据 #?.db"
    monkeypatch.setattr(workspace, "WS", replace(workspace.WS, home=tmp_path))
    assert sqlite_engine.database_path(core.Connection(key="x", engine="sqlite", url="sqlite:///a.db")) == tmp_path / "a.db"


@pytest.mark.unit
def test_sqlite_missing_python_module_reports_dependency(monkeypatch):
    importer = builtins.__import__
    def without_sqlite(name, *args, **kwargs):
        if name == 'sqlite3':
            raise ImportError('not bundled')
        return importer(name, *args, **kwargs)
    monkeypatch.setattr(builtins, '__import__', without_sqlite)
    with pytest.raises(core.QuarryError, match="no sqlite3 module") as caught:
        core.run_query(core.Connection(key='x', engine='sqlite', url='sqlite:///file.db'), 'SELECT 1')
    assert caught.value.exit_code == core.EXIT_CONNECTION_ERROR


@pytest.mark.unit
@pytest.mark.parametrize("url", ["", ":memory:", "sqlite:///:memory:", "sqlite:///", "file:/tmp/a.db",
    "sqlite://remote/a.db", "sqlite:///a.db?mode=rwc", "sqlite:///a.db#fragment", "postgres://host/db",
    "sqlite://[broken/db", "sqlite:///~quarry_no_such_user_123/db.sqlite"])
def test_sqlite_rejects_non_file_or_unsafe_uris(url):
    with pytest.raises(core.QuarryError):
        sqlite_engine.database_path(core.Connection(key="x", engine="sqlite", url=url))


@pytest.mark.unit
@pytest.mark.parametrize("field,value", [("ssh_host", "host"), ("ssh_user", "user"), ("ssh_key", "key"), ("ssh_port", 22)])
def test_sqlite_rejects_network_tunnel_configuration(field, value):
    conn = core.Connection(key="x", engine="sqlite", url="sqlite:///a.db", **{field: value})
    with pytest.raises(core.QuarryError, match="SSH"):
        sqlite_engine.database_path(conn)


@pytest.mark.integration
def test_sqlite_missing_invalid_and_directory_files(sqlite_conn, tmp_path):
    for name in ("missing.db", "directory", "invalid.db"):
        target = tmp_path / name
        if name == "directory":
            target.mkdir()
        if name == "invalid.db":
            target.write_text("not a SQLite database")
        sqlite_conn.url = str(target)
        for write in (False, True):
            with pytest.raises(core.QuarryError) as caught:
                core.run_query(sqlite_conn, "SELECT * FROM sqlite_schema", allow_write=write)
            assert caught.value.exit_code == core.EXIT_CONNECTION_ERROR
        assert name != "missing.db" or not target.exists()


@pytest.mark.integration
def test_sqlite_values_pagination_ctes_and_explicit_limit(sqlite_conn):
    for sql in ["VALUES (1),(2),(3); -- final comment",
                "WITH c AS (SELECT 1) VALUES (1),(2),(3)",
                "SELECT 1 AS n UNION ALL VALUES (2),(3)",
                "SELECT id FROM entries ORDER BY id",
                "WITH c AS (SELECT id FROM entries) SELECT id FROM c ORDER BY id"]:
        first = core.run_query(sqlite_conn, sql, max_rows=1)
        second = core.run_query(sqlite_conn, sql, max_rows=1, offset=1)
        assert list(first.rows[0].values()) == [1] and first.truncated
        assert list(second.rows[0].values()) == [2] and second.truncated
    explicit = core.run_query(sqlite_conn, "SELECT id FROM entries ORDER BY id LIMIT 2", max_rows=1, offset=2)
    assert len(explicit.rows) == 2 and not explicit.truncated
    nested = core.run_query(sqlite_conn, "SELECT * FROM (SELECT id FROM entries LIMIT 3)", max_rows=1)
    assert nested.row_count == 1 and nested.truncated
    compound = core.run_query(sqlite_conn, "SELECT 1 AS x,2 AS x UNION ALL VALUES(3,4)", max_rows=1, offset=1)
    assert compound.rows == [{"x": 3, "x_2": 4}]
    assert core.run_query(sqlite_conn, "VALUES(1)", max_rows=1, offset=5).rows == []


@pytest.mark.integration
def test_sqlite_utility_pagination_uses_offset(sqlite_conn):
    for sql in ("PRAGMA table_info(entries)", "EXPLAIN SELECT * FROM entries"):
        all_rows = core.run_query(sqlite_conn, sql, max_rows=0).rows
        first = core.run_query(sqlite_conn, sql, max_rows=1)
        second = core.run_query(sqlite_conn, sql, max_rows=1, offset=1)
        assert first.rows == all_rows[:1] and first.truncated
        assert second.rows == all_rows[1:2] and second.truncated
        assert core.run_query(sqlite_conn, sql, max_rows=1, offset=len(all_rows)).rows == []


@pytest.mark.integration
def test_sqlite_replace_function_is_readonly_in_ctes_and_plans(sqlite_conn):
    sql = "WITH c AS (SELECT 'abc' AS text) SELECT replace(text,'b','B') AS value FROM c"
    assert core.run_query(sqlite_conn, sql).rows == [{"value": "aBc"}]
    assert core.run_query(sqlite_conn, "EXPLAIN QUERY PLAN " + sql).row_count > 0


@pytest.mark.integration
def test_sqlite_types_duplicates_empty_columns_and_csv(sqlite_conn, capsys):
    res = core.run_query(sqlite_conn, "SELECT 9223372036854775807 AS big, X'00ff' AS binary, NULL AS empty, 1.5 AS real, 1e999 AS infinity")
    assert res.rows == [{"big": "9223372036854775807", "binary": {"$binary": "AP8="}, "empty": None, "real": 1.5, "infinity": {"$number": "Infinity"}}]
    json.dumps(res.to_dict(), allow_nan=False)
    for predicate in ("", " WHERE 0"):
        dup = core.run_query(sqlite_conn, "SELECT id AS a, note AS a, id AS a_2 FROM entries" + predicate)
        assert [c["name"] for c in dup.columns] == ["a", "a_2", "a_2_2"]
    core.execute_sql(conn=sqlite_conn, sql="SELECT id, note FROM entries WHERE 0", psql_vars={}, fmt="csv")
    assert capsys.readouterr().out.strip() == "id,note"


@pytest.mark.integration
def test_sqlite_params_preserve_strings_identifiers_comments(sqlite_conn):
    value = "O'Reilly\\path; DELETE FROM entries; -- :other"
    res = core.run_query(sqlite_conn, "SELECT :'value' AS value, ':value' AS literal, :n AS n -- :other", params={"value": value, "n": "42"})
    assert res.rows == [{"value": value, "literal": ":value", "n": 42}]
    assert core.run_query(sqlite_conn, 'SELECT 1 AS ":missing"').rows == [{":missing": 1}]
    with pytest.raises(core.QuarryError, match="missing required param"):
        core.run_query(sqlite_conn, "SELECT :'missing'")
    with pytest.raises(core.QuarryError) as caught:
        core.run_query(sqlite_conn, "SELECT :raw", params={"raw": "1; DELETE FROM entries"})
    assert caught.value.exit_code == core.EXIT_SAFETY_BLOCKED
    bracket = core.run_query(sqlite_conn, "SELECT ':x; DELETE' AS [odd; :name], 'it''s fine' AS `with;tick` /* :ignored */")
    assert bracket.rows == [{"odd; :name": ":x; DELETE", "with;tick": "it's fine"}]


@pytest.mark.integration
@pytest.mark.parametrize("sql", ["DELETE FROM entries", "CREATE TABLE danger(x)",
    "PRAGMA user_version=7", "PRAGMA journal_mode=WAL", "PRAGMA query_only=OFF",
    "WITH q AS (SELECT 1) DELETE FROM entries", "SELECT 1; DELETE FROM entries",
    "ATTACH 'other.db' AS other", "BEGIN", "VACUUM INTO 'other.db'",
    "SELECT load_extension('anything')"])
def test_sqlite_default_readonly_blocks_side_effects(sqlite_conn, sql):
    with pytest.raises(core.QuarryError) as caught:
        core.run_query(sqlite_conn, sql)
    assert caught.value.exit_code == core.EXIT_SAFETY_BLOCKED
    assert core.run_query(sqlite_conn, "SELECT count(*) AS n FROM entries").rows == [{"n": 3}]


@pytest.mark.integration
def test_sqlite_engine_readonly_backstop_and_write_boundary(sqlite_conn):
    # Bypass the lexical classifier to prove the actual connection is protected.
    for sql in ("DELETE FROM entries", "CREATE TEMP TABLE danger(x)", "PRAGMA user_version=3"):
        with pytest.raises(core.QuarryError) as caught:
            sqlite_engine.run_query(sqlite_conn, sql, read_only=True)
        assert caught.value.exit_code == core.EXIT_SAFETY_BLOCKED
    for sql in ("ATTACH ':memory:' AS other", "SELECT load_extension('anything')", "COMMIT"):
        with pytest.raises(core.QuarryError) as caught:
            core.run_query(sqlite_conn, sql, allow_write=True)
        assert caught.value.exit_code == core.EXIT_SAFETY_BLOCKED


@pytest.mark.integration
def test_sqlite_write_commits_and_constraints_rollback(sqlite_conn):
    result = core.run_query(sqlite_conn, "UPDATE entries SET note='updated' WHERE id=1 RETURNING id,note", allow_write=True)
    assert result.rows == [{"id": 1, "note": "updated"}]
    with pytest.raises(core.QuarryError):
        core.run_query(sqlite_conn, "INSERT INTO entries(id,note) VALUES(4,'four'),(1,'duplicate')", allow_write=True)
    assert core.run_query(sqlite_conn, "SELECT count(*) AS n FROM entries").rows == [{"n": 3}]
    core.run_query(sqlite_conn, "CREATE TABLE child(parent REFERENCES entries(id))", allow_write=True)
    with pytest.raises(core.QuarryError, match="FOREIGN KEY"):
        core.run_query(sqlite_conn, "INSERT INTO child VALUES(999)", allow_write=True)


@pytest.mark.integration
def test_sqlite_timeout_interrupts_query_and_rolls_back_write(sqlite_conn):
    recursive = "WITH RECURSIVE c(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM c WHERE x<100000000) "
    for sql, write in [(recursive + "SELECT sum(x) FROM c", False),
                       (recursive + "INSERT INTO entries(id,note) SELECT x+10,'generated' FROM c", True)]:
        start = time.monotonic()
        with pytest.raises(core.QuarryError, match="timed out") as caught:
            core.run_query(sqlite_conn, sql, allow_write=write, timeout=0.02)
        assert caught.value.exit_code == core.EXIT_SQL_ERROR
        assert time.monotonic() - start < 2
    assert core.run_query(sqlite_conn, "SELECT count(*) AS n FROM entries").rows == [{"n": 3}]


@pytest.mark.integration
def test_sqlite_locked_database_wait_is_bounded(sqlite_conn):
    db = sqlite3.connect(sqlite_engine.database_path(sqlite_conn))
    try:
        db.execute("BEGIN EXCLUSIVE")
        start = time.monotonic()
        with pytest.raises(core.QuarryError, match="locked"):
            core.run_query(sqlite_conn, "SELECT * FROM entries", timeout=0.05)
        assert time.monotonic() - start < 2
    finally:
        db.close()


@pytest.mark.integration
def test_sqlite_commit_lock_uses_remaining_query_timeout(sqlite_conn, monkeypatch):
    connect = sqlite3.connect
    reader = connect(sqlite_engine.database_path(sqlite_conn))
    reader.execute("BEGIN")
    reader.execute("SELECT * FROM entries").fetchall()

    def connect_with_slow_function(*args, **kwargs):
        db = connect(*args, **kwargs)
        def pause(value):
            time.sleep(0.4)
            return value + " updated"
        db.create_function("task_pause", 1, pause)
        return db

    monkeypatch.setattr(sqlite3, "connect", connect_with_slow_function)
    try:
        started = time.monotonic()
        with pytest.raises(core.QuarryError, match="locked|timed out"):
            core.run_query(sqlite_conn, "UPDATE entries SET note=task_pause(note) WHERE id=1",
                           allow_write=True, timeout=0.8)
        assert time.monotonic() - started < 1.1
    finally:
        reader.close()
    assert core.run_query(sqlite_conn, "SELECT note FROM entries WHERE id=1").rows == [{"note": "first"}]


@pytest.mark.integration
def test_sqlite_metadata_and_file_cache_identity(sqlite_conn, tmp_path):
    tables = core.cached_tables(sqlite_conn, 'same', 'dev')
    assert tables['tables'] == ['daily.events', 'entries', 'entry_view']
    columns = core.cached_columns(sqlite_conn, 'same', 'dev', 'daily.events', raise_errors=True)['rows']
    assert [c['column_name'] for c in columns] == ['a b', 'n', 'derived']
    assert columns[2]['hidden'] == 2
    assert core.cached_columns(sqlite_conn, 'same', 'dev', "x'); DROP TABLE entries;--", raise_errors=True)['rows'] == []
    assert core.cached_health(sqlite_conn, 'same', 'dev')['ok']
    other = tmp_path / 'other.db'
    with sqlite3.connect(other) as db:
        db.execute('CREATE TABLE other(x)')
    sqlite_conn.url = str(other)
    assert core.cached_tables(sqlite_conn, 'same', 'dev')['tables'] == ['other']
    assert core.cached_columns(sqlite_conn, 'same', 'dev', 'daily.events')['rows'] == []
    other.unlink()
    assert not core.cached_health(sqlite_conn, 'same', 'dev')['ok']


@pytest.mark.integration
def test_sqlite_primary_key_nullability_and_metadata_failures(sqlite_conn):
    def columns(table):
        return core.cached_columns(sqlite_conn, 'sample', None, table, raise_errors=True)['rows']
    assert columns('entries')[0]['is_nullable'] == 'NO'  # INTEGER PRIMARY KEY aliases rowid.
    for name, definition in [('composite', 'a INTEGER, b INTEGER, PRIMARY KEY(a,b)'),
                             ('descending', 'a INTEGER PRIMARY KEY DESC')]:
        core.run_query(sqlite_conn, f'CREATE TABLE {name}({definition})', allow_write=True)
        assert columns(name)[0]['is_nullable'] == 'YES'  # These are ordinary indexes.
    # A corrupt/unreachable SQLite file keeps the non-raising schema panel contract.
    sqlite_conn.url = 'sqlite:///missing.db'
    assert core.cached_columns(sqlite_conn, 'sample', None, 'entries') == {'rows': []}
    with pytest.raises(core.QuarryError):
        columns('entries')


@pytest.mark.integration
@pytest.mark.parametrize('kwargs', [{'max_rows': -1}, {'offset': -1}, {'timeout': 0}, {'connect_timeout': 0}])
def test_sqlite_rejects_invalid_query_options(sqlite_conn, kwargs):
    with pytest.raises(core.QuarryError) as caught:
        core.run_query(sqlite_conn, 'SELECT 1', **kwargs)
    assert caught.value.exit_code == core.EXIT_USAGE


@pytest.mark.integration
def test_sqlite_sql_and_connection_errors_have_stable_codes(sqlite_conn, monkeypatch):
    for sql in ('SELECT * FROM missing', 'WITH c AS (SELECT 1) invalid', "SELECT 'unterminated"):
        with pytest.raises(core.QuarryError) as caught:
            core.run_query(sqlite_conn, sql)
        assert caught.value.exit_code == core.EXIT_SQL_ERROR
    def refuse(*args, **kwargs):
        raise sqlite3.OperationalError('unable to open database file')
    monkeypatch.setattr(sqlite3, 'connect', refuse)
    with pytest.raises(core.QuarryError) as caught:
        core.run_query(sqlite_conn, 'SELECT 1')
    assert caught.value.exit_code == core.EXIT_CONNECTION_ERROR


@pytest.mark.integration
def test_sqlite_wal_readonly_sees_live_writes(sqlite_conn):
    db = sqlite3.connect(sqlite_engine.database_path(sqlite_conn))
    try:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("INSERT INTO entries VALUES(4,'wal',NULL)")
        db.commit()
        assert core.run_query(sqlite_conn, "SELECT note FROM entries WHERE id=4").rows == [{"note": 'wal'}]
    finally:
        db.close()
