"""Release regressions assert database effects and lossless result contracts."""
import json
import os
import shlex
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from conftest import requires_db, requires_mysql, requires_redis
from quarry import cli, core, mcp, workspace


@requires_db
@pytest.mark.integration
def test_readonly_explain_cannot_delete_through_gui_or_mcp(gui_server, pg_exec, monkeypatch):
    pg_exec("CREATE TABLE release_guard(id int); INSERT INTO release_guard VALUES (1),(2)")
    try:
        sql = "EXPLAIN ANALYZE DELETE FROM release_guard"
        status, body = gui_server.post('/api/query', {'db': 'testpg', 'sql': sql})
        assert status == 400 and body['code'] == 8
        monkeypatch.setattr(mcp, '_ALLOW_WRITE_FLAG', False)
        with pytest.raises(core.QuarryError) as exc:
            mcp.tool_exec_sql('testpg', sql)
        assert exc.value.exit_code == 8
        assert pg_exec('SELECT count(*) FROM release_guard')[1].strip() == '2'
    finally:
        pg_exec('DROP TABLE release_guard')


@requires_db
@pytest.mark.integration
def test_readonly_database_blocks_writing_function(ws, pg_exec):
    pg_exec("CREATE TABLE release_effect(id int); CREATE FUNCTION release_mutate() RETURNS int "
            "LANGUAGE plpgsql AS $$ BEGIN INSERT INTO release_effect VALUES (1); RETURN 1; END $$")
    try:
        with pytest.raises(core.QuarryError) as exc:
            core.run_query(core.get_connection('testpg'), 'SELECT release_mutate()')
        assert exc.value.exit_code == 8
        assert pg_exec('SELECT count(*) FROM release_effect')[1].strip() == '0'
    finally:
        pg_exec('DROP FUNCTION release_mutate(); DROP TABLE release_effect')


@requires_db
@pytest.mark.integration
def test_postgres_authorized_write_and_returning(ws, pg_exec, monkeypatch):
    conn = core.get_connection('testpg')
    core.run_query(conn, 'CREATE TABLE release_write(id int)', allow_write=True)
    try:
        monkeypatch.setattr(mcp, '_ALLOW_WRITE_FLAG', True)
        result = mcp.tool_exec_sql('testpg', 'INSERT INTO release_write VALUES (42) RETURNING id', write=True)
        assert result['rows'] == [{'id': 42}]
        assert pg_exec('SELECT id FROM release_write')[1].strip() == '42'
        core.run_query(conn, 'UPDATE release_write SET id=43', allow_write=True)
        assert core.run_query(conn, 'SELECT id FROM release_write').rows == [{'id': 43}]
    finally:
        pg_exec('DROP TABLE release_write')


@requires_db
@pytest.mark.integration
def test_postgres_lossless_columns_and_values(ws):
    conn = core.get_connection('testpg')
    result = core.run_query(conn, 'SELECT 9007199254740993::bigint AS id, '
                           '0.123456789012345678901::numeric AS amount, 1 AS dup, 2 AS dup')
    assert result.rows == [{'id': '9007199254740993', 'amount': '0.123456789012345678901', 'dup': 1, 'dup_2': 2}]
    empty = core.run_query(conn, 'SELECT 1 AS id, 2 AS id WHERE false', with_types=True)
    assert [c['name'] for c in empty.columns] == ['id', 'id_2']
    assert empty.rows == []


@requires_db
@pytest.mark.integration
def test_inner_limit_does_not_disable_outer_cap_or_pagination(ws):
    conn = core.get_connection('testpg')
    sql = 'SELECT n FROM generate_series(1,700) AS n CROSS JOIN (SELECT 1 LIMIT 1) sub ORDER BY n'
    first = core.run_query(conn, sql, max_rows=5)
    second = core.run_query(conn, sql, max_rows=5, offset=5)
    assert first.truncated and first.row_count == 5
    assert [r['n'] for r in second.rows] == [6, 7, 8, 9, 10]


@requires_db
@pytest.mark.e2e
def test_cli_default_cap_and_explicit_unlimited(qy):
    sql = 'SELECT generate_series(1,700) AS n'
    capped = qy('exec', 'testpg', '--sql', sql)
    assert capped.returncode == 0 and len(json.loads(capped.stdout)) == 500
    assert 'truncated' in capped.stderr
    full = qy('exec', 'testpg', '--sql', sql, '--max-rows', '0')
    assert full.returncode == 0 and len(json.loads(full.stdout)) == 700


@pytest.mark.unit
@pytest.mark.parametrize('sql', ['MATCH (n) SET n.x=1 RETURN n', 'MATCH (n) DETACH DELETE n', 'CALL db.mutate()'])
def test_cypher_writes_rejected_before_transport(sql, monkeypatch):
    monkeypatch.setattr(core, 'run_neptune_cypher', lambda *a, **k: pytest.fail('write reached transport'))
    conn = core.Connection(key='graph', url='http://localhost:8182', engine='neptune', env='prod', production=True)
    with pytest.raises(core.QuarryError) as exc:
        core.run_query(conn, sql)
    assert exc.value.exit_code == 8


