"""Local development services — `qy local up/down/status`.

Bring Postgres, Redis, and an empty Neptune-compatible endpoint up so a
locally-running service talks only to `localhost` instead of shared dev data.
Postgres and Redis use Docker; the Neptune endpoint is a small local process.
`up <key>` auto-registers an `env=local` connection into `connections.toml`.

Shared-container model: ONE Postgres container hosts many logical databases
(one per connection key); it is not a container-per-key. Data lives on a named
docker volume so it survives `down` (and `stop`/`start`) unless `--purge`.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, replace
from pathlib import Path

from . import core, workspace
from .core import EXIT_CONNECTION_ERROR, CONN_KEY_RE, QuarryError

LOCAL_ENV = "local"

# Credentials baked into the local Postgres container (localhost-only; the whole
# point of `qy local` is that nothing here is remotely reachable).
LOCAL_PG_USER = "quarry"
LOCAL_PG_PASSWORD = "quarry"

# A logical-db name doubles as a Postgres database name and a connection-key
# suffix, so it must be a safe SQL identifier — reuse the connection-key rule.
SAFE_DB_RE = CONN_KEY_RE


@dataclass(frozen=True)
class EngineSpec:
    engine: str
    container: str
    volume: str
    port: int          # host port (configurable for Docker engines)
    internal_port: int
    default_image: str

    def url(self, dbname: str, *, redis_db: int | None = None) -> str:
        if self.engine == "postgres":
            return (f"postgresql://{LOCAL_PG_USER}:{LOCAL_PG_PASSWORD}"
                    f"@localhost:{self.port}/{dbname}")
        if self.engine == "redis":
            return f"redis://localhost:{self.port}/{redis_db if redis_db is not None else 0}"
        return f"https://localhost:{self.port}"


PG_SPEC = EngineSpec(
    engine="postgres", container="quarry-local-postgres", volume="quarry-local-pgdata",
    port=5433, internal_port=5432, default_image="postgres:16-alpine",
)
REDIS_SPEC = EngineSpec(
    engine="redis", container="quarry-local-redis", volume="quarry-local-redisdata",
    port=6380, internal_port=6379, default_image="redis:7-alpine",
)
NEPTUNE_SPEC = EngineSpec(
    engine="neptune", container="quarry-local-neptune-empty", volume="",
    port=18182, internal_port=18182, default_image="empty",
)
SPECS: dict[str, EngineSpec] = {
    "postgres": PG_SPEC, "redis": REDIS_SPEC, "neptune": NEPTUNE_SPEC,
}


def specs_for(engine: str | None) -> list[EngineSpec]:
    if engine in (None, "all"):
        return [configured_spec(name) for name in SPECS]
    return [configured_spec(engine)]


def configured_spec(engine: str) -> EngineSpec:
    spec = SPECS[engine]
    port = workspace._read_config().get("local", {}).get(f"{engine}_port", spec.port)
    if type(port) is not int or not 1 <= port <= 65535:
        raise QuarryError(f"invalid local {engine} port in config.toml", exit_code=core.EXIT_USAGE)
    return replace(spec, port=port)


def port_owner(port: int) -> str:
    rc, out, _ = _run_docker(["ps", "--filter", f"publish={port}", "--format", "{{.Names}}"], timeout=10)
    return ", ".join(out.split()) if rc == 0 and out.strip() else "another service"


def _port_conflict_message(spec: EngineSpec) -> str:
    return (f"port {spec.port} is already in use by {port_owner(spec.port)}; "
            f"choose a free port with `qy local up --engine {spec.engine} --port <port>`. "
            "The existing service has not been stopped.")


def connection_failure_hint(conn: core.Connection) -> str | None:
    """Read-only context for failed local connections; never auto-start Docker."""
    from urllib.parse import urlsplit

    url = urlsplit(conn.url)
    engine = core.connection_engine(conn)
    if (conn.env != LOCAL_ENV or engine not in ("postgres", "redis")
            or url.hostname not in ("localhost", "127.0.0.1", "::1") or conn.ssh_host):
        return None
    spec = configured_spec(engine)
    if url.port != spec.port:
        return f"local connection uses port {url.port}; Quarry's managed {engine} port is {spec.port}. Check the connection target."
    status = engine_status(spec)
    if not status["docker"]:
        return f"Docker is unavailable; start Docker, then run `qy local up --engine {engine}`."
    if status.get("port_conflict"):
        return _port_conflict_message(spec)
    if not status["running"]:
        return f"Quarry's local {engine} is not running; run `qy local up --engine {engine}`."
    return f"Quarry's local {engine} is running; check the connection credentials and database name."


def _sync_managed_ports(spec: EngineSpec, old_port: int) -> None:
    """Update only registered Quarry-volume connections still on the old port."""
    from urllib.parse import urlsplit

    for ws in workspace.WS_LIST:
        path = ws.connections_file
        with core.connections_file_lock(path):
            header, data = core._read_connections_file_parts(path)
            changed = False
            for fields in data.values():
                if (fields.get("env") != LOCAL_ENV or fields.get("local_volume") != spec.volume
                        or fields.get("ssh_host")):
                    continue
                url = urlsplit(str(fields.get("url", "")))
                if (url.hostname not in ("localhost", "127.0.0.1", "::1") or url.port != old_port
                        or core.infer_engine(str(fields.get("url", "")), fields.get("engine")) != spec.engine):
                    continue
                fields["url"] = url._replace(netloc=url.netloc.rsplit(":", 1)[0] + f":{spec.port}").geturl()
                changed = True
            if changed:
                shutil.copy2(path, path.with_name(f"{path.name}.before-port-{time.time_ns()}"))
                core._write_connections_file(header, data, path)


def start_on_port(spec: EngineSpec, port: int, *, image: str | None = None) -> tuple[EngineSpec, str]:
    """Explicit port change: preserve the named data volume; never stop services."""
    if spec.engine not in ("postgres", "redis") or not 1 <= port <= 65535:
        raise QuarryError("--port requires postgres/redis and a port from 1 to 65535", exit_code=core.EXIT_USAGE)
    require_docker()
    updated = replace(spec, port=port)
    state = container_state(spec.container)
    actual_port = spec.port
    if state != "absent":
        rc, out, _ = _run_docker(["inspect", spec.container], timeout=10)
        if rc:
            raise QuarryError("could not inspect local container before port change", exit_code=EXIT_CONNECTION_ERROR)
        info = json.loads(out)[0]
        bindings = info.get("HostConfig", {}).get("PortBindings", {}).get(f"{spec.internal_port}/tcp") or []
        actual_port = int(bindings[0]["HostPort"]) if bindings else 0
        if actual_port != port:
            if state == "running":
                raise QuarryError(f"stop the Quarry container first with `qy local down --engine {spec.engine}` "
                                  "before changing its port", exit_code=EXIT_CONNECTION_ERROR)
            if port_in_use(port):
                raise QuarryError(_port_conflict_message(updated), exit_code=EXIT_CONNECTION_ERROR)
            # Do not recreate a custom container whose data is stored elsewhere.
            mounts = info.get("Mounts", [])
            if not any(m.get("Name") == spec.volume for m in mounts):
                raise QuarryError("container uses a custom data mount; change its port manually", exit_code=core.EXIT_USAGE)
            image = image or info.get("Config", {}).get("Image")
            rc, _, error = _run_docker(["rm", spec.container], timeout=30)
            if rc:
                raise QuarryError(f"could not recreate stopped container: {error.strip()}", exit_code=EXIT_CONNECTION_ERROR)
    result = start_container(updated, image=image)
    # Save only after startup succeeds. Persisted config is shared by all workspaces.
    workspace._write_table_scalar("local", f"{spec.engine}_port", str(port))
    _sync_managed_ports(updated, spec.port)
    return updated, result


# ---------------------------------------------------------------------------
# docker CLI seam — the single place a real docker subprocess is invoked
# ---------------------------------------------------------------------------

def resolve_docker() -> str:
    docker = shutil.which("docker")
    if not docker:
        raise QuarryError(
            "docker not found in PATH — install Docker to use `qy local`",
            exit_code=EXIT_CONNECTION_ERROR,
        )
    return docker


def _run_docker(args: list[str], *, timeout: int = 60) -> tuple[int, str, str]:  # pragma: no cover - thin subprocess seam
    cmd = [resolve_docker(), *args]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return (-1, "", f"docker timed out after {timeout}s")
    return (proc.returncode, proc.stdout, proc.stderr)


def docker_available() -> bool:
    """True if the docker binary exists AND the daemon answers `docker version`."""
    if not shutil.which("docker"):
        return False
    try:
        rc, _, _ = _run_docker(["version", "--format", "{{.Server.Version}}"], timeout=10)
    except Exception:
        return False
    return rc == 0


def require_docker() -> None:
    """Raise a readable QuarryError when docker is missing or the daemon is down."""
    if not shutil.which("docker"):
        raise QuarryError(
            "docker not found in PATH — install Docker to use `qy local`",
            exit_code=EXIT_CONNECTION_ERROR,
        )
    rc, _, _ = _run_docker(["version", "--format", "{{.Server.Version}}"], timeout=10)
    if rc != 0:
        raise QuarryError(
            "docker is installed but the daemon isn't responding (is Docker running?)",
            exit_code=EXIT_CONNECTION_ERROR,
        )


# ---------------------------------------------------------------------------
# container / volume / port inspection
# ---------------------------------------------------------------------------

def container_state(name: str) -> str:
    """One of: 'running' | 'stopped' | 'absent'."""
    rc, out, _ = _run_docker(["inspect", "-f", "{{.State.Running}}", name], timeout=15)
    if rc != 0:
        return "absent"
    return "running" if out.strip() == "true" else "stopped"


def container_image(name: str) -> str | None:
    rc, out, _ = _run_docker(["inspect", "-f", "{{.Config.Image}}", name], timeout=15)
    return out.strip() if rc == 0 and out.strip() else None


def volume_exists(name: str) -> bool:
    rc, _, _ = _run_docker(["volume", "inspect", name], timeout=15)
    return rc == 0


def port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _is_port_conflict(stderr: str, port: int) -> bool:
    """True if a failed `docker run` stderr looks like a host-port bind conflict.

    Covers the race between `port_in_use`'s pre-check and the actual `docker run`
    (another process/container can grab the port in between).
    """
    m = stderr.lower()
    if "address already in use" in m or "port is already allocated" in m:
        return True
    return f":{port}" in m and "bind" in m


# ---------------------------------------------------------------------------
# lifecycle: up / down
# ---------------------------------------------------------------------------

def _docker_run_args(spec: EngineSpec, image: str) -> list[str]:
    base = ["run", "-d", "--name", spec.container, "-p", f"{spec.port}:{spec.internal_port}"]
    if spec.engine == "postgres":
        return base + [
            "-e", f"POSTGRES_USER={LOCAL_PG_USER}",
            "-e", f"POSTGRES_PASSWORD={LOCAL_PG_PASSWORD}",
            "-e", "POSTGRES_DB=postgres",
            "-v", f"{spec.volume}:/var/lib/postgresql/data",
            image,
        ]
    return base + ["-v", f"{spec.volume}:/data", image]


def start_container(spec: EngineSpec, *, image: str | None = None) -> str:
    """Bring the container up idempotently. Returns 'running' (already up),
    'started' (a stopped container resumed), or 'created' (freshly run)."""
    if spec.engine == "neptune":
        return start_neptune_empty(spec)
    require_docker()
    state = container_state(spec.container)
    if state == "running":
        return "running"
    if state == "stopped":
        rc, _, e = _run_docker(["start", spec.container], timeout=30)
        if rc != 0:
            raise QuarryError(
                _port_conflict_message(spec) if _is_port_conflict(e, spec.port)
                else f"failed to start container {spec.container}: {e.strip()}",
                exit_code=EXIT_CONNECTION_ERROR,
            )
        return "started"
    # absent -> create fresh; the host port must be free (only meaningful here,
    # since an already-running quarry container legitimately holds the port).
    if port_in_use(spec.port):
        raise QuarryError(
            _port_conflict_message(spec),
            exit_code=EXIT_CONNECTION_ERROR,
        )
    rc, _, e = _run_docker(_docker_run_args(spec, image or spec.default_image), timeout=180)
    if rc != 0:
        if _is_port_conflict(e, spec.port):
            raise QuarryError(
                _port_conflict_message(spec),
                exit_code=EXIT_CONNECTION_ERROR,
            )
        raise QuarryError(
            f"failed to start local {spec.engine} container: {e.strip()}",
            exit_code=EXIT_CONNECTION_ERROR,
        )
    return "created"


def wait_pg_ready(spec: EngineSpec, *, timeout: int = 40) -> bool:
    # -h 127.0.0.1: on first boot the official postgres image runs a temporary
    # init-phase server that listens ONLY on the unix socket, then restarts as
    # the real one. A socket-based pg_isready reports ready during that window
    # and the next command lands in the restart gap — checking over TCP counts
    # only the final server.
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rc, _, _ = _run_docker(
            ["exec", spec.container, "pg_isready", "-h", "127.0.0.1",
             "-U", LOCAL_PG_USER], timeout=10)
        if rc == 0:
            return True
        time.sleep(0.5)
    return False


def _is_transient_pg_error(stderr: str) -> bool:
    """Connection-level failures worth retrying while the server settles
    (socket not up yet / restarting / crash-recovery on a resumed volume)."""
    m = (stderr or "").lower()
    return ("connection to server" in m or "could not connect" in m
            or "starting up" in m or "no such file or directory" in m)


def ensure_pg_database(spec: EngineSpec, dbname: str, *, retry_for: float = 15.0) -> None:
    """Create the logical database inside the shared Postgres container if absent."""
    if not SAFE_DB_RE.match(dbname):  # defensive: callers validate first
        raise QuarryError(f"invalid local database name '{dbname}'", exit_code=core.EXIT_USAGE)
    deadline = time.monotonic() + retry_for
    while True:
        rc, out, e = _run_docker(
            ["exec", spec.container, "psql", "-U", LOCAL_PG_USER, "-d", "postgres", "-tAc",
             f"SELECT 1 FROM pg_database WHERE datname='{dbname}'"], timeout=15)
        if rc == 0 and out.strip() == "1":
            return
        if rc == 0:
            rc, _, e = _run_docker(
                ["exec", spec.container, "createdb", "-U", LOCAL_PG_USER, dbname], timeout=30)
            if rc == 0 or "already exists" in (e or "").lower():
                return
        if _is_transient_pg_error(e) and time.monotonic() < deadline:
            time.sleep(0.5)
            continue
        raise QuarryError(
            f"failed to create database '{dbname}': {(e or '').strip()}",
            exit_code=EXIT_CONNECTION_ERROR,
        )


def down_engine(spec: EngineSpec, *, purge: bool) -> dict:
    """Stop the container. With purge=True also remove it and its data volume.
    Returns a summary dict for the CLI to render."""
    if spec.engine == "neptune":
        return down_neptune_empty(spec, purge=purge)
    require_docker()
    state = container_state(spec.container)
    result = {"engine": spec.engine, "was": state, "stopped": False,
              "purged": purge, "removed_volume": False}
    if state == "running":
        rc, _, e = _run_docker(["stop", spec.container], timeout=60)
        if rc != 0:
            raise QuarryError(
                f"failed to stop container {spec.container}: {e.strip()}",
                exit_code=EXIT_CONNECTION_ERROR,
            )
        result["stopped"] = True
    if purge:
        if state != "absent":
            _run_docker(["rm", "-f", spec.container], timeout=60)
        if volume_exists(spec.volume):
            rc, _, e = _run_docker(["volume", "rm", spec.volume], timeout=30)
            if rc != 0:
                raise QuarryError(
                    f"failed to remove volume {spec.volume}: {e.strip()}",
                    exit_code=EXIT_CONNECTION_ERROR,
                )
            result["removed_volume"] = True
    return result


def engine_status(spec: EngineSpec) -> dict:
    """Read-only container status. Never raises on a missing daemon — reports it."""
    if spec.engine == "neptune":
        return neptune_empty_status(spec)
    if not docker_available():
        return {"engine": spec.engine, "docker": False, "running": False,
                "state": "unknown", "port": spec.port, "image": None,
                "volume": spec.volume, "volume_exists": False}
    state = container_state(spec.container)
    image = container_image(spec.container) if state != "absent" else None
    conflict = state != "running" and port_in_use(spec.port)
    return {"engine": spec.engine, "docker": True, "running": state == "running",
            "state": state, "port": spec.port, "image": image,
            "volume": spec.volume, "volume_exists": volume_exists(spec.volume),
            "port_conflict": conflict, "port_owner": port_owner(spec.port) if conflict else None}


# ---------------------------------------------------------------------------
# local empty Neptune process
# ---------------------------------------------------------------------------

def neptune_state_dir() -> Path:
    base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return base / "quarry" / "neptune-empty"


def _neptune_paths() -> tuple[Path, Path, Path, Path]:
    state = neptune_state_dir()
    return state / "pid", state / "cert.pem", state / "key.pem", state / "server.log"


def _read_neptune_pid() -> int | None:
    pid_path, _, _, _ = _neptune_paths()
    try:
        return int(pid_path.read_text(encoding="utf-8").strip())
    except (FileNotFoundError, ValueError):
        return None


def _owned_neptune_pid(pid: int | None) -> bool:
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    proc = subprocess.run(
        ["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True, check=False,
    )
    return proc.returncode == 0 and "quarry.neptune_empty" in proc.stdout


def _ensure_neptune_certificate(cert: Path, key: Path) -> None:
    if cert.exists() and key.exists():
        return
    openssl = shutil.which("openssl")
    if not openssl:
        raise QuarryError(
            "openssl not found in PATH — required for the local Neptune HTTPS endpoint",
            exit_code=EXIT_CONNECTION_ERROR,
        )
    proc = subprocess.run([
        openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "3650",
        "-keyout", str(key), "-out", str(cert), "-subj", "/CN=localhost",
        "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1",
    ], capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise QuarryError(
            f"failed to create local Neptune TLS certificate: {proc.stderr.strip()}",
            exit_code=EXIT_CONNECTION_ERROR,
        )


def _neptune_health(port: int, *, timeout: float = 1.0) -> bool:
    context = ssl._create_unverified_context()
    try:
        with urllib.request.urlopen(
            f"https://127.0.0.1:{port}/health", timeout=timeout, context=context,
        ) as response:
            payload = json.loads(response.read())
        return response.status == 200 and payload.get("backend") == "empty"
    except (OSError, ValueError, urllib.error.URLError):
        return False


def start_neptune_empty(spec: EngineSpec = NEPTUNE_SPEC, *, timeout: float = 10.0) -> str:
    pid_path, cert, key, log = _neptune_paths()
    pid = _read_neptune_pid()
    if _owned_neptune_pid(pid) and _neptune_health(spec.port):
        return "running"
    if port_in_use(spec.port):
        raise QuarryError(
            f"port {spec.port} is already in use — free it or stop the conflicting service",
            exit_code=EXIT_CONNECTION_ERROR,
        )
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    _ensure_neptune_certificate(cert, key)
    with log.open("ab") as output:
        proc = subprocess.Popen(
            [sys.executable, "-m", "quarry.neptune_empty", "--port", str(spec.port),
             "--cert", str(cert), "--key", str(key)],
            stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    pid_path.write_text(f"{proc.pid}\n", encoding="utf-8")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _neptune_health(spec.port):
            return "created"
        if proc.poll() is not None:
            break
        time.sleep(0.1)
    detail = log.read_text(encoding="utf-8", errors="replace")[-500:] if log.exists() else ""
    raise QuarryError(
        f"local Neptune empty endpoint did not become ready: {detail.strip()}",
        exit_code=EXIT_CONNECTION_ERROR,
    )


def down_neptune_empty(spec: EngineSpec = NEPTUNE_SPEC, *, purge: bool) -> dict:
    pid_path, cert, key, log = _neptune_paths()
    pid = _read_neptune_pid()
    running = _owned_neptune_pid(pid)
    result = {"engine": spec.engine, "was": "running" if running else "absent",
              "stopped": False, "purged": purge, "removed_volume": False}
    if running and pid is not None:
        os.kill(pid, 15)
        deadline = time.monotonic() + 5
        while _owned_neptune_pid(pid) and time.monotonic() < deadline:
            time.sleep(0.1)
        if _owned_neptune_pid(pid):
            os.kill(pid, 9)
        result["stopped"] = True
    pid_path.unlink(missing_ok=True)
    if purge:
        for path in (cert, key, log):
            path.unlink(missing_ok=True)
        try:
            pid_path.parent.rmdir()
        except OSError:
            pass
    return result


def neptune_empty_status(spec: EngineSpec = NEPTUNE_SPEC) -> dict:
    pid = _read_neptune_pid()
    running = _owned_neptune_pid(pid) and _neptune_health(spec.port)
    return {"engine": spec.engine, "backend": "empty", "docker": None,
            "running": running, "state": "running" if running else "absent",
            "port": spec.port, "image": None, "volume": None,
            "volume_exists": False, "pid": pid if running else None}


# ---------------------------------------------------------------------------
# connection registration (pure connections.toml read/write — no docker)
# ---------------------------------------------------------------------------

def _logical_of(key: str, fields: dict[str, object]) -> str:
    db = fields.get("db")
    return str(db) if db else key


def existing_local_key(data: dict[str, dict[str, object]], logical: str) -> str | None:
    """The key of an already-registered env=local connection for this env-set, if any.

    Checks the deterministic `<logical>_local` key first — it is what
    `_pick_local_key` would hand out, so it stays the identity anchor even if the
    user later edits that connection's `db` field (only its content, not the key
    itself, is user-editable in practice). Falls back to matching by content for
    connections.toml files written before this convention existed.
    """
    base = f"{logical}_{LOCAL_ENV}"
    base_fields = data.get(base)
    if base_fields is not None and str(base_fields.get("env") or "").lower() == LOCAL_ENV:
        return base
    for k, f in data.items():
        if _logical_of(k, f) == logical and str(f.get("env") or "").lower() == LOCAL_ENV:
            return k
    return None


def _pick_local_key(data: dict[str, dict[str, object]], logical: str) -> str:
    base = f"{logical}_{LOCAL_ENV}"
    if base not in data:
        return base
    i = 2
    while f"{base}{i}" in data:
        i += 1
    return f"{base}{i}"


def redis_db_of(url: str) -> int | None:
    """The redis database index in a URL path (`redis://host:port/3` → 3)."""
    from urllib.parse import urlsplit

    seg = urlsplit(url).path.lstrip("/").split("/", 1)[0]
    return int(seg) if seg.isdigit() else None


def source_redis_db(logical: str) -> int | None:
    """Redis db index of the env-set's remote member (DEFAULT_ENV preferred),
    so the auto-registered local connection keeps the same index and a
    service's connection string ports over with only host:port changed."""
    try:
        conns = core.load_connections()
    except QuarryError:
        return None
    members = {
        (c.env or ""): c for c in conns.values()
        if c.logical_db == logical and (c.env or "").lower() != LOCAL_ENV
        and core.connection_engine(c) == "redis"
    }
    if not members:
        return None
    pick = members.get(core.DEFAULT_ENV) or members[sorted(members)[0]]
    return redis_db_of(pick.url)


def stored_local_image(logical: str) -> str | None:
    _, data = core._read_connections_file_parts()
    key = existing_local_key(data, logical)
    img = data[key].get("local_image") if key else None
    return str(img) if img is not None else None


def register_local_connection(
    logical: str, spec: EngineSpec, *, image: str | None = None, group: str | None = None,
    redis_db: int | None = None,
) -> tuple[str, bool]:
    """Idempotently ensure an env=local connection for `logical` exists.

    Returns (key, created). If a local connection already exists for this env-set
    it is left untouched (never overwrite user-edited fields) and created=False.
    """
    with core.connections_file_lock():
        header, data = core._read_connections_file_parts()
        existing = existing_local_key(data, logical)
        if existing:
            return existing, False
        key = _pick_local_key(data, logical)
        fields: dict[str, str] = {
            "url": spec.url(logical, redis_db=redis_db),
            "engine": spec.engine,
            "env": LOCAL_ENV,
            "db": logical,
        }
        if group:
            fields["group"] = group
        if image:
            fields["local_image"] = image
        if spec.volume:
            fields["local_volume"] = spec.volume
        if spec.engine == "neptune":
            fields["local_backend"] = "empty"
        data[key] = fields
        core._write_connections_file(header, data)
        return key, True
