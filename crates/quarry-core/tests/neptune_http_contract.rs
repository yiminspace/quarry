use quarry_core::*;
use serde_json::json;
use std::{
    io::{Read, Write},
    net::TcpListener,
    time::Duration,
};
fn fixture(status: &str, body: &str) -> (String, std::thread::JoinHandle<String>) {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let addr = listener.local_addr().unwrap();
    let response=format!("HTTP/1.1 {status}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\nLocation: http://127.0.0.1:9/forbidden\r\n\r\n{body}",body.len());
    let handle = std::thread::spawn(move || {
        let (mut stream, _) = listener.accept().unwrap();
        stream
            .set_read_timeout(Some(Duration::from_secs(2)))
            .unwrap();
        let mut data = vec![];
        let mut buffer = [0; 4096];
        loop {
            let n = stream.read(&mut buffer).unwrap();
            if n == 0 {
                break;
            }
            data.extend_from_slice(&buffer[..n]);
            if let Some(end) = data.windows(4).position(|x| x == b"\r\n\r\n") {
                let headers = String::from_utf8_lossy(&data[..end]);
                let length = headers
                    .lines()
                    .find_map(|line| {
                        line.split_once(':').and_then(|(name, value)| {
                            name.eq_ignore_ascii_case("content-length")
                                .then(|| value.trim().parse::<usize>().ok())
                                .flatten()
                        })
                    })
                    .unwrap_or(0);
                if data.len() >= end + 4 + length {
                    break;
                }
            }
        }
        stream.write_all(response.as_bytes()).unwrap();
        String::from_utf8(data).unwrap()
    });
    (format!("http://{addr}"), handle)
}
#[tokio::test]
async fn exact_json_values_and_real_http_request_shape() {
    let (url, server) = fixture(
        "200 OK",
        r#"{"results":[{"big":9007199254740993,"decimal":0.1234567890123456789,"name":"中文"}]}"#,
    );
    let c = ConnectionSpec::new(Engine::Neptune, url);
    let out = execute(&c, QueryRequest::new("MATCH(n) RETURN n"))
        .await
        .unwrap();
    let row = &out.rows_json()[0];
    assert_eq!(row["big"], json!("9007199254740993"));
    assert_eq!(row["decimal"], json!("0.1234567890123456789"));
    assert_eq!(row["name"], json!("中文"));
    assert!(!out.size_is_estimated);
    let request = server.join().unwrap();
    assert!(request.starts_with("POST /openCypher HTTP/1.1"));
    assert!(request.contains("query=MATCH"));
    assert!(request.contains("LIMIT+501"));
}
#[tokio::test]
async fn redirects_unknown_shapes_and_response_budgets_fail_closed() {
    let (url, server) = fixture("302 Found", "{}");
    let e = execute(
        &ConnectionSpec::new(Engine::Neptune, url),
        QueryRequest::new("RETURN 1"),
    )
    .await
    .unwrap_err();
    assert_eq!(e.code, ErrorCode::Execution);
    server.join().unwrap();
    let (url, server) = fixture("200 OK", r#"{"not_results":[]}"#);
    assert!(execute(
        &ConnectionSpec::new(Engine::Neptune, url),
        QueryRequest::new("RETURN 1")
    )
    .await
    .is_err());
    server.join().unwrap();
    let (url, server) = fixture(
        "200 OK",
        r#"{"results":[{"value":"longer than the budget"}]}"#,
    );
    let mut r = QueryRequest::new("RETURN 1");
    r.policy.max_bytes = 16;
    assert_eq!(
        execute(&ConnectionSpec::new(Engine::Neptune, url), r)
            .await
            .unwrap_err()
            .code,
        ErrorCode::Budget
    );
    server.join().unwrap();
}
