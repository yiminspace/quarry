//! Authenticated hosted execution and an explicitly paired desktop companion.
//! Hosted state holds no user documents or database results after a request.
mod store;
use async_trait::async_trait;
use axum::{
    Json, Router,
    body::{Body, to_bytes},
    extract::{Request, State},
    http::{Method, StatusCode, header},
    response::{IntoResponse, Response},
    routing::any,
};
use futures_util::StreamExt;
use quarry_assistant::MetadataProvider;
use quarry_core::{CancellationToken, QueryRequest};
use quarry_runtime::ConnectionRecord;
use quarry_service::{MetadataOperation, QuarryService};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    collections::{BTreeMap, HashMap},
    path::PathBuf,
    sync::{Arc, Mutex},
    time::{SystemTime, UNIX_EPOCH},
};
pub use store::PersonalStore;
pub type Result<T> = quarry_service::Result<T>;
pub(crate) fn failure(status: u16, code: &str, message: &str) -> quarry_service::Failure {
    quarry_service::Failure {
        status,
        code: code.into(),
        message: message.into(),
    }
}
fn now() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs()
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Principal {
    pub id: String,
    #[serde(default)]
    pub permissions: Vec<String>,
    #[serde(default)]
    pub roles: Vec<String>,
}
/// The host must verify the persisted IAM session and this exact permission/scope
/// on every invocation, failing closed. Client identity headers are never trusted.
#[async_trait]
pub trait Authorizer: Send + Sync {
    async fn authorize(&self, bearer: &str, permission: &str) -> Result<Principal>;
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct HostedResource {
    pub id: String,
    pub project: String,
    pub db: String,
    pub env: String,
    pub connection_key: String,
    #[serde(default)]
    pub workspace: Option<PathBuf>,
    #[serde(default)]
    pub readers: Vec<String>,
}
impl HostedResource {
    fn granted(&self, p: &Principal) -> bool {
        self.readers.iter().any(|s| {
            s == &p.id
                || s.strip_prefix("role:")
                    .is_some_and(|r| p.roles.iter().any(|v| v == r))
        })
    }
}
type Active = Arc<Mutex<HashMap<(String, String), (String, CancellationToken)>>>;
#[derive(Clone)]
pub struct HostedState {
    pub service: Arc<QuarryService>,
    pub authorizer: Arc<dyn Authorizer>,
    resources: Arc<Vec<HostedResource>>,
    active: Active,
    assistant: Option<AssistantOptions>,
}
impl HostedState {
    pub fn new(
        service: Arc<QuarryService>,
        authorizer: Arc<dyn Authorizer>,
        resources: Vec<HostedResource>,
    ) -> Result<Self> {
        let mut ids = std::collections::HashSet::new();
        for r in &resources {
            if r.id.is_empty()
                || r.id.len() > 200
                || !ids.insert(r.id.clone())
                || r.project.is_empty()
                || r.db.is_empty()
                || r.env.is_empty()
            {
                return Err(failure(
                    400,
                    "invalid",
                    "Resource IDs must be unique and explicit",
                ));
            }
            service.resolve(&r.connection_key, Some(&r.env), r.workspace.as_deref())?;
        }
        Ok(Self {
            service,
            authorizer,
            resources: Arc::new(resources),
            active: Arc::new(Mutex::new(HashMap::new())),
            assistant: None,
        })
    }
    fn resource(&self, p: &Principal, id: &str) -> Result<&HostedResource> {
        self.resources
            .iter()
            .find(|r| r.id == id && r.granted(p))
            .ok_or_else(|| failure(404, "not_found", "Database resource is unavailable"))
    }
    fn record(&self, r: &HostedResource) -> Result<ConnectionRecord> {
        self.service
            .resolve(&r.connection_key, Some(&r.env), r.workspace.as_deref())
    }
}
pub fn hosted_router(state: HostedState) -> Router {
    Router::new()
        .fallback(any(hosted_dispatch))
        .with_state(state)
}
fn error_response(error: quarry_service::Failure) -> Response {
    (
        StatusCode::from_u16(error.status).unwrap_or(StatusCode::INTERNAL_SERVER_ERROR),
        Json(json!({"error":error.message,"code":error.code})),
    )
        .into_response()
}
fn response(value: Value) -> Response {
    let mut r = Json(value).into_response();
    r.headers_mut().insert(
        header::CACHE_CONTROL,
        header::HeaderValue::from_static("no-store"),
    );
    r
}
fn bearer(req: &Request) -> Result<String> {
    req.headers()
        .get(header::AUTHORIZATION)
        .and_then(|v| v.to_str().ok())
        .and_then(|v| v.strip_prefix("Bearer "))
        .filter(|s| !s.is_empty() && s.len() <= 8192 && !s.chars().any(char::is_control))
        .map(String::from)
        .ok_or_else(|| failure(401, "unauthorized", "IAM authentication required"))
}
fn query(req: &Request) -> BTreeMap<String, String> {
    url::form_urlencoded::parse(req.uri().query().unwrap_or("").as_bytes())
        .into_owned()
        .collect()
}
async fn body(req: Request) -> Result<Value> {
    let b = to_bytes(req.into_body(), 17 * 1024 * 1024)
        .await
        .map_err(|_| failure(413, "too_large", "Request exceeds size limit"))?;
    if b.is_empty() {
        return Ok(json!({}));
    }
    let v: Value =
        serde_json::from_slice(&b).map_err(|_| failure(400, "invalid", "Invalid JSON request"))?;
    if !v.is_object() {
        return Err(failure(400, "invalid", "Request must be an object"));
    }
    Ok(v)
}
fn text<'a>(v: &'a Value, key: &str) -> Result<&'a str> {
    v[key]
        .as_str()
        .filter(|s| !s.is_empty() && s.len() <= 200_000 && !s.contains('\0'))
        .ok_or_else(|| failure(400, "invalid", "Required text field is missing or invalid"))
}
fn id<'a>(q: &'a BTreeMap<String, String>, b: &'a Value) -> Result<&'a str> {
    b["resourceId"]
        .as_str()
        .or_else(|| q.get("resourceId").map(String::as_str))
        .ok_or_else(|| failure(400, "invalid", "Explicit database resource required"))
}
async fn hosted_dispatch(State(s): State<HostedState>, req: Request) -> Response {
    if req.method() == Method::POST && req.uri().path() == "/api/assistant/chat" {
        return match hosted_chat(&s, req).await {
            Ok(r) => r,
            Err(e) => error_response(e),
        };
    }
    let operation = async {
        let token = bearer(&req)?;
        let path = req.uri().path().to_owned();
        let method = req.method().clone();
        let q = query(&req);
        let permission = if path == "/api/schema/export" {
            "lab:db:schema:export"
        } else {
            "lab:db:read"
        };
        let p = s.authorizer.authorize(&token, permission).await?;
        if permission != "lab:db:read" {
            let read = s.authorizer.authorize(&token, "lab:db:read").await?;
            if read.id != p.id {
                return Err(failure(403, "forbidden", "IAM identity changed"));
            }
        }
        let b = if method == Method::POST {
            body(req).await?
        } else {
            json!({})
        };
        match (method.as_str(), path.as_str()) {
            ("GET", "/api/platform") => Ok(platform(
                "hosted",
                &p,
                &s.resources
                    .iter()
                    .filter(|r| r.granted(&p))
                    .filter_map(|r| s.record(r).ok().map(|c| resource_json(r, &c)))
                    .collect::<Vec<_>>(),
            )),
            ("GET", "/api/connections") => Ok(catalog(
                &s.resources
                    .iter()
                    .filter(|r| r.granted(&p))
                    .filter_map(|r| s.record(r).ok().map(|c| resource_json(r, &c)))
                    .collect::<Vec<_>>(),
            )),
            ("GET", "/api/queries") => Ok(json!([])),
            ("GET", "/api/assistant/config") => Ok(assistant_details(s.assistant.as_ref())),
            ("POST", "/api/cancel") => {
                if let Some((rid, _)) = s
                    .active
                    .lock()
                    .map_err(|_| failure(503, "unavailable", "Execution registry unavailable"))?
                    .get(&(p.id.clone(), text(&b, "requestId")?.into()))
                {
                    s.resource(&p, rid)?;
                }
                cancel(&s.active, &p, &b)
            }
            _ => {
                let r = s.resource(&p, id(&q, &b)?)?;
                let c = s.record(r)?;
                if method == Method::GET && path == "/api/schema/export" {
                    let schema = s
                        .service
                        .local()?
                        .export_schema(&c, &s.service.tunnels)
                        .await
                        .map_err(quarry_service::Failure::from)?;
                    return Ok(json!({"schema":schema,"engine":"postgres","resourceId":r.id}));
                }
                execute_api(&s.service, &s.active, &p, &r.id, &c, &method, &path, &q, &b).await
            }
        }
    }
    .await;
    match operation {
        Ok(mut v) => {
            if path_is_platform(&v) {
                v["capabilities"]["assistant"] = json!(s.assistant.is_some());
            }
            response(v)
        }
        Err(e) => error_response(e),
    }
}
fn resource_json(r: &HostedResource, c: &ConnectionRecord) -> Value {
    json!({"id":r.id,"resourceId":r.id,"project":r.project,"db":r.db,"env":r.env,"engine":c.spec().map(|s|s.engine.as_str()).unwrap_or("unknown"),"production":c.production})
}
fn platform(mode: &str, p: &Principal, resources: &[Value]) -> Value {
    json!({"mode":mode,"principal":p,"resources":resources,"personalStorage":"companion","authMode":if mode=="companion"{"paired-local"}else{"iam"},"capabilities":{"query":true,"schema":true,"export":true,"assistant":false,"sharedQueries":false,"local":mode=="companion","personalStorage":mode=="companion"}})
}
fn catalog(resources: &[Value]) -> Value {
    let mut groups: BTreeMap<String, BTreeMap<String, Vec<Value>>> = BTreeMap::new();
    for r in resources {
        groups.entry(r["project"].as_str().unwrap_or("local").into()).or_default().entry(r["db"].as_str().unwrap_or("").into()).or_default().push(json!({"env":r["env"],"resourceId":r["id"],"key":r["id"],"engine":r["engine"],"production":r["production"],"ssh":false,"proxied":false,"region":null,"sourceResourceId":r["sourceResourceId"]}));
    }
    json!({"groups":groups.into_iter().map(|(g,items)|json!({"group":g,"ws":g,"items":items.into_iter().map(|(db,envs)|json!({"db":db,"engine":envs[0]["engine"],"is_env_set":true,"envs":envs})).collect::<Vec<_>>()})).collect::<Vec<_>>(),"workspace":"platform","workspaces":[]})
}
fn cancel(active: &Active, p: &Principal, b: &Value) -> Result<Value> {
    let request = text(b, "requestId")?;
    let map = active
        .lock()
        .map_err(|_| failure(503, "unavailable", "Execution registry unavailable"))?;
    let cancelled = if let Some((_, token)) = map.get(&(p.id.clone(), request.into())) {
        token.cancel();
        true
    } else {
        false
    };
    Ok(json!({"cancelled":cancelled}))
}
struct Lease {
    active: Active,
    key: (String, String),
}
impl Drop for Lease {
    fn drop(&mut self) {
        if let Ok(mut map) = self.active.lock()
            && let Some((_, token)) = map.remove(&self.key)
        {
            token.cancel();
        }
    }
}
#[allow(clippy::too_many_arguments)]
async fn execute_api(
    service: &QuarryService,
    active: &Active,
    p: &Principal,
    resource_id: &str,
    c: &ConnectionRecord,
    method: &Method,
    path: &str,
    q: &BTreeMap<String, String>,
    b: &Value,
) -> Result<Value> {
    match (method.as_str(), path) {
        ("GET", "/api/info") => Ok(
            json!({"db":c.logical_db(),"env":c.env,"engine":c.spec()?.engine.as_str(),"production":c.production,"resourceId":resource_id}),
        ),
        ("GET", "/api/tables") => service.tables(c).await.map_err(safe_database_error),
        ("GET", "/api/columns") => service
            .columns(
                c,
                q.get("table")
                    .ok_or_else(|| failure(400, "invalid", "Table required"))?,
            )
            .await
            .map_err(safe_database_error),
        ("GET", "/api/health") => service
            .metadata(c, MetadataOperation::Ping, None, CancellationToken::new())
            .await
            .map(|_| json!({"ok":true}))
            .map_err(safe_database_error),
        ("GET", "/api/inspect") => service
            .metadata(
                c,
                MetadataOperation::InspectKey(
                    q.get("key")
                        .ok_or_else(|| failure(400, "invalid", "Key required"))?
                        .clone(),
                ),
                None,
                CancellationToken::new(),
            )
            .await
            .map_err(safe_database_error),
        ("POST", "/api/record-link") => service
            .metadata(
                c,
                MetadataOperation::RecordLink {
                    query: text(b, "sql")?.into(),
                    row: b["row"].clone(),
                },
                None,
                CancellationToken::new(),
            )
            .await
            .map_err(safe_database_error),
        ("POST", "/api/query") => {
            let sql = text(b, "sql")?;
            let request_id = b["requestId"]
                .as_str()
                .filter(|s| !s.is_empty() && s.len() <= 100)
                .map(String::from)
                .unwrap_or_else(|| uuid::Uuid::new_v4().to_string());
            let mut request = QueryRequest::new(sql);
            let max = b["maxRows"].as_u64().unwrap_or(500);
            if !(1..=5000).contains(&max) {
                return Err(failure(
                    400,
                    "invalid",
                    "Row limit must be between 1 and 5000",
                ));
            }
            request.policy.max_rows = Some(max as usize);
            request.policy.offset = b["offset"].as_u64().unwrap_or(0).min(1_000_000) as usize;
            request.policy.max_bytes = 16 * 1024 * 1024;
            request.policy.timeout_ms = Some(30_000);
            let key = (p.id.clone(), request_id.clone());
            {
                let mut map = active
                    .lock()
                    .map_err(|_| failure(503, "unavailable", "Execution registry unavailable"))?;
                if map.contains_key(&key) {
                    return Err(failure(409, "conflict", "Request is already running"));
                }
                if map.len() >= 64 {
                    return Err(failure(429, "busy", "Too many active database requests"));
                }
                map.insert(
                    key.clone(),
                    (resource_id.into(), request.cancellation.clone()),
                );
            }
            let _lease = Lease {
                active: active.clone(),
                key,
            };
            let mut out = service
                .execute(c, request)
                .await
                .map_err(safe_database_error)?
                .to_legacy_json();
            out["resourceId"] = json!(resource_id);
            out["requestId"] = json!(request_id);
            Ok(out)
        }
        _ => Err(failure(404, "not_found", "Operation is unavailable")),
    }
}
fn safe_database_error(e: quarry_service::Failure) -> quarry_service::Failure {
    if e.status == 400 || e.status == 403 || e.status == 499 {
        failure(
            e.status,
            &e.code,
            "Database operation was rejected or cancelled",
        )
    } else {
        failure(
            e.status,
            &e.code,
            "Database operation failed; check the connection or query",
        )
    }
}

