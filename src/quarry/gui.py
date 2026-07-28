"""Quarry GUI — a local, zero-dependency data viewer (stdlib http.server).

One more *face* over the core engine: browse connections grouped into project
folders and env-sets, pick a table (or Redis key), run read-only SQL, and read
a polished data grid. Slate & Copper theme, light/dark.

Launch:  qy gui            (or: python -m quarry.gui)
"""

from __future__ import annotations

import json
import logging
import mimetypes
import os
import queue
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

from . import __version__, cache, core, keepalive, local, local_sync, proxy, redis_engine, tunnel, workspace
from .core import QuarryError

log = logging.getLogger("quarry.gui")

_WEB_DIST = Path(__file__).resolve().parent / "web_dist"


def _setup_logging() -> None:
    if log.handlers:
        return
    h = logging.StreamHandler(sys.stderr)
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)-5s %(message)s", "%H:%M:%S"))
    log.addHandler(h)
    log.setLevel(logging.INFO)

# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

# Server-side cache so table lists + health survive browser reloads AND `qy gui`
# restarts. Storage (in-memory dict + on-disk JSON at ~/.cache/quarry/gui-cache.json)
# lives in cache.py (issue #97) so the CLI and MCP share the same cache instead
# of each re-querying the DB on every invocation.


# ---------------------------------------------------------------------------
# Events (SSE) — the GUI's change-notification contract
# ---------------------------------------------------------------------------
# One channel, `GET /api/events`, streams JSON events of the shape
# {"type": <str>, "ts": <epoch seconds>}. Events are *hints to refetch*, never
# data carriers — losing one is harmless, so there is no replay/Last-Event-ID.
#
# Event types (the contract consumed by web/src/useEvents.ts and by future
# features building on this channel):
#   workspace_changed — a watched workspace file (config.toml, any
#                       connections.toml, any queries/**/*.sql) changed on
#                       disk; clients should refetch connections + queries.
#   update_available  — the background update checker (below) found a newer
#                       quarry-db release on PyPI; clients should refetch
#                       GET /api/update to paint the header badge.
#
# A comment line (`: hb`) is sent every HEARTBEAT_SEC as keep-alive; clients
# also use the EventSource auto-reconnect that follows a server restart to
# re-check /api/version and prompt a page reload after an upgrade.

HEARTBEAT_SEC = float(os.environ.get("QUARRY_EVENTS_HEARTBEAT", "15"))
WATCH_INTERVAL_SEC = float(os.environ.get("QUARRY_WATCH_INTERVAL", "2"))

_SUBSCRIBERS: set[queue.Queue] = set()
_SUB_LOCK = threading.Lock()
_WATCHER_STARTED = False


def publish_event(type_: str) -> None:
    evt = {"type": type_, "ts": time.time()}
    with _SUB_LOCK:
        subs = list(_SUBSCRIBERS)
    for q in subs:
        try:
            q.put_nowait(evt)
        except queue.Full:  # slow consumer: drop — events are refetch hints
            pass


def _sse_format(evt: dict) -> bytes:
    return f"data: {json.dumps(evt, ensure_ascii=False)}\n\n".encode("utf-8")


def _close_event_streams() -> None:
    """Ask every open SSE handler to end its stream (each blocks on its own
    queue, so a normal event is the only way to wake them). Process exit does
    this implicitly for `qy gui`; serve() and tests use it for a clean stop."""
    publish_event("_close")


def _ws_fingerprint() -> dict[str, float]:
    """mtime of every watched file: the workspace-list config plus each
    workspace's connections.toml and queries/**/*.sql. Dict compare catches
    edits, additions, and deletions alike."""
    fp: dict[str, float] = {}
    paths = [workspace._config_path()]
    for w in workspace.WS_LIST:
        paths.append(w.connections_file)
        try:
            paths.extend(sorted(w.queries_dir.rglob("*.sql")))
        except OSError:
            pass
    for p in paths:
        try:
            fp[str(p)] = p.stat().st_mtime
        except OSError:
            continue
    return fp


def _apply_workspace_change() -> None:
    """React to an on-disk workspace change: re-resolve the workspace list
    (config.toml may have changed), drop health cache entries (connection URLs
    may now differ, making cached probe results lies), and notify clients.
    Table/column caches survive — they are keyed by db@env and refresh via the
    existing fresh=1 path."""
    workspace.reload_workspace()
    cache.drop_prefix("health:")
    publish_event("workspace_changed")


