//! Database execution independent of CLI, HTTP, IAM or process-global workspaces.
mod drivers;
mod metadata;
pub mod policy;
mod transport;

pub use drivers::inspect_key;
pub use metadata::{columns, ping, record_query, schemas, tables};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value as JsonValue};
use std::{collections::BTreeMap, path::PathBuf};
pub use tokio_util::sync::CancellationToken;

pub type Result<T> = std::result::Result<T, Error>;
#[derive(Debug, Clone, Serialize, Deserialize, thiserror::Error)]
#[error("{message}")]
pub struct Error {
    pub code: ErrorCode,
    pub message: String,
}
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ErrorCode {
    InvalidInput,
    Connection,
    Execution,
    Safety,
    Cancelled,
    Timeout,
    Budget,
    Busy,
    Unsupported,
}
impl Error {
    pub fn new(code: ErrorCode, message: impl Into<String>) -> Self {
        Self {
            code,
            message: message.into(),
        }
    }
    pub fn invalid(s: impl Into<String>) -> Self {
        Self::new(ErrorCode::InvalidInput, s)
    }
    pub fn safety(s: impl Into<String>) -> Self {
        Self::new(ErrorCode::Safety, s)
    }
    pub fn exit_code(&self) -> i32 {
        match self.code {
            ErrorCode::Connection => 2,
            ErrorCode::Safety => 8,
            ErrorCode::InvalidInput => 1,
            _ => 3,
        }
    }
}
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Engine {
    #[default]
    Postgres,
    #[serde(rename = "mysql")]
    MySql,
    Sqlite,
    Redis,
    Neptune,
}
impl Engine {
    pub fn infer(url: &str) -> Result<Self> {
        let s = url.to_ascii_lowercase();
        if s.starts_with("postgres://") || s.starts_with("postgresql://") {
            Ok(Self::Postgres)
        } else if s.starts_with("mysql://") {
            Ok(Self::MySql)
        } else if s.starts_with("sqlite:") {
            Ok(Self::Sqlite)
        } else if s.starts_with("redis://") || s.starts_with("rediss://") {
            Ok(Self::Redis)
        } else if s.starts_with("https://") || s.starts_with("http://") {
            Ok(Self::Neptune)
        } else {
            Err(Error::invalid(
                "Unknown database URL; set an explicit engine",
            ))
        }
    }
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Postgres => "postgres",
            Self::MySql => "mysql",
            Self::Sqlite => "sqlite",
            Self::Redis => "redis",
            Self::Neptune => "neptune",
        }
    }
    pub fn capabilities(self) -> EngineCapabilities {
        EngineCapabilities {
            transactions: matches!(self, Self::Postgres | Self::MySql | Self::Sqlite),
            schema: !matches!(self, Self::Redis | Self::Neptune),
            local_lifecycle: matches!(self, Self::Postgres | Self::Redis | Self::Neptune),
            schema_sync: self == Self::Postgres,
            experimental: self == Self::Neptune,
        }
    }
}
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct EngineCapabilities {
    pub transactions: bool,
    pub schema: bool,
    pub local_lifecycle: bool,
    pub schema_sync: bool,
    pub experimental: bool,
}
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct TargetId {
    pub platform: String,
    pub workspace: String,
    pub database: String,
    pub environment: String,
    pub execution_location: String,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct NetworkEndpoint {
    pub host: String,
    pub port: u16,
}
#[derive(Clone, Serialize, Deserialize)]
#[serde(default)]
pub struct ConnectionSpec {
    pub key: String,
    pub db: String,
    pub env: String,
    pub engine: Engine,
    pub url: String,
    pub production: bool,
    pub workspace: PathBuf,
    pub timeout_ms: u64,
    pub connect_timeout_ms: u64,
    pub network_endpoint: Option<NetworkEndpoint>,
    pub tls_ca_file: Option<PathBuf>,
    pub proxy_url: Option<String>,
}
impl std::fmt::Debug for ConnectionSpec {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("ConnectionSpec")
            .field("key", &self.key)
            .field("engine", &self.engine)
            .field("url", &"[redacted]")
            .finish()
    }
}
impl Default for ConnectionSpec {
    fn default() -> Self {
        Self {
            key: String::new(),
            db: String::new(),
            env: "dev".into(),
            engine: Engine::Postgres,
            url: String::new(),
            production: false,
            workspace: PathBuf::from("."),
            timeout_ms: 300_000,
            connect_timeout_ms: 15_000,
            network_endpoint: None,
            tls_ca_file: None,
            proxy_url: None,
        }
    }
}
impl ConnectionSpec {
    pub fn new(engine: Engine, url: impl Into<String>) -> Self {
        Self {
            engine,
            url: url.into(),
            ..Self::default()
        }
    }
    pub fn effective_url(&self) -> Result<String> {
        if let Some(e) = &self.network_endpoint {
            if self.engine == Engine::Sqlite {
                return Err(Error::invalid("SQLite does not support network forwarding"));
            }
            let mut url =
                url::Url::parse(&self.url).map_err(|_| Error::invalid("Invalid database URL"))?;
            url.set_host(Some(&e.host))
                .map_err(|_| Error::invalid("Invalid forwarded host"))?;
            url.set_port(Some(e.port))
                .map_err(|_| Error::invalid("Invalid forwarded port"))?;
            Ok(url.to_string())
        } else {
            Ok(self.url.clone())
        }
    }
}
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default)]
pub struct ExecutionPolicy {
    pub allow_write: bool,
    pub confirm_production: bool,
    pub max_rows: Option<usize>,
    pub offset: usize,
    pub max_bytes: usize,
    pub timeout_ms: Option<u64>,
    pub connect_timeout_ms: Option<u64>,
}
impl Default for ExecutionPolicy {
    fn default() -> Self {
        Self {
            allow_write: false,
            confirm_production: false,
            max_rows: Some(500),
            offset: 0,
            max_bytes: 64 * 1024 * 1024,
            timeout_ms: None,
            connect_timeout_ms: None,
        }
    }
}
#[derive(Debug, Clone)]
pub struct QueryRequest {
    pub query: String,
    pub parameters: BTreeMap<String, String>,
    pub policy: ExecutionPolicy,
    pub cancellation: CancellationToken,
}
impl QueryRequest {
    pub fn new(query: impl Into<String>) -> Self {
        Self {
            query: query.into(),
            parameters: BTreeMap::new(),
            policy: ExecutionPolicy::default(),
            cancellation: CancellationToken::new(),
        }
    }
}
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Column {
    pub name: String,
    #[serde(rename = "type")]
    pub type_name: Option<String>,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "kind", content = "value", rename_all = "snake_case")]
