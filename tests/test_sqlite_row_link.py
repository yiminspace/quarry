"""SQLite record links use real key metadata and retain one-record identity."""
import sqlite3

import pytest

from quarry import core, row_link


pytestmark = pytest.mark.integration


@pytest.fixture
def sqlite_conn(tmp_path):
    path = tmp_path / 'records.sqlite'
    with sqlite3.connect(path) as db:
        db.executescript('''
            CREATE TABLE records (tenant TEXT, ID INTEGER, slug TEXT UNIQUE,
                                  PRIMARY KEY (tenant, ID));
            CREATE TABLE "odd.table" ("odd\"\"key" TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE collated (id TEXT COLLATE NOCASE);
            CREATE UNIQUE INDEX collated_key ON collated(id COLLATE BINARY);
            INSERT INTO collated VALUES ('a'), ('A');
            CREATE TABLE ambiguous (id PRIMARY KEY, slug TEXT UNIQUE);
            INSERT INTO ambiguous VALUES (9007199254740993, 'large-integer');
            INSERT INTO ambiguous VALUES ('9007199254740993', 'numeric-text');
            INSERT INTO ambiguous VALUES (X'616263', 'binary');
            INSERT INTO ambiguous VALUES ('abc', 'text');
            INSERT INTO ambiguous VALUES (1e999, 'positive-infinity');
            INSERT INTO ambiguous VALUES (-1e999, 'negative-infinity');
            INSERT INTO ambiguous VALUES ('inf', 'short-infinity-text');
            INSERT INTO ambiguous VALUES ('Infinity', 'infinity-text');
            CREATE TABLE partial (id TEXT, flag INTEGER);
            CREATE UNIQUE INDEX partial_key ON partial(id) WHERE flag = 1;
            CREATE TABLE expression (id TEXT, slug TEXT);
            CREATE UNIQUE INDEX expression_key ON expression(lower(id), slug);
            CREATE VIEW record_view AS SELECT * FROM records;
        ''')
        db.execute('INSERT INTO records VALUES (?, ?, ?)', ("O'Reilly\\team & 中", 3, 'fallback'))
        db.execute('INSERT INTO "odd.table" VALUES (?, ?)', ('a\\b\'c', 'test'))
    return core.Connection(key='sqlite', url='sqlite:///' + str(path), engine='sqlite')


def _link(conn, sql):
    result = core.run_query(conn, sql, max_rows=0)
    return row_link.record_query(conn, result.sql, result.rows[0]), result.rows


def test_sqlite_composite_key_and_plain_string_escaping(sqlite_conn):
    out, rows = _link(sqlite_conn, 'SELECT * FROM main.records')
    assert out['table'] == 'main.records'
    assert '"tenant" COLLATE BINARY = ' in out['sql']
    assert '"ID" COLLATE BINARY = 3' in out['sql']
    assert "'O''Reilly\\team & 中'" in out['sql']
    assert core.run_query(sqlite_conn, out['sql']).rows == rows


def test_sqlite_default_row_cap_keeps_record_link_available(sqlite_conn):
    result = core.run_query(sqlite_conn, 'SELECT * FROM records')
    out = row_link.record_query(sqlite_conn, result.sql, result.rows[0])
    assert core.run_query(sqlite_conn, out['sql']).rows == result.rows


def test_sqlite_row_cap_does_not_enable_ambiguous_query_links(sqlite_conn):
    result = core.run_query(sqlite_conn, 'SELECT id FROM collated UNION SELECT id FROM ambiguous')
    assert row_link.record_query(sqlite_conn, result.sql, result.rows[0]) == {'reason': 'unsupported_query'}


@pytest.mark.parametrize('source', ['"odd.table"', '`odd.table`', '[odd.table]'])
def test_sqlite_quoted_special_identifiers(sqlite_conn, source):
    out, rows = _link(sqlite_conn, f'SELECT * FROM {source}')
    assert '"odd""key" COLLATE BINARY' in out['sql']
    assert core.run_query(sqlite_conn, out['sql']).rows == rows


def test_sqlite_key_names_use_ascii_case_insensitive_matching(sqlite_conn):
    out = row_link.record_query(sqlite_conn, 'SELECT TENANT, id FROM RECORDS',
                               {'TENANT': "O'Reilly\\team & 中", 'id': 3})
    assert core.run_query(sqlite_conn, out['sql']).rows[0]['slug'] == 'fallback'


def test_sqlite_unique_fallback_for_unprojected_primary_key(sqlite_conn):
    out, _ = _link(sqlite_conn, 'SELECT slug FROM records')
    assert '"slug" COLLATE BINARY' in out['sql']
    assert '"tenant"' not in out['sql']
    assert core.run_query(sqlite_conn, out['sql']).rows[0]['ID'] == 3


def test_sqlite_index_collation_does_not_broaden_record_link(sqlite_conn):
    out, rows = _link(sqlite_conn, "SELECT * FROM collated WHERE id = 'a' COLLATE BINARY")
    assert core.run_query(sqlite_conn, out['sql']).rows == rows == [{'id': 'a'}]


@pytest.mark.parametrize('condition', [
    "slug = 'large-integer'", "slug = 'numeric-text'", "slug = 'binary'",
    "slug = 'positive-infinity'", "slug = 'negative-infinity'",
])
def test_sqlite_serialized_keys_are_not_mistaken_for_text(sqlite_conn, condition):
    sql = 'SELECT id FROM ambiguous WHERE ' + condition
    out, _ = _link(sqlite_conn, sql)
    assert out == {'reason': 'no_unique_key'}


@pytest.mark.parametrize('value', ['inf', 'Infinity'])
def test_sqlite_nonfinite_number_labels_remain_usable_text_keys(sqlite_conn, value):
    out, rows = _link(sqlite_conn, f"SELECT id FROM ambiguous WHERE id = '{value}'")
    assert core.run_query(sqlite_conn, out['sql']).rows[0]['id'] == rows[0]['id'] == value


def test_sqlite_serialized_key_falls_back_to_unambiguous_unique(sqlite_conn):
    out, rows = _link(sqlite_conn, "SELECT * FROM ambiguous WHERE slug = 'binary'")
    assert '"slug" COLLATE BINARY' in out['sql']
    assert core.run_query(sqlite_conn, out['sql']).rows == rows


@pytest.mark.parametrize('table', ['partial', 'expression', 'record_view'])
def test_sqlite_partial_expression_and_view_keys_are_not_supported(sqlite_conn, table):
    assert row_link.record_query(sqlite_conn, f'SELECT * FROM {table}',
                                {'id': 'a', 'slug': 'b'}) == {'reason': 'no_unique_key'}


@pytest.mark.parametrize('sql', [
    'SELECT * FROM records JOIN collated ON records.slug = collated.id',
    'SELECT * FROM records UNION SELECT * FROM records',
    'SELECT * FROM other.records', 'SELECT * FROM [unterminated',
])
def test_sqlite_ambiguous_or_nonmain_sources_are_rejected(sqlite_conn, sql):
    assert row_link.record_query(sqlite_conn, sql, {}) == {'reason': 'unsupported_query'}


def test_sqlite_null_and_extremely_long_numeric_keys_are_unavailable(sqlite_conn):
    for value in (None, '9' * 5000):
        assert row_link.record_query(sqlite_conn, 'SELECT id FROM ambiguous',
                                    {'id': value}) == {'reason': 'no_unique_key'}
