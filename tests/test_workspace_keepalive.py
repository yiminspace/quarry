"""Scoped keeper policy, using isolated configuration files only."""
import json

import pytest

from quarry import keepalive, workspace


@pytest.fixture
def config(monkeypatch, tmp_path):
    path = tmp_path / "config.toml"
    monkeypatch.setenv("QUARRY_CONFIG", str(path))
    monkeypatch.setenv("QUARRY_KEEPALIVE_DIR", str(tmp_path / "keeper"))
    return path


@pytest.mark.unit
@pytest.mark.parametrize("flag", ["keep_alive", "reconnect"])
def test_scoped_true_and_false_do_not_leak(config, tmp_path, flag):
    setter = getattr(workspace, f"set_tunnel_{flag}")
    getter = getattr(workspace, f"is_tunnel_{flag}_enabled")
    first, second = tmp_path / "first", tmp_path / "second"
    setter(str(first), True)
    assert getter(first)
    assert not getter(second)
    setter(str(second), True)
    setter(str(first), False)
    assert not getter(first)
    assert getter(second)
    assert "[tunnel]" not in config.read_text()


@pytest.mark.unit
@pytest.mark.parametrize("flag", ["keep_alive", "reconnect"])
@pytest.mark.parametrize("legacy", [True, False])
def test_explicit_override_wins_legacy_only_for_its_workspace(config, tmp_path, flag, legacy):
    setter = getattr(workspace, f"set_tunnel_{flag}")
    getter = getattr(workspace, f"is_tunnel_{flag}_enabled")
    config.write_text(f"# preserve\n[tunnel]\n{flag} = {str(legacy).lower()}\n[other]\nvalue = 42\n")
    first, second = tmp_path / "first", tmp_path / "second"
    setter(str(first), not legacy)
    assert getter(first) is not legacy
    assert getter(second) is legacy
    assert workspace._read_config()["tunnel"][flag] is legacy
    assert workspace._read_config()["other"] == {"value": 42}
    assert "# preserve" in config.read_text()
    setter(str(first / ".." / "first"), legacy)
    assert getter(first) is legacy
    disabled = workspace._read_config().get(f"tunnel_{flag}_disabled_workspaces", [])
    enabled = workspace._read_config().get(f"tunnel_{flag}_workspaces", [])
    assert not (disabled and enabled)


@pytest.mark.unit
def test_disabled_override_wins_conflicting_lists(config, tmp_path):
    ws = str(tmp_path / "ws")
    config.write_text(f"tunnel_reconnect_workspaces = [{json.dumps(ws)}]\n"
                      f"tunnel_reconnect_disabled_workspaces = [{json.dumps(ws)}]\n")
    assert workspace.tunnel_reconnect_setting(ws) is False


@pytest.mark.unit
@pytest.mark.parametrize("setting", [None, False, True, "scoped_false"])
def test_start_defaults_only_absent_reconnect(config, monkeypatch, tmp_path, setting):
    ws = tmp_path / "ws"
    if setting == "scoped_false":
        workspace.set_tunnel_reconnect(str(ws), False)
    elif setting is not None:
        config.write_text(f"[tunnel]\nreconnect = {str(setting).lower()}\n")
    monkeypatch.setattr(keepalive.subprocess, "Popen", lambda *a, **k: type("P", (), {"pid": 12345})())
    monkeypatch.setattr(keepalive, "_wait_started", lambda *a: None)
    assert keepalive.start(ws) == (True, 12345)
    assert workspace.is_tunnel_keep_alive_enabled(ws)
    assert workspace.is_tunnel_reconnect_enabled(ws) is (setting is None or setting is True)
    # Starting a workspace never turns on keep-alive for another workspace.
    assert not workspace.is_tunnel_keep_alive_enabled(tmp_path / "other")
    assert workspace.tunnel_reconnect_setting(tmp_path / "other") is (
        None if setting in (None, "scoped_false") else setting)
