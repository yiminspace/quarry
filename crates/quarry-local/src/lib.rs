//! Local Docker/database lifecycle. All configuration paths are caller-owned.
//! No function in this crate runs at construction or implicitly starts Docker.
mod neptune;
mod sync;
pub use neptune::serve_neptune;

use quarry_runtime::{
    ConnectionRecord, DaemonLauncher, Result, RuntimeConfig, RuntimeError, WorkspaceCatalog,
};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{collections::BTreeMap, path::PathBuf, process::Stdio, time::Duration};
use tokio::process::Command;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct LocalSpec {
    pub engine: String,
    pub container: String,
    pub volume: String,
    pub port: u16,
    pub internal_port: u16,
    pub image: String,
}
#[derive(Debug, Clone)]
pub struct LocalManager {
    pub config: RuntimeConfig,
    pub state_dir: PathBuf,
    pub docker: PathBuf,
    pub pg_dump: PathBuf,
    pub psql: PathBuf,
    resource_namespace: Option<String>,
    neptune_launcher: Option<DaemonLauncher>,
}
impl LocalManager {
    pub fn new(config: RuntimeConfig, state_dir: impl Into<PathBuf>) -> Self {
        Self {
            config,
            state_dir: state_dir.into(),
            docker: "docker".into(),
            pg_dump: "pg_dump".into(),
            psql: "psql".into(),
            resource_namespace: None,
            neptune_launcher: None,
        }
    }
    pub fn with_namespace(mut self, namespace: impl Into<String>) -> Result<Self> {
        let namespace = namespace.into();
        quarry_runtime::validate_name(&namespace)?;
        if namespace.len() > 80 {
            return Err(RuntimeError::Invalid(
                "resource namespace is too long".into(),
            ));
        }
        self.resource_namespace = Some(namespace);
        Ok(self)
    }
    pub fn with_tools(
        mut self,
        psql: Option<PathBuf>,
        pg_dump: Option<PathBuf>,
        docker: Option<PathBuf>,
    ) -> Self {
        if let Some(tool) = psql {
            self.psql = tool;
        }
        if let Some(tool) = pg_dump {
            self.pg_dump = tool;
        }
        if let Some(tool) = docker {
            self.docker = tool;
        }
        self
    }
    pub fn with_neptune_launcher(mut self, launcher: DaemonLauncher) -> Self {
        self.neptune_launcher = Some(launcher);
        self
    }
    pub fn spec(&self, engine: &str) -> Result<LocalSpec> {
        let (spec, port) = match engine {
            "postgres" => (
                LocalSpec {
                    engine: engine.into(),
                    container: "quarry-local-postgres".into(),
                    volume: "quarry-local-pgdata".into(),
                    port: 5433,
                    internal_port: 5432,
                    image: "postgres:16-alpine".into(),
                },
                5433,
            ),
            "neptune" => (
                LocalSpec {
                    engine: engine.into(),
                    container: "quarry-local-neptune-empty".into(),
                    volume: String::new(),
                    port: 18182,
                    internal_port: 18182,
                    image: "empty".into(),
                },
                18182,
            ),
            "redis" => (
                LocalSpec {
                    engine: engine.into(),
                    container: "quarry-local-redis".into(),
                    volume: "quarry-local-redisdata".into(),
                    port: 6380,
                    internal_port: 6379,
                    image: "redis:7-alpine".into(),
                },
                6380,
            ),
            _ => {
                return Err(RuntimeError::Invalid(
                    "Docker local engine must be postgres or redis".into(),
                ));
            }
        };
        let config = RuntimeConfig::load(&self.config.path)?;
        let spec = if let Some(namespace) = &self.resource_namespace {
            let suffix = match engine {
                "postgres" => "pgdata",
                "redis" => "redisdata",
                _ => "",
            };
            LocalSpec {
                container: format!("{namespace}-{engine}"),
                volume: if suffix.is_empty() {
                    String::new()
                } else {
                    format!("{namespace}-{suffix}")
                },
                ..spec
            }
        } else {
            spec
        };
        Ok(LocalSpec {
            port: config
                .local
                .get(&format!("{engine}_port"))
                .copied()
                .unwrap_or(port),
            ..spec
        })
    }
    pub async fn status(&self, engine: Option<&str>) -> Result<Value> {
        if engine == Some("neptune") {
            return self.neptune_status().await;
        }
        if engine.is_none() || engine == Some("all") {
            let mut items = vec![];
            for name in ["postgres", "redis"] {
                items.push(self.engine_status(&self.spec(name)?).await?);
            }
            items.push(self.neptune_status().await?);
            return Ok(json!({"items":items}));
        }
        self.engine_status(&self.spec(engine.unwrap())?).await
    }
    async fn engine_status(&self, spec: &LocalSpec) -> Result<Value> {
        let version = self
            .docker(
                &["version", "--format", "{{.Server.Version}}"],
                BTreeMap::new(),
                10,
            )
            .await;
        if version.is_err() {
            return Ok(
                json!({"engine":spec.engine,"docker":false,"state":"unknown","running":false,"port":spec.port,"volume":spec.volume}),
            );
        }
        let info = self.inspect(&spec.container).await?;
        let running = info
            .as_ref()
            .and_then(|v| v.pointer("/State/Running"))
            .and_then(Value::as_bool)
            .unwrap_or(false);
        let image = info
            .as_ref()
            .and_then(|v| v.pointer("/Config/Image"))
            .cloned()
            .unwrap_or(Value::Null);
        let binding = info.as_ref().map(|value| published_port(value, spec));
        let actual_port = binding
            .as_ref()
            .and_then(|value| value.as_ref().ok())
            .copied();
        let binding_conflict = binding.as_ref().and_then(|value| match value {
            Err(error) => Some(error.to_string()),
            Ok(port) if *port != spec.port => Some(
                "published port differs from the configured local port; reconcile it explicitly"
                    .into(),
            ),
            _ => None,
        });
        let volume_exists = self
            .docker(&["volume", "inspect", &spec.volume], BTreeMap::new(), 10)
            .await
            .is_ok();
        let conflict = !running
            && tokio::net::TcpStream::connect(("127.0.0.1", spec.port))
                .await
                .is_ok();
        Ok(
            json!({"engine":spec.engine,"docker":true,"running":running,"state":if running{"running"}else if info.is_some(){"stopped"}else{"absent"},"port":actual_port.unwrap_or(spec.port),"configured_port":spec.port,"published_bindings":info.as_ref().and_then(|v|v.pointer("/HostConfig/PortBindings")),"loopback_verified":binding.as_ref().is_some_and(Result::is_ok),"binding_conflict":binding_conflict,"image":image,"volume":spec.volume,"volume_exists":volume_exists,"port_conflict":conflict}),
        )
    }
    pub async fn up(&self, engine: &str, image: Option<&str>, port: Option<u16>) -> Result<Value> {
        if engine == "neptune" {
            return self.up_neptune(port, None).await;
        }
        let mut spec = self.spec(engine)?;
        let _lock = self.state_lock(&format!("engine-{engine}")).await?;
        if port == Some(0) {
            return Err(RuntimeError::Invalid("local port must be positive".into()));
        }
        if let Some(image) = image {
            if image.starts_with('-') || image.chars().any(char::is_whitespace) {
                return Err(RuntimeError::Invalid("invalid Docker image".into()));
            }
            spec.image = image.into();
        }
        let old_port = spec.port;
        if let Some(port) = port {
            spec.port = port;
        }
        self.docker(
            &["version", "--format", "{{.Server.Version}}"],
            BTreeMap::new(),
            10,
        )
        .await?;
        let info = self.inspect(&spec.container).await?;
        let mut exists = info.is_some();
        if let Some(info) = &info {
            let running = info
                .pointer("/State/Running")
                .and_then(Value::as_bool)
                .unwrap_or(false);
            let actual = published_port(info, &spec)?;
            if actual != spec.port {
                if port.is_none() {
                    return Err(RuntimeError::Invalid("existing container publishes a different port from configuration; inspect and reconcile the configured port or request an explicit port change; container and volume were left unchanged".into()));
                }
                if running {
                    return Err(RuntimeError::Invalid(
                        "stop the managed container before changing its port".into(),
                    ));
                }
                let managed = info["Mounts"]
                    .as_array()
                    .is_some_and(|m| m.iter().any(|v| v["Name"] == spec.volume));
                if !managed {
                    return Err(RuntimeError::Invalid(
                        "custom data mount: refusing container recreation".into(),
                    ));
                }
                self.require_free_port(spec.port).await?;
                if image.is_none()
                    && let Some(current) = info.pointer("/Config/Image").and_then(Value::as_str)
                {
                    spec.image = current.into();
                }
                self.docker(&["rm", &spec.container], BTreeMap::new(), 30)
                    .await?;
                exists = false;
            } else if running {
                return Ok(json!({"engine":engine,"state":"running","port":spec.port}));
            }
        }
        let state = if exists {
            self.docker(&["start", &spec.container], BTreeMap::new(), 30)
                .await?;
            "started"
        } else {
            self.require_free_port(spec.port).await?;
            let publish = format!("127.0.0.1:{}:{}", spec.port, spec.internal_port);
            let mount = format!(
                "{}:{}",
                spec.volume,
                if engine == "postgres" {
                    "/var/lib/postgresql/data"
                } else {
                    "/data"
                }
            );
            let mut args = vec![
                "run",
                "-d",
                "--name",
                &spec.container,
                "-p",
                &publish,
                "-v",
                &mount,
            ];
            let mut env = BTreeMap::new();
            if engine == "postgres" {
                args.extend([
                    "-e",
                    "POSTGRES_USER",
                    "-e",
                    "POSTGRES_PASSWORD",
                    "-e",
                    "POSTGRES_DB",
                ]);
                env.insert("POSTGRES_USER".into(), "quarry".into());
                env.insert("POSTGRES_PASSWORD".into(), "quarry".into());
                env.insert("POSTGRES_DB".into(), "postgres".into());
            }
            args.push(&spec.image);
            self.docker(&args, env, 180).await?;
            "created"
        };
        if engine == "postgres" {
            let mut ready = false;
            for _ in 0..80 {
                if self
                    .docker(
                        &[
                            "exec",
                            &spec.container,
                            "pg_isready",
                            "-h",
                            "127.0.0.1",
                            "-U",
                            "quarry",
                        ],
                        BTreeMap::new(),
                        5,
                    )
                    .await
                    .is_ok()
                {
                    ready = true;
                    break;
                }
                tokio::time::sleep(Duration::from_millis(500)).await;
            }
            if !ready {
                return Err(RuntimeError::Operation(
                    "local PostgreSQL did not become ready".into(),
                ));
            }
        }
        if spec.port != old_port {
            self.config.set_local_port(engine, spec.port)?;
            self.reconcile_ports(engine, old_port, spec.port)?;
        }
        Ok(
            json!({"engine":engine,"state":state,"port":spec.port,"container":spec.container,"volume":spec.volume}),
        )
    }
    pub async fn down(&self, engine: &str, purge: bool) -> Result<Value> {
        if engine == "all" {
            let mut items = vec![];
            for engine in ["postgres", "redis", "neptune"] {
                items.push(self.down_one(engine, purge).await?);
            }
            return Ok(json!({"items":items}));
        }
        self.down_one(engine, purge).await
    }
    async fn down_one(&self, engine: &str, purge: bool) -> Result<Value> {
        if engine == "neptune" {
            return self.down_neptune(purge).await;
        }
        let spec = self.spec(engine)?;
        let _lock = self.state_lock(&format!("engine-{engine}")).await?;
        let info = self.inspect(&spec.container).await?;
        let mut stopped = false;
        if let Some(info) = &info {
            if info.pointer("/State/Running").and_then(Value::as_bool) == Some(true) {
                self.docker(&["stop", &spec.container], BTreeMap::new(), 60)
                    .await?;
                stopped = true;
            }
            if purge {
                let managed = info["Mounts"]
                    .as_array()
                    .is_some_and(|m| m.iter().any(|v| v["Name"] == spec.volume));
                if !managed {
                    return Err(RuntimeError::Invalid(
                        "custom data mount: refusing purge".into(),
                    ));
                }
                self.docker(&["rm", &spec.container], BTreeMap::new(), 30)
                    .await?;
            }
        }
        let mut removed_volume = false;
        if purge
            && self
                .docker(&["volume", "inspect", &spec.volume], BTreeMap::new(), 10)
                .await
                .is_ok()
        {
            self.docker(&["volume", "rm", &spec.volume], BTreeMap::new(), 30)
                .await?;
            removed_volume = true;
        }
        Ok(
            json!({"engine":engine,"stopped":stopped,"purged":purge,"removed_volume":removed_volume}),
        )
    }
    pub async fn register(
        &self,
        catalog: &WorkspaceCatalog,
        db: &str,
        env: Option<&str>,
    ) -> Result<ConnectionRecord> {
        let source = catalog.resolve(db, env, None)?;
        self.register_from(catalog, source).await
    }
    pub fn registered(
        &self,
        catalog: &WorkspaceCatalog,
        source: &ConnectionRecord,
    ) -> Result<Option<ConnectionRecord>> {
        let scope = local_scope(source);
        let mut found = catalog
            .connections()?
            .into_iter()
            .filter(|connection| {
                if connection.logical_db() != source.logical_db() || connection.env != "local" {
                    return false;
                }
                if let Some(stored) = connection
                    .extra
                    .get("local_scope")
                    .and_then(toml::Value::as_str)
                {
                    return stored == scope;
                }
                if let Some(group) = source.group.as_deref().filter(|g| !g.is_empty()) {
                    connection.group.as_deref() == Some(group)
                } else {
                    connection.workspace == source.workspace
                }
            })
            .collect::<Vec<_>>();
        if found.len() > 1 {
            return Err(RuntimeError::Ambiguous("multiple local registrations exist for this project/database; keep one canonical local connection".into()));
        }
        Ok(found.pop())
    }
    pub fn stored_image(
        &self,
        catalog: &WorkspaceCatalog,
        source: &ConnectionRecord,
    ) -> Result<Option<String>> {
        Ok(self.registered(catalog, source)?.and_then(|c| {
            c.extra
                .get("local_image")
                .and_then(toml::Value::as_str)
                .map(String::from)
        }))
    }
    /// Register a logical database without requiring a remote connection first.
    /// Callers may start the engine with explicit image/port/fixture options first;
    /// registration reuses the resulting managed service and never copies data.
    pub async fn register_new(
        &self,
        catalog: &WorkspaceCatalog,
        workspace: &std::path::Path,
        db: &str,
        engine: &str,
    ) -> Result<ConnectionRecord> {
        quarry_runtime::validate_name(db)?;
        let workspace = catalog.workspace(workspace)?.home.clone();
        let url = match engine {
            "postgres" => format!("postgresql://localhost/{db}"),
            "redis" => "redis://localhost/0".into(),
            "neptune" => "https://localhost".into(),
            _ => {
                return Err(RuntimeError::Invalid(
                    "local engine must be postgres, redis or neptune".into(),
                ));
            }
        };
        let source: ConnectionRecord = serde_json::from_value(json!({
            "key":db,"db":db,"engine":engine,"url":url,"workspace":workspace
        }))?;
        self.register_from(catalog, source).await
    }
    async fn register_from(
        &self,
        catalog: &WorkspaceCatalog,
        source: ConnectionRecord,
    ) -> Result<ConnectionRecord> {
        let _lock = file_lock(self.config.base().join("local-registrations.lock")).await?;
        let existing = self.registered(catalog, &source)?;
        let stored_image = existing
            .as_ref()
            .and_then(|c| c.extra.get("local_image"))
            .and_then(toml::Value::as_str);
        let engine = match source.spec()?.engine {
            quarry_core::Engine::Postgres => "postgres",
            quarry_core::Engine::Redis => "redis",
            quarry_core::Engine::Neptune => "neptune",
            quarry_core::Engine::Sqlite => {
                return Err(RuntimeError::Invalid(
                    "SQLite uses an existing local file and does not need Docker".into(),
                ));
            }
            _ => {
                return Err(RuntimeError::Invalid(
                    "local registration is available for PostgreSQL, Redis and Neptune".into(),
                ));
            }
        };
        if engine != "neptune" || self.neptune_status().await?["running"] != true {
            self.up(engine, stored_image, None).await?;
        }
        let mut spec = self.spec(engine)?;
        if let Some(image) = stored_image {
            spec.image = image.into();
        }
        if engine != "neptune"
            && let Some(image) = self.inspect(&spec.container).await?.and_then(|v| {
                v.pointer("/Config/Image")
                    .and_then(Value::as_str)
                    .map(String::from)
            })
        {
            spec.image = image;
        }
        let connections = catalog.connections()?;
        if let Some(existing) = &existing {
            let managed = if engine == "neptune" {
                existing
                    .extra
                    .get("local_backend")
                    .and_then(toml::Value::as_str)
                    .is_some_and(|v| ["empty", "mock"].contains(&v))
            } else {
                existing
                    .extra
                    .get("local_volume")
                    .and_then(toml::Value::as_str)
                    == Some(&spec.volume)
            };
            if !managed {
                return Ok(existing.clone());
            }
        }
        quarry_runtime::validate_name(source.logical_db())?;
        let identity = local_identity(&source);
        if engine == "postgres" {
            let exists = self
                .docker(
                    &[
                        "exec",
                        &spec.container,
                        "psql",
                        "-U",
                        "quarry",
                        "-d",
                        "postgres",
                        "-tAc",
                        &format!("SELECT 1 FROM pg_database WHERE datname='{}'", identity),
                    ],
                    BTreeMap::new(),
                    15,
                )
                .await?;
            if exists.trim() != "1" {
                self.docker(
                    &[
                        "exec",
                        &spec.container,
                        "createdb",
                        "-U",
                        "quarry",
                        &identity,
                    ],
                    BTreeMap::new(),
                    30,
                )
                .await?;
            }
        }
        let mut local = existing.clone().unwrap_or_else(|| source.clone());
        let base = format!("{}_local", source.logical_db());
        let mut key = base.clone();
        let mut index = 2;
        while connections
            .iter()
            .any(|c| c.key == key && c.workspace == source.workspace)
        {
            key = format!("{base}{index}");
            index += 1;
        }
        if existing.is_none() {
            local.key = key;
        }
        local.engine = Some(engine.into());
        local.env = "local".into();
        local.env_explicit = true;
        local.production = false;
        local.ssh_host = None;
        local.ssh_key = None;
        local.ssh_port = None;
        local.ssh_user = None;
        local.proxy_url = None;
        for key in [
            "tls_ca_file",
            "local_volume",
            "local_backend",
            "local_image",
        ] {
            local.extra.remove(key);
        }
        local.db = Some(source.logical_db().into());
        local
            .extra
            .insert("local_scope".into(), toml_value(&local_scope(&source)));
        let redis_db = url::Url::parse(&source.url)
            .ok()
            .and_then(|u| u.path().trim_start_matches('/').parse::<u8>().ok())
            .unwrap_or(0);
        local.url = if engine == "postgres" {
            format!(
                "postgresql://quarry:quarry@localhost:{}/{}",
                spec.port, identity
            )
        } else if engine == "redis" {
            format!("redis://localhost:{}/{}", spec.port, redis_db)
        } else {
            format!("https://localhost:{}", spec.port)
        };
        if !spec.volume.is_empty() {
            local
                .extra
                .insert("local_volume".into(), toml_value(&spec.volume));
        } else {
            let status = self.neptune_status().await?;
            local.extra.insert(
                "local_backend".into(),
                toml_value(status["backend"].as_str().unwrap_or("empty")),
            );
            local.extra.insert(
                "tls_ca_file".into(),
                toml_value(
                    self.state_dir
                        .join("neptune/cert.pem")
                        .to_string_lossy()
                        .as_ref(),
                ),
            );
        }
        local
            .extra
            .insert("local_image".into(), toml_value(&spec.image));
        catalog.put_connection(&local.workspace, &local, existing.is_some())?;
        Ok(local)
    }
    pub(crate) async fn state_lock(&self, name: &str) -> Result<std::fs::File> {
        let path = self.state_dir.join(format!("{name}.lock"));
        file_lock(path).await
    }
    fn reconcile_ports(&self, engine: &str, old: u16, new: u16) -> Result<()> {
        let config = RuntimeConfig::load(&self.config.path)?;
        let catalog = WorkspaceCatalog::load(config, &[])?;
        for mut connection in catalog.connections()? {
            if connection.env != "local"
                || connection.ssh_host.is_some()
                || !connection.spec().is_ok_and(|s| s.engine.as_str() == engine)
            {
                continue;
            }
            let managed = if engine == "neptune" {
                connection
                    .extra
                    .get("local_backend")
                    .and_then(toml::Value::as_str)
                    .is_some_and(|b| ["empty", "mock"].contains(&b))
            } else {
                connection
                    .extra
                    .get("local_volume")
                    .and_then(toml::Value::as_str)
                    == Some(&self.spec(engine)?.volume)
            };
            if !managed {
                continue;
            }
            let mut url = url::Url::parse(&connection.url)
                .map_err(|_| RuntimeError::Invalid("invalid registered local URL".into()))?;
            if url.port() == Some(old) && url.host_str().is_some_and(is_loopback) {
                url.set_port(Some(new))
                    .map_err(|_| RuntimeError::Invalid("invalid local port".into()))?;
                connection.url = url.into();
                if engine == "neptune" {
                    connection.extra.insert(
                        "tls_ca_file".into(),
                        toml_value(
                            self.neptune_dir()
                                .join("cert.pem")
                                .to_string_lossy()
                                .as_ref(),
                        ),
                    );
                }
                catalog.put_connection(&connection.workspace, &connection, true)?;
            }
        }
        Ok(())
    }
    async fn require_free_port(&self, port: u16) -> Result<()> {
        if tokio::net::TcpStream::connect(("127.0.0.1", port))
            .await
            .is_ok()
        {
            Err(RuntimeError::Invalid(format!(
                "port {port} is already in use; existing services were not stopped"
            )))
        } else {
            Ok(())
        }
    }
    async fn inspect(&self, name: &str) -> Result<Option<Value>> {
        match self.docker(&["inspect", name], BTreeMap::new(), 10).await {
            Ok(output) => {
                let values: Vec<Value> = serde_json::from_str(&output)?;
                Ok(values.into_iter().next())
            }
            Err(RuntimeError::Operation(message)) if message.contains("not found") => Ok(None),
            Err(e) => Err(e),
        }
    }
    async fn docker(
        &self,
        args: &[&str],
        env: BTreeMap<String, String>,
        timeout: u64,
    ) -> Result<String> {
        let mut command = Command::new(&self.docker);
        command.args(args).envs(env).kill_on_drop(true);
        let output = tokio::time::timeout(Duration::from_secs(timeout), command.output())
            .await
            .map_err(|_| RuntimeError::Operation("Docker operation timed out".into()))??;
        if !output.status.success() {
            let error = String::from_utf8_lossy(&output.stderr).to_lowercase();
            let reason = if error.contains("no such") || error.contains("not found") {
                "Docker resource not found"
            } else if error.contains("address already in use")
                || error.contains("port is already allocated")
            {
                "Docker port is already in use"
            } else if error.contains("connect") || error.contains("daemon") {
                "Docker daemon is unavailable"
            } else {
                "Docker operation failed"
            };
            return Err(RuntimeError::Operation(reason.into()));
        }
        Ok(String::from_utf8_lossy(&output.stdout).into())
    }
}
fn toml_value(value: &str) -> toml::Value {
    toml::Value::String(value.into())
}
fn is_loopback(host: &str) -> bool {
    let host = host.trim_start_matches('[').trim_end_matches(']');
    host == "localhost"
        || host
            .parse::<std::net::IpAddr>()
            .is_ok_and(|v| v.is_loopback())
}
fn published_port(info: &Value, spec: &LocalSpec) -> Result<u16> {
    let expected = format!("{}/tcp", spec.internal_port);
    let inspect = |mapping: &Value| -> Result<u16> {
        let mappings = mapping.as_object().ok_or_else(||RuntimeError::Invalid("existing container has no explicit port mapping; inspect its network mode before migration; container and volume were left unchanged".into()))?;
        let mut expected_port = None;
        for (container_port, bindings) in mappings {
            if bindings.is_null() {
                continue;
            }
            let bindings = bindings.as_array().ok_or_else(||RuntimeError::Invalid("cannot verify existing container port bindings; container and volume were left unchanged".into()))?;
            for binding in bindings {
                let host = binding["HostIp"].as_str().unwrap_or("");
                if !host
                    .parse::<std::net::IpAddr>()
                    .is_ok_and(|ip| ip.is_loopback())
                {
                    return Err(RuntimeError::Invalid("existing container publishes on a wildcard or non-loopback address; inspect and retain its actual image, mounts and port, then explicitly recreate only the container with a 127.0.0.1 binding while preserving its named volume; do not purge; container and volume were left unchanged".into()));
                }
                let port = binding["HostPort"].as_str().and_then(|p|p.parse::<u16>().ok()).filter(|p|*p>0)
                    .ok_or_else(||RuntimeError::Invalid("existing container has an invalid published port; container and volume were left unchanged".into()))?;
                if container_port == &expected {
                    if expected_port.is_some_and(|previous| previous != port) {
                        return Err(RuntimeError::Invalid("existing container maps the database to multiple host ports; inspect and explicitly reconcile its mappings; container and volume were left unchanged".into()));
                    }
                    expected_port = Some(port);
                }
            }
        }
        expected_port.ok_or_else(||RuntimeError::Invalid("existing container does not publish the expected database port; container and volume were left unchanged".into()))
    };
    let configured = inspect(&info["HostConfig"]["PortBindings"])?;
    if info["State"]["Running"] == true {
        let active = inspect(&info["NetworkSettings"]["Ports"])?;
        if active != configured {
            return Err(RuntimeError::Invalid("active Docker port mappings differ from the container configuration; inspect and explicitly reconcile them; container and volume were left unchanged".into()));
        }
    }
    Ok(configured)
}
async fn file_lock(path: PathBuf) -> Result<std::fs::File> {
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    tokio::task::spawn_blocking(move || {
        let mut options = std::fs::OpenOptions::new();
        options.create(true).truncate(false).read(true).write(true);
        #[cfg(unix)]
        {
            use std::os::unix::fs::OpenOptionsExt;
            options.mode(0o600);
        }
        let file = options.open(path)?;
        fs2::FileExt::lock_exclusive(&file)?;
        Ok(file)
    })
    .await
    .map_err(|_| RuntimeError::Operation("local state lock worker failed".into()))?
}
pub fn local_scope(source: &ConnectionRecord) -> String {
    source
        .group
        .as_deref()
        .filter(|g| !g.is_empty())
        .map(|g| format!("group:{g}"))
        .unwrap_or_else(|| format!("workspace:{}", source.workspace.display()))
}
pub fn local_identity(source: &ConnectionRecord) -> String {
    let scope = local_scope(source);
    let normalize = |v: &str| {
        let raw = regex::Regex::new(r"[^A-Za-z0-9_]+")
            .unwrap()
            .replace_all(v, "_")
            .trim_matches('_')
            .to_ascii_lowercase();
        if raw.is_empty() {
            "workspace".into()
        } else if !raw.starts_with(|c: char| c.is_ascii_alphabetic()) {
            format!("ws_{raw}")
        } else {
            raw
        }
    };
    let namespace = if let Some(group) = source.group.as_deref().filter(|g| !g.is_empty()) {
        let mut namespace = normalize(group);
        if namespace != group.to_lowercase() {
            namespace += &format!("_{:x}", Sha256::digest(group.as_bytes()))[..9];
        }
        namespace
    } else {
        let basename = source
            .workspace
            .file_name()
            .unwrap_or_default()
            .to_string_lossy();
        format!(
            "{}_{:x}",
            normalize(&basename),
            Sha256::digest(source.workspace.to_string_lossy().as_bytes())
        )[..normalize(&basename).len() + 9]
            .into()
    };
    let name = format!("{namespace}_{}", source.logical_db());
    if name.len() > 54 {
        format!(
            "{}_{:x}",
            &name[..45],
            Sha256::digest(format!("{scope}:{}", source.logical_db()).as_bytes())
        )[..54]
            .into()
    } else {
        name
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn inspected_container(host: &str, port: u16, running: bool) -> Value {
        json!({"State":{"Running":running},"Config":{"Image":"postgres:17-alpine"},
            "HostConfig":{"PortBindings":{"5432/tcp":[{"HostIp":host,"HostPort":port.to_string()}]}},
            "NetworkSettings":{"Ports":{"5432/tcp":[{"HostIp":host,"HostPort":port.to_string()}]}},
            "Mounts":[{"Type":"volume","Name":"quarry-local-pgdata","Destination":"/var/lib/postgresql/data"}]})
    }
    fn docker_inspection_fixture(
        info: &Value,
        configured_port: u16,
    ) -> (tempfile::TempDir, LocalManager, PathBuf) {
        use std::os::unix::fs::PermissionsExt;
        let dir = tempfile::tempdir().unwrap();
        let inspect = dir.path().join("inspect.json");
        std::fs::write(&inspect, serde_json::to_vec(&vec![info]).unwrap()).unwrap();
        let calls = dir.path().join("calls");
        let docker = dir.path().join("docker");
        std::fs::write(
            &docker,
            format!(
                r#"#!/bin/sh
printf '%s\n' "$1" >> '{}'
case "$1" in
 version) echo fixture;;
 inspect) cat '{}';;
 volume) echo fixture;;
 *) echo 'unexpected mutation in read-only Docker fixture' >&2; exit 91;;
esac
"#,
                calls.display(),
                inspect.display()
            ),
        )
        .unwrap();
        std::fs::set_permissions(&docker, std::fs::Permissions::from_mode(0o700)).unwrap();
        let config = RuntimeConfig::load(dir.path().join("config.toml"))
            .unwrap()
            .set_local_port("postgres", configured_port)
            .unwrap();
        let manager = LocalManager::new(config, dir.path().join("state")).with_tools(
            None,
            None,
            Some(docker),
        );
        (dir, manager, calls)
    }
    #[tokio::test]
    async fn existing_wildcard_publish_requires_review_and_never_mutates_container_or_volume() {
        let mut fixtures = vec![];
        for host in ["0.0.0.0", "::", ""] {
            for running in [true, false] {
                fixtures.push(inspected_container(host, 55434, running));
            }
        }
        let mut active_wildcard = inspected_container("127.0.0.1", 55434, true);
        active_wildcard["NetworkSettings"]["Ports"]["5432/tcp"][0]["HostIp"] = json!("0.0.0.0");
        fixtures.push(active_wildcard);
        let mut additional_wildcard = inspected_container("127.0.0.1", 55434, true);
        additional_wildcard["HostConfig"]["PortBindings"]["9999/tcp"] =
            json!([{"HostIp":"0.0.0.0","HostPort":"9999"}]);
        fixtures.push(additional_wildcard);
        for fixture in fixtures {
            let (_dir, manager, calls) = docker_inspection_fixture(&fixture, 55434);
            let error = manager.up("postgres", None, None).await.unwrap_err();
            assert!(matches!(error, RuntimeError::Invalid(_)));
            assert!(error.to_string().contains("preserving its named volume"));
            if fixture["State"]["Running"] == true {
                let status = manager.status(Some("postgres")).await.unwrap();
                assert_eq!(status["loopback_verified"], false);
                assert!(status["binding_conflict"].is_string());
            }
            assert!(
                std::fs::read_to_string(calls)
                    .unwrap()
                    .lines()
                    .all(|call| ["version", "inspect", "volume"].contains(&call))
            );
        }
    }
    #[tokio::test]
    async fn existing_loopback_container_preserves_configured_port_and_actual_image() {
        for host in ["127.0.0.1", "::1"] {
            let fixture = inspected_container(host, 55434, true);
            let (_dir, manager, calls) = docker_inspection_fixture(&fixture, 55434);
            let output = manager.up("postgres", None, None).await.unwrap();
            assert_eq!(output["state"], "running");
            assert_eq!(output["port"], 55434);
            let status = manager.status(Some("postgres")).await.unwrap();
            assert_eq!(status["loopback_verified"], true);
            assert_eq!(status["image"], "postgres:17-alpine");
            assert_eq!(status["configured_port"], 55434);
            assert!(status["binding_conflict"].is_null());
            assert_eq!(
                RuntimeConfig::load(&manager.config.path).unwrap().local["postgres_port"],
                55434
            );
            assert!(
                std::fs::read_to_string(calls)
                    .unwrap()
                    .lines()
                    .all(|call| ["version", "inspect", "volume"].contains(&call))
            );
        }
    }
    #[tokio::test]
    async fn existing_stopped_container_port_drift_is_not_implicitly_recreated() {
        let fixture = inspected_container("127.0.0.1", 55435, false);
        let (_dir, manager, calls) = docker_inspection_fixture(&fixture, 55434);
        let error = manager.up("postgres", None, None).await.unwrap_err();
        assert!(matches!(error, RuntimeError::Invalid(_)));
        assert!(error.to_string().contains("explicit port change"));
        assert_eq!(
            std::fs::read_to_string(calls).unwrap(),
            "version\ninspect\n"
        );
        assert_eq!(
            RuntimeConfig::load(&manager.config.path).unwrap().local["postgres_port"],
            55434
        );
    }
    #[test]
    fn group_local_registration_survives_workspace_moves_and_retains_image() {
        let dir = tempfile::tempdir().unwrap();
        let a = dir.path().join("a");
        let b = dir.path().join("b");
        let c = dir.path().join("c");
        for path in [&a, &b, &c] {
            std::fs::create_dir_all(path).unwrap();
        }
        std::fs::write(a.join("connections.toml"),"[app_local]\nurl='postgres://localhost:5433/brain_app'\ndb='app'\nenv='local'\ngroup='brain'\nlocal_image='postgres:17-alpine'\n").unwrap();
        std::fs::write(
            b.join("connections.toml"),
            "[app_dev]\nurl='postgres://remote/app'\ndb='app'\nenv='dev'\ngroup='brain'\n",
        )
        .unwrap();
        std::fs::write(c.join("connections.toml"),"[app_dev]\nurl='postgres://remote/app'\ndb='app'\nenv='dev'\n[app_local]\nurl='postgres://localhost:5433/other_app'\ndb='app'\nenv='local'\n").unwrap();
        let config = RuntimeConfig::load(dir.path().join("config.toml")).unwrap();
        let catalog =
            WorkspaceCatalog::load(config.clone(), &[a.clone(), b.clone(), c.clone()]).unwrap();
        let manager = LocalManager::new(config, dir.path().join("state"));
        let grouped = catalog.resolve("app", Some("dev"), Some(&b)).unwrap();
        assert_eq!(
            manager
                .registered(&catalog, &grouped)
                .unwrap()
                .unwrap()
                .workspace,
            catalog.workspace(&a).unwrap().home
        );
        assert_eq!(
            manager.stored_image(&catalog, &grouped).unwrap().as_deref(),
            Some("postgres:17-alpine")
        );
        let ungrouped = catalog.resolve("app", Some("dev"), Some(&c)).unwrap();
        assert_eq!(
            manager
                .registered(&catalog, &ungrouped)
                .unwrap()
                .unwrap()
                .workspace,
            catalog.workspace(&c).unwrap().home
        );
        std::fs::write(b.join("connections.toml"),"[app_dev]\nurl='postgres://remote/app'\ndb='app'\nenv='dev'\ngroup='brain'\n[duplicate_local]\nurl='postgres://localhost:5433/brain_app'\ndb='app'\nenv='local'\nlocal_scope='group:brain'\n").unwrap();
        assert!(matches!(
            manager.registered(&catalog, &grouped),
            Err(RuntimeError::Ambiguous(_))
        ));
    }
    #[tokio::test]
    async fn docker_creation_binds_loopback_and_omits_password_from_arguments() {
        use std::os::unix::fs::PermissionsExt;
        let dir = tempfile::tempdir().unwrap();
        let script = dir.path().join("docker");
        let args_file = dir.path().join("arguments");
        std::fs::write(&script,format!("#!/bin/sh\ncase \"$1\" in\nversion) echo fixture;;\ninspect) echo 'No such container' >&2; exit 1;;\nrun) printf '%s\\n' \"$@\" > '{}'; echo created;;\nexec) exit 0;;\n*) exit 0;;\nesac\n",args_file.display())).unwrap();
        std::fs::set_permissions(&script, std::fs::Permissions::from_mode(0o700)).unwrap();
        let config = RuntimeConfig::load(dir.path().join("config.toml")).unwrap();
        let mut manager = LocalManager::new(config, dir.path().join("state"));
        manager.docker = script;
        let value = manager.up("postgres", None, None).await.unwrap();
        assert_eq!(value["state"], "created");
        let args = std::fs::read_to_string(args_file).unwrap();
        assert!(args.contains("127.0.0.1:5433:5432"));
        assert!(!args.contains("POSTGRES_PASSWORD="));
        assert!(!args.contains("quarry:quarry"));
    }
    #[tokio::test]
    async fn new_logical_database_registers_without_a_remote_connection() {
        use std::os::unix::fs::PermissionsExt;
        let dir = tempfile::tempdir().unwrap();
        let workspace = dir.path().join("workspace");
        std::fs::create_dir_all(&workspace).unwrap();
        let docker = dir.path().join("docker");
        std::fs::write(&docker,"#!/bin/sh\ncase \"$1\" in\nversion) echo fixture;;\ninspect) echo 'No such container' >&2; exit 1;;\nrun) echo created;;\nexec) exit 0;;\n*) exit 0;;\nesac\n").unwrap();
        std::fs::set_permissions(&docker, std::fs::Permissions::from_mode(0o700)).unwrap();
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        drop(listener);
        let config = RuntimeConfig::load(dir.path().join("config.toml"))
            .unwrap()
            .set_local_port("postgres", port)
            .unwrap();
        let catalog =
            WorkspaceCatalog::load(config.clone(), std::slice::from_ref(&workspace)).unwrap();
        let mut manager = LocalManager::new(config, dir.path().join("state"));
        manager.docker = docker;
        let local = manager
            .register_new(&catalog, &workspace, "standalone", "postgres")
            .await
            .unwrap();
        assert_eq!(local.env, "local");
        assert_eq!(local.logical_db(), "standalone");
        assert_eq!(url::Url::parse(&local.url).unwrap().port(), Some(port));
        assert!(local.ssh_host.is_none());
        let repeated = manager
            .register_new(&catalog, &workspace, "standalone", "postgres")
            .await
            .unwrap();
        assert_eq!(local.key, repeated.key);
        assert_eq!(catalog.connections().unwrap().len(), 1);
    }
    #[test]
    fn stable_local_names_preserve_group_ownership() {
        let record:ConnectionRecord=serde_json::from_value(json!({"key":"app_dev","db":"app","url":"postgres://localhost/app","engine":"postgres","env":"dev","group":"brain","workspace":"/tmp/brain"})).unwrap();
        assert_eq!(local_identity(&record), "brain_app");
        let mut other = record.clone();
        other.group = Some("personal".into());
        assert_ne!(local_identity(&record), local_identity(&other));
        other.group = None;
        assert_eq!(local_identity(&other), "brain_d7909f87_app");
        other.group = Some("Brain Tools".into());
        assert_eq!(local_identity(&other), "brain_tools_bd35212b_app");
    }
}
