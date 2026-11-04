//! Reusable Quarry facade for a standalone client or an embedding host.
//! All workspace/state locations are explicit; this crate starts no listener.
use async_trait::async_trait;
use quarry_core::{ConnectionSpec, Executor, QueryOutcome, QueryRequest};
use quarry_runtime::{ConnectionRecord, RuntimeConfig, TunnelManager, WorkspaceCatalog};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    collections::{BTreeMap, BTreeSet},
    path::{Path, PathBuf},
    sync::Arc,
    sync::Mutex,
};

#[derive(Debug, thiserror::Error)]
#[error("{message}")]
pub struct Failure {
    pub status: u16,
    pub code: String,
    pub message: String,
}
pub type Result<T> = std::result::Result<T, Failure>;
#[derive(Clone, Debug)]
pub enum MetadataOperation {
    Tables,
    Columns(String),
    Ping,
    InspectKey(String),
    RecordLink { query: String, row: Value },
}
pub fn display_path(path: &Path) -> String {
    if let Some(home) = std::env::var_os("HOME")
        && let Ok(relative) = path.strip_prefix(Path::new(&home))
    {
        return if relative.as_os_str().is_empty() {
            "~".into()
        } else {
            format!("~/{}", relative.display())
        };
    }
    path.display().to_string()
}

/// An optional, bounded metadata cache; never contains result rows or credentials.
/// Identity hashes include the full connection and local file generation.
pub struct MetadataCache {
    path: PathBuf,
    entries: Mutex<BTreeMap<String, Value>>,
}
impl MetadataCache {
    fn new(path: PathBuf) -> Self {
        let entries = std::fs::metadata(&path)
            .ok()
            .filter(|m| m.len() <= 8_388_608)
            .and_then(|_| std::fs::read(&path).ok())
            .and_then(|data| serde_json::from_slice(&data).ok())
            .unwrap_or_default();
        Self {
            path,
            entries: Mutex::new(entries),
        }
    }
    pub fn get(&self, key: &str, fingerprint: &str, ttl: Option<u64>) -> Option<Value> {
        let entries = self.entries.lock().ok()?;
        let entry = entries.get(key)?;
        if entry["fingerprint"] != fingerprint
            || ttl.is_some_and(|ttl| {
                cache_now().saturating_sub(entry["timestamp"].as_u64().unwrap_or(0)) >= ttl
            })
        {
            return None;
        }
        entry.get("value").cloned()
    }
    pub fn put(&self, key: &str, fingerprint: &str, value: &Value) {
        if value.to_string().len() > 1_048_576 {
            return;
        }
        if let Ok(mut entries) = self.entries.lock() {
            entries.insert(
                key.into(),
                json!({"fingerprint":fingerprint,"timestamp":cache_now(),"value":value}),
            );
            while entries.len() > 256
                || serde_json::to_vec(&*entries).map_or(usize::MAX, |data| data.len()) > 8_388_608
            {
                let oldest = entries
                    .iter()
                    .min_by_key(|(_, v)| v["timestamp"].as_u64().unwrap_or(0))
                    .map(|(key, _)| key.clone());
                if let Some(oldest) = oldest {
                    entries.remove(&oldest);
                } else {
                    break;
                }
            }
            if let Ok(bytes) = serde_json::to_vec(&*entries) {
                let _ = quarry_runtime::atomic_write(&self.path, &bytes);
            }
        }
    }
    pub fn clear(&self) {
        if let Ok(mut entries) = self.entries.lock() {
            entries.clear();
            let _ = quarry_runtime::atomic_write(&self.path, b"{}");
        }
    }
}
fn cache_now() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs()
}

pub fn metadata_identity(record: &ConnectionRecord, proxy: bool) -> String {
    let mut fields = vec![
        json!(record.workspace),
        json!(record.key),
        json!(record.url),
        json!(record.ssh_host),
        json!(record.ssh_user),
        json!(record.ssh_key),
        json!(record.ssh_port),
        json!(proxy),
    ];
    if record
        .spec()
        .is_ok_and(|s| s.engine == quarry_core::Engine::Sqlite)
    {
        let raw = record.url.strip_prefix("sqlite:///").unwrap_or(&record.url);
        let raw = percent_encoding::percent_decode_str(raw).decode_utf8_lossy();
        if let Ok(path) = quarry_runtime::absolute(Path::new(raw.as_ref()), &record.workspace) {
            for file in [
                path.clone(),
                PathBuf::from(format!("{}-wal", path.display())),
            ] {
                let metadata = file.metadata().ok();
                fields.push(json!(metadata.as_ref().map(|m| format!(
                    "{:?}:{}",
                    m.modified().ok(),
                    m.len()
                ))));
                #[cfg(unix)]
                {
                    use std::os::unix::fs::MetadataExt;
                    fields.push(json!(metadata.map(|m| (m.dev(), m.ino()))));
                }
            }
        }
    }
    format!(
        "{:x}",
        Sha256::digest(serde_json::to_vec(&fields).unwrap_or_default())
    )
}
impl Failure {
    pub fn invalid(s: impl Into<String>) -> Self {
        Self {
            status: 400,
            code: "invalid_input".into(),
            message: s.into(),
        }
    }
}
impl From<quarry_core::Error> for Failure {
    fn from(e: quarry_core::Error) -> Self {
        Self {
            status: match e.code {
                quarry_core::ErrorCode::Safety => 403,
                quarry_core::ErrorCode::Timeout => 504,
                quarry_core::ErrorCode::Connection => 503,
                quarry_core::ErrorCode::Busy => 429,
                quarry_core::ErrorCode::Cancelled => 499,
                quarry_core::ErrorCode::Budget => 413,
                _ => 400,
            },
            code: serde_json::to_value(e.code)
                .ok()
                .and_then(|v| v.as_str().map(String::from))
                .unwrap_or_else(|| "execution".into()),
            message: e.message,
        }
    }
}
impl From<quarry_runtime::RuntimeError> for Failure {
    fn from(e: quarry_runtime::RuntimeError) -> Self {
        match e {
            quarry_runtime::RuntimeError::Core(error) => error.into(),
            quarry_runtime::RuntimeError::NotFound(message) => Self {
                status: 404,
                code: "not_found".into(),
                message,
            },
            quarry_runtime::RuntimeError::Ambiguous(message) => Self {
                status: 409,
                code: "ambiguous".into(),
                message: format!("ambiguous resource: {message}"),
            },
            other => Self::invalid(other.to_string()),
        }
    }
}
impl From<quarry_assistant::AssistantError> for Failure {
    fn from(e: quarry_assistant::AssistantError) -> Self {
        Self {
            status: e.status,
            code: e.code.into(),
            message: e.message,
        }
    }
}
impl From<std::io::Error> for Failure {
    fn from(e: std::io::Error) -> Self {
        Self::invalid(e.to_string())
    }
}
impl From<serde_json::Error> for Failure {
    fn from(e: serde_json::Error) -> Self {
        Self::invalid(e.to_string())
    }
}

