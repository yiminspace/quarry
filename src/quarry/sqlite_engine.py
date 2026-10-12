"""SQLite file connections, with engine-enforced read-only access.

Each request owns its connection. Files must already exist; URI options,
attached databases and extension loading are deliberately not exposed.
"""
from __future__ import annotations

import base64
import json
import math
import re
import time
from pathlib import Path
from urllib.parse import unquote, urlsplit

from . import workspace


def database_path(conn) -> Path:
    from .core import QuarryError

    try:
        return _database_path(conn)
    except (ValueError, RuntimeError, OSError) as exc:
        raise QuarryError(f"invalid SQLite file path: {exc}") from exc


def _database_path(conn) -> Path:
    from .core import QuarryError

    if any((conn.ssh_host, conn.ssh_user, conn.ssh_key, conn.ssh_port)):
        raise QuarryError("SQLite uses a local file; SSH tunnels are not supported")
    value = conn.url.strip()
    if value.lower().startswith("sqlite:"):
        parsed = urlsplit(value)
        if parsed.netloc or parsed.query or parsed.fragment or not value.lower().startswith("sqlite:///"):
            raise QuarryError("use sqlite:///relative.db or sqlite:////absolute/path.db without URI options")
        value = unquote(parsed.path[1:])
    elif "://" in value or value.lower().startswith("file:"):
        raise QuarryError("SQLite expects a local file path or sqlite:/// URL")
    if not value or value == ":memory:" or "\x00" in value:
        raise QuarryError("SQLite requires an existing database file; in-memory databases are not supported")
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = Path(conn.source or workspace.WS.home) / path
    return path.resolve()


def _spans(sql: str):
    """Yield opaque SQL spans (comments/quoted values/identifiers)."""
    i = 0
    while i < len(sql):
        start = i
        if sql.startswith("--", i):
            end = sql.find("\n", i)
            i = len(sql) if end < 0 else end
        elif sql.startswith("/*", i):
            end = sql.find("*/", i + 2)
            i = len(sql) if end < 0 else end + 2
        elif sql[i] in ("'", '"', '`', '['):
            close = ']' if sql[i] == '[' else sql[i]
            i += 1
            while i < len(sql):
                if sql[i] == close:
                    i += 1
                    if close != ']' and i < len(sql) and sql[i] == close:
                        i += 1
                    else:
                        break
                else:
                    i += 1
        else:
            i += 1
            continue
        yield start, i


def skeleton(sql: str) -> str:
    chars = list(sql)
    for start, end in _spans(sql):
        chars[start:end] = ' ' * (end - start)
    return ''.join(chars)


def substitute_params(sql: str, params: dict) -> str:
    """Quarry's :'quoted' / :raw parameters, using SQLite string semantics.

    Only SQL tokens are substituted, never literals, identifiers or comments.
    SQLite does not treat a backslash as an escape character in a string.
    """
    from .core import QuarryError

    opaque = dict(_spans(sql))
    pattern = re.compile(r":'(\w+)'|:(\w+)")
    out = []
    i = 0
    while i < len(sql):
        if i in opaque:
            out.append(sql[i:opaque[i]])
            i = opaque[i]
            continue
        match = pattern.match(sql, i)
        if match:
            name = match[1] or match[2]
            if name not in params:
                raise QuarryError(f"missing required param '{name}'")
            value = str(params[name])
            out.append("'" + value.replace("'", "''") + "'" if match[1] else value)
            i = match.end()
        else:
            out.append(sql[i])
            i += 1
    return ''.join(out)


# PRAGMAs with an argument that names an object, rather than changing a setting.
_OBJECT_PRAGMAS = {"table_info", "table_xinfo", "index_list", "index_info",
                   "index_xinfo", "foreign_key_list"}
_VALUE_PRAGMAS = {"database_list", "compile_options", "collation_list",
                  "function_list", "module_list", "pragma_list", "foreign_keys",
                  "user_version", "application_id", "schema_version", "data_version",
                  "page_count", "freelist_count", "page_size", "encoding",
                  "journal_mode", "integrity_check", "quick_check", "foreign_key_check"}


