//! Exercises the standalone GUI lifecycle with real processes and loopback sockets.
#![cfg(unix)]

use std::{
    fs,
    io::{Read, Write},
    net::{SocketAddr, TcpListener, TcpStream},
    os::unix::fs::PermissionsExt,
    path::Path,
    process::{Child, Command, Stdio},
    sync::mpsc,
    thread,
    time::{Duration, Instant},
};

fn wait_for<T>(description: &str, mut check: impl FnMut() -> Option<T>) -> T {
    let deadline = Instant::now() + Duration::from_secs(5);
    loop {
        if let Some(value) = check() {
            return value;
        }
        assert!(
            Instant::now() < deadline,
            "timed out waiting for {description}"
        );
        thread::sleep(Duration::from_millis(10));
    }
}

fn fixture() -> tempfile::TempDir {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    for config in ["config.toml", "other.toml"] {
        fs::write(root.join(config), "workspaces = ['workspace-a']\n").unwrap();
    }
    for workspace in ["workspace-a", "workspace-b"] {
        fs::create_dir(root.join(workspace)).unwrap();
    }
    for program in ["open", "xdg-open"] {
        let opener = root.join(program);
        fs::write(
            &opener,
            "#!/bin/sh\nprintf '%s\\n' \"$1\" >> \"$QUARRY_TEST_OPEN_LOG\"\n",
        )
        .unwrap();
        fs::set_permissions(opener, fs::Permissions::from_mode(0o700)).unwrap();
    }
    dir
}

struct GuiProcess {
    child: Child,
    stderr: tempfile::NamedTempFile,
}

fn qy(root: &Path, config: &str, workspace: Option<&str>) -> Command {
    let mut command = Command::new(env!("CARGO_BIN_EXE_qy"));
    command
        .current_dir(root)
        .env("HOME", root)
        .env("PATH", format!("{}:/usr/bin:/bin", root.display()))
        .env("QUARRY_AI_HOME", root.join("assistant"))
        .env("QUARRY_UPDATE_CHECK", "0")
        .env("QUARRY_AUTO_UPDATE", "0")
        .env("QUARRY_TEST_OPEN_LOG", root.join("browser.log"))
        .env("TOKIO_WORKER_THREADS", "2")
        .args(["--config", config]);
    if let Some(workspace) = workspace {
        command.args(["--workspace", workspace]);
    }
    command
}

impl GuiProcess {
    fn start(
        root: &Path,
        config: &str,
        workspace: Option<&str>,
        host: &str,
        port: u16,
        open: bool,
    ) -> Self {
        let stderr = tempfile::NamedTempFile::new_in(root).unwrap();
        let mut command = qy(root, config, workspace);
        command
            .args(["gui", "--host", host, "--port"])
            .arg(port.to_string())
            .stdout(Stdio::null())
            .stderr(stderr.reopen().unwrap());
        if !open {
            command.arg("--no-browser");
        }
        let child = command.spawn().unwrap();
        Self { child, stderr }
    }

    fn output(&self) -> String {
        String::from_utf8_lossy(&fs::read(self.stderr.path()).unwrap()).into_owned()
    }

    fn address(&mut self) -> SocketAddr {
        wait_for("the GUI launch URL", || {
            let exited = self.child.try_wait().unwrap().is_some();
            let output = self.output();
            let address = output.lines().find_map(|line| {
                line.strip_prefix("Quarry GUI → http://")?
                    .strip_suffix("/quarry/")?
                    .parse()
                    .ok()
            });
            if address.is_none() {
                assert!(!exited, "{output}");
            }
            address
        })
    }

    fn exited_successfully(&mut self) {
        let status = wait_for("the GUI command to exit", || self.child.try_wait().unwrap());
        assert!(status.success(), "{}", self.output());
    }

    fn still_serving(&mut self, address: SocketAddr) {
        wait_for("the original GUI to serve HTTP", || {
            assert!(
                self.child.try_wait().unwrap().is_none(),
                "{}",
                self.output()
            );
            version_response(address)
        });
    }
}

