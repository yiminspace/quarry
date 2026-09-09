"""Shared test fixtures + markers for the Quarry suite.

Layers (select with `-m`):
  unit         pure logic, no DB / no network / no subprocess     (always runs)
  integration  in-process against a real engine (needs a DB)
  e2e          drives a real `qy` / GUI server / MCP subprocess    (needs a DB)

DB-backed tests need a reachable Postgres with a `quarry_test` database seeded
from tests/seed.sql. Set QUARRY_TEST_DB_URL to override the URL. Every DB-backed
test skips (never fails) when the DB is unreachable, so `pytest` is green on a
laptop with nothing installed — CI provides the DB and runs the full matrix.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from quarry import cache, tunnel, workspace  # noqa: E402

TEST_DB_URL = os.environ.get(
    "QUARRY_TEST_DB_URL", "postgresql://localhost:5432/quarry_test"
)
REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src"


# ---------------------------------------------------------------------------
# environment probes (each returns bool; used to skip, never to fail)
# ---------------------------------------------------------------------------

def _psql() -> str | None:
    for cand in ("psql", "/opt/homebrew/opt/postgresql@13/bin/psql"):
        if shutil.which(cand) or Path(cand).exists():
            return cand
    return None


def _db_reachable() -> bool:
    psql = _psql()
    if not psql:
        return False
    try:
        proc = subprocess.run(
            [psql, TEST_DB_URL, "-tAc", "SELECT 1"],
            capture_output=True, text=True, timeout=8,
        )
        return proc.returncode == 0 and proc.stdout.strip() == "1"
    except Exception:
        return False


def _redis_reachable() -> bool:
    cli = shutil.which("redis-cli") or shutil.which("/opt/homebrew/bin/redis-cli")
    if not cli:
        return False
    try:
        target = os.environ.get("QUARRY_TEST_REDIS_URL")
        args = [cli, "-u", target, "ping"] if target else [cli, "ping"]
        proc = subprocess.run(args, capture_output=True, text=True, timeout=5)
        return proc.returncode == 0 and "PONG" in proc.stdout.upper()
    except Exception:
        return False


def _mysql_available() -> bool:
    try:
        import pymysql  # noqa: F401
    except Exception:
        return False
    return bool(os.environ.get("QUARRY_TEST_MYSQL_URL"))


def _docker_reachable() -> bool:
    """True only if the docker binary exists AND the daemon answers. `qy local`'s
    real container-lifecycle tests need this; without it they skip (never fail),
    mirroring the DB/redis engine-dependent policy."""
    if not shutil.which("docker"):
        return False
    try:
        proc = subprocess.run(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            capture_output=True, text=True, timeout=10,
        )
        return proc.returncode == 0
    except Exception:
        return False


DB_OK = _db_reachable()
REDIS_OK = _redis_reachable()
MYSQL_OK = _mysql_available()
DOCKER_OK = _docker_reachable()

requires_db = pytest.mark.skipif(not DB_OK, reason="quarry_test Postgres not reachable")
requires_redis = pytest.mark.skipif(not REDIS_OK, reason="local redis-cli/redis not reachable")
requires_mysql = pytest.mark.skipif(not MYSQL_OK, reason="QUARRY_TEST_MYSQL_URL unset or pymysql missing")
requires_docker = pytest.mark.skipif(not DOCKER_OK, reason="docker binary/daemon not available")


def pytest_configure(config: pytest.Config) -> None:
    for name, desc in (
        ("unit", "pure logic; no DB, network, or subprocess"),
        ("integration", "in-process against a real database engine"),
        ("e2e", "drives a real qy CLI / GUI server / MCP subprocess"),
    ):
        config.addinivalue_line("markers", f"{name}: {desc}")


_BROWSER_FIXTURES = {"page", "gui_url", "_pw_browser"}
# true e2e = a real external process (qy / mcp subprocess) or browser.
_E2E_FIXTURES = {"qy", "mcp", "mcp_ws", "client"}
# integration = in-process against a real DB, incl. the in-thread GUI HTTP server.
_INTEGRATION_FIXTURES = {"ws", "pg_exec", "gui_server"}


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Give every test a layer marker (unit / integration / e2e) if it lacks one,
    inferred from the fixtures it uses — so `pytest -m unit` etc. cover the whole
    suite, including tests written before the markers existed. Browser tests are
    a sub-kind of e2e (marked both, so `-m e2e` includes them, `-m browser` isolates)."""
    for item in items:
        fx = set(getattr(item, "fixturenames", []))
        if fx & _BROWSER_FIXTURES and not item.get_closest_marker("browser"):
            item.add_marker(pytest.mark.browser)
        if any(item.get_closest_marker(m) for m in ("unit", "integration", "e2e")):
            continue
        if fx & (_BROWSER_FIXTURES | _E2E_FIXTURES):
            item.add_marker(pytest.mark.e2e)
        elif fx & _INTEGRATION_FIXTURES:
            item.add_marker(pytest.mark.integration)
        else:
            item.add_marker(pytest.mark.unit)


