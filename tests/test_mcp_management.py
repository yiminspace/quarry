"""Saved-query and diagnostic contracts, isolated from user workspaces and databases."""
import json
import os
import sqlite3
from pathlib import Path

import pytest

from quarry import core, mcp, workspace

pytestmark = pytest.mark.integration


@pytest.fixture
def home(tmp_path, monkeypatch):
    for attr in ('WS', 'WS_LIST', '_EXPLICIT'):
        monkeypatch.setattr(workspace, attr, getattr(workspace, attr))
    (tmp_path / 'connections.toml').write_text('[db]\nurl="sqlite:///data.db"\n')
    with sqlite3.connect(tmp_path / 'data.db') as db:
        db.execute('CREATE TABLE items(id INTEGER)')
    workspace.configure_workspace(str(tmp_path))
    return tmp_path


def call(tool_name, **args):
    result = mcp._handle_tools_call({'name': tool_name, 'arguments': args})
    return json.loads(result['content'][0]['text']), result['isError']


def test_save_describe_without_execution_and_preserve_overwrite(home, monkeypatch):
    monkeypatch.setattr(core, 'run_query', lambda *a, **k: pytest.fail('save must not execute SQL'))
    args = dict(name='lookup', db='db', sql="SELECT * FROM items WHERE id=:'id'", desc='Lookup',
                tags=['items'], params=[{'name': 'id', 'type': 'int', 'required': True}])
    saved, failed = call('save_query', **args)
    assert not failed
    target = Path(saved['path'])
    original = target.read_bytes()
    details, failed = call('describe_saved_query', name='lookup')
    assert not failed and details['sql'] == args['sql'] + ';'
    assert details['params'] == [{'name': 'id', 'type': 'int', 'required': True, 'default': None}]
    assert details['schema_sources'] == [] and details['last_validated'] is None
    assert call('save_query', **args)[1]
    assert target.read_bytes() == original
    assert not call('save_query', **{**args, 'sql': 'DELETE FROM items'}, overwrite=True)[1]
    assert core.load_query('lookup').sql == 'DELETE FROM items;'
    assert core.load_query('lookup').last_validated is None
    assert call('describe_saved_query', name='missing')[1]


@pytest.mark.parametrize('changes', [
    {'name': '../escape'}, {'name': ' query '}, {'name': '   '}, {'name': 'wild*'}, {'name': 'wild?'}, {'name': 'a[b]'},
    {'name': 'bad\u2028name'}, {'desc': 'bad\u0085header'}, {'name': 'a/b'}, {'db': '../db'}, {'sql': '   '},
    {'sql': '-- @param: injected (text)\nSELECT 1'}, {'desc': 'bad\n-- @db: other'},
    {'tags': ['bad\nheader']}, {'tags': ['a,b']}, {'tags': ['']}, {'tags': 'tag'},
    {'params': {}}, {'params': [42]}, {'params': [{'name': 'x', 'extra': True}]},
    {'params': [{'name': ''}]}, {'params': [{'name': 'x', 'type': 'bad)'}]},
    {'params': [{'name': 'x'}, {'name': 'x'}]},
    {'params': [{'name': 'x', 'required': 'false'}]},
    {'params': [{'name': 'x', 'required': True, 'default': '1'}]},
    {'params': [{'name': 'x', 'default': 'bad)'}]},
    {'params': [{'name': 'x', 'default': ' a '}]}, {'overwrite': 'false'},
    {'workspace': '/not/a/configured/workspace'},
])
def test_invalid_save_does_not_create_files(home, changes):
    payload, failed = call('save_query', **(dict(name='test', db='db', sql='SELECT 1') | changes))
    assert failed and payload['code'] == core.EXIT_USAGE
    assert not list(home.rglob('*.sql'))


def test_symlink_escape_is_rejected_even_on_overwrite(home, tmp_path_factory):
    outside = tmp_path_factory.mktemp('outside')
    (home / 'queries').mkdir()
    (home / 'queries/db').symlink_to(outside, target_is_directory=True)
    assert call('save_query', name='escape', db='db', sql='SELECT 1', overwrite=True)[1]
    assert not list(outside.iterdir())


def test_target_symlink_is_not_followed(home):
    target = home / 'queries/db'
    target.mkdir(parents=True)
    outside = home / 'keep.sql'
    outside.write_text('keep')
    (target / 'escape.sql').symlink_to(outside)
    assert call('save_query', name='escape', db='db', sql='SELECT 1', overwrite=True)[1]
    assert outside.read_text() == 'keep'


