//! No global hosts/DNS edits: original certificate identities traverse explicit
//! forwarding endpoints over the same private transport used by SSH consumers.
#![cfg(unix)]
use quarry_core::*;
use std::{io::BufReader, path::PathBuf, process::Command, sync::Arc};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio_rustls::{rustls, TlsAcceptor};

const HOST: &str = "database.quarry.invalid";
struct Certificates {
    dir: tempfile::TempDir,
}
impl Certificates {
    fn new() -> Self {
        let dir = tempfile::tempdir().unwrap();
        let command = |args: &[&str]| {
            let result = Command::new("openssl")
                .args(args)
                .current_dir(dir.path())
                .output()
                .expect("openssl is required for isolated TLS contracts");
            assert!(
                result.status.success(),
                "openssl fixture generation failed: {}",
                String::from_utf8_lossy(&result.stderr)
            );
        };
        command(&[
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-sha256",
            "-nodes",
            "-days",
            "1",
            "-subj",
            "/CN=Quarry fixture CA",
            "-keyout",
            "ca.key",
            "-out",
            "ca.pem",
            "-addext",
            "basicConstraints=critical,CA:TRUE",
            "-addext",
            "keyUsage=critical,keyCertSign,cRLSign",
        ]);
        command(&[
            "req",
            "-newkey",
            "rsa:2048",
            "-sha256",
            "-nodes",
            "-subj",
            "/CN=database.quarry.invalid",
            "-keyout",
            "server.key",
            "-out",
            "server.csr",
        ]);
        std::fs::write(dir.path().join("server.ext"), format!("basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\nsubjectAltName=DNS:{HOST}\n")).unwrap();
        command(&[
            "x509",
            "-req",
            "-in",
            "server.csr",
            "-CA",
            "ca.pem",
            "-CAkey",
            "ca.key",
            "-CAcreateserial",
            "-days",
            "1",
            "-sha256",
            "-extfile",
            "server.ext",
            "-out",
            "server.pem",
        ]);
        Self { dir }
    }
    fn ca(&self) -> PathBuf {
        self.dir.path().join("ca.pem")
    }
    fn acceptor(&self) -> TlsAcceptor {
        let certificates = rustls_pemfile::certs(&mut BufReader::new(
            std::fs::File::open(self.dir.path().join("server.pem")).unwrap(),
        ))
        .collect::<std::result::Result<Vec<_>, _>>()
        .unwrap();
        let key = rustls_pemfile::private_key(&mut BufReader::new(
            std::fs::File::open(self.dir.path().join("server.key")).unwrap(),
        ))
        .unwrap()
        .unwrap();
        let provider = Arc::new(rustls::crypto::ring::default_provider());
        let config = rustls::ServerConfig::builder_with_provider(provider)
            .with_safe_default_protocol_versions()
            .unwrap()
            .with_no_client_auth()
            .with_single_cert(certificates, key)
            .unwrap();
        TlsAcceptor::from(Arc::new(config))
    }
}
async fn read_packet<S: AsyncReadExt + Unpin>(stream: &mut S) -> Vec<u8> {
    let mut header = [0u8; 4];
    stream.read_exact(&mut header).await.unwrap();
    let length =
        usize::from(header[0]) | (usize::from(header[1]) << 8) | (usize::from(header[2]) << 16);
    assert!(length < 1_048_576);
    let mut payload = vec![0; length];
    stream.read_exact(&mut payload).await.unwrap();
    payload
}
async fn write_packet<S: AsyncWriteExt + Unpin>(stream: &mut S, sequence: u8, payload: &[u8]) {
    let length = payload.len();
    stream
        .write_all(&[
            (length & 255) as u8,
            ((length >> 8) & 255) as u8,
            ((length >> 16) & 255) as u8,
            sequence,
        ])
        .await
        .unwrap();
    stream.write_all(payload).await.unwrap();
    stream.flush().await.unwrap();
}
async fn mysql_tls_fixture(
    certs: &Certificates,
) -> (NetworkEndpoint, tokio::task::JoinHandle<bool>) {
    mysql_tls_fixture_mode(certs, false).await
}
async fn mysql_tls_fixture_mode(
    certs: &Certificates,
    expect_ping: bool,
) -> (NetworkEndpoint, tokio::task::JoinHandle<bool>) {
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let port = listener.local_addr().unwrap().port();
    let acceptor = certs.acceptor();
    let task = tokio::spawn(async move {
        let (mut stream, _) = listener.accept().await.unwrap();
        let capabilities = 0x0000_0200u32 | 0x0000_0800 | 0x0000_8000 | 0x0008_0000 | 0x0000_2000;
        let mut greeting = vec![10];
        greeting.extend_from_slice(b"8.4.0-quarry-fixture\0");
        greeting.extend_from_slice(&7u32.to_le_bytes());
        greeting.extend_from_slice(b"abcdefgh\0");
        greeting.extend_from_slice(&(capabilities as u16).to_le_bytes());
        greeting.push(45);
        greeting.extend_from_slice(&2u16.to_le_bytes());
        greeting.extend_from_slice(&((capabilities >> 16) as u16).to_le_bytes());
        greeting.push(21);
        greeting.extend_from_slice(&[0; 10]);
        greeting.extend_from_slice(b"ijklmnopqrst\0");
        greeting.extend_from_slice(b"mysql_native_password\0");
        write_packet(&mut stream, 0, &greeting).await;
        let request = read_packet(&mut stream).await;
        assert_eq!(
            request.len(),
            32,
            "Client must request TLS before authentication"
        );
        let Ok(mut tls) = acceptor.accept(stream).await else {
            return false;
        };
        assert_eq!(
            tls.get_ref().1.server_name(),
            Some(HOST),
            "Original hostname must be the TLS SNI"
        );
        let auth = read_packet(&mut tls).await;
        assert!(auth.len() > 32, "TLS established before authentication");
        if expect_ping {
            let ok = [0x00, 0, 0, 2, 0, 0, 0];
            write_packet(&mut tls, 3, &ok).await;
            let command = read_packet(&mut tls).await;
            assert_eq!(
                command,
                [0x0e],
                "Ping must use COM_PING directly, without SQL, SET, BEGIN or statement preparation"
            );
            write_packet(&mut tls, 1, &ok).await;
            return true;
        }
        let mut failure = vec![0xff];
        failure.extend_from_slice(&1045u16.to_le_bytes());
        failure.extend_from_slice(b"#28000quarry fixture authenticated over verified TLS");
        write_packet(&mut tls, 3, &failure).await;
        true
    });
    (
        NetworkEndpoint {
            host: "127.0.0.1".into(),
            port,
        },
        task,
    )
}