def _watch_tick(prev: dict[str, float]) -> dict[str, float]:
    """One watcher iteration: compare fingerprints, apply on change. Split out
    of the loop so tests can drive it deterministically."""
    cur = _ws_fingerprint()
    if cur != prev:
        try:
            _apply_workspace_change()
        except Exception:  # noqa: BLE001 — watcher must survive bad configs
            log.exception("workspace watcher: reload failed")
    return cur


def _watch_loop() -> None:  # pragma: no cover — thread wrapper around _watch_tick
    fp = _ws_fingerprint()
    while True:
        time.sleep(WATCH_INTERVAL_SEC)
        fp = _watch_tick(fp)


def _ensure_watcher() -> None:
    """Start the (single, daemon) file watcher lazily on first SSE subscriber —
    no client listening means nobody to notify, so no thread until then."""
    global _WATCHER_STARTED
    with _SUB_LOCK:
        if _WATCHER_STARTED:
            return
        _WATCHER_STARTED = True
    threading.Thread(target=_watch_loop, name="quarry-gui-watcher", daemon=True).start()


# ---------------------------------------------------------------------------
# Update check — PyPI polling for a newer quarry-db release
# ---------------------------------------------------------------------------
# A background daemon thread, throttled to once per UPDATE_CHECK_INTERVAL_SEC
# (default 24h, tracked via a `checked_at` timestamp persisted in gui-cache so
# the throttle survives `qy gui` restarts), asks PyPI's JSON API for the
# latest quarry-db release. A newer version publishes `update_available` over
# /api/events; GET /api/update reports the last-known state for the header's
# first paint (before any event fires).
#
# Silent by design: QUARRY_UPDATE_CHECK=0, an editable/dev install (nothing to
# `pipx upgrade`), or any network failure all mean "no update" with nothing
# surfaced anywhere — this channel must never become noise or a false alarm.

PYPI_URL = os.environ.get("QUARRY_PYPI_URL", "https://pypi.org/pypi/quarry-db/json")
UPDATE_CHECK_INTERVAL_SEC = float(os.environ.get("QUARRY_UPDATE_INTERVAL", str(24 * 3600)))
UPDATE_LOOP_SLEEP_SEC = float(os.environ.get("QUARRY_UPDATE_LOOP_SLEEP", "3600"))
_UPDATE_CACHE_KEY = "update_check"
_UPDATE_CHECKER_STARTED = False
_UPDATE_CHECKER_LOCK = threading.Lock()


def _update_check_disabled() -> bool:
    return os.environ.get("QUARRY_UPDATE_CHECK", "") == "0"


def _is_editable_install() -> bool:
    """True for `pip install -e` / source-checkout installs — an editable
    install has no PyPI wheel to upgrade into, so checking is pointless noise
    for contributors. Detected via the `direct_url.json` dist-info metadata
    pip writes for such installs (`dir_info.editable: true`); a normal PyPI
    install has no direct_url.json at all."""
    try:
        import importlib.metadata as importlib_metadata

        dist = importlib_metadata.distribution("quarry-db")
        raw = dist.read_text("direct_url.json")
        if not raw:
            return False
        info = json.loads(raw)
        return bool(info.get("dir_info", {}).get("editable"))
    except Exception:  # noqa: BLE001 — never let detection break the checker
        return False


def _parse_version(v: str) -> tuple[int, ...]:
    """Numeric dot-segments only (ignores any pre-release suffix) — enough to
    compare quarry-db's plain MAJOR.MINOR.PATCH releases by segment rather
    than as strings (so 0.10.0 > 0.9.0). Never raises on odd input."""
    out = []
    for part in (v or "").split("."):
        m = re.match(r"\d+", part)
        out.append(int(m.group()) if m else 0)
    return tuple(out)


def _version_gt(a: str, b: str) -> bool:
    return _parse_version(a) > _parse_version(b)


def _fetch_latest_version(timeout: float = 5.0) -> str | None:
    """The latest version PyPI reports for quarry-db, or None on ANY failure
    (network, timeout, bad JSON) — callers must treat None as "couldn't check
    this time", never as an error to surface."""
    try:
        req = Request(PYPI_URL, headers={"Accept": "application/json"})
        with urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        v = data.get("info", {}).get("version")
        return v if isinstance(v, str) and v else None
    except Exception:  # noqa: BLE001 — silent by design, see module docstring
        return None


