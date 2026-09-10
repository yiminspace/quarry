"""Quarry MCP face — a Model Context Protocol server over stdio, pure stdlib.

Agents connect natively instead of shelling out to the CLI, and get the same
kernel with the same safety rails: read-only by default, automatic row caps,
and graduated prod protection.

Launch:  qy mcp [--workspace PATH] [--write]
         (or: python -m quarry.mcp)

Protocol: JSON-RPC 2.0, newline-delimited, over stdin/stdout (MCP stdio
transport). Logs go to stderr; stdout carries only protocol messages.

Write policy (graduated, mirrors the CLI):
  - server default            -> every write/DDL is blocked (exit code 8)
  - server started --write    -> a call may pass {"write": true}
  - target connection is production        -> the call must ALSO pass {"confirm_prod": true}
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from . import cache, core, workspace
from .core import EXIT_SAFETY_BLOCKED, QuarryError

PROTOCOL_VERSION = "2025-06-18"


def _server_info() -> dict:
    from . import __version__
    return {"name": "quarry", "version": __version__}

_ALLOW_WRITE_FLAG = False   # set by --write at server start


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

def tool_list_connections() -> dict:
    """Connection tree: groups -> logical dbs -> envs."""
    return {"groups": core.group_connections(),
            "workspaces": [str(w.home) for w in workspace.WS_LIST]}


def tool_list_tables(db: str, env: str | None = None) -> dict:
    """Table (or, for redis, key) list for a db@env, backed by the shared
    metadata cache (issue #97) so a repeat call — from this or any other
    Quarry face, including the GUI's own table panel — skips the DB round
    trip. Redis goes through the same cached (per-key type/TTL) entry the
    GUI populates and just projects out the key names, rather than always
    re-scanning; the key list is capped at 400 (cache.py's cap), same as
    the GUI's."""
    conn = core.resolve_connection(db, env)
    res = core.cached_tables(conn, db, env, default_timeout=core.MCP_EXECUTE_TIMEOUT_SEC)
    if res["engine"] == "redis":
        return {"engine": "redis", "keys": [k.get("key") for k in res.get("keys", [])]}
    return {"engine": res["engine"], "tables": res.get("tables", [])}


def tool_describe_table(db: str, table: str, env: str | None = None) -> dict:
    """Column metadata for one table, backed by the shared metadata cache
    (issue #97). `columns` gains a `character_maximum_length` field
    (additive) now that this shares core.cached_columns with the GUI/CLI."""
    conn = core.resolve_connection(db, env)
    engine = core.connection_engine(conn)
    if engine in ("redis", "neptune"):
        raise QuarryError(f"describe_table is not supported for engine={engine}")
    safe = "".join(ch for ch in table if ch.isalnum() or ch in "_$")
    if not safe:
        raise QuarryError("invalid table name")
    res = core.cached_columns(conn, db, env, safe, raise_errors=True,
                              default_timeout=core.MCP_EXECUTE_TIMEOUT_SEC)
    return {"table": safe, "engine": engine, "columns": res["rows"]}


def _check_write_policy(conn, write: bool, confirm_prod: bool) -> bool:
    """Apply the graduated write policy; returns allow_write for run_query."""
    if not write:
        return False
    if not _ALLOW_WRITE_FLAG:
        raise QuarryError(
            "writes are disabled: this MCP server was started without --write",
            exit_code=EXIT_SAFETY_BLOCKED,
        )
    if conn.production and not confirm_prod:
        raise QuarryError(
            'target connection is production — retry with {"confirm_prod": true} to confirm',
            exit_code=EXIT_SAFETY_BLOCKED,
        )
    return True


def tool_exec_sql(db: str, sql: str, env: str | None = None, max_rows: int = 500,
                  write: bool = False, confirm_prod: bool = False) -> dict:
    conn = core.resolve_connection(db, env)
    allow_write = _check_write_policy(conn, write, confirm_prod)
    res = core.run_query(conn, sql, allow_write=allow_write,
                         max_rows=int(max_rows), with_types=True,
                         default_timeout=core.MCP_EXECUTE_TIMEOUT_SEC)
    return res.to_dict()


def tool_list_saved_queries() -> dict:
    return {"queries": [
        {"name": q.name, "db": q.db, "desc": q.desc,
         "params": [{"name": p.name, "type": p.type, "required": p.required,
                     "default": p.default} for p in q.params]}
        for q in core.list_all_queries()]}


def tool_run_saved_query(name: str, params: dict | None = None,
                         env: str | None = None, max_rows: int = 500) -> dict:
    q = core.load_query(name)
    conn = core.resolve_connection(q.db, env)
    resolved = core.resolve_params(q, {k: str(v) for k, v in (params or {}).items()})
    res = core.run_query(conn, q.sql, params=resolved,
                         max_rows=int(max_rows), with_types=True,
                         default_timeout=core.MCP_EXECUTE_TIMEOUT_SEC)
    return res.to_dict()


def _s(**props) -> dict:
    required = [k for k, v in props.items() if v.pop("_req", False)]
    return {"type": "object", "properties": props, "required": required}


TOOLS: list[dict[str, Any]] = [
    {
        "name": "list_connections",
        "description": ("List all configured database connections, grouped into project "
                        "folders and env-sets (same logical db across dev/prod/...). "
                        "Start here to learn which `db` keys exist."),
        "inputSchema": _s(),
        "fn": lambda a: tool_list_connections(),
    },
    {
        "name": "list_tables",
        "description": "List tables of a database (or keys for a redis connection).",
        "inputSchema": _s(
            db={"type": "string", "description": "logical db or connection key", "_req": True},
            env={"type": "string", "description": "env-set member (dev/prod/...); defaults to dev"},
        ),
        "fn": lambda a: tool_list_tables(a["db"], a.get("env")),
    },
    {
        "name": "describe_table",
        "description": "Column names, types and nullability for one table.",
        "inputSchema": _s(
            db={"type": "string", "_req": True},
            table={"type": "string", "_req": True},
            env={"type": "string"},
        ),
        "fn": lambda a: tool_describe_table(a["db"], a["table"], a.get("env")),
    },
    {
        "name": "exec_sql",
        "description": ("Execute SQL (or a redis command) against a database and get a "
                        "structured result {columns, rows, rowCount, truncated, elapsedMs}. "
                        "READ-ONLY by default: writes/DDL fail with code 8 unless you pass "
                        "write=true AND the server was started with --write; a configured production connection "
                        "additionally requires confirm_prod=true. SELECTs without LIMIT get "
                        "an automatic LIMIT (max_rows, default 500)."),
        "inputSchema": _s(
            db={"type": "string", "_req": True},
            sql={"type": "string", "_req": True},
            env={"type": "string"},
            max_rows={"type": "integer", "default": 500},
            write={"type": "boolean", "default": False},
            confirm_prod={"type": "boolean", "default": False},
        ),
        "fn": lambda a: tool_exec_sql(a["db"], a["sql"], a.get("env"),
                                      a.get("max_rows", 500), a.get("write", False),
                                      a.get("confirm_prod", False)),
    },
    {
        "name": "list_saved_queries",
        "description": "List the workspace's saved named queries (name, target db, params).",
        "inputSchema": _s(),
        "fn": lambda a: tool_list_saved_queries(),
    },
    {
        "name": "run_saved_query",
        "description": "Run a saved named query by name, with `params` as {name: value}.",
        "inputSchema": _s(
            name={"type": "string", "_req": True},
            params={"type": "object"},
            env={"type": "string"},
            max_rows={"type": "integer", "default": 500},
        ),
        "fn": lambda a: tool_run_saved_query(a["name"], a.get("params"),
                                             a.get("env"), a.get("max_rows", 500)),
    },
]


# ---------------------------------------------------------------------------
# JSON-RPC over stdio
# ---------------------------------------------------------------------------

def _tools_payload() -> list[dict]:
    return [{k: t[k] for k in ("name", "description", "inputSchema")} for t in TOOLS]


def _handle_tools_call(params: dict) -> dict:
    name = params.get("name")
    args = params.get("arguments") or {}
    tool = next((t for t in TOOLS if t["name"] == name), None)
    if tool is None:
        raise QuarryError(f"unknown tool: {name}")
    missing = [f for f in tool["inputSchema"].get("required", []) if args.get(f) in (None, "")]
    if missing:
        # a malformed call is a tool error (isError), not a JSON-RPC protocol fault
        payload = {"error": f"missing required argument(s): {', '.join(missing)}", "code": core.EXIT_USAGE}
        return {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
                "isError": True}
    try:
        result = tool["fn"](args)
        return {"content": [{"type": "text",
                             "text": json.dumps(result, ensure_ascii=False, default=str)}],
                "isError": False}
    except QuarryError as exc:
        payload = {"error": str(exc), "code": exc.exit_code}
        return {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
                "isError": True}
    except Exception as exc:  # noqa: BLE001 — surface as a tool failure, not a protocol crash
        payload = {"error": f"{type(exc).__name__}: {exc}", "code": None}
        return {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
                "isError": True}


def _dispatch(method: str, params: dict) -> dict | None:
    """Return a result dict, or None for notifications (no response)."""
    if method == "initialize":
        return {"protocolVersion": params.get("protocolVersion") or PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": _server_info()}
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": _tools_payload()}
    if method == "tools/call":
        return _handle_tools_call(params)
    if method in ("resources/list", "resources/templates/list"):
        return {"resources": []} if method == "resources/list" else {"resourceTemplates": []}
    if method == "prompts/list":
        return {"prompts": []}
    if method.startswith("notifications/"):
        return None
    raise QuarryError(f"method not found: {method}")


def serve(ws_path: str | None = None, allow_write: bool = False) -> int:
    global _ALLOW_WRITE_FLAG
    _ALLOW_WRITE_FLAG = allow_write
    workspace.configure_workspace(ws_path)
    cache.load()
    homes = ", ".join(str(w.home) for w in workspace.WS_LIST)
    print(f"quarry mcp: serving on stdio (workspace(s): {homes}, "
          f"writes {'ENABLED' if allow_write else 'disabled'})", file=sys.stderr, flush=True)

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        msg_id = msg.get("id")
        method = msg.get("method", "")
        try:
            result = _dispatch(method, msg.get("params") or {})
            if msg_id is None:          # notification — no response
                continue
            reply: dict[str, Any] = {"jsonrpc": "2.0", "id": msg_id, "result": result}
        except QuarryError as exc:
            if msg_id is None:
                continue
            code = -32601 if str(exc).startswith("method not found") else -32000
            reply = {"jsonrpc": "2.0", "id": msg_id,
                     "error": {"code": code, "message": str(exc)}}
        except Exception as exc:  # noqa: BLE001 — protocol must never crash
            if msg_id is None:
                continue
            reply = {"jsonrpc": "2.0", "id": msg_id,
                     "error": {"code": -32603, "message": f"internal error: {exc}"}}
        sys.stdout.write(json.dumps(reply, ensure_ascii=False, default=str) + "\n")
        sys.stdout.flush()
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="quarry-mcp", description=__doc__)
    ap.add_argument("--workspace", default=None)
    ap.add_argument("--write", action="store_true",
                    help="allow tool calls to request writes (still per-call opt-in)")
    args = ap.parse_args(argv)
    return serve(args.workspace, allow_write=args.write)


if __name__ == "__main__":
    sys.exit(main())
