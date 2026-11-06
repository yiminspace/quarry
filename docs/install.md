# Install and upgrade Quarry 2

Supported release artifacts: macOS 11+ arm64/x86_64 and Linux glibc 2.28+ arm64/x86_64.
Windows and musl/Alpine binaries are not part of this release.

## Native binary (no Python or Rust toolchain)

Install or upgrade to the latest stable release with one command:

```sh
curl -fsSL https://yiminspace.github.io/quarry/install.sh | sh
```

The website serves the repository's canonical installer. It detects the OS and
architecture, downloads the latest stable native archive, and verifies SHA-256.
It installs the `qy` entry point at `~/.local/bin/qy` with versioned executables
in the adjoining `.quarry/releases` directory.

For a specific version or custom directory:

```sh
curl -fsSL https://yiminspace.github.io/quarry/install.sh | sh -s -- --version 2.0.0 --dir "$HOME/.local/bin"
```

To inspect the script before running, download it from the matching Git tag:

```sh
curl -fsSL https://raw.githubusercontent.com/yiminspace/quarry/v2.0.0/install.sh -o install-quarry.sh
```

```sh
sh install-quarry.sh --version 2.0.0
$HOME/.local/bin/qy --version
```

Use `--dir` to choose another directory and add it to PATH.
Release archives and `SHA256SUMS` are available on
[GitHub Releases](https://github.com/yiminspace/quarry/releases).

## Update

```sh
qy update
```

Starting with releases that include `qy update`, the installer-managed
executable checks for a newer stable release in the background, at most once
per hour. Quarry verifies and stages the download, then activates it at the
next launch. Run `qy update` to download and activate
the new version immediately. Set `QUARRY_AUTO_UPDATE=0` to opt out of automatic
downloads and activation. `QUARRY_UPDATE_CHECK=0` also hides the release notice
in the workbench and opts out of automatic updates. Both settings allow
explicit `qy update`.

If you copied a native binary manually, rerun the installer at the same
location to make it eligible for future self-updates. Use your normal build
process to update source builds. YLab's embedded copy updates with YLab.
The hosted workbench updates when the operator deploys a new Docker image.

## Switch from an existing Python package

1. Finish active queries and Assistant turns. Stop the old GUI and keeper with
   the version that started them. Use `qy down` for the keeper.
2. Remove the old executable through its owning tool
   (`pip uninstall quarry-db`, `pipx uninstall quarry-db` or
   `uv tool uninstall quarry-db`). Then run the native installer above.
3. Remove any shell function that invokes `python -m quarry` or overrides
   `PYENV_VERSION` for `qy`. Refresh the shell command cache (`rehash` in zsh).
4. Run `type -a qy` and `qy --version` to verify the new executable. Reopen
   `qy gui` at the same origin to retain browser drafts and preferences.
5. Set your MCP client to run the installed `qy mcp` directly. Existing MCP
   Registry entries continue to describe their historical package versions.

Config, saved queries and assistant histories are retained. Existing Local data
is not automatically purged. SSH and Docker still use their system executables;
PostgreSQL schema sync additionally needs compatible `pg_dump`/`psql`. Ordinary
queries use native drivers. IAM model access may use a trusted local credential
command; no private account configuration is included in releases.

Python 1.11.0 is preserved by tag and the protected `archive/python-1.x` branch.
To roll back, reinstall that exact version in its original installer and restore
its private config backup if using Rust-only credential settings.
