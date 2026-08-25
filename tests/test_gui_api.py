"""GUI HTTP API tests — drive the real ThreadingHTTPServer on an ephemeral port.

Split into two layers:
  * pure-unit tests of the backend helpers (no server, no DB)
  * e2e tests through the running server (need Postgres)
"""

from __future__ import annotations

import pytest

from conftest import requires_db


# ---------------------------------------------------------------------------
# unit — backend helpers
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_display_path_redacts_home(monkeypatch, tmp_path):
    from pathlib import Path

    from quarry import gui
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert gui._display_path(tmp_path / "ws" / "a") == "~/ws/a"
    assert gui._display_path("/opt/other") == "/opt/other"


@pytest.mark.unit
def test_is_quarry_gui_rejects_foreign_gui(monkeypatch):
    """A foreign process whose command merely ends in 'gui' must NOT be reclaimed."""
    import subprocess

    from quarry import gui

    def fake_run(cmd, **kw):
        pid = cmd[cmd.index("-p") + 1]
        table = {
            "1": "node /app/server.js gui",           # foreign -> not ours
            "2": "python -m quarry.gui",               # ours
            "3": "/usr/local/bin/qy gui --port 8765",  # ours
            "4": "make gui",                           # foreign -> not ours
        }
        return subprocess.CompletedProcess(cmd, 0, stdout=table.get(pid, ""), stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert gui._is_quarry_gui(1) is False
    assert gui._is_quarry_gui(2) is True
    assert gui._is_quarry_gui(3) is True
    assert gui._is_quarry_gui(4) is False


@pytest.mark.unit
def test_health_freshness_ttl(monkeypatch):
    from quarry import gui
    monkeypatch.setattr(gui.core, "HEALTH_TTL_SEC", 120)
    now = 1_000_000.0
    monkeypatch.setattr(gui.time, "time", lambda: now)
    assert gui.core._health_fresh_enough({"ok": True, "_ts": now - 10}) is True
    assert gui.core._health_fresh_enough({"ok": True, "_ts": now - 200}) is False
    assert gui.core._health_fresh_enough({"ok": True}) is False  # legacy entry, no _ts


@pytest.mark.unit
def test_api_keepalive_status_and_controls(monkeypatch):
    from quarry import gui

    state = {"running": False}

    def fake_status(_ws):
        return {
            "workspace": "/tmp/ws",
            "enabled": True,
            "reconnect": True,
            "keeper": {"running": state["running"], "pid": 4242 if state["running"] else None},
            "tunnels": [],
            "updatedAt": 0.0,
        }

    monkeypatch.setattr(gui.keepalive, "status", fake_status)
    monkeypatch.setattr(gui.keepalive, "start", lambda _ws: state.__setitem__("running", True) or (True, 4242))
    monkeypatch.setattr(gui.keepalive, "stop", lambda _ws: state.__setitem__("running", False) or True)

    down = gui.api_keepalive()
    assert down["keeper"]["running"] is False
    up = gui.api_keepalive_up()
    assert up["keeper"]["running"] is True
    back = gui.api_keepalive_down()
    assert back["keeper"]["running"] is False


# ---------------------------------------------------------------------------
# e2e — through the server
# ---------------------------------------------------------------------------

@requires_db
@pytest.mark.integration
def test_connections_endpoint(gui_server):
    code, body = gui_server.get("/api/connections")
    assert code == 200
    dbs = [it["db"] for g in body["groups"] for it in g["items"]]
    assert "testpg" in dbs


@requires_db
@pytest.mark.integration
def test_update_endpoint_reads_cache_without_hitting_pypi(gui_server):
    """GET /api/update never triggers a PyPI call itself (the background
    checker owns that); against a fresh cache it reports the current version
    with no known update."""
    from quarry import __version__

    code, body = gui_server.get("/api/update")
    assert code == 200
    assert body == {"current": __version__, "latest": None, "available": False}


@requires_db
@pytest.mark.integration
def test_changelog_endpoint_returns_parsed_versions(gui_server):
    """GET /api/changelog serves the real CHANGELOG.md, parsed into
    structured version sections (used by the header's What's New panel)."""
    code, body = gui_server.get("/api/changelog")
    assert code == 200
    assert isinstance(body, list) and body
    first = body[0]
    assert {"version", "date", "entries"} <= first.keys()
    assert isinstance(first["entries"], list)


@requires_db
@pytest.mark.integration
def test_tables_cache_lifecycle(gui_server):
    code, first = gui_server.get("/api/tables?db=testpg&env=test&fresh=1")
    assert code == 200 and first["_cached"] is False
    assert "customers" in first["tables"] and "orders" in first["tables"]
    code, second = gui_server.get("/api/tables?db=testpg&env=test")
    assert second["_cached"] is True  # served from cache on the second hit


@requires_db
@pytest.mark.integration
def test_query_returns_typed_rows(gui_server):
    code, body = gui_server.post("/api/query",
                                 {"db": "testpg", "env": "test", "sql": "SELECT * FROM customers"})
    assert code == 200
    assert body["rowCount"] == 3
    assert {c["name"] for c in body["columns"]} >= {"id", "name", "email"}


@requires_db
@pytest.mark.integration
def test_query_write_blocked(gui_server):
    code, body = gui_server.post("/api/query",
                                 {"db": "testpg", "env": "test", "sql": "DELETE FROM customers"})
    assert code == 400 and body["code"] == 8


@requires_db
@pytest.mark.integration
def test_query_multi_statement_blocked(gui_server):
    code, body = gui_server.post(
        "/api/query", {"db": "testpg", "env": "test", "sql": "SELECT 1; DROP TABLE customers"})
    assert code == 400 and body["code"] == 8


@requires_db
@pytest.mark.integration
def test_run_saved_query_with_params(gui_server, tmp_path):
    (tmp_path / "queries" / "top.sql").write_text(
        "-- @name: top\n-- @db: testpg\n-- @param: n (int, default=5)\n"
        "SELECT name FROM customers ORDER BY id LIMIT :n\n", encoding="utf-8")
    code, body = gui_server.post("/api/run", {"name": "top", "env": "test", "params": {"n": "2"}})
    assert code == 200 and body["rowCount"] == 2


@requires_db
@pytest.mark.integration
def test_health_probe(gui_server):
    code, body = gui_server.get("/api/health?db=testpg&env=test&fresh=1")
    assert code == 200 and body == {"ok": True}       # no _ts leaked into the response
    code, cached = gui_server.get("/api/health?db=testpg&env=test&cached=1")
    assert cached["ok"] is True                        # painted from cache without probing


@requires_db
@pytest.mark.integration
def test_columns_endpoint_and_injection_safe(gui_server):
    code, body = gui_server.get("/api/columns?db=testpg&env=test&table=customers")
    assert code == 200 and "email" in body["columns"]
    assert body["types"]["email"] == "text"
    # a malicious table name is bound as a query parameter (not spliced into SQL text)
    # -> matches no real table -> empty result, never an error or an injected statement.
    code, evil = gui_server.get("/api/columns?db=testpg&env=test&table=x%27%3B%20DROP--")
    assert code == 200 and evil == {"columns": [], "types": {}}


@requires_db
@pytest.mark.integration
def test_inspect_rejects_non_redis(gui_server):
    code, body = gui_server.get("/api/inspect?db=testpg&env=test&key=foo")
    assert code == 400 and "redis-only" in body["error"]


@requires_db
@pytest.mark.integration
def test_query_missing_field_is_clean_400(gui_server):
    code, body = gui_server.post("/api/query", {})
    assert code == 400 and "db" in body["error"]


@requires_db
@pytest.mark.integration
def test_query_bad_maxrows_is_clean_400(gui_server):
    code, body = gui_server.post("/api/query",
                                 {"db": "testpg", "env": "test", "sql": "SELECT 1", "maxRows": "abc"})
    assert code == 400 and "maxRows" in body["error"]


@requires_db
@pytest.mark.integration
def test_query_bad_offset_is_clean_400(gui_server):
    code, body = gui_server.post("/api/query",
                                 {"db": "testpg", "env": "test", "sql": "SELECT 1", "offset": "abc"})
    assert code == 400 and "offset" in body["error"]


@requires_db
@pytest.mark.integration
def test_query_offset_pages_through_results(gui_server):
    # grid "load more": page 1 is truncated, page 2 (offset=maxRows) finishes the set
    code, page1 = gui_server.post("/api/query", {
        "db": "testpg", "env": "test", "sql": "SELECT id FROM customers ORDER BY id", "maxRows": 2})
    assert code == 200 and page1["rowCount"] == 2 and page1["truncated"] is True
    code, page2 = gui_server.post("/api/query", {
        "db": "testpg", "env": "test", "sql": "SELECT id FROM customers ORDER BY id",
        "maxRows": 2, "offset": 2})
    assert code == 200 and [r["id"] for r in page2["rows"]] == [3]
    assert page2["truncated"] is False


@requires_db
@pytest.mark.integration
def test_unknown_db_is_400(gui_server):
    code, body = gui_server.get("/api/tables?db=ghost&env=")
    assert code == 400 and "unknown db" in body["error"]


@requires_db
@pytest.mark.integration
def test_not_found_is_404(gui_server):
    code, _ = gui_server.get("/api/nope")
    assert code == 404


# ---------------------------------------------------------------------------
# e2e — origin / host gate (security). No DB needed but grouped with the server.
# ---------------------------------------------------------------------------

@requires_db
@pytest.mark.integration
def test_foreign_host_rejected(gui_server):
    code, body = gui_server.get("/api/connections", headers={"Host": "evil.example"})
    assert code == 403


@requires_db
@pytest.mark.integration
def test_foreign_origin_rejected(gui_server):
    code, body = gui_server.post("/api/query",
                                 {"db": "testpg", "sql": "SELECT 1"},
                                 headers={"Origin": "http://evil.example"})
    assert code == 403


@requires_db
@pytest.mark.integration
def test_root_redirects_to_react_app(gui_server):
    """`/` (and `/index.html`) is the only landing page shipped now, so it
    redirects to the React app at `/app/` rather than serving HTML itself."""
    import urllib.request
    with urllib.request.urlopen(gui_server.base + "/", timeout=10) as r:
        html = r.read().decode()
    assert r.status == 200 and r.geturl() == gui_server.base + "/app/"
    assert "<title>Quarry</title>" in html


@requires_db
@pytest.mark.integration
def test_gui_html_and_favicon_asset(gui_server):
    import urllib.request
    with urllib.request.urlopen(gui_server.base + "/app/", timeout=10) as r:
        html = r.read().decode()
    assert '<link rel="icon" type="image/svg+xml" href="/app/favicon.svg"' in html

    with urllib.request.urlopen(gui_server.base + "/app/favicon.svg", timeout=10) as r:
        favicon = r.read().decode()
        content_type = r.headers.get_content_type()
    assert content_type == "image/svg+xml"
    assert "<text" not in favicon
    assert 'viewBox="0 0 64 64"' in favicon


# ---------------------------------------------------------------------------
# connection info (/api/conninfo) — resolved config, password always masked
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_mask_url_variants():
    from quarry import gui
    assert gui._mask_url("postgresql://u:secret@h:5432/db") == "postgresql://u:••••@h:5432/db"
    assert gui._mask_url("redis://:secret@h:6379/1") == "redis://:••••@h:6379/1"
    # no credentials -> unchanged
    assert gui._mask_url("postgresql://localhost:5432/db") == "postgresql://localhost:5432/db"
    assert gui._mask_url("https://ep.neptune.amazonaws.com:8182") == (
        "https://ep.neptune.amazonaws.com:8182")


@pytest.mark.unit
def test_api_conninfo_resolves_and_masks(tmp_path, monkeypatch):
    """The info panel shows what quarry will actually dial — including the SSH
    tunnel and which connections.toml the entry came from — but never the password."""
    from pathlib import Path

    from quarry import gui, workspace

    (tmp_path / "connections.toml").write_text(
        '[shop_dev]\nurl = "postgresql://app:hunter2@db.internal:5433/shopdb"\n'
        'engine = "postgres"\nenv = "dev"\ndb = "shop"\ngroup = "acme"\n'
        'ssh_host = "bastion.example.com"\nssh_user = "ec2-user"\nssh_port = 2222\n',
        encoding="utf-8",
    )
    (tmp_path / "queries").mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    workspace.configure_workspace(str(tmp_path))
    try:
        info = gui.api_conninfo("shop", "dev")
    finally:
        workspace.configure_workspace(None)
    assert info["key"] == "shop_dev" and info["engine"] == "postgres"
    assert info["host"] == "db.internal" and info["port"] == 5433
    assert info["database"] == "shopdb" and info["group"] == "acme"
    assert "hunter2" not in info["url"] and info["url"].startswith("postgresql://app:")
    assert info["tunnel"] == {"host": "bastion.example.com", "user": "ec2-user",
                              "port": 2222, "key": None}
    assert info["file"].endswith("connections.toml")
    assert "hunter2" not in str(info)  # nothing anywhere in the payload leaks it


# ---------------------------------------------------------------------------
# proxy status (issue #101) — GUI reports observed proxy state, not a guess
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_api_connections_marks_env_as_proxied_when_a_live_tunnel_is_actually_proxied(tmp_path, monkeypatch):
    """issue #101 r1-2: the badge must reflect an *actually established*
    proxied tunnel (tunnel.list_tunnels() fact), not merely that a fresh
    connection attempt would currently choose to proxy — so this fakes the
    tunnel pool directly instead of the proxy-discovery plumbing."""
    from pathlib import Path

    from quarry import gui, tunnel, workspace

    (tmp_path / "connections.toml").write_text(
        '[shop_dev]\nurl = "postgresql://app@db.internal:5432/shopdb"\n'
        'engine = "postgres"\nenv = "dev"\ndb = "shop"\n'
        'ssh_host = "bastion.example.com"\nssh_user = "ec2-user"\n'
        '\n[shop_prod]\nurl = "postgresql://app@db.internal:5432/shopdb"\n'
        'engine = "postgres"\nenv = "prod"\ndb = "shop"\n'
        'ssh_host = "bastion.example.com"\nssh_user = "ec2-user"\n',
        encoding="utf-8",
    )
    (tmp_path / "queries").mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    live_tunnel = {
        "ssh_target": "ec2-user@bastion.example.com:22",
        "db_target": "db.internal:5432",
        "local_port": 15000,
        "proxied": True,
        "proxy": "127.0.0.1:6152",
        "alive": True,
    }
    monkeypatch.setattr(tunnel, "list_tunnels", lambda: [live_tunnel])
    workspace.configure_workspace(str(tmp_path))
    try:
        out = gui.api_connections()
    finally:
        workspace.configure_workspace(None)
    envs = [e for g in out["groups"] for it in g["items"] for e in it["envs"]]
    assert {e["env"]: e["proxied"] for e in envs} == {"dev": True, "prod": True}


@pytest.mark.unit
def test_api_connections_marks_env_as_not_proxied_when_no_tunnel_established_yet(tmp_path, monkeypatch):
    """issue #101 r1-2: before any query has run against an env (no tunnel in
    the pool at all), the badge must stay off — even if the workspace toggle
    is on and a proxy is discoverable — since nothing is actually tunneled."""
    from pathlib import Path

    from quarry import gui, proxy, tunnel, workspace

    (tmp_path / "connections.toml").write_text(
        '[shop_dev]\nurl = "postgresql://app@db.internal:5432/shopdb"\n'
        'engine = "postgres"\nenv = "dev"\ndb = "shop"\n'
        'ssh_host = "bastion.example.com"\nssh_user = "ec2-user"\n',
        encoding="utf-8",
    )
    (tmp_path / "queries").mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(workspace, "is_proxy_enabled", lambda home: True)
    monkeypatch.setattr(proxy, "discover_proxy", lambda: proxy.ProxyInfo(host="127.0.0.1", port=6152, source="system"))
    monkeypatch.setattr(proxy, "_port_listening", lambda host, port, timeout=0.3: True)
    monkeypatch.setattr(tunnel, "list_tunnels", lambda: [])
    workspace.configure_workspace(str(tmp_path))
    try:
        out = gui.api_connections()
    finally:
        workspace.configure_workspace(None)
    envs = [e for g in out["groups"] for it in g["items"] for e in it["envs"]]
    assert envs[0]["proxied"] is False


@pytest.mark.unit
def test_api_connections_marks_env_as_not_proxied_when_proxy_unreachable(tmp_path, monkeypatch):
    from pathlib import Path

    from quarry import gui, proxy, workspace

    (tmp_path / "connections.toml").write_text(
        '[shop_dev]\nurl = "postgresql://app@db.internal:5432/shopdb"\n'
        'engine = "postgres"\nenv = "dev"\ndb = "shop"\n'
        'ssh_host = "bastion.example.com"\nssh_user = "ec2-user"\n',
        encoding="utf-8",
    )
    (tmp_path / "queries").mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(workspace, "is_proxy_enabled", lambda home: True)
    monkeypatch.setattr(proxy, "discover_proxy", lambda: None)
    workspace.configure_workspace(str(tmp_path))
    try:
        out = gui.api_connections()
    finally:
        workspace.configure_workspace(None)
    envs = [e for g in out["groups"] for it in g["items"] for e in it["envs"]]
    assert envs[0]["proxied"] is False


@pytest.mark.unit
def test_api_workspaces_reports_proxy_toggle_and_discovered_proxy(tmp_path, monkeypatch):
    from quarry import gui, proxy, workspace

    good = tmp_path / "good_ws"
    good.mkdir()
    (good / "connections.toml").write_text("", encoding="utf-8")
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'workspaces = ["{good}"]\n', encoding="utf-8")
    monkeypatch.setenv("QUARRY_CONFIG", str(cfg))
    monkeypatch.setattr(workspace, "is_proxy_enabled", lambda home: str(home) == str(good))
    monkeypatch.setattr(proxy, "discover_proxy", lambda: proxy.ProxyInfo(host="127.0.0.1", port=6152, source="system"))

    out = gui.api_workspaces()
    assert out["items"][0]["proxyEnabled"] is True
    assert out["proxyDiscovered"] == {"host": "127.0.0.1", "port": 6152, "source": "system"}


