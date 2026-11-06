//! Local HTTP workbench. The library facade is separate from transport DTOs.
mod native;
use crate::gui_launch::{
    CHALLENGE_HEADER, Instance, PROOF_HEADER, ProofPurpose, STOP_HEADER, STOP_PATH,
};
use crate::service::{Failure, Host, Result};
use axum::{
    Json, Router,
    body::{Body, to_bytes},
    extract::{Request, State},
    http::{StatusCode, header},
    response::{
        IntoResponse, Response,
        sse::{Event, KeepAlive, Sse},
    },
    routing::any,
};
pub(crate) use native::NativeWorkbench;
use native::cloud_id;
pub use native::{CloudBackend, NativeHistory};
use quarry_core::QueryRequest;
use quarry_runtime::QueryStore;
use serde_json::{Value, json};
use std::{
    collections::BTreeMap,
    convert::Infallible,
    path::{Component, Path},
    sync::Arc,
    time::Duration,
};
use tokio_stream::{StreamExt, wrappers::ReceiverStream};

static UI: include_dir::Dir<'_> =
    include_dir::include_dir!("$CARGO_MANIFEST_DIR/../../src/quarry/web_dist");
#[derive(Clone)]
pub struct WebState {
    pub host: Host,
    pub assistant: Arc<quarry_assistant::AssistantService>,
    pub port: u16,
    pub shutdown: quarry_core::CancellationToken,
    pub(crate) native: Option<Arc<NativeWorkbench>>,
    pub(crate) standalone_updates: bool,
    pub(crate) managed_install: bool,
    pub(crate) gui_instance: Option<Instance>,
}
trait FailureResponse {
    fn into_response(self) -> Response;
}
impl FailureResponse for Failure {
    fn into_response(self) -> Response {
        (
            StatusCode::from_u16(self.status).unwrap_or(StatusCode::BAD_REQUEST),
            Json(json!({"error":self.message,"code":self.code})),
        )
            .into_response()
    }
}
pub fn router(state: WebState) -> Router {
    Router::new()
        .fallback(any(dispatch))
        .layer(axum::extract::DefaultBodyLimit::max(1_048_576))
        .with_state(state)
}
fn local_request(req: &Request, port: u16) -> bool {
    if req.headers().get_all(header::HOST).iter().count() != 1 {
        return false;
    }
    let host = req
        .headers()
        .get(header::HOST)
        .and_then(|s| s.to_str().ok())
        .unwrap_or("");
    if ![
        format!("127.0.0.1:{port}"),
        format!("localhost:{port}"),
        format!("[::1]:{port}"),
    ]
    .contains(&host.to_lowercase())
    {
        return false;
    }
    for field in [header::ORIGIN, header::REFERER] {
        if let Some(origin) = req.headers().get(field) {
            let Ok(origin) = origin.to_str() else {
                return false;
            };
            let Ok(url) = url::Url::parse(origin) else {
                return false;
            };
            if url.scheme() != "http"
                || !matches!(url.host_str(), Some("127.0.0.1" | "localhost" | "[::1]"))
                || url.port_or_known_default() != Some(port)
            {
                return false;
            }
        }
    }
    req.headers()
        .get("sec-fetch-site")
        .is_none_or(|v| v != "cross-site")
}
async fn dispatch(State(state): State<WebState>, req: Request) -> Response {
    if !local_request(&req, state.port) {
        return (
            StatusCode::FORBIDDEN,
            Json(json!({"error":"forbidden: non-local origin"})),
        )
            .into_response();
    }
    let method = req.method().clone();
    let uri = req.uri().clone();
    let path = uri.path();
    if method == axum::http::Method::GET && !path.starts_with("/api/") {
        return static_file(path, state.native.as_deref());
    }
    let challenge = req
        .headers()
        .get(CHALLENGE_HEADER)
        .and_then(|value| uuid::Uuid::try_parse_ascii(value.as_bytes()).ok());
    if method == axum::http::Method::POST && path == STOP_PATH {
        let proof = challenge.and_then(|challenge| {
            state
                .gui_instance?
                .proof(&state.host, challenge, ProofPurpose::Stop)
        });
        if !proof.is_some_and(|proof| {
            req.headers()
                .get(STOP_HEADER)
                .is_some_and(|value| value == proof.as_str())
        }) {
            return StatusCode::FORBIDDEN.into_response();
        }
        state.shutdown.cancel();
        return StatusCode::NO_CONTENT.into_response();
    }
    if method == axum::http::Method::GET
        && path == "/api/version"
        && state
            .native
            .as_ref()
            .is_some_and(|native| native.requires_session())
        && let Some(proof) = challenge
            .and_then(|challenge| {
                state
                    .gui_instance?
                    .proof(&state.host, challenge, ProofPurpose::Discover)
            })
            .and_then(|proof| header::HeaderValue::from_str(&proof).ok())
    {
        return (StatusCode::NO_CONTENT, [(PROOF_HEADER, proof)]).into_response();
    }
    if let Some(native) = &state.native {
        if req
            .headers()
            .get("sec-fetch-site")
            .is_some_and(|site| site == "same-site")
        {
            return native::session_required().into_response();
        }
        if native.requires_session()
            && path == "/api/workbench/session"
            && method == axum::http::Method::POST
        {
            let code = match to_bytes(req.into_body(), 256).await {
                Ok(bytes) => serde_json::from_slice::<Value>(&bytes).ok(),
                Err(_) => None,
            };
            return match code
                .as_ref()
                .and_then(|value| value["code"].as_str())
                .ok_or_else(native::session_required)
                .and_then(|code| native.establish(code, state.port))
            {
                Ok(cookie) => (
                    StatusCode::NO_CONTENT,
                    [
                        (header::SET_COOKIE, cookie),
                        (header::CACHE_CONTROL, "no-store".into()),
                    ],
                )
                    .into_response(),
                Err(error) => error.into_response(),
            };
        }
        if native.requires_session() && !native.authorized(req.headers(), state.port) {
            return native::session_required().into_response();
        }
        if native.requires_session()
            && path == "/api/workbench/session"
            && method == axum::http::Method::GET
        {
            return (
                StatusCode::NO_CONTENT,
                [(header::CACHE_CONTROL, "no-store")],
            )
                .into_response();
        }
    }
    let query: BTreeMap<String, String> =
        url::form_urlencoded::parse(uri.query().unwrap_or("").as_bytes())
            .into_owned()
            .collect();
    let launch_proof = if method == axum::http::Method::GET && path == "/api/version" {
        challenge
            .and_then(|challenge| {
                state
                    .gui_instance?
                    .proof(&state.host, challenge, ProofPurpose::Discover)
            })
            .and_then(|proof| header::HeaderValue::from_str(&proof).ok())
    } else {
        None
    };
    if method == axum::http::Method::GET && path == "/api/events" {
        return events(state);
    }
    let body = if method == axum::http::Method::POST {
        let limit = if state.native.is_some() && path == "/api/personal" {
            // Allow the full SQLite document plus the value/revision envelope.
            quarry_platform::PersonalStore::MAX_DOCUMENT_BYTES + 131_072
        } else {
            262_144
        };
        match to_bytes(req.into_body(), limit).await {
            Ok(bytes) => match serde_json::from_slice::<Value>(&bytes) {
                Ok(value) if value.is_object() => value,
                Ok(_) => return Failure::invalid("Request must be an object").into_response(),
                Err(_) => return Failure::invalid("Invalid JSON request").into_response(),
            },
            Err(_) => {
                return (
                    StatusCode::PAYLOAD_TOO_LARGE,
                    Json(json!({"error":"Request body too large"})),
                )
                    .into_response();
            }
        }
    } else {
        json!({})
    };
    if method == axum::http::Method::POST && path == "/api/assistant/chat" {
        return match state.assistant.start_chat(body, true).await {
            Ok(receiver) => {
                let stream = receiver.map(|event| Ok::<_, Infallible>(format!("{}\n", event)));
                Response::builder()
                    .header(header::CONTENT_TYPE, "application/x-ndjson; charset=utf-8")
                    .header(header::CACHE_CONTROL, "no-store")
                    .header("X-Accel-Buffering", "no")
                    .body(Body::from_stream(stream))
                    .unwrap()
            }
            Err(error) => Failure::from(error).into_response(),
        };
    }
    let result = tokio::select! {
        _ = state.shutdown.cancelled()=>Err(Failure{status:499,code:"cancelled".into(),message:"Workbench is shutting down".into()}),
        result=async {
            if method == axum::http::Method::GET {get(&state,path,&query).await}
            else if method == axum::http::Method::POST {post(&state,path,body).await}
            else {Err(Failure{status:405,code:"method_not_allowed".into(),message:"Method not allowed".into()})}
        }=>result,
    };
    match result {
        Ok(value) => {
            let mut response = Json(value).into_response();
            response.headers_mut().insert(
                header::CACHE_CONTROL,
                header::HeaderValue::from_static("no-store"),
            );
            if let Some(proof) = launch_proof {
                response.headers_mut().insert(PROOF_HEADER, proof);
            }
            response
        }
        Err(error) => error.into_response(),
    }
}
fn static_file(path: &str, native: Option<&NativeWorkbench>) -> Response {
    if path == "/" || path == "/index.html" || path == "/quarry" {
        return (
            StatusCode::TEMPORARY_REDIRECT,
            [(header::LOCATION, "/quarry/")],
        )
            .into_response();
    }
    let Some(relative) = path.strip_prefix("/quarry/") else {
        return StatusCode::NOT_FOUND.into_response();
    };
    let decoded = percent_encoding::percent_decode_str(relative).decode_utf8_lossy();
    if Path::new(decoded.as_ref())
        .components()
        .any(|c| !matches!(c, Component::Normal(_)))
    {
        return StatusCode::NOT_FOUND.into_response();
    }
    let relative = if relative.is_empty() {
        "index.html"
    } else {
        decoded.as_ref()
    };
    match UI.get_file(relative) {
        Some(file) if native.is_some() && relative == "index.html" => {
            let native = native.expect("native workbench");
            let mode = if native.shared_records() {
                let entry = if native.is_standalone() {
                    "quarry"
                } else {
                    "ylab"
                };
                format!(
                    "<meta name=\"quarry-mode\" content=\"native\"><meta name=\"quarry-workbench-entry\" content=\"{entry}\"><meta name=\"quarry-workbench-scope\" content=\"{}\">",
                    native.storage_scope()
                )
            } else {
                String::new()
            };
            let html = String::from_utf8_lossy(file.contents()).replacen(
                "<head>",
                &format!("<head><meta name=\"quarry-federated\" content=\"true\">{mode}"),
                1,
            );
            Response::builder()
                .header(header::CONTENT_TYPE, "text/html; charset=utf-8")
                .header(header::CACHE_CONTROL, "no-store")
                .header("X-Content-Type-Options", "nosniff")
                .header("X-Frame-Options", "DENY")
                .body(Body::from(html))
                .unwrap()
        }
        Some(file) => Response::builder()
            .header(
                header::CONTENT_TYPE,
                mime_guess::from_path(relative)
                    .first_or_octet_stream()
                    .as_ref(),
            )
            .header("X-Content-Type-Options", "nosniff")
            .body(Body::from(file.contents()))
            .unwrap(),
        None => StatusCode::NOT_FOUND.into_response(),
    }
}
fn events(state: WebState) -> Response {
    let (tx, rx) = tokio::sync::mpsc::channel(4);
    tokio::spawn(async move {
        tokio::select! {
         _ = state.shutdown.cancelled()=>(),
         _ = async {
          let mut previous = fingerprint(&state.host);
          let mut next_update=tokio::time::Instant::now();
          let mut announced=None;
          let _ = tx.send(json!({"type":"hello"})).await;
          loop {
              tokio::time::sleep(Duration::from_secs(1)).await;
              if tx.is_closed() {
                  break;
              }
              if tokio::time::Instant::now()>=next_update {
                  next_update=tokio::time::Instant::now()+Duration::from_secs(1800);
                  if let Ok(info)=update_info(&state).await && info["available"]==true && info["latest"].as_str()!=announced.as_deref() {
                      announced=info["latest"].as_str().map(String::from);
                      if tx.send(json!({"type":"update_available"})).await.is_err(){break;}
                  }
              }
              let current = fingerprint(&state.host);
              if current != previous {
                  previous = current;
                  if tx.send(json!({"type":"workspace_changed"})).await.is_err() {
                      break;
                  }
              }
          }
         } => (),
        }
    });
    let stream =
        ReceiverStream::new(rx).map(|v| Ok::<_, Infallible>(Event::default().data(v.to_string())));
    Sse::new(stream)
        .keep_alive(KeepAlive::new().interval(Duration::from_secs(10)))
        .into_response()
}
fn fingerprint(host: &Host) -> String {
    fn walk(p: &Path, out: &mut Vec<String>) {
        if let Ok(meta) = p.metadata() {
            if meta.is_file() {
                out.push(format!(
                    "{}:{:?}:{}",
                    p.display(),
                    meta.modified().ok(),
                    meta.len()
                ));
            } else if let Ok(entries) = p.read_dir() {
                for e in entries.flatten() {
                    if !e.path().is_symlink() {
                        walk(&e.path(), out);
                    }
                }
            }
        }
    }
    let mut values = vec![];
    walk(&host.config_path, &mut values);
    if let Ok(c) = host.catalog() {
        for w in c.workspaces {
            walk(&w.connections_file, &mut values);
            walk(&w.queries_dir, &mut values);
        }
    }
    values.sort();
    values.join("\n")
}
fn arg<'a>(q: &'a BTreeMap<String, String>, key: &str) -> Option<&'a str> {
    q.get(key).map(String::as_str).filter(|s| !s.is_empty())
}
fn req<'a>(v: &'a Value, key: &str) -> Result<&'a str> {
    v[key]
        .as_str()
        .filter(|s| !s.is_empty())
        .ok_or_else(|| Failure::invalid(format!("Missing field {key}")))
}
fn pagination(body: &Value, request: &mut QueryRequest) -> Result<()> {
    fn number(body: &Value, key: &str, default: usize) -> Result<usize> {
        match body.get(key) {
            None | Some(Value::Null) => Ok(default),
            Some(value) => value
                .as_u64()
                .and_then(|n| usize::try_from(n).ok())
                .ok_or_else(|| Failure::invalid(format!("{key} must be a non-negative integer"))),
        }
    }
    request.policy.max_rows = Some(number(body, "maxRows", 500)?).filter(|n| *n > 0);
    request.policy.offset = number(body, "offset", 0)?;
    Ok(())
}