@pytest.mark.unit
def test_cypher_read_limit_and_page():
    sql, cap = core.enforce_safety('MATCH (n) RETURN n', allow_write=False, max_rows=5, offset=5, engine='neptune')
    assert sql.endswith('SKIP 5 LIMIT 6') and cap == 5


@pytest.mark.unit
def test_escaped_cypher_literals_do_not_hide_write_clauses():
    assert not core.is_query_read_only(r"MATCH (n) WHERE n.s = 'a\'b' SET n.x='c' RETURN n", 'neptune')
    sql = r"MATCH (n) WHERE n.s = 'a\'LIMIT' RETURN n"
    bounded, cap = core.enforce_safety(sql, allow_write=False, max_rows=5, engine='neptune')
    assert bounded.endswith('LIMIT 6') and cap == 5


@pytest.mark.unit
@pytest.mark.parametrize('keyword', ['create', 'merge', 'set', 'delete', 'detach', 'remove', 'drop', 'call', 'foreach', 'load'])
def test_cypher_property_map_and_label_names_are_reads(keyword):
    queries = [f'MATCH (n) RETURN n.{keyword}',
               f'MATCH (n:{keyword} {{{keyword}: 1}}) RETURN n . {keyword}',
               f'MATCH (n) RETURN {{{keyword}: n.{keyword}}}',
               f'MATCH (n) RETURN n : {keyword}']
    for sql in queries:
        assert core.is_query_read_only(sql, 'neptune')
        assert core.enforce_safety(sql, allow_write=False, max_rows=5, engine='neptune')[1] == 5
    # Recognizing a property must never hide the following real write clause.
    assert not core.is_query_read_only(f'MATCH (n) WHERE n.{keyword}=1 SET n.x=2 RETURN n', 'neptune')
    assert not core.is_query_read_only(f'MATCH (n:{keyword}) DELETE n', 'neptune')


@pytest.mark.unit
@pytest.mark.parametrize('sql', [
    'MATCH (n) CALL { WITH n SET n.x=1 } RETURN n',
    'MATCH (n) FOREACH (x IN [1] | SET n.x=x) RETURN n',
    'MATCH (n) REMOVE n.set RETURN n',
])
def test_nested_cypher_mutations_remain_blocked(sql):
    assert not core.is_query_read_only(sql, 'neptune')


@pytest.mark.unit
def test_mysql_executable_comments_rejected_without_execution():
    conn = core.Connection(key='mysql', url='mysql://localhost/db', engine='mysql')
    with pytest.raises(core.QuarryError, match='executable MySQL comments'):
        core.run_query(conn, "SELECT 1 /*! INTO OUTFILE '/tmp/out' */", allow_write=True)


@pytest.mark.unit
@pytest.mark.parametrize('sql', ['SELECT 1 INTO copied', 'SET ROLE admin', 'EXPLAIN ANALYZE DELETE FROM t', 'SELECT 1 \\gexec'])
def test_non_read_commands_fail_closed(sql):
    assert not core.is_read_only(sql)


@pytest.mark.unit
@pytest.mark.parametrize('sql', ['SELECT 1; DELETE FROM t', 'SELECT 1 \\gexec', ''])
def test_write_flag_does_not_allow_client_commands_or_batches(sql, monkeypatch):
    monkeypatch.setattr(core, '_rows_postgres', lambda *a, **k: pytest.fail('unsafe input reached execution'))
    conn = core.Connection(key='pg', url='postgresql://localhost/test', engine='postgres')
    with pytest.raises(core.QuarryError):
        core.run_query(conn, sql, allow_write=True)


@pytest.mark.unit
def test_limit_validation_and_explicit_disable():
    for max_rows, offset in [(-1, 0), (5, -1)]:
        with pytest.raises(core.QuarryError):
            core.enforce_safety('SELECT 1', allow_write=False, max_rows=max_rows, offset=offset)
    assert core.enforce_safety('SELECT 1', allow_write=False, max_rows=0) == ('SELECT 1', None)


@pytest.mark.unit
def test_redis_invalid_json_is_an_error(monkeypatch):
    from quarry import redis_engine
    monkeypatch.setattr(redis_engine, 'resolve_redis_cli', lambda: 'redis-cli')
    monkeypatch.setattr(redis_engine.subprocess, 'run', lambda *a, **k: subprocess.CompletedProcess([], 0, 'invalid', ''))
    with pytest.raises(core.QuarryError) as exc:
        redis_engine.run_redis('redis://localhost', 'PING')
    assert exc.value.exit_code == 3


