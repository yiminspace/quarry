use quarry_core::*;
use serde_json::json;
fn fixture() -> (tempfile::TempDir, ConnectionSpec) {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("database with spaces.db");
    let db = rusqlite::Connection::open(&path).unwrap();
    db.execute_batch("PRAGMA foreign_keys=ON; CREATE TABLE parent(id INTEGER PRIMARY KEY, code TEXT UNIQUE); CREATE TABLE child(id INTEGER PRIMARY KEY,parent_id INTEGER REFERENCES parent(id), payload BLOB, value TEXT); INSERT INTO parent VALUES(1,'one'); INSERT INTO child VALUES(1,1,x'00ff','old');").unwrap();
    let spec = ConnectionSpec::new(Engine::Sqlite, path.to_str().unwrap());
    (dir, spec)
}
#[tokio::test]
async fn exact_values_duplicate_headers_and_empty_results() {
    let (_dir, c) = fixture();
    let r = execute(
        &c,
        QueryRequest::new(
            "SELECT 9007199254740993 AS n, x'00ff' AS b, 1 AS id, 2 AS id, NULL AS missing",
        ),
    )
    .await
    .unwrap();
    let rows = r.rows_json();
    assert_eq!(rows[0]["n"], json!("9007199254740993"));
    assert_eq!(rows[0]["b"], json!({"$binary":"AP8="}));
    assert_eq!(rows[0]["id"], json!(1));
    assert_eq!(rows[0]["id_2"], json!(2));
    let legacy = r.to_legacy_json();
    assert_eq!(legacy["columns"][3]["name"], "id_2");
    assert_eq!(
        legacy["rows"][0][legacy["columns"][3]["name"].as_str().unwrap()],
        2
    );
    assert_eq!(
        r.columns[3].name, "id",
        "Native column positions retain original labels"
    );
    let r = execute(&c, QueryRequest::new("SELECT id,value FROM child WHERE 0"))
        .await
        .unwrap();
    assert_eq!(r.columns.len(), 2);
    assert!(r.rows.is_empty());
}
#[tokio::test]
async fn read_only_backstop_and_authorized_atomic_writes() {
    let (_dir, c) = fixture();
    for query in [
        "UPDATE child SET value='bad'",
        "PRAGMA writable_schema=1",
        "ATTACH DATABASE '/tmp/invalid.db' AS extra",
        "SELECT load_extension('bad')",
        "WITH x AS (DELETE FROM child) SELECT * FROM x",
    ] {
        assert!(
            execute(&c, QueryRequest::new(query)).await.is_err(),
            "{query}"
        );
    }
    let mut r = QueryRequest::new("UPDATE child SET parent_id=999 WHERE id=1");
    r.policy.allow_write = true;
    assert!(execute(&c, r).await.is_err());
    assert_eq!(
        execute(&c, QueryRequest::new("SELECT parent_id FROM child"))
            .await
            .unwrap()
            .rows_json()[0]["parent_id"],
        json!(1)
    );
    let mut r = QueryRequest::new("UPDATE child SET value='new' WHERE id=1 RETURNING value");
    r.policy.allow_write = true;
    assert_eq!(
        execute(&c, r).await.unwrap().rows_json()[0]["value"],
        json!("new")
    );
}
#[tokio::test]
async fn paging_and_schema_and_record_link() {
    let (_dir, c) = fixture();
    let mut r = QueryRequest::new("VALUES(1),(2),(3),(4)");
    r.policy.max_rows = Some(2);
    r.policy.offset = 1;
    let out = execute(&c, r).await.unwrap();
    assert_eq!(out.rows.len(), 2);
    assert_eq!(out.rows[0][0].to_json(), json!(2));
    assert!(out.truncated);
    let meta = columns(&c, "child").await.unwrap();
    assert_eq!(meta["primary_key"], json!(["id"]));
    assert_eq!(meta["foreign_keys"][0]["referenced_table"], json!("parent"));
    let link = record_query(&c, "SELECT * FROM parent", &json!({"id":1,"code":"one"}))
        .await
        .unwrap();
    assert_eq!(link["sql"], "SELECT * FROM \"parent\" WHERE \"id\" = 1;");
    let link = record_query(&c, "SELECT * FROM parent", &json!({"code":"one"}))
        .await
        .unwrap();
    assert!(link["sql"].as_str().unwrap().contains("\"code\" = 'one'"));
    let link = record_query(
        &c,
        "SELECT * FROM parent JOIN child ON parent.id=child.id",
        &json!({"id":1}),
    )
    .await
    .unwrap();
    assert_eq!(link["reason"], json!("unsupported_query"));
}
#[tokio::test]
async fn deadlines_cancellation_and_budgets() {
    let (_dir, c) = fixture();
    let mut r=QueryRequest::new("WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<100000000) SELECT sum(x) FROM n");
    r.policy.timeout_ms = Some(10);
    assert_eq!(execute(&c, r).await.unwrap_err().code, ErrorCode::Timeout);
    let mut r = QueryRequest::new("SELECT printf('%10000s','x')");
    r.policy.max_bytes = 100;
    assert_eq!(execute(&c, r).await.unwrap_err().code, ErrorCode::Budget);
    let r = QueryRequest::new("SELECT 1");
    r.cancellation.cancel();
    assert_eq!(execute(&c, r).await.unwrap_err().code, ErrorCode::Cancelled);
    assert_eq!(
        execute(&c, QueryRequest::new("SELECT 1"))
            .await
            .unwrap()
            .row_count(),
        1
    );
}
#[tokio::test]
async fn parameters_preserve_literal_bytes_and_missing_files_are_not_created() {
    let (_dir, c) = fixture();
    let mut r = QueryRequest::new("SELECT :'v' AS value");
    r.parameters
        .insert("v".into(), "O'Reilly\\x :v 中文".into());
    assert_eq!(
        execute(&c, r).await.unwrap().rows_json()[0]["value"],
        json!("O'Reilly\\x :v 中文")
    );
    let dir = tempfile::tempdir().unwrap();
    let p = dir.path().join("missing.db");
    let c = ConnectionSpec::new(Engine::Sqlite, p.to_str().unwrap());
    assert!(execute(&c, QueryRequest::new("SELECT 1")).await.is_err());
    assert!(!p.exists());
}
#[tokio::test]
async fn production_confirmation_is_not_an_authorization_bypass() {
    let (_dir, mut c) = fixture();
    c.production = true;
    let mut r = QueryRequest::new("DELETE FROM child");
    r.policy.allow_write = true;
    assert_eq!(execute(&c, r).await.unwrap_err().code, ErrorCode::Safety);
}