#[derive(Clone)]
pub struct QuarryService {
    pub config_path: PathBuf,
    pub explicit: Vec<PathBuf>,
    pub state_dir: PathBuf,
    pub tunnels: Arc<TunnelManager>,
    pub cache: Arc<MetadataCache>,
    pub keeper_launcher: Option<quarry_runtime::DaemonLauncher>,
    local_psql: Option<PathBuf>,
    local_pg_dump: Option<PathBuf>,
    neptune_launcher: Option<quarry_runtime::DaemonLauncher>,
    executor: Executor,
}
impl QuarryService {
    pub fn new(config_path: PathBuf, explicit: Vec<PathBuf>, state_dir: PathBuf) -> Self {
        Self {
            config_path,
            explicit,
            state_dir: state_dir.clone(),
            tunnels: Arc::new(TunnelManager::new(state_dir.join("tunnels"))),
            cache: Arc::new(MetadataCache::new(state_dir.join("metadata-cache.json"))),
            keeper_launcher: None,
            local_psql: None,
            local_pg_dump: None,
            neptune_launcher: None,
            executor: Executor::new(16, 64).expect("valid host execution limits"),
        }
    }
    pub fn with_keeper_launcher(mut self, launcher: quarry_runtime::DaemonLauncher) -> Self {
        self.keeper_launcher = Some(launcher);
        self
    }
    pub fn with_local_tools(mut self, psql: Option<PathBuf>, pg_dump: Option<PathBuf>) -> Self {
        self.local_psql = psql;
        self.local_pg_dump = pg_dump;
        self
    }
    pub fn with_neptune_launcher(mut self, launcher: quarry_runtime::DaemonLauncher) -> Self {
        self.neptune_launcher = Some(launcher);
        self
    }
    pub fn catalog(&self) -> Result<WorkspaceCatalog> {
        Ok(WorkspaceCatalog::load(
            RuntimeConfig::load(&self.config_path)?,
            &self.explicit,
        )?)
    }
    pub fn resolve(
        &self,
        db: &str,
        env: Option<&str>,
        workspace: Option<&Path>,
    ) -> Result<ConnectionRecord> {
        Ok(self.catalog()?.resolve(db, env, workspace)?)
    }
    /// Resolve an environment sibling of a saved query's owning workspace.
    /// The file remains scoped to its original workspace; a Local environment
    /// may live in another configured workspace in the same merged UI group.
    pub fn resolve_related(
        &self,
        db: &str,
        env: Option<&str>,
        workspace: &Path,
    ) -> Result<ConnectionRecord> {
        let scoped_error = match self.resolve(db, env, Some(workspace)) {
            Ok(record) => return Ok(record),
            Err(error) if error.code == "not_found" => error,
            Err(error) => return Err(error),
        };
        let scope = quarry_runtime::absolute(workspace, &std::env::current_dir()?)?;
        let catalog = self.catalog()?;
        if !catalog.workspaces.iter().any(|entry| entry.home == scope) {
            return Err(scoped_error);
        }
        // Global resolution must remain unique. In particular, a duplicate
        // connection key or logical-db/environment never picks the first file.
        let candidate = self.resolve(db, env, None)?;
        let connections = catalog.connections()?;
        let origins = connections
            .iter()
            .filter(|record| {
                record.workspace == scope && (record.logical_db() == db || record.key == db)
            })
            .map(ConnectionRecord::logical_db)
            .collect::<BTreeSet<_>>();
        if !origins.is_empty() && (origins.len() != 1 || !origins.contains(candidate.logical_db()))
        {
            return Err(scoped_error);
        }
        let named_groups = connections
            .iter()
            .filter(|record| record.logical_db() == candidate.logical_db())
            .filter_map(|record| record.group.as_deref())
            .filter(|group| !group.is_empty())
            .collect::<BTreeSet<_>>();
        let inherited_group = if named_groups.len() == 1 {
            named_groups.first().copied()
        } else {
            None
        };
        let effective_group = |record: &ConnectionRecord| -> String {
            record
                .group
                .as_deref()
                .filter(|group| !group.is_empty())
                .or(inherited_group)
                .unwrap_or("")
                .to_owned()
        };
        let group = effective_group(&candidate);
        let anchored = connections.iter().any(|record| {
            record.workspace == scope
                && record.logical_db() == candidate.logical_db()
                && effective_group(record) == group
        });
        // The UI may show a group at the workspace owning most of its members,
        // even when this particular database has no physical record there.
        let represented = connection_groups(&connections)?.iter().any(|entry| {
            entry["ws"]
                .as_str()
                .is_some_and(|source| Path::new(source) == scope)
                && entry["group"].as_str().unwrap_or("") == group
                && entry["items"].as_array().is_some_and(|items| {
                    items
                        .iter()
                        .any(|item| item["db"] == candidate.logical_db())
                })
        });
        if anchored || represented {
            Ok(candidate)
        } else {
            Err(scoped_error)
        }
    }
    pub async fn execute(
        &self,
        record: &ConnectionRecord,
        request: QueryRequest,
    ) -> Result<QueryOutcome> {
        self.execute_with_proxy(record, request, None).await
    }
    pub async fn metadata(
        &self,
        record: &ConnectionRecord,
        operation: MetadataOperation,
        timeout_ms: Option<u64>,
        cancellation: quarry_core::CancellationToken,
    ) -> Result<Value> {
        let deadline = timeout_ms.unwrap_or(record.spec()?.timeout_ms).min(30_000);
        Ok(self
            .executor
            .execute_operation(deadline, cancellation, || async {
                let (_lease, mut spec) = self.spec_lease(record).await.map_err(|e| {
                    quarry_core::Error::new(quarry_core::ErrorCode::Connection, e.message)
                })?;
                spec.timeout_ms = spec.timeout_ms.min(deadline);
                spec.connect_timeout_ms = spec.connect_timeout_ms.min(deadline);
                match operation {
                    MetadataOperation::Tables => quarry_core::tables(&spec).await,
                    MetadataOperation::Columns(table) => quarry_core::columns(&spec, &table).await,
                    MetadataOperation::Ping => quarry_core::ping(&spec).await,
                    MetadataOperation::InspectKey(key) => {
                        quarry_core::inspect_key(&spec, &key).await
                    }
                    MetadataOperation::RecordLink { query, row } => {
                        quarry_core::record_query(&spec, &query, &row).await
                    }
                }
            })
            .await?)
    }
    pub async fn tables(&self, record: &ConnectionRecord) -> Result<Value> {
        self.metadata(
            record,
            MetadataOperation::Tables,
            None,
            quarry_core::CancellationToken::new(),
        )
        .await
    }
    pub async fn columns(&self, record: &ConnectionRecord, table: &str) -> Result<Value> {
        self.metadata(
            record,
            MetadataOperation::Columns(table.into()),
            None,
            quarry_core::CancellationToken::new(),
        )
        .await
    }
    pub async fn execute_with_proxy(
        &self,
        record: &ConnectionRecord,
        request: QueryRequest,
        proxy: Option<bool>,
    ) -> Result<QueryOutcome> {
        Ok(self
            .executor
            .execute_prepared(request, || async {
                self.spec_lease_with_proxy(record, proxy)
                    .await
                    .map_err(|error| {
                        quarry_core::Error::new(quarry_core::ErrorCode::Connection, error.message)
                    })
            })
            .await?)
    }
    pub async fn spec_lease(
        &self,
        record: &ConnectionRecord,
    ) -> Result<(quarry_runtime::TunnelLease, ConnectionSpec)> {
        self.spec_lease_with_proxy(record, None).await
    }
    async fn spec_lease_with_proxy(
        &self,
        record: &ConnectionRecord,
        proxy: Option<bool>,
    ) -> Result<(quarry_runtime::TunnelLease, ConnectionSpec)> {
        // None means inherit the per-workspace setting; explicit false bypasses
        // that toggle while preserving the SSH client's own normal config.
        let proxy = match proxy {
            Some(value) => Some(value),
            None if RuntimeConfig::load(&self.config_path)?.enabled("proxy", &record.workspace) => {
                Some(true)
            }
            None => None,
        };
        let lease = self.tunnels.open(record, proxy).await?;
        let spec = lease.spec(record)?;
        Ok((lease, spec))
    }
    pub async fn connections(&self) -> Result<Value> {
        let catalog = self.catalog()?;
        let connections = catalog.connections()?;
        let mut groups = connection_groups(&connections)?;
        let proxy = quarry_runtime::ProxyInfo::discover();
        for group in &mut groups {
            group["ws"] = json!(display_path(Path::new(group["ws"].as_str().unwrap_or(""))));
            if let Some(items) = group["items"].as_array_mut() {
                for item in items {
                    if let Some(envs) = item["envs"].as_array_mut() {
                        for env in envs {
                            if let Some(record) = connections.iter().find(|c| {
                                env["key"] == c.key && env["env"] == json!(c.display_env())
                            }) {
                                let enabled = catalog.config.enabled("proxy", &record.workspace);
                                env["proxied"] = json!(if record.ssh_host.is_some() {
                                    self.tunnels
                                        .proxied(record, enabled.then_some(true), proxy.clone())
                                        .await
                                        .unwrap_or(false)
                                } else {
                                    // Direct Neptune evaluates its HTTP proxy for each request.
                                    record.spec()?.engine == quarry_core::Engine::Neptune
                                        && record.proxy_url.is_some()
                                });
                            }
                        }
                    }
                }
            }
        }
        Ok(
            json!({"groups":groups,"workspace":catalog.workspaces.first().map(|w|display_path(&w.home)),"workspaces":catalog.workspaces.iter().map(|w|display_path(&w.home)).collect::<Vec<_>>()}),
        )
    }

