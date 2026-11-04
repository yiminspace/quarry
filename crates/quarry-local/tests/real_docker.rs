//! Explicit opt-in test. Never uses a user's quarry-local-* resources.
use quarry_local::LocalManager;
use quarry_runtime::RuntimeConfig;
use quarry_runtime::{ConnectionRecord, TunnelManager, WorkspaceCatalog};
use serde_json::Value;
use std::{
    process::Command,
    time::{SystemTime, UNIX_EPOCH},
};
struct Cleanup {
    container: String,
    volume: String,
}
struct SourceCleanup {
    container: String,
    database: String,
}
impl Drop for SourceCleanup {
    fn drop(&mut self) {
        let _ = docker(&[
            "exec",
            &self.container,
            "dropdb",
            "--if-exists",
            "--force",
            "-U",
            "postgres",
            &self.database,
        ]);
    }
}
impl Drop for Cleanup {
    fn drop(&mut self) {
        let _ = Command::new("docker")
            .args(["rm", "-f", &self.container])
            .output();
        let _ = Command::new("docker")
            .args(["volume", "rm", &self.volume])
            .output();
    }
}
fn docker(args: &[&str]) -> std::process::Output {
    Command::new("docker")
        .args(args)
        .output()
        .expect("docker executable required")
}
#[tokio::test]
async fn independent_postgres_lifecycle_preserves_data_and_explicitly_purges() {
    if std::env::var("QUARRY_TEST_DOCKER").as_deref() != Ok("1") {
        eprintln!("SKIPPED: set QUARRY_TEST_DOCKER=1 for real disposable Docker lifecycle");
        return;
    }
    let dir = tempfile::tempdir().unwrap();
    let namespace = format!(
        "quarry-rust-test-{}-{}",
        std::process::id(),
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    );
    let config = RuntimeConfig::load(dir.path().join("config.toml")).unwrap();
    let manager = LocalManager::new(config, dir.path().join("state"))
        .with_namespace(namespace)
        .unwrap();
    let spec = manager.spec("postgres").unwrap();
    assert!(!spec.container.starts_with("quarry-local-"));
    let _cleanup = Cleanup {
        container: spec.container.clone(),
        volume: spec.volume.clone(),
    };
    let socket = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let port = socket.local_addr().unwrap().port();
    drop(socket);
    let first = manager.up("postgres", None, Some(port)).await.unwrap();
    assert_eq!(first["state"], "created");
    let again = manager.up("postgres", None, None).await.unwrap();
    assert_eq!(again["state"], "running");
    let inspect = docker(&["inspect", &spec.container]);
    assert!(inspect.status.success());
    let json: Value = serde_json::from_slice(&inspect.stdout).unwrap();
    assert_eq!(
        json[0]["HostConfig"]["PortBindings"]["5432/tcp"][0]["HostIp"],
        "127.0.0.1"
    );
    assert_eq!(
        json[0]["HostConfig"]["PortBindings"]["5432/tcp"][0]["HostPort"],
        port.to_string()
    );
    let write = docker(&[
        "exec",
        &spec.container,
        "psql",
        "-U",
        "quarry",
        "-d",
        "postgres",
        "-v",
        "ON_ERROR_STOP=1",
        "-c",
        "CREATE TABLE retained(id integer); INSERT INTO retained VALUES(41);",
    ]);
    assert!(write.status.success(), "isolated fixture write failed");
    manager.down("postgres", false).await.unwrap();
    assert!(
        docker(&["volume", "inspect", &spec.volume])
            .status
            .success()
    );
    manager.up("postgres", None, None).await.unwrap();
    let read = docker(&[
        "exec",
        &spec.container,
        "psql",
        "-U",
        "quarry",
        "-d",
        "postgres",
        "-tAc",
        "SELECT id FROM retained",
    ]);
    assert!(read.status.success());
    assert_eq!(String::from_utf8(read.stdout).unwrap().trim(), "41");
    manager.down("postgres", true).await.unwrap();
    assert!(!docker(&["inspect", &spec.container]).status.success());
    assert!(
        !docker(&["volume", "inspect", &spec.volume])
            .status
            .success()
    );
}

