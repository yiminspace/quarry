//! Cloud Local: per-user managed databases on a deployment-owned Docker
//! daemon (normally Docker-in-Docker beside the hosted server).
//!
//! Each Local is one container owned by exactly one IAM principal. Containers
//! publish only on the private network between the server and its Docker
//! daemon; users reach them through hosted API calls or an authenticated
//! byte tunnel. Credentials never leave the server.
use crate::{Principal, Result, failure, hash, now, toml_string};
use quarry_local::{ContainerLimits, DockerEndpoint, LocalManager};
use quarry_runtime::{ConnectionRecord, RuntimeConfig, WorkspaceCatalog};
use quarry_service::QuarryService;
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::{
    collections::{BTreeMap, HashMap},
    path::{Path, PathBuf},
    sync::{Arc, Mutex},
};

/// Deployment-owned configuration. Nothing here is selectable over HTTP.
#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CloudLocalOptions {
    /// Docker daemon address, e.g. `tcp://dind:2376`.
    pub docker_host: String,
    /// Directory holding `ca.pem`, `cert.pem`, `key.pem` for Docker TLS.
    #[serde(default)]
    pub docker_cert_path: Option<PathBuf>,
    /// Host name of the Docker daemon as seen from this server.
    pub connect_host: String,
    #[serde(default = "publish_ip")]
    pub publish_ip: String,
    /// Inclusive port range allocated to Locals inside the Docker daemon.
    #[serde(default = "port_range")]
    pub ports: [u16; 2],
    #[serde(default = "max_per_user")]
    pub max_per_user: usize,
    #[serde(default)]
    pub memory: Option<String>,
    #[serde(default)]
    pub cpus: Option<String>,
    /// Image name prefixes users may pick, e.g. `postgres:`. Empty uses defaults.
    #[serde(default)]
    pub images: Vec<String>,
    #[serde(default = "ticket_hours")]
    pub ticket_hours: u64,
}
fn publish_ip() -> String {
    "0.0.0.0".into()
}
fn port_range() -> [u16; 2] {
    [20000, 20999]
}
fn max_per_user() -> usize {
    5
}
fn ticket_hours() -> u64 {
    12
}
const DEFAULT_IMAGES: &[&str] = &[
    "postgres:",
    "postgis/postgis:",
    "pgvector/pgvector:",
    "redis:",
    "redis/redis-stack-server:",
    "valkey/valkey:",
];
const MAX_TUNNELS_PER_USER: usize = 32;

impl CloudLocalOptions {
    pub fn validate(&self) -> std::result::Result<(), String> {
        let [low, high] = self.ports;
        if low < 1024 || high < low || high - low > 20_000 {
            return Err("cloud_local.ports must be an increasing range above 1023".into());
        }
        if self.max_per_user == 0 || self.max_per_user > 100 {
            return Err("cloud_local.max_per_user must be between 1 and 100".into());
        }
        if self.ticket_hours == 0 || self.ticket_hours > 24 * 30 {
            return Err("cloud_local.ticket_hours must be between 1 and 720".into());
        }
        if self
            .images
            .iter()
            .any(|i| i.is_empty() || i.len() > 128 || i.starts_with('-') || i.contains(' '))
        {
            return Err("cloud_local.images contains an invalid prefix".into());
        }
        self.endpoint()
            .validate()
            .map_err(|_| "cloud_local Docker endpoint is invalid".to_string())?;
        self.limits(BTreeMap::new())
            .validate()
            .map_err(|_| "cloud_local memory/cpus limits are invalid".to_string())?;
        Ok(())
    }
    fn endpoint(&self) -> DockerEndpoint {
        DockerEndpoint {
            host: self.docker_host.clone(),
            cert_path: self.docker_cert_path.clone(),
            publish_ip: self.publish_ip.clone(),
            connect_host: self.connect_host.clone(),
        }
    }
    fn limits(&self, labels: BTreeMap<String, String>) -> ContainerLimits {
        ContainerLimits {
            memory: self.memory.clone(),
            cpus: self.cpus.clone(),
            labels,
        }
    }
    fn image_allowed(&self, image: &str) -> bool {
        let valid = !image.is_empty()
            && image.len() <= 200
            && !image.starts_with('-')
            && image
                .bytes()
                .all(|c| c.is_ascii_alphanumeric() || b"-._/:@".contains(&c));
        let prefixes: Vec<&str> = if self.images.is_empty() {
            DEFAULT_IMAGES.to_vec()
        } else {
            self.images.iter().map(String::as_str).collect()
        };
        valid && prefixes.iter().any(|p| image.starts_with(p))
    }
}