fn local_target(
    host: &Host,
    db: &str,
    env: Option<&str>,
    workspace: Option<&Path>,
    target: Option<&str>,
) -> Result<quarry_runtime::ConnectionRecord> {
    if let Some(id) = env.and_then(|env| env.strip_prefix(native::LOCAL_ROUTE_PREFIX)) {
        if !id.starts_with("local:") || target.is_some_and(|target| target != id) {
            return Err(Failure::invalid(
                "Connection target changed. Refresh the connection list.",
            ));
        }
        let record = crate::federated_cli::resolve_local_target(host, id)?;
        if record.logical_db() != db && record.key != db && !db.starts_with("local-db:") {
            return Err(Failure::invalid(
                "Connection target changed. Refresh the connection list.",
            ));
        }
        return Ok(record);
    }
    if let Some(target) = target {
        let record = crate::federated_cli::resolve_local_target(host, target)?;
        if (record.logical_db() != db && record.key != db) || record.display_env() != env {
            return Err(Failure::invalid(
                "Connection target changed. Refresh the connection list.",
            ));
        }
        return Ok(record);
    }
    host.resolve(db, env, workspace)
}

fn cloud_workbench(state: &WebState) -> Result<&NativeWorkbench> {
    state
        .native
        .as_deref()
        .filter(|native| native.requires_session())
        .ok_or_else(|| Failure::invalid("Cloud connection is unavailable"))
}

