"""Quarry core — the engine.

Lifted from the `dbq` CLI, generalized to be workspace-driven and importable
as a library (the CLI, GUI, and skill are all thin faces over this module).

Engines:
    postgres  - shells out to the `psql` binary
    mysql     - via pymysql (optional dependency)
    neptune   - openCypher over HTTP (urllib)

Paths (connections file, queries dir, psql binary) come from the current
Workspace (see workspace.py); reference `workspace.WS` at call time so that
--workspace reconfiguration is honored.
"""

from __future__ import annotations

import contextlib
import csv
import hashlib
import io
import ipaddress
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import time
import tomllib
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, unquote, urlencode, urlparse, urlunparse
from urllib.request import ProxyHandler, Request, build_opener, urlopen

from . import cache
from . import proxy as proxy_mod
from . import redis_engine, tunnel, workspace

# Exit codes (stable contract for callers)
EXIT_OK = 0
EXIT_USAGE = 1
EXIT_CONNECTION_ERROR = 2
EXIT_SQL_ERROR = 3
EXIT_NO_DATA = 4
EXIT_STRICT_DRIFT = 5
EXIT_FINGERPRINT_STALE = 6
EXIT_FINGERPRINT_MISSING = 7
EXIT_SAFETY_BLOCKED = 8   # write/DDL blocked without --write
EXIT_SYNC_DENIED = 9      # `qy local sync` refused: target is not env=local on a loopback host

NEPTUNE_TIMEOUT_SEC = int(os.environ.get("QUARRY_NEPTUNE_TIMEOUT", "60"))
NEPTUNE_INSECURE = os.environ.get("QUARRY_NEPTUNE_INSECURE", "").strip().lower() in {"1", "true", "yes", "on"}

# Default safety cap on rows returned when the SQL has no explicit LIMIT.
DEFAULT_MAX_ROWS = int(os.environ.get("QUARRY_MAX_ROWS", "500"))

# --- Query timeouts (issue #94) ---------------------------------------------
# Connection establishment (incl. SSH tunnel setup) is capped independently of
# query execution, so an unreachable host fails fast instead of eating the
# whole execution budget.
DEFAULT_CONNECT_TIMEOUT_SEC = 15
# Query execution: CLI/GUI default. MCP uses a tighter default (agents should
# converge faster) — see MCP_EXECUTE_TIMEOUT_SEC.
DEFAULT_EXECUTE_TIMEOUT_SEC = 300
MCP_EXECUTE_TIMEOUT_SEC = 120
# `qy ping` (issue #110): a lightweight reachability probe, not a query —
# bounds both tunnel/connect setup and the probe statement itself, same as
# `connections test`'s existing (undocumented) default.
DEFAULT_PING_TIMEOUT_SEC = 10

TIMEOUT_HINT = (" (increase it with --timeout, the QUARRY_TIMEOUT env var, "
                "or the connection's `timeout` setting in connections.toml)")


def _with_timeout_hint(message: str) -> str:
    return message if TIMEOUT_HINT in message else f"{message}{TIMEOUT_HINT}"


class QuarryError(Exception):
    """Engine-level error carrying a stable exit code (raised by the library API)."""

    def __init__(self, message: str, exit_code: int = EXIT_USAGE):
        super().__init__(message)
        self.exit_code = exit_code


# ---------------------------------------------------------------------------
# Small utilities
# ---------------------------------------------------------------------------

def err(msg: str, *, exit_code: int | None = None) -> None:
    """With exit_code: raise QuarryError (so library callers like the GUI get a
    catchable error instead of a process-killing SystemExit; the CLI converts it
    to an exit code in main()). Without exit_code: print a non-fatal warning."""
    if exit_code is not None:
        raise QuarryError(msg, exit_code)
    print(f"quarry: {msg}", file=sys.stderr)


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def format_bytes(n: float) -> str:
    """Human-readable byte size, auto-scaled: '0 B', '512 B', '4.2 KB', '1.3 MB'."""
    if n < 1024:
        return f"{int(n)} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / (1024 * 1024):.1f} MB"


def format_query_stats(elapsed_ms: int, download_bytes: int, size_is_estimated: bool) -> str:
    """A one-line 'elapsed · download size · avg speed' summary for the CLI's
    stderr (issue #105, built on #104's `elapsed_ms`/`download_bytes`/
    `size_is_estimated`). `size_is_estimated` marks the size and speed with
    '≈' — postgres/mysql/redis approximate download size from a subprocess's
    stdout or re-serialized rows, only neptune's is an exact byte count."""
    approx = "≈" if size_is_estimated else ""
    elapsed_sec = elapsed_ms / 1000 or 0.001  # avoid division by zero for <1ms queries
    speed_bps = download_bytes / elapsed_sec
    return (
        f"{elapsed_ms}ms · downloaded {approx}{format_bytes(download_bytes)} "
        f"· avg speed {approx}{format_bytes(speed_bps)}/s"
    )


def resolve_psql() -> str:
    psql_bin = workspace.WS.psql_bin
    if shutil.which(psql_bin):
        return psql_bin
    homebrew = "/opt/homebrew/opt/postgresql@13/bin/psql"
    if Path(homebrew).exists():
        return homebrew
    err("psql not found in PATH (set QUARRY_PSQL or install postgresql)", exit_code=EXIT_CONNECTION_ERROR)
    return ""  # unreachable


# ---------------------------------------------------------------------------
# Connections
# ---------------------------------------------------------------------------

@dataclass
class Connection:
    key: str
    url: str
    region: str | None = None
    env: str | None = None
    notes: str | None = None
    engine: str = "postgres"
    # Optional SSH bastion (set -> queries are tunneled, see tunnel.py)
    ssh_host: str | None = None
    ssh_user: str | None = None
    ssh_key: str | None = None
    ssh_port: int | None = None
    # Organization: `group` = sidebar folder (project); `db` = logical database
    # identity. Connections sharing `db` (same schema, different env) form an
    # env-set and share saved queries.
    group: str | None = None
    db: str | None = None
    source: str | None = None   # workspace home this connection was loaded from
    # Per-connection query execution timeout override (seconds), see resolve_timeout().
    timeout: int | None = None

    @property
    def logical_db(self) -> str:
        return self.db or self.key


# Default env picked for an env-set when none is specified (safest = dev).
DEFAULT_ENV = os.environ.get("QUARRY_DEFAULT_ENV", "dev")


def resolve_timeout(
    conn: Connection | None, cli_timeout: int | None = None, *, default: int = DEFAULT_EXECUTE_TIMEOUT_SEC,
) -> int:
    """Resolve the effective query execution timeout (seconds).

    Priority: explicit arg (e.g. CLI `--timeout`) > `QUARRY_TIMEOUT` env var >
    the connection's configured `timeout` > `default`."""
    if cli_timeout is not None:
        return cli_timeout
    env_val = os.environ.get("QUARRY_TIMEOUT")
    if env_val:
        try:
            parsed = int(env_val)
        except ValueError:
            parsed = None
        # A non-positive value (0 or negative) is as malformed as non-numeric
        # text here: it isn't rejected up front like --timeout/connections.toml
        # (this env var reaches many library callers, not just the CLI's own
        # argparse validation), so fall through instead of e.g. reducing PG's
        # statement_timeout to ~1ms and cancelling almost every query.
        if parsed is not None and parsed > 0:
            return parsed
    if conn is not None and conn.timeout is not None:
        return conn.timeout
    return default


def load_connections() -> dict[str, Connection]:
    wss = workspace.WS_LIST
    if not any(w.connections_file.exists() for w in wss):
        err(f"connections file not found: {wss[0].connections_file}", exit_code=EXIT_USAGE)
    out: dict[str, Connection] = {}
    for w in wss:
        conn_file = w.connections_file
        if not conn_file.exists():
            continue
        with conn_file.open("rb") as f:
            raw = tomllib.load(f)
        for key, val in raw.items():
            if key in out:  # earlier workspace wins on conflict
                continue
            if not isinstance(val, dict) or "url" not in val:
                err(f"connection [{key}] is missing required 'url'", exit_code=EXIT_USAGE)
            ssh_port = val.get("ssh_port")
            timeout = val.get("timeout")
            if timeout is not None and int(timeout) <= 0:
                err(f"connection [{key}]: 'timeout' must be a positive integer (seconds), "
                    f"got {timeout}", exit_code=EXIT_USAGE)
            out[key] = Connection(
            key=key,
            url=val["url"],
            region=val.get("region"),
            env=val.get("env"),
            notes=val.get("notes"),
            engine=infer_engine(val["url"], val.get("engine")),
            ssh_host=val.get("ssh_host"),
            ssh_user=val.get("ssh_user"),
            ssh_key=val.get("ssh_key"),
            ssh_port=int(ssh_port) if ssh_port else None,
            group=val.get("group"),
            db=val.get("db"),
            source=str(w.home),
            timeout=int(timeout) if timeout is not None else None,
        )
    return out


def resolve_connection(name: str, env: str | None = None) -> Connection:
    """Resolve a target to a Connection.

    `name` may be a connection key OR a logical db (env-set) name.
      - direct key, no --env  -> that connection (backward compatible)
      - logical db / --env     -> the env-set member for that env (default: dev)
    """
    conns = load_connections()
    if env is None and name in conns:
        return conns[name]

    # `name` may be a logical db OR a connection key — search the env-set by
    # logical db either way (so `@db: shop_dev` + --env jp still
    # resolves to the shop env-set's jp member, keeping legacy query
    # files working).
    logical = conns[name].logical_db if name in conns else name
    members = {c.env or "": c for c in conns.values() if c.logical_db == logical}
    if members:
        target = env or DEFAULT_ENV
        if target in members:
            return members[target]
        if env is None and len(members) == 1:
            return next(iter(members.values()))
        if env is None:  # multi-env set, no dev -> pick a stable first
            return members[sorted(members)[0]]
        if name in conns:  # env given but not in set -> fall back to the key
            return conns[name]
        avail = ", ".join(sorted(e for e in members if e)) or "<none>"
        err(f"env '{env}' not found for '{name}'. Available: {avail}", exit_code=EXIT_USAGE)

    if name in conns:
        return conns[name]
    available = ", ".join(sorted(conns.keys())) or "<none>"
    err(f"unknown db '{name}'. Available: {available}", exit_code=EXIT_USAGE)
    raise SystemExit(EXIT_USAGE)  # unreachable