def _check_for_update(force: bool = False) -> None:
    """One throttled check: skip entirely if disabled/editable; skip the PyPI
    call if the last check is still within the interval (unless forced);
    otherwise fetch + cache the result and publish an event if a newer
    release appeared. `checked_at` advances on every real attempt (even a
    failed one) so a PyPI outage can't turn into a retry storm."""
    if _update_check_disabled() or _is_editable_install():
        return
    c = cache.get(_UPDATE_CACHE_KEY) or {}
    last = c.get("checked_at")
    if not force and isinstance(last, (int, float)) and (time.time() - last) < UPDATE_CHECK_INTERVAL_SEC:
        return
    latest = _fetch_latest_version()
    now = time.time()
    if latest is None:
        cache.put(_UPDATE_CACHE_KEY, {**c, "checked_at": now})
        return
    available = _version_gt(latest, __version__)
    cache.put(_UPDATE_CACHE_KEY, {"checked_at": now, "latest": latest, "available": available})
    if available:
        publish_event("update_available")


def _update_loop() -> None:  # pragma: no cover — thread wrapper around _check_for_update
    while True:
        try:
            _check_for_update()
        except Exception:  # noqa: BLE001 — checker must survive a bad response
            log.exception("update checker: check failed")
        time.sleep(UPDATE_LOOP_SLEEP_SEC)


def _ensure_update_checker() -> None:
    """Start the (single, daemon) update-check thread. Unlike the workspace
    watcher this isn't gated on an SSE subscriber — GET /api/update must have
    something to report on the very first page load."""
    global _UPDATE_CHECKER_STARTED
    with _UPDATE_CHECKER_LOCK:
        if _UPDATE_CHECKER_STARTED:
            return
        _UPDATE_CHECKER_STARTED = True
    threading.Thread(target=_update_loop, name="quarry-gui-update-checker", daemon=True).start()


def api_update() -> dict:
    """Last-known update-check state — never triggers a network call itself
    (that's the background thread's job); this just reads the cache. The
    cached `available` flag is never trusted directly: it's re-derived from
    `latest` vs. the *current* __version__ on every call, so a value cached
    before an upgrade (or before QUARRY_UPDATE_CHECK=0 was set) can't outlive
    its relevance and keep showing a stale badge."""
    if _update_check_disabled() or _is_editable_install():
        return {"current": __version__, "latest": None, "available": False}
    c = cache.get(_UPDATE_CACHE_KEY) or {}
    latest = c.get("latest")
    return {
        "current": __version__,
        "latest": latest,
        "available": bool(latest) and _version_gt(latest, __version__),
    }


# ---------------------------------------------------------------------------
# What's New — CHANGELOG.md parsing for the header's "what changed since you
# last looked" panel (companion to the update-check badge above: that one is
# about a newer PyPI release you haven't installed yet, this one is about
# what the release you're ALREADY running actually changed).
# ---------------------------------------------------------------------------
# CHANGELOG.md ships next to this file in standard wheels (see hatch_build.py
# — packages=["src/quarry"] alone wouldn't reach a repo-root file); an
# editable/source-checkout install deliberately has no such copy (a bundled
# one would shadow the editable redirect), so fall back to the repo root two
# levels up from src/quarry/gui.py.

CHANGELOG_MAX_VERSIONS = 20

# semantic-release's `## v1.2.3 (YYYY-MM-DD)` heading, and the older
# hand-written `## [1.2.3] — YYYY-MM-DD` form used before #74.
_CHANGELOG_HEADING_RE = re.compile(
    r"^## \[?v?(?P<version>\d+\.\d+\.\d+[\w.-]*)\]?\s*[(\u2014-]\s*"
    r"(?P<date>\d{4}-\d{2}-\d{2})\)?\s*$"
)
# A trailing parenthetical made entirely of markdown links — semantic-release's
# `([#77](url), [`sha`](url))` PR/commit footer — is internal bookkeeping, not
# release-note content, so it's stripped from each entry.
_CHANGELOG_META_RE = re.compile(r"\s*\((?:\[[^\]]+\]\([^)]+\)(?:,\s*)?)+\)\s*$")


def _changelog_path() -> Path:
    bundled = Path(__file__).resolve().parent / "CHANGELOG.md"
    if bundled.exists():
        return bundled
    return Path(__file__).resolve().parents[2] / "CHANGELOG.md"


