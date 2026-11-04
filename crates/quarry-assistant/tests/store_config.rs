use quarry_assistant::{ConfigManager, ConversationStore, Credential, Target};
use serde_json::json;
use std::{collections::HashMap, fs};
use tempfile::tempdir;
fn target() -> Target {
    Target {
        db: "shop".into(),
        env: Some("dev".into()),
        workspace: Some("/fixture/project".into()),
    }
}
#[test]
fn preserves_drafts_cas_idempotent_apply_and_original_restore_checkpoint() {
    let dir = tempdir().unwrap();
    let s = ConversationStore::new(dir.path().join("history.sqlite3"));
    let d = s.create(&target(), "select original", "fp", None).unwrap();
    let id = d["id"].as_str().unwrap();
    assert!(d.get("_fingerprint").is_none());
    s.append_message(
        id,
        "assistant",
        "proposal",
        json!({"id":"reply","sql":"select proposed","status":"complete","baseRevision":0}),
    )
    .unwrap();
    let edited = s.update(id, 0, "select manual").unwrap();
    assert_eq!(edited["revision"], 1);
    assert_eq!(s.update(id, 0, "select stale").unwrap_err().status, 409);
    assert_eq!(
        s.apply(id, 1, "select proposed", "reply", false)
            .unwrap_err()
            .status,
        409
    );
    let applied = s.apply(id, 1, "select proposed", "reply", true).unwrap();
    let cp = applied["checkpoints"][0]["id"].as_str().unwrap();
    assert_eq!(applied["checkpoints"][0]["beforeSql"], "select manual");
    let retried = s.apply(id, 1, "select proposed", "reply", true).unwrap();
    assert_eq!(retried, applied);
    let restored = s.restore(id, 2, cp).unwrap();
    assert_eq!(restored["sql"], "select manual");
    assert_eq!(restored["checkpoints"].as_array().unwrap().len(), 1);
    assert_eq!(restored["revision"], 3);
    assert_eq!(s.restore(id, 2, cp).unwrap(), restored);
    assert_eq!(
        restored["messages"].as_array().unwrap().last().unwrap()["role"],
        "system"
    );
    assert_eq!(s.fingerprint(id).unwrap(), "fp");
}
#[test]
fn reads_existing_python_history_and_does_not_allow_restore_of_restore() {
    let dir = tempdir().unwrap();
    let path = dir.path().join("history.sqlite3");
    let c = rusqlite::Connection::open(&path).unwrap();
    c.execute_batch("CREATE TABLE conversations(id TEXT PRIMARY KEY,document TEXT NOT NULL)")
        .unwrap();
    let doc = json!({"id":"legacy","db":"shop","env":"dev","workspace":"/fixture/project","sql":"select changed","revision":2,"messages":[{"id":"legacy-restore","role":"system","content":"restored"}],"checkpoints":[{"id":"bad-checkpoint","messageId":"legacy-restore","beforeSql":"select prior","afterSql":"select changed"}],"updatedAt":1,"_fingerprint":"legacy-fp","futureField":{"preserved":true}});
    c.execute(
        "INSERT INTO conversations VALUES (?1,?2)",
        rusqlite::params!["legacy", doc.to_string()],
    )
    .unwrap();
    drop(c);
    let store = ConversationStore::new(path);
    assert_eq!(
        store.get("legacy").unwrap()["futureField"],
        json!({"preserved":true})
    );
    assert!(store.restore("legacy", 2, "bad-checkpoint").is_err());
    assert_eq!(store.fingerprint("legacy").unwrap(), "legacy-fp");
    assert_eq!(
        store.update("legacy", 2, "select new").unwrap()["futureField"],
        json!({"preserved":true})
    );
}
#[test]
fn incomplete_reply_or_forged_sql_never_changes_document() {
    let dir = tempdir().unwrap();
    let store = ConversationStore::new(dir.path().join("history.sqlite3"));
    let doc = store.create(&target(), "select 1", "fp", None).unwrap();
    let id = doc["id"].as_str().unwrap();
    store
        .append_message(
            id,
            "assistant",
            "broken",
            json!({"id":"reply","sql":"delete from shop","status":"interrupted","baseRevision":0}),
        )
        .unwrap();
    assert!(
        store
            .apply(id, 0, "delete from shop", "reply", true)
            .is_err()
    );
    assert!(store.apply(id, 0, "select forged", "reply", true).is_err());
    assert_eq!(store.get(id).unwrap()["sql"], "select 1");
}
#[test]
fn explicit_configuration_preserves_other_tables_and_never_exposes_credentials() {
    let dir = tempdir().unwrap();
    let path = dir.path().join("config.toml");
    fs::write(&path, "# retained comment\n[unrelated]\nanswer = 42\n").unwrap();
    let manager = ConfigManager::new(&path, HashMap::new(), "zh_CN.UTF-8");
    assert_eq!(manager.status(), json!({"configured":false}));
    assert_eq!(
        manager.preferences().unwrap()["resolvedReplyLanguage"],
        "zh"
    );
    manager
        .configure(
            "https://model.example/v1/",
            "fixture",
            Credential::Stored("secret-only-in-file".into()),
            true,
            "disabled",
            30,
        )
        .unwrap();
    assert!(
        !manager
            .details()
            .unwrap()
            .to_string()
            .contains("secret-only-in-file")
    );
    assert!(!format!("{:?}", manager.load().unwrap()).contains("secret-only-in-file"));
    assert!(
        fs::read_to_string(&path)
            .unwrap()
            .contains("# retained comment")
    );
    manager.set_reply_language("en").unwrap();
    assert_eq!(
        manager.preferences().unwrap()["resolvedReplyLanguage"],
        "en"
    );
    assert_eq!(manager.load().unwrap().model, "fixture");
    let before = fs::read(&path).unwrap();
    assert!(
        manager
            .configure(
                "https://user:password@model.example/",
                "invalid",
                Credential::None,
                false,
                "default",
                30
            )
            .is_err()
    );
    assert_eq!(fs::read(&path).unwrap(), before);
    manager
        .configure(
            "https://model.example/v1",
            "next",
            Credential::None,
            false,
            "default",
            30,
        )
        .unwrap();
    assert!(!manager.credentials_path.exists());
    assert!(fs::read_to_string(&path).unwrap().contains("answer = 42"));
}
#[test]
fn environment_credentials_are_injected_explicitly_and_bound_to_settings() {
    let dir = tempdir().unwrap();
    let manager = ConfigManager::new(
        dir.path().join("config.toml"),
        HashMap::from([("TEST_AI_SECRET".into(), "injected-fixture-key".into())]),
        "C",
    );
    manager
        .configure(
            "http://127.0.0.1:1234/v1",
            "fixture",
            Credential::Environment("TEST_AI_SECRET".into()),
            false,
            "default",
            10,
        )
        .unwrap();
    assert_eq!(manager.load().unwrap().api_key, "injected-fixture-key");
    assert!(
        !fs::read_to_string(&manager.config_path)
            .unwrap()
            .contains("injected-fixture-key")
    );
    assert_eq!(
        manager.preferences().unwrap()["resolvedReplyLanguage"],
        "en"
    );
}

