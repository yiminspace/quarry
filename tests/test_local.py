"""Unit tests for `qy local` — the docker lifecycle logic + env=local auto-register.

These never touch a real docker daemon: the single subprocess seam
(`local._run_docker`) is stubbed so every branch of the arg-building /
control-flow / connection-registration logic is exercised deterministically.
The real container-lifecycle behavior (a container actually comes up, data
actually persists) is asserted separately in test_local_docker.py, which skips
when docker is unavailable.
"""

from __future__ import annotations

import argparse
import socket
import tomllib
from pathlib import Path

import pytest

from quarry import core, local, workspace


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------

@pytest.fixture()
def local_ws(tmp_path: Path):
    """A temp workspace (no DB needed) configured process-wide, for the
    connections.toml read/write registration tests."""
    (tmp_path / "connections.toml").write_text(
        '[shop]\nurl = "postgresql://dev-host/shop"\nengine = "postgres"\n'
        'env = "dev"\ngroup = "commerce"\n',
        encoding="utf-8",
    )
    (tmp_path / "queries").mkdir()
    workspace.configure_workspace(str(tmp_path))
    yield tmp_path
    workspace.configure_workspace(None)


def _read_conns(tmp_path: Path) -> dict:
    with (tmp_path / "connections.toml").open("rb") as f:
        return tomllib.load(f)


# ---------------------------------------------------------------------------
# docker seam: resolve / availability / require
# ---------------------------------------------------------------------------

def test_resolve_docker_missing(monkeypatch):
    monkeypatch.setattr(local.shutil, "which", lambda _n: None)
    with pytest.raises(core.QuarryError) as ei:
        local.resolve_docker()
    assert ei.value.exit_code == core.EXIT_CONNECTION_ERROR
    assert "docker not found" in str(ei.value)


def test_resolve_docker_found(monkeypatch):
    monkeypatch.setattr(local.shutil, "which", lambda _n: "/usr/bin/docker")
    assert local.resolve_docker() == "/usr/bin/docker"


def test_docker_available_variants(monkeypatch):
    monkeypatch.setattr(local.shutil, "which", lambda _n: None)
    assert local.docker_available() is False

    monkeypatch.setattr(local.shutil, "which", lambda _n: "/usr/bin/docker")
    monkeypatch.setattr(local, "_run_docker", lambda *a, **k: (0, "27.0", ""))
    assert local.docker_available() is True

    monkeypatch.setattr(local, "_run_docker", lambda *a, **k: (1, "", "err"))
    assert local.docker_available() is False

    def _boom(*a, **k):
        raise RuntimeError("nope")
    monkeypatch.setattr(local, "_run_docker", _boom)
    assert local.docker_available() is False


def test_require_docker(monkeypatch):
    monkeypatch.setattr(local.shutil, "which", lambda _n: None)
    with pytest.raises(core.QuarryError):
        local.require_docker()

    monkeypatch.setattr(local.shutil, "which", lambda _n: "/usr/bin/docker")
    monkeypatch.setattr(local, "_run_docker", lambda *a, **k: (1, "", ""))
    with pytest.raises(core.QuarryError) as ei:
        local.require_docker()
    assert "daemon" in str(ei.value)

    monkeypatch.setattr(local, "_run_docker", lambda *a, **k: (0, "27.0", ""))
    assert local.require_docker() is None


# ---------------------------------------------------------------------------
# inspection helpers
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("rc,out,expected", [
    (1, "", "absent"),
    (0, "true\n", "running"),
    (0, "false\n", "stopped"),
])
def test_container_state(monkeypatch, rc, out, expected):
    monkeypatch.setattr(local, "_run_docker", lambda *a, **k: (rc, out, ""))
    assert local.container_state("c") == expected


def test_container_image(monkeypatch):
    monkeypatch.setattr(local, "_run_docker", lambda *a, **k: (0, "postgres:16-alpine\n", ""))
    assert local.container_image("c") == "postgres:16-alpine"
    monkeypatch.setattr(local, "_run_docker", lambda *a, **k: (1, "", "no such"))
    assert local.container_image("c") is None


