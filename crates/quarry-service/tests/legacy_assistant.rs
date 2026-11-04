//! Native migration test: the exact Python SQLite document table is opened by
//! the reusable host and assistant together, with no Python bridge at runtime.
#![cfg(unix)]
use quarry_assistant::{AssistantService, ConfigManager, ConversationStore};
use quarry_service::{QuarryService, display_path};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{collections::HashMap, path::Path, sync::Arc};

#[cfg(unix)]
fn python_sqlite_fingerprint(workspace: &Path, database: &Path) -> String {
    use std::os::unix::fs::MetadataExt;
    let metadata = database.metadata().unwrap();
    // This is Python json.dumps(identity)'s original ASCII fixture wire, not
    // the production helper. Python's default comma separator includes space.
    let identity = format!(
        "[{}, \"notes\", \"notes\", null, \"sqlite:///notes.db\", \"sqlite\", null, null, null, null, {}, {}, {}]",
        serde_json::to_string(&workspace.display().to_string()).unwrap(),
        serde_json::to_string(&database.display().to_string()).unwrap(),
        metadata.dev(),
        metadata.ino()
    );
    assert!(identity.is_ascii());
    format!("{:x}", Sha256::digest(identity.as_bytes()))
}
#[cfg(unix)]
#[tokio::test]
async fn python_history_uses_real_host_identity_and_preserves_apply_restore_state() {
    let directory = tempfile::tempdir().unwrap();
    let workspace = std::fs::canonicalize(directory.path()).unwrap();
    std::fs::write(
        workspace.join("connections.toml"),
        "[notes]\nurl='sqlite:///notes.db'\ngroup='Files'\n",
    )
    .unwrap();
    let database = workspace.join("notes.db");
    rusqlite::Connection::open(&database)
        .unwrap()
        .execute_batch("CREATE TABLE entries(id INTEGER PRIMARY KEY, note TEXT)")
        .unwrap();
    let fingerprint = python_sqlite_fingerprint(&workspace, &database);
    let history = workspace.join("assistant/history.sqlite3");
    std::fs::create_dir(history.parent().unwrap()).unwrap();
    let old = rusqlite::Connection::open(&history).unwrap();
    old.execute_batch("CREATE TABLE conversations (id TEXT PRIMARY KEY, document TEXT NOT NULL)")
        .unwrap();
    let document = json!({"id":"legacy-python","title":"Existing SQL history","db":"notes","env":null,"workspace":workspace,"sql":"SELECT 1","revision":0,"messages":[{"id":"reply-1","role":"assistant","content":"Read entries","sql":"SELECT id, note FROM entries","status":"complete","baseRevision":0}],"checkpoints":[],"updatedAt":1730000000000_u64,"_fingerprint":fingerprint,"futureField":{"keep":"历史字段"}});
    old.execute(
        "INSERT INTO conversations VALUES (?1, ?2)",
        rusqlite::params!["legacy-python", document.to_string()],
    )
    .unwrap();
    drop(old);
    let host = Arc::new(QuarryService::new(
        workspace.join("config.toml"),
        vec![workspace.clone()],
        workspace.join("state"),
    ));
    let assistant = AssistantService::new(
        ConversationStore::new(&history),
        ConfigManager::new(workspace.join("config.toml"), HashMap::new(), "en"),
        host,
    );
    let listed = assistant.list_conversations().await.unwrap();
    let summary = &listed["conversations"][0];
    assert_eq!(summary["id"], "legacy-python");
    assert_eq!(summary["env"], Value::Null);
    assert_eq!(summary["connectionAvailable"], true);
    assert_eq!(summary["guiWorkspace"], display_path(&workspace));
    assert!(summary.get("messages").is_none());
    assert!(summary.get("_fingerprint").is_none());
    let loaded = assistant.get_conversation("legacy-python").await.unwrap();
    assert_eq!(loaded["messages"], document["messages"]);
    assert_eq!(loaded["futureField"], document["futureField"]);
    assert_eq!(loaded["workspace"], json!(workspace));
    let applied=assistant.apply_conversation(json!({"id":"legacy-python","revision":0,"sql":"SELECT id, note FROM entries","messageId":"reply-1"})).await.unwrap();
    assert_eq!(applied["revision"], 1);
    assert_eq!(applied["sql"], "SELECT id, note FROM entries");
    assert_eq!(applied["checkpoints"][0]["beforeSql"], "SELECT 1");
    assert_eq!(applied["env"], Value::Null);
    assert_eq!(applied["connectionAvailable"], true);
    let retry=assistant.apply_conversation(json!({"id":"legacy-python","revision":0,"sql":"SELECT id, note FROM entries","messageId":"reply-1"})).await.unwrap();
    assert_eq!(retry, applied);
    let checkpoint = applied["checkpoints"][0]["id"].as_str().unwrap();
    let restored = assistant
        .restore_conversation(json!({"id":"legacy-python","revision":1,"checkpointId":checkpoint}))
        .await
        .unwrap();
    assert_eq!(restored["sql"], "SELECT 1");
    assert_eq!(restored["revision"], 2);
    assert_eq!(restored["checkpoints"].as_array().unwrap().len(), 1);
    assert_eq!(
        restored["checkpoints"][0]["restorations"][0]["beforeSql"],
        "SELECT id, note FROM entries"
    );
    assert_eq!(restored["futureField"], document["futureField"]);
    let retry = assistant
        .restore_conversation(json!({"id":"legacy-python","revision":1,"checkpointId":checkpoint}))
        .await
        .unwrap();
    assert_eq!(retry, restored);
    // Reopen the same on-disk table after all Rust mutations; private identity
    // and legacy unknown fields remain intact instead of being re-imported.
    let stored: Value = serde_json::from_str(
        &rusqlite::Connection::open(&history)
            .unwrap()
            .query_row(
                "SELECT document FROM conversations WHERE id='legacy-python'",
                [],
                |row| row.get::<_, String>(0),
            )
            .unwrap(),
    )
    .unwrap();
    assert_eq!(stored["_fingerprint"], fingerprint);
    assert_eq!(stored["env"], Value::Null);
    assert_eq!(stored["futureField"], document["futureField"]);
    // File replacement must disable further edits while history stays readable.
    let replacement = workspace.join("replacement.db");
    rusqlite::Connection::open(&replacement).unwrap();
    std::fs::rename(replacement, &database).unwrap();
    assert_eq!(
        assistant.get_conversation("legacy-python").await.unwrap()["connectionAvailable"],
        false
    );
    assert!(
        assistant
            .update_conversation(json!({"id":"legacy-python","revision":2,"sql":"SELECT 2"}))
            .await
            .is_err()
    );
}
