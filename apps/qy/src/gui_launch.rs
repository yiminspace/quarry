use crate::service::{Failure, Host, Result};
use sha2::{Digest, Sha256};
use std::{
    io::ErrorKind,
    net::{Ipv4Addr, Ipv6Addr, SocketAddr},
    os::unix::fs::PermissionsExt,
    path::PathBuf,
    time::Duration,
};
use tokio::{net::TcpListener, time::Instant};
use uuid::Uuid;

pub(crate) const CHALLENGE_HEADER: &str = "x-quarry-launch-challenge";
pub(crate) const PROOF_HEADER: &str = "x-quarry-launch-proof";
const NEARBY_PORTS: u16 = 30;

pub(crate) enum Launch {
    Start(TcpListener, Option<Instance>),
    Reuse(SocketAddr),
}

#[derive(Clone, Copy)]
pub(crate) struct Instance(Uuid);

impl Instance {
    fn path(host: &Host, address: SocketAddr) -> PathBuf {
        let family = if address.is_ipv4() { 4 } else { 6 };
        host.state_dir
            .join(format!("gui-{family}-{}.key", address.port()))
    }

    fn publish(host: &Host, address: SocketAddr) -> Option<Self> {
        let instance = Self(Uuid::new_v4());
        quarry_runtime::atomic_write(&Self::path(host, address), instance.0.as_bytes()).ok()?;
        Some(instance)
    }

    fn read(path: &std::path::Path) -> Option<Self> {
        let metadata = path.symlink_metadata().ok()?;
        if !metadata.is_file() || metadata.len() != 16 || metadata.permissions().mode() & 0o077 != 0
        {
            return None;
        }
        Some(Self(Uuid::from_slice(&std::fs::read(path).ok()?).ok()?))
    }

    pub(crate) fn proof(self, host: &Host, challenge: Uuid) -> Option<String> {
        // Resolve config-driven workspaces on each launch probe so settings
        // changes do not require the running GUI to republish its identity.
        let catalog = host.catalog().ok()?;
        let workspaces: Vec<_> = catalog
            .workspaces
            .iter()
            .map(|workspace| &workspace.home)
            .collect();
        let selection =
            serde_json::to_vec(&(&host.config_path, host.explicit.is_empty(), workspaces)).ok()?;
        let mut hash = Sha256::new();
        // Prove knowledge of the private local key without sending it over HTTP.
        hash.update(self.0.as_bytes());
        hash.update(challenge.as_bytes());
        hash.update(selection);
        Some(format!("{:x}", hash.finalize()))
    }
}

async fn matches_instance(host: &Host, address: SocketAddr, budget: Duration) -> bool {
    let Ok(client) = reqwest::Client::builder()
        .no_proxy()
        .redirect(reqwest::redirect::Policy::none())
        .build()
    else {
        return false;
    };
    let key = Instance::path(host, address);
    let deadline = Instant::now() + budget;
    let url = format!("http://{address}/api/version");
    loop {
        if let Some(instance) = Instance::read(&key) {
            let challenge = Uuid::new_v4();
            let Some(proof) = instance.proof(host, challenge) else {
                return false;
            };
            if let Ok(response) = client
                .get(&url)
                .header(CHALLENGE_HEADER, challenge.to_string())
                .timeout(deadline.saturating_duration_since(Instant::now()))
                .send()
                .await
            {
                if response.status() == reqwest::StatusCode::OK
                    && response
                        .headers()
                        .get(PROOF_HEADER)
                        .is_some_and(|value| value == proof.as_str())
                {
                    return true;
                }
                // The owner may have replaced a stale key while bringing up its listener.
                if Instance::read(&key).is_none_or(|current| current.0 == instance.0) {
                    return false;
                }
            }
        }
        if Instant::now() >= deadline {
            return false;
        }
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
}

pub(crate) async fn standalone(host: &Host, bind: &str, port: u16) -> Result<Launch> {
    let ip = if bind == "::1" {
        Ipv6Addr::LOCALHOST.into()
    } else {
        Ipv4Addr::LOCALHOST.into()
    };
    let end = if port == 0 {
        0
    } else {
        port.saturating_add(NEARBY_PORTS)
    };
    for candidate in port..=end {
        let address = SocketAddr::new(ip, candidate);
        match TcpListener::bind(address).await {
            Ok(listener) => {
                let instance = Instance::publish(host, listener.local_addr()?);
                return Ok(Launch::Start(listener, instance));
            }
            Err(error) if error.kind() == ErrorKind::AddrInUse && port != 0 => {
                let budget = Duration::from_millis(if candidate == port { 500 } else { 80 });
                if matches_instance(host, address, budget).await {
                    return Ok(Launch::Reuse(address));
                }
            }
            Err(error) => return Err(error.into()),
        }
    }
    Err(Failure::invalid(format!(
        "No Quarry GUI port available on {ip} from {port} to {end}. Choose another with --port."
    )))
}