def test_volume_exists(monkeypatch):
    monkeypatch.setattr(local, "_run_docker", lambda *a, **k: (0, "[]", ""))
    assert local.volume_exists("v") is True
    monkeypatch.setattr(local, "_run_docker", lambda *a, **k: (1, "", "no such volume"))
    assert local.volume_exists("v") is False


def test_port_in_use():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        s.listen()
        port = s.getsockname()[1]
        assert local.port_in_use(port) is True
    # port is released once the socket closes
    assert local.port_in_use(port) is False


# ---------------------------------------------------------------------------
# spec helpers
# ---------------------------------------------------------------------------

def test_specs_for():
    assert [s.engine for s in local.specs_for(None)] == ["postgres", "redis", "neptune"]
    assert [s.engine for s in local.specs_for("all")] == ["postgres", "redis", "neptune"]
    assert [s.engine for s in local.specs_for("redis")] == ["redis"]


def test_spec_url():
    assert local.PG_SPEC.url("shop") == "postgresql://quarry:quarry@localhost:5433/shop"
    assert local.REDIS_SPEC.url("shop") == "redis://localhost:6380/0"
    assert local.REDIS_SPEC.url("shop", redis_db=3) == "redis://localhost:6380/3"
    assert local.NEPTUNE_SPEC.url("graph") == "https://localhost:18182"


def test_redis_db_of():
    assert local.redis_db_of("redis://:pw@10.0.0.5:6379/1") == 1
    assert local.redis_db_of("redis://localhost:6380/0") == 0
    assert local.redis_db_of("redis://localhost:6380") is None
    assert local.redis_db_of("redis://localhost:6380/notanum") is None


def test_docker_run_args():
    pg = local._docker_run_args(local.PG_SPEC, "postgres:16-alpine")
    assert "5433:5432" in pg and "quarry-local-pgdata:/var/lib/postgresql/data" in pg
    assert pg[-1] == "postgres:16-alpine"
    rd = local._docker_run_args(local.REDIS_SPEC, "redis:7-alpine")
    assert "6380:6379" in rd and "quarry-local-redisdata:/data" in rd


# ---------------------------------------------------------------------------
# start_container
# ---------------------------------------------------------------------------

def _no_require(monkeypatch):
    monkeypatch.setattr(local, "require_docker", lambda: None)


def test_start_container_already_running(monkeypatch):
    _no_require(monkeypatch)
    monkeypatch.setattr(local, "container_state", lambda _n: "running")
    assert local.start_container(local.PG_SPEC) == "running"


def test_start_container_resume_stopped(monkeypatch):
    _no_require(monkeypatch)
    monkeypatch.setattr(local, "container_state", lambda _n: "stopped")
    monkeypatch.setattr(local, "_run_docker", lambda *a, **k: (0, "", ""))
    assert local.start_container(local.PG_SPEC) == "started"


def test_start_container_resume_fails(monkeypatch):
    _no_require(monkeypatch)
    monkeypatch.setattr(local, "container_state", lambda _n: "stopped")
    monkeypatch.setattr(local, "_run_docker", lambda *a, **k: (1, "", "boom"))
    with pytest.raises(core.QuarryError):
        local.start_container(local.PG_SPEC)


def test_start_container_port_conflict(monkeypatch):
    _no_require(monkeypatch)
    monkeypatch.setattr(local, "container_state", lambda _n: "absent")
    monkeypatch.setattr(local, "port_in_use", lambda _p: True)
    with pytest.raises(core.QuarryError) as ei:
        local.start_container(local.PG_SPEC)
    assert "already in use" in str(ei.value)


def test_start_container_creates_fresh(monkeypatch):
    _no_require(monkeypatch)
    monkeypatch.setattr(local, "container_state", lambda _n: "absent")
    monkeypatch.setattr(local, "port_in_use", lambda _p: False)
    seen = {}

    def fake(args, *, timeout=60):
        seen["args"] = args
        return (0, "cid", "")
    monkeypatch.setattr(local, "_run_docker", fake)
    assert local.start_container(local.PG_SPEC, image="postgres:17") == "created"
    assert seen["args"][-1] == "postgres:17"