async fn postgres_ping_tls_fixture(
    certs: &Certificates,
) -> (NetworkEndpoint, tokio::task::JoinHandle<bool>) {
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let port = listener.local_addr().unwrap().port();
    let acceptor = certs.acceptor();
    let task = tokio::spawn(async move {
        let (mut stream, _) = listener.accept().await.unwrap();
        let mut ssl_request = [0; 8];
        stream.read_exact(&mut ssl_request).await.unwrap();
        assert_eq!(ssl_request, [0, 0, 0, 8, 4, 210, 22, 47]);
        stream.write_all(b"S").await.unwrap();
        stream.flush().await.unwrap();
        let Ok(mut tls) = acceptor.accept(stream).await else {
            return false;
        };
        assert_eq!(tls.get_ref().1.server_name(), Some(HOST));
        let size = tls.read_u32().await.unwrap() as usize;
        assert!((8..16_384).contains(&size));
        let mut startup = vec![0; size - 4];
        tls.read_exact(&mut startup).await.unwrap();
        assert_eq!(&startup[..4], &[0, 3, 0, 0]);
        // AuthenticationOk, BackendKeyData, ReadyForQuery. The peer accepts
        // protocol startup settings, then exactly one Sync; no SQL is supported.
        tls.write_all(&[
            b'R', 0, 0, 0, 8, 0, 0, 0, 0, b'K', 0, 0, 0, 12, 0, 0, 0, 42, 0, 0, 0, 17, b'Z', 0, 0,
            0, 5, b'I',
        ])
        .await
        .unwrap();
        tls.flush().await.unwrap();
        let mut message = [0; 5];
        tls.read_exact(&mut message).await.unwrap();
        assert_eq!(
            message,
            [b'S', 0, 0, 0, 4],
            "PostgreSQL ping must use Sync, without Query/Parse/Bind/Describe, BEGIN or SET"
        );
        tls.write_all(&[b'Z', 0, 0, 0, 5, b'I']).await.unwrap();
        tls.flush().await.unwrap();
        true
    });
    (
        NetworkEndpoint {
            host: "127.0.0.1".into(),
            port,
        },
        task,
    )
}