@pytest.mark.unit
def test_api_workspaces_proxy_discovered_none_when_nothing_found(tmp_path, monkeypatch):
    from quarry import gui, proxy

    monkeypatch.setenv("QUARRY_CONFIG", str(tmp_path / "no-such-config.toml"))
    monkeypatch.setattr(proxy, "discover_proxy", lambda: None)
    assert gui.api_workspaces()["proxyDiscovered"] is None


@requires_db
@pytest.mark.integration
def test_conninfo_endpoint(gui_server):
    code, body = gui_server.get("/api/conninfo?db=testpg&env=test")
    assert code == 200
    assert body["key"] == "testpg" and body["engine"] == "postgres"
    assert body["database"] and body["host"]
    assert body["tunnel"] is None
    assert body["file"].endswith("connections.toml")


@requires_db
@pytest.mark.integration
def test_conninfo_unknown_connection_is_readable_error(gui_server):
    code, body = gui_server.get("/api/conninfo?db=nope&env=test")
    assert code == 400 and "error" in body


@pytest.mark.unit
def test_api_conninfo_reveal_returns_real_url(tmp_path, monkeypatch):
    from quarry import gui, workspace

    (tmp_path / "connections.toml").write_text(
        '[shop_dev]\nurl = "postgresql://app:hunter2@localhost:5433/shopdb"\n'
        'engine = "postgres"\nenv = "dev"\ndb = "shop"\n',
        encoding="utf-8",
    )
    (tmp_path / "queries").mkdir()
    workspace.configure_workspace(str(tmp_path))
    try:
        masked = gui.api_conninfo("shop", "dev")
        revealed = gui.api_conninfo("shop", "dev", reveal=True)
    finally:
        workspace.configure_workspace(None)
    assert "hunter2" not in masked["url"]
    assert revealed["url"] == "postgresql://app:hunter2@localhost:5433/shopdb"


