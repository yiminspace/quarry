//! A host app calls the same facade directly, without a qy subprocess or Python.
use quarry_core::QueryRequest;
use quarry_service::QuarryService;

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let dir = tempfile::tempdir()?;
    let db = rusqlite::Connection::open(dir.path().join("demo.db"))?;
    db.execute_batch("CREATE TABLE items(id INTEGER PRIMARY KEY, name TEXT);INSERT INTO items VALUES(1,'embedded')")?;
    drop(db);
    std::fs::write(
        dir.path().join("connections.toml"),
        "[demo]\nurl='sqlite:///demo.db'\n",
    )?;
    let service = QuarryService::new(
        dir.path().join("config.toml"),
        vec![dir.path().to_owned()],
        dir.path().join("state"),
    );
    let target = service.resolve("demo", None, None)?;
    let result = service
        .execute(&target, QueryRequest::new("SELECT * FROM items"))
        .await?;
    assert_eq!(result.rows_json()[0]["name"], "embedded");
    println!("{}", result.to_legacy_json());
    service.tunnels.close_all().await?;
    Ok(())
}
