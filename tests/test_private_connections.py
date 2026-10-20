"""Credential writes and psql password handling, without live configuration/DBs."""
import os
from pathlib import Path
import stat
import subprocess
import tomllib
from urllib.parse import parse_qs, urlparse

import pytest

from quarry import core, workspace

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("existing", [False, True])
def test_connections_private_atomic_write(tmp_path, monkeypatch, existing):
    target = tmp_path / "connections.toml"
    if existing:
        target.write_text("# old\n")
        target.chmod(0o644)
    monkeypatch.setattr(workspace.WS, "connections_file", target)
    replace = os.replace
    seen = []

    def inspect_replace(src, dst):
        src = Path(src)
        assert src.parent == target.parent
        assert src != target
        assert stat.S_IMODE(src.stat().st_mode) == 0o600
        assert tomllib.loads(src.read_text())["db"]["url"] == "postgres://u:secret@h/d"
        assert target.read_text() == "# old\n" if existing else not target.exists()
        seen.append(src)
        replace(src, dst)

    monkeypatch.setattr(core.os, "replace", inspect_replace)
    previous_umask = os.umask(0)
    try:
        core._write_connections_file(["# preserved"], {"db": {"url": "postgres://u:secret@h/d", "production": True}})
    finally:
        os.umask(previous_umask)
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert target.read_text().startswith("# preserved\n")
    assert tomllib.loads(target.read_text())["db"]["production"] is True
    assert seen and not seen[0].exists()


@pytest.mark.parametrize("failure", ["fsync", "replace"])
def test_connections_failure_preserves_original_and_cleans_temp(tmp_path, monkeypatch, failure):
    target = tmp_path / "connections.toml"
    target.write_text("original")
    def fail(*args):
        raise OSError("injected failure")
    monkeypatch.setattr(core.os, failure, fail)
    with pytest.raises(OSError, match="injected failure"):
        core._write_connections_file([], {"db": {"url": "secret"}}, target)
    assert target.read_text() == "original"
    assert list(tmp_path.iterdir()) == [target]


def test_connections_unique_temps_and_existing_lock(tmp_path, monkeypatch):
    fcntl = pytest.importorskip("fcntl")
    target = tmp_path / "connections.toml"
    replace = os.replace
    names = []
    def inspect_replace(src, dst):
        names.append(src)
        replace(src, dst)
    monkeypatch.setattr(core.os, "replace", inspect_replace)
    with core.connections_file_lock(target):
        lock = target.with_name(target.name + ".lock")
        inode = lock.stat().st_ino
        for _ in range(2):
            core._write_connections_file([], {"db": {"url": "secret"}}, target)
            assert lock.stat().st_ino == inode
            with lock.open() as contender:
                with pytest.raises(BlockingIOError):
                    fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
    assert len(set(names)) == 2
    with lock.open() as contender:
        fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(contender, fcntl.LOCK_UN)


@pytest.mark.parametrize("url,password", [
    ("postgresql://user:s%3Ae%5Cc%40r%25et@host:5432/db", "s:e\\c@r%et"),
    ("postgres://user:old@[::1]:5432/db?password=new%3Asecret&passfile=/old", "new:secret"),
    ("postgres://host1:5432,host2:5433/db?user=u&password=a+b%20c", "a+b c"),
    ("postgres:///db?host=%2Ftmp&password=first&%70assword=last", "last"),
])
def test_psql_private_password_and_preserved_options(tmp_path, monkeypatch, url, password):
    monkeypatch.setattr(core.tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr(core, "resolve_psql", lambda: "psql")
    monkeypatch.setenv("PGPASSWORD", "inherited")
    monkeypatch.setenv("PGPASSFILE", "/inherited")
    seen = []
    def run(cmd, **kwargs):
        parsed = urlparse(cmd[1])
        query = parse_qs(parsed.query)
        assert parsed.password is None
        assert "password" not in query and "passfile" not in query
        assert query["sslmode"] == ["verify-full"]
        assert query["sslrootcert"] == ["/official CA.pem"]
        assert query["connect_timeout"] == ["7"]
        assert "ON_ERROR_STOP=1" in cmd and "n=3" in cmd
        assert cmd[-2:] == ["-f", "-"]
        assert kwargs["input"] == "select 1" and kwargs["timeout"] == 12
        env = kwargs["env"]
        assert "PGPASSWORD" not in env
        assert env["PGCONNECT_TIMEOUT"] == "7"
        pgpass = Path(env["PGPASSFILE"])
        seen.append(pgpass)
        assert stat.S_IMODE(pgpass.stat().st_mode) == 0o600
        escaped = password.replace("\\", "\\\\").replace(":", "\\:")
        assert pgpass.read_text() == f"*:*:*:*:{escaped}\n"
        return subprocess.CompletedProcess(cmd, 0, "ok", "")
    monkeypatch.setattr(core.subprocess, "run", run)
    url += ("&" if "?" in url else "?") + "sslmode=verify-full&sslrootcert=%2Fofficial%20CA.pem"
    assert core.run_psql_capture(url, "select 1", psql_vars={"n": "3"}, timeout=12, connect_timeout=7) == (0, "ok", "")
    assert seen and not seen[0].exists()
    assert os.environ["PGPASSWORD"] == "inherited"
    assert os.environ["PGPASSFILE"] == "/inherited"
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("failure", ["timeout", "spawn", "sql"])
def test_psql_password_cleanup_on_failure(tmp_path, monkeypatch, failure):
    monkeypatch.setattr(core.tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr(core, "resolve_psql", lambda: "psql")
    def run(cmd, **kwargs):
        assert Path(kwargs["env"]["PGPASSFILE"]).exists()
        assert "secret" not in repr(cmd)
        if failure == "timeout":
            raise subprocess.TimeoutExpired(cmd, 1)
        if failure == "spawn":
            raise OSError("spawn failed")
        return subprocess.CompletedProcess(cmd, 3, "", "SQL failed")
    monkeypatch.setattr(core.subprocess, "run", run)
    if failure == "spawn":
        with pytest.raises(OSError, match="spawn failed"):
            core.run_psql_capture("postgres://u:secret@h/d", "select 1")
    else:
        result = core.run_psql_capture("postgres://u:secret@h/d", "select 1")
        assert result[0] == (-1 if failure == "timeout" else 3)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("encoded", ["%0A", "%0D", "%00"])
def test_psql_rejects_pgpass_line_injection(monkeypatch, encoded):
    def forbidden(*args, **kwargs):
        pytest.fail("must reject before spawning psql")
    monkeypatch.setattr(core.subprocess, "run", forbidden)
    with pytest.raises(core.QuarryError, match="unsupported by PGPASSFILE") as exc:
        core.run_psql_capture(f"postgres://u:secret{encoded}injection@h/d", "select 1")
    assert "secret" not in str(exc.value)


@pytest.mark.parametrize("url", ["postgres://u@h/d", "postgres://u:@h/d"])
def test_psql_passwordless_preserves_environment(monkeypatch, url):
    monkeypatch.setattr(core, "resolve_psql", lambda: "psql")
    def run(cmd, **kwargs):
        assert kwargs["env"] is None
        assert urlparse(cmd[1]).password is None
        return subprocess.CompletedProcess(cmd, 0, "", "")
    monkeypatch.setattr(core.subprocess, "run", run)
    assert core.run_psql_capture(url, "select 1")[0] == 0