@requires_db
@pytest.mark.integration
def test_local_up_endpoint_unknown_db(gui_server):
    code, body = gui_server.post("/api/local/up", {"db": "nope"})
    assert code == 400 and "unknown connection" in body["error"]


@requires_db
@pytest.mark.integration
def test_local_sync_endpoint_refuses_non_local(gui_server):
    """The env-set has no local member — resolve falls back to the remote
    connection and the sync gate must refuse (same invariant as the CLI)."""
    code, body = gui_server.post("/api/local/sync", {"db": "testpg"})
    assert code == 400
    assert body["code"] == core_exit_sync_denied()
    assert "sync refused" in body["error"]


def core_exit_sync_denied():
    from quarry import core
    return core.EXIT_SYNC_DENIED


@pytest.mark.unit
def test_api_local_up_orchestration(tmp_path, monkeypatch):
    """The GUI endpoint mirrors `qy local up <db>`: container started, local
    connection registered with the source's group. Container/docker behavior
    itself is covered by test_local_docker.py — here the seams are mocked."""
    import tomllib

    from quarry import gui, local, workspace

    (tmp_path / "connections.toml").write_text(
        '[shop_dev]\nurl = "postgresql://dev-host/shop"\nengine = "postgres"\n'
        'env = "dev"\ndb = "shop"\ngroup = "acme"\n',
        encoding="utf-8",
    )
    (tmp_path / "queries").mkdir()
    workspace.configure_workspace(str(tmp_path))
    calls = []
    monkeypatch.setattr(local, "start_container", lambda spec, image=None: calls.append("start") or "created")
    monkeypatch.setattr(local, "wait_pg_ready", lambda spec, **kw: True)
    monkeypatch.setattr(local, "ensure_pg_database", lambda spec, db: calls.append(f"ensure:{db}"))
    monkeypatch.setattr(
        gui, "api_local_sync",
        lambda body: calls.append(f"sync:{body['db']}<-{body['from']}") or
        {"db": body["db"], "prev": None, "from": body["from"]})
    try:
        out = gui.api_local_up({"db": "shop"})
        with (tmp_path / "connections.toml").open("rb") as f:
            data = tomllib.load(f)
    finally:
        workspace.configure_workspace(None)
    assert out == {"key": "shop_local", "created": True, "engine": "postgres",
                   "state": "created", "port": local.PG_SPEC.port,
                   "synced_from": "dev"}      # fresh env auto-fills from the sibling
    assert calls == ["start", "ensure:shop", "sync:shop<-dev"]
    assert data["shop_local"]["env"] == "local" and data["shop_local"]["group"] == "acme"


