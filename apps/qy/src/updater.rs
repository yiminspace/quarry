use crate::service::{Failure, Result};
use semver::Version;
use std::{
    ffi::{OsStr, OsString},
    fs::{self, File, OpenOptions},
    io::{self, Write},
    os::unix::{fs::symlink, process::CommandExt},
    path::{Path, PathBuf},
    process::{Command, Stdio},
    time::Duration,
};

const INSTALLER: &str = include_str!("../../../install.sh");
const MANAGED_DIR: &str = ".quarry";
const LAUNCHER: &str = ".quarry/current";
const CURRENT: &str = "current";
const PENDING: &str = "pending";
const UPDATE_CHECK: &str = "--internal-quarry-update-check";
const CHECK_INTERVAL: Duration = Duration::from_secs(60 * 60);
const LATEST_RELEASE: &str = "https://api.github.com/repos/yiminspace/quarry/releases/latest";

struct Install {
    root: PathBuf,
}

fn version(value: &str) -> Result<Version> {
    let version =
        Version::parse(value).map_err(|_| Failure::invalid("Invalid Quarry release version"))?;
    let prerelease = version.pre.as_str();
    let is_rc = prerelease
        .strip_prefix("rc.")
        .is_some_and(|number| number.bytes().all(|byte| byte.is_ascii_digit()));
    if version.major != 2 || !version.build.is_empty() || !(prerelease.is_empty() || is_rc) {
        return Err(Failure::invalid("Invalid Quarry release version"));
    }
    Ok(version)
}

fn target(version: &Version) -> PathBuf {
    PathBuf::from("releases")
        .join(version.to_string())
        .join("qy")
}

impl Install {
    fn for_executable() -> Option<Self> {
        let executable = std::env::current_exe().ok()?.canonicalize().ok()?;
        let version = version(env!("CARGO_PKG_VERSION")).ok()?;
        let root = executable.ancestors().nth(3)?;
        let install = Self { root: root.into() };
        if root.file_name()? != MANAGED_DIR
            || root.join(target(&version)) != executable
            || !install.has_launcher()
            || install.pointer(CURRENT)? != version
        {
            return None;
        }
        Some(install)
    }

    fn has_launcher(&self) -> bool {
        self.root
            .parent()
            .and_then(|dir| fs::read_link(dir.join("qy")).ok())
            .is_some_and(|link| link == Path::new(LAUNCHER))
    }

    fn pointer(&self, name: &str) -> Option<Version> {
        let link = fs::read_link(self.root.join(name)).ok()?;
        let version = version(link.parent()?.file_name()?.to_str()?).ok()?;
        if link != target(&version) {
            return None;
        }
        let path = self.root.join(&link);
        let metadata = path.symlink_metadata().ok()?;
        if !metadata.is_file() || path.canonicalize().ok()? != path {
            return None;
        }
        Some(version)
    }

    fn set_pointer(&self, name: &str, version: &Version) -> Result<()> {
        let temporary = tempfile::tempdir_in(&self.root)?;
        let pointer = temporary.path().join("link");
        symlink(target(version), &pointer)?;
        fs::rename(pointer, self.root.join(name))?;
        Ok(())
    }

    fn lock(&self) -> Result<File> {
        Ok(OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .truncate(false)
            .open(self.root.join("update.lock"))?)
    }

    fn check_due(&self) -> bool {
        self.root
            .join("last-check")
            .metadata()
            .and_then(|metadata| metadata.modified())
            .map_or(true, |checked| {
                checked.elapsed().unwrap_or_default() >= CHECK_INTERVAL
            })
    }

    fn save_release(&self, version: &Version, binary: &Path) -> Result<()> {
        let destination = self.root.join(target(version));
        let directory = destination.parent().expect("release path has a directory");
        ensure_directory(&self.root.join("releases"))?;
        ensure_directory(directory)?;
        let staging = tempfile::tempdir_in(directory)?;
        let candidate = staging.path().join("qy");
        fs::copy(binary, &candidate)?;
        fs::rename(candidate, destination)?;
        Ok(())
    }

