"""Real-docker integration tests for `qy local sync`.

Uses a throwaway Postgres container as the sync target and the suite's
`quarry_test` database (when reachable) as the source. Skips when docker or
the test DB is unavailable — same policy as test_local_docker.py.
"""

from __future__ import annotations

import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from conftest import TEST_DB_URL, requires_db, requires_docker
from quarry import core, local, local_sync, workspace

PG_IMAGE = "postgres:16-alpine"
REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_port_free(port: int, *, timeout: float = 30.0) -> None:
    """After docker rm the host port can linger briefly before the next test."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not local.port_in_use(port):
            return
        time.sleep(0.05)
    raise RuntimeError(f"port {port} still in use after {timeout}s")


def _start_container(spec: local.EngineSpec, *, image: str, attempts: int = 5) -> None:
    """`start_container`'s own pre-check/bind race can still lose to a port
    that gets grabbed transiently right between the check and `docker run`
    (see `_is_port_conflict` in local.py). Retry a couple of times before
    giving up — the conflict is momentary, not a real occupant of the port.
    """
    for attempt in range(attempts):
        try:
            local.start_container(spec, image=image)
            return
        except core.QuarryError as e:
            if attempt == attempts - 1 or "already in use" not in str(e):
                raise
            time.sleep(1)


def _pg_dump() -> str | None:
    import shutil
    for cand in ("pg_dump", "/opt/homebrew/opt/postgresql@13/bin/pg_dump"):
        if shutil.which(cand):
            return cand
    return None


requires_pg_dump = pytest.mark.skipif(not _pg_dump(), reason="pg_dump not in PATH")


@pytest.fixture()
def pg_spec():
    uniq = uuid.uuid4().hex[:8]
    port = _free_port()
    _wait_port_free(port)
    spec = local.EngineSpec(
        engine="postgres",
        container=f"quarry-test-sync-pg-{uniq}",
        volume=f"quarry-test-sync-pgvol-{uniq}",
        port=port,
        internal_port=5432,
        default_image=PG_IMAGE,
    )
    try:
        yield spec
    finally:
        local._run_docker(["rm", "-f", spec.container], timeout=60)
        local._run_docker(["volume", "rm", spec.volume], timeout=30)


@pytest.fixture()
def sync_ws(tmp_path: Path, pg_spec):
    """Workspace: quarry_test as dev source, docker container as local target."""
    logical = "syncshop"
    local_url = pg_spec.url(logical)
    (tmp_path / "connections.toml").write_text(
        f'[{logical}_dev]\nurl = "{TEST_DB_URL}"\nengine = "postgres"\n'
        f'env = "dev"\ndb = "{logical}"\n\n'
        f'[{logical}_local]\nurl = "{local_url}"\nengine = "postgres"\n'
        f'env = "local"\ndb = "{logical}"\n',
        encoding="utf-8",
    )
    (tmp_path / "queries").mkdir()
    workspace.configure_workspace(str(tmp_path))
    _start_container(pg_spec, image=PG_IMAGE)
    assert local.wait_pg_ready(pg_spec, timeout=60)
    local.ensure_pg_database(pg_spec, logical)
    yield tmp_path, logical, local_url
    workspace.configure_workspace(None)


def _psql(url: str, sql: str) -> tuple[int, str, str]:
    return core.run_psql_capture(url, sql, timeout=30)


def _hold_connection(url: str) -> subprocess.Popen:
    """Open a long-lived session on the target DB (outside sync's control)."""
    psql = core.resolve_psql()
    return subprocess.Popen(
        [psql, url, "--no-psqlrc", "-c", "BEGIN; SELECT pg_sleep(600);"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )


@requires_pg_dump
@pytest.mark.integration
@requires_docker
@requires_db
def test_sync_empty_local_matches_source(sync_ws):
    _ws, logical, local_url = sync_ws
    local_sync.sync_schema(logical, from_env="dev")
    local_sync.assert_schemas_match(TEST_DB_URL, local_url)


@requires_pg_dump
@pytest.mark.integration
@requires_docker
@requires_db
def test_sync_idempotent_with_non_public_source_schema(sync_ws):
    _ws, logical, local_url = sync_ws
    extra = f"sync_extra_{uuid.uuid4().hex[:8]}"
    rc, _, err = _psql(
        TEST_DB_URL,
        f"CREATE SCHEMA {extra}; CREATE TABLE {extra}.t(id int);",
    )
    assert rc == 0, err
    try:
        local_sync.sync_schema(logical, from_env="dev")
        local_sync.sync_schema(logical, from_env="dev")
        local_sync.assert_schemas_match(TEST_DB_URL, local_url)
        rc, out, err = _psql(
            local_url,
            "SELECT COUNT(*) FROM information_schema.tables "
            f"WHERE table_schema='{extra}'",
        )
        assert rc == 0 and out.strip() == "1", err
    finally:
        _psql(TEST_DB_URL, f"DROP SCHEMA IF EXISTS {extra} CASCADE")


@requires_pg_dump
@pytest.mark.integration
@requires_docker
@requires_db
def test_sync_clears_stale_non_public_schema_on_resync(sync_ws):
    _ws, logical, local_url = sync_ws
    local_sync.sync_schema(logical, from_env="dev")
    stale = f"stale_extra_{uuid.uuid4().hex[:8]}"
    rc, _, err = _psql(
        local_url,
        f"CREATE SCHEMA {stale}; CREATE TABLE {stale}.garbage(id int);",
    )
    assert rc == 0, err

    local_sync.sync_schema(logical, from_env="dev")
    local_sync.assert_schemas_match(TEST_DB_URL, local_url)

    rc, out, err = _psql(
        local_url,
        "SELECT COUNT(*) FROM information_schema.schemata "
        f"WHERE schema_name='{stale}'",
    )
    assert rc == 0 and out.strip() == "0", err


@requires_pg_dump
@pytest.mark.integration
@requires_docker
@requires_db
def test_sync_clears_stale_objects_on_resync(sync_ws):
    _ws, logical, local_url = sync_ws
    local_sync.sync_schema(logical, from_env="dev")
    rc, _, err = _psql(
        local_url,
        "CREATE TABLE leftover_garbage(id int); "
        "INSERT INTO customers(name, email) VALUES ('stale', 'stale@ex.com');",
    )
    assert rc == 0, err

    local_sync.sync_schema(logical, from_env="dev")
    local_sync.assert_schemas_match(TEST_DB_URL, local_url)

    rc, out, err = _psql(
        local_url,
        "SELECT COUNT(*) FROM information_schema.tables "
        "WHERE table_schema='public' AND table_name='leftover_garbage'",
    )
    assert rc == 0 and out.strip() == "0", err

    rc, out, err = _psql(local_url, "SELECT COUNT(*) FROM customers")
    assert rc == 0 and out.strip() == "0", err


@requires_pg_dump
@pytest.mark.integration
@requires_docker
@requires_db
def test_sync_terminates_concurrent_holder(sync_ws):
    _ws, logical, local_url = sync_ws
    holder = _hold_connection(local_url)
    try:
        local_sync.sync_schema(logical, from_env="dev")
        local_sync.assert_schemas_match(TEST_DB_URL, local_url)
    finally:
        holder.terminate()
        holder.wait(timeout=10)
    # holder should have been terminated (not still sleeping)
    assert holder.poll() is not None


@requires_pg_dump
@pytest.mark.integration
@requires_docker
@requires_db
def test_sync_idempotent_with_publication(sync_ws):
    _ws, logical, local_url = sync_ws
    pub = f"sync_pub_{uuid.uuid4().hex[:8]}"
    rc, _, err = _psql(TEST_DB_URL, f"CREATE PUBLICATION {pub} FOR TABLE customers;")
    assert rc == 0, err
    try:
        local_sync.sync_schema(logical, from_env="dev")
        local_sync.sync_schema(logical, from_env="dev")
        local_sync.assert_schemas_match(TEST_DB_URL, local_url)
        rc, out, err = _psql(
            local_url,
            "SELECT COUNT(*) FROM pg_publication WHERE pubname = "
            f"'{pub}'",
        )
        assert rc == 0 and out.strip() == "1", err
    finally:
        _psql(TEST_DB_URL, f"DROP PUBLICATION IF EXISTS {pub}")


@requires_pg_dump
@pytest.mark.integration
@requires_docker
@requires_db
def test_sync_keeps_previous_copy_as_prev(sync_ws):
    """The pre-sync database survives one generation as <db>__prev."""
    _ws, logical, local_url = sync_ws
    local_sync.sync_schema(logical, from_env="dev")
    rc, _, err = _psql(
        local_url,
        "CREATE TABLE prev_marker(id int); INSERT INTO prev_marker VALUES (42);",
    )
    assert rc == 0, err

    res = local_sync.sync_schema(logical, from_env="dev")
    assert res["prev"] == f"{logical}__prev"
    local_sync.assert_schemas_match(TEST_DB_URL, local_url)
    # marker is gone from the live db but readable in the kept previous copy
    rc, out, _ = _psql(
        local_url,
        "SELECT COUNT(*) FROM information_schema.tables WHERE table_name='prev_marker'")
    assert rc == 0 and out.strip() == "0"
    prev_url = local_sync._replace_db(local_url, res["prev"])
    rc, out, err = _psql(prev_url, "SELECT id FROM prev_marker")
    assert rc == 0 and out.strip() == "42", err


@requires_pg_dump
@pytest.mark.integration
@requires_docker
@requires_db
def test_sync_failure_leaves_live_db_intact(sync_ws):
    """A dump that fails to apply must not touch the live db and must not
    leave a half-built staging database behind."""
    _ws, logical, local_url = sync_ws
    local_sync.sync_schema(logical, from_env="dev")
    rc, _, err = _psql(local_url, "CREATE TABLE survivor(id int);")
    assert rc == 0, err

    from unittest import mock

    with mock.patch.object(
        local_sync, "run_pg_dump_schema", return_value="CREATE TABLE broken(;\n",
    ):
        with pytest.raises(core.QuarryError):
            local_sync.sync_schema(logical, from_env="dev")

    rc, out, err = _psql(
        local_url,
        "SELECT COUNT(*) FROM information_schema.tables WHERE table_name='survivor'")
    assert rc == 0 and out.strip() == "1", err
    admin_url = local_sync._admin_url(local_url)
    assert local_sync.database_exists(admin_url, f"{logical}__staging") is False


@requires_pg_dump
@pytest.mark.integration
@requires_docker
@requires_db
def test_sync_creates_main_db_when_absent(sync_ws):
    """Sync works on a container where the logical db was never created —
    the swap itself brings the live db into existence."""
    _ws, logical, local_url = sync_ws
    admin_url = local_sync._admin_url(local_url)
    local_sync.drop_database(admin_url, logical)
    assert local_sync.database_exists(admin_url, logical) is False

    res = local_sync.sync_schema(logical, from_env="dev")
    assert res["prev"] is None
    local_sync.assert_schemas_match(TEST_DB_URL, local_url)


@requires_pg_dump
@pytest.mark.integration
@requires_docker
@requires_db
def test_cli_local_sync_subprocess(sync_ws):
    ws, logical, _local_url = sync_ws
    import os

    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC)
    proc = subprocess.run(
        [sys.executable, "-m", "quarry.cli", "--workspace", str(ws),
         "local", "sync", logical, "--from", "dev"],
        capture_output=True, text=True, env=env, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    assert "synced schema" in proc.stdout


@pytest.mark.integration
@requires_db
def test_cli_rejects_non_local_target(tmp_path):
    """Sync must refuse when the resolved target is not env=local."""
    (tmp_path / "connections.toml").write_text(
        f'[only_dev]\nurl = "{TEST_DB_URL}"\nengine = "postgres"\nenv = "dev"\n',
        encoding="utf-8",
    )
    (tmp_path / "queries").mkdir()
    import os

    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC)
    proc = subprocess.run(
        [sys.executable, "-m", "quarry.cli", "--workspace", str(tmp_path),
         "local", "sync", "only_dev"],
        capture_output=True, text=True, env=env, timeout=30,
    )
    assert proc.returncode == core.EXIT_SYNC_DENIED
    assert "sync refused" in proc.stderr


@pytest.mark.integration
def test_cli_rejects_local_env_on_remote_host(tmp_path):
    """env=local pointing at a non-loopback host must be refused — the gate is
    env AND host, so a hand-edited connections.toml cannot aim sync remotely."""
    (tmp_path / "connections.toml").write_text(
        '[shop_dev]\nurl = "postgresql://dev-host/shop"\nengine = "postgres"\n'
        'env = "dev"\ndb = "shop"\n\n'
        '[shop_local]\nurl = "postgresql://10.0.0.5:5432/shop"\nengine = "postgres"\n'
        'env = "local"\ndb = "shop"\n',
        encoding="utf-8",
    )
    (tmp_path / "queries").mkdir()
    import os

    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC)
    proc = subprocess.run(
        [sys.executable, "-m", "quarry.cli", "--workspace", str(tmp_path),
         "local", "sync", "shop"],
        capture_output=True, text=True, env=env, timeout=30,
    )
    assert proc.returncode == core.EXIT_SYNC_DENIED
    assert "loopback" in proc.stderr