def test_start_container_create_fails(monkeypatch):
    _no_require(monkeypatch)
    monkeypatch.setattr(local, "container_state", lambda _n: "absent")
    monkeypatch.setattr(local, "port_in_use", lambda _p: False)
    monkeypatch.setattr(local, "_run_docker", lambda *a, **k: (1, "", "no image"))
    with pytest.raises(core.QuarryError):
        local.start_container(local.PG_SPEC)


def test_start_container_create_race_port_conflict(monkeypatch):
    # port_in_use's pre-check passes, but another process wins the bind race
    # before `docker run` actually executes — the real docker error must still
    # surface as the friendly "already in use" message, not a raw docker error.
    _no_require(monkeypatch)
    monkeypatch.setattr(local, "container_state", lambda _n: "absent")
    monkeypatch.setattr(local, "port_in_use", lambda _p: False)
    monkeypatch.setattr(
        local, "_run_docker",
        lambda *a, **k: (125, "", "docker: Error response from daemon: "
                          "driver failed programming external connectivity on "
                          "endpoint x: Bind for 0.0.0.0:5433 failed: port is already allocated."))
    with pytest.raises(core.QuarryError) as ei:
        local.start_container(local.PG_SPEC)
    assert "already in use" in str(ei.value)


def test_is_port_conflict():
    assert local._is_port_conflict("port is already allocated", 5433) is True
    assert local._is_port_conflict("bind: address already in use", 5433) is True
    assert local._is_port_conflict("something failed to bind :5433 here", 5433) is True
    assert local._is_port_conflict("no such image", 5433) is False


# ---------------------------------------------------------------------------
# wait_pg_ready / ensure_pg_database
# ---------------------------------------------------------------------------

def test_wait_pg_ready_retries_then_ok(monkeypatch):
    monkeypatch.setattr(local.time, "sleep", lambda _s: None)
    seq = [(1, "", ""), (0, "", "")]
    monkeypatch.setattr(local, "_run_docker", lambda *a, **k: seq.pop(0))
    assert local.wait_pg_ready(local.PG_SPEC, timeout=5) is True


def test_wait_pg_ready_times_out(monkeypatch):
    assert local.wait_pg_ready(local.PG_SPEC, timeout=0) is False


def test_ensure_pg_database_invalid_name():
    with pytest.raises(core.QuarryError):
        local.ensure_pg_database(local.PG_SPEC, "bad-name")


def test_ensure_pg_database_already_present(monkeypatch):
    calls = []

    def fake(args, *, timeout=60):
        calls.append(args)
        return (0, "1\n", "")
    monkeypatch.setattr(local, "_run_docker", fake)
    local.ensure_pg_database(local.PG_SPEC, "shop")
    assert len(calls) == 1  # existence check only, no createdb


def test_ensure_pg_database_creates(monkeypatch):
    def fake(args, *, timeout=60):
        if "createdb" in args:
            return (0, "", "")
        return (0, "", "")  # existence check: empty -> not present
    monkeypatch.setattr(local, "_run_docker", fake)
    local.ensure_pg_database(local.PG_SPEC, "shop")


def test_ensure_pg_database_create_race_tolerated(monkeypatch):
    def fake(args, *, timeout=60):
        if "createdb" in args:
            return (1, "", 'database "shop" already exists')
        return (0, "", "")
    monkeypatch.setattr(local, "_run_docker", fake)
    local.ensure_pg_database(local.PG_SPEC, "shop")  # no raise


def test_ensure_pg_database_create_error(monkeypatch):
    def fake(args, *, timeout=60):
        if "createdb" in args:
            return (1, "", "permission denied")
        return (0, "", "")
    monkeypatch.setattr(local, "_run_docker", fake)
    with pytest.raises(core.QuarryError):
        local.ensure_pg_database(local.PG_SPEC, "shop")


