"""Coverage-closing tests for quarry.gui backend.

Targets the last uncovered lines in quarry.gui:
  34-39     _setup_logging (add handler once, idempotent second call)
  117->116  api_connections: group with a truthy 'ws' -> home-redacted
  225       api_queries: a seeded saved query is returned with its params
  280->282  Handler._local_origin_ok: a *local* Origin header passes through
  299       Handler._err: a non-QuarryError maps to a 400 with an error body
  322       do_GET dispatch of /api/queries
  345       do_POST unknown /api/xxx -> 404

Unit tests use monkeypatch only (no DB / network). The HTTP-handler edge
branches drive the real in-thread GUI server via the gui_server fixture, which
configures the temp Postgres workspace, so they are integration + requires_db.
"""

from __future__ import annotations

import logging

import pytest

from quarry import core, gui, workspace
from conftest import requires_db


# ---------------------------------------------------------------------------
# _setup_logging  (lines 34-39)
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_setup_logging_adds_handler_once_then_idempotent():
    saved = list(gui.log.handlers)
    try:
        gui.log.handlers.clear()
        assert gui.log.handlers == []

        gui._setup_logging()

        # a StreamHandler was attached and the level set to INFO
        assert len(gui.log.handlers) == 1
        assert isinstance(gui.log.handlers[0], logging.StreamHandler)
        assert gui.log.level == logging.INFO
        first = gui.log.handlers[0]

        # second call is a no-op (early return at line 34-35): no duplicate handler
        gui._setup_logging()
        assert len(gui.log.handlers) == 1
        assert gui.log.handlers[0] is first
    finally:
        gui.log.handlers.clear()
        gui.log.handlers.extend(saved)


# ---------------------------------------------------------------------------
# api_connections  (branch 117->116: group whose 'ws' is truthy is redacted)
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_api_connections_redacts_group_ws(ws):
    # The `ws` fixture writes one [testpg] connection; group_connections() sets
    # each group's "ws" to that connection's source (the workspace home), so the
    # `if g.get("ws")` branch runs and the ws path gets home-redacted.
    out = gui.api_connections()

    # at least one group must carry a truthy ws (proves the branch was taken)
    ws_values = [g["ws"] for g in out["groups"] if g.get("ws")]
    assert ws_values, "expected a group with a truthy 'ws' to exercise 117->118"

    def redacted(p: str) -> bool:
        return p.startswith("~") or p.startswith("/")

    for v in ws_values:
        assert redacted(v), f"group ws not redacted/absolute: {v}"
    assert redacted(out["workspace"])
    for home in out["workspaces"]:
        assert redacted(home)


@pytest.mark.unit
def test_api_connections_redacts_ws_under_home(ws, monkeypatch):
    # Force the workspace home to look like it is *under* $HOME so the redaction
    # actually rewrites the leading segment to "~" (deterministic, not just
    # "starts with /"). group_connections()'s "ws" is the connection source.
    fake_home = "/Users/coveragebot"
    fake_source = fake_home + "/projects/demo"

    monkeypatch.setattr(gui.Path, "home", staticmethod(lambda: gui.Path(fake_home)))

    def fake_groups():
        # one group WITH a truthy ws (117->118, redaction runs) and one with a
        # falsy ws (117->116, the `if` is skipped) so both branch legs are taken.
        return [
            {"group": "demo", "ws": fake_source, "items": []},
            {"group": None, "ws": None, "items": []},
        ]

    monkeypatch.setattr(core, "group_connections", fake_groups)
    monkeypatch.setattr(workspace, "WS_LIST", [])
    monkeypatch.setattr(workspace, "WS", type("W", (), {"home": fake_home})())

    out = gui.api_connections()
    assert out["groups"][0]["ws"] == "~/projects/demo"
    # the falsy-ws group is passed through untouched
    assert out["groups"][1]["ws"] is None


