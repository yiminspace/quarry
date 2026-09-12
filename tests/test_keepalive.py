from __future__ import annotations

import contextlib
import copy
import json
import os
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
    monkeypatch.setattr(keepalive.tunnel, "_process_identity", lambda pid: f"start:{pid}")
    monkeypatch.setattr(keepalive.tunnel, "tunnel_identity", lambda conn, engine: (conn.ssh_host, conn.url))
    monkeypatch.setattr(keepalive.tunnel, "close_tunnel_identity", lambda identity: None)
    monkeypatch.setattr(keepalive.workspace, "is_proxy_enabled", lambda ws: False)
    monkeypatch.setattr(keepalive.workspace, "tunnel_reconnect_setting", lambda ws: False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
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
    monkeypatch.setattr(keepalive, "_wait_started", lambda *a: None)
    started, pid = keepalive.start(ws)
    assert started is True and pid == 12345
    assert calls["keep_alive"]
    assert calls["reconnect"] == []

    # already running branch
    monkeypatch.setattr(keepalive, "keeper_running", lambda _ws: (True, 23456))
    started2, pid2 = keepalive.start(ws)
    assert started2 is False and pid2 == 23456

    # stop: no pid file then with pid file
    # Child publishes its own identity; parent never writes a bare PID.
    assert not keepalive._pid_file(ws).exists()
    keepalive._write_text(keepalive._pid_file(ws), json.dumps({"pid": 12345, "identity": "old"}))
    monkeypatch.setattr(keepalive.os, "kill", lambda *args: pytest.fail("must not signal a reused PID"))
    assert keepalive.stop(ws) is True
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



@pytest.mark.unit
@pytest.mark.parametrize("contents", ["12345", "0", "-1", '{}', '{"pid": true, "identity": "x"}'])
def test_unverified_pid_never_signalled(_ka_dir, monkeypatch, tmp_path, contents):
    ws = tmp_path / "ws"
    keepalive._write_text(keepalive._pid_file(ws), contents)
    monkeypatch.setattr(keepalive.os, "kill", lambda *args: pytest.fail("unverified signal"))
    assert keepalive.keeper_running(ws) == (False, None)
    assert keepalive.stop(ws) is False


@pytest.mark.unit
def test_stop_checks_identity_and_preserves_successor(_ka_dir, monkeypatch, tmp_path):
    ws = tmp_path / "ws"
    record = {"pid": 12345, "identity": "original"}
    successor = {"pid": 23456, "identity": "successor"}
    keepalive._write_text(keepalive._pid_file(ws), json.dumps(record))
    monkeypatch.setattr(keepalive.tunnel, "_process_identity", lambda pid: "original")
    signals = []

    def kill(pid, sig):
        signals.append((pid, sig))
        keepalive._write_text(keepalive._pid_file(ws), json.dumps(successor))
        monkeypatch.setattr(keepalive.tunnel, "_process_identity", lambda pid: "reused")

    monkeypatch.setattr(keepalive.os, "kill", kill)
    assert keepalive.stop(ws)
    assert signals == [(12345, signal.SIGTERM)]
    assert keepalive._read_record(ws) == successor


@pytest.mark.unit
def test_stop_timeout_preserves_record(_ka_dir, monkeypatch, tmp_path):
    ws = tmp_path / "ws"
    record = {"pid": 12345, "identity": "start:12345"}
    keepalive._write_text(keepalive._pid_file(ws), json.dumps(record))
    monkeypatch.setattr(keepalive.os, "kill", lambda *args: None)
    clock = iter([0, 6])
    monkeypatch.setattr(keepalive.time, "monotonic", lambda: next(clock))
    assert keepalive.stop(ws) is False
    assert keepalive._read_record(ws) == record


@pytest.mark.unit
def test_lock_excludes_start_and_direct_run(_ka_dir, monkeypatch, tmp_path):
    ws = tmp_path / "ws"
    fd = keepalive._acquire_lock(ws)
    assert fd is not None
    monkeypatch.setattr(keepalive.subprocess, "Popen", lambda *a, **k: pytest.fail("duplicate spawn"))
    monkeypatch.setattr(keepalive.tunnel, "close_all", lambda: pytest.fail("nonowner cleanup"))
    try:
        assert keepalive.start(ws) == (False, None)
        assert keepalive._run_loop(ws) == 0
    finally:
        os.close(fd)
    new_fd = keepalive._acquire_lock(ws)
    assert new_fd is not None
    os.close(new_fd)


@pytest.mark.unit
def test_spawn_transfers_lock_and_closes_log(_ka_dir, monkeypatch, tmp_path):
    ws = tmp_path / "ws"
    monkeypatch.setattr(keepalive.workspace, "set_tunnel_keep_alive", lambda *a: None)
    inherited = []
    logs = []

    def spawn(args, **kwargs):
        fd, = kwargs["pass_fds"]
        assert keepalive._acquire_lock(ws) is None
        assert args[-2:] == ["--lock-fd", str(fd)]
        inherited.append(os.dup(fd))
        logs.append(kwargs["stdout"])
        return type("P", (), {"pid": 12345})()

    monkeypatch.setattr(keepalive.subprocess, "Popen", spawn)
    monkeypatch.setattr(keepalive, "_wait_started", lambda *a: None)
    assert keepalive.start(ws) == (True, 12345)
    try:
        assert logs[0].closed
        assert keepalive._acquire_lock(ws) is None
    finally:
        os.close(inherited[0])
    fd = keepalive._acquire_lock(ws)
    assert fd is not None
    os.close(fd)


@pytest.mark.unit
def test_loop_exception_releases_ownership(_ka_dir, monkeypatch, tmp_path):
    ws = tmp_path / "ws"
    closed = []
    monkeypatch.setattr(keepalive, "_serve", lambda ws: (_ for _ in ()).throw(RuntimeError("status failed")))
    monkeypatch.setattr(keepalive.tunnel, "close_all", lambda: closed.append(True))
    with pytest.raises(RuntimeError, match="status failed"):
        keepalive._run_loop(ws)
    assert closed == [True]
    assert not keepalive._pid_file(ws).exists()
    fd = keepalive._acquire_lock(ws)
    assert fd is not None
    os.close(fd)


@pytest.fixture
def loop_driver(_ka_dir, monkeypatch, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    handlers = {}
    snapshots = []
    opened = []
    retired = []
    state = {"conns": [_Conn(source=str(ws))], "reconnect": True, "tick": 0,
             "alive": False, "error": None, "load_error": None, "loads": 0}
    monkeypatch.setattr(keepalive.signal, "signal", lambda sig, fn: handlers.__setitem__(sig, fn))
    monkeypatch.setattr(keepalive.workspace, "configure_workspace", lambda ws: None)
    monkeypatch.setattr(keepalive.workspace, "is_tunnel_reconnect_enabled", lambda ws: state["reconnect"])
    monkeypatch.setattr(keepalive.core, "connection_engine", lambda c: "postgres")
    monkeypatch.setattr(keepalive.time, "monotonic", lambda: float(state["tick"] * 100))
    monkeypatch.setattr(keepalive.tunnel, "close_all", lambda: None)
    monkeypatch.setattr(keepalive.tunnel, "close_tunnel_identity", retired.append)

    def load():
        state["loads"] += 1
        if state["load_error"]:
            raise state["load_error"]
        return {c.key: c for c in state["conns"]}

    monkeypatch.setattr(keepalive.core, "load_connections", load)
    monkeypatch.setattr(keepalive.tunnel, "tunnel_fact_for", lambda c, e:
                        {"alive": True, "local_port": 55123} if state["alive"] else None)

    @contextlib.contextmanager
    def open_tunnel(c, e):
        opened.append((state["tick"], c.key))
        if state["error"]:
            raise state["error"]
        state["alive"] = True
        yield

    monkeypatch.setattr(keepalive.tunnel, "open_tunnel", open_tunnel)
    monkeypatch.setattr(keepalive, "write_status", lambda ws, p: snapshots.append(copy.deepcopy(p)))

    def run(ticks, step=lambda tick: None):
        def sleep(seconds):
            state["tick"] += 1
            step(state["tick"])
            if state["tick"] >= ticks:
                handlers[signal.SIGTERM]()
        monkeypatch.setattr(keepalive.time, "sleep", sleep)
        assert keepalive._run_loop(ws) == 0
        return snapshots

    return state, opened, retired, run, ws


@pytest.mark.unit
def test_reconnect_false_never_opens_but_observes_existing(loop_driver):
    state, opened, _, run, _ = loop_driver
    state.update(reconnect=False, alive=True)

    def step(tick):
        state["alive"] = False

    snapshots = run(3, step)
    assert [p["tunnels"][0]["state"] for p in snapshots] == ["up", "down", "down"]
    assert "localPort" not in snapshots[-1]["tunnels"][0]
    assert opened == []


@pytest.mark.unit
@pytest.mark.parametrize("error", [
    RuntimeError("ssh key not found: /missing"),
    RuntimeError("Permission denied (publickey)."),
    RuntimeError("Host key verification failed."),
    ValueError("invalid port"),
    keepalive.core.QuarryError("bad connection config"),
])
def test_permanent_failures_wait_for_relevant_changes(loop_driver, error):
    state, opened, _, run, _ = loop_driver
    state["error"] = error

    def step(tick):
        if tick == 1:
            state["conns"][0].notes = "unrelated edit"
        if tick == 2:
            state["reconnect"] = False
        if tick == 3:
            state["reconnect"] = True
        if tick == 4:
            state["conns"][0].ssh_key = "/new/key"
            state["error"] = None

    snapshots = run(6, step)
    assert opened == [(0, "shop"), (4, "shop")]
    assert all(p["tunnels"][0]["state"] == "blocked" for p in snapshots[:4])
    assert snapshots[-1]["tunnels"][0]["state"] == "up"


@pytest.mark.unit
def test_transient_error_retries_and_toggle_respected(loop_driver):
    state, opened, _, run, _ = loop_driver
    state["error"] = RuntimeError("connection timed out")

    def step(tick):
        if tick == 2:
            state["reconnect"] = False
        if tick == 3:
            state.update(reconnect=True, error=None)

    snapshots = run(5, step)
    assert opened == [(0, "shop"), (1, "shop"), (3, "shop")]
    assert snapshots[2]["tunnels"][0]["state"] == "down"
    assert snapshots[-1]["tunnels"][0]["state"] == "up"


@pytest.mark.unit
def test_config_parse_failure_stays_alive_until_file_changes(loop_driver):
    state, opened, _, run, ws = loop_driver
    state["load_error"] = ValueError("invalid TOML")

    def step(tick):
        if tick == 3:
            (ws / "connections.toml").write_text("# repaired")
            state["load_error"] = None

    snapshots = run(4, step)
    assert state["loads"] == 2
    assert snapshots[0]["state"] == "blocked"
    assert snapshots[-1]["state"] == "running"
    assert opened == [(3, "shop")]


@pytest.mark.unit
def test_removed_shared_identity_retired_only_after_last_connection(loop_driver):
    state, _, retired, run, _ = loop_driver
    state["conns"].append(_Conn(key="other"))
    identity = ("bastion", "postgresql://db/app")

    def step(tick):
        if tick == 1:
            state["conns"].pop()
        if tick == 2:
            assert retired == []
            state["conns"].clear()

    snapshots = run(3, step)
    assert retired == [identity]
    assert snapshots[-1]["tunnels"] == []


@pytest.mark.unit
def test_reconfigured_identity_retired(loop_driver):
    state, _, retired, run, _ = loop_driver

    def step(tick):
        if tick == 1:
            state["conns"][0].ssh_host = "new-bastion"

    run(2, step)
    assert retired == [("bastion", "postgresql://db/app")]


@pytest.mark.unit
def test_retry_revision_detects_key_creation_replacement_and_permissions(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(keepalive.workspace, "is_proxy_enabled", lambda ws: False)
    conn = _Conn(source=str(tmp_path))
    conn.ssh_key = str(tmp_path / "key")
    missing = keepalive._retry_revision(conn)
    Path(conn.ssh_key).write_text("first")
    created = keepalive._retry_revision(conn)
    assert missing != created
    Path(conn.ssh_key).write_text("other")
    replaced = keepalive._retry_revision(conn)
    assert created != replaced
    Path(conn.ssh_key).chmod(0o600)
    assert keepalive._retry_revision(conn) != replaced
    stable = keepalive._retry_revision(conn)
    conn.notes = "not a retry trigger"
    assert keepalive._retry_revision(conn) == stable


@pytest.mark.unit
@pytest.mark.parametrize("error", [None, RuntimeError("connection refused")])
def test_reconnect_false_initial_attempt_once_per_revision(loop_driver, error):
    state, opened, _, run, _ = loop_driver
    state.update(reconnect=False, error=error)

    def step(tick):
        state["alive"] = False  # initial success drops before the next tick
        if tick == 3:
            state["conns"][0].ssh_key = "/changed/key"

    snapshots = run(5, step)
    assert opened == [(0, "shop"), (3, "shop")]
    assert snapshots[1]["tunnels"][0]["state"] == "down"
    assert snapshots[-1]["tunnels"][0]["state"] == "down"


@pytest.mark.unit
def test_stopped_keeper_never_reports_stale_up(_ka_dir, monkeypatch, tmp_path):
    ws = tmp_path / "ws"
    monkeypatch.setattr(keepalive, "keeper_running", lambda ws: (False, 12345))
    keepalive._write_text(keepalive._status_file(ws), json.dumps({
        "state": "running", "tunnels": [{"connection": "shop", "state": "up", "localPort": 54321}]}))
    result = keepalive.status(ws)
    assert result["state"] == "stopped"
    assert result["tunnels"] == [{"connection": "shop", "state": "down"}]


@pytest.mark.unit
def test_atomic_private_write_preserves_old_file_on_failure(_ka_dir, monkeypatch, tmp_path):
    path = tmp_path / "record.json"
    keepalive._write_text(path, "old")
    assert path.stat().st_mode & 0o777 == 0o600

    def replace(source, target):
        assert Path(source).read_text() == "new"
        assert path.read_text() == "old"
        assert Path(source).stat().st_mode & 0o777 == 0o600
        raise OSError("replace failed")

    monkeypatch.setattr(keepalive.os, "replace", replace)
    with pytest.raises(OSError, match="replace failed"):
        keepalive._write_text(path, "new")
    assert path.read_text() == "old"
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.unit
def test_startup_handshake_waits_for_verified_child(_ka_dir, monkeypatch, tmp_path):
    ws = tmp_path / "ws"
    proc = type("P", (), {"pid": 12345, "poll": lambda self: None})()
    snapshots = iter([(False, None), (True, 54321), (True, 12345)])
    sleeps = []
    monkeypatch.setattr(keepalive, "keeper_running", lambda ws: next(snapshots))
    monkeypatch.setattr(keepalive.time, "sleep", sleeps.append)
    keepalive._wait_started(ws, proc)
    assert sleeps == [0.05, 0.05]


@pytest.mark.unit
@pytest.mark.parametrize("exited", [False, True])
def test_startup_handshake_reports_failure_without_signal(_ka_dir, monkeypatch, tmp_path, exited):
    proc = type("P", (), {"pid": 12345, "poll": lambda self: 1 if exited else None})()
    monkeypatch.setattr(keepalive.os, "kill", lambda *a: pytest.fail("unverified signal"))
    monkeypatch.setattr(keepalive, "keeper_running", lambda ws: (False, None))
    clock = iter([0, 6])
    monkeypatch.setattr(keepalive.time, "monotonic", lambda: next(clock))
    with pytest.raises(RuntimeError, match="startup|within 5 seconds"):
        keepalive._wait_started(tmp_path / "ws", proc)


@pytest.mark.unit
def test_missing_key_creation_unblocks_actual_revision(loop_driver):
    state, opened, _, run, ws = loop_driver
    key = ws / "key"
    state["conns"][0].ssh_key = str(key)
    state["error"] = RuntimeError("ssh key not found")

    def step(tick):
        if tick == 3:
            key.write_text("key installed")
            state["error"] = None

    run(5, step)
    assert opened == [(0, "shop"), (3, "shop")]


@pytest.mark.unit
def test_transient_retry_backoff_is_bounded(loop_driver, monkeypatch):
    state, opened, _, run, _ = loop_driver
    state["error"] = RuntimeError("timeout")
    monkeypatch.setattr(keepalive.time, "monotonic", lambda: float(state["tick"]))
    run(65)
    assert [tick for tick, _ in opened] == [0, 1, 3, 7, 15, 31, 61]


@pytest.mark.unit
def test_spawn_failure_releases_lock(_ka_dir, monkeypatch, tmp_path):
    ws = tmp_path / "ws"
    monkeypatch.setattr(keepalive.workspace, "set_tunnel_keep_alive", lambda *a: None)
    monkeypatch.setattr(keepalive.subprocess, "Popen", lambda *a, **k: (_ for _ in ()).throw(OSError("spawn failed")))
    with pytest.raises(OSError, match="spawn failed"):
        keepalive.start(ws)
    fd = keepalive._acquire_lock(ws)
    assert fd is not None
    os.close(fd)


@pytest.mark.unit
def test_run_rejects_unrelated_inherited_fd(_ka_dir, monkeypatch, tmp_path):
    ws = tmp_path / "ws"
    fd = keepalive._acquire_lock(ws)
    os.close(fd)
    unrelated = os.open(tmp_path / "unrelated", os.O_CREAT | os.O_RDWR, 0o600)
    monkeypatch.setattr(keepalive.tunnel, "close_all", lambda: pytest.fail("nonowner cleanup"))
    with pytest.raises(ValueError, match="different file"):
        keepalive._run_loop(ws, unrelated)
    assert not keepalive._pid_file(ws).exists()
