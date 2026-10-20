"""Shared query-metadata cache — table lists, column metadata, health probes.

Originally lived only inside gui.py; moved down here (issue #97) so `cli.py`
and `mcp.py` benefit from the same on-disk cache instead of re-querying the
DB (often over a slow SSH tunnel) on every invocation. Storage location and
JSON format are unchanged from the GUI-only implementation, so an existing
`~/.cache/quarry/gui-cache.json` keeps working after upgrade.

No expiry at this layer except what callers embed in their own values (e.g.
core.py's health probes track their own `_ts`/TTL). Entries persist until
overwritten or explicitly dropped.

Config-fingerprint invalidation (issue #97's "implicit invalidation" ask) is
layered on here too: callers may pass `fingerprint=` to get()/put(); a value
whose stored `_fp` doesn't match the caller's current fingerprint is treated
as a miss on read. This is a lazy, read-time backstop — callers with a
proactive invalidation signal (like the GUI's workspace file-watcher) should
still call drop_prefix() eagerly; the two mechanisms are complementary.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

def _default_cache_file() -> Path:
    # QUARRY_CACHE_FILE lets a subprocess (CLI/MCP) or a test point at an
    # isolated file instead of the real ~/.cache — same override pattern as
    # QUARRY_CONNECTIONS_FILE/QUARRY_QUERIES_DIR in workspace.py.
    override = os.environ.get("QUARRY_CACHE_FILE")
    return Path(override).expanduser() if override else Path.home() / ".cache" / "quarry" / "gui-cache.json"


CACHE_FILE = _default_cache_file()

_CACHE: dict[str, dict] = {}
_LOCK = threading.Lock()


def load() -> None:
    try:
        with CACHE_FILE.open(encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            _CACHE.update(data)
    except Exception:
        pass


def save() -> None:
    try:
        with _LOCK:
            snapshot = dict(_CACHE)
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = CACHE_FILE.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(snapshot, f, ensure_ascii=False)
        os.replace(tmp, CACHE_FILE)
    except Exception:
        pass


def get(key: str, *, fingerprint: str | None = None):
    """The cached value for `key`, or None if absent. When `fingerprint` is
    given, a value stored with a different (or no) `_fp` is treated as a
    miss — this is what lets a connection-config change (URL, SSH settings,
    proxy toggle) auto-invalidate stale entries even in a fresh CLI/MCP
    process that never saw the change happen."""
    v = _CACHE.get(key)
    if v is None:
        return None
    if fingerprint is not None and v.get("_fp") != fingerprint:
        return None
    return v


def put(key: str, value: dict, *, fingerprint: str | None = None) -> dict:
    """Store `value` under `key`, persisting to disk. When `fingerprint` is
    given it is stamped onto the stored entry (as `_fp`) so a later get()
    with a different fingerprint treats it as stale; the returned dict never
    carries `_fp` (callers see only their own value shape)."""
    stored = {**value, "_fp": fingerprint} if fingerprint is not None else dict(value)
    _CACHE[key] = stored
    save()
    return value


def drop_prefix(prefix: str) -> None:
    """Remove every key starting with `prefix` (e.g. "health:") and persist."""
    for k in [k for k in list(_CACHE) if k.startswith(prefix)]:
        _CACHE.pop(k, None)
    save()


def clear() -> None:
    _CACHE.clear()
