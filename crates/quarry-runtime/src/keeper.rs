use crate::{Result, RuntimeConfig, RuntimeError, TunnelManager, WorkspaceCatalog, atomic_write};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    collections::BTreeMap,
    path::{Path, PathBuf},
    time::Duration,
};
#[derive(Debug, Clone)]
pub struct KeeperManager {
    pub config_path: PathBuf,
    pub state_dir: PathBuf,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
struct KeeperRecord {
    pid: u32,
    identity: String,
    workspace: PathBuf,
}
impl KeeperManager {
    pub fn new(config_path: PathBuf, state_dir: PathBuf) -> Self {
        Self {
            config_path,
            state_dir,
        }
    }
    fn key(&self, workspace: &Path) -> String {
        format!(
            "{:x}",
            Sha256::digest(workspace.to_string_lossy().as_bytes())
        )[..16]
            .into()
    }
    fn file(&self, workspace: &Path, suffix: &str) -> PathBuf {
        self.state_dir
            .join(format!("{}.{}", self.key(workspace), suffix))
    }
    fn record(&self, workspace: &Path) -> Result<Option<KeeperRecord>> {
        let path = self.file(workspace, "pid");
        if !path.exists() {
            return Ok(None);
        }
        let record: KeeperRecord = serde_json::from_slice(&std::fs::read(path)?)?;
        if record.workspace != workspace {
            return Ok(None);
        }
        if crate::transport::process_identity(record.pid).is_ok_and(|id| id == record.identity) {
            Ok(Some(record))
        } else {
            Ok(None)
        }
    }
    pub fn status(&self, workspace: &Path) -> Result<Value> {
        if let Some(record) = self.record(workspace)? {
            let payload = std::fs::read(self.file(workspace, "json"))
                .ok()
                .and_then(|b| serde_json::from_slice::<Value>(&b).ok())
                .unwrap_or_else(|| json!({"state":"starting","tunnels":[]}));
            Ok(json!({"running":true,"pid":record.pid,"workspace":workspace,"status":payload}))
        } else {
            Ok(
                json!({"running":false,"workspace":workspace,"status":{"state":"down","tunnels":[]}}),
            )
        }
    }
    pub async fn start(&self, workspace: &Path, executable: &Path) -> Result<Value> {
        self.start_with_launcher(
            workspace,
            &crate::DaemonLauncher::new(executable).arg("internal-keeper"),
        )
        .await
    }
    /// Start an explicit host entry point, appending --config, --workspace and
    /// --state-dir. That handler calls run_keeper with those same paths.
    pub async fn start_with_launcher(
        &self,
        workspace: &Path,
        launcher: &crate::DaemonLauncher,
    ) -> Result<Value> {
        if self.record(workspace)?.is_some() {
            return self.status(workspace);
        }
        let config = RuntimeConfig::load(&self.config_path)?;
        if config.setting("reconnect", workspace).is_none() {
            config.set_toggle("tunnel_reconnect_workspaces", workspace, true)?;
        }

        std::fs::create_dir_all(&self.state_dir)?;
        let options = || {
            let mut options = std::fs::OpenOptions::new();
            options.create(true).append(true);
            #[cfg(unix)]
            {
                use std::os::unix::fs::OpenOptionsExt;
                options.mode(0o600);
            }
            options
        };
        let log = options().open(self.file(workspace, "log"))?;
        let mut command = std::process::Command::new(&launcher.program);
        command
            .args(&launcher.prefix_args)
            .arg("--config")
            .arg(&self.config_path)
            .arg("--workspace")
            .arg(workspace)
            .arg("--state-dir")
            .arg(&self.state_dir)
            .stdin(std::process::Stdio::null())
            .stdout(log.try_clone()?)
            .stderr(log);
        #[cfg(unix)]
        {
            use std::os::unix::process::CommandExt;
            command.process_group(0);
        }
        let mut child = command.spawn()?;
        for _ in 0..100 {
            if self.record(workspace)?.is_some() {
                return self.status(workspace);
            }
            if child.try_wait()?.is_some() {
                return Err(RuntimeError::Operation(
                    "keeper exited during startup; inspect its private log".into(),
                ));
            }
            tokio::time::sleep(Duration::from_millis(50)).await;
        }
        Err(RuntimeError::Operation(
            "keeper did not become ready".into(),
        ))
    }
    pub async fn stop(&self, workspace: &Path) -> Result<Value> {
        let Some(record) = self.record(workspace)? else {
            return self.status(workspace);
        };
        if !crate::transport::process_identity(record.pid).is_ok_and(|id| id == record.identity) {
            return Err(RuntimeError::Operation(
                "keeper identity changed; refusing to signal it".into(),
            ));
        }
        let status = std::process::Command::new("kill")
            .args(["-TERM", &record.pid.to_string()])
            .status()?;
        if !status.success() {
            return Err(RuntimeError::Operation("could not stop keeper".into()));
        }
        for _ in 0..100 {
            if self.record(workspace)?.is_none() {
                return self.status(workspace);
            }
            tokio::time::sleep(Duration::from_millis(50)).await;
        }
        Err(RuntimeError::Operation(
            "keeper did not stop within five seconds".into(),
        ))
    }
}
pub async fn run_keeper(
    config_path: PathBuf,
    workspace: PathBuf,
    state_dir: PathBuf,
) -> Result<()> {
    let manager = KeeperManager::new(config_path.clone(), state_dir.clone());
    std::fs::create_dir_all(&state_dir)?;
    let lock = std::fs::OpenOptions::new()
        .create(true)
        .truncate(false)
        .read(true)
        .write(true)
        .open(manager.file(&workspace, "lock"))?;
    if fs2::FileExt::try_lock_exclusive(&lock).is_err() {
        return Err(RuntimeError::Operation(
            "a keeper already owns this workspace".into(),
        ));
    }
    let record = KeeperRecord {
        pid: std::process::id(),
        identity: crate::transport::process_identity(std::process::id())?,
        workspace: workspace.clone(),
    };
    atomic_write(
        &manager.file(&workspace, "pid"),
        &serde_json::to_vec(&record)?,
    )?;
    let tunnels = TunnelManager::new(state_dir.join("tunnels")).with_shared_owner(true);
    let mut retries: BTreeMap<String, (u64, std::time::Instant)> = BTreeMap::new();
    let mut revisions: BTreeMap<String, String> = BTreeMap::new();
    let mut blocked: BTreeMap<String, String> = BTreeMap::new();
    let mut attempted = std::collections::BTreeSet::new();
    #[cfg(unix)]
    let mut terminate = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())?;
    let shutdown = async {
        #[cfg(unix)]
        {
            tokio::select! {_ = tokio::signal::ctrl_c()=>{},_ = terminate.recv()=>{}}
        }
        #[cfg(not(unix))]
        {
            let _ = tokio::signal::ctrl_c().await;
        }
    };
    tokio::pin!(shutdown);
    let result = async {
        'service: loop {
            tokio::select! {
                _ = &mut shutdown => break,
                _ = tokio::time::sleep(Duration::from_secs(1)) => {}
            }
            let config = match RuntimeConfig::load(&config_path) {
                Ok(config) => config,
                Err(error) => {
                    atomic_write(&manager.file(&workspace,"json"),&serde_json::to_vec(&json!({"state":"blocked","lastError":error.to_string(),"tunnels":[]}))?)?;
                    continue;
                }
            };
            let reconnect = config.enabled("reconnect", &workspace);
            let catalog = WorkspaceCatalog::load(config.clone(), std::slice::from_ref(&workspace))?;
            let connections = match catalog.connections() {
                Ok(connections) => connections,
                Err(error) => {
                    atomic_write(&manager.file(&workspace,"json"),&serde_json::to_vec(&json!({"state":"blocked","lastError":error.to_string(),"tunnels":[]}))?)?;
                    continue;
                }
            };
            tunnels.retain_connections(&connections).await?;
            let mut states = vec![];
            for connection in connections.iter().filter(|c| c.ssh_host.is_some()) {
                let key = format!("{}:{}", connection.key, connection.env);
                let key_revision = connection.ssh_key.as_ref()
                    .and_then(|p| crate::absolute(Path::new(p), &workspace).ok())
                    .and_then(|p| std::fs::metadata(p).ok()).and_then(|m| m.modified().ok());
                // Hash sensitive settings before using them as an in-memory revision.
                let config_revision = std::env::var_os("HOME").map(PathBuf::from)
                    .and_then(|home| std::fs::read(home.join(".ssh/config")).ok())
                    .map(|bytes| format!("{:x}", Sha256::digest(bytes)));
                let revision = format!("{:x}|{key_revision:?}|{config_revision:?}|{}|{reconnect}",
                    Sha256::digest(serde_json::to_vec(connection)?), config.enabled("proxy", &workspace));
                if revisions.get(&key) != Some(&revision) {
                    revisions.insert(key.clone(), revision);
                    retries.remove(&key);
                    blocked.remove(&key);
                    attempted.remove(&key);
                }
                if let Some(error) = blocked.get(&key) {
                    states.push(json!({"connection":connection.key,"env":connection.env,"state":"blocked","lastError":error}));
                    continue;
                }
                if let Some((_, when)) = retries.get(&key)
                    && (std::time::Instant::now() < *when || (!reconnect && attempted.contains(&key))) {
                        states.push(json!({"connection":connection.key,"env":connection.env,"state":if reconnect{"reconnecting"}else{"down"}}));
                        continue;
                    }
                attempted.insert(key.clone());
                let connected = tokio::select! {
                    _ = &mut shutdown => break 'service,
                    result = tunnels.open(connection,config.enabled("proxy",&workspace).then_some(true)) => result,
                };
                match connected {
                    Ok(lease) => {
                        retries.remove(&key);
                        states.push(json!({"connection":connection.key,"env":connection.env,"state":"up","localPort":lease.endpoint().map(|e|e.port)}));
                    }
                    Err(error) => {
                        let permanent = matches!(error, RuntimeError::Invalid(_) | RuntimeError::NotFound(_));
                        let message = error.to_string();
                        if permanent {
                            blocked.insert(key.clone(), message.clone());
                        } else {
                            let previous = retries.get(&key).map(|v|v.0).unwrap_or(1);
                            retries.insert(key, ((previous * 2).min(30), std::time::Instant::now()+Duration::from_secs(previous)));
                        }
                        states.push(json!({"connection":connection.key,"env":connection.env,"state":if permanent{"blocked"}else if reconnect{"reconnecting"}else{"down"},"lastError":message}));
                    }
                }
            }
            atomic_write(&manager.file(&workspace,"json"),&serde_json::to_vec(&json!({"state":"running","tunnels":states,"updatedAt":chrono::Utc::now().timestamp()}))?)?;
        }
        Ok::<_, RuntimeError>(())
    }.await;
    let _ = tunnels.close_all().await;
    if manager
        .record(&workspace)?
        .is_some_and(|r| r.pid == std::process::id())
    {
        let _ = std::fs::remove_file(manager.file(&workspace, "pid"));
    }
    drop(lock);
    result
}
