use argon2::{Argon2, PasswordHasher, password_hash::SaltString};
use axum::{Router, body::Body, http::Request};
use http_body_util::BodyExt;
use quarry_platform::Authorizer;
use quarry_server::{
    auth::Auth,
    config::{AuthConfig, Config},
};
use serde_json::{Value, json};
use std::{path::Path, sync::Arc};
use tower::ServiceExt;
fn encoded() -> String {
    Argon2::default()
        .hash_password(
            b"test-only-password",
            &SaltString::encode_b64(b"test-only-salt-123456").unwrap(),
        )
        .unwrap()
        .to_string()
}
fn auth_config() -> AuthConfig {
    AuthConfig::Builtin {
        user_id: "admin".into(),
        username: "owner".into(),
        password_hash: encoded(),
        roles: vec![],
    }
}
fn fixture(path: &Path) -> Config {
    std::fs::write(path.join("config.toml"), "").unwrap();
    let workspace = path.join("workspace");
    std::fs::create_dir(&workspace).unwrap();
    std::fs::write(
        workspace.join("connections.toml"),
        "[notes]\nurl='sqlite:///notes.sqlite'\nenv='dev'\nproduction=true\n",
    )
    .unwrap();
    let db = rusqlite::Connection::open(workspace.join("notes.sqlite")).unwrap();
    db.execute_batch("CREATE TABLE entries(n INTEGER); INSERT INTO entries VALUES(42)")
        .unwrap();
    let resource = |id: &str, reader: &str| quarry_platform::HostedResource {
        id: id.into(),
        project: "example".into(),
        db: "notes".into(),
        env: "dev".into(),
        connection_key: "notes".into(),
        workspace: Some(workspace.clone()),
        readers: vec![reader.into()],
    };
    Config {
        config_path: path.join("config.toml"),
        workspaces: vec![workspace.clone()],
        state_dir: path.join("state"),
        resources: vec![
            resource("allowed", "admin"),
            resource("hidden", "someone-else"),
        ],
        auth: auth_config(),
        assistant_base_url: None,
        assistant_model: None,
        cloud_local: None,
        listen: "127.0.0.1:0".into(),
    }
}
async fn request(
    router: &Router,
    method: &str,
    path: &str,
    bearer: Option<&str>,
    value: Option<Value>,
) -> (u16, Value) {
    let mut builder = Request::builder().method(method).uri(path);
    if let Some(bearer) = bearer {
        builder = builder.header("Authorization", format!("Bearer {bearer}"));
    }
    let response = router
        .clone()
        .oneshot(
            builder
                .header("Content-Type", "application/json")
                .body(Body::from(value.map(|v| v.to_string()).unwrap_or_default()))
                .unwrap(),
        )
        .await
        .unwrap();
    let status = response.status().as_u16();
    let bytes = response.into_body().collect().await.unwrap().to_bytes();
    (
        status,
        serde_json::from_slice(&bytes)
            .unwrap_or_else(|_| json!({"text":String::from_utf8_lossy(&bytes)})),
    )
}
#[tokio::test]
async fn one_server_serves_ui_auth_health_and_enforces_resource_readonly_boundaries() {
    let temp = tempfile::tempdir().unwrap();
    let router = quarry_server::router(fixture(temp.path()), "test-sha".into()).unwrap();
    let (status, health) = request(&router, "GET", "/health", None, None).await;
    assert_eq!(status, 200);
    assert_eq!(health["version"], "test-sha");
    let (_, discovery) = request(&router, "GET", "/api/hosted/config", None, None).await;
    assert_eq!(discovery["auth"]["mode"], "builtin");
    assert!(!discovery.to_string().contains("password"));
    let (status, index) = request(&router, "GET", "/quarry/", None, None).await;
    assert_eq!(status, 200);
    assert!(index["text"].as_str().unwrap().contains("quarry-mode"));
    assert_eq!(
        request(&router, "GET", "/api/db/platform", None, None)
            .await
            .0,
        401
    );
    assert_eq!(
        request(
            &router,
            "POST",
            "/api/hosted/auth/login",
            None,
            Some(json!({"username":"owner","password":"incorrect"}))
        )
        .await
        .0,
        401
    );
    let (status, session) = request(
        &router,
        "POST",
        "/api/hosted/auth/login",
        None,
        Some(json!({"username":"owner","password":"test-only-password"})),
    )
    .await;
    assert_eq!(status, 200);
    let token = session["access_token"].as_str().unwrap();
    let (status, catalog) = request(&router, "GET", "/api/db/platform", Some(token), None).await;
    assert_eq!(status, 200);
    assert_eq!(catalog["resources"].as_array().unwrap().len(), 1);
    assert_eq!(catalog["resources"][0]["id"], "allowed");
    let query = |id: &str, sql: &str| json!({"resourceId":id,"sql":sql,"maxRows":10,"offset":0,"requestId":uuid::Uuid::new_v4().to_string()});
    let (status, data) = request(
        &router,
        "POST",
        "/api/db/query",
        Some(token),
        Some(query("allowed", "SELECT n FROM entries")),
    )
    .await;
    assert_eq!(status, 200, "{data}");
    assert_eq!(data["rows"][0]["n"], 42);
    assert_eq!(
        request(
            &router,
            "POST",
            "/api/db/query",
            Some(token),
            Some(query("hidden", "SELECT n FROM entries"))
        )
        .await
        .0,
        404
    );
    assert_ne!(
        request(
            &router,
            "POST",
            "/api/db/query",
            Some(token),
            Some(query("allowed", "DELETE FROM entries"))
        )
        .await
        .0,
        200
    );
    for path in [
        "/api/db/personal",
        "/api/db/local/up",
        "/api/personal",
        "/api/local/up",
    ] {
        assert_ne!(
            request(&router, "POST", path, Some(token), Some(json!({})))
                .await
                .0,
            200
        );
    }
    assert_eq!(
        request(&router, "GET", "/quarry/../Cargo.toml", None, None)
            .await
            .0,
        404
    );
    let (status, p) = request(
        &router,
        "GET",
        "/api/db/authorize?permission=lab%3Adb%3Alocal",
        Some(token),
        None,
    )
    .await;
    assert_eq!(status, 200);
    assert_eq!(p["id"], "admin");
}
#[tokio::test]
async fn persisted_sessions_rotate_revoke_and_handoffs_are_single_use_and_parent_bound() {
    let temp = tempfile::tempdir().unwrap();
    let state = temp.path().join("state");
    let auth = Auth::new(auth_config(), state.clone()).unwrap();
    let initial = auth
        .login("owner".into(), "test-only-password".into())
        .await
        .unwrap();
    let first = initial["access_token"].as_str().unwrap();
    let refresh = initial["refresh_token"].as_str().unwrap();
    let reloaded = Auth::new(auth_config(), state.clone()).unwrap();
    assert!(reloaded.authorize(first, "lab:db:read").await.is_ok());
    let code = auth.handoff(first).await.unwrap()["code"]
        .as_str()
        .unwrap()
        .to_owned();
    let browser = auth.consume_handoff(&code).unwrap();
    assert_ne!(browser["access_token"], initial["access_token"]);
    assert!(auth.consume_handoff(&code).is_err());
    let stale = auth.handoff(first).await.unwrap()["code"]
        .as_str()
        .unwrap()
        .to_owned();
    let next = auth.refresh(refresh).unwrap();
    assert!(auth.refresh(refresh).is_err());
    assert!(auth.authorize(first, "lab:db:read").await.is_err());
    assert!(auth.consume_handoff(&stale).is_err());
    let next_token = next["access_token"].as_str().unwrap();
    assert!(auth.authorize(next_token, "lab:db:read").await.is_ok());
    let revoke_code = auth.handoff(next_token).await.unwrap()["code"]
        .as_str()
        .unwrap()
        .to_owned();
    auth.logout(next["refresh_token"].as_str().unwrap())
        .unwrap();
    assert!(auth.authorize(next_token, "lab:db:read").await.is_err());
    assert!(auth.consume_handoff(&revoke_code).is_err());
    // Browser handoff created an independent session; parent logout preserves it.
    assert!(
        auth.authorize(browser["access_token"].as_str().unwrap(), "lab:db:read")
            .await
            .is_ok()
    );
    let mut config = auth_config();
    if let AuthConfig::Builtin { password_hash, .. } = &mut config {
        *password_hash = Argon2::default()
            .hash_password(
                b"new-test-password",
                &SaltString::encode_b64(b"test-only-salt-123456").unwrap(),
            )
            .unwrap()
            .to_string();
    }
    let changed = Auth::new(config, state.clone()).unwrap();
    assert!(
        changed
            .authorize(browser["access_token"].as_str().unwrap(), "lab:db:read")
            .await
            .is_err()
    );
    let raw = std::fs::read(state.join("hosted-auth.sqlite3")).unwrap();
    assert!(!raw.windows(first.len()).any(|w| w == first.as_bytes()));
    assert!(!raw.windows(refresh.len()).any(|w| w == refresh.as_bytes()));
}
#[tokio::test]
async fn handoff_expiry_and_login_rate_limit_are_enforced() {
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("state");
    let auth = Auth::new(auth_config(), path.clone()).unwrap();
    let initial = auth
        .login("owner".into(), "test-only-password".into())
        .await
        .unwrap();
    let code = auth
        .handoff(initial["access_token"].as_str().unwrap())
        .await
        .unwrap()["code"]
        .as_str()
        .unwrap()
        .to_owned();
    rusqlite::Connection::open(path.join("hosted-auth.sqlite3"))
        .unwrap()
        .execute("UPDATE handoffs SET expires=0", [])
        .unwrap();
    assert!(auth.consume_handoff(&code).is_err());
    for _ in 0..19 {
        assert_eq!(
            auth.login("wrong".into(), "incorrect".into())
                .await
                .unwrap_err()
                .status,
            401
        );
    }
    assert_eq!(
        auth.login("owner".into(), "test-only-password".into())
            .await
            .unwrap_err()
            .status,
        429
    );
}
#[tokio::test]
async fn external_identity_is_verified_per_call_never_taken_from_client_headers() {
    use axum::{extract::State, http::HeaderMap, routing::get};
    use std::sync::Mutex;
    let calls = Arc::new(Mutex::new(Vec::new()));
    async fn verify(
        State(calls): State<Arc<Mutex<Vec<String>>>>,
        headers: HeaderMap,
    ) -> axum::response::Response {
        use axum::response::IntoResponse;
        let permission = headers
            .get("x-required-permission")
            .unwrap()
            .to_str()
            .unwrap()
            .to_owned();
        calls.lock().unwrap().push(permission.clone());
        if headers.get("authorization").unwrap() != "Bearer live-token" {
            return axum::http::StatusCode::UNAUTHORIZED.into_response();
        }
        let mut r = axum::http::StatusCode::OK.into_response();
        r.headers_mut()
            .insert("x-user-id", "admin".parse().unwrap());
        r.headers_mut().insert(
            "x-user-permissions",
            "lab:db:read,lab:db:local".parse().unwrap(),
        );
        r
    }
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let address = listener.local_addr().unwrap();
    let iam = Router::new()
        .route("/verify", get(verify))
        .with_state(calls.clone());
    let task = tokio::spawn(async move { axum::serve(listener, iam).await.unwrap() });
    let temp = tempfile::tempdir().unwrap();
    let mut config = fixture(temp.path());
    config.auth = AuthConfig::External {
        cli_handoff: false,
        verify_url: format!("http://{address}/verify"),
        allow_insecure_http: false,
        response: quarry_server::config::VerifyResponse::Headers,
        session_key: "platform-session".into(),
        login_url: "/iam/?return=/quarry/".into(),
        refresh_path: "/api/iam/refresh".into(),
        logout_path: None,
    };
    let router = quarry_server::router(config, "test".into()).unwrap();
    assert_eq!(
        request(&router, "GET", "/api/db/platform", Some("live-token"), None)
            .await
            .0,
        200
    );
    assert_eq!(
        request(&router, "GET", "/api/db/platform", Some("live-token"), None)
            .await
            .0,
        200
    );
    assert_eq!(calls.lock().unwrap().len(), 2);
    let forged = Request::builder()
        .uri("/api/db/platform")
        .header("x-user-id", "admin")
        .header("x-user-permissions", "*")
        .body(Body::empty())
        .unwrap();
    assert_eq!(router.clone().oneshot(forged).await.unwrap().status(), 401);
    assert_eq!(
        request(
            &router,
            "GET",
            "/api/db/platform",
            Some("revoked-token"),
            None
        )
        .await
        .0,
        401
    );
    assert_eq!(
        request(
            &router,
            "GET",
            "/api/db/authorize?permission=lab%3Aai%3Ainvoke",
            Some("live-token"),
            None
        )
        .await
        .0,
        403
    );
    task.abort();
}
#[test]
fn insecure_configurations_fail_before_listening() {
    let temp = tempfile::tempdir().unwrap();
    let mut config = fixture(temp.path());
    if let AuthConfig::Builtin { password_hash, .. } = &mut config.auth {
        *password_hash = "plaintext".into();
    }
    assert!(config.validate().is_err());
    config.auth = AuthConfig::External {
        cli_handoff: false,
        verify_url: "http://public.example/verify".into(),
        allow_insecure_http: false,
        response: quarry_server::config::VerifyResponse::Headers,
        session_key: "s".into(),
        login_url: "//evil.example".into(),
        refresh_path: "/refresh".into(),
        logout_path: None,
    };
    assert!(config.validate().is_err());
}

