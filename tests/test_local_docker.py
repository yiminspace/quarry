"""Real-docker lifecycle tests for `qy local` — the behavioral half.

These run only when a docker daemon is reachable (CI ubuntu runners have one);
otherwise every test skips (never fails), matching the DB/redis policy. Unlike
test_local.py (which stubs the subprocess seam and can only prove arg wiring),
these do real `docker run`/`stop`/`start`/`volume rm` against throwaway
containers to assert container-level behavior: a container actually comes up,
`up` is idempotent, data survives stop→start on the named volume, and
`down --purge` yields a clean database.

Every container/volume here uses a unique throwaway name so a developer's real
`qy local` state is never touched.
"""

from __future__ import annotations

import socket
import uuid

import pytest

from quarry import local
from conftest import requires_docker

pytestmark = [pytest.mark.integration, requires_docker]

PG_IMAGE = "postgres:16-alpine"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture()
def pg_spec():
    uniq = uuid.uuid4().hex[:8]
    spec = local.EngineSpec(
        engine="postgres",
        container=f"quarry-test-pg-{uniq}",
        volume=f"quarry-test-pgvol-{uniq}",
        port=_free_port(),
        internal_port=5432,
        default_image=PG_IMAGE,
    )
    try:
        yield spec
    finally:
        local._run_docker(["rm", "-f", spec.container], timeout=60)
        local._run_docker(["volume", "rm", spec.volume], timeout=30)


def _psql(spec, sql: str, db: str = "postgres"):
    return local._run_docker(
        ["exec", spec.container, "psql", "-U", local.LOCAL_PG_USER, "-d", db, "-tAc", sql],
        timeout=20,
    )


def _bring_up(spec) -> None:
    local.start_container(spec, image=PG_IMAGE)
    assert local.wait_pg_ready(spec, timeout=60), "postgres never became ready"


def test_up_is_idempotent_and_status_reports_running(pg_spec):
    assert local.start_container(pg_spec, image=PG_IMAGE) == "created"
    assert local.wait_pg_ready(pg_spec, timeout=60)
    # a second up must not create a duplicate container
    assert local.start_container(pg_spec, image=PG_IMAGE) == "running"

    st = local.engine_status(pg_spec)
    assert st["running"] is True
    assert st["port"] == pg_spec.port
    assert "postgres" in (st["image"] or "")


def test_data_persists_across_stop_start(pg_spec):
    _bring_up(pg_spec)
    local.ensure_pg_database(pg_spec, "probe")
    # idempotent createdb: a second call must not error
    local.ensure_pg_database(pg_spec, "probe")
    rc, _, err = _psql(pg_spec, "CREATE TABLE t(id int); INSERT INTO t VALUES (42)", db="probe")
    assert rc == 0, err

    # down WITHOUT purge keeps the volume; start again and the row is still there
    res = local.down_engine(pg_spec, purge=False)
    assert res["stopped"] is True and res["removed_volume"] is False
    assert local.start_container(pg_spec, image=PG_IMAGE) == "started"
    assert local.wait_pg_ready(pg_spec, timeout=60)
    rc, out, err = _psql(pg_spec, "SELECT id FROM t", db="probe")
    assert rc == 0 and out.strip() == "42", err


def test_purge_yields_clean_database(pg_spec):
    _bring_up(pg_spec)
    local.ensure_pg_database(pg_spec, "probe")

    res = local.down_engine(pg_spec, purge=True)
    assert res["removed_volume"] is True
    assert local.volume_exists(pg_spec.volume) is False

    # a fresh up starts from an empty volume: the old logical db is gone
    _bring_up(pg_spec)
    rc, out, _ = _psql(
        pg_spec, "SELECT 1 FROM pg_database WHERE datname='probe'")
    assert rc == 0 and out.strip() == ""


def test_port_conflict_is_reported(pg_spec):
    # hold the host port so the fresh `docker run` bind would collide
    with socket.socket() as s:
        s.bind(("127.0.0.1", pg_spec.port))
        s.listen()
        with pytest.raises(local.QuarryError) as ei:
            local.start_container(pg_spec, image=PG_IMAGE)
    assert "already in use" in str(ei.value)