async fn record_local(
    native: &NativeWorkbench,
    record: &quarry_runtime::ConnectionRecord,
    sql: &str,
    request_id: Option<&str>,
) {
    let id = crate::federated_cli::local_target_id(record);
    let target = json!({"db":record.logical_db(),"env":record.display_env(),"placement":"local","workspace":record.workspace,"key":record.key});
    native
        .history
        .record(target, Some(&id), sql, request_id)
        .await;
}

async fn record_cloud(
    native: &NativeWorkbench,
    db: &str,
    env: Option<&str>,
    id: &str,
    sql: &str,
    request_id: Option<&str>,
) {
    let target = json!({"db":db,"env":env,"placement":"remote"});
    native
        .history
        .record(target, Some(id), sql, request_id)
        .await;
}

async fn get(state: &WebState, path: &str, q: &BTreeMap<String, String>) -> Result<Value> {
    let host = &state.host;
    match path {
        "/api/version" => Ok(json!({"name":"Quarry","version":env!("CARGO_PKG_VERSION")})),
        "/api/connections" => {
            let local = host.connections().await?;
            match &state.native {
                Some(native) => Ok(native.connections(local).await),
                None => Ok(local),
            }
        }
        "/api/history"
            if state
                .native
                .as_ref()
                .is_some_and(|native| native.shared_records()) =>
        {
            state.native.as_ref().unwrap().read_history().await
        }
        "/api/personal"
            if state
                .native
                .as_ref()
                .is_some_and(|native| native.shared_records()) =>
        {
            state.native.as_ref().unwrap().workbench(None).await
        }
        "/api/workspaces" => host.workspaces(),
        "/api/queries" => queries(host),
        "/api/tables" | "/api/columns" | "/api/health" | "/api/conninfo" | "/api/inspect" => {
            let db = arg(q, "db").ok_or_else(|| Failure::invalid("Missing database"))?;
            if let Some(id) = cloud_id(arg(q, "env")) {
                return cloud_workbench(state)?.get(path, id, q).await;
            }
            let record = local_target(
                host,
                db,
                arg(q, "env"),
                arg(q, "workspace").map(Path::new),
                arg(q, "target"),
            )?;
            if path == "/api/conninfo" {
                return host.info(&record, arg(q, "reveal") == Some("1"));
            }
            if path != "/api/inspect" {
                return metadata(state, path, q, &record).await;
            }
            host.metadata(
                &record,
                quarry_service::MetadataOperation::InspectKey(
                    arg(q, "key")
                        .ok_or_else(|| Failure::invalid("Missing key"))?
                        .into(),
                ),
                None,
                quarry_core::CancellationToken::new(),
            )
            .await
        }
        "/api/keepalive" => crate::keepalive(host, None).await,
        "/api/update" => update_info(state).await,
        "/api/changelog" => Ok(changelog()),
        "/api/assistant/config" => Ok(state.assistant.config().status()),
        "/api/assistant/preferences" => Ok(state.assistant.config().preferences()?),
        "/api/assistant/conversations" => {
            if let Some(id) = arg(q, "id") {
                Ok(state.assistant.get_conversation(id).await?)
            } else {
                Ok(state.assistant.list_conversations().await?)
            }
        }
        _ => Err(Failure {
            status: 404,
            code: "not_found".into(),
            message: "Not found".into(),
        }),
    }
}
async fn update_info(state: &WebState) -> Result<Value> {
    if !state.standalone_updates || std::env::var("QUARRY_UPDATE_CHECK").as_deref() == Ok("0") {
        return Ok(json!({"current":env!("CARGO_PKG_VERSION"),"latest":null,"available":false}));
    }
    let mut value = standalone_update_info(&state.host).await;
    value["managed"] = json!(state.managed_install);
    Ok(value)
}

