"""Key-based record SQL uses verified metadata and never executes the record SELECT."""
from types import SimpleNamespace

import pytest

from conftest import requires_mysql

from quarry import row_link


def _metadata(monkeypatch, keys, engine='postgres'):
    monkeypatch.setattr(row_link.core, 'connection_engine', lambda _: engine)
    calls = []
    def run(conn, sql, **kwargs):
        calls.append((sql, kwargs))
        return SimpleNamespace(rows=[{'key_id': str(i), 'column_name': col}
                                     for i, key in enumerate(keys) for col in key])
    monkeypatch.setattr(row_link.core, 'run_query', run)
    return calls


@pytest.mark.parametrize('engine', ['postgres', 'mysql'])
def test_record_query_composite_primary_and_escaped_values(monkeypatch, engine):
    calls = _metadata(monkeypatch, [['tenant', 'id'], ['slug']], engine)
    out = row_link.record_query(None, 'select * from app.records',
                               {'tenant': "O'Reilly\\team & 中", 'id': 3, 'slug': 'other'})
    quote = '"' if engine == 'postgres' else '`'
    assert f'{quote}id{quote} = 3' in out['sql']
    assert f'{quote}tenant{quote} = ' in out['sql']
    assert 'slug' not in out['sql']
    if engine == 'postgres':
        assert "E'O''Reilly\\\\team & 中'" in out['sql']
        assert calls[0][1]['params'] == {'relation': '"app"."records"'}
    else:
        assert "CONVERT(X'" in out['sql']
        assert calls[0][1]['params'] == {'schema': 'app', 'table': 'records'}
    assert len(calls) == 1


def test_record_query_unique_fallback_and_missing_keys(monkeypatch):
    _metadata(monkeypatch, [['id'], ['slug']])
    out = row_link.record_query(None, 'SELECT slug FROM records WHERE slug = \'abc\'',
                               {'id': 42, 'slug': 'abc'})
    assert out['sql'] == 'SELECT * FROM "records" WHERE "slug" = \'abc\';'
    for row in ({}, {'slug': None}, {'slug': {}}, {'slug': 9007199254740992}):
        assert row_link.record_query(None, 'select slug from records', row) == {'reason': 'no_unique_key'}


@pytest.mark.parametrize('sql', [
    'select * from a join b on a.id=b.id', 'select count(*) from a',
    'select id as slug from a', 'select * from a, b',
    'select * from a union select * from b', 'with x as (select * from a) select * from x',
    'select * from a where id in (select id from b)', 'select * from a; select * from b',
])
def test_record_query_rejects_ambiguous_sources(monkeypatch, sql):
    calls = _metadata(monkeypatch, [['id']])
    assert row_link.record_query(None, sql, {'id': 1}) == {'reason': 'unsupported_query'}
    assert not calls


def test_record_query_quoted_identifiers_and_no_keys(monkeypatch):
    calls = _metadata(monkeypatch, [['odd"key']])
    out = row_link.record_query(None, 'select * from "odd.schema"."odd.table"', {'odd"key': True})
    assert out['sql'] == 'SELECT * FROM "odd.schema"."odd.table" WHERE "odd""key" = TRUE;'
    assert calls[0][1]['params']['relation'] == '"odd.schema"."odd.table"'
    _metadata(monkeypatch, [])
    assert row_link.record_query(None, 'select * from a', {'id': 1}) == {'reason': 'no_unique_key'}


@pytest.mark.parametrize('sql', [
    "select * from records where status = 'union'",
    "select * from records where label = 'a,b; (select from join) -- /*'",
    "select * from records where label = 'it''s union'",
    r"select * from records where label = E'it\'s union'",
    "select * from records where label = $tag$union, (select);$tag$",
])
def test_record_query_literals_do_not_change_structure(monkeypatch, sql):
    _metadata(monkeypatch, [['id']])
    assert row_link.record_query(None, sql, {'id': 1})['sql'] == 'SELECT * FROM "records" WHERE "id" = 1;'


@pytest.mark.parametrize('sql', [
    "select * from a where label = 'union' union select * from b",
    "select * from a where label = 'a,b'; select * from b",
    "select * from a where label = 'unterminated",
    "select * from a where label = $$unterminated",
])
def test_record_query_masks_literals_but_keeps_unsupported_structure(monkeypatch, sql):
    calls = _metadata(monkeypatch, [['id']])
    assert row_link.record_query(None, sql, {'id': 1}) == {'reason': 'unsupported_query'}
    assert not calls


def test_record_query_mysql_case_insensitive_key_names(monkeypatch):
    _metadata(monkeypatch, [['ID']], 'mysql')
    assert row_link.record_query(None, 'select id from records', {'id': 7})['sql'] == 'SELECT * FROM `records` WHERE `ID` = 7;'


@pytest.mark.parametrize('value', ['base64:/w==', 'decoded-ascii-bytes'])
def test_record_query_mysql_binary_keys_disable_link(monkeypatch, value):
    monkeypatch.setattr(row_link.core, 'connection_engine', lambda _: 'mysql')
    monkeypatch.setattr(row_link.core, 'run_query', lambda *args, **kwargs: SimpleNamespace(rows=[
        {'key_id': 'PRIMARY', 'column_name': 'id', 'data_type': 'varbinary'}]))
    assert row_link.record_query(None, 'select * from records', {'id': value}) == {'reason': 'no_unique_key'}


def test_record_query_binary_key_falls_back_to_text_unique_key(monkeypatch):
    monkeypatch.setattr(row_link.core, 'connection_engine', lambda _: 'mysql')
    monkeypatch.setattr(row_link.core, 'run_query', lambda *args, **kwargs: SimpleNamespace(rows=[
        {'key_id': 'PRIMARY', 'column_name': 'ID', 'data_type': 'binary'},
        {'key_id': 'slug_key', 'column_name': 'Slug', 'data_type': 'varchar'}]))
    out = row_link.record_query(None, 'select * from records', {'ID': 'base64:/w==', 'slug': 'base64:/w=='})
    assert '`Slug` = CONVERT(' in out['sql']
    assert '`ID` =' not in out['sql']


# The CI database verifies information_schema joins and actual driver column
# names; local environments without MySQL skip only this integration case.
@requires_mysql
@pytest.mark.integration
def test_record_query_real_mysql_casing_and_binary_keys():
    import os
    conn = row_link.core.Connection(key='mysql', url=os.environ['QUARRY_TEST_MYSQL_URL'], engine='mysql')
    run = lambda sql: row_link.core.run_query(conn, sql, allow_write=True, max_rows=0)
    run('CREATE TABLE qy_record_link_mysql_case (ID varchar(50) PRIMARY KEY)')
    try:
        run("INSERT INTO qy_record_link_mysql_case VALUES ('upper-key')")
        result = run('SELECT id FROM qy_record_link_mysql_case')
        out = row_link.record_query(conn, result.sql, result.rows[0])
        assert '`ID` =' in out['sql']
        assert run(out['sql']).rows == [{'ID': 'upper-key'}]
    finally:
        run('DROP TABLE qy_record_link_mysql_case')
    run('CREATE TABLE qy_record_link_mysql_binary (ID varbinary(10) PRIMARY KEY)')
    try:
        run("INSERT INTO qy_record_link_mysql_binary VALUES (X'ff')")
        result = run('SELECT * FROM qy_record_link_mysql_binary')
        assert result.rows == [{'ID': 'base64:/w=='}]
        assert row_link.record_query(conn, result.sql, result.rows[0]) == {'reason': 'no_unique_key'}
    finally:
        run('DROP TABLE qy_record_link_mysql_binary')
