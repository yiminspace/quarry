from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import signal
import subprocess
import sys
import tempfile
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
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _read_record(ws_home: "str | Path") -> dict[str, Any] | None:
    try:
        data = json.loads(_pid_file(ws_home).read_text(encoding="utf-8"))
        if (isinstance(data, dict) and type(data.get("pid")) is int
                and data["pid"] > 1 and isinstance(data.get("identity"), str)
                and data["identity"]):
            return data
    except (OSError, ValueError):
        pass
    # Bare legacy PIDs cannot establish identity; never signal them.
    return None


def _read_pid(ws_home: "str | Path") -> int | None:
    record = _read_record(ws_home)
    return record["pid"] if record else None


def _record_alive(record: dict[str, Any]) -> bool:
    return tunnel._process_identity(record["pid"]) == record["identity"]


def keeper_running(ws_home: "str | Path") -> tuple[bool, int | None]:
    record = _read_record(ws_home)
    return (_record_alive(record), record["pid"]) if record else (False, None)


def _acquire_lock(ws_home: "str | Path") -> int | None:
    path = _state_dir() / f"{_ws_key(ws_home)}.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    return fd


def _remove_record(ws_home: "str | Path", record: dict[str, Any]) -> None:
    if _read_record(ws_home) == record:
        _pid_file(ws_home).unlink(missing_ok=True)


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
    if not running:
        payload["state"] = "stopped"
        payload["tunnels"] = [
            {**{k: v for k, v in item.items() if k != "localPort"}, "state": "down"}
            for item in payload.get("tunnels", []) if isinstance(item, dict)
        ]
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
    fd = _acquire_lock(ws_home)
    if fd is None:
        return False, _read_pid(ws_home)
    try:
        running, pid = keeper_running(ws_home)
        if running:
            return False, pid
        workspace.set_tunnel_keep_alive(str(ws_home), True)
        if workspace.tunnel_reconnect_setting(ws_home) is None:
            workspace.set_tunnel_reconnect(str(ws_home), True)
        # Pass the already-held lock to the child: no spawn/publication gap.
        with _log_file(ws_home).open("a", encoding="utf-8") as logf:
            proc = subprocess.Popen(
                [sys.executable, "-m", "quarry.keepalive", "--workspace", str(ws_home),
                 "run", "--lock-fd", str(fd)],
                env={**os.environ, "QUARRY_TUNNEL_OWNER": "keeper"},
                stdin=subprocess.DEVNULL, stdout=logf, stderr=logf,
                start_new_session=True, pass_fds=(fd,),
            )
        _wait_started(ws_home, proc)
        return True, proc.pid
    finally:
        # Do not LOCK_UN: the child shares this open file description.
        os.close(fd)


def _wait_started(ws_home: Path, proc: subprocess.Popen) -> None:
    deadline = time.monotonic() + 5.0
    while True:
        if proc.poll() is not None:
            raise RuntimeError(f"keeper exited during startup; see {_log_file(ws_home)}")
        running, pid = keeper_running(ws_home)
        if running and pid == proc.pid:
            return
        if time.monotonic() >= deadline:
            raise RuntimeError(f"keeper did not publish its identity within 5 seconds; see {_log_file(ws_home)}")
        time.sleep(0.05)


def stop(ws_home: "str | Path") -> bool:
    ws_home = Path(ws_home).expanduser().resolve()
    record = _read_record(ws_home)
    if record is None:
        return False
    if _record_alive(record):
        try:
            os.kill(record["pid"], signal.SIGTERM)
        except ProcessLookupError:
            pass
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and _record_alive(record):
            time.sleep(0.1)
        if _record_alive(record):
            return False  # Still running: preserve its ownership record.
    # Serialize stale cleanup against a new starter publishing its record.
    fd = _acquire_lock(ws_home)
    if fd is not None:
        try:
            _remove_record(ws_home, record)
        finally:
            os.close(fd)
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


def _file_revision(path: Path) -> tuple:
    try:
        stat = path.stat()
        return (str(path), stat.st_ino, stat.st_mtime_ns, stat.st_ctime_ns,
                stat.st_mode, stat.st_size)
    except OSError as exc:
        return (str(path), type(exc).__name__, exc.errno)


def _retry_revision(conn) -> tuple:
    # Ignore unrelated connection metadata and keeper/reconnect toggles.
    key = getattr(conn, "ssh_key", None)
    ssh_home = Path.home() / ".ssh"
    files = [ssh_home / "config", ssh_home / "known_hosts"]
    if key:
        files.append(Path(key).expanduser())
    else:
        files.extend(ssh_home / name for name in ("id_rsa", "id_ecdsa", "id_ed25519"))
    return (tuple(getattr(conn, name, None) for name in
                  ("url", "engine", "ssh_host", "ssh_port", "ssh_user", "ssh_key", "source")),
            workspace.is_proxy_enabled(getattr(conn, "source", None) or workspace.WS.home),
            tuple(_file_revision(path) for path in files))


def _permanent_failure(exc: Exception) -> bool:
    if isinstance(exc, (ValueError, TypeError, FileNotFoundError, PermissionError)):
        return True
    if isinstance(exc, core.QuarryError) and exc.exit_code == core.EXIT_USAGE:
        return True
    message = str(exc).lower()
    return any(marker in message for marker in (
        "ssh key not found", "permission denied", "authentication failed",
        "host key verification failed", "remote host identification has changed",
        "bad configuration", "bad configuration option", "invalid format",
        "load key", "no such identity", "unprotected private key file",
    ))


