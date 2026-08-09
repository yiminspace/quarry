"""Quarry — 多引擎数据库查询内核,一核多脸(CLI / GUI / Skill)。

Library entry points (used by the GUI and any programmatic face):

    from quarry import configure_workspace, load_connections, run_query
    configure_workspace("~/my-workspace")
    result = run_query(get_connection("shop"), "select 1 as ok")
    print(result.to_dict())
"""

from __future__ import annotations

from .core import (
    Connection,
    Param,
    QuarryError,
    Query,
    QueryResult,
    connection_engine,
    enforce_safety,
    get_connection,
    is_read_only,
    list_all_queries,
    load_connections,
    load_query,
    run_query,
)
from .workspace import Workspace, build_workspaces, configure_workspace

__version__ = "0.21.3"

__all__ = [
    "__version__",
    "Connection",
    "Param",
    "Query",
    "QueryResult",
    "QuarryError",
    "Workspace",
    "build_workspaces",
    "configure_workspace",
    "connection_engine",
    "enforce_safety",
    "get_connection",
    "is_read_only",
    "list_all_queries",
    "load_connections",
    "load_query",
    "run_query",
]
