#!/usr/bin/env python3
"""Exercise the native installer, packaged CLI, existing workspace, and GUI."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import tomllib

root = Path(__file__).resolve().parents[1]
dist = Path(sys.argv[1]).resolve()
(archive,) = dist.glob("*.tar.gz")
version = tomllib.loads((root / "Cargo.toml").read_text())["workspace"]["package"]["version"]
assert archive.name.startswith(f"quarry-{version}-")
checksums = archive.with_name(archive.name + ".sha256")
with archive.open("rb") as file:
    digest = hashlib.file_digest(file, "sha256").hexdigest()
assert checksums.read_text() == f"{digest}  {archive.name}\n"

with tarfile.open(archive) as tar:
    assert set(tar.getnames()) == {"qy", "LICENSE", "INSTALL.md"}
    binary = tar.getmember("qy")
    assert binary.isfile() and binary.mode & 0o111

with tempfile.TemporaryDirectory() as tmp:
    temp = Path(tmp)
    home = temp / "home"
    home.mkdir()
    workspace = temp / "workspace"
    workspace.mkdir()
    db = workspace / "fixture.sqlite3"
    with sqlite3.connect(db) as connection:
        connection.execute("CREATE TABLE sentinel(value TEXT)")
        connection.execute("INSERT INTO sentinel VALUES('preserved')")
    config = home / ".config/quarry/config.toml"
    config.parent.mkdir(parents=True)
    config.write_text(f"workspaces = [{json.dumps(str(workspace))}]\n")
    connections = workspace / "connections.toml"
    connections.write_text('[test]\nurl="sqlite:///fixture.sqlite3"\nengine="sqlite"\n')
    queries = workspace / "queries"
    queries.mkdir()
    saved_query = queries / "read_marker.sql"
    saved_query.write_text("-- @name: read_marker\n-- @db: test\nSELECT value FROM sentinel;\n")
    before = {file: file.read_bytes() for file in (config, connections, db, saved_query)}

    # Serve only this candidate and its checksum from a disposable fixture.
    # The release can be tested before GitHub publishes it.
    fixtures = temp / "bin"
    fixtures.mkdir()
    curl = fixtures / "curl"
    curl.write_text(
        f"#!{sys.executable}\n"
        "import shutil, sys\n"
        f"base = 'https://github.com/yiminspace/quarry/releases/download/v{version}/'\n"
        f"assets = {{{archive.name!r}: {str(archive)!r}, 'SHA256SUMS': {str(checksums)!r}}}\n"
        "url, = [arg for arg in sys.argv[1:] if arg.startswith('https://')]\n"
        "assert url.startswith(base), url\n"
        "shutil.copyfile(assets[url[len(base):]], sys.argv[sys.argv.index('-o') + 1])\n"
    )
    curl.chmod(0o755)
    env = {
        "HOME": str(home),
        "PATH": str(fixtures) + os.pathsep + os.defpath,
        "QUARRY_UPDATE_CHECK": "0",
        "QUARRY_AUTO_UPDATE": "0",
    }
    install_dir = home / ".local/bin"
    subprocess.run(
        ["/bin/sh", root / "install.sh", "--version", version, "--dir", install_dir],
        env=env, check=True, timeout=30,
    )
    qy = install_dir / "qy"
    runtime_env = dict(env, PATH="/nonexistent")
    installed = subprocess.check_output([qy, "--version"], env=runtime_env, text=True, timeout=10)
    assert installed.strip() == f"qy {version}"
    cli = subprocess.check_output([qy, "run", "read_marker"], env=runtime_env, timeout=10)
    assert json.loads(cli) == [{"value": "preserved"}]
    mcp = subprocess.check_output(
        [qy, "mcp"], env=runtime_env, timeout=10,
        input=b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}\n',
    )
    assert "exec_sql" in {tool["name"] for tool in json.loads(mcp)["result"]["tools"]}
    subprocess.run([sys.executable, root / "scripts/verify_gui_install.py", qy],
                   env=runtime_env, check=True, timeout=45)
    for file, contents in before.items():
        assert file.read_bytes() == contents, f"existing workspace data changed: {file}"

print("Native install, Python-free CLI and MCP, existing workspace and packaged GUI passed")
