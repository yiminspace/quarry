use async_trait::async_trait;
use axum::{
    Json, Router,
    body::{Body, to_bytes},
    http::{Request, StatusCode},
    routing::post,
};
use quarry_platform::*;
use quarry_service::QuarryService;
use serde_json::{Value, json};
use std::sync::{
    Arc, Mutex,
    atomic::{AtomicBool, AtomicUsize, Ordering},
};
use tower::ServiceExt;

struct Auth {
    offline: AtomicBool,
    calls: AtomicUsize,
}
#[async_trait]
impl Authorizer for Auth {
    async fn authorize(&self, token: &str, permission: &str) -> Result<Principal> {
        self.calls.fetch_add(1, Ordering::SeqCst);
        if self.offline.load(Ordering::SeqCst) || !["alice", "bob"].contains(&token) {
            return Err(quarry_service::Failure {
                status: 401,
                code: "unauthorized".into(),
                message: "Not authorized".into(),
            });
        }
        Ok(Principal {
            id: token.into(),
            permissions: vec![permission.into()],
            roles: vec![],
        })
    }
}
struct Fixture {
    dir: tempfile::TempDir,
    service: Arc<QuarryService>,
    auth: Arc<Auth>,
}
impl Fixture {
    fn new() -> Self {
        let dir = tempfile::tempdir().unwrap();
        let ws = dir.path().join("workspace");
        std::fs::create_dir(&ws).unwrap();
        std::fs::write(dir.path().join("config.toml"), "").unwrap();
        let db = ws.join("notes.db");
        let c = rusqlite::Connection::open(&db).unwrap();
        c.execute_batch("CREATE TABLE notes(id INTEGER PRIMARY KEY, content TEXT);INSERT INTO notes VALUES(1,'example');").unwrap();
        std::fs::write(
            ws.join("connections.toml"),
            format!(
                "[notes]\nurl='sqlite:///{}'\nengine='sqlite'\nenv='dev'\nproduction=true\n",
                db.display()
            ),
        )
        .unwrap();
        let service = Arc::new(QuarryService::new(
            dir.path().join("config.toml"),
            vec![ws],
            dir.path().join("runtime"),
        ));
        Self {
            dir,
            service,
            auth: Arc::new(Auth {
                offline: AtomicBool::new(false),
                calls: AtomicUsize::new(0),
            }),
        }
    }
    fn hosted(&self) -> HostedState {
        HostedState::new(
            self.service.clone(),
            self.auth.clone(),
            vec![HostedResource {
                id: "notes-dev".into(),
                project: "project".into(),
                db: "notes".into(),
                env: "dev".into(),
                connection_key: "notes".into(),
                workspace: None,
                readers: vec!["alice".into()],
            }],
        )
        .unwrap()
    }
    fn companion(&self) -> CompanionState {
        CompanionState::new(
            self.service.clone(),
            self.auth.clone(),
            CompanionConfig {
                origin: "https://console.example".into(),
                user_id: "alice".into(),
                state_path: self.dir.path().join("personal/documents.sqlite"),
                port: 8766,
            },
        )
        .unwrap()
    }
}
#[allow(clippy::too_many_arguments)]
async fn send(
    router: Router,
    method: &str,
    path: &str,
    token: &str,
    body: Option<Value>,
    pair: Option<&str>,
    origin: Option<&str>,
    owner: Option<&str>,
) -> (StatusCode, Value, axum::http::HeaderMap) {
    let mut req = Request::builder()
        .method(method)
        .uri(path)
        .header("Host", "127.0.0.1:8766")
        .header("Authorization", format!("Bearer {token}"));
    if let Some(v) = pair {
        req = req.header("X-Quarry-Companion-Token", v);
    }
    if let Some(v) = origin {
        req = req.header("Origin", v);
    }
    if let Some(v) = owner {
        req = req.header("X-Quarry-User-Id", v);
    }
    let out = router
        .oneshot(
            req.body(Body::from(body.map(|v| v.to_string()).unwrap_or_default()))
                .unwrap(),
        )
        .await
        .unwrap();
    let status = out.status();
    let headers = out.headers().clone();
    let bytes = to_bytes(out.into_body(), 20 * 1024 * 1024).await.unwrap();
    let value = serde_json::from_slice(&bytes)
        .unwrap_or_else(|_| json!({"raw":String::from_utf8_lossy(&bytes)}));
    (status, value, headers)
}
#[tokio::test]
async fn hosted_filters_catalog_hides_credentials_and_requires_resource_for_every_operation() {
    let f = Fixture::new();
    let app = hosted_router(f.hosted());
    let (_, v, _) = send(
        app.clone(),
        "GET",
        "/api/connections",
        "bob",
        None,
        None,
        None,
        None,
    )
    .await;
    assert_eq!(v["groups"], json!([]));
    let (status, v, _) = send(
        app.clone(),
        "GET",
        "/api/info?resourceId=notes-dev",
        "alice",
        None,
        None,
        None,
        None,
    )
    .await;
    assert_eq!(status, 200);
    assert!(v.get("url").is_none());
    assert!(!v.to_string().contains(f.dir.path().to_str().unwrap()));
    for path in [
        "/api/tables?resourceId=notes-dev",
        "/api/columns?resourceId=notes-dev&table=notes",
        "/api/info?resourceId=notes-dev",
    ] {
        assert_eq!(
            send(app.clone(), "GET", path, "bob", None, None, None, None)
                .await
                .0,
            404
        );
    }
    assert_eq!(
        send(
            app.clone(),
            "POST",
            "/api/query",
            "alice",
            Some(json!({"db":"notes","env":"dev","workspace":f.dir.path(),"sql":"SELECT 1"})),
            None,
            None,
            None
        )
        .await
        .0,
        400
    );
    assert_eq!(
        send(
            app.clone(),
            "GET",
            "/api/info?resourceId=notes-dev",
            "forged",
            None,
            None,
            None,
            None
        )
        .await
        .0,
        401
    );
    f.auth.offline.store(true, Ordering::SeqCst);
    assert_eq!(
        send(
            app,
            "GET",
            "/api/connections",
            "alice",
            None,
            None,
            None,
            None
        )
        .await
        .0,
        401
    );
}
#[tokio::test]
async fn hosted_query_is_read_only_bounded_and_owner_cancel_isolated() {
    let f = Fixture::new();
    let app = hosted_router(f.hosted());
    let(status,v,_)=send(app.clone(),"POST","/api/query","alice",Some(json!({"resourceId":"notes-dev","sql":"SELECT * FROM notes","maxRows":1,"requestId":"q1"})),None,None,None).await;
    assert_eq!(status, 200, "{v}");
    assert_eq!(v["rows"][0]["id"], 1);
    assert_eq!(v["requestId"], "q1");
    let(status,_,_)=send(app.clone(),"POST","/api/query","alice",Some(json!({"resourceId":"notes-dev","sql":"DELETE FROM notes","write":true,"confirmProduction":true})),None,None,None).await;
    assert!(!status.is_success());
    assert_eq!(
        send(
            app.clone(),
            "POST",
            "/api/query",
            "alice",
            Some(json!({"resourceId":"notes-dev","sql":"SELECT 1","maxRows":5001})),
            None,
            None,
            None
        )
        .await
        .0,
        400
    );
    assert_eq!(
        send(
            app,
            "POST",
            "/api/cancel",
            "bob",
            Some(json!({"requestId":"q1"})),
            None,
            None,
            None
        )
        .await
        .1["cancelled"],
        false
    );
}
#[tokio::test]
async fn companion_pairs_once_and_local_state_survives_cloud_outage_but_revokes_locally() {
    let f = Fixture::new();
    let state = f.companion();
    let code = state.pairing_code()["code"].as_str().unwrap().to_owned();
    let app = companion_router(state.clone());
    let origin = Some("https://console.example");
    let (status, v, _) = send(
        app.clone(),
        "POST",
        "/api/pair",
        "bob",
        Some(json!({"code":code})),
        None,
        origin,
        None,
    )
    .await;
    assert_eq!(status, 403, "{v}");
    let (status, v, _) = send(
        app.clone(),
        "POST",
        "/api/pair",
        "alice",
        Some(json!({"code":code})),
        None,
        origin,
        None,
    )
    .await;
    assert_eq!(status, 200, "{v}");
    let token = v["token"].as_str().unwrap().to_owned();
    assert_eq!(
        send(
            app.clone(),
            "POST",
            "/api/pair",
            "alice",
            Some(json!({"code":code})),
            None,
            origin,
            None
        )
        .await
        .0,
        403
    );
    let calls = f.auth.calls.load(Ordering::SeqCst);
    f.auth.offline.store(true, Ordering::SeqCst);
    let(status,v,_)=send(app.clone(),"POST","/api/personal","expired",Some(json!({"kind":"workbench","key":"workspace","value":{"draft":"SELECT 42"},"revision":0})),Some(&token),origin,Some("alice")).await;
    assert_eq!(status, 200, "{v}");
    assert_eq!(v["revision"], 1);
    let (status, v, _) = send(
        app.clone(),
        "GET",
        "/api/personal?kind=workbench&key=workspace",
        "expired",
        None,
        Some(&token),
        origin,
        Some("alice"),
    )
    .await;
    assert_eq!(status, 200);
    assert_eq!(v["value"]["draft"], "SELECT 42");
    assert_eq!(calls, f.auth.calls.load(Ordering::SeqCst));
    assert_eq!(
        send(
            app.clone(),
            "POST",
            "/api/personal",
            "",
            Some(json!({"kind":"workbench","key":"workspace","value":{},"revision":0})),
            Some(&token),
            origin,
            Some("alice")
        )
        .await
        .0,
        409
    );
    assert_eq!(
        send(
            app.clone(),
            "GET",
            "/api/platform",
            "",
            None,
            Some(&token),
            origin,
            Some("bob")
        )
        .await
        .0,
        403
    );
    let (status, _, headers) = send(
        app.clone(),
        "GET",
        "/api/platform",
        "",
        None,
        Some(&token),
        Some("https://evil.example"),
        Some("alice"),
    )
    .await;
    assert_eq!(status, 403);
    assert!(headers.get("Access-Control-Allow-Origin").is_none());
    assert_eq!(
        send(
            app.clone(),
            "POST",
            "/api/unpair",
            "",
            Some(json!({})),
            Some(&token),
            origin,
            Some("alice")
        )
        .await
        .0,
        200
    );
    assert_eq!(
        send(
            app,
            "GET",
            "/api/platform",
            "",
            None,
            Some(&token),
            origin,
            Some("alice")
        )
        .await
        .0,
        401
    );
    assert!(f.dir.path().join("personal/documents.sqlite").is_file());
}
#[test]
fn personal_documents_are_origin_and_account_scoped_with_compare_and_swap() {
    let d = tempfile::tempdir().unwrap();
    let store = PersonalStore::new(d.path().join("private/state.sqlite"));
    store
        .put(
            "https://one",
            "alice",
            "workbench",
            "workspace",
            json!({"sql":"private"}),
            0,
        )
        .unwrap();
    for (origin, user) in [("https://one", "bob"), ("https://two", "alice")] {
        assert!(store.get(origin, user, "workbench", "workspace").unwrap()["value"].is_null());
    }
    assert_eq!(
        store
            .put(
                "https://one",
                "alice",
                "workbench",
                "workspace",
                json!({}),
                0
            )
            .unwrap_err()
            .status,
        409
    );
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        assert_eq!(
            std::fs::metadata(d.path().join("private"))
                .unwrap()
                .permissions()
                .mode()
                & 0o777,
            0o700
        );
        assert_eq!(
            std::fs::metadata(d.path().join("private/state.sqlite"))
                .unwrap()
                .permissions()
                .mode()
                & 0o777,
            0o600
        );
    }
}
#[tokio::test]
async fn ephemeral_assistant_uses_user_bearer_and_keeps_no_hosted_history() {
    let seen = Arc::new(Mutex::new(String::new()));
    let capture = seen.clone();
    let model=Router::new().route("/v1/chat/completions",post(move |headers:axum::http::HeaderMap|{let capture=capture.clone();async move {*capture.lock().unwrap()=headers.get("Authorization").unwrap().to_str().unwrap().into();Json(json!({"choices":[{"message":{"role":"assistant","content":"Returns one row."},"finish_reason":"stop"}]}))}}));
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let port = listener.local_addr().unwrap().port();
    let server = tokio::spawn(async move {
        axum::serve(listener, model).await.unwrap();
    });
    let f = Fixture::new();
    let state = f
        .hosted()
        .with_assistant(AssistantOptions {
            base_url: format!("http://127.0.0.1:{port}/v1"),
            model: "fixture".into(),
        })
        .unwrap();
    let app = hosted_router(state);
    let (status, v, _) = send(
        app.clone(),
        "POST",
        "/api/assistant/chat",
        "alice",
        Some(json!({"resourceId":"notes-dev","sql":"SELECT 42","explain":true,"requestId":"a1"})),
        None,
        None,
        None,
    )
    .await;
    assert_eq!(status, 200, "{v}");
    let raw = v["raw"].as_str().unwrap();
    let events: Vec<Value> = raw
        .lines()
        .map(|l| serde_json::from_str(l).unwrap())
        .collect();
    let done = events
        .iter()
        .find(|e| e["type"] == "done")
        .unwrap_or_else(|| panic!("{raw}"));
    assert!(done["conversation"].is_object());
    assert_eq!(*seen.lock().unwrap(), "Bearer alice");
    assert!(!raw.contains(f.dir.path().to_str().unwrap()));
    let(status,v,_)=send(app,"POST","/api/assistant/chat","alice",Some(json!({"resourceId":"notes-dev","sql":"SELECT 43","prompt":"Continue","requestId":"a2","conversation":done["conversation"]})),None,None,None).await;
    assert_eq!(status, 200, "{v}");
    assert!(!f.dir.path().join("runtime/assistant.sqlite").exists());
    server.abort();
}