#[tokio::test]
async fn cli_browser_ticket_is_single_use_and_session_enforces_scope_and_current_grants() {
    use axum::{extract::State, http::HeaderMap, routing::get};
    use std::sync::atomic::{AtomicBool, Ordering};

    async fn verify(
        State(revoked): State<Arc<AtomicBool>>,
        headers: HeaderMap,
    ) -> (axum::http::StatusCode, axum::Json<Value>) {
        let status = if headers
            .get("authorization")
            .is_none_or(|header| header != "Bearer personal")
        {
            axum::http::StatusCode::UNAUTHORIZED
        } else if revoked.load(Ordering::Relaxed) {
            axum::http::StatusCode::FORBIDDEN
        } else {
            axum::http::StatusCode::OK
        };
        (
            status,
            axum::Json(json!({"id":"admin","permissions":["lab:db:read"],"roles":[]})),
        )
    }
    let revoked = Arc::new(AtomicBool::new(false));
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let address = listener.local_addr().unwrap();
    let iam = Router::new()
        .route("/verify", get(verify))
        .with_state(revoked.clone());
    let task = tokio::spawn(async move { axum::serve(listener, iam).await.unwrap() });
    let temp = tempfile::tempdir().unwrap();
    let mut config = fixture(temp.path());
    config.resources[1].readers = vec!["admin".into()];
    config.auth = AuthConfig::External {
        cli_handoff: true,
        verify_url: format!("http://{address}/verify"),
        allow_insecure_http: false,
        response: quarry_server::config::VerifyResponse::Json,
        session_key: "unused".into(),
        login_url: "/unused".into(),
        refresh_path: "/unused".into(),
        logout_path: None,
    };
    let router = quarry_server::router(config, "test".into()).unwrap();
    let ticket = request(
        &router,
        "POST",
        "/api/db/browser-ticket",
        Some("personal"),
        Some(json!({"resourceId":"allowed"})),
    )
    .await;
    assert_eq!(ticket.0, 200);
    assert_eq!(ticket.1["expires_in"], 60);
    let redeem = || {
        Request::builder()
            .method("POST")
            .uri("/quarry/browser-ticket/consume")
            .header("sec-fetch-site", "same-origin")
            .body(Body::from(json!({"code":ticket.1["code"]}).to_string()))
            .unwrap()
    };
    let session = router.clone().oneshot(redeem()).await.unwrap();
    assert_eq!(session.status(), 200);
    let cookie = session.headers()["set-cookie"].to_str().unwrap().to_owned();
    assert!(cookie.contains("; Secure; HttpOnly; SameSite=Strict; Max-Age=1800"));
    let cookie = cookie.split(';').next().unwrap();
    let data: Value =
        serde_json::from_slice(&session.into_body().collect().await.unwrap().to_bytes()).unwrap();
    let path = data["path"].as_str().unwrap();
    assert_eq!(
        router.clone().oneshot(redeem()).await.unwrap().status(),
        401
    );
    let browser_request = |op: &str| {
        Request::builder()
            .uri(format!("{path}api/{op}"))
            .header("Cookie", cookie)
            .body(Body::empty())
            .unwrap()
    };
    let catalog = router
        .clone()
        .oneshot(browser_request("platform"))
        .await
        .unwrap();
    assert_eq!(catalog.status(), 200);
    let data: Value =
        serde_json::from_slice(&catalog.into_body().collect().await.unwrap().to_bytes()).unwrap();
    assert_eq!(data["resources"].as_array().unwrap().len(), 1);
    assert_eq!(data["resources"][0]["id"], "allowed");
    assert_eq!(
        router
            .clone()
            .oneshot(browser_request("tables?resourceId=hidden"))
            .await
            .unwrap()
            .status(),
        404
    );
    assert_eq!(
        router
            .clone()
            .oneshot(browser_request("tables?resourceId=allowed"))
            .await
            .unwrap()
            .status(),
        200
    );
    assert_eq!(
        request(
            &router,
            "GET",
            &format!("{path}api/platform"),
            Some("personal"),
            None
        )
        .await
        .0,
        401
    );
    revoked.store(true, Ordering::Relaxed);
    assert_eq!(
        router
            .oneshot(browser_request("tables?resourceId=allowed"))
            .await
            .unwrap()
            .status(),
        403
    );
    task.abort();
}
