use crate::*;
use std::collections::BTreeMap;

async fn read(
    conn: &ConnectionSpec,
    sql: &str,
    params: &[(&str, &str)],
    limit: usize,
) -> Result<QueryOutcome> {
    let mut r = QueryRequest::new(sql);
    r.parameters = params
        .iter()
        .map(|(k, v)| (k.to_string(), v.to_string()))
        .collect();
    r.policy.max_rows = Some(limit);
    r.policy.timeout_ms = Some(conn.timeout_ms.min(30_000));
    execute(conn, r).await
}
fn strings(out: &QueryOutcome, key: &str) -> Vec<String> {
    out.rows_json()
        .into_iter()
        .filter_map(|r| r.get(key).and_then(JsonValue::as_str).map(str::to_owned))
        .collect()
}
pub async fn schemas(conn: &ConnectionSpec) -> Result<JsonValue> {
    let sql = match conn.engine {
        Engine::Postgres => {
            "SELECT schema_name FROM information_schema.schemata WHERE schema_name NOT IN ('pg_catalog','information_schema') AND schema_name NOT LIKE 'pg_%' ORDER BY schema_name"
        }
        Engine::MySql => {
            "SELECT schema_name AS schema_name FROM information_schema.schemata WHERE schema_name NOT IN ('information_schema','mysql','performance_schema','sys') ORDER BY schema_name"
        }
        Engine::Sqlite => return Ok(json!({"schemas":["main"]})),
        _ => return Ok(json!({"schemas":[]})),
    };
    let out = read(conn, sql, &[], 5000).await?;
    Ok(json!({"schemas":strings(&out,"schema_name"),"capped":out.truncated}))
}
pub async fn tables(conn: &ConnectionSpec) -> Result<JsonValue> {
    if conn.engine == Engine::Redis {
        return crate::drivers::redis_keys(conn).await;
    }

    let sql = match conn.engine {
        Engine::Postgres => {
            "SELECT CASE WHEN table_schema = 'public' AND position('.' in table_name) = 0 THEN table_name ELSE quote_ident(table_schema) || '.' || quote_ident(table_name) END AS table_name FROM information_schema.tables WHERE table_schema NOT IN ('pg_catalog','information_schema') AND table_schema NOT LIKE 'pg_%' ORDER BY table_schema,table_name"
        }
        Engine::MySql => {
            "SELECT table_name AS table_name FROM information_schema.tables WHERE table_schema = DATABASE() ORDER BY table_name"
        }
        Engine::Sqlite => {
            "SELECT name AS table_name FROM sqlite_schema WHERE type IN ('table','view') AND name NOT GLOB 'sqlite_*' ORDER BY name"
        }
        Engine::Neptune => {
            return Ok(json!({"engine":"neptune","tables":[],"capped":false,"experimental":true}));
        }
        Engine::Redis => unreachable!(),
    };
    let out = read(conn, sql, &[], 5000).await?;
    Ok(
        json!({"engine":conn.engine.as_str(),"tables":strings(&out,"table_name"),"capped":out.truncated,"_cached":false}),
    )
}
pub async fn columns(conn: &ConnectionSpec, table: &str) -> Result<JsonValue> {
    if table.trim().is_empty() {
        return Err(Error::invalid("Table name is empty"));
    }
    if matches!(conn.engine, Engine::Redis | Engine::Neptune) {
        return Ok(json!({"rows":[],"columns":[],"types":{},"primary_key":[],"foreign_keys":[]}));
    }
    let (schema_name, table_name) = relation_parts(conn.engine, table)?;
    let metadata_table = if conn.engine == Engine::Postgres {
        format!(
            "{}.{}",
            quote_identifier(conn.engine, &schema_name),
            quote_identifier(conn.engine, &table_name)
        )
    } else {
        table_name.clone()
    };
    let sql = match conn.engine {
        Engine::Postgres => {
            "SELECT cols.column_name,cols.data_type,cols.is_nullable,cols.column_default,cols.character_maximum_length,format_type(a.atttypid,a.atttypmod) AS formatted_type,col_description(c.oid,a.attnum) AS comment FROM information_schema.columns cols JOIN pg_namespace n ON n.nspname=cols.table_schema JOIN pg_class c ON c.relnamespace=n.oid AND c.relname=cols.table_name JOIN pg_attribute a ON a.attrelid=c.oid AND a.attname=cols.column_name AND a.attnum>0 WHERE cols.table_schema=:'schema' AND cols.table_name=:'name' ORDER BY cols.ordinal_position"
        }
        Engine::MySql => {
            "SELECT column_name AS column_name,data_type AS data_type,is_nullable AS is_nullable,column_default AS column_default,character_maximum_length AS character_maximum_length,column_type AS column_type,column_comment AS column_comment FROM information_schema.columns WHERE table_schema=COALESCE(NULLIF(:'schema',''),DATABASE()) AND table_name=:'name' ORDER BY ordinal_position"
        }
        Engine::Sqlite => {
            "SELECT name AS column_name,type AS data_type,CASE WHEN \"notnull\"=1 OR (pk>0 AND upper(type)='INTEGER' AND NOT EXISTS (SELECT 1 FROM pragma_index_list(:'table','main') WHERE origin='pk')) THEN 'NO' ELSE 'YES' END AS is_nullable,dflt_value AS column_default,NULL AS character_maximum_length,pk AS primary_key,hidden FROM pragma_table_xinfo(:'table','main') ORDER BY cid"
        }
        _ => unreachable!(),
    };
    let description = read(
        conn,
        sql,
        &[
            ("table", metadata_table.as_str()),
            ("schema", &schema_name),
            ("name", &table_name),
        ],
        2000,
    )
    .await?;
    let mut capped = description.truncated;
    let rows = description.rows_json();
    let names: Vec<_> = rows
        .iter()
        .filter_map(|r| r.get("column_name").cloned())
        .collect();
    let types: serde_json::Map<String, JsonValue> = rows
        .iter()
        .filter_map(|r| {
            Some((
                r.get("column_name")?.as_str()?.to_string(),
                r.get("data_type")?.clone(),
            ))
        })
        .collect();
    let (keys, keys_capped) = key_metadata(conn, table).await?;
    capped |= keys_capped;
    let mut primary = vec![];
    let mut seen = std::collections::HashSet::new();
    for k in &keys {
        let value = k.get("primary_key");
        let is_primary = value == Some(&json!(true))
            || value == Some(&json!(1))
            || value == Some(&json!("1"))
            || value == Some(&json!("t"));
        if is_primary {
            if let Some(name) = k.get("column_name").and_then(JsonValue::as_str) {
                if seen.insert(name.to_owned()) {
                    primary.push(name.to_owned());
                }
            }
        }
    }
    let fk_sql = match conn.engine {
        Engine::Postgres => {
            "SELECT c.conname AS constraint_name, a.attname AS column_name, rn.nspname AS referenced_schema, rc.relname AS referenced_table, ra.attname AS referenced_column, k.ordinality AS position FROM pg_constraint c JOIN pg_class rc ON rc.oid=c.confrelid JOIN pg_namespace rn ON rn.oid=rc.relnamespace CROSS JOIN LATERAL unnest(c.conkey,c.confkey) WITH ORDINALITY AS k(local_attnum,remote_attnum,ordinality) JOIN pg_attribute a ON a.attrelid=c.conrelid AND a.attnum=k.local_attnum JOIN pg_attribute ra ON ra.attrelid=c.confrelid AND ra.attnum=k.remote_attnum WHERE c.contype='f' AND c.conrelid=to_regclass(:'table') ORDER BY c.conname,k.ordinality"
        }
        Engine::MySql => {
            "SELECT constraint_name AS constraint_name,column_name AS column_name,referenced_table_schema AS referenced_schema,referenced_table_name AS referenced_table,referenced_column_name AS referenced_column,ordinal_position AS position FROM information_schema.key_column_usage WHERE table_schema=COALESCE(NULLIF(:'schema',''),DATABASE()) AND table_name=:'name' AND referenced_table_name IS NOT NULL ORDER BY constraint_name,ordinal_position"
        }
        Engine::Sqlite => {
            "SELECT CAST(f.id AS TEXT) AS constraint_name,f.\"from\" AS column_name,'main' AS referenced_schema,f.\"table\" AS referenced_table,COALESCE(f.\"to\",p.name) AS referenced_column,f.seq+1 AS position FROM pragma_foreign_key_list(:'table','main') f LEFT JOIN pragma_table_info(f.\"table\",'main') p ON p.pk=f.seq+1 ORDER BY f.id,f.seq"
        }
        _ => unreachable!(),
    };
    let fk_description = read(
        conn,
        fk_sql,
        &[
            ("table", metadata_table.as_str()),
            ("schema", &schema_name),
            ("name", &table_name),
        ],
        2000,
    )
    .await?;
    capped |= fk_description.truncated;
    let fk_rows = fk_description.rows_json();
    let mut grouped: BTreeMap<String, JsonValue> = BTreeMap::new();
    for row in fk_rows {
        let key = row
            .get("constraint_name")
            .map(|v| {
                v.as_str()
                    .map(str::to_owned)
                    .unwrap_or_else(|| v.to_string())
            })
            .unwrap_or_default();
        let item=grouped.entry(key.clone()).or_insert_with(||json!({"name":key,"columns":[],"referenced_schema":row.get("referenced_schema"),"referenced_table":row.get("referenced_table"),"referenced_columns":[]}));
        item["columns"]
            .as_array_mut()
            .unwrap()
            .push(row.get("column_name").cloned().unwrap_or(JsonValue::Null));
        item["referenced_columns"].as_array_mut().unwrap().push(
            row.get("referenced_column")
                .cloned()
                .unwrap_or(JsonValue::Null),
        );
    }
    Ok(
        json!({"rows":rows,"columns":names,"types":types,"primary_key":primary,"foreign_keys":grouped.into_values().collect::<Vec<_>>(),"keys":keys,"capped":capped}),
    )
}
async fn key_metadata(conn: &ConnectionSpec, table: &str) -> Result<(Vec<JsonValue>, bool)> {
    let (schema_name, table_name) = relation_parts(conn.engine, table)?;
    let metadata_table = if conn.engine == Engine::Postgres {
        format!(
            "{}.{}",
            quote_identifier(conn.engine, &schema_name),
            quote_identifier(conn.engine, &table_name)
        )
    } else {
        table_name.clone()
    };
    let sql = match conn.engine {
        Engine::Postgres => {
            "SELECT i.indexrelid::text AS key_id,i.indisprimary AS primary_key,a.attname AS column_name,k.ordinality AS position FROM pg_index i JOIN pg_class c ON c.oid=i.indrelid CROSS JOIN LATERAL unnest(i.indkey) WITH ORDINALITY AS k(attnum,ordinality) JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=k.attnum WHERE c.oid=to_regclass(:'table') AND c.relkind IN ('r','p') AND i.indisunique AND i.indisvalid AND i.indisready AND i.indpred IS NULL AND i.indexprs IS NULL AND (c.relkind='p' OR NOT EXISTS(SELECT 1 FROM pg_inherits WHERE inhparent=c.oid)) AND k.ordinality<=i.indnkeyatts ORDER BY i.indisprimary DESC,i.indexrelid,k.ordinality"
        }
        Engine::MySql => {
            "SELECT tc.constraint_name AS key_id,(tc.constraint_type='PRIMARY KEY') AS primary_key,k.column_name AS column_name,k.ordinal_position AS position,c.data_type AS data_type FROM information_schema.table_constraints tc JOIN information_schema.key_column_usage k ON k.constraint_schema=tc.constraint_schema AND k.table_name=tc.table_name AND k.constraint_name=tc.constraint_name JOIN information_schema.columns c ON c.table_schema=k.table_schema AND c.table_name=k.table_name AND c.column_name=k.column_name WHERE tc.table_schema=COALESCE(NULLIF(:'schema',''),DATABASE()) AND tc.table_name=:'name' AND tc.constraint_type IN ('PRIMARY KEY','UNIQUE') ORDER BY primary_key DESC,key_id,position"
        }
        Engine::Sqlite => {
            "SELECT 'primary' AS key_id,1 AS primary_key,t.name AS column_name,t.pk AS position FROM pragma_table_xinfo(:'table','main') t WHERE t.pk>0 AND EXISTS(SELECT 1 FROM sqlite_schema WHERE name=:'table' COLLATE NOCASE AND type='table') UNION ALL SELECT 'unique:'||i.name AS key_id,0 AS primary_key,x.name AS column_name,x.seqno+1 AS position FROM pragma_index_list(:'table','main') i JOIN pragma_index_xinfo(i.name,'main') x ON x.\"key\"=1 WHERE i.\"unique\"=1 AND i.partial=0 AND i.origin!='pk' AND NOT EXISTS(SELECT 1 FROM pragma_index_xinfo(i.name,'main') invalid WHERE invalid.\"key\"=1 AND invalid.cid<0) ORDER BY primary_key DESC,key_id,position"
        }
        _ => return Ok((vec![], false)),
    };
    let out = read(
        conn,
        sql,
        &[
            ("table", metadata_table.as_str()),
            ("schema", &schema_name),
            ("name", &table_name),
        ],
        2000,
    )
    .await?;
    Ok((out.rows_json(), out.truncated))
}
pub async fn ping(conn: &ConnectionSpec) -> Result<JsonValue> {
    let started = std::time::Instant::now();
    if matches!(conn.engine, Engine::Postgres | Engine::MySql) {
        crate::drivers::protocol_ping(conn).await?;
    } else {
        let sql = match conn.engine {
            Engine::Redis => "PING",
            Engine::Neptune => "RETURN 1 AS quarry_ping",
            _ => "SELECT 1 AS quarry_ping",
        };
        let mut r = QueryRequest::new(sql);
        r.policy.timeout_ms = Some(conn.timeout_ms.min(30_000));
        r.policy.max_rows = Some(1);
        execute(conn, r).await?;
    }
    Ok(
        json!({"ok":true,"elapsed_ms":started.elapsed().as_millis(),"engine":conn.engine.as_str(),"db":conn.db,"env":conn.env}),
    )
}
fn quote_identifier(engine: Engine, name: &str) -> String {
    if engine == Engine::MySql {
        format!("`{}`", name.replace('`', "``"))
    } else {
        format!("\"{}\"", name.replace('"', "\"\""))
    }
}
fn scalar_literal(engine: Engine, v: &JsonValue) -> Option<String> {
    match v {
        JsonValue::Null => None,
        JsonValue::Bool(v) => Some(if *v { "TRUE" } else { "FALSE" }.into()),
        JsonValue::Number(n) => {
            if n.as_i64()
                .is_some_and(|n| n.unsigned_abs() > 9_007_199_254_740_991)
                || n.as_u64().is_some_and(|n| n > 9_007_199_254_740_991)
            {
                None
            } else {
                Some(n.to_string())
            }
        }
        JsonValue::String(s) => {
            let postgres_escape = engine == Engine::Postgres && s.contains('\\');
            let s = s.replace('\'', "''");
            let s = if matches!(engine, Engine::Postgres | Engine::MySql) {
                s.replace('\\', "\\\\")
            } else {
                s
            };
            Some(format!("{}'{}'", if postgres_escape { "E" } else { "" }, s))
        }
        _ => None,
    }
}
pub async fn record_query(
    conn: &ConnectionSpec,
    query: &str,
    row: &JsonValue,
) -> Result<JsonValue> {
    if !matches!(
        conn.engine,
        Engine::Postgres | Engine::MySql | Engine::Sqlite
    ) {
        return Ok(json!({"reason":"unsupported_query"}));
    }
    let identifier = r#"(?:"(?:[^"]|"")+"|`(?:[^`]|``)+`|[a-zA-Z_][a-zA-Z0-9_$]*)"#;
    let pattern = format!(
        r"(?is)^\s*select\s+(?P<projection>\*|(?:{identifier})(?:\s*,\s*{identifier})*)\s+from\s+(?P<table>{identifier}(?:\s*\.\s*{identifier})?)(?:\s+(?:where|order\s+by|limit|offset|fetch)\b.*)?\s*;?\s*$"
    );
    let regex = regex::Regex::new(&pattern)
        .map_err(|_| Error::invalid("Invalid record locator pattern"))?;
    let Some(found) = regex.captures(query) else {
        return Ok(json!({"reason":"unsupported_query"}));
    };
    let sk = policy::skeleton(query)?;
    if regex::Regex::new(r"(?i)\b(join|union|intersect|except|group|having|into|for)\b")
        .unwrap()
        .is_match(&sk)
        || sk.to_ascii_lowercase().matches("select").count() != 1
    {
        return Ok(json!({"reason":"unsupported_query"}));
    }
    let table = found.name("table").unwrap().as_str().trim();
    let (schema_name, table_name) = relation_identifier_parts(conn.engine, table)?;
    if conn.engine == Engine::Sqlite && schema_name != "main" {
        return Ok(json!({"reason":"unsupported_query"}));
    }
    let table_lookup = if conn.engine == Engine::Sqlite {
        table_name.clone()
    } else {
        table.to_string()
    };
    let quoted_table = if policy::skeleton(table)?.contains('.') {
        format!(
            "{}.{}",
            quote_identifier(conn.engine, &schema_name),
            quote_identifier(conn.engine, &table_name)
        )
    } else {
        quote_identifier(conn.engine, &table_name)
    };
    let (keys, capped) = key_metadata(conn, &table_lookup).await?;
    if capped {
        return Ok(json!({"reason":"metadata_truncated"}));
    }
    let mut groups: Vec<(String, Vec<JsonValue>)> = vec![];
    for key in keys {
        let id = key["key_id"]
            .as_str()
            .map(str::to_owned)
            .unwrap_or_else(|| key["key_id"].to_string());
        if let Some((_, items)) = groups.iter_mut().find(|(k, _)| k == &id) {
            items.push(key)
        } else {
            groups.push((id, vec![key]));
        }
    }
    let Some(row) = row.as_object() else {
        return Ok(json!({"reason":"no_unique_key"}));
    };
    for (_, keys) in groups {
        let mut clauses = vec![];
        let mut columns = vec![];
        for key in keys {
            let Some(name) = key["column_name"].as_str() else {
                clauses.clear();
                break;
            };
            let binary = matches!(
                key.get("data_type").and_then(JsonValue::as_str),
                Some(
                    "binary"
                        | "varbinary"
                        | "tinyblob"
                        | "blob"
                        | "mediumblob"
                        | "longblob"
                        | "bit"
                )
            );
            if binary {
                clauses.clear();
                break;
            }
            let value = row
                .iter()
                .find(|(k, _)| {
                    if conn.engine == Engine::Postgres {
                        k.as_str() == name
                    } else {
                        k.eq_ignore_ascii_case(name)
                    }
                })
                .map(|(_, v)| v);
            let Some(literal) = value.and_then(|v| scalar_literal(conn.engine, v)) else {
                clauses.clear();
                break;
            };
            clauses.push(format!(
                "{} = {}",
                quote_identifier(conn.engine, name),
                literal
            ));
            columns.push(name.to_owned());
        }
        if !clauses.is_empty() {
            return Ok(
                json!({"sql":format!("SELECT * FROM {quoted_table} WHERE {};",clauses.join(" AND ")),"table":if conn.engine==Engine::Sqlite {table_lookup.as_str()}else{table},"keyColumns":columns}),
            );
        }
    }
    Ok(json!({"reason":"no_unique_key"}))
}