    pub fn workspaces(&self) -> Result<Value> {
        let config = RuntimeConfig::load(&self.config_path)?;
        let items = config.workspaces.iter().map(|path| {
            let path = quarry_runtime::absolute(path, &config.base())?;
            Ok(json!({"dir":path,"display":display_path(&path),"exists":path.exists(),"hasConnections":path.join("connections.toml").is_file(),"proxyEnabled":config.enabled("proxy",&path)}))
        }).collect::<Result<Vec<_>>>()?;
        Ok(
            json!({"config":display_path(&self.config_path),"items":items,"proxyDiscovered":quarry_runtime::ProxyInfo::discover()}),
        )
    }

    pub fn info(&self, record: &ConnectionRecord, reveal: bool) -> Result<Value> {
        let url = url::Url::parse(&record.url).ok();
        let redacted = record.redacted();
        let spec = record.spec()?;
        let mut out = json!({"key":record.key,"db":record.logical_db(),"env":record.display_env(),"production":record.production,"engine":spec.engine.as_str(),"url":if reveal {record.url.clone()}else{redacted.url.replace("REDACTED","••••")},"host":url.as_ref().and_then(|u|u.host_str()),"port":url.as_ref().and_then(|u|u.port_or_known_default()),"database":url.as_ref().map(|u|u.path().trim_start_matches('/')),"group":record.group,"region":record.extra.get("region").and_then(toml::Value::as_str),"notes":record.notes,"file":display_path(&record.workspace.join("connections.toml")),"tunnel":null});
        if record.ssh_host.is_some() {
            out["tunnel"] = json!({"host":record.ssh_host,"user":record.ssh_user,"port":record.ssh_port.unwrap_or(22),"key":record.ssh_key});
        }
        if spec.engine == quarry_core::Engine::Sqlite {
            let raw = record.url.strip_prefix("sqlite:///").unwrap_or(&record.url);
            let p = PathBuf::from(raw);
            let p = if p.is_absolute() {
                p
            } else {
                record.workspace.join(p)
            };
            out["databasePath"] = json!(p);
            out["database"] = json!(p.file_name());
            out["host"] = Value::Null;
            out["port"] = Value::Null;
        }
        Ok(out)
    }
    pub fn local(&self) -> Result<quarry_local::LocalManager> {
        let manager = quarry_local::LocalManager::new(
            RuntimeConfig::load(&self.config_path)?,
            self.state_dir.join("local"),
        )
        .with_tools(self.local_psql.clone(), self.local_pg_dump.clone(), None);
        Ok(if let Some(launcher) = &self.neptune_launcher {
            manager.with_neptune_launcher(launcher.clone())
        } else {
            manager
        })
    }
    pub async fn local_up(&self, db: &str) -> Result<Value> {
        let catalog = self.catalog()?;
        let connections = catalog.connections()?;
        let mut sources = connections
            .iter()
            .filter(|c| c.logical_db() == db && c.env != "local")
            .collect::<Vec<_>>();
        sources.sort_by_key(|c| (c.env != "dev", &c.env));
        let source = sources
            .first()
            .copied()
            .ok_or_else(|| Failure::invalid("Database has no remote source"))?;
        // Fail on ambiguous names before starting any local service.
        self.resolve(db, Some(&source.env), None)?;
        let manager = self.local()?;
        let engine = source.spec()?.engine;
        let image = manager.stored_image(&catalog, source)?;
        let status = manager.up(engine.as_str(), image.as_deref(), None).await?;
        let record = manager.register(&catalog, db, Some(&source.env)).await?;
        self.cache.clear();
        let created = !connections
            .iter()
            .any(|c| c.key == record.key && c.workspace == record.workspace && c.env == "local");
        let mut out = json!({"key":record.key,"created":created,"engine":engine.as_str(),"state":status.get("state").cloned().unwrap_or(json!("running")),"port":status.get("port").cloned().unwrap_or(Value::Null)});
        if created && engine == quarry_core::Engine::Postgres && source.display_env().is_some() {
            match manager.sync(source, &record, &self.tunnels).await {
                Ok(_) => out["synced_from"] = json!(source.display_env()),
                Err(error) => out["sync_error"] = json!(error.to_string()),
            }
        }
        Ok(out)
    }
    pub async fn local_sync(&self, db: &str, from: &str) -> Result<Value> {
        let source = self.resolve(db, Some(from), None)?;
        let target = self.resolve_related(db, Some("local"), &source.workspace)?;
        let mut out = self.local()?.sync(&source, &target, &self.tunnels).await?;
        out["from"] = json!(from);
        Ok(out)
    }
}

