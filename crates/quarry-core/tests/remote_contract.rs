//! These tests require explicitly supplied disposable loopback databases.
use quarry_core::*;
use serde_json::json;
fn target(engine: Engine, name: &str) -> ConnectionSpec {
    let url = std::env::var(name).expect("Explicit isolated test URL required");
    let parsed = url::Url::parse(&url).unwrap();
    assert!(
        matches!(parsed.host_str(), Some("127.0.0.1" | "localhost")),
        "Only isolated loopback test databases accepted"
    );
    ConnectionSpec::new(engine, url)
}
async fn write(c: &ConnectionSpec, sql: &str) -> QueryOutcome {
    let mut r = QueryRequest::new(sql);
    r.policy.allow_write = true;
    execute(c, r).await.unwrap()
}
#[tokio::test]
#[ignore = "requires isolated QUARRY_TEST_POSTGRES_URL"]
async fn postgres_native_value_metadata_readonly_and_cancellation() {
    let c = target(Engine::Postgres, "QUARRY_TEST_POSTGRES_URL");
    let name = format!("quarry_{}", uuid::Uuid::new_v4().simple());
    write(&c,&format!("CREATE TABLE {name}(id BIGINT PRIMARY KEY, code TEXT UNIQUE, parent_id BIGINT REFERENCES {name}(id), amount NUMERIC(30,10), body JSONB)")).await;
    write(&c,&format!("INSERT INTO {name} VALUES(1,'one',1,12345678901234567890.1234567890,'{{\"n\":9007199254740993,\"d\":0.1234567890123456789}}')")).await;
    let r=execute(&c,QueryRequest::new(format!("SELECT id,amount,body,9007199254740993::bigint AS big,ARRAY[1,2,NULL] AS a,ARRAY['x,y',NULL,'NULL'] AS b,decode('00ff','hex') AS binary,1 AS id FROM {name}"))).await.unwrap();
    let row = &r.rows_json()[0];
    assert_eq!(
        r.affected_rows, 0,
        "SELECT count is not a database change count"
    );
    assert_eq!(row["amount"], json!("12345678901234567890.1234567890"));
    assert_eq!(row["big"], json!("9007199254740993"));
    assert_eq!(row["body"]["n"], json!("9007199254740993"));
    assert_eq!(row["body"]["d"], json!("0.1234567890123456789"));
    assert_eq!(row["a"], json!([1, 2, null]));
    assert_eq!(row["b"], json!(["x,y", null, "NULL"]));
    assert_eq!(row["id_2"], json!(1));
    assert_eq!(row["binary"], json!({"$binary":"AP8="}));
    let out = execute(
        &c,
        QueryRequest::new(format!("SELECT id,amount FROM {name} WHERE false")),
    )
    .await
    .unwrap();
    assert!(out.rows.is_empty());
    assert_eq!(out.columns.len(), 2);
    let meta = columns(&c, &name).await.unwrap();
    assert_eq!(meta["primary_key"], json!(["id"]));
    assert_eq!(meta["foreign_keys"][0]["referenced_table"], json!(name));
    let link = record_query(&c, &format!("SELECT * FROM {name}"), &json!({"code":"one"}))
        .await
        .unwrap();
    assert_eq!(
        link["sql"],
        format!("SELECT * FROM \"{name}\" WHERE \"code\" = 'one';")
    );
    let link = record_query(
        &c,
        &format!("SELECT * FROM {name}"),
        &json!({"code":"O'Reilly\\team"}),
    )
    .await
    .unwrap();
    assert!(link["sql"]
        .as_str()
        .unwrap()
        .contains("E'O''Reilly\\\\team'"));
    write(&c,&format!("CREATE FUNCTION {name}_mutate() RETURNS integer LANGUAGE plpgsql AS $$BEGIN INSERT INTO {name}(id,code) VALUES(2,'two'); RETURN 1; END$$")).await;
    assert_eq!(
        execute(&c, QueryRequest::new(format!("SELECT {name}_mutate()")))
            .await
            .unwrap_err()
            .code,
        ErrorCode::Safety
    );
    let mut r = QueryRequest::new("SELECT pg_sleep(2)");
    r.policy.timeout_ms = Some(30);
    let e = execute(&c, r).await.unwrap_err();
    assert_eq!(e.code, ErrorCode::Timeout);
    let r = QueryRequest::new("SELECT pg_sleep(10)");
    let cancel = r.cancellation.clone();
    let copied = c.clone();
    let handle = tokio::spawn(async move { execute(&copied, r).await });
    tokio::time::sleep(std::time::Duration::from_millis(100)).await;
    cancel.cancel();
    assert_eq!(
        handle.await.unwrap().unwrap_err().code,
        ErrorCode::Cancelled
    );
    let mut stopped = false;
    for _ in 0..30 {
        let active=execute(&c,QueryRequest::new("SELECT count(*) AS count FROM pg_stat_activity WHERE state='active' AND query LIKE 'SELECT pg_sleep(10)%' AND pid<>pg_backend_pid()")).await.unwrap();
        if active.rows_json()[0]["count"] == json!(0) {
            stopped = true;
            break;
        }
        tokio::time::sleep(std::time::Duration::from_millis(50)).await;
    }
    assert!(
        stopped,
        "Cancelled PostgreSQL backend query must actually stop"
    );
    let mut r = QueryRequest::new("SELECT :'value' AS value");
    r.parameters
        .insert("value".into(), "back\\slash 'quoted' 中文".into());
    assert_eq!(
        execute(&c, r).await.unwrap().rows_json()[0]["value"],
        json!("back\\slash 'quoted' 中文")
    );
    write(&c, &format!("DROP FUNCTION {name}_mutate()")).await;
    write(&c, &format!("DROP TABLE {name}")).await;
}
#[tokio::test]
#[ignore = "requires isolated QUARRY_TEST_MYSQL_URL"]
async fn mysql_native_value_metadata_and_readonly() {
    let c = target(Engine::MySql, "QUARRY_TEST_MYSQL_URL");
    let name = format!("quarry_{}", uuid::Uuid::new_v4().simple());
    write(&c,&format!("CREATE TABLE {name}(id BIGINT PRIMARY KEY, code VARCHAR(30) UNIQUE,parent_id BIGINT,amount DECIMAL(30,10),stamp DATETIME(6),FOREIGN KEY(parent_id) REFERENCES {name}(id))")).await;
    write(&c,&format!("INSERT INTO {name} VALUES(1,'one',1,12345678901234567890.1234567890,'2026-01-02 03:04:05.123456')")).await;
    let out=execute(&c,QueryRequest::new(format!("SELECT id,amount,stamp,CAST(18446744073709551615 AS UNSIGNED) AS huge,UNHEX('00ff') AS binary,1 AS id FROM {name}"))).await.unwrap();
    let row = &out.rows_json()[0];
    assert_eq!(row["amount"], json!("12345678901234567890.1234567890"));
    assert_eq!(row["huge"], json!("18446744073709551615"));
    assert!(row["stamp"].as_str().unwrap().ends_with("123456"));
    assert_eq!(row["id_2"], json!(1));
    assert_eq!(row["binary"], json!({"$binary":"AP8="}));
    let out = execute(
        &c,
        QueryRequest::new(format!("SELECT * FROM {name} WHERE 0")),
    )
    .await
    .unwrap();
    assert!(out.rows.is_empty());
    assert_eq!(out.columns.len(), 5);
    let meta = columns(&c, &name).await.unwrap();
    assert_eq!(meta["primary_key"], json!(["id"]));
    assert_eq!(meta["foreign_keys"][0]["referenced_table"], json!(name));
    assert!(
        execute(&c, QueryRequest::new(format!("DELETE FROM {name}")))
            .await
            .is_err()
    );
    write(&c, &format!("DROP TABLE {name}")).await;
}
#[tokio::test]
#[ignore = "requires isolated QUARRY_TEST_REDIS_URL"]
async fn redis_native_values_scanning_and_readonly() {
    let c = target(Engine::Redis, "QUARRY_TEST_REDIS_URL");
    let key = format!("quarry:{}", uuid::Uuid::new_v4());
    let command = format!("SET '{key}' 'line one\nline two'");
    write(&c, &command).await;
    let out = execute(&c, QueryRequest::new(format!("GET '{key}'")))
        .await
        .unwrap();
    assert_eq!(out.rows_json()[0]["value"], json!("line one\nline two"));
    let out = execute(&c, QueryRequest::new(format!("GET '{key}:missing'")))
        .await
        .unwrap();
    assert_eq!(out.rows_json()[0]["value"], json!(null));
    let out = execute(
        &c,
        QueryRequest::new(format!("--scan --pattern '{key}*' --count 1")),
    )
    .await
    .unwrap();
    assert!(out.rows_json().iter().any(|r| r["value"] == json!(key)));
    assert!(execute(&c, QueryRequest::new(format!("GETDEL '{key}'")))
        .await
        .is_err());
    write(&c, &format!("DEL '{key}'")).await;
}