def _parse_changelog(text: str) -> list[dict]:
    """Structured `{version, date, entries}` sections, newest first, one per
    released-version heading. The `## [Unreleased]` section (no version/date
    yet) is intentionally skipped — nothing has a __version__ to compare it
    against. Capped at CHANGELOG_MAX_VERSIONS: the panel only ever needs to
    bridge a user from their last-seen version to the current one, never the
    full history."""
    versions: list[dict] = []
    current: dict | None = None
    entry_lines: list[str] = []

    def flush_entry() -> None:
        if current is not None and entry_lines:
            joined = " ".join(entry_lines).strip()
            joined = _CHANGELOG_META_RE.sub("", joined)
            joined = joined.replace("**", "").strip()
            if joined:
                current["entries"].append(joined)
        entry_lines.clear()

    for line in text.splitlines():
        heading = _CHANGELOG_HEADING_RE.match(line)
        if heading:
            flush_entry()
            if len(versions) >= CHANGELOG_MAX_VERSIONS:
                break
            current = {"version": heading.group("version"), "date": heading.group("date"), "entries": []}
            versions.append(current)
            continue
        if line.startswith("## "):  # any other top-level heading (e.g. [Unreleased]) ends the section
            flush_entry()
            current = None
            continue
        if current is None:
            continue
        if line.startswith("- "):
            flush_entry()
            entry_lines.append(line[2:].strip())
        elif line.strip() and not line.startswith("#"):
            entry_lines.append(line.strip())
        elif not line.strip():
            flush_entry()
    flush_entry()
    return versions


def api_changelog() -> list[dict]:
    """Top CHANGELOG_MAX_VERSIONS released-version sections — an empty list
    (never an error) if CHANGELOG.md isn't present, e.g. a stripped-down
    install; this is cosmetic and must never break the page."""
    try:
        text = _changelog_path().read_text(encoding="utf-8")
    except OSError:
        return []
    return _parse_changelog(text)


def _resolve(db: str, env: str | None):
    return core.resolve_connection(db, env)


def _display_path(p) -> str:
    """Home-relative display path — keeps usernames out of the UI (and screenshots)."""
    s, home = str(p), str(Path.home())
    return "~" + s[len(home):] if s.startswith(home) else s


def api_connections() -> dict:
    homes = [_display_path(w.home) for w in workspace.WS_LIST]
    groups = core.group_connections()
    for g in groups:
        if g.get("ws"):
            g["ws"] = _display_path(g["ws"])
    # issue #101: attach an observed (not guessed) proxied flag per env, sourced
    # from the actual tunnel-decision logic rather than left for the frontend
    # to infer from workspace config alone.
    core.attach_proxy_status(groups)
    return {"groups": groups, "workspace": _display_path(workspace.WS.home), "workspaces": homes}


def api_workspaces() -> dict:
    """The config.toml-registered workspace list, for the header's manage-UI
    (issue #15 — that list used to be display-only). Mirrors `qy workspace
    list`: raw dirs as written, each flagged if missing or lacking a
    connections.toml, so a typo is visible before it silently contributes
    nothing.

    issue #101: also reports, per workspace, whether its proxy toggle is on
    (`qy proxy on|off`) and the currently discovered system/env proxy (if
    any) — one `discover_proxy()` call shared across all items, mirroring
    `core.attach_proxy_status`'s single-probe approach for connections."""
    discovered = proxy.discover_proxy()
    proxy_discovered = (
        {"host": discovered.host, "port": discovered.port, "source": discovered.source}
        if discovered else None
    )
    items = []
    for d in workspace.config_workspaces():
        home = Path(d).expanduser()
        items.append({
            "dir": d,
            "display": _display_path(home),
            "exists": home.exists(),
            "hasConnections": (home / "connections.toml").exists(),
            "proxyEnabled": workspace.is_proxy_enabled(home),
        })
    return {
        "config": _display_path(workspace._config_path()),
        "items": items,
        "proxyDiscovered": proxy_discovered,
    }


def api_keepalive() -> dict:
    """Workspace tunnel keep-alive status (same contract as `qy status --format json`)."""
    return keepalive.status(workspace.WS.home)


def api_keepalive_up() -> dict:
    keepalive.start(workspace.WS.home)
    return api_keepalive()


def api_keepalive_down() -> dict:
    keepalive.stop(workspace.WS.home)
    return api_keepalive()


def api_workspace_add(body: dict) -> dict:
    workspace.add_workspace(_req(body, "dir"))
    # reload_workspace(), not configure_workspace(None): the GUI is a long-lived
    # process that may have been launched with an explicit --workspace override,
    # which a hardcoded None would silently drop.
    workspace.reload_workspace()
    return api_workspaces()


