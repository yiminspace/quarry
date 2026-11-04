use serde_json::Value;
use std::{
    io::Write,
    path::Path,
    process::{Command, Output, Stdio},
};

fn run(root: &Path, workspaces: &[&Path], args: &[&str]) -> Output {
    let mut command = Command::new(env!("CARGO_BIN_EXE_qy"));
    command
        .env_clear()
        .current_dir(root)
        .arg("--config")
        .arg(root.join("config.toml"));
    for workspace in workspaces {
        command.arg("--workspace").arg(workspace);
    }
    command.args(args).output().unwrap()
}
fn workspace(root: &Path, name: &str) -> std::path::PathBuf {
    let path = root.join(name);
    std::fs::create_dir_all(path.join("queries/notes")).unwrap();
    std::fs::write(
        path.join("connections.toml"),
        "[notes]\nurl='sqlite:///notes.db'\n",
    )
    .unwrap();
    std::fs::write(path.join("notes.db"), b"").unwrap();
    path
}
fn success(output: Output) -> Value {
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    serde_json::from_slice(&output.stdout).unwrap()
}
#[test]
fn explicit_configuration_without_home_and_stdin_secret_stay_private() {
    let dir = tempfile::tempdir().unwrap();
    let missing = run(
        dir.path(),
        &[],
        &["ai", "sql", "write something", "--db", "missing"],
    );
    assert!(!missing.status.success());
    assert!(!dir.path().join("assistant/history.sqlite3").exists());
    let mut child = Command::new(env!("CARGO_BIN_EXE_qy"))
        .env_clear()
        .current_dir(dir.path())
        .arg("--config")
        .arg(dir.path().join("config.toml"))
        .args([
            "ai",
            "configure",
            "--base-url",
            "http://127.0.0.1:1/v1",
            "--model",
            "fixture",
            "--api-key-stdin",
        ])
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    child
        .stdin
        .take()
        .unwrap()
        .write_all(b"private-fixture-key\n")
        .unwrap();
    let output = child.wait_with_output().unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert!(!String::from_utf8_lossy(&output.stdout).contains("private-fixture-key"));
    assert!(!String::from_utf8_lossy(&output.stderr).contains("private-fixture-key"));
    let show = success(run(dir.path(), &[], &["ai", "show"]));
    assert_eq!(show["configured"], true);
    assert!(!show.to_string().contains("private-fixture-key"));
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        assert_eq!(
            std::fs::metadata(dir.path().join("assistant-credentials.json"))
                .unwrap()
                .permissions()
                .mode()
                & 0o777,
            0o600
        );
    }
}
#[test]
fn run_scopes_saved_query_and_overrides_or_removes_outer_limit() {
    let dir = tempfile::tempdir().unwrap();
    let first = workspace(dir.path(), "one");
    let second = workspace(dir.path(), "two");
    for (ws, label) in [(&first, "first"), (&second, "second")] {
        success(run(
            dir.path(),
            &[ws],
            &[
                "exec",
                "notes",
                "--write",
                "--yes",
                "--sql",
                "CREATE TABLE marker (label TEXT)",
            ],
        ));
        success(run(
            dir.path(),
            &[ws],
            &[
                "exec",
                "notes",
                "--write",
                "--yes",
                "--sql",
                &format!("INSERT INTO marker VALUES ('{label}'),('another')"),
            ],
        ));
    }
    std::fs::write(
        second.join("queries/notes/in_second.sql"),
        "-- @name: in_second\n-- @db: notes\n\nSELECT label FROM marker ORDER BY label LIMIT 1\n",
    )
    .unwrap();
    let normal = success(run(dir.path(), &[&first, &second], &["run", "in_second"]));
    assert_eq!(normal.as_array().unwrap().len(), 1);
    let full = success(run(
        dir.path(),
        &[&first, &second],
        &["run", "in_second", "--full", "--strict"],
    ));
    assert_eq!(full.as_array().unwrap().len(), 2);
    assert!(
        full.as_array()
            .unwrap()
            .iter()
            .any(|row| row["label"] == "second")
    );
    assert!(!full.to_string().contains("first"));
    let limited = success(run(
        dir.path(),
        &[&first, &second],
        &["run", "in_second", "--limit", "2"],
    ));
    assert_eq!(limited.as_array().unwrap().len(), 2);
}

#[test]
fn cli_exit_categories_preserve_connection_sql_and_safety_distinctions() {
    let dir = tempfile::tempdir().unwrap();
    let ws = workspace(dir.path(), "workspace");
    let connection = run(dir.path(), &[&ws], &["exec", "notes", "--sql", "SELECT 1"]);
    assert!(connection.status.success());
    let safety = run(
        dir.path(),
        &[&ws],
        &["exec", "notes", "--sql", "DELETE FROM marker"],
    );
    assert_eq!(safety.status.code(), Some(8));
    let sql = run(
        dir.path(),
        &[&ws],
        &["exec", "notes", "--sql", "SELECT * FROM no_such_table"],
    );
    assert_eq!(sql.status.code(), Some(3));
    std::fs::remove_file(ws.join("notes.db")).unwrap();
    let unavailable = run(dir.path(), &[&ws], &["exec", "notes", "--sql", "SELECT 1"]);
    assert_eq!(unavailable.status.code(), Some(2));
}