pub enum Value {
    Null,
    Bool(bool),
    Integer(i64),
    Unsigned(u64),
    Decimal(String),
    Float(f64),
    Text(String),
    Json(JsonValue),
    Binary(Vec<u8>),
    DateTime(String),
}
impl Value {
    pub fn to_json(&self) -> JsonValue {
        use base64::Engine as _;
        match self {
            Self::Null => JsonValue::Null,
            Self::Bool(v) => json!(v),
            Self::Integer(v) if v.unsigned_abs() <= 9_007_199_254_740_991 => json!(v),
            Self::Unsigned(v) if *v <= 9_007_199_254_740_991 => json!(v),
            Self::Integer(v) => json!(v.to_string()),
            Self::Unsigned(v) => json!(v.to_string()),
            Self::Decimal(v) | Self::Text(v) | Self::DateTime(v) => json!(v),
            Self::Float(v) if v.is_finite() => json!(v),
            Self::Float(v) => {
                json!({"$number":if v.is_nan(){"NaN"}else if v.is_sign_positive(){"Infinity"}else{"-Infinity"}})
            }
            Self::Json(v) => v.clone(),
            Self::Binary(v) => {
                json!({"$binary":base64::engine::general_purpose::STANDARD.encode(v)})
            }
        }
    }
}
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct QueryOutcome {
    pub execution_id: String,
    pub engine: Engine,
    pub query: String,
    #[serde(skip_serializing, default)]
    pub executed_query: String,
    #[serde(default)]
    pub has_parameters: bool,
    pub columns: Vec<Column>,
    pub rows: Vec<Vec<Value>>,
    pub affected_rows: u64,
    pub truncated: bool,
    pub elapsed_ms: u64,
    pub download_bytes: usize,
    pub size_is_estimated: bool,
}
impl QueryOutcome {
    pub fn row_count(&self) -> usize {
        self.rows.len()
    }
    pub fn rows_json(&self) -> Vec<JsonValue> {
        let names = unique_names(self.columns.iter().map(|c| c.name.as_str()));
        self.rows
            .iter()
            .map(|r| {
                let mut map = serde_json::Map::new();
                for (i, name) in names.iter().enumerate() {
                    map.insert(name.clone(), r.get(i).unwrap_or(&Value::Null).to_json());
                }
                JsonValue::Object(map)
            })
            .collect()
    }
    pub fn to_legacy_json(&self) -> JsonValue {
        let columns = unique_names(self.columns.iter().map(|c| c.name.as_str()))
            .into_iter()
            .zip(&self.columns)
            .map(|(name, column)| Column {
                name,
                type_name: column.type_name.clone(),
            })
            .collect::<Vec<_>>();
        json!({"executionId":self.execution_id,"columns":columns,"rows":self.rows_json(),"rowCount":self.row_count(),"affectedRows":self.affected_rows,"truncated":self.truncated,"elapsedMs":self.elapsed_ms,"engine":self.engine.as_str(),"sql":if self.has_parameters {&self.query}else{&self.executed_query},"downloadBytes":self.download_bytes,"sizeIsEstimated":self.size_is_estimated})
    }
}
pub fn unique_names<'a>(names: impl IntoIterator<Item = &'a str>) -> Vec<String> {
    let mut used = std::collections::HashSet::new();
    names
        .into_iter()
        .map(|name| {
            let mut chosen = name.to_owned();
            let mut i = 2;
            while !used.insert(chosen.clone()) {
                chosen = format!("{name}_{i}");
                i += 1;
            }
            chosen
        })
        .collect()
}