def test_multiple_workspaces_need_explicit_target_and_unique_name(home, tmp_path):
    second = tmp_path / 'second'
    second.mkdir()
    (second / 'connections.toml').write_text('[second]\nurl="sqlite:///data.db"\n')
    workspace.configure_workspace(str(home) + os.pathsep + str(second))
    assert call('save_query', name='q', db='second', sql='SELECT 1')[1]
    assert call('save_query', name='q', db='second', sql='SELECT 1', workspace=str(home))[1]
    out, failed = call('save_query', name='q', db='second', sql='SELECT 1', workspace=str(second))
    assert not failed and Path(out['path']).is_relative_to(second)
    assert not (home / 'queries').exists()
    assert call('save_query', name='q', db='db', sql='SELECT 1', workspace=str(home), overwrite=True)[1]
    (home / 'queries/db').mkdir(parents=True)
    (home / 'queries/db/q.sql').write_text('-- @name: q\n-- @db: db\nSELECT 2;')
    assert call('describe_saved_query', name='q')[1]  # existing ambiguity is never silently resolved


def test_ping_sqlite_and_missing_file(home):
    out, failed = call('ping_connection', db='db', timeout=1)
    assert not failed and out['ok'] and out['elapsed_ms'] >= 0
    assert out['key'] == 'db' and out['error'] is None
    (home / 'data.db').unlink()
    out, failed = call('ping_connection', db='db')
    assert not failed and not out['ok'] and out['error_kind']
    assert not (home / 'data.db').exists()
    assert call('ping_connection', db='unknown')[1]


@pytest.mark.parametrize('timeout', [0, -1, 31, 1.5, True, '10'])
def test_ping_rejects_invalid_timeout(home, timeout):
    assert call('ping_connection', db='db', timeout=timeout)[1]


@pytest.mark.parametrize(('error', 'kind'), [
    ('timed out SECRET', 'timeout'), ('password authentication failed SECRET', 'authentication'),
    ('ssh key missing SECRET', 'ssh'), ('psql not found SECRET', 'dependency_or_file'),
    ('refused postgresql://user:SECRET@host/db?token=SECRET', 'connection'),
])
def test_ping_sanitizes_errors_and_preserves_target(home, monkeypatch, error, kind):
    def fail(*args, **kwargs):
        raise RuntimeError(error)
    monkeypatch.setattr(core, 'run_query', fail)
    out, failed = call('ping_connection', db='db')
    assert not failed and not out['ok'] and out['error_kind'] == kind
    assert 'SECRET' not in json.dumps(out)
    assert out['db'] == 'db' and out['production'] is False


@pytest.mark.parametrize(('name', 'db'), [('..', 'db'), ('q', '../db'), ('q', 'a\\b')])
def test_shared_save_rejects_unsafe_path(home, name, db):
    with pytest.raises(core.QuarryError):
        core.save_query(core.Query(name=name, db=db, sql='SELECT 1'))


def test_mcp_save_is_readable_by_cli_and_cli_save_by_mcp(home, capsys):
    from quarry import cli
    assert not call('save_query', name='from_mcp', db='db', sql='SELECT 1')[1]
    args = cli.build_parser().parse_args(['describe', 'from_mcp', '--format', 'json'])
    assert args.func(args) == 0
    assert json.loads(capsys.readouterr().out)['sql'] == 'SELECT 1;'
    args = cli.build_parser().parse_args(['save', 'from_cli', '--db', 'db',
                                          '--sql', 'SELECT 2', '--no-validate'])
    assert args.func(args) == 0
    out, failed = call('describe_saved_query', name='from_cli')
    assert not failed and out['sql'] == 'SELECT 2;'


@pytest.mark.parametrize(('first', 'second', 'db'), [
    ('[sales]\nurl="sqlite:///first.db"\n',
     '[sales_dev]\nurl="sqlite:///second.db"\ndb="sales"\nenv="dev"\n', 'sales'),
    ('[sales_prod]\nurl="sqlite:///first.db"\ndb="sales"\nenv="prod"\n',
     '[sales_dev]\nurl="sqlite:///second.db"\ndb="sales"\nenv="dev"\n', 'sales'),
    ('[sales_prod]\nurl="sqlite:///first.db"\ndb="sales"\nenv="prod"\n',
     '[sales_dev]\nurl="sqlite:///second.db"\ndb="sales"\nenv="dev"\n', 'sales_dev'),
])
def test_save_rejects_cross_workspace_resolution(home, first, second, db):
    (home / 'connections.toml').write_text(first)
    other = home / 'second'
    other.mkdir()
    (other / 'connections.toml').write_text(second)
    workspace.configure_workspace(str(home) + os.pathsep + str(other))
    payload, failed = call('save_query', name='collision', db=db, sql='SELECT 1', workspace=str(other))
    assert failed and payload['code'] == core.EXIT_USAGE
    assert 'resolves across workspaces' in payload['error']
    assert not list(home.rglob('*.sql'))