fn http_response(address: SocketAddr, request: &str, headers: &str) -> Option<String> {
    let timeout = Duration::from_millis(200);
    let mut stream = TcpStream::connect_timeout(&address, timeout).ok()?;
    stream.set_read_timeout(Some(timeout)).unwrap();
    stream.set_write_timeout(Some(timeout)).unwrap();
    write!(
        stream,
        "{request} HTTP/1.1\r\nHost: {address}\r\nConnection: close\r\n{headers}\r\n"
    )
    .ok()?;
    let mut response = String::new();
    stream.read_to_string(&mut response).ok()?;
    Some(response)
}

fn version_response(address: SocketAddr) -> Option<()> {
    let response = http_response(address, "GET /api/version", "")?;
    (response.starts_with("HTTP/1.1 200 OK") && response.contains("Quarry")).then_some(())
}

fn stop_gui(root: &Path, config: &str, workspace: Option<&str>, host: &str, port: u16) -> String {
    let output = qy(root, config, workspace)
        .args(["gui", "stop", "--host", host, "--port"])
        .arg(port.to_string())
        .output()
        .unwrap();
    assert!(output.status.success(), "{:?}", output);
    String::from_utf8(output.stdout).unwrap()
}

impl Drop for GuiProcess {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

#[test]
fn native_gui_reuses_matching_workbench_and_opens_its_url() {
    let dir = fixture();
    let root = dir.path();
    let mut original = GuiProcess::start(root, "config.toml", None, "localhost", 0, false);
    let address = original.address();
    let preferred_ip = TcpListener::bind(("localhost", 0))
        .unwrap()
        .local_addr()
        .unwrap()
        .ip();
    assert_eq!(address.ip(), preferred_ip);
    original.still_serving(address);

    let mut reentry = GuiProcess::start(
        root,
        "config.toml",
        None,
        "localhost",
        address.port(),
        false,
    );
    assert_eq!(reentry.address(), address);
    reentry.exited_successfully();
    assert!(!root.join("browser.log").exists());

    fs::write(
        root.join("config.toml"),
        "workspaces = ['workspace-a', 'workspace-b']\n",
    )
    .unwrap();
    let mut reentry = GuiProcess::start(
        root,
        "config.toml",
        None,
        &address.ip().to_string(),
        address.port(),
        true,
    );
    assert_eq!(reentry.address(), address);
    reentry.exited_successfully();
    let opened = wait_for("the browser opener", || {
        fs::read_to_string(root.join("browser.log"))
            .ok()
            .filter(|log| log.ends_with('\n'))
    });
    assert_eq!(opened, format!("http://{address}/quarry/\n"));
    original.still_serving(address);

    // Discovery is available over HTTP, so its proof cannot authorize a stop.
    let challenge = format!("x-quarry-launch-challenge: {}\r\n", uuid::Uuid::new_v4());
    let response = http_response(address, "GET /api/version", &challenge).unwrap();
    let proof = response
        .lines()
        .find_map(|line| line.strip_prefix("x-quarry-launch-proof: "))
        .unwrap();
    let headers = format!("{challenge}x-quarry-stop-proof: {proof}\r\n");
    let response = http_response(address, "POST /api/gui/stop", &headers).unwrap();
    assert!(response.starts_with("HTTP/1.1 403 Forbidden"));
    original.still_serving(address);

    let message = stop_gui(root, "config.toml", None, "localhost", address.port());
    assert!(message.contains(&format!("http://{address}/quarry/")));
    original.exited_successfully();
    assert!(version_response(address).is_none());
    assert!(
        stop_gui(root, "config.toml", None, "localhost", address.port())
            .contains("No matching Quarry GUI")
    );
}

#[test]
fn native_gui_keeps_foreign_and_different_workbenches_alive() {
    let dir = fixture();
    let root = dir.path();
    let foreign = (0..10)
        .map(|_| TcpListener::bind("127.0.0.1:0").unwrap())
        .find(|listener| listener.local_addr().unwrap().port() < u16::MAX - 30)
        .expect("an ephemeral port with room above it");
    let occupied = foreign.local_addr().unwrap();
    // A server with only the legacy version response is not enough to prove
    // that it is the matching standalone workbench.
    foreign.set_nonblocking(true).unwrap();
    let (stop, shutdown) = mpsc::channel::<()>();
    let foreign = thread::spawn(move || {
        let body = format!(
            r#"{{"name":"Quarry","version":"{}"}}"#,
            env!("CARGO_PKG_VERSION")
        );
        while shutdown.try_recv() == Err(mpsc::TryRecvError::Empty) {
            if let Ok((mut stream, _)) = foreign.accept() {
                let timeout = Some(Duration::from_millis(200));
                stream.set_read_timeout(timeout).unwrap();
                stream.set_write_timeout(timeout).unwrap();
                let _ = stream.read(&mut [0; 1024]);
                let _ = write!(
                    stream,
                    "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
                    body.len()
                );
            }
            thread::sleep(Duration::from_millis(5));
        }
    });
    assert!(version_response(occupied).is_some());

    let mut original = GuiProcess::start(
        root,
        "config.toml",
        Some("workspace-a"),
        "127.0.0.1",
        occupied.port(),
        false,
    );
    let original_address = original.address();
    assert_ne!(original_address, occupied);
    original.still_serving(original_address);

    let mut reentry = GuiProcess::start(
        root,
        "config.toml",
        Some("workspace-a"),
        "127.0.0.1",
        occupied.port(),
        false,
    );
    assert_eq!(reentry.address(), original_address);
    reentry.exited_successfully();

    for (config, workspace) in [
        ("config.toml", Some("workspace-b")),
        ("other.toml", Some("workspace-a")),
        ("config.toml", None),
    ] {
        let mut different = GuiProcess::start(
            root,
            config,
            workspace,
            "127.0.0.1",
            original_address.port(),
            false,
        );
        let address = different.address();
        assert_ne!(address, original_address);
        assert_ne!(address, occupied);
        different.still_serving(address);
        original.still_serving(original_address);
    }
    assert!(
        stop_gui(root, "other.toml", None, "127.0.0.1", occupied.port())
            .contains("No matching Quarry GUI")
    );
    assert!(version_response(occupied).is_some());
    drop(stop);
    foreign.join().unwrap();

    // A repeated launch keeps the same browser origin after the blocker exits.
    drop(TcpListener::bind(occupied).unwrap());
    let mut reentry = GuiProcess::start(
        root,
        "config.toml",
        Some("workspace-a"),
        "127.0.0.1",
        occupied.port(),
        false,
    );
    assert_eq!(reentry.address(), original_address);
    reentry.exited_successfully();
    original.still_serving(original_address);

    let mut different = GuiProcess::start(
        root,
        "config.toml",
        Some("workspace-b"),
        "127.0.0.1",
        occupied.port(),
        false,
    );
    let different_address = different.address();
    assert_eq!(different_address, occupied);
    let message = stop_gui(
        root,
        "config.toml",
        Some("workspace-a"),
        "127.0.0.1",
        occupied.port(),
    );
    assert!(message.contains(&format!("http://{original_address}/quarry/")));
    original.exited_successfully();
    different.still_serving(different_address);
}

#[test]
fn simultaneous_gui_launches_share_the_same_workbench() {
    let dir = fixture();
    let root = dir.path();
    let reservation = TcpListener::bind("127.0.0.1:0").unwrap();
    let address = reservation.local_addr().unwrap();
    drop(reservation);
    let mut first = GuiProcess::start(
        root,
        "config.toml",
        Some("workspace-a"),
        "127.0.0.1",
        address.port(),
        false,
    );
    let mut second = GuiProcess::start(
        root,
        "config.toml",
        Some("workspace-a"),
        "127.0.0.1",
        address.port(),
        false,
    );
    assert_eq!(first.address(), address);
    assert_eq!(second.address(), address);
    let first_survived = wait_for("one GUI launch to reuse the other", || {
        match (
            first.child.try_wait().unwrap(),
            second.child.try_wait().unwrap(),
        ) {
            (Some(status), None) => {
                assert!(status.success(), "{}", first.output());
                Some(false)
            }
            (None, Some(status)) => {
                assert!(status.success(), "{}", second.output());
                Some(true)
            }
            (None, None) => None,
            (Some(_), Some(_)) => panic!("neither GUI process survived"),
        }
    });
    if first_survived {
        &mut first
    } else {
        &mut second
    }
    .still_serving(address);
}