#[derive(Clone, Debug)]
pub struct CompanionConfig {
    pub origin: String,
    pub user_id: String,
    pub state_path: PathBuf,
    pub port: u16,
}
struct PairState {
    code_hash: String,
    code: String,
    expires: u64,
    attempts: u8,
    sessions: HashMap<String, u64>,
}
#[derive(Clone)]
pub struct CompanionState {
    pub service: Arc<QuarryService>,
    pub authorizer: Arc<dyn Authorizer>,
    config: Arc<CompanionConfig>,
    pairs: Arc<Mutex<PairState>>,
    personal: PersonalStore,
    docker_executable: Option<PathBuf>,
    active: Active,
    assistant: Option<AssistantOptions>,
}
impl CompanionState {
    pub fn new(
        service: Arc<QuarryService>,
        authorizer: Arc<dyn Authorizer>,
        config: CompanionConfig,
    ) -> Result<Self> {
        let origin = url::Url::parse(&config.origin)
            .map_err(|_| failure(400, "invalid", "Invalid trusted platform origin"))?;
        if origin.origin().ascii_serialization() != config.origin
            || !(origin.scheme() == "https"
                || origin.scheme() == "http"
                    && matches!(origin.host_str(), Some("localhost" | "127.0.0.1" | "[::1]")))
            || config.user_id.is_empty()
            || config.port == 0
            || !config.state_path.is_absolute()
        {
            return Err(failure(
                400,
                "invalid",
                "Explicit HTTPS platform origin, IAM user, port and absolute state path required",
            ));
        }
        let code = uuid::Uuid::new_v4().simple().to_string();
        let code_hash = hash(&code);
        Ok(Self {
            service,
            authorizer,
            personal: PersonalStore::new(config.state_path.clone()),
            docker_executable: None,
            config: Arc::new(config),
            pairs: Arc::new(Mutex::new(PairState {
                code_hash,
                code,
                expires: now() + 300,
                attempts: 0,
                sessions: HashMap::new(),
            })),
            active: Arc::new(Mutex::new(HashMap::new())),
            assistant: None,
        })
    }
    /// Display only in the user's terminal. Never expose through an HTTP status endpoint.
    pub fn pairing_code(&self) -> Value {
        let p = self.pairs.lock().unwrap_or_else(|e| e.into_inner());
        json!({"code":p.code,"expiresAt":p.expires,"origin":self.config.origin,"port":self.config.port})
    }
    pub fn revoke_all(&self) {
        if let Ok(mut p) = self.pairs.lock() {
            p.sessions.clear();
            p.expires = 0;
        }
        if let Ok(mut active) = self.active.lock() {
            for (_, token) in active.values() {
                token.cancel();
            }
            active.clear();
        }
    }
    fn local_resources(&self) -> Result<Vec<(Value, ConnectionRecord)>> {
        let mut resources = Vec::new();
        let device = self.personal.device_id()?;
        for c in self.service.catalog()?.connections()? {
            let engine = c.spec()?.engine;
            let local_address = engine == quarry_core::Engine::Sqlite
                || url::Url::parse(&c.url).is_ok_and(|u| {
                    matches!(u.host_str(), Some("localhost" | "127.0.0.1" | "[::1]"))
                });
            if c.env != "local"
                || c.ssh_host.is_some()
                || !local_address
                || c.extra.get("platform_origin").and_then(toml_string)
                    != Some(self.config.origin.as_str())
                || c.extra.get("platform_user_id").and_then(toml_string)
                    != Some(self.config.user_id.as_str())
            {
                continue;
            }
            let id = format!(
                "local:{}",
                hash(&format!("{device}:{}:{}", c.workspace.display(), c.key))
            );
            let project = c
                .extra
                .get("platform_project")
                .and_then(toml_string)
                .map(String::from)
                .or_else(|| c.group.clone())
                .unwrap_or_else(|| "local".into());
            let db = c
                .extra
                .get("platform_db")
                .and_then(toml_string)
                .unwrap_or(c.logical_db());
            let source = c.extra.get("platform_resource_id").and_then(toml_string);
            let value = json!({"id":id,"resourceId":id,"project":project,"db":db,"env":"local","engine":engine.as_str(),"production":false,"sourceResourceId":source});
            resources.push((value, c));
        }
        Ok(resources)
    }
}
// Keep TOML types at the runtime boundary without adding a separate parser here.
fn toml_string(v: &toml::Value) -> Option<&str> {
    v.as_str()
}
fn hash(v: &str) -> String {
    format!("{:x}", Sha256::digest(v.as_bytes()))
}
pub fn companion_router(state: CompanionState) -> Router {
    Router::new()
        .fallback(any(companion_dispatch))
        .with_state(state)
}
fn local_boundary(s: &CompanionState, req: &Request) -> Result<()> {
    let h = req.headers();
    let host = h
        .get(header::HOST)
        .and_then(|v| v.to_str().ok())
        .unwrap_or("")
        .to_lowercase();
    if h.get_all(header::HOST).iter().count() != 1
        || ![
            format!("127.0.0.1:{}", s.config.port),
            format!("localhost:{}", s.config.port),
            format!("[::1]:{}", s.config.port),
        ]
        .contains(&host)
        || h.get(header::ORIGIN).and_then(|v| v.to_str().ok()) != Some(s.config.origin.as_str())
    {
        return Err(failure(
            403,
            "forbidden",
            "Companion requires its paired platform origin and loopback host",
        ));
    }
    Ok(())
}
fn cors(s: &CompanionState, mut r: Response) -> Response {
    if let Ok(origin) = header::HeaderValue::from_str(&s.config.origin) {
        r.headers_mut()
            .insert(header::ACCESS_CONTROL_ALLOW_ORIGIN, origin);
    }
    r.headers_mut()
        .insert(header::VARY, header::HeaderValue::from_static("Origin"));
    r.headers_mut().insert(
        header::ACCESS_CONTROL_ALLOW_METHODS,
        header::HeaderValue::from_static("GET, POST, OPTIONS"),
    );
    r.headers_mut().insert(
        header::ACCESS_CONTROL_ALLOW_HEADERS,
        header::HeaderValue::from_static(
            "Authorization, Content-Type, X-Quarry-Companion-Token, X-Quarry-User-Id",
        ),
    );
    r.headers_mut().insert(
        "Access-Control-Allow-Private-Network",
        header::HeaderValue::from_static("true"),
    );
    r
}
async fn companion_dispatch(State(s): State<CompanionState>, req: Request) -> Response {
    if let Err(e) = local_boundary(&s, &req) {
        return error_response(e);
    }
    if req.method() == Method::OPTIONS {
        return cors(&s, StatusCode::NO_CONTENT.into_response());
    }
    if req.method() == Method::POST && req.uri().path() == "/api/assistant/chat" {
        return cors(
            &s,
            match companion_chat(&s, req).await {
                Ok(r) => r,
                Err(e) => error_response(e),
            },
        );
    }
    let operation = async {
        let token = bearer(&req).unwrap_or_default();
        let user_header = req
            .headers()
            .get("X-Quarry-User-Id")
            .and_then(|v| v.to_str().ok())
            .map(String::from);
        let path = req.uri().path().to_owned();
        let method = req.method().clone();
        let q = query(&req);
        let paired = req
            .headers()
            .get("X-Quarry-Companion-Token")
            .and_then(|v| v.to_str().ok())
            .map(hash);
        let b = if method == Method::POST {
            body(req).await?
        } else {
            json!({})
        };
        if method == Method::POST && path == "/api/pair" {
            let p = s.authorizer.authorize(&token, "lab:db:local").await?;
            if p.id != s.config.user_id {
                return Err(failure(
                    403,
                    "forbidden",
                    "Companion is paired to another IAM user",
                ));
            }
            let mut pairs = s
                .pairs
                .lock()
                .map_err(|_| failure(503, "unavailable", "Pairing unavailable"))?;
            if now() >= pairs.expires || pairs.attempts >= 5 {
                return Err(failure(
                    403,
                    "pairing_expired",
                    "Pairing expired. Restart the companion to pair again.",
                ));
            }
            pairs.attempts += 1;
            if hash(text(&b, "code")?) != pairs.code_hash {
                return Err(failure(403, "forbidden", "Invalid pairing code"));
            }
            let token = uuid::Uuid::new_v4().simple().to_string();
            let expires = now() + 8 * 3600;
            pairs.sessions.insert(hash(&token), expires);
            pairs.expires = 0;
            pairs.code.clear();
            return Ok(
                json!({"token":token,"expiresAt":expires,"userId":p.id,"origin":s.config.origin}),
            );
        }
        if user_header.as_deref() != Some(s.config.user_id.as_str()) {
            return Err(failure(
                403,
                "forbidden",
                "Companion owner header does not match this account",
            ));
        }
        let p = Principal {
            id: s.config.user_id.clone(),
            permissions: vec!["local:paired".into()],
            roles: vec![],
        };
        let paired = paired.ok_or_else(|| {
            failure(
                401,
                "pairing_required",
                "Pair this computer before continuing",
            )
        })?;
        {
            let mut pairs = s
                .pairs
                .lock()
                .map_err(|_| failure(503, "unavailable", "Pairing unavailable"))?;
            pairs.sessions.retain(|_, exp| *exp > now());
            if !pairs.sessions.contains_key(&paired) {
                return Err(failure(
                    401,
                    "pairing_required",
                    "Companion pairing expired or was revoked",
                ));
            }
        }
        match (method.as_str(), path.as_str()) {
            ("POST", "/api/unpair") => {
                s.revoke_all();
                Ok(json!({"revoked":true}))
            }
            ("GET", "/api/platform") => {
                let mut value = platform(
                    "companion",
                    &p,
                    &s.local_resources()?
                        .into_iter()
                        .map(|(v, _)| v)
                        .collect::<Vec<_>>(),
                );
                value["deviceId"] = json!(s.personal.device_id()?);
                Ok(value)
            }
            ("GET", "/api/connections") => Ok(catalog(
                &s.local_resources()?
                    .into_iter()
                    .map(|(v, _)| v)
                    .collect::<Vec<_>>(),
            )),
            ("GET", "/api/queries") => Ok(json!([])),
            ("GET", "/api/assistant/config") => Ok(assistant_details(s.assistant.as_ref())),
            ("GET", "/api/personal") => s.personal.get(
                &s.config.origin,
                &p.id,
                q.get("kind").map(String::as_str).unwrap_or("workbench"),
                q.get("key").map(String::as_str).unwrap_or("workspace"),
            ),
            ("POST", "/api/personal") => s.personal.put(
                &s.config.origin,
                &p.id,
                text(&b, "kind")?,
                text(&b, "key")?,
                b["value"].clone(),
                b["revision"]
                    .as_u64()
                    .ok_or_else(|| failure(400, "invalid", "Revision required"))?,
            ),
            ("POST", "/api/cancel") => cancel(&s.active, &p, &b),
            ("GET", "/api/local/status") => s.local_status().await,
            ("POST", "/api/local/down") => s.stop_local(&b).await,
            ("POST", "/api/local/up") => s.create_local(&b).await,
            _ => {
                let target = id(&q, &b)?;
                let (_, c) = s
                    .local_resources()?
                    .into_iter()
                    .find(|(v, _)| v["id"] == target)
                    .ok_or_else(|| failure(404, "not_found", "Local database is unavailable"))?;
                if method == Method::POST && path == "/api/schema/import" {
                    let result = s.import_hosted_schema(&token, &p, &c, &b).await?;
                    s.service.cache.clear();
                    return Ok(result);
                }
                execute_api(
                    &s.service, &s.active, &p, target, &c, &method, &path, &q, &b,
                )
                .await
            }
        }
    }
    .await;
    cors(
        &s,
        match operation {
            Ok(mut v) => {
                if path_is_platform(&v) {
                    v["capabilities"]["assistant"] = json!(s.assistant.is_some());
                }
                response(v)
            }
            Err(e) => error_response(e),
        },
    )
}
impl CompanionState {
    /// Optional executable selected only by a trusted embedding host, never by
    /// HTTP input. Useful for non-PATH Docker installs and isolated contracts.
    pub fn with_docker_executable(mut self, path: PathBuf) -> Result<Self> {
        if !path.is_absolute() {
            return Err(failure(
                400,
                "invalid",
                "Docker executable must be an absolute trusted path",
            ));
        }
        self.docker_executable = Some(path);
        Ok(self)
    }
    fn local_namespace(&self, source: &str) -> String {
        format!(
            "ylab-{}",
            &hash(&format!(
                "{}:{}:{source}",
                self.config.origin, self.config.user_id
            ))[..20]
        )
    }
    fn managed_local(&self, source: &str) -> Result<quarry_local::LocalManager> {
        let namespace = self.local_namespace(source);
        let base = self
            .config
            .state_path
            .parent()
            .ok_or_else(|| failure(400, "invalid", "Invalid personal storage path"))?
            .join("local-runtime")
            .join(&namespace);
        let mut manager = self.service.local()?.with_namespace(namespace)?;
        // Ports and daemon state are owned by this account AND source. Never
        // edit the standalone Quarry configuration or claim its default volumes.
        manager.config = quarry_runtime::RuntimeConfig::load(base.join("config.toml"))?;
        manager.state_dir = base.join("state");
        if let Some(path) = &self.docker_executable {
            manager.docker = path.clone();
        }
        Ok(manager)
    }
    async fn lifecycle_lock(manager: &quarry_local::LocalManager) -> Result<std::fs::File> {
        let path = manager.state_dir.join("platform-lifecycle.lock");
        tokio::task::spawn_blocking(move || {
            let parent = path
                .parent()
                .ok_or_else(|| failure(400, "invalid", "Invalid managed state path"))?;
            std::fs::create_dir_all(parent)?;
            #[cfg(unix)]
            {
                use std::os::unix::fs::PermissionsExt;
                std::fs::set_permissions(parent, std::fs::Permissions::from_mode(0o700))?;
            }
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
        .map_err(|_| failure(503, "unavailable", "Cannot lock local lifecycle"))?
    }
    async fn start_managed_engine(
        &self,
        manager: &quarry_local::LocalManager,
        engine: &str,
    ) -> Result<()> {
        let configured = manager.config.local.contains_key(&format!("{engine}_port"));
        let port = if configured {
            None
        } else {
            let status = manager.status(Some(engine)).await?;
            if status["state"] == "running"
                || status["state"] == "stopped"
                || status["running"] == true
            {
                if engine != "neptune" && status["loopback_verified"] != true {
                    return Err(failure(
                        409,
                        "conflict",
                        "Existing managed service has an unverified network binding",
                    ));
                }
                let port = status["port"]
                    .as_u64()
                    .and_then(|n| u16::try_from(n).ok())
                    .filter(|n| *n > 0)
                    .ok_or_else(|| {
                        failure(
                            409,
                            "conflict",
                            "Existing managed service has no verified port",
                        )
                    })?;
                manager.config.set_local_port(engine, port)?;
                None
            } else {
                let socket = std::net::TcpListener::bind((std::net::Ipv4Addr::LOCALHOST, 0))?;
                let port = socket.local_addr()?.port();
                drop(socket);
                Some(port)
            }
        };
        manager.up(engine, None, port).await?;
        Ok(())
    }
    fn owned_managed_record(
        &self,
        resource_id: &str,
    ) -> Result<(ConnectionRecord, quarry_local::LocalManager)> {
        let (_, record) = self
            .local_resources()?
            .into_iter()
            .find(|(value, _)| value["id"] == resource_id)
            .ok_or_else(|| failure(404, "not_found", "Local database is unavailable"))?;
        let source = record
            .extra
            .get("platform_resource_id")
            .and_then(toml_string)
            .ok_or_else(|| {
                failure(
                    403,
                    "forbidden",
                    "Local service is not managed by this pairing",
                )
            })?;
        let namespace = self.local_namespace(source);
        let manager = self.managed_local(source)?;
        if record.extra.get("platform_namespace").and_then(toml_string) != Some(namespace.as_str())
        {
            return Err(failure(
                403,
                "forbidden",
                "Local service belongs to another lifecycle namespace",
            ));
        }
        let engine = record.spec()?.engine;
        let spec = manager.spec(engine.as_str())?;
        let owned = if engine == quarry_core::Engine::Neptune {
            record.extra.get("tls_ca_file").and_then(toml_string)
                == Some(
                    manager
                        .state_dir
                        .join("neptune/cert.pem")
                        .to_string_lossy()
                        .as_ref(),
                )
                && record
                    .extra
                    .get("local_backend")
                    .and_then(toml_string)
                    .is_some()
        } else {
            record.extra.get("local_volume").and_then(toml_string) == Some(spec.volume.as_str())
        };
        if !owned {
            return Err(failure(
                403,
                "forbidden",
                "Local service volume or daemon does not belong to this pairing",
            ));
        }
        Ok((record, manager))
    }
    async fn stop_local(&self, b: &Value) -> Result<Value> {
        // Resolve the exact owned target before constructing any Docker command.
        let (record, manager) = self.owned_managed_record(text(b, "resourceId")?)?;
        let engine = record.spec()?.engine.as_str();
        if b["engine"].as_str().is_some_and(|value| value != engine) {
            return Err(failure(
                400,
                "invalid",
                "Engine does not match the selected Local resource",
            ));
        }
        let _lock = Self::lifecycle_lock(&manager).await?;
        let mut value = manager.down(engine, false).await?;
        value["resourceId"] = b["resourceId"].clone();
        Ok(value)
    }
    async fn local_status(&self) -> Result<Value> {
        let mut items = Vec::new();
        for (resource, _) in self.local_resources()? {
            if let Ok((record, manager)) =
                self.owned_managed_record(resource["id"].as_str().unwrap_or(""))
            {
                let mut status = manager.status(Some(record.spec()?.engine.as_str())).await?;
                status["resourceId"] = resource["id"].clone();
                status["sourceResourceId"] = resource["sourceResourceId"].clone();
                items.push(status);
            }
        }
        Ok(json!({"items":items}))
    }
    async fn create_local(&self, b: &Value) -> Result<Value> {
        let db = text(b, "db")?;
        let source = text(b, "sourceResourceId")?;
        let project = text(b, "project")?;
        let engine = text(b, "engine")?;
        if !["postgres", "redis", "neptune"].contains(&engine) {
            return Err(failure(400, "invalid", "Unsupported managed local engine"));
        }
        if self.local_resources()?.iter().any(|(resource, _)| {
            resource["sourceResourceId"] == source && resource["engine"] != engine
        }) {
            return Err(failure(
                400,
                "invalid",
                "A Local source cannot change its database engine",
            ));
        }
        let catalog = self.service.catalog()?;
        let workspace = catalog
            .workspaces
            .first()
            .ok_or_else(|| {
                failure(
                    400,
                    "invalid",
                    "Register a local workspace before creating databases",
                )
            })?
            .home
            .clone();
        // Local identity includes platform and source; a prod-derived database
        // never silently replaces another environment's Local.
        let identity = format!(
            "ylab_{}",
            &hash(&format!(
                "{}:{}:{source}",
                self.config.origin, self.config.user_id
            ))[..20]
        );
        let manager = self.managed_local(source)?;
        let _lock = Self::lifecycle_lock(&manager).await?;
        self.start_managed_engine(&manager, engine).await?;
        let mut c = manager
            .register_new(&catalog, &workspace, &identity, engine)
            .await?;
        c.extra.insert(
            "platform_namespace".into(),
            self.local_namespace(source).into(),
        );
        c.extra
            .insert("platform_origin".into(), self.config.origin.clone().into());
        c.extra.insert(
            "platform_user_id".into(),
            self.config.user_id.clone().into(),
        );
        c.extra.insert("platform_resource_id".into(), source.into());
        c.extra.insert("platform_project".into(), project.into());
        c.extra.insert("platform_db".into(), db.into());
        catalog.put_connection(&workspace, &c, true)?;
        self.service.cache.clear();
        let resources = self
            .local_resources()?
            .into_iter()
            .filter(|(v, _)| v["sourceResourceId"] == source)
            .map(|(v, _)| v)
            .collect::<Vec<_>>();
        Ok(json!({"resources":resources,"created":true}))
    }
}

impl CompanionState {
    async fn import_hosted_schema(
        &self,
        token: &str,
        p: &Principal,
        target: &ConnectionRecord,
        b: &Value,
    ) -> Result<Value> {
        if b.get("schema").is_some() {
            return Err(failure(
                400,
                "invalid",
                "Schema must be fetched from the authorized platform",
            ));
        }
        let source = text(b, "sourceResourceId")?;
        if target
            .extra
            .get("platform_resource_id")
            .and_then(toml_string)
            != Some(source)
        {
            return Err(failure(
                400,
                "invalid",
                "Schema source does not match this Local database",
            ));
        }
        self.authorizer
            .authorize(token, "lab:db:schema:export")
            .await?
            .id
            .eq(&p.id)
            .then_some(())
            .ok_or_else(|| failure(403, "forbidden", "IAM identity changed"))?;
        let mut url = url::Url::parse(&format!("{}/api/db/schema/export", self.config.origin))
            .map_err(|_| failure(400, "invalid", "Invalid platform origin"))?;
        url.query_pairs_mut().append_pair("resourceId", source);
        let client = reqwest::Client::builder()
            .redirect(reqwest::redirect::Policy::none())
            .timeout(std::time::Duration::from_secs(60))
            .build()
            .map_err(|_| failure(503, "unavailable", "Cannot initialize platform transport"))?;
        let mut reply = client
            .get(url)
            .bearer_auth(token)
            .send()
            .await
            .map_err(|_| {
                failure(
                    503,
                    "unavailable",
                    "Cannot obtain authorized platform schema",
                )
            })?;
        if !reply.status().is_success() {
            return Err(failure(403, "forbidden", "Platform rejected schema export"));
        }
        let mut bytes = Vec::new();
        while let Some(chunk) = reply
            .chunk()
            .await
            .map_err(|_| failure(503, "unavailable", "Schema transfer failed"))?
        {
            if bytes.len() + chunk.len() > 9 * 1024 * 1024 {
                return Err(failure(413, "too_large", "Schema exceeds transfer budget"));
            }
            bytes.extend_from_slice(&chunk);
        }
        let value: Value = serde_json::from_slice(&bytes)
            .map_err(|_| failure(503, "unavailable", "Invalid platform schema response"))?;
        if value["resourceId"] != source || value["engine"] != "postgres" {
            return Err(failure(400, "invalid", "Platform schema target mismatch"));
        }
        Ok(self
            .service
            .local()?
            .import_schema(
                target,
                value["schema"]
                    .as_str()
                    .filter(|v| v.len() <= 8 * 1024 * 1024)
                    .ok_or_else(|| failure(400, "invalid", "Invalid platform schema"))?,
            )
            .await?)
    }
}

fn path_is_platform(v: &Value) -> bool {
    v["mode"].is_string() && v["capabilities"].is_object()
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct AssistantOptions {
    pub base_url: String,
    pub model: String,
}
impl AssistantOptions {
    fn config(&self, bearer: &str) -> quarry_assistant::ModelConfig {
        quarry_assistant::ModelConfig {
            base_url: self.base_url.clone(),
            model: self.model.clone(),
            api_key: bearer.into(),
            credential_command: None,
            bifrost: false,
            thinking: "disabled".into(),
            timeout: std::time::Duration::from_secs(30),
        }
    }
}
impl HostedState {
    pub fn with_assistant(mut self, options: AssistantOptions) -> Result<Self> {
        options.config("").validate()?;
        self.assistant = Some(options);
        Ok(self)
    }
}
impl CompanionState {
    pub fn with_assistant(mut self, options: AssistantOptions) -> Result<Self> {
        options.config("").validate()?;
        self.assistant = Some(options);
        Ok(self)
    }
}
fn assistant_details(options: Option<&AssistantOptions>) -> Value {
    match options {
        Some(o) => {
            json!({"configured":true,"model":o.model,"base_url":o.base_url,"auth":"iam","bifrost":false,"thinking":"disabled","timeout":30,"api_key":"********"})
        }
        None => json!({"configured":false}),
    }
}
struct ScopedMetadata {
    service: Arc<QuarryService>,
    authorizer: Arc<dyn Authorizer>,
    bearer: String,
    principal: String,
    record: ConnectionRecord,
    resource_id: String,
    resource: Option<HostedResource>,
    permission: &'static str,
}
fn assistant_error(e: quarry_service::Failure) -> quarry_assistant::AssistantError {
    quarry_assistant::AssistantError {
        status: e.status,
        code: "PLATFORM",
        message: "Database metadata is unavailable or no longer authorized".into(),
    }
}
impl ScopedMetadata {
    async fn internal(
        &self,
    ) -> quarry_assistant::AssistantResult<quarry_assistant::ResolvedContext> {
        let p = self
            .authorizer
            .authorize(&self.bearer, self.permission)
            .await
            .map_err(assistant_error)?;
        if p.id != self.principal || self.resource.as_ref().is_some_and(|r| !r.granted(&p)) {
            return Err(assistant_error(failure(
                403,
                "forbidden",
                "Permission revoked",
            )));
        }
        MetadataProvider::resolve(
            self.service.as_ref(),
            &quarry_assistant::Target {
                db: self.record.key.clone(),
                env: Some(self.record.env.clone()),
                workspace: Some(self.record.workspace.to_string_lossy().into()),
            },
        )
        .await
    }
}
#[async_trait]
impl MetadataProvider for ScopedMetadata {
    async fn resolve(
        &self,
        _: &quarry_assistant::Target,
    ) -> quarry_assistant::AssistantResult<quarry_assistant::ResolvedContext> {
        let mut c = self.internal().await?;
        c.target = quarry_assistant::Target {
            db: self.resource_id.clone(),
            env: Some(self.record.env.clone()),
            workspace: None,
        };
        c.gui_workspace = None;
        Ok(c)
    }
    async fn find_tables(
        &self,
        _: &quarry_assistant::ResolvedContext,
        query: &str,
        limit: usize,
    ) -> quarry_assistant::AssistantResult<Value> {
        let c = self.internal().await?;
        MetadataProvider::find_tables(self.service.as_ref(), &c, query, limit).await
    }
    async fn describe_tables(
        &self,
        _: &quarry_assistant::ResolvedContext,
        tables: &[String],
    ) -> quarry_assistant::AssistantResult<Value> {
        let c = self.internal().await?;
        MetadataProvider::describe_tables(self.service.as_ref(), &c, tables).await
    }
}
async fn hosted_chat(s: &HostedState, req: Request) -> Result<Response> {
    let token = bearer(&req)?;
    let p = s.authorizer.authorize(&token, "lab:db:read").await?;
    let ai = s.authorizer.authorize(&token, "lab:ai:invoke").await?;
    if p.id != ai.id {
        return Err(failure(403, "forbidden", "IAM identity changed"));
    }
    let b = body(req).await?;
    let r = s.resource(&p, text(&b, "resourceId")?)?.clone();
    let record = s.record(&r)?;
    let metadata = ScopedMetadata {
        service: s.service.clone(),
        authorizer: s.authorizer.clone(),
        bearer: token.clone(),
        principal: p.id,
        record,
        resource_id: r.id.clone(),
        resource: Some(r),
        permission: "lab:db:read",
    };
    ephemeral_chat(s.assistant.as_ref(), s.active.clone(), metadata, token, b).await
}
async fn companion_chat(s: &CompanionState, req: Request) -> Result<Response> {
    if req
        .headers()
        .get("X-Quarry-User-Id")
        .and_then(|v| v.to_str().ok())
        != Some(s.config.user_id.as_str())
    {
        return Err(failure(403, "forbidden", "Companion owner mismatch"));
    }
    let token = bearer(&req)?;
    let p = s.authorizer.authorize(&token, "lab:db:local").await?;
    if p.id != s.config.user_id {
        return Err(failure(403, "forbidden", "Wrong IAM user"));
    }
    let paired = req
        .headers()
        .get("X-Quarry-Companion-Token")
        .and_then(|v| v.to_str().ok())
        .map(hash)
        .ok_or_else(|| failure(401, "pairing_required", "Pair this computer first"))?;
    if !s
        .pairs
        .lock()
        .map_err(|_| failure(503, "unavailable", "Pairing unavailable"))?
        .sessions
        .get(&paired)
        .is_some_and(|exp| *exp > now())
    {
        return Err(failure(
            401,
            "pairing_required",
            "Companion pairing expired or revoked",
        ));
    }
    let ai = s.authorizer.authorize(&token, "lab:ai:invoke").await?;
    if ai.id != p.id {
        return Err(failure(403, "forbidden", "IAM identity changed"));
    }
    let b = body(req).await?;
    let resource_id = text(&b, "resourceId")?.to_owned();
    let (_, record) = s
        .local_resources()?
        .into_iter()
        .find(|(v, _)| v["id"] == resource_id)
        .ok_or_else(|| failure(404, "not_found", "Local target unavailable"))?;
    let metadata = ScopedMetadata {
        service: s.service.clone(),
        authorizer: s.authorizer.clone(),
        bearer: token.clone(),
        principal: p.id,
        record,
        resource_id,
        resource: None,
        permission: "lab:db:local",
    };
    ephemeral_chat(s.assistant.as_ref(), s.active.clone(), metadata, token, b).await
}
async fn ephemeral_chat(
    options: Option<&AssistantOptions>,
    active: Active,
    metadata: ScopedMetadata,
    token: String,
    mut b: Value,
) -> Result<Response> {
    let options =
        options.ok_or_else(|| failure(503, "unavailable", "SQL assistant is not configured"))?;
    let request_id = b["requestId"]
        .as_str()
        .filter(|v| !v.is_empty() && v.len() <= 100)
        .map(String::from)
        .unwrap_or_else(|| uuid::Uuid::new_v4().to_string());
    b["requestId"] = json!(request_id);
    let key = (metadata.principal.clone(), request_id);
    let cancel = CancellationToken::new();
    {
        let mut map = active
            .lock()
            .map_err(|_| failure(503, "unavailable", "Execution registry unavailable"))?;
        if map.contains_key(&key) {
            return Err(failure(409, "conflict", "Request already running"));
        }
        if map.len() >= 64 {
            return Err(failure(429, "busy", "Too many requests"));
        }
        map.insert(key.clone(), (metadata.resource_id.clone(), cancel.clone()));
    }
    let lease = Lease { active, key };
    let metadata = Arc::new(metadata);
    let ctx = metadata
        .resolve(&quarry_assistant::Target {
            db: metadata.resource_id.clone(),
            env: None,
            workspace: None,
        })
        .await?;
    let store = quarry_assistant::ConversationStore::memory()?;
    let doc = if let Some(mut doc) = b.get("conversation").filter(|v| v.is_object()).cloned() {
        doc["db"] = json!(ctx.target.db);
        doc["env"] = json!(ctx.target.env);
        doc["workspace"] = Value::Null;
        store.import_document(doc, &ctx.fingerprint)?
    } else {
        store.create(
            &ctx.target,
            b["sql"].as_str().unwrap_or(""),
            &ctx.fingerprint,
            None,
        )?
    };
    b["conversationId"] = doc["id"].clone();
    b["revision"] = doc["revision"].clone();
    let manager = quarry_assistant::ConfigManager::new(
        PathBuf::from("/__quarry_ephemeral_no_config__"),
        HashMap::new(),
        "zh",
    );
    let assistant = quarry_assistant::AssistantService::new(store, manager, metadata)
        .with_model_config(options.config(&token))?;
    let stream = assistant.start_chat(b, true).await?;
    let stream = stream
        .take_until(cancel.cancelled_owned())
        .map(move |event| {
            let _keep = &lease;
            Ok::<_, std::convert::Infallible>(format!("{event}\n"))
        });
    Response::builder()
        .header(header::CONTENT_TYPE, "application/x-ndjson; charset=utf-8")
        .header(header::CACHE_CONTROL, "no-store")
        .header("X-Accel-Buffering", "no")
        .body(Body::from_stream(stream))
        .map_err(|_| failure(500, "internal", "Cannot start assistant stream"))
}