#[tokio::test]
async fn assistant_admission_is_shared_and_disconnect_releases_the_budget() {
    let model = Router::new().route(
        "/v1/chat/completions",
        post(|| async { std::future::pending::<Json<Value>>().await }),
    );
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let port = listener.local_addr().unwrap().port();
    let server = tokio::spawn(async move {
        axum::serve(listener, model).await.unwrap();
    });
    let f = Fixture::new();
    let app = hosted_router(
        f.hosted()
            .with_assistant(AssistantOptions {
                base_url: format!("http://127.0.0.1:{port}/v1"),
                model: "pending".into(),
            })
            .unwrap(),
    );
    let request = |id: usize| {
        Request::builder().method("POST").uri("/api/assistant/chat").header("Authorization","Bearer alice").body(Body::from(json!({"resourceId":"notes-dev","sql":"SELECT 1","explain":true,"requestId":format!("held-{id}")}).to_string())).unwrap()
    };
    let mut held = vec![];
    for index in 0..64 {
        let response = app.clone().oneshot(request(index)).await.unwrap();
        assert_eq!(response.status(), 200);
        held.push(response);
    }
    assert_eq!(
        app.clone().oneshot(request(64)).await.unwrap().status(),
        429
    );
    drop(held.pop());
    let next = app.oneshot(request(65)).await.unwrap();
    assert_eq!(next.status(), 200);
    drop(next);
    drop(held);
    server.abort();
}