/// Uses actual PostgreSQL pg_dump/psql tools from the disposable containers.
/// The host's pre-existing psql may be too old for modern dump \restrict guards;
/// forwarding the tool invocation into its matching container keeps those
/// guards intact rather than deleting them to accommodate an old client.
#[tokio::test]
async fn schema_sync_uses_real_dump_staging_swap_and_preserves_source_and_previous_data() {
    use std::os::unix::fs::PermissionsExt;
    if std::env::var("QUARRY_TEST_DOCKER").as_deref() != Ok("1") {
        eprintln!("SKIPPED: QUARRY_TEST_DOCKER=1 and the disposable core PG fixture are required");
        return;
    }
    let source_container = std::env::var("QUARRY_TEST_SOURCE_CONTAINER")
        .unwrap_or_else(|_| "quarry-rust-core-pg".into());
    assert!(
        source_container.starts_with("quarry-rust-"),
        "only disposable Quarry Rust test fixtures are allowed"
    );
    let dir = tempfile::tempdir().unwrap();
    let unique = format!(
        "{}{}",
        std::process::id(),
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    );
    let source_db = format!("sync_src_{unique}");
    let created = docker(&[
        "exec",
        &source_container,
        "createdb",
        "-U",
        "postgres",
        &source_db,
    ]);
    assert!(
        created.status.success(),
        "disposable source fixture must be running"
    );
    let _source_cleanup = SourceCleanup {
        container: source_container.clone(),
        database: source_db.clone(),
    };
    let write = docker(&[
        "exec",
        &source_container,
        "psql",
        "-U",
        "postgres",
        "-d",
        &source_db,
        "-v",
        "ON_ERROR_STOP=1",
        "-c",
        "CREATE SCHEMA audit; CREATE TYPE audit.kind AS ENUM ('created','updated'); CREATE TABLE audit.events(id bigint GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY, label text NOT NULL, kind audit.kind DEFAULT 'created'); CREATE INDEX events_label_idx ON audit.events(label); CREATE VIEW audit.event_labels AS SELECT label FROM audit.events; INSERT INTO audit.events(label) VALUES('source-row-must-remain');",
    ]);
    assert!(write.status.success());
    let ws = dir.path().join("workspace");
    std::fs::create_dir_all(&ws).unwrap();
    let config = RuntimeConfig::load(dir.path().join("config.toml"))
        .unwrap()
        .set_workspaces(std::slice::from_ref(&ws))
        .unwrap();
    let catalog = WorkspaceCatalog::load(config.clone(), &[]).unwrap();
    let mut manager = LocalManager::new(config, dir.path().join("state"))
        .with_namespace(format!("quarry-rust-test-sync-{unique}"))
        .unwrap();
    let spec = manager.spec("postgres").unwrap();
    let _target_cleanup = Cleanup {
        container: spec.container.clone(),
        volume: spec.volume.clone(),
    };
    let socket = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let port = socket.local_addr().unwrap().port();
    drop(socket);
    manager.up("postgres", None, Some(port)).await.unwrap();
    let target = manager
        .register_new(&catalog, &ws, "schema_sync", "postgres")
        .await
        .unwrap();
    let target_db = url::Url::parse(&target.url)
        .unwrap()
        .path()
        .trim_start_matches('/')
        .to_string();
    let old = docker(&[
        "exec",
        &spec.container,
        "psql",
        "-U",
        "quarry",
        "-d",
        &target_db,
        "-v",
        "ON_ERROR_STOP=1",
        "-c",
        "CREATE TABLE old_live(id integer); INSERT INTO old_live VALUES(99);",
    ]);
    assert!(old.status.success());
    let source: ConnectionRecord = serde_json::from_value(serde_json::json!({"key":"sync_source","db":"schema_sync","env":"dev","engine":"postgres","url":format!("postgres://postgres:fixture-only@127.0.0.1:55322/{source_db}"),"workspace":ws})).unwrap();
    let dump = dir.path().join("pg_dump");
    std::fs::write(&dump,format!(r#"#!/bin/sh
if [ "$1" = --version ]; then exec docker exec '{}' pg_dump --version; fi
PGOPTIONS='-c default_transaction_read_only=on' exec docker exec -i -e PGPASSWORD -e PGOPTIONS '{}' pg_dump --schema-only --no-owner --no-privileges --dbname 'postgres://postgres@127.0.0.1:5432/{}'
"#,source_container,source_container,source_db)).unwrap();
    let psql = dir.path().join("psql");
    std::fs::write(&psql,format!(r#"#!/bin/sh
for arg in "$@"; do connection=$arg; done
database=${{connection##*/}}
case "$connection" in
 *:55322/*) container='{}'; user=postgres; options='-c default_transaction_read_only=on';;
 *:{}/*) container='{}'; user=quarry; options='';;
 *) exit 72;;
esac
PGOPTIONS="$options" exec docker exec -i -e PGPASSWORD -e PGOPTIONS "$container" psql --no-psqlrc --set ON_ERROR_STOP=1 --tuples-only --no-align --quiet --dbname "postgres://$user@127.0.0.1:5432/$database"
"#,source_container,port,spec.container)).unwrap();
    for tool in [&dump, &psql] {
        std::fs::set_permissions(tool, std::fs::Permissions::from_mode(0o700)).unwrap();
    }
    manager.pg_dump = dump;
    manager.psql = psql;
    let outcome = manager
        .sync(
            &source,
            &target,
            &TunnelManager::new(dir.path().join("tunnels")),
        )
        .await
        .unwrap();
    assert_eq!(outcome["schema_only"], true);
    let read = |container: &str, user: &str, db: &str, sql: &str| {
        let output = docker(&["exec", container, "psql", "-U", user, "-d", db, "-tAc", sql]);
        assert!(
            output.status.success(),
            "real schema verification SQL failed"
        );
        String::from_utf8(output.stdout).unwrap().trim().to_string()
    };
    assert_eq!(
        read(
            &spec.container,
            "quarry",
            &target_db,
            "SELECT count(*) FROM audit.events"
        ),
        "0"
    );
    assert_eq!(
        read(
            &spec.container,
            "quarry",
            &target_db,
            "SELECT to_regclass('audit.event_labels') IS NOT NULL AND to_regclass('audit.events_label_idx') IS NOT NULL AND to_regtype('audit.kind') IS NOT NULL"
        ),
        "t"
    );
    assert_eq!(
        read(
            &source_container,
            "postgres",
            &source_db,
            "SELECT label FROM audit.events"
        ),
        "source-row-must-remain"
    );
    assert_eq!(
        read(
            &spec.container,
            "quarry",
            &format!("{target_db}__prev"),
            "SELECT id FROM old_live"
        ),
        "99"
    );
    assert_eq!(
        read(
            &spec.container,
            "quarry",
            &target_db,
            "SELECT to_regclass('public.old_live') IS NULL"
        ),
        "t"
    );
    manager.down("postgres", true).await.unwrap();
}