#[tokio::test]
async fn postgres_ping_uses_sync_over_verified_forwarded_tls_and_rejects_wrong_hostname() {
    let certs = Certificates::new();
    let (endpoint, server) = postgres_ping_tls_fixture(&certs).await;
    let mut connection = ConnectionSpec::new(
        Engine::Postgres,
        format!("postgres://fixture:fixture-only@{HOST}:5432/fixture?sslmode=verify-full"),
    );
    connection.network_endpoint = Some(endpoint);
    connection.tls_ca_file = Some(certs.ca());
    connection.timeout_ms = 3000;
    assert_eq!(ping(&connection).await.unwrap()["ok"], true);
    assert!(server.await.unwrap());
    let (endpoint, server) = postgres_ping_tls_fixture(&certs).await;
    connection.network_endpoint = Some(endpoint);
    connection.url = connection.url.replace(HOST, "wrong.quarry.invalid");
    assert_eq!(
        ping(&connection).await.unwrap_err().code,
        ErrorCode::Connection
    );
    assert!(
        !server.await.unwrap(),
        "Wrong hostname must fail before authentication or Sync"
    );
}

#[tokio::test]
async fn mysql_ping_uses_com_ping_over_verified_forwarded_tls_and_rejects_wrong_hostname() {
    let certs = Certificates::new();
    let (endpoint, server) = mysql_tls_fixture_mode(&certs, true).await;
    let mut connection = ConnectionSpec::new(
        Engine::MySql,
        format!("mysql://fixture:fixture-only@{HOST}:3306/fixture?ssl-mode=VERIFY_IDENTITY"),
    );
    connection.network_endpoint = Some(endpoint);
    connection.tls_ca_file = Some(certs.ca());
    connection.timeout_ms = 3000;
    assert_eq!(ping(&connection).await.unwrap()["ok"], true);
    assert!(server.await.unwrap());
    let (endpoint, server) = mysql_tls_fixture_mode(&certs, true).await;
    connection.network_endpoint = Some(endpoint);
    connection.url = connection.url.replace(HOST, "wrong.quarry.invalid");
    assert_eq!(
        ping(&connection).await.unwrap_err().code,
        ErrorCode::Connection
    );
    assert!(
        !server.await.unwrap(),
        "Wrong hostname must fail before authentication or COM_PING"
    );
}
#[tokio::test]
async fn mysql_forwarding_preserves_strict_tls_identity_and_rejects_wrong_certificates() {
    let certs = Certificates::new();
    let (endpoint, server) = mysql_tls_fixture(&certs).await;
    let mut connection = ConnectionSpec::new(
        Engine::MySql,
        format!("mysql://fixture:fixture-only@{HOST}:3306/fixture?ssl-mode=VERIFY_IDENTITY"),
    );
    connection.network_endpoint = Some(endpoint);
    connection.tls_ca_file = Some(certs.ca());
    connection.timeout_ms = 3000;
    let error = execute(&connection, QueryRequest::new("SELECT 1"))
        .await
        .unwrap_err();
    assert!(
        error
            .message
            .contains("quarry fixture authenticated over verified TLS"),
        "{error}"
    );
    assert!(server.await.unwrap());
    let (endpoint, server) = mysql_tls_fixture(&certs).await;
    connection.network_endpoint = Some(endpoint);
    connection.url = connection.url.replace(HOST, "wrong.quarry.invalid");
    let error = execute(&connection, QueryRequest::new("SELECT 1"))
        .await
        .unwrap_err();
    assert_eq!(error.code, ErrorCode::Connection);
    assert!(
        !server.await.unwrap(),
        "Wrong hostname must fail the TLS handshake"
    );
    let (endpoint, server) = mysql_tls_fixture(&certs).await;
    connection.network_endpoint = Some(endpoint);
    connection.url = connection.url.replace("wrong.quarry.invalid", HOST);
    connection.tls_ca_file = None;
    assert_eq!(
        execute(&connection, QueryRequest::new("SELECT 1"))
            .await
            .unwrap_err()
            .code,
        ErrorCode::Connection
    );
    assert!(!server.await.unwrap(), "Untrusted fixture CA must fail TLS");
}
#[tokio::test]
async fn neptune_forwarding_preserves_original_tls_identity_and_http_authority() {
    let certs = Certificates::new();
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let port = listener.local_addr().unwrap().port();
    let acceptor = certs.acceptor();
    let server = tokio::spawn(async move {
        let (stream, _) = listener.accept().await.unwrap();
        let mut tls = acceptor.accept(stream).await.unwrap();
        assert_eq!(tls.get_ref().1.server_name(), Some(HOST));
        let mut request = vec![];
        let mut buffer = [0u8; 4096];
        loop {
            let n = tls.read(&mut buffer).await.unwrap();
            request.extend_from_slice(&buffer[..n]);
            if request.windows(4).any(|v| v == b"\r\n\r\n") {
                break;
            }
        }
        let body = r#"{"results":[{"value":"verified tunnel"}]}"#;
        tls.write_all(
            format!(
                "HTTP/1.1 200 OK\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
                body.len()
            )
            .as_bytes(),
        )
        .await
        .unwrap();
        String::from_utf8(request).unwrap()
    });
    let mut connection = ConnectionSpec::new(Engine::Neptune, format!("https://{HOST}:8182"));
    connection.network_endpoint = Some(NetworkEndpoint {
        host: "127.0.0.1".into(),
        port,
    });
    connection.tls_ca_file = Some(certs.ca());
    connection.timeout_ms = 3000;
    let result = execute(&connection, QueryRequest::new("RETURN 1"))
        .await
        .unwrap();
    assert_eq!(result.rows_json()[0]["value"], "verified tunnel");
    let request = server.await.unwrap().to_lowercase();
    assert!(request.contains(&format!("host: {HOST}:8182")), "{request}");
}