#[derive(Default, Serialize, Deserialize)]
struct Tickets {
    /// ticket hash → (owner, resource id, expiry)
    tickets: BTreeMap<String, (String, String, u64)>,
}
#[derive(Default, Serialize, Deserialize)]
struct Ports {
    ports: BTreeMap<u16, String>,
}

/// A cloud Local resolved for one owner.
pub struct OwnedLocal {
    pub id: String,
    pub record: ConnectionRecord,
    manager: LocalManager,
}
impl OwnedLocal {
    /// The private address of the database inside the Docker network.
    pub fn address(&self) -> Result<(String, u16)> {
        let url = url::Url::parse(&self.record.url)
            .map_err(|_| failure(500, "internal", "Invalid cloud Local address"))?;
        Ok((
            url.host_str()
                .ok_or_else(|| failure(500, "internal", "Invalid cloud Local address"))?
                .trim_start_matches('[')
                .trim_end_matches(']')
                .to_owned(),
            url.port()
                .ok_or_else(|| failure(500, "internal", "Invalid cloud Local address"))?,
        ))
    }
}

#[derive(Clone)]
pub struct CloudLocals {
    options: Arc<CloudLocalOptions>,
    service: Arc<QuarryService>,
    root: PathBuf,
    tunnels: Arc<Mutex<HashMap<String, usize>>>,
}
/// Releases a tunnel slot when the connection ends.
pub struct TunnelSlot {
    tunnels: Arc<Mutex<HashMap<String, usize>>>,
    owner: String,
}
impl Drop for TunnelSlot {
    fn drop(&mut self) {
        if let Ok(mut map) = self.tunnels.lock()
            && let Some(count) = map.get_mut(&self.owner)
        {
            *count = count.saturating_sub(1);
            if *count == 0 {
                map.remove(&self.owner);
            }
        }
    }
}

