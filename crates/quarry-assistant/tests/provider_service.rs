use async_trait::async_trait;
use axum::{
    Json, Router,
    body::{Body, Bytes},
    extract::State,
    http::{HeaderMap, StatusCode},
    response::{IntoResponse, Response},
    routing::post,
};
use futures_util::{StreamExt, stream};
use quarry_assistant::{
    AssistantService, ConfigManager, ConversationStore, Credential, MetadataProvider,
    ResolvedContext, Result, SYSTEM_PROMPT, Target,
};
use serde_json::{Value, json};
use std::{
    collections::HashMap,
    convert::Infallible,
    sync::{
        Arc, Mutex,
        atomic::{AtomicUsize, Ordering},
    },
    time::Duration,
};
use tempfile::TempDir;

#[derive(Clone)]
struct Meta {
    calls: Arc<AtomicUsize>,
    fingerprint: Arc<Mutex<String>>,
}
#[async_trait]
impl MetadataProvider for Meta {
    async fn resolve(&self, target: &Target) -> Result<ResolvedContext> {
        Ok(ResolvedContext {
            target: target.clone(),
            engine: "postgres".into(),
            fingerprint: self.fingerprint.lock().unwrap().clone(),
            gui_workspace: Some("fixture".into()),
        })
    }
    async fn find_tables(&self, _: &ResolvedContext, query: &str, limit: usize) -> Result<Value> {
        self.calls.fetch_add(1, Ordering::SeqCst);
        assert_eq!(limit, 30);
        assert_eq!(query, "orders");
        Ok(json!({"tables":["public.orders"],"capped":false}))
    }
    async fn describe_tables(&self, _: &ResolvedContext, _: &[String]) -> Result<Value> {
        panic!("model did not request this catalog operation")
    }
}
#[derive(Clone)]
struct Provider {
    mode: &'static str,
    requests: Arc<Mutex<Vec<(HeaderMap, Value)>>>,
}
fn call(id: &str, name: &str, args: Value) -> Value {
    json!({"id":id,"type":"function","function":{"name":name,"arguments":args.to_string()}})
}
fn completion(content: &str, calls: Vec<Value>) -> Value {
    let finish = if calls.is_empty() {
        "stop"
    } else {
        "tool_calls"
    };
    json!({"choices":[{"message":{"content":content,"tool_calls":calls},"finish_reason":finish}]})
}
fn sse_data(value: Value) -> String {
    format!("data: {value}\n\n")
}
fn streamed(content: &str, calls: Vec<Value>, done: bool) -> Response {
    let mut wire = sse_data(json!({"choices":[{"delta":{"content":content}}]}));
    for (index, call) in calls.iter().enumerate() {
        let args = call["function"]["arguments"].as_str().unwrap();
        let cut = args.len() / 2;
        wire += &sse_data(
            json!({"choices":[{"delta":{"tool_calls":[{"index":index,"id":call["id"],"type":"function","function":{"name":call["function"]["name"],"arguments":&args[..cut]}}]}}]}),
        );
        wire += &sse_data(
            json!({"choices":[{"delta":{"tool_calls":[{"index":index,"function":{"arguments":&args[cut..]}}]}}]}),
        );
    }
    wire += &sse_data(
        json!({"choices":[{"delta":{},"finish_reason":if calls.is_empty(){"stop"}else{"tool_calls"}}]}),
    );
    if done {
        wire += "data: [DONE]\n\n";
    }
    // Split UTF-8 and JSON across transport chunks: parser must buffer bytes.
    let chunks = wire
        .as_bytes()
        .chunks(3)
        .map(|b| Ok::<_, Infallible>(Bytes::copy_from_slice(b)))
        .collect::<Vec<_>>();
    (
        [("content-type", "text/event-stream")],
        Body::from_stream(stream::iter(chunks)),
    )
        .into_response()
}
async fn provider(
    State(state): State<Provider>,
    headers: HeaderMap,
    Json(body): Json<Value>,
) -> Response {
    let round = {
        let mut req = state.requests.lock().unwrap();
        req.push((headers, body));
        req.len()
    };
    match state.mode {
        "tools" => match round {
            1 => streamed(
                "你好",
                vec![call("catalog", "find_tables", json!({"query":"orders"}))],
                true,
            ),
            2 => Json(completion(
                "",
                vec![call(
                    "proposal",
                    "propose_sql",
                    json!({"sql":"SELECT id FROM public.orders"}),
                )],
            ))
            .into_response(),
            _ => Json(completion("Ready", vec![])).into_response(),
        },
        "explain-injection" => Json(completion(
            "",
            vec![call(
                "forbidden",
                "propose_sql",
                json!({"sql":"DELETE FROM public.orders"}),
            )],
        ))
        .into_response(),
        "truncated" => streamed("partial", vec![], false),
        "headers-stall" => std::future::pending::<Response>().await,
        "stall" => {
            let first = sse_data(json!({"choices":[{"delta":{"content":"started"}}]}));
            let data = stream::once(async move { Ok::<_, Infallible>(Bytes::from(first)) })
                .chain(stream::pending());
            (
                [("content-type", "text/event-stream")],
                Body::from_stream(data),
            )
                .into_response()
        }
        "redirect" => (
            StatusCode::TEMPORARY_REDIRECT,
            [("location", "/should-not-receive-credentials")],
            "provider-private-error-body",
        )
            .into_response(),
        _ => Json(completion("read-only explanation", vec![])).into_response(),
    }
}
struct Fixture {
    service: AssistantService,
    requests: Arc<Mutex<Vec<(HeaderMap, Value)>>>,
    meta: Meta,
    _dir: TempDir,
    server: tokio::task::JoinHandle<()>,
}
impl Drop for Fixture {
    fn drop(&mut self) {
        self.service.close();
        self.server.abort();
    }
}
async fn fixture(mode: &'static str) -> Fixture {
    let dir = tempfile::tempdir().unwrap();
    let requests = Arc::new(Mutex::new(Vec::new()));
    let state = Provider {
        mode,
        requests: requests.clone(),
    };
    let app = Router::new()
        .route("/v1/chat/completions", post(provider))
        .with_state(state);
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let base = format!("http://{}/v1", listener.local_addr().unwrap());
    let server = tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
    let config = ConfigManager::new(dir.path().join("config.toml"), HashMap::new(), "zh_CN");
    config
        .configure(
            &base,
            "fixture-model",
            Credential::Stored("fixture-secret".into()),
            true,
            "disabled",
            5,
        )
        .unwrap();
    let meta = Meta {
        calls: Arc::new(AtomicUsize::new(0)),
        fingerprint: Arc::new(Mutex::new("fp".into())),
    };
    let service = AssistantService::new(
        ConversationStore::new(dir.path().join("assistant/history.sqlite3")),
        config,
        Arc::new(meta.clone()),
    );
    Fixture {
        service,
        requests,
        meta,
        _dir: dir,
        server,
    }
}
async fn create(f: &Fixture) -> Value {
    f.service
        .create_conversation(
            json!({"db":"shop","env":"dev","workspace":"/fixture","sql":"SELECT 1"}),
        )
        .await
        .unwrap()
}
async fn collect(f: &Fixture, doc: &Value, explain: bool) -> Vec<Value> {
    let mut stream=f.service.start_chat(json!({"conversationId":doc["id"],"requestId":"request-fixture","revision":doc["revision"],"sql":doc["sql"],"prompt":if explain{"Explain"}else{"Write SQL"},"explain":explain}),true).await.unwrap();
    let mut events = Vec::new();
    while let Some(event) = tokio::time::timeout(Duration::from_secs(3), stream.next())
        .await
        .unwrap()
    {
        events.push(event);
    }
    events
}
#[tokio::test]
async fn real_http_stream_tools_proposal_json_fallback_and_apply_are_separate() {
    let f = fixture("tools").await;
    let doc = create(&f).await;
    let events = collect(&f, &doc, false).await;
    assert_eq!(events.last().unwrap()["type"], "done");
    assert_eq!(f.meta.calls.load(Ordering::SeqCst), 1);
    assert!(
        events
            .iter()
            .any(|e| e["type"] == "text_delta" && e["delta"] == "你好")
    );
    let proposal = events.iter().find(|e| e["type"] == "sql_proposal").unwrap();
    let saved = f
        .service
        .get_conversation(doc["id"].as_str().unwrap())
        .await
        .unwrap();
    assert_eq!(saved["sql"], "SELECT 1");
    let applied=f.service.apply_conversation(json!({"id":doc["id"],"revision":0,"sql":proposal["sql"],"messageId":proposal["messageId"]})).await.unwrap();
    assert_eq!(applied["sql"], "SELECT id FROM public.orders");
    let requests = f.requests.lock().unwrap();
    assert_eq!(requests.len(), 3);
    for (headers, body) in requests.iter() {
        assert_eq!(headers["authorization"], "Bearer fixture-secret");
        assert_eq!(headers["x-bf-vk"], "fixture-secret");
        assert_eq!(headers["x-bf-passthrough-extra-params"], "true");
        assert_eq!(headers["x-bf-lh-session-id"], doc["id"].as_str().unwrap());
        assert_eq!(body["thinking"]["type"], "disabled");
        assert_eq!(body["messages"][0]["content"], SYSTEM_PROMPT);
        assert!(!body["messages"].to_string().contains("fixture-secret"));
    }
    assert!(
        requests[0].1["messages"]
            .to_string()
            .contains("Simplified Chinese")
    );
}