def pytest_report_header(config: pytest.Config) -> list[str]:
    def dot(ok: bool) -> str:
        return "available" if ok else "MISSING (dependent tests skip)"
    return [
        "quarry test environment:",
        f"  postgres ({TEST_DB_URL}): {dot(DB_OK)}",
        f"  redis:                    {dot(REDIS_OK)}",
        f"  mysql:                    {dot(MYSQL_OK)}",
        f"  browser (playwright):     {dot(BROWSER_OK)}",
        f"  docker:                   {dot(DOCKER_OK)}",
    ]


# ---------------------------------------------------------------------------
# Shared metadata cache isolation (issue #97) — cache.py's storage is now
# used by core.py (and therefore by gui.py/cli.py/mcp.py alike), so every
# test — not just GUI ones — needs its own cache file instead of touching
# the developer's real ~/.cache/quarry/gui-cache.json. QUARRY_CACHE_FILE
# covers CLI/MCP subprocess fixtures (qy, mcp); the direct monkeypatch
# covers in-process calls (gui.py, core.cached_* unit tests).
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path: Path, monkeypatch):
    cache_file = tmp_path / "gui-cache.json"
    monkeypatch.setattr(cache, "CACHE_FILE", cache_file)
    monkeypatch.setenv("QUARRY_CACHE_FILE", str(cache_file))
    cache._CACHE.clear()
    yield
    cache._CACHE.clear()


# The cross-process tunnel registry (issue #101 r1-1) is on-disk by design —
# so it needs the same per-test isolation as the metadata cache above, or
# tests would read/write the developer's real ~/.cache/quarry/tunnels.json.
@pytest.fixture(autouse=True)
def _isolated_tunnel_registry(tmp_path: Path, monkeypatch):
    registry_file = tmp_path / "tunnels.json"
    monkeypatch.setattr(tunnel, "REGISTRY_FILE", registry_file)
    monkeypatch.setenv("QUARRY_TUNNEL_REGISTRY_FILE", str(registry_file))
    tunnel._OWNED_REGISTRY_KEYS.clear()
    yield
    tunnel._OWNED_REGISTRY_KEYS.clear()


# ---------------------------------------------------------------------------
# workspace helpers
# ---------------------------------------------------------------------------

SEED_EXTRA_SQL = """
DROP TABLE IF EXISTS qy_wide;
CREATE TABLE qy_wide (id serial PRIMARY KEY, blob jsonb, note text);
INSERT INTO qy_wide (blob, note) VALUES
  ('{"a":1,"b":[1,2,3]}', 'héllo, "world"'),
  (NULL, E'line1\\nline2');
"""


def _write_ws(dirpath: Path) -> Path:
    (dirpath / "connections.toml").write_text(
        f'[testpg]\nurl = "{TEST_DB_URL}"\nengine = "postgres"\nenv = "test"\n',
        encoding="utf-8",
    )
    (dirpath / "queries").mkdir(exist_ok=True)
    return dirpath


@pytest.fixture()
def ws(tmp_path: Path):
    """A temp workspace with one connection (testpg) + an empty queries dir,
    configured as the process-wide active workspace (for in-process core/gui tests)."""
    if _psql() and _psql() != "psql":
        os.environ["QUARRY_PSQL"] = _psql()
    _write_ws(tmp_path)
    workspace.configure_workspace(str(tmp_path))
    yield tmp_path
    workspace.configure_workspace(None)