def group_connections() -> list[dict[str, Any]]:
    """Structured view for CLI/GUI: [{group, items: [{db, is_env_set, envs:[...]}]}]."""
    conns = list(load_connections().values())
    groups: dict[str, dict[str, list[Connection]]] = {}
    gsrc: dict[str, str | None] = {}
    order: list[str] = []
    for c in conns:
        g = c.group or ""
        if g not in groups:
            groups[g] = {}
            gsrc[g] = c.source
            order.append(g)
        groups[g].setdefault(c.logical_db, []).append(c)

    out: list[dict[str, Any]] = []
    for g in order:
        items = []
        for ldb, members in groups[g].items():
            # local pinned first regardless of registration order, so it's the
            # default pick (envs.find(dev) || envs[0]) when there's no dev env,
            # and always the leftmost pill/tab in the GUI.
            ordered = sorted(members, key=lambda m: 0 if m.env == "local" else 1)
            items.append({
                "db": ldb,
                "is_env_set": len(members) > 1 or bool(members[0].env),
                "engine": connection_engine(members[0]),
                "envs": [
                    {"env": m.env, "key": m.key, "engine": connection_engine(m),
                     "region": m.region, "ssh": bool(m.ssh_host)}
                    for m in ordered
                ],
            })
        out.append({"group": g or None, "ws": gsrc.get(g), "items": items})
    return out


def infer_engine(url: str, explicit: str | None = None) -> str:
    if explicit:
        engine = explicit.strip().lower()
        if engine not in {"postgres", "mysql", "neptune", "redis"}:
            err(f"unsupported engine '{explicit}' (expected postgres|mysql|neptune|redis)", exit_code=EXIT_USAGE)
        return engine
    lower = url.lower()
    if lower.startswith("mysql://") or lower.startswith("mysql+"):
        return "mysql"
    if lower.startswith("redis://") or lower.startswith("rediss://"):
        return "redis"
    if "neptune.amazonaws.com" in lower:
        return "neptune"
    return "postgres"


def connection_engine(conn: Connection) -> str:
    return infer_engine(conn.url, conn.engine)


def get_connection(key: str) -> Connection:
    conns = load_connections()
    if key not in conns:
        available = ", ".join(sorted(conns.keys())) or "<none>"
        err(f"unknown db key '{key}'. Available: {available}", exit_code=EXIT_USAGE)
    return conns[key]


CONN_KEY_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]*$")


def _toml_escape_string(s: str) -> str:
    s = s.replace("\\", "\\\\").replace('"', '\\"')
    s = s.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
    return f'"{s}"'


def _is_preservable_field(fv: object) -> bool:
    if isinstance(fv, (str, int, float, bool)):
        return True
    return isinstance(fv, list) and all(isinstance(i, (str, int, float, bool)) for i in fv)


def _toml_value(v: object) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, list):
        return "[" + ", ".join(_toml_value(item) for item in v) + "]"
    return _toml_escape_string(str(v))