#[cfg(unix)]
#[tokio::test]
async fn long_running_service_reads_fresh_command_bearer_for_each_request() {
    let f = fixture("explanation").await;
    let token_path = f._dir.path().join("ephemeral-token");
    std::fs::write(&token_path, "first-private-bearer\n").unwrap();
    let path = &f.service.config().config_path;
    let mut config: toml_edit::DocumentMut =
        std::fs::read_to_string(path).unwrap().parse().unwrap();
    config["assistant"]["auth"] = toml_edit::value("command");
    config["assistant"]["bifrost"] = toml_edit::value(false);
    let mut argv = toml_edit::Array::new();
    argv.push("/bin/cat");
    argv.push(token_path.to_str().unwrap());
    config["assistant"]["credential_command"] = toml_edit::value(argv);
    std::fs::write(path, config.to_string()).unwrap();
    let doc = create(&f).await;
    assert_eq!(
        collect(&f, &doc, true).await.last().unwrap()["type"],
        "done"
    );
    std::fs::write(&token_path, "second-private-bearer\n").unwrap();
    assert_eq!(
        collect(&f, &doc, true).await.last().unwrap()["type"],
        "done"
    );
    {
        let requests = f.requests.lock().unwrap();
        assert_eq!(requests.len(), 2);
        assert_eq!(
            requests[0].0["authorization"],
            "Bearer first-private-bearer"
        );
        assert_eq!(
            requests[1].0["authorization"],
            "Bearer second-private-bearer"
        );
        assert!(
            requests
                .iter()
                .all(|(headers, body)| !headers.contains_key("x-bf-vk")
                    && !body.to_string().contains("private-bearer"))
        );
    }
    let history = f
        .service
        .get_conversation(doc["id"].as_str().unwrap())
        .await
        .unwrap();
    assert!(!history.to_string().contains("private-bearer"));
    assert!(f.service.config().load().unwrap().api_key.is_empty());
}
#[tokio::test]
async fn explanation_cannot_propose_sql_even_when_provider_ignores_tool_list() {
    let f = fixture("explain-injection").await;
    let doc = create(&f).await;
    let events = collect(&f, &doc, true).await;
    assert_eq!(events.last().unwrap()["type"], "error");
    assert!(!events.iter().any(|e| e["type"] == "sql_proposal"));
    assert_eq!(
        f.service
            .get_conversation(doc["id"].as_str().unwrap())
            .await
            .unwrap()["sql"],
        "SELECT 1"
    );
    let requests = f.requests.lock().unwrap();
    assert_eq!(requests[0].1["tools"].as_array().unwrap().len(), 2);
}
#[tokio::test]
async fn truncated_stream_does_not_persist_a_complete_reply_or_proposal() {
    let f = fixture("truncated").await;
    let doc = create(&f).await;
    let events = collect(&f, &doc, false).await;
    assert_eq!(events.last().unwrap()["type"], "error");
    let history = f
        .service
        .get_conversation(doc["id"].as_str().unwrap())
        .await
        .unwrap();
    assert_eq!(
        history["messages"].as_array().unwrap().last().unwrap()["status"],
        "interrupted"
    );
    assert_eq!(history["sql"], "SELECT 1");
}
#[tokio::test]
async fn cancellation_interrupts_pending_network_read_and_releases_conversation() {
    let f = fixture("stall").await;
    let doc = create(&f).await;
    let id = doc["id"].as_str().unwrap();
    let body = json!({"conversationId":id,"requestId":"cancel-me","revision":0,"sql":"SELECT 1","prompt":"explain"});
    let mut stream = f.service.start_chat(body.clone(), true).await.unwrap();
    while let Some(event) = tokio::time::timeout(Duration::from_secs(2), stream.next())
        .await
        .unwrap()
    {
        if event["type"] == "text_delta" {
            break;
        }
    }
    assert_eq!(f.service.cancel(id, "wrong")["cancelled"], false);
    assert_eq!(f.service.cancel(id, "cancel-me")["cancelled"], true);
    let mut end = None;
    while let Some(event) = tokio::time::timeout(Duration::from_secs(2), stream.next())
        .await
        .unwrap()
    {
        end = Some(event);
    }
    assert_eq!(end.unwrap()["type"], "error");
    let next = f.service.start_chat(body, true).await.unwrap();
    drop(next);
    tokio::time::timeout(Duration::from_secs(2), async {
        while f.service.cancel(id, "cancel-me")["cancelled"] != false {
            tokio::task::yield_now().await;
            tokio::time::sleep(Duration::from_millis(5)).await;
        }
    })
    .await
    .expect("dropping the stream must release its active request");
}
#[tokio::test]
async fn changed_connection_keeps_history_readable_but_blocks_new_generation() {
    let f = fixture("normal").await;
    let doc = create(&f).await;
    *f.meta.fingerprint.lock().unwrap() = "changed".into();
    let read = f
        .service
        .get_conversation(doc["id"].as_str().unwrap())
        .await
        .unwrap();
    assert_eq!(read["connectionAvailable"], false);
    let err = match f
        .service
        .start_chat(
            json!({"conversationId":doc["id"],"revision":0,"prompt":"explain","sql":"SELECT 1"}),
            true,
        )
        .await
    {
        Err(e) => e,
        Ok(_) => panic!("changed connection accepted"),
    };
    assert!(err.message.contains("changed"));
    assert!(f.requests.lock().unwrap().is_empty());
}
#[tokio::test]
async fn provider_redirects_are_not_followed_and_private_error_body_is_not_exposed() {
    let f = fixture("redirect").await;
    let doc = create(&f).await;
    let events = collect(&f, &doc, false).await;
    let last = events.last().unwrap();
    assert_eq!(last["type"], "error");
    assert!(last["error"].as_str().unwrap().contains("307"));
    assert!(!last.to_string().contains("provider-private-error-body"));
    assert_eq!(f.requests.lock().unwrap().len(), 1);
}

