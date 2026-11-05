pub mod auth;
pub mod config;
mod updates;
use auth::{Auth, fail};
use axum::{
    Json, Router,
    body::{Body, to_bytes},
    extract::{Request, State},
    http::{Method, StatusCode, Uri, header},
    response::{IntoResponse, Response},
    routing::any,
};
use quarry_platform::{Authorizer, HostedState};
use quarry_service::QuarryService;
use serde_json::{Value, json};
use std::sync::Arc;
use tower::ServiceExt;
static UI: include_dir::Dir<'_> =
    include_dir::include_dir!("$CARGO_MANIFEST_DIR/../../src/quarry/web_dist");
#[derive(Clone)]
struct App {
    auth: Arc<Auth>,
    hosted: Router,
    updates: Arc<updates::UpdateCheck>,
    version: String,
}
pub fn router(config: config::Config, version: String) -> Result<Router, quarry_service::Failure> {
    config
        .validate()
        .map_err(|message| fail(400, "invalid", &message))?;
    let service = Arc::new(QuarryService::new(
        config.config_path,
        config.workspaces,
        config.state_dir.join("runtime"),
    ));
    let auth = Arc::new(Auth::new(config.auth, config.state_dir)?);
    let mut hosted = HostedState::new(service, auth.clone(), config.resources)?;
    if let (Some(base_url), Some(model)) = (config.assistant_base_url, config.assistant_model) {
        hosted = hosted.with_assistant(quarry_platform::AssistantOptions { base_url, model })?;
    }
    Ok(Router::new().fallback(any(dispatch)).with_state(App {
        auth,
        hosted: quarry_platform::hosted_router(hosted),
        updates: Arc::new(updates::UpdateCheck::new()),
        version,
    }))
}
fn json_response(value: Value) -> Response {
    let mut r = Json(value).into_response();
    r.headers_mut().insert(
        header::CACHE_CONTROL,
        header::HeaderValue::from_static("no-store"),
    );
    r
}
fn error(e: quarry_service::Failure) -> Response {
    let mut r = json_response(json!({"error":{"code":e.code,"message":e.message}}));
    *r.status_mut() = StatusCode::from_u16(e.status).unwrap_or(StatusCode::INTERNAL_SERVER_ERROR);
    r
}
fn bearer(req: &Request) -> Result<String, quarry_service::Failure> {
    req.headers()
        .get(header::AUTHORIZATION)
        .and_then(|v| v.to_str().ok())
        .and_then(|s| s.strip_prefix("Bearer "))
        .map(String::from)
        .ok_or_else(|| fail(401, "unauthorized", "Authentication required"))
}
async fn body(req: Request) -> Result<Value, quarry_service::Failure> {
    let bytes = to_bytes(req.into_body(), 16384)
        .await
        .map_err(|_| fail(413, "too_large", "Request too large"))?;
    serde_json::from_slice(&bytes).map_err(|_| fail(400, "invalid", "Invalid JSON request"))
}
fn text(v: &Value, key: &str) -> Result<String, quarry_service::Failure> {
    v[key]
        .as_str()
        .map(String::from)
        .ok_or_else(|| fail(400, "invalid", "Required text field missing"))
}
async fn dispatch(State(app): State<App>, req: Request) -> Response {
    let path = req.uri().path().to_owned();
    let method = req.method().clone();
    let result=async {
        if path=="/health" && method==Method::GET {return Ok(json_response(json!({"status":"ok","service":"quarry","version":app.version,"packageVersion":env!("CARGO_PKG_VERSION")})));}
        if path=="/api/hosted/config" && method==Method::GET {return Ok(json_response(app.auth.discovery()));}
        if path=="/api/hosted/update" && method==Method::GET {return Ok(json_response(app.updates.status().await));}
        if path=="/api/db/authorize" && method==Method::GET {
            let query=url::form_urlencoded::parse(req.uri().query().unwrap_or("").as_bytes()).collect::<std::collections::HashMap<_,_>>();
            let permission=query.get("permission").map(|s|s.as_ref()).unwrap_or("lab:db:read");
            if !["lab:db:read","lab:db:local","lab:db:schema:export","lab:ai:invoke"].contains(&permission){return Err(fail(403,"forbidden","Unknown permission"));}
            let principal=app.auth.authorize(&bearer(&req)?,permission).await?;
            return Ok(json_response(serde_json::to_value(principal).map_err(|_|fail(500,"internal","Cannot encode identity"))?));
        }
        if path=="/api/hosted/auth/me" && method==Method::GET { let p=app.auth.authorize(&bearer(&req)?,"lab:db:read").await?;return Ok(json_response(json!({"user":{"id":p.id},"permissions":p.permissions,"roles":p.roles}))); }
        if path=="/api/hosted/auth/handoff" && method==Method::POST {return app.auth.handoff(&bearer(&req)?).await.map(json_response);}
        if path.starts_with("/api/hosted/auth/") && method==Method::POST {
            let v=body(req).await?;
            let result=match path.as_str(){
                "/api/hosted/auth/login"=>app.auth.login(text(&v,"username")?,text(&v,"password")?).await,
                "/api/hosted/auth/refresh"=>app.auth.refresh(&text(&v,"refresh_token")?),
                "/api/hosted/auth/logout"=>app.auth.logout(&text(&v,"refresh_token")?),
                "/api/hosted/auth/handoff/consume"=>app.auth.consume_handoff(&text(&v,"code")?),
                _=>Err(fail(404,"not_found","Unknown authentication endpoint")),
            };
            return result.map(json_response);
        }
        if path.starts_with("/api/") {
            let mut req=req;
            if let Some(tail)=path.strip_prefix("/api/db/") {let mapped=format!("/api/{tail}{}",req.uri().query().map(|q|format!("?{q}")).unwrap_or_default());*req.uri_mut()=mapped.parse::<Uri>().map_err(|_|fail(400,"invalid","Invalid API path"))?;}
            return Ok(app.hosted.oneshot(req).await.unwrap_or_else(|never|match never{}));
        }
        if method!=Method::GET && method!=Method::HEAD {return Err(fail(405,"method_not_allowed","Method not allowed"));}
        if path=="/" || path=="/quarry" {return Ok(axum::response::Redirect::temporary("/quarry/").into_response());}
        let asset=if path=="/quarry/" {"index.html"} else {path.strip_prefix("/quarry/").ok_or_else(||fail(404,"not_found","Not found"))?};
        if asset.split('/').any(|part|part=="..") || asset.contains('\\'){return Err(fail(404,"not_found","Not found"));}
        let file=UI.get_file(asset).ok_or_else(||fail(404,"not_found","Not found"))?;
        let content=if asset=="index.html" {file.contents_utf8().ok_or_else(||fail(500,"internal","Invalid workbench bundle"))?.replace("<head>","<head><meta name=\"quarry-mode\" content=\"platform\">").into_bytes()} else {file.contents().to_vec()};
        let mut response=Response::new(if method==Method::HEAD{Body::empty()}else{Body::from(content)});
        response.headers_mut().insert(header::CONTENT_TYPE,header::HeaderValue::from_str(mime_guess::from_path(asset).first_or_octet_stream().as_ref()).map_err(|_|fail(500,"internal","Invalid asset type"))?);
        response.headers_mut().insert(header::CACHE_CONTROL,header::HeaderValue::from_static(if asset.starts_with("assets/"){"public,max-age=31536000,immutable"}else{"no-cache"}));
        Ok(response)
    }.await;
    let mut response = result.unwrap_or_else(error);
    response.headers_mut().insert(
        header::HeaderName::from_static("x-content-type-options"),
        header::HeaderValue::from_static("nosniff"),
    );
    response
}
