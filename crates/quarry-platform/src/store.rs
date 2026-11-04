//! Personal state lives only in the desktop companion's SQLite file.
use crate::{Result, failure};
use rusqlite::{Connection, OptionalExtension, TransactionBehavior, params};
use serde_json::{Value, json};
use std::path::PathBuf;

#[derive(Clone)]
pub struct PersonalStore {
    path: PathBuf,
}
impl PersonalStore {
    pub fn new(path: PathBuf) -> Self {
        Self { path }
    }
    fn open(&self) -> Result<Connection> {
        let parent = self
            .path
            .parent()
            .ok_or_else(|| failure(400, "invalid", "Explicit personal storage path required"))?;
        if self.path.is_symlink() || parent.is_symlink() {
            return Err(failure(
                400,
                "invalid",
                "Personal storage must not be a symbolic link",
            ));
        }
        let created = !parent.exists();
        std::fs::create_dir_all(parent)?;
        #[cfg(unix)]
        if created {
            use std::os::unix::fs::PermissionsExt;
            std::fs::set_permissions(parent, std::fs::Permissions::from_mode(0o700))?;
        }
        #[cfg(not(unix))]
        let _ = created;
        #[cfg(unix)]
        {
            use std::os::unix::fs::OpenOptionsExt;
            std::fs::OpenOptions::new()
                .create(true)
                .truncate(false)
                .read(true)
                .write(true)
                .mode(0o600)
                .open(&self.path)?;
            use std::os::unix::fs::PermissionsExt;
            std::fs::set_permissions(&self.path, std::fs::Permissions::from_mode(0o600))?;
        }
        let c = Connection::open(&self.path).map_err(store_error)?;
        c.busy_timeout(std::time::Duration::from_secs(5))
            .map_err(store_error)?;
        c.execute_batch("CREATE TABLE IF NOT EXISTS personal_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL); CREATE TABLE IF NOT EXISTS personal_documents (origin TEXT NOT NULL, owner TEXT NOT NULL, kind TEXT NOT NULL, document_key TEXT NOT NULL, revision INTEGER NOT NULL, value TEXT NOT NULL, PRIMARY KEY(origin,owner,kind,document_key))").map_err(store_error)?;
        Ok(c)
    }
    /// Stable identity of this personal store on this computer. It is not an
    /// IAM identity or credential; it prevents cross-device Local result aliases.
    pub fn device_id(&self) -> Result<String> {
        let mut connection = self.open()?;
        let tx = connection
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(store_error)?;
        let existing: Option<String> = tx
            .query_row(
                "SELECT value FROM personal_metadata WHERE key='device_id'",
                [],
                |r| r.get(0),
            )
            .optional()
            .map_err(store_error)?;
        let id = match existing {
            Some(id) if uuid::Uuid::parse_str(&id).is_ok() => id,
            Some(_) => {
                return Err(failure(
                    503,
                    "storage_unavailable",
                    "Personal device identity is invalid",
                ));
            }
            None => {
                let id = uuid::Uuid::new_v4().to_string();
                tx.execute(
                    "INSERT INTO personal_metadata(key,value) VALUES('device_id',?1)",
                    [&id],
                )
                .map_err(store_error)?;
                id
            }
        };
        tx.commit().map_err(store_error)?;
        Ok(id)
    }
    pub fn get(&self, origin: &str, owner: &str, kind: &str, key: &str) -> Result<Value> {
        validate(kind, key)?;
        let c = self.open()?;
        let row:Option<(u64,String)>=c.query_row("SELECT revision,value FROM personal_documents WHERE origin=?1 AND owner=?2 AND kind=?3 AND document_key=?4",params![origin,owner,kind,key],|r|Ok((r.get(0)?,r.get(1)?))).optional().map_err(store_error)?;
        match row {
            Some((revision, value)) => {
                Ok(json!({"revision":revision,"value":serde_json::from_str::<Value>(&value)?}))
            }
            None => Ok(json!({"revision":0,"value":null})),
        }
    }
    pub fn put(
        &self,
        origin: &str,
        owner: &str,
        kind: &str,
        key: &str,
        value: Value,
        revision: u64,
    ) -> Result<Value> {
        validate(kind, key)?;
        let raw = serde_json::to_string(&value)?;
        if raw.len() > 16 * 1024 * 1024 {
            return Err(failure(
                413,
                "too_large",
                "Personal document exceeds 16 MiB",
            ));
        }
        let mut c = self.open()?;
        let tx = c
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(store_error)?;
        let current:Option<u64>=tx.query_row("SELECT revision FROM personal_documents WHERE origin=?1 AND owner=?2 AND kind=?3 AND document_key=?4",params![origin,owner,kind,key],|r|r.get(0)).optional().map_err(store_error)?;
        if current.unwrap_or(0) != revision {
            return Err(failure(
                409,
                "conflict",
                "Personal document changed. Reload before saving.",
            ));
        }
        let next = revision
            .checked_add(1)
            .filter(|r| *r <= i64::MAX as u64)
            .ok_or_else(|| failure(400, "invalid", "Invalid revision"))?;
        tx.execute("INSERT INTO personal_documents VALUES (?1,?2,?3,?4,?5,?6) ON CONFLICT(origin,owner,kind,document_key) DO UPDATE SET revision=excluded.revision,value=excluded.value",params![origin,owner,kind,key,next,raw]).map_err(store_error)?;
        tx.commit().map_err(store_error)?;
        Ok(json!({"revision":next,"value":value}))
    }
}
fn validate(kind: &str, key: &str) -> Result<()> {
    if kind.is_empty()
        || kind.len() > 64
        || key.is_empty()
        || key.len() > 512
        || kind.chars().chain(key.chars()).any(char::is_control)
    {
        return Err(failure(400, "invalid", "Invalid personal document key"));
    }
    Ok(())
}
fn store_error(_: rusqlite::Error) -> quarry_service::Failure {
    failure(
        503,
        "storage_unavailable",
        "Cannot access personal desktop storage",
    )
}