def is_read_only(sql: str) -> bool:
    sk = skeleton(sql).strip().rstrip(';').strip()
    if ';' in sk or not sk:
        return False
    if re.match(r"(?:select|values)\b", sk, re.I):
        return True
    if re.match(r"(?:with|explain)\b", sk, re.I):
        return not re.search(r"\b(?:insert|update|delete|create|drop|alter|attach|detach|vacuum)\b|\breplace\b(?!\s*\()", sk, re.I)
    if re.match(r"pragma\b", sk, re.I):
        # Quoted pragma names are conservatively rejected here;
        # table-valued PRAGMAs work too.
        match = re.match(r"\s*pragma\s+(?:main\s*\.\s*)?(\w+)\b", sk, re.I)
        if not match or '=' in sk:
            return False
        name = match[1].lower()
        return name in _OBJECT_PRAGMAS or (name in _VALUE_PRAGMAS and '(' not in sk)
    return False


def append_limit(sql: str, max_rows: int, offset: int) -> str:
    """Append the row cap to either executable SQL or its parameter template."""
    semi = skeleton(sql).find(';')
    inner = sql if semi < 0 else sql[:semi]
    return f"{inner}\nLIMIT {max_rows + 1} OFFSET {offset}"


def enforce_safety(sql: str, *, allow_write: bool, max_rows: int | None,
                   offset: int = 0) -> tuple[str, int | None]:
    from .core import QuarryError, EXIT_SAFETY_BLOCKED

    if (max_rows is not None and max_rows < 0) or offset < 0:
        raise QuarryError("max_rows and offset must be non-negative")
    sk = skeleton(sql)
    statements = [part for part in sk.split(';') if part.strip()]
    if len(statements) != 1:
        raise QuarryError("execute one non-empty statement at a time", EXIT_SAFETY_BLOCKED)
    if re.match(r"\s*(?:attach|detach|begin|commit|end|rollback|savepoint|release|vacuum)\b", sk, re.I):
        raise QuarryError("SQLite executes one statement against one existing file; ATTACH, VACUUM and transaction commands are not supported", EXIT_SAFETY_BLOCKED)
    if re.search(r"\b(?:load_extension|writefile|readfile)\s*\(", sk, re.I):
        raise QuarryError("SQLite extension loading and external file functions are not supported", EXIT_SAFETY_BLOCKED)
    readonly = is_read_only(sql)
    if not allow_write and not readonly:
        raise QuarryError("blocked a write/DDL statement (read-only by default; pass --write to allow)", EXIT_SAFETY_BLOCKED)
    if not max_rows or not readonly:
        return sql, None
    if re.match(r"\s*(?:select|with|values)\b", sk, re.I):
        # Only a top-level LIMIT belongs to the caller; nested limits do not
        # bound the outer result. VALUES is paged while fetching because it
        # rejects LIMIT, and a SELECT wrapper would rename duplicate columns.
        depth = 0
        top = []
        for ch in sk:
            if ch == '(':
                depth += 1
            elif ch == ')':
                depth = max(0, depth - 1)
            top.append(ch if depth == 0 else ' ')
        top_sql = ''.join(top)
        if re.search(r"\blimit\b", top_sql, re.I):
            return sql, None
        terms = re.findall(r"\b(?:select|values)\b", top_sql, re.I)
        if terms and terms[-1].lower() == "select":
            return append_limit(sql, max_rows, offset), max_rows
    # VALUES/PRAGMA/EXPLAIN cannot take a trailing LIMIT. Bound and offset
    # fetching in the adapter instead, preserving the original column names.
    return sql, max_rows