fn relation_parts(engine: Engine, input: &str) -> Result<(String, String)> {
    if engine == Engine::Sqlite && !input.trim().starts_with(['"', '`']) {
        return Ok(("main".into(), input.trim().into()));
    }
    relation_identifier_parts(engine, input)
}
fn relation_identifier_parts(engine: Engine, input: &str) -> Result<(String, String)> {
    let default = if engine == Engine::Postgres {
        "public"
    } else if engine == Engine::Sqlite {
        "main"
    } else {
        ""
    };
    let input = input.trim();
    if !input.contains('.') && !input.starts_with(['"', '`']) {
        return Ok((default.into(), input.into()));
    }
    let mut parts = vec![];
    let mut current = String::new();
    let mut chars = input.chars().peekable();
    let mut quote = None;
    while let Some(ch) = chars.next() {
        if let Some(q) = quote {
            if ch == q {
                if chars.peek() == Some(&q) {
                    current.push(q);
                    chars.next();
                } else {
                    quote = None
                }
            } else {
                current.push(ch)
            }
        } else if matches!(ch, '"' | '`') {
            quote = Some(ch)
        } else if ch == '.' {
            parts.push(current.trim().to_owned());
            current.clear();
        } else {
            current.push(ch)
        }
    }
    if quote.is_some() {
        return Err(Error::invalid("Unterminated relation identifier"));
    }
    parts.push(current.trim().to_owned());
    match parts.as_slice() {
        [table] if !table.is_empty() => Ok((default.into(), table.clone())),
        [schema, table] if !schema.is_empty() && !table.is_empty() => {
            Ok((schema.clone(), table.clone()))
        }
        _ => Err(Error::invalid("Expected a table or schema-qualified table")),
    }
}