def api_workspace_remove(body: dict) -> dict:
    d = _req(body, "dir")
    if not workspace.remove_workspace(d):
        raise QuarryError(f"workspace not found in config: {d}")
    workspace.reload_workspace()
    return api_workspaces()


def api_tables(db: str, env: str | None, fresh: bool = False) -> dict:
    conn = _resolve(db, env)
    return core.cached_tables(conn, db, env, fresh=fresh)


def api_columns(db: str, env: str | None, table: str) -> dict:
    """Column names + types for one table (postgres/mysql), cached. `columns`
    (a flat name list) powers editor autocomplete of `table.<col>`; `types`
    (name -> data_type) powers the sidebar's table-structure browser. Never
    raises — returns {columns: [], types: {}} on any miss.

    The table name is matched via a bound `:'table'` query parameter (psql -v
    for postgres, our own quote_val escaping for mysql) rather than a
    character-stripping sanitizer — a stripped name silently dropped legal
    quoted/special-character table names (e.g. `qy-review-weird`) that
    `/api/tables` had just listed, so clicking them showed an empty schema."""
    if not (table or "").strip():
        return {"columns": [], "types": {}}
    try:
        conn = _resolve(db, env)
        res = core.cached_columns(conn, db, env, table)
    except Exception:  # noqa: BLE001
        return {"columns": [], "types": {}}
    rows = res.get("rows", [])
    cols = [r.get("column_name") for r in rows if r.get("column_name")]
    types = {r.get("column_name"): r.get("data_type") for r in rows if r.get("column_name")}
    return {"columns": cols, "types": types}


def api_inspect(db: str, env: str | None, key: str) -> dict:
    if not key:
        raise QuarryError("inspect requires a 'key' parameter")
    conn = _resolve(db, env)
    engine = core.connection_engine(conn)
    if engine != "redis":
        raise QuarryError(f"inspect is redis-only (connection '{db}' is {engine})")
    with tunnel.open_tunnel(conn, engine) as url:
        rows = redis_engine.inspect_key(url, key)
    return {"columns": core._columns_from_rows(rows), "rows": rows, "rowCount": len(rows),
            "truncated": False, "elapsedMs": 0, "engine": "redis", "sql": f"# inspect {key}",
            "downloadBytes": 0, "sizeIsEstimated": True}


_URL_PASSWORD_RE = re.compile(r"(://[^/@?#]*?):[^/@?#]*@")


def _mask_url(url: str) -> str:
    """Mask the password in a connection URL — the info panel must never leak
    credentials into screenshots or screen shares."""
    return _URL_PASSWORD_RE.sub(r"\1:••••@", url)


def api_conninfo(db: str, env: str | None, reveal: bool = False) -> dict:
    """Resolved-connection details for the info panel: what quarry will actually
    dial for this db@env, and which file that came from. Diagnosing "why can't I
    connect" starts with seeing the config that is really in effect.

    reveal=True returns the URL with its password — the GUI is localhost-only
    and the value already lives in the user's own connections.toml; the mask is
    a screenshot/screen-share guard, not an access control."""
    conn = _resolve(db, env)
    p = urlparse(conn.url)
    file = workspace.WS.connections_file
    for w in workspace.WS_LIST:
        if str(w.home) == (conn.source or ""):
            file = w.connections_file
            break
    out = {
        "key": conn.key, "db": conn.logical_db, "env": conn.env,
        "engine": core.connection_engine(conn),
        "url": conn.url if reveal else _mask_url(conn.url),
        "host": p.hostname, "port": p.port,
        "database": (p.path or "").lstrip("/") or None,
        "group": conn.group, "region": conn.region, "notes": conn.notes,
        "file": _display_path(file), "tunnel": None,
    }
    if conn.ssh_host:
        out["tunnel"] = {"host": conn.ssh_host, "user": conn.ssh_user,
                         "port": conn.ssh_port or 22, "key": conn.ssh_key}
    return out