#[test]
fn trusted_command_configuration_is_inspected_without_execution_or_argv_disclosure() {
    let dir = tempdir().unwrap();
    let path = dir.path().join("config.toml");
    let marker = dir.path().join("must-not-exist");
    let argv = json!([
        "/bin/sh",
        "-c",
        format!("touch {}; printf private-command-token", marker.display())
    ]);
    fs::write(&path, format!("[assistant]\nbase_url = 'https://model.example/v1'\nmodel = 'fixture'\nauth = 'command'\ncredential_command = {argv}\n")).unwrap();
    let manager = ConfigManager::new(&path, HashMap::new(), "en");
    assert_eq!(
        manager.status(),
        json!({"configured": true, "model": "fixture"})
    );
    let details = manager.details().unwrap();
    assert_eq!(details["auth"], "command");
    assert!(details.get("credential_command").is_none());
    assert!(!details.to_string().contains("private-command-token"));
    assert!(!format!("{:?}", manager.load().unwrap()).contains("private-command-token"));
    assert!(!marker.exists());
    assert!(!manager.credentials_path.exists());
    // The existing local configure API cannot create/preserve an executable hook.
    manager
        .configure(
            "https://model.example/v1",
            "fixture",
            Credential::None,
            false,
            "default",
            30,
        )
        .unwrap();
    assert!(manager.load().unwrap().credential_command.is_none());
    assert!(!marker.exists());
}

#[test]
fn command_configuration_rejects_relative_or_unbounded_argv() {
    let dir = tempdir().unwrap();
    let path = dir.path().join("config.toml");
    let manager = ConfigManager::new(&path, HashMap::new(), "en");
    for argv in [
        json!([]),
        json!(["ylab", "auth"]),
        json!(["/bin/echo", 1]),
        json!(["/bin/echo", "line\nbreak"]),
        json!(["/bin/echo", "x".repeat(4097)]),
    ] {
        fs::write(&path, format!("[assistant]\nbase_url = 'https://model.example/v1'\nmodel = 'fixture'\nauth = 'command'\ncredential_command = {argv}\n")).unwrap();
        assert_eq!(manager.status(), json!({"configured": false}));
        assert!(manager.load().is_err());
    }
}

