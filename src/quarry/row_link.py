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


def _mask_literals(sql: str, engine: str) -> str | None:
    """Hide literal contents before checking SQL structure; preserve identifiers."""
    chars = list(sql)
    i = 0
    while i < len(sql):
        quote = sql[i]
        if quote not in ("'", '"', '`'):
            if engine == 'postgres' and quote == '$':
                tag = re.match(r'\$(?:[A-Za-z_][A-Za-z0-9_]*)?\$', sql[i:])
                if tag and (i == 0 or not re.match(r'[\w$]', sql[i - 1])):
                    end = sql.find(tag[0], i + len(tag[0]))
                    if end < 0:
                        return None
                    end += len(tag[0])
                    chars[i:end] = ' ' * (end - i)
                    i = end
                    continue
            i += 1
            continue
        start = i
        # Quoted identifiers use doubled quotes; string backslash escapes are
        # enabled by MySQL or PostgreSQL's explicit E prefix.
        backslash = quote == "'" and (engine == 'mysql' or
                    (i > 0 and sql[i - 1] in 'eE' and
                     (i == 1 or not re.match(r'[\w$]', sql[i - 2]))))
        i += 1
        while i < len(sql):
            if backslash and sql[i] == '\\':
                i += 2
            elif sql[i] == quote:
                i += 1
                if i < len(sql) and sql[i] == quote:
                    i += 1
                else:
                    break
            else:
                i += 1
        else:
            return None
        if quote == "'":
            chars[start:i] = ' ' * (i - start)
    return ''.join(chars)


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
    masked = _mask_literals(sql, engine) if engine in ('postgres', 'mysql') else None
    match = _SELECT.fullmatch(masked) if masked is not None else None
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
            k.column_name AS column_name, k.ordinal_position AS position, c.data_type AS data_type
            FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage k
              ON k.constraint_schema = tc.constraint_schema
              AND k.table_name = tc.table_name AND k.constraint_name = tc.constraint_name
            JOIN information_schema.columns c ON c.table_schema = k.table_schema
              AND c.table_name = k.table_name AND c.column_name = k.column_name
            WHERE tc.table_schema = COALESCE(NULLIF(:'schema', ''), DATABASE())
              AND tc.table_name = :'table'
              AND tc.constraint_type IN ('PRIMARY KEY', 'UNIQUE')
            ORDER BY primary_key DESC, key_id, position"""
        params = {'schema': schema or '', 'table': table}
    keys: dict[str, list[str]] = {}
    binary_columns: set[str] = set()
    for entry in core.run_query(conn, metadata, params=params, max_rows=0, timeout=15).rows:
        keys.setdefault(str(entry['key_id']), []).append(entry['column_name'])
        if engine == 'mysql' and str(entry.get('data_type', '')).lower() in (
                'binary', 'varbinary', 'tinyblob', 'blob', 'mediumblob', 'longblob', 'bit'):
            binary_columns.add(entry['column_name'])
    projected = None if match['cols'] == '*' else {
        _name(p, engine) for p in re.findall(_IDENT, match['cols'])}
    normalize = str.casefold if engine == 'mysql' else lambda name: name
    projected = None if projected is None else {normalize(name) for name in projected}
    values_by_name = {normalize(name): value for name, value in row.items()}
    for columns in keys.values():
        if any(col in binary_columns for col in columns):
            continue  # Serialized bytes are display values, not recoverable typed keys.
        if projected is not None and not {normalize(col) for col in columns} <= projected:
            continue
        values = [_literal(values_by_name.get(normalize(col)), engine) for col in columns]
        if not columns or any(value is None for value in values):
            continue
        predicates = [f'{quoted(col)} = {value}' for col, value in zip(columns, values)]
        return {'table': '.'.join(p if re.fullmatch(r'[a-z_][a-z0-9_$]*', p) else quoted(p)
                                  for p in parts),
                'sql': 'SELECT * FROM ' + '.'.join(quoted(p) for p in parts)
                       + ' WHERE ' + ' AND '.join(predicates) + ';'}
    return {'reason': 'no_unique_key'}