def api_local_up(body: dict) -> dict:
    """GUI counterpart of `qy local up <db>`: start the shared local container
    for the env-set's engine and register an env=local connection."""
    db = _req(body, "db")
    conns = core.load_connections()
    members = {(c.env or ""): c for c in conns.values() if c.logical_db == db}
    if not members:
        raise QuarryError(f"unknown connection '{db}'")
    src = (members.get(core.DEFAULT_ENV)
           or next((m for e, m in sorted(members.items()) if e.lower() != local.LOCAL_ENV),
                   members[sorted(members)[0]]))
    engine = core.connection_engine(src)
    if engine not in local.SPECS:
        raise QuarryError(
            f"engine '{engine}' has no local-container support (postgres/redis only)")
    if not local.SAFE_DB_RE.match(db):
        raise QuarryError(f"'{db}' is not a valid local db name")
    spec = local.SPECS[engine]
    state = local.start_container(spec, image=local.stored_local_image(db))
    redis_db = local.source_redis_db(db) if engine == "redis" else None
    key, created = local.register_local_connection(
        db, spec, group=src.group, redis_db=redis_db)
    out = {"key": key, "created": created, "engine": engine,
           "state": state, "port": spec.port}
    if engine == "postgres":
        if not local.wait_pg_ready(spec):
            raise QuarryError("local postgres did not become ready in time")
        local.ensure_pg_database(spec, db)
        # First-time convenience: a freshly registered local env is an empty
        # shell — fill it from the remote sibling right away. A sync failure
        # must not undo the successful `up`; it is reported alongside.
        src_env = (src.env or "").lower()
        if created and src_env and src_env != local.LOCAL_ENV:
            try:
                res = api_local_sync({"db": db, "from": src.env})
                out["synced_from"] = res["from"]
            except Exception as e:  # noqa: BLE001
                out["sync_error"] = str(e)
    return out


def api_local_sync(body: dict) -> dict:
    """GUI counterpart of `qy local sync <db> [--from env]`. All safety gates
    (env=local + loopback host + no tunnel, postgres-only) live in sync_schema."""
    db = _req(body, "db")
    from_env = body.get("from") or core.DEFAULT_ENV
    res = local_sync.sync_schema(db, from_env=from_env)
    # the swapped-in database invalidates cached table lists for this env-set
    cache.drop_prefix(f"tables:{db}@")
    return {**res, "from": from_env}


def api_health(db: str, env: str | None, fresh: bool = False, cached_only: bool = False) -> dict:
    try:
        conn = _resolve(db, env)
    except Exception as e:  # noqa: BLE001 — an unresolvable connection is a health failure, not a crash
        return {"ok": False, "error": str(e)[:200]}
    return core.cached_health(conn, db, env, fresh=fresh, cached_only=cached_only)


def api_queries() -> list[dict]:
    return [{"name": q.name, "db": q.db, "desc": q.desc, "sql": q.sql,
             "params": [{"name": p.name, "type": p.type, "required": p.required, "default": p.default}
                        for p in q.params]}
            for q in core.list_all_queries()]


def _req(body: dict, field: str):
    val = body.get(field)
    if val is None or val == "":
        raise QuarryError(f"missing required field '{field}'")
    return val


def _max_rows(body: dict) -> int:
    try:
        return int(body.get("maxRows") or 500)
    except (TypeError, ValueError):
        raise QuarryError(f"maxRows must be an integer, got {body.get('maxRows')!r}")


def _offset(body: dict) -> int:
    try:
        return int(body.get("offset") or 0)
    except (TypeError, ValueError):
        raise QuarryError(f"offset must be an integer, got {body.get('offset')!r}")


def api_query(body: dict) -> dict:
    conn = _resolve(_req(body, "db"), body.get("env"))
    res = core.run_query(
        conn, _req(body, "sql"), max_rows=_max_rows(body), offset=_offset(body), with_types=True
    )
    return res.to_dict()