#[tokio::test]
async fn paired_local_database_is_account_scoped_and_operates_offline() {
    let f = Fixture::new();
    let catalog = f.service.catalog().unwrap();
    for owner in ["alice", "bob"] {
        let mut c = f.service.resolve("notes", Some("dev"), None).unwrap();
        c.key = format!("notes_{owner}");
        c.env = "local".into();
        c.production = false;
        c.extra
            .insert("platform_origin".into(), "https://console.example".into());
        c.extra.insert("platform_user_id".into(), owner.into());
        c.extra
            .insert("platform_resource_id".into(), "notes-dev".into());
        c.extra.insert("platform_project".into(), "project".into());
        c.extra.insert("platform_db".into(), "notes".into());
        catalog.put_connection(&c.workspace, &c, false).unwrap();
    }
    let state = f.companion();
    let code = state.pairing_code()["code"].as_str().unwrap().to_owned();
    let app = companion_router(state);
    let origin = Some("https://console.example");
    let (_, pair, _) = send(
        app.clone(),
        "POST",
        "/api/pair",
        "alice",
        Some(json!({"code":code})),
        None,
        origin,
        None,
    )
    .await;
    let token = pair["token"].as_str().unwrap();
    f.auth.offline.store(true, Ordering::SeqCst);
    let (status, v, _) = send(
        app.clone(),
        "GET",
        "/api/platform",
        "expired",
        None,
        Some(token),
        origin,
        Some("alice"),
    )
    .await;
    assert_eq!(status, 200, "{v}");
    assert_eq!(v["resources"].as_array().unwrap().len(), 1);
    assert_eq!(v["authMode"], "paired-local");
    let id = v["resources"][0]["id"].as_str().unwrap();
    let (status, v, _) = send(
        app.clone(),
        "POST",
        "/api/query",
        "expired",
        Some(json!({"resourceId":id,"sql":"SELECT * FROM notes"})),
        Some(token),
        origin,
        Some("alice"),
    )
    .await;
    assert_eq!(status, 200, "{v}");
    assert_eq!(v["rows"][0]["content"], "example");
    assert_eq!(
        send(
            app.clone(),
            "POST",
            "/api/schema/import",
            "expired",
            Some(json!({"resourceId":id,"sourceResourceId":"notes-dev"})),
            Some(token),
            origin,
            Some("alice")
        )
        .await
        .0,
        401
    );
    assert_eq!(send(app.clone(),"POST","/api/schema/import","expired",Some(json!({"resourceId":id,"sourceResourceId":"notes-dev","schema":"CREATE EXTENSION malicious"})),Some(token),origin,Some("alice")).await.0,400);
    assert_eq!(
        send(
            app,
            "POST",
            "/api/assistant/chat",
            "expired",
            Some(json!({"resourceId":id,"sql":"SELECT 1","explain":true})),
            Some(token),
            origin,
            Some("alice")
        )
        .await
        .0,
        401
    );
}