#[tokio::test]
async fn real_postgres_strict_tls_forwarding_and_cancellation() {
    if std::env::var("QUARRY_TEST_DOCKER").as_deref() != Ok("1") {
        eprintln!("SKIPPED: set QUARRY_TEST_DOCKER=1 for an isolated real PostgreSQL TLS server");
        return;
    }
    struct Container(String);
    impl Drop for Container {
        fn drop(&mut self) {
            let _ = Command::new("docker").args(["rm", "-f", &self.0]).output();
        }
    }
    let certs = Certificates::new();
    let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let port = listener.local_addr().unwrap().port();
    drop(listener);
    let name = format!("quarry-rust-tls-{}", uuid::Uuid::new_v4().simple());
    let _cleanup = Container(name.clone());
    let output=Command::new("docker").args(["run","--detach","--rm","--name",&name,"-e","POSTGRES_PASSWORD=fixture-only","-e","POSTGRES_DB=quarry_tls","-p",&format!("127.0.0.1:{port}:5432"),"--mount",&format!("type=bind,source={},target=/quarry-certs,readonly",certs.dir.path().display()),"--entrypoint","sh","postgres:16-alpine","-c","cp /quarry-certs/server.pem /tmp/server.pem && cp /quarry-certs/server.key /tmp/server.key && chown postgres:postgres /tmp/server.pem /tmp/server.key && chmod 600 /tmp/server.key && exec docker-entrypoint.sh postgres -c ssl=on -c ssl_cert_file=/tmp/server.pem -c ssl_key_file=/tmp/server.key"]).output().expect("Docker executable required");
    assert!(
        output.status.success(),
        "isolated TLS container launch failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    let mut connection = ConnectionSpec::new(
        Engine::Postgres,
        format!("postgres://postgres:fixture-only@{HOST}:5432/quarry_tls?sslmode=verify-full"),
    );
    connection.network_endpoint = Some(NetworkEndpoint {
        host: "127.0.0.1".into(),
        port,
    });
    connection.tls_ca_file = Some(certs.ca());
    connection.connect_timeout_ms = 1000;
    connection.timeout_ms = 3000;
    let started = std::time::Instant::now();
    loop {
        match execute(
            &connection,
            QueryRequest::new(
                "SELECT current_database() AS name,ssl FROM pg_stat_ssl WHERE pid=pg_backend_pid()",
            ),
        )
        .await
        {
            Ok(result) => {
                assert_eq!(result.rows_json()[0]["name"], "quarry_tls");
                assert_eq!(result.rows_json()[0]["ssl"], true);
                break;
            }
            Err(error) => {
                assert!(
                    started.elapsed() < std::time::Duration::from_secs(30),
                    "TLS PostgreSQL did not become ready: {error}"
                );
                tokio::time::sleep(std::time::Duration::from_millis(100)).await;
            }
        }
    }
    let mut wrong = connection.clone();
    wrong.url = wrong.url.replace(HOST, "wrong.quarry.invalid");
    assert_eq!(
        execute(&wrong, QueryRequest::new("SELECT 1"))
            .await
            .unwrap_err()
            .code,
        ErrorCode::Connection,
        "Mismatched hostname must be rejected"
    );
    wrong = connection.clone();
    wrong.tls_ca_file = None;
    assert_eq!(
        execute(&wrong, QueryRequest::new("SELECT 1"))
            .await
            .unwrap_err()
            .code,
        ErrorCode::Connection,
        "Untrusted CA must be rejected"
    );
    let request = QueryRequest::new("SELECT pg_sleep(10)");
    let cancel = request.cancellation.clone();
    let copied = connection.clone();
    let running = tokio::spawn(async move { execute(&copied, request).await });
    tokio::time::sleep(std::time::Duration::from_millis(100)).await;
    cancel.cancel();
    assert_eq!(
        running.await.unwrap().unwrap_err().code,
        ErrorCode::Cancelled
    );
    let mut stopped = false;
    for _ in 0..30 {
        let result=execute(&connection,QueryRequest::new("SELECT count(*) AS active FROM pg_stat_activity WHERE state='active' AND query LIKE 'SELECT pg_sleep(10)%' AND pid<>pg_backend_pid()")).await.unwrap();
        if result.rows_json()[0]["active"] == 0 {
            stopped = true;
            break;
        }
        tokio::time::sleep(std::time::Duration::from_millis(50)).await;
    }
    assert!(
        stopped,
        "Cancellation must reach the real TLS PostgreSQL backend through the forwarding lease"
    );
}