#[tokio::test]
#[ignore = "requires isolated QUARRY_TEST_POSTGRES_URL"]
async fn postgres_special_relation_metadata_and_canonical_types() {
    let c = target(Engine::Postgres, "QUARRY_TEST_POSTGRES_URL");
    let raw = format!("quarry-weird-{}", uuid::Uuid::new_v4().simple());
    let relation = format!("\"{raw}\"");
    write(&c,&format!("CREATE TABLE {relation}(id integer PRIMARY KEY,value varchar(17),amount numeric(20,7))")).await;
    write(
        &c,
        &format!("COMMENT ON COLUMN {relation}.value IS 'fixture column comment'"),
    )
    .await;
    let metadata = columns(&c, &raw).await.unwrap();
    assert_eq!(metadata["columns"], json!(["id", "value", "amount"]));
    assert_eq!(
        metadata["rows"][1]["formatted_type"],
        json!("character varying(17)")
    );
    assert_eq!(
        metadata["rows"][1]["comment"],
        json!("fixture column comment")
    );
    let result = execute(
        &c,
        QueryRequest::new("SELECT 1::integer AS n,'text'::text AS t,ARRAY[1]::integer[] AS a"),
    )
    .await
    .unwrap();
    assert_eq!(result.columns[0].type_name.as_deref(), Some("integer"));
    assert_eq!(result.columns[1].type_name.as_deref(), Some("text"));
    assert_eq!(result.columns[2].type_name.as_deref(), Some("integer[]"));
    let mut escaped = c.clone();
    let mut url = url::Url::parse(&escaped.url).unwrap();
    url.query_pairs_mut()
        .append_pair("options", "-c bytea_output=escape");
    escaped.url = url.to_string();
    let value = execute(
        &escaped,
        QueryRequest::new("SELECT decode('41626300ff5c','hex') AS data"),
    )
    .await
    .unwrap();
    assert_eq!(value.rows_json()[0]["data"], json!({"$binary":"QWJjAP9c"}));
    write(&c, &format!("DROP TABLE {relation}")).await;
}