async fn standalone_update_info(host: &Host) -> Value {
    if let Some(value) = host
        .cache
        .get("release-update", env!("CARGO_PKG_VERSION"), Some(1800))
    {
        return value;
    }
    let result = reqwest::Client::new()
        .get("https://api.github.com/repos/yiminspace/quarry/releases/latest")
        .header("user-agent", "quarry-rust")
        .timeout(Duration::from_secs(5))
        .send()
        .await;
    let latest = match result {
        Ok(r) if r.status().is_success() => r.json::<Value>().await.ok().and_then(|v| {
            v["tag_name"]
                .as_str()
                .map(|s| s.trim_start_matches('v').to_string())
        }),
        _ => None,
    };
    let value = json!({"current":env!("CARGO_PKG_VERSION"),"available":latest.as_ref().is_some_and(|v|version_gt(v,env!("CARGO_PKG_VERSION"))),"latest":latest});
    host.cache
        .put("release-update", env!("CARGO_PKG_VERSION"), &value);
    value
}
async fn post(state: &WebState, path: &str, body: Value) -> Result<Value> {
    let host = &state.host;
    match path {
        "/api/personal"
            if state
                .native
                .as_ref()
                .is_some_and(|native| native.shared_records()) =>
        {
            state.native.as_ref().unwrap().workbench(Some(body)).await
        }
        "/api/query" => {
            let db = req(&body, "db")?;
            let env = body["env"].as_str();
            let sql = req(&body, "sql")?;
            let request_id = body["requestId"].as_str();
            if let Some(id) = cloud_id(env) {
                let native = cloud_workbench(state)?;
                let result = native.post(path, id, body.clone()).await?;
                record_cloud(
                    native,
                    db,
                    env,
                    id,
                    sql,
                    result["requestId"].as_str().or(request_id),
                )
                .await;
                return Ok(result);
            }
            let record = local_target(
                host,
                db,
                env,
                body["workspace"].as_str().map(Path::new),
                body["target"].as_str(),
            )?;
            let mut request = QueryRequest::new(sql);
            pagination(&body, &mut request)?;
            let result = host.execute(&record, request).await?.to_legacy_json();
            if let Some(native) = &state.native {
                record_local(native, &record, sql, request_id).await;
            }
            Ok(result)
        }
        "/api/run" => {
            let store = QueryStore::new(host.catalog()?);
            let query = if let Some(id) = body["queryId"].as_str() {
                store
                    .list()?
                    .into_iter()
                    .find(|q| query_id(q) == id)
                    .ok_or_else(|| Failure::invalid("Saved query no longer exists"))?
            } else {
                store.describe(
                    req(&body, "name")?,
                    body["workspace"].as_str().map(Path::new),
                )?
            };
            let env = body["env"].as_str();
            let params: BTreeMap<String, String> = body["params"]
                .as_object()
                .map(|v| {
                    v.iter()
                        .map(|(k, v)| {
                            (
                                k.clone(),
                                v.as_str()
                                    .map(String::from)
                                    .unwrap_or_else(|| v.to_string()),
                            )
                        })
                        .collect()
                })
                .unwrap_or_default();
            let params = query.parameters(&params)?;
            if let Some(id) = cloud_id(env) {
                let native = cloud_workbench(state)?;
                let resource = native.resource(id)?;
                let (db, sql) =
                    resource.saved_sql(host, &query.db, &query.workspace, &query.sql, &params)?;
                let mut out = native.post("/api/query", id, json!({"sql":sql,"maxRows":body["maxRows"],"offset":body["offset"],"requestId":body["requestId"]})).await?;
                record_cloud(native, &db, env, id, &query.sql, out["requestId"].as_str()).await;
                // Match local execution's parameter privacy at browser persistence boundaries.
                if !params.is_empty() {
                    out["sql"] = json!(query.sql);
                }
                out["db"] = json!(
                    body["db"]
                        .as_str()
                        .filter(|db| db.starts_with("local-db:"))
                        .unwrap_or(&db)
                );
                out["env"] = json!(env);
                return Ok(out);
            }
            let record = match body["target"].as_str() {
                Some(target) => local_target(host, &query.db, env, None, Some(target))?,
                None if env.is_some_and(|env| env.starts_with(native::LOCAL_ROUTE_PREFIX)) => {
                    local_target(host, &query.db, env, None, None)?
                }
                None => host.resolve_related(&query.db, env, &query.workspace)?,
            };
            let mut request = QueryRequest::new(&query.sql);
            request.parameters = params;
            pagination(&body, &mut request)?;
            let mut out = host.execute(&record, request).await?.to_legacy_json();
            out["db"] = json!(
                body["db"]
                    .as_str()
                    .filter(|db| db.starts_with("local-db:"))
                    .unwrap_or(record.logical_db())
            );
            out["env"] = json!(
                env.filter(|env| env.starts_with(native::LOCAL_ROUTE_PREFIX))
                    .or_else(|| record.display_env())
            );
            if let Some(native) = &state.native {
                record_local(
                    native,
                    &record,
                    out["sql"].as_str().unwrap_or(&query.sql),
                    body["requestId"].as_str(),
                )
                .await;
            }
            Ok(out)
        }
        "/api/cancel" if state.native.is_some() => {
            let native = cloud_workbench(state)?;
            let id = cloud_id(body["env"].as_str())
                .ok_or_else(|| Failure::invalid("Missing cloud query target"))?
                .to_owned();
            req(&body, "requestId")?;
            native.post(path, &id, body).await
        }
        "/api/record-link" => {
            if let Some(id) = cloud_id(body["env"].as_str()) {
                return cloud_workbench(state)?.post(path, id, body.clone()).await;
            }
            let record = local_target(
                host,
                req(&body, "db")?,
                body["env"].as_str(),
                None,
                body["target"].as_str(),
            )?;
            Ok(host
                .metadata(
                    &record,
                    quarry_service::MetadataOperation::RecordLink {
                        query: req(&body, "sql")?.into(),
                        row: body["row"].clone(),
                    },
                    None,
                    quarry_core::CancellationToken::new(),
                )
                .await
                .unwrap_or(json!({"reason":"metadata_unavailable"})))
        }
        "/api/local/up" => host.local_up(req(&body, "db")?).await,
        "/api/local/sync" => {
            let result = host
                .local_sync(req(&body, "db")?, body["from"].as_str().unwrap_or("dev"))
                .await;
            host.cache.clear();
            result
        }
        "/api/workspaces/add" | "/api/workspaces/remove" => {
            let config = quarry_runtime::RuntimeConfig::load(&host.config_path)?;
            if path.ends_with("/add") {
                config.add_workspace(Path::new(req(&body, "dir")?))?;
            } else {
                config.remove_workspace(Path::new(req(&body, "dir")?))?;
            }
            host.workspaces()
        }
        "/api/keepalive/up" => crate::keepalive(host, Some(true)).await,
        "/api/keepalive/down" => crate::keepalive(host, Some(false)).await,
        "/api/assistant/conversations" => Ok(state.assistant.create_conversation(body).await?),
        "/api/assistant/cancel" => Ok(state
            .assistant
            .cancel(req(&body, "conversationId")?, req(&body, "requestId")?)),
        "/api/assistant/conversations/update" => {
            Ok(state.assistant.update_conversation(body).await?)
        }
        "/api/assistant/conversations/apply" => {
            Ok(state.assistant.apply_conversation(body).await?)
        }
        "/api/assistant/conversations/restore" => {
            Ok(state.assistant.restore_conversation(body).await?)
        }
        _ => Err(Failure {
            status: 404,
            code: "not_found".into(),
            message: "Not found".into(),
        }),
    }
}
async fn metadata(
    state: &WebState,
    path: &str,
    q: &BTreeMap<String, String>,
    record: &quarry_runtime::ConnectionRecord,
) -> Result<Value> {
    let host = &state.host;
    let identity = crate::service::metadata_identity(
        record,
        quarry_runtime::RuntimeConfig::load(&host.config_path)?.enabled("proxy", &record.workspace),
    );
    let key = format!(
        "{path}:{}:{}:{}",
        record.workspace.display(),
        record.key,
        arg(q, "table").unwrap_or("")
    );
    let health_key = format!("/api/health:{}:{}:", record.workspace.display(), record.key);
    let health_ttl = std::env::var("QUARRY_HEALTH_TTL")
        .ok()
        .and_then(|s| s.parse::<u64>().ok())
        .unwrap_or(120);
    if arg(q, "fresh") != Some("1")
        && let Some(mut value) = host.cache.get(
            &key,
            &identity,
            if path == "/api/health" {
                Some(health_ttl)
            } else {
                None
            },
        )
    {
        if path == "/api/tables" {
            value["_cached"] = json!(true);
        }
        return Ok(value);
    }
    if path == "/api/health" && arg(q, "cached") == Some("1") {
        return Ok(json!({"ok":null}));
    }
    let operation = match path {
        "/api/health" => quarry_service::MetadataOperation::Ping,
        "/api/tables" => quarry_service::MetadataOperation::Tables,
        "/api/columns" => {
            quarry_service::MetadataOperation::Columns(arg(q, "table").unwrap_or("").into())
        }
        _ => unreachable!(),
    };
    let result = host
        .metadata(
            record,
            operation,
            if path == "/api/health" {
                Some(6000)
            } else {
                None
            },
            quarry_core::CancellationToken::new(),
        )
        .await
        .map(|value| match path {
            "/api/health" => json!({"ok":true}),
            "/api/columns" => json!({"columns":value["columns"],"types":value["types"]}),
            _ => value,
        });
    let value = match result {
        Ok(value) => {
            host.cache.put(&health_key, &identity, &json!({"ok":true}));
            value
        }
        Err(error) if path == "/api/health" => json!({"ok":false,"error":error.message}),
        Err(_) if path == "/api/columns" => return Ok(json!({"columns":[],"types":{}})),
        Err(error) => return Err(error),
    };
    host.cache.put(&key, &identity, &value);
    Ok(value)
}
fn query_id(q: &quarry_runtime::SavedQuery) -> String {
    fn ascii(value: &str) -> String {
        let value = serde_json::to_string(value).expect("JSON string");
        let mut out = String::new();
        for ch in value.chars() {
            if ch.is_ascii() {
                out.push(ch);
            } else {
                for unit in ch.encode_utf16(&mut [0; 2]) {
                    out.push_str(&format!("\\u{unit:04x}"));
                }
            }
        }
        out
    }
    format!(
        "[{}, {}]",
        ascii(&crate::service::display_path(&q.workspace)),
        ascii(
            &q.path
                .strip_prefix(q.workspace.join("queries"))
                .unwrap_or(&q.path)
                .to_string_lossy()
                .replace('\\', "/")
        )
    )
}
fn queries(host: &Host) -> Result<Value> {
    let inventory = QueryStore::new(host.catalog()?).scan()?;
    crate::warn_query_issues(&inventory);
    Ok(json!(inventory.queries.iter().map(|q|json!({"name":q.name,"db":q.db,"desc":q.desc,"sql":q.sql,"ws":crate::service::display_path(&q.workspace),"queryId":query_id(q),"params":q.params})).collect::<Vec<_>>()))
}
fn version_gt(a: &str, b: &str) -> bool {
    semver::Version::parse(a.trim_start_matches('v'))
        .ok()
        .zip(semver::Version::parse(b.trim_start_matches('v')).ok())
        .is_some_and(|(a, b)| a > b)
}
fn changelog() -> Value {
    parse_changelog(include_str!("../../../CHANGELOG.md"))
}
fn parse_changelog(text: &str) -> Value {
    let heading = regex::Regex::new(
        r"^## \[?v?(\d+\.\d+\.\d+[\w.-]*)\]?\s*[(—-]\s*(\d{4}-\d{2}-\d{2})\)?\s*$",
    )
    .expect("changelog heading");
    let footer = regex::Regex::new(r"\s*\((?:\[[^\]]+\]\([^)]+\)(?:,\s*)?)+\)\s*$")
        .expect("changelog footer");
    fn flush(current: &mut Option<Value>, entry: &mut Vec<String>, footer: &regex::Regex) {
        if let Some(current) = current.as_mut()
            && !entry.is_empty()
        {
            let joined = entry.join(" ");
            let clean = footer.replace(&joined, "").replace("**", "");
            if !clean.trim().is_empty() {
                current["entries"]
                    .as_array_mut()
                    .unwrap()
                    .push(json!(clean.trim()));
            }
        }
        entry.clear();
    }
    let mut items = vec![];
    let mut current = None;
    let mut entry = vec![];
    for line in text.lines() {
        if let Some(capture) = heading.captures(line) {
            flush(&mut current, &mut entry, &footer);
            if let Some(value) = current.take() {
                items.push(value);
            }
            if items.len() >= 20 {
                break;
            }
            current = Some(json!({"version":capture[1],"date":capture[2],"entries":[]}));
        } else if line.starts_with("## ") {
            flush(&mut current, &mut entry, &footer);
            if let Some(value) = current.take() {
                items.push(value);
            }
        } else if current.is_some() {
            if let Some(value) = line.strip_prefix("- ") {
                flush(&mut current, &mut entry, &footer);
                entry.push(value.trim().to_owned());
            } else if line.trim().is_empty() {
                flush(&mut current, &mut entry, &footer);
            } else if !line.starts_with('#') {
                entry.push(line.trim().to_owned());
            }
        }
    }
    flush(&mut current, &mut entry, &footer);
    if let Some(value) = current {
        items.push(value);
    }
    items.truncate(20);
    json!(items)
}

