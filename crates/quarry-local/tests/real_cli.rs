//! Opt-in process acceptance against the built application. All state is disposable.
use serde_json::{Value, json};
use std::{
    path::{Path, PathBuf},
    process::Command,
};
struct Cli {
    binary: PathBuf,
    config: PathBuf,
    workspace: PathBuf,
}
impl Cli {
    fn run(&self, args: &[&str]) -> Value {
        let output = Command::new(&self.binary)
            .arg("--config")
            .arg(&self.config)
            .arg("--workspace")
            .arg(&self.workspace)
            .args(args)
            .output()
            .expect("start isolated qy");
        assert!(
            output.status.success(),
            "isolated qy {args:?} failed: {}",
            String::from_utf8_lossy(&output.stderr)
        );
        serde_json::from_slice(&output.stdout).expect("JSON output")
    }
}
impl Drop for Cli {
    fn drop(&mut self) {
        for args in [vec!["down"], vec!["local", "down", "--engine", "neptune"]] {
            let _ = Command::new(&self.binary)
                .arg("--config")
                .arg(&self.config)
                .arg("--workspace")
                .arg(&self.workspace)
                .args(args)
                .output();
        }
    }
}
#[test]
fn durable_keeper_and_neptune_use_the_real_cli_entrypoints() {
    let Some(binary) = std::env::var_os("QUARRY_TEST_BINARY") else {
        eprintln!("SKIPPED: QUARRY_TEST_BINARY must name a freshly built qy");
        return;
    };
    let binary = Path::new(&binary).canonicalize().expect("binary path");
    let root = tempfile::tempdir().unwrap();
    let workspace = root.path().join("ws");
    std::fs::create_dir_all(&workspace).unwrap();
    let config = root.path().join("config.toml");
    std::fs::write(
        &config,
        format!(
            "workspaces = [{}]\n",
            serde_json::to_string(workspace.to_str().unwrap()).unwrap()
        ),
    )
    .unwrap();
    let cli = Cli {
        binary,
        config,
        workspace,
    };
    let first = cli.run(&["up"]);
    let second = cli.run(&["up"]);
    assert_eq!(first["keeper"]["running"], true);
    assert_eq!(first["keeper"]["pid"], second["keeper"]["pid"]);
    assert_eq!(cli.run(&["status"])["keeper"]["running"], true);
    assert_eq!(cli.run(&["down"])["keeper"]["running"], false);
    let socket = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let port = socket.local_addr().unwrap().port().to_string();
    drop(socket);
    let fixture = root.path().join("fixture.json");
    std::fs::write(&fixture,serde_json::to_vec(&json!({"responses":[{"query_contains":"RETURN 9 AS n","query_exact":true,"parameters":{},"parameters_exact":true,"results":[{"n":9}]}]})).unwrap()).unwrap();
    let fixture = fixture.to_str().unwrap();
    assert_eq!(
        cli.run(&[
            "local",
            "up",
            "--engine",
            "neptune",
            "--port",
            &port,
            "--fixture",
            fixture
        ])["state"],
        "created"
    );
    assert_eq!(
        cli.run(&[
            "local",
            "up",
            "--engine",
            "neptune",
            "--port",
            &port,
            "--fixture",
            fixture
        ])["state"],
        "running"
    );
    let status = cli.run(&["local", "status", "--engine", "neptune"]);
    assert_eq!(status["running"], true);
    assert_eq!(status["backend"], "mock");
    assert_eq!(
        cli.run(&["local", "down", "--engine", "neptune"])["stopped"],
        true
    );
    assert_eq!(
        cli.run(&["local", "status", "--engine", "neptune"])["running"],
        false
    );
}