#[tokio::test]
async fn stalled_headers_obey_configured_timeout_and_save_interrupted_history() {
    let f = fixture("headers-stall").await;
    let config = f.service.config().load().unwrap();
    f.service
        .config()
        .configure(
            &config.base_url,
            &config.model,
            Credential::Stored("fixture-secret".into()),
            true,
            "disabled",
            1,
        )
        .unwrap();
    let doc = create(&f).await;
    let events = collect(&f, &doc, false).await;
    assert_eq!(events.last().unwrap()["type"], "error");
    assert!(
        events.last().unwrap()["error"]
            .as_str()
            .unwrap()
            .contains("timed out")
    );
    let saved = f
        .service
        .get_conversation(doc["id"].as_str().unwrap())
        .await
        .unwrap();
    assert_eq!(
        saved["messages"].as_array().unwrap().last().unwrap()["status"],
        "interrupted"
    );
    assert_eq!(saved["revision"], 0);
    assert_eq!(saved["sql"], "SELECT 1");
}
#[tokio::test]
async fn close_cancels_four_pending_generations_and_admission_recovers() {
    let f = fixture("headers-stall").await;
    let mut streams = Vec::new();
    let mut ids = Vec::new();
    for index in 0..4 {
        let doc = create(&f).await;
        ids.push(doc["id"].as_str().unwrap().to_owned());
        streams.push(f.service.start_chat(json!({"conversationId":doc["id"],"requestId":format!("pending-{index}"),"revision":0,"sql":"SELECT 1","prompt":"Explain"}),true).await.unwrap());
    }
    let fifth = create(&f).await;
    let body = json!({"conversationId":fifth["id"],"requestId":"fifth","revision":0,"sql":"SELECT 1","prompt":"Explain"});
    assert!(match f.service.start_chat(body.clone(), true).await {
        Err(error) => error.message.contains("busy"),
        Ok(_) => false,
    });
    f.service.close();
    for stream in &mut streams {
        let mut terminal = None;
        while let Some(event) = tokio::time::timeout(Duration::from_secs(2), stream.next())
            .await
            .unwrap()
        {
            terminal = Some(event);
        }
        assert_eq!(terminal.unwrap()["type"], "error");
    }
    for id in ids {
        let saved = f.service.get_conversation(&id).await.unwrap();
        assert_eq!(
            saved["messages"].as_array().unwrap().last().unwrap()["status"],
            "interrupted"
        );
        assert_eq!(saved["sql"], "SELECT 1");
    }
    // Leases are released on completion, so a fifth conversation can start.
    let next = f.service.start_chat(body, true).await.unwrap();
    drop(next);
}

