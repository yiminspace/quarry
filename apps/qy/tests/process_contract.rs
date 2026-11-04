//! Native process tests: these never import or launch the Python implementation.
use serde_json::{Value, json};
use std::{
    fs,
    io::{BufRead, BufReader, Write},
    process::{Command, Stdio},
};
fn fixture() -> tempfile::TempDir {
    let dir = tempfile::tempdir().unwrap();
    fs::write(dir.path().join("config.toml"), "").unwrap();
    fs::write(
        dir.path().join("connections.toml"),
        "[notes]\nurl='sqlite:///notes.db'\nengine='sqlite'\n",
    )
    .unwrap();
    let db = rusqlite::Connection::open(dir.path().join("notes.db")).unwrap();
    db.execute_batch("CREATE TABLE entries(id INTEGER PRIMARY KEY, note TEXT, payload BLOB); INSERT INTO entries VALUES(1,'before',X'00FF');").unwrap();
    dir
}
fn cli(dir: &tempfile::TempDir) -> Command {
    let mut command = Command::new(env!("CARGO_BIN_EXE_qy"));
    command
        .arg("--config")
        .arg(dir.path().join("config.toml"))
        .arg("--workspace")
        .arg(dir.path())
        .env("QUARRY_AI_HOME", dir.path().join("assistant"))
        .env("QUARRY_UPDATE_CHECK", "0");
    command
}
#[test]
fn native_sqlite_preserves_values_and_enforces_write_policy() {
    let dir = fixture();
    let out = cli(&dir)
        .args([
            "exec",
            "notes",
            "--sql",
            "SELECT 9223372036854775807 AS id, payload FROM entries",
        ])
        .output()
        .unwrap();
    assert!(
        out.status.success(),
        "{}",
        String::from_utf8_lossy(&out.stderr)
    );
    let rows: Value = serde_json::from_slice(&out.stdout).unwrap();
    assert_eq!(rows[0]["id"], "9223372036854775807");
    assert_eq!(rows[0]["payload"], json!({"$binary":"AP8="}));
    let blocked = cli(&dir)
        .args(["exec", "notes", "--sql", "UPDATE entries SET note='bad'"])
        .output()
        .unwrap();
    assert!(!blocked.status.success());
    assert_eq!(
        rusqlite::Connection::open(dir.path().join("notes.db"))
            .unwrap()
            .query_row("SELECT note FROM entries", [], |r| r.get::<_, String>(0))
            .unwrap(),
        "before"
    );
    let allowed = cli(&dir)
        .args([
            "exec",
            "notes",
            "--sql",
            "UPDATE entries SET note='after'",
            "--write",
        ])
        .output()
        .unwrap();
    assert!(
        allowed.status.success(),
        "{}",
        String::from_utf8_lossy(&allowed.stderr)
    );
}
#[test]
fn native_mcp_uses_the_same_database_and_keeps_stdout_protocol_only() {
    let dir = fixture();
    let mut process = cli(&dir)
        .args(["mcp"])
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    let mut input = process.stdin.take().unwrap();
    let mut output = BufReader::new(process.stdout.take().unwrap());
    for (id, method, params) in [
        (1, "initialize", json!({})),
        (2, "tools/list", json!({})),
        (
            3,
            "tools/call",
            json!({"name":"exec_sql","arguments":{"db":"notes","sql":"SELECT note FROM entries"}}),
        ),
        (
            4,
            "tools/call",
            json!({"name":"exec_sql","arguments":{"db":"notes","sql":"DELETE FROM entries","write":true}}),
        ),
    ] {
        writeln!(
            input,
            "{}",
            json!({"jsonrpc":"2.0","id":id,"method":method,"params":params})
        )
        .unwrap();
        input.flush().unwrap();
        let mut line = String::new();
        output.read_line(&mut line).unwrap();
        let value: Value = serde_json::from_str(&line).unwrap();
        assert_eq!(value["id"], id);
        match id {
            2 => assert_eq!(value["result"]["tools"].as_array().unwrap().len(), 9),
            3 => assert_eq!(
                value["result"]["structuredContent"]["rows"][0]["note"],
                "before"
            ),
            4 => assert_eq!(value["result"]["isError"], true),
            _ => {}
        }
    }
    drop(input);
    assert!(process.wait().unwrap().success());
}
#[test]
fn native_query_files_remain_explicit_assets() {
    let dir = fixture();
    let saved = cli(&dir)
        .args([
            "save",
            "recent",
            "--db",
            "notes",
            "--sql",
            "SELECT note FROM entries",
            "--no-validate",
        ])
        .output()
        .unwrap();
    assert!(
        saved.status.success(),
        "{}",
        String::from_utf8_lossy(&saved.stderr)
    );
    let result = cli(&dir).args(["run", "recent"]).output().unwrap();
    assert!(
        result.status.success(),
        "{}",
        String::from_utf8_lossy(&result.stderr)
    );
    assert_eq!(
        serde_json::from_slice::<Value>(&result.stdout).unwrap()[0]["note"],
        "before"
    );
    assert!(dir.path().join("queries/notes/recent.sql").is_file());
    let duplicate = cli(&dir)
        .args([
            "save",
            "recent",
            "--db",
            "notes",
            "--sql",
            "SELECT 2",
            "--no-validate",
        ])
        .output()
        .unwrap();
    assert!(!duplicate.status.success());
}