pub async fn execute(conn: &ConnectionSpec, request: QueryRequest) -> Result<QueryOutcome> {
    drivers::execute(conn, request).await
}

/// Instance-scoped admission control for every embedding application. No global state.
#[derive(Clone, Debug)]
pub struct Executor {
    active: std::sync::Arc<tokio::sync::Semaphore>,
    admitted: std::sync::Arc<tokio::sync::Semaphore>,
}
impl Executor {
    pub fn new(max_concurrent: usize, queue_bound: usize) -> Result<Self> {
        if max_concurrent == 0 {
            return Err(Error::invalid("Executor concurrency must be positive"));
        }
        let total = max_concurrent
            .checked_add(queue_bound)
            .ok_or_else(|| Error::invalid("Executor capacity overflow"))?;
        Ok(Self {
            active: std::sync::Arc::new(tokio::sync::Semaphore::new(max_concurrent)),
            admitted: std::sync::Arc::new(tokio::sync::Semaphore::new(total)),
        })
    }
    /// Runs a complete operation (including connection/SSH preparation) inside
    /// this executor's shared admission queue. The closure is not invoked until
    /// an active slot is available. One deadline covers queuing and execution.
    pub async fn execute_operation<T, F, Fut>(
        &self,
        timeout_ms: u64,
        cancellation: CancellationToken,
        operation: F,
    ) -> Result<T>
    where
        F: FnOnce() -> Fut,
        Fut: std::future::Future<Output = Result<T>>,
    {
        if timeout_ms == 0 {
            return Err(Error::invalid("Operation deadline must be positive"));
        }
        if cancellation.is_cancelled() {
            return Err(Error::new(
                ErrorCode::Cancelled,
                "Operation cancelled before admission",
            ));
        }
        let _admitted = self
            .admitted
            .clone()
            .try_acquire_owned()
            .map_err(|_| Error::new(ErrorCode::Busy, "Executor admission queue is full"))?;
        let budget = std::time::Duration::from_millis(timeout_ms);
        let admitted = async {
            let _active = self
                .active
                .clone()
                .acquire_owned()
                .await
                .map_err(|_| Error::new(ErrorCode::Cancelled, "Executor stopped"))?;
            operation().await
        };
        tokio::select! {
            biased;
            _=cancellation.cancelled()=>Err(Error::new(ErrorCode::Cancelled,"Executor operation cancelled")),
            result=tokio::time::timeout(budget,admitted)=>match result {
                Ok(result)=>result,
                Err(_)=>{cancellation.cancel();Err(Error::new(ErrorCode::Timeout,"Operation admission/preparation/execution deadline exceeded"))}
            }
        }
    }
    pub async fn execute(
        &self,
        conn: &ConnectionSpec,
        mut request: QueryRequest,
    ) -> Result<QueryOutcome> {
        let started = std::time::Instant::now();
        let timeout = request.policy.timeout_ms.unwrap_or(conn.timeout_ms);
        let budget = std::time::Duration::from_millis(timeout);
        self.execute_operation(timeout, request.cancellation.clone(), || async {
            let remaining = budget.saturating_sub(started.elapsed());
            if remaining.is_zero() {
                return Err(Error::new(
                    ErrorCode::Timeout,
                    "Query execution deadline exhausted",
                ));
            }
            request.policy.timeout_ms = Some(remaining.as_millis().max(1) as u64);
            crate::execute(conn, request).await
        })
        .await
    }
}