#[test]
fn device_identity_survives_restart_and_differs_between_computers() {
    let first = tempfile::tempdir().unwrap();
    let second = tempfile::tempdir().unwrap();
    let path = first.path().join("personal.sqlite");
    let id = PersonalStore::new(path.clone()).device_id().unwrap();
    assert_eq!(PersonalStore::new(path).device_id().unwrap(), id);
    assert_ne!(
        PersonalStore::new(second.path().join("personal.sqlite"))
            .device_id()
            .unwrap(),
        id
    );
}

#[cfg(unix)]
#[tokio::test]
async fn lifecycle_rejects_unowned_targets_before_any_docker_command() {
    use std::os::unix::fs::PermissionsExt;
    let f = Fixture::new();
    let trap = f.dir.path().join("docker-trap");
    let log = f.dir.path().join("docker-called");
    std::fs::write(
        &trap,
        format!("#!/bin/sh\necho called >> '{}'\nexit 1\n", log.display()),
    )
    .unwrap();
    std::fs::set_permissions(&trap, std::fs::Permissions::from_mode(0o700)).unwrap();
    let catalog = f.service.catalog().unwrap();
    let mut legacy = f.service.resolve("notes", Some("dev"), None).unwrap();
    legacy.key = "legacy_local".into();
    legacy.engine = Some("postgres".into());
    legacy.url = "postgresql://fixture:fixture@127.0.0.1:1/legacy".into();
    legacy.env = "local".into();
    legacy.production = false;
    for (key, value) in [
        ("platform_origin", "https://console.example"),
        ("platform_user_id", "alice"),
        ("platform_resource_id", "notes-dev"),
        ("platform_project", "project"),
        ("platform_db", "notes"),
        ("local_volume", "quarry-local-pgdata"),
    ] {
        legacy.extra.insert(key.into(), value.into());
    }
    catalog
        .put_connection(&legacy.workspace, &legacy, false)
        .unwrap();
    let state = f.companion().with_docker_executable(trap.clone()).unwrap();
    let code = state.pairing_code()["code"].as_str().unwrap().to_owned();
    let app = companion_router(state);
    let origin = Some("https://console.example");
    let (_, paired, _) = send(
        app.clone(),
        "POST",
        "/api/pair",
        "alice",
        Some(json!({"code":code})),
        None,
        origin,
        None,
    )
    .await;
    let token = paired["token"].as_str().unwrap();
    let (_, platform, _) = send(
        app.clone(),
        "GET",
        "/api/platform",
        "",
        None,
        Some(token),
        origin,
        Some("alice"),
    )
    .await;
    let id = platform["resources"][0]["id"].as_str().unwrap();
    for (body, status) in [
        (json!({"engine":"postgres"}), 400),
        (json!({"resourceId":"unknown","engine":"postgres"}), 404),
        (json!({"resourceId":id,"engine":"postgres"}), 403),
    ] {
        assert_eq!(
            send(
                app.clone(),
                "POST",
                "/api/local/down",
                "",
                Some(body),
                Some(token),
                origin,
                Some("alice")
            )
            .await
            .0,
            status
        );
    }
    assert_eq!(send(app.clone(),"POST","/api/local/up","",Some(json!({"db":"notes","project":"project","sourceResourceId":"notes-dev","engine":"redis"})),Some(token),origin,Some("alice")).await.0,400);
    assert_eq!(
        send(
            app,
            "GET",
            "/api/local/status",
            "",
            None,
            Some(token),
            origin,
            Some("alice")
        )
        .await
        .1["items"],
        json!([])
    );
    let bob = CompanionState::new(
        f.service.clone(),
        f.auth.clone(),
        CompanionConfig {
            origin: "https://console.example".into(),
            user_id: "bob".into(),
            state_path: f.dir.path().join("bob/personal.sqlite"),
            port: 8766,
        },
    )
    .unwrap()
    .with_docker_executable(trap)
    .unwrap();
    let code = bob.pairing_code()["code"].as_str().unwrap().to_owned();
    let bob = companion_router(bob);
    let (_, paired, _) = send(
        bob.clone(),
        "POST",
        "/api/pair",
        "bob",
        Some(json!({"code":code})),
        None,
        origin,
        None,
    )
    .await;
    assert_eq!(
        send(
            bob,
            "POST",
            "/api/local/down",
            "",
            Some(json!({"resourceId":id})),
            paired["token"].as_str(),
            origin,
            Some("bob")
        )
        .await
        .0,
        404
    );
    assert!(
        !log.exists(),
        "unauthorized lifecycle must not invoke Docker at all"
    );
    assert_eq!(
        std::fs::read_to_string(f.dir.path().join("config.toml")).unwrap(),
        ""
    );
}