impl CloudLocals {
    pub fn new(
        options: CloudLocalOptions,
        service: Arc<QuarryService>,
        root: PathBuf,
    ) -> Result<Self> {
        options
            .validate()
            .map_err(|message| failure(400, "invalid", &message))?;
        if !root.is_absolute() {
            return Err(failure(
                400,
                "invalid",
                "Cloud Local state must be absolute",
            ));
        }
        private_dir(&root)?;
        Ok(Self {
            options: Arc::new(options),
            service,
            root,
            tunnels: Arc::new(Mutex::new(HashMap::new())),
        })
    }
    fn user_dir(&self, owner: &str) -> PathBuf {
        self.root.join("users").join(&hash(owner)[..32])
    }
    fn workspace(&self, owner: &str) -> PathBuf {
        self.user_dir(owner).join("workspace")
    }
    fn catalog(&self, owner: &str) -> Result<WorkspaceCatalog> {
        let dir = self.user_dir(owner);
        private_dir(&dir)?;
        private_dir(&self.workspace(owner))?;
        Ok(WorkspaceCatalog::load(
            RuntimeConfig::load(dir.join("config.toml"))?,
            &[self.workspace(owner)],
        )?)
    }
    fn namespace(owner: &str, name: &str) -> String {
        format!("qcl-{}", &hash(&format!("{owner}:{name}"))[..20])
    }
    fn manager(&self, owner: &str, namespace: &str) -> Result<LocalManager> {
        let base = self.user_dir(owner).join("locals").join(namespace);
        private_dir(&base)?;
        let mut manager = self
            .service
            .local()?
            .with_namespace(namespace)?
            .with_endpoint(self.options.endpoint())?
            .with_limits(self.options.limits(BTreeMap::from([
                ("quarry.cloud-local".into(), "1".into()),
                ("quarry.owner".into(), hash(owner)[..24].to_owned()),
            ])))?;
        manager.config = RuntimeConfig::load(base.join("config.toml"))?;
        manager.state_dir = base.join("state");
        Ok(manager)
    }
    fn resource_id(owner: &str, key: &str) -> String {
        format!("cloud:{}", &hash(&format!("{owner}:{key}"))[..24])
    }
    fn owned(&self, owner: &str) -> Result<Vec<(String, ConnectionRecord)>> {
        let mut out = Vec::new();
        for c in self.catalog(owner)?.connections()? {
            if c.extra.get("cloud_owner").and_then(toml_string) == Some(owner)
                && c.extra
                    .get("cloud_namespace")
                    .and_then(toml_string)
                    .is_some()
                && c.env == "local"
            {
                out.push((Self::resource_id(owner, &c.key), c));
            }
        }
        Ok(out)
    }
    /// Resolve one Local owned by `owner`, failing closed for anyone else.
    pub fn resolve(&self, owner: &str, id: &str) -> Result<OwnedLocal> {
        let (id, record) = self
            .owned(owner)?
            .into_iter()
            .find(|(rid, _)| rid == id)
            .ok_or_else(|| failure(404, "not_found", "Cloud Local is unavailable"))?;
        let namespace = record
            .extra
            .get("cloud_namespace")
            .and_then(toml_string)
            .ok_or_else(|| failure(403, "forbidden", "Cloud Local has no owner namespace"))?
            .to_owned();
        let manager = self.manager(owner, &namespace)?;
        let host = url::Url::parse(&record.url)
            .ok()
            .and_then(|u| u.host_str().map(String::from))
            .unwrap_or_default();
        if !manager.is_managed_host(&host) {
            return Err(failure(
                403,
                "forbidden",
                "Cloud Local address is not managed",
            ));
        }
        Ok(OwnedLocal {
            id,
            record,
            manager,
        })
    }
    pub fn resources(&self, owner: &str) -> Result<Vec<Value>> {
        Ok(self
            .owned(owner)?
            .into_iter()
            .map(|(id, c)| {
                let get = |k: &str| c.extra.get(k).and_then(toml_string).map(String::from);
                json!({
                    "id": id, "resourceId": id,
                    "project": get("platform_project").unwrap_or_else(|| "cloud-local".into()),
                    "db": get("platform_db").unwrap_or_else(|| c.logical_db().into()),
                    "env": "local", "placement": "cloud",
                    "engine": c.spec().map(|s| s.engine.as_str()).unwrap_or("unknown"),
                    "production": false,
                    "image": get("local_image"),
                    "sourceResourceId": get("platform_resource_id"),
                })
            })
            .collect())
    }
    pub async fn status(&self, owner: &str) -> Result<Value> {
        let mut items = Vec::new();
        for (id, _) in self.owned(owner)? {
            let local = self.resolve(owner, &id)?;
            let engine = local.record.spec()?.engine;
            let mut status = local.manager.status(Some(engine.as_str())).await?;
            // Internal addresses stay on the server.
            if let Some(object) = status.as_object_mut() {
                object.remove("published_bindings");
                object.remove("port");
                object.remove("configured_port");
            }
            status["resourceId"] = json!(id);
            status["placement"] = json!("cloud");
            items.push(status);
        }
        Ok(json!({"items":items}))
    }
    /// Create (or restart) a Local. `source` is an already-authorized hosted
    /// resource and its connection, used only for labels and engine.
    pub async fn create(
        &self,
        p: &Principal,
        b: &Value,
        source: Option<(&str, &str, &str, &ConnectionRecord)>,
    ) -> Result<Value> {
        let owner = p.id.as_str();
        let engine = b["engine"]
            .as_str()
            .or_else(|| source.and_then(|(_, _, _, c)| c.engine.as_deref()))
            .ok_or_else(|| failure(400, "invalid", "Engine required"))?;
        let engine = match engine {
            "postgres" | "postgresql" => "postgres",
            "redis" => "redis",
            _ => {
                return Err(failure(
                    400,
                    "invalid",
                    "Cloud Local supports PostgreSQL and Redis",
                ));
            }
        };
        if let Some((_, _, _, c)) = source
            && c.spec()?.engine.as_str() != engine
        {
            return Err(failure(
                400,
                "invalid",
                "Engine must match the source database",
            ));
        }
        let name = b["name"]
            .as_str()
            .map(String::from)
            .or_else(|| source.map(|(_, _, db, _)| db.to_owned()))
            .ok_or_else(|| failure(400, "invalid", "Name required"))?;
        quarry_runtime::validate_name(&name)
            .map_err(|_| failure(400, "invalid", "Name may contain letters, digits, _ and -"))?;
        if name.len() > 40 {
            return Err(failure(400, "invalid", "Name is too long"));
        }
        let image = b["image"].as_str().filter(|s| !s.is_empty());
        if let Some(image) = image
            && !self.options.image_allowed(image)
        {
            return Err(failure(
                400,
                "invalid",
                "This image is not allowed for cloud Local",
            ));
        }
        let user = self.user_dir(owner);
        private_dir(&user)?;
        let _lock = lock(user.join("lifecycle.lock")).await?;
        let namespace = Self::namespace(owner, &name);
        if let Some((id, existing)) = self
            .owned(owner)?
            .into_iter()
            .find(|(_, c)| c.extra.get("cloud_namespace").and_then(toml_string) == Some(&namespace))
        {
            if existing.spec()?.engine.as_str() != engine {
                return Err(failure(
                    409,
                    "conflict",
                    "A cloud Local with this name uses another engine",
                ));
            }
            let local = self.resolve(owner, &id)?;
            local.manager.up(engine, None, None).await?;
            return Ok(json!({"created":false,"started":true,"resources":self.resources(owner)?}));
        }
        if self.owned(owner)?.len() >= self.options.max_per_user {
            return Err(failure(
                429,
                "quota",
                "Cloud Local limit reached; delete one before creating another",
            ));
        }
        let manager = self.manager(owner, &namespace)?;
        let port = self.allocate_port(&namespace).await?;
        let result = async {
            manager.up(engine, image, Some(port)).await?;
            let catalog = self.catalog(owner)?;
            let workspace = self.workspace(owner);
            let mut c = manager
                .register_new(&catalog, &workspace, &name, engine)
                .await?;
            c.extra.insert("cloud_owner".into(), owner.into());
            c.extra
                .insert("cloud_namespace".into(), namespace.clone().into());
            c.extra.insert("platform_db".into(), name.clone().into());
            let project = b["project"]
                .as_str()
                .map(String::from)
                .or_else(|| source.map(|(_, project, _, _)| project.to_owned()))
                .unwrap_or_else(|| "cloud-local".into());
            c.extra.insert("platform_project".into(), project.into());
            if let Some((id, _, _, _)) = source {
                c.extra.insert("platform_resource_id".into(), id.into());
            }
            catalog.put_connection(&workspace, &c, true)?;
            Ok::<_, quarry_service::Failure>(())
        }
        .await;
        if let Err(error) = result {
            let _ = manager.down(engine, true).await;
            self.release_port(&namespace).await?;
            return Err(error);
        }
        self.service.cache.clear();
        Ok(json!({"created":true,"resources":self.resources(owner)?}))
    }
    pub async fn stop(&self, p: &Principal, b: &Value) -> Result<Value> {
        let id = b["resourceId"]
            .as_str()
            .ok_or_else(|| failure(400, "invalid", "resourceId required"))?;
        let purge = b["purge"].as_bool().unwrap_or(false);
        let local = self.resolve(&p.id, id)?;
        let _lock = lock(self.user_dir(&p.id).join("lifecycle.lock")).await?;
        let engine = local.record.spec()?.engine;
        let mut value = local.manager.down(engine.as_str(), purge).await?;
        if purge {
            let namespace = local
                .record
                .extra
                .get("cloud_namespace")
                .and_then(toml_string)
                .unwrap_or_default()
                .to_owned();
            self.catalog(&p.id)?
                .remove_connection(&self.workspace(&p.id), &local.record.key)?;
            self.release_port(&namespace).await?;
            self.revoke_tickets(&p.id, id).await?;
            let _ = std::fs::remove_dir_all(self.user_dir(&p.id).join("locals").join(&namespace));
            self.service.cache.clear();
        }
        value["resourceId"] = json!(id);
        Ok(value)
    }
    /// Import a PostgreSQL schema exported in-process from an authorized
    /// hosted source. No business rows are copied.
    pub async fn import_schema(
        &self,
        p: &Principal,
        id: &str,
        source: &ConnectionRecord,
    ) -> Result<Value> {
        let local = self.resolve(&p.id, id)?;
        let schema = self
            .service
            .local()?
            .export_schema(source, &self.service.tunnels)
            .await?;
        let value = local.manager.import_schema(&local.record, &schema).await?;
        self.service.cache.clear();
        Ok(value)
    }
    /// Issue a tunnel ticket for one Local. Only its hash is stored.
    pub async fn ticket(&self, p: &Principal, id: &str) -> Result<Value> {
        let local = self.resolve(&p.id, id)?;
        let ticket = format!("qt_{}", uuid::Uuid::new_v4().simple());
        let expires = now() + self.options.ticket_hours * 3600;
        self.edit_tickets(|t| {
            t.tickets.retain(|_, (_, _, exp)| *exp > now());
            t.tickets
                .insert(hash(&ticket), (p.id.clone(), local.id.clone(), expires));
            Ok(())
        })
        .await?;
        let engine = local.record.spec()?.engine;
        Ok(json!({
            "ticket": ticket, "expiresAt": expires, "resourceId": local.id,
            "engine": engine.as_str(),
            "database": url::Url::parse(&local.record.url).ok().map(|u| u.path().trim_start_matches('/').to_owned()),
            "user": if engine == quarry_core::Engine::Postgres { Some("quarry") } else { None },
            "password": if engine == quarry_core::Engine::Postgres { Some("quarry") } else { None },
        }))
    }
    /// Validate a tunnel ticket and reserve a tunnel slot for its owner.
    pub async fn open_tunnel(&self, ticket: &str) -> Result<(OwnedLocal, TunnelSlot)> {
        if !ticket.starts_with("qt_") || ticket.len() > 100 {
            return Err(failure(401, "unauthorized", "Invalid tunnel ticket"));
        }
        let entry = self
            .read_tickets()
            .await?
            .tickets
            .get(&hash(ticket))
            .cloned()
            .filter(|(_, _, exp)| *exp > now())
            .ok_or_else(|| failure(401, "unauthorized", "Tunnel ticket expired or revoked"))?;
        let local = self.resolve(&entry.0, &entry.1)?;
        let mut map = self
            .tunnels
            .lock()
            .map_err(|_| failure(503, "unavailable", "Tunnel registry unavailable"))?;
        let count = map.entry(entry.0.clone()).or_default();
        if *count >= MAX_TUNNELS_PER_USER {
            return Err(failure(429, "busy", "Too many open tunnel connections"));
        }
        *count += 1;
        Ok((
            local,
            TunnelSlot {
                tunnels: self.tunnels.clone(),
                owner: entry.0,
            },
        ))
    }
    async fn revoke_tickets(&self, owner: &str, id: &str) -> Result<()> {
        self.edit_tickets(|t| {
            t.tickets.retain(|_, (o, r, _)| !(o == owner && r == id));
            Ok(())
        })
        .await
    }
    async fn read_tickets(&self) -> Result<Tickets> {
        let path = self.root.join("tickets.json");
        let _lock = lock(self.root.join("tickets.lock")).await?;
        read_json(&path)
    }
    async fn edit_tickets(&self, f: impl FnOnce(&mut Tickets) -> Result<()>) -> Result<()> {
        let path = self.root.join("tickets.json");
        let _lock = lock(self.root.join("tickets.lock")).await?;
        let mut tickets: Tickets = read_json(&path)?;
        f(&mut tickets)?;
        write_json(&path, &tickets)
    }
    async fn allocate_port(&self, namespace: &str) -> Result<u16> {
        let path = self.root.join("ports.json");
        let _lock = lock(self.root.join("ports.lock")).await?;
        let mut ports: Ports = read_json(&path)?;
        if let Some((port, _)) = ports.ports.iter().find(|(_, ns)| *ns == namespace) {
            return Ok(*port);
        }
        let [low, high] = self.options.ports;
        for port in low..=high {
            // The registry plus Docker's own allocation check are
            // authoritative; TCP probes through port proxies are not.
            if ports.ports.contains_key(&port) {
                continue;
            }
            ports.ports.insert(port, namespace.into());
            write_json(&path, &ports)?;
            return Ok(port);
        }
        Err(failure(
            503,
            "unavailable",
            "No cloud Local ports are available",
        ))
    }
    async fn release_port(&self, namespace: &str) -> Result<()> {
        let path = self.root.join("ports.json");
        let _lock = lock(self.root.join("ports.lock")).await?;
        let mut ports: Ports = read_json(&path)?;
        ports.ports.retain(|_, ns| ns != namespace);
        write_json(&path, &ports)
    }
}

