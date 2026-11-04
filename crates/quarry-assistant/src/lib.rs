//! SQL explanation/proposals with bounded metadata tools; never executes model SQL.
mod config;
mod error;
mod provider;
mod store;
use async_trait::async_trait;
pub use config::{ConfigManager, Credential, ModelConfig, resolve_language};
pub use error::Result as AssistantResult;
pub use error::{AssistantError, Result};
use futures_util::Stream;
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::{
    collections::HashMap,
    pin::Pin,
    sync::{Arc, Mutex},
    task::{Context, Poll},
    time::Duration,
};
pub use store::ConversationStore;
use tokio::{sync::mpsc, time::Instant};
use tokio_util::sync::CancellationToken;
use uuid::Uuid;

pub const SYSTEM_PROMPT: &str = include_str!("../assets/system-prompt.txt");
pub const MAX_SQL: usize = 64_000;
pub const MAX_PROMPT: usize = 8_000;
pub const MAX_CONTEXT: usize = 120_000;
pub const MAX_RESPONSE: usize = 64_000;
pub const MAX_ROUNDS: usize = 6;
pub const MAX_TOOL_CALLS: usize = 16;
pub fn tools() -> Vec<Value> {
    serde_json::from_str(include_str!("../assets/tools.json"))
        .expect("bundled tool schema is valid")
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
pub struct Target {
    pub db: String,
    pub env: Option<String>,
    pub workspace: Option<String>,
}
#[derive(Clone, Debug)]
pub struct ResolvedContext {
    pub target: Target,
    pub engine: String,
    pub fingerprint: String,
    pub gui_workspace: Option<String>,
}
/// Implementors expose catalog operations only, scoped to one resolved target. The
/// assistant deliberately has no arbitrary SQL or business-row execution method.
#[async_trait]
pub trait MetadataProvider: Send + Sync {
    async fn resolve(&self, target: &Target) -> Result<ResolvedContext>;
    async fn find_tables(
        &self,
        context: &ResolvedContext,
        query: &str,
        limit: usize,
    ) -> Result<Value>;
    async fn describe_tables(&self, context: &ResolvedContext, tables: &[String]) -> Result<Value>;
}
struct Active {
    request_id: String,
    cancel: CancellationToken,
}
struct Inner {
    store: ConversationStore,
    config: ConfigManager,
    model_override: Option<ModelConfig>,
    metadata: Arc<dyn MetadataProvider>,
    active: Mutex<HashMap<String, Active>>,
}
#[derive(Clone)]
pub struct AssistantService {
    inner: Arc<Inner>,
}
struct Lease {
    inner: Arc<Inner>,
    conversation_id: String,
    request_id: String,
}
impl Drop for Lease {
    fn drop(&mut self) {
        let mut active = self.inner.active.lock().unwrap_or_else(|e| e.into_inner());
        if active
            .get(&self.conversation_id)
            .is_some_and(|a| a.request_id == self.request_id)
        {
            active.remove(&self.conversation_id);
        }
    }
}
/// Drop closes upstream generation. HTTP adapters should serialize each Value as
/// one NDJSON line; the `requestId` and `conversationId` envelope is always present.
pub struct ChatStream {
    receiver: mpsc::Receiver<Value>,
    cancel: CancellationToken,
}
impl Stream for ChatStream {
    type Item = Value;
    fn poll_next(mut self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<Option<Self::Item>> {
        self.receiver.poll_recv(cx)
    }
}
impl Drop for ChatStream {
    fn drop(&mut self) {
        self.cancel.cancel();
    }
}
fn field_text<'a>(body: &'a Value, key: &str, limit: usize, required: bool) -> Result<&'a str> {
    error::text(body.get(key).unwrap_or(&Value::Null), key, limit, required)
}
fn revision(body: &Value) -> Result<u64> {
    body["revision"]
        .as_u64()
        .ok_or_else(|| AssistantError::invalid("A nonnegative SQL revision is required"))
}
fn target(body: &Value) -> Result<Target> {
    let db = field_text(body, "db", 512, true)?.to_owned();
    let optional = |key: &str| -> Result<Option<String>> {
        match body.get(key) {
            None | Some(Value::Null) => Ok(None),
            Some(v) => Ok(Some(error::text(v, key, 4096, false)?.to_owned())),
        }
    };
    Ok(Target {
        db,
        env: optional("env")?,
        workspace: optional("workspace")?,
    })
}
impl AssistantService {
    pub fn new(
        store: ConversationStore,
        config: ConfigManager,
        metadata: Arc<dyn MetadataProvider>,
    ) -> Self {
        Self {
            inner: Arc::new(Inner {
                store,
                config,
                model_override: None,
                metadata,
                active: Mutex::new(HashMap::new()),
            }),
        }
    }
    /// Supply host-selected endpoint/model and a request-scoped IAM credential.
    /// This never writes credentials or reads model configuration from disk.
    pub fn with_model_config(mut self, config: ModelConfig) -> Result<Self> {
        config.validate()?;
        std::sync::Arc::get_mut(&mut self.inner)
            .ok_or_else(|| AssistantError::invalid("Configure the assistant before sharing it"))?
            .model_override = Some(config);
        Ok(self)
    }
    pub fn config(&self) -> &ConfigManager {
        &self.inner.config
    }
    pub fn store(&self) -> &ConversationStore {
        &self.inner.store
    }
    async fn history<T: Send + 'static>(
        &self,
        f: impl FnOnce(ConversationStore) -> Result<T> + Send + 'static,
    ) -> Result<T> {
        let store = self.inner.store.clone();
        tokio::task::spawn_blocking(move || f(store))
            .await
            .map_err(|_| AssistantError::unavailable("Cannot access SQL assistant history."))?
    }
    async fn resolve(&self, target: &Target) -> Result<ResolvedContext> {
        let context=self.inner.metadata.resolve(target).await.map_err(|_|AssistantError::invalid("The database connection is unavailable or changed. Start a new conversation for this connection."))?;
        if !["postgres", "mysql", "sqlite"].contains(&context.engine.as_str()) {
            return Err(AssistantError::invalid(
                "SQL Assistant supports PostgreSQL, MySQL and SQLite connections",
            ));
        }
        Ok(context)
    }
    async fn context(&self, doc: &Value) -> Result<ResolvedContext> {
        let resolved = self.resolve(&target(doc)?).await?;
        let id = field_text(doc, "id", 100, true)?.to_owned();
        let fp = self.history(move |s| s.fingerprint(&id)).await?;
        if resolved.fingerprint != fp {
            return Err(AssistantError::invalid(
                "The database connection changed. Start a new conversation for this connection.",
            ));
        }
        Ok(resolved)
    }
    async fn view(&self, mut doc: Value) -> Value {
        match self.context(&doc).await {
            Ok(c) => {
                doc["guiWorkspace"] = json!(c.gui_workspace.or(c.target.workspace));
                doc["connectionAvailable"] = json!(true);
            }
            Err(_) => {
                doc["guiWorkspace"] = Value::Null;
                doc["connectionAvailable"] = json!(false);
            }
        }
        doc
    }
    pub async fn create_conversation(&self, body: Value) -> Result<Value> {
        let ctx = self.resolve(&target(&body)?).await?;
        let sql = if body.get("sql").is_none() {
            String::new()
        } else {
            field_text(&body, "sql", MAX_SQL, false)?.to_owned()
        };
        let title = body["title"].as_str().map(str::to_owned);
        let doc = self
            .history(move |s| s.create(&ctx.target, &sql, &ctx.fingerprint, title.as_deref()))
            .await?;
        Ok(self.view(doc).await)
    }
    pub async fn get_conversation(&self, id: &str) -> Result<Value> {
        let id = id.to_owned();
        let doc = self.history(move |s| s.get(&id)).await?;
        Ok(self.view(doc).await)
    }
    pub async fn list_conversations(&self) -> Result<Value> {
        let docs = self.history(|s| s.list()).await?;
        let mut result = Vec::new();
        for d in docs {
            result.push(self.view(d).await);
        }
        Ok(json!({"conversations":result}))
    }
    pub async fn update_conversation(&self, body: Value) -> Result<Value> {
        let id = field_text(&body, "id", 100, true)?.to_owned();
        let rev = revision(&body)?;
        let sql = field_text(&body, "sql", 200_000, false)?.to_owned();
        let doc = self.get_conversation(&id).await?;
        self.context(&doc).await?;
        let result = self.history(move |s| s.update(&id, rev, &sql)).await?;
        Ok(self.view(result).await)
    }
    pub async fn apply_conversation(&self, body: Value) -> Result<Value> {
        let id = field_text(&body, "id", 100, true)?.to_owned();
        let rev = revision(&body)?;
        let sql = field_text(&body, "sql", 200_000, false)?.to_owned();
        let message_id = field_text(&body, "messageId", 100, true)?.to_owned();
        let explicit = match body.get("explicit") {
            None => false,
            Some(Value::Bool(v)) => *v,
            _ => return Err(AssistantError::invalid("explicit must be a boolean")),
        };
        let doc = self.get_conversation(&id).await?;
        self.context(&doc).await?;
        let result = self
            .history(move |s| s.apply(&id, rev, &sql, &message_id, explicit))
            .await?;
        Ok(self.view(result).await)
    }
    pub async fn restore_conversation(&self, body: Value) -> Result<Value> {
        let id = field_text(&body, "id", 100, true)?.to_owned();
        let rev = revision(&body)?;
        let checkpoint_id = field_text(&body, "checkpointId", 100, true)?.to_owned();
        let doc = self.get_conversation(&id).await?;
        self.context(&doc).await?;
        let result = self
            .history(move |s| s.restore(&id, rev, &checkpoint_id))
            .await?;
        Ok(self.view(result).await)
    }
    pub fn cancel(&self, conversation_id: &str, request_id: &str) -> Value {
        let active = self.inner.active.lock().unwrap_or_else(|e| e.into_inner());
        let cancelled = active.get(conversation_id).is_some_and(|a| {
            if a.request_id == request_id {
                a.cancel.cancel();
                true
            } else {
                false
            }
        });
        json!({"cancelled":cancelled})
    }
    pub fn close(&self) {
        for active in self
            .inner
            .active
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .values()
        {
            active.cancel.cancel();
        }
    }
    /// Call inside a Tokio runtime. Any preflight failure is an ordinary typed
    /// error; accepted requests finish with `done` or `error`, never partial SQL.
    pub async fn start_chat(&self, body: Value, gui: bool) -> Result<ChatStream> {
        let id = field_text(&body, "conversationId", 100, true)?.to_owned();
        let request_id = match body.get("requestId") {
            Some(v) => error::text(v, "Request ID", 100, true)?.to_owned(),
            None => Uuid::new_v4().simple().to_string(),
        };
        let doc = self.get_conversation(&id).await?;
        let ctx = self.context(&doc).await?;
        let config = match &self.inner.model_override {
            Some(config) => config.clone(),
            None => self.inner.config.load()?,
        };
        let sql = error::text(
            body.get("sql").unwrap_or(&doc["sql"]),
            "SQL",
            MAX_SQL,
            false,
        )?
        .to_owned();
        let explain = body["explain"].as_bool().unwrap_or(false);
        if explain && sql.trim().is_empty() {
            return Err(AssistantError::invalid(
                "Write some SQL before asking for an explanation.",
            ));
        }
        let default_prompt = if explain {
            if gui {
                "Explain the current SQL."
            } else {
                "请用中文解读这段 SQL。"
            }
        } else {
            ""
        };
        let prompt = error::text(
            body.get("prompt")
                .filter(|v| v.as_str().is_some_and(|s| !s.is_empty()))
                .unwrap_or(&json!(default_prompt)),
            "Message",
            MAX_PROMPT,
            true,
        )?
        .to_owned();
        let cancel = CancellationToken::new();
        {
            let mut active = self.inner.active.lock().unwrap_or_else(|e| e.into_inner());
            if active.contains_key(&id) {
                return Err(AssistantError::conflict(
                    "This conversation is already generating a reply.",
                ));
            }
            if active.len() >= 4 {
                return Err(AssistantError::unavailable(
                    "The assistant is busy. Wait for another reply to finish.",
                ));
            }
            active.insert(
                id.clone(),
                Active {
                    request_id: request_id.clone(),
                    cancel: cancel.clone(),
                },
            );
        }
        let lease = Lease {
            inner: self.inner.clone(),
            conversation_id: id.clone(),
            request_id: request_id.clone(),
        };
        let rev = revision(&body)?;
        let id_copy = id.clone();
        let sql_copy = sql.clone();
        let doc = self
            .history(move |s| s.update(&id_copy, rev, &sql_copy))
            .await?;
        let base_revision = doc["revision"].as_u64().unwrap();
        let language = if gui && self.inner.model_override.is_some() {
            let language = body["replyLanguage"].as_str().unwrap_or("zh");
            if !["zh", "en"].contains(&language) {
                return Err(AssistantError::invalid("Invalid reply language"));
            }
            Some(language.to_owned())
        } else if gui {
            self.inner.config.preferences()?["resolvedReplyLanguage"]
                .as_str()
                .map(str::to_owned)
        } else {
            None
        };
        let messages = messages(&doc, &ctx, &sql, &prompt, language.as_deref());
        let id_copy = id.clone();
        let prompt_copy = prompt.clone();
        let title = if explain {
            json!({"title":sql})
        } else {
            json!({})
        };
        let doc = self
            .history(move |s| s.append_message(&id_copy, "user", &prompt_copy, title))
            .await?;
        let (sender, receiver) = mpsc::channel(32);
        let service = self.clone();
        let stream_cancel = cancel.clone();
        tokio::spawn(async move {
            let _lease = lease;
            let envelope = json!({"conversationId":id,"requestId":request_id});
            let accumulated = Arc::new(Mutex::new(String::new()));
            let initial = service.view(doc.clone()).await;
            let _ = send(
                &sender,
                &envelope,
                json!({"type":"request","conversation":initial}),
            )
            .await;
            let result = service
                .turn(
                    &id,
                    &doc,
                    config,
                    messages,
                    base_revision,
                    explain,
                    &cancel,
                    &sender,
                    &envelope,
                    &accumulated,
                )
                .await;
            if let Err(error) = result {
                let partial = accumulated
                    .lock()
                    .unwrap_or_else(|e| e.into_inner())
                    .clone();
                let id_copy = id.clone();
                let _ = service
                    .history(move |s| {
                        s.append_message(
                            &id_copy,
                            "assistant",
                            if partial.is_empty() {
                                "Reply interrupted. Try again."
                            } else {
                                &partial
                            },
                            json!({"status":"interrupted"}),
                        )
                    })
                    .await;
                let _ = send(
                    &sender,
                    &envelope,
                    json!({"type":"error","error":error.message}),
                )
                .await;
            }
        });
        Ok(ChatStream {
            receiver,
            cancel: stream_cancel,
        })
    }
    #[allow(clippy::too_many_arguments)]
    async fn turn(
        &self,
        id: &str,
        doc: &Value,
        config: ModelConfig,
        mut messages: Vec<Value>,
        base_revision: u64,
        explain: bool,
        cancel: &CancellationToken,
        sender: &mpsc::Sender<Value>,
        envelope: &Value,
        accumulated: &Arc<Mutex<String>>,
    ) -> Result<()> {
        let deadline = Instant::now() + Duration::from_secs(180);
        let mut proposal = None::<String>;
        let mut total_tools = 0;
        let all_tools = tools();
        let allowed_tools = if explain { &all_tools[..2] } else { &all_tools };
        for _ in 0..MAX_ROUNDS {
            if cancel.is_cancelled() || sender.is_closed() {
                return Err(AssistantError::cancelled());
            }
            if Instant::now() >= deadline {
                return Err(AssistantError::invalid(
                    "Model response exceeded the request time limit.",
                ));
            }
            self.context(doc).await?;
            if serde_json::to_vec(&messages)
                .map_err(|_| AssistantError::invalid("Invalid model context"))?
                .len()
                > MAX_CONTEXT
            {
                return Err(AssistantError::invalid(
                    "Conversation context is too large. Start a new conversation or narrow the tables.",
                ));
            }
            let complete = provider::round(
                &config,
                &messages,
                allowed_tools,
                id,
                cancel,
                deadline,
                provider::Sink {
                    sender,
                    envelope,
                    accumulated,
                },
            )
            .await?;
            if complete.calls.is_empty() {
                self.context(doc).await?;
                let content = accumulated
                    .lock()
                    .unwrap_or_else(|e| e.into_inner())
                    .clone();
                let message_id = Uuid::new_v4().simple().to_string();
                let mut extra =
                    json!({"id":message_id,"status":"complete","baseRevision":base_revision});
                if let Some(sql) = &proposal {
                    extra["sql"] = json!(sql);
                }
                let id_copy = id.to_owned();
                let saved = self
                    .history(move |s| s.append_message(&id_copy, "assistant", &content, extra))
                    .await?;
                if let Some(sql) = proposal {
                    let _ = send(
                        sender,
                        envelope,
                        json!({"type":"sql_proposal","sql":sql,"messageId":message_id}),
                    )
                    .await;
                }
                // A disconnected client after persistence does not create a second interrupted message.
                let _ = send(
                    sender,
                    envelope,
                    json!({"type":"done","conversation":self.view(saved).await}),
                )
                .await;
                return Ok(());
            }
            total_tools += complete.calls.len();
            if total_tools > MAX_TOOL_CALLS {
                return Err(AssistantError::invalid(
                    "Metadata lookup limit reached. Narrow the tables and try again.",
                ));
            }
            messages.push(json!({"role":"assistant","content":if complete.content.is_empty(){Value::Null}else{json!(complete.content)},"tool_calls":complete.calls}));
            for call in complete.calls {
                if cancel.is_cancelled() {
                    return Err(AssistantError::cancelled());
                }
                let context = self.context(doc).await?;
                let name = call["function"]["name"].as_str().unwrap();
                let status = match name {
                    "find_tables" => "Finding tables…",
                    "describe_tables" => "Reading table structure…",
                    _ => "Preparing SQL…",
                };
                send(
                    sender,
                    envelope,
                    json!({"type":"tool_status","name":name,"text":status}),
                )
                .await?;
                let args: Value = serde_json::from_str(
                    call["function"]["arguments"].as_str().unwrap(),
                )
                .map_err(|_| {
                    AssistantError::invalid("Model returned invalid metadata tool arguments.")
                })?;
                let result = if name == "propose_sql" {
                    if explain || proposal.is_some() {
                        return Err(AssistantError::invalid(
                            "SQL proposals are not allowed in this explanation, or a proposal was already provided.",
                        ));
                    }
                    if args
                        .as_object()
                        .is_none_or(|a| a.len() != 1 || !a.contains_key("sql"))
                    {
                        return Err(AssistantError::invalid(
                            "Model SQL proposal must contain only the complete SQL.",
                        ));
                    }
                    proposal = Some(field_text(&args, "sql", MAX_SQL, true)?.to_owned());
                    json!({"status":"proposed","note":"SQL proposal recorded for review; no query executed and no editor changed."})
                } else {
                    let future = self.metadata_tool(&context, name, &args);
                    let metadata = tokio::select! {_=cancel.cancelled()=>return Err(AssistantError::cancelled()),r=tokio::time::timeout_at(deadline,future)=>r.unwrap_or_else(|_|Err(AssistantError::invalid("Metadata lookup timed out.")))};
                    metadata.unwrap_or_else(|_|json!({"error":"Metadata is unavailable for these arguments. Check exact table names; do not invent their schema."}))
                };
                let serialized = serde_json::to_string(&result).unwrap_or_default();
                let serialized = if serialized.chars().count() > 40_000 {
                    json!({"error":"Metadata exceeds the context limit. Describe fewer tables."})
                        .to_string()
                } else {
                    serialized
                };
                messages
                    .push(json!({"role":"tool","tool_call_id":call["id"],"content":serialized}));
            }
        }
        Err(AssistantError::invalid(
            "Assistant turn limit reached. Narrow your request and try again.",
        ))
    }
    async fn metadata_tool(
        &self,
        context: &ResolvedContext,
        name: &str,
        args: &Value,
    ) -> Result<Value> {
        let object = args.as_object().ok_or_else(|| {
            AssistantError::invalid("Model metadata tool arguments must be an object.")
        })?;
        match name {
            "find_tables" => {
                if object.keys().any(|k| k != "query") {
                    return Err(AssistantError::invalid(
                        "Unsupported find_tables arguments.",
                    ));
                }
                let empty = json!("");
                let query = error::text(
                    args.get("query").unwrap_or(&empty),
                    "Table search",
                    200,
                    false,
                )?;
                self.inner.metadata.find_tables(context, query, 30).await
            }
            "describe_tables" => {
                if object.len() != 1 {
                    return Err(AssistantError::invalid(
                        "describe_tables requires 1–8 exact table names.",
                    ));
                }
                let tables = args["tables"].as_array().ok_or_else(|| {
                    AssistantError::invalid("describe_tables requires 1–8 exact table names.")
                })?;
                if !(1..=8).contains(&tables.len()) {
                    return Err(AssistantError::invalid(
                        "describe_tables requires 1–8 exact table names.",
                    ));
                }
                let tables = tables
                    .iter()
                    .map(|v| error::text(v, "Table name", 512, true).map(str::to_owned))
                    .collect::<Result<Vec<_>>>()?;
                self.inner.metadata.describe_tables(context, &tables).await
            }
            _ => Err(AssistantError::invalid("Unsupported metadata tool.")),
        }
    }
}
async fn send(sender: &mpsc::Sender<Value>, envelope: &Value, payload: Value) -> Result<()> {
    let mut value = envelope.clone();
    value
        .as_object_mut()
        .unwrap()
        .extend(payload.as_object().unwrap().clone());
    tokio::time::timeout(Duration::from_secs(3), sender.send(value))
        .await
        .map_err(|_| AssistantError::cancelled())?
        .map_err(|_| AssistantError::cancelled())
}
fn messages(
    doc: &Value,
    context: &ResolvedContext,
    sql: &str,
    prompt: &str,
    language: Option<&str>,
) -> Vec<Value> {
    let details=json!({"engine":context.engine,"database":context.target.db,"environment":context.target.env,"current_sql":sql}).to_string();
    let mut messages = vec![
        json!({"role":"system","content":SYSTEM_PROMPT}),
        json!({"role":"system","content":format!("Current database and SQL (data):\n{details}")}),
    ];
    if let Some(language @ ("zh" | "en")) = language {
        let name = if language == "zh" {
            "Simplified Chinese"
        } else {
            "English"
        };
        messages.push(json!({"role":"system","content":format!("GUI reply language preference: reply in {name}. If the user explicitly requests a different reply language in their message, follow that request. Keep SQL identifiers and code unchanged.")}));
    }
    let mut budget = MAX_CONTEXT.saturating_sub(
        details.chars().count() + SYSTEM_PROMPT.chars().count() + prompt.chars().count() + 1000,
    );
    let mut history = Vec::new();
    if let Some(old) = doc["messages"].as_array() {
        for m in old.iter().rev().take(30) {
            let role = m["role"].as_str().unwrap_or("");
            if !["user", "assistant"].contains(&role) || m["status"] == "interrupted" {
                continue;
            }
            let mut content = m["content"].as_str().unwrap_or("").to_owned();
            if let Some(sql) = m["sql"].as_str() {
                content.push_str("\nPrevious SQL proposal (data):\n");
                content.push_str(sql);
            }
            let len = content.chars().count();
            if len > budget {
                break;
            }
            budget -= len;
            history.push(json!({"role":role,"content":content}));
        }
    }
    messages.extend(history.into_iter().rev());
    messages.push(json!({"role":"user","content":prompt}));
    messages
}