@pytest.fixture()
def wsdir(tmp_path: Path) -> Path:
    """A temp workspace on disk (no process-wide configure) for subprocess e2e."""
    return _write_ws(tmp_path)


# ---------------------------------------------------------------------------
# CLI subprocess runner
# ---------------------------------------------------------------------------

@pytest.fixture()
def pg_exec():
    """Run raw SQL against the test Postgres via psql (for test setup/teardown/
    assertions that must bypass the read-only rail — e.g. proving a blocked write
    did NOT run). Returns (rc, stdout, stderr)."""
    from quarry import core

    def run(sql: str):
        return core.run_psql_capture(TEST_DB_URL, sql, timeout=15)
    return run


@pytest.fixture()
def qy(wsdir: Path):
    """Run `qy` as a subprocess against the temp workspace; returns CompletedProcess."""
    def run(*args: str, timeout: int = 25) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        env["PYTHONPATH"] = str(SRC)
        env.pop("QUARRY_WORKSPACE", None)
        if _psql() and _psql() != "psql":
            env["QUARRY_PSQL"] = _psql()
        return subprocess.run(
            [sys.executable, "-m", "quarry.cli", "--workspace", str(wsdir), *args],
            capture_output=True, text=True, env=env, timeout=timeout,
        )
    return run


# ---------------------------------------------------------------------------
# GUI server fixture (real ThreadingHTTPServer on an ephemeral port)
# ---------------------------------------------------------------------------

class GuiClient:
    def __init__(self, base: str):
        self.base = base

    def _req(self, method: str, path: str, body=None, headers=None):
        import urllib.error
        import urllib.request
        data = json.dumps(body).encode() if body is not None else None
        h = {"Content-Type": "application/json"} if data else {}
        h.update(headers or {})
        req = urllib.request.Request(self.base + path, data=data, headers=h, method=method)
        # The test server is a localhost ephemeral port; never route through a
        # system HTTP proxy. A proxy that keys on the Host header would misroute
        # our foreign-Host test (evil.example) and return 502 instead of reaching
        # the server, masking the real 403 response.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(req, timeout=30) as r:
                return r.status, json.loads(r.read().decode() or "null")
        except urllib.error.HTTPError as e:
            raw = e.read().decode()
            try:
                return e.code, json.loads(raw)
            except json.JSONDecodeError:
                return e.code, raw

    def get(self, path: str, headers=None):
        return self._req("GET", path, None, headers)

    def post(self, path: str, body: dict, headers=None):
        return self._req("POST", path, body, headers)


from contextlib import contextmanager  # noqa: E402


@contextmanager
def _running_gui(tmp_path: Path, seed_queries=None, extra_conn: str | None = None):
    """Start the real GUI HTTP server on an ephemeral port against a temp workspace.
    Yields the base URL. The shared cache (cache.py) is already isolated per-test
    by the autouse _isolated_cache fixture.

    seed_queries: optional {name: sql_text} written into queries/ before serving.
    extra_conn:   optional TOML appended to connections.toml (extra connections,
                  e.g. an env-set or a redis target for browser tests).
    """
    from http.server import ThreadingHTTPServer

    from quarry import gui
    _write_ws(tmp_path)
    if extra_conn:
        with (tmp_path / "connections.toml").open("a", encoding="utf-8") as f:
            f.write(extra_conn)
    for name, text in (seed_queries or {}).items():
        (tmp_path / "queries" / f"{name}.sql").write_text(text, encoding="utf-8")
    if _psql() and _psql() != "psql":
        os.environ["QUARRY_PSQL"] = _psql()
    workspace.configure_workspace(str(tmp_path))

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    httpd = ThreadingHTTPServer(("127.0.0.1", port), gui.Handler)
    threading.Thread(target=lambda: httpd.serve_forever(poll_interval=0.02), daemon=True).start()
    time.sleep(0.05)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        workspace.configure_workspace(None)


@pytest.fixture()
def gui_server(tmp_path: Path):
    """Real GUI HTTP server + a JSON GuiClient (for API-level e2e)."""
    with _running_gui(tmp_path) as base:
        yield GuiClient(base)


