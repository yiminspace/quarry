//! `qy local connect`: expose a cloud Local on a loopback port.
//!
//! Each accepted TCP connection opens one WebSocket to the hosted server's
//! tunnel endpoint, authenticated by a ticket the owner issued from the web
//! workbench. Bytes are forwarded verbatim, so any database driver works.
use futures_util::{SinkExt, StreamExt};
use quarry_service::{Failure, Result};

fn failure(status: u16, code: &str, message: &str) -> Failure {
    Failure {
        status,
        code: code.into(),
        message: message.into(),
    }
}
use std::net::SocketAddr;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio_tungstenite::tungstenite::{Message, client::IntoClientRequest, http::HeaderValue};

/// Build the WebSocket endpoint from the hosted Quarry address the user sees.
pub fn tunnel_url(server: &str) -> Result<url::Url> {
    let mut url = url::Url::parse(server.trim())
        .map_err(|_| Failure::invalid("Server must be an http(s) URL"))?;
    let loopback = matches!(url.host_str(), Some("localhost" | "127.0.0.1" | "[::1]"));
    let scheme = match url.scheme() {
        "https" => "wss",
        "http" if loopback => "ws",
        "http" => {
            return Err(Failure::invalid(
                "Use https for a remote Quarry server; http is only allowed on localhost",
            ));
        }
        _ => return Err(Failure::invalid("Server must be an http(s) URL")),
    };
    if !url.username().is_empty() || url.password().is_some() {
        return Err(Failure::invalid("Server URL must not contain credentials"));
    }
    url.set_scheme(scheme)
        .map_err(|_| Failure::invalid("Invalid server URL"))?;
    // Accept the workbench address (…/quarry/) as well as the bare origin.
    let base = url
        .path()
        .trim_end_matches('/')
        .trim_end_matches("/quarry")
        .to_owned();
    url.set_path(&format!("{base}/api/db/local/tunnel"));
    url.set_query(None);
    url.set_fragment(None);
    Ok(url)
}

async fn open(
    url: &url::Url,
    ticket: &str,
) -> Result<
    tokio_tungstenite::WebSocketStream<tokio_tungstenite::MaybeTlsStream<tokio::net::TcpStream>>,
> {
    let mut request = url
        .as_str()
        .into_client_request()
        .map_err(|_| Failure::invalid("Invalid tunnel URL"))?;
    request.headers_mut().insert(
        "Authorization",
        HeaderValue::from_str(&format!("Bearer {ticket}"))
            .map_err(|_| Failure::invalid("Invalid tunnel ticket"))?,
    );
    let (socket, _) = tokio::time::timeout(
        std::time::Duration::from_secs(15),
        tokio_tungstenite::connect_async(request),
    )
    .await
    .map_err(|_| failure(503, "unavailable", "Quarry server did not answer"))?
    .map_err(|error| match error {
        tokio_tungstenite::tungstenite::Error::Http(response) => {
            let status = response.status().as_u16();
            let message = match status {
                401 => "Tunnel ticket expired or revoked; copy a new one from the workbench",
                403 => "Not allowed to open this cloud Local",
                404 => "Cloud Local is not enabled on this server",
                429 => "Too many open tunnel connections",
                503 => "Cloud Local is not running; start it from the workbench",
                _ => "Quarry server refused the tunnel",
            };
            failure(status, "tunnel", message)
        }
        _ => failure(503, "unavailable", "Cannot reach the Quarry server"),
    })?;
    Ok(socket)
}

async fn bridge(url: url::Url, ticket: String, mut tcp: tokio::net::TcpStream) -> Result<()> {
    let socket = open(&url, &ticket).await?;
    let _ = tcp.set_nodelay(true);
    let (mut reader, mut writer) = tcp.split();
    let (mut sink, mut stream) = socket.split();
    let upstream = async {
        let mut buffer = vec![0u8; 64 * 1024];
        loop {
            match reader.read(&mut buffer).await {
                Ok(0) | Err(_) => break,
                Ok(n) => {
                    if sink
                        .send(Message::Binary(buffer[..n].to_vec().into()))
                        .await
                        .is_err()
                    {
                        break;
                    }
                }
            }
        }
        let _ = sink.send(Message::Close(None)).await;
    };
    let downstream = async {
        while let Some(Ok(message)) = stream.next().await {
            match message {
                Message::Binary(bytes) => {
                    if writer.write_all(&bytes).await.is_err() {
                        break;
                    }
                }
                Message::Close(_) => break,
                _ => {}
            }
        }
        let _ = writer.shutdown().await;
    };
    tokio::select! {
        _ = upstream => {},
        _ = downstream => {},
    }
    Ok(())
}

pub async fn connect(server: &str, ticket: &str, port: u16) -> Result<()> {
    let _ = rustls::crypto::ring::default_provider().install_default();
    if !ticket.starts_with("qt_") || ticket.len() > 100 {
        return Err(Failure::invalid(
            "Tunnel ticket must start with qt_; copy it from the workbench",
        ));
    }
    let url = tunnel_url(server)?;
    // Fail fast on a bad ticket or stopped database before listening.
    open(&url, ticket).await?.close(None).await.ok();
    let address = SocketAddr::from(([127, 0, 0, 1], port));
    let listener = tokio::net::TcpListener::bind(address).await.map_err(|_| {
        Failure::invalid(format!(
            "Port {port} is already in use; pick another with --port"
        ))
    })?;
    eprintln!(
        "Cloud Local tunnel ready on 127.0.0.1:{} (Ctrl-C to stop)",
        listener.local_addr()?.port()
    );
    loop {
        tokio::select! {
            accepted = listener.accept() => {
                let (tcp, _) = accepted?;
                let url = url.clone();
                let ticket = ticket.to_owned();
                tokio::spawn(async move {
                    if let Err(error) = bridge(url, ticket, tcp).await {
                        eprintln!("Tunnel connection failed: {}", error.message);
                    }
                });
            }
            _ = tokio::signal::ctrl_c() => {
                eprintln!("Tunnel stopped");
                return Ok(());
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn tunnel_url_accepts_workbench_address_and_requires_tls_remotely() {
        assert_eq!(
            tunnel_url("https://q.example.com/quarry/")
                .unwrap()
                .as_str(),
            "wss://q.example.com/api/db/local/tunnel"
        );
        assert_eq!(
            tunnel_url("https://q.example.com/tools/").unwrap().as_str(),
            "wss://q.example.com/tools/api/db/local/tunnel"
        );
        assert_eq!(
            tunnel_url("http://127.0.0.1:8080").unwrap().as_str(),
            "ws://127.0.0.1:8080/api/db/local/tunnel"
        );
        assert!(tunnel_url("http://q.example.com").is_err());
        assert!(tunnel_url("https://u:p@q.example.com").is_err());
        assert!(tunnel_url("ftp://q.example.com").is_err());
    }
}
