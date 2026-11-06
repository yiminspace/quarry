use crate::{ConnectionRecord, Result, RuntimeError, absolute, atomic_write, lock_file};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::{
    collections::BTreeMap,
    path::{Path, PathBuf},
    sync::{
        Arc, Weak,
        atomic::{AtomicU64, Ordering},
    },
    time::Duration,
};
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    net::{TcpListener, TcpStream},
    process::{Child, Command},
    sync::Mutex,
};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ProxyInfo {
    pub host: String,
    pub port: u16,
    pub source: String,
    #[serde(default)]
    pub exceptions: Vec<String>,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ProxyDecision {
    pub proxy: Option<ProxyInfo>,
    pub discovered: Option<ProxyInfo>,
    pub reason: String,
}
impl ProxyInfo {
    pub fn http_url(&self) -> String {
        let host = self.host.trim_start_matches('[').trim_end_matches(']');
        format!(
            "http://{}:{}",
            if host.contains(':') {
                format!("[{host}]")
            } else {
                host.into()
            },
            self.port
        )
    }
    pub fn discover() -> Option<Self> {
        #[cfg(target_os = "macos")]
        {
            if let Ok(output) = std::process::Command::new("/usr/sbin/scutil")
                .arg("--proxy")
                .output()
                && output.status.success()
                && let Some(proxy) = Self::from_scutil(&String::from_utf8_lossy(&output.stdout))
            {
                return Some(proxy);
            }
        }
        for key in [
            "ALL_PROXY",
            "all_proxy",
            "HTTPS_PROXY",
            "https_proxy",
            "HTTP_PROXY",
            "http_proxy",
        ] {
            if let Ok(value) = std::env::var(key) {
                let value = if value.contains("://") {
                    value
                } else {
                    format!("http://{value}")
                };
                if let Ok(url) = url::Url::parse(&value) {
                    if !url.username().is_empty() || url.password().is_some() {
                        continue;
                    }
                    if let (Some(host), Some(port)) = (url.host_str(), url.port_or_known_default())
                    {
                        return Some(Self {
                            host: host.trim_start_matches('[').trim_end_matches(']').into(),
                            port,
                            source: "environment".into(),
                            exceptions: vec![],
                        });
                    }
                }
            }
        }
        None
    }
    pub fn from_scutil(text: &str) -> Option<Self> {
        let mut fields = BTreeMap::new();
        let mut exceptions = vec![];
        let mut in_exceptions = false;
        for line in text.lines() {
            let line = line.trim();
            if line.starts_with("ExceptionsList") {
                in_exceptions = true;
                continue;
            }
            if in_exceptions {
                if line.starts_with('}') {
                    in_exceptions = false;
                } else if let Some((_, v)) = line.split_once(':') {
                    exceptions.push(v.trim().into());
                }
                continue;
            }
            if let Some((k, v)) = line.split_once(':') {
                fields.insert(k.trim(), v.trim());
            }
        }
        for prefix in ["HTTPS", "HTTP"] {
            if fields.get(format!("{prefix}Enable").as_str()) == Some(&"1") {
                let host = fields.get(format!("{prefix}Proxy").as_str())?.to_string();
                let port = fields.get(format!("{prefix}Port").as_str())?.parse().ok()?;
                return Some(Self {
                    host,
                    port,
                    source: "system".into(),
                    exceptions,
                });
            }
        }
        None
    }
    pub fn decision(enabled: bool, host: &str, discovered: Option<Self>) -> ProxyDecision {
        if !enabled {
            return ProxyDecision {
                proxy: None,
                discovered,
                reason: "disabled".into(),
            };
        }
        let Some(proxy) = discovered.clone() else {
            return ProxyDecision {
                proxy: None,
                discovered,
                reason: "not_discovered".into(),
            };
        };
        if is_loopback(host) || proxy.exceptions.iter().any(|v| host_exception(host, v)) {
            return ProxyDecision {
                proxy: None,
                discovered,
                reason: "exception_list".into(),
            };
        }
        let reachable =
            std::net::ToSocketAddrs::to_socket_addrs(&(proxy.host.as_str(), proxy.port))
                .ok()
                .is_some_and(|mut addresses| {
                    addresses.any(|a| {
                        std::net::TcpStream::connect_timeout(&a, Duration::from_millis(300)).is_ok()
                    })
                });
        ProxyDecision {
            proxy: reachable.then_some(proxy),
            discovered,
            reason: if reachable { "ok" } else { "port_unreachable" }.into(),
        }
    }
}
fn is_loopback(host: &str) -> bool {
    let host = host.trim_start_matches('[').trim_end_matches(']');
    host == "localhost"
        || host
            .parse::<std::net::IpAddr>()
            .is_ok_and(|ip| ip.is_loopback())
}
fn host_exception(host: &str, pattern: &str) -> bool {
    let host = host.trim_start_matches('[').trim_end_matches(']');
    if let Some(suffix) = pattern.strip_prefix("*.") {
        return host == suffix || host.ends_with(&format!(".{suffix}"));
    }
    if let Some((base, bits)) = pattern.split_once('/') {
        match (
            base.parse::<std::net::IpAddr>(),
            host.parse::<std::net::IpAddr>(),
            bits.parse::<u32>(),
        ) {
            (Ok(std::net::IpAddr::V4(base)), Ok(std::net::IpAddr::V4(ip)), Ok(bits))
                if bits <= 32 =>
            {
                return (u32::from(base) ^ u32::from(ip))
                    & (!0u32).checked_shl(32 - bits).unwrap_or(0)
                    == 0;
            }
            (Ok(std::net::IpAddr::V6(base)), Ok(std::net::IpAddr::V6(ip)), Ok(bits))
                if bits <= 128 =>
            {
                return (u128::from(base) ^ u128::from(ip))
                    & (!0u128).checked_shl(128 - bits).unwrap_or(0)
                    == 0;
            }
            _ => return false,
        }
    }
    host.eq_ignore_ascii_case(pattern)
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TunnelStatus {
    #[serde(default)]
    pub shared: bool,
    pub id: String,
    pub host: String,
    pub port: u16,
    pub local_port: u16,
    pub owner_pid: u32,
    pub child_pid: u32,
    pub owner_identity: String,
    pub child_identity: String,
    pub proxy: Option<ProxyInfo>,
    pub workspace: PathBuf,
}
struct ProxyTask(tokio::task::JoinHandle<()>);
impl ProxyTask {
    fn abort(&self) {
        self.0.abort();
    }
}
impl Drop for ProxyTask {
    fn drop(&mut self) {
        self.0.abort();
    }
}
struct TunnelEntry {
    status: TunnelStatus,
    child: Mutex<Option<Child>>,
    proxy_task: Option<ProxyTask>,
    _stderr_task: Option<ProxyTask>,
}
impl Drop for TunnelEntry {
    fn drop(&mut self) {
        if let Some(task) = &self.proxy_task {
            task.abort();
        }
    }
}
#[derive(Clone)]
pub struct TunnelLease {
    entry: Option<Arc<TunnelEntry>>,
}
impl TunnelLease {
    pub fn endpoint(&self) -> Option<quarry_core::NetworkEndpoint> {
        self.entry.as_ref().map(|e| quarry_core::NetworkEndpoint {
            host: "127.0.0.1".into(),
            port: e.status.local_port,
        })
    }
    pub fn spec(&self, record: &ConnectionRecord) -> Result<quarry_core::ConnectionSpec> {
        let mut spec = record.spec()?;
        spec.network_endpoint = self.endpoint();
        Ok(spec)
    }
}
struct TunnelIdentity {
    key: String,
    resolved_host: String,
    resolved_port: u16,
    target_host: String,
    port: u16,
    proxy: Option<ProxyInfo>,
}
pub struct TunnelManager {
    state_dir: PathBuf,
    ssh: PathBuf,
    pool: Mutex<BTreeMap<String, Arc<TunnelEntry>>>,
    shared: bool,
    connections: Mutex<BTreeMap<String, String>>,
    setups: Mutex<BTreeMap<String, Weak<Mutex<()>>>>,
    generation: AtomicU64,
}
impl TunnelManager {
    pub fn new(state_dir: impl Into<PathBuf>) -> Self {
        Self {
            state_dir: state_dir.into(),
            ssh: "ssh".into(),
            pool: Mutex::new(BTreeMap::new()),
            shared: false,
            connections: Mutex::new(BTreeMap::new()),
            setups: Mutex::new(BTreeMap::new()),
            generation: AtomicU64::new(0),
        }
    }
    pub fn with_shared_owner(mut self, shared: bool) -> Self {
        self.shared = shared;
        self
    }
    pub fn with_ssh_executable(mut self, executable: impl Into<PathBuf>) -> Self {
        self.ssh = executable.into();
        self
    }
    fn registry(&self) -> PathBuf {
        self.state_dir.join("tunnels.json")
    }
    async fn identity(
        &self,
        record: &ConnectionRecord,
        use_proxy: Option<bool>,
        discovered: Option<ProxyInfo>,
    ) -> Result<TunnelIdentity> {
        let ssh_host = record
            .ssh_host
            .as_deref()
            .ok_or_else(|| RuntimeError::Invalid("SSH host missing".into()))?;
        if ssh_host.starts_with('-')
            || ssh_host.chars().any(char::is_whitespace)
            || record
                .ssh_user
                .as_ref()
                .is_some_and(|u| u.starts_with('-') || u.chars().any(char::is_whitespace))
        {
            return Err(RuntimeError::Invalid("invalid SSH host or user".into()));
        }
        let (resolved_host, resolved_port, ssh_configuration) =
            resolved_ssh_configuration(&self.ssh, record).await?;
        let parsed = url::Url::parse(&record.url)
            .map_err(|_| RuntimeError::Invalid("invalid tunneled connection URL".into()))?;
        let target_host = parsed
            .query_pairs()
            .find(|(k, _)| k == "hostaddr" || k == "host")
            .map(|(_, v)| v.into_owned())
            .or_else(|| parsed.host_str().map(String::from))
            .unwrap_or_else(|| "127.0.0.1".into());
        let target_host = target_host
            .trim_start_matches('[')
            .trim_end_matches(']')
            .to_string();
        let port = parsed
            .query_pairs()
            .find(|(k, _)| k == "port")
            .and_then(|(_, v)| v.parse().ok())
            .or_else(|| parsed.port())
            .unwrap_or(match record.spec()?.engine {
                quarry_core::Engine::Postgres => 5432,
                quarry_core::Engine::MySql => 3306,
                quarry_core::Engine::Redis => 6379,
                quarry_core::Engine::Neptune => 8182,
                _ => {
                    return Err(RuntimeError::Invalid(
                        "SQLite cannot use an SSH tunnel".into(),
                    ));
                }
            });
        if target_host.chars().any(char::is_whitespace) || port == 0 {
            return Err(RuntimeError::Invalid(
                "invalid SSH forwarding destination".into(),
            ));
        }
        let decision = ProxyInfo::decision(use_proxy.unwrap_or(false), &resolved_host, discovered);
        let proxy = decision.proxy;
        let key = format!(
            "{:x}",
            Sha256::digest(
                format!(
                    "{ssh_host}|{:?}|{:?}|{}|{target_host}|{port}|{:?}|{use_proxy:?}|{ssh_configuration}",
                    record.ssh_port,
                    record.ssh_user,
                    record.ssh_key.as_deref().unwrap_or(""),
                    proxy
                )
                .as_bytes()
            )
        );
        Ok(TunnelIdentity {
            key,
            resolved_host,
            resolved_port,
            target_host,
            port,
            proxy,
        })
    }
    /// Report a live tunnel for the exact current connection/SSH/proxy dimensions.
    /// This reads existing state only; listing connections must never create a tunnel.
    pub async fn proxied(
        &self,
        record: &ConnectionRecord,
        use_proxy: Option<bool>,
        discovered: Option<ProxyInfo>,
    ) -> Result<bool> {
        if record.ssh_host.is_none() || use_proxy != Some(true) || discovered.is_none() {
            return Ok(false);
        }
        let registry = self.read_registry()?;
        let pooled = self
            .pool
            .lock()
            .await
            .values()
            .map(|entry| (entry.status.id.clone(), entry.status.clone()))
            .collect::<BTreeMap<_, _>>();
        // Avoid resolving SSH config for never-opened connections, including
        // connections whose key is absent. A configured proxy is not a live fact.
        if !registry
            .values()
            .chain(pooled.values())
            .any(|status| status.proxy.is_some())
        {
            return Ok(false);
        }
        let identity = self.identity(record, use_proxy, discovered).await?;
        let status = pooled
            .get(&identity.key)
            .or_else(|| registry.get(&identity.key));
        Ok(match status {
            Some(status) if status.proxy.is_some() => entry_alive(status).await,
            _ => false,
        })
    }
    async fn setup_lock(&self, key: &str) -> Arc<Mutex<()>> {
        let mut setups = self.setups.lock().await;
        setups.retain(|_, lock| lock.strong_count() > 0);
        if let Some(lock) = setups.get(key).and_then(Weak::upgrade) {
            return lock;
        }
        let lock = Arc::new(Mutex::new(()));
        setups.insert(key.to_owned(), Arc::downgrade(&lock));
        lock
    }
    async fn publish_entry(
        &self,
        record: &ConnectionRecord,
        key: &str,
        entry: Arc<TunnelEntry>,
        generation: u64,
    ) -> Result<()> {
        let mut pool = self.pool.lock().await;
        if self.generation.load(Ordering::SeqCst) != generation {
            return Err(RuntimeError::Operation(
                "tunnel pool closed during connection setup".into(),
            ));
        }
        let connection_id = format!(
            "{}|{}|{}",
            record.workspace.display(),
            record.key,
            record.env
        );
        let mut connections = self.connections.lock().await;
        if let Some(previous) = connections.insert(connection_id, key.to_owned())
            && previous != key
            && !connections.values().any(|value| value == &previous)
        {
            pool.remove(&previous);
        }
        pool.insert(key.to_owned(), entry);
        Ok(())
    }
    pub async fn open(
        &self,
        record: &ConnectionRecord,
        use_proxy: Option<bool>,
    ) -> Result<TunnelLease> {
        if record.ssh_host.is_none() {
            return Ok(TunnelLease { entry: None });
        }
        self.open_with_proxy_info(record, use_proxy, ProxyInfo::discover())
            .await
    }
    async fn open_with_proxy_info(
        &self,
        record: &ConnectionRecord,
        use_proxy: Option<bool>,
        discovered: Option<ProxyInfo>,
    ) -> Result<TunnelLease> {
        let Some(ssh_host) = &record.ssh_host else {
            return Ok(TunnelLease { entry: None });
        };
        let TunnelIdentity {
            key,
            resolved_host,
            resolved_port,
            target_host,
            port,
            proxy,
        } = self.identity(record, use_proxy, discovered).await?;
        let generation = self.generation.load(Ordering::SeqCst);
        let setup = self.setup_lock(&key).await;
        let _setup = setup.lock().await;
        if self.generation.load(Ordering::SeqCst) != generation {
            return Err(RuntimeError::Operation(
                "tunnel pool closed during connection setup".into(),
            ));
        }
        // Slow SSH setup and liveness checks are serialized only for this exact
        // tunnel. Unrelated probes, queries and status reads keep making progress.
        let existing = self.pool.lock().await.get(&key).cloned();
        if let Some(entry) = existing
            && entry_alive(&entry.status).await
        {
            self.publish_entry(record, &key, entry.clone(), generation)
                .await?;
            return Ok(TunnelLease { entry: Some(entry) });
        }
        self.pool.lock().await.remove(&key);
        let registry = self.read_registry()?;
        if let Some(status) = registry.get(&key)
            && status.shared
            && entry_alive(status).await
        {
            let entry = Arc::new(TunnelEntry {
                status: status.clone(),
                child: Mutex::new(None),
                proxy_task: None,
                _stderr_task: None,
            });
            self.publish_entry(record, &key, entry.clone(), generation)
                .await?;
            return Ok(TunnelLease { entry: Some(entry) });
        }
        let listener = TcpListener::bind(("127.0.0.1", 0)).await?;
        let local_port = listener.local_addr()?.port();
        drop(listener);
        let mut command = Command::new(&self.ssh);
        command.kill_on_drop(true).args([
            "-N",
            "-o",
            "ExitOnForwardFailure=yes",
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=accept-new",
            "-o",
            "ConnectTimeout=10",
            "-o",
            "ServerAliveInterval=15",
        ]);
        let mut proxy_task = None;
        if let Some(proxy) = &proxy {
            // A loopback byte relay lets OpenSSH keep its key/host semantics while Rust owns CONNECT.
            let listener = TcpListener::bind(("127.0.0.1", 0)).await?;
            let relay_port = listener.local_addr()?.port();
            let proxy = proxy.clone();
            let host = resolved_host.clone();
            let ssh_port = resolved_port;
            proxy_task = Some(ProxyTask(tokio::spawn(async move {
                while let Ok((mut local, _)) = listener.accept().await {
                    let proxy = proxy.clone();
                    let host = host.clone();
                    tokio::spawn(async move {
                        if let Ok(mut upstream) = proxy_connect(&proxy, &host, ssh_port).await {
                            let _ = tokio::io::copy_bidirectional(&mut local, &mut upstream).await;
                        }
                    });
                }
            })));
            command.args([
                "-o",
                "ProxyCommand=none",
                "-o",
                "ProxyJump=none",
                "-o",
                &format!("HostKeyAlias={ssh_host}"),
                "-o",
                "Hostname=127.0.0.1",
                "-p",
                &relay_port.to_string(),
            ]);
        } else {
            if use_proxy == Some(false) {
                command.args(["-o", "ProxyCommand=none", "-o", "ProxyJump=none"]);
            }
            if let Some(port) = record.ssh_port {
                command.arg("-p").arg(port.to_string());
            }
        }
        let forward_host = if target_host.contains(':') {
            format!("[{target_host}]")
        } else {
            target_host.clone()
        };
        command
            .arg("-L")
            .arg(format!("127.0.0.1:{local_port}:{forward_host}:{port}"));
        if let Some(key) = &record.ssh_key {
            let key = absolute(Path::new(key), &record.workspace)?;
            if !key.is_file() {
                return Err(RuntimeError::NotFound(
                    "SSH private key is unavailable".into(),
                ));
            }
            command.arg("-i").arg(key);
        }
        command.arg(if let Some(user) = &record.ssh_user {
            format!("{user}@{ssh_host}")
        } else {
            ssh_host.clone()
        });
        command
            .stdout(std::process::Stdio::null())
            .stderr(std::process::Stdio::piped());
        let mut child = command.spawn()?;
        let stderr = Arc::new(std::sync::Mutex::new(Vec::<u8>::new()));
        let mut stderr_task = child.stderr.take().map(|mut stream| {
            let bytes = stderr.clone();
            ProxyTask(tokio::spawn(async move {
                let mut buffer = [0u8; 2048];
                while let Ok(size) = stream.read(&mut buffer).await {
                    if size == 0 {
                        break;
                    }
                    if let Ok(mut bytes) = bytes.lock() {
                        bytes.extend_from_slice(&buffer[..size]);
                        let surplus = bytes.len().saturating_sub(8192);
                        bytes.drain(..surplus);
                    }
                }
            }))
        });
        let child_pid = child
            .id()
            .ok_or_else(|| RuntimeError::Operation("SSH did not start".into()))?;
        let mut ready = false;
        for _ in 0..100 {
            if child.try_wait()?.is_some() {
                if let Some(task) = &proxy_task {
                    task.abort();
                }
                // Let the bounded reader consume the final diagnostic, while
                // never returning raw SSH stderr (it may contain credentials).
                if let Some(task) = stderr_task.as_mut() {
                    let _ = tokio::time::timeout(Duration::from_millis(300), &mut task.0).await;
                }
                let diagnostic = stderr
                    .lock()
                    .map(|b| String::from_utf8_lossy(&b).to_ascii_lowercase())
                    .unwrap_or_default();
                return Err(ssh_setup_error(&diagnostic));
            }
            if TcpStream::connect(("127.0.0.1", local_port)).await.is_ok() {
                ready = true;
                break;
            }
            tokio::time::sleep(Duration::from_millis(100)).await;
        }
        if !ready {
            let _ = child.kill().await;
            if let Some(task) = &proxy_task {
                task.abort();
            }
            return Err(RuntimeError::Operation("SSH tunnel setup timed out".into()));
        }
        let status = TunnelStatus {
            shared: self.shared,
            id: key.clone(),
            host: ssh_host.clone(),
            port: resolved_port,
            local_port,
            owner_pid: std::process::id(),
            child_pid,
            owner_identity: process_identity(std::process::id())?,
            child_identity: process_identity(child_pid)?,
            proxy,
            workspace: record.workspace.clone(),
        };
        let entry = Arc::new(TunnelEntry {
            status: status.clone(),
            child: Mutex::new(Some(child)),
            proxy_task,
            _stderr_task: stderr_task,
        });
        self.update_registry(|r| {
            r.insert(key.clone(), status);
        })?;
        self.publish_entry(record, &key, entry.clone(), generation)
            .await?;
        Ok(TunnelLease { entry: Some(entry) })
    }
    pub async fn retain_connections(&self, records: &[ConnectionRecord]) -> Result<()> {
        let wanted = records
            .iter()
            .map(|r| format!("{}|{}|{}", r.workspace.display(), r.key, r.env))
            .collect::<std::collections::BTreeSet<_>>();
        let mut pool = self.pool.lock().await;
        let mut connections = self.connections.lock().await;
        connections.retain(|key, _| wanted.contains(key));
        let active = connections
            .values()
            .cloned()
            .collect::<std::collections::BTreeSet<_>>();
        pool.retain(|key, _| active.contains(key));
        Ok(())
    }
    pub async fn status(&self) -> Result<Vec<TunnelStatus>> {
        let registry = self.read_registry()?;
        let mut out = vec![];
        for status in registry.values() {
            if entry_alive(status).await {
                out.push(status.clone());
            }
        }
        Ok(out)
    }
    pub async fn close_all(&self) -> Result<()> {
        let entries = {
            let mut pool = self.pool.lock().await;
            self.generation.fetch_add(1, Ordering::SeqCst);
            self.connections.lock().await.clear();
            std::mem::take(&mut *pool)
        };
        for entry in entries.values() {
            if entry.status.owner_pid == std::process::id()
                && let Some(mut child) = entry.child.lock().await.take()
            {
                let _ = child.kill().await;
                let _ = child.wait().await;
                let id = entry.status.id.clone();
                self.update_registry(|r| {
                    r.remove(&id);
                })?;
            }
        }
        Ok(())
    }
    fn read_registry(&self) -> Result<BTreeMap<String, TunnelStatus>> {
        match std::fs::read(self.registry()) {
            Ok(bytes) => serde_json::from_slice(&bytes).map_err(Into::into),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(BTreeMap::new()),
            Err(e) => Err(e.into()),
        }
    }
    fn update_registry(&self, f: impl FnOnce(&mut BTreeMap<String, TunnelStatus>)) -> Result<()> {
        let _guard = lock_file(&self.state_dir.join("tunnels.lock"))?;
        let mut values = self.read_registry()?;
        f(&mut values);
        atomic_write(&self.registry(), &serde_json::to_vec_pretty(&values)?)
    }
}
pub(crate) fn process_identity(pid: u32) -> Result<String> {
    let out = std::process::Command::new("ps")
        .args(["-p", &pid.to_string(), "-o", "lstart=", "-o", "comm="])
        .env("LC_ALL", "C")
        .env("TZ", "UTC")
        .output()?;
    if !out.status.success() || out.stdout.is_empty() {
        return Err(RuntimeError::Operation(
            "process identity is unavailable".into(),
        ));
    }
    Ok(String::from_utf8_lossy(&out.stdout).trim().into())
}
async fn entry_alive(status: &TunnelStatus) -> bool {
    process_identity(status.owner_pid).is_ok_and(|id| id == status.owner_identity)
        && process_identity(status.child_pid).is_ok_and(|id| id == status.child_identity)
        && tokio::time::timeout(
            Duration::from_millis(300),
            TcpStream::connect(("127.0.0.1", status.local_port)),
        )
        .await
        .is_ok_and(|r| r.is_ok())
}
async fn proxy_connect(proxy: &ProxyInfo, host: &str, port: u16) -> Result<TcpStream> {
    let host = host.trim_start_matches('[').trim_end_matches(']');
    let authority = format!(
        "{}:{port}",
        if host.contains(':') {
            format!("[{host}]")
        } else {
            host.into()
        }
    );
    let mut stream = tokio::time::timeout(
        Duration::from_secs(10),
        TcpStream::connect((proxy.host.as_str(), proxy.port)),
    )
    .await
    .map_err(|_| RuntimeError::Operation("proxy connect timed out".into()))??;
    stream
        .write_all(format!("CONNECT {authority} HTTP/1.1\r\nHost: {authority}\r\n\r\n").as_bytes())
        .await?;
    let mut response = vec![];
    while !response.ends_with(b"\r\n\r\n") {
        if response.len() > 16384 {
            return Err(RuntimeError::Operation(
                "proxy response headers too large".into(),
            ));
        }
        let byte = tokio::time::timeout(Duration::from_secs(10), stream.read_u8())
            .await
            .map_err(|_| RuntimeError::Operation("proxy handshake timed out".into()))??;
        response.push(byte);
    }
    let first = String::from_utf8_lossy(&response);
    if !first.lines().next().is_some_and(|l| {
        l.split_whitespace()
            .nth(1)
            .is_some_and(|code| code.starts_with('2'))
    }) {
        return Err(RuntimeError::Operation("proxy rejected CONNECT".into()));
    }
    Ok(stream)
}

fn ssh_setup_error(diagnostic: &str) -> RuntimeError {
    let diagnostic = diagnostic.to_ascii_lowercase();
    if diagnostic.contains("permission denied")
        || diagnostic.contains("authentication failed")
        || diagnostic.contains("no more authentication methods")
    {
        RuntimeError::Invalid(
            "SSH authentication failed; update credentials or SSH configuration".into(),
        )
    } else if diagnostic.contains("host key verification failed")
        || diagnostic.contains("remote host identification has changed")
    {
        RuntimeError::Invalid("SSH host key verification failed; verify known_hosts".into())
    } else if diagnostic.contains("could not resolve hostname") {
        RuntimeError::Operation("SSH hostname lookup failed".into())
    } else if diagnostic.contains("connection refused") {
        RuntimeError::Operation("SSH connection refused".into())
    } else {
        RuntimeError::Operation(
            "SSH tunnel exited during setup; verify authentication, host key and target".into(),
        )
    }
}
async fn resolved_ssh_configuration(
    executable: &Path,
    record: &ConnectionRecord,
) -> Result<(String, u16, String)> {
    let host = record
        .ssh_host
        .as_deref()
        .ok_or_else(|| RuntimeError::Invalid("SSH host missing".into()))?;
    let mut command = Command::new(executable);
    command.arg("-G");
    if let Some(port) = record.ssh_port {
        command.arg("-p").arg(port.to_string());
    }
    if let Some(user) = &record.ssh_user {
        command.arg("-l").arg(user);
    }
    if let Some(key) = &record.ssh_key {
        command
            .arg("-i")
            .arg(absolute(Path::new(key), &record.workspace)?);
    }
    command.arg(host);
    command.kill_on_drop(true);
    let output = tokio::time::timeout(Duration::from_secs(10), command.output())
        .await
        .map_err(|_| {
            RuntimeError::Operation("OpenSSH configuration resolution timed out".into())
        })??;
    if !output.status.success() {
        return Err(RuntimeError::Invalid(
            "OpenSSH configuration could not be resolved".into(),
        ));
    }
    let text = String::from_utf8_lossy(&output.stdout);
    let mut hostname = host.to_string();
    let mut port = record.ssh_port.unwrap_or(22);
    for line in text.lines() {
        if let Some((key, value)) = line.split_once(' ') {
            if key == "hostname" {
                hostname = value.trim().into();
            } else if key == "port" {
                port = value
                    .trim()
                    .parse()
                    .map_err(|_| RuntimeError::Invalid("invalid SSH port configuration".into()))?;
            }
        }
    }
    let digest = format!("{:x}", Sha256::digest(&output.stdout));
    Ok((hostname, port, digest))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn write_mock_ssh(path: &Path, script: String) {
        use std::os::unix::fs::PermissionsExt;

        // Keep writable executable descriptors out of this process so a
        // concurrent fork cannot inherit one and cause Linux exec to fail.
        let writer = std::process::Command::new("/bin/sh")
            .args([
                "-c",
                r#"umask 077; printf '%s' "$1" > "$2""#,
                "quarry-fixture",
            ])
            .arg(script)
            .arg(path)
            .status()
            .unwrap();
        assert!(writer.success(), "cannot write mock SSH fixture");
        std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o700)).unwrap();
    }

    #[test]
    fn mock_ssh_process() {
        let Ok(port) = std::env::var("QUARRY_TEST_MOCK_SSH_PORT") else {
            return;
        };
        if let Ok(gate) = std::env::var("QUARRY_TEST_MOCK_SSH_GATE") {
            while !Path::new(&gate).exists() {
                std::thread::sleep(Duration::from_millis(10));
            }
        }
        let listener =
            std::net::TcpListener::bind(("127.0.0.1", port.parse::<u16>().unwrap())).unwrap();
        for socket in listener.incoming() {
            drop(socket.unwrap());
        }
    }
    #[tokio::test]
    async fn ssh_uses_system_configuration_unless_explicitly_disabled_and_leases_survive_replacement()
     {
        let dir = tempfile::tempdir().unwrap();
        let script = dir.path().join("ssh");
        let log = dir.path().join("args");
        let binary = std::env::current_exe()
            .unwrap()
            .to_string_lossy()
            .replace('\'', "'\\''");
        write_mock_ssh(
            &script,
            format!(
                r#"#!/bin/sh
if [ "$1" = -G ]; then printf 'hostname ssh.example\nport 22\nuser configured\n'; exit 0; fi
printf '%s\n' "$@" >> '{}'
for arg in "$@"; do
 case "$arg" in 127.0.0.1:*:*) port=${{arg#127.0.0.1:}}; port=${{port%%:*}};; esac
done
QUARRY_TEST_MOCK_SSH_PORT="$port" exec '{}' --exact transport::tests::mock_ssh_process --nocapture
"#,
                log.display(),
                binary
            ),
        );
        let manager = TunnelManager::new(dir.path().join("state")).with_ssh_executable(&script);
        let record:ConnectionRecord=serde_json::from_value(serde_json::json!({"key":"app","url":"postgres://user:secret@database.internal/app","engine":"postgres","ssh_host":"configured-alias","workspace":dir.path()})).unwrap();
        let first = manager.open(&record, None).await.unwrap();
        let first_port = first.endpoint().unwrap().port;
        let args = std::fs::read_to_string(&log).unwrap();
        assert!(args.contains("configured-alias"));
        assert!(!args.contains("root@"));
        assert!(!args.contains("ProxyCommand=none"));
        assert!(!args.contains("secret"));
        let second = manager.open(&record, Some(false)).await.unwrap();
        assert_ne!(first_port, second.endpoint().unwrap().port);
        assert!(TcpStream::connect(("127.0.0.1", first_port)).await.is_ok());
        let args = std::fs::read_to_string(&log).unwrap();
        assert!(args.contains("ProxyCommand=none"));
        assert!(args.contains("ProxyJump=none"));
        assert!(args.contains("127.0.0.1:"));
        drop(first);
        manager.close_all().await.unwrap();
        assert!(manager.status().await.unwrap().is_empty());
    }
    #[tokio::test]
    async fn slow_ssh_setup_does_not_block_unrelated_tunnels_and_cannot_survive_close() {
        let dir = tempfile::tempdir().unwrap();
        let script = dir.path().join("ssh");
        let gate = dir.path().join("release-slow-ssh");
        let started = dir.path().join("slow-ssh-started");
        let binary = std::env::current_exe()
            .unwrap()
            .to_string_lossy()
            .replace('\'', "'\\''");
        write_mock_ssh(
            &script,
            format!(
                r#"#!/bin/sh
if [ "$1" = -G ]; then printf 'hostname ssh.example\nport 22\nuser configured\n'; exit 0; fi
for arg in "$@"; do
 case "$arg" in 127.0.0.1:*:*) port=${{arg#127.0.0.1:}}; port=${{port%%:*}};; esac
 if [ "$arg" = slow-bastion ]; then export QUARRY_TEST_MOCK_SSH_GATE='{}'; : > '{}'; fi
done
QUARRY_TEST_MOCK_SSH_PORT="$port" exec '{}' --exact transport::tests::mock_ssh_process --nocapture
"#,
                gate.display(),
                started.display(),
                binary
            ),
        );
        let manager =
            Arc::new(TunnelManager::new(dir.path().join("state")).with_ssh_executable(&script));
        let slow:ConnectionRecord=serde_json::from_value(serde_json::json!({"key":"slow","url":"postgres://user:secret@slow.internal/app","engine":"postgres","ssh_host":"slow-bastion","workspace":dir.path()})).unwrap();
        let mut fast = slow.clone();
        fast.key = "fast".into();
        fast.ssh_host = Some("fast-bastion".into());
        let copy = manager.clone();
        let task_slow = slow.clone();
        let waiting = tokio::spawn(async move { copy.open(&task_slow, Some(false)).await });
        tokio::time::timeout(Duration::from_secs(2), async {
            while !started.exists() {
                tokio::time::sleep(Duration::from_millis(5)).await;
            }
        })
        .await
        .unwrap();
        // This would time out while the old global lock covered slow SSH startup.
        let (first, second) = tokio::time::timeout(Duration::from_secs(1), async {
            tokio::join!(
                manager.open(&fast, Some(false)),
                manager.open(&fast, Some(false))
            )
        })
        .await
        .expect("unrelated tunnel was blocked by slow SSH startup");
        let first = first.unwrap();
        let second = second.unwrap();
        assert_eq!(
            first.endpoint().unwrap().port,
            second.endpoint().unwrap().port
        );
        tokio::time::timeout(Duration::from_millis(500), manager.close_all())
            .await
            .unwrap()
            .unwrap();
        std::fs::write(&gate, b"go").unwrap();
        let error = tokio::time::timeout(Duration::from_secs(2), waiting)
            .await
            .unwrap()
            .unwrap()
            .err()
            .expect("closed pool accepted an in-flight tunnel");
        assert!(error.to_string().contains("pool closed"));
        // Closing and canceling one setup must not poison later requests.
        let reopened =
            tokio::time::timeout(Duration::from_secs(1), manager.open(&fast, Some(false)))
                .await
                .unwrap()
                .unwrap();
        assert!(reopened.endpoint().is_some());
        std::fs::remove_file(&gate).unwrap();
        std::fs::remove_file(&started).unwrap();
        let timed_out =
            tokio::time::timeout(Duration::from_millis(500), manager.open(&slow, Some(false)))
                .await;
        assert!(timed_out.is_err());
        assert!(
            started.exists(),
            "slow SSH did not reach the cancellation fixture"
        );
        std::fs::write(&gate, b"go").unwrap();
        let recovered =
            tokio::time::timeout(Duration::from_secs(1), manager.open(&slow, Some(false)))
                .await
                .unwrap()
                .unwrap();
        assert!(recovered.endpoint().is_some());
        manager.close_all().await.unwrap();
    }
    #[tokio::test]
    async fn proxy_badge_requires_exact_live_tunnel_including_shared_owner() {
        let dir = tempfile::tempdir().unwrap();
        let script = dir.path().join("ssh");
        let binary = std::env::current_exe()
            .unwrap()
            .to_string_lossy()
            .replace('\'', "'\\''");
        write_mock_ssh(
            &script,
            format!(
                r#"#!/bin/sh
if [ "$1" = -G ]; then printf 'hostname ssh.example\nport 22\nuser configured\n'; exit 0; fi
for arg in "$@"; do
 case "$arg" in 127.0.0.1:*:*) port=${{arg#127.0.0.1:}}; port=${{port%%:*}};; esac
done
QUARRY_TEST_MOCK_SSH_PORT="$port" exec '{}' --exact transport::tests::mock_ssh_process --nocapture
"#,
                binary
            ),
        );
        let state = dir.path().join("state");
        let manager = TunnelManager::new(&state)
            .with_ssh_executable(&script)
            .with_shared_owner(true);
        let observer = TunnelManager::new(&state).with_ssh_executable(&script);
        let listener = std::net::TcpListener::bind(("127.0.0.1", 0)).unwrap();
        let proxy = ProxyInfo {
            host: "127.0.0.1".into(),
            port: listener.local_addr().unwrap().port(),
            source: "test".into(),
            exceptions: vec![],
        };
        let record:ConnectionRecord=serde_json::from_value(serde_json::json!({"key":"app","url":"postgres://user:secret@database.internal/app","engine":"postgres","ssh_host":"configured-alias","workspace":dir.path()})).unwrap();
        // A reachable proxy and enabled toggle cannot predict a live tunnel.
        assert!(
            !manager
                .proxied(&record, Some(true), Some(proxy.clone()))
                .await
                .unwrap()
        );
        assert!(!state.exists());
        let lease = manager
            .open_with_proxy_info(&record, Some(true), Some(proxy.clone()))
            .await
            .unwrap();
        assert!(
            manager
                .proxied(&record, Some(true), Some(proxy.clone()))
                .await
                .unwrap()
        );
        assert!(
            observer
                .proxied(&record, Some(true), Some(proxy.clone()))
                .await
                .unwrap()
        );
        let mut other = record.clone();
        other.url = "postgres://user:secret@other.internal/app".into();
        assert!(
            !observer
                .proxied(&other, Some(true), Some(proxy.clone()))
                .await
                .unwrap()
        );
        other = record.clone();
        other.ssh_key = Some(dir.path().join("missing-key").display().to_string());
        assert!(
            !observer
                .proxied(&other, Some(true), Some(proxy.clone()))
                .await
                .unwrap()
        );
        assert!(
            !observer
                .proxied(&record, None, Some(proxy.clone()))
                .await
                .unwrap()
        );
        assert!(!observer.proxied(&record, Some(true), None).await.unwrap());
        let mut other_proxy = proxy.clone();
        let second_listener = std::net::TcpListener::bind(("127.0.0.1", 0)).unwrap();
        other_proxy.port = second_listener.local_addr().unwrap().port();
        assert!(
            !observer
                .proxied(&record, Some(true), Some(other_proxy))
                .await
                .unwrap()
        );
        // An open port is insufficient when registry process identity is stale.
        observer
            .update_registry(|registry| {
                for status in registry.values_mut() {
                    status.child_identity = "reused-pid".into();
                }
            })
            .unwrap();
        assert!(
            !observer
                .proxied(&record, Some(true), Some(proxy.clone()))
                .await
                .unwrap()
        );
        manager.close_all().await.unwrap();
        assert!(
            !observer
                .proxied(&record, Some(true), Some(proxy))
                .await
                .unwrap()
        );
        drop(lease);
    }
    #[test]
    fn ssh_diagnostics_are_classified_without_disclosing_raw_stderr() {
        let error = ssh_setup_error("Permission denied (publickey). secret-value proxypassword");
        assert!(matches!(error, RuntimeError::Invalid(_)));
        assert!(!error.to_string().contains("secret-value"));
        assert!(matches!(
            ssh_setup_error("Host key verification failed"),
            RuntimeError::Invalid(_)
        ));
        assert!(matches!(
            ssh_setup_error("Connection refused"),
            RuntimeError::Operation(_)
        ));
    }
    #[test]
    fn proxies_respect_exceptions() {
        let p = ProxyInfo {
            host: "127.0.0.1".into(),
            port: 1,
            source: "test".into(),
            exceptions: vec!["*.internal".into(), "10.0.0.0/8".into()],
        };
        assert!(host_exception("10.4.5.6", "10.0.0.0/8"));
        assert!(host_exception("db.internal", "*.internal"));
        assert_eq!(
            ProxyInfo::decision(true, "localhost", Some(p)).reason,
            "exception_list"
        );
    }
}