/// One logical database spans workspace-local copies and hosted environments.
/// Preserve load order, unique sibling-group inheritance, and the majority
/// source rule used by the existing UI's persisted tab identity.
fn connection_groups(connections: &[ConnectionRecord]) -> Result<Vec<Value>> {
    let mut sibling_groups: BTreeMap<&str, BTreeSet<&str>> = BTreeMap::new();
    for connection in connections {
        if let Some(group) = connection.group.as_deref().filter(|g| !g.is_empty()) {
            sibling_groups
                .entry(connection.logical_db())
                .or_default()
                .insert(group);
        }
    }
    let mut groups: Vec<(String, PathBuf, Vec<&ConnectionRecord>)> = Vec::new();
    for connection in connections {
        let inherited = sibling_groups
            .get(connection.logical_db())
            .filter(|g| g.len() == 1)
            .and_then(|g| g.first().copied());
        let group = connection
            .group
            .as_deref()
            .filter(|g| !g.is_empty())
            .or(inherited)
            .unwrap_or("");
        if let Some((_, _, members)) = groups.iter_mut().find(|(name, _, _)| name == group) {
            members.push(connection);
        } else {
            let source = if connection
                .group
                .as_deref()
                .filter(|g| !g.is_empty())
                .is_some()
                || group.is_empty()
            {
                connection.workspace.clone()
            } else {
                connections
                    .iter()
                    .find(|other| {
                        other.logical_db() == connection.logical_db()
                            && other.group.as_deref() == Some(group)
                    })
                    .map(|other| other.workspace.clone())
                    .unwrap_or_else(|| connection.workspace.clone())
            };
            groups.push((group.into(), source, vec![connection]));
        }
    }
    groups.into_iter().map(|(group,fallback,members)| {
        let mut sources: BTreeMap<&Path,usize> = BTreeMap::new();
        for member in &members { *sources.entry(&member.workspace).or_default() += 1; }
        let max = sources.values().copied().max().unwrap_or(0);
        let winners = sources.into_iter().filter(|(_,n)|*n == max).map(|(path,_)|path).collect::<Vec<_>>();
        let source = if winners.len() == 1 { winners[0] } else { &fallback };
        let mut databases: Vec<(&str,Vec<&ConnectionRecord>)> = Vec::new();
        for member in members {
            if let Some((_,envs)) = databases.iter_mut().find(|(db,_)|*db == member.logical_db()) { envs.push(member); }
            else { databases.push((member.logical_db(),vec![member])); }
        }
        let items = databases.into_iter().map(|(db,mut members)| {
            let is_env_set = members.len() > 1 || members[0].display_env().is_some_and(|e|!e.is_empty());
            let engine = members[0].spec()?.engine;
            members.sort_by_key(|member| if member.env == "local" { 0 } else { 1 });
            let envs = members.into_iter().map(|c|Ok(json!({"env":c.display_env(),"key":c.key,"engine":c.spec()?.engine.as_str(),"region":c.extra.get("region").and_then(toml::Value::as_str),"ssh":c.ssh_host.is_some(),"proxied":false,"production":c.production}))).collect::<Result<Vec<_>>>()?;
            Ok(json!({"db":db,"is_env_set":is_env_set,"engine":engine.as_str(),"envs":envs}))
        }).collect::<Result<Vec<_>>>()?;
        Ok(json!({"group":if group.is_empty(){Value::Null}else{json!(group)},"ws":source,"items":items}))
    }).collect()
}