def test_ensure_pg_database_retries_while_server_settles(monkeypatch):
    # first boot of the postgres image: the init-phase server restarts and a
    # connection-level failure is transient — ensure must retry, not raise.
    seq = [
        (2, "", 'psql: error: connection to server on socket '
                '"/var/run/postgresql/.s.PGSQL.5432" failed: No such file or directory'),
        (0, "", ""),   # probe: server up, db absent
        (0, "", ""),   # createdb succeeds
    ]
    monkeypatch.setattr(local, "_run_docker", lambda *a, **k: seq.pop(0))
    monkeypatch.setattr(local.time, "sleep", lambda _s: None)
    local.ensure_pg_database(local.PG_SPEC, "shop")
    assert not seq  # every scripted step consumed


def test_ensure_pg_database_transient_error_gives_up_after_deadline(monkeypatch):
    monkeypatch.setattr(
        local, "_run_docker",
        lambda *a, **k: (2, "", "could not connect to server"))
    with pytest.raises(core.QuarryError) as ei:
        local.ensure_pg_database(local.PG_SPEC, "shop", retry_for=0)
    assert ei.value.exit_code == core.EXIT_CONNECTION_ERROR


def test_is_transient_pg_error():
    assert local._is_transient_pg_error("FATAL: the database system is starting up")
    assert local._is_transient_pg_error(
        'connection to server on socket "/x" failed: No such file or directory')
    assert not local._is_transient_pg_error("permission denied")
    assert not local._is_transient_pg_error("")


# ---------------------------------------------------------------------------
# down_engine
# ---------------------------------------------------------------------------

def test_down_absent_no_purge(monkeypatch):
    _no_require(monkeypatch)
    monkeypatch.setattr(local, "container_state", lambda _n: "absent")
    res = local.down_engine(local.PG_SPEC, purge=False)
    assert res["was"] == "absent" and res["stopped"] is False


def test_down_running_stop(monkeypatch):
    _no_require(monkeypatch)
    monkeypatch.setattr(local, "container_state", lambda _n: "running")
    monkeypatch.setattr(local, "_run_docker", lambda *a, **k: (0, "", ""))
    res = local.down_engine(local.PG_SPEC, purge=False)
    assert res["stopped"] is True and res["removed_volume"] is False


def test_down_stop_fails(monkeypatch):
    _no_require(monkeypatch)
    monkeypatch.setattr(local, "container_state", lambda _n: "running")
    monkeypatch.setattr(local, "_run_docker", lambda *a, **k: (1, "", "boom"))
    with pytest.raises(core.QuarryError):
        local.down_engine(local.PG_SPEC, purge=False)


def test_down_purge_removes_volume(monkeypatch):
    _no_require(monkeypatch)
    monkeypatch.setattr(local, "container_state", lambda _n: "stopped")
    monkeypatch.setattr(local, "volume_exists", lambda _v: True)
    monkeypatch.setattr(local, "_run_docker", lambda *a, **k: (0, "", ""))
    res = local.down_engine(local.PG_SPEC, purge=True)
    assert res["purged"] is True and res["removed_volume"] is True


def test_down_purge_no_volume(monkeypatch):
    _no_require(monkeypatch)
    monkeypatch.setattr(local, "container_state", lambda _n: "absent")
    monkeypatch.setattr(local, "volume_exists", lambda _v: False)
    monkeypatch.setattr(local, "_run_docker", lambda *a, **k: (0, "", ""))
    res = local.down_engine(local.PG_SPEC, purge=True)
    assert res["removed_volume"] is False


def test_down_purge_volume_rm_fails(monkeypatch):
    _no_require(monkeypatch)
    monkeypatch.setattr(local, "container_state", lambda _n: "stopped")
    monkeypatch.setattr(local, "volume_exists", lambda _v: True)

    def fake(args, *, timeout=60):
        if args[:2] == ["volume", "rm"]:
            return (1, "", "volume in use")
        return (0, "", "")
    monkeypatch.setattr(local, "_run_docker", fake)
    with pytest.raises(core.QuarryError):
        local.down_engine(local.PG_SPEC, purge=True)


