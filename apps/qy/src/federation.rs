use crate::http::CloudBackend;
use futures_util::{StreamExt, stream::FuturesUnordered};
use quarry_core::Engine;
use quarry_runtime::{ConnectionRecord, absolute};
use quarry_service::{Failure, Result};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    collections::{BTreeMap, BTreeSet},
    fs::{self, File, OpenOptions},
    io::Read,
    path::{Path, PathBuf},
    process::Stdio,
    time::Duration,
};
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    process::Command,
    time::timeout,
};
use url::Url;

const VERSION: u32 = 1;
const MAX_DOCUMENT: u64 = 16 * 1024 * 1024;
const MAX_REQUEST: usize = 512 * 1024;
const MAX_PROVIDERS: usize = 16;
const MAX_RESOURCES: usize = 4000;

fn failure(status: u16, code: &str, message: &str) -> Failure {
    Failure {
        status,
        code: code.into(),
        message: message.into(),
    }
}

fn unavailable() -> Failure {
    failure(
        503,
        "provider_unavailable",
        "The database provider is unavailable",
    )
}

fn missing_target() -> Failure {
    failure(404, "not_found", "The database target is unavailable")
}

fn invalid_registry() -> Failure {
    Failure::invalid("The Quarry provider registry is invalid or is not private")
}

fn home() -> Result<PathBuf> {
    std::env::var_os("HOME")
        .map(PathBuf::from)
        .filter(|path| path.is_absolute())
        .ok_or_else(|| Failure::invalid("HOME is not configured"))
}

pub fn default_config_path() -> Result<PathBuf> {
    Ok(home()?.join(".config/quarry/config.toml"))
}

fn hash(parts: impl Serialize) -> String {
    format!(
        "{:x}",
        Sha256::digest(serde_json::to_vec(&parts).expect("serialize hash input"))
    )
}

fn valid_text(value: &str, max: usize) -> bool {
    !value.is_empty() && value.len() <= max && !value.chars().any(char::is_control)
}

fn valid_resource_id(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 200
        && value
            .bytes()
            .all(|ch| ch.is_ascii_alphanumeric() || b"._:-".contains(&ch))
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ProviderIdentity {
    pub id: String,
    pub label: String,
    pub origin: String,
    pub profile: String,
    pub owner: String,
}

impl ProviderIdentity {
    pub fn validate(&self) -> Result<()> {
        let origin =
            Url::parse(&self.origin).map_err(|_| Failure::invalid("Invalid provider origin"))?;
        let loopback = matches!(origin.host_str(), Some("127.0.0.1" | "[::1]"));
        if !valid_resource_id(&self.id)
            || !valid_text(&self.label, 128)
            || origin.as_str() != self.origin
            || origin.host_str().is_none()
            || !(origin.scheme() == "https" || origin.scheme() == "http" && loopback)
            || !origin.username().is_empty()
            || origin.password().is_some()
            || origin.path() != "/"
            || origin.query().is_some()
            || origin.fragment().is_some()
            || !valid_text(&self.origin, 2048)
            || !valid_text(&self.profile, 64)
            || !self
                .profile
                .bytes()
                .all(|ch| ch.is_ascii_alphanumeric() || b"-_".contains(&ch))
            || !valid_text(&self.owner, 256)
        {
            return Err(Failure::invalid("Invalid database provider identity"));
        }
        Ok(())
    }

    pub fn scope_id(&self) -> String {
        hash((&self.id, &self.origin, &self.profile, &self.owner))
    }

    pub fn route_id(&self, upstream_id: &str) -> String {
        format!(
            "f:{}",
            hash((
                &self.id,
                &self.origin,
                &self.profile,
                &self.owner,
                upstream_id
            ))
        )
    }

    fn slot(&self) -> String {
        hash((&self.id, &self.origin, &self.profile))
    }
}

/// Locally trusted command, separate from provider requests and catalog data.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ProviderHelper {
    pub executable: PathBuf,
    pub args: Vec<String>,
}

impl ProviderHelper {
    fn validate(&self) -> Result<()> {
        if !self.executable.is_absolute()
            || self.executable.file_name().is_none()
            || !self
                .executable
                .to_str()
                .is_some_and(|path| valid_text(path, 4096))
            || self.args.len() > 16
            || self
                .args
                .iter()
                .any(|arg| !arg.is_empty() && !valid_text(arg, 4096))
        {
            return Err(Failure::invalid("Invalid database provider helper"));
        }
        Ok(())
    }
}

/// The helper owns credentials. The request carries only an exact account
/// scope and a bounded, fixed database operation.
#[derive(Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ProviderRequest {
    pub version: u32,
    pub provider: ProviderIdentity,
    pub method: String,
    pub path: String,
    pub pairs: Vec<(String, String)>,
    pub body: Option<Value>,
}

