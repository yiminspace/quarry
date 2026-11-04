//! Explicit machine/runtime services shared by qy, ylab and bcli.
//! Construct a catalog with caller-selected paths; this library never selects a
//! global workspace or creates a Tokio runtime.
mod keeper;
mod queries;
mod transport;
mod workspace;
pub use keeper::*;
pub use queries::*;
pub use transport::*;
pub use workspace::*;

#[derive(Debug, thiserror::Error)]
pub enum RuntimeError {
    #[error("{0}")]
    Invalid(String),
    #[error("ambiguous resource: {0}")]
    Ambiguous(String),
    #[error("resource not found: {0}")]
    NotFound(String),
    #[error("{0}")]
    Operation(String),
    #[error(transparent)]
    Core(#[from] quarry_core::Error),
    #[error(transparent)]
    Io(#[from] std::io::Error),
    #[error(transparent)]
    Json(#[from] serde_json::Error),
}
pub type Result<T> = std::result::Result<T, RuntimeError>;

/// A host-owned daemon entry point. Libraries never assume that the embedding
/// binary implements qy's private commands. The runtime appends documented
/// state/config arguments after the caller-selected prefix.
#[derive(Debug, Clone)]
pub struct DaemonLauncher {
    pub program: std::path::PathBuf,
    pub prefix_args: Vec<std::ffi::OsString>,
}
impl DaemonLauncher {
    pub fn new(program: impl Into<std::path::PathBuf>) -> Self {
        Self {
            program: program.into(),
            prefix_args: Vec::new(),
        }
    }
    pub fn arg(mut self, argument: impl Into<std::ffi::OsString>) -> Self {
        self.prefix_args.push(argument.into());
        self
    }
}

pub(crate) fn lock_file(path: &std::path::Path) -> Result<std::fs::File> {
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    let file = std::fs::OpenOptions::new()
        .create(true)
        .truncate(false)
        .read(true)
        .write(true)
        .open(path)?;
    fs2::FileExt::lock_exclusive(&file)?;
    Ok(file)
}
/// Replace one file atomically, preserving private permissions and refusing symlinks.
pub fn atomic_write(path: &std::path::Path, bytes: &[u8]) -> Result<()> {
    use std::io::Write;
    use std::sync::atomic::{AtomicU64, Ordering};
    static NEXT: AtomicU64 = AtomicU64::new(0);
    if path
        .symlink_metadata()
        .is_ok_and(|m| m.file_type().is_symlink())
    {
        return Err(RuntimeError::Invalid("refusing to replace symlink".into()));
    }
    let parent = path
        .parent()
        .ok_or_else(|| RuntimeError::Invalid("file has no parent".into()))?;
    std::fs::create_dir_all(parent)?;
    let stage = parent.join(format!(
        ".quarry-{}-{}.tmp",
        std::process::id(),
        NEXT.fetch_add(1, Ordering::Relaxed)
    ));
    let result = (|| -> Result<()> {
        let mut options = std::fs::OpenOptions::new();
        options.create_new(true).write(true);
        #[cfg(unix)]
        {
            use std::os::unix::fs::OpenOptionsExt;
            options.mode(0o600);
        }
        let mut file = options.open(&stage)?;
        file.write_all(bytes)?;
        file.sync_all()?;
        std::fs::rename(&stage, path)?;
        std::fs::File::open(parent)?.sync_all()?;
        Ok(())
    })();
    if result.is_err() {
        let _ = std::fs::remove_file(&stage);
    }
    result
}
