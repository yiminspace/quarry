use crate::{Result, RuntimeError, atomic_write, lock_file};
use serde::{Deserialize, Serialize};
use std::{
    collections::BTreeMap,
    path::{Path, PathBuf},
};
use toml_edit::{DocumentMut, Item, value};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RuntimeConfig {
    #[serde(skip)]
    pub path: PathBuf,
    #[serde(default)]
    pub workspaces: Vec<PathBuf>,
    #[serde(default)]
    pub proxy_enabled_workspaces: Vec<PathBuf>,
    #[serde(default)]
    pub tunnel_keep_alive_workspaces: Vec<PathBuf>,
    #[serde(default)]
    pub tunnel_reconnect_workspaces: Vec<PathBuf>,
    #[serde(default)]
    pub tunnel_keep_alive_disabled_workspaces: Vec<PathBuf>,
    #[serde(default)]
    pub tunnel_reconnect_disabled_workspaces: Vec<PathBuf>,
    #[serde(default)]
    pub tunnel: BTreeMap<String, bool>,
    #[serde(default)]
    pub local: BTreeMap<String, u16>,
}
impl RuntimeConfig {
    pub fn load(path: impl AsRef<Path>) -> Result<Self> {
        let path = absolute(path.as_ref(), &std::env::current_dir()?)?;
        let text = match std::fs::read_to_string(&path) {
            Ok(s) => s,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => String::new(),
            Err(e) => return Err(e.into()),
        };
        let mut config: Self = toml::from_str(&text)
            .map_err(|_| RuntimeError::Invalid("invalid runtime TOML configuration".into()))?;
        config.path = path;
        Ok(config)
    }
    pub fn set_workspaces(&self, paths: &[PathBuf]) -> Result<Self> {
        self.edit(|doc| {
            let mut array = toml_edit::Array::new();
            for p in paths {
                array.push(p.to_string_lossy().as_ref());
            }
            doc["workspaces"] = value(array);
            Ok(())
        })
    }
    pub fn add_workspace(&self, path: &Path) -> Result<Self> {
        let resolved = absolute(path, &std::env::current_dir()?)?;
        let mut paths = self.workspaces.clone();
        if !paths
            .iter()
            .any(|x| absolute(x, &self.base()).ok() == Some(resolved.clone()))
        {
            paths.push(resolved);
        }
        self.set_workspaces(&paths)
    }
    pub fn remove_workspace(&self, path: &Path) -> Result<Self> {
        let target = absolute(path, &std::env::current_dir()?)?;
        let paths = self
            .workspaces
            .iter()
            .filter(|x| absolute(x, &self.base()).ok() != Some(target.clone()))
            .cloned()
            .collect::<Vec<_>>();
        self.set_workspaces(&paths)
    }
    pub fn set_toggle(&self, key: &str, workspace: &Path, enabled: bool) -> Result<Self> {
        if ![
            "proxy_enabled_workspaces",
            "tunnel_keep_alive_workspaces",
            "tunnel_reconnect_workspaces",
        ]
        .contains(&key)
        {
            return Err(RuntimeError::Invalid("unknown workspace toggle".into()));
        }
        let target = absolute(workspace, &std::env::current_dir()?)?;
        self.edit(|doc| {
            let mut array = toml_edit::Array::new();
            if let Some(old) = doc.get(key).and_then(Item::as_array) {
                for val in old {
                    if let Some(s) = val.as_str()
                        && absolute(Path::new(s), &self.base())? != target
                    {
                        array.push(s);
                    }
                }
            }
            if enabled {
                array.push(target.to_string_lossy().as_ref());
            }
            doc[key] = value(array);
            if key.starts_with("tunnel_") {
                let disabled = format!(
                    "{}_disabled_workspaces",
                    key.trim_end_matches("_workspaces")
                );
                let mut values = toml_edit::Array::new();
                if let Some(old) = doc.get(&disabled).and_then(Item::as_array) {
                    for item in old {
                        if let Some(raw) = item.as_str()
                            && absolute(Path::new(raw), &self.base())? != target
                        {
                            values.push(raw);
                        }
                    }
                }
                if !enabled {
                    values.push(target.to_string_lossy().as_ref());
                }
                doc[&disabled] = value(values);
            }
            Ok(())
        })
    }
    pub fn set_local_port(&self, engine: &str, port: u16) -> Result<Self> {
        if port == 0 || !["postgres", "redis", "neptune"].contains(&engine) {
            return Err(RuntimeError::Invalid("invalid local engine/port".into()));
        }
        self.edit(|doc| {
            doc["local"][format!("{engine}_port")] = value(i64::from(port));
            Ok(())
        })
    }
    pub fn enabled(&self, key: &str, ws: &Path) -> bool {
        self.setting(key, ws).unwrap_or(false)
    }
    pub fn setting(&self, key: &str, ws: &Path) -> Option<bool> {
        let ws = absolute(ws, &std::env::current_dir().ok()?).ok()?;
        let list = match key {
            "proxy" => &self.proxy_enabled_workspaces,
            "keep_alive" => &self.tunnel_keep_alive_workspaces,
            "reconnect" => &self.tunnel_reconnect_workspaces,
            _ => return None,
        };
        let disabled = match key {
            "keep_alive" => Some(&self.tunnel_keep_alive_disabled_workspaces),
            "reconnect" => Some(&self.tunnel_reconnect_disabled_workspaces),
            _ => None,
        };
        let has = |paths: &Vec<PathBuf>| {
            paths
                .iter()
                .any(|p| absolute(p, &self.base()).ok().as_ref() == Some(&ws))
        };
        if disabled.is_some_and(has) {
            Some(false)
        } else if has(list) {
            Some(true)
        } else {
            self.tunnel.get(key).copied()
        }
    }
    pub fn base(&self) -> PathBuf {
        self.path.parent().unwrap_or(Path::new(".")).to_path_buf()
    }
    pub fn edit(&self, update: impl FnOnce(&mut DocumentMut) -> Result<()>) -> Result<Self> {
        let _lock = lock_file(&self.path.with_extension("toml.lock"))?;
        let text = read_optional_text(&self.path)?;
        let mut doc = text
            .parse::<DocumentMut>()
            .map_err(|_| RuntimeError::Invalid("invalid runtime configuration".into()))?;
        update(&mut doc)?;
        atomic_write(&self.path, doc.to_string().as_bytes())?;
        Self::load(&self.path)
    }
}
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Workspace {
    pub home: PathBuf,
    pub connections_file: PathBuf,
    pub queries_dir: PathBuf,
}
#[derive(Clone, Serialize, Deserialize)]
pub struct ConnectionRecord {
    #[serde(default)]
    pub key: String,
    pub url: String,
    #[serde(default)]
    pub engine: Option<String>,
    #[serde(default)]
    pub db: Option<String>,
    #[serde(default = "default_env")]
    pub env: String,
    #[serde(skip, default = "explicit_env")]
    pub env_explicit: bool,
    #[serde(skip)]
    pub proxy_url: Option<String>,
    #[serde(default)]
    pub group: Option<String>,
    #[serde(default)]
    pub production: bool,
    #[serde(default)]
    pub notes: Option<String>,
    #[serde(default)]
    pub ssh_host: Option<String>,
    #[serde(default)]
    pub ssh_user: Option<String>,
    #[serde(default)]
    pub ssh_key: Option<String>,
    #[serde(default)]
    pub ssh_port: Option<u16>,
    #[serde(default)]
    pub workspace: PathBuf,
    #[serde(flatten)]
    pub extra: BTreeMap<String, toml::Value>,
}
fn explicit_env() -> bool {
    true
}
fn default_env() -> String {
    "dev".into()
}
impl ConnectionRecord {
    pub fn display_env(&self) -> Option<&str> {
        self.env_explicit.then_some(self.env.as_str())
    }
    pub fn logical_db(&self) -> &str {
        self.db.as_deref().unwrap_or(&self.key)
    }
    pub fn spec(&self) -> Result<quarry_core::ConnectionSpec> {
        let engine = if let Some(engine) = &self.engine {
            match engine.as_str() {
                "postgres" | "postgresql" => Ok(quarry_core::Engine::Postgres),
                "mysql" | "mariadb" => Ok(quarry_core::Engine::MySql),
                "sqlite" => Ok(quarry_core::Engine::Sqlite),
                "redis" => Ok(quarry_core::Engine::Redis),
                "neptune" => Ok(quarry_core::Engine::Neptune),
                _ => Err(quarry_core::Error::invalid("unknown database engine")),
            }
        } else {
            quarry_core::Engine::infer(&self.url)
        }
        .map_err(RuntimeError::from)?;
        let milliseconds = |key: &str, default: u64| -> Result<u64> {
            let Some(value) = self.extra.get(key) else {
                return Ok(default);
            };
            let seconds = value
                .as_integer()
                .filter(|seconds| *seconds > 0)
                .ok_or_else(|| {
                    RuntimeError::Invalid(format!(
                        "connection {key} must be positive integer seconds"
                    ))
                })?;
            (seconds as u64)
                .checked_mul(1000)
                .ok_or_else(|| RuntimeError::Invalid(format!("connection {key} is too large")))
        };
        let defaults = quarry_core::ConnectionSpec::default();
        Ok(quarry_core::ConnectionSpec {
            key: self.key.clone(),
            db: self.logical_db().into(),
            env: self.env.clone(),
            engine,
            url: self.url.clone(),
            production: self.production,
            workspace: self.workspace.clone(),
            timeout_ms: milliseconds("timeout", defaults.timeout_ms)?,
            connect_timeout_ms: milliseconds("connect_timeout", defaults.connect_timeout_ms)?,
            proxy_url: self.proxy_url.clone(),
            tls_ca_file: self
                .extra
                .get("tls_ca_file")
                .and_then(|v| v.as_str())
                .map(PathBuf::from),
            ..Default::default()
        })
    }
    pub fn redacted(&self) -> Self {
        let mut copy = self.clone();
        if let Ok(mut url) = url::Url::parse(&copy.url) {
            if url.password().is_some() {
                let _ = url.set_password(Some("REDACTED"));
            }
            let parameters = url
                .query_pairs()
                .map(|(key, value)| {
                    let value = if sensitive_field(&key) {
                        "REDACTED".into()
                    } else {
                        value.into_owned()
                    };
                    (key.into_owned(), value)
                })
                .collect::<Vec<_>>();
            if !parameters.is_empty() {
                url.query_pairs_mut().clear().extend_pairs(parameters);
            }
            copy.url = url.to_string();
        } else {
            copy.url = "[REDACTED INVALID URL]".into();
        }
        copy.ssh_key = None;
        copy.extra.retain(|key, _| !sensitive_field(key));
        copy.proxy_url = None;
        copy
    }
}
fn read_optional_text(path: &Path) -> Result<String> {
    match std::fs::read_to_string(path) {
        Ok(text) => Ok(text),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(String::new()),
        Err(error) => Err(error.into()),
    }
}
fn sensitive_field(key: &str) -> bool {
    let lower = key.to_ascii_lowercase();
    lower.contains("password")
        || lower.contains("secret")
        || lower.contains("token")
        || lower.contains("private_key")
        || lower == "sslkey"
        || lower == "api_key"
}
#[derive(Debug, Clone)]
pub struct WorkspaceCatalog {
    pub config: RuntimeConfig,
    pub workspaces: Vec<Workspace>,
}
impl WorkspaceCatalog {
    pub fn load(config: RuntimeConfig, explicit: &[PathBuf]) -> Result<Self> {
        let cwd = std::env::current_dir()?;
        let paths = if !explicit.is_empty() {
            explicit.to_vec()
        } else if !config.workspaces.is_empty() {
            config.workspaces.clone()
        } else {
            vec![cwd.clone()]
        };
        let workspaces = paths
            .iter()
            .map(|p| {
                let home = absolute(p, &cwd)?;
                Ok(Workspace {
                    connections_file: home.join("connections.toml"),
                    queries_dir: home.join("queries"),
                    home,
                })
            })
            .collect::<Result<Vec<_>>>()?;
        Ok(Self { config, workspaces })
    }
    pub fn connections(&self) -> Result<Vec<ConnectionRecord>> {
        let mut out = vec![];
        let discovered = crate::ProxyInfo::discover();
        for ws in &self.workspaces {
            if !ws.connections_file.exists() {
                continue;
            }
            let text = std::fs::read_to_string(&ws.connections_file)?;
            let mut tables: BTreeMap<String, toml::Value> =
                toml::from_str(&text).map_err(|_| {
                    RuntimeError::Invalid(format!(
                        "invalid connections TOML: {}",
                        ws.connections_file.display()
                    ))
                })?;
            let document = text
                .parse::<DocumentMut>()
                .map_err(|_| RuntimeError::Invalid("invalid connections TOML".into()))?;
            // Declaration order is observable in the existing sidebar, default
            // environment selection and CLI; never sort it by connection key.
            for (key, _) in document.iter() {
                let table = tables
                    .remove(key)
                    .ok_or_else(|| RuntimeError::Invalid("invalid connection table".into()))?;
                validate_name(key)?;
                let env_explicit = table.get("env").is_some();
                let mut record: ConnectionRecord = table
                    .try_into()
                    .map_err(|_| RuntimeError::Invalid(format!("invalid connection {key}")))?;
                record.key = key.to_string();
                record.env_explicit = env_explicit;
                record.workspace = ws.home.clone();
                if self.config.enabled("proxy", &ws.home)
                    && record.ssh_host.is_none()
                    && let Ok(url) = url::Url::parse(&record.url)
                    && let Some(host) = url.host_str()
                {
                    let decision = crate::ProxyInfo::decision(true, host, discovered.clone());
                    record.proxy_url = decision.proxy.map(|p| p.http_url());
                }
                out.push(record);
            }
        }
        Ok(out)
    }
    pub fn resolve(
        &self,
        db: &str,
        env: Option<&str>,
        workspace: Option<&Path>,
    ) -> Result<ConnectionRecord> {
        let conns = self.connections()?;
        let scope = workspace
            .map(|w| absolute(w, &std::env::current_dir()?))
            .transpose()?;
        let in_scope = |c: &&ConnectionRecord| scope.as_ref().is_none_or(|w| w == &c.workspace);
        let exact = conns
            .iter()
            .filter(in_scope)
            .filter(|c| c.key == db && env.is_none_or(|e| e == c.env))
            .collect::<Vec<_>>();
        let candidates = if exact.is_empty() {
            conns
                .iter()
                .filter(in_scope)
                .filter(|c| c.logical_db() == db && c.env == env.unwrap_or("dev"))
                .collect::<Vec<_>>()
        } else {
            exact
        };
        match candidates.as_slice() {
            [one] => Ok((*one).clone()),
            [] => Err(RuntimeError::NotFound(format!(
                "{db}@{}",
                env.unwrap_or("dev")
            ))),
            _ => Err(RuntimeError::Ambiguous(format!("{db}; select a workspace"))),
        }
    }
    pub fn put_connection(
        &self,
        workspace: &Path,
        record: &ConnectionRecord,
        overwrite: bool,
    ) -> Result<()> {
        validate_name(&record.key)?;
        let ws = self.workspace(workspace)?;
        let _guard = lock_file(&ws.connections_file.with_extension("toml.lock"))?;
        let text = read_optional_text(&ws.connections_file)?;
        let mut doc = text
            .parse::<DocumentMut>()
            .map_err(|_| RuntimeError::Invalid("invalid connections TOML".into()))?;
        if doc.contains_key(&record.key) && !overwrite {
            return Err(RuntimeError::Invalid(
                "connection exists; explicit overwrite required".into(),
            ));
        }
        let mut item = toml_edit::ser::to_document(record)
            .map_err(|e| RuntimeError::Invalid(e.to_string()))?;
        item.remove("key");
        item.remove("workspace");
        item.remove("env_explicit");
        if !record.env_explicit && record.env == "dev" {
            item.remove("env");
        }
        if let Some(table) = doc.get_mut(&record.key).and_then(Item::as_table_mut) {
            let obsolete = table
                .iter()
                .filter(|(key, _)| !item.contains_key(key))
                .map(|(key, _)| key.to_string())
                .collect::<Vec<_>>();
            for key in obsolete {
                table.remove(&key);
            }
            for (key, new) in item.iter() {
                let mut new = new.clone();
                if let (Some(old_value), Some(new_value)) =
                    (table.get(key).and_then(Item::as_value), new.as_value_mut())
                {
                    *new_value.decor_mut() = old_value.decor().clone();
                }
                table.insert(key, new);
            }
        } else {
            doc[&record.key] = Item::Table(item.as_table().clone());
        }
        atomic_write(&ws.connections_file, doc.to_string().as_bytes())
    }
    pub fn remove_connection(&self, workspace: &Path, key: &str) -> Result<bool> {
        let ws = self.workspace(workspace)?;
        let _guard = lock_file(&ws.connections_file.with_extension("toml.lock"))?;
        let text = std::fs::read_to_string(&ws.connections_file)?;
        let mut doc = text
            .parse::<DocumentMut>()
            .map_err(|_| RuntimeError::Invalid("invalid connections TOML".into()))?;
        let removed = doc.remove(key).is_some();
        if removed {
            atomic_write(&ws.connections_file, doc.to_string().as_bytes())?;
        }
        Ok(removed)
    }
    pub fn workspace(&self, path: &Path) -> Result<&Workspace> {
        let path = absolute(path, &std::env::current_dir()?)?;
        self.workspaces
            .iter()
            .find(|w| w.home == path)
            .ok_or_else(|| {
                RuntimeError::NotFound("workspace is not registered in this catalog".into())
            })
    }
    pub fn skill_links(&self, skill: &Path) -> Result<()> {
        if !skill.join("SKILL.md").is_file() {
            return Err(RuntimeError::Invalid(
                "skill directory requires SKILL.md".into(),
            ));
        }
        let root = skill.join("queries");
        if root.is_symlink() || root.exists() && !root.is_dir() {
            return Err(RuntimeError::Invalid(
                "skill queries must be a real directory".into(),
            ));
        }
        let mut planned = BTreeMap::new();
        for ws in &self.workspaces {
            let name = ws.home.file_name().ok_or_else(|| {
                RuntimeError::Invalid("workspace requires a directory name".into())
            })?;
            let target = ws.queries_dir.clone();
            let link = root.join(name);
            if target.starts_with(&root) || root.starts_with(&target) {
                return Err(RuntimeError::Invalid("recursive skill link".into()));
            }
            if planned
                .insert(link.clone(), target.clone())
                .is_some_and(|old| old != target)
            {
                return Err(RuntimeError::Ambiguous(
                    "duplicate workspace basename".into(),
                ));
            }
            if link.symlink_metadata().is_ok()
                && (!link.is_symlink() || std::fs::read_link(&link)? != target)
            {
                return Err(RuntimeError::Invalid(
                    "skill link conflicts with an existing entry".into(),
                ));
            }
        }
        std::fs::create_dir_all(&root)?;
        for (link, target) in planned {
            std::fs::create_dir_all(&target)?;
            if link.symlink_metadata().is_err() {
                #[cfg(unix)]
                std::os::unix::fs::symlink(target, link)?;
                #[cfg(not(unix))]
                return Err(RuntimeError::Invalid(
                    "skill links unsupported on this platform".into(),
                ));
            }
        }
        Ok(())
    }
}
pub fn validate_name(name: &str) -> Result<()> {
    if name.is_empty()
        || !name
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || c == '_' || c == '-')
        || name.starts_with('-')
    {
        Err(RuntimeError::Invalid(
            "resource name must contain only letters, digits, underscore or hyphen".into(),
        ))
    } else {
        Ok(())
    }
}
pub fn absolute(path: &Path, base: &Path) -> Result<PathBuf> {
    let s = path.to_string_lossy();
    let expanded = if s == "~" || s.starts_with("~/") {
        let home = std::env::var_os("HOME")
            .ok_or_else(|| RuntimeError::Invalid("HOME missing for tilde path".into()))?;
        PathBuf::from(home).join(s.strip_prefix("~/").unwrap_or(""))
    } else {
        path.into()
    };
    let joined = if expanded.is_absolute() {
        expanded
    } else {
        base.join(expanded)
    };
    if joined.exists() {
        Ok(joined.canonicalize()?)
    } else {
        let mut normalized = PathBuf::new();
        for c in joined.components() {
            match c {
                std::path::Component::ParentDir => {
                    normalized.pop();
                }
                std::path::Component::CurDir => {}
                _ => normalized.push(c),
            }
        }
        Ok(normalized)
    }
}