pub(crate) fn web_asset(path: &str) -> Option<(&'static [u8], String)> {
    let path = path.trim_start_matches('/');
    let path = if path.is_empty() { "index.html" } else { path };
    if Path::new(path)
        .components()
        .any(|c| !matches!(c, Component::Normal(_)))
    {
        return None;
    }
    UI.get_file(path).map(|file| {
        (
            file.contents(),
            mime_guess::from_path(path)
                .first_or_octet_stream()
                .to_string(),
        )
    })
}

#[cfg(test)]
mod native_http_tests {
    use super::*;
    use http_body_util::BodyExt;
    use tower::ServiceExt;

    fn fixture() -> (tempfile::TempDir, WebState) {
        let dir = tempfile::tempdir().unwrap();
        let db = rusqlite::Connection::open(dir.path().join("notes.db")).unwrap();
        db.execute_batch("CREATE TABLE entries (id INTEGER PRIMARY KEY, note TEXT);INSERT INTO entries VALUES (1,'one'),(2,'two'),(3,'three')").unwrap();
        std::fs::write(
            dir.path().join("connections.toml"),
            "[notes]\nurl='sqlite:///notes.db'\n",
        )
        .unwrap();
        let host = Host::new(
            dir.path().join("config.toml"),
            vec![dir.path().to_path_buf()],
            dir.path().join("state"),
        );
        let config = quarry_assistant::ConfigManager::new(
            host.config_path.clone(),
            Default::default(),
            "en",
        );
        let assistant = Arc::new(quarry_assistant::AssistantService::new(
            quarry_assistant::ConversationStore::new(dir.path().join("history.sqlite3")),
            config,
            Arc::new(host.clone()),
        ));
        (
            dir,
            WebState {
                host,
                assistant,
                port: 8765,
                shutdown: quarry_core::CancellationToken::new(),
                native: None,
                standalone_updates: true,
                managed_install: false,
                gui_instance: None,
            },
        )
    }
    #[tokio::test]
    async fn embedded_workbench_hides_standalone_updates() {
        let (_dir, mut state) = fixture();
        state.standalone_updates = false;
        state.host.cache.put(
            "release-update",
            env!("CARGO_PKG_VERSION"),
            &json!({"current":env!("CARGO_PKG_VERSION"),"latest":"99.0.0","available":true}),
        );
        let (status, info) = send(&state, "/api/update", None, "localhost:8765", None).await;
        assert_eq!(status, 200);
        assert_eq!(info["available"], false);
        assert_eq!(info["latest"], Value::Null);
    }
    #[test]
    fn released_notes_parse_both_formats_and_fold_wrapped_bullets() {
        let value = parse_changelog(
            "## [Unreleased]\n- pending\n## v1.11.0 (2026-10-20)\n### Features\n- **SQL** assistant\n  preserves history ([#1](https://example.invalid/1), [`abc`](https://example.invalid/c))\n\n## [1.10.0] — 2026-10-19\n- previous\n",
        );
        assert_eq!(
            value[0],
            json!({"version":"1.11.0","date":"2026-10-20","entries":["SQL assistant preserves history"]})
        );
        assert_eq!(value[1]["version"], "1.10.0");
        assert!(
            changelog()
                .as_array()
                .unwrap()
                .iter()
                .any(|v| v["version"] == "1.11.0")
        );
        assert!(version_gt("2.0.0", "2.0.0-alpha.1"));
    }
    #[tokio::test]
    async fn saved_query_id_and_omitted_environment_preserve_browser_state() {
        let (dir, state) = fixture();
        std::fs::create_dir_all(dir.path().join("queries")).unwrap();
        std::fs::write(
            dir.path().join("queries/recent.sql"),
            "-- @name: recent\n-- @db: notes\nSELECT * FROM entries\n",
        )
        .unwrap();
        std::fs::write(dir.path().join("queries/broken.sql"), b"\xff").unwrap();
        let queries = send(&state, "/api/queries", None, "localhost:8765", None)
            .await
            .1;
        assert_eq!(
            queries.as_array().unwrap().len(),
            1,
            "A malformed query must not hide a valid one"
        );
        let id = queries[0]["queryId"].as_str().unwrap();
        assert!(id.contains(", \"recent.sql\""));
        let result = send(
            &state,
            "/api/run",
            Some(json!({"queryId":id})),
            "localhost:8765",
            None,
        )
        .await;
        assert_eq!(result.0, 200);
        assert_eq!(result.1["rowCount"], 3);
        assert_eq!(result.1["env"], Value::Null);
    }
    #[tokio::test]
    async fn cloud_saved_query_sends_parameters_but_keeps_them_out_of_personal_sql() {
        struct TestCloud;
        #[async_trait::async_trait]
        impl CloudBackend for TestCloud {
            async fn request(
                &self,
                _method: &str,
                path: &str,
                _pairs: &[(&str, String)],
                body: Option<Value>,
            ) -> Result<Value> {
                match path {
                    "/api/platform" => Ok(json!({"resources":[
                        {"id":"notes-dev","project":"demo","db":"notes","env":"dev","engine":"sqlite","production":true}
                    ]})),
                    "/api/query" => {
                        let body = body.unwrap();
                        assert_eq!(body["resourceId"], "notes-dev");
                        assert_eq!(body["sql"], "SELECT 'private-value'");
                        Ok(json!({"sql":body["sql"],"requestId":"saved-run"}))
                    }
                    _ => panic!("Unexpected cloud path"),
                }
            }
        }
        let (dir, mut state) = fixture();
        let native = NativeWorkbench::new(
            Arc::new(TestCloud),
            NativeHistory::new(
                dir.path().join("personal.sqlite3"),
                "cloud".into(),
                "owner".into(),
            ),
        );
        native
            .connections(state.host.connections().await.unwrap())
            .await;
        state.native = Some(native.clone());
        std::fs::create_dir_all(dir.path().join("queries")).unwrap();
        std::fs::write(
            dir.path().join("queries/private.sql"),
            "-- @name: private\n-- @db: notes\n-- @param: key (text)\nSELECT :'key'\n",
        )
        .unwrap();
        let output = post(
            &state,
            "/api/run",
            json!({
                "name":"private","env":"cloud:notes-dev","params":{"key":"private-value"}
            }),
        )
        .await
        .unwrap();
        assert_eq!(output["sql"], "SELECT :'key'");
        let history = native.read_history().await.unwrap();
        assert_eq!(history["history"][0]["sql"], "SELECT :'key'");
    }
    #[tokio::test]
    async fn native_personal_http_preserves_sqlite_document_budget_and_revision() {
        struct UnusedCloud;
        #[async_trait::async_trait]
        impl CloudBackend for UnusedCloud {
            async fn request(
                &self,
                _method: &str,
                _path: &str,
                _pairs: &[(&str, String)],
                _body: Option<Value>,
            ) -> Result<Value> {
                panic!("Personal storage must work offline");
            }
        }

        let (dir, mut state) = fixture();
        let native = NativeWorkbench::new(
            Arc::new(UnusedCloud),
            NativeHistory::new(
                dir.path().join("personal.sqlite3"),
                "local".into(),
                "owner".into(),
            ),
        );
        let launch = native.launch_url("http://localhost:8765/quarry/");
        let code = launch.split_once("#quarry_workbench=").unwrap().1;
        let session = native.establish(code, state.port).unwrap();
        let cookie = session.split_once(';').unwrap().0;
        state.native = Some(native);
        let app = router(state);
        let save = |revision: u64, value: Value| {
            app.clone().oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/api/personal")
                    .header("host", "localhost:8765")
                    .header("cookie", cookie)
                    .header("content-type", "application/json")
                    .body(Body::from(
                        json!({"value":value,"revision":revision}).to_string(),
                    ))
                    .unwrap(),
            )
        };
        let budget = quarry_platform::PersonalStore::MAX_DOCUMENT_BYTES;
        let overhead = json!({"values":{"draft":""}}).to_string().len();
        let limit = budget - overhead;
        let document = |size| json!({"values":{"draft":"x".repeat(size)}});