@pytest.mark.unit
def test_api_local_up_reports_sync_failure_without_undoing_up(tmp_path, monkeypatch):
    from quarry import gui, local, workspace

    (tmp_path / "connections.toml").write_text(
        '[shop_dev]\nurl = "postgresql://dev-host/shop"\nengine = "postgres"\n'
        'env = "dev"\ndb = "shop"\n',
        encoding="utf-8",
    )
    (tmp_path / "queries").mkdir()
    workspace.configure_workspace(str(tmp_path))
    monkeypatch.setattr(local, "start_container", lambda spec, image=None: "created")
    monkeypatch.setattr(local, "wait_pg_ready", lambda spec, **kw: True)
    monkeypatch.setattr(local, "ensure_pg_database", lambda spec, db: None)

    def boom(body):
        raise gui.QuarryError("pg_dump client is PostgreSQL 13 but source server is 17")
    monkeypatch.setattr(gui, "api_local_sync", boom)
    try:
        out = gui.api_local_up({"db": "shop"})
    finally:
        workspace.configure_workspace(None)
    assert out["created"] is True                      # the up itself succeeded
    assert "PostgreSQL 13" in out["sync_error"]        # ...and the failure is surfaced


@pytest.mark.unit
def test_api_local_up_rejects_unsupported_engine(tmp_path, monkeypatch):
    from quarry import gui, workspace

    (tmp_path / "connections.toml").write_text(
        '[graph_dev]\nurl = "https://ep.neptune.amazonaws.com:8182"\n'
        'engine = "neptune"\nenv = "dev"\ndb = "graph"\n',
        encoding="utf-8",
    )
    (tmp_path / "queries").mkdir()
    workspace.configure_workspace(str(tmp_path))
    try:
        with pytest.raises(Exception) as ei:
            gui.api_local_up({"db": "graph"})
    finally:
        workspace.configure_workspace(None)
    assert "no local-container support" in str(ei.value)