impl std::fmt::Debug for ConnectionRecord {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("ConnectionRecord")
            .field("key", &self.key)
            .field("db", &self.db)
            .field("env", &self.env)
            .field("workspace", &self.workspace)
            .field("url", &"[REDACTED]")
            .finish()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn connections_preserve_toml_declaration_order_and_implicit_environment() {
        let dir = tempfile::tempdir().unwrap();
        std::fs::write(dir.path().join("connections.toml"),"[z_last_alphabetically]\nurl='sqlite:///z.db'\n[a_first_alphabetically]\nurl='sqlite:///a.db'\nenv='local'\n").unwrap();
        let catalog = WorkspaceCatalog::load(
            RuntimeConfig::load(dir.path().join("config.toml")).unwrap(),
            &[dir.path().to_path_buf()],
        )
        .unwrap();
        let connections = catalog.connections().unwrap();
        assert_eq!(
            connections
                .iter()
                .map(|c| c.key.as_str())
                .collect::<Vec<_>>(),
            vec!["z_last_alphabetically", "a_first_alphabetically"]
        );
        assert_eq!(connections[0].display_env(), None);
        assert_eq!(connections[1].display_env(), Some("local"));
    }
    #[test]
    fn workspace_disable_overrides_global_defaults_and_enable_clears_it() {
        let dir = tempfile::tempdir().unwrap();
        let config_path = dir.path().join("config.toml");
        std::fs::write(&config_path, "[tunnel]\nkeep_alive=true\nreconnect=true\n").unwrap();
        let config = RuntimeConfig::load(&config_path).unwrap();
        assert!(config.enabled("keep_alive", dir.path()));
        let config = config
            .set_toggle("tunnel_keep_alive_workspaces", dir.path(), false)
            .unwrap();
        assert!(!config.enabled("keep_alive", dir.path()));
        assert_eq!(config.tunnel_keep_alive_disabled_workspaces.len(), 1);
        let config = config
            .set_toggle("tunnel_keep_alive_workspaces", dir.path(), true)
            .unwrap();
        assert!(config.enabled("keep_alive", dir.path()));
        assert!(config.tunnel_keep_alive_disabled_workspaces.is_empty());
        let config = config
            .set_toggle("tunnel_reconnect_workspaces", dir.path(), false)
            .unwrap();
        assert!(!config.enabled("reconnect", dir.path()));
    }
    #[test]
    fn connection_timeouts_are_checked_and_redaction_covers_query_secrets() {
        let mut connection:ConnectionRecord=serde_json::from_value(serde_json::json!({
            "url":"postgres://user:secret@localhost/app?password=another&sslpassword=third&application_name=quarry",
            "engine":"postgres","timeout":19,"connect_timeout":3,"api_key":"fourth"
        })).unwrap();
        let spec = connection.spec().unwrap();
        assert_eq!(spec.timeout_ms, 19000);
        assert_eq!(spec.connect_timeout_ms, 3000);
        let visible = serde_json::to_string(&connection.redacted()).unwrap();
        for hidden in ["secret", "another", "third", "fourth"] {
            assert!(!visible.contains(hidden));
        }
        assert!(visible.contains("application_name=quarry"));
        for bad in [
            toml::Value::Integer(0),
            toml::Value::Integer(-1),
            toml::Value::Integer(i64::MAX),
            toml::Value::String("15".into()),
        ] {
            connection.extra.insert("timeout".into(), bad);
            assert!(connection.spec().is_err());
        }
    }
    #[test]
    fn existing_configuration_assets_roundtrip_without_secret_debug() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path().join("ws");
        std::fs::create_dir_all(&root).unwrap();
        let config_path = dir.path().join("config.toml");
        std::fs::write(
            &config_path,
            format!(
                "# keep comment\nworkspaces = [\"{}\"]\n[local]\npostgres_port = 55434\n",
                root.display()
            ),
        )
        .unwrap();
        std::fs::write(root.join("connections.toml"),"# connection comment\n[app_dev]\nurl = 'postgres://user:secret@localhost/db'\nengine = 'postgres'\ndb = 'app'\nenv = 'dev'\n[notes]\nurl = 'sqlite:///notes.db'\nengine = 'sqlite'\n").unwrap();
        let config = RuntimeConfig::load(&config_path).unwrap();
        assert_eq!(config.local["postgres_port"], 55434);
        let catalog = WorkspaceCatalog::load(config.clone(), &[]).unwrap();
        let mut record = catalog.resolve("app", Some("dev"), None).unwrap();
        assert!(!format!("{record:?}").contains("secret"));
        assert!(!record.redacted().url.contains("secret"));
        assert_eq!(record.spec().unwrap().engine, quarry_core::Engine::Postgres);
        record.notes = Some("updated".into());
        catalog.put_connection(&root, &record, true).unwrap();
        let text = std::fs::read_to_string(root.join("connections.toml")).unwrap();
        assert!(text.contains("# connection comment"));
        assert!(text.contains("[notes]"));
        config
            .set_toggle("proxy_enabled_workspaces", &root, true)
            .unwrap();
        let text = std::fs::read_to_string(config_path).unwrap();
        assert!(text.contains("# keep comment"));
        assert!(text.contains("postgres_port = 55434"));
    }
    #[test]
    fn duplicate_connections_require_an_explicit_workspace() {
        let dir = tempfile::tempdir().unwrap();
        let mut paths = vec![];
        for name in ["a", "b"] {
            let path = dir.path().join(name);
            std::fs::create_dir_all(&path).unwrap();
            std::fs::write(
                path.join("connections.toml"),
                "[app]\nurl='sqlite:///db.sqlite'\nengine='sqlite'\n",
            )
            .unwrap();
            paths.push(path);
        }
        let config = RuntimeConfig::load(dir.path().join("config.toml")).unwrap();
        let catalog = WorkspaceCatalog::load(config, &paths).unwrap();
        assert!(matches!(
            catalog.resolve("app", None, None),
            Err(RuntimeError::Ambiguous(_))
        ));
        assert!(catalog.resolve("app", None, Some(&paths[0])).is_ok());
    }
    #[test]
    fn skill_conflict_does_not_overwrite_user_files() {
        let dir = tempfile::tempdir().unwrap();
        let ws = dir.path().join("workspace");
        std::fs::create_dir_all(&ws).unwrap();
        let skill = dir.path().join("skill");
        std::fs::create_dir_all(skill.join("queries")).unwrap();
        std::fs::write(skill.join("SKILL.md"), "test").unwrap();
        std::fs::write(skill.join("queries/workspace"), "keep").unwrap();
        let catalog = WorkspaceCatalog::load(
            RuntimeConfig::load(dir.path().join("config.toml")).unwrap(),
            &[ws],
        )
        .unwrap();
        assert!(catalog.skill_links(&skill).is_err());
        assert_eq!(
            std::fs::read_to_string(skill.join("queries/workspace")).unwrap(),
            "keep"
        );
    }
}