def run_query(conn, sql: str, *, read_only: bool = True, timeout: float = 300,
              connect_timeout: float | None = None, fetch_limit: int | None = None,
              fetch_offset: int = 0):
    from .core import (QuarryError, ResultRows, EXIT_CONNECTION_ERROR,
                       EXIT_SQL_ERROR, EXIT_SAFETY_BLOCKED, _unique_names,
                       serialize_row, _with_timeout_hint)
    try:
        import sqlite3
    except ImportError as exc:
        raise QuarryError("this Python installation has no sqlite3 module", EXIT_CONNECTION_ERROR) from exc
    path = database_path(conn)
    if timeout <= 0 or (connect_timeout is not None and connect_timeout <= 0):
        raise QuarryError("SQLite timeouts must be positive")
    if not path.is_file():
        raise QuarryError(f"SQLite database file does not exist or is not a regular file: {path}", EXIT_CONNECTION_ERROR)
    deadline = time.monotonic() + timeout
    lock_timeout = min(timeout, connect_timeout if connect_timeout is not None else timeout)
    try:
        db = sqlite3.connect(path.as_uri() + ("?mode=ro" if read_only else "?mode=rw"),
                             uri=True, timeout=lock_timeout, isolation_level=None)
    except sqlite3.Error as exc:
        raise QuarryError(f"SQLite connection failed: {exc}", EXIT_CONNECTION_ERROR) from exc
    denied = False

    def authorize(action, arg1, arg2, _database, _trigger):
        nonlocal denied
        blocked = action in (sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH, sqlite3.SQLITE_TRANSACTION,
                             sqlite3.SQLITE_SAVEPOINT)
        if action == sqlite3.SQLITE_FUNCTION and (arg2 or '').lower() in ('load_extension', 'writefile', 'readfile'):
            blocked = True
        if read_only:
            if action == sqlite3.SQLITE_PRAGMA:
                name = (arg1 or '').lower()
                blocked |= not (name in _OBJECT_PRAGMAS or (name in _VALUE_PRAGMAS and arg2 is None))
            elif action not in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION,
                                 sqlite3.SQLITE_RECURSIVE):
                blocked = True
        if blocked:
            denied = True
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    try:
        if read_only:
            db.execute("PRAGMA query_only = ON")
        db.execute("PRAGMA foreign_keys = ON")
        db.execute("BEGIN")
        db.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
        db.set_authorizer(authorize)
        cursor = db.execute(sql)
        names = _unique_names([column[0] for column in cursor.description or ()])
        skip = fetch_offset
        while skip:
            chunk = cursor.fetchmany(min(skip, 1000))
            if not chunk:
                break
            skip -= len(chunk)
        raw = cursor.fetchmany(fetch_limit) if fetch_limit else cursor.fetchall()
        rows = ResultRows(columns=names)
        for values in raw:
            row = {name: ({"$binary": base64.b64encode(value).decode('ascii')}
                          if isinstance(value, bytes) else
                          {"$number": "NaN" if math.isnan(value) else "Infinity" if value > 0 else "-Infinity"}
                          if isinstance(value, float) and not math.isfinite(value) else value)
                   for name, value in zip(names, values)}
            rows.append(serialize_row(row))
        cursor.close()
        db.set_authorizer(None)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise QuarryError(_with_timeout_hint(f"SQLite query timed out after {timeout}s"), EXIT_SQL_ERROR)
        # COMMIT may itself wait for a reader. Use the remaining execution
        # budget instead of starting another full busy timeout after the SQL.
        lock_timeout = min(lock_timeout, remaining)
        db.execute(f"PRAGMA busy_timeout = {int(lock_timeout * 1000)}")
        db.commit()
        return rows, len(json.dumps(rows, ensure_ascii=False).encode('utf-8'))
    except sqlite3.Error as exc:
        message = str(exc)
        code = getattr(exc, 'sqlite_errorcode', 0) & 255
        if denied:
            raise QuarryError(f"SQLite operation blocked by database safety policy: {message}", EXIT_SAFETY_BLOCKED) from exc
        if code == sqlite3.SQLITE_INTERRUPT:
            message = _with_timeout_hint(f"SQLite query timed out after {timeout}s")
        elif code in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
            message = f"SQLite database is locked (waited up to {lock_timeout}s); finish the other transaction and retry"
        if code in (sqlite3.SQLITE_CANTOPEN, sqlite3.SQLITE_NOTADB, sqlite3.SQLITE_CORRUPT):
            raise QuarryError(f"SQLite database error: {message}", EXIT_CONNECTION_ERROR) from exc
        raise QuarryError(f"SQLite error: {message}", EXIT_SQL_ERROR) from exc
    finally:
        db.set_authorizer(None)
        db.close()  # rolls back an interrupted or failed write
