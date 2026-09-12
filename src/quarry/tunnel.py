"""SSH tunnel support — reach a DB that's only bound to the bastion's localhost.

Zero-dependency: shells out to the system `ssh` binary (`ssh -L`). A connection
grows optional fields:  ssh_host, ssh_user, ssh_key, ssh_port.

Tunnels are POOLED (keyed by ssh target + db host:port) and reused across calls,
so a long-running GUI opens each tunnel once instead of per query. Pooled
tunnels are torn down at process exit. Failures are fast: a missing key file
errors immediately, and if `ssh` exits early we surface its stderr rather than
waiting out the full timeout.

Each process's pool lives only in that process's memory, so a live tunnel is
also mirrored to a small on-disk registry (~/.cache/quarry/tunnels.json) —
this is what lets a separately-invoked `qy proxy` (or the GUI's per-connection
proxy badge) see tunnels a different, long-running `qy gui`/MCP process is
holding open, as an observed fact rather than an empty list (issue #101).
"""

from __future__ import annotations

import atexit
from collections import deque
import contextlib
import fcntl
import json
import os
import shlex
import socket
import subprocess
import sys
import threading
import tempfile
import time
from pathlib import Path
from urllib.parse import quote, urlencode, urlparse, urlunparse

from . import proxy as proxy_mod
from . import workspace

DEFAULT_DB_PORT = {"postgres": 5432, "mysql": 3306, "redis": 6379, "neptune": 8182}

_POOL: dict[tuple, "_Tunnel"] = {}
_LOCK = threading.RLock()
_RETIRED: list["_Tunnel"] = []
_SETUP_LOCKS: dict[tuple, threading.Lock] = {}
_POOL_GENERATION = 0


def _default_registry_file() -> Path:
    # QUARRY_TUNNEL_REGISTRY_FILE lets a test point at an isolated file —
    # same override pattern as cache.py's QUARRY_CACHE_FILE.
    override = os.environ.get("QUARRY_TUNNEL_REGISTRY_FILE")
    return Path(override).expanduser() if override else Path.home() / ".cache" / "quarry" / "tunnels.json"


REGISTRY_FILE = _default_registry_file()

# Registry keys this process itself wrote (issue #101 r1-1): `_POOL` only
# ever holds tunnels *this* process spawned, so this process is the only one
# allowed to remove their registry entries (on stale-dimension replacement or
# at exit) — a long-running `qy gui`/MCP process's entries must survive a
# separately-invoked `qy proxy` reading (and garbage-collecting dead entries
# from) the same file.
_OWNED_REGISTRY_KEYS: set[str] = set()