# ---------------------------------------------------------------------------
# engine_status
# ---------------------------------------------------------------------------

def test_engine_status_no_docker(monkeypatch):
    monkeypatch.setattr(local, "docker_available", lambda: False)
    st = local.engine_status(local.PG_SPEC)
    assert st["docker"] is False and st["running"] is False


def test_engine_status_running(monkeypatch):
    monkeypatch.setattr(local, "docker_available", lambda: True)
    monkeypatch.setattr(local, "container_state", lambda _n: "running")
    monkeypatch.setattr(local, "container_image", lambda _n: "postgres:16-alpine")
    monkeypatch.setattr(local, "volume_exists", lambda _v: True)
    st = local.engine_status(local.PG_SPEC)
    assert st["running"] is True and st["image"] == "postgres:16-alpine"


def test_engine_status_absent(monkeypatch):
    monkeypatch.setattr(local, "docker_available", lambda: True)
    monkeypatch.setattr(local, "container_state", lambda _n: "absent")
    monkeypatch.setattr(local, "volume_exists", lambda _v: False)
    st = local.engine_status(local.PG_SPEC)
    assert st["running"] is False and st["image"] is None


# ---------------------------------------------------------------------------
# connection registration
# ---------------------------------------------------------------------------

def test_register_creates_local_connection(local_ws):
    key, created = local.register_local_connection(
        "shop", local.PG_SPEC, image="postgres:17", group="commerce")
    assert created is True
    data = _read_conns(local_ws)
    assert key == "shop_local"
    reg = data[key]
    assert reg["env"] == "local" and reg["db"] == "shop"
    assert reg["url"] == "postgresql://quarry:quarry@localhost:5433/shop"
    assert reg["local_image"] == "postgres:17"
    assert reg["local_volume"] == "quarry-local-pgdata"
    assert reg["group"] == "commerce"
    # the pre-existing dev connection is untouched
    assert data["shop"]["url"] == "postgresql://dev-host/shop"


def test_register_is_idempotent(local_ws):
    key1, c1 = local.register_local_connection("shop", local.PG_SPEC)
    # user hand-edits the local url; a second up must NOT overwrite it
    header, data = core._read_connections_file_parts()
    data[key1]["url"] = "postgresql://quarry:quarry@localhost:5433/custom"
    core._write_connections_file(header, data)
    key2, c2 = local.register_local_connection("shop", local.PG_SPEC)
    assert c1 is True and c2 is False and key1 == key2
    assert _read_conns(local_ws)[key1]["url"].endswith("/custom")


def test_pick_local_key_avoids_collision(local_ws):
    header, data = core._read_connections_file_parts()
    # non-local connections already squat on both natural key names
    data["shop_local"] = {"url": "postgresql://x/y", "engine": "postgres", "env": "dev"}
    data["shop_local2"] = {"url": "postgresql://x/z", "engine": "postgres", "env": "dev"}
    core._write_connections_file(header, data)
    key, created = local.register_local_connection("shop", local.PG_SPEC)
    assert created is True and key == "shop_local3"


def test_register_preserves_non_string_fields(local_ws):
    # an existing connection carries a numeric field (e.g. an SSH tunnel port).
    header, data = core._read_connections_file_parts()
    data["shop"]["ssh_port"] = 2222
    core._write_connections_file(header, data)
    local.register_local_connection("shop", local.PG_SPEC)
    reg = _read_conns(local_ws)
    # auto-registering the local connection must not drop/mangle the numeric field
    assert reg["shop"]["ssh_port"] == 2222
    assert isinstance(reg["shop"]["ssh_port"], int)


def test_stored_local_image(local_ws):
    assert local.stored_local_image("shop") is None
    local.register_local_connection("shop", local.PG_SPEC, image="postgres:15-alpine")
    assert local.stored_local_image("shop") == "postgres:15-alpine"


