use crate::service::{Failure, Host, Result};
use sha2::{Digest, Sha256};
use std::{
    fs::OpenOptions,
    io::{ErrorKind, Read},
    net::{Ipv4Addr, Ipv6Addr, SocketAddr},
    os::unix::fs::{OpenOptionsExt, PermissionsExt},
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
        let mut file = OpenOptions::new()
            .read(true)
            .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK)
            .open(path)
            .ok()?;
        let metadata = file.metadata().ok()?;
        if !metadata.is_file() || metadata.len() != 16 || metadata.permissions().mode() & 0o077 != 0
        {
            return None;
        }
        let mut key = [0; 16];
        file.read_exact(&mut key).ok()?;
        Some(Self(Uuid::from_bytes(key)))
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

async fn probe(
    client: &reqwest::Client,
    host: &Host,
    address: SocketAddr,
    instance: Instance,
    timeout: Duration,
) -> Option<bool> {
    let challenge = Uuid::new_v4();
    let proof = instance.proof(host, challenge)?;
    let response = client
        .get(format!("http://{address}/api/version"))
        .header(CHALLENGE_HEADER, challenge.to_string())
        .timeout(timeout)
        .send()
        .await
        .ok()?;
    Some(
        response.status() == reqwest::StatusCode::OK
            && response
                .headers()
                .get(PROOF_HEADER)
                .is_some_and(|value| value == proof.as_str()),
    )
}

async fn matches_instance(
    client: &reqwest::Client,
    host: &Host,
    address: SocketAddr,
    budget: Duration,
) -> bool {
    let key = Instance::path(host, address);
    let deadline = Instant::now() + budget;
    loop {
        if let Some(instance) = Instance::read(&key)
            && let Some(matches) = probe(
                client,
                host,
                address,
                instance,
                deadline.saturating_duration_since(Instant::now()),
            )
            .await
        {
            if matches {
                return true;
            }
            // The owner may have replaced a stale key while bringing up its listener.
            if Instance::read(&key).is_none_or(|current| current.0 == instance.0) {
                return false;
            }
        }
        if Instant::now() >= deadline {
            return false;
        }
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
}

pub(crate) async fn standalone(host: &Host, bind: &str, port: u16) -> Result<Launch> {
    // Keep the platform's localhost address order, which selects the browser origin.
    let addresses: Vec<_> = tokio::net::lookup_host((bind, 0))
        .await?
        .map(|address| address.ip())
        .filter(|ip| *ip == Ipv4Addr::LOCALHOST || *ip == Ipv6Addr::LOCALHOST)
        .collect();
    if addresses.is_empty() {
        return Err(Failure::invalid(
            "Local GUI host did not resolve to a loopback address",
        ));
    }
    let end = if port == 0 {
        0
    } else {
        port.saturating_add(NEARBY_PORTS)
    };
    let client = (port != 0)
        .then(|| {
            reqwest::Client::builder()
                .no_proxy()
                .redirect(reqwest::redirect::Policy::none())
                .build()
                .ok()
        })
        .flatten();
    // An existing workbench may still use a fallback port after the requested
    // port becomes free. Probe only addresses with a published instance key.
    if let Some(client) = &client {
        let candidates = (port..=end)
            .flat_map(|port| addresses.iter().map(move |ip| SocketAddr::new(*ip, port)));
        for address in candidates {
            if let Some(instance) = Instance::read(&Instance::path(host, address))
                && probe(client, host, address, instance, Duration::from_millis(250)).await
                    == Some(true)
            {
                return Ok(Launch::Reuse(address));
            }
        }
    }
    for candidate in port..=end {
        let mut occupied = false;
        let mut last_error = None;
        for &ip in &addresses {
            let address = SocketAddr::new(ip, candidate);
            match TcpListener::bind(address).await {
                Ok(listener) => {
                    let instance = Instance::publish(host, listener.local_addr()?);
                    return Ok(Launch::Start(listener, instance));
                }
                Err(error) if error.kind() == ErrorKind::AddrInUse && port != 0 => {
                    occupied = true;
                    let budget = Duration::from_millis(if candidate == port { 500 } else { 80 });
                    if let Some(client) = &client
                        && matches_instance(client, host, address, budget).await
                    {
                        return Ok(Launch::Reuse(address));
                    }
                }
                Err(error) => last_error = Some(error),
            }
        }
        if let Some(error) = last_error
            && !occupied
        {
            return Err(error.into());
        }
    }
    Err(Failure::invalid(format!(
        "No Quarry GUI port available on {bind} from {port} to {end}. Choose another with --port."
    )))
}
