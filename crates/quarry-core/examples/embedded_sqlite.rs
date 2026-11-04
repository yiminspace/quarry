//! `cargo run -p quarry-core --example embedded_sqlite`
//! Embeds Quarry directly; no qy executable, workspace global, Python or HTTP server.
use quarry_core::{ConnectionSpec, Engine, Executor, QueryRequest};
#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let directory = tempfile::tempdir()?;
    let path = directory.path().join("embedded.db");
    let fixture = rusqlite::Connection::open(&path)?;
    fixture.execute_batch("CREATE TABLE sample(id INTEGER PRIMARY KEY,value TEXT NOT NULL); INSERT INTO sample VALUES(1,'embedded Rust core');")?;
    drop(fixture);
    let executor = Executor::new(2, 4)?;
    let connection = ConnectionSpec::new(
        Engine::Sqlite,
        path.to_str().ok_or("Non-UTF-8 fixture path")?,
    );
    let result = executor
        .execute(
            &connection,
            QueryRequest::new("SELECT id,value FROM sample"),
        )
        .await?;
    assert_eq!(result.row_count(), 1);
    println!(
        "{}",
        serde_json::to_string_pretty(&result.to_legacy_json())?
    );
    Ok(())
}