def test_register_redis_carries_source_db_index(local_ws):
    header, data = core._read_connections_file_parts()
    data["cache"] = {"url": "redis://:pw@10.0.0.5:6379/2", "engine": "redis", "env": "dev"}
    core._write_connections_file(header, data)
    assert local.source_redis_db("cache") == 2
    key, created = local.register_local_connection(
        "cache", local.REDIS_SPEC, redis_db=local.source_redis_db("cache"))
    assert created is True
    assert _read_conns(local_ws)[key]["url"] == "redis://localhost:6380/2"


def test_register_neptune_empty_connection(local_ws):
    key, created = local.register_local_connection("neptune", local.NEPTUNE_SPEC)
    assert created is True
    assert key == "neptune_local"
    reg = _read_conns(local_ws)[key]
    assert reg["url"] == "https://localhost:18182"
    assert reg["engine"] == "neptune"
    assert reg["local_backend"] == "empty"
    assert "local_volume" not in reg


def test_source_redis_db_prefers_default_env(local_ws):
    header, data = core._read_connections_file_parts()
    data["cache_jp"] = {"url": "redis://jp-host:6379/5", "engine": "redis",
                        "env": "jp", "db": "cache"}
    data["cache_dev"] = {"url": "redis://dev-host:6379/1", "engine": "redis",
                         "env": "dev", "db": "cache"}
    core._write_connections_file(header, data)
    assert local.source_redis_db("cache") == 1


def test_source_redis_db_ignores_local_and_non_redis(local_ws):
    header, data = core._read_connections_file_parts()
    data["cache_local"] = {"url": "redis://localhost:6380/4", "engine": "redis",
                           "env": "local", "db": "cache"}
    core._write_connections_file(header, data)
    # only a local member exists -> no source index; "shop" is postgres-only
    assert local.source_redis_db("cache") is None
    assert local.source_redis_db("shop") is None


def test_existing_local_key_matches_by_db_field():
    data = {
        "shop": {"url": "u", "env": "dev"},
        "shop_local": {"url": "u", "env": "local", "db": "shop"},
    }
    assert local.existing_local_key(data, "shop") == "shop_local"
    assert local.existing_local_key(data, "other") is None


def test_existing_local_key_survives_manual_db_field_edit():
    # user renamed [shop_local]'s `db` field for clarity; the connection is
    # still found via its (stable) key name, so a repeat `up shop` doesn't
    # spawn a duplicate `shop_local2` pointing at the same data.
    data = {
        "shop": {"url": "u", "env": "dev"},
        "shop_local": {"url": "u", "env": "local", "db": "shop_dev_mirror"},
    }
    assert local.existing_local_key(data, "shop") == "shop_local"


def test_existing_local_key_falls_back_to_content_match():
    # no key named "shop_local" exists (e.g. it collided and got a numbered
    # suffix) — matching by db/env content is the fallback for that case.
    data = {"shop_local2": {"url": "u", "env": "local", "db": "shop"}}
    assert local.existing_local_key(data, "shop") == "shop_local2"


# ---------------------------------------------------------------------------
# CLI handlers (cmd_local_up / down / status)
# ---------------------------------------------------------------------------

from quarry import cli  # noqa: E402


def test_resolve_target_known_connection(local_ws):
    logical, spec, group = cli._resolve_local_target("shop", None)
    assert logical == "shop" and spec.engine == "postgres" and group == "commerce"


def test_resolve_target_exact_key_beats_logical_db(local_ws):
    # `analytics` has logical db "shop" and sits *before* the explicit `shop` key;
    # `up shop` must resolve to the exact key, not the earlier logical-db collision.
    (local_ws / "connections.toml").write_text(
        '[analytics]\nurl = "postgresql://dev-host/shop"\nengine = "postgres"\n'
        'env = "dev"\ndb = "shop"\n\n'
        '[shop]\nurl = "redis://dev-host:6379"\nengine = "redis"\nenv = "dev"\n',
        encoding="utf-8",
    )
    logical, spec, group = cli._resolve_local_target("shop", None)
    assert logical == "shop" and spec.engine == "redis"


def test_resolve_target_engine_mismatch(local_ws):
    with pytest.raises(core.QuarryError):
        cli._resolve_local_target("shop", "redis")