fn assistant_table_metadata(table: &str, data: &Value) -> Result<Value> {
    let rows = data["rows"]
        .as_array()
        .ok_or_else(|| Failure::invalid("Invalid table metadata"))?;
    // Hidden virtual-table columns are implementation details, not query fields.
    let visible = rows
        .iter()
        .filter(|row| row["hidden"] != 1)
        .collect::<Vec<_>>();
    if visible.is_empty() {
        return Err(Failure::invalid(format!(
            "Table not found or not accessible: {table}"
        )));
    }
    let columns = visible.iter().take(500).map(|row| {
        let mut column = json!({"name":row["column_name"],"type":row.get("formatted_type").or_else(||row.get("column_type")).unwrap_or(&row["data_type"]),"nullable":row["is_nullable"].as_str().map(|s|s.eq_ignore_ascii_case("YES")).unwrap_or_else(||row["is_nullable"] == true || row["is_nullable"] == 1)});
        if let Some(comment) = row.get("comment").or_else(||row.get("column_comment")) { column["comment"] = comment.clone(); }
        column
    }).collect::<Vec<_>>();
    let foreign_keys = data["foreign_keys"]
        .as_array()
        .map(Vec::as_slice)
        .unwrap_or_default();
    let foreign_rows = foreign_keys
        .iter()
        .map(|key| key["columns"].as_array().map_or(0, Vec::len))
        .sum::<usize>();
    let mut remaining = 500;
    let mut relations = Vec::new();
    for key in foreign_keys {
        if remaining == 0 {
            break;
        }
        let columns = key["columns"]
            .as_array()
            .map(Vec::as_slice)
            .unwrap_or_default();
        let targets = key["referenced_columns"]
            .as_array()
            .map(Vec::as_slice)
            .unwrap_or_default();
        let count = remaining.min(columns.len());
        let mut relation = json!({"columns":columns.iter().take(count).collect::<Vec<_>>(),"targetTable":key["referenced_table"],"targetColumns":targets.iter().take(count).collect::<Vec<_>>()});
        if let Some(schema) = key.get("referenced_schema").filter(|v| !v.is_null()) {
            relation["targetSchema"] = schema.clone();
        }
        relations.push(relation);
        remaining -= count;
    }
    Ok(
        json!({"name":table,"columns":columns,"primaryKey":data.get("primary_key").cloned().unwrap_or(json!([])),"foreignKeys":relations,"capped":visible.len()>500 || foreign_rows>500 || data["capped"] == true}),
    )
}

#[async_trait]
impl quarry_assistant::MetadataProvider for QuarryService {
    async fn resolve(
        &self,
        target: &quarry_assistant::Target,
    ) -> quarry_assistant::AssistantResult<quarry_assistant::ResolvedContext> {
        let record = if let Some(workspace) = target.workspace.as_deref() {
            self.resolve_related(&target.db, target.env.as_deref(), Path::new(workspace))
        } else {
            self.resolve(&target.db, target.env.as_deref(), None)
        }
        .map_err(assistant_error)?;
        let spec = record.spec().map_err(|e| assistant_error(e.into()))?;
        if !matches!(
            spec.engine,
            quarry_core::Engine::Postgres
                | quarry_core::Engine::MySql
                | quarry_core::Engine::Sqlite
        ) {
            return Err(assistant_error(Failure::invalid(
                "SQL assistant requires PostgreSQL, MySQL or SQLite",
            )));
        }
        let (fingerprint, legacy_env) = assistant_fingerprint(&record).map_err(assistant_error)?;
        let groups = self.connections().await.map_err(assistant_error)?;
        let gui_workspace = groups["groups"]
            .as_array()
            .and_then(|groups| {
                groups.iter().find(|group| {
                    group["items"].as_array().is_some_and(|items| {
                        items.iter().any(|item| {
                            item["db"] == record.logical_db()
                                && item["envs"].as_array().is_some_and(|envs| {
                                    envs.iter().any(|env| {
                                        env["key"] == record.key
                                            && env["env"] == json!(record.display_env())
                                    })
                                })
                        })
                    })
                })
            })
            .and_then(|group| group["ws"].as_str())
            .map(String::from);
        Ok(quarry_assistant::ResolvedContext {
            target: quarry_assistant::Target {
                db: record.logical_db().into(),
                env: legacy_env,
                workspace: Some(record.workspace.display().to_string()),
            },
            engine: spec.engine.as_str().into(),
            fingerprint,
            gui_workspace,
        })
    }
    async fn find_tables(
        &self,
        ctx: &quarry_assistant::ResolvedContext,
        query: &str,
        limit: usize,
    ) -> quarry_assistant::AssistantResult<Value> {
        let record = QuarryService::resolve(
            self,
            &ctx.target.db,
            ctx.target.env.as_deref(),
            ctx.target.workspace.as_deref().map(Path::new),
        )
        .map_err(assistant_error)?;
        let all = self.tables(&record).await.map_err(assistant_error)?;
        let names = all["tables"]
            .as_array()
            .cloned()
            .unwrap_or_default()
            .into_iter()
            .filter(|v| {
                v.as_str()
                    .unwrap_or("")
                    .to_lowercase()
                    .contains(&query.to_lowercase())
            })
            .collect::<Vec<_>>();
        Ok(
            json!({"tables":names.iter().take(limit).collect::<Vec<_>>(),"capped":names.len()>limit||all["capped"]==true}),
        )
    }
    async fn describe_tables(
        &self,
        ctx: &quarry_assistant::ResolvedContext,
        tables: &[String],
    ) -> quarry_assistant::AssistantResult<Value> {
        if tables.len() > 8 {
            return Err(assistant_error(Failure::invalid(
                "At most eight tables may be described",
            )));
        }
        let record = QuarryService::resolve(
            self,
            &ctx.target.db,
            ctx.target.env.as_deref(),
            ctx.target.workspace.as_deref().map(Path::new),
        )
        .map_err(assistant_error)?;
        let mut descriptions = vec![];
        for table in tables {
            let data = self
                .columns(&record, table)
                .await
                .map_err(assistant_error)?;
            descriptions.push(assistant_table_metadata(table, &data).map_err(assistant_error)?);
        }
        Ok(json!({"tables":descriptions}))
    }
}
fn assistant_error(e: Failure) -> quarry_assistant::AssistantError {
    quarry_assistant::AssistantError {
        status: e.status,
        code: "RUNTIME",
        message: e.message,
    }
}

