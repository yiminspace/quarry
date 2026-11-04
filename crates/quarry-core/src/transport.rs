//! Per-execution transport overrides preserve the database's TLS identity.
//!
//! SQLx 0.8 exposes a Unix socket address independently from its TLS `host`.
//! A private Unix-to-TCP bridge therefore connects to an SSH forwarding endpoint
//! without substituting localhost for the certificate's original hostname.
use crate::{Error, ErrorCode, NetworkEndpoint, Result};
use std::{
    path::{Path, PathBuf},
    sync::Arc,
};

#[derive(Clone)]
pub(crate) struct TunnelBridge(Arc<BridgeLease>);
struct BridgeLease {
    _directory: tempfile::TempDir,
    socket: PathBuf,
    task: tokio::task::JoinHandle<()>,
}
impl Drop for BridgeLease {
    fn drop(&mut self) {
        self.task.abort();
    }
}
impl TunnelBridge {
    #[cfg(unix)]
    pub(crate) async fn new(endpoint: &NetworkEndpoint, socket_name: &str) -> Result<Self> {
        if endpoint.host.is_empty() || endpoint.port == 0 {
            return Err(Error::invalid(
                "Forwarding endpoint must have a host and positive port",
            ));
        }
        // A short private path also fits macOS's sockaddr_un path length. TempDir
        // creates the directory with mode 0700; the listener never binds a TCP port.
        let directory = tempfile::Builder::new()
            .prefix("qy-tunnel-")
            .tempdir_in("/tmp")
            .map_err(|_| {
                Error::new(
                    ErrorCode::Connection,
                    "Cannot create private forwarding directory",
                )
            })?;
        let socket = directory.path().join(socket_name);
        let listener = tokio::net::UnixListener::bind(&socket).map_err(|_| {
            Error::new(
                ErrorCode::Connection,
                "Cannot bind private forwarding socket",
            )
        })?;
        let endpoint = endpoint.clone();
        let task = tokio::spawn(async move {
            let mut connections = tokio::task::JoinSet::new();
            loop {
                tokio::select! {
                    result = listener.accept() => {
                        let Ok((mut incoming, _)) = result else { break };
                        let endpoint = endpoint.clone();
                        connections.spawn(async move {
                            if let Ok(mut target) = tokio::net::TcpStream::connect((endpoint.host.as_str(), endpoint.port)).await {
                                let _ = tokio::io::copy_bidirectional(&mut incoming, &mut target).await;
                            }
                        });
                    }
                    _ = connections.join_next(), if !connections.is_empty() => {}
                }
            }
        });
        Ok(Self(Arc::new(BridgeLease {
            _directory: directory,
            socket,
            task,
        })))
    }
    #[cfg(not(unix))]
    pub(crate) async fn new(_: &NetworkEndpoint, _: &str) -> Result<Self> {
        Err(Error::new(
            ErrorCode::Unsupported,
            "Independent forwarded SQL transport requires Unix sockets on this platform",
        ))
    }
    pub(crate) fn directory(&self) -> &Path {
        self.0._directory.path()
    }
    pub(crate) fn socket(&self) -> &Path {
        &self.0.socket
    }
}
