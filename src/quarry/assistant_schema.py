"""Bounded, read-only catalog tools for the SQL assistant.

These tools accept names, never model-generated SQL. Credentials stay in the
local connection object and are not part of the model-visible response.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

from . import core, sqlite_engine, workspace
from .core import QuarryError


@dataclass(frozen=True)
class Context:
    conn: core.Connection
    workspace: str
    db: str
    env: str | None
    engine: str
    fingerprint: str


def resolve_context(workspace_path: str | None, db: str, env: str | None) -> Context:
    if not isinstance(db, str) or not db:
        raise QuarryError("Select a database before using the SQL assistant")
    conn = core.resolve_connection(db, env)
    if env is not None and conn.env != env:
        raise QuarryError("The conversation's database environment is no longer available")
    source = str(Path(conn.source or workspace.WS.home).expanduser().resolve())
    if workspace_path and str(Path(workspace_path).expanduser().resolve()) != source:
        # A GUI group can span workspaces (a local copy plus remote envs).
        # Accept its displayed workspace only when this exact db belongs to
        # that group; the stored context always uses the actual source.
        group_source = next((g.get("ws") for g in core.group_connections()
                             if any(i["db"] == conn.logical_db for i in g["items"])), None)
        if not group_source or Path(workspace_path).expanduser().resolve() != Path(group_source).expanduser().resolve():
            raise QuarryError("This conversation belongs to a different workspace")
    engine = core.connection_engine(conn)
    if engine not in ("postgres", "mysql", "sqlite"):
        raise QuarryError("SQL Assistant supports PostgreSQL, MySQL and SQLite connections")
    identity = [source, conn.key, conn.logical_db, conn.env, conn.url, engine,
                conn.ssh_host, conn.ssh_user, conn.ssh_port, conn.ssh_key]
    if engine == "sqlite":
        path = sqlite_engine.database_path(conn)
        identity.append(str(path))
        # Identity changes on file replacement, not ordinary data writes.
        try:
            stat = path.stat()
            identity.extend([stat.st_dev, stat.st_ino])
        except FileNotFoundError:
            raise QuarryError("The conversation's SQLite database file is unavailable") from None
    fp = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
    return Context(conn, source, conn.logical_db, conn.env, engine, fp)


def _query(ctx: Context, sql: str, params: dict, limit=501) -> list[dict]:
    return core.run_query(ctx.conn, sql, params=params, max_rows=limit,
                          timeout=15, connect_timeout=10).rows


def find_tables(context: Context, query: str = "", limit: int = 30) -> dict:
    if not isinstance(query, str) or len(query) > 200:
        raise QuarryError("Table search must be text, up to 200 characters")
    if type(limit) is not int or not 1 <= limit <= 100:
        raise QuarryError("Table search limit must be between 1 and 100")
    if context.engine == "sqlite":
        sql = """SELECT name AS table_name FROM sqlite_schema
            WHERE type IN ('table', 'view') AND name NOT GLOB 'sqlite_*'
              AND lower(name) LIKE lower(:'pattern') ORDER BY name"""
    elif context.engine == "mysql":
        sql = """SELECT table_name AS table_name FROM information_schema.tables
            WHERE table_schema = DATABASE() AND lower(table_name) LIKE lower(:'pattern')
            ORDER BY table_name"""
    else:
        sql = """SELECT CASE WHEN table_schema = 'public' AND position('.' in table_name) = 0
                THEN table_name ELSE quote_ident(table_schema) || '.' || quote_ident(table_name) END AS table_name
            FROM information_schema.tables WHERE table_schema NOT IN ('pg_catalog', 'information_schema')
              AND table_schema NOT LIKE 'pg_%'
              AND (table_schema || '.' || table_name) ILIKE :'pattern'
            ORDER BY table_schema, table_name"""
    rows = _query(context, sql, {"pattern": "%" + query + "%"}, limit + 1)
    return {"tables": [r["table_name"] for r in rows[:limit]], "capped": len(rows) > limit}


def _pg_relation(table: str) -> str:
    # Catalog names for public tables are bare, including spaces and case.
    # Qualified names returned by find_tables are properly quoted already.
    if "." not in table:
        name = table[1:-1].replace('""', '"') if table.startswith('"') and table.endswith('"') else table
        return '"public"."' + name.replace('"', '""') + '"'
    ident = r'(?:"(?:[^"]|"")+"|[A-Za-z_][A-Za-z_0-9$]*)'
    if not re.fullmatch(rf'{ident}\.{ident}', table):
        raise QuarryError("Use the schema-qualified table name returned by find_tables")
    return table


def _describe(ctx: Context, table: str) -> dict:
    params = {"table": table}
    if ctx.engine == "sqlite":
        columns = _query(ctx, """SELECT name AS name, type AS type,
            CASE WHEN "notnull" = 1 THEN 0 ELSE 1 END AS nullable, pk AS primary_position
            FROM pragma_table_xinfo(:'table', 'main') WHERE hidden != 1 ORDER BY cid""", params)
        keys = _query(ctx, """SELECT id AS key_id, seq AS position, "from" AS column_name,
            "table" AS target_table, "to" AS target_column FROM pragma_foreign_key_list(:'table', 'main')
            ORDER BY id, seq""", params)
        primary = [r["name"] for r in sorted(columns, key=lambda r: r["primary_position"]) if r["primary_position"]]
    elif ctx.engine == "mysql":
        # MySQL connections select one database; avoid silently interpreting a
        # dotted string as a different database from the selected connection.
        columns = _query(ctx, """SELECT column_name AS name, column_type AS type,
            (is_nullable = 'YES') AS nullable, column_comment AS comment,
            (column_key = 'PRI') AS primary_position FROM information_schema.columns
            WHERE table_schema = DATABASE() AND table_name = :'table' ORDER BY ordinal_position""", params)
        keys = _query(ctx, """SELECT constraint_name AS key_id, ordinal_position AS position,
            column_name AS column_name, referenced_table_schema AS target_schema,
            referenced_table_name AS target_table, referenced_column_name AS target_column
            FROM information_schema.key_column_usage WHERE table_schema = DATABASE()
              AND table_name = :'table' AND referenced_table_name IS NOT NULL
            ORDER BY constraint_name, ordinal_position""", params)
        primary = [r["name"] for r in columns if r["primary_position"]]
    else:
        params = {"table": _pg_relation(table)}
        columns = _query(ctx, """SELECT a.attname AS name, format_type(a.atttypid, a.atttypmod) AS type,
            NOT a.attnotnull AS nullable, col_description(a.attrelid, a.attnum) AS comment,
            COALESCE((SELECT array_position(c.conkey, a.attnum) FROM pg_constraint c
                      WHERE c.conrelid = a.attrelid AND c.contype = 'p'), 0) AS primary_position
            FROM pg_attribute a WHERE a.attrelid = to_regclass(:'table')
              AND a.attnum > 0 AND NOT a.attisdropped ORDER BY a.attnum""", params)
        keys = _query(ctx, """SELECT c.conname AS key_id, k.position AS position,
            a.attname AS column_name, n.nspname AS target_schema,
            r.relname AS target_table, b.attname AS target_column
            FROM pg_constraint c
            CROSS JOIN LATERAL unnest(c.conkey) WITH ORDINALITY AS k(attnum, position)
            JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum
            JOIN pg_class r ON r.oid = c.confrelid
            JOIN pg_namespace n ON n.oid = r.relnamespace
            JOIN pg_attribute b ON b.attrelid = c.confrelid AND b.attnum = c.confkey[k.position]
            WHERE c.contype = 'f' AND c.conrelid = to_regclass(:'table')
            ORDER BY c.conname, k.position""", params)
        primary = [r["name"] for r in sorted(columns, key=lambda r: r["primary_position"]) if r["primary_position"]]
    if not columns:
        raise QuarryError(f"Table not found or not accessible: {table}")
    relations = {}
    for row in keys[:500]:
        relation = relations.setdefault(str(row["key_id"]), {
            "columns": [], "targetTable": row["target_table"], "targetColumns": [],
            **({"targetSchema": row["target_schema"]} if row.get("target_schema") else {})})
        relation["columns"].append(row["column_name"])
        relation["targetColumns"].append(row["target_column"])
    return {"name": table, "columns": [{k: v for k, v in c.items() if k != "primary_position"} for c in columns[:500]],
            "primaryKey": primary, "foreignKeys": list(relations.values()),
            "capped": len(columns) > 500 or len(keys) > 500}


def describe_tables(context: Context, tables: list[str]) -> dict:
    if not isinstance(tables, list) or not 1 <= len(tables) <= 8:
        raise QuarryError("Describe between one and eight tables at a time")
    if any(not isinstance(t, str) or not t or len(t) > 512 for t in tables):
        raise QuarryError("Invalid table name")
    return {"tables": [_describe(context, table) for table in dict.fromkeys(tables)]}
