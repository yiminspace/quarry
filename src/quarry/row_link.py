"""Conservative single-record SELECT generation from relational key metadata."""
from __future__ import annotations

import math
import re

from . import core

_IDENT = r'(?:[A-Za-z_][A-Za-z0-9_$]*|"(?:[^"]|"")+"|`(?:[^`]|``)+`)'
_SELECT = re.compile(
    rf'^\s*SELECT\s+(?P<cols>\*|{_IDENT}(?:\s*,\s*{_IDENT})*)\s+FROM\s+'
    rf'(?P<table>{_IDENT}(?:\s*\.\s*{_IDENT})?)(?P<tail>\s+(?:WHERE|ORDER\s+BY|LIMIT|OFFSET)\b[^;]*)?\s*;?\s*$',
    re.I,
)


def _name(part: str, engine: str) -> str:
    part = part.strip()
    if part[0] in ('"', '`'):
        return part[1:-1].replace(part[0] * 2, part[0])
    return part.lower() if engine == 'postgres' else part


def _literal(value: object, engine: str) -> str | None:
    if isinstance(value, bool):
        return 'TRUE' if value else 'FALSE'
    if isinstance(value, (int, float)):
        if not math.isfinite(value) or abs(value) > 9007199254740991:
            return None  # JSON/JavaScript may already have rounded the key.
        return str(value)
    if isinstance(value, str):
        if '\x00' in value:
            return None
        if engine == 'mysql':
            return "CONVERT(X'" + value.encode('utf-8').hex() + "' USING utf8mb4)"
        escaped = value.replace("'", "''")
        if '\\' in value:
            return "E'" + escaped.replace('\\', '\\\\') + "'"
        return "'" + escaped + "'"
    return None  # NULL unique keys need not identify one record.


def record_query(conn, sql: str, row: dict) -> dict:
    engine = core.connection_engine(conn)
    match = _SELECT.fullmatch(sql) if engine in ('postgres', 'mysql') else None
    if not match:
        return {'reason': 'unsupported_query'}
    tail = match['tail'] or ''
    if re.search(r'\b(select|from|join|union|intersect|except|group|having|into|for)\b|--|/\*|[,()]', tail, re.I):
        return {'reason': 'unsupported_query'}
    # Split on actual identifier tokens, so dots inside quoted names survive.
    parts = [_name(p, engine) for p in re.findall(_IDENT, match['table'])]
    schema, table = (parts if len(parts) == 2 else (None, parts[0]))

    def quoted(name: str) -> str:
        if engine == 'mysql':
            return '`' + name.replace('`', '``') + '`'
        return '"' + name.replace('"', '""') + '"'

    if engine == 'postgres':
        metadata = """SELECT i.indexrelid::text AS key_id, i.indisprimary AS primary_key,
            a.attname AS column_name, k.ordinality AS position
            FROM pg_index i
            JOIN pg_class c ON c.oid = i.indrelid
            CROSS JOIN LATERAL unnest(i.indkey) WITH ORDINALITY AS k(attnum, ordinality)
            JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = k.attnum
            WHERE c.oid = to_regclass(:'relation') AND c.relkind IN ('r', 'p')
              AND i.indisunique AND i.indisvalid AND i.indisready
              AND i.indpred IS NULL AND i.indexprs IS NULL
              AND (c.relkind = 'p' OR NOT EXISTS
                   (SELECT 1 FROM pg_inherits WHERE inhparent = c.oid))
              AND k.ordinality <= i.indnkeyatts
            ORDER BY i.indisprimary DESC, i.indexrelid, k.ordinality"""
        relation = '.'.join(quoted(p) for p in parts)
        params = {'relation': relation}
    else:
        metadata = """SELECT tc.constraint_name AS key_id,
            (tc.constraint_type = 'PRIMARY KEY') AS primary_key,
            k.column_name AS column_name, k.ordinal_position AS position
            FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage k
              ON k.constraint_schema = tc.constraint_schema
              AND k.table_name = tc.table_name AND k.constraint_name = tc.constraint_name
            WHERE tc.table_schema = COALESCE(NULLIF(:'schema', ''), DATABASE())
              AND tc.table_name = :'table'
              AND tc.constraint_type IN ('PRIMARY KEY', 'UNIQUE')
            ORDER BY primary_key DESC, key_id, position"""
        params = {'schema': schema or '', 'table': table}
    keys: dict[str, list[str]] = {}
    for entry in core.run_query(conn, metadata, params=params, max_rows=0, timeout=15).rows:
        keys.setdefault(str(entry['key_id']), []).append(entry['column_name'])
    projected = None if match['cols'] == '*' else {
        _name(p, engine) for p in re.findall(_IDENT, match['cols'])}
    for columns in keys.values():
        if projected is not None and not set(columns) <= projected:
            continue
        values = [_literal(row.get(col), engine) for col in columns]
        if not columns or any(value is None for value in values):
            continue
        predicates = [f'{quoted(col)} = {value}' for col, value in zip(columns, values)]
        return {'table': '.'.join(p if re.fullmatch(r'[a-z_][a-z0-9_$]*', p) else quoted(p)
                                  for p in parts),
                'sql': 'SELECT * FROM ' + '.'.join(quoted(p) for p in parts)
                       + ' WHERE ' + ' AND '.join(predicates) + ';'}
    return {'reason': 'no_unique_key'}