    fn remove_pending(&self) {
        let _ = fs::remove_file(self.root.join(PENDING));
    }

    fn newer_pending(&self, current: &Version) -> Option<Version> {
        self.pointer(PENDING)
            .filter(|candidate| candidate > current)
    }
}

fn ensure_directory(path: &Path) -> Result<()> {
    match fs::create_dir(path) {
        Ok(()) => Ok(()),
        Err(error)
            if error.kind() == io::ErrorKind::AlreadyExists
                && path
                    .symlink_metadata()
                    .is_ok_and(|metadata| metadata.is_dir()) =>
        {
            Ok(())
        }
        Err(error) => Err(error.into()),
    }
}

pub(crate) fn is_managed_install() -> bool {
    Install::for_executable().is_some()
}

fn auto_enabled() -> bool {
    std::env::var_os("QUARRY_AUTO_UPDATE").as_deref() != Some("0".as_ref())
        && std::env::var_os("QUARRY_UPDATE_CHECK").as_deref() != Some("0".as_ref())
}

pub(crate) fn internal_command(args: &[OsString]) -> Option<Result<()>> {
    match args {
        [_, command] if command == UPDATE_CHECK => Some(background_check()),
        [_, command] if command == "--internal-quarry-install-protocol" => {
            println!("1");
            Some(Ok(()))
        }
        [_, command, directory, release, source] if command == "--internal-quarry-install" => Some(
            install_release(Path::new(directory), release, Path::new(source)),
        ),
        _ => None,
    }
}

fn install_release(directory: &Path, release: &OsStr, source: &Path) -> Result<()> {
    let release = version(
        release
            .to_str()
            .ok_or_else(|| Failure::invalid("Invalid Quarry release version"))?,
    )?;
    let directory = directory.canonicalize()?;
    let install = Install {
        root: directory.join(MANAGED_DIR),
    };
    ensure_directory(&install.root)?;
    let _lock = install.lock()?;
    fs2::FileExt::lock_exclusive(&_lock)?;
    let launcher = directory.join("qy");
    match launcher.symlink_metadata() {
        Ok(metadata) if metadata.is_symlink() && install.has_launcher() => {}
        Ok(metadata) if metadata.is_file() => {
            let backup = tempfile::Builder::new()
                .prefix("previous-qy-")
                .tempfile_in(&install.root)?;
            fs::copy(&launcher, backup.path())?;
            backup.keep().map_err(|error| error.error)?;
        }
        Err(error) if error.kind() == io::ErrorKind::NotFound => {}
        _ => {
            return Err(Failure::invalid(
                "The selected directory has a qy launcher owned by another installer",
            ));
        }
    }
    install.save_release(&release, source)?;
    install.set_pointer(CURRENT, &release)?;
    install.remove_pending();
    if !install.has_launcher() {
        let temporary = tempfile::tempdir_in(&directory)?;
        let link = temporary.path().join("qy");
        symlink(LAUNCHER, &link)?;
        fs::rename(link, launcher)?;
    }
    Ok(())
}

pub(crate) fn on_start(args: &[OsString]) {
    if !auto_enabled() {
        return;
    }
    let Some(install) = Install::for_executable() else {
        return;
    };
    if install.root.join(PENDING).is_symlink() {
        let _ = activate_on_start(&install, args);
    }
    if install.check_due()
        && let Ok(mut worker) = Command::new(install.root.join(CURRENT))
            .arg(UPDATE_CHECK)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .process_group(0)
            .spawn()
    {
        // Reap the worker if the caller is a long-lived GUI or MCP process.
        let _ = std::thread::Builder::new().spawn(move || worker.wait());
    }
}