#[test]
fn concurrent_sqlite_cas_has_one_winner_and_rejected_mutation_is_atomic() {
    let dir = tempdir().unwrap();
    let store = ConversationStore::new(dir.path().join("history.sqlite3"));
    let doc = store
        .create(&target(), "select original", "fp", None)
        .unwrap();
    let id = doc["id"].as_str().unwrap().to_owned();
    let barrier = std::sync::Arc::new(std::sync::Barrier::new(3));
    let workers = ["select first", "select second"].map(|sql| {
        let store = store.clone();
        let id = id.clone();
        let barrier = barrier.clone();
        std::thread::spawn(move || {
            barrier.wait();
            store.update(&id, 0, sql)
        })
    });
    barrier.wait();
    let results = workers.map(|worker| worker.join().unwrap());
    assert_eq!(results.iter().filter(|result| result.is_ok()).count(), 1);
    assert_eq!(
        results
            .iter()
            .find_map(|result| result.as_ref().err())
            .unwrap()
            .status,
        409
    );
    let saved = store.get(&id).unwrap();
    assert_eq!(saved["revision"], 1);
    assert!(matches!(
        saved["sql"].as_str(),
        Some("select first" | "select second")
    ));
    assert!(store.restore(&id, 1, "nonexistent-checkpoint").is_err());
    assert_eq!(store.get(&id).unwrap(), saved);
    assert_eq!(store.fingerprint(&id).unwrap(), "fp");
}

#[test]
fn explicit_language_provider_is_live_while_default_configuration_is_a_snapshot() {
    use std::sync::{
        Arc, Mutex,
        atomic::{AtomicUsize, Ordering},
    };
    let dir = tempdir().unwrap();
    let path = dir.path().join("config.toml");
    let language = Arc::new(Mutex::new("zh_CN.UTF-8".to_owned()));
    let calls = Arc::new(AtomicUsize::new(0));
    let source = language.clone();
    let count = calls.clone();
    let live =
        ConfigManager::new(&path, HashMap::new(), "en").with_system_language_provider(move || {
            count.fetch_add(1, Ordering::SeqCst);
            source.lock().unwrap().clone()
        });
    let snapshot = ConfigManager::new(&path, HashMap::new(), "en");
    assert_eq!(live.preferences().unwrap()["resolvedReplyLanguage"], "zh");
    assert_eq!(
        snapshot.preferences().unwrap()["resolvedReplyLanguage"],
        "en"
    );
    *language.lock().unwrap() = "en_US.UTF-8".into();
    assert_eq!(
        live.clone().preferences().unwrap()["resolvedReplyLanguage"],
        "en"
    );
    // A separate CLI/config instance changes the same file; the running host
    // reads that change on the next preferences request without reconstruction.
    snapshot.set_reply_language("zh").unwrap();
    let before = calls.load(Ordering::SeqCst);
    assert_eq!(live.preferences().unwrap()["resolvedReplyLanguage"], "zh");
    assert_eq!(calls.load(Ordering::SeqCst), before);
    snapshot.set_reply_language("system").unwrap();
    assert_eq!(live.preferences().unwrap()["resolvedReplyLanguage"], "en");
    *language.lock().unwrap() = "zh-Hans".into();
    assert_eq!(live.preferences().unwrap()["resolvedReplyLanguage"], "zh");
    assert_eq!(
        snapshot.preferences().unwrap()["resolvedReplyLanguage"],
        "en"
    );
}

#[test]
fn ephemeral_documents_are_isolated_and_host_fingerprint_cannot_be_forged() {
    let first = ConversationStore::memory().unwrap();
    let doc = first
        .create(&target(), "select 1", "authorized-target", None)
        .unwrap();
    let id = doc["id"].as_str().unwrap();
    let clone = first.clone();
    drop(first);
    assert_eq!(clone.get(id).unwrap()["sql"], "select 1");
    let second = ConversationStore::memory().unwrap();
    assert!(second.get(id).is_err());
    let mut imported = doc.clone();
    imported["_fingerprint"] = json!("forged");
    imported["_credential"] = json!("never-store");
    let public = second.import_document(imported, "host-target").unwrap();
    assert!(public.get("_credential").is_none());
    assert_eq!(second.fingerprint(id).unwrap(), "host-target");
    let dir = tempdir().unwrap();
    let path = dir.path().join("must-not-exist/history.sqlite3");
    assert!(
        ConversationStore::new(&path)
            .import_document(doc, "fp")
            .is_err()
    );
    assert!(!path.parent().unwrap().exists());
}

#[test]
fn ephemeral_import_rejects_malformed_and_oversized_history_before_mutation() {
    let store = ConversationStore::memory().unwrap();
    let doc = store
        .create(&target(), "select original", "fp", None)
        .unwrap();
    let id = doc["id"].as_str().unwrap();
    for field in ["id", "db", "sql", "revision", "messages", "checkpoints"] {
        let mut bad = doc.clone();
        bad[field] = serde_json::Value::Null;
        assert!(store.import_document(bad, "forged").is_err(), "{field}");
    }
    let mut huge = doc.clone();
    huge["messages"] = json!([{"content":"x".repeat(2_097_152)}]);
    assert!(store.import_document(huge, "forged").is_err());
    assert_eq!(store.get(id).unwrap(), doc);
    assert_eq!(store.fingerprint(id).unwrap(), "fp");
}