// Match 1.x hashlib.sha256(json.dumps(identity).encode()) without a Python
// runtime. Only the digest leaves this function: connection URLs stay private.
fn legacy_identity_digest(fields: &[Value]) -> Result<String> {
    fn scalar(value: &Value) -> Result<String> {
        match value {
            Value::Null => Ok("null".into()),
            Value::Number(number) if number.is_i64() || number.is_u64() => Ok(number.to_string()),
            Value::String(text) => {
                let mut out = String::from("\"");
                for c in text.chars() {
                    match c {
                        '"' => out.push_str("\\\""),
                        '\\' => out.push_str("\\\\"),
                        '\n' => out.push_str("\\n"),
                        '\r' => out.push_str("\\r"),
                        '\t' => out.push_str("\\t"),
                        '\x08' => out.push_str("\\b"),
                        '\x0c' => out.push_str("\\f"),
                        c if !(' '..='~').contains(&c) => {
                            for unit in c.encode_utf16(&mut [0; 2]) {
                                out.push_str(&format!("\\u{unit:04x}"));
                            }
                        }
                        c => out.push(c),
                    }
                }
                out.push('"');
                Ok(out)
            }
            _ => Err(Failure::invalid("Invalid assistant connection identity")),
        }
    }
    let values = fields.iter().map(scalar).collect::<Result<Vec<_>>>()?;
    Ok(format!(
        "{:x}",
        Sha256::digest(format!("[{}]", values.join(", ")).as_bytes())
    ))
}
fn assistant_fingerprint(record: &ConnectionRecord) -> Result<(String, Option<String>)> {
    let source = std::fs::canonicalize(&record.workspace)
        .map_err(|_| Failure::invalid("The assistant workspace is unavailable"))?;
    // Read the original presence of env: Python used None for an omitted field,
    // while the Rust execution catalog may use an effective default environment.
    let raw = std::fs::read_to_string(source.join("connections.toml"))
        .map_err(|_| Failure::invalid("The assistant connection configuration is unavailable"))?;
    let document: toml::Value = toml::from_str(&raw)
        .map_err(|_| Failure::invalid("Invalid assistant connection configuration"))?;
    let entry = document
        .get(&record.key)
        .and_then(toml::Value::as_table)
        .ok_or_else(|| Failure::invalid("The assistant connection changed"))?;
    if entry.get("url").and_then(toml::Value::as_str) != Some(record.url.as_str()) {
        return Err(Failure::invalid("The assistant connection changed"));
    }
    let env = entry
        .get("env")
        .and_then(toml::Value::as_str)
        .map(str::to_owned);
    let engine = record.spec()?.engine;
    let mut identity = vec![
        json!(source.display().to_string()),
        json!(record.key),
        json!(record.logical_db()),
        json!(env),
        json!(record.url),
        json!(engine.as_str()),
        json!(record.ssh_host),
        json!(record.ssh_user),
        json!(record.ssh_port),
        json!(record.ssh_key),
    ];
    if engine == quarry_core::Engine::Sqlite {
        let raw = record.url.strip_prefix("sqlite:///").unwrap_or(&record.url);
        let decoded = percent_encoding::percent_decode_str(raw)
            .decode_utf8()
            .map_err(|_| Failure::invalid("Invalid SQLite path"))?;
        let path = quarry_runtime::absolute(Path::new(decoded.as_ref()), &source)?;
        let path = std::fs::canonicalize(path).map_err(|_| {
            Failure::invalid("The conversation's SQLite database file is unavailable")
        })?;
        let metadata = path.metadata().map_err(|_| {
            Failure::invalid("The conversation's SQLite database file is unavailable")
        })?;
        identity.push(json!(path.display().to_string()));
        #[cfg(unix)]
        {
            use std::os::unix::fs::MetadataExt;
            identity.extend([json!(metadata.dev()), json!(metadata.ino())]);
        }
        #[cfg(not(unix))]
        {
            return Err(Failure::invalid(
                "SQLite history migration requires a verified filesystem identity adapter on this platform",
            ));
        }
    }
    Ok((legacy_identity_digest(&identity)?, env))
}
#[cfg(test)]
mod assistant_identity_tests {
    use super::*;
    #[test]
    fn python_json_digest_matches_unicode_and_null_fixture() {
        let fields = vec![
            json!("/tmp/项目😀"),
            json!("shop"),
            json!("shop"),
            Value::Null,
            json!("postgresql://fixture:fake-secret@db:5432/shop"),
            json!("postgres"),
            Value::Null,
            Value::Null,
            Value::Null,
            Value::Null,
        ];
        assert_eq!(
            legacy_identity_digest(&fields).unwrap(),
            "f70268daad15619f52135c6bda270f8bd4dbbc7b32d06819325f44d1e3f5113f"
        );
    }
    #[test]
    fn sqlite_replacement_invalidates_identity_but_data_edits_do_not() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("notes db.sqlite");
        std::fs::write(&path, b"fixture").unwrap();
        std::fs::write(
            dir.path().join("connections.toml"),
            "[notes]\nengine=\"sqlite\"\nurl=\"sqlite:///notes%20db.sqlite\"\n",
        )
        .unwrap();
        let record:ConnectionRecord=serde_json::from_value(json!({"key":"notes","engine":"sqlite","url":"sqlite:///notes%20db.sqlite","workspace":dir.path()})).unwrap();
        let first = assistant_fingerprint(&record).unwrap();
        assert_eq!(first.1, None);
        std::fs::write(&path, b"data edit").unwrap();
        assert_eq!(assistant_fingerprint(&record).unwrap(), first);
        let replacement = dir.path().join("replacement");
        std::fs::write(&replacement, b"new database").unwrap();
        std::fs::rename(&replacement, &path).unwrap();
        assert_ne!(assistant_fingerprint(&record).unwrap().0, first.0);
    }
}

#[cfg(test)]
mod host_integration_tests {
    use super::*;
    use quarry_assistant::{MetadataProvider, Target};

