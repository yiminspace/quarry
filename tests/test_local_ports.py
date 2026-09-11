"""Port changes preserve local data and leave foreign connections/services alone."""
import json
import os
from dataclasses import replace

import pytest

from quarry import core, local, workspace


@pytest.fixture()
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv('QUARRY_CONFIG', str(tmp_path / 'config.toml'))
    (tmp_path / 'config.toml').write_text('# keep\nworkspaces = []\n[tunnel]\nkeep_alive = true\n')
    workspace.configure_workspace(str(tmp_path / 'ws'))
    monkeypatch.setattr(local, 'require_docker', lambda: None)
    return tmp_path


def test_configured_port_roundtrip_preserves_config(setup):
    workspace._write_table_scalar('local', 'postgres_port', '55434')
    assert local.configured_spec('postgres').port == 55434
    assert local.specs_for('postgres')[0].url('shop').endswith(':55434/shop')
    assert workspace._read_config()['tunnel']['keep_alive'] is True
    assert '# keep' in (setup / 'config.toml').read_text()


def test_only_managed_old_port_connections_move(setup):
    workspaces = [setup / 'one', setup / 'two']
    workspace.configure_workspace(os.pathsep.join(map(str, workspaces)))
    for ws in workspaces:
        ws.mkdir()
        ws.joinpath('connections.toml').write_text('''[managed]
url = "postgresql://u:pw@localhost:5433/shop?sslmode=disable"
engine = "postgres"
env = "local"
local_volume = "quarry-local-pgdata"
[custom]
url = "postgresql://u:pw@localhost:55432/shop"
env = "local"
local_volume = "quarry-local-pgdata"
[foreign]
url = "postgresql://u:pw@localhost:5433/auth"
env = "local"
[remote]
url = "postgresql://u:pw@localhost:5433/shop"
env = "prod"
local_volume = "quarry-local-pgdata"
''')
    local._sync_managed_ports(replace(local.PG_SPEC, port=55434), 5433)
    for ws in workspaces:
        _, data = core._read_connections_file_parts(ws / 'connections.toml')
        assert data['managed']['url'] == 'postgresql://u:pw@localhost:55434/shop?sslmode=disable'
        assert ':55432/' in data['custom']['url']
        assert ':5433/' in data['foreign']['url'] and ':5433/' in data['remote']['url']
        assert list(ws.glob('connections.toml.before-port-*'))


def fake_inspect(monkeypatch, state='stopped', mount=True):
    monkeypatch.setattr(local, 'container_state', lambda name: state)
    calls = []
    def docker(args, **kwargs):
        calls.append(args)
        if args[0] == 'inspect':
            return 0, json.dumps([{'HostConfig': {'PortBindings': {'5432/tcp': [{'HostPort': '5433'}]}},
                                   'Config': {'Image': 'postgres:16'},
                                   'Mounts': [{'Name': local.PG_SPEC.volume}] if mount else []}]), ''
        return 0, '', ''
    monkeypatch.setattr(local, '_run_docker', docker)
    return calls


def test_stopped_container_recreated_without_deleting_volume(setup, monkeypatch):
    calls = fake_inspect(monkeypatch)
    monkeypatch.setattr(local, 'port_in_use', lambda port: False)
    monkeypatch.setattr(local, 'start_container', lambda spec, **kw: 'created')
    spec, state = local.start_on_port(local.PG_SPEC, 55434)
    assert spec.port == 55434 and state == 'created'
    assert ['rm', local.PG_SPEC.container] in calls
    assert not any('volume' in call for call in calls)
    assert local.configured_spec('postgres').port == 55434


@pytest.mark.parametrize('state,mount,occupied', [('running', True, False), ('stopped', False, False), ('stopped', True, True)])
def test_port_change_refuses_unsafe_recreation(setup, monkeypatch, state, mount, occupied):
    calls = fake_inspect(monkeypatch, state=state, mount=mount)
    monkeypatch.setattr(local, 'port_in_use', lambda port: occupied)
    monkeypatch.setattr(local, 'port_owner', lambda port: 'authkit-postgres-dev')
    with pytest.raises(core.QuarryError):
        local.start_on_port(local.PG_SPEC, 55434)
    assert not any(call[0] == 'rm' for call in calls)
    assert 'local' not in workspace._read_config()


def test_start_failure_does_not_persist_port(setup, monkeypatch):
    monkeypatch.setattr(local, 'container_state', lambda name: 'absent')
    def fail(*args, **kwargs):
        raise core.QuarryError('Docker startup failed')
    monkeypatch.setattr(local, 'start_container', fail)
    with pytest.raises(core.QuarryError):
        local.start_on_port(local.PG_SPEC, 55434)
    assert 'local' not in workspace._read_config()


def test_failure_hint_names_foreign_container(setup, monkeypatch):
    monkeypatch.setattr(local, 'engine_status', lambda spec: {'docker': True, 'running': False, 'port_conflict': True})
    monkeypatch.setattr(local, 'port_owner', lambda port: 'authkit-postgres-dev')
    conn = core.Connection(key='local', url=local.PG_SPEC.url('shop'), env='local')
    hint = local.connection_failure_hint(conn)
    assert 'authkit-postgres-dev' in hint and '--port' in hint
    assert local.connection_failure_hint(core.Connection(key='remote', url=conn.url, env='prod')) is None


def test_failure_hint_docker_off_is_distinct(setup, monkeypatch):
    monkeypatch.setattr(local, 'engine_status', lambda spec: {'docker': False, 'running': False})
    conn = core.Connection(key='local', url=local.PG_SPEC.url('shop'), env='local')
    assert 'start Docker' in local.connection_failure_hint(conn)