@contextlib.contextmanager
def connections_file_lock():
    """Serialize read-modify-write access to connections.toml across processes.

    Best-effort: on platforms without `fcntl` (e.g. Windows) this is a no-op,
    matching the rest of the codebase's POSIX-first assumptions (docker/ssh/psql).
    """
    try:
        import fcntl
    except ImportError:  # pragma: no cover - non-POSIX platform
        yield
        return
    lock_path = workspace.WS.connections_file.with_name(workspace.WS.connections_file.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a+") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def _read_connections_file_parts() -> tuple[list[str], dict[str, dict[str, object]]]:
    conn_file = workspace.WS.connections_file
    if not conn_file.exists():
        return ([], {})
    text = conn_file.read_text(encoding="utf-8")
    lines = text.splitlines()
    header: list[str] = []
    for line in lines:
        if line.lstrip().startswith("["):
            break
        header.append(line)
    while header and header[-1].strip() == "":
        header.pop()

    with conn_file.open("rb") as f:
        raw = tomllib.load(f)
    data: dict[str, dict[str, object]] = {}
    for k, v in raw.items():
        if isinstance(v, dict):
            kept: dict[str, object] = {}
            for fk, fv in v.items():
                if _is_preservable_field(fv):
                    kept[fk] = fv
                else:
                    print(
                        f"warning: connections.toml [{k}].{fk} has an unsupported "
                        "type and will be dropped if this file is rewritten",
                        file=sys.stderr,
                    )
            data[k] = kept
    return (header, data)


def _write_connections_file(header: list[str], data: dict[str, dict[str, object]]) -> None:
    parts: list[str] = []
    if header:
        parts.append("\n".join(header))
        parts.append("")
    field_order = ["url", "engine", "region", "env", "notes"]
    for key, fields in data.items():
        if not CONN_KEY_RE.match(key):
            err(f"invalid connection key '{key}' (must match {CONN_KEY_RE.pattern})", exit_code=EXIT_USAGE)
        parts.append(f"[{key}]")
        emitted: set[str] = set()
        for fk in field_order:
            if fk in fields:
                parts.append(f"{fk:<6} = {_toml_value(fields[fk])}")
                emitted.add(fk)
        for fk, fv in fields.items():
            if fk in emitted:
                continue
            parts.append(f"{fk} = {_toml_value(fv)}")
        parts.append("")
    text = "\n".join(parts).rstrip("\n") + "\n"
    workspace.WS.connections_file.write_text(text, encoding="utf-8")


# ---------------------------------------------------------------------------
# Connection write-time safety checks (issue #76)
#
# A local dev .env (loopback host, throwaway credentials) getting registered
# as if it were a real remote connection is a real, recurring footgun. Every
# signal checked here is derivable from the command's own arguments plus the
# existing connections.toml content — no DB round-trip, no external file.
# ---------------------------------------------------------------------------

_ENGINE_DEFAULT_PORT = {"postgres": 5432, "mysql": 3306, "redis": 6379, "neptune": 8182}
_LOCAL_KEY_SUFFIX_RE = re.compile(r"_local\d*$")


def _conn_host_port(url: str, engine: str) -> tuple[str | None, int | None]:
    raw = url
    if "://" not in raw:
        # Neptune alone accepts a bare `host:port` endpoint (normalize_neptune_endpoint
        # prepends https://); other engines require a scheme, so a schemeless value
        # there is just an invalid URL, not a host:port worth extracting.
        if engine != "neptune":
            return (None, None)
        raw = f"https://{raw}"
    parsed = urlparse(raw)
    return (parsed.hostname, parsed.port or _ENGINE_DEFAULT_PORT.get(engine))


def _normalize_host_for_compare(host: str | None) -> str | None:
    if host is None:
        return None
    return "127.0.0.1" if _is_loopback_host(host) else host.lower()


def _find_port_conflict(
    data: dict[str, dict[str, object]], *, exclude_key: str, host: str | None, port: int | None,
) -> tuple[str, dict[str, object]] | None:
    """The (key, fields) of another entry already bound to the same host:port, if any."""
    if host is None or port is None:
        return None
    norm_host = _normalize_host_for_compare(host)
    for k, fields in data.items():
        if k == exclude_key:
            continue
        url = fields.get("url")
        if not isinstance(url, str):
            continue
        raw_engine = fields.get("engine")
        engine = infer_engine(url, raw_engine if isinstance(raw_engine, str) else None)
        ohost, oport = _conn_host_port(url, engine)
        if _normalize_host_for_compare(ohost) == norm_host and oport == port:
            return (k, fields)
    return None


def check_connection_write(
    key: str, fields: dict[str, object], existing: dict[str, dict[str, object]], *, force: bool = False,
) -> None:
    """Deterministic sanity checks run by `connections add`/`set` before a
    write: a host:port already claimed by another entry, a loopback host with
    no ssh tunnel, and an env=local key that doesn't follow the local naming
    convention. The first is a hard error unless `force`; the rest are
    non-fatal warnings the caller may ignore."""
    url = fields.get("url")
    raw_engine = fields.get("engine")
    engine = str(raw_engine) if isinstance(raw_engine, str) else (infer_engine(url) if isinstance(url, str) else "postgres")
    host, port = _conn_host_port(url, engine) if isinstance(url, str) else (None, None)

    conflict = _find_port_conflict(existing, exclude_key=key, host=host, port=port)
    if conflict is not None:
        occ_key, occ_fields = conflict
        purpose = occ_fields.get("notes") or occ_fields.get("local_image")
        purpose_txt = f" — {purpose}" if purpose else ""
        msg = (
            f"host:port '{host}:{port}' is already used by connection [{occ_key}]{purpose_txt}. "
            "Adding another connection to the same target usually means a config mix-up "
            "(e.g. a local Docker shadow db mistaken for a remote server). "
            "Pass --force to add it anyway."
        )
        if force:
            err(msg)
        else:
            err(msg, exit_code=EXIT_USAGE)

    if host is not None and _is_loopback_host(host) and not fields.get("ssh_host"):
        err(
            f"host '{host}' is a local loopback address and no ssh_host is set. "
            "If this connection is meant to reach a remote server, double-check that "
            "service's deployment docs (README / docker-compose / .env.example) rather "
            "than a local dev .env — add --ssh-host/--ssh-user/--ssh-key to tunnel to the "
            "real host. If this really is a local database, you can ignore this notice."
        )

    if str(fields.get("env") or "").lower() == "local" and not _LOCAL_KEY_SUFFIX_RE.search(key):
        err(
            f"connection key '{key}' has env=local but doesn't follow the '<name>_local' "
            f"naming convention (e.g. '{key}_local') used by `qy local up` — consider "
            "renaming so local connections are easy to tell apart from remote ones."
        )

    # issue #96: the workspace proxy toggle only routes ssh_host connections
    # (via ProxyCommand) — a direct DB connection's client (psql/mysql) never
    # issues an HTTP CONNECT, so the toggle silently does nothing for it.
    if (not fields.get("ssh_host") and engine != "neptune"
            and workspace.is_proxy_enabled(workspace.WS.home)):
        err(
            f"workspace has the proxy enabled (`qy proxy`), but connection [{key}] has no "
            "ssh_host — the proxy only applies to ssh-tunneled connections (and Neptune's "
            "direct HTTPS requests), so it will have no effect here."
        )


# ---------------------------------------------------------------------------
# Query metadata (saved .sql files)
# ---------------------------------------------------------------------------

META_LINE_RE = re.compile(r"^\s*--\s*@([\w-]+)\s*:\s*(.*?)\s*$")
PARAM_RE = re.compile(
    r"^(?P<name>[a-zA-Z_][\w]*)\s*"
    r"(?:\(\s*(?P<type>[\w]+)?\s*"
    r"(?:,\s*(?P<spec>required|default\s*=\s*[^)]*))?\s*\))?\s*$"
)


@dataclass
class Param:
    name: str
    type: str = "text"
    required: bool = False
    default: str | None = None

    def to_meta_value(self) -> str:
        spec_parts: list[str] = [self.type]
        if self.required:
            spec_parts.append("required")
        elif self.default is not None:
            spec_parts.append(f"default={self.default}")
        return f"{self.name} ({', '.join(spec_parts)})"


@dataclass
class Query:
    name: str
    db: str
    desc: str = ""
    tags: list[str] = field(default_factory=list)
    params: list[Param] = field(default_factory=list)
    schema_sources: list[str] = field(default_factory=list)
    source_fingerprint: str | None = None
    saved_at: str | None = None
    last_validated: str | None = None
    sql: str = ""
    path: Path | None = None

    @property
    def has_limit(self) -> bool:
        return bool(re.search(r"\bLIMIT\b", self.sql, re.IGNORECASE))


def _parse_param_spec(raw: str) -> Param:
    m = PARAM_RE.match(raw)
    if not m:
        err(f"invalid @param spec: {raw!r}", exit_code=EXIT_USAGE)
    name = m.group("name")
    typ = m.group("type") or "text"
    spec = m.group("spec") or ""
    required = False
    default: str | None = None
    if spec.startswith("required"):
        required = True
    elif spec.startswith("default"):
        _, _, val = spec.partition("=")
        default = val.strip()
    return Param(name=name, type=typ, required=required, default=default)


def parse_query_file(path: Path) -> Query:
    text = path.read_text(encoding="utf-8")
    meta: dict[str, list[str]] = {}
    body_lines: list[str] = []
    in_header = True
    for line in text.splitlines():
        if in_header:
            stripped = line.strip()
            if stripped == "" or stripped.startswith("--"):
                m = META_LINE_RE.match(line)
                if m:
                    meta.setdefault(m.group(1), []).append(m.group(2))
                    continue
                if stripped == "" or stripped.startswith("--"):
                    if stripped.startswith("--") and not m:
                        body_lines.append(line)
                    continue
            in_header = False
        body_lines.append(line)

    name_vals = meta.get("name", [])
    db_vals = meta.get("db", [])
    if not name_vals or not db_vals:
        err(f"{path}: missing @name or @db in header", exit_code=EXIT_USAGE)
    if name_vals[0] != path.stem:
        err(
            f"{path}: @name '{name_vals[0]}' does not match filename stem '{path.stem}'",
            exit_code=EXIT_USAGE,
        )

    tags_raw = ",".join(meta.get("tags", []))
    tags = [t.strip() for t in tags_raw.split(",") if t.strip()]
    params = [_parse_param_spec(p) for p in meta.get("param", [])]

    return Query(
        name=name_vals[0],
        db=db_vals[0],
        desc=" ".join(meta.get("desc", [])).strip(),
        tags=tags,
        params=params,
        schema_sources=[s for s in meta.get("schema-source", []) if s],
        source_fingerprint=(meta.get("source-fingerprint") or [None])[0],
        saved_at=(meta.get("saved-at") or [None])[0],
        last_validated=(meta.get("last-validated") or [None])[0],
        sql="\n".join(body_lines).strip(),
        path=path,
    )


def find_query_file(name: str) -> Path:
    matches: list[Path] = []
    for w in workspace.WS_LIST:
        matches += sorted(w.queries_dir.glob(f"**/{name}.sql"))
    if not matches:
        err(f"query '{name}' not found under {workspace.WS.queries_dir}", exit_code=EXIT_USAGE)
    if len(matches) > 1:
        err(
            f"query name '{name}' is ambiguous: {', '.join(str(m) for m in matches)}",
            exit_code=EXIT_USAGE,
        )
    return matches[0]


def load_query(name: str) -> Query:
    return parse_query_file(find_query_file(name))


def list_all_queries() -> list[Query]:
    out: list[Query] = []
    seen: set[str] = set()
    for w in workspace.WS_LIST:
        if not w.queries_dir.exists():
            continue
        for path in sorted(w.queries_dir.glob("**/*.sql")):
            try:
                q = parse_query_file(path)
            except SystemExit:
                raise
            except Exception as exc:
                err(f"failed to parse {path}: {exc}")
                continue
            if q.name in seen:  # earlier workspace wins
                continue
            seen.add(q.name)
            out.append(q)
    return out


# ---------------------------------------------------------------------------
# Fingerprint
# ---------------------------------------------------------------------------

def _resolve_source_path(p: str) -> Path:
    """Resolve a @schema-source value to an absolute path.

    Resolution order:
      1. absolute / ~ -> expand directly
      2. relative to CWD
      3. relative to ~/workspace (common monorepo root)
      4. relative to the workspace home's parent
      5. as-is (caller checks .exists())
    """
    raw = Path(os.path.expanduser(p))
    if raw.is_absolute():
        return raw
    candidates = [
        Path.cwd() / raw,
        Path.home() / "workspace" / raw,
        workspace.WS.home.parent / raw,
    ]
    for c in candidates:
        if c.exists():
            return c.resolve()
    return raw


def compute_fingerprint(sources: list[str]) -> tuple[str, list[dict[str, Any]]]:
    h = hashlib.sha256()
    details: list[dict[str, Any]] = []
    for declared in sorted(sources):
        resolved = _resolve_source_path(declared)
        if not resolved.exists():
            h.update(f"<MISSING:{declared}>".encode())
            details.append({"declared": declared, "resolved": str(resolved), "exists": False, "size": None})
            continue
        data = resolved.read_bytes()
        size = len(data)
        h.update(f"<FILE:{declared}:{size}>".encode())
        h.update(data)
        details.append({"declared": declared, "resolved": str(resolved), "exists": True, "size": size})
    return ("sha256:" + h.hexdigest()[:16], details)


# ---------------------------------------------------------------------------
# Safety rails (read-only default + auto-limit) — Quarry's AI-native guardrails
# ---------------------------------------------------------------------------

_WRITE_RE = re.compile(
    r"^\s*(insert|update|delete|drop|alter|create|truncate|grant|revoke|"
    r"merge|replace|call|do|vacuum|reindex|cluster|comment|lock|copy)\b",
    re.IGNORECASE,
)
# Data-modifying statements that are legal *inside* a top-level WITH (CTE) and
# would otherwise slip past a leading-keyword check (`WITH d AS (DELETE ...) ...`).
_CTE_WRITE_RE = re.compile(r"\b(insert|update|delete|merge)\b", re.IGNORECASE)
_LEADING_COMMENT_RE = re.compile(r"^\s*(--[^\n]*\n|/\*.*?\*/\s*)", re.DOTALL)
# Clauses that already bound the row count, or make a trailing `LIMIT` illegal.
_FETCH_RE = re.compile(r"\bFETCH\s+(?:FIRST|NEXT)\b", re.IGNORECASE)
_LOCK_RE = re.compile(r"\bFOR\s+(?:UPDATE|SHARE|NO\s+KEY\s+UPDATE|KEY\s+SHARE)\b", re.IGNORECASE)
_DOLLAR_TAG_RE = re.compile(r"\$([A-Za-z_]\w*)?\$")


def _strip_leading_comments(sql: str) -> str:
    prev = None
    out = sql
    while out != prev:
        prev = out
        out = _LEADING_COMMENT_RE.sub("", out, count=1)
    return out


def sql_skeleton(sql: str) -> str:
    """Blank out comments, string literals, dollar-quoted bodies, and quoted
    identifiers so keyword scanning and `;` splitting can't be fooled by content
    inside them (e.g. `WHERE x = 'DELETE; DROP'` or a column named "limit")."""
    out: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        c = sql[i]
        if c == "-" and i + 1 < n and sql[i + 1] == "-":            # line comment
            while i < n and sql[i] != "\n":
                i += 1
            out.append(" ")
            continue
        if c == "/" and i + 1 < n and sql[i + 1] == "*":            # block comment
            i += 2
            while i + 1 < n and not (sql[i] == "*" and sql[i + 1] == "/"):
                i += 1
            i += 2
            out.append(" ")
            continue
        if c == "'":                                                 # string literal
            i += 1
            while i < n:
                if sql[i] == "'" and i + 1 < n and sql[i + 1] == "'":
                    i += 2
                    continue
                if sql[i] == "'":
                    i += 1
                    break
                i += 1
            out.append("''")
            continue
        if c == '"':                                                 # quoted identifier
            i += 1
            while i < n:
                if sql[i] == '"' and i + 1 < n and sql[i + 1] == '"':
                    i += 2
                    continue
                if sql[i] == '"':
                    i += 1
                    break
                i += 1
            out.append(' "id" ')
            continue
        if c == "$":                                                 # dollar-quoted string
            m = _DOLLAR_TAG_RE.match(sql, i)
            if m:
                tag = m.group(0)
                end = sql.find(tag, i + len(tag))
                i = n if end == -1 else end + len(tag)
                out.append(" ")
                continue
        out.append(c)
        i += 1
    return "".join(out)


def _statements(sql: str) -> list[str]:
    """Top-level statements (skeleton-split on `;`), empties dropped."""
    return [s for s in sql_skeleton(sql).split(";") if s.strip()]


def is_read_only(sql: str) -> bool:
    """Conservative read-only check.

    Allows a *single* `SELECT` / `WITH ... SELECT` / `SHOW` / `EXPLAIN` / `TABLE`
    / `VALUES` statement. Blocks: any write/DDL by leading keyword, multiple
    statements (`SELECT 1; DROP TABLE t`), and data-modifying CTEs
    (`WITH d AS (DELETE ...) SELECT ...`).
    """
    stmts = _statements(sql)
    if len(stmts) > 1:                       # only one statement may run read-only
        return False
    head = (stmts[0] if stmts else sql_skeleton(sql)).lstrip()
    if _WRITE_RE.match(head):
        return False
    if re.match(r"\s*with\b", head, re.IGNORECASE) and _CTE_WRITE_RE.search(head):
        return False
    return True


def _strip_trailing_semicolons(sql: str) -> str:
    return re.sub(r";\s*$", "", sql.strip())


def has_limit(sql: str) -> bool:
    """True if the query already bounds its rows (LIMIT or FETCH FIRST/NEXT).
    Scans the skeleton so `WHERE x = 'LIMIT'` is not a false positive."""
    sk = sql_skeleton(sql)
    return bool(re.search(r"\bLIMIT\b", sk, re.IGNORECASE) or _FETCH_RE.search(sk))


def enforce_safety(
    sql: str,
    *,
    allow_write: bool,
    max_rows: int | None,
    offset: int = 0,
) -> tuple[str, int | None]:
    """Return (possibly-modified sql, applied_limit).

    - Raises QuarryError(EXIT_SAFETY_BLOCKED) on a write/DDL when allow_write=False.
    - When max_rows is set, the statement is read-only, and has no LIMIT, append
      `LIMIT max_rows+1` so the caller can detect truncation. applied_limit is the
      intended row cap (max_rows), else None.
    - When offset is set (grid "load more" pagination) it's appended as
      `OFFSET offset` alongside the auto LIMIT — only meaningful together with
      applied_limit; a query that already has its own LIMIT ignores offset
      (we never rewrite hand-written SQL).
    """
    if not allow_write and not is_read_only(sql):
        raise QuarryError(
            "blocked a write/DDL statement (read-only by default; pass --write to allow)",
            exit_code=EXIT_SAFETY_BLOCKED,
        )
    if max_rows is not None and is_read_only(sql) and not has_limit(sql):
        sk = sql_skeleton(sql)
        cleaned = sk.lstrip()
        # only statements that accept a trailing LIMIT (not EXPLAIN/SHOW/utility
        # output, and not a locking clause which must come after LIMIT)
        if re.match(r"^(select|with|table|values)\b", cleaned, re.IGNORECASE) and not _LOCK_RE.search(sk):
            inner = _strip_trailing_semicolons(sql)
            clause = f"LIMIT {max_rows + 1}"
            if offset:
                clause += f" OFFSET {offset}"
            return (f"{inner}\n{clause}", max_rows)
    return (sql, None)


# ---------------------------------------------------------------------------
# psql wrapping (postgres engine)
# ---------------------------------------------------------------------------

def _psql_args(url: str) -> list[str]:
    # ON_ERROR_STOP: without it psql -f returns 0 on SQL errors and a failed
    # statement would surface as an empty (successful-looking) result.
    return [resolve_psql(), url, "--no-psqlrc", "--quiet", "--no-align", "--tuples-only",
            "-v", "ON_ERROR_STOP=1"]


def _pg_url_with_connect_timeout(url: str, connect_timeout: int) -> str:
    """Force libpq's connect_timeout to `connect_timeout`, overriding any value
    already present in the URL's query string. libpq's precedence is
    connection-string params > PGCONNECT_TIMEOUT env var > built-in default, so
    a URL that already sets `?connect_timeout=N` (e.g. hand-authored in
    connections.toml) would silently ignore our env var and keep waiting on
    its own value — see issue #94 review r1-1."""
    parsed = urlparse(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query["connect_timeout"] = str(connect_timeout)
    return urlunparse(parsed._replace(query=urlencode(query)))


def run_psql_capture(
    url: str,
    sql: str,
    *,
    psql_vars: dict[str, str] | None = None,
    timeout: int = 60,
    connect_timeout: int | None = None,
) -> tuple[int, str, str]:
    """Run `sql` through psql and capture (returncode, stdout, stderr).

    `timeout` bounds the whole subprocess (connect + execute) as a last-resort
    client-side kill. `connect_timeout`, when given, is enforced two ways: as
    the URL's `connect_timeout` query param (authoritative — overrides any
    value already in the URL) and as PGCONNECT_TIMEOUT (belt-and-suspenders
    for non-URI conninfo strings, which have no query string to rewrite) —
    so a dead/unreachable server is reported as a connection failure (psql
    exit code 2) well before `timeout`. Existing short-probe callers that
    don't pass connect_timeout keep their old undifferentiated behavior."""
    if connect_timeout is not None:
        url = _pg_url_with_connect_timeout(url, connect_timeout)
    cmd = _psql_args(url)
    for k, v in (psql_vars or {}).items():
        cmd.extend(["-v", f"{k}={v}"])
    cmd.extend(["-f", "-"])
    env = None
    if connect_timeout is not None:
        env = {**os.environ, "PGCONNECT_TIMEOUT": str(connect_timeout)}
    try:
        proc = subprocess.run(cmd, input=sql, capture_output=True, text=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return (-1, "", _with_timeout_hint(f"psql timed out after {timeout}s"))
    return (proc.returncode, proc.stdout, proc.stderr)


def _pg_statement_timeout_prefix(execute_timeout: int) -> str:
    """A `SET statement_timeout` statement (~90% of execute_timeout) prepended to
    the script we send psql, so PostgreSQL cancels a runaway query server-side
    instead of leaving a zombie query behind when the client gives up."""
    stmt_timeout_ms = max(1, int(execute_timeout * 0.9 * 1000))
    return f"SET statement_timeout = '{stmt_timeout_ms}ms';\n"


def _psql_error_message(rc: int, errout: str) -> tuple[str, int]:
    """(message, exit_code) for a failed psql invocation. psql's own exit codes
    distinguish a connection failure (2) from a script/statement error (any
    other nonzero, incl. 3 under ON_ERROR_STOP) — see `man psql` EXIT STATUS."""
    msg = errout.strip()
    if rc == 2:
        return (f"postgres connection failed: {msg}", EXIT_CONNECTION_ERROR)
    if "statement timeout" in msg.lower():
        msg = _with_timeout_hint(msg)
    return (f"psql failed: {msg}", EXIT_SQL_ERROR)


def wrap_for_json(sql: str) -> str:
    inner = _strip_trailing_semicolons(sql)
    return (
        "SELECT COALESCE(json_agg(row_to_json(_q_t)), '[]'::json)::text "
        f"FROM ({inner}) _q_t"
    )


def wrap_for_csv(sql: str, with_header: bool = True) -> str:
    inner = _strip_trailing_semicolons(sql)
    return f"COPY ({inner}) TO STDOUT WITH CSV HEADER" if with_header else f"COPY ({inner}) TO STDOUT WITH CSV"


# ---------------------------------------------------------------------------
# MySQL wrapping
# ---------------------------------------------------------------------------


def import_pymysql():
    try:
        import pymysql  # type: ignore[import-not-found]
        return pymysql
    except ImportError:
        err("pymysql not found (pip install pymysql)", exit_code=EXIT_CONNECTION_ERROR)
        raise SystemExit(EXIT_CONNECTION_ERROR)


def parse_mysql_url(url: str) -> dict[str, Any]:
    normalized = re.sub(r"^mysql\+[^:]+://", "mysql://", url.strip(), count=1)
    parsed = urlparse(normalized)
    if parsed.scheme != "mysql":
        err(f"not a mysql URL: {url}", exit_code=EXIT_USAGE)
    database = unquote(parsed.path.lstrip("/"))
    if not database:
        err(f"mysql URL missing database name: {url}", exit_code=EXIT_USAGE)
    return {
        "host": parsed.hostname or "localhost",
        "port": parsed.port or 3306,
        "user": unquote(parsed.username or ""),
        "password": unquote(parsed.password or ""),
        "database": database,
    }


_PARAM_RE = re.compile(r":'(\w+)'|:(\w+)")


def substitute_params(sql: str, params: dict[str, str]) -> str:
    """Substitute `:'name'` (quoted+escaped) and `:name` (raw) placeholders in a
    single left-to-right pass, so a substituted value that itself contains a
    `:token` is never re-substituted."""
    def quote_val(value: str) -> str:
        return "'" + str(value).replace("\\", "\\\\").replace("'", "''") + "'"

    def repl(match: re.Match[str]) -> str:
        quoted, raw = match.group(1), match.group(2)
        name = quoted or raw
        if name not in params:
            return match.group(0)
        return quote_val(params[name]) if quoted else str(params[name])

    return _PARAM_RE.sub(repl, sql)


def serialize_row(row: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in row.items():
        if isinstance(value, datetime):
            out[key] = value.isoformat(sep=" ", timespec="seconds")
        elif isinstance(value, date):        # bare date: isoformat() takes no kwargs
            out[key] = value.isoformat()
        elif isinstance(value, Decimal):
            out[key] = float(value)
        elif isinstance(value, (bytes, bytearray, memoryview)):
            out[key] = bytes(value).decode("utf-8", errors="replace")
        else:
            out[key] = value
    return out


def run_mysql_query(
    url: str,
    sql: str,
    *,
    params: dict[str, str] | None = None,
    timeout: int = 60,
    connect_timeout: int | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """Returns (rows, download_bytes) — pymysql exposes no raw response buffer,
    so download_bytes (issue #104) is the rows re-serialized to JSON, an
    estimate of the actual wire size.

    `timeout` bounds query execution: as a client-side socket cap (pymysql
    read_timeout/write_timeout) and, best-effort, as a server-side execution
    cap (MAX_EXECUTION_TIME / max_statement_time session vars) so the server
    also cancels a runaway statement instead of just the client giving up —
    same intent as the PostgreSQL statement_timeout backstop (issue #94).
    `connect_timeout` bounds connection establishment independently. Callers
    that don't pass connect_timeout keep the pre-#94 behavior of reusing
    `timeout` for both."""
    pymysql = import_pymysql()
    cfg = parse_mysql_url(url)
    rendered = substitute_params(sql, params or {})
    ct = connect_timeout if connect_timeout is not None else timeout
    try:
        conn = pymysql.connect(
            host=cfg["host"], port=cfg["port"], user=cfg["user"], password=cfg["password"],
            database=cfg["database"], connect_timeout=ct, read_timeout=timeout,
            write_timeout=timeout, cursorclass=pymysql.cursors.DictCursor,
        )
    except pymysql.err.MySQLError as exc:
        raise QuarryError(f"mysql connection failed: {exc}", exit_code=EXIT_CONNECTION_ERROR) from exc
    try:
        with conn.cursor() as cur:
            # Server-side execution cap (issue #94 review r1-2): read_timeout/
            # write_timeout above only bound the client's socket wait — the
            # server keeps running the statement after the client gives up,
            # the same "zombie query" problem PG's statement_timeout fixes.
            # Try both session variables best-effort since the two MySQL-family
            # dialects disagree: MySQL >=5.7.4 has MAX_EXECUTION_TIME
            # (milliseconds, SELECT-only); MariaDB has max_statement_time
            # (seconds, all statement types). Whichever the server doesn't
            # recognize errors out harmlessly — this is a backstop, not a
            # requirement, so it must never abort the query itself.
            stmt_timeout_ms = max(1, timeout * 1000)
            with contextlib.suppress(pymysql.err.MySQLError):
                cur.execute(f"SET SESSION MAX_EXECUTION_TIME = {stmt_timeout_ms}")
            with contextlib.suppress(pymysql.err.MySQLError):
                cur.execute(f"SET SESSION max_statement_time = {max(1, timeout)}")
            cur.execute(rendered)
            rows = cur.fetchall() if cur.description else []
    except pymysql.err.MySQLError as exc:
        msg = str(exc)
        if "timed out" in msg.lower() or "timeout" in msg.lower():
            msg = _with_timeout_hint(msg)
        raise QuarryError(f"mysql error: {msg}", exit_code=EXIT_SQL_ERROR) from exc
    except (TimeoutError, OSError) as exc:
        raise QuarryError(_with_timeout_hint(f"mysql query timed out after {timeout}s"),
                          exit_code=EXIT_SQL_ERROR) from exc
    finally:
        conn.close()
    serialized = [serialize_row(dict(row)) for row in rows]
    download_bytes = len(json.dumps(serialized, default=str).encode("utf-8"))
    return serialized, download_bytes


# ---------------------------------------------------------------------------
# Neptune (openCypher over HTTP)
# ---------------------------------------------------------------------------

def normalize_neptune_endpoint(url: str) -> str:
    raw = url.strip()
    if not raw:
        err("empty Neptune endpoint URL", exit_code=EXIT_USAGE)
    if "://" not in raw:
        raw = f"https://{raw}"
    parsed = urlparse(raw)
    if not parsed.hostname:
        err(f"invalid Neptune endpoint URL: {url}", exit_code=EXIT_USAGE)
    scheme = parsed.scheme or "https"
    if scheme not in {"http", "https"}:
        err(f"unsupported Neptune URL scheme '{scheme}' (expected http/https)", exit_code=EXIT_USAGE)
    port = parsed.port or 8182
    path = parsed.path.rstrip("/")
    base = f"{scheme}://{parsed.hostname}:{port}"
    if path and path != "/":
        base += path
    return base


def _neptune_cypher_url(base_url: str) -> str:
    return base_url if base_url.endswith("/openCypher") else f"{base_url}/openCypher"


def _is_loopback_host(hostname: str | None) -> bool:
    if not hostname:
        return False
    if hostname == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def _neptune_ssl_context(hostname: str | None) -> ssl.SSLContext | None:
    # Loopback endpoints are SSH-tunnel forwards: the cert is issued for the
    # real Neptune hostname, so hostname verification can never pass there.
    if NEPTUNE_INSECURE or _is_loopback_host(hostname):
        return ssl._create_unverified_context()
    return None


def _normalize_row(row: Any) -> dict[str, Any]:
    return row if isinstance(row, dict) else {"value": row}


def _extract_neptune_rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [_normalize_row(r) for r in payload]
    if isinstance(payload, dict):
        if isinstance(payload.get("results"), list):
            return [_normalize_row(r) for r in payload["results"]]
        if isinstance(payload.get("result"), list):
            return [_normalize_row(r) for r in payload["result"]]
    return [_normalize_row(payload)]


def _connection_workspace_home(conn: Connection) -> "str | Path":
    """The workspace a Connection was actually loaded from (PR #98 review, r1-2) —
    proxy toggles are per-workspace, so this must be used instead of always
    assuming the primary workspace (workspace.WS.home)."""
    return getattr(conn, "source", None) or workspace.WS.home


def connection_proxy_target(conn: Connection) -> str | None:
    """The host a proxy decision would actually be evaluated against for
    `conn`, or None if this connection can't use a proxy at all (issue #101):
    an ssh_host connection routes its ssh session through the proxy; a direct
    (non-tunneled) Neptune connection can route its HTTPS requests through it;
    anything else (a direct postgres/mysql/redis connection) never touches
    the proxy regardless of the workspace toggle — see
    `check_connection_write`'s warning at `connections add` time."""
    if getattr(conn, "ssh_host", None):
        return conn.ssh_host
    if connection_engine(conn) == "neptune":
        return urlparse(conn.url).hostname
    return None


def resolve_proxy_decision(
    conn: Connection, *, override: bool | None = None,
    discovered: "proxy_mod.ProxyInfo | None | object" = proxy_mod.UNSET_DISCOVERY,
) -> "proxy_mod.ProxyDecision | None":
    """The full should-we-proxy decision for `conn`, or None if it can't use a
    proxy at all (see `connection_proxy_target`). Shared by the CLI's
    fallback-to-direct stderr hint and the GUI's per-connection proxy badge
    (issue #101) — both need the *reason*, not just tunnel.py's plain yes/no."""
    target_host = connection_proxy_target(conn)
    if target_host is None:
        return None
    ws_home = _connection_workspace_home(conn)
    return proxy_mod.evaluate_proxy(target_host, workspace_home=ws_home, override=override, discovered=discovered)


def proxy_fallback_notice(conn: Connection, *, use_proxy: bool | None = None) -> str | None:
    """A one-line explanation for the CLI's stderr (issue #101), or None when
    there's nothing worth saying: the proxy engaged, `--no-proxy` was passed
    explicitly, the workspace toggle is off, or `conn` can't use a proxy at
    all. Without this, a query that silently fell back to a direct (possibly
    throttled) connection looks identical to one that's actually proxied —
    the only symptom is that it's slower than expected, with no way to tell
    the two situations apart."""
    if use_proxy is False:
        return None
    decision = resolve_proxy_decision(conn, override=use_proxy)
    if decision is None or decision.proxy is not None:
        return None
    if decision.reason == "not_discovered":
        return (
            f"connection [{conn.key}]: workspace proxy is enabled, but none was discovered "
            "(system settings and ALL_PROXY/HTTPS_PROXY are all unset) — running directly."
        )
    if decision.reason == "port_unreachable":
        where = f"{decision.discovered.host}:{decision.discovered.port}" if decision.discovered else "?"
        return (
            f"connection [{conn.key}]: workspace proxy is enabled and a proxy was discovered "
            f"at {where}, but nothing is listening there — running directly."
        )
    if decision.reason == "exception_list":
        target = connection_proxy_target(conn)
        return (
            f"connection [{conn.key}]: {target} is covered by the proxy's exceptions list — "
            "routing directly, as configured."
        )
    return None  # "disabled": the workspace proxy isn't even on — nothing to report


def _live_proxied_fact(conn: Connection, engine: str, discovered: "proxy_mod.ProxyInfo | None") -> bool:
    """Whether `conn` is *actually* being routed through the proxy right now
    (issue #101 r1-2), not whether a fresh connection attempted this instant
    would be. The two can disagree: right after a workspace's proxy toggle
    flips, the old tunnel keeps running un-proxied until the next query
    re-establishes it (see `tunnel._terminate_stale_dimension`); before any
    query has ever run against an env, no tunnel exists at all yet. Reporting
    the decision instead of the fact would flash the badge on early or keep
    it on past the point where it's still true.

    ssh-tunneled connections (postgres/mysql/redis via ssh_host) have a real
    pooled tunnel.py resource to check — `tunnel.tunnel_fact_for` reports
    whether the tunnel that's currently alive for this exact target is
    proxied. A direct (non-tunneled) Neptune connection has no such
    persistent resource: every request re-evaluates the proxy decision
    independently, so there's no stale-state window here and
    `resolve_proxy_decision` is already the fact, not a stale guess."""
    if getattr(conn, "ssh_host", None):
        fact = tunnel.tunnel_fact_for(conn, engine)
        return bool(fact and fact["proxied"] and fact["alive"])
    decision = resolve_proxy_decision(conn, discovered=discovered)
    return bool(decision and decision.proxy is not None)


def attach_proxy_status(groups: list[dict[str, Any]]) -> None:
    """Mutate `group_connections()`'s output in place, adding a `proxied` bool
    to each env entry — ground truth for the GUI's env-pill badge (issue
    #101), not a frontend guess. `discover_proxy()` runs once for the whole
    call (a workspace has exactly one system-wide proxy) and the result is
    reused across every connection, rather than re-probed once per row.

    `groups` was itself built from a `load_connections()` call (inside
    `group_connections()`), so re-loading here is normally redundant, not
    risky — but callers that already have `groups` from elsewhere (tests
    stubbing `group_connections()`, or a future caller with no workspace
    configured) shouldn't have this best-effort enrichment step crash the
    whole connections listing; skip decoration rather than propagate."""
    discovered = proxy_mod.discover_proxy()
    try:
        conns = {c.key: c for c in load_connections().values()}
    except Exception:
        return
    for g in groups:
        for item in g["items"]:
            for e in item["envs"]:
                conn = conns.get(e["key"])
                e["proxied"] = _live_proxied_fact(conn, e["engine"], discovered) if conn else False


def run_neptune_cypher(
    endpoint_url: str,
    cypher: str,
    *,
    params: dict[str, str] | None = None,
    timeout: int = NEPTUNE_TIMEOUT_SEC,
    use_proxy: bool | None = None,
    workspace_home: "str | Path | None" = None,
) -> tuple[list[dict[str, Any]], int]:
    """Returns (rows, download_bytes) — download_bytes (issue #104) is the exact
    HTTP response body byte count, read before JSON decoding.

    `use_proxy` (issue #96): Neptune talks plain HTTPS, so — unlike ssh_host
    connections (tunnel.py's ProxyCommand) — it can go through the workspace's
    proxy directly. Only considered when the endpoint isn't already an
    ssh-tunnel loopback rewrite (tunnel.open_tunnel already resolved that url;
    routing a loopback forward through an HTTP proxy would be pointless and
    the cert there is for a hostname the proxy can't verify anyway).

    `workspace_home` (PR #98 review): the toggle is per-workspace, so callers
    holding a Connection loaded from a non-primary workspace must pass its
    `conn.source` — otherwise this always falls back to the primary workspace
    (workspace.WS.home), which is wrong in multi-workspace setups."""
    rendered = substitute_params(cypher, params or {})
    base = normalize_neptune_endpoint(endpoint_url)
    target = _neptune_cypher_url(base)
    body = urlencode({"query": rendered}).encode("utf-8")
    req = Request(target, data=body, method="POST", headers={
        "Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"})
    hostname = urlparse(target).hostname
    ssl_context = _neptune_ssl_context(hostname)
    proxy_info = None
    if not _is_loopback_host(hostname):
        proxy_info = proxy_mod.should_use_proxy(
            hostname, workspace_home=workspace_home or workspace.WS.home, override=use_proxy)
    try:
        if proxy_info is not None:
            opener = build_opener(ProxyHandler({"https": f"http://{proxy_info.host}:{proxy_info.port}"}))
            with opener.open(req, timeout=timeout) as resp:
                raw_bytes = resp.read()
        else:
            with urlopen(req, timeout=timeout, context=ssl_context) as resp:
                raw_bytes = resp.read()
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise QuarryError(f"neptune HTTP {exc.code}: {detail}", exit_code=EXIT_SQL_ERROR) from exc
    except URLError as exc:
        raise QuarryError(f"neptune request failed: {exc.reason}", exit_code=EXIT_CONNECTION_ERROR) from exc
    except TimeoutError as exc:
        raise QuarryError(_with_timeout_hint(f"neptune request timed out after {timeout}s"),
                          exit_code=EXIT_CONNECTION_ERROR) from exc
    raw = raw_bytes.decode("utf-8")
    try:
        payload = json.loads(raw) if raw.strip() else []
    except json.JSONDecodeError as exc:
        raise QuarryError(f"neptune returned non-JSON body: {raw[:200]}", exit_code=EXIT_SQL_ERROR) from exc
    return _extract_neptune_rows(payload), len(raw_bytes)


# ---------------------------------------------------------------------------
# Structured query API (used by GUI + rich JSON contract)
# ---------------------------------------------------------------------------

@dataclass
class QueryResult:
    columns: list[dict[str, Any]]   # [{"name": ..., "type": null}]  (types: v2)
    rows: list[dict[str, Any]]
    row_count: int
    truncated: bool
    elapsed_ms: int
    engine: str
    sql: str
    # issue #104: response-body size, for the GUI/CLI's average-speed display.
    # Postgres/Redis go through a subprocess CLI (psql/redis-cli) — the size is
    # the subprocess's stdout text, UTF-8 encoded, which is an approximation of
    # the real wire-protocol payload, hence size_is_estimated=True. MySQL has no
    # raw response buffer via pymysql, so its size is the rows re-serialized to
    # JSON (also estimated). Neptune talks plain HTTP, so its size is the exact
    # response body byte count (size_is_estimated=False).
    download_bytes: int = 0
    size_is_estimated: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "columns": self.columns,
            "rows": self.rows,
            "rowCount": self.row_count,
            "truncated": self.truncated,
            "elapsedMs": self.elapsed_ms,
            "engine": self.engine,
            "sql": self.sql,
            "downloadBytes": self.download_bytes,
            "sizeIsEstimated": self.size_is_estimated,
        }


def _columns_from_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: list[str] = []
    for row in rows:
        for k in row.keys():
            if k not in seen:
                seen.append(k)
    return [{"name": c, "type": None} for c in seen]


_PG_TEXT_STMT_RE = re.compile(r"^\s*(explain|show)\b", re.IGNORECASE)


def _rows_postgres(
    url: str, sql: str, params: dict[str, str], execute_timeout: int,
    connect_timeout: int = DEFAULT_CONNECT_TIMEOUT_SEC,
) -> tuple[list[dict[str, Any]], int]:
    """Returns (rows, download_bytes) — download_bytes is psql's stdout text,
    UTF-8 encoded (issue #104: an approximation of the real wire-protocol
    payload, since psql doesn't expose that)."""
    # Total subprocess budget is a last-resort safety net; the real split between
    # connect and execute is enforced server-side (PGCONNECT_TIMEOUT + statement_timeout).
    total_timeout = connect_timeout + execute_timeout
    prefix = _pg_statement_timeout_prefix(execute_timeout)
    # EXPLAIN / SHOW can't live inside a subquery -> run raw, one text row per line.
    cleaned = _strip_leading_comments(sql).lstrip()
    m = _PG_TEXT_STMT_RE.match(cleaned)
    if m:
        rc, out, errout = run_psql_capture(url, prefix + sql, psql_vars=params,
                                            timeout=total_timeout, connect_timeout=connect_timeout)
        if rc != 0:
            msg, code = _psql_error_message(rc, errout)
            raise QuarryError(msg, exit_code=code)
        col = "QUERY PLAN" if m.group(1).lower() == "explain" else "output"
        rows = [{col: line} for line in out.rstrip("\n").splitlines()]
        return rows, len(out.encode("utf-8"))
    wrapped = wrap_for_json(sql)
    rc, out, errout = run_psql_capture(url, prefix + wrapped, psql_vars=params,
                                        timeout=total_timeout, connect_timeout=connect_timeout)
    if rc != 0:
        msg, code = _psql_error_message(rc, errout)
        raise QuarryError(msg, exit_code=code)
    text = out.strip() or "[]"
    try:
        rows = json.loads(text)
    except json.JSONDecodeError as exc:
        raise QuarryError(f"postgres returned non-JSON body: {text[:200]}", exit_code=EXIT_SQL_ERROR) from exc
    return rows, len(out.encode("utf-8"))


def _pg_column_types(url: str, sql: str, params: dict[str, str], timeout: int = 15) -> dict[str, str]:
    """Real result column types via psql \\gdesc (best-effort; {} on failure)."""
    probe = _strip_trailing_semicolons(sql) + "\n\\gdesc"
    rc, out, _ = run_psql_capture(url, probe, psql_vars=params, timeout=timeout)
    if rc != 0:
        return {}
    types: dict[str, str] = {}
    for line in out.strip().splitlines():
        if "|" in line:
            name, _, typ = line.partition("|")
            types[name.strip()] = typ.strip()
    return types


def run_query(
    conn: Connection,
    sql: str,
    *,
    params: dict[str, str] | None = None,
    allow_write: bool = False,
    max_rows: int | None = DEFAULT_MAX_ROWS,
    offset: int = 0,
    timeout: int | None = None,
    connect_timeout: int | None = None,
    default_timeout: int = DEFAULT_EXECUTE_TIMEOUT_SEC,
    with_types: bool = False,
    use_proxy: bool | None = None,
) -> QueryResult:
    """Run a query and return a structured QueryResult. The library entry point
    that the GUI and `--format json` rich mode use. Applies the safety rails and
    opens an SSH tunnel when the connection has ssh_host.

    Timeout resolution (issue #94): `timeout`, when given, wins outright
    (e.g. the CLI's explicit `--timeout`); otherwise it falls back through
    `QUARRY_TIMEOUT` -> the connection's configured `timeout` -> `default_timeout`
    (300s for CLI/GUI, 120s for MCP — see resolve_timeout()). Connection
    establishment (incl. SSH tunnel setup) uses the independent, shorter
    `connect_timeout` (default 15s) so an unreachable host fails fast.

    offset supports the grid's "load more" pagination: it's only honored when
    max_rows also auto-appends a LIMIT (i.e. the SQL has no explicit LIMIT of
    its own) — see enforce_safety.

    with_types=True fetches real result column types (PostgreSQL only, via \\gdesc);
    other engines leave column types null (the GUI infers from values)."""
    params = params or {}
    engine = connection_engine(conn)
    col_types: dict[str, str] = {}
    execute_timeout = resolve_timeout(conn, timeout, default=default_timeout)
    conn_timeout = connect_timeout if connect_timeout is not None else DEFAULT_CONNECT_TIMEOUT_SEC

    # Redis takes a command string, not SQL — use redis-specific safety.
    if engine == "redis":
        if not allow_write and not redis_engine.is_redis_read_only(sql):
            raise QuarryError(
                "blocked a redis write command (read-only by default; pass --write to allow)",
                exit_code=EXIT_SAFETY_BLOCKED,
            )
        start = time.monotonic()
        with tunnel.open_tunnel(conn, engine, connect_timeout=conn_timeout, use_proxy=use_proxy) as url:
            rows, download_bytes = redis_engine.run_redis(url, sql, timeout=execute_timeout)
        elapsed_ms = int((time.monotonic() - start) * 1000)
        applied_limit = max_rows
        size_is_estimated = True
    else:
        safe_sql, applied_limit = enforce_safety(
            sql, allow_write=allow_write, max_rows=max_rows, offset=offset
        )
        sql = safe_sql
        start = time.monotonic()
        with tunnel.open_tunnel(conn, engine, connect_timeout=conn_timeout, use_proxy=use_proxy) as url:
            if engine == "neptune":
                rows, download_bytes = run_neptune_cypher(
                    url, sql, params=params, timeout=execute_timeout, use_proxy=use_proxy,
                    workspace_home=_connection_workspace_home(conn))
                size_is_estimated = False
            elif engine == "mysql":
                rows, download_bytes = run_mysql_query(url, sql, params=params, timeout=execute_timeout,
                                                        connect_timeout=conn_timeout)
                size_is_estimated = True
            else:
                rows, download_bytes = _rows_postgres(url, sql, params, execute_timeout, connect_timeout=conn_timeout)
                size_is_estimated = True
                if with_types:
                    col_types = _pg_column_types(url, sql, params)
        elapsed_ms = int((time.monotonic() - start) * 1000)

    truncated = False
    if applied_limit is not None and len(rows) > applied_limit:
        rows = rows[:applied_limit]
        truncated = True

    columns = _columns_from_rows(rows)
    if col_types:
        for c in columns:
            c["type"] = col_types.get(c["name"])

    return QueryResult(
        columns=columns,
        rows=rows,
        row_count=len(rows),
        truncated=truncated,
        elapsed_ms=elapsed_ms,
        engine=engine,
        sql=sql,
        download_bytes=download_bytes,
        size_is_estimated=size_is_estimated,
    )


# ---------------------------------------------------------------------------
# Query-metadata cache (issue #97) — table lists, column metadata, health
# probes, shared by the GUI, CLI, and MCP so all three benefit from the same
# on-disk cache (see cache.py) instead of each re-querying the DB, which is
# often slow over an SSH tunnel.
#
# `tables:*`/`columns:*` entries have no expiry (they change only when the
# underlying schema does, so a fresh=1 call or the GUI's post-sync purge is
# what refreshes them). `health:*` entries carry a short TTL (HEALTH_TTL_SEC)
# *and* a connection_fingerprint: the fingerprint changing (a different URL,
# SSH bastion, or the SSH-proxy toggle) invalidates a cached probe result
# immediately, even from a brand-new CLI/MCP process that never saw the
# config change happen — the GUI additionally purges `health:*` proactively
# via its workspace file-watcher (see gui.py), so this is a backstop that
# specifically benefits the two faces without a long-lived watcher.
# ---------------------------------------------------------------------------

HEALTH_TTL_SEC = int(os.environ.get("QUARRY_HEALTH_TTL", "120"))


def connection_fingerprint(conn: Connection) -> str:
    """Short hash of everything that determines what a probe actually dials:
    the URL, SSH bastion settings, and the per-workspace SSH-proxy toggle
    (issue #98). Cached health results are stamped with this; a later read
    under a different fingerprint is treated as a miss (see cache.get)."""
    parts = [
        conn.url or "",
        conn.ssh_host or "",
        conn.ssh_user or "",
        conn.ssh_key or "",
        str(conn.ssh_port or ""),
        "proxy" if workspace.is_proxy_enabled(_connection_workspace_home(conn)) else "noproxy",
    ]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]


def _health_fresh_enough(c: dict) -> bool:
    """A cached health entry is usable if it is younger than HEALTH_TTL_SEC.
    Legacy entries without a timestamp are treated as expired (re-probed)."""
    ts = c.get("_ts")
    return isinstance(ts, (int, float)) and (time.time() - ts) < HEALTH_TTL_SEC


def _put_health(key: str, fingerprint: str, value: dict) -> dict:
    """Persist a health result with a timestamp + fingerprint; return it
    without either (callers see only {ok, error})."""
    cache.put(key, {**value, "_ts": time.time()}, fingerprint=fingerprint)
    return value


def cached_health(conn: Connection, db: str, env: str | None, *,
                   fresh: bool = False, cached_only: bool = False) -> dict:
    """Fast connectivity probe. Never raises — returns {ok, error}. Cached for
    HEALTH_TTL_SEC (default 120s) so reloads paint dots instantly but a
    transient failure self-heals; also invalidated the moment the resolved
    connection's fingerprint changes (URL/SSH/proxy-toggle edits). cached_only
    =True returns a still-fresh cache entry or {ok: None} without probing
    (used to paint dots instantly on page load)."""
    key = f"health:{db}@{env}"
    fp = connection_fingerprint(conn)
    c = cache.get(key, fingerprint=fp)
    if not fresh and c is not None and _health_fresh_enough(c):
        return {k: v for k, v in c.items() if k not in ("_ts", "_fp")}
    if cached_only:
        return {"ok": None}
    try:
        engine = connection_engine(conn)
        with tunnel.open_tunnel(conn, engine) as url:
            if engine == "redis":
                redis_engine.run_redis(url, "PING", timeout=6)
            elif engine == "mysql":
                run_mysql_query(url, "SELECT 1", timeout=6)
            elif engine == "neptune":
                run_neptune_cypher(url, "RETURN 1 AS ok", timeout=6,
                                   workspace_home=_connection_workspace_home(conn))
            else:
                rc, _out, e = run_psql_capture(url, "SELECT 1", timeout=6)
                if rc != 0:
                    return _put_health(key, fp, {"ok": False, "error": (e.strip() or "connect failed")[:200]})
        return _put_health(key, fp, {"ok": True})
    except Exception as e:  # noqa: BLE001
        return _put_health(key, fp, {"ok": False, "error": str(e)[:200]})


def _list_tables(conn: Connection, *, default_timeout: int = DEFAULT_EXECUTE_TIMEOUT_SEC) -> list[str]:
    engine = connection_engine(conn)
    if engine == "redis":
        with tunnel.open_tunnel(conn, engine) as url:
            return redis_engine.scan_keys(url, count=1000)
    if engine == "mysql":
        # alias AS table_name: MySQL 8 returns information_schema headers uppercase.
        sql = ("SELECT table_name AS table_name FROM information_schema.tables "
               "WHERE table_schema = DATABASE() ORDER BY table_name")
    elif engine == "neptune":
        return []
    else:
        sql = (
            "SELECT CASE "
            "WHEN table_schema = 'public' AND position('.' in table_name) = 0 THEN table_name "
            "ELSE quote_ident(table_schema) || '.' || quote_ident(table_name) "
            "END AS table_name FROM information_schema.tables "
            "WHERE table_schema NOT IN ('pg_catalog', 'information_schema') "
            "AND table_schema NOT LIKE 'pg_%' "
            "ORDER BY table_schema, table_name"
        )
    res = run_query(conn, sql, max_rows=5000, default_timeout=default_timeout)
    return [r.get("table_name") for r in res.rows if r.get("table_name")]


def cached_tables(conn: Connection, db: str, env: str | None, *, fresh: bool = False,
                   default_timeout: int = DEFAULT_EXECUTE_TIMEOUT_SEC) -> dict:
    """Table (or, for redis, key) list for one db@env, cached with no expiry
    (schema changes are rare; a fresh=1 call or a local-sync's targeted purge
    is what refreshes an entry)."""
    key = f"tables:{db}@{env}"
    if not fresh:
        c = cache.get(key)
        if c is not None:
            return {**c, "_cached": True}
    engine = connection_engine(conn)
    if engine == "redis":
        with tunnel.open_tunnel(conn, engine) as url:
            ks = redis_engine.keys_with_meta(url, cap=400)
            # `capped` tells the caller the key list was cut off (never silently truncate)
            out = {"engine": "redis", "keys": ks, "capped": len(ks) >= 400}
    else:
        ts = _list_tables(conn, default_timeout=default_timeout)
        # `capped` tells the caller the list hit _list_tables' 5000-row cap
        out = {"tables": ts, "engine": engine, "capped": len(ts) >= 5000}
    cache.put(key, out)
    return {**out, "_cached": False}


def cached_columns(conn: Connection, db: str, env: str | None, table: str, *,
                    fresh: bool = False, raise_errors: bool = False,
                    default_timeout: int = DEFAULT_EXECUTE_TIMEOUT_SEC) -> dict:
    """Column metadata for one table — column_name, data_type, is_nullable,
    column_default, character_maximum_length — as {"rows": [...]}, cached by
    db@env:table. The table name is matched via a bound `:'table'` query
    parameter (psql -v for postgres, our own quote_val escaping for mysql)
    rather than a character-stripping sanitizer, so quoted/special-character
    table names still work.

    By default (raise_errors=False, the GUI's use case) returns {"rows": []}
    for engines with no relational schema (redis/neptune), for a blank table
    name, or on any query error — a schema-panel fetch must never crash the
    page. raise_errors=True (the CLI's use case) instead lets a query failure
    propagate as the QuarryError it already is, so `qy describe-table` keeps
    reporting the real connection/SQL error instead of an empty result."""
    if not (table or "").strip():
        return {"rows": []}
    key = f"columns:{db}@{env}:{table}"
    if not fresh:
        c = cache.get(key)
        if c is not None:
            if "rows" in c:
                return c
            legacy_cols = c.get("columns")
            if isinstance(legacy_cols, list):
                # pre-#97 GUI-only cache entry: {"columns": [...], "types": {...}}
                # instead of {"rows": [...]}. Reconstruct as many rows fields as
                # the legacy shape carries (data_type only) rather than treating
                # an existing gui-cache.json as a miss, then re-persist in the
                # canonical shape so later reads skip this translation.
                legacy_types = c.get("types") or {}
                rows = [
                    {"column_name": name, "data_type": legacy_types.get(name),
                     "is_nullable": None, "column_default": None,
                     "character_maximum_length": None}
                    for name in legacy_cols
                ]
                return cache.put(key, {"rows": rows})
    try:
        engine = connection_engine(conn)
        if engine in ("redis", "neptune"):
            return cache.put(key, {"rows": []})
        if engine == "mysql":
            sql = (
                "SELECT column_name, data_type, is_nullable, column_default, "
                "character_maximum_length FROM information_schema.columns "
                "WHERE table_schema = DATABASE() AND table_name = :'table' "
                "ORDER BY ordinal_position"
            )
        elif "." in table:
            sql = (
                "WITH target AS (SELECT parse_ident(:'table') AS parts) "
                "SELECT column_name, data_type, is_nullable, column_default, "
                "character_maximum_length FROM information_schema.columns, target "
                "WHERE table_schema = parts[cardinality(parts) - 1] "
                "AND table_name = parts[cardinality(parts)] "
                "ORDER BY ordinal_position"
            )
        else:
            sql = (
                "SELECT column_name, data_type, is_nullable, column_default, "
                "character_maximum_length FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = :'table' "
                "ORDER BY ordinal_position"
            )
        res = run_query(conn, sql, params={"table": table}, max_rows=2000,
                        default_timeout=default_timeout)
        return cache.put(key, {"rows": res.rows})
    except Exception:  # noqa: BLE001
        if raise_errors:
            raise
        return {"rows": []}


# ---------------------------------------------------------------------------
# Output formatting (CLI presentation)
# ---------------------------------------------------------------------------

def rows_to_csv(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return ""
    fieldnames: list[str] = []
    for row in rows:                       # union of keys — rows may be heterogeneous
        for k in row:
            if k not in fieldnames:
                fieldnames.append(k)
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames, restval="", extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue()


def _csv_limit(text: str, n: int) -> str:
    """Keep the header + first `n` data rows of CSV text (quote-safe)."""
    parsed = list(csv.reader(io.StringIO(text)))
    if not parsed:
        return text
    buf = io.StringIO()
    csv.writer(buf).writerows(parsed[: 1 + n])
    return buf.getvalue()


def emit_rows_json(rows: list[dict[str, Any]]) -> None:
    if sys.stdout.isatty():
        json.dump(rows, sys.stdout, indent=2, ensure_ascii=False)
    else:
        json.dump(rows, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")


def emit_rows_ndjson(rows: list[dict[str, Any]]) -> None:
    for row in rows:
        json.dump(row, sys.stdout, ensure_ascii=False)
        sys.stdout.write("\n")


def emit_csv(stdout_text: str) -> None:
    sys.stdout.write(stdout_text)
    if not stdout_text.endswith("\n"):
        sys.stdout.write("\n")


def emit_table(stdout_text: str) -> None:
    reader = csv.reader(io.StringIO(stdout_text))
    rows = list(reader)
    if not rows:
        return
    widths = [max(len(row[i]) if i < len(row) else 0 for row in rows) for i in range(len(rows[0]))]
    sep = "  "
    for idx, row in enumerate(rows):
        padded = [row[i].ljust(widths[i]) if i < len(row) else "".ljust(widths[i]) for i in range(len(rows[0]))]
        print(sep.join(padded))
        if idx == 0:
            print(sep.join("-" * w for w in widths))


def emit_rows_csv(rows: list[dict[str, Any]]) -> None:
    emit_csv(rows_to_csv(rows))


def emit_rows_table(rows: list[dict[str, Any]]) -> None:
    emit_table(rows_to_csv(rows))


def emit_json(stdout_text: str) -> None:
    text = stdout_text.strip() or "[]"
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        sys.stdout.write(text + "\n")
        return
    if sys.stdout.isatty():
        json.dump(data, sys.stdout, indent=2, ensure_ascii=False)
        sys.stdout.write("\n")
    else:
        json.dump(data, sys.stdout, ensure_ascii=False)
        sys.stdout.write("\n")


def emit_ndjson(stdout_text: str) -> None:
    text = stdout_text.strip() or "[]"
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        sys.stdout.write(text + "\n")
        return
    if not isinstance(data, list):
        data = [data]
    for row in data:
        json.dump(row, sys.stdout, ensure_ascii=False)
        sys.stdout.write("\n")


# ---------------------------------------------------------------------------
# Param resolution
# ---------------------------------------------------------------------------

def parse_kv_args(items: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in items:
        if "=" not in item:
            err(f"invalid param '{item}', expected key=value", exit_code=EXIT_USAGE)
        k, _, v = item.partition("=")
        out[k.strip()] = v
    return out


def resolve_params(query: Query, provided: dict[str, str]) -> dict[str, str]:
    resolved: dict[str, str] = {}
    declared = {p.name: p for p in query.params}
    for name, p in declared.items():
        if name in provided:
            resolved[name] = provided[name]
        elif p.default is not None:
            resolved[name] = p.default
        elif p.required:
            err(f"missing required param '{name}'", exit_code=EXIT_USAGE)
    for name, val in provided.items():
        if name not in resolved:
            resolved[name] = val
    return resolved


# ---------------------------------------------------------------------------
# execute_sql — CLI path (keeps psql COPY for csv/table; faithful to dbq)
# ---------------------------------------------------------------------------

def _emit_rows(rows: list[dict[str, Any]], fmt: str) -> int:
    if fmt == "json":
        emit_rows_json(rows)
    elif fmt == "ndjson":
        emit_rows_ndjson(rows)
    elif fmt == "csv":
        emit_rows_csv(rows)
    elif fmt == "table":
        emit_rows_table(rows)
    else:
        err(f"unknown format: {fmt}", exit_code=EXIT_USAGE)
    return EXIT_OK


def execute_sql(
    *,
    conn: Connection,
    sql: str,
    psql_vars: dict[str, str],
    fmt: str,
    allow_write: bool = False,
    max_rows: int | None = None,
    timeout: int | None = None,
    connect_timeout: int | None = None,
    use_proxy: bool | None = None,
    stats: dict[str, Any] | None = None,
) -> int:
    """`stats`, when given (issue #104), is filled in with `elapsed_ms`,
    `download_bytes`, and `size_is_estimated` for this execution — the CLI's
    execute path has its own return contract (an exit code, not a
    QueryResult) and printed output, so those fields (same semantics/naming
    as QueryResult, see run_query) are surfaced via this optional out-param
    instead of changing either."""
    engine = connection_engine(conn)
    execute_timeout = resolve_timeout(conn, timeout, default=DEFAULT_EXECUTE_TIMEOUT_SEC)
    conn_timeout = connect_timeout if connect_timeout is not None else DEFAULT_CONNECT_TIMEOUT_SEC
    start = time.monotonic()

    def _finish(exit_code: int, download_bytes: int) -> int:
        if stats is not None:
            stats["elapsed_ms"] = int((time.monotonic() - start) * 1000)
            stats["download_bytes"] = download_bytes
            # Same rule as QueryResult.size_is_estimated: only neptune's byte
            # count is exact (raw HTTP response body); others approximate it.
            stats["size_is_estimated"] = engine != "neptune"
        return exit_code

    if engine == "redis":
        if not allow_write and not redis_engine.is_redis_read_only(sql):
            raise QuarryError(
                "blocked a redis write command (read-only by default; pass --write to allow)",
                exit_code=EXIT_SAFETY_BLOCKED,
            )
        with tunnel.open_tunnel(conn, engine, connect_timeout=conn_timeout, use_proxy=use_proxy) as url:
            rows, download_bytes = redis_engine.run_redis(url, sql, timeout=execute_timeout)
        return _finish(_emit_rows(rows, fmt), download_bytes)

    safe_sql, applied_limit = enforce_safety(sql, allow_write=allow_write, max_rows=max_rows)

    if engine in ("neptune", "mysql"):
        with tunnel.open_tunnel(conn, engine, connect_timeout=conn_timeout, use_proxy=use_proxy) as url:
            rows, download_bytes = (
                run_neptune_cypher(url, safe_sql, params=psql_vars, timeout=execute_timeout, use_proxy=use_proxy,
                                   workspace_home=_connection_workspace_home(conn))
                if engine == "neptune"
                else run_mysql_query(url, safe_sql, params=psql_vars, timeout=execute_timeout,
                                     connect_timeout=conn_timeout))
        if applied_limit is not None and len(rows) > applied_limit:
            rows = rows[:applied_limit]           # drop the +1 truncation-probe row
        return _finish(_emit_rows(rows, fmt), download_bytes)

    total_timeout = conn_timeout + execute_timeout
    prefix = _pg_statement_timeout_prefix(execute_timeout)
    with tunnel.open_tunnel(conn, engine, connect_timeout=conn_timeout, use_proxy=use_proxy) as url:
        if fmt in ("json", "ndjson"):
            rc, out, errout = run_psql_capture(url, prefix + wrap_for_json(safe_sql), psql_vars=psql_vars,
                                               timeout=total_timeout, connect_timeout=conn_timeout)
            if rc != 0:
                msg, code = _psql_error_message(rc, errout)
                err(msg, exit_code=code)
            download_bytes = len(out.encode("utf-8"))
            if applied_limit is not None:
                data = json.loads(out.strip() or "[]")
                data = data[:applied_limit] if isinstance(data, list) else data
                emit_rows_json(data) if fmt == "json" else emit_rows_ndjson(data)
            else:
                emit_json(out) if fmt == "json" else emit_ndjson(out)
            return _finish(EXIT_OK, download_bytes)
        if fmt in ("csv", "table"):
            rc, out, errout = run_psql_capture(url, prefix + wrap_for_csv(safe_sql), psql_vars=psql_vars,
                                               timeout=total_timeout, connect_timeout=conn_timeout)
            if rc != 0:
                msg, code = _psql_error_message(rc, errout)
                err(msg, exit_code=code)
            download_bytes = len(out.encode("utf-8"))
            if applied_limit is not None:
                out = _csv_limit(out, applied_limit)
            emit_csv(out) if fmt == "csv" else emit_table(out)
            return _finish(EXIT_OK, download_bytes)
    err(f"unknown format: {fmt}", exit_code=EXIT_USAGE)
    return EXIT_USAGE


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _dummy_value_for(p: Param) -> str:
    t = p.type.lower()
    if t in ("uuid",):
        return "00000000-0000-0000-0000-000000000000"
    if t in ("int", "integer", "bigint", "smallint"):
        return "0"
    if t in ("float", "real", "double", "numeric", "decimal"):
        return "0"
    if t in ("bool", "boolean"):
        return "false"
    if t in ("timestamp", "timestamptz", "date", "time"):
        return "1970-01-01"
    return ""


def validate_query(q: Query, conn: Connection, *, use_proxy: bool | None = None) -> int:
    psql_vars: dict[str, str] = {}
    for p in q.params:
        psql_vars[p.name] = p.default if p.default is not None else _dummy_value_for(p)

    engine = connection_engine(conn)
    # Validation must be side-effect-free: a multi-statement or data-modifying
    # body would otherwise execute its writes under `EXPLAIN <body>`.
    ok = redis_engine.is_redis_read_only(q.sql) if engine == "redis" else is_read_only(q.sql)
    if not ok:
        err("validation failed: query is not read-only (writes/DDL or multiple statements)",
            exit_code=EXIT_SAFETY_BLOCKED)
        return EXIT_SAFETY_BLOCKED

    explain_sql = "EXPLAIN " + _strip_trailing_semicolons(q.sql)
    try:
        with tunnel.open_tunnel(conn, engine, use_proxy=use_proxy) as url:
            if engine == "redis":
                redis_engine.run_redis(url, q.sql, timeout=20)
            elif engine == "neptune":
                run_neptune_cypher(url, q.sql, params=psql_vars, timeout=20, use_proxy=use_proxy,
                                   workspace_home=_connection_workspace_home(conn))
            elif engine == "mysql":
                run_mysql_query(url, explain_sql, params=psql_vars, timeout=20)
            else:
                rc, _out, errout = run_psql_capture(url, explain_sql, psql_vars=psql_vars, timeout=20)
                if rc != 0:
                    err(f"validation failed: {errout.strip()}")
                    return EXIT_SQL_ERROR
    except Exception as exc:
        err(f"validation failed: {exc}")
        return EXIT_SQL_ERROR
    return EXIT_OK