#[tokio::test]
async fn real_rediss_forwarding_shared_query_metadata_inspect_and_ping_verify_hostname() {
    if std::env::var("QUARRY_TEST_DOCKER").as_deref() != Ok("1") {
        eprintln!("SKIPPED: set QUARRY_TEST_DOCKER=1 for an isolated real Redis TLS server");
        return;
    }
    struct Container(String);
    impl Drop for Container {
        fn drop(&mut self) {
            let _ = Command::new("docker").args(["rm", "-f", &self.0]).output();
        }
    }
    let certs = Certificates::new();
    let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let port = listener.local_addr().unwrap().port();
    drop(listener);
    let name = format!("quarry-rust-rediss-{}", uuid::Uuid::new_v4().simple());
    let _cleanup = Container(name.clone());
    let output=Command::new("docker").args(["run","--detach","--rm","--name",&name,"-p",&format!("127.0.0.1:{port}:6379"),"--mount",&format!("type=bind,source={},target=/quarry-certs,readonly",certs.dir.path().display()),"--entrypoint","sh","redis:7-alpine","-c","cp /quarry-certs/server.pem /tmp/server.pem && cp /quarry-certs/server.key /tmp/server.key && cp /quarry-certs/ca.pem /tmp/ca.pem && chown redis:redis /tmp/server.pem /tmp/server.key /tmp/ca.pem && chmod 600 /tmp/server.key && exec docker-entrypoint.sh redis-server --port 0 --tls-port 6379 --tls-cert-file /tmp/server.pem --tls-key-file /tmp/server.key --tls-ca-cert-file /tmp/ca.pem --tls-auth-clients no --requirepass fixture-only"]).output().expect("Docker executable required");
    assert!(
        output.status.success(),
        "isolated Redis TLS container launch failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    let mut connection = ConnectionSpec::new(
        Engine::Redis,
        format!("rediss://:fixture-only@{HOST}:6379/3"),
    );
    connection.network_endpoint = Some(NetworkEndpoint {
        host: "127.0.0.1".into(),
        port,
    });
    connection.tls_ca_file = Some(certs.ca());
    connection.connect_timeout_ms = 1000;
    connection.timeout_ms = 3000;
    let started = std::time::Instant::now();
    loop {
        match ping(&connection).await {
            Ok(result) => {
                assert_eq!(result["ok"], true);
                break;
            }
            Err(error) => {
                assert!(
                    started.elapsed() < std::time::Duration::from_secs(20),
                    "TLS Redis did not become ready: {error}"
                );
                tokio::time::sleep(std::time::Duration::from_millis(100)).await;
            }
        }
    }
    let mut write = QueryRequest::new("SET 'quarry tls\nkey' 'verified\nvalue'");
    write.policy.allow_write = true;
    execute(&connection, write).await.unwrap();
    let result = execute(&connection, QueryRequest::new("GET 'quarry tls\nkey'"))
        .await
        .unwrap();
    assert_eq!(result.rows_json()[0]["value"], "verified\nvalue");
    let inspected = inspect_key(&connection, "quarry tls\nkey").await.unwrap();
    assert_eq!(inspected["rows"][0]["type"], "string");
    assert_eq!(inspected["rows"][0]["value"], "verified\nvalue");
    let catalog = tables(&connection).await.unwrap();
    assert_eq!(catalog["keys"][0]["key"], "quarry tls\nkey");
    assert_eq!(catalog["keys"][0]["type"], "string");
    // Every operation follows the same verified connector, including helper paths.
    let mut wrong = connection.clone();
    wrong.url = wrong.url.replace(HOST, "wrong.quarry.invalid");
    assert_eq!(
        execute(&wrong, QueryRequest::new("GET 'quarry tls\nkey'"))
            .await
            .unwrap_err()
            .code,
        ErrorCode::Connection
    );
    assert_eq!(
        inspect_key(&wrong, "quarry tls\nkey")
            .await
            .unwrap_err()
            .code,
        ErrorCode::Connection
    );
    assert_eq!(
        tables(&wrong).await.unwrap_err().code,
        ErrorCode::Connection
    );
    assert_eq!(ping(&wrong).await.unwrap_err().code, ErrorCode::Connection);
    wrong = connection.clone();
    wrong.tls_ca_file = None;
    assert_eq!(
        ping(&wrong).await.unwrap_err().code,
        ErrorCode::Connection,
        "Untrusted CA must fail Redis TLS"
    );
    wrong = connection.clone();
    wrong.url.push_str("#insecure");
    assert_eq!(
        ping(&wrong).await.unwrap_err().code,
        ErrorCode::Safety,
        "Insecure URL fragment must not silently bypass hostname checks"
    );
}