# ---------------------------------------------------------------------------
# api_queries  (line 225: build the list of saved-query dicts, incl. params)
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_api_queries_returns_saved_query_with_params(ws):
    # Seed a query file with a param so the nested param comprehension on
    # line 226 is exercised too.
    qtext = (
        "-- @name: cov_active_customers\n"
        "-- @db: testpg\n"
        "-- @desc: active customers by region\n"
        "-- @param: region (text, default=us)\n"
        "-- @param: limit_n (int, required)\n"
        "SELECT 1 AS ok\n"
    )
    (ws / "queries" / "cov_active_customers.sql").write_text(qtext, encoding="utf-8")

    out = gui.api_queries()
    by_name = {q["name"]: q for q in out}
    assert "cov_active_customers" in by_name

    q = by_name["cov_active_customers"]
    assert q["db"] == "testpg"
    assert q["desc"] == "active customers by region"
    assert q["sql"] == "SELECT 1 AS ok"

    params = {p["name"]: p for p in q["params"]}
    assert params["region"]["type"] == "text"
    assert params["region"]["required"] is False
    assert params["region"]["default"] == "us"
    assert params["limit_n"]["type"] == "int"
    assert params["limit_n"]["required"] is True


# ---------------------------------------------------------------------------
# HTTP handler edge branches, via the real in-thread GUI server
# ---------------------------------------------------------------------------

@pytest.mark.integration
@requires_db
def test_get_unknown_api_path_is_404(gui_server):
    # do_GET falls through every elif to the else -> 404 (line 326)
    code, body = gui_server.get("/api/nope")
    assert code == 404
    assert body == {"error": "not found"}


@pytest.mark.integration
@requires_db
def test_post_unknown_api_path_is_404(gui_server):
    # do_POST falls through to the else -> 404 (line 345)
    code, body = gui_server.post("/api/nope", {"anything": 1})
    assert code == 404
    assert body == {"error": "not found"}


@pytest.mark.integration
@requires_db
def test_local_origin_header_passes_through(gui_server):
    # A local Origin header exercises _local_origin_ok's 280->282 fall-through
    # (origin present, hostname IS local -> no 403). The request still succeeds.
    code, body = gui_server.get("/api/queries", headers={"Origin": "http://localhost:9999"})
    assert code == 200
    assert isinstance(body, list)


@pytest.mark.integration
@requires_db
def test_get_queries_dispatch_returns_seeded_query(tmp_path):
    # Drives line 322 (the /api/queries dispatch inside do_GET) end-to-end with a
    # seeded query file, using the same server plumbing as gui_server.
    from conftest import _running_gui, GuiClient

    seed = {
        "cov_get_q": (
            "-- @name: cov_get_q\n"
            "-- @db: testpg\n"
            "-- @param: n (int, required)\n"
            "SELECT :n AS n\n"
        )
    }
    with _running_gui(tmp_path, seed_queries=seed) as base:
        code, body = GuiClient(base).get("/api/queries")
    assert code == 200
    names = {q["name"] for q in body}
    assert "cov_get_q" in names
    q = next(q for q in body if q["name"] == "cov_get_q")
    assert q["params"][0]["name"] == "n"
    assert q["params"][0]["required"] is True


@pytest.mark.integration
@requires_db
def test_non_quarry_error_in_handler_maps_to_400(gui_server, monkeypatch):
    # _err's non-QuarryError branch (line 299): make a GET handler raise a plain
    # RuntimeError. do_GET catches BaseException and _err logs via log.error +
    # traceback, then _send(400, ...) with the error text.
    def boom():
        raise RuntimeError("kaboom-not-a-quarry-error")

    monkeypatch.setattr(gui, "api_connections", boom)
    code, body = gui_server.get("/api/connections")
    assert code == 400
    assert body["error"] == "kaboom-not-a-quarry-error"
    # a plain RuntimeError has no exit_code -> code is null
    assert body.get("code") is None


@pytest.mark.integration
@requires_db
def test_get_inspect_non_redis_maps_to_400(gui_server):
    # QuarryError branch of _err (line 297) + do_GET dispatch of /api/inspect:
    # inspect against the postgres testpg is redis-only -> 400 with an error body.
    code, body = gui_server.get("/api/inspect?db=testpg&env=test&key=foo")
    assert code == 400
    assert "redis-only" in body["error"]


@pytest.mark.integration
@requires_db
def test_post_query_bad_body_maps_to_400(gui_server):
    # do_POST 400 path: /api/query with a body missing 'db' -> QuarryError -> 400.
    code, body = gui_server.post("/api/query", {"sql": "SELECT 1"})
    assert code == 400
    assert "db" in body["error"]