#[tokio::test]
#[ignore = "requires Docker with preloaded redis:7-alpine; creates only unique ylab-<hash> fixtures"]
async fn disposable_redis_sources_have_separate_ports_volumes_data_and_lifecycle() {
    use sha2::{Digest, Sha256};
    let output = std::process::Command::new("docker")
        .args(["image", "inspect", "redis:7-alpine"])
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "preload the disposable Redis image first"
    );
    let f = Fixture::new();
    let origin = format!("https://fixture-{}.invalid", uuid::Uuid::new_v4().simple());
    let sources = ["source-dev", "source-prod"];
    let namespaces = sources.map(|source| {
        format!(
            "ylab-{}",
            &format!(
                "{:x}",
                Sha256::digest(format!("{origin}:alice:{source}").as_bytes())
            )[..20]
        )
    });
    struct Cleanup(Vec<String>);
    impl Drop for Cleanup {
        fn drop(&mut self) {
            for namespace in &self.0 {
                let _ = std::process::Command::new("docker")
                    .args(["rm", "-f", &format!("{namespace}-redis")])
                    .output();
                let _ = std::process::Command::new("docker")
                    .args(["volume", "rm", &format!("{namespace}-redisdata")])
                    .output();
            }
        }
    }
    let _cleanup = Cleanup(namespaces.to_vec());
    let state = CompanionState::new(
        f.service.clone(),
        f.auth.clone(),
        CompanionConfig {
            origin: origin.clone(),
            user_id: "alice".into(),
            state_path: f.dir.path().join("alice/personal.sqlite"),
            port: 8766,
        },
    )
    .unwrap();
    let code = state.pairing_code()["code"].as_str().unwrap().to_owned();
    let app = companion_router(state);
    let origin = Some(origin.as_str());
    let (_, pair, _) = send(
        app.clone(),
        "POST",
        "/api/pair",
        "alice",
        Some(json!({"code":code})),
        None,
        origin,
        None,
    )
    .await;
    let token = pair["token"].as_str().unwrap();
    let mut ids = Vec::new();
    let mut ports = Vec::new();
    for (source, namespace) in sources.iter().zip(&namespaces) {
        let(status,value,_)=send(app.clone(),"POST","/api/local/up","",Some(json!({"db":"cache","project":"integration","sourceResourceId":source,"engine":"redis"})),Some(token),origin,Some("alice")).await;
        assert_eq!(status, 200, "{value}");
        ids.push(value["resources"][0]["id"].as_str().unwrap().to_owned());
        let container = format!("{namespace}-redis");
        let inspect = std::process::Command::new("docker")
            .args(["inspect", &container])
            .output()
            .unwrap();
        let info: Value = serde_json::from_slice(&inspect.stdout).unwrap();
        let binding = &info[0]["HostConfig"]["PortBindings"]["6379/tcp"][0];
        assert_eq!(binding["HostIp"], "127.0.0.1");
        ports.push(binding["HostPort"].as_str().unwrap().to_owned());
        assert!(
            info[0]["Mounts"]
                .as_array()
                .unwrap()
                .iter()
                .any(|v| v["Name"] == format!("{namespace}-redisdata"))
        );
        assert!(
            std::process::Command::new("docker")
                .args(["exec", &container, "redis-cli", "SET", "marker", source])
                .output()
                .unwrap()
                .status
                .success()
        );
    }
    assert_ne!(ports[0], ports[1]);
    assert_ne!(ids[0], ids[1]);
    for (id, source) in ids.iter().zip(sources) {
        let (status, value, _) = send(
            app.clone(),
            "POST",
            "/api/query",
            "",
            Some(json!({"resourceId":id,"sql":"GET marker"})),
            Some(token),
            origin,
            Some("alice"),
        )
        .await;
        assert_eq!(status, 200, "{value}");
        assert!(value.to_string().contains(source));
    }
    assert_eq!(
        send(
            app.clone(),
            "POST",
            "/api/local/down",
            "",
            Some(json!({"resourceId":ids[0]})),
            Some(token),
            origin,
            Some("alice")
        )
        .await
        .0,
        200
    );
    let inspect = std::process::Command::new("docker")
        .args([
            "inspect",
            "--format",
            "{{.State.Running}}",
            &format!("{}-redis", namespaces[0]),
        ])
        .output()
        .unwrap();
    assert_eq!(String::from_utf8_lossy(&inspect.stdout).trim(), "false");
    let (status, value, _) = send(
        app.clone(),
        "POST",
        "/api/query",
        "",
        Some(json!({"resourceId":ids[1],"sql":"GET marker"})),
        Some(token),
        origin,
        Some("alice"),
    )
    .await;
    assert_eq!(status, 200, "{value}");
    assert!(value.to_string().contains("source-prod"));
    let(status,value,_)=send(app.clone(),"POST","/api/local/up","",Some(json!({"db":"cache","project":"integration","sourceResourceId":sources[0],"engine":"redis"})),Some(token),origin,Some("alice")).await;
    assert_eq!(status, 200, "{value}");
    assert_eq!(value["resources"][0]["id"], ids[0]);
    let (status, value, _) = send(
        app,
        "POST",
        "/api/query",
        "",
        Some(json!({"resourceId":ids[0],"sql":"GET marker"})),
        Some(token),
        origin,
        Some("alice"),
    )
    .await;
    assert_eq!(status, 200, "{value}");
    assert!(value.to_string().contains("source-dev"));
    assert_eq!(
        std::fs::read_to_string(f.dir.path().join("config.toml")).unwrap(),
        ""
    );
}