        assert_eq!(
            save(0, document(limit)).await.unwrap().status(),
            StatusCode::OK
        );
        let oversized = save(1, document(limit + 1)).await.unwrap();
        assert_eq!(oversized.status(), StatusCode::PAYLOAD_TOO_LARGE);
        let body = oversized.into_body().collect().await.unwrap().to_bytes();
        assert_eq!(
            serde_json::from_slice::<Value>(&body).unwrap()["code"],
            "too_large"
        );
        assert_eq!(
            save(1, document(32)).await.unwrap().status(),
            StatusCode::OK
        );
    }
    async fn send(
        state: &WebState,
        path: &str,
        body: Option<Value>,
        host: &str,
        origin: Option<&str>,
    ) -> (u16, Value) {
        let mut req = Request::builder().uri(path).header("host", host);
        if let Some(origin) = origin {
            req = req.header("origin", origin);
        }
        let req = if let Some(body) = body {
            req.method("POST")
                .header("content-type", "application/json")
                .body(Body::from(body.to_string()))
        } else {
            req.body(Body::empty())
        }
        .unwrap();
        let response = router(state.clone()).oneshot(req).await.unwrap();
        let status = response.status().as_u16();
        let data = response.into_body().collect().await.unwrap().to_bytes();
        (status, serde_json::from_slice(&data).unwrap_or(Value::Null))
    }
    #[tokio::test]
    async fn local_boundary_and_native_query_policy() {
        let (_dir, state) = fixture();
        for (host, origin) in [
            ("evil.example:8765", None),
            ("127.0.0.1:8765", Some("https://evil.example")),
            ("127.0.0.1:9999", None),
        ] {
            assert_eq!(
                send(&state, "/api/connections", None, host, origin).await.0,
                403
            );
        }
        let (status,value)=send(&state,"/api/query",Some(json!({"db":"notes","sql":"SELECT * FROM entries ORDER BY id","maxRows":1,"offset":1})),"127.0.0.1:8765",Some("http://127.0.0.1:8765")).await;
        assert_eq!(status, 200);
        assert_eq!(value["rows"][0]["note"], "two");
        assert_eq!(value["truncated"], true);
        assert_eq!(
            send(
                &state,
                "/api/query",
                Some(json!({"db":"notes","sql":"DELETE FROM entries"})),
                "127.0.0.1:8765",
                None
            )
            .await
            .0,
            403
        );
        assert_eq!(
            send(
                &state,
                "/api/query",
                Some(json!({"db":"notes","sql":"SELECT 1","maxRows":-1})),
                "127.0.0.1:8765",
                None
            )
            .await
            .0,
            400
        );
    }
    #[tokio::test]
    async fn health_cache_survives_restart_and_file_replacement_invalidates_it() {
        let (dir, state) = fixture();
        assert_eq!(
            send(
                &state,
                "/api/health?db=notes&cached=1",
                None,
                "localhost:8765",
                None
            )
            .await
            .1,
            json!({"ok":null})
        );
        assert_eq!(
            send(&state, "/api/health?db=notes", None, "localhost:8765", None)
                .await
                .1,
            json!({"ok":true})
        );
        let mut restarted = state.clone();
        restarted.host = Host::new(
            state.host.config_path.clone(),
            state.host.explicit.clone(),
            state.host.state_dir.clone(),
        );
        assert_eq!(
            send(
                &restarted,
                "/api/health?db=notes&cached=1",
                None,
                "localhost:8765",
                None
            )
            .await
            .1,
            json!({"ok":true})
        );
        std::fs::remove_file(dir.path().join("notes.db")).unwrap();
        assert_eq!(
            send(
                &state,
                "/api/health?db=notes&cached=1",
                None,
                "localhost:8765",
                None
            )
            .await
            .1,
            json!({"ok":null})
        );
        assert_eq!(
            send(&state, "/api/health?db=notes", None, "localhost:8765", None)
                .await
                .1["ok"],
            false
        );
        assert!(!dir.path().join("notes.db").exists());
    }
    #[tokio::test]
    async fn metadata_uses_gui_shape_and_refreshes_after_schema_changes() {
        let (dir, state) = fixture();
        let path = "/api/tables?db=notes";
        assert_eq!(
            send(&state, path, None, "localhost:8765", None).await.1["_cached"],
            false
        );
        assert_eq!(
            send(&state, path, None, "localhost:8765", None).await.1["_cached"],
            true
        );
        let value = send(
            &state,
            "/api/columns?db=notes&table=entries",
            None,
            "localhost:8765",
            None,
        )
        .await
        .1;
        assert_eq!(value["columns"], json!(["id", "note"]));
        assert_eq!(value["types"]["note"], "TEXT");
        rusqlite::Connection::open(dir.path().join("notes.db"))
            .unwrap()
            .execute_batch("CREATE TABLE \"odd table-name\" (val TEXT)")
            .unwrap();
        let value = send(&state, path, None, "localhost:8765", None).await.1;
        assert!(
            value["tables"]
                .as_array()
                .unwrap()
                .contains(&json!("odd table-name"))
        );
        assert_eq!(
            send(
                &state,
                "/api/columns?db=notes&table=odd%20table-name",
                None,
                "localhost:8765",
                None
            )
            .await
            .1["columns"],
            json!(["val"])
        );
    }
    #[tokio::test]
    async fn static_root_and_app_path_redirect_without_path_escape() {
        let (_dir, state) = fixture();
        for path in ["/", "/quarry", "/index.html"] {
            let response = router(state.clone())
                .oneshot(
                    Request::builder()
                        .uri(path)
                        .header("host", "localhost:8765")
                        .body(Body::empty())
                        .unwrap(),
                )
                .await
                .unwrap();
            assert_eq!(response.status(), StatusCode::TEMPORARY_REDIRECT);
            assert_eq!(response.headers()["location"], "/quarry/");
        }
        assert_eq!(
            send(
                &state,
                "/quarry/%2e%2e/Cargo.toml",
                None,
                "localhost:8765",
                None
            )
            .await
            .0,
            404
        );
        assert_eq!(
            send(&state, "/quarry/", None, "localhost:8765", None)
                .await
                .0,
            200
        );
    }
}
