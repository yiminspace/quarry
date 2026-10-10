"""Key-based record SQL uses verified metadata and never executes the record SELECT."""
from types import SimpleNamespace

import pytest

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