def api_run(body: dict) -> dict:
    q = core.load_query(_req(body, "name"))
    conn = _resolve(q.db, body.get("env"))
    params = core.resolve_params(q, body.get("params") or {})
    res = core.run_query(
        conn, q.sql, params=params, max_rows=_max_rows(body), offset=_offset(body), with_types=True
    )
    out = res.to_dict()
    # A saved query runs on its OWN connection (q.db), which may differ from the
    # tab that launched it. Report the producing connection so the client tags &
    # persists the result under it instead of the tab's current connection.
    out["db"] = conn.logical_db
    out["env"] = conn.env
    return out


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _local_origin_ok(self) -> bool:
        """Reject DNS-rebinding (foreign Host) and cross-site XHR (foreign Origin).
        The GUI is localhost-only and unauthenticated — without this, any web page
        the user visits could POST queries to the local API."""
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
        if host not in _LOCAL_HOSTS:
            return False
        origin = self.headers.get("Origin")
        if origin:
            oh = urlparse(origin).hostname or ""
            if oh not in _LOCAL_HOSTS:
                return False
        return True

    def _send(self, code, payload, content_type="application/json"):
        data = (json.dumps(payload, ensure_ascii=False).encode("utf-8")
                if content_type == "application/json"
                else (payload.encode("utf-8") if isinstance(payload, str) else payload))
        self.send_response(code)
        self.send_header("Content-Type", content_type + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _send_file(self, code: int, path: Path) -> None:
        data = path.read_bytes()
        mime, _ = mimetypes.guess_type(str(path))
        content_type = mime or "application/octet-stream"
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _serve_events(self) -> None:
        """SSE stream (see the Events contract above). Runs in this handler's
        own thread (ThreadingHTTPServer, daemon threads) until the client
        disconnects — detected by the failed heartbeat/event write."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        q: queue.Queue = queue.Queue(maxsize=64)
        with _SUB_LOCK:
            _SUBSCRIBERS.add(q)
        _ensure_watcher()
        log.info("GET /api/events (stream opened)")
        try:
            self.wfile.write(_sse_format({"type": "hello", "ts": time.time()}))
            self.wfile.flush()
            while True:
                try:
                    evt = q.get(timeout=HEARTBEAT_SEC)
                    if evt.get("type") == "_close":  # graceful server stop
                        break
                    self.wfile.write(_sse_format(evt))
                except queue.Empty:
                    self.wfile.write(b": hb\n\n")
                self.wfile.flush()
        except OSError:  # BrokenPipe/ConnectionReset — client went away
            pass
        finally:
            with _SUB_LOCK:
                _SUBSCRIBERS.discard(q)

    def _serve_react_app(self, path: str) -> bool:
        """Serve the Vite-built React app — the GUI's only frontend, under /app/."""
        if path == "/app":
            self.send_response(301)
            self.send_header("Location", "/app/")
            self.end_headers()
            return True
        if not path.startswith("/app/"):
            return False
        root = _WEB_DIST.resolve()
        rel = path[len("/app/"):] or "index.html"
        target = (root / rel).resolve()
        if not str(target).startswith(str(root)):
            self._send(403, {"error": "forbidden"})
            return True
        if not target.is_file():
            target = root / "index.html"
            if not target.is_file():
                self._send(404, {"error": "react app not built"})
                return True
        self._send_file(200, target)
        return True

    def _err(self, exc, path):
        if isinstance(exc, QuarryError):
            log.warning("%s → %s (code=%s)", path, exc, exc.exit_code)
        else:
            log.error("%s → %s\n%s", path, exc, traceback.format_exc())
        self._send(400, {"error": str(exc), "code": getattr(exc, "exit_code", None)})

    def do_GET(self):
        if not self._local_origin_ok():
            return self._send(403, {"error": "forbidden: non-local origin"})
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        g = lambda k: (qs.get(k) or [None])[0]
        flag = lambda k: str(g(k) or "").lower() not in ("", "0", "false", "no")
        t0 = time.monotonic()
        try:
            if u.path in ("/", "/index.html"):
                self.send_response(301)
                self.send_header("Location", "/app/")
                self.end_headers()
                return
            if self._serve_react_app(u.path):
                return
            if u.path == "/api/events":
                return self._serve_events()
            if u.path == "/api/version":
                out = {"name": "Quarry", "version": __version__}
            elif u.path == "/api/connections":
                out = api_connections()
            elif u.path == "/api/workspaces":
                out = api_workspaces()
            elif u.path == "/api/tables":
                out = api_tables(g("db"), g("env"), fresh=flag("fresh"))
            elif u.path == "/api/inspect":
                out = api_inspect(g("db"), g("env"), g("key"))
            elif u.path == "/api/columns":
                out = api_columns(g("db"), g("env"), g("table"))
            elif u.path == "/api/queries":
                out = api_queries()
            elif u.path == "/api/health":
                out = api_health(g("db"), g("env"), fresh=flag("fresh"), cached_only=flag("cached"))
            elif u.path == "/api/conninfo":
                out = api_conninfo(g("db"), g("env"), reveal=flag("reveal"))
            elif u.path == "/api/update":
                out = api_update()
            elif u.path == "/api/changelog":
                out = api_changelog()
            elif u.path == "/api/keepalive":
                out = api_keepalive()
            else:
                return self._send(404, {"error": "not found"})
            log.info("GET %s (%d ms)", self.path, int((time.monotonic() - t0) * 1000))
            self._send(200, out)
        except BaseException as e:  # noqa: BLE001  (catch SystemExit too)
            self._err(e, "GET " + self.path)

    def do_POST(self):
        if not self._local_origin_ok():
            return self._send(403, {"error": "forbidden: non-local origin"})
        u = urlparse(self.path)
        t0 = time.monotonic()
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            if u.path == "/api/query":
                out = api_query(body)
            elif u.path == "/api/run":
                out = api_run(body)
            elif u.path == "/api/local/up":
                out = api_local_up(body)
            elif u.path == "/api/local/sync":
                out = api_local_sync(body)
            elif u.path == "/api/workspaces/add":
                out = api_workspace_add(body)
            elif u.path == "/api/workspaces/remove":
                out = api_workspace_remove(body)
            elif u.path == "/api/keepalive/up":
                out = api_keepalive_up()
            elif u.path == "/api/keepalive/down":
                out = api_keepalive_down()
            else:
                return self._send(404, {"error": "not found"})
            log.info("POST %s (%d ms)", u.path, int((time.monotonic() - t0) * 1000))
            self._send(200, out)
        except BaseException as e:  # noqa: BLE001
            self._err(e, "POST " + u.path)


def _port_pids(port: int) -> list[int]:
    try:
        r = subprocess.run(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
                           capture_output=True, text=True, timeout=3)
        return [int(x) for x in r.stdout.split()]
    except Exception:
        return []


def _is_quarry_gui(pid: int) -> bool:
    """True only for *our own* quarry GUI process — never a foreign 'gui' command,
    so _reclaim_port can't SIGTERM someone else's server on the same port."""
    try:
        r = subprocess.run(["ps", "-p", str(pid), "-o", "command="],
                           capture_output=True, text=True, timeout=3)
        c = r.stdout.lower()
        if "gui" not in c:
            return False
        # require an unambiguous quarry/qy marker in the command line
        return "quarry" in c or "/qy " in c or c.strip().endswith("/qy") or "-m quarry" in c
    except Exception:
        return False


def _reclaim_port(port: int) -> bool:
    """Kill a previous quarry-gui listening on `port` (never a foreign process)."""
    killed = False
    for pid in _port_pids(port):
        if pid != os.getpid() and _is_quarry_gui(pid):
            try:
                os.kill(pid, signal.SIGTERM)
                killed = True
            except Exception:
                pass
    if killed:
        time.sleep(0.8)
    return killed


def _next_free_port(host: str, start: int, tries: int = 30) -> int:
    for p in range(start + 1, start + 1 + tries):
        with socket.socket() as s:
            try:
                s.bind((host, p))
                return p
            except OSError:
                continue
    return start


def _bind(host: str, port: int) -> tuple[ThreadingHTTPServer, int]:
    try:
        return ThreadingHTTPServer((host, port), Handler), port
    except OSError as e:
        if e.errno not in (48, 98):   # not address-in-use
            raise
        if _reclaim_port(port):       # our own old instance -> take it over
            print(f"port {port}: stopped the previous Quarry GUI and took over.", flush=True)
            return ThreadingHTTPServer((host, port), Handler), port
        new_port = _next_free_port(host, port)  # someone else -> move over
        print(f"port {port} is taken (not Quarry) — using {new_port} instead.", flush=True)
        return ThreadingHTTPServer((host, new_port), Handler), new_port


def serve(host="127.0.0.1", port=8765, ws_path=None, open_browser=True) -> int:  # pragma: no cover
    # blocking serve_forever loop — exercised by the real `qy gui` e2e, not the in-process gate
    workspace.configure_workspace(ws_path)
    _setup_logging()
    cache.load()
    _ensure_update_checker()
    httpd, port = _bind(host, port)
    url = f"http://{host}:{port}"
    homes = ", ".join(str(w.home) for w in workspace.WS_LIST)
    print(f"Quarry GUI → {url}", flush=True)
    print(f"  workspace(s): {homes}", flush=True)
    print("  requests + errors log below; Ctrl-C to stop.", flush=True)
    if open_browser:
        try:
            webbrowser.open(f"{url}/app/")
        except Exception:
            pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nbye.")
    finally:
        _close_event_streams()
        httpd.server_close()
    return 0


def main() -> int:
    return serve()


if __name__ == "__main__":
    sys.exit(main())