impl ProviderRequest {
    pub fn validate(&self) -> Result<()> {
        self.provider.validate()?;
        if self.version != VERSION {
            return Err(Failure::invalid("Unsupported database provider protocol"));
        }
        let expected: &[&str] = match (self.method.as_str(), self.path.as_str()) {
            ("GET", "/identity" | "/api/platform") if self.body.is_none() => &[],
            ("GET", "/api/tables" | "/api/health" | "/api/schema/export")
                if self.body.is_none() =>
            {
                &["resourceId"]
            }
            ("GET", "/api/columns") if self.body.is_none() => &["resourceId", "table"],
            ("GET", "/api/inspect") if self.body.is_none() => &["resourceId", "key"],
            ("POST", "/api/query" | "/api/cancel" | "/api/record-link" | "/api/explain")
                if self.body.as_ref().and_then(Value::as_object).is_some() =>
            {
                &[]
            }
            _ => return Err(Failure::invalid("Unsupported database provider operation")),
        };
        if self.pairs.len() != expected.len()
            || expected.iter().any(|key| {
                self.pairs
                    .iter()
                    .filter(|(name, value)| {
                        name == key
                            && if *key == "resourceId" {
                                valid_resource_id(value)
                            } else {
                                valid_text(value, 4096)
                            }
                    })
                    .count()
                    != 1
            })
            || self
                .body
                .as_ref()
                .is_some_and(|body| !body["resourceId"].as_str().is_some_and(valid_resource_id))
        {
            return Err(Failure::invalid("Invalid database provider arguments"));
        }
        Ok(())
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Status {
    Connected,
    Unavailable,
    SignedOut,
    Forbidden,
}

impl Status {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Connected => "connected",
            Self::Unavailable => "unavailable",
            Self::SignedOut => "signed_out",
            Self::Forbidden => "forbidden",
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct LocalTarget {
    pub id: String,
    pub workspace: PathBuf,
    pub key: String,
}

impl LocalTarget {
    fn validate(&self) -> Result<()> {
        quarry_runtime::validate_name(&self.key)?;
        let id = self
            .id
            .strip_prefix("local:")
            .and_then(|id| id.split_once(':'));
        if !self.workspace.is_absolute()
            || !id.is_some_and(|(digest, key)| {
                key == self.key
                    && digest.len() == 64
                    && digest
                        .bytes()
                        .all(|ch| ch.is_ascii_digit() || b"abcdef".contains(&ch))
            })
        {
            return Err(Failure::invalid("Invalid local database target"));
        }
        Ok(())
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Resource {
    pub id: String,
    pub upstream_id: String,
    pub provider: ProviderIdentity,
    pub project: String,
    pub db: String,
    pub env: Option<String>,
    pub engine: Engine,
    pub production: bool,
    pub status: Status,
    pub association: Option<LocalTarget>,
}

#[derive(Debug, Serialize, Deserialize)]
pub struct ProviderStatus {
    pub provider: ProviderIdentity,
    pub status: Status,
    pub identity_verified: bool,
}

#[derive(Debug, Serialize, Deserialize)]
pub struct Catalog {
    pub resources: Vec<Resource>,
    pub status: Status,
    pub providers: Vec<ProviderStatus>,
}

#[derive(Clone, Debug, Serialize)]
pub struct AssociationCandidate {
    pub resource_id: String,
    pub local: LocalTarget,
    pub evidence: AssociationEvidence,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum AssociationEvidence {
    ManagedCopy,
    MatchingLabels,
}

fn association_evidence(
    local: &ConnectionRecord,
    resource: &Resource,
) -> Option<AssociationEvidence> {
    if !local
        .spec()
        .is_ok_and(|spec| spec.engine == resource.engine)
    {
        return None;
    }
    let extra = |key| local.extra.get(key).and_then(toml::Value::as_str);
    let origin = extra("platform_origin").and_then(|value| Url::parse(value).ok());
    if origin
        .as_ref()
        .is_some_and(|origin| origin.as_str() == resource.provider.origin)
        && extra("platform_user_id") == Some(resource.provider.owner.as_str())
        && extra("platform_resource_id") == Some(resource.upstream_id.as_str())
    {
        Some(AssociationEvidence::ManagedCopy)
    } else if local.logical_db() == resource.db
        && local
            .group
            .as_ref()
            .is_some_and(|group| group.eq_ignore_ascii_case(&resource.project))
    {
        Some(AssociationEvidence::MatchingLabels)
    } else {
        None
    }
}

pub fn association_candidates(
    catalog: &Catalog,
    locals: &[ConnectionRecord],
) -> Vec<AssociationCandidate> {
    catalog
        .resources
        .iter()
        .filter(|resource| resource.association.is_none() && resource.status == Status::Connected)
        .flat_map(|resource| {
            locals.iter().filter_map(|local| {
                let evidence = association_evidence(local, resource)?;
                Some(AssociationCandidate {
                    resource_id: resource.id.clone(),
                    local: LocalTarget {
                        id: crate::federated_cli::local_target_id(local),
                        workspace: local.workspace.clone(),
                        key: local.key.clone(),
                    },
                    evidence,
                })
            })
        })
        .collect()
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
struct UpstreamResource {
    id: String,
    project: String,
    db: String,
    env: Option<String>,
    engine: Engine,
    production: bool,
}

impl UpstreamResource {
    fn is_valid(&self) -> bool {
        valid_resource_id(&self.id)
            && valid_text(&self.project, 256)
            && valid_text(&self.db, 256)
            && self.env.as_deref().is_none_or(|env| valid_text(env, 256))
    }

    fn from_catalog(catalog: &Value, provider: &ProviderIdentity) -> Result<Vec<Self>> {
        if catalog["principal"]["id"].as_str() != Some(&provider.owner) {
            return Err(failure(
                403,
                "provider_account_mismatch",
                "The database catalog belongs to a different account",
            ));
        }
        let resources = catalog["resources"]
            .as_array()
            .ok_or_else(|| Failure::invalid("Invalid database provider catalog"))?;
        if resources.len() > MAX_RESOURCES {
            return Err(Failure::invalid("Database provider catalog is too large"));
        }
        let resources: Vec<Self> = serde_json::from_value(catalog["resources"].clone())?;
        let mut ids = BTreeSet::new();
        if !resources
            .iter()
            .all(|resource| resource.is_valid() && ids.insert(&resource.id))
        {
            return Err(Failure::invalid("Invalid database provider catalog"));
        }
        Ok(resources)
    }
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Registered {
    provider: ProviderIdentity,
    helper: ProviderHelper,
    resources: Vec<UpstreamResource>,
}

#[derive(Debug, Default, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct State {
    providers: BTreeMap<String, Registered>,
    associations: BTreeMap<String, LocalTarget>,
}

#[derive(Clone, Debug)]
pub struct Registry {
    root: PathBuf,
    scope: String,
}

fn private_file(metadata: &fs::Metadata) -> bool {
    metadata.is_file() && private_mode(metadata)
}

fn private_mode(metadata: &fs::Metadata) -> bool {
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        // SAFETY: geteuid takes no arguments and does not access caller memory.
        metadata.uid() == unsafe { libc::geteuid() } && metadata.mode() & 0o077 == 0
    }
    #[cfg(not(unix))]
    {
        let _ = metadata;
        true
    }
}

impl Registry {
    pub fn new(config_path: &Path) -> Result<Self> {
        let config_path = absolute(config_path, &std::env::current_dir()?)?;
        Ok(Self {
            root: home()?.join(".config/quarry/federation"),
            scope: format!(
                "{:x}",
                Sha256::digest(config_path.as_os_str().as_encoded_bytes())
            ),
        })
    }

    fn document_path(&self) -> PathBuf {
        self.root.join(format!("{}.v2.json", self.scope))
    }

    fn check_root(&self, create: bool) -> Result<bool> {
        if create {
            let parent = self.root.parent().ok_or_else(invalid_registry)?;
            fs::create_dir_all(parent)?;
            let mut builder = fs::DirBuilder::new();
            #[cfg(unix)]
            {
                use std::os::unix::fs::DirBuilderExt;
                builder.mode(0o700);
            }
            match builder.create(&self.root) {
                Ok(()) => {}
                Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => {}
                Err(error) => return Err(error.into()),
            }
        }
        match self.root.symlink_metadata() {
            Ok(metadata) if metadata.is_dir() && private_mode(&metadata) => Ok(true),
            Ok(_) => Err(invalid_registry()),
            Err(error) if !create && error.kind() == std::io::ErrorKind::NotFound => Ok(false),
            Err(error) => Err(error.into()),
        }
    }

    fn load(&self) -> Result<State> {
        if !self.check_root(false)? {
            return Ok(State::default());
        }
        let path = self.document_path();
        let mut options = OpenOptions::new();
        options.read(true);
        #[cfg(unix)]
        {
            use std::os::unix::fs::OpenOptionsExt;
            options.custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK);
        }
        let file = match options.open(path) {
            Ok(file) => file,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
                return Ok(State::default());
            }
            Err(_) => return Err(invalid_registry()),
        };
        if !private_file(&file.metadata()?) {
            return Err(invalid_registry());
        }
        let mut bytes = Vec::new();
        file.take(MAX_DOCUMENT + 1).read_to_end(&mut bytes)?;
        if bytes.len() as u64 > MAX_DOCUMENT {
            return Err(invalid_registry());
        }
        let state: State = serde_json::from_slice(&bytes).map_err(|_| invalid_registry())?;
        if state.providers.len() > MAX_PROVIDERS
            || state
                .associations
                .values()
                .any(|target| target.validate().is_err())
            || state.providers.iter().any(|(slot, entry)| {
                entry.provider.validate().is_err()
                    || entry.helper.validate().is_err()
                    || &entry.provider.slot() != slot
                    || entry.resources.len() > MAX_RESOURCES
                    || entry.resources.iter().any(|resource| !resource.is_valid())
            })
        {
            return Err(invalid_registry());
        }
        Ok(state)
    }

    fn update<T>(&self, change: impl FnOnce(&mut State) -> Result<(T, bool)>) -> Result<T> {
        self.check_root(true)?;
        let lock_path = self.root.join(format!("{}.v2.lock", self.scope));
        let mut options = OpenOptions::new();
        options.create(true).read(true).write(true);
        #[cfg(unix)]
        {
            use std::os::unix::fs::OpenOptionsExt;
            options
                .mode(0o600)
                .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK);
        }
        let lock: File = options.open(lock_path).map_err(|_| invalid_registry())?;
        if !private_file(&lock.metadata()?) {
            return Err(invalid_registry());
        }
        fs2::FileExt::lock_exclusive(&lock)?;
        let mut state = self.load()?;
        let (outcome, changed) = change(&mut state)?;
        if changed {
            let bytes = serde_json::to_vec(&state)?;
            if bytes.len() as u64 > MAX_DOCUMENT {
                return Err(Failure::invalid("Database provider registry is too large"));
            }
            quarry_runtime::atomic_write(&self.document_path(), &bytes)?;
        }
        Ok(outcome)
    }

    fn register(
        &self,
        provider: &ProviderIdentity,
        helper: &ProviderHelper,
        catalog: &Value,
    ) -> Result<usize> {
        provider.validate()?;
        helper.validate()?;
        let resources = UpstreamResource::from_catalog(catalog, provider)?;
        let count = resources.len();
        self.update(|state| {
            let slot = provider.slot();
            if state.providers.len() >= MAX_PROVIDERS && !state.providers.contains_key(&slot) {
                return Err(Failure::invalid("Database provider registry is full"));
            }
            let entry = Registered {
                provider: provider.clone(),
                helper: helper.clone(),
                resources,
            };
            let changed = state.providers.get(&slot) != Some(&entry);
            if changed {
                state.providers.insert(slot, entry);
            }
            Ok((count, changed))
        })
    }

    pub fn has_providers(&self) -> Result<bool> {
        Ok(!self.load()?.providers.is_empty())
    }

    pub fn contains_registration(
        &self,
        provider: &ProviderIdentity,
        helper: &ProviderHelper,
    ) -> Result<bool> {
        provider.validate()?;
        helper.validate()?;
        Ok(self.cached(provider, helper)?.is_some())
    }

    fn cached(
        &self,
        provider: &ProviderIdentity,
        helper: &ProviderHelper,
    ) -> Result<Option<Registered>> {
        Ok(self
            .load()?
            .providers
            .remove(&provider.slot())
            .filter(|entry| &entry.provider == provider && &entry.helper == helper))
    }

    pub fn associate(&self, resource_id: &str, target: LocalTarget) -> Result<()> {
        self.associate_many(&[(resource_id.into(), target)])
    }

    pub fn associate_many(&self, entries: &[(String, LocalTarget)]) -> Result<()> {
        self.update_associations(entries, false).map(drop)
    }

    pub(crate) fn associate_missing(
        &self,
        entries: &[(String, LocalTarget)],
    ) -> Result<Vec<String>> {
        self.update_associations(entries, true)
    }

    fn update_associations(
        &self,
        entries: &[(String, LocalTarget)],
        keep_existing: bool,
    ) -> Result<Vec<String>> {
        let cwd = std::env::current_dir()?;
        let mut links = BTreeMap::new();
        for (id, target) in entries {
            let target = LocalTarget {
                id: target.id.clone(),
                workspace: absolute(&target.workspace, &cwd)?,
                key: target.key.clone(),
            };
            target.validate()?;
            if links.insert(id.clone(), target).is_some() {
                return Err(Failure::invalid(
                    "A cloud target can only have one association",
                ));
            }
        }
        if links.is_empty() {
            return Ok(vec![]);
        }
        self.update(|state| {
            let registered: BTreeSet<_> = state
                .providers
                .values()
                .flat_map(|entry| {
                    entry
                        .resources
                        .iter()
                        .map(|resource| entry.provider.route_id(&resource.id))
                })
                .collect();
            if links.keys().any(|id| !registered.contains(id)) {
                return Err(missing_target());
            }
            if keep_existing {
                links.retain(|id, _| !state.associations.contains_key(id));
            }
            let applied: Vec<_> = links
                .iter()
                .filter(|(id, target)| state.associations.get(*id) != Some(*target))
                .map(|(id, _)| id.clone())
                .collect();
            let changed = !applied.is_empty();
            state.associations.extend(links);
            Ok((applied, changed))
        })
    }

    pub fn disassociate(&self, resource_id: &str) -> Result<bool> {
        self.update(|state| {
            let removed = state.associations.remove(resource_id).is_some();
            Ok((removed, removed))
        })
    }

    fn refresh(
        &self,
        previous: &Registered,
        resources: Vec<UpstreamResource>,
    ) -> Result<Option<Vec<UpstreamResource>>> {
        self.update(|state| {
            let entry = state.providers.get_mut(&previous.provider.slot());
            if let Some(entry) = entry.filter(|entry| {
                entry.provider == previous.provider && entry.helper == previous.helper
            }) {
                let changed = entry == previous && entry.resources != resources;
                if changed {
                    entry.resources = resources;
                }
                Ok((Some(entry.resources.clone()), changed))
            } else {
                Ok((None, false))
            }
        })
    }
}

#[derive(Clone)]
pub struct Federation {
    registry: Registry,
}

impl Federation {
    pub fn new(config_path: &Path) -> Result<Self> {
        Ok(Self {
            registry: Registry::new(config_path)?,
        })
    }

    pub fn registry(&self) -> &Registry {
        &self.registry
    }

    pub async fn register(
        &self,
        provider: &ProviderIdentity,
        helper: &ProviderHelper,
    ) -> Result<usize> {
        helper.validate()?;
        let identity = Self::probe(provider, helper, "/identity").await?;
        match identity.get("id") {
            Some(Value::String(id)) if id == &provider.owner => {}
            Some(Value::String(_)) => {
                return Err(failure(
                    403,
                    "provider_account_mismatch",
                    "The database provider uses a different account",
                ));
            }
            Some(Value::Null) => {
                return Err(failure(
                    401,
                    "sign_in_required",
                    "Sign in to the database provider",
                ));
            }
            _ => return Err(unavailable()),
        }
        let catalog = Self::probe(provider, helper, "/api/platform").await?;
        self.registry.register(provider, helper, &catalog)
    }

    async fn call(helper: &ProviderHelper, request: ProviderRequest) -> Result<Value> {
        request.validate()?;
        let input = serde_json::to_vec(&request)?;
        if input.len() > MAX_REQUEST {
            return Err(Failure::invalid("Database provider request is too large"));
        }
        let limit = match request.path.as_str() {
            "/identity" => Duration::from_secs(2),
            "/api/platform" => Duration::from_secs(3),
            _ => Duration::from_secs(65),
        };
        timeout(limit, async {
            let mut child = Command::new(&helper.executable)
                .args(&helper.args)
                .stdin(Stdio::piped())
                .stdout(Stdio::piped())
                .stderr(Stdio::null())
                .kill_on_drop(true)
                .spawn()
                .map_err(|_| unavailable())?;
            let mut stdin = child.stdin.take().ok_or_else(unavailable)?;
            let stdout = child.stdout.take().ok_or_else(unavailable)?;
            let send = async move {
                stdin.write_all(&input).await?;
                stdin.shutdown().await?;
                drop(stdin);
                Ok::<_, std::io::Error>(())
            };
            let receive = async {
                let mut bytes = Vec::new();
                stdout
                    .take(MAX_DOCUMENT + 1)
                    .read_to_end(&mut bytes)
                    .await?;
                Ok::<_, std::io::Error>(bytes)
            };
            let ((), bytes) = tokio::try_join!(send, receive).map_err(|_| unavailable())?;
            if bytes.len() as u64 > MAX_DOCUMENT || !child.wait().await.is_ok_and(|s| s.success()) {
                return Err(unavailable());
            }
            let output: Value = serde_json::from_slice(&bytes).map_err(|_| unavailable())?;
            match (output.get("value"), output.get("error")) {
                (Some(value), None) => Ok(value.clone()),
                (None, Some(error)) => Err(Failure {
                    status: error["status"]
                        .as_u64()
                        .and_then(|status| u16::try_from(status).ok())
                        .filter(|status| (400..600).contains(status))
                        .ok_or_else(unavailable)?,
                    code: error["code"]
                        .as_str()
                        .filter(|code| valid_text(code, 128))
                        .ok_or_else(unavailable)?
                        .into(),
                    message: error["message"]
                        .as_str()
                        .filter(|message| valid_text(message, 1024))
                        .ok_or_else(unavailable)?
                        .into(),
                }),
                _ => Err(unavailable()),
            }
        })
        .await
        .map_err(|_| unavailable())?
    }

    async fn probe(
        provider: &ProviderIdentity,
        helper: &ProviderHelper,
        path: &str,
    ) -> Result<Value> {
        Self::call(
            helper,
            ProviderRequest {
                version: VERSION,
                provider: provider.clone(),
                method: "GET".into(),
                path: path.into(),
                pairs: vec![],
                body: None,
            },
        )
        .await
    }

    async fn verify_identity(entry: &Registered) -> std::result::Result<(), Status> {
        match Self::probe(&entry.provider, &entry.helper, "/identity").await {
            Ok(value) if value["id"].as_str() == Some(&entry.provider.owner) => Ok(()),
            Ok(value)
                if value
                    .get("id")
                    .is_some_and(|id| id.is_null() || id.is_string()) =>
            {
                Err(Status::SignedOut)
            }
            Err(error) if error.status == 401 => Err(Status::SignedOut),
            Err(error) if error.status == 403 => Err(Status::Forbidden),
            _ => Err(Status::Unavailable),
        }
    }

    /// Cached IDs whose owning account still exists in the provider's local
    /// credentials. This never contacts the cloud or confers a resource grant.
    pub async fn authorized_cached_ids(&self) -> Result<Vec<String>> {
        let state = self.registry.load()?;
        let mut checks: FuturesUnordered<_> = state
            .providers
            .into_values()
            .map(|entry| async move {
                Self::verify_identity(&entry).await.ok()?;
                self.registry
                    .cached(&entry.provider, &entry.helper)
                    .ok()
                    .flatten()
            })
            .collect();
        let mut ids = Vec::new();
        while let Some(entry) = checks.next().await {
            if let Some(entry) = entry {
                ids.extend(
                    entry
                        .resources
                        .iter()
                        .map(|resource| entry.provider.route_id(&resource.id)),
                );
            }
        }
        ids.sort_unstable();
        Ok(ids)
    }

    async fn refresh_provider(&self, entry: Registered) -> (ProviderStatus, Vec<UpstreamResource>) {
        let (status, resources, identity_verified) = if let Err(status) =
            Self::verify_identity(&entry).await
        {
            (status, vec![], false)
        } else {
            let response = Self::probe(&entry.provider, &entry.helper, "/api/platform")
                .await
                .and_then(|document| UpstreamResource::from_catalog(&document, &entry.provider));
            match response {
                Ok(resources) => match self.registry.refresh(&entry, resources) {
                    Ok(Some(resources)) => (Status::Connected, resources, true),
                    Ok(None) => (Status::SignedOut, vec![], false),
                    Err(_) => (Status::Unavailable, vec![], true),
                },
                Err(error) if matches!(error.status, 401 | 403) => {
                    let _ = self.registry.refresh(&entry, vec![]);
                    let status = if error.status == 401
                        || matches!(
                            error.code.as_str(),
                            "provider_account_mismatch" | "cloud_account_mismatch"
                        ) {
                        Status::SignedOut
                    } else {
                        Status::Forbidden
                    };
                    (status, vec![], status == Status::Forbidden)
                }
                Err(_) => match Self::verify_identity(&entry).await {
                    Ok(()) => match self.registry.cached(&entry.provider, &entry.helper) {
                        Ok(Some(cached)) => (Status::Unavailable, cached.resources, true),
                        Ok(None) => (Status::SignedOut, vec![], false),
                        Err(_) => (Status::Unavailable, vec![], true),
                    },
                    Err(status) => (status, vec![], false),
                },
            }
        };
        (
            ProviderStatus {
                provider: entry.provider,
                status,
                identity_verified,
            },
            resources,
        )
    }

    pub async fn catalog(&self) -> Result<Catalog> {
        let state = self.registry.load()?;
        let mut pending: FuturesUnordered<_> = state
            .providers
            .into_values()
            .map(|entry| self.refresh_provider(entry))
            .collect();
        let mut catalog = Catalog {
            resources: vec![],
            status: Status::SignedOut,
            providers: vec![],
        };
        while let Some((status, resources)) = pending.next().await {
            for resource in resources {
                let id = status.provider.route_id(&resource.id);
                let association = state.associations.get(&id).cloned();
                catalog.resources.push(Resource {
                    id,
                    upstream_id: resource.id,
                    provider: status.provider.clone(),
                    project: resource.project,
                    db: resource.db,
                    env: resource.env,
                    engine: resource.engine,
                    production: resource.production,
                    status: status.status,
                    association,
                });
            }
            catalog.providers.push(status);
        }
        catalog.providers.sort_by_key(|entry| entry.provider.slot());
        catalog.resources.sort_by(|a, b| {
            (&a.project, &a.db, &a.env, &a.id).cmp(&(&b.project, &b.db, &b.env, &b.id))
        });
        for status in [Status::Unavailable, Status::Connected, Status::Forbidden] {
            if catalog.providers.iter().any(|entry| entry.status == status) {
                catalog.status = status;
                break;
            }
        }
        Ok(catalog)
    }
}

#[async_trait::async_trait]
impl CloudBackend for Federation {
    async fn authorized_resource_ids(&self) -> Result<Option<Vec<String>>> {
        self.authorized_cached_ids().await.map(Some)
    }

    async fn request(
        &self,
        method: &str,
        path: &str,
        pairs: &[(&str, String)],
        mut body: Option<Value>,
    ) -> Result<Value> {
        if method == "GET" && path == "/api/platform" && pairs.is_empty() && body.is_none() {
            return Ok(serde_json::to_value(self.catalog().await?)?);
        }
        let mut pairs: Vec<_> = pairs
            .iter()
            .map(|(name, value)| ((*name).to_owned(), value.clone()))
            .collect();
        let route = if method == "GET" {
            pairs
                .iter_mut()
                .find(|(name, _)| name == "resourceId")
                .map(|(_, route)| route)
        } else {
            None
        };
        let route_id = route
            .as_deref()
            .map(String::as_str)
            .or_else(|| body.as_ref().and_then(|value| value["resourceId"].as_str()))
            .ok_or_else(missing_target)?;
        let state = self.registry.load()?;
        let (provider, helper, upstream_id) = state
            .providers
            .into_values()
            .find_map(|entry| {
                entry
                    .resources
                    .into_iter()
                    .find(|resource| entry.provider.route_id(&resource.id) == route_id)
                    .map(|resource| (entry.provider, entry.helper, resource.id))
            })
            .ok_or_else(missing_target)?;
        if let Some(route) = route {
            *route = upstream_id;
        } else if let Some(body) = body.as_mut() {
            body["resourceId"] = json!(upstream_id);
        }
        Self::call(
            &helper,
            ProviderRequest {
                version: VERSION,
                provider,
                method: method.into(),
                path: path.into(),
                pairs,
                body,
            },
        )
        .await
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn provider(owner: &str) -> ProviderIdentity {
        ProviderIdentity {
            id: "fixture".into(),
            label: "Fixture Cloud".into(),
            origin: "https://lab.example/".into(),
            profile: "production".into(),
            owner: owner.into(),
        }
    }

    fn helper(executable: PathBuf, args: &[&str]) -> ProviderHelper {
        ProviderHelper {
            executable,
            args: args.iter().map(|arg| (*arg).to_owned()).collect(),
        }
    }

    fn catalog(owner: &str, id: &str) -> Value {
        json!({
            "principal": {"id": owner, "email": "not-for-cache@example.com"},
            "resources": [{
                "id": id, "project": "demo", "db": "notes", "env": "dev",
                "engine": "sqlite", "production": false, "unexpectedPrivateField": "secret",
            }],
        })
    }

    fn registry(root: &Path) -> Registry {
        Registry {
            root: root.join("private"),
            scope: "fixture".into(),
        }
    }

    #[test]
    fn registry_persists_redacted_resources_and_only_explicit_associations() {
        let root = tempfile::tempdir().unwrap();
        let registry = registry(root.path());
        let alice = provider("alice");
        let bob = provider("bob");
        let helper = helper(root.path().join("provider"), &["db", "provider"]);
        let id = alice.route_id("notes");
        assert_ne!(id, bob.route_id("notes"));
        let mut renamed = alice.clone();
        renamed.label = "Renamed Cloud".into();
        assert_eq!(renamed.route_id("notes"), id);
        assert!(!registry.has_providers().unwrap());
        assert!(
            registry
                .register(&alice, &helper, &catalog("bob", "notes"))
                .is_err()
        );
        registry
            .register(&alice, &helper, &catalog("alice", "notes"))
            .unwrap();
        assert!(registry.contains_registration(&alice, &helper).unwrap());
        assert!(!registry.contains_registration(&bob, &helper).unwrap());
        assert!(registry.load().unwrap().associations.is_empty());
        let text = fs::read_to_string(registry.document_path()).unwrap();
        assert!(!text.contains("not-for-cache"));
        assert!(!text.contains("unexpectedPrivateField"));
        let target = LocalTarget {
            id: format!("local:{}:local_notes", "a".repeat(64)),
            workspace: root.path().canonicalize().unwrap(),
            key: "local_notes".into(),
        };
        assert!(registry.associate("notes", target.clone()).is_err());
        assert!(
            registry
                .associate_many(&[
                    (id.clone(), target.clone()),
                    ("missing".into(), target.clone()),
                ])
                .is_err()
        );
        assert!(registry.load().unwrap().associations.is_empty());
        assert_eq!(
            registry
                .associate_missing(&[(id.clone(), target.clone())])
                .unwrap(),
            vec![id.clone()]
        );
        let alternative = LocalTarget {
            id: format!("local:{}:other", "b".repeat(64)),
            key: "other".into(),
            ..target.clone()
        };
        assert!(
            registry
                .associate_missing(&[(id.clone(), alternative)])
                .unwrap()
                .is_empty()
        );
        assert_eq!(registry.load().unwrap().associations[&id], target);
        registry
            .register(&bob, &helper, &catalog("bob", "notes"))
            .unwrap();
        assert!(!registry.contains_registration(&alice, &helper).unwrap());
        assert!(registry.contains_registration(&bob, &helper).unwrap());
        assert!(
            !registry
                .load()
                .unwrap()
                .associations
                .contains_key(&bob.route_id("notes"))
        );
        assert!(registry.disassociate(&id).unwrap());
        assert!(!registry.disassociate(&id).unwrap());
    }

    #[test]
    fn candidates_distinguish_provenance_from_label_similarity() {
        let alice = provider("alice");
        let cloud = Resource {
            id: alice.route_id("notes"),
            upstream_id: "notes".into(),
            provider: alice,
            project: "demo".into(),
            db: "notes".into(),
            env: Some("dev".into()),
            engine: Engine::Sqlite,
            production: false,
            status: Status::Connected,
            association: None,
        };
        let mut local: ConnectionRecord = toml::from_str(
            "key = 'notes_local'\ndb = 'notes'\ngroup = 'Demo'\nurl = 'sqlite:///fixture.db'\n",
        )
        .unwrap();
        assert_eq!(
            association_evidence(&local, &cloud),
            Some(AssociationEvidence::MatchingLabels)
        );
        local.group = Some("another-project".into());
        assert_eq!(association_evidence(&local, &cloud), None);
        for (key, value) in [
            ("platform_origin", "https://lab.example"),
            ("platform_user_id", "alice"),
            ("platform_resource_id", "notes"),
        ] {
            local.extra.insert(key.into(), value.into());
        }
        assert_eq!(
            association_evidence(&local, &cloud),
            Some(AssociationEvidence::ManagedCopy)
        );
    }

    #[cfg(unix)]
    #[test]
    fn registry_rejects_symlinks_and_public_files() {
        use std::os::unix::fs::{PermissionsExt, symlink};

        let root = tempfile::tempdir().unwrap();
        let registry = registry(root.path());
        let alice = provider("alice");
        let helper = helper(root.path().join("provider"), &[]);
        registry
            .register(&alice, &helper, &catalog("alice", "notes"))
            .unwrap();
        let path = registry.document_path();
        assert_eq!(path.metadata().unwrap().permissions().mode() & 0o777, 0o600);
        fs::set_permissions(&path, fs::Permissions::from_mode(0o644)).unwrap();
        assert!(registry.has_providers().is_err());
        let moved = root.path().join("moved.json");
        fs::rename(&path, &moved).unwrap();
        fs::set_permissions(&moved, fs::Permissions::from_mode(0o600)).unwrap();
        symlink(moved, path).unwrap();
        assert!(registry.has_providers().is_err());
    }

    #[test]
    fn provider_protocol_rejects_arbitrary_routes_and_mixed_arguments() {
        let mut request = ProviderRequest {
            version: 1,
            provider: provider("alice"),
            method: "GET".into(),
            path: "/identity".into(),
            pairs: vec![],
            body: None,
        };
        assert!(request.validate().is_ok());
        request.path = "/api/schema/export".into();
        assert!(request.validate().is_err());
        request.pairs.push(("resourceId".into(), "notes".into()));
        assert!(request.validate().is_ok());
        request.pairs.push(("resourceId".into(), "other".into()));
        assert!(request.validate().is_err());
        request.pairs.pop();
        request.path = "https://other.example/api/db/query".into();
        assert!(request.validate().is_err());
        request.path = "/api/query".into();
        request.method = "POST".into();
        request.pairs.clear();
        request.body = Some(json!({"resourceId": "notes", "sql": "SELECT 1"}));
        assert!(request.validate().is_ok());
        request.provider.origin = "https://user:password@lab.example/".into();
        assert!(request.validate().is_err());
        request.provider.origin = "http://lab.example/".into();
        assert!(request.validate().is_err());
        assert!(helper("provider".into(), &[]).validate().is_err());
        assert!(
            helper("/opt/provider".into(), &["db\n"])
                .validate()
                .is_err()
        );
    }

    #[cfg(unix)]
    #[tokio::test]
    async fn provider_route_keeps_cached_catalog_only_for_matching_identity() {
        use std::os::unix::fs::PermissionsExt;

        let root = tempfile::tempdir().unwrap();
        let registry = registry(root.path());
        let alice = provider("alice");
        let program = root.path().join("provider");
        let helper = helper(program.clone(), &["db", "provider"]);
        let fixture = |identity: &str, catalog_reply: &str| {
            let source = format!(
                r#"#!/bin/sh
[ "$#" = 2 ] && [ "$1" = db ] && [ "$2" = provider ] || exit 2
input=$(cat)
case "$input" in
    *'"/identity"'*) printf '%s' '{identity}';;
    *'"/api/platform"'*) printf '%s' '{catalog_reply}';;
    *'"/api/query"'*'"resourceId":"notes"'*) printf '%s' '{{"value":{{"rows":[1]}}}}';;
    *) exit 2;;
esac
"#
            );
            fs::write(&program, source).unwrap();
            fs::set_permissions(&program, fs::Permissions::from_mode(0o700)).unwrap();
        };
        let cloud = Federation { registry };
        let id = alice.route_id("notes");
        fixture(
            r#"{"value":{"id":"alice"}}"#,
            &json!({"value": catalog("alice", "notes")}).to_string(),
        );
        assert_eq!(cloud.register(&alice, &helper).await.unwrap(), 1);
        assert_eq!(cloud.catalog().await.unwrap().resources[0].id, id);
        let value = cloud
            .request(
                "POST",
                "/api/query",
                &[],
                Some(json!({"resourceId": id, "sql": "SELECT 1"})),
            )
            .await
            .unwrap();
        assert_eq!(value["rows"], json!([1]));
        fixture(
            r#"{"value":{"id":"alice"}}"#,
            r#"{"error":{"status":503,"code":"offline","message":"Offline"}}"#,
        );
        let offline = cloud.catalog().await.unwrap();
        assert_eq!(offline.resources[0].id, id);
        assert_eq!(offline.resources[0].status, Status::Unavailable);
        assert_eq!(cloud.authorized_cached_ids().await.unwrap(), vec![id]);
        fixture(r#"{"value":{"id":"bob"}}"#, "{}");
        assert_eq!(
            cloud.register(&alice, &helper).await.unwrap_err().code,
            "provider_account_mismatch"
        );
        let switched = cloud.catalog().await.unwrap();
        assert_eq!(switched.status, Status::SignedOut);
        assert!(switched.resources.is_empty());
        assert!(cloud.authorized_cached_ids().await.unwrap().is_empty());
        fixture(
            r#"{"value":{"id":"alice"}}"#,
            r#"{"error":{"status":403,"code":"forbidden","message":"Forbidden"}}"#,
        );
        assert!(cloud.catalog().await.unwrap().resources.is_empty());
        assert!(cloud.authorized_cached_ids().await.unwrap().is_empty());
    }

    #[cfg(unix)]
    #[tokio::test]
    async fn same_upstream_id_is_independent_across_providers() {
        use std::os::unix::fs::PermissionsExt;

        let root = tempfile::tempdir().unwrap();
        let registry = registry(root.path());
        let fixture = |id: &str| {
            let program = root.path().join(id);
            let source = format!(
                r#"#!/bin/sh
[ "$#" = 1 ] && [ "$1" = "{id}" ] || exit 2
input=$(cat)
case "$input" in
    *'"id":"{id}"'*'"/identity"'*) printf '%s' '{{"value":{{"id":"alice"}}}}';;
    *'"id":"{id}"'*'"/api/platform"'*) printf '%s' '{}';;
    *'"id":"{id}"'*'"/api/tables"'*'"notes"'*) printf '%s' '{{"value":"{id}"}}';;
    *) exit 2;;
esac
"#,
                json!({"value":catalog("alice", "notes")}),
            );
            fs::write(&program, source).unwrap();
            fs::set_permissions(&program, fs::Permissions::from_mode(0o700)).unwrap();
            (
                ProviderIdentity {
                    id: id.into(),
                    ..provider("alice")
                },
                helper(program, &[id]),
            )
        };
        let (first, first_helper) = fixture("first");
        let (second, second_helper) = fixture("second");
        let cloud = Federation { registry };
        cloud.register(&first, &first_helper).await.unwrap();
        cloud.register(&second, &second_helper).await.unwrap();
        let catalog = cloud.catalog().await.unwrap();
        assert_eq!(catalog.resources.len(), 2);
        assert_ne!(catalog.resources[0].id, catalog.resources[1].id);
        for provider in [&first, &second] {
            let value = cloud
                .request(
                    "GET",
                    "/api/tables",
                    &[("resourceId", provider.route_id("notes"))],
                    None,
                )
                .await
                .unwrap();
            assert_eq!(value, provider.id);
        }
        fs::remove_file(&first_helper.executable).unwrap();
        let catalog = cloud.catalog().await.unwrap();
        assert_eq!(catalog.resources.len(), 1);
        assert_eq!(catalog.resources[0].provider, second);
        assert_eq!(catalog.resources[0].status, Status::Connected);
    }
}