@pytest.mark.unit
def test_api_local_sync_clears_table_cache_for_the_env_set(monkeypatch):
    from quarry import gui, local_sync

    monkeypatch.setattr(local_sync, "sync_schema",
                        lambda db, from_env: {"db": db, "prev": f"{db}__prev"})
    gui.cache._CACHE.update({"tables:shop@local": {"tables": ["stale"]},
                             "tables:shop@dev": {"tables": ["stale"]},
                             "tables:other@dev": {"tables": ["keep"]}})
    out = gui.api_local_sync({"db": "shop", "from": "dev"})
    assert out == {"db": "shop", "prev": "shop__prev", "from": "dev"}
    assert "tables:shop@local" not in gui.cache._CACHE
    assert "tables:shop@dev" not in gui.cache._CACHE
    assert "tables:other@dev" in gui.cache._CACHE


# ---------------------------------------------------------------------------
# workspace manager (issue #15) — config.toml-registered workspaces used to
# be display-only; these add/list/remove them.
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_api_workspaces_flags_missing_dir_and_missing_connections_file(tmp_path, monkeypatch):
    from quarry import gui

    good = tmp_path / "good_ws"
    good.mkdir()
    (good / "connections.toml").write_text("", encoding="utf-8")
    bare = tmp_path / "bare_ws"        # exists, but no connections.toml
    bare.mkdir()
    missing = tmp_path / "missing_ws"  # never created
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        f'workspaces = ["{good}", "{bare}", "{missing}"]\n', encoding="utf-8")
    monkeypatch.setenv("QUARRY_CONFIG", str(cfg))

    out = gui.api_workspaces()
    assert [it["dir"] for it in out["items"]] == [str(good), str(bare), str(missing)]
    assert out["items"][0] == {"dir": str(good), "display": gui._display_path(good),
                                "exists": True, "hasConnections": True, "proxyEnabled": False}
    assert out["items"][1]["exists"] is True and out["items"][1]["hasConnections"] is False
    assert out["items"][2]["exists"] is False and out["items"][2]["hasConnections"] is False


