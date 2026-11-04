use crate::*;
use futures_util::TryStreamExt;
use sqlx::Either;
use sqlx::{Column as _, Connection, Executor, Row, TypeInfo, ValueRef};
use std::{
    str::FromStr,
    time::{Duration, Instant},
};

fn error(code: ErrorCode, context: &str, err: impl std::fmt::Display) -> Error {
    let message = err.to_string();
    // Driver messages can contain endpoints but must not echo supplied connection strings/passwords.
    let safe = if message.contains("://") {
        "Database driver rejected the connection/query".to_owned()
    } else {
        message
    };
    Error::new(code, format!("{context}: {safe}"))
}
fn check_request(conn: &ConnectionSpec, r: &QueryRequest) -> Result<()> {
    if r.policy.allow_write && conn.production && !r.policy.confirm_production {
        return Err(Error::safety(
            "Production writes require explicit confirmation",
        ));
    }
    if r.policy.timeout_ms.unwrap_or(conn.timeout_ms) == 0
        || r.policy
            .connect_timeout_ms
            .unwrap_or(conn.connect_timeout_ms)
            == 0
    {
        return Err(Error::invalid("Timeouts must be positive"));
    }
    if r.cancellation.is_cancelled() {
        return Err(Error::new(
            ErrorCode::Cancelled,
            "Query cancelled before execution",
        ));
    }
    Ok(())
}
struct CancelQueryOnDrop {
    token: CancellationToken,
    armed: bool,
}
impl Drop for CancelQueryOnDrop {
    fn drop(&mut self) {
        if self.armed {
            self.token.cancel();
        }
    }
}
pub async fn execute(conn: &ConnectionSpec, request: QueryRequest) -> Result<QueryOutcome> {
    check_request(conn, &request)?;
    let (sql, cap) = policy::prepare(conn.engine, &request)?;
    let started = Instant::now();
    let timeout = request.policy.timeout_ms.unwrap_or(conn.timeout_ms);
    let mut outcome = QueryOutcome {
        execution_id: uuid::Uuid::new_v4().to_string(),
        engine: conn.engine,
        query: request.query.clone(),
        executed_query: sql.clone(),
        has_parameters: !request.parameters.is_empty(),
        columns: vec![],
        rows: vec![],
        affected_rows: 0,
        truncated: false,
        elapsed_ms: 0,
        download_bytes: 0,
        size_is_estimated: true,
    };
    let future = async {
        match conn.engine {
            Engine::Postgres => postgres(conn, &request, &sql, cap, &mut outcome).await,
            Engine::MySql => mysql(conn, &request, &sql, cap, &mut outcome).await,
            Engine::Sqlite => {
                outcome = sqlite(conn.clone(), request.clone(), sql.clone(), cap).await?;
                Ok(())
            }
            Engine::Redis => redis_query(conn, &request, &sql, cap, &mut outcome).await,
            Engine::Neptune => neptune(conn, &request, &sql, cap, &mut outcome).await,
        }
    };
    // Declared after the driver future so cancellation is signalled before that
    // future drops its connection/monitor guards. This also interrupts SQLite's
    // blocking worker when an embedding host drops an operation on its deadline.
    let mut on_drop = CancelQueryOnDrop {
        token: request.cancellation.clone(),
        armed: true,
    };
    let completed = tokio::select! {
        biased;
        _=request.cancellation.cancelled()=>Err(Error::new(ErrorCode::Cancelled,"Query cancellation requested")),
        result=tokio::time::timeout(Duration::from_millis(timeout),future)=>match result {
            Ok(result)=>{on_drop.armed=false;result},
            Err(_)=>{request.cancellation.cancel();Err(Error::new(ErrorCode::Timeout,"Query execution deadline exceeded"))}
        }
    };
    completed?;
    outcome.elapsed_ms = started.elapsed().as_millis() as u64;
    Ok(outcome)
}
fn push(
    out: &mut QueryOutcome,
    row: Vec<Value>,
    r: &QueryRequest,
    cap: Option<usize>,
) -> Result<bool> {
    if let Some(cap) = cap {
        if out.rows.len() >= cap {
            out.truncated = true;
            return Ok(false);
        }
    }
    let bytes = serde_json::to_vec(&row.iter().map(Value::to_json).collect::<Vec<_>>())
        .map_err(|_| Error::new(ErrorCode::Execution, "Cannot serialize query row"))?
        .len();
    out.download_bytes = out.download_bytes.saturating_add(bytes);
    if out.download_bytes > r.policy.max_bytes {
        return Err(Error::new(
            ErrorCode::Budget,
            "Query result exceeds the configured response byte budget",
        ));
    }
    out.rows.push(row);
    Ok(true)
}
fn normalize_json(v: JsonValue) -> JsonValue {
    match v {
        JsonValue::Number(n) => {
            let s = n.to_string();
            if s.contains(['.', 'e', 'E'])
                || n.as_i64()
                    .is_some_and(|x| x.unsigned_abs() > 9_007_199_254_740_991)
                || n.as_u64().is_some_and(|x| x > 9_007_199_254_740_991)
            {
                json!(s)
            } else {
                JsonValue::Number(n)
            }
        }
        JsonValue::Array(a) => JsonValue::Array(a.into_iter().map(normalize_json).collect()),
        JsonValue::Object(o) => {
            JsonValue::Object(o.into_iter().map(|(k, v)| (k, normalize_json(v))).collect())
        }
        other => other,
    }
}
fn decode_hex(s: &str) -> Result<Vec<u8>> {
    let s = s.strip_prefix("\\x").unwrap_or(s);
    if !s.len().is_multiple_of(2) {
        return Err(Error::new(
            ErrorCode::Execution,
            "Invalid hex database value",
        ));
    }
    s.as_bytes()
        .chunks(2)
        .map(|p| {
            let t = std::str::from_utf8(p)
                .map_err(|_| Error::new(ErrorCode::Execution, "Invalid hex value"))?;
            u8::from_str_radix(t, 16)
                .map_err(|_| Error::new(ErrorCode::Execution, "Invalid hex value"))
        })
        .collect()
}
fn decode_pg_bytea(text: &str) -> Result<Vec<u8>> {
    if text.starts_with("\\x") {
        return decode_hex(text);
    }
    let bytes = text.as_bytes();
    let mut decoded = Vec::with_capacity(bytes.len());
    let mut index = 0;
    while index < bytes.len() {
        if bytes[index] != b'\\' {
            decoded.push(bytes[index]);
            index += 1;
            continue;
        }
        if bytes.get(index + 1) == Some(&b'\\') {
            decoded.push(b'\\');
            index += 2;
            continue;
        }
        let octal = bytes
            .get(index + 1..index + 4)
            .ok_or_else(|| Error::new(ErrorCode::Execution, "Invalid PostgreSQL bytea escape"))?;
        if !octal.iter().all(|v| matches!(v, b'0'..=b'7')) || octal[0] > b'3' {
            return Err(Error::new(
                ErrorCode::Execution,
                "Invalid PostgreSQL bytea octal escape",
            ));
        }
        decoded.push((octal[0] - b'0') * 64 + (octal[1] - b'0') * 8 + (octal[2] - b'0'));
        index += 4;
    }
    Ok(decoded)
}
fn pg_text(ty: &str, text: &str) -> Result<Value> {
    let ty = ty.to_ascii_uppercase();
    if ty.ends_with("[]") || ty.starts_with('_') {
        return Ok(Value::Json(pg_array(text, &ty)?));
    }
    Ok(match ty.as_str() {
        "BOOL" | "BOOLEAN" => Value::Bool(text == "t" || text == "true"),
        "INT2" | "INT4" | "INT8" | "SMALLINT" | "INTEGER" | "BIGINT" | "OID" => text
            .parse::<i64>()
            .map(Value::Integer)
            .unwrap_or_else(|_| Value::Decimal(text.into())),
        "NUMERIC" | "DECIMAL" | "MONEY" => Value::Decimal(text.into()),
        "FLOAT4" | "FLOAT8" | "REAL" | "DOUBLE PRECISION" => Value::Float(match text {
            "Infinity" => f64::INFINITY,
            "-Infinity" => f64::NEG_INFINITY,
            "NaN" => f64::NAN,
            _ => text.parse().map_err(|_| {
                Error::new(
                    ErrorCode::Execution,
                    "Invalid floating point database value",
                )
            })?,
        }),
        "JSON" | "JSONB" => Value::Json(normalize_json(
            serde_json::from_str(text)
                .map_err(|_| Error::new(ErrorCode::Execution, "Invalid JSON database value"))?,
        )),
        "BYTEA" => Value::Binary(decode_pg_bytea(text)?),
        "DATE" | "TIMESTAMP" | "TIMESTAMPTZ" | "TIME" | "TIMETZ" | "INTERVAL" => {
            Value::DateTime(text.into())
        }
        _ => Value::Text(text.into()),
    })
}
fn pg_array(text: &str, ty: &str) -> Result<JsonValue> {
    // Text protocol array syntax handles dimensions, quoted delimiters, NULL and escaped values.
    let text = text.split_once('=').map(|(_, v)| v).unwrap_or(text);
    let bytes = text.as_bytes();
    let mut pos = 0;
    fn level(bytes: &[u8], pos: &mut usize, ty: &str) -> Result<JsonValue> {
        if bytes.get(*pos) != Some(&b'{') {
            return Err(Error::new(ErrorCode::Execution, "Invalid PostgreSQL array"));
        }
        *pos += 1;
        let mut values = vec![];
        while *pos < bytes.len() && bytes[*pos] != b'}' {
            if bytes[*pos] == b'{' {
                values.push(level(bytes, pos, ty)?)
            } else {
                let quoted = bytes[*pos] == b'"';
                let mut token = vec![];
                if quoted {
                    *pos += 1;
                    while *pos < bytes.len() {
                        let c = bytes[*pos];
                        *pos += 1;
                        if c == b'"' {
                            break;
                        }
                        if c == b'\\' {
                            if *pos >= bytes.len() {
                                return Err(Error::new(
                                    ErrorCode::Execution,
                                    "Invalid array escape",
                                ));
                            }
                            token.push(bytes[*pos]);
                            *pos += 1;
                        } else {
                            token.push(c)
                        }
                    }
                } else {
                    while *pos < bytes.len() && !matches!(bytes[*pos], b',' | b'}') {
                        token.push(bytes[*pos]);
                        *pos += 1;
                    }
                }
                let s = String::from_utf8(token)
                    .map_err(|_| Error::new(ErrorCode::Execution, "Invalid array UTF-8"))?;
                if !quoted && s == "NULL" {
                    values.push(JsonValue::Null)
                } else {
                    let element = ty.trim_end_matches("[]").trim_start_matches('_');
                    values.push(pg_text(element, &s)?.to_json());
                }
            }
            if bytes.get(*pos) == Some(&b',') {
                *pos += 1;
            } else if bytes.get(*pos) != Some(&b'}') {
                return Err(Error::new(ErrorCode::Execution, "Invalid array separator"));
            }
        }
        if bytes.get(*pos) != Some(&b'}') {
            return Err(Error::new(
                ErrorCode::Execution,
                "Unterminated PostgreSQL array",
            ));
        }
        *pos += 1;
        Ok(JsonValue::Array(values))
    }
    let result = level(bytes, &mut pos, ty)?;
    if pos != bytes.len() {
        return Err(Error::new(
            ErrorCode::Execution,
            "Trailing PostgreSQL array data",
        ));
    }
    Ok(result)
}
pub(crate) async fn protocol_ping(conn: &ConnectionSpec) -> Result<()> {
    if conn.timeout_ms == 0 || conn.connect_timeout_ms == 0 {
        return Err(Error::invalid("Timeouts must be positive"));
    }
    let operation = async {
        match conn.engine {
            Engine::Postgres => {
                let (mut db, _options, _transport) =
                    connect_postgres(conn, conn.connect_timeout_ms).await?;
                db.ping()
                    .await
                    .map_err(|e| error(ErrorCode::Connection, "PostgreSQL ping", e))
            }
            Engine::MySql => {
                let (mut db, _options, _transport) =
                    connect_mysql(conn, conn.connect_timeout_ms, true).await?;
                db.ping()
                    .await
                    .map_err(|e| error(ErrorCode::Connection, "MySQL ping", e))
            }
            _ => Err(Error::invalid(
                "Native protocol ping requires PostgreSQL or MySQL",
            )),
        }
    };
    tokio::time::timeout(
        Duration::from_millis(conn.timeout_ms.min(30_000)),
        operation,
    )
    .await
    .map_err(|_| Error::new(ErrorCode::Timeout, "Connection ping deadline exceeded"))?
}
async fn connect_postgres(
    conn: &ConnectionSpec,
    connect_timeout_ms: u64,
) -> Result<(
    sqlx::PgConnection,
    sqlx::postgres::PgConnectOptions,
    Option<crate::transport::TunnelBridge>,
)> {
    let mut options = sqlx::postgres::PgConnectOptions::from_str(&conn.url)
        .map_err(|e| error(ErrorCode::Connection, "PostgreSQL options", e))?;
    if let Some(path) = &conn.tls_ca_file {
        options = options.ssl_root_cert(path);
    }
    let transport = if let Some(endpoint) = &conn.network_endpoint {
        let socket_name = format!(".s.PGSQL.{}", options.get_port());
        let bridge = crate::transport::TunnelBridge::new(endpoint, &socket_name).await?;
        options = options.socket(bridge.directory());
        Some(bridge)
    } else {
        None
    };
    let ct = Duration::from_millis(connect_timeout_ms);
    let db = tokio::time::timeout(ct, sqlx::PgConnection::connect_with(&options))
        .await
        .map_err(|_| Error::new(ErrorCode::Connection, "PostgreSQL connection timed out"))?
        .map_err(|e| error(ErrorCode::Connection, "PostgreSQL connection", e))?;
    Ok((db, options, transport))
}
async fn postgres(
    conn: &ConnectionSpec,
    r: &QueryRequest,
    sql: &str,
    cap: Option<usize>,
    out: &mut QueryOutcome,
) -> Result<()> {
    let (mut db, options, transport) = connect_postgres(
        conn,
        r.policy
            .connect_timeout_ms
            .unwrap_or(conn.connect_timeout_ms),
    )
    .await?;
    let pid: i32 = sqlx::query_scalar("SELECT pg_backend_pid()")
        .fetch_one(&mut db)
        .await
        .map_err(|e| {
            error(
                ErrorCode::Connection,
                "Read own PostgreSQL backend identity",
                e,
            )
        })?;
    let _cancel = cancel_pg(
        options.clone(),
        pid,
        r.cancellation.clone(),
        transport.clone(),
    );
    let mut tx = db
        .begin()
        .await
        .map_err(|e| error(ErrorCode::Execution, "Begin transaction", e))?;
    if !r.policy.allow_write {
        sqlx::query("SET TRANSACTION READ ONLY")
            .execute(&mut *tx)
            .await
            .map_err(|e| error(ErrorCode::Safety, "Read-only transaction", e))?;
    }
    sqlx::query(&format!(
        "SET LOCAL statement_timeout = {}",
        r.policy.timeout_ms.unwrap_or(conn.timeout_ms)
    ))
    .execute(&mut *tx)
    .await
    .map_err(|e| error(ErrorCode::Execution, "Set statement timeout", e))?;
    let desc = (&mut *tx)
        .describe(sql)
        .await
        .map_err(|e| error(ErrorCode::Execution, "Describe PostgreSQL query", e))?;
    out.columns = desc
        .columns()
        .iter()
        .map(|c| Column {
            name: c.name().into(),
            type_name: Some(pg_type_name(c.type_info().name())),
        })
        .collect();
    let mut query = sqlx::raw_sql(sql).fetch_many(&mut *tx);
    while let Some(event) = query.try_next().await.map_err(|e| {
        let code = if e.to_string().contains("read-only") {
            ErrorCode::Safety
        } else {
            ErrorCode::Execution
        };
        sql_error(code, "PostgreSQL query", e)
    })? {
        match event {
            Either::Left(result) => {
                // PostgreSQL also reports SELECT row counts in CommandComplete.
                // They are returned rows, not changed rows.
                if !policy::is_read_only(Engine::Postgres, sql)? {
                    out.affected_rows = out.affected_rows.saturating_add(result.rows_affected());
                }
            }
            Either::Right(row) => {
                if out.columns.is_empty() {
                    out.columns = row
                        .columns()
                        .iter()
                        .map(|c| Column {
                            name: c.name().into(),
                            type_name: Some(pg_type_name(c.type_info().name())),
                        })
                        .collect();
                }
                let mut values = vec![];
                for i in 0..row.len() {
                    let raw = row
                        .try_get_raw(i)
                        .map_err(|e| error(ErrorCode::Execution, "Read PostgreSQL value", e))?;
                    if raw.is_null() {
                        values.push(Value::Null)
                    } else {
                        let bytes = raw
                            .as_bytes()
                            .map_err(|e| error(ErrorCode::Execution, "Read PostgreSQL value", e))?;
                        let text = std::str::from_utf8(bytes).map_err(|_| {
                            Error::new(ErrorCode::Execution, "Invalid PostgreSQL text value")
                        })?;
                        values.push(pg_text(row.columns()[i].type_info().name(), text)?);
                    }
                }
                if !push(out, values, r, cap)? {
                    break;
                }
            }
        }
    }
    drop(query);
    tx.commit()
        .await
        .map_err(|e| error(ErrorCode::Execution, "Commit query", e))?;
    Ok(())
}
fn mysql_value(row: &sqlx::mysql::MySqlRow, i: usize) -> Result<Value> {
    let raw = row
        .try_get_raw(i)
        .map_err(|e| error(ErrorCode::Execution, "Read MySQL value", e))?;
    if raw.is_null() {
        return Ok(Value::Null);
    }
    // raw_sql uses MySQL's text protocol: decode values by actual column type, retaining
    // decimal scale and zero dates instead of forcing every value through a Rust scalar.
    let bytes = row
        .try_get_unchecked::<Vec<u8>, _>(i)
        .map_err(|e| error(ErrorCode::Execution, "Read MySQL text value", e))?;
    mysql_text(row.columns()[i].type_info().name(), bytes)
}
fn mysql_text(ty: &str, bytes: Vec<u8>) -> Result<Value> {
    let text = std::str::from_utf8(&bytes).ok();
    if ty.contains("UNSIGNED") {
        return Ok(text
            .and_then(|s| s.parse::<u64>().ok())
            .map(Value::Unsigned)
            .unwrap_or(Value::Binary(bytes)));
    }
    Ok(match ty {
        "TINYINT" | "SMALLINT" | "MEDIUMINT" | "INT" | "INTEGER" | "BIGINT" | "YEAR" => text
            .and_then(|s| s.parse::<i64>().ok())
            .map(Value::Integer)
            .unwrap_or(Value::Binary(bytes)),
        "DECIMAL" | "NEWDECIMAL" => Value::Decimal(
            text.ok_or_else(|| Error::new(ErrorCode::Execution, "Invalid MySQL decimal"))?
                .to_owned(),
        ),
        "FLOAT" | "DOUBLE" => Value::Float(
            text.and_then(|s| s.parse().ok())
                .ok_or_else(|| Error::new(ErrorCode::Execution, "Invalid MySQL float"))?,
        ),
        "BINARY" | "VARBINARY" | "TINYBLOB" | "BLOB" | "MEDIUMBLOB" | "LONGBLOB" | "BIT"
        | "GEOMETRY" => Value::Binary(bytes),
        "JSON" => Value::Json(normalize_json(
            serde_json::from_slice(&bytes)
                .map_err(|_| Error::new(ErrorCode::Execution, "Invalid MySQL JSON"))?,
        )),
        "DATE" | "DATETIME" | "TIMESTAMP" | "TIME" => Value::DateTime(
            text.ok_or_else(|| Error::new(ErrorCode::Execution, "Invalid MySQL date/time"))?
                .to_owned(),
        ),
        _ => match String::from_utf8(bytes) {
            Ok(v) => Value::Text(v),
            Err(e) => Value::Binary(e.into_bytes()),
        },
    })
}
async fn connect_mysql(
    conn: &ConnectionSpec,
    connect_timeout_ms: u64,
    protocol_ping: bool,
) -> Result<(
    sqlx::MySqlConnection,
    sqlx::mysql::MySqlConnectOptions,
    Option<crate::transport::TunnelBridge>,
)> {
    let mut options = sqlx::mysql::MySqlConnectOptions::from_str(&conn.url)
        .map_err(|e| error(ErrorCode::Connection, "MySQL options", e))?;
    if let Some(path) = &conn.tls_ca_file {
        options = options.ssl_ca(path);
    }
    let transport = if let Some(endpoint) = &conn.network_endpoint {
        let bridge = crate::transport::TunnelBridge::new(endpoint, "mysql.sock").await?;
        options = options.socket(bridge.socket());
        Some(bridge)
    } else {
        None
    };
    if protocol_ping {
        // Protocol PING consumes no SQL values, so driver session normalization
        // is unnecessary. Keep TLS/authentication and forwarding identical.
        options = options
            .pipes_as_concat(false)
            .no_engine_substitution(false)
            .timezone(None)
            .set_names(false);
    }
    let ct = Duration::from_millis(connect_timeout_ms);
    let db = tokio::time::timeout(ct, sqlx::MySqlConnection::connect_with(&options))
        .await
        .map_err(|_| Error::new(ErrorCode::Connection, "MySQL connection timed out"))?
        .map_err(|e| error(ErrorCode::Connection, "MySQL connection", e))?;
    Ok((db, options, transport))
}
async fn mysql(
    conn: &ConnectionSpec,
    r: &QueryRequest,
    sql: &str,
    cap: Option<usize>,
    out: &mut QueryOutcome,
) -> Result<()> {
    let (mut db, options, transport) = connect_mysql(
        conn,
        r.policy
            .connect_timeout_ms
            .unwrap_or(conn.connect_timeout_ms),
        false,
    )
    .await?;
    let id: u64 = sqlx::query_scalar("SELECT CONNECTION_ID()")
        .fetch_one(&mut db)
        .await
        .map_err(|e| {
            error(
                ErrorCode::Connection,
                "Read own MySQL connection identity",
                e,
            )
        })?;
    let _cancel = cancel_mysql(
        options.clone(),
        id,
        r.cancellation.clone(),
        transport.clone(),
    );
    let timeout = r.policy.timeout_ms.unwrap_or(conn.timeout_ms);
    // MySQL SELECT cap; MariaDB has a distinct all-statement session cap.
    let _ = sqlx::query(&format!("SET SESSION MAX_EXECUTION_TIME = {timeout}"))
        .execute(&mut db)
        .await;
    let _ = sqlx::query(&format!(
        "SET SESSION max_statement_time = {}",
        (timeout as f64) / 1000.0
    ))
    .execute(&mut db)
    .await;
    if !r.policy.allow_write {
        sqlx::query("SET TRANSACTION READ ONLY")
            .execute(&mut db)
            .await
            .map_err(|e| error(ErrorCode::Safety, "MySQL read-only transaction", e))?;
    }
    let mut tx = db
        .begin()
        .await
        .map_err(|e| error(ErrorCode::Execution, "Begin MySQL transaction", e))?;
    let desc = (&mut *tx)
        .describe(sql)
        .await
        .map_err(|e| error(ErrorCode::Execution, "Describe MySQL query", e))?;
    out.columns = desc
        .columns()
        .iter()
        .map(|c| Column {
            name: c.name().into(),
            type_name: Some(c.type_info().name().into()),
        })
        .collect();
    let mut query = sqlx::raw_sql(sql).fetch_many(&mut *tx);
    while let Some(event) = query.try_next().await.map_err(|e| {
        sql_error(
            if e.to_string()
                .to_ascii_lowercase()
                .contains("read only transaction")
            {
                ErrorCode::Safety
            } else {
                ErrorCode::Execution
            },
            "MySQL query",
            e,
        )
    })? {
        match event {
            Either::Left(result) => {
                out.affected_rows = out.affected_rows.saturating_add(result.rows_affected())
            }
            Either::Right(row) => {
                let values = (0..row.len())
                    .map(|i| mysql_value(&row, i))
                    .collect::<Result<Vec<_>>>()?;
                if !push(out, values, r, cap)? {
                    break;
                }
            }
        }
    }
    drop(query);
    tx.commit()
        .await
        .map_err(|e| error(ErrorCode::Execution, "Commit MySQL query", e))?;
    Ok(())
}

