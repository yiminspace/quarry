pub mod auth;
mod cli_handoff;
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
use cli_handoff::{Access, Handoffs};
use quarry_platform::{Authorizer, HostedResourceScope, HostedState};
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
    hosted_state: HostedState,
    cli_handoff: Option<Arc<Handoffs>>,
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
    let cli_handoff = matches!(
        &config.auth,
        config::AuthConfig::External {
            cli_handoff: true,
            ..
        }
    )
    .then(|| Arc::new(Handoffs::default()));
    let auth = Arc::new(Auth::new(config.auth, config.state_dir)?);
    let mut hosted = HostedState::new(service, auth.clone(), config.resources)?;
    if let (Some(base_url), Some(model)) = (config.assistant_base_url, config.assistant_model) {
        hosted = hosted.with_assistant(quarry_platform::AssistantOptions { base_url, model })?;
    }
    Ok(Router::new().fallback(any(dispatch)).with_state(App {
        auth,
        hosted: quarry_platform::hosted_router(hosted.clone()),
        hosted_state: hosted,
        cli_handoff,
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
async fn cli_browser(
    app: &App,
    sessions: &Handoffs,
    mut req: Request,
) -> Result<Response, quarry_service::Failure> {
    let path = req.uri().path().to_owned();
    if path == "/api/db/browser-ticket" && req.method() == Method::POST {
        let bearer = bearer(&req)?;
        let principal = app.auth.authorize(&bearer, "lab:db:read").await?;
        let payload = body(req).await?;
        let resource = text(&payload, "resourceId")?.to_owned();
        app.hosted_state.resource(&principal, &resource)?;
        let code = sessions.issue(Access { bearer, resource })?;
        return Ok(json_response(json!({"code": code, "expires_in": 60})));
    }
    if path == "/quarry/browser-ticket/consume" && req.method() == Method::POST {
        if req
            .headers()
            .get("sec-fetch-site")
            .and_then(|v| v.to_str().ok())
            != Some("same-origin")
        {
            return Err(fail(
                403,
                "forbidden",
                "Open the database from the command line.",
            ));
        }
        let payload = body(req).await?;
        let (id, cookie) = sessions.consume(&text(&payload, "code")?)?;
        let path = format!("/quarry/session/{id}/");
        let mut response = json_response(json!({"path": path}));
        response.headers_mut().insert(
            header::SET_COOKIE,
            format!(
                "{}={cookie}; Path={path}; Secure; HttpOnly; SameSite=Strict; Max-Age={}",
                cli_handoff::COOKIE,
                cli_handoff::SESSION_TTL.as_secs()
            )
            .parse()
            .map_err(|_| fail(500, "internal", "Cannot create browser cookie"))?,
        );
        return Ok(response);
    }
    let (id, tail) = path
        .strip_prefix("/quarry/session/")
        .and_then(|rest| rest.split_once('/'))
        .ok_or_else(|| fail(404, "not_found", "Unknown browser path"))?;
    if id.len() != 32 || !id.bytes().all(|byte| byte.is_ascii_hexdigit()) {
        return Err(fail(404, "not_found", "Unknown browser path"));
    }
    if tail.is_empty() && matches!(*req.method(), Method::GET | Method::HEAD) {
        return workbench("index.html", req.method(), true);
    }
    let operation = tail
        .strip_prefix("api/")
        .ok_or_else(|| fail(404, "not_found", "Unknown browser operation"))?;
    if !matches!(*req.method(), Method::GET | Method::HEAD)
        && req
            .headers()
            .get("sec-fetch-site")
            .and_then(|v| v.to_str().ok())
            != Some("same-origin")
    {
        return Err(fail(
            403,
            "forbidden",
            "Open the database from the command line.",
        ));
    }
    let cookie = req
        .headers()
        .get_all(header::COOKIE)
        .iter()
        .filter_map(|header| header.to_str().ok())
        .flat_map(|header| header.split(';'))
        .filter_map(|pair| pair.trim().split_once('='))
        .find_map(|(name, value)| (name == cli_handoff::COOKIE).then_some(value))
        .ok_or_else(|| {
            fail(
                401,
                "unauthorized",
                "Reopen the database from the command line.",
            )
        })?;
    let (access, remaining) = sessions.session(id, cookie)?;
    let uri = format!(
        "/api/{operation}{}",
        req.uri()
            .query()
            .map(|q| format!("?{q}"))
            .unwrap_or_default()
    );
    *req.uri_mut() = uri
        .parse()
        .map_err(|_| fail(400, "invalid", "Invalid API path"))?;
    req.headers_mut().insert(
        header::AUTHORIZATION,
        format!("Bearer {}", access.bearer)
            .parse()
            .map_err(|_| fail(401, "unauthorized", "Invalid session credential"))?,
    );
    req.extensions_mut()
        .insert(HostedResourceScope(access.resource));
    let mut response = app
        .hosted
        .clone()
        .oneshot(req)
        .await
        .unwrap_or_else(|never| match never {});
    response.headers_mut().insert(
        "x-quarry-session-remaining-ms",
        header::HeaderValue::from_str(&remaining.as_millis().to_string())
            .map_err(|_| fail(500, "internal", "Cannot encode session expiry"))?,
    );
    Ok(response)
}

fn workbench(
    asset: &str,
    method: &Method,
    cli_mode: bool,
) -> Result<Response, quarry_service::Failure> {
    if asset.split('/').any(|part| part == "..") || asset.contains('\\') {
        return Err(fail(404, "not_found", "Not found"));
    }
    let file = UI
        .get_file(asset)
        .ok_or_else(|| fail(404, "not_found", "Not found"))?;
    let content = if asset == "index.html" {
        let meta = if cli_mode {
            r#"<head><meta name="quarry-mode" content="platform"><meta name="quarry-cli-session" content="true">"#
        } else {
            r#"<head><meta name="quarry-mode" content="platform">"#
        };
        file.contents_utf8()
            .ok_or_else(|| fail(500, "internal", "Invalid workbench bundle"))?
            .replace("<head>", meta)
            .into_bytes()
    } else {
        file.contents().to_vec()
    };
    let mut response = Response::new(if method == Method::HEAD {
        Body::empty()
    } else {
        Body::from(content)
    });
    response.headers_mut().insert(
        header::CONTENT_TYPE,
        header::HeaderValue::from_str(
            mime_guess::from_path(asset)
                .first_or_octet_stream()
                .as_ref(),
        )
        .map_err(|_| fail(500, "internal", "Invalid asset type"))?,
    );
    response.headers_mut().insert(
        header::CACHE_CONTROL,
        header::HeaderValue::from_static(if cli_mode && asset == "index.html" {
            "no-store"
        } else if asset.starts_with("assets/") {
            "public,max-age=31536000,immutable"
        } else {
            "no-cache"
        }),
    );
    Ok(response)
}

async fn dispatch(State(app): State<App>, req: Request) -> Response {
    let path = req.uri().path().to_owned();
    let method = req.method().clone();
    let result=async {
        if let Some(sessions) = &app.cli_handoff
            && (path == "/api/db/browser-ticket" || path == "/quarry/browser-ticket/consume" || path.starts_with("/quarry/session/"))
        {
            return cli_browser(&app, sessions, req).await;
        }
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
        workbench(asset, &method, app.cli_handoff.is_some())
    }.await;
    let mut response = result.unwrap_or_else(error);
    response.headers_mut().insert(
        header::HeaderName::from_static("x-content-type-options"),
        header::HeaderValue::from_static("nosniff"),
    );
    response
}