@pytest.mark.unit
def test_api_workspaces_empty_config_lists_nothing(tmp_path, monkeypatch):
    from quarry import gui

    monkeypatch.setenv("QUARRY_CONFIG", str(tmp_path / "no-such-config.toml"))
    assert gui.api_workspaces()["items"] == []


@pytest.mark.unit
def test_api_workspace_add_and_remove_round_trip(tmp_path, monkeypatch):
    """Add takes effect immediately (WS_LIST is rebuilt), remove drops it again,
    and removing something already gone is a clean 400-worthy error, not a crash."""
    from quarry import gui, workspace

    monkeypatch.setenv("QUARRY_CONFIG", str(tmp_path / "config.toml"))
    new_ws = tmp_path / "new_ws"
    try:
        out = gui.api_workspace_add({"dir": str(new_ws)})
        assert [it["dir"] for it in out["items"]] == [str(new_ws)]
        assert str(new_ws.resolve()) in [str(w.home) for w in workspace.WS_LIST]

        # adding the same dir again is a no-op, not a duplicate entry
        out = gui.api_workspace_add({"dir": str(new_ws)})
        assert len(out["items"]) == 1

        out = gui.api_workspace_remove({"dir": str(new_ws)})
        assert out["items"] == []

        with pytest.raises(gui.QuarryError):
            gui.api_workspace_remove({"dir": str(new_ws)})
    finally:
        workspace.configure_workspace(None)