impl Executor {
    /// Acquires admission before opening an SSH lease or any other external resource.
    /// Callers should resolve the request's configured timeout before calling this method.
    pub async fn execute_prepared<F, Fut, Guard>(
        &self,
        mut request: QueryRequest,
        prepare: F,
    ) -> Result<QueryOutcome>
    where
        F: FnOnce() -> Fut,
        Fut: std::future::Future<Output = Result<(Guard, ConnectionSpec)>>,
    {
        let started = std::time::Instant::now();
        let timeout = request.policy.timeout_ms.unwrap_or(300_000);
        let budget = std::time::Duration::from_millis(timeout);
        self.execute_operation(timeout, request.cancellation.clone(), || async {
            let (_guard, conn) = prepare().await?;
            let target_budget = std::time::Duration::from_millis(
                request.policy.timeout_ms.unwrap_or(conn.timeout_ms),
            );
            let remaining = budget.min(target_budget).saturating_sub(started.elapsed());
            if remaining.is_zero() {
                return Err(Error::new(
                    ErrorCode::Timeout,
                    "Query deadline expired during preparation",
                ));
            }
            request.policy.timeout_ms = Some(remaining.as_millis().max(1) as u64);
            crate::execute(&conn, request).await
        })
        .await
    }
}
#[cfg(test)]
mod executor_tests {
    use super::*;
    #[tokio::test]
    async fn metadata_operations_and_queries_share_capacity_queue_deadlines_and_cancellation() {
        use std::sync::{
            atomic::{AtomicUsize, Ordering},
            Arc,
        };
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("mixed.db");
        rusqlite::Connection::open(&path)
            .unwrap()
            .execute_batch("CREATE TABLE sample(id INTEGER PRIMARY KEY)")
            .unwrap();
        let connection = ConnectionSpec::new(Engine::Sqlite, path.to_str().unwrap());
        let executor = Executor::new(1, 1).unwrap();
        let query=QueryRequest::new("WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<100000000) SELECT sum(x) FROM n");
        let query_cancel = query.cancellation.clone();
        let target = connection.clone();
        let copy = executor.clone();
        let running = tokio::spawn(async move { copy.execute(&target, query).await });
        while executor.active.available_permits() != 0 {
            tokio::task::yield_now().await;
        }
        let called = Arc::new(AtomicUsize::new(0));
        let prepared = called.clone();
        let target = connection.clone();
        let queued_cancel = CancellationToken::new();
        let error = executor
            .execute_operation(10, queued_cancel.clone(), || async move {
                prepared.fetch_add(1, Ordering::SeqCst);
                crate::columns(&target, "sample").await
            })
            .await
            .unwrap_err();
        assert_eq!(error.code, ErrorCode::Timeout);
        assert!(queued_cancel.is_cancelled());
        assert_eq!(
            called.load(Ordering::SeqCst),
            0,
            "Queued metadata must not open a connection before admission"
        );
        let ready = Arc::new(tokio::sync::Notify::new());
        let signal = ready.clone();
        let target = connection.clone();
        let copy = executor.clone();
        let operation_cancel = CancellationToken::new();
        let cancellation = operation_cancel.clone();
        let metadata = tokio::spawn(async move {
            copy.execute_operation(3000, cancellation, || async move {
                signal.notify_one();
                crate::columns(&target, "sample").await
            })
            .await
        });
        while executor.admitted.available_permits() != 0 {
            tokio::task::yield_now().await;
        }
        assert_eq!(
            executor
                .execute(&connection, QueryRequest::new("SELECT 3"))
                .await
                .unwrap_err()
                .code,
            ErrorCode::Busy
        );
        query_cancel.cancel();
        assert_eq!(
            running.await.unwrap().unwrap_err().code,
            ErrorCode::Cancelled
        );
        ready.notified().await;
        let schema = metadata.await.unwrap().unwrap();
        assert_eq!(schema["primary_key"], serde_json::json!(["id"]));
        assert_eq!(executor.active.available_permits(), 1);
        assert_eq!(executor.admitted.available_permits(), 2);
        let result = executor
            .execute_operation(3000, operation_cancel, || async {
                crate::tables(&connection).await
            })
            .await
            .unwrap();
        assert_eq!(result["tables"], serde_json::json!(["sample"]));
    }
    #[tokio::test]
    async fn operation_deadline_drops_leases_and_cancels_a_nested_blocking_query() {
        use std::sync::{
            atomic::{AtomicUsize, Ordering},
            Arc,
        };
        struct Lease(Arc<AtomicUsize>);
        impl Drop for Lease {
            fn drop(&mut self) {
                self.0.fetch_add(1, Ordering::SeqCst);
            }
        }
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("operation.db");
        rusqlite::Connection::open(&path).unwrap();
        let connection = ConnectionSpec::new(Engine::Sqlite, path.to_str().unwrap());
        let executor = Executor::new(1, 0).unwrap();
        let request=QueryRequest::new("WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<100000000) SELECT sum(x) FROM n");
        let query_cancel = request.cancellation.clone();
        let drops = Arc::new(AtomicUsize::new(0));
        let lease_drops = drops.clone();
        let context_cancel = CancellationToken::new();
        let error = executor
            .execute_operation(20, context_cancel.clone(), || async {
                let _lease = Lease(lease_drops);
                crate::execute(&connection, request).await
            })
            .await
            .unwrap_err();
        assert_eq!(error.code, ErrorCode::Timeout);
        assert!(context_cancel.is_cancelled());
        assert!(
            query_cancel.is_cancelled(),
            "Dropping an operation must signal its separately constructed database request"
        );
        assert_eq!(drops.load(Ordering::SeqCst), 1);
        assert_eq!(executor.active.available_permits(), 1);
        assert_eq!(executor.admitted.available_permits(), 1);
    }
    #[tokio::test]
    async fn prepared_execution_admits_before_leasing_and_cleans_cancelled_preparation() {
        use std::sync::{
            atomic::{AtomicUsize, Ordering},
            Arc,
        };
        struct Lease(Arc<AtomicUsize>);
        impl Drop for Lease {
            fn drop(&mut self) {
                self.0.fetch_add(1, Ordering::SeqCst);
            }
        }
        let executor = Executor::new(1, 0).unwrap();
        let entered = Arc::new(tokio::sync::Notify::new());
        let drops = Arc::new(AtomicUsize::new(0));
        let request = QueryRequest::new("SELECT 1");
        let cancel = request.cancellation.clone();
        let copy = executor.clone();
        let ready = entered.clone();
        let closed = drops.clone();
        let running = tokio::spawn(async move {
            copy.execute_prepared(request, || async move {
                let lease = Lease(closed);
                ready.notify_one();
                std::future::pending::<()>().await;
                Ok((lease, ConnectionSpec::default()))
            })
            .await
        });
        entered.notified().await;
        let called = Arc::new(AtomicUsize::new(0));
        let prepared = called.clone();
        let busy = executor
            .execute_prepared(QueryRequest::new("SELECT 2"), || async move {
                prepared.fetch_add(1, Ordering::SeqCst);
                Ok(((), ConnectionSpec::default()))
            })
            .await
            .unwrap_err();
        assert_eq!(busy.code, ErrorCode::Busy);
        assert_eq!(
            called.load(Ordering::SeqCst),
            0,
            "Rejected calls must not open external leases"
        );
        cancel.cancel();
        assert_eq!(
            running.await.unwrap().unwrap_err().code,
            ErrorCode::Cancelled
        );
        assert_eq!(
            drops.load(Ordering::SeqCst),
            1,
            "Cancelled preparation must drop its lease"
        );
        let mut request = QueryRequest::new("SELECT 3");
        request.policy.timeout_ms = Some(10);
        let timeout = executor
            .execute_prepared(request, || async {
                std::future::pending::<()>().await;
                Ok(((), ConnectionSpec::default()))
            })
            .await
            .unwrap_err();
        assert_eq!(timeout.code, ErrorCode::Timeout);
        assert_eq!(executor.active.available_permits(), 1);
        assert_eq!(executor.admitted.available_permits(), 1);
    }
    #[tokio::test]
    async fn admission_queue_is_bounded_and_cancelled_waiters_release_capacity() {
        let directory = tempfile::tempdir().unwrap();
        let path = directory.path().join("database.db");
        rusqlite::Connection::open(&path).unwrap();
        let conn = ConnectionSpec::new(Engine::Sqlite, path.to_str().unwrap());
        let executor = Executor::new(1, 1).unwrap();
        let first=QueryRequest::new("WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<100000000) SELECT sum(x) FROM n");
        let cancel_first = first.cancellation.clone();
        let copied = executor.clone();
        let target = conn.clone();
        let running = tokio::spawn(async move { copied.execute(&target, first).await });
        while executor.active.available_permits() != 0 {
            tokio::task::yield_now().await
        }
        let second = QueryRequest::new("SELECT 2");
        let cancel_second = second.cancellation.clone();
        let copied = executor.clone();
        let target = conn.clone();
        let waiting = tokio::spawn(async move { copied.execute(&target, second).await });
        while executor.admitted.available_permits() != 0 {
            tokio::task::yield_now().await
        }
        assert_eq!(
            executor
                .execute(&conn, QueryRequest::new("SELECT 3"))
                .await
                .unwrap_err()
                .code,
            ErrorCode::Busy
        );
        cancel_second.cancel();
        assert_eq!(
            waiting.await.unwrap().unwrap_err().code,
            ErrorCode::Cancelled
        );
        cancel_first.cancel();
        assert_eq!(
            running.await.unwrap().unwrap_err().code,
            ErrorCode::Cancelled
        );
        assert_eq!(
            executor
                .execute(&conn, QueryRequest::new("SELECT 4"))
                .await
                .unwrap()
                .rows_json()[0]["4"],
            serde_json::json!(4)
        );
    }
}
