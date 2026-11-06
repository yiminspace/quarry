#![cfg(any(target_os = "macos", target_os = "linux"))]

use sha2::{Digest, Sha256};
use std::{
    fs,
    os::unix::fs::{PermissionsExt, symlink},
    path::{Path, PathBuf},
    process::{Command, Output},
    thread,
    time::{Duration, Instant},
};

const QY: &str = env!("CARGO_BIN_EXE_qy");
const VERSION: &str = env!("CARGO_PKG_VERSION");
const CHECK: &str = "--internal-quarry-update-check";

struct Fixture {
    root: tempfile::TempDir,
    latest: String,
}

impl Fixture {
    fn new() -> Self {
        let root = tempfile::tempdir().unwrap();
        let mut latest = semver::Version::parse(VERSION).unwrap();
        latest.patch += 1;
        latest.pre = semver::Prerelease::EMPTY;
        let fixture = Self {
            root,
            latest: latest.to_string(),
        };
        fs::create_dir(fixture.path("tools")).unwrap();
        fs::write(fixture.path("config.toml"), "").unwrap();
        executable(
            &fixture.path("tools/curl"),
            r#"#!/bin/sh
for arg do
  case "$arg" in
    https://api.github.com/repos/yiminspace/quarry/releases/latest)
      echo latest >> "$FIXTURE/curl.log"
      printf '{"tag_name":"v%s"}\n' "$LATEST_FIXTURE"
      exit 0
      ;;
    https://github.com/yiminspace/quarry/releases/download/v*)
      asset=${arg##*/}
      ;;
    -o)
      need_output=1
      ;;
    *)
      if [ "${need_output:-0}" = 1 ]; then
        printf '%s\n' "$asset" >> "$FIXTURE/curl.log"
        cp "$FIXTURE/$asset" "$arg"
        exit $?
      fi
      ;;
  esac
done
exit 2
"#,
        );
        fixture
    }

    fn path(&self, path: impl AsRef<Path>) -> PathBuf {
        self.root.path().join(path)
    }

    fn command(&self, executable: impl AsRef<Path>) -> Command {
        let mut command = Command::new(executable.as_ref());
        command
            .env_clear()
            .env(
                "PATH",
                format!("{}:/usr/bin:/bin", self.path("tools").display()),
            )
            .env("HOME", self.root.path())
            .env("QUARRY_CONFIG", self.path("config.toml"))
            .env("FIXTURE", self.root.path())
            .env("LATEST_FIXTURE", &self.latest)
            .current_dir(self.root.path());
        command
    }

    fn managed(&self) -> PathBuf {
        let target = format!("releases/{VERSION}/qy");
        let binary = self.path(format!("managed dir/.quarry/{target}"));
        fs::create_dir_all(binary.parent().unwrap()).unwrap();
        if fs::hard_link(QY, &binary).is_err() {
            fs::copy(QY, &binary).unwrap();
        }
        symlink(target, self.path("managed dir/.quarry/current")).unwrap();
        let launcher = self.path("managed dir/qy");
        symlink(".quarry/current", &launcher).unwrap();
        launcher
    }

    fn archive(&self, version: &str, valid_checksum: bool) {
        let architecture = if cfg!(target_arch = "aarch64") {
            "arm64"
        } else {
            "x86_64"
        };
        let os = std::env::consts::OS;
        let asset = format!("quarry-{version}-{os}-{architecture}.tar.gz");
        let binary = format!(
            r#"#!/bin/sh
if [ "$1" = "--version" ]; then
  echo 'qy {version}'
  exit 0
fi
printf '%s\n' "$@"
exit 37
"#
        );
        let contents = tempfile::tempdir_in(self.root.path()).unwrap();
        executable(&contents.path().join("qy"), &binary);
        assert!(
            Command::new("tar")
                .arg("-czf")
                .arg(self.path(&asset))
                .arg("-C")
                .arg(contents.path())
                .arg("qy")
                .status()
                .unwrap()
                .success()
        );
        let digest = if valid_checksum {
            Sha256::digest(fs::read(self.path(&asset)).unwrap()).into()
        } else {
            [0u8; 32]
        };
        let digest = digest
            .iter()
            .map(|byte| format!("{byte:02x}"))
            .collect::<String>();
        fs::write(self.path("SHA256SUMS"), format!("{digest}  {asset}\n")).unwrap();
    }

    fn pointer(&self, name: &str) -> PathBuf {
        fs::read_link(self.path(format!("managed dir/.quarry/{name}"))).unwrap()
    }
}

fn executable(path: &Path, content: &str) {
    fs::write(path, content).unwrap();
    fs::set_permissions(path, fs::Permissions::from_mode(0o755)).unwrap();
}

fn success(output: Output) -> String {
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    String::from_utf8(output.stdout).unwrap()
}