@pytest.mark.unit
def test_api_workspace_add_and_remove_keep_explicit_workspace_session(tmp_path, monkeypatch):
    """A GUI process launched with an explicit --workspace flag must keep serving
    that workspace's connections after an add/remove through the manager UI —
    reload_workspace() must not fall back to config.toml like a fresh, unpinned
    process would (PR #40 review r1-1)."""
    from quarry import gui, workspace

    explicit_ws = tmp_path / "explicit_ws"
    explicit_ws.mkdir()
    (explicit_ws / "connections.toml").write_text("[pinned]\nurl = \"postgresql://x/db\"\n")
    monkeypatch.setenv("QUARRY_CONFIG", str(tmp_path / "config.toml"))
    other_ws = tmp_path / "other_ws"
    try:
        workspace.configure_workspace(str(explicit_ws))
        assert workspace.WS.home == explicit_ws.resolve()

        gui.api_workspace_add({"dir": str(other_ws)})
        assert workspace.WS.home == explicit_ws.resolve()
        assert "pinned" in workspace.WS.connections_file.read_text()

        gui.api_workspace_remove({"dir": str(other_ws)})
        assert workspace.WS.home == explicit_ws.resolve()
    finally:
        workspace.configure_workspace(None)


@pytest.mark.unit
def test_api_workspace_add_requires_dir_field():
    from quarry import gui

    with pytest.raises(gui.QuarryError):
        gui.api_workspace_add({})


