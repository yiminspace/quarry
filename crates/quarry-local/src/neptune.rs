use crate::*;
use std::{path::Path, sync::Arc};
use tokio::sync::Mutex;
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    net::TcpListener,
};
#[derive(Debug, Clone, Serialize, Deserialize)]
struct ProcessRecord {
    pid: u32,
    identity: String,
    port: u16,
    backend: String,
    fixture_sha256: Option<String>,
}
fn process_identity(pid: u32) -> Result<String> {
    let output = std::process::Command::new("ps")
        .args(["-p", &pid.to_string(), "-o", "lstart=", "-o", "comm="])
        .env("LC_ALL", "C")
        .env("TZ", "UTC")
        .output()?;
    if !output.status.success() || output.stdout.is_empty() {
        return Err(RuntimeError::Operation(
            "process identity unavailable".into(),
        ));
    }
    Ok(String::from_utf8_lossy(&output.stdout).trim().into())
}
impl LocalManager {
    pub(crate) fn neptune_dir(&self) -> PathBuf {
        self.state_dir.join("neptune")
    }
    fn neptune_record(&self) -> Result<Option<ProcessRecord>> {
        let path = self.neptune_dir().join("pid.json");
        let bytes = match std::fs::read(path) {
            Ok(bytes) => bytes,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
            Err(error) => return Err(error.into()),
        };
        let record: ProcessRecord = serde_json::from_slice(&bytes)?;
        if process_identity(record.pid).is_ok_and(|id| id == record.identity) {
            Ok(Some(record))
        } else {
            Ok(None)
        }
    }
    fn neptune_client(&self) -> Result<reqwest::Client> {
        let cert = std::fs::read(self.neptune_dir().join("cert.pem"))?;
        let cert = reqwest::Certificate::from_pem(&cert)
            .map_err(|_| RuntimeError::Invalid("invalid local Neptune certificate".into()))?;
        reqwest::Client::builder()
            .add_root_certificate(cert)
            .timeout(Duration::from_secs(3))
            .no_proxy()
            .build()
            .map_err(|_| {
                RuntimeError::Operation("cannot initialize local Neptune HTTPS client".into())
            })
    }
    pub async fn neptune_status(&self) -> Result<Value> {
        let config = RuntimeConfig::load(&self.config.path)?;
        let port = config.local.get("neptune_port").copied().unwrap_or(18182);
        let record = self.neptune_record()?;
        let mut running = false;
        let mut backend = "empty".to_string();
        if let Some(record) = &record {
            backend = record.backend.clone();
            if let Ok(client) = self.neptune_client() {
                running = client
                    .get(format!("https://127.0.0.1:{}/health", record.port))
                    .send()
                    .await
                    .is_ok_and(|r| r.status().is_success());
            }
        }
        Ok(
            json!({"engine":"neptune","backend":backend,"docker":null,"running":running,"state":if running{"running"}else{"absent"},"port":record.as_ref().map(|r|r.port).unwrap_or(port),"pid":record.map(|r|r.pid),"volume":null,"volume_exists":false}),
        )
    }
    pub async fn up_neptune(&self, port: Option<u16>, fixture: Option<&Path>) -> Result<Value> {
        let _lock = self.state_lock("engine-neptune").await?;
        let config = RuntimeConfig::load(&self.config.path)?;
        let port =
            port.unwrap_or_else(|| config.local.get("neptune_port").copied().unwrap_or(18182));
        if port == 0 {
            return Err(RuntimeError::Invalid(
                "local Neptune port must be positive".into(),
            ));
        }
        let fixture_hash = fixture
            .map(|path| std::fs::read(path).map(|bytes| format!("{:x}", Sha256::digest(bytes))))
            .transpose()?;
        if let Some(record) = self.neptune_record()? {
            if record.port == port
                && record.fixture_sha256 == fixture_hash
                && self.neptune_status().await?["running"] == true
            {
                return Ok(json!({"engine":"neptune","state":"running","port":port}));
            }
            return Err(RuntimeError::Invalid(
                "stop the existing Neptune endpoint before changing its port or mode".into(),
            ));
        }
        let launcher = self.neptune_launcher.as_ref().ok_or_else(||RuntimeError::Invalid(
            "persistent local Neptune requires an explicit host daemon launcher; configure with_neptune_launcher or run serve_neptune in-process".into()
        ))?;
        self.require_free_port(port).await?;
        let dir = self.neptune_dir();
        std::fs::create_dir_all(&dir)?;
        let mut options = std::fs::OpenOptions::new();
        options.create(true).append(true);
        #[cfg(unix)]
        {
            use std::os::unix::fs::OpenOptionsExt;
            options.mode(0o600);
        }
        let log = options.open(dir.join("server.log"))?;
        let mut command = std::process::Command::new(&launcher.program);
        command
            .args(&launcher.prefix_args)
            .arg("--state-dir")
            .arg(&self.state_dir)
            .arg("--port")
            .arg(port.to_string());
        if let Some(fixture) = fixture {
            command.arg("--fixture").arg(fixture.canonicalize()?);
        }
        command
            .stdin(Stdio::null())
            .stdout(log.try_clone()?)
            .stderr(log);
        #[cfg(unix)]
        {
            use std::os::unix::process::CommandExt;
            command.process_group(0);
        }
        let mut child = command.spawn()?;
        for _ in 0..100 {
            if let Some(record) = self.neptune_record()?
                && record.port == port
                && record.fixture_sha256 == fixture_hash
                && self.neptune_status().await?["running"] == true
            {
                self.config.set_local_port("neptune", port)?;
                let old = config.local.get("neptune_port").copied().unwrap_or(18182);
                if old != port {
                    self.reconcile_ports("neptune", old, port)?;
                }
                return Ok(
                    json!({"engine":"neptune","state":"created","port":port,"backend":record.backend,"tls_ca_file":dir.join("cert.pem")}),
                );
            }
            if child.try_wait()?.is_some() {
                return Err(RuntimeError::Operation(
                    "local Neptune server exited; inspect its private log".into(),
                ));
            }
            tokio::time::sleep(Duration::from_millis(100)).await;
        }
        Err(RuntimeError::Operation(
            "local Neptune server did not become ready".into(),
        ))
    }
    pub async fn down_neptune(&self, purge: bool) -> Result<Value> {
        let _lock = self.state_lock("engine-neptune").await?;
        let record = self.neptune_record()?;
        if let Some(record) = &record {
            if !std::process::Command::new("kill")
                .args(["-TERM", &record.pid.to_string()])
                .status()?
                .success()
            {
                return Err(RuntimeError::Operation("cannot stop local Neptune".into()));
            }
            for _ in 0..50 {
                if self.neptune_record()?.is_none() {
                    break;
                }
                tokio::time::sleep(Duration::from_millis(100)).await;
            }
            if self.neptune_record()?.is_some() {
                return Err(RuntimeError::Operation(
                    "local Neptune did not stop; refusing to purge files".into(),
                ));
            }
        }
        let dir = self.neptune_dir();
        let _ = std::fs::remove_file(dir.join("pid.json"));
        if purge {
            for name in [
                "cert.pem",
                "key.pem",
                "server.log",
                "server-cert.pem",
                "server-key.pem",
                "cert.srl",
            ] {
                let _ = std::fs::remove_file(dir.join(name));
            }
        }
        Ok(
            json!({"engine":"neptune","stopped":record.is_some(),"purged":purge,"removed_volume":false}),
        )
    }
    pub async fn neptune_calls(&self, after: u64) -> Result<Value> {
        let record = self
            .neptune_record()?
            .ok_or_else(|| RuntimeError::NotFound("local Neptune mock is not running".into()))?;
        if record.backend != "mock" {
            return Err(RuntimeError::Invalid(
                "local Neptune is not in fixture mode".into(),
            ));
        }
        self.neptune_client()?
            .get(format!(
                "https://127.0.0.1:{}/__mock__/calls?after={after}",
                record.port
            ))
            .send()
            .await
            .map_err(|_| RuntimeError::Operation("cannot read local mock calls".into()))?
            .error_for_status()
            .map_err(|_| RuntimeError::Operation("mock calls request failed".into()))?
            .json()
            .await
            .map_err(|_| RuntimeError::Operation("invalid mock call response".into()))
    }
}
#[derive(Default)]
struct MockState {
    fixture: Option<Value>,
    hash: Option<String>,
    calls: Vec<Value>,
    next: u64,
}
pub async fn serve_neptune(state_dir: PathBuf, port: u16, fixture: Option<PathBuf>) -> Result<()> {
    if port == 0 {
        return Err(RuntimeError::Invalid("port must be positive".into()));
    }
    let dir = state_dir.join("neptune");
    std::fs::create_dir_all(&dir)?;
    let lock = std::fs::OpenOptions::new()
        .create(true)
        .truncate(false)
        .read(true)
        .write(true)
        .open(dir.join("server.lock"))?;
    if fs2::FileExt::try_lock_exclusive(&lock).is_err() {
        return Err(RuntimeError::Operation(
            "local Neptune is already running".into(),
        ));
    }
    let mut state = MockState::default();
    if let Some(fixture) = fixture {
        let bytes = std::fs::read(fixture)?;
        let data: Value = serde_json::from_slice(&bytes)?;
        let responses = data["responses"].as_array().ok_or_else(|| {
            RuntimeError::Invalid("Neptune fixture requires responses array".into())
        })?;
        for response in responses {
            if response["query_contains"]
                .as_str()
                .is_none_or(|v| v.is_empty())
                || !response["results"].is_array()
            {
                return Err(RuntimeError::Invalid(
                    "invalid Neptune fixture response".into(),
                ));
            }
        }
        state.fixture = Some(data);
        state.hash = Some(format!("{:x}", Sha256::digest(bytes)));
    }
    let cert = dir.join("cert.pem");
    let key = dir.join("key.pem");
    if !cert.exists() || !key.exists() {
        let output = Command::new("openssl")
            .args([
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-days",
                "3650",
                "-subj",
                "/CN=localhost",
                "-addext",
                "subjectAltName=DNS:localhost,IP:127.0.0.1",
                "-keyout",
            ])
            .arg(&key)
            .arg("-out")
            .arg(&cert)
            .output()
            .await?;
        if !output.status.success() {
            return Err(RuntimeError::Operation(
                "cannot create local Neptune TLS certificate".into(),
            ));
        }
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            std::fs::set_permissions(&key, std::fs::Permissions::from_mode(0o600))?;
        }
    }
    let server_cert = dir.join("server-cert.pem");
    let server_key = dir.join("server-key.pem");
    if !server_cert.exists() || !server_key.exists() {
        let csr = dir.join("server.csr");
        let generated = Command::new("openssl")
            .args([
                "req",
                "-new",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-subj",
                "/CN=localhost",
                "-keyout",
            ])
            .arg(&server_key)
            .arg("-out")
            .arg(&csr)
            .output()
            .await?;
        if !generated.status.success() {
            return Err(RuntimeError::Operation(
                "cannot generate local Neptune TLS key".into(),
            ));
        }
        let extensions = dir.join("server.ext");
        quarry_runtime::atomic_write(&extensions,b"basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\nsubjectAltName=DNS:localhost,IP:127.0.0.1\n")?;
        let signed = Command::new("openssl")
            .args(["x509", "-req", "-days", "3650", "-in"])
            .arg(&csr)
            .arg("-CA")
            .arg(&cert)
            .arg("-CAkey")
            .arg(&key)
            .arg("-CAcreateserial")
            .arg("-out")
            .arg(&server_cert)
            .arg("-extfile")
            .arg(&extensions)
            .output()
            .await?;
        if !signed.status.success() {
            return Err(RuntimeError::Operation(
                "cannot sign local Neptune TLS server certificate".into(),
            ));
        }
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            std::fs::set_permissions(&server_key, std::fs::Permissions::from_mode(0o600))?;
        }
        let _ = std::fs::remove_file(csr);
        let _ = std::fs::remove_file(extensions);
    }
    let certificates = rustls_pemfile::certs(&mut std::io::BufReader::new(std::fs::File::open(
        &server_cert,
    )?))
    .collect::<std::io::Result<Vec<_>>>()?;
    let private = rustls_pemfile::private_key(&mut std::io::BufReader::new(std::fs::File::open(
        &server_key,
    )?))?
    .ok_or_else(|| RuntimeError::Invalid("local TLS key missing".into()))?;
    let server = tokio_rustls::rustls::ServerConfig::builder_with_provider(Arc::new(
        tokio_rustls::rustls::crypto::ring::default_provider(),
    ))
    .with_safe_default_protocol_versions()
    .map_err(|_| RuntimeError::Operation("TLS protocol configuration failed".into()))?
    .with_no_client_auth()
    .with_single_cert(certificates, private)
    .map_err(|_| RuntimeError::Invalid("local TLS certificate/key mismatch".into()))?;
    let acceptor = tokio_rustls::TlsAcceptor::from(Arc::new(server));
    let listener = TcpListener::bind(("127.0.0.1", port)).await?;
    let record = ProcessRecord {
        pid: std::process::id(),
        identity: process_identity(std::process::id())?,
        port,
        backend: if state.fixture.is_some() {
            "mock"
        } else {
            "empty"
        }
        .into(),
        fixture_sha256: state.hash.clone(),
    };
    quarry_runtime::atomic_write(&dir.join("pid.json"), &serde_json::to_vec(&record)?)?;
    let state = Arc::new(Mutex::new(state));
    #[cfg(unix)]
    let mut terminate = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())?;
    let shutdown = async {
        #[cfg(unix)]
        {
            tokio::select! {_ = tokio::signal::ctrl_c()=>{},_ = terminate.recv()=>{}}
        }
        #[cfg(not(unix))]
        {
            let _ = tokio::signal::ctrl_c().await;
        }
    };
    tokio::pin!(shutdown);
    loop {
        tokio::select! {_ = &mut shutdown=>break,incoming=listener.accept()=>{let(stream,_)=incoming?;let acceptor=acceptor.clone();let state=state.clone();tokio::spawn(async move{let _=tokio::time::timeout(Duration::from_secs(15),async move{let mut tls=acceptor.accept(stream).await.map_err(std::io::Error::other)?;let(status,body)=handle_request(&mut tls,&state).await.unwrap_or((400,json!({"message":"invalid request"})));let body=serde_json::to_vec(&body).map_err(std::io::Error::other)?;tls.write_all(format!("HTTP/1.1 {status} {}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",if status==200{"OK"}else{"Error"},body.len()).as_bytes()).await?;tls.write_all(&body).await?;tls.shutdown().await?;Ok::<_,std::io::Error>(())}).await;});}}
    }
    let _ = std::fs::remove_file(dir.join("pid.json"));
    drop(lock);
    Ok(())
}
async fn handle_request<S: tokio::io::AsyncRead + tokio::io::AsyncWrite + Unpin>(
    stream: &mut S,
    state: &Arc<Mutex<MockState>>,
) -> Result<(u16, Value)> {
    let mut header = vec![];
    while !header.ends_with(b"\r\n\r\n") {
        if header.len() > 16384 {
            return Err(RuntimeError::Invalid("headers too large".into()));
        }
        header.push(stream.read_u8().await?);
    }
    let header =
        String::from_utf8(header).map_err(|_| RuntimeError::Invalid("invalid headers".into()))?;
    let first = header.lines().next().unwrap_or("");
    let mut parts = first.split_whitespace();
    let method = parts.next().unwrap_or("");
    let path = parts.next().unwrap_or("");
    let length = header
        .lines()
        .filter_map(|l| l.split_once(':'))
        .find(|(k, _)| k.eq_ignore_ascii_case("content-length"))
        .map(|(_, v)| v.trim().parse::<usize>())
        .transpose()
        .map_err(|_| RuntimeError::Invalid("invalid content length".into()))?
        .unwrap_or(0);
    if length > 8 * 1024 * 1024 {
        return Ok((413, json!({"message":"request too large"})));
    }
    let mut body = vec![0; length];
    stream.read_exact(&mut body).await?;
    let mut state = state.lock().await;
    if method == "GET" && path == "/health" {
        return Ok((
            200,
            json!({"status":"ok","backend":if state.fixture.is_some(){"mock"}else{"empty"},"fixture_sha256":state.hash}),
        ));
    }
    if method == "GET" && path.starts_with("/__mock__/calls") && state.fixture.is_some() {
        let url = url::Url::parse(&format!("http://localhost{path}"))
            .map_err(|_| RuntimeError::Invalid("invalid path".into()))?;
        let after = url
            .query_pairs()
            .find(|(k, _)| k == "after")
            .map(|(_, v)| v.parse::<usize>())
            .transpose()
            .map_err(|_| RuntimeError::Invalid("invalid cursor".into()))?
            .unwrap_or(0);
        return Ok((
            200,
            json!({"calls":state.calls.iter().filter(|v|v["sequence"].as_u64().unwrap_or(0)>after as u64).collect::<Vec<_>>(),"next":state.next}),
        ));
    }
    if method != "POST" || !path.eq_ignore_ascii_case("/openCypher") {
        return Ok((404, json!({"message":"not found"})));
    }
    let payload = if header.to_lowercase().contains("application/json") {
        serde_json::from_slice::<Value>(&body)?
    } else {
        let fields = url::form_urlencoded::parse(&body).collect::<BTreeMap<_, _>>();
        json!({"query":fields.get("query").map(|v|v.as_ref()).unwrap_or(""),"parameters":fields.get("parameters").and_then(|s|serde_json::from_str::<Value>(s).ok()).unwrap_or_else(||json!({}))})
    };
    let query = payload["query"]
        .as_str()
        .or_else(|| payload["openCypherQuery"].as_str())
        .unwrap_or("");
    let params = if let Some(s) = payload["parameters"].as_str() {
        serde_json::from_str::<Value>(s)?
    } else {
        payload
            .get("parameters")
            .cloned()
            .unwrap_or_else(|| json!({}))
    };
    if let Some(fixture) = &state.fixture {
        let matched = fixture["responses"]
            .as_array()
            .unwrap()
            .iter()
            .find(|r| {
                let needle = r["query_contains"].as_str().unwrap_or("");
                let query_ok = if r["query_exact"] == true {
                    query == needle
                } else {
                    query.contains(needle)
                };
                let expected = r.get("parameters").cloned().unwrap_or_else(|| json!({}));
                let params_ok = if r["parameters_exact"] == true {
                    params == expected
                } else {
                    expected
                        .as_object()
                        .is_some_and(|e| e.iter().all(|(k, v)| params.get(k) == Some(v)))
                };
                query_ok && params_ok
            })
            .map(|r| r["results"].clone());
        state.next += 1;
        let next = state.next;
        state.calls.push(
            json!({"sequence":next,"query":query,"parameters":params,"matched":matched.is_some()}),
        );
        if state.calls.len() > 10000 {
            state.calls.remove(0);
        }
        return Ok(match matched {
            Some(results) => (200, json!({"results":results})),
            None => (501, json!({"message":"no matching mock response"})),
        });
    }
    Ok((200, json!({"results":[]})))
}