def test_resolve_target_logical_db_prefers_default_env(local_ws):
    # No key is literally named "shop2"; two env-set members share that logical
    # db. Resolution must prefer DEFAULT_ENV ("dev"), same as `qy run shop2`
    # would, not "whichever sits first in the file" (here that's the jp member).
    (local_ws / "connections.toml").write_text(
        '[shop2_jp]\nurl = "postgresql://jp-host/shop2"\nengine = "postgres"\n'
        'env = "jp"\ndb = "shop2"\ngroup = "jp-group"\n\n'
        '[shop2_dev]\nurl = "postgresql://dev-host/shop2"\nengine = "postgres"\n'
        'env = "dev"\ndb = "shop2"\ngroup = "dev-group"\n',
        encoding="utf-8",
    )
    logical, spec, group = cli._resolve_local_target("shop2", None)
    assert logical == "shop2" and group == "dev-group"


def test_resolve_target_unknown_defaults_postgres(local_ws):
    logical, spec, group = cli._resolve_local_target("fresh", "all")
    assert logical == "fresh" and spec.engine == "postgres" and group is None


def test_resolve_target_unknown_redis(local_ws):
    _, spec, _ = cli._resolve_local_target("cache", "redis")
    assert spec.engine == "redis"


def test_resolve_target_unknown_neptune(local_ws):
    _, spec, _ = cli._resolve_local_target("neptune", "neptune")
    assert spec.engine == "neptune"


def test_resolve_target_invalid_name(local_ws):
    with pytest.raises(core.QuarryError):
        cli._resolve_local_target("bad-name", None)


def test_cmd_local_up_no_key(monkeypatch, capsys):
    monkeypatch.setattr(local, "start_container", lambda spec, image=None: "created")
    monkeypatch.setattr(local, "container_image", lambda _name: "redis:7-alpine")
    args = argparse.Namespace(key=None, engine="redis", image=None)
    assert cli.cmd_local_up(args) == core.EXIT_OK
    out = capsys.readouterr().out
    assert "redis" in out and "created" in out


def test_cmd_local_up_neptune(monkeypatch, capsys, local_ws):
    monkeypatch.setattr(local, "start_container", lambda spec, image=None: "created")
    args = argparse.Namespace(key="neptune", engine="neptune", image=None)
    assert cli.cmd_local_up(args) == core.EXIT_OK
    out = capsys.readouterr().out
    assert "Neptune empty endpoint created" in out
    assert _read_conns(local_ws)["neptune_local"]["local_backend"] == "empty"


def test_cmd_local_up_with_key_postgres(monkeypatch, capsys, local_ws):
    monkeypatch.setattr(local, "start_container", lambda spec, image=None: "created")
    monkeypatch.setattr(local, "container_image", lambda _name: "postgres:16-alpine")
    monkeypatch.setattr(local, "wait_pg_ready", lambda spec: True)
    monkeypatch.setattr(local, "ensure_pg_database", lambda spec, db: None)
    args = argparse.Namespace(key="shop", engine=None, image=None)
    assert cli.cmd_local_up(args) == core.EXIT_OK
    out = capsys.readouterr().out
    assert "registered connection [shop_local]" in out
    assert _read_conns(local_ws)["shop_local"]["env"] == "local"


def test_cmd_local_up_registers_before_readiness_wait(monkeypatch, local_ws):
    # A container that starts but never becomes ready must still end up
    # registered — otherwise a retried `up` looks like a fresh, unregistered run.
    monkeypatch.setattr(local, "start_container", lambda spec, image=None: "created")
    monkeypatch.setattr(local, "container_image", lambda _name: "postgres:16-alpine")
    monkeypatch.setattr(local, "wait_pg_ready", lambda spec: False)
    args = argparse.Namespace(key="shop", engine=None, image=None)
    with pytest.raises(core.QuarryError):
        cli.cmd_local_up(args)
    assert _read_conns(local_ws)["shop_local"]["env"] == "local"