@requires_db
@pytest.mark.integration
def test_workspaces_endpoints_through_http(gui_server, tmp_path, monkeypatch):
    """Same round trip, but through the real HTTP server — proves the routes
    are wired and errors come back as clean 400s, not 500s."""
    monkeypatch.setenv("QUARRY_CONFIG", str(tmp_path / "config.toml"))
    extra_ws = tmp_path / "extra_ws"

    code, body = gui_server.post("/api/workspaces/add", {"dir": str(extra_ws)})
    assert code == 200
    assert [it["dir"] for it in body["items"]] == [str(extra_ws)]

    code, body = gui_server.get("/api/workspaces")
    assert code == 200 and len(body["items"]) == 1

    code, body = gui_server.post("/api/workspaces/remove", {"dir": str(extra_ws)})
    assert code == 200 and body["items"] == []

    code, body = gui_server.post("/api/workspaces/remove", {"dir": str(extra_ws)})
    assert code == 400 and "not found" in body["error"]

    code, body = gui_server.post("/api/workspaces/add", {})
    assert code == 400 and "dir" in body["error"]


# ---------------------------------------------------------------------------
# /api/events — the SSE stream, driven over a raw socket (a JSON client would
# block forever on the never-ending response)
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_events_stream_hello_publish_heartbeat_and_close(gui_server, monkeypatch):
    import socket

    from quarry import gui

    monkeypatch.setattr(gui, "HEARTBEAT_SEC", 0.2)
    monkeypatch.setattr(gui, "_ensure_watcher", lambda: None)  # no global thread leak
    host, _, port = gui_server.base.removeprefix("http://").partition(":")
    with socket.create_connection((host, int(port)), timeout=5) as s:
        s.settimeout(5)
        s.sendall(b"GET /api/events HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
        buf = b""

        def read_until(marker: bytes) -> None:
            nonlocal buf
            while marker not in buf:
                chunk = s.recv(4096)
                assert chunk, f"stream ended before {marker!r} (got: {buf!r})"
                buf += chunk

        read_until(b'"hello"')                    # headers + greeting event
        assert b"text/event-stream" in buf
        read_until(b": hb\n\n")                   # keep-alive after 0.2s idle
        gui.publish_event("workspace_changed")    # subscribed (hello proves it)
        read_until(b'data: {"type": "workspace_changed"')
        gui._close_event_streams()                # graceful stop ends the stream
        while True:
            chunk = s.recv(4096)
            if not chunk:
                break                             # server closed the connection
            assert b'"_close"' not in chunk       # the close marker is never sent