@pytest.fixture()
def gui_url(tmp_path: Path):
    """Real GUI HTTP server; yields the base URL (for the browser suite)."""
    with _running_gui(tmp_path) as base:
        yield base


# ---------------------------------------------------------------------------
# Playwright browser fixtures (used by the `browser`-marked GUI frontend suite)
# ---------------------------------------------------------------------------

def _playwright_ready() -> bool:
    try:
        import playwright  # noqa: F401
    except Exception:
        return False
    # the chromium headless-shell must actually be installed
    from pathlib import Path as _P
    cache = _P.home() / "Library" / "Caches" / "ms-playwright"
    alt = _P.home() / ".cache" / "ms-playwright"
    return any(p.exists() and any(p.glob("chromium*")) for p in (cache, alt))


BROWSER_OK = _playwright_ready()
requires_browser = pytest.mark.skipif(
    not (BROWSER_OK and DB_OK), reason="playwright+chromium or test DB unavailable")


@pytest.fixture(scope="session")
def _pw_browser():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch()
        yield browser
        browser.close()


def stub_cdn(ctx) -> None:
    """Serve the icon-font CSS from the CDN as an empty local stub so browser
    tests are hermetic: no external network, no networkidle flake when the CDN
    stalls, no console errors from a blocked request."""
    ctx.route("**://cdn.jsdelivr.net/**",
              lambda route: route.fulfill(status=200, content_type="text/css", body=""))


def stub_events(ctx) -> None:
    """Answer `/api/events` with a valid SSE body that ends immediately and
    postpones the EventSource reconnect beyond any test's lifetime. The real
    endpoint holds its request open forever, so `goto(..., "networkidle")`
    would never resolve. Tests that exercise the live event channel build
    their own context without this stub and wait on explicit selectors."""
    ctx.route("**/api/events",
              lambda route: route.fulfill(status=200, content_type="text/event-stream",
                                          body="retry: 86400000\n\n"))


@pytest.fixture()
def page(_pw_browser, gui_url):
    """A Playwright page already navigated to a live GUI. Console errors are
    captured on the page object as `page._console_errors` for assertions."""
    ctx = _pw_browser.new_context(viewport={"width": 1280, "height": 900})
    stub_cdn(ctx)
    stub_events(ctx)
    pg = ctx.new_page()
    pg._console_errors = []
    pg.on("console", lambda m: m.type == "error" and pg._console_errors.append(m.text))
    pg.on("pageerror", lambda e: pg._console_errors.append(str(e)))
    pg.goto(gui_url, wait_until="networkidle")
    try:
        yield pg
    finally:
        ctx.close()


# ---------------------------------------------------------------------------
# MCP stdio client fixture
# ---------------------------------------------------------------------------

class MCPClient:
    def __init__(self, ws_dir: Path, write: bool = False):
        args = [sys.executable, "-m", "quarry.mcp", "--workspace", str(ws_dir)]
        if write:
            args.append("--write")
        env = {**os.environ, "PYTHONPATH": str(SRC)}
        if _psql() and _psql() != "psql":
            env["QUARRY_PSQL"] = _psql()
        self.proc = subprocess.Popen(
            args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, env=env,
        )
        self._id = 0

    def rpc(self, method: str, params: dict | None = None):
        self._id += 1
        msg = {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params or {}}
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()
        reply = json.loads(self.proc.stdout.readline())
        assert reply["id"] == self._id
        return reply

    def notify(self, method: str):
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": method}) + "\n")
        self.proc.stdin.flush()

    def call_tool(self, name: str, args: dict | None = None):
        reply = self.rpc("tools/call", {"name": name, "arguments": args or {}})
        result = reply["result"]
        payload = json.loads(result["content"][0]["text"])
        return payload, result["isError"]

    def close(self):
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=10)
        except Exception:
            self.proc.kill()


@pytest.fixture()
def mcp(wsdir: Path):
    """An MCP stdio client bound to the temp Postgres workspace (read-only server)."""
    c = MCPClient(wsdir, write=False)
    c.rpc("initialize", {"protocolVersion": "2025-06-18"})
    c.notify("notifications/initialized")
    yield c
    c.close()
