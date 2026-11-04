# Install and upgrade Quarry 2

Supported release artifacts: macOS arm64/x86_64 and Linux glibc 2.28+ arm64/x86_64.
Windows and musl/Alpine binaries are not part of this release.

## Native binary (no Python or Rust toolchain)

Download `install.sh` from the matching Git tag, inspect it, then run:

```sh
sh install.sh --version 2.0.0-rc.2
$HOME/.local/bin/qy --version
```

The installer checks SHA-256 before replacing the executable. It installs to
`~/.local/bin`; use `--dir` to choose another directory and add it to PATH.
Release archives and `SHA256SUMS` are available on GitHub Releases.

## pip / pipx / uv

```sh
pip install --upgrade --only-binary=:all: quarry-db==2.0.0rc2
# or: pipx install --pip-args='--only-binary=:all:' quarry-db==2.0.0rc2
# or: uv tool install quarry-db==2.0.0rc2
qy --version
```

These install the same native Rust executable from a platform wheel. Python is
needed to run pip, not to execute the installed program. No Python business API
ships in version 2. The old `from quarry import ...` API remains in Python 1.x;
use the Rust crates, CLI JSON or MCP for new integrations. No source build is
silently required on unsupported platforms.

## Upgrade an existing Python installation

1. Finish active queries/assistant turns. Stop the old GUI and keeper through
   their owning process/version (`qy down` stops the keeper, not Docker data).
2. Upgrade through the same installer/tool that owns the existing installation.
   Avoid installing pip, pipx and native copies into competing PATH locations.
3. Remove any custom shell `qy` function that forces `PYENV_VERSION` or invokes
   `python -m quarry`. Refresh the shell command cache (`rehash` in zsh).
4. Check `type -a qy` and `qy --version`, then restart `qy gui` at the same origin
   to retain browser drafts and preferences. Update MCP to run `qy mcp`.

Config, saved queries and assistant histories are retained. Existing Local data
is not automatically purged. SSH and Docker still use their system executables;
PostgreSQL schema sync additionally needs compatible `pg_dump`/`psql`. Ordinary
queries use native drivers. IAM model access may use a trusted local credential
command; no private account configuration is included in releases.

Python 1.11.0 is preserved by tag and the protected `archive/python-1.x` branch.
To roll back, reinstall that exact version in its original installer and restore
its private config backup if using Rust-only credential settings.
