use crate::service::{Failure, Host, Result};
use sha2::{Digest, Sha256};
use std::{
    fs::OpenOptions,
    io::{ErrorKind, Read},
    net::{IpAddr, Ipv4Addr, Ipv6Addr, SocketAddr},
    os::unix::fs::{OpenOptionsExt, PermissionsExt},
    path::PathBuf,
    time::Duration,
};
use tokio::{net::TcpListener, time::Instant};
use uuid::Uuid;

pub(crate) const CHALLENGE_HEADER: &str = "x-quarry-launch-challenge";
pub(crate) const PROOF_HEADER: &str = "x-quarry-launch-proof";
pub(crate) const STOP_HEADER: &str = "x-quarry-stop-proof";
pub(crate) const STOP_PATH: &str = "/api/gui/stop";
const NEARBY_PORTS: u16 = 30;

#[derive(Clone, Copy)]
pub(crate) enum ProofPurpose {
    Discover,
    Stop,
}

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

    pub(crate) fn proof(
        self,
        host: &Host,
        challenge: Uuid,
        purpose: ProofPurpose,
    ) -> Option<String> {
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
        // A discovery response must never authorize a shutdown.
        if matches!(purpose, ProofPurpose::Stop) {
            hash.update(b"quarry-gui-stop\0");
        }
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
    let proof = instance.proof(host, challenge, ProofPurpose::Discover)?;
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

async fn loopback_addresses(bind: &str) -> Result<Vec<IpAddr>> {
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
    Ok(addresses)
}

fn last_port(port: u16) -> u16 {
    if port == 0 {
        0
    } else {
        port.saturating_add(NEARBY_PORTS)
    }
}

fn local_client() -> reqwest::Result<reqwest::Client> {
    reqwest::Client::builder()
        .no_proxy()
        .redirect(reqwest::redirect::Policy::none())
        .build()
}

async fn find_instance(
    client: &reqwest::Client,
    host: &Host,
    addresses: &[IpAddr],
    port: u16,
) -> Option<(SocketAddr, Instance)> {
    // A workbench can still use a fallback port after the requested port is free.
    for candidate in port..=last_port(port) {
        for &ip in addresses {
            let address = SocketAddr::new(ip, candidate);
            if let Some(instance) = Instance::read(&Instance::path(host, address))
                && probe(client, host, address, instance, Duration::from_millis(250)).await
                    == Some(true)
            {
                return Some((address, instance));
            }
        }
    }
    None
}

pub(crate) async fn stop(host: &Host, bind: &str, port: u16) -> Result<Option<SocketAddr>> {
    if port == 0 {
        return Err(Failure::invalid(
            "Use the port printed by the running Quarry GUI",
        ));
    }
    let addresses = loopback_addresses(bind).await?;
    let client = local_client().map_err(|error| Failure::invalid(error.to_string()))?;
    let Some((address, instance)) = find_instance(&client, host, &addresses, port).await else {
        return Ok(None);
    };
    let challenge = Uuid::new_v4();
    let proof = instance
        .proof(host, challenge, ProofPurpose::Stop)
        .ok_or_else(|| Failure::invalid("Could not identify the selected Quarry workbench"))?;
    let response = client
        .post(format!("http://{address}{STOP_PATH}"))
        .header(CHALLENGE_HEADER, challenge.to_string())
        .header(STOP_HEADER, proof)
        .json(&serde_json::json!({}))
        .timeout(Duration::from_secs(2))
        .send()
        .await
        .map_err(|error| Failure::invalid(format!("Could not stop the Quarry GUI: {error}")))?;
    match response.status() {
        reqwest::StatusCode::NO_CONTENT => Ok(Some(address)),
        reqwest::StatusCode::NOT_FOUND => Err(Failure::invalid(
            "The running Quarry GUI does not support this command. Stop it from its original terminal.",
        )),
        status => Err(Failure::invalid(format!(
            "Could not stop the Quarry GUI: HTTP {status}"
        ))),
    }
}

pub(crate) async fn standalone(host: &Host, bind: &str, port: u16) -> Result<Launch> {
    let addresses = loopback_addresses(bind).await?;
    let end = last_port(port);
    let client = (port != 0).then(|| local_client().ok()).flatten();
    if let Some(client) = &client
        && let Some((address, _)) = find_instance(client, host, &addresses, port).await
    {
        return Ok(Launch::Reuse(address));
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