#[tokio::test]
async fn parameter_values_are_private_at_persistence_boundaries() {
    let (_dir, c) = fixture();
    let mut request = QueryRequest::new("SELECT 42 AS value WHERE length(:'token') > 0");
    request
        .parameters
        .insert("token".into(), "private-fixture-credential".into());
    let outcome = execute(&c, request).await.unwrap();
    assert_eq!(outcome.rows_json()[0]["value"], 42);
    assert!(outcome
        .executed_query
        .contains("private-fixture-credential"));
    let legacy = outcome.to_legacy_json();
    assert!(legacy["sql"].as_str().unwrap().contains(":'token'"));
    assert!(!legacy.to_string().contains("private-fixture-credential"));
    assert!(!serde_json::to_string(&outcome)
        .unwrap()
        .contains("private-fixture-credential"));
}
#[tokio::test]
async fn sqlite_metadata_accepts_literal_dots_and_record_links_name_the_table() {
    let (_dir, c) = fixture();
    let mut request =
        QueryRequest::new("CREATE TABLE \"daily.events\"(id INTEGER PRIMARY KEY,note TEXT)");
    request.policy.allow_write = true;
    execute(&c, request).await.unwrap();
    let meta = columns(&c, "daily.events").await.unwrap();
    assert_eq!(meta["columns"], json!(["id", "note"]));
    let link = record_query(
        &c,
        "SELECT * FROM \"daily.events\" LIMIT 250",
        &json!({"id":1,"note":"value"}),
    )
    .await
    .unwrap();
    assert_eq!(link["table"], "daily.events");
    assert_eq!(
        link["sql"],
        "SELECT * FROM \"daily.events\" WHERE \"id\" = 1;"
    );
}
