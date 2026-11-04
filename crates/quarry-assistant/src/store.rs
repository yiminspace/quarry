//! Durable conversation storage. The JSON document layout also imports 1.x history files.
use crate::{AssistantError, Result, Target};
use rusqlite::{Connection, OptionalExtension, TransactionBehavior};
use serde_json::{Value, json};
use std::{
    fs,
    path::{Path, PathBuf},
    time::{Duration, SystemTime, UNIX_EPOCH},
};
use uuid::Uuid;

fn storage_error(_: impl std::fmt::Display) -> AssistantError {
    AssistantError::unavailable("Cannot access SQL assistant history.")
}
pub(crate) fn now_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis() as u64
}
fn valid_sql(sql: &str) -> Result<()> {
    if sql.chars().count() > 200_000 || sql.contains('\0') {
        Err(AssistantError::invalid(
            "SQL must be text, up to 200,000 characters",
        ))
    } else {
        Ok(())
    }
}
fn current_revision(doc: &Value) -> Result<u64> {
    doc["revision"]
        .as_u64()
        .ok_or_else(|| AssistantError::invalid("Invalid conversation revision"))
}
fn cas(doc: &Value, revision: u64) -> Result<()> {
    if current_revision(doc)? != revision {
        Err(AssistantError::conflict(
            "This conversation changed. Reopen it from History before applying SQL.",
        ))
    } else {
        Ok(())
    }
}
fn public(mut doc: Value) -> Value {
    if let Some(object) = doc.as_object_mut() {
        object.retain(|key, _| !key.starts_with('_'));
    }
    doc
}