    fn write_workspace(root: &Path, name: &str, content: &str) -> PathBuf {
        let directory = root.join(name);
        std::fs::create_dir_all(&directory).unwrap();
        std::fs::write(directory.join("connections.toml"), content).unwrap();
        std::fs::canonicalize(directory).unwrap()
    }
    fn host(root: &Path, workspaces: Vec<PathBuf>) -> QuarryService {
        QuarryService::new(root.join("config.toml"), workspaces, root.join("state"))
    }
    #[test]
    fn workspace_status_resolves_tilde_and_relative_paths_without_hiding_missing_entries() {
        let Some(home) = std::env::var_os("HOME") else {
            return;
        };
        let home = PathBuf::from(home);
        let tilde_dir = tempfile::Builder::new()
            .prefix(".quarry-workspace-test-")
            .tempdir_in(&home)
            .unwrap();
        std::fs::write(tilde_dir.path().join("connections.toml"), "").unwrap();
        let tilde = format!(
            "~/{}",
            tilde_dir.path().file_name().unwrap().to_string_lossy()
        );
        let dir = tempfile::tempdir().unwrap();
        let relative = write_workspace(dir.path(), "relative", "");
        let empty = dir.path().join("empty");
        std::fs::create_dir(&empty).unwrap();
        std::fs::create_dir(dir.path().join("unregistered")).unwrap();
        let config = format!(
            "workspaces = {}\nproxy_enabled_workspaces = {}\n",
            json!([tilde, "relative", "empty", "missing"]),
            json!([tilde])
        );
        let config_path = dir.path().join("config.toml");
        std::fs::write(&config_path, &config).unwrap();
        let listing = host(dir.path(), vec![]).workspaces().unwrap();
        let items = listing["items"].as_array().unwrap();
        assert_eq!(items.len(), 4);
        assert_eq!(
            items[0]["dir"],
            json!(std::fs::canonicalize(tilde_dir.path()).unwrap())
        );
        assert_eq!(items[0]["display"], json!(tilde));
        assert_eq!(items[0]["exists"], true);
        assert_eq!(items[0]["hasConnections"], true);
        assert_eq!(items[0]["proxyEnabled"], true);
        assert_eq!(items[1]["dir"], json!(relative));
        assert_eq!(items[1]["exists"], true);
        assert_eq!(items[1]["hasConnections"], true);
        assert_eq!(items[2]["exists"], true);
        assert_eq!(items[2]["hasConnections"], false);
        assert_eq!(items[3]["exists"], false);
        assert_eq!(items[3]["hasConnections"], false);
        assert_eq!(std::fs::read_to_string(config_path).unwrap(), config);
    }
    #[tokio::test]
    async fn merges_local_remote_and_keeps_real_source_for_assistant_identity() {
        let dir = tempfile::tempdir().unwrap();
        let local = write_workspace(
            dir.path(),
            "local",
            "[shop_local]\ndb='shop'\nenv='local'\nurl='sqlite:///shop.db'\n[notes]\nurl='sqlite:///notes.db'\n",
        );
        let remote = write_workspace(
            dir.path(),
            "remote",
            "[shop_dev]\ndb='shop'\nenv='dev'\ngroup='Product'\nurl='sqlite:///shop.db'\n[shop_prod]\ndb='shop'\nenv='prod'\ngroup='Product'\nurl='sqlite:///prod.db'\n",
        );
        std::fs::write(local.join("shop.db"), b"").unwrap();
        let host = host(dir.path(), vec![local.clone(), remote.clone()]);
        let result = host.connections().await.unwrap();
        let group = result["groups"]
            .as_array()
            .unwrap()
            .iter()
            .find(|group| group["group"] == "Product")
            .unwrap();
        assert_eq!(group["ws"], json!(remote));
        assert_eq!(group["items"].as_array().unwrap().len(), 1);
        let shop = &group["items"][0];
        assert_eq!(shop["is_env_set"], true);
        assert_eq!(
            shop["envs"]
                .as_array()
                .unwrap()
                .iter()
                .map(|env| env["env"].as_str().unwrap())
                .collect::<Vec<_>>(),
            vec!["local", "dev", "prod"]
        );
        let notes = result["groups"]
            .as_array()
            .unwrap()
            .iter()
            .find(|group| group["group"].is_null())
            .unwrap();
        assert_eq!(notes["items"][0]["is_env_set"], false);
        assert_eq!(notes["items"][0]["envs"][0]["env"], Value::Null);
        let context = MetadataProvider::resolve(
            &host,
            &Target {
                db: "shop".into(),
                env: Some("local".into()),
                workspace: Some(remote.display().to_string()),
            },
        )
        .await
        .unwrap();
        assert_eq!(context.target.workspace, Some(local.display().to_string()));
        assert_eq!(context.gui_workspace, Some(display_path(&remote)));
        let unrelated = write_workspace(dir.path(), "unrelated", "");
        assert!(
            MetadataProvider::resolve(
                &host,
                &Target {
                    db: "shop".into(),
                    env: Some("local".into()),
                    workspace: Some(unrelated.display().to_string())
                }
            )
            .await
            .is_err()
        );
    }
    #[tokio::test]
    async fn duplicate_database_environment_requires_explicit_source() {
        let dir = tempfile::tempdir().unwrap();
        let first = write_workspace(
            dir.path(),
            "one",
            "[shop]\nenv='dev'\ngroup='Product'\nurl='sqlite:///one.db'\n",
        );
        let second = write_workspace(
            dir.path(),
            "two",
            "[shop]\nenv='dev'\ngroup='Product'\nurl='sqlite:///two.db'\n",
        );
        let host = host(dir.path(), vec![first.clone(), second]);
        assert!(
            host.resolve("shop", Some("dev"), None)
                .unwrap_err()
                .message
                .contains("ambiguous")
        );
        assert_eq!(
            host.resolve("shop", Some("dev"), Some(&first))
                .unwrap()
                .workspace,
            first
        );
        // Duplicate members remain visible; the host never silently picks one.
        assert_eq!(
            host.connections().await.unwrap()["groups"][0]["items"][0]["envs"]
                .as_array()
                .unwrap()
                .len(),
            2
        );
    }
    #[tokio::test]
    async fn sqlite_metadata_retains_model_tool_structure_and_never_reads_rows() {
        let dir = tempfile::tempdir().unwrap();
        let ws = write_workspace(
            dir.path(),
            "workspace",
            "[notes]\nurl='sqlite:///notes.db'\n",
        );
        std::fs::write(ws.join("notes.db"), b"").unwrap();
        let host = host(dir.path(), vec![ws.clone()]);
        let record = host.resolve("notes", None, None).unwrap();
        for sql in [
            "CREATE TABLE parent (id INTEGER PRIMARY KEY)",
            "CREATE TABLE child (id INTEGER PRIMARY KEY, parent_id INTEGER NOT NULL REFERENCES parent(id), label TEXT)",
            "INSERT INTO parent VALUES (9)",
            "INSERT INTO child VALUES (1, 9, 'private-row-value')",
        ] {
            let mut request = QueryRequest::new(sql);
            request.policy.allow_write = true;
            host.execute(&record, request).await.unwrap();
        }
        let context = MetadataProvider::resolve(
            &host,
            &Target {
                db: "notes".into(),
                env: None,
                workspace: Some(ws.display().to_string()),
            },
        )
        .await
        .unwrap();
        let result = MetadataProvider::describe_tables(&host, &context, &["child".into()])
            .await
            .unwrap();
        let description = &result["tables"][0];
        assert_eq!(description["name"], "child");
        assert_eq!(description["primaryKey"], json!(["id"]));
        assert_eq!(
            description["columns"][1],
            json!({"name":"parent_id","type":"INTEGER","nullable":false})
        );
        assert_eq!(
            description["foreignKeys"][0]["columns"],
            json!(["parent_id"])
        );
        assert_eq!(description["foreignKeys"][0]["targetTable"], "parent");
        assert_eq!(
            description["foreignKeys"][0]["targetColumns"],
            json!(["id"])
        );
        assert_eq!(description["capped"], false);
        assert!(!result.to_string().contains("private-row-value"));
        assert!(
            MetadataProvider::describe_tables(&host, &context, &["missing".into()])
                .await
                .is_err()
        );
    }
}