pub(crate) fn sqlite_path(conn: &ConnectionSpec) -> Result<std::path::PathBuf> {
    if conn.network_endpoint.is_some() {
        return Err(Error::invalid("SQLite cannot use a network endpoint"));
    }
    let raw = if let Some(rest) = conn.url.strip_prefix("sqlite:///") {
        rest
    } else if conn.url.starts_with("sqlite:") {
        return Err(Error::invalid(
            "Use sqlite:///relative.db or sqlite:////absolute.db",
        ));
    } else {
        &conn.url
    };
    if raw.is_empty() || raw == ":memory:" || raw.contains(['?', '#']) {
        return Err(Error::invalid(
            "SQLite requires one existing file without URI options",
        ));
    }
    let mut bytes = vec![];
    let b = raw.as_bytes();
    let mut i = 0;
    while i < b.len() {
        if b[i] == b'%' && i + 2 < b.len() {
            let n = std::str::from_utf8(&b[i + 1..i + 3])
                .ok()
                .and_then(|s| u8::from_str_radix(s, 16).ok())
                .ok_or_else(|| Error::invalid("Invalid SQLite path escape"))?;
            bytes.push(n);
            i += 3;
        } else {
            bytes.push(b[i]);
            i += 1
        }
    }
    let raw = String::from_utf8(bytes).map_err(|_| Error::invalid("Invalid SQLite UTF-8 path"))?;
    if raw.contains('\0') {
        return Err(Error::invalid("Invalid SQLite path"));
    }
    let path = std::path::PathBuf::from(raw);
    let path = if path.is_absolute() {
        path
    } else {
        conn.workspace.join(path)
    };
    if !path.is_file() {
        return Err(Error::new(
            ErrorCode::Connection,
            "SQLite file does not exist or is not a regular file",
        ));
    }
    Ok(path)
}
async fn sqlite(
    conn: ConnectionSpec,
    r: QueryRequest,
    sql: String,
    cap: Option<usize>,
) -> Result<QueryOutcome> {
    let path = sqlite_path(&conn)?;
    tokio::task::spawn_blocking(move || {
        use rusqlite::{
            hooks::{AuthAction, AuthContext, Authorization},
            types::ValueRef,
            OpenFlags,
        };
        let start = Instant::now();
        let timeout = r.policy.timeout_ms.unwrap_or(conn.timeout_ms);
        let deadline = start + Duration::from_millis(timeout);
        let readonly = !r.policy.allow_write;
        let db = rusqlite::Connection::open_with_flags(
            path,
            if readonly {
                OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX
            } else {
                OpenFlags::SQLITE_OPEN_READ_WRITE | OpenFlags::SQLITE_OPEN_NO_MUTEX
            },
        )
        .map_err(|e| error(ErrorCode::Connection, "Open SQLite file", e))?;
        db.busy_timeout(Duration::from_millis(
            r.policy
                .connect_timeout_ms
                .unwrap_or(conn.connect_timeout_ms)
                .min(timeout),
        ))
        .map_err(|e| error(ErrorCode::Connection, "SQLite lock timeout", e))?;
        db.execute_batch(if readonly {
            "PRAGMA query_only=ON; PRAGMA foreign_keys=ON; BEGIN"
        } else {
            "PRAGMA foreign_keys=ON; BEGIN"
        })
        .map_err(|e| error(ErrorCode::Execution, "Begin SQLite transaction", e))?;
        let cancellation = r.cancellation.clone();
        db.progress_handler(
            1000,
            Some(move || Instant::now() >= deadline || cancellation.is_cancelled()),
        );
        let denied = std::sync::Arc::new(std::sync::atomic::AtomicBool::new(false));
        let denied_hook = denied.clone();
        db.authorizer(Some(move |ctx: AuthContext<'_>| {
            let forbidden = match ctx.action {
                AuthAction::Attach { .. }
                | AuthAction::Detach { .. }
                | AuthAction::Transaction { .. }
                | AuthAction::Savepoint { .. } => true,
                AuthAction::Function { function_name } => matches!(
                    function_name.to_ascii_lowercase().as_str(),
                    "load_extension" | "writefile" | "readfile"
                ),
                AuthAction::Pragma {
                    pragma_name,
                    pragma_value,
                } if readonly => {
                    let object = [
                        "table_info",
                        "table_xinfo",
                        "index_list",
                        "index_info",
                        "index_xinfo",
                        "foreign_key_list",
                    ];
                    let value = [
                        "database_list",
                        "compile_options",
                        "foreign_keys",
                        "query_only",
                        "schema_version",
                        "user_version",
                        "encoding",
                        "journal_mode",
                        "page_count",
                        "page_size",
                        "freelist_count",
                    ];
                    !(object.contains(&pragma_name.to_ascii_lowercase().as_str())
                        || (pragma_value.is_none()
                            && value.contains(&pragma_name.to_ascii_lowercase().as_str())))
                }
                AuthAction::Read { .. } | AuthAction::Select | AuthAction::Recursive => false,
                _ => readonly,
            };
            if forbidden {
                denied_hook.store(true, std::sync::atomic::Ordering::Relaxed);
                Authorization::Deny
            } else {
                Authorization::Allow
            }
        }));
        let result = (|| {
            let mut out = QueryOutcome {
                execution_id: uuid::Uuid::new_v4().to_string(),
                engine: Engine::Sqlite,
                query: r.query.clone(),
                executed_query: sql.clone(),
                has_parameters: !r.parameters.is_empty(),
                columns: vec![],
                rows: vec![],
                affected_rows: 0,
                truncated: false,
                elapsed_ms: 0,
                download_bytes: 0,
                size_is_estimated: true,
            };
            let mut statement = db
                .prepare(&sql)
                .map_err(|e| sqlite_error(e, &denied, &r, deadline))?;
            out.columns = statement
                .columns()
                .iter()
                .map(|c| Column {
                    name: c.name().into(),
                    type_name: c.decl_type().map(str::to_owned),
                })
                .collect();
            let is_write = !statement.readonly();
            if statement.column_count() == 0 {
                out.affected_rows = statement
                    .execute([])
                    .map_err(|e| sqlite_error(e, &denied, &r, deadline))?
                    as u64;
            } else {
                let count = statement.column_count();
                let mut rows = statement
                    .query([])
                    .map_err(|e| sqlite_error(e, &denied, &r, deadline))?;
                let manual_offset = cap.is_some() && !sql.contains("\nLIMIT ");
                let mut skipped = 0;
                while let Some(row) = rows
                    .next()
                    .map_err(|e| sqlite_error(e, &denied, &r, deadline))?
                {
                    if manual_offset && skipped < r.policy.offset {
                        skipped += 1;
                        continue;
                    }
                    let mut values = vec![];
                    for i in 0..count {
                        values.push(
                            match row
                                .get_ref(i)
                                .map_err(|e| error(ErrorCode::Execution, "Read SQLite value", e))?
                            {
                                ValueRef::Null => Value::Null,
                                ValueRef::Integer(v) => Value::Integer(v),
                                ValueRef::Real(v) => Value::Float(v),
                                ValueRef::Text(v) => match std::str::from_utf8(v) {
                                    Ok(s) => Value::Text(s.into()),
                                    Err(_) => Value::Binary(v.to_vec()),
                                },
                                ValueRef::Blob(v) => Value::Binary(v.to_vec()),
                            },
                        );
                    }
                    if !push(&mut out, values, &r, cap)? {
                        break;
                    }
                }
                if is_write {
                    out.affected_rows = db.changes();
                }
            }
            if r.cancellation.is_cancelled() {
                return Err(Error::new(ErrorCode::Cancelled, "SQLite query cancelled"));
            }
            if Instant::now() >= deadline {
                return Err(Error::new(
                    ErrorCode::Timeout,
                    "SQLite execution deadline exceeded",
                ));
            }
            Ok(out)
        })();
        db.authorizer(None::<fn(AuthContext<'_>) -> Authorization>);
        db.progress_handler(0, None::<fn() -> bool>);
        let mut out = result?;
        db.busy_timeout(deadline.saturating_duration_since(Instant::now()))
            .map_err(|e| error(ErrorCode::Execution, "SQLite commit budget", e))?;
        db.execute_batch("COMMIT")
            .map_err(|e| error(ErrorCode::Execution, "Commit SQLite transaction", e))?;
        out.elapsed_ms = start.elapsed().as_millis() as u64;
        Ok(out)
    })
    .await
    .map_err(|_| Error::new(ErrorCode::Execution, "SQLite worker stopped"))?
}
fn sqlite_error(
    e: rusqlite::Error,
    denied: &std::sync::atomic::AtomicBool,
    r: &QueryRequest,
    deadline: Instant,
) -> Error {
    let code = if denied.load(std::sync::atomic::Ordering::Relaxed) {
        ErrorCode::Safety
    } else if r.cancellation.is_cancelled() {
        ErrorCode::Cancelled
    } else if Instant::now() >= deadline {
        ErrorCode::Timeout
    } else {
        ErrorCode::Execution
    };
    error(code, "SQLite query", e)
}
struct RedisDriver(tokio::task::JoinHandle<()>);
impl Drop for RedisDriver {
    fn drop(&mut self) {
        self.0.abort();
    }
}
async fn redis_connection(
    conn: &ConnectionSpec,
    connect_timeout_ms: u64,
) -> Result<(redis::aio::MultiplexedConnection, Option<RedisDriver>)> {
    let client = redis::Client::open(conn.url.as_str())
        .map_err(|e| error(ErrorCode::Connection, "Redis options", e))?;
    let info = client.get_connection_info().clone();
    let operation = async {
        match &info.addr {
            redis::ConnectionAddr::TcpTls {
                host,
                port,
                insecure,
                ..
            } => {
                if *insecure {
                    return Err(Error::safety(
                        "Redis TLS requires certificate and hostname verification",
                    ));
                }
                let endpoint = conn.network_endpoint.clone().unwrap_or(NetworkEndpoint {
                    host: host.clone(),
                    port: *port,
                });
                let stream =
                    tokio::net::TcpStream::connect((endpoint.host.as_str(), endpoint.port))
                        .await
                        .map_err(|e| error(ErrorCode::Connection, "Redis TCP connection", e))?;
                let config = redis_tls_config(conn.tls_ca_file.as_deref())?;
                let hostname = tokio_rustls::rustls::pki_types::ServerName::try_from(host.clone())
                    .map_err(|_| Error::invalid("Invalid Redis TLS server name"))?;
                let stream = tokio_rustls::TlsConnector::from(std::sync::Arc::new(config))
                    .connect(hostname, stream)
                    .await
                    .map_err(|e| error(ErrorCode::Connection, "Redis TLS handshake", e))?;
                let (connection, driver) =
                    redis::aio::MultiplexedConnection::new_with_response_timeout(
                        &info.redis,
                        stream,
                        Some(Duration::from_millis(conn.timeout_ms)),
                    )
                    .await
                    .map_err(|e| error(ErrorCode::Connection, "Redis protocol setup", e))?;
                Ok((connection, Some(RedisDriver(tokio::spawn(driver)))))
            }
            _ => {
                let client = redis::Client::open(conn.effective_url()?)
                    .map_err(|e| error(ErrorCode::Connection, "Redis options", e))?;
                let mut connection = client
                    .get_multiplexed_async_connection()
                    .await
                    .map_err(|e| error(ErrorCode::Connection, "Redis connection", e))?;
                connection.set_response_timeout(Duration::from_millis(conn.timeout_ms));
                Ok((connection, None))
            }
        }
    };
    tokio::time::timeout(Duration::from_millis(connect_timeout_ms), operation)
        .await
        .map_err(|_| Error::new(ErrorCode::Connection, "Redis connection timed out"))?
}
fn redis_tls_config(ca: Option<&std::path::Path>) -> Result<tokio_rustls::rustls::ClientConfig> {
    use tokio_rustls::rustls;
    let mut roots = rustls::RootCertStore::empty();
    if let Some(ca) = ca {
        let data = std::fs::read(ca)
            .map_err(|_| Error::new(ErrorCode::Connection, "Cannot read Redis TLS CA file"))?;
        let certificates = rustls_pemfile::certs(&mut std::io::Cursor::new(data))
            .collect::<std::result::Result<Vec<_>, _>>()
            .map_err(|_| Error::invalid("Invalid Redis TLS CA PEM"))?;
        if certificates.is_empty() {
            return Err(Error::invalid("Redis TLS CA file contains no certificates"));
        }
        for certificate in certificates {
            roots
                .add(certificate)
                .map_err(|_| Error::invalid("Invalid Redis TLS CA certificate"))?;
        }
    } else {
        let native = rustls_native_certs::load_native_certs();
        for certificate in native.certs {
            let _ = roots.add(certificate);
        }
        if roots.is_empty() {
            return Err(Error::new(
                ErrorCode::Connection,
                "No trusted system certificates available for Redis TLS",
            ));
        }
    }
    let provider = std::sync::Arc::new(rustls::crypto::ring::default_provider());
    Ok(rustls::ClientConfig::builder_with_provider(provider)
        .with_safe_default_protocol_versions()
        .map_err(|_| {
            Error::new(
                ErrorCode::Connection,
                "Cannot configure Redis TLS protocols",
            )
        })?
        .with_root_certificates(roots)
        .with_no_client_auth())
}
fn redis_value(v: redis::Value) -> Result<Value> {
    use redis::Value as R;
    Ok(match v {
        R::Nil => Value::Null,
        R::Int(v) => Value::Integer(v),
        R::Double(v) => Value::Float(v),
        R::Boolean(v) => Value::Bool(v),
        R::BigNumber(v) => Value::Decimal(v.to_string()),
        R::SimpleString(v) => Value::Text(v),
        R::BulkString(v) => match String::from_utf8(v) {
            Ok(s) => Value::Text(s),
            Err(e) => Value::Binary(e.into_bytes()),
        },
        R::Okay => Value::Text("OK".into()),
        R::VerbatimString { text, .. } => Value::Text(text),
        R::Array(v) | R::Set(v) => Value::Json(JsonValue::Array(
            v.into_iter()
                .map(|x| redis_value(x).map(|v| v.to_json()))
                .collect::<Result<_>>()?,
        )),
        R::Map(v) => Value::Json(JsonValue::Array(
            v.into_iter()
                .map(|(k, v)| {
                    Ok(json!([
                        redis_value(k)?.to_json(),
                        redis_value(v)?.to_json()
                    ]))
                })
                .collect::<Result<Vec<_>>>()?,
        )),
        R::Attribute { data, .. } => redis_value(*data)?,
        R::Push { data, .. } => Value::Json(JsonValue::Array(
            data.into_iter()
                .map(|x| redis_value(x).map(|v| v.to_json()))
                .collect::<Result<_>>()?,
        )),
        R::ServerError(_) => {
            return Err(Error::new(
                ErrorCode::Execution,
                "Redis server returned an error",
            ));
        }
    })
}
async fn redis_query(
    conn: &ConnectionSpec,
    r: &QueryRequest,
    sql: &str,
    cap: Option<usize>,
    out: &mut QueryOutcome,
) -> Result<()> {
    let (mut db, _driver) = redis_connection(
        conn,
        r.policy
            .connect_timeout_ms
            .unwrap_or(conn.connect_timeout_ms),
    )
    .await?;
    let args =
        shell_words::split(sql).map_err(|_| Error::invalid("Invalid Redis command quoting"))?;
    let head = args
        .first()
        .ok_or_else(|| Error::invalid("Redis command is empty"))?;
    out.columns = vec![Column {
        name: "value".into(),
        type_name: None,
    }];
    if head.eq_ignore_ascii_case("--scan") {
        let mut pattern = None;
        let mut count = 500usize;
        let mut cursor = 0u64;
        let mut delay = 0.0f64;
        let mut i = 1;
        while i < args.len() {
            let option = args[i].as_str();
            i += 1;
            let value = args
                .get(i)
                .ok_or_else(|| Error::invalid("Missing Redis scan option value"))?;
            i += 1;
            match option {
                "--pattern" => pattern = Some(value),
                "--count" => {
                    count = value
                        .parse()
                        .map_err(|_| Error::invalid("Invalid scan count"))?
                }
                "--cursor" => {
                    cursor = value
                        .parse()
                        .map_err(|_| Error::invalid("Invalid scan cursor"))?
                }
                "-i" => {
                    delay = value
                        .parse()
                        .map_err(|_| Error::invalid("Invalid scan interval"))?
                }
                _ => return Err(Error::invalid("Unsupported Redis scan option")),
            }
        }
        if count == 0 || !delay.is_finite() || delay < 0.0 {
            return Err(Error::invalid("Invalid scan count/interval"));
        }
        loop {
            let mut command = redis::cmd("SCAN");
            command.arg(cursor).arg("COUNT").arg(count);
            if let Some(pattern) = pattern {
                command.arg("MATCH").arg(pattern);
            }
            let (next, values): (u64, Vec<Vec<u8>>) = command
                .query_async(&mut db)
                .await
                .map_err(|e| error(ErrorCode::Execution, "Redis SCAN", e))?;
            for value in values {
                if !push(
                    out,
                    vec![redis_value(redis::Value::BulkString(value))?],
                    r,
                    cap,
                )? {
                    return Ok(());
                }
            }
            cursor = next;
            if cursor == 0 {
                break;
            }
            if delay > 0.0 {
                tokio::time::sleep(Duration::from_secs_f64(delay)).await
            }
        }
    } else {
        let mut cmd = redis::cmd(head);
        for arg in &args[1..] {
            cmd.arg(arg);
        }
        let value: redis::Value = cmd
            .query_async(&mut db)
            .await
            .map_err(|e| error(ErrorCode::Execution, "Redis command", e))?;
        let values = match value {
            redis::Value::Array(v) | redis::Value::Set(v) => v,
            v => vec![v],
        };
        for v in values {
            if !push(out, vec![redis_value(v)?], r, cap)? {
                break;
            }
        }
    }
    Ok(())
}
async fn neptune(
    conn: &ConnectionSpec,
    r: &QueryRequest,
    sql: &str,
    cap: Option<usize>,
    out: &mut QueryOutcome,
) -> Result<()> {
    let mut url =
        url::Url::parse(&conn.url).map_err(|_| Error::invalid("Invalid Neptune endpoint"))?;
    if !matches!(url.scheme(), "http" | "https")
        || !url.username().is_empty()
        || url.password().is_some()
        || url.fragment().is_some()
    {
        return Err(Error::invalid(
            "Neptune requires an HTTP(S) endpoint without credentials or fragments",
        ));
    }
    let path = url.path().trim_end_matches('/');
    if path.is_empty() {
        url.set_path("/openCypher")
    } else if !path.ends_with("/openCypher") {
        url.set_path(&format!("{path}/openCypher"))
    }
    let mut builder = reqwest::Client::builder().no_proxy();
    let _transport = if let Some(endpoint) = &conn.network_endpoint {
        let bridge = crate::transport::TunnelBridge::new(endpoint, "neptune.sock").await?;
        #[cfg(unix)]
        {
            builder = builder.unix_socket(bridge.socket());
        }
        Some(bridge)
    } else {
        None
    };
    let loopback = url
        .host_str()
        .is_some_and(|host| matches!(host, "localhost" | "127.0.0.1" | "::1"));
    if !loopback && conn.network_endpoint.is_none() {
        if let Some(proxy) = &conn.proxy_url {
            builder = builder.proxy(
                reqwest::Proxy::all(proxy)
                    .map_err(|_| Error::invalid("Invalid configured HTTP proxy"))?,
            );
        }
    }
    if let Some(path) = &conn.tls_ca_file {
        let bytes = std::fs::read(path)
            .map_err(|_| Error::new(ErrorCode::Connection, "Cannot read configured TLS CA"))?;
        let certificate = reqwest::Certificate::from_pem(&bytes)
            .map_err(|_| Error::new(ErrorCode::Connection, "Invalid configured TLS CA"))?;
        builder = builder.add_root_certificate(certificate);
    }
    let client = builder
        .connect_timeout(Duration::from_millis(
            r.policy
                .connect_timeout_ms
                .unwrap_or(conn.connect_timeout_ms),
        ))
        .redirect(reqwest::redirect::Policy::none())
        .build()
        .map_err(|e| error(ErrorCode::Connection, "Neptune client", e))?;
    let mut response = client
        .post(url)
        .form(&[("query", sql)])
        .send()
        .await
        .map_err(|e| error(ErrorCode::Connection, "Neptune endpoint", e))?;
    if !response.status().is_success() {
        return Err(Error::new(
            ErrorCode::Execution,
            format!("Neptune HTTP {}", response.status().as_u16()),
        ));
    }
    let mut bytes = vec![];
    while let Some(chunk) = response
        .chunk()
        .await
        .map_err(|e| error(ErrorCode::Execution, "Neptune response", e))?
    {
        if bytes.len().saturating_add(chunk.len()) > r.policy.max_bytes {
            return Err(Error::new(
                ErrorCode::Budget,
                "Neptune response exceeds byte budget",
            ));
        }
        bytes.extend_from_slice(&chunk)
    }
    let data: JsonValue = serde_json::from_slice(&bytes)
        .map_err(|_| Error::new(ErrorCode::Execution, "Invalid Neptune response JSON"))?;
    let result = data
        .get("results")
        .or_else(|| data.get("result").and_then(|x| x.get("data")))
        .or_else(|| data.get("data"))
        .unwrap_or(&data);
    let list = result
        .as_array()
        .ok_or_else(|| Error::new(ErrorCode::Execution, "Unknown Neptune result shape"))?;
    let mut names = vec![];
    for row in list {
        if let Some(object) = row.as_object() {
            for name in object.keys() {
                if !names.contains(name) {
                    names.push(name.clone());
                }
            }
        } else if names.is_empty() {
            names.push("value".into())
        }
    }
    out.columns = names
        .iter()
        .map(|name| Column {
            name: name.clone(),
            type_name: None,
        })
        .collect();
    for row in list {
        let values = if let Some(object) = row.as_object() {
            names
                .iter()
                .map(|n| {
                    Value::Json(normalize_json(
                        object.get(n).cloned().unwrap_or(JsonValue::Null),
                    ))
                })
                .collect()
        } else {
            vec![Value::Json(normalize_json(row.clone()))]
        };
        if !push(out, values, r, cap)? {
            break;
        }
    }
    out.download_bytes = bytes.len();
    out.size_is_estimated = false;
    Ok(())
}

fn sql_error(default: ErrorCode, context: &str, e: sqlx::Error) -> Error {
    let code = e
        .as_database_error()
        .and_then(|e| e.code())
        .map(|c| c.to_string());
    let mapped = match code.as_deref() {
        Some("25006" | "1792") => ErrorCode::Safety,
        Some("57014" | "3024" | "1969") => ErrorCode::Timeout,
        Some("1317") => ErrorCode::Cancelled,
        _ => default,
    };
    error(mapped, context, e)
}
struct CancellationGuard(CancellationToken);
impl Drop for CancellationGuard {
    fn drop(&mut self) {
        self.0.cancel();
    }
}
fn cancel_pg(
    options: sqlx::postgres::PgConnectOptions,
    pid: i32,
    cancellation: CancellationToken,
    transport: Option<crate::transport::TunnelBridge>,
) -> CancellationGuard {
    let finished = CancellationToken::new();
    let done = finished.clone();
    tokio::spawn(async move {
        // Keep the forwarding listener alive until cancellation has reached the server.
        let _transport = transport;
        tokio::select! {biased;_ = cancellation.cancelled()=>{},_ = done.cancelled()=>return}
        let _ = tokio::time::timeout(Duration::from_secs(2), async {
            let mut connection = sqlx::PgConnection::connect_with(&options).await?;
            let _: bool = sqlx::query_scalar("SELECT pg_cancel_backend($1)")
                .bind(pid)
                .fetch_one(&mut connection)
                .await?;
            Ok::<(), sqlx::Error>(())
        })
        .await;
    });
    CancellationGuard(finished)
}
fn cancel_mysql(
    options: sqlx::mysql::MySqlConnectOptions,
    id: u64,
    cancellation: CancellationToken,
    transport: Option<crate::transport::TunnelBridge>,
) -> CancellationGuard {
    let finished = CancellationToken::new();
    let done = finished.clone();
    tokio::spawn(async move {
        // Keep the forwarding listener alive until cancellation has reached the server.
        let _transport = transport;
        tokio::select! {biased;_ = cancellation.cancelled()=>{},_ = done.cancelled()=>return}
        let _ = tokio::time::timeout(Duration::from_secs(2), async {
            let mut connection = sqlx::MySqlConnection::connect_with(&options).await?;
            sqlx::query(&format!("KILL QUERY {id}"))
                .execute(&mut connection)
                .await?;
            Ok::<(), sqlx::Error>(())
        })
        .await;
    });
    CancellationGuard(finished)
}

pub(crate) async fn redis_keys(conn: &ConnectionSpec) -> Result<JsonValue> {
    let mut r = QueryRequest::new("--scan --count 500");
    r.policy.max_rows = Some(400);
    let out = execute(conn, r).await?;
    let (mut db, _driver) = redis_connection(conn, conn.connect_timeout_ms).await?;
    // Redis keys are arbitrary bytes. Keep binary keys visible instead of silently
    // dropping them from the catalog when the legacy inspector accepts only UTF-8.
    let names = out
        .rows
        .iter()
        .map(|row| match row.first() {
            Some(Value::Text(key)) => Ok((json!(key), key.as_bytes().to_vec(), true)),
            Some(Value::Binary(key)) => {
                Ok((Value::Binary(key.clone()).to_json(), key.clone(), false))
            }
            _ => Err(Error::new(
                ErrorCode::Execution,
                "Invalid Redis SCAN key value",
            )),
        })
        .collect::<Result<Vec<_>>>()?;
    let mut pipe = redis::pipe();
    for (_, key, _) in &names {
        pipe.cmd("TYPE").arg(key).cmd("TTL").arg(key);
    }
    let values: Vec<redis::Value> = if names.is_empty() {
        vec![]
    } else {
        tokio::time::timeout(
            Duration::from_millis(conn.timeout_ms),
            pipe.query_async(&mut db),
        )
        .await
        .map_err(|_| Error::new(ErrorCode::Timeout, "Redis metadata deadline exceeded"))?
        .map_err(|e| error(ErrorCode::Execution, "Redis key metadata", e))?
    };
    let keys = names
        .iter()
        .enumerate()
        .map(|(i, (key, _, inspectable))| {
            let kind = values
                .get(i * 2)
                .cloned()
                .map(redis_value)
                .transpose()?
                .map(|v| v.to_json())
                .unwrap_or(json!("?"));
            let ttl = values
                .get(i * 2 + 1)
                .cloned()
                .map(redis_value)
                .transpose()?
                .map(|v| v.to_json())
                .unwrap_or(JsonValue::Null);
            Ok(json!({"key":key,"type":kind,"ttl":ttl,"inspectable":inspectable}))
        })
        .collect::<Result<Vec<_>>>()?;
    Ok(json!({"engine":"redis","keys":keys,"capped":out.truncated,"_cached":false}))
}
pub async fn inspect_key(conn: &ConnectionSpec, key: &str) -> Result<JsonValue> {
    if conn.engine != Engine::Redis {
        return Err(Error::invalid("Key inspection requires a Redis target"));
    }
    let start = Instant::now();
    let (mut db, _driver) = redis_connection(conn, conn.connect_timeout_ms).await?;
    let operation = async {
        let kind: String = redis::cmd("TYPE")
            .arg(key)
            .query_async(&mut db)
            .await
            .map_err(|e| error(ErrorCode::Execution, "Redis key type", e))?;
        let mut command = match kind.as_str() {
            "string" => redis::cmd("GET"),
            "hash" => redis::cmd("HGETALL"),
            "list" => redis::cmd("LRANGE"),
            "set" => redis::cmd("SMEMBERS"),
            "zset" => redis::cmd("ZRANGE"),
            _ => {
                return Ok(
                    json!({"columns":[{"name":"key","type":null},{"name":"type","type":null},{"name":"value","type":null}],"rows":[{"key":key,"type":kind,"value":"(unsupported type)"}],"rowCount":1,"truncated":false,"engine":"redis","sql":format!("# inspect {key}")}),
                );
            }
        };
        command.arg(key);
        if matches!(kind.as_str(), "list" | "zset") {
            command.arg(0).arg(-1);
        }
        if kind == "zset" {
            command.arg("WITHSCORES");
        }
        let raw: redis::Value = command
            .query_async(&mut db)
            .await
            .map_err(|e| error(ErrorCode::Execution, "Redis key inspect", e))?;
        let values = match raw {
            redis::Value::Array(v) | redis::Value::Set(v) => v,
            v => vec![v],
        };
        let capped = values.len() > 500;
        let mut rows = vec![];
        let mut bytes = 0usize;
        for value in values.into_iter().take(500) {
            let row = json!({"key":key,"type":kind,"value":redis_value(value)?.to_json()});
            bytes = bytes.saturating_add(
                serde_json::to_vec(&row)
                    .map_err(|_| Error::new(ErrorCode::Execution, "Invalid Redis row"))?
                    .len(),
            );
            if bytes > 64 * 1024 * 1024 {
                return Err(Error::new(
                    ErrorCode::Budget,
                    "Redis key response exceeds byte budget",
                ));
            }
            rows.push(row);
        }
        Ok(
            json!({"columns":[{"name":"key","type":null},{"name":"type","type":null},{"name":"value","type":null}],"rowCount":rows.len(),"rows":rows,"truncated":capped,"elapsedMs":start.elapsed().as_millis(),"engine":"redis","sql":format!("# inspect {key}"),"downloadBytes":bytes,"sizeIsEstimated":true}),
        )
    };
    tokio::time::timeout(Duration::from_millis(conn.timeout_ms), operation)
        .await
        .map_err(|_| Error::new(ErrorCode::Timeout, "Redis inspection deadline exceeded"))?
}

fn pg_type_name(name: &str) -> String {
    if let Some(element) = name.strip_suffix("[]") {
        return format!("{}[]", pg_type_name(element));
    }
    match name {
        "BOOL" => "boolean",
        "INT2" => "smallint",
        "INT4" => "integer",
        "INT8" => "bigint",
        "FLOAT4" => "real",
        "FLOAT8" => "double precision",
        "NUMERIC" => "numeric",
        "TEXT" => "text",
        "VARCHAR" => "character varying",
        "BPCHAR" => "character",
        "CHAR" => "\"char\"",
        "BYTEA" => "bytea",
        "TIMESTAMP" => "timestamp without time zone",
        "TIMESTAMPTZ" => "timestamp with time zone",
        "TIME" => "time without time zone",
        "TIMETZ" => "time with time zone",
        "DATE" => "date",
        "INTERVAL" => "interval",
        "JSON" => "json",
        "JSONB" => "jsonb",
        "UUID" => "uuid",
        "OID" => "oid",
        other => other,
    }
    .to_owned()
}

#[cfg(test)]
mod value_tests {
    use super::*;
    #[test]
    fn binary_values_keep_type_even_when_utf8_and_postgres_uses_escape_output() {
        assert_eq!(
            decode_pg_bytea(r"Abc\000\377\\").unwrap(),
            b"Abc\x00\xff\\".to_vec()
        );
        assert_eq!(
            decode_pg_bytea(r"\x41626300ff5c").unwrap(),
            b"Abc\x00\xff\\".to_vec()
        );
        assert!(decode_pg_bytea(r"\777").is_err());
        assert_eq!(
            mysql_text("VARBINARY", b"ASCII binary".to_vec())
                .unwrap()
                .to_json(),
            json!({"$binary":"QVNDSUkgYmluYXJ5"})
        );
        assert_eq!(
            mysql_text("VARCHAR", b"ASCII text".to_vec())
                .unwrap()
                .to_json(),
            json!("ASCII text")
        );
    }
}