@pytest.mark.unit
def test_mcp_workspace_after_subcommand_preserves_global():
    parser = cli.build_parser()
    assert parser.parse_args(['mcp', '--workspace', '/tmp/demo']).workspace == '/tmp/demo'
    assert parser.parse_args(['--workspace', '/tmp/demo', 'mcp']).workspace == '/tmp/demo'


@requires_redis
@pytest.mark.integration
def test_redis_nil_empty_errors_and_prod_confirmation(tmp_path):
    url = os.environ.get('QUARRY_TEST_REDIS_URL', 'redis://127.0.0.1:6379/15')
    key = 'qy-release-' + uuid.uuid4().hex
    conn = core.Connection(key='cache', url=url, engine='redis', env='prod', production=True)
    try:
        assert core.run_query(conn, f'GET {key}').rows == [{'value': None}]
        for value in ['', 'first\nsecond\n']:
            core.run_query(conn, f'SET {key} {shlex.quote(value)}', allow_write=True)
            assert core.run_query(conn, f'GET {key}').rows == [{'value': value}]
        with pytest.raises(core.QuarryError) as exc:
            core.run_query(conn, f'LRANGE {key} 0 -1')
        assert exc.value.exit_code == 3
        (tmp_path / 'connections.toml').write_text(f'[cache]\nurl="{url}"\nengine="redis"\nenv="prod"\nproduction=true\n')
        proc = subprocess.run([sys.executable, '-m', 'quarry.cli', '--workspace', str(tmp_path),
                               'exec', 'cache', '--sql', f'SET {key} overwritten', '--write'],
                              input='', capture_output=True, text=True,
                              env={**os.environ, 'PYTHONPATH': str(Path(__file__).parents[1] / 'src')})
        assert proc.returncode != 0 and 'PROD' in proc.stderr
        assert core.run_query(conn, f'GET {key}').rows == [{'value': 'first\nsecond\n'}]
    finally:
        core.run_query(conn, f'DEL {key}', allow_write=True)


@requires_redis
@pytest.mark.integration
def test_redis_scan_mode_preserves_keys_and_cli_output(tmp_path):
    url = os.environ.get('QUARRY_TEST_REDIS_URL', 'redis://127.0.0.1:6379/15')
    prefix = 'qy-scan-' + uuid.uuid4().hex
    keys = [prefix + suffix for suffix in ['plain', 'two words', 'line\nbreak', '"quoted"']]
    conn = core.Connection(key='cache', url=url, engine='redis')
    try:
        for key in keys:
            core.run_query(conn, shlex.join(['SET', key, 'v']), allow_write=True)
        command = f'--scan --pattern {prefix}* --count 1'
        assert {r['value'] for r in core.run_query(conn, command).rows} == set(keys)
        assert core.run_query(conn, f'--scan --pattern {prefix}-missing').rows == []
        (tmp_path / 'connections.toml').write_text(f'[cache]\nurl="{url}"\nengine="redis"\n')
        proc = subprocess.run([sys.executable, '-m', 'quarry.cli', '--workspace', str(tmp_path),
                               'exec', 'cache', '--sql', command], capture_output=True, text=True,
                              env={**os.environ, 'PYTHONPATH': str(Path(__file__).parents[1] / 'src')})
        assert proc.returncode == 0, proc.stderr
        assert {r['value'] for r in json.loads(proc.stdout)} == set(keys)
    finally:
        core.run_query(conn, shlex.join(['DEL', *keys]), allow_write=True)


@requires_mysql
@pytest.mark.integration
def test_mysql_write_commit_and_lossless_empty_results():
    conn = core.Connection(key='mysql', url=os.environ['QUARRY_TEST_MYSQL_URL'], engine='mysql')
    table = 'qy_release_' + uuid.uuid4().hex
    core.run_query(conn, f'CREATE TABLE {table}(id bigint, amount decimal(30,21))', allow_write=True)
    try:
        with pytest.raises(core.QuarryError) as exc:
            core.run_query(conn, f'INSERT INTO {table} VALUES (1,1)')
        assert exc.value.exit_code == 8
        core.run_query(conn, f'INSERT INTO {table} VALUES (9007199254740993,0.123456789012345678901)', allow_write=True)
        rows = core.run_query(conn, f'SELECT * FROM {table}').rows
        assert rows == [{'id': '9007199254740993', 'amount': '0.123456789012345678901'}]
        empty = core.run_query(conn, f'SELECT id, id FROM {table} WHERE false')
        assert [c['name'] for c in empty.columns] == ['id', 'id_2']
        assert core.run_query(conn, 'SELECT 1 AS id, 2 AS id, 3 AS id').rows == [{'id': 1, 'id_2': 2, 'id_3': 3}]
    finally:
        core.run_query(conn, f'DROP TABLE {table}', allow_write=True)