fn activate_on_start(install: &Install, args: &[OsString]) -> Result<()> {
    let lock = install.lock()?;
    if fs2::FileExt::try_lock_exclusive(&lock).is_err() {
        return Ok(());
    }
    let Some(current) = install.pointer(CURRENT) else {
        return Ok(());
    };
    let Some(candidate) = install.newer_pending(&current) else {
        return Ok(());
    };
    install.set_pointer(CURRENT, &candidate)?;
    install.remove_pending();
    let error = Command::new(install.root.join(target(&candidate)))
        .arg0(&args[0])
        .args(&args[1..])
        .exec();
    install.set_pointer(CURRENT, &current)?;
    Err(error.into())
}

fn background_check() -> Result<()> {
    if !auto_enabled() {
        return Ok(());
    }
    let Some(install) = Install::for_executable() else {
        return Ok(());
    };
    let lock = install.lock()?;
    if fs2::FileExt::try_lock_exclusive(&lock).is_err() || !install.check_due() {
        return Ok(());
    }
    fs::write(install.root.join("last-check"), b"")?;
    stage_latest(&install)?;
    Ok(())
}

pub(crate) fn update_now() -> Result<()> {
    let install = Install::for_executable().ok_or_else(|| {
        Failure::invalid("Install Quarry with the official native install.sh to use qy update")
    })?;
    let lock = install.lock()?;
    fs2::FileExt::lock_exclusive(&lock)?;
    fs::write(install.root.join("last-check"), b"")?;
    let (current, latest) = stage_latest(&install)?;
    if let Some(latest) = latest {
        install.set_pointer(CURRENT, &latest)?;
        install.remove_pending();
        println!("Updated Quarry to {latest}.");
    } else {
        println!("Quarry {current} is up to date.");
    }
    Ok(())
}

fn stage_latest(install: &Install) -> Result<(Version, Option<Version>)> {
    let current = install
        .pointer(CURRENT)
        .ok_or_else(|| Failure::invalid("Missing current Quarry release"))?;
    let response = Command::new("curl")
        .args([
            "-q",
            "-fsSL",
            "--proto",
            "=https",
            "--proto-redir",
            "=https",
            "--connect-timeout",
            "10",
            "--max-time",
            "30",
            "--max-filesize",
            "1048576",
            LATEST_RELEASE,
        ])
        .output()?;
    if !response.status.success() {
        return Err(Failure::invalid(
            "Unable to check the latest Quarry release",
        ));
    }
    let release: serde_json::Value = serde_json::from_slice(&response.stdout)?;
    let tag = release["tag_name"]
        .as_str()
        .and_then(|tag| tag.strip_prefix('v'))
        .ok_or_else(|| Failure::invalid("Missing latest Quarry release version"))?;
    let latest = version(tag)?;
    let pending = install.newer_pending(&current);
    if latest <= current
        || !latest.pre.is_empty()
        || release["prerelease"].as_bool() == Some(true)
        || release["draft"].as_bool() == Some(true)
        || pending.as_ref().is_some_and(|pending| pending >= &latest)
    {
        return Ok((current, pending));
    }

    let staging = tempfile::tempdir_in(&install.root)?;
    let mut downloader = Command::new("/bin/sh")
        .args([
            "-s",
            "--",
            "--download-only",
            "--version",
            &latest.to_string(),
            "--dir",
        ])
        .arg(staging.path())
        .stdin(Stdio::piped())
        .stdout(Stdio::null())
        .stderr(Stdio::piped())
        .spawn()?;
    let write = downloader
        .stdin
        .take()
        .expect("piped installer stdin")
        .write_all(INSTALLER.as_bytes());
    let output = downloader.wait_with_output()?;
    write?;
    if !output.status.success() {
        let error = String::from_utf8_lossy(&output.stderr);
        return Err(Failure::invalid(format!(
            "Quarry release verification failed: {}",
            error.trim()
        )));
    }
    install.save_release(&latest, &staging.path().join("qy"))?;
    install.set_pointer(PENDING, &latest)?;
    Ok((current, Some(latest)))
}
