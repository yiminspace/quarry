from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from . import core, tunnel, workspace

_LOOP_SLEEP_SEC = 1.0
_BACKOFF_MAX_SEC = 30.0


def _state_dir() -> Path:
    override = os.environ.get("QUARRY_KEEPALIVE_DIR")
    return Path(override).expanduser() if override else Path.home() / ".cache" / "quarry" / "keepalive"


def _ws_key(ws_home: "str | Path") -> str:
    raw = str(Path(ws_home).expanduser().resolve())
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _pid_file(ws_home: "str | Path") -> Path:
    return _state_dir() / f"{_ws_key(ws_home)}.pid"


def _status_file(ws_home: "str | Path") -> Path:
    return _state_dir() / f"{_ws_key(ws_home)}.json"


def _log_file(ws_home: "str | Path") -> Path:
    return _state_dir() / f"{_ws_key(ws_home)}.log"


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _read_pid(ws_home: "str | Path") -> int | None:
    p = _pid_file(ws_home)
    try:
        return int(p.read_text(encoding="utf-8").strip())
    except Exception:
        return None


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def keeper_running(ws_home: "str | Path") -> tuple[bool, int | None]:
    pid = _read_pid(ws_home)
    if pid is None:
        return False, None
    return _pid_alive(pid), pid


def _status_payload(ws_home: "str | Path") -> dict[str, Any]:
    running, pid = keeper_running(ws_home)
    payload = {
        "workspace": str(Path(ws_home).expanduser().resolve()),
        "enabled": workspace.is_tunnel_keep_alive_enabled(ws_home),
        "reconnect": workspace.is_tunnel_reconnect_enabled(ws_home),
        "keeper": {"running": running, "pid": pid},
        "tunnels": [],
        "updatedAt": time.time(),
    }
    p = _status_file(ws_home)
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                payload.update(data)
                payload["workspace"] = str(Path(ws_home).expanduser().resolve())
                payload["enabled"] = workspace.is_tunnel_keep_alive_enabled(ws_home)
                payload["reconnect"] = workspace.is_tunnel_reconnect_enabled(ws_home)
                payload["keeper"] = {"running": running, "pid": pid}
        except Exception:
            pass
    return payload


def status(ws_home: "str | Path") -> dict[str, Any]:
    return _status_payload(ws_home)


def write_status(ws_home: "str | Path", payload: dict[str, Any]) -> None:
    payload = {**payload}
    payload["updatedAt"] = time.time()
    payload["workspace"] = str(Path(ws_home).expanduser().resolve())
    payload["enabled"] = workspace.is_tunnel_keep_alive_enabled(ws_home)
    payload["reconnect"] = workspace.is_tunnel_reconnect_enabled(ws_home)
    running, pid = keeper_running(ws_home)
    payload["keeper"] = {"running": running, "pid": pid}
    _write_text(_status_file(ws_home), json.dumps(payload, ensure_ascii=False))


def start(ws_home: "str | Path") -> tuple[bool, int | None]:
    ws_home = Path(ws_home).expanduser().resolve()
    running, pid = keeper_running(ws_home)
    if running:
        return False, pid
    workspace.set_tunnel_keep_alive(str(ws_home), True)
    if not workspace.is_tunnel_reconnect_enabled(ws_home):
        workspace.set_tunnel_reconnect(str(ws_home), True)
    _state_dir().mkdir(parents=True, exist_ok=True)
    logf = _log_file(ws_home).open("a", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-m", "quarry.keepalive", "--workspace", str(ws_home), "run"],
        env={**os.environ, "QUARRY_TUNNEL_OWNER": "keeper"},
        stdin=subprocess.DEVNULL,
        stdout=logf,
        stderr=logf,
        start_new_session=True,
    )
    _write_text(_pid_file(ws_home), str(proc.pid))
    return True, proc.pid


def stop(ws_home: "str | Path") -> bool:
    ws_home = Path(ws_home).expanduser().resolve()
    pid = _read_pid(ws_home)
    if pid is None:
        return False
    if _pid_alive(pid):
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and _pid_alive(pid):
            time.sleep(0.1)
    try:
        _pid_file(ws_home).unlink()
    except OSError:
        pass
    return True


def _hint_key(conn: core.Connection) -> str:
    return f"{conn.key}@{conn.env or ''}"


def should_hint_keeper_down(conn: core.Connection) -> bool:
    ws_home = getattr(conn, "source", None) or workspace.WS.home
    if not getattr(conn, "ssh_host", None):
        return False
    if not workspace.is_tunnel_keep_alive_enabled(ws_home):
        return False
    running, _ = keeper_running(ws_home)
    return not running


def _run_loop(ws_home: "str | Path") -> int:
    ws_home = Path(ws_home).expanduser().resolve()
    workspace.configure_workspace(str(ws_home))
    should_stop = False

    def _stop(*_args):
        nonlocal should_stop
        should_stop = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    _write_text(_pid_file(ws_home), str(os.getpid()))
    backoff: dict[str, float] = {}
    next_try_at: dict[str, float] = {}
    states: dict[str, dict[str, Any]] = {}
    while not should_stop:
        workspace.configure_workspace(str(ws_home))
        reconnect_enabled = workspace.is_tunnel_reconnect_enabled(ws_home)
        conns = [c for c in core.load_connections().values() if getattr(c, "ssh_host", None)]
        now = time.monotonic()
        seen: set[str] = set()
        for conn in conns:
            key = _hint_key(conn)
            seen.add(key)
            st = states.get(key, {"connection": conn.key, "env": conn.env, "state": "down", "lastError": None})
            due = next_try_at.get(key, 0.0)
            if st.get("state") == "up":
                fact = tunnel.tunnel_fact_for(conn, core.connection_engine(conn))
                if fact and fact.get("alive"):
                    st["localPort"] = fact.get("local_port")
                    st["updatedAt"] = time.time()
                    states[key] = st
                    continue
                st["state"] = "reconnecting" if reconnect_enabled else "down"
            if now < due:
                states[key] = st
                continue
            try:
                with tunnel.open_tunnel(conn, core.connection_engine(conn)):
                    pass
                fact = tunnel.tunnel_fact_for(conn, core.connection_engine(conn))
                st["state"] = "up"
                st["lastError"] = None
                st["localPort"] = fact.get("local_port") if fact else None
                backoff[key] = 1.0
                next_try_at[key] = 0.0
            except Exception as exc:  # noqa: BLE001
                st["lastError"] = str(exc)
                st["state"] = "reconnecting" if reconnect_enabled else "down"
                delay = backoff.get(key, 1.0)
                next_try_at[key] = now + delay
                backoff[key] = min(delay * 2.0, _BACKOFF_MAX_SEC)
            st["updatedAt"] = time.time()
            states[key] = st
        for stale in [k for k in states if k not in seen]:
            states.pop(stale, None)
            backoff.pop(stale, None)
            next_try_at.pop(stale, None)
        write_status(ws_home, {"tunnels": [states[k] for k in sorted(states)]})
        time.sleep(_LOOP_SLEEP_SEC)
    tunnel.close_all()
    write_status(ws_home, {"tunnels": [states[k] for k in sorted(states)]})
    try:
        _pid_file(ws_home).unlink()
    except OSError:
        pass
    return 0


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="quarry.keepalive")
    p.add_argument("--workspace", required=True)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run")
    return p


def main() -> int:
    args = _parser().parse_args()
    if args.cmd == "run":
        return _run_loop(args.workspace)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