#[cfg(unix)]
#[test]
fn native_gui_shutdown_closes_an_open_sse_connection() {
    use std::{
        io::Read,
        net::{TcpListener, TcpStream},
        time::{Duration, Instant},
    };
    struct ChildGuard(std::process::Child);
    impl Drop for ChildGuard {
        fn drop(&mut self) {
            let _ = self.0.kill();
            let _ = self.0.wait();
        }
    }
    let dir = fixture();
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let port = listener.local_addr().unwrap().port();
    drop(listener);
    let child = cli(&dir)
        .args(["gui", "--no-browser", "--port", &port.to_string()])
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .unwrap();
    let mut child = ChildGuard(child);
    let start = Instant::now();
    let mut stream = loop {
        match TcpStream::connect(("127.0.0.1", port)) {
            Ok(stream) => break stream,
            Err(_) if start.elapsed() < Duration::from_secs(5) => {
                std::thread::sleep(Duration::from_millis(20))
            }
            Err(error) => panic!("native server did not start: {error}"),
        }
    };
    stream
        .set_read_timeout(Some(Duration::from_secs(3)))
        .unwrap();
    write!(
        stream,
        "GET /api/events HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n\r\n"
    )
    .unwrap();
    let mut bytes = [0u8; 1024];
    let n = stream.read(&mut bytes).unwrap();
    assert!(String::from_utf8_lossy(&bytes[..n]).contains("200 OK"));
    assert!(
        Command::new("kill")
            .arg("-INT")
            .arg(child.0.id().to_string())
            .status()
            .unwrap()
            .success()
    );
    let start = Instant::now();
    loop {
        if let Some(status) = child.0.try_wait().unwrap() {
            assert!(status.success());
            break;
        }
        assert!(
            start.elapsed() < Duration::from_secs(2),
            "an open SSE connection must not block graceful shutdown"
        );
        std::thread::sleep(Duration::from_millis(10));
    }
}

#[test]
fn native_mcp_saved_query_can_use_a_related_local_workspace() {
    let dir = fixture();
    let local = dir.path().join("local");
    fs::create_dir_all(&local).unwrap();
    let db = rusqlite::Connection::open(local.join("local.db")).unwrap();
    db.execute_batch("CREATE TABLE entries(note TEXT);INSERT INTO entries VALUES('local-copy')")
        .unwrap();
    fs::write(
        local.join("connections.toml"),
        "[notes_local]\ndb='notes'\nenv='local'\ngroup='Notebook'\nurl='sqlite:///local.db'\n",
    )
    .unwrap();
    assert!(
        cli(&dir)
            .args([
                "save",
                "recent",
                "--db",
                "notes",
                "--sql",
                "SELECT note FROM entries",
                "--no-validate"
            ])
            .output()
            .unwrap()
            .status
            .success()
    );
    let mut process = cli(&dir)
        .arg("--workspace")
        .arg(&local)
        .arg("mcp")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .spawn()
        .unwrap();
    let mut input = process.stdin.take().unwrap();
    writeln!(input,"{}",json!({"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"run_saved_query","arguments":{"name":"recent","env":"local","workspace":dir.path()}}})).unwrap();
    drop(input);
    let output = process.wait_with_output().unwrap();
    assert!(output.status.success());
    let response: Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(response["result"]["isError"], false, "{response}");
    assert_eq!(
        response["result"]["structuredContent"]["rows"][0]["note"],
        "local-copy"
    );
}
