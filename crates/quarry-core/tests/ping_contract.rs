//! Probes must stay bounded even when TCP accepts but authentication never finishes.
use quarry_core::{ping, ConnectionSpec, Engine, ErrorCode};
use std::time::Duration;

#[tokio::test]
async fn protocol_ping_deadline_includes_stalled_authentication() {
    for engine in [Engine::Postgres, Engine::MySql] {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let port = listener.local_addr().unwrap().port();
        let server = tokio::spawn(async move {
            let (_socket, _) = listener.accept().await.unwrap();
            std::future::pending::<()>().await;
        });
        let url = match engine {
            Engine::Postgres => {
                format!("postgres://fixture:fixture@127.0.0.1:{port}/fixture?sslmode=disable")
            }
            Engine::MySql => {
                format!("mysql://fixture:fixture@127.0.0.1:{port}/fixture?ssl-mode=disabled")
            }
            _ => unreachable!(),
        };
        let mut connection = ConnectionSpec::new(engine, url);
        connection.connect_timeout_ms = 5000;
        connection.timeout_ms = 40;
        let error = tokio::time::timeout(Duration::from_millis(500), ping(&connection))
            .await
            .expect("ping ignored its total deadline")
            .unwrap_err();
        assert_eq!(error.code, ErrorCode::Timeout);
        server.abort();
        let _ = server.await;
        connection.timeout_ms = 0;
        assert_eq!(
            ping(&connection).await.unwrap_err().code,
            ErrorCode::InvalidInput
        );
    }
}