class _Tunnel:
    def __init__(self, proc: subprocess.Popen | None, local_port: int, *, attached: bool = False):
        self.proc = proc
        self.local_port = local_port
        self.attached = attached
        self.registry_entry = None
        self.users = 0
        self.retired = False
        self.shared = os.environ.get("QUARRY_TUNNEL_OWNER") == "keeper"

    def alive(self) -> bool:
        if self.proc is None:
            return bool(self.registry_entry and _entry_alive(*self.registry_entry))
        return self.proc.poll() is None


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait_port(host: str, port: int, proc: subprocess.Popen, timeout: float = 9.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:   # ssh exited early — no point waiting
            return False
        try:
            with socket.create_connection((host, port), timeout=1):
                return True
        except OSError:
            time.sleep(0.2)
    return False


def _db_host_port(url: str, engine: str) -> tuple[str, int]:
    parsed = urlparse(url if "://" in url else f"//{url}", scheme=engine)
    if engine == "postgres":
        from .core import _pg_uri_query
        params = dict(_pg_uri_query(parsed.query))
        return (params.get("hostaddr") or params.get("host") or parsed.hostname or "127.0.0.1",
                int(params.get("port") or parsed.port or 5432))
    return (parsed.hostname or "127.0.0.1", parsed.port or DEFAULT_DB_PORT.get(engine, 5432))


def _port_open(host: str, port: int) -> bool:
    """A quick, best-effort liveness probe for a tunnel this process doesn't
    hold the subprocess handle for (a registry entry written by another
    process) — same 0.3s-probe style as proxy.py's `_port_listening`."""
    try:
        with socket.create_connection((host, port), timeout=0.3):
            return True
    except OSError:
        return False


def _registry_key(key: tuple) -> str:
    ssh_host, ssh_port, ssh_user, ssh_key, db_host, db_port, proxy_key = key
    proxy_part = f"{proxy_key[0]}:{proxy_key[1]}" if proxy_key else "-"
    return f"{ssh_user}@{ssh_host}:{ssh_port}|{ssh_key or '-'}|{db_host}:{db_port}|{proxy_part}"


def _load_registry() -> dict:
    try:
        with REGISTRY_FILE.open(encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _process_identity(pid) -> str | None:
    """Process start time and command, not merely a recyclable PID."""
    try:
        if int(pid) <= 0:
            return None
        result = subprocess.run(
            ["ps", "-ww", "-p", str(int(pid)), "-o", "lstart=", "-o", "command="],
            capture_output=True, text=True, timeout=2,
        )
        return result.stdout.strip() or None if result.returncode == 0 else None
    except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        return None


def _entry_alive(pid, entry: dict) -> bool:
    if not isinstance(entry, dict):
        return False
    port = entry.get("local_port")
    return bool(
        type(port) is int and 0 < port < 65536
        and entry.get("owner_identity")
        and entry.get("ssh_identity")
        and _process_identity(pid) == entry["owner_identity"]
        and _process_identity(entry.get("ssh_pid")) == entry["ssh_identity"]
        and _port_open("127.0.0.1", port)
    )


def _registry_attached_tunnel(key: tuple) -> _Tunnel | None:
    registry = _load_registry().get(_registry_key(key))
    if not isinstance(registry, dict):
        return None
    for pid, entry in registry.items():
        if pid == _own_pid() or not isinstance(entry, dict):
            continue
        if entry.get("owner") == "keeper" and _entry_alive(pid, entry):
            t = _Tunnel(None, entry["local_port"], attached=True)
            t.registry_entry = (pid, dict(entry))
            return t
    return None


@contextlib.contextmanager
def _registry_transaction():
    """Lock a stable sidecar inode across the whole read/modify/replace."""
    REGISTRY_FILE.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(str(REGISTRY_FILE) + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, "a") as lock:
        os.fchmod(lock.fileno(), 0o600)
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _save_registry(data: dict) -> None:
    """Atomic private snapshot; mutating callers hold _registry_transaction."""
    tmp = None
    try:
        REGISTRY_FILE.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, tmp = tempfile.mkstemp(prefix=REGISTRY_FILE.name + ".", suffix=".tmp",
                                   dir=REGISTRY_FILE.parent)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, REGISTRY_FILE)
    finally:
        if tmp and os.path.exists(tmp):
            os.unlink(tmp)


def _own_pid() -> str:
    return str(os.getpid())


def _register_tunnel(key: tuple, t: "_Tunnel", proxy_key) -> None:
    """Persist a freshly-established tunnel to the cross-process registry
    (issue #101 r1-1): `_POOL` is only ever visible within this process, so
    without this, a separately-invoked `qy proxy` (a brand-new process with
    its own empty `_POOL`) can never observe a tunnel a long-running `qy
    gui`/MCP process is holding open — `qy proxy` would report an empty
    tunnel list even while queries are actively flowing through one.

    Entries are nested one level deeper than `_registry_key(key)` alone,
    keyed by this process's own pid (issue #101 r2-1): two *different*
    processes can independently open a tunnel to the exact same (ssh
    target, db target, proxy dimension) — `_POOL` is per-process, so
    nothing stops that — and a flat `{rkey: entry}` map would let the
    second writer silently clobber the first's entry, then let whichever
    process happens to exit first delete an entry that might by then
    belong to the *other*, still-running process.

    Must be called with `_LOCK` already held."""
    ssh_host, ssh_port, ssh_user, _ssh_key, db_host, db_port, _pk = key
    rkey = _registry_key(key)
    with _registry_transaction():
        registry = _load_registry()
        registry.setdefault(rkey, {})[_own_pid()] = {
            "ssh_target": f"{ssh_user}@{ssh_host}:{ssh_port}",
            "db_target": f"{db_host}:{db_port}",
            "local_port": t.local_port,
            "proxied": proxy_key is not None,
            "proxy": f"{proxy_key[0]}:{proxy_key[1]}" if proxy_key else None,
            "owner_identity": _process_identity(os.getpid()),
            "ssh_pid": getattr(t.proc, "pid", None),
            "ssh_identity": _process_identity(getattr(t.proc, "pid", None)),
            "owner": os.environ.get("QUARRY_TUNNEL_OWNER", "client"),
        }
        _save_registry(registry)
        _OWNED_REGISTRY_KEYS.add(rkey)


def _unregister_own_tunnel(registry: dict, rkey: str) -> bool:
    """Remove *this process's own* slot for `rkey` from `registry` (in
    place), leaving any other process's entry for the same `rkey`
    untouched. Returns whether anything was actually removed. Must be
    called with `_LOCK` already held."""
    procs = registry.get(rkey)
    if not procs or _own_pid() not in procs:
        return False
    procs.pop(_own_pid(), None)
    if not procs:
        registry.pop(rkey, None)
    return True


def _rewrite_url_hostport(url: str, new_host: str, new_port: int) -> str:
    parsed = urlparse(url)
    userinfo = ""
    if parsed.username or parsed.password:   # handle password-only (redis://:pw@host)
        userinfo = parsed.username or ""
        if parsed.password:
            userinfo += f":{parsed.password}"
        userinfo += "@"
    return urlunparse(parsed._replace(netloc=f"{userinfo}{new_host}:{new_port}"))


def _proxy_command_option(proxy_info: "proxy_mod.ProxyInfo") -> str:
    """`-o ProxyCommand=...` value: ssh substitutes %h/%p with the ssh target it
    is actually connecting to (the bastion), so proxycommand.py doesn't need to
    know it ahead of time. `sys.executable` (not a bare `python`) because a
    GUI/launchd-spawned process's PATH is often too thin to find one."""
    return (f"{shlex.quote(sys.executable)} -m quarry.proxycommand "
            f"{shlex.quote(proxy_info.host)} {proxy_info.port} %h %p")


class _StderrCapture:
    """Continuously drain SSH diagnostics without unbounded memory or a full pipe."""
    def __init__(self, stream):
        self.chunks = deque(maxlen=8)
        self.lock = threading.Lock()
        self.thread = threading.Thread(target=self._drain, args=(stream,), daemon=True)
        self.thread.start()

    def _drain(self, stream):
        try:
            while chunk := stream.read(4096):
                with self.lock:
                    self.chunks.append(chunk)
        except (OSError, ValueError):
            pass
        finally:
            stream.close()

    def diagnostic(self) -> bytes:
        with self.lock:
            return b"".join(self.chunks)


def _make_tunnel(
    conn, db_host: str, db_port: int, connect_timeout: float | None = None,
    proxy_info: "proxy_mod.ProxyInfo | None" = None,
) -> _Tunnel:
    """`connect_timeout=None` keeps the historical fixed budget (ssh
    ConnectTimeout=6, port-wait up to 9s) used by short probes (connections
    test, describe-table, health checks — untouched by issue #94). A given
    value (query paths pass DEFAULT_CONNECT_TIMEOUT_SEC) drives both.

    `proxy_info`, when set, routes the ssh TCP stream through it via
    `ProxyCommand` (see proxycommand.py) — the fix for issue #96's throttled
    cross-border ssh tunnels."""
    from .core import EXIT_CONNECTION_ERROR, QuarryError

    key_path = os.path.expanduser(conn.ssh_key) if getattr(conn, "ssh_key", None) else None
    if key_path and not os.path.exists(key_path):
        raise QuarryError(
            f"ssh key not found: {key_path} — install the bastion key (or fix ssh_key)",
            exit_code=EXIT_CONNECTION_ERROR,
        )
    local_port = _free_port()
    wait_timeout = connect_timeout if connect_timeout is not None else 9.0
    ssh_connect_timeout = max(1, int(connect_timeout)) if connect_timeout is not None else 6
    cmd = [
        "ssh", "-N",
        "-o", "ExitOnForwardFailure=yes", "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=accept-new", "-o", f"ConnectTimeout={ssh_connect_timeout}",
        "-o", "ServerAliveInterval=15",
    ]
    if proxy_info is not None:
        cmd += ["-o", f"ProxyCommand={_proxy_command_option(proxy_info)}"]
    else:
        # Direct means direct even when ~/.ssh/config supplies a proxy/jump.
        cmd += ["-o", "ProxyCommand=none", "-o", "ProxyJump=none"]
    forward_host = f"[{db_host}]" if ":" in db_host else db_host
    cmd += [
        "-L", f"127.0.0.1:{local_port}:{forward_host}:{db_port}",
        "-p", str(getattr(conn, "ssh_port", None) or 22),
    ]
    if key_path:
        cmd += ["-i", key_path]
    cmd += [f"{getattr(conn, 'ssh_user', None) or 'root'}@{conn.ssh_host}"]

    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    capture = _StderrCapture(proc.stderr) if getattr(proc, "stderr", None) is not None else None
    if not _wait_port("127.0.0.1", local_port, proc, timeout=wait_timeout):
        stderr = b""
        try:
            _stop_process(_Tunnel(proc, local_port))
            if capture is None:
                _, stderr = proc.communicate(timeout=3)
        except Exception:
            pass
        if capture is not None:
            capture.thread.join(timeout=0.2)
            stderr = capture.diagnostic()
        detail = (stderr.decode("utf-8", "replace").strip() or "port not ready / timeout")[:300]
        raise QuarryError(
            f"ssh tunnel to {conn.ssh_host} failed: {detail}",
            exit_code=EXIT_CONNECTION_ERROR,
        )
    return _Tunnel(proc, local_port)


@contextlib.contextmanager
def open_tunnel(conn, engine: str, connect_timeout: float | None = None, use_proxy: bool | None = None):
    """Yield an effective DB URL. If conn has ssh_host, ensure a pooled tunnel and
    yield a forwarded URL; PostgreSQL retains its TLS host via hostaddr.

    `connect_timeout` bounds tunnel establishment (issue #94: connection setup is
    capped independently of, and more tightly than, query execution) — it only
    matters on the first call for a given ssh target; a pooled/reused tunnel
    returns immediately. Callers that don't pass it (existing short probes) keep
    the historical fixed budget — see `_make_tunnel`.

    `use_proxy` (issue #96): None defers to the owning workspace's persisted
    proxy toggle (`qy proxy on|off`); False forces a direct connection for this
    call only (CLI `--no-proxy`). Only affects connections with `ssh_host` — a
    direct (non-tunneled) DB connection can't be routed through an HTTP proxy,
    see `check_connection_write`'s warning at `connections add` time."""
    if not getattr(conn, "ssh_host", None):
        yield conn.url
        return

    db_host, db_port = _db_host_port(conn.url, engine)
    key = tunnel_identity(conn, engine, use_proxy=use_proxy)
    proxy_key = key[-1]
    proxy_info = (proxy_mod.ProxyInfo(host=proxy_key[0], port=proxy_key[1], source="tunnel")
                  if proxy_key else None)
    with _LOCK:
        setup_lock = _SETUP_LOCKS.setdefault(key, threading.Lock())
        generation = _POOL_GENERATION
    with setup_lock:
        with _LOCK:
            t = _POOL.get(key)
        if t is not None and not t.alive():
            close_tunnel_identity(key)
            t = None
        fresh = t is None
        if fresh:
            t = _registry_attached_tunnel(key)
            if t is None:
                # SSH connection/port wait must not block unrelated pool keys.
                t = _make_tunnel(conn, db_host, db_port, connect_timeout=connect_timeout, proxy_info=proxy_info)
        with _LOCK:
            if generation != _POOL_GENERATION:
                if fresh and t.proc is not None:
                    _stop_process(t)
                raise RuntimeError("tunnel pool closed during connection setup")
            if fresh:
                try:
                    if not t.attached:
                        _register_tunnel(key, t, proxy_key)
                except Exception:
                    _stop_process(t)
                    raise
                _POOL[key] = t
                _terminate_stale_dimension(key)
            elif _POOL.get(key) is not t:
                # Another dimension retired this entry during its liveness probe.
                raise RuntimeError("tunnel retired during connection setup; retry")
            t.users += 1
            local_port = t.local_port
    try:
        if engine == "postgres":
            from .core import _pg_uri_query
            parsed = urlparse(conn.url)
            query = [(k, v) for k, v in _pg_uri_query(parsed.query)
                     if k not in {"hostaddr", "port"}]
            query.extend([("hostaddr", "127.0.0.1"), ("port", str(local_port))])
            # libpq verifies the original host but connects to the loopback forward.
            original_host = parsed.hostname or "127.0.0.1"
            host = f"[{original_host}]" if ":" in original_host else original_host
            rewritten = _rewrite_url_hostport(conn.url, host, local_port)
            yield urlunparse(urlparse(rewritten)._replace(query=urlencode(query, quote_via=quote)))
        else:
            yield _rewrite_url_hostport(conn.url, "127.0.0.1", local_port)
    finally:
        with _LOCK:
            t.users -= 1
            _finish_retired(t)


def tunnel_identity(conn, engine: str, use_proxy: bool | None = None) -> tuple:
    db_host, db_port = _db_host_port(conn.url, engine)
    info = proxy_mod.should_use_proxy(
        conn.ssh_host, workspace_home=getattr(conn, "source", None) or workspace.WS.home,
        override=use_proxy,
    )
    return (conn.ssh_host, getattr(conn, "ssh_port", None) or 22,
            getattr(conn, "ssh_user", None) or "root", getattr(conn, "ssh_key", None) or "",
            db_host, db_port, (info.host, info.port) if info else None)


def _stop_process(t: _Tunnel) -> None:
    if t.proc is None:
        return
    try:
        t.proc.terminate()
        t.proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        t.proc.kill()
        t.proc.wait(timeout=3)
    except (OSError, RuntimeError):
        pass


def _finish_retired(t: _Tunnel) -> None:
    if not t.retired or t.users:
        return
    if t.shared and t.proc is not None and t.proc.poll() is None:
        return
    _stop_process(t)
    if t in _RETIRED:
        _RETIRED.remove(t)


def close_tunnel_identity(identity: tuple) -> None:
    """Stop offering a tunnel for new work; preserve existing borrowers."""
    with _LOCK:
        t = _POOL.pop(identity, None)
        if t is None:
            return
        t.retired = True
        _RETIRED.append(t)
        rkey = _registry_key(identity)
        try:
            with _registry_transaction():
                registry = _load_registry()
                if _unregister_own_tunnel(registry, rkey):
                    _save_registry(registry)
            _OWNED_REGISTRY_KEYS.discard(rkey)
        finally:
            _finish_retired(t)


def _terminate_stale_dimension(new_key: tuple) -> None:
    # Keeper clients outlive keeper open_tunnel contexts; shared retired
    # processes remain until shutdown because cross-process leases are unknown.
    for key in list(_POOL):
        if key[:-1] == new_key[:-1] and key != new_key:
            close_tunnel_identity(key)


def list_tunnels() -> list[dict]:
    """Snapshot of every tunnel currently reachable, in-process or not
    (issue #101 r1-1): `qy proxy` and the GUI need to answer "is this
    connection actually going through the proxy right now, and is the tunnel
    still alive?" as an observed fact — but `qy proxy` runs as its own fresh
    process, so a tunnel a long-running `qy gui`/MCP process is holding open
    lives entirely in *that* process's `_POOL`, invisible here without the
    shared registry file. Entries from other processes have their liveness
    re-checked using both process identities and the recorded local port;
    stale snapshots are pruned without overwriting concurrent registrations.

    Registry entries are nested by pid (issue #101 r2-1) — this process's
    own entries are skipped here (by pid, not by logical key) because
    they're already fully represented via `_POOL` above; two *different*
    processes can each independently hold a live tunnel to the exact same
    (ssh target, db target, proxy dimension), and both must show up."""
    with _LOCK:
        items = []
        for key, t in _POOL.items():
            ssh_host, ssh_port, ssh_user, _ssh_key, db_host, db_port, proxy_key = key
            items.append({
                "ssh_target": f"{ssh_user}@{ssh_host}:{ssh_port}",
                "db_target": f"{db_host}:{db_port}",
                "local_port": t.local_port,
                "proxied": proxy_key is not None,
                "proxy": f"{proxy_key[0]}:{proxy_key[1]}" if proxy_key else None,
                "alive": t.alive(),
            })
        registry = _load_registry()
    own_pid = _own_pid()
    stale: list[tuple[str, str]] = []
    for rkey, procs in registry.items():
        if not isinstance(procs, dict):
            continue
        for pid, entry in procs.items():
            if pid == own_pid:
                continue  # this process's own tunnel — already listed via _POOL
            if not _entry_alive(pid, entry):
                stale.append((rkey, pid))
                continue
            items.append({**entry, "alive": True})
    if stale:
        with _LOCK, _registry_transaction():
            current = _load_registry()
            for rkey, pid in stale:
                procs = current.get(rkey)
                if isinstance(procs, dict) and procs.get(pid) == registry[rkey][pid]:
                    procs.pop(pid, None)
                    if not procs:
                        current.pop(rkey, None)
            _save_registry(current)
    return items


def tunnel_fact_for(conn, engine: str) -> dict | None:
    """The currently-live `list_tunnels()` entry for `conn`, if any (issue
    #101 r1-2) — for the GUI's per-connection proxy badge to report what a
    tunnel is actually doing right now instead of predicting what a fresh
    connection would do. Returns None when `conn` has no `ssh_host` (nothing
    to tunnel) or no tunnel currently exists for it (never queried yet, or
    the old tunnel from before a workspace proxy-toggle flip was already
    retired by `_terminate_stale_dimension` and the replacement hasn't
    been created yet). Exact key and current proxy dimensions must match."""
    if not getattr(conn, "ssh_host", None):
        return None
    key = tunnel_identity(conn, engine)
    with _LOCK:
        t = _POOL.get(key)
        if t is not None and t.alive():
            return {"ssh_target": f"{key[2]}@{key[0]}:{key[1]}",
                    "db_target": f"{key[4]}:{key[5]}", "local_port": t.local_port,
                    "proxied": key[-1] is not None,
                    "proxy": f"{key[-1][0]}:{key[-1][1]}" if key[-1] else None,
                    "alive": True}
        procs = _load_registry().get(_registry_key(key), {})
        if isinstance(procs, dict):
            for pid, entry in procs.items():
                if _entry_alive(pid, entry):
                    return {**entry, "alive": True}
    return None


def close_all() -> None:
    global _POOL_GENERATION
    with _LOCK:
        _POOL_GENERATION += 1
        for t in [*_POOL.values(), *_RETIRED]:
            _stop_process(t)
        _POOL.clear()
        _RETIRED.clear()
        if _OWNED_REGISTRY_KEYS:
            with _registry_transaction():
                registry = _load_registry()
                changed = False
                for rkey in _OWNED_REGISTRY_KEYS:
                    changed = _unregister_own_tunnel(registry, rkey) or changed
                if changed:
                    _save_registry(registry)
                _OWNED_REGISTRY_KEYS.clear()


atexit.register(close_all)