#[test]
fn verifies_stages_throttles_and_restarts_with_original_arguments() {
    let fixture = Fixture::new();
    let launcher = fixture.managed();
    fixture.archive(&fixture.latest, true);
    for flag in ["--help", "--version"] {
        success(fixture.command(&launcher).arg(flag).output().unwrap());
    }
    assert!(!fixture.path("curl.log").exists());

    success(fixture.command(&launcher).arg("list").output().unwrap());
    let pending = fixture.path("managed dir/.quarry/pending");
    let deadline = Instant::now() + Duration::from_secs(10);
    while !pending.is_symlink() {
        assert!(
            Instant::now() < deadline,
            "update worker did not stage a release"
        );
        thread::sleep(Duration::from_millis(20));
    }
    assert_eq!(
        fixture.pointer("current"),
        Path::new(&format!("releases/{VERSION}/qy"))
    );
    success(fixture.command(&launcher).arg(CHECK).output().unwrap());
    assert_eq!(
        fs::read_to_string(fixture.path("curl.log"))
            .unwrap()
            .lines()
            .count(),
        3
    );

    success(
        fixture
            .command(&launcher)
            .env("QUARRY_AUTO_UPDATE", "0")
            .arg("list")
            .output()
            .unwrap(),
    );
    assert!(pending.is_symlink());

    let args = ["list", "--db", "notes with spaces", "--format", "json"];
    let deadline = Instant::now() + Duration::from_secs(10);
    let mut result = fixture.command(&launcher).args(args).output().unwrap();
    // The worker writes the pending pointer just before it drops the lock.
    while result.status.success() && pending.is_symlink() {
        assert!(Instant::now() < deadline, "staged release did not activate");
        thread::sleep(Duration::from_millis(20));
        result = fixture.command(&launcher).args(args).output().unwrap();
    }
    assert_eq!(result.status.code(), Some(37));
    assert_eq!(
        String::from_utf8(result.stdout).unwrap(),
        args.join("\n") + "\n"
    );
    assert_eq!(
        fixture.pointer("current"),
        Path::new(&format!("releases/{}/qy", fixture.latest))
    );
    assert!(!pending.is_symlink());
}

#[test]
fn explicit_update_works_when_disabled_and_refuses_bad_checksums() {
    let fixture = Fixture::new();
    let launcher = fixture.managed();
    fixture.archive(&fixture.latest, false);
    let update = || {
        fixture
            .command(&launcher)
            .env("QUARRY_AUTO_UPDATE", "0")
            .arg("update")
            .output()
            .unwrap()
    };
    let failure = update();
    assert!(!failure.status.success());
    assert!(String::from_utf8_lossy(&failure.stderr).contains("Checksum mismatch"));
    assert_eq!(
        fixture.pointer("current"),
        Path::new(&format!("releases/{VERSION}/qy"))
    );

    fixture.archive(&fixture.latest, true);
    assert!(success(update()).contains(&format!("Updated Quarry to {}", fixture.latest)));
    assert_eq!(
        success(
            fixture
                .command(&launcher)
                .arg("--version")
                .output()
                .unwrap()
        )
        .trim(),
        format!("qy {}", fixture.latest)
    );
}

#[test]
fn update_never_installs_prereleases_or_downgrades() {
    let fixture = Fixture::new();
    let launcher = fixture.managed();
    for tag in [VERSION.to_owned(), format!("{}-rc.1", fixture.latest)] {
        let output = fixture
            .command(&launcher)
            .env("LATEST_FIXTURE", tag)
            .arg("update")
            .output()
            .unwrap();
        assert!(success(output).contains("is up to date"));
    }
    assert_eq!(
        fixture.pointer("current"),
        Path::new(&format!("releases/{VERSION}/qy"))
    );
    assert_eq!(
        fs::read_to_string(fixture.path("curl.log")).unwrap(),
        "latest\nlatest\n"
    );
}

#[test]
fn exec_failure_restores_previous_launcher() {
    let fixture = Fixture::new();
    let launcher = fixture.managed();
    let target = format!("releases/{}/qy", fixture.latest);
    let invalid = fixture.path(format!("managed dir/.quarry/{target}"));
    fs::create_dir_all(invalid.parent().unwrap()).unwrap();
    executable(&invalid, "#!/quarry-nonexistent-interpreter\n");
    let pending = fixture.path("managed dir/.quarry/pending");
    symlink(target, &pending).unwrap();
    fs::write(fixture.path("managed dir/.quarry/last-check"), "").unwrap();

    success(fixture.command(&launcher).arg("list").output().unwrap());
    assert_eq!(
        fixture.pointer("current"),
        Path::new(&format!("releases/{VERSION}/qy"))
    );
    assert!(!pending.is_symlink());
    assert!(!fixture.path("curl.log").exists());
}

#[test]
fn rerunning_installer_migrates_a_legacy_binary_and_preserves_foreign_symlinks() {
    let fixture = Fixture::new();
    fixture.archive(VERSION, true);
    let dir = fixture.path("managed dir");
    fs::create_dir(&dir).unwrap();
    let launcher = dir.join("qy");
    executable(&launcher, "#!/bin/sh\necho legacy\n");
    let install = || {
        fixture
            .command("/bin/sh")
            .arg(Path::new(env!("CARGO_MANIFEST_DIR")).join("../../install.sh"))
            .args(["--version", VERSION, "--dir"])
            .arg(&dir)
            .output()
            .unwrap()
    };
    success(install());
    assert_eq!(
        fs::read_link(&launcher).unwrap(),
        Path::new(".quarry/current")
    );
    assert_eq!(
        fixture.pointer("current"),
        Path::new(&format!("releases/{VERSION}/qy"))
    );
    let backup = fs::read_dir(dir.join(".quarry"))
        .unwrap()
        .flatten()
        .find(|entry| {
            entry
                .file_name()
                .to_string_lossy()
                .starts_with("previous-qy-")
        })
        .unwrap();
    assert_eq!(
        success(fixture.command(backup.path()).output().unwrap()).trim(),
        "legacy"
    );

    fs::remove_file(&launcher).unwrap();
    symlink("../tools/curl", &launcher).unwrap();
    assert!(!install().status.success());
    assert_eq!(
        fs::read_link(&launcher).unwrap(),
        Path::new("../tools/curl")
    );
}

#[test]
fn unpacked_executable_never_checks_or_updates() {
    let fixture = Fixture::new();
    success(fixture.command(QY).arg("list").output().unwrap());
    let failure = fixture.command(QY).arg("update").output().unwrap();
    assert!(!failure.status.success());
    assert!(String::from_utf8_lossy(&failure.stderr).contains("official native install.sh"));
    assert!(!fixture.path("curl.log").exists());
}