#[tokio::test]
async fn hosted_override_uses_request_identity_without_reading_or_creating_local_config() {
    let f = fixture("plain").await;
    let mut config = f.service.config().load().unwrap();
    config.api_key = "request-scoped-iam".into();
    config.bifrost = false;
    let missing = f._dir.path().join("never-created/config.toml");
    let service = AssistantService::new(
        ConversationStore::memory().unwrap(),
        ConfigManager::new(&missing, HashMap::new(), "en"),
        Arc::new(f.meta.clone()),
    )
    .with_model_config(config)
    .unwrap();
    let doc = service
        .create_conversation(json!({"db":"shop","env":"dev","sql":"SELECT 1"}))
        .await
        .unwrap();
    let mut stream = service.start_chat(json!({"conversationId":doc["id"],"requestId":"ephemeral","revision":0,"sql":"SELECT 1","prompt":"Explain","explain":true,"replyLanguage":"en"}),true).await.unwrap();
    let mut done = false;
    while let Some(event) = stream.next().await {
        done |= event["type"] == "done";
    }
    assert!(done);
    assert!(!missing.parent().unwrap().exists());
    let requests = f.requests.lock().unwrap();
    assert_eq!(
        requests.last().unwrap().0["authorization"],
        "Bearer request-scoped-iam"
    );
    assert!(!requests.last().unwrap().0.contains_key("x-bf-vk"));
}