def _run_loop(ws_home: "str | Path", lock_fd: int | None = None) -> int:
    ws_home = Path(ws_home).expanduser().resolve()
    fd = lock_fd if lock_fd is not None else _acquire_lock(ws_home)
    if fd is None:
        return 0
    if lock_fd is not None:
        try:
            expected = (_state_dir() / f"{_ws_key(ws_home)}.lock").stat()
            actual = os.fstat(fd)
            if (actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino):
                raise ValueError("inherited keeper lock belongs to a different file")
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            os.close(fd)
            raise
    record = None
    try:
        identity = tunnel._process_identity(os.getpid())
        if identity is None:
            raise RuntimeError("cannot establish keeper process identity")
        record = {"pid": os.getpid(), "identity": identity}
        _write_text(_pid_file(ws_home), json.dumps(record))
        return _serve(ws_home)
    finally:
        try:
            tunnel.close_all()
        finally:
            try:
                if record is not None:
                    _remove_record(ws_home, record)
            finally:
                os.close(fd)


def _serve(ws_home: Path) -> int:
    should_stop = False

    def _stop(*_args):
        nonlocal should_stop
        should_stop = True

    previous = {sig: signal.signal(sig, _stop) for sig in (signal.SIGTERM, signal.SIGINT)}
    backoff: dict[str, float] = {}
    next_try_at: dict[str, float] = {}
    states: dict[str, dict[str, Any]] = {}
    revisions: dict[str, tuple] = {}
    identities: dict[str, tuple] = {}
    attempted: dict[str, bool] = {}
    config_failure = None
    try:
        while not should_stop:
            config_path = Path(os.environ.get("QUARRY_CONNECTIONS_FILE") or ws_home / "connections.toml").expanduser()
            config_revision = _file_revision(config_path)
            if config_failure is not None and config_failure[0] == config_revision:
                time.sleep(_LOOP_SLEEP_SEC)
                continue
            try:
                workspace.configure_workspace(str(ws_home))
                conns = [c for c in core.load_connections().values() if getattr(c, "ssh_host", None)]
                config_failure = None
            except Exception as exc:
                config_failure = (config_revision, str(exc))
                write_status(ws_home, {"state": "blocked", "lastError": str(exc),
                                       "tunnels": list(states.values())})
                time.sleep(_LOOP_SLEEP_SEC)
                continue
            reconnect_enabled = workspace.is_tunnel_reconnect_enabled(ws_home)
            now = time.monotonic()
            seen: set[str] = set()
            old_identities = set(identities.values())
            for conn in conns:
                if should_stop:
                    break
                key = _hint_key(conn)
                seen.add(key)
                revision = _retry_revision(conn)
                changed = revisions.get(key) != revision
                if changed:
                    revisions[key] = revision
                    states.pop(key, None)
                    backoff.pop(key, None)
                    next_try_at.pop(key, None)
                    identities.pop(key, None)
                    attempted.pop(key, None)
                st = states.get(key, {"connection": conn.key, "env": conn.env,
                                      "state": "down", "lastError": None})
                if st["state"] == "blocked":
                    states[key] = st
                    continue
                try:
                    engine = core.connection_engine(conn)
                    identity = tunnel.tunnel_identity(conn, engine)
                    if key in identities and identities[key] != identity:
                        st = {"connection": conn.key, "env": conn.env,
                              "state": "down", "lastError": None}
                        backoff.pop(key, None)
                        next_try_at.pop(key, None)
                        attempted.pop(key, None)
                    identities[key] = identity
                    fact = tunnel.tunnel_fact_for(conn, engine)
                    if fact and fact.get("alive"):
                        attempted[key] = True
                        st.update(state="up", lastError=None, localPort=fact.get("local_port"))
                    else:
                        st.pop("localPort", None)
                        if not reconnect_enabled and attempted.get(key, False):
                            st["state"] = "down"
                        elif now >= next_try_at.get(key, 0.0):
                            attempted[key] = True
                            with tunnel.open_tunnel(conn, engine):
                                pass
                            fact = tunnel.tunnel_fact_for(conn, engine)
                            if not fact or not fact.get("alive"):
                                raise RuntimeError("tunnel did not become live")
                            st.update(state="up", lastError=None, localPort=fact.get("local_port"))
                            backoff[key] = 1.0
                            next_try_at[key] = 0.0
                        else:
                            st["state"] = "reconnecting"
                except Exception as exc:
                    attempted[key] = True
                    st.pop("localPort", None)
                    st["lastError"] = str(exc)
                    st["state"] = ("blocked" if _permanent_failure(exc) else
                                   "reconnecting" if reconnect_enabled else "down")
                    delay = backoff.get(key, 1.0)
                    next_try_at[key] = now + delay
                    backoff[key] = min(delay * 2.0, _BACKOFF_MAX_SEC)
                finally:
                    st["updatedAt"] = time.time()
                    states[key] = st
            if not should_stop:
                for mapping in (states, backoff, next_try_at, revisions, identities, attempted):
                    for stale in set(mapping) - seen:
                        del mapping[stale]
                for stale_identity in old_identities - set(identities.values()):
                    tunnel.close_tunnel_identity(stale_identity)
            write_status(ws_home, {"state": "running", "lastError": None,
                                   "tunnels": [states[k] for k in sorted(states)]})
            if not should_stop:
                time.sleep(_LOOP_SLEEP_SEC)
        return 0
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="quarry.keepalive")
    p.add_argument("--workspace", required=True)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run").add_argument("--lock-fd", type=int, help=argparse.SUPPRESS)
    return p


def main() -> int:
    args = _parser().parse_args()
    if args.cmd == "run":
        return _run_loop(args.workspace, args.lock_fd) if args.lock_fd is not None else _run_loop(args.workspace)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