fn private_dir(path: &Path) -> Result<()> {
    std::fs::create_dir_all(path)?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o700))?;
    }
    Ok(())
}
fn read_json<T: for<'de> Deserialize<'de> + Default>(path: &Path) -> Result<T> {
    match std::fs::read(path) {
        Ok(bytes) => serde_json::from_slice(&bytes)
            .map_err(|_| failure(503, "unavailable", "Cloud Local state is corrupt")),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(T::default()),
        Err(e) => Err(e.into()),
    }
}
fn write_json<T: Serialize>(path: &Path, value: &T) -> Result<()> {
    let bytes = serde_json::to_vec(value)
        .map_err(|_| failure(500, "internal", "Cannot encode cloud Local state"))?;
    quarry_runtime::atomic_write(path, &bytes)?;
    Ok(())
}
async fn lock(path: PathBuf) -> Result<std::fs::File> {
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
    .map_err(|_| failure(503, "unavailable", "Cannot lock cloud Local state"))?
}

#[cfg(test)]
mod tests {
    use super::*;
    fn options() -> CloudLocalOptions {
        serde_json::from_value(json!({
            "docker_host": "tcp://dind:2376",
            "connect_host": "dind",
        }))
        .unwrap()
    }
    fn locals(dir: &Path) -> CloudLocals {
        let service = Arc::new(QuarryService::new(
            dir.join("config.toml"),
            vec![],
            dir.join("runtime"),
        ));
        CloudLocals::new(options(), service, dir.join("cloud")).unwrap()
    }
    fn seed(locals: &CloudLocals, owner: &str, host: &str) -> String {
        let ws = locals.workspace(owner);
        locals.catalog(owner).unwrap();
        std::fs::write(
            ws.join("connections.toml"),
            format!(
                "[orders_local]\nurl='postgresql://quarry:quarry@{host}:20000/orders'\ndb='orders'\nenv='local'\nengine='postgres'\ncloud_owner='{owner}'\ncloud_namespace='{}'\n",
                CloudLocals::namespace(owner, "orders")
            ),
        )
        .unwrap();
        CloudLocals::resource_id(owner, "orders_local")
    }
    #[test]
    fn locals_resolve_only_for_their_owner_and_managed_host() {
        let dir = tempfile::tempdir().unwrap();
        let locals = locals(dir.path());
        let id = seed(&locals, "alice", "dind");
        assert_eq!(
            locals.resolve("alice", &id).unwrap().address().unwrap(),
            ("dind".into(), 20000)
        );
        assert_eq!(locals.resources("alice").unwrap()[0]["placement"], "cloud");
        assert_eq!(locals.resolve("bob", &id).err().unwrap().status, 404);
        assert!(locals.resources("bob").unwrap().is_empty());
        // A record pointing anywhere but the managed daemon is never reachable.
        let id = seed(&locals, "mallory", "10.0.0.5");
        assert_eq!(locals.resolve("mallory", &id).err().unwrap().status, 403);
    }
    #[tokio::test]
    async fn tickets_bind_owner_and_resource_and_are_revocable() {
        let dir = tempfile::tempdir().unwrap();
        let locals = locals(dir.path());
        let id = seed(&locals, "alice", "dind");
        let alice = Principal {
            id: "alice".into(),
            permissions: vec![],
            roles: vec![],
        };
        let bob = Principal {
            id: "bob".into(),
            permissions: vec![],
            roles: vec![],
        };
        assert_eq!(locals.ticket(&bob, &id).await.unwrap_err().status, 404);
        let ticket = locals.ticket(&alice, &id).await.unwrap()["ticket"]
            .as_str()
            .unwrap()
            .to_owned();
        let (local, _slot) = locals.open_tunnel(&ticket).await.unwrap();
        assert_eq!(local.id, id);
        assert!(
            !std::fs::read_to_string(dir.path().join("cloud/tickets.json"))
                .unwrap()
                .contains(&ticket)
        );
        locals.revoke_tickets("alice", &id).await.unwrap();
        assert_eq!(locals.open_tunnel(&ticket).await.err().unwrap().status, 401);
        assert_eq!(
            locals.open_tunnel("qt_unknown").await.err().unwrap().status,
            401
        );
    }
    #[test]
    fn images_are_limited_to_database_prefixes() {
        let o = options();
        assert!(o.image_allowed("postgres:15-alpine"));
        assert!(o.image_allowed("postgis/postgis:16-3.4"));
        assert!(o.image_allowed("redis:7"));
        assert!(!o.image_allowed("alpine:3"));
        assert!(!o.image_allowed("postgres:15 --privileged"));
        assert!(!o.image_allowed("-postgres"));
        let mut bad = options();
        bad.ports = [80, 90];
        assert!(bad.validate().is_err());
    }
}