#[tokio::test]
#[ignore = "requires isolated QUARRY_TEST_REDIS_URL"]
async fn redis_type_aware_inspection_preserves_keys_and_binary_catalog_entries() {
    let c = target(Engine::Redis, "QUARRY_TEST_REDIS_URL");
    let client = redis::Client::open(c.url.as_str()).unwrap();
    let mut fixture = client.get_multiplexed_async_connection().await.unwrap();
    let prefix = format!("quarry-inspect:{}", uuid::Uuid::new_v4());
    let keys =
        ["string", "hash", "list", "set", "zset"].map(|kind| format!("{prefix} {kind}\nkey"));
    let mut pipe = redis::pipe();
    pipe.cmd("SET").arg(&keys[0]).arg("value\nkept").ignore();
    pipe.cmd("HSET")
        .arg(&keys[1])
        .arg("field")
        .arg("value")
        .ignore();
    pipe.cmd("RPUSH")
        .arg(&keys[2])
        .arg("first")
        .arg("second")
        .ignore();
    pipe.cmd("SADD").arg(&keys[3]).arg("member").ignore();
    pipe.cmd("ZADD")
        .arg(&keys[4])
        .arg(1.5)
        .arg("member")
        .ignore();
    let mut binary_key = prefix.as_bytes().to_vec();
    binary_key.push(0xff);
    pipe.cmd("SET").arg(&binary_key).arg("binary key").ignore();
    pipe.query_async::<()>(&mut fixture).await.unwrap();
    for (key, kind) in keys.iter().zip(["string", "hash", "list", "set", "zset"]) {
        let inspected = inspect_key(&c, key).await.unwrap();
        assert_eq!(inspected["rows"][0]["type"], kind, "{inspected}");
    }
    assert_eq!(
        inspect_key(&c, &keys[0]).await.unwrap()["rows"][0]["value"],
        "value\nkept"
    );
    let catalog = tables(&c).await.unwrap();
    let encoded = Value::Binary(binary_key.clone()).to_json();
    let binary = catalog["keys"]
        .as_array()
        .unwrap()
        .iter()
        .find(|r| r["key"] == encoded)
        .expect("Binary keys remain visible in metadata");
    assert_eq!(binary["type"], "string");
    assert_eq!(binary["inspectable"], false);
    let mut clean = redis::cmd("DEL");
    for key in &keys {
        clean.arg(key);
    }
    clean
        .arg(binary_key)
        .query_async::<i64>(&mut fixture)
        .await
        .unwrap();
}