#[derive(Clone, Debug)]
pub struct ConversationStore {
    path: PathBuf,
    // Keep a named in-memory SQLite database alive across per-operation connections.
    memory_keeper: Option<std::sync::Arc<std::sync::Mutex<Connection>>>,
}
impl ConversationStore {
    /// The caller supplies the exact location. No user home or environment is discovered.
    pub fn new(path: impl Into<PathBuf>) -> Self {
        Self {
            path: path.into(),
            memory_keeper: None,
        }
    }
    /// Request-scoped storage which never creates a directory or file. The last
    /// clone dropping destroys its database, including messages and SQL proposals.
    pub fn memory() -> Result<Self> {
        let uri = format!(
            "file:quarry-assistant-{}?mode=memory&cache=shared",
            Uuid::new_v4()
        );
        let connection = Connection::open_with_flags(
            &uri,
            rusqlite::OpenFlags::SQLITE_OPEN_READ_WRITE
                | rusqlite::OpenFlags::SQLITE_OPEN_CREATE
                | rusqlite::OpenFlags::SQLITE_OPEN_URI,
        )
        .map_err(storage_error)?;
        let store = Self {
            path: uri.into(),
            memory_keeper: Some(std::sync::Arc::new(std::sync::Mutex::new(connection))),
        };
        store.open()?;
        Ok(store)
    }
    /// Seed a single ephemeral request from the owner's local document. Client
    /// private fields never survive; the host supplies the authorized fingerprint.
    pub fn import_document(&self, document: Value, fingerprint: &str) -> Result<Value> {
        if self.memory_keeper.is_none() {
            return Err(AssistantError::invalid(
                "Document import requires ephemeral storage",
            ));
        }
        let object = document
            .as_object()
            .ok_or_else(|| AssistantError::invalid("Invalid conversation"))?;
        if serde_json::to_vec(&document).map_err(storage_error)?.len() > 2_097_152 {
            return Err(AssistantError::invalid("Conversation is too large"));
        }
        for key in ["id", "db"] {
            let value = object
                .get(key)
                .and_then(Value::as_str)
                .ok_or_else(|| AssistantError::invalid("Invalid conversation identity"))?;
            if value.is_empty()
                || value.len() > if key == "id" { 100 } else { 512 }
                || value.chars().any(char::is_control)
            {
                return Err(AssistantError::invalid("Invalid conversation identity"));
            }
        }
        valid_sql(
            document["sql"]
                .as_str()
                .ok_or_else(|| AssistantError::invalid("Invalid conversation SQL"))?,
        )?;
        current_revision(&document)?;
        for key in ["env", "workspace"] {
            if let Some(value) = document.get(key)
                && !value.is_null()
                && value
                    .as_str()
                    .is_none_or(|s| s.len() > 4096 || s.chars().any(char::is_control))
            {
                return Err(AssistantError::invalid("Invalid conversation target"));
            }
        }
        for key in ["messages", "checkpoints"] {
            let items = document[key]
                .as_array()
                .ok_or_else(|| AssistantError::invalid("Invalid conversation history"))?;
            if items.len() > 2000 || items.iter().any(|v| !v.is_object()) {
                return Err(AssistantError::invalid("Invalid conversation history"));
            }
        }
        let keys = [
            "id",
            "title",
            "db",
            "env",
            "workspace",
            "sql",
            "revision",
            "messages",
            "checkpoints",
            "createdAt",
            "updatedAt",
        ];
        let mut doc = Value::Object(
            object
                .iter()
                .filter(|(key, _)| keys.contains(&key.as_str()))
                .map(|(k, v)| (k.clone(), v.clone()))
                .collect(),
        );
        doc["_fingerprint"] = json!(fingerprint);
        let mut c = self.open()?;
        let tx = c
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(storage_error)?;
        Self::save(&tx, &mut doc)?;
        tx.commit().map_err(storage_error)?;
        Ok(public(doc))
    }
    pub fn path(&self) -> &Path {
        &self.path
    }
    fn open(&self) -> Result<Connection> {
        if self.memory_keeper.is_some() {
            let c = Connection::open_with_flags(
                &self.path,
                rusqlite::OpenFlags::SQLITE_OPEN_READ_WRITE
                    | rusqlite::OpenFlags::SQLITE_OPEN_CREATE
                    | rusqlite::OpenFlags::SQLITE_OPEN_URI,
            )
            .map_err(storage_error)?;
            c.busy_timeout(Duration::from_secs(10))
                .map_err(storage_error)?;
            c.execute_batch("CREATE TABLE IF NOT EXISTS conversations (id TEXT PRIMARY KEY, document TEXT NOT NULL)").map_err(storage_error)?;
            return Ok(c);
        }
        let parent = self.path.parent().ok_or_else(|| {
            AssistantError::invalid("History requires an explicit parent directory")
        })?;
        let created = !parent.exists();
        fs::create_dir_all(parent).map_err(storage_error)?;
        #[cfg(unix)]
        {
            use std::os::unix::fs::{OpenOptionsExt, PermissionsExt};
            if created {
                fs::set_permissions(parent, fs::Permissions::from_mode(0o700))
                    .map_err(storage_error)?;
            }
            fs::OpenOptions::new()
                .create(true)
                .truncate(false)
                .read(true)
                .write(true)
                .mode(0o600)
                .open(&self.path)
                .map_err(storage_error)?;
            fs::set_permissions(&self.path, fs::Permissions::from_mode(0o600))
                .map_err(storage_error)?;
        }
        #[cfg(not(unix))]
        let _ = created;
        let c = Connection::open(&self.path).map_err(storage_error)?;
        c.busy_timeout(Duration::from_secs(10))
            .map_err(storage_error)?;
        c.execute_batch("CREATE TABLE IF NOT EXISTS conversations (id TEXT PRIMARY KEY, document TEXT NOT NULL)").map_err(storage_error)?;
        Ok(c)
    }
    fn load(c: &Connection, id: &str) -> Result<Value> {
        let raw: Option<String> = c
            .query_row(
                "SELECT document FROM conversations WHERE id=?1",
                [id],
                |row| row.get(0),
            )
            .optional()
            .map_err(storage_error)?;
        serde_json::from_str(
            &raw.ok_or_else(|| AssistantError::not_found("Conversation not found"))?,
        )
        .map_err(storage_error)
    }
    fn save(c: &Connection, doc: &mut Value) -> Result<()> {
        doc["updatedAt"] = json!(now_ms());
        c.execute(
            "INSERT OR REPLACE INTO conversations VALUES (?1,?2)",
            rusqlite::params![
                doc["id"]
                    .as_str()
                    .ok_or_else(|| AssistantError::invalid("Invalid conversation id"))?,
                doc.to_string()
            ],
        )
        .map_err(storage_error)?;
        Ok(())
    }
    fn mutate(&self, id: &str, f: impl FnOnce(&mut Value) -> Result<bool>) -> Result<Value> {
        let mut c = self.open()?;
        let tx = c
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(storage_error)?;
        let mut doc = Self::load(&tx, id)?;
        if f(&mut doc)? {
            Self::save(&tx, &mut doc)?;
        }
        tx.commit().map_err(storage_error)?;
        Ok(public(doc))
    }
    pub fn create(
        &self,
        target: &Target,
        sql: &str,
        fingerprint: &str,
        title: Option<&str>,
    ) -> Result<Value> {
        valid_sql(sql)?;
        let mut doc = json!({"id":Uuid::new_v4().to_string(),"title":title.unwrap_or("SQL conversation").chars().take(120).collect::<String>(),"db":target.db,"env":target.env,"workspace":target.workspace,"sql":sql,"revision":0,"messages":[],"checkpoints":[],"_fingerprint":fingerprint});
        let mut c = self.open()?;
        let tx = c
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(storage_error)?;
        Self::save(&tx, &mut doc)?;
        tx.commit().map_err(storage_error)?;
        Ok(public(doc))
    }
    pub fn get(&self, id: &str) -> Result<Value> {
        Ok(public(Self::load(&self.open()?, id)?))
    }
    pub fn fingerprint(&self, id: &str) -> Result<String> {
        Self::load(&self.open()?, id)?["_fingerprint"]
            .as_str()
            .map(str::to_owned)
            .ok_or_else(|| AssistantError::invalid("Conversation fingerprint is unavailable"))
    }
    pub fn list(&self) -> Result<Vec<Value>> {
        let c = self.open()?;
        let mut stmt = c
            .prepare("SELECT document FROM conversations")
            .map_err(storage_error)?;
        let rows = stmt
            .query_map([], |row| row.get::<_, String>(0))
            .map_err(storage_error)?;
        let mut docs = Vec::new();
        for row in rows {
            let mut doc =
                public(serde_json::from_str(&row.map_err(storage_error)?).map_err(storage_error)?);
            if let Some(o) = doc.as_object_mut() {
                o.remove("messages");
                o.remove("checkpoints");
            }
            docs.push(doc);
        }
        docs.sort_by_key(|d| std::cmp::Reverse(d["updatedAt"].as_u64().unwrap_or_default()));
        Ok(docs)
    }
    pub fn update(&self, id: &str, revision: u64, sql: &str) -> Result<Value> {
        valid_sql(sql)?;
        self.mutate(id, |doc| {
            cas(doc, revision)?;
            if doc["sql"] != sql {
                doc["sql"] = json!(sql);
                doc["revision"] = json!(revision + 1);
            }
            Ok(true)
        })
    }
    pub fn append_message(
        &self,
        id: &str,
        role: &str,
        content: &str,
        extra: Value,
    ) -> Result<Value> {
        if !["user", "assistant", "system"].contains(&role) || content.chars().count() > 200_000 {
            return Err(AssistantError::invalid("Invalid conversation message"));
        }
        let message_id = extra["id"]
            .as_str()
            .map(str::to_owned)
            .unwrap_or_else(|| Uuid::new_v4().to_string());
        let mut message = json!({"id":message_id,"role":role,"content":content});
        for key in ["sql", "status", "checkpointId", "requestId", "baseRevision"] {
            if let Some(v) = extra.get(key) {
                if key == "sql" {
                    valid_sql(
                        v.as_str()
                            .ok_or_else(|| AssistantError::invalid("Invalid SQL proposal"))?,
                    )?;
                }
                message[key] = v.clone();
            }
        }
        self.mutate(id, |doc| {
            let messages = doc["messages"]
                .as_array()
                .ok_or_else(|| AssistantError::invalid("Invalid conversation messages"))?;
            if messages.iter().any(|m| m["id"] == message_id) {
                return Err(AssistantError::conflict(
                    "This reply has already been saved",
                ));
            }
            if role == "user" && !messages.iter().any(|m| m["role"] == "user") {
                doc["title"] = json!(
                    extra["title"]
                        .as_str()
                        .unwrap_or(content)
                        .split_whitespace()
                        .collect::<Vec<_>>()
                        .join(" ")
                        .chars()
                        .take(100)
                        .collect::<String>()
                );
            }
            doc["messages"].as_array_mut().unwrap().push(message);
            Ok(true)
        })
    }
    pub fn apply(
        &self,
        id: &str,
        revision: u64,
        sql: &str,
        message_id: &str,
        explicit: bool,
    ) -> Result<Value> {
        valid_sql(sql)?;
        self.mutate(id,|doc|{
            let messages=doc["messages"].as_array().ok_or_else(||AssistantError::invalid("Invalid conversation messages"))?;
            let index=messages.iter().position(|m|m["id"]==message_id).ok_or_else(||AssistantError::invalid("SQL does not match a completed assistant proposal"))?;
            let message=messages[index].clone();
            if message["role"]!="assistant"||message["sql"]!=sql{return Err(AssistantError::invalid("SQL does not match a completed assistant proposal"));}
            if message.get("status").and_then(Value::as_str).unwrap_or("complete")!="complete"{return Err(AssistantError::invalid("An incomplete reply cannot change SQL"));}
            let checkpoint=doc["checkpoints"].as_array().ok_or_else(||AssistantError::invalid("Invalid checkpoints"))?.iter().any(|cp|cp["id"]==message["checkpointId"] && cp["messageId"]==message_id && cp["afterSql"]==sql);
            if checkpoint && doc["sql"]==sql {if revision>current_revision(doc)?{cas(doc,revision)?;}return Ok(false);}
            cas(doc,revision)?;
            if !explicit && message.get("baseRevision").and_then(Value::as_u64).unwrap_or(revision)!=revision{return Err(AssistantError::conflict("SQL changed while the assistant was replying. Review the suggestion before applying it."));}
            if !explicit && message.get("checkpointId").is_some(){return Err(AssistantError::conflict("This SQL change has already been applied"));}
            if doc["sql"]!=sql {let checkpoint_id=Uuid::new_v4().to_string();let before=doc["sql"].clone();doc["checkpoints"].as_array_mut().unwrap().push(json!({"id":checkpoint_id,"messageId":message_id,"beforeSql":before,"afterSql":sql,"createdAt":now_ms()}));doc["sql"]=json!(sql);doc["revision"]=json!(revision+1);doc["messages"][index]["checkpointId"]=json!(checkpoint_id);}
            Ok(true)
        })
    }
    pub fn restore(&self, id: &str, revision: u64, checkpoint_id: &str) -> Result<Value> {
        self.mutate(id,|doc|{
        let cps=doc["checkpoints"].as_array().ok_or_else(||AssistantError::invalid("Invalid checkpoints"))?;
        let index=cps.iter().position(|cp|cp["id"]==checkpoint_id).ok_or_else(||AssistantError::not_found("Checkpoint not found"))?;
        let cp=cps[index].clone();
        if !doc["messages"].as_array().unwrap().iter().any(|m|m["id"]==cp["messageId"] && m["role"]=="assistant"){return Err(AssistantError::invalid("Only assistant SQL changes can be restored"));}
        if doc["sql"]==cp["beforeSql"] {if revision>current_revision(doc)?{cas(doc,revision)?;}return Ok(false);}
        cas(doc,revision)?;
        let restoration=json!({"id":Uuid::new_v4().to_string(),"beforeSql":doc["sql"],"afterSql":cp["beforeSql"],"createdAt":now_ms()});
        if !doc["checkpoints"][index]["restorations"].is_array(){doc["checkpoints"][index]["restorations"]=json!([]);}
        doc["checkpoints"][index]["restorations"].as_array_mut().unwrap().push(restoration.clone());doc["sql"]=cp["beforeSql"].clone();doc["revision"]=json!(revision+1);
        doc["messages"].as_array_mut().unwrap().push(json!({"id":restoration["id"],"role":"system","content":"Previous SQL restored.","restoredCheckpointId":checkpoint_id}));Ok(true)
    })
    }
}
