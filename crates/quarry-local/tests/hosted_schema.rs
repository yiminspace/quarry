//! Explicit fixture-only rehearsal for separate hosted export and desktop import.
use quarry_local::LocalManager;
use quarry_runtime::{ConnectionRecord, RuntimeConfig, TunnelManager};
use serde_json::json;
use std::{path::PathBuf, process::Command};

struct Fixture {
    admin: url::Url,
    psql: PathBuf,
    databases: Vec<String>,
}
impl Fixture {
    fn sql(&self, database: &str, sql: &str) -> String {
        let mut target = self.admin.clone();
        target.set_path(&format!("/{database}"));
        let password = target.password().unwrap_or("").to_owned();
        target.set_password(None).unwrap();
        let output = Command::new(&self.psql)
            .args([
                "--no-psqlrc",
                "--set",
                "ON_ERROR_STOP=1",
                "--tuples-only",
                "--no-align",
                "--dbname",
                target.as_str(),
                "--command",
                sql,
            ])
            .env("PGPASSWORD", password)
            .output()
            .unwrap();
        assert!(
            output.status.success(),
            "fixture SQL failed: {}",
            String::from_utf8_lossy(&output.stderr)
        );
        String::from_utf8(output.stdout).unwrap().trim().into()
    }
    fn record(&self, db: &str, env: &str, workspace: &std::path::Path) -> ConnectionRecord {
        let mut url = self.admin.clone();
        url.set_path(&format!("/{db}"));
        serde_json::from_value(json!({"key":db,"url":url.as_str(),"engine":"postgres","env":env,"workspace":workspace})).unwrap()
    }
}
impl Drop for Fixture {
    fn drop(&mut self) {
        for db in &self.databases {
            let mut url = self.admin.clone();
            url.set_path("/postgres");
            let password = url.password().unwrap_or("").to_owned();
            url.set_password(None).unwrap();
            let _ = Command::new(&self.psql)
                .args([
                    "--no-psqlrc",
                    "--dbname",
                    url.as_str(),
                    "--command",
                    &format!("DROP DATABASE IF EXISTS \"{db}\" WITH (FORCE)"),
                ])
                .env("PGPASSWORD", password)
                .output();
        }
    }
}
#[tokio::test]
async fn hosted_schema_roundtrip_preserves_rows_source_previous_and_failed_activation() {
    let Some(url) = std::env::var_os("QUARRY_TEST_SCHEMA_URL") else {
        eprintln!("SKIPPED: QUARRY_TEST_SCHEMA_URL must select disposable PostgreSQL fixture");
        return;
    };
    let admin = url::Url::parse(&url.to_string_lossy()).unwrap();
    assert!(
        matches!(admin.host_str(), Some("127.0.0.1" | "localhost")),
        "test fixture must be loopback"
    );
    let psql = std::env::var_os("QUARRY_TEST_PSQL")
        .map(PathBuf::from)
        .unwrap_or_else(|| "psql".into());
    let pg_dump = std::env::var_os("QUARRY_TEST_PG_DUMP")
        .map(PathBuf::from)
        .unwrap_or_else(|| "pg_dump".into());
    let suffix = format!(
        "{}_{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_millis()
    );
    let source = format!("quarry_hs_{suffix}");
    let target = format!("quarry_ht_{suffix}");
    let fixture = Fixture {
        admin,
        psql: psql.clone(),
        databases: vec![
            source.clone(),
            target.clone(),
            format!("{target}__prev"),
            format!("{target}__staging"),
        ],
    };
    fixture.sql("postgres", &format!("CREATE DATABASE \"{source}\""));
    fixture.sql("postgres", &format!("CREATE DATABASE \"{target}\""));
    fixture.sql(&source,"CREATE SCHEMA audit; CREATE TYPE audit.kind AS ENUM ('a','b'); CREATE TABLE audit.events(id integer PRIMARY KEY,kind audit.kind); INSERT INTO audit.events VALUES(1,'a'); CREATE VIEW audit.event_ids AS SELECT id FROM audit.events; CREATE INDEX events_kind ON audit.events(kind);");
    fixture.sql(
        &target,
        "CREATE TABLE old_live(id integer); INSERT INTO old_live VALUES(99)",
    );
    let dir = tempfile::tempdir().unwrap();
    let manager = LocalManager::new(
        RuntimeConfig::load(dir.path().join("config.toml")).unwrap(),
        dir.path().join("state"),
    )
    .with_tools(Some(psql), Some(pg_dump), None);
    let source_record = fixture.record(&source, "prod", dir.path());
    let target_record = fixture.record(&target, "local", dir.path());
    let schema = manager
        .export_schema(
            &source_record,
            &TunnelManager::new(dir.path().join("tunnels")),
        )
        .await
        .unwrap();
    assert!(!schema.contains("fixture-hosted-only"));
    assert!(!schema.contains("INSERT INTO audit.events"));
    let imported = manager
        .import_schema(&target_record, &schema)
        .await
        .unwrap();
    assert_eq!(imported["schema_only"], true);
    assert_eq!(
        fixture.sql(&source, "SELECT count(*) FROM audit.events"),
        "1"
    );
    assert_eq!(
        fixture.sql(&target, "SELECT count(*) FROM audit.events"),
        "0"
    );
    assert_eq!(fixture.sql(&target,"SELECT to_regclass('audit.event_ids') IS NOT NULL AND to_regtype('audit.kind') IS NOT NULL AND to_regclass('audit.events_kind') IS NOT NULL"),"t");
    assert_eq!(
        fixture.sql(&format!("{target}__prev"), "SELECT id FROM old_live"),
        "99"
    );
    assert!(
        manager
            .import_schema(&target_record, "CREATE TABLE broken(")
            .await
            .is_err()
    );
    assert_eq!(
        fixture.sql(&target, "SELECT count(*) FROM audit.events"),
        "0"
    );
    assert_eq!(
        fixture.sql(&format!("{target}__prev"), "SELECT id FROM old_live"),
        "99"
    );
}