#[cfg(test)]
mod tests {
    use super::*;
    #[tokio::test]
    async fn persistent_server_requires_explicit_host_launcher() {
        let dir = tempfile::tempdir().unwrap();
        let manager = LocalManager::new(
            RuntimeConfig::load(dir.path().join("config.toml")).unwrap(),
            dir.path().join("state"),
        );
        let error = manager.up_neptune(None, None).await.unwrap_err();
        assert!(matches!(error, RuntimeError::Invalid(_)));
        assert!(error.to_string().contains("explicit host daemon launcher"));
        assert!(!manager.neptune_dir().join("pid.json").exists());
        assert!(!manager.neptune_dir().join("server.log").exists());
    }
    #[tokio::test]
    async fn persistent_server_passes_arguments_to_the_host_selected_entrypoint() {
        let dir = tempfile::tempdir().unwrap();
        let script = dir.path().join("feature-host");
        let args = dir.path().join("args");
        write_executable_fixture(
            &script,
            &format!(
                "#!/bin/sh\nprintf '%s\\n' \"$@\" > '{}'\nexit 70\n",
                args.display()
            ),
        );
        let manager = LocalManager::new(
            RuntimeConfig::load(dir.path().join("config.toml")).unwrap(),
            dir.path().join("state"),
        )
        .with_neptune_launcher(
            DaemonLauncher::new(&script)
                .arg("database")
                .arg("serve-local"),
        );
        let socket = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let port = socket.local_addr().unwrap().port();
        drop(socket);
        assert!(manager.up_neptune(Some(port), None).await.is_err());
        let args = std::fs::read_to_string(args).unwrap();
        assert!(args.starts_with("database\nserve-local\n--state-dir\n"));
        assert!(args.contains(&format!("--port\n{port}")));
        assert!(!args.contains("internal-neptune"));
    }
    #[tokio::test]
    async fn real_local_tls_fixture_is_strict_and_records_calls() {
        let dir = tempfile::tempdir().unwrap();
        let fixture = dir.path().join("fixture.json");
        std::fs::write(&fixture,serde_json::to_vec(&json!({"responses":[{"query_contains":"RETURN 7 AS n","query_exact":true,"parameters_exact":true,"parameters":{},"results":[{"n":7}]}]})).unwrap()).unwrap();
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        drop(listener);
        let state = dir.path().join("state");
        let handle = tokio::spawn(serve_neptune(state.clone(), port, Some(fixture)));
        for _ in 0..100 {
            if state.join("neptune/pid.json").exists() {
                break;
            }
            if handle.is_finished() {
                panic!("local Neptune server failed during startup");
            }
            tokio::time::sleep(Duration::from_millis(30)).await;
        }
        let cert =
            reqwest::Certificate::from_pem(&std::fs::read(state.join("neptune/cert.pem")).unwrap())
                .unwrap();
        let client = reqwest::Client::builder()
            .add_root_certificate(cert)
            .no_proxy()
            .build()
            .unwrap();
        let base = format!("https://127.0.0.1:{port}");
        let health: Value = client
            .get(format!("{base}/health"))
            .send()
            .await
            .unwrap()
            .json()
            .await
            .unwrap();
        assert_eq!(health["backend"], "mock");
        let result: Value = client
            .post(format!("{base}/openCypher"))
            .json(&json!({"query":"RETURN 7 AS n","parameters":{}}))
            .send()
            .await
            .unwrap()
            .json()
            .await
            .unwrap();
        assert_eq!(result["results"][0]["n"], 7);
        let missing = client
            .post(format!("{base}/openCypher"))
            .json(&json!({"query":"RETURN 8 AS n","parameters":{}}))
            .send()
            .await
            .unwrap();
        assert_eq!(missing.status().as_u16(), 501);
        let calls: Value = client
            .get(format!("{base}/__mock__/calls?after=0"))
            .send()
            .await
            .unwrap()
            .json()
            .await
            .unwrap();
        assert_eq!(calls["next"], 2);
        assert_eq!(calls["calls"][1]["matched"], false);
        handle.abort();
        let _ = handle.await;
    }
}