#[cfg(test)]
mod related_connection_tests {
    use super::*;
    fn workspace(root: &Path, name: &str, text: &str) -> PathBuf {
        let path = root.join(name);
        std::fs::create_dir_all(&path).unwrap();
        std::fs::write(path.join("connections.toml"), text).unwrap();
        std::fs::canonicalize(path).unwrap()
    }
    fn service(root: &Path, workspaces: Vec<PathBuf>) -> QuarryService {
        QuarryService::new(root.join("config.toml"), workspaces, root.join("state"))
    }
    #[test]
    fn remote_query_can_target_local_copy_with_inherited_group() {
        let dir = tempfile::tempdir().unwrap();
        let remote = workspace(
            dir.path(),
            "remote",
            "[shop_dev]\ndb='shop'\nenv='dev'\ngroup='Product'\nurl='sqlite:///dev.db'\n[shop_prod]\ndb='shop'\nenv='prod'\ngroup='Product'\nurl='sqlite:///prod.db'\n",
        );
        let local = workspace(
            dir.path(),
            "local",
            "[shop_local]\ndb='shop'\nenv='local'\nurl='sqlite:///local.db'\n",
        );
        let service = service(dir.path(), vec![remote.clone(), local.clone()]);
        assert!(
            service
                .resolve("shop", Some("local"), Some(&remote))
                .is_err()
        );
        assert_eq!(
            service
                .resolve_related("shop", Some("local"), &remote)
                .unwrap()
                .workspace,
            local
        );
        assert_eq!(
            service
                .resolve_related("shop", Some("dev"), &remote)
                .unwrap()
                .workspace,
            remote
        );
    }
    #[test]
    fn related_resolution_never_crosses_an_explicit_group_or_ambiguous_environment() {
        let dir = tempfile::tempdir().unwrap();
        let remote = workspace(
            dir.path(),
            "remote",
            "[shop_dev]\ndb='shop'\nenv='dev'\ngroup='Product'\nurl='sqlite:///dev.db'\n",
        );
        let unrelated = workspace(
            dir.path(),
            "unrelated",
            "[shop_local]\ndb='shop'\nenv='local'\ngroup='Other'\nurl='sqlite:///other.db'\n",
        );
        let service = service(dir.path(), vec![remote.clone(), unrelated]);
        assert!(
            service
                .resolve_related("shop", Some("local"), &remote)
                .is_err()
        );
        let local_one = workspace(
            dir.path(),
            "one",
            "[one]\ndb='shop'\nenv='local'\ngroup='Product'\nurl='sqlite:///one.db'\n",
        );
        let local_two = workspace(
            dir.path(),
            "two",
            "[two]\ndb='shop'\nenv='local'\ngroup='Product'\nurl='sqlite:///two.db'\n",
        );
        let service = super::related_connection_tests::service(
            dir.path(),
            vec![remote.clone(), local_one, local_two],
        );
        assert_eq!(
            service
                .resolve_related("shop", Some("local"), &remote)
                .unwrap_err()
                .code,
            "ambiguous"
        );
    }
    #[test]
    fn representative_workspace_alias_requires_the_actual_database_group() {
        let dir = tempfile::tempdir().unwrap();
        let representative = workspace(
            dir.path(),
            "representative",
            "[alpha]\nenv='dev'\ngroup='Product'\nurl='sqlite:///a.db'\n[beta]\nenv='dev'\ngroup='Product'\nurl='sqlite:///b.db'\n",
        );
        let local = workspace(
            dir.path(),
            "local",
            "[shared]\ndb='shop'\nenv='local'\ngroup='Product'\nurl='sqlite:///local.db'\n",
        );
        let service = service(dir.path(), vec![representative.clone(), local.clone()]);
        assert_eq!(
            service
                .resolve_related("shop", Some("local"), &representative)
                .unwrap()
                .workspace,
            local
        );
        let unrelated = workspace(
            dir.path(),
            "unrelated",
            "[shared]\ndb='other'\nenv='local'\ngroup='Other'\nurl='sqlite:///other.db'\n",
        );
        let service = super::related_connection_tests::service(
            dir.path(),
            vec![representative, local, unrelated.clone()],
        );
        assert!(
            service
                .resolve_related("shop", Some("local"), &unrelated)
                .is_err()
        );
        let outside = workspace(dir.path(), "outside", "");
        assert!(
            service
                .resolve_related("shop", Some("local"), &outside)
                .is_err()
        );
    }
}

#[cfg(test)]
mod alias_collision_tests {
    use super::*;
    #[test]
    fn a_connection_key_cannot_redirect_a_related_logical_database() {
        let dir = tempfile::tempdir().unwrap();
        let source = dir.path().join("source");
        let other = dir.path().join("other");
        for path in [&source, &other] {
            std::fs::create_dir_all(path).unwrap();
        }
        std::fs::write(
            source.join("connections.toml"),
            "[shop_dev]\ndb='shop'\nenv='dev'\ngroup='Product'\nurl='sqlite:///shop.db'\n",
        )
        .unwrap();
        std::fs::write(
            other.join("connections.toml"),
            "[shop]\ndb='unrelated'\nenv='local'\ngroup='Product'\nurl='sqlite:///other.db'\n",
        )
        .unwrap();
        let service = QuarryService::new(
            dir.path().join("config.toml"),
            vec![source.clone(), other],
            dir.path().join("state"),
        );
        let error = service
            .resolve_related("shop", Some("local"), &source)
            .unwrap_err();
        assert_eq!(error.code, "not_found");
    }
}
