from __future__ import annotations

import contextlib
import signal
from pathlib import Path

import pytest

from quarry import keepalive


class _Conn:
    def __init__(self, key="shop", env="dev", ssh_host="bastion", source="/tmp/ws", url="postgresql://db/app"):
        self.key = key
        self.env = env
        self.ssh_host = ssh_host
        self.source = source
        self.url = url


@pytest.fixture()
def _ka_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("QUARRY_KEEPALIVE_DIR", str(tmp_path / "ka"))
    return tmp_path / "ka"


@pytest.mark.unit
def test_status_payload_and_write_status_roundtrip(_ka_dir, monkeypatch, tmp_path):
    ws = tmp_path / "ws"
    monkeypatch.setattr(keepalive.workspace, "is_tunnel_keep_alive_enabled", lambda _ws: True)
    monkeypatch.setattr(keepalive.workspace, "is_tunnel_reconnect_enabled", lambda _ws: False)
    monkeypatch.setattr(keepalive, "keeper_running", lambda _ws: (False, None))

    keepalive.write_status(ws, {"tunnels": [{"connection": "shop", "state": "up"}]})
    payload = keepalive.status(ws)
    assert payload["workspace"] == str(ws.resolve())
    assert payload["enabled"] is True
    assert payload["reconnect"] is False
    assert payload["keeper"]["running"] is False
    assert payload["tunnels"][0]["connection"] == "shop"

    # malformed status file should not crash and falls back to defaults
    keepalive._status_file(ws).write_text("{bad json", encoding="utf-8")
    fallback = keepalive.status(ws)
    assert fallback["tunnels"] == []


@pytest.mark.unit
def test_start_stop_and_hint_logic(_ka_dir, monkeypatch, tmp_path):
    ws = tmp_path / "ws"
    calls = {"keep_alive": [], "reconnect": []}
    monkeypatch.setattr(keepalive.workspace, "set_tunnel_keep_alive", lambda home, enabled: calls["keep_alive"].append((home, enabled)))
    monkeypatch.setattr(keepalive.workspace, "is_tunnel_reconnect_enabled", lambda _ws: False)
    monkeypatch.setattr(keepalive.workspace, "set_tunnel_reconnect", lambda home, enabled: calls["reconnect"].append((home, enabled)))
    monkeypatch.setattr(keepalive.workspace, "is_tunnel_keep_alive_enabled", lambda _ws: True)

    class _P:
        pid = 12345

    monkeypatch.setattr(keepalive, "keeper_running", lambda _ws: (False, None))
    monkeypatch.setattr(keepalive.subprocess, "Popen", lambda *a, **k: _P())
    started, pid = keepalive.start(ws)
    assert started is True and pid == 12345
    assert calls["keep_alive"]
    assert calls["reconnect"]

    # already running branch
    monkeypatch.setattr(keepalive, "keeper_running", lambda _ws: (True, 23456))
    started2, pid2 = keepalive.start(ws)
    assert started2 is False and pid2 == 23456

    # stop: no pid file then with pid file
    assert keepalive.stop(ws) is True  # pid file exists from start
    assert keepalive.stop(ws) is False

    # hint logic
    monkeypatch.setattr(keepalive, "keeper_running", lambda _ws: (False, None))
    c = _Conn(source=str(ws))
    assert keepalive.should_hint_keeper_down(c) is True
    c2 = _Conn(ssh_host=None, source=str(ws))
    assert keepalive.should_hint_keeper_down(c2) is False


@pytest.mark.unit
def test_run_loop_tracks_up_state_and_exits_cleanly(_ka_dir, monkeypatch, tmp_path):
    ws = tmp_path / "ws"
    handlers = {}
    writes = []
    conn = _Conn(source=str(ws))
    monkeypatch.setattr(keepalive.signal, "signal", lambda sig, fn: handlers.__setitem__(sig, fn))
    monkeypatch.setattr(keepalive.workspace, "configure_workspace", lambda _ws: None)
    monkeypatch.setattr(keepalive.workspace, "is_tunnel_reconnect_enabled", lambda _ws: True)
    monkeypatch.setattr(keepalive.core, "load_connections", lambda: {"shop_dev": conn})
    monkeypatch.setattr(keepalive.core, "connection_engine", lambda _c: "postgres")
    monkeypatch.setattr(keepalive.tunnel, "tunnel_fact_for", lambda _c, _e: {"alive": True, "local_port": 55123})

    @contextlib.contextmanager
    def _open(*_a, **_k):
        yield

    monkeypatch.setattr(keepalive.tunnel, "open_tunnel", _open)
    monkeypatch.setattr(keepalive.tunnel, "close_all", lambda: writes.append(("closed", True)))
    monkeypatch.setattr(keepalive.time, "sleep", lambda _s: None)

    def _write(_ws, payload):
        writes.append(payload)
        handlers[signal.SIGTERM]()

    monkeypatch.setattr(keepalive, "write_status", _write)
    rc = keepalive._run_loop(ws)
    assert rc == 0
    assert any(isinstance(x, dict) and x["tunnels"][0]["state"] == "up" for x in writes if isinstance(x, dict))
    assert ("closed", True) in writes


@pytest.mark.unit
def test_run_loop_sets_reconnecting_on_open_error(_ka_dir, monkeypatch, tmp_path):
    ws = tmp_path / "ws"
    handlers = {}
    writes = []
    conn = _Conn(source=str(ws))
    monkeypatch.setattr(keepalive.signal, "signal", lambda sig, fn: handlers.__setitem__(sig, fn))
    monkeypatch.setattr(keepalive.workspace, "configure_workspace", lambda _ws: None)
    monkeypatch.setattr(keepalive.workspace, "is_tunnel_reconnect_enabled", lambda _ws: True)
    monkeypatch.setattr(keepalive.core, "load_connections", lambda: {"shop_dev": conn})
    monkeypatch.setattr(keepalive.core, "connection_engine", lambda _c: "postgres")
    monkeypatch.setattr(keepalive.tunnel, "tunnel_fact_for", lambda _c, _e: None)
    monkeypatch.setattr(keepalive.tunnel, "close_all", lambda: None)
    monkeypatch.setattr(keepalive.time, "sleep", lambda _s: None)

    @contextlib.contextmanager
    def _open(*_a, **_k):
        raise RuntimeError("boom")
        yield

    monkeypatch.setattr(keepalive.tunnel, "open_tunnel", _open)

    def _write(_ws, payload):
        writes.append(payload)
        handlers[signal.SIGTERM]()

    monkeypatch.setattr(keepalive, "write_status", _write)
    rc = keepalive._run_loop(ws)
    assert rc == 0
    item = writes[0]["tunnels"][0]
    assert item["state"] == "reconnecting"
    assert "boom" in item["lastError"]


@pytest.mark.unit
def test_parser_and_main_dispatch(monkeypatch, tmp_path):
    ws = tmp_path / "ws"
    monkeypatch.setattr(keepalive.sys, "argv", ["quarry.keepalive", "--workspace", str(ws), "run"])
    monkeypatch.setattr(keepalive, "_run_loop", lambda home: 7 if str(home) == str(ws) else 1)
    assert keepalive.main() == 7

    class _Args:
        cmd = "other"
        workspace = str(ws)

    monkeypatch.setattr(keepalive, "_parser", lambda: type("P", (), {"parse_args": lambda self: _Args()})())
    assert keepalive.main() == 1