def test_cmd_local_up_already_registered(monkeypatch, capsys, local_ws):
    local.register_local_connection("shop", local.PG_SPEC)
    monkeypatch.setattr(local, "start_container", lambda spec, image=None: "running")
    monkeypatch.setattr(local, "container_image", lambda _name: "postgres:16-alpine")
    monkeypatch.setattr(local, "wait_pg_ready", lambda spec: True)
    monkeypatch.setattr(local, "ensure_pg_database", lambda spec, db: None)
    args = argparse.Namespace(key="shop", engine=None, image=None)
    assert cli.cmd_local_up(args) == core.EXIT_OK
    assert "already registered" in capsys.readouterr().out


def test_cmd_local_down_purge(monkeypatch, capsys):
    monkeypatch.setattr(local, "down_engine",
                        lambda spec, purge: {"engine": spec.engine, "was": "running",
                                             "stopped": True, "purged": True,
                                             "removed_volume": True})
    args = argparse.Namespace(engine="postgres", purge=True)
    assert cli.cmd_local_down(args) == core.EXIT_OK
    out = capsys.readouterr().out
    assert "removed" in out and "destroyed" in out


def test_cmd_local_down_absent(monkeypatch, capsys):
    monkeypatch.setattr(local, "down_engine",
                        lambda spec, purge: {"engine": spec.engine, "was": "absent",
                                             "stopped": False, "purged": False,
                                             "removed_volume": False})
    args = argparse.Namespace(engine="redis", purge=False)
    assert cli.cmd_local_down(args) == core.EXIT_OK
    assert "not present" in capsys.readouterr().out


def test_cmd_local_down_stop_keeps_volume(monkeypatch, capsys):
    monkeypatch.setattr(local, "down_engine",
                        lambda spec, purge: {"engine": spec.engine, "was": "running",
                                             "stopped": True, "purged": False,
                                             "removed_volume": False})
    args = argparse.Namespace(engine="postgres", purge=False)
    assert cli.cmd_local_down(args) == core.EXIT_OK
    assert "stopped" in capsys.readouterr().out


def test_cmd_local_down_purge_no_volume(monkeypatch, capsys):
    monkeypatch.setattr(local, "down_engine",
                        lambda spec, purge: {"engine": spec.engine, "was": "stopped",
                                             "stopped": False, "purged": True,
                                             "removed_volume": False})
    args = argparse.Namespace(engine="postgres", purge=True)
    assert cli.cmd_local_down(args) == core.EXIT_OK
    assert "did not exist" in capsys.readouterr().out


def test_cmd_local_status_text(monkeypatch, capsys):
    monkeypatch.setattr(local, "engine_status", lambda spec: {
        "engine": spec.engine, "docker": True,
        "running": spec.engine == "postgres", "state": "running",
        "port": spec.port, "image": "postgres:16-alpine",
        "volume": spec.volume, "volume_exists": True})
    args = argparse.Namespace(engine="all", format="text")
    assert cli.cmd_local_status(args) == core.EXIT_OK
    out = capsys.readouterr().out
    assert "running" in out and "not running" in out


def test_cmd_local_status_no_docker(monkeypatch, capsys):
    monkeypatch.setattr(local, "engine_status", lambda spec: {
        "engine": spec.engine, "docker": False, "running": False,
        "state": "unknown", "port": spec.port, "image": None,
        "volume": spec.volume, "volume_exists": False})
    args = argparse.Namespace(engine="all", format="text")
    assert cli.cmd_local_status(args) == core.EXIT_OK
    assert "docker unavailable" in capsys.readouterr().out


def test_cmd_local_status_json(monkeypatch, capsys):
    monkeypatch.setattr(local, "engine_status", lambda spec: {
        "engine": spec.engine, "docker": True, "running": False,
        "state": "stopped", "port": spec.port, "image": None,
        "volume": spec.volume, "volume_exists": True})
    args = argparse.Namespace(engine="postgres", format="json")
    assert cli.cmd_local_status(args) == core.EXIT_OK
    import json
    data = json.loads(capsys.readouterr().out)
    assert data[0]["engine"] == "postgres"
