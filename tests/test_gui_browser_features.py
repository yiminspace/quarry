"""Browser e2e for the GUI feature matrix (TESTING.md "GUI feature matrix").

Covers the rows the original test_gui_browser.py left open: draft preservation,
env-set pills / prod guard, request-race latest-wins, numeric sort + restore,
persistence across reloads, redis key tree, and assorted grid/toolbar gaps.

Console errors are an autouse invariant here: any page-level JS error fails the
test that produced it.
"""

from __future__ import annotations

import json
import re
import shutil
import socket
import subprocess
import time
from contextlib import contextmanager
from urllib.parse import parse_qs, urlencode, urlparse

import pytest

from conftest import _running_gui, requires_chromium, stub_cdn, stub_events, english_browser_context, DB_OK, REDIS_OK, TEST_DB_URL
from test_gui_browser import _run_result, _select_testpg, _set_sql
from test_gui_browser import page_saved  # noqa: F401  (fixture reused below)

pytestmark = [requires_chromium, pytest.mark.browser]


def test_table_query_disclosure_preserves_draft_and_does_not_execute(page_noparam):
    page = page_noparam
    _select_testpg(page)
    _set_sql(page, "select 42 as keep_my_draft")
    requests = []
    page.on("request", lambda req: requests.append(req.url) if req.method == "POST" and
            (req.url.endswith("/api/query") or req.url.endswith("/api/run")) else None)
    row = page.locator('.table-query-group').filter(has=page.locator('[data-t="customers"]'))
    row.locator('[data-t="customers"]').click()
    _run_result(page)
    requests.clear()
    row.locator('[data-q="all-cust"]').click()
    page.wait_for_function("document.querySelector('#sql').value.includes('FROM customers')")
    assert not requests
    assert page.evaluate("JSON.parse(localStorage.qy_tabs).some(t => t.sql === 'select 42 as keep_my_draft')")
    assert page.locator('.query-count').count() == 0
    assert page.locator('#sql').get_attribute('readonly') is not None
    assert page.locator('#sql').evaluate('el => getComputedStyle(el).cursor') == 'not-allowed'
    assert 'read-only' in page.locator('#sql').get_attribute('title')
    fixed_index = page.locator('#tabs [aria-selected="true"]').get_attribute('data-i')
    tab_count = page.evaluate('JSON.parse(localStorage.qy_tabs).length')
    row.locator('[data-q="all-cust"]').click()
    assert page.evaluate('JSON.parse(localStorage.qy_tabs).length') == tab_count
    assert not requests
    original = page.locator('#sql').input_value()
    page.locator('#copySavedBtn').click()
    assert page.locator('#sql').get_attribute('readonly') is None
    _set_sql(page, 'select 123 as editable_copy')
    assert row.locator('[data-q="all-cust"]').get_attribute('aria-pressed') == 'false'
    page.locator('#tabs [data-i="' + fixed_index + '"]').click()
    assert row.locator('[data-q="all-cust"]').get_attribute('aria-pressed') == 'true'
    row.locator('[data-q="all-cust"]').click()
    assert page.locator('#sql').input_value() == original
    assert row.locator('[data-q="all-cust"]').get_attribute('aria-pressed') == 'true'
    page.get_by_role('button', name='Collapse all', exact=True).click()
    assert page.locator('#sql').input_value() == original
    assert not row.is_visible()


def test_fixed_query_uses_explicit_environment_without_production_autorun(page_envset):
    page = page_envset
    page.route('**/api/queries', lambda route: route.fulfill(json=[dict(
        name='prod-customers', queryId='pinned-prod', db='shop_prod', desc='Production customers',
        sql='SELECT * FROM customers', params=[])]))
    page.reload(wait_until='networkidle')
    requests = []
    page.on('request', lambda req: requests.append(req.url) if req.method == 'POST' and
            (req.url.endswith('/api/query') or req.url.endswith('/api/run')) else None)
    # The workspace index remains available even before a connection is selected.
    page.locator('.qname[data-q="prod-customers"]').click()
    page.locator('#prodBadge').wait_for(state='visible')
    count = page.evaluate('JSON.parse(localStorage.qy_tabs).length')
    page.locator('.qname[data-q="prod-customers"]').last.click()
    assert page.evaluate('JSON.parse(localStorage.qy_tabs).length') == count
    assert page.locator('#sql').get_attribute('readonly') is not None
    assert not requests


def test_saved_query_waits_for_tables_before_anchoring(page_noparam):
    page = page_noparam
    pending = []
    page.route('**/api/tables?*', lambda route: pending.append(route))
    page.locator('.query-index-children [data-q="all-cust"]').click()
    page.wait_for_timeout(100)
    assert page.locator('.other-queries').count() == 0
    assert pending
    for route in pending:
        route.fulfill(json={'engine': 'postgres', 'tables': ['public.customers'], 'capped': False})
    query = page.locator('#tbl-panel [data-q="all-cust"]')
    query.wait_for(state='visible')
    assert query.get_attribute('aria-pressed') == 'true'
    assert page.locator('#tbl-panel [data-t="public.customers"]').get_attribute('aria-expanded') == 'true'
    assert page.locator('.other-queries').count() == 0


@pytest.mark.parametrize('direction', ['ArrowUp', 'ArrowDown'])
def test_fixed_query_history_shortcuts_restore_editable_tab(page_noparam, direction):
    page = page_noparam
    _select_testpg(page)
    history_sql = 'select 7 as previous_query'
    draft_sql = 'select 88 as unfinished_draft'
    _run_sql(page, history_sql)
    if direction == 'ArrowDown':
        _set_sql(page, draft_sql)
        page.locator('#sql').press('ControlOrMeta+ArrowUp')
        assert page.locator('#sql').input_value() == history_sql
    page.locator('.query-index-children [data-q="all-cust"]').click()
    page.wait_for_selector('#grid table tbody tr')
    template = page.locator('#sql').input_value()
    assert page.locator('#sql').get_attribute('readonly') is not None
    requests = []
    page.on('request', lambda req: requests.append(req.url) if req.method == 'POST' else None)
    page.locator('#sql').press('ControlOrMeta+' + direction)
    assert page.locator('#sql').input_value() == (draft_sql if direction == 'ArrowDown' else history_sql)
    assert page.locator('#sql').get_attribute('readonly') is None
    assert page.evaluate('JSON.parse(localStorage.qy_tabs).find(tab => tab.savedQueryId).sql') == template
    assert not requests


def test_graph_query_discovery_rows(page_neptune):
    page = page_neptune
    queries = [
        {'name': 'related', 'db': 'graph', 'desc': '查看记忆关联',
         'sql': 'MATCH (n:MindNode)-[r]->(a:Artifact) RETURN n LIMIT 10', 'params': []},
        {'name': 'structure', 'db': 'graph', 'desc': '查看节点类别',
         'sql': 'MATCH (n) RETURN labels(n) LIMIT 10', 'params': []},
    ]
    page.route('**/api/queries', lambda route: route.fulfill(json=queries))
    page.route('**/api/tables?*', lambda route: route.fulfill(json={
        'engine': 'neptune', 'tables': [], 'capped': False}))
    page.route('**/api/query', lambda route: route.fulfill(json={
        'columns': [], 'rows': [], 'rowCount': 0, 'truncated': False,
        'elapsedMs': 0, 'engine': 'neptune', 'sql': '', 'downloadBytes': 0}))
    page.reload(wait_until='networkidle')
    page.locator('.dbrow[data-db="graph"]').click()
    page.locator('#tbl-panel [data-t="MindNode"]').wait_for()
    assert page.locator('.query-count').count() == 0
    for label in ('MindNode', 'Artifact'):
        row = page.locator('.table-query-group').filter(has=page.locator('[data-t="' + label + '"]'))
        row.locator('[data-t="' + label + '"]').click()
        assert row.locator('[data-q="related"]').inner_text() == '查看记忆关联'
    assert page.locator('.other-queries').count() == 0
    assert page.locator('.query-index-children [data-q="structure"]').is_visible()


def test_query_titles_expand_description_without_counts(page_envset, tmp_path):
    page = page_envset
    description = 'LINE 相关记忆，按更新时间倒序返回 10 条，每条摘要最多 500 字符。'
    queries = [dict(name='line', db='testpg', desc=description,
                    sql='SELECT * FROM customers', params=[]),
               dict(name='shop-query', db='shop', desc='查看客户记录',
                    sql='SELECT * FROM customers', params=[])]
    page.route('**/api/queries', lambda route: route.fulfill(json=queries))
    page.reload(wait_until='networkidle')
    _select_testpg(page)
    assert page.locator('.query-count').count() == 0
    heading = page.locator('[data-saved-ws]').first
    assert 'All queries' in heading.inner_text()
    assert 'vg-grp' not in (heading.get_attribute('class') or '')
    row = page.locator('.table-query-group').filter(has=page.locator('[data-t="customers"]'))
    row.locator('[data-t="customers"]').click()
    query = row.locator('[data-q="line"]')
    assert query.locator('.query-title').inner_text() == 'LINE 相关记忆'
    assert query.locator('.query-description').count() == 0
    query.click()
    assert query.locator('.query-description').inner_text() == description
    assert query.get_attribute('aria-pressed') == 'true'
    assert query.locator('.query-description').evaluate('el => el.scrollWidth <= el.clientWidth')
    page.screenshot(path=str(tmp_path / 'sidebar-layout.png'))
    print('Layout screenshot:', tmp_path / 'sidebar-layout.png')


def test_app_uses_distinct_svg_favicon(page):
    icon = page.locator('head link[rel="icon"]')
    assert icon.get_attribute("type") == "image/svg+xml"
    assert icon.get_attribute("href") == "/quarry/favicon.svg"
    response = page.request.get(icon.evaluate("node => node.href"))
    assert response.ok
    assert "<text" not in response.text()


# ---------------------------------------------------------------------------
# helpers + fixtures
# ---------------------------------------------------------------------------

def _mk_page(browser, url):
    ctx = english_browser_context(browser, viewport={"width": 1280, "height": 900})
    stub_cdn(ctx)
    stub_events(ctx)
    pg = ctx.new_page()
    pg._console_errors = []
    pg.on("console", lambda m: m.type == "error" and pg._console_errors.append(m.text))
    pg.on("pageerror", lambda e: pg._console_errors.append(str(e)))
    pg.goto(url, wait_until="networkidle")
    return ctx, pg


@pytest.fixture(autouse=True)
def _console_clean(request):
    """Invariant: no page JS errors during any test in this module."""
    # Keep the legacy Postgres requirement, but do not gate SQLite's standalone
    # browser coverage on a server it never connects to.
    if not {"page_sqlite", "page_sqlite_workspaces", "page_sqlite_default_language"}.intersection(request.fixturenames) and not DB_OK:
        pytest.skip("quarry_test Postgres not reachable")
    pages = [request.getfixturevalue(n)
             for n in ("page", "page_envset", "page_envset_local", "page_noparam",
                       "page_redis", "page_redis_capped", "page_dead", "page_clip",
                       "page_explicit_production", "page_saved", "page_missing_group", "page_neptune", "page_saved_multi", "page_sqlite", "page_sqlite_workspaces", "page_sqlite_default_language")
             if n in request.fixturenames]     # grab refs while fixtures are alive
    yield
    for pg in pages:
        # network-layer noise (offline CDN icons, deliberately aborted requests)
        # is not a page JS error — only real script errors fail the test
        errors = [e for e in pg._console_errors
                  if "Failed to load resource" not in e and "net::ERR_" not in e]
        assert not errors, f"console errors: {errors}"


ENVSET_TOML = f"""
[shop_dev]
url = "{TEST_DB_URL}"
engine = "postgres"
env = "dev"
db = "shop"
group = "acme"

[shop_prod]
url = "{TEST_DB_URL}"
engine = "postgres"
env = "prod"
production = true
db = "shop"
group = "acme"
"""

NEPTUNE_ENVSET_TOML = """
[graph_dev]
url = "https://dev.example.neptune.amazonaws.com:8182"
engine = "neptune"
env = "dev"
db = "graph"
group = "brain"

[graph_local]
url = "https://localhost:18182"
engine = "neptune"
env = "local"
db = "graph"
group = "brain"

[graph_prod]
url = "https://prod.example.neptune.amazonaws.com:8182"
engine = "neptune"
env = "prod"
production = true
db = "graph"
group = "brain"
"""


@pytest.fixture()
def page_neptune(_pw_browser, tmp_path):
    with _running_gui(tmp_path, extra_conn=NEPTUNE_ENVSET_TOML) as url:
        ctx, pg = _mk_page(_pw_browser, url)
        try:
            yield pg
        finally:
            ctx.close()


@pytest.fixture()
def page_envset(_pw_browser, tmp_path):
    """A page whose workspace adds a two-env set (shop: dev + prod) on the test DB."""
    with _running_gui(tmp_path, extra_conn=ENVSET_TOML) as url:
        ctx, pg = _mk_page(_pw_browser, url)
        try:
            yield pg
        finally:
            ctx.close()


ENVSET_LOCAL_TOML = f"""
[stash_prod]
url = "{TEST_DB_URL}"
engine = "postgres"
env = "prod"
production = true
db = "stash"
group = "acme"

[stash_local]
url = "{TEST_DB_URL}"
engine = "postgres"
env = "local"
db = "stash"
group = "acme"
"""


@pytest.fixture()
def page_envset_local(_pw_browser, tmp_path):
    """A page whose workspace adds a two-env set (stash: prod registered *before*
    local, no dev) — exercises "local always sorts first" (issue #44)."""
    with _running_gui(tmp_path, extra_conn=ENVSET_LOCAL_TOML) as url:
        ctx, pg = _mk_page(_pw_browser, url)
        try:
            yield pg
        finally:
            ctx.close()


MISSING_GROUP_ENVSET_TOML = f"""
[queue_local]
url = "redis://127.0.0.1:6379/0"
engine = "redis"
env = "local"
db = "queue"
group = "acme"

[queue]
url = "redis://dev.example.com:6379/0"
engine = "redis"
env = "dev"
"""


@pytest.fixture()
def page_missing_group(_pw_browser, tmp_path):
    """One env sibling omits group; the sidebar must still render one env-set."""
    with _running_gui(tmp_path, extra_conn=MISSING_GROUP_ENVSET_TOML) as url:
        ctx, pg = _mk_page(_pw_browser, url)
        try:
            yield pg
        finally:
            ctx.close()


@pytest.fixture()
def page_noparam(_pw_browser, tmp_path):
    """A page whose workspace seeds a saved query WITHOUT params (runs on click)."""
    q = "-- @name: all-cust\n-- @db: testpg\nSELECT * FROM customers ORDER BY id\n"
    with _running_gui(tmp_path, seed_queries={"all-cust": q}) as url:
        ctx, pg = _mk_page(_pw_browser, url)
        try:
            yield pg
        finally:
            ctx.close()


@pytest.fixture()
def page_saved_multi(_pw_browser, tmp_path):
    """A page with a shop env-set AND a saved query bound to testpg — lets us run a
    saved query while the active tab is bound to a DIFFERENT connection."""
    q = "-- @name: all-cust\n-- @db: testpg\nSELECT * FROM customers ORDER BY id\n"
    with _running_gui(tmp_path, extra_conn=ENVSET_TOML,
                      seed_queries={"all-cust": q}) as url:
        ctx, pg = _mk_page(_pw_browser, url)
        try:
            yield pg
        finally:
            ctx.close()


# --- redis: reuse a local redis on 6379 (CI service) or spawn an ephemeral one ---

_REDIS_SERVER = shutil.which("redis-server") or (
    "/opt/homebrew/bin/redis-server"
    if shutil.which("/opt/homebrew/bin/redis-server") else None)
_REDIS_KEYS = ["qygui:sess:1", "qygui:sess:2", "qygui:jobs"]


def _rcli(url: str, *args: str) -> str:
    cli = shutil.which("redis-cli") or "/opt/homebrew/bin/redis-cli"
    r = subprocess.run([cli, "-u", url, *args], capture_output=True, text=True, timeout=10)
    return r.stdout.strip()


@contextmanager
def _redis_running():
    """Yield a usable redis URL: the shared local redis (db 15) when reachable,
    else an ephemeral redis-server on a free port; skips when neither exists."""
    if REDIS_OK:
        yield "redis://127.0.0.1:6379/15"
        return
    if not (_REDIS_SERVER and (shutil.which("redis-cli") or shutil.which("/opt/homebrew/bin/redis-cli"))):
        pytest.skip("no redis reachable and no redis-server binary")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    proc = subprocess.Popen(
        [_REDIS_SERVER, "--port", str(port), "--save", "", "--appendonly", "no"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    rurl = f"redis://127.0.0.1:{port}/0"
    try:
        for _ in range(50):  # wait for readiness
            if _rcli(rurl, "ping") == "PONG":
                break
            time.sleep(0.1)
        else:
            pytest.skip("ephemeral redis-server did not come up")
        yield rurl
    finally:
        proc.terminate()


@contextmanager
def _redis_page(browser, tmp_path, rurl):
    extra = f'[testredis]\nurl = "{rurl}"\nengine = "redis"\n'
    with _running_gui(tmp_path, extra_conn=extra) as url:
        ctx, pg = _mk_page(browser, url)
        try:
            yield pg
        finally:
            ctx.close()


@pytest.fixture()
def page_redis(_pw_browser, tmp_path):
    """A page whose workspace adds a redis connection with a small seeded key tree."""
    with _redis_running() as rurl:
        _rcli(rurl, "del", *_REDIS_KEYS)
        _rcli(rurl, "set", "qygui:sess:1", "alpha", "EX", "3600")
        _rcli(rurl, "set", "qygui:sess:2", "beta")
        _rcli(rurl, "rpush", "qygui:jobs", "a", "b")
        try:
            with _redis_page(_pw_browser, tmp_path, rurl) as pg:
                yield pg
        finally:
            _rcli(rurl, "del", *_REDIS_KEYS)


@pytest.fixture()
def page_redis_capped(_pw_browser, tmp_path):
    """A page whose redis connection holds >400 keys, so the key list is capped."""
    with _redis_running() as rurl:
        keys = [f"qycap:{i}" for i in range(401)]
        _rcli(rurl, "mset", *[a for k in keys for a in (k, "x")])
        try:
            with _redis_page(_pw_browser, tmp_path, rurl) as pg:
                yield pg
        finally:
            _rcli(rurl, "del", *keys)


DEAD_TOML = '[deadpg]\nurl = "postgresql://127.0.0.1:9/nope"\nengine = "postgres"\n'


@pytest.fixture()
def page_dead(_pw_browser, tmp_path):
    """A page whose workspace adds an unreachable postgres connection."""
    with _running_gui(tmp_path, extra_conn=DEAD_TOML) as url:
        ctx, pg = _mk_page(_pw_browser, url)
        try:
            yield pg
        finally:
            ctx.close()


@pytest.fixture()
def page_clip(_pw_browser, gui_url):
    """A page with clipboard permissions granted (for copy-path tests)."""
    ctx = english_browser_context(_pw_browser, viewport={"width": 1280, "height": 900})
    ctx.grant_permissions(["clipboard-read", "clipboard-write"])
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


def _col_values(page, nth: int = 2) -> list[str]:
    """Text of the nth cell (1-based; 1 = rownum) of every body row."""
    return page.evaluate(
        f"[...document.querySelectorAll('#grid tbody tr td:nth-child({nth})')]"
        ".map(td => td.textContent)")


def _run_sql(page, sql: str):
    _set_sql(page, sql)
    page.locator("#runBtn").click()
    _run_result(page)


# ---------------------------------------------------------------------------
# 1. Draft preservation (fix: table click / history recall must not lose SQL)
# ---------------------------------------------------------------------------

def test_table_click_preserves_draft_on_previous_tab(page):
    _select_testpg(page)
    _set_sql(page, "select 123 as draft_marker")     # hand-written, never run
    page.locator('#tbl-panel .tname[data-t="customers"]').click()
    page.wait_for_selector("#grid table tbody tr")
    assert page.locator(".tab[data-i]").count() == 2
    assert page.locator("#sql").input_value() == "select * from customers"
    page.locator('.tab[data-i="0"]').click()
    page.wait_for_function(
        "document.querySelector('#sql').value.includes('draft_marker')")


def test_history_nav_stashes_and_restores_draft(page):
    _select_testpg(page)
    _run_sql(page, "select 1 as a")
    _set_sql(page, "select 999 as unfinished")       # draft on top of history
    page.locator("#sql").focus()
    page.keyboard.press("ControlOrMeta+ArrowUp")     # walk back -> history entry
    page.wait_for_function(
        "document.querySelector('#sql').value === 'select 1 as a'")
    page.keyboard.press("ControlOrMeta+ArrowDown")   # walk forward -> draft restored
    page.wait_for_function(
        "document.querySelector('#sql').value === 'select 999 as unfinished'")


# ---------------------------------------------------------------------------
# 2. Env pills / prod guard (fix: no auto-run on prod)
# ---------------------------------------------------------------------------

def _relative_luminance(rgb: str) -> float:
    match = re.fullmatch(r"rgba?\(([\d.]+), ([\d.]+), ([\d.]+)(?:, [^)]+)?\)", rgb)
    if match:
        channels = [float(value) / 255 for value in match.groups()]
    else:
        # CSS color-mix() retains the modern color(srgb ...) serialization.
        match = re.fullmatch(r"color\(srgb ([\d.]+) ([\d.]+) ([\d.]+)(?: / [^)]+)?\)", rgb)
        assert match, rgb
        channels = [float(value) for value in match.groups()]
    linear = [value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4
              for value in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast_ratio(locator) -> tuple[str, str, float]:
    fg, bg = locator.evaluate(
        """(el) => {
            const style = getComputedStyle(el);
            return [style.color, style.backgroundColor];
        }"""
    )
    lighter, darker = sorted((_relative_luminance(fg), _relative_luminance(bg)), reverse=True)
    return fg, bg, (lighter + 0.05) / (darker + 0.05)


def test_env_pills_default_dev_and_prod_badge(page_envset):
    page = page_envset
    page.wait_for_selector('.dbrow[data-db="shop"]')
    assert page.locator("#side .pill").count() == 0
    page.locator('.dbrow[data-db="shop"]').click()
    page.wait_for_selector("#esw .ep")
    assert page.locator("#side .pill").count() == 0
    assert "on" in page.locator('#esw .ep[data-env="dev"]').get_attribute("class").split()
    assert "prod" in page.locator('#esw .ep[data-env="prod"]').get_attribute("class").split()
    assert page.locator("#prodBadge").is_hidden()
    page.locator('#esw .ep[data-env="prod"]').click()
    page.wait_for_selector("#prodBadge", state="visible")
    assert page.locator("#side .pill").count() == 0


def test_selected_prod_pills_keep_accessible_contrast_in_light_themes(page_envset):
    page = page_envset
    page.locator('.dbrow[data-db="shop"]').click()
    page.wait_for_selector("#esw .ep")
    page.locator('#esw .ep[data-env="prod"]').click()
    page.locator(".vg-switcher-mode").click()
    assert page.evaluate("document.documentElement.dataset.mode") == "light"

    for theme in ("slate", "signal"):
        if theme == "signal":
            page.locator(".vg-switcher-trigger").click()
            page.get_by_role("menuitemradio", name="Signal").click()
        assert page.evaluate("document.documentElement.dataset.theme") == theme
        fg, bg, ratio = _contrast_ratio(page.locator('#esw .ep[data-env="prod"]').first)
        assert page.locator('#esw .ep[data-env="prod"]').evaluate('''el => {
            const probe = document.createElement('span');
            probe.style.color = 'var(--production-fg)';
            probe.style.backgroundColor = 'var(--production-selection)';
            el.append(probe);
            const actual = getComputedStyle(el), expected = getComputedStyle(probe);
            const matches = actual.color === expected.color && actual.backgroundColor === expected.backgroundColor;
            probe.remove(); return matches;
        }''')
        assert ratio >= 4.5, (theme, fg, bg, ratio)


def test_prod_env_switch_does_not_autorun(page_envset):
    page = page_envset
    page.wait_for_selector('.dbrow[data-db="shop"]')
    page.locator('.dbrow[data-db="shop"]').click()
    page.wait_for_selector("#esw .ep")
    _run_sql(page, "select 1 as a")
    queries = []
    page.on("request", lambda r: "/api/query" in r.url and queries.append(r.url))
    page.locator('#esw .ep[data-env="prod"]').click()
    page.wait_for_selector("#prodBadge")                   # persistent connection context
    assert page.locator("#toast").is_hidden()
    page.wait_for_timeout(700)
    assert queries == []                                   # no auto-run happened
    assert page.locator("#grid table").count() == 0        # new prod group is empty


def test_nonprod_env_switch_restores_without_autorun(page_envset):
    page = page_envset
    page.wait_for_selector('.dbrow[data-db="shop"]')
    page.locator('.dbrow[data-db="shop"]').click()
    page.wait_for_selector("#esw .ep")
    _run_sql(page, "select 1 as a")
    page.locator('#esw .ep[data-env="prod"]').click()      # -> prod (no run)
    page.wait_for_selector("#prodBadge")
    assert page.locator("#toast").is_hidden()
    queries = []
    page.on("request", lambda r: "/api/query" in r.url and queries.append(r.url))
    page.locator('#esw .ep[data-env="dev"]').click()       # -> dev restores
    page.wait_for_selector("#grid table tbody tr")
    page.wait_for_timeout(300)
    assert queries == []
    assert page.locator("#sql").input_value() == "select 1 as a"
    assert page.locator('#grid td[data-v="1"]').count() == 1


def test_neptune_connection_opens_starter_without_autorun(page_neptune):
    page = page_neptune
    queries = []
    def query_response(route):
        queries.append(route.request.post_data_json)
        route.fulfill(json={
            "columns": [], "rows": [], "rowCount": 0, "truncated": False,
            "elapsedMs": 1, "engine": "neptune",
            "sql": "MATCH (n) RETURN n LIMIT 25", "downloadBytes": 14,
            "sizeIsEstimated": False,
        })
    page.route("**/api/query", query_response)

    _select_testpg(page)
    _set_sql(page, "select 42 as draft")
    page.locator('.dbrow[data-db="graph"]').click()
    assert page.locator("#sql").input_value() == "MATCH (n) RETURN n LIMIT 25"
    assert page.locator("#status").is_hidden()
    tabs = page.evaluate("JSON.parse(localStorage.getItem('qy_tabs'))")
    assert [(tab["db"], tab["env"], tab["sql"]) for tab in tabs] == [
        ("testpg", "test", "select 42 as draft"),
        ("graph", "dev", "MATCH (n) RETURN n LIMIT 25")
    ]

    page.locator('#esw .ep[data-env="local"]').click()
    assert page.locator("#sql").input_value() == "MATCH (n) RETURN n LIMIT 25"
    page.wait_for_selector("#status", state="visible")
    tabs = page.evaluate("JSON.parse(localStorage.getItem('qy_tabs'))")
    assert [(tab["db"], tab["env"]) for tab in tabs] == [
        ("testpg", "test"), ("graph", "dev"), ("graph", "local")
    ]

    page.locator('#esw .ep[data-env="dev"]').click()
    tabs = page.evaluate("JSON.parse(localStorage.getItem('qy_tabs'))")
    assert len(tabs) == 3
    assert page.locator("#sql").input_value() == "MATCH (n) RETURN n LIMIT 25"

    page.locator('#esw .ep[data-env="prod"]').click()
    page.wait_for_selector("#prodBadge")
    assert page.locator("#toast").is_hidden()
    assert page.locator("#sql").input_value() == "MATCH (n) RETURN n LIMIT 25"
    page.wait_for_timeout(200)
    assert [(q["db"], q["env"]) for q in queries] == [("graph", "local"), ("graph", "dev")]
    page.locator('#esw .ep[data-env="dev"]').click()
    page.wait_for_selector("#status", state="visible")
    assert [(query["db"], query["env"]) for query in queries] == [("graph", "local"), ("graph", "dev")]


def test_local_env_sorts_first_and_is_default_without_dev(page_envset_local):
    # "stash" registers prod before local, and has no dev env — local must
    # still be the leftmost pill/tab and the default-selected env (issue #44).
    page = page_envset_local
    page.wait_for_selector('.dbrow[data-db="stash"]')
    assert page.locator("#side .pill").count() == 0
    page.locator('.dbrow[data-db="stash"]').click()
    page.wait_for_selector("#esw .ep")
    assert page.locator("#side .pill").count() == 0
    eps = page.locator("#esw .ep")
    assert [eps.nth(i).get_attribute("data-env") for i in range(eps.count())] == ["local", "prod"]
    assert "on" in eps.nth(0).get_attribute("class").split()


def test_missing_group_env_sibling_stays_in_one_sidebar_env_set(page_missing_group):
    page = page_missing_group
    page.wait_for_selector('.dbrow[data-db="queue"]')
    assert page.locator('.dbrow[data-db="queue"]').count() == 1
    page.locator('.dbrow[data-db="queue"]').click()
    page.wait_for_selector("#esw .ep")
    eps = page.locator("#esw .ep")
    assert [eps.nth(i).get_attribute("data-env") for i in range(eps.count())] == ["local", "dev"]
    group = page.locator('[data-grp]', has_text="acme")
    assert group.count() == 1


MIXED_ENGINE_TOML = f"""
[shop]
url = "{TEST_DB_URL}"
engine = "postgres"
group = "acme"

[cache]
url = "redis://127.0.0.1:6379/0"
engine = "redis"
group = "acme"

[scratch]
url = "sqlite:///scratch.db"
engine = "sqlite"
group = "acme"
"""


def test_sidebar_groups_mixed_engines_under_workspace_group(_pw_browser, tmp_path):
    with _running_gui(tmp_path, extra_conn=MIXED_ENGINE_TOML) as url:
        ctx, page = _mk_page(_pw_browser, url)
        try:
            page.wait_for_selector('.dbrow[data-db="shop"]')
            assert page.locator("[data-engine]").count() == 0
            assert page.locator('.dbrow[data-db="shop"] small').inner_text() == "postgres"
            assert page.locator('.dbrow[data-db="cache"] small').inner_text() == "redis"
            assert page.locator('.dbrow[data-db="scratch"] small').inner_text() == "sqlite"
            names = [el.get_attribute("data-db") for el in page.locator(".dbrow").all()]
            assert names.index("shop") < names.index("scratch") < names.index("cache")
            assert page.locator("#side .pill").count() == 0
            errors = [e for e in page._console_errors
                      if "Failed to load resource" not in e and "net::ERR_" not in e]
            assert not errors, f"console errors: {errors}"
        finally:
            ctx.close()


# ---------------------------------------------------------------------------
# 2b. Proxy status badges (issue #101) — GUI must show observed, server-sourced
# proxy state, not something the frontend infers on its own.
# ---------------------------------------------------------------------------

PROXY_ENVSET_TOML = f"""
[shop_dev]
url = "{TEST_DB_URL}"
engine = "postgres"
env = "dev"
db = "shop"
group = "acme"
ssh_host = "bastion.example.com"
ssh_user = "ec2-user"

[shop_prod]
url = "{TEST_DB_URL}"
engine = "postgres"
env = "prod"
production = true
db = "shop"
group = "acme"
"""


@contextmanager
def _proxied_envset_page(browser, tmp_path, monkeypatch, *, enabled: bool, discovered: bool, reachable: bool = True):
    """Like page_envset, but shop_dev carries ssh_host and `tunnel.tunnel_fact_for()`
    is monkeypatched to simulate an already-established tunnel (issue #101
    r1-2: the badge reflects a *live* tunnel fact, not a prediction of what a
    fresh connection would do — merely toggling workspace config doesn't
    establish one, so the fake pool entry is what actually drives the
    badge here, gated by the same enabled/discovered/reachable combination
    a real tunnel's proxy decision would have been gated by)."""
    from quarry import tunnel

    db_host, db_port = tunnel._db_host_port(TEST_DB_URL, "postgres")
    live_tunnel = {
        "ssh_target": "ec2-user@bastion.example.com:22",
        "db_target": f"{db_host}:{db_port}",
        "local_port": 55555,
        "proxied": True,
        "proxy": "127.0.0.1:6152",
        "alive": True,
    }
    state = {"proxied": enabled and discovered and reachable}
    monkeypatch.setattr(tunnel, "tunnel_fact_for", lambda conn, engine:
                        live_tunnel if state["proxied"] and conn.ssh_host else None)
    with _running_gui(tmp_path, extra_conn=PROXY_ENVSET_TOML) as url:
        ctx, pg = _mk_page(browser, url)
        try:
            yield pg, state
        finally:
            ctx.close()


def test_proxy_badge_shown_only_on_the_tunneled_env_when_proxy_active(_pw_browser, tmp_path, monkeypatch):
    with _proxied_envset_page(_pw_browser, tmp_path, monkeypatch, enabled=True, discovered=True) as (page, _state):
        page.wait_for_selector('.dbrow[data-db="shop"]')
        page.locator('.dbrow[data-db="shop"]').click()
        page.wait_for_selector("#esw .ep")
        # shop_dev has a live, actually-proxied tunnel -> badge visible
        page.wait_for_selector('[data-testid="proxy-badge"][data-db="shop"][data-env="dev"]')
        # shop_prod has no ssh_host at all -> never a candidate for the badge
        assert page.locator('[data-testid="proxy-badge"][data-db="shop"][data-env="prod"]').count() == 0


def test_proxy_badge_hidden_when_workspace_toggle_off(_pw_browser, tmp_path, monkeypatch):
    with _proxied_envset_page(_pw_browser, tmp_path, monkeypatch, enabled=False, discovered=True) as (page, _state):
        page.wait_for_selector('.dbrow[data-db="shop"]')
        page.locator('.dbrow[data-db="shop"]').click()
        page.wait_for_selector("#esw .ep")
        assert page.locator('[data-testid="proxy-badge"]').count() == 0


def test_proxy_badge_hidden_when_nothing_discovered(_pw_browser, tmp_path, monkeypatch):
    with _proxied_envset_page(_pw_browser, tmp_path, monkeypatch, enabled=True, discovered=False) as (page, _state):
        page.wait_for_selector('.dbrow[data-db="shop"]')
        page.locator('.dbrow[data-db="shop"]').click()
        page.wait_for_selector("#esw .ep")
        assert page.locator('[data-testid="proxy-badge"]').count() == 0


def test_proxy_badge_follows_server_state_across_reload(_pw_browser, tmp_path, monkeypatch):
    """The badge is a live reflection of server state (not cached client-side):
    the underlying tunnel fact changing and reloading must flip the badge too."""
    with _proxied_envset_page(_pw_browser, tmp_path, monkeypatch, enabled=True, discovered=True) as (page, state):
        page.wait_for_selector('.dbrow[data-db="shop"]')
        page.locator('.dbrow[data-db="shop"]').click()
        page.wait_for_selector("#esw .ep")
        page.wait_for_selector('[data-testid="proxy-badge"][data-db="shop"][data-env="dev"]')

        state["proxied"] = False
        page.reload(wait_until="networkidle")
        page.wait_for_selector('.dbrow[data-db="shop"]')
        assert page.locator('[data-testid="proxy-badge"]').count() == 0


# ---------------------------------------------------------------------------
# 3. Request race (fix: latest-wins — a stale slow response never overwrites)
# ---------------------------------------------------------------------------

def test_stale_slow_response_does_not_overwrite(page):
    _select_testpg(page)
    _set_sql(page, "select pg_sleep(1.2), 'slow' as tag")
    page.locator("#runBtn").click()                       # slow query in flight
    _set_sql(page, "select 'fast' as tag")
    page.locator("#runBtn").click()                       # newer query wins
    page.wait_for_selector('#grid td[data-v="fast"]')
    page.wait_for_timeout(1600)                           # let the slow response land
    assert page.locator('#grid td[data-v="fast"]').count() == 1
    assert page.locator('#grid td[data-v="slow"]').count() == 0


# ---------------------------------------------------------------------------
# 4. Sort (fix: numeric strings sort numerically; 3rd click restores; reset on new result)
# ---------------------------------------------------------------------------

def test_sort_numeric_strings_and_third_click_restores(page):
    _select_testpg(page)
    _run_sql(page, "select n from (values ('9'),('10'),('2')) as v(n)")
    assert _col_values(page) == ["9", "10", "2"]
    th = page.locator('#grid th[data-i="0"]')
    th.click()                                            # asc, numeric-aware
    assert _col_values(page) == ["2", "9", "10"]
    page.locator('#grid th[data-i="0"]').click()          # desc
    assert _col_values(page) == ["10", "9", "2"]
    page.locator('#grid th[data-i="0"]').click()          # 3rd click -> original order
    assert _col_values(page) == ["9", "10", "2"]
    assert page.locator("#grid th .ar").count() == 0      # arrow cleared


def test_new_result_resets_sort_state(page):
    _select_testpg(page)
    _run_sql(page, "select n from (values ('9'),('2')) as v(n)")
    page.locator('#grid th[data-i="0"]').click()
    assert page.locator("#grid th .ar").count() == 1
    _run_sql(page, "select 1 as a")
    assert page.locator("#grid th .ar").count() == 0


# ---------------------------------------------------------------------------
# 5. Sidebar: table filter box
# ---------------------------------------------------------------------------

def test_table_filter_box(page):
    _select_testpg(page)
    page.locator(".sidebar-searchbar .tsearch").fill("cust")
    assert page.locator('.search-object').filter(has_text='customers').is_visible()
    assert page.locator('.search-object').filter(has_text='orders').count() == 0
    page.locator(".sidebar-searchbar .tsearch").fill("")
    assert page.locator('#tbl-panel .tname[data-t="orders"]').is_visible()


def test_sidebar_search_is_global_restores_tree_and_does_not_fetch(page_envset, tmp_path):
    page = page_envset
    page.route('**/api/queries', lambda route: route.fulfill(json=[dict(
        name='find-line', db='shop', desc='查找 LINE 记忆', sql='SELECT 1', params=[])]))
    page.reload(wait_until='networkidle')
    _select_testpg(page)
    page.locator('[data-t="customers"]').wait_for()
    _set_sql(page, 'select 77 as preserved_draft')
    page.locator('.dbrow[data-db="shop"]').click()
    page.locator('#tbl-panel[data-db="shop"] [data-t="customers"]').wait_for()
    # Capture all persisted expansion state; searching must not write it.
    before = page.evaluate('localStorage.qy_collapsed')
    active = page.locator('#tabs [aria-selected="true"]').get_attribute('title')
    requests = []
    page.on('request', lambda request: requests.append(request.url) if '/api/' in request.url else None)
    search = page.locator('.sidebar-searchbar input')
    search.fill('customers')
    results = page.locator('.sidebar-search-results')
    assert results.locator('.search-db').all_text_contents() == ['testpg', 'shop']
    assert results.locator('.search-object').count() >= 2
    page.screenshot(path=str(tmp_path / 'global-search.png'))
    search.fill('LINE')
    assert results.locator('[data-q="find-line"]').is_visible()
    assert results.locator('.search-db').inner_text() == 'shop'
    search.fill('does-not-exist-xyz')
    assert 'No matches' in results.inner_text()
    search.press('Escape')
    assert page.evaluate('localStorage.qy_collapsed') == before
    assert page.locator('#tabs [aria-selected="true"]').get_attribute('title') == active
    assert not requests
    search.fill('customers')
    results.locator('.search-object').first.click()
    page.wait_for_selector('#grid table')
    assert page.locator('#qtitle').inner_text() == 'testpg'
    assert page.evaluate("JSON.parse(localStorage.qy_tabs).some(tab => tab.sql === 'select 77 as preserved_draft')")
    sql = page.locator('#sql').input_value()
    page.get_by_role('button', name='Collapse all', exact=True).click()
    assert search.input_value() == ''
    assert page.locator('#sql').input_value() == sql
    assert page.locator('.sidebar-tree > div > .gbody').evaluate_all('els => els.every(el => getComputedStyle(el).display === "none")')


# ---------------------------------------------------------------------------
# 6. Grid states: empty result, truncation badge
# ---------------------------------------------------------------------------

def test_zero_rows_empty_state(page):
    _select_testpg(page)
    _set_sql(page, "select * from customers where false")
    page.locator("#runBtn").click()
    page.wait_for_selector('#grid .empty:has-text("0")')
    assert page.locator('#grid thead th[data-i="0"]').inner_text().startswith('id')


def test_lossless_numeric_sort_and_export(page, tmp_path):
    _select_testpg(page)
    _set_sql(page, "SELECT * FROM (VALUES (9007199254740993::bigint, "
             "0.123456789012345678901::numeric), (9007199254740992, "
             "0.123456789012345678902)) AS t(id,amount)")
    page.locator('#runBtn').click()
    page.wait_for_function("document.querySelector('#grid tbody td:nth-child(2)')?.textContent === '9007199254740993'")
    page.locator('#grid th[data-i="0"]').click()
    page.wait_for_function("document.querySelector('#grid tbody td:nth-child(2)')?.textContent === '9007199254740992'")
    with page.expect_download() as event:
        page.locator('#jsonBtn').click()
    path = tmp_path / 'exact.json'
    event.value.save_as(str(path))
    rows = json.loads(path.read_text())
    assert rows == [
        {'id': '9007199254740992', 'amount': '0.123456789012345678902'},
        {'id': '9007199254740993', 'amount': '0.123456789012345678901'},
    ]
    with page.expect_download() as event:
        page.locator('#csvBtn').click()
    csv_path = tmp_path / 'exact.csv'
    event.value.save_as(str(csv_path))
    assert '9007199254740993,0.123456789012345678901' in csv_path.read_text(encoding='utf-8-sig')
    # Both sort directions and restoring original order share the same result.
    page.locator('#grid th[data-i="0"]').click()
    page.locator('#grid th[data-i="0"]').click()
    assert _col_values(page)[0] == '9007199254740993'


def test_empty_columns_and_duplicate_names_export(page, tmp_path):
    _select_testpg(page)
    _set_sql(page, 'SELECT 1 AS "a,b", 2 AS id, 3 AS id WHERE false')
    page.locator('#runBtn').click()
    page.wait_for_selector('#grid .empty:has-text("0")')
    assert page.locator('#grid th[data-i]').count() == 3
    assert page.locator('#grid th[data-i="2"]').inner_text().startswith('id_2')
    with page.expect_download() as event:
        page.locator('#csvBtn').click()
    path = tmp_path / 'empty.csv'
    event.value.save_as(str(path))
    assert path.read_text(encoding='utf-8-sig') == '"a,b",id,id_2'


def test_truncated_badge_shows(page):
    _select_testpg(page)
    _run_sql(page, "select * from generate_series(1,600)")
    page.wait_for_selector("#status .tr")                 # "truncated to cap"
    assert page.locator("#grid tbody tr").count() == 500  # default maxRows


# ---------------------------------------------------------------------------
# 6a. Status bar: download size + avg speed (issue #106, built on #104's
#     downloadBytes/sizeIsEstimated)
# ---------------------------------------------------------------------------

def test_status_bar_shows_download_size_and_speed(page):
    _select_testpg(page)
    _run_sql(page, "select * from customers")
    dl = page.locator("#dlSize")
    speed = page.locator("#avgSpeed")
    dl.wait_for()
    # postgres goes through psql's subprocess stdout, so its byte count is an
    # approximation — both fields carry the '≈' prefix and an explanatory tooltip
    assert dl.inner_text().strip().startswith("≈")
    assert speed.inner_text().strip().startswith("≈")
    assert speed.inner_text().strip().endswith("/s")
    assert dl.get_attribute("title")
    assert speed.get_attribute("title")


def test_status_bar_metric_tooltips(page):
    # every status-bar metric explains itself on hover (same native `title`
    # pattern as the large-result session notice). The toolbar cap is per
    # fetch, so the rows tip must not just repeat the visible count.
    _select_testpg(page)
    page.select_option("#maxRows", "100")
    _run_sql(page, "select * from generate_series(1,250)")
    page.wait_for_selector("#statusTruncated")
    tips = {
        "#statusRows": "per-fetch",
        "#statusElapsed": "Wall time",
        "#dlSize": "Payload size",
        "#avgSpeed": "throughput",
        "#statusTruncated": "max-rows cap",
        "#loadMoreBtn": "next page",
        "#statusTarget": "Connection",
        "#maxRows": "per fetch",
    }
    for sel, needle in tips.items():
        title = page.locator(sel).get_attribute("title") or ""
        assert needle.lower() in title.lower(), (sel, title)
    assert "estimated" in (page.locator("#dlSize").get_attribute("title") or "").lower()
    assert "estimated" in (page.locator("#avgSpeed").get_attribute("title") or "").lower()


def test_load_more_paginates_truncated_result(page):
    # real pagination (issue: grid "load more" beyond the max-rows cap)
    _select_testpg(page)
    page.select_option("#maxRows", "100")
    _run_sql(page, "select * from generate_series(1,250)")
    page.wait_for_selector("#status .tr")
    assert page.locator("#grid tbody tr").count() == 100
    load_more = page.locator("#loadMoreBtn")
    load_more.wait_for()

    load_more.click()
    page.wait_for_function("document.querySelectorAll('#grid tbody tr').length === 200")
    assert page.locator("#status .tr").count() == 1       # still truncated (50 rows left)
    assert page.locator("#loadMoreBtn").count() == 1

    load_more.click()                                       # tail page: the remaining 50 rows
    page.wait_for_function("document.querySelectorAll('#grid tbody tr').length === 250")
    assert page.locator("#status .tr").count() == 0        # no longer truncated
    assert page.locator("#loadMoreBtn").count() == 0        # button gone once fully loaded

    # rows are contiguous, not duplicated / reshuffled by the two page fetches
    assert _col_values(page) == [str(n) for n in range(1, 251)]


_UNIT_BYTES = {"B": 1, "KB": 1024, "MB": 1024 * 1024}


def _parse_bytes(text: str) -> float:
    n, unit = re.search(r"([\d.]+)\s*(B|KB|MB)", text).groups()
    return float(n) * _UNIT_BYTES[unit]


def test_load_more_accumulates_download_size_and_speed(page):
    # each "load more" page must ADD to downloadBytes/avg speed, not reset them
    # to the new page alone (same accumulation rule as elapsedMs)
    _select_testpg(page)
    page.select_option("#maxRows", "100")
    _run_sql(page, "select * from generate_series(1,250)")
    page.wait_for_selector("#status .tr")
    dl_before = _parse_bytes(page.locator("#dlSize").inner_text())

    page.locator("#loadMoreBtn").click()
    page.wait_for_function("document.querySelectorAll('#grid tbody tr').length === 200")
    dl_after_1 = _parse_bytes(page.locator("#dlSize").inner_text())
    assert dl_after_1 > dl_before

    page.locator("#loadMoreBtn").click()
    page.wait_for_function("document.querySelectorAll('#grid tbody tr').length === 250")
    dl_after_2 = _parse_bytes(page.locator("#dlSize").inner_text())
    assert dl_after_2 > dl_after_1

    # avg speed re-derives from the accumulated totals, not just the last page
    speed_text = page.locator("#avgSpeed").inner_text()
    assert speed_text.endswith("/s")
    assert _parse_bytes(speed_text.removesuffix("/s")) > 0


def test_load_more_keeps_active_sort_applied(page):
    # fix: loading more while the grid is sorted must re-sort the combined
    # rows, not just append the next page's raw SQL order under a stale arrow
    _select_testpg(page)
    page.select_option("#maxRows", "100")
    _run_sql(page, "select * from generate_series(1,250)")
    page.wait_for_selector("#status .tr")
    th = page.locator('#grid th[data-i="0"]')
    th.click()                                              # asc
    th.click()                                              # desc: 100..1
    assert _col_values(page) == [str(n) for n in range(100, 0, -1)]

    page.locator("#loadMoreBtn").click()
    page.wait_for_function("document.querySelectorAll('#grid tbody tr').length === 200")
    assert page.locator("#grid th .ar").count() == 1        # sort indicator still active
    assert _col_values(page) == [str(n) for n in range(200, 0, -1)]

    page.locator("#loadMoreBtn").click()
    page.wait_for_function("document.querySelectorAll('#grid tbody tr').length === 250")
    assert _col_values(page) == [str(n) for n in range(250, 0, -1)]


# ---------------------------------------------------------------------------
# 7. History: empty toast, search filter
# ---------------------------------------------------------------------------

def test_history_empty_keeps_conversations_accessible(page):
    page.locator("#histBtn").click()
    page.get_by_test_id("history-queries").wait_for()
    assert page.locator(".modal .hitem").count() == 0
    assert page.get_by_test_id("history-conversations").is_visible()


def test_history_search_filters(page):
    _select_testpg(page)
    _run_sql(page, "select 11 as aa_marker")
    _run_sql(page, "select 22 as bb_marker")
    page.locator("#histBtn").click()
    page.wait_for_selector(".modal .hitem")
    page.locator(".modal .hsearch").fill("aa_marker")
    page.wait_for_function(
        "document.querySelectorAll('.modal .hitem').length === 1")
    assert "aa_marker" in page.locator(".modal .hitem").inner_text()


# ---------------------------------------------------------------------------
# 8. EXPLAIN guard without a connection
# ---------------------------------------------------------------------------

def test_explain_without_connection_toasts(page):
    page.locator("#expBtn").click()
    page.wait_for_selector("#toast", state="visible")
    assert page.locator(".modal").count() == 0


# ---------------------------------------------------------------------------
# 9. Cell modal: JSON tree
# ---------------------------------------------------------------------------

def test_cell_json_opens_tree_modal(page):
    _select_testpg(page)
    _run_sql(page, """select '{"a":1,"b":[1,2,3]}'::jsonb as blob""")
    page.locator("#grid td.json").dblclick()
    tree = page.locator(".modal details.jt")
    tree.wait_for()                                        # tree exists but is lazy/collapsed
    assert page.locator(".modal .jk", has_text="a").count() == 0
    summary = tree.locator("summary")
    summary.hover()
    assert summary.get_attribute("title") == "Expand group"
    summary.click()
    page.wait_for_function("document.querySelector('.modal details.jt > summary')?.title === 'Collapse group'")
    page.wait_for_selector(".modal .jk")                  # children mount only after expansion
    assert page.locator(".modal .jk", has_text="a").count() >= 1
    page.keyboard.press("Escape")
    page.wait_for_selector(".modal", state="detached")


def test_large_cell_is_bounded_in_dom_and_kept_session_only(page):
    _select_testpg(page)
    _run_sql(page, "select repeat('x', 600000) as payload")

    cell = page.locator('#grid td[data-truncated="true"]')
    cell.wait_for()
    assert len(cell.inner_text()) == 512
    assert cell.inner_text().endswith("…")
    assert cell.get_attribute("data-v") is None            # no full-value DOM attribute copy
    assert "600000" not in (cell.get_attribute("title") or "")
    assert page.evaluate("document.querySelector('#grid').innerHTML.length") < 5_000
    assert page.locator("#resultNotSaved").is_visible()
    assert page.evaluate("JSON.parse(localStorage.getItem('qy_tabres'))") == [None]

    # Row detail is bounded too; the explicit cell inspector starts at one
    # 20k chunk instead of mounting the entire 600k value.
    page.locator("#grid td.rownum").click()
    detail = page.locator('.modal td[data-truncated="true"]')
    assert len(detail.inner_text()) == 512
    page.keyboard.press("Escape")
    cell.dblclick()
    modal_preview = page.locator(".modal pre")
    modal_preview.wait_for()
    assert len(modal_preview.inner_text()) == 20_000
    assert page.locator(".modal .ciactions", has_text="Show more").count() == 1


def test_large_cell_copy_uses_full_value_not_preview(page_clip):
    page = page_clip
    _select_testpg(page)
    _run_sql(page, "select repeat('x', 600000) as payload")
    cell = page.locator('#grid td[data-truncated="true"]')
    cell.dblclick()
    page.locator("#cpy").click()
    page.wait_for_selector(".modal", state="detached")
    assert page.evaluate("navigator.clipboard.readText().then(v => v.length)") == 600_000


# ---------------------------------------------------------------------------
# 10. Grid keyboard navigation
# ---------------------------------------------------------------------------

def test_grid_keyboard_nav_and_enter_opens_modal(page):
    _select_testpg(page)
    _run_sql(page, "select * from customers order by id")
    first = page.locator("#grid tbody tr").first.locator("td:not(.rownum)").first
    first.click()
    page.wait_for_selector("#grid td.sel")
    page.keyboard.press("ArrowRight")
    page.keyboard.press("ArrowDown")
    pos = page.evaluate(
        "(() => { const td = document.querySelector('#grid td.sel');"
        "return {row: td.parentElement.sectionRowIndex, col: td.cellIndex}; })()")
    assert pos == {"row": 1, "col": 2}
    page.keyboard.press("Enter")                          # open the selected cell
    page.wait_for_selector(".modal")
    page.keyboard.press("Escape")
    page.wait_for_selector(".modal", state="detached")


# ---------------------------------------------------------------------------
# 11. Persistence across reloads: theme, editor+result, group collapse
# ---------------------------------------------------------------------------

def test_theme_persists_after_reload(page):
    page.locator(".vg-switcher-mode").click()
    assert page.evaluate("document.documentElement.dataset.mode") == "light"
    page.reload(wait_until="networkidle")
    assert page.evaluate("document.documentElement.dataset.mode") == "light"


def test_editor_and_result_restored_after_reload(page):
    _select_testpg(page)
    _run_sql(page, "select 41+1 as answer")
    page.wait_for_selector('#grid td[data-v="42"]')
    page.reload(wait_until="networkidle")
    page.wait_for_selector('.dbrow.on[data-db="testpg"]')     # connection restored
    assert page.locator("#sql").input_value() == "select 41+1 as answer"
    page.wait_for_selector('#grid td[data-v="42"]')           # result grid restored
    assert page.locator("#qtitle").inner_text() == "testpg"


def test_group_collapse_persists_after_reload(page):
    page.wait_for_selector("#side [data-grp]")
    page.locator("#side [data-grp]").first.click()            # collapse
    assert page.locator("#side .gbody").first.is_hidden()
    page.reload(wait_until="networkidle")
    page.wait_for_selector("#side [data-grp]")
    assert page.locator("#side .gbody").first.is_hidden()


# ---------------------------------------------------------------------------
# 12. Saved query without params runs on click
# ---------------------------------------------------------------------------

def test_saved_query_without_params_runs_directly(page_noparam):
    page = page_noparam
    page.wait_for_selector('.qname[data-q="all-cust"]')
    page.locator('.qname[data-q="all-cust"]').click()
    page.wait_for_selector("#grid table tbody tr")            # ran without a modal
    assert page.locator(".modal").count() == 0
    assert page.locator("#grid tbody tr").count() == 3
    assert "customers" in page.locator("#sql").input_value().lower()
    assert page.locator('[data-qsrc="testpg"]').inner_text() == "testpg"
    page.get_by_role('button', name='Collapse all', exact=True).click()
    assert page.locator('#grid tbody tr').count() == 3
    page.reload(wait_until='networkidle')
    assert page.locator('#sql').get_attribute('readonly') is not None
    assert page.locator('#grid tbody tr').count() == 3


def test_saved_queries_group_by_logical_db(_pw_browser, tmp_path):
    queries = {
        "session-west": "-- @name: session-west\n-- @db: shop_dev\nSELECT 1\n",
        "session-east": "-- @name: session-east\n-- @db: shop_prod\nSELECT 1\n",
        "all-cust": "-- @name: all-cust\n-- @db: testpg\nSELECT 1\n",
    }
    with _running_gui(tmp_path, extra_conn=ENVSET_TOML, seed_queries=queries) as url:
        ctx, page = _mk_page(_pw_browser, url)
        try:
            page.wait_for_selector('.qname[data-q="session-west"]')
            assert page.locator('[data-qsrc="shop"]').count() == 1
            assert page.locator('[data-qsrc="shop_dev"]').count() == 0
            assert page.locator('[data-qsrc="testpg"]').count() == 1
            shop = page.locator('[data-qsrc="shop"]').locator("xpath=..")
            assert shop.locator('.qname[data-q="session-west"]').count() == 1
            assert shop.locator('.qname[data-q="session-east"]').count() == 1
            assert shop.locator('.qname[data-q="all-cust"]').count() == 0
            assert all('queries' in title.lower() for title in page.locator('[data-saved-ws]').all_inner_texts())
            errors = [e for e in page._console_errors
                      if "Failed to load resource" not in e and "net::ERR_" not in e]
            assert not errors, f"console errors: {errors}"
        finally:
            ctx.close()


def test_saved_query_result_persisted_under_producing_connection(page_saved_multi):
    """A saved query runs on ITS OWN connection (testpg). When launched from a tab
    bound to a different connection (shop@dev), the result must be tagged & persisted
    under the producing connection (testpg), never the tab's previous connection."""
    page = page_saved_multi
    page.wait_for_selector('.dbrow[data-db="shop"]')
    page.locator('.dbrow[data-db="shop"]').click()            # bind the active tab to shop@dev
    page.wait_for_selector("#esw .ep")
    _set_sql(page, 'select 7 as keep_shop_draft')
    page.wait_for_selector('.qname[data-q="all-cust"]')
    page.locator('.qname[data-q="all-cust"]').click()         # run in the testpg group
    page.wait_for_selector("#grid table tbody tr")
    saved = page.evaluate("JSON.parse(localStorage.getItem('qy_tabres'))")
    assert saved[1]["db"] == "testpg"                         # producing conn, not shop
    tabs = page.evaluate("JSON.parse(localStorage.getItem('qy_tabs'))")
    assert tabs[0]["db"] == "shop"
    assert tabs[0]["sql"] == "select 7 as keep_shop_draft"
    assert tabs[1]["db"] == "testpg"
    # and it survives a reload under the producing connection
    page.reload(wait_until="networkidle")
    page.wait_for_selector('.dbrow[data-db="testpg"].on')
    page.wait_for_selector("#grid table tbody tr")
    assert page.locator("#grid tbody tr").count() == 3


# ---------------------------------------------------------------------------
# 13. Autocomplete: table.column via /api/columns
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("late_source", ["input_frame", "column_lookup"])
def test_autocomplete_escape_blocks_late_refresh(page, late_source):
    _select_testpg(page)
    ta = page.locator("#sql")
    ta.fill("sel")
    page.wait_for_selector(".acbox", state="visible")
    if late_source == "input_frame":
        page.evaluate("""() => {
            window.originalRaf = window.requestAnimationFrame;
            window.pendingFrames = [];
            window.requestAnimationFrame = cb => {
                window.pendingFrames.push(cb); return 999999;
            };
        }""")
        ta.type("e")
    else:
        page.evaluate("""() => {
            const originalFetch = window.fetch;
            window.fetch = (...args) => String(args[0]).includes('/api/columns?')
                ? new Promise(resolve => {
                    window.releaseColumns = () => resolve(new Response(
                        JSON.stringify({columns: ['name', 'email']}),
                        {headers: {'Content-Type': 'application/json'}}));
                }) : originalFetch(...args);
        }""")
        ta.fill("customers.")
        page.wait_for_function("typeof window.releaseColumns === 'function'")
    ta.press("Escape")
    page.wait_for_selector(".acbox", state="hidden")
    page.evaluate("""async source => {
        if (source === 'input_frame') {
            window.requestAnimationFrame = window.originalRaf;
            window.pendingFrames.forEach(cb => cb(performance.now()));
        } else {
            window.releaseColumns();
        }
        await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    }""", late_source)
    assert not page.locator(".acbox").is_visible()
    # Dismissal is temporary: the next edit must offer suggestions again.
    ta.type("c" if late_source == "input_frame" else "n")
    page.wait_for_selector(".acbox", state="visible")
    assert page.locator(".acitem", has_text="SELECT" if late_source == "input_frame" else "name").count() >= 1


def test_autocomplete_columns_after_table_dot(page):
    _select_testpg(page)
    page.locator("#sql").focus()
    page.keyboard.type("select customers.")
    page.wait_for_selector(".acbox .acitem .ack-col", timeout=8000)
    items = page.locator(".acbox .acitem").all_inner_texts()
    assert any("id" in it for it in items)
    page.keyboard.press("Escape")                             # close cleanly
    page.wait_for_selector(".acbox", state="hidden")


# ---------------------------------------------------------------------------
# 14. Redis: key tree, badges, filter, inspect
# ---------------------------------------------------------------------------

def test_redis_key_tree_badges_filter_and_inspect(page_redis):
    page = page_redis
    page.wait_for_selector('.dbrow[data-db="testredis"]')
    page.locator('.dbrow[data-db="testredis"]').click()
    page.wait_for_selector('#tbl-panel .tname[data-key="qygui:sess:1"]', timeout=15000)
    folder = page.locator("#tbl-panel .knode").first
    folder.hover()
    assert folder.get_attribute("title") == "Collapse group"
    folder.click()
    assert folder.get_attribute("title") == "Expand group"
    folder.click()
    # tree: qygui -> sess -> leaves; TTL badge on sess:1; type badge on jobs
    leaf1 = page.locator('#tbl-panel .tname[data-key="qygui:sess:1"]')
    assert leaf1.locator(".rbadge.ttl").count() == 1
    jobs = page.locator('#tbl-panel .tname[data-key="qygui:jobs"]')
    assert "list" in jobs.locator(".rbadge").first.inner_text()
    # filter narrows the tree
    page.locator(".sidebar-searchbar .tsearch").fill("jobs")
    assert page.locator('.search-object').filter(has_text='qygui:sess:1').count() == 0
    assert page.locator('.search-object').filter(has_text='qygui:jobs').count() == 1
    page.locator(".sidebar-searchbar .tsearch").fill("")
    # inspect a key -> grid renders, editor shows the inspect marker
    page.wait_for_selector('#tbl-panel .tname[data-key="qygui:sess:1"]')
    page.locator('#tbl-panel .tname[data-key="qygui:sess:1"]').click()
    page.wait_for_selector("#grid table tbody tr")
    assert page.locator("#sql").input_value() == "# qygui:sess:1"
    # redis editor placeholder + EXPLAIN is refused with a toast
    assert "redis" in page.locator("#sql").get_attribute("placeholder")
    page.locator("#expBtn").click()
    page.wait_for_selector("#toast", state="visible")
    assert page.locator(".modal").count() == 0


def test_redis_capped_key_list_shows_notice(page_redis_capped):
    page = page_redis_capped
    page.wait_for_selector('.dbrow[data-db="testredis"]')
    page.locator('.dbrow[data-db="testredis"]').click()
    page.wait_for_selector("#tbl-panel .hmeta", timeout=20000)   # "showing only the first N keys"
    note = page.locator("#tbl-panel .hmeta").inner_text()
    assert any(ch.isdigit() for ch in note)


# ---------------------------------------------------------------------------
# 15. Health-check button: ok + down dots, error tooltip, cached repaint
# ---------------------------------------------------------------------------

def test_health_button_paints_ok_and_down_dots(page_dead):
    page = page_dead
    page.wait_for_selector('.dbrow[data-db="deadpg"]')
    page.locator("#healthBtn").click()
    page.wait_for_selector('.dbrow[data-db="testpg"] .dot.ok', timeout=20000)
    page.wait_for_selector('.dbrow[data-db="deadpg"] .dot.down', timeout=20000)
    row = page.locator('.dbrow[data-db="deadpg"]')
    assert "down" in row.get_attribute("class").split()          # dimmed row
    assert (row.get_attribute("title") or "").strip()            # error tooltip


def test_health_dots_repaint_from_cache_after_reload(page):
    page.locator("#healthBtn").click()
    page.wait_for_selector('.dbrow[data-db="testpg"] .dot.ok', timeout=20000)
    page.reload(wait_until="networkidle")
    # no click: dots repaint from the backend health cache (cached=1)
    page.wait_for_selector('.dbrow[data-db="testpg"] .dot.ok', timeout=10000)


def test_dead_connection_click_shows_error_panel(page_dead):
    page = page_dead
    page.wait_for_selector('.dbrow[data-db="deadpg"]')
    page.locator('.dbrow[data-db="deadpg"]').click()
    page.wait_for_selector("#tbl-panel .empty", timeout=20000)   # error text, not a spinner
    assert page.locator("#tbl-panel .empty").inner_text().strip()
    page.wait_for_selector('.dbrow[data-db="deadpg"] .dot.down')


# ---------------------------------------------------------------------------
# 16. SWR: cached table list refreshes in the background
# ---------------------------------------------------------------------------

def test_swr_refreshes_stale_table_list(page, pg_exec):
    _select_testpg(page)                                  # populates the backend cache
    pg_exec("CREATE TABLE qyswr_zzz (id int)")
    try:
        page.reload(wait_until="networkidle")             # restore auto-selects testpg
        # cached paint lacks the new table; the SWR fresh fetch brings it in
        page.wait_for_selector('#tbl-panel .tname[data-t="qyswr_zzz"]', timeout=20000)
    finally:
        pg_exec("DROP TABLE IF EXISTS qyswr_zzz")


# ---------------------------------------------------------------------------
# 17. Layout drags persist: sidebar width, editor height, column width
# ---------------------------------------------------------------------------

def _drag(page, selector, dx, dy):
    box = page.locator(selector).bounding_box()
    x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    page.mouse.move(x, y)
    page.mouse.down()
    page.mouse.move(x + dx, y + dy, steps=4)
    page.mouse.up()


def test_sidebar_width_drag_persists(page):
    w0 = page.evaluate("document.querySelector('#side').offsetWidth")
    _drag(page, "#resizer", 80, 0)
    w1 = page.evaluate("document.querySelector('#side').offsetWidth")
    assert w1 >= w0 + 60
    page.reload(wait_until="networkidle")
    assert abs(page.evaluate("document.querySelector('#side').offsetWidth") - w1) <= 2


def test_editor_height_drag_persists(page):
    h0 = page.evaluate("document.querySelector('.edwrap').offsetHeight")
    _drag(page, "#edresizer", 0, 60)
    h1 = page.evaluate("document.querySelector('.edwrap').offsetHeight")
    assert h1 >= h0 + 40
    page.reload(wait_until="networkidle")
    assert abs(page.evaluate("document.querySelector('.edwrap').offsetHeight") - h1) <= 2


def test_column_width_drag(page):
    _select_testpg(page)
    _run_sql(page, "select * from customers order by id")
    w0 = page.evaluate("document.querySelector('#grid th[data-i=\"0\"]').offsetWidth")
    _drag(page, '#grid th[data-i="0"] .rz', 70, 0)
    w1 = page.evaluate("document.querySelector('#grid th[data-i=\"0\"]').offsetWidth")
    assert w1 >= w0 + 50


# ---------------------------------------------------------------------------
# 18. Editor chrome: highlight overlay, placeholder states, tab title, migration
# ---------------------------------------------------------------------------

def test_sql_highlight_overlay(page):
    _set_sql(page, "select 'txt' from t -- note")
    hl = page.evaluate("document.querySelector('#hl').innerHTML")
    assert "tok-kw" in hl and "tok-str" in hl and "tok-cm" in hl


def test_placeholder_states(page):
    ph0 = page.locator("#sql").get_attribute("placeholder")
    assert "Pick a connection" in ph0
    _select_testpg(page)
    ph1 = page.locator("#sql").get_attribute("placeholder")
    assert ph1 != ph0 and "SQL" in ph1


def test_tab_title_shows_db_at_env(page):
    _select_testpg(page)
    page.wait_for_selector(".tab.on")
    assert page.locator(".tab.on").inner_text().startswith("testpg@test")


def test_tab_title_derives_from_sql_table_and_distinguishes_tabs(page):
    _select_testpg(page)
    _set_sql(page, "select * from customers")
    page.wait_for_function(
        "document.querySelector('.tab[data-i=\"0\"] .lbl').textContent.includes('customers')")

    page.locator("#tabAdd").click()                        # inherits testpg@test from tab 0
    _set_sql(page, "select * from orders")
    page.wait_for_function(
        "document.querySelector('.tab[data-i=\"1\"] .lbl').textContent.includes('orders')")

    title0 = page.locator('.tab[data-i="0"] .lbl').inner_text()
    title1 = page.locator('.tab[data-i="1"] .lbl').inner_text()
    assert title0 != title1
    assert "customers" in title0 and "orders" in title1


def test_tab_title_updates_when_sql_switches_table(page):
    _select_testpg(page)
    _set_sql(page, "select * from customers")
    page.wait_for_function(
        "document.querySelector('.tab[data-i=\"0\"] .lbl').textContent.includes('customers')")
    before = page.locator('.tab[data-i="0"] .lbl').inner_text()

    _set_sql(page, "select * from orders")
    page.locator("#runBtn").click()
    _run_result(page)
    page.wait_for_function(
        "document.querySelector('.tab[data-i=\"0\"] .lbl').textContent.includes('orders')")
    after = page.locator('.tab[data-i="0"] .lbl').inner_text()
    assert before != after


def test_legacy_qy_ui_migrates_into_tabs(page):
    page.evaluate(
        "localStorage.setItem('qy_ui', JSON.stringify({sql:'select 5 as mig'}));"
        "localStorage.removeItem('qy_tabs'); localStorage.removeItem('qy_ati');")
    page.reload(wait_until="networkidle")
    page.wait_for_function("document.querySelector('#sql').value === 'select 5 as mig'")


# ---------------------------------------------------------------------------
# 19. Autocomplete: FROM/JOIN narrows to tables only
# ---------------------------------------------------------------------------

def test_autocomplete_from_narrows_to_tables(page):
    _select_testpg(page)
    page.locator("#sql").focus()
    page.keyboard.type("select * from cust")
    page.wait_for_selector(".acbox .acitem")
    kinds = page.evaluate("[...document.querySelectorAll('.acbox .acitem .ack')].map(e=>e.textContent)")
    assert kinds and all(k == "tbl" for k in kinds)       # after FROM: tables only
    page.keyboard.press("Escape")
    page.keyboard.press("ControlOrMeta+a")
    page.keyboard.type("sel")
    page.wait_for_selector(".acbox .acitem .ack-kw")      # bare word: keywords offered


# ---------------------------------------------------------------------------
# 20. Toolbar: loading spinner, maxRows cap, network-error message
# ---------------------------------------------------------------------------

def test_run_shows_loading_spinner(page):
    _select_testpg(page)
    _set_sql(page, "select pg_sleep(0.8), 1 as a")
    page.locator("#runBtn").click()
    page.wait_for_selector("#grid .spin")                 # spinner while running
    page.wait_for_selector("#grid table", timeout=15000)


def test_max_rows_selector_caps_and_persists(page):
    _select_testpg(page)
    page.select_option("#maxRows", "100")
    _run_sql(page, "select * from generate_series(1,200)")
    assert page.locator("#grid tbody tr").count() == 100
    page.wait_for_selector("#status .tr")                 # truncated badge
    page.reload(wait_until="networkidle")
    assert page.locator("#maxRows").input_value() == "100"


@pytest.mark.parametrize('lang', ['en', 'zh'])
def test_row_limit_selector_has_readable_label_and_keyboard_control(page, lang):
    from playwright.sync_api import expect

    _select_testpg(page)
    if lang == 'zh':
        page.locator('.vg-lang-switch').click()
    limit = page.locator('#maxRows')
    assert limit.get_attribute('aria-label') == limit.get_attribute('title')
    assert limit.get_attribute('title')
    page.select_option('#maxRows', '100')
    assert limit.locator('option:checked').inner_text() == ('100 rows' if lang == 'en' else '100 行')
    requests = []
    page.on('request', lambda req: requests.append(req.url) if req.method == 'POST' and
            (req.url.endswith('/api/query') or req.url.endswith('/api/run')) else None)
    limit.focus()
    # Native type-ahead works across platforms without depending on an OS
    # popup, which headless Chromium on macOS does not expose to arrow keys.
    limit.press_sequentially('5000')
    limit.press('Enter')
    expect(limit).to_have_value('5000')
    assert limit.locator('option:checked').inner_text() == ('5000 rows' if lang == 'en' else '5000 行')
    assert not requests
    page.reload(wait_until='networkidle')
    expect(page.locator('#maxRows')).to_have_value('5000')


def test_network_error_shows_readable_message(page):
    _select_testpg(page)
    page.route("**/api/query", lambda route: route.abort())
    _set_sql(page, "select 1")
    page.locator("#runBtn").click()
    page.wait_for_selector("#grid .err")
    msg = page.locator("#grid .err").inner_text().strip()
    assert msg and msg != "{}"                            # not the old JSON.stringify(TypeError)


# ---------------------------------------------------------------------------
# 21. Exports: CSV content (BOM + escaping), JSON content
# ---------------------------------------------------------------------------

def test_csv_export_content_bom_and_escaping(page):
    _select_testpg(page)
    _run_sql(page, "select 'a,b' as x, 'q\"t' as y, null as z")
    with page.expect_download() as dl:
        page.locator("#csvBtn").click()
    d = dl.value
    assert d.suggested_filename == "quarry-testpg.csv"
    content = open(d.path(), encoding="utf-8").read()
    assert content.startswith("\ufeff")                  # Excel-safe BOM
    assert content[1:] == 'x,y,z\n"a,b","q""t",'


def test_json_export_content(page):
    _select_testpg(page)
    _run_sql(page, "select 'a' as x, 2 as n")
    with page.expect_download() as dl:
        page.locator("#jsonBtn").click()
    d = dl.value
    assert d.suggested_filename == "quarry-testpg.json"
    assert json.load(open(d.path(), encoding="utf-8")) == [{"x": "a", "n": 2}]


# ---------------------------------------------------------------------------
# 22. Clipboard: Cmd+C on a selected cell; dblclick-copies short values
# ---------------------------------------------------------------------------

def test_cell_copy_via_keyboard_and_dblclick(page_clip):
    page = page_clip
    _select_testpg(page)
    _run_sql(page, "select 'copyme' as v")
    cell = page.locator('#grid td[data-v="copyme"]')
    cell.click()
    page.keyboard.press("ControlOrMeta+c")
    page.wait_for_selector("#toast", state="visible")
    assert page.evaluate("navigator.clipboard.readText()") == "copyme"
    page.wait_for_selector("#toast", state="hidden")      # ok-toast auto-hides
    cell.dblclick()                                       # short non-JSON value -> copy
    page.wait_for_selector("#toast", state="visible")
    assert page.evaluate(
        "document.querySelector('#toast').style.background").endswith("--ok-bg)")


# ---------------------------------------------------------------------------
# 23. Grid cell type coloring
# ---------------------------------------------------------------------------

def test_cell_type_coloring(page):
    _select_testpg(page)
    _run_sql(page, "select 1 as n, 'a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11' as u,"
                   " '2024-01-02 03:04:05' as ts, true as b, null as z")
    for cls in ("num", "uuid", "ts", "bool", "null"):
        assert page.locator(f"#grid td.{cls}").count() >= 1, cls


# ---------------------------------------------------------------------------
# 24. Sidebar: re-click toggles the table panel
# ---------------------------------------------------------------------------

def test_reclick_connection_toggles_panel(page):
    _select_testpg(page)
    page.locator('.dbrow[data-db="testpg"]').click()
    assert page.locator("#tbl-panel").is_hidden()
    page.locator('.dbrow[data-db="testpg"]').click()
    assert page.locator("#tbl-panel").is_visible()


# ---------------------------------------------------------------------------
# 25. Saved-query param modal: Enter submits, click-out closes
# ---------------------------------------------------------------------------

def test_param_modal_enter_submits_and_clickout_closes(page_saved):
    page = page_saved
    page.wait_for_selector('.qname[data-q="cust-by-id"]')
    page.locator('.qname[data-q="cust-by-id"]').click()
    page.wait_for_selector(".modal input.pf")
    page.locator('.modal input.pf[data-p="id"]').fill("1")
    page.keyboard.press("Enter")                          # Enter submits
    page.wait_for_selector(".modal", state="detached")
    page.wait_for_selector("#grid table tbody tr")
    page.locator('#runBtn').click()   # Re-run explicitly; selecting a fixed tab only restores it.
    page.wait_for_selector(".modal input.pf")
    page.locator(".modal").click(position={"x": 8, "y": 8})   # backdrop click closes
    page.wait_for_selector(".modal", state="detached")


# ---------------------------------------------------------------------------
# 26. Language toggle applies to the whole chrome
# ---------------------------------------------------------------------------

def test_language_toggle_full_chrome(page):
    page.locator(".vg-lang-switch").click()                # -> zh, reloads
    # optional chaining: avoid throwing mid-reload before #runLbl remounts
    # (same CI flake fixed in test_gui_visual.py, issue #94)
    page.wait_for_function("document.querySelector('#runLbl')?.textContent === '运行'")
    assert page.locator("#fmtLbl").inner_text() == "格式化"
    assert page.locator("#histLbl").inner_text() == "历史"
    assert page.locator("#roBadge").count() == 0


# ---------------------------------------------------------------------------
# 27. History modal closes on Escape
# ---------------------------------------------------------------------------

def test_history_modal_escape_closes(page):
    _select_testpg(page)
    _run_sql(page, "select 1 as a")
    page.locator("#histBtn").click()
    page.wait_for_selector(".modal .hitem")
    page.keyboard.press("Escape")
    page.wait_for_selector(".modal", state="detached")


# ---------------------------------------------------------------------------
# 28. Mixed-case table names are quoted when generated
# ---------------------------------------------------------------------------

def test_mixed_case_table_click_is_quoted(page, pg_exec):
    pg_exec('CREATE TABLE "QyCamelZz" (id int); INSERT INTO "QyCamelZz" VALUES (7)')
    try:
        _select_testpg(page)
        page.wait_for_selector('#tbl-panel .tname[data-t="QyCamelZz"]', timeout=20000)
        page.locator('#tbl-panel .tname[data-t="QyCamelZz"]').click()
        page.wait_for_selector('#grid td[data-v="7"]')    # would 400 if unquoted
        assert '"QyCamelZz"' in page.locator("#sql").input_value()
    finally:
        pg_exec('DROP TABLE IF EXISTS "QyCamelZz"')


def test_tab_title_distinguishes_quoted_mixed_case_tables(page, pg_exec):
    pg_exec('CREATE TABLE "QyCamelZz" (id int); CREATE TABLE "QyCamelAa" (id int)')
    try:
        _select_testpg(page)
        page.wait_for_selector('#tbl-panel .tname[data-t="QyCamelZz"]', timeout=20000)
        page.locator('#tbl-panel .tname[data-t="QyCamelZz"]').click()
        page.wait_for_function(
            "document.querySelector('.tab[data-i=\"0\"] .lbl').textContent === 'QyCamelZz'")

        page.locator("#tabAdd").click()                    # inherits testpg@test from tab 0
        page.wait_for_selector('#tbl-panel .tname[data-t="QyCamelAa"]')
        page.locator('#tbl-panel .tname[data-t="QyCamelAa"]').click()
        page.wait_for_function(
            "document.querySelector('.tab[data-i=\"1\"] .lbl').textContent === 'QyCamelAa'")

        title0 = page.locator('.tab[data-i="0"] .lbl').inner_text()
        title1 = page.locator('.tab[data-i="1"] .lbl').inner_text()
        assert title0 != title1
    finally:
        pg_exec('DROP TABLE IF EXISTS "QyCamelZz"; DROP TABLE IF EXISTS "QyCamelAa"')


# ---------------------------------------------------------------------------
# 29. Tabs: per-tab result isolation (grid/status/export follow the active tab)
# ---------------------------------------------------------------------------

def test_tab_switch_isolates_results(page):
    _select_testpg(page)
    _run_sql(page, "select 1 as a")
    page.wait_for_selector('#grid td[data-v="1"]')
    page.locator("#tabAdd").click()                       # new tab: grid must clear
    page.wait_for_selector("#grid .empty")
    assert page.locator("#status").is_hidden()
    _run_sql(page, "select 2 as b")
    page.wait_for_selector('#grid td[data-v="2"]')
    page.locator('.tab[data-i="0"]').click()              # back to tab 1: its result returns
    page.wait_for_selector('#grid td[data-v="1"]')
    assert page.locator('#grid td[data-v="2"]').count() == 0
    with page.expect_download() as dl:                    # export = active tab's data
        page.locator("#csvBtn").click()
    assert "a\n1" in open(dl.value.path(), encoding="utf-8").read()
    page.locator('.tab[data-i="1"]').click()
    page.wait_for_selector('#grid td[data-v="2"]')


# ---------------------------------------------------------------------------
# 30. Tabs: closing a tab never silently loses its SQL
# ---------------------------------------------------------------------------

def test_close_tab_preserves_sql_in_history(page):
    _select_testpg(page)
    _set_sql(page, "select 111 as keepme_inactive")       # tab 1 draft, never run
    page.locator("#tabAdd").click()                       # -> tab 2 active (tab 1 saved)
    page.locator('.tab .x[data-x="0"]').click()           # close INACTIVE tab 1 -> history
    _set_sql(page, "select 222 as keepme_active")         # draft in the remaining tab
    page.locator("#tabAdd").click()                       # new empty tab active
    page.locator('.tab[data-i="0"]').click()              # back to the draft tab
    page.wait_for_function(
        "document.querySelector('#sql').value === 'select 222 as keepme_active'")
    page.locator(".tab.on .x").click()                    # close the ACTIVE draft tab -> history
    page.locator("#histBtn").click()
    page.wait_for_selector(".modal .hitem")
    assert page.locator(".modal .hitem", has_text="keepme_inactive").count() == 1
    assert page.locator(".modal .hitem", has_text="keepme_active").count() == 1


# ---------------------------------------------------------------------------
# 31. Tabs: a tab whose connection vanished unbinds instead of silently rebinding
# ---------------------------------------------------------------------------

def test_stale_tab_connection_unbinds_not_rebinds(page):
    page.evaluate(
        "localStorage.setItem('qy_tabs', JSON.stringify(["
        "{sql:'select 1',db:'testpg',env:null},"
        "{sql:'select 9 as ghost',db:'ghostdb',env:null}]));"
        "localStorage.setItem('qy_ati','1');")
    page.goto(page.url.split("?")[0], wait_until="networkidle")
    page.wait_for_function("document.querySelector('#sql').value === 'select 9 as ghost'")
    txt = page.locator("#qtitle").inner_text()
    assert "No connection" in txt or "未选连接" in txt     # unbound, not rebound
    saved = page.evaluate("JSON.parse(localStorage.getItem('qy_tabs'))")
    assert saved[1]["db"] is None                          # persisted as unbound too


# ---------------------------------------------------------------------------
# 31b. Tabs: every tab's result — not just the active one — survives a reload
# ---------------------------------------------------------------------------

def test_per_tab_results_persist_across_reload(page):
    _select_testpg(page)
    _run_sql(page, "select 1 as a")                       # tab 0 result
    page.wait_for_selector('#grid td[data-v="1"]')
    page.locator("#tabAdd").click()                       # -> tab 1
    _run_sql(page, "select 2 as b")                       # tab 1 result
    page.wait_for_selector('#grid td[data-v="2"]')
    page.reload(wait_until="networkidle")
    page.wait_for_selector('.tab[data-i="1"]')
    page.wait_for_selector('#grid td[data-v="2"]')        # active tab (1) restored
    page.locator('.tab[data-i="0"]').click()              # inactive tab 0's result restored too
    page.wait_for_selector('#grid td[data-v="1"]')
    assert page.locator('#grid td[data-v="2"]').count() == 0
    page.locator('.tab[data-i="1"]').click()
    page.wait_for_selector('#grid td[data-v="2"]')        # still isolated, no cross-tab bleed


# ---------------------------------------------------------------------------
# 31c. Tabs: an in-flight request that lands AFTER the user switched tabs is
#      stored on its own tab — it must never overwrite the now-active tab.
# ---------------------------------------------------------------------------

def test_slow_response_routes_to_origin_tab_not_active(page):
    _select_testpg(page)
    _set_sql(page, "select pg_sleep(1.2), 'slowtab0' as tag")
    page.locator("#runBtn").click()                       # slow query in flight on tab 0
    page.locator("#tabAdd").click()                       # switch to tab 1 while it runs
    page.wait_for_selector("#grid .empty")
    _run_sql(page, "select 'fasttab1' as tag")            # tab 1 gets its own fast result
    page.wait_for_selector('#grid td[data-v="fasttab1"]')
    page.wait_for_timeout(1600)                           # let tab 0's slow response land
    # active tab (1) must still show its own result, never tab 0's slow rows
    assert page.locator('#grid td[data-v="fasttab1"]').count() == 1
    assert page.locator('#grid td[data-v="slowtab0"]').count() == 0
    page.locator('.tab[data-i="0"]').click()              # tab 0 kept its own routed result
    page.wait_for_selector('#grid td[data-v="slowtab0"]')
    assert page.locator('#grid td[data-v="fasttab1"]').count() == 0


# ---------------------------------------------------------------------------
# 31d. Tabs: re-pointing a tab to another connection must not let the old grid
#      be restored under the new connection after a reload.
# ---------------------------------------------------------------------------

def test_result_stays_in_dev_group_after_prod_reload(page_envset):
    page = page_envset
    page.wait_for_selector('.dbrow[data-db="shop"]')
    page.locator('.dbrow[data-db="shop"]').click()
    page.wait_for_selector("#esw .ep")
    _run_sql(page, "select 42 as dev_only")               # result produced on shop@dev
    page.wait_for_selector('#grid td[data-v="42"]')
    page.locator('#esw .ep[data-env="prod"]').click()     # switch to separate prod group
    page.wait_for_selector("#prodBadge")
    assert page.locator("#toast").is_hidden()
    # the persisted result is tagged with its PRODUCING connection (dev), not the
    # tab's current prod connection — so it can't masquerade as prod data
    saved = page.evaluate("JSON.parse(localStorage.getItem('qy_tabres'))")
    assert saved[0]["env"] == "dev"
    page.reload(wait_until="networkidle")
    page.wait_for_selector('.dbrow[data-db="shop"]')
    page.wait_for_timeout(400)
    # tab reloads bound to prod; the dev-tagged result no longer matches, so the
    # grid comes back empty instead of restoring dev rows mislabeled as prod
    assert page.locator('#grid td[data-v="42"]').count() == 0


# ---------------------------------------------------------------------------
# 31e. Upgrade path: the legacy single-result key (qy_result) carries the env it
#      was produced under; after upgrade + reload it must not be restored under a
#      tab that has since been re-pointed to a DIFFERENT env of the same db.
# ---------------------------------------------------------------------------

_LEGACY_RES = ("{columns:[{name:'v',type:'int4'}],rows:[{v:42}],"
               "rowCount:1,elapsedMs:1,engine:'postgres'}")


def _seed_legacy(page, tab_env):
    """Simulate an old version's storage: one tab bound to shop@<tab_env>, a
    single qy_result produced on shop@dev, and NO qy_tabres (the new key)."""
    page.evaluate(
        "([tabEnv])=>{"
        "localStorage.setItem('qy_tabs',JSON.stringify([{id:'t1',sql:'select 42 as v',db:'shop',env:tabEnv}]));"
        "localStorage.setItem('qy_ati','0');"
        "localStorage.removeItem('qy_tabres');"
        f"localStorage.setItem('qy_result',JSON.stringify({{db:'shop',env:'dev',res:{_LEGACY_RES}}}));"
        "}", [tab_env])
    page.reload(wait_until="networkidle")
    page.wait_for_selector('.dbrow[data-db="shop"]')
    page.wait_for_timeout(400)


def test_legacy_qy_result_env_mismatch_not_restored(page_envset):
    page = page_envset
    page.wait_for_selector('.dbrow[data-db="shop"]')
    _seed_legacy(page, "prod")                            # tab is now prod, result was dev
    # the dev-produced result must not be repainted under the prod tab
    assert page.locator('#grid td[data-v="42"]').count() == 0
    assert page.locator("#prodBadge").is_visible()        # tab really is on prod


def test_legacy_qy_result_env_match_restored(page_envset):
    page = page_envset
    page.wait_for_selector('.dbrow[data-db="shop"]')
    _seed_legacy(page, "dev")                             # tab still on dev -> connection matches
    page.wait_for_selector('#grid td[data-v="42"]')       # legacy result correctly restored


# ---------------------------------------------------------------------------
# 31f. Switching environments keeps a pending response on the original tab;
#      it never repaints the destination group and is restored on return.
# ---------------------------------------------------------------------------

def test_inflight_response_returns_to_origin_env_group(page_envset):
    page = page_envset
    page.wait_for_selector('.dbrow[data-db="shop"]')
    page.locator('.dbrow[data-db="shop"]').click()
    page.wait_for_selector("#esw .ep")                    # on shop@dev
    _set_sql(page, "select pg_sleep(1.2), 42 as devval")
    page.locator("#runBtn").click()                       # slow query in flight on shop@dev
    page.wait_for_selector("#grid .spin")
    page.locator('#esw .ep[data-env="prod"]').click()     # separate prod tab (no autorun)
    page.wait_for_selector("#prodBadge")
    assert page.locator("#toast").is_hidden()
    page.wait_for_timeout(1600)                            # let the dev response land
    # the dev rows must never surface under the now-prod tab
    assert page.locator('#grid td[data-v="42"]').count() == 0
    saved = page.evaluate("JSON.parse(localStorage.getItem('qy_tabres'))")
    assert saved[0]["env"] == "dev"
    assert saved[0]["res"]["rows"][0]["devval"] == 42
    page.locator('#esw .ep[data-env="dev"]').click()
    page.wait_for_selector('#grid td[data-v="42"]')


# ---------------------------------------------------------------------------
# 32. Table list: current-table highlight, manual refresh, alt+click insert-only
# ---------------------------------------------------------------------------

def test_table_click_highlights_current_table(page):
    _select_testpg(page)
    page.locator('#tbl-panel .tname[data-t="customers"]').click()
    page.wait_for_selector('#tbl-panel .tname.on[data-t="customers"]')
    page.locator('#tbl-panel .tname[data-t="orders"]').click()
    page.wait_for_selector('#tbl-panel .tname.on[data-t="orders"]')
    assert page.locator("#tbl-panel .tname.on").count() == 1
    _run_sql(page, "select 1 as custom")                  # custom SQL clears the highlight
    assert page.locator("#tbl-panel .tname.on").count() == 0


def test_table_list_manual_refresh(page, pg_exec):
    _select_testpg(page)
    pg_exec("CREATE TABLE qyref_zzz (id int)")
    try:
        assert page.locator('#tbl-panel .tname[data-t="qyref_zzz"]').count() == 0
        _set_sql(page, 'select 123 as keep_draft')
        page.locator('.dbrow.on').hover()
        page.locator(".dbrow.on .treload").click()       # manual fresh fetch
        page.wait_for_selector('#tbl-panel .tname[data-t="qyref_zzz"]', timeout=20000)
        assert page.locator('#tbl-panel').is_visible()
        assert page.locator('#sql').input_value() == 'select 123 as keep_draft'
        assert page.locator('#qtitle').inner_text() == 'testpg'
    finally:
        pg_exec("DROP TABLE IF EXISTS qyref_zzz")


def test_failed_refresh_keeps_cached_tables_and_stops_loading(page):
    _select_testpg(page)
    page.locator('[data-t="customers"]').wait_for()
    page.route('**/api/tables?*', lambda route: route.fulfill(status=503, json={'error': 'List refresh unavailable'}))
    page.locator('.dbrow.on').hover()
    page.locator('.dbrow.on .treload').click()
    page.locator('#tbl-panel [role="status"]').wait_for()
    assert page.locator('[data-t="customers"]').is_visible()
    assert page.locator('#tbl-panel .spin').count() == 0
    assert 'List refresh unavailable' in page.locator('#tbl-panel').inner_text()


def test_table_list_timeout_ends_spinner(page):
    page.add_init_script('''const original = AbortSignal.timeout.bind(AbortSignal);
      AbortSignal.timeout = ms => original(ms === 30000 ? 150 : ms);''')
    page.route('**/api/tables?*', lambda route: None)
    page.reload(wait_until='domcontentloaded')
    page.locator('.dbrow[data-db="testpg"]').click()
    page.wait_for_function("document.querySelector('#tbl-panel')?.textContent.toLowerCase().includes('time')")
    assert page.locator('#tbl-panel .spin').count() == 0


def test_alt_click_inserts_without_running(page):
    _select_testpg(page)
    queries = []
    page.on("request", lambda r: "/api/query" in r.url and queries.append(r.url))
    page.locator('#tbl-panel .tname[data-t="orders"]').click(modifiers=["Alt"])
    page.wait_for_function(
        "document.querySelector('#sql').value.includes('from orders')")
    page.wait_for_timeout(500)
    assert queries == []                                   # inserted, not executed
    assert page.locator("#grid .empty").count() == 1       # grid untouched


def test_non_public_schema_table_is_visible_and_clickable(page, pg_exec):
    pg_exec("DROP SCHEMA IF EXISTS qy_gui_schema CASCADE")
    rc, _, err = pg_exec("CREATE SCHEMA qy_gui_schema")
    assert rc == 0, err
    rc, _, err = pg_exec(
        "CREATE TABLE qy_gui_schema.events (id integer, label text); "
        "INSERT INTO qy_gui_schema.events VALUES (1, 'ready')"
    )
    assert rc == 0, err
    try:
        _select_testpg(page)
        sel = '#tbl-panel .tname[data-t="qy_gui_schema.events"]'
        page.wait_for_selector(sel)
        page.locator(sel).click()
        page.wait_for_selector('#grid td[data-v="ready"]')
        assert page.locator("#sql").input_value() == (
            "select * from qy_gui_schema.events"
        )
        page.locator(sel).dblclick()
        page.wait_for_selector("#structbox .cirow")
        structure = page.locator("#structbox").inner_text()
        assert "id" in structure and "label" in structure
        page.keyboard.press("Escape")
        page.locator("#sql").focus()
        page.keyboard.press("ControlOrMeta+a")
        page.keyboard.type("select qy_gui_schema.events.l")
        page.wait_for_selector(".acbox .acitem .ack-col", timeout=8000)
        assert any("label" in item for item in page.locator(".acbox .acitem").all_inner_texts())
    finally:
        pg_exec("DROP SCHEMA IF EXISTS qy_gui_schema CASCADE")


# ---------------------------------------------------------------------------
# 80/81. connection-info modal: resolved config + live reachability
# ---------------------------------------------------------------------------

def test_conn_info_modal_shows_resolved_config_and_health(page):
    # no connection selected -> the button is hidden
    assert page.locator("#ciBtn").is_hidden()
    _select_testpg(page)
    page.locator("#ciBtn").click()
    page.wait_for_selector(".modal .cirow")
    body = page.locator("#cibody").inner_text()
    assert "testpg" in body and "connections.toml" in body
    # live probe lands as ok against the reachable test database
    page.wait_for_selector(".cihealth.ok")
    # the URL's password slot must be masked. The password itself may be a
    # legitimate substring elsewhere (CI's password doubles as the username and
    # the db-name prefix), so only the `:password@` position proves a leak.
    import re as _re
    m = _re.match(r".*://[^:/@]+:([^@]+)@", TEST_DB_URL)
    if m:
        assert f":{m.group(1)}@" not in body
        assert "••••" in body
    # click outside closes the modal
    page.mouse.click(5, 5)
    assert page.locator(".modal").count() == 0


# ---------------------------------------------------------------------------
# 82-84. connection-info: url copy/eye + local-env action buttons
# ---------------------------------------------------------------------------

LOCAL_ENV_TOML = f"""
[shoploc_dev]
url = "{TEST_DB_URL}"
engine = "postgres"
env = "dev"
db = "shoploc"

[shoploc_local]
url = "{TEST_DB_URL}"
engine = "postgres"
env = "local"
db = "shoploc"
"""


@pytest.fixture()
def page_localenv(_pw_browser, tmp_path):
    """A page whose workspace has an env-set WITH a local member."""
    with _running_gui(tmp_path, extra_conn=LOCAL_ENV_TOML) as url:
        ctx, pg = _mk_page(_pw_browser, url)
        try:
            yield pg
        finally:
            ctx.close()


def test_conn_info_url_eye_toggles_and_copy_copies_real_url(page_clip):
    page = page_clip
    _select_testpg(page)
    page.locator("#ciBtn").click()
    page.wait_for_selector("#ciurl")
    # eye toggles between masked and revealed (and back)
    page.locator("#ciEye").click()
    page.wait_for_selector('#ciEye .ti-eye-off', state="attached")
    assert "••••" not in page.locator("#ciurl").inner_text()
    page.locator("#ciEye").click()
    page.wait_for_selector('#ciEye .ti-eye', state="attached")
    # copy puts the REAL url (usable in a service env file) on the clipboard
    page.locator("#ciCopy").click()
    page.wait_for_selector("#toast", state="visible")
    assert page.evaluate("navigator.clipboard.readText()") == TEST_DB_URL


def test_conn_info_offers_create_local_when_set_has_none(page):
    _select_testpg(page)
    page.locator("#ciBtn").click()
    page.wait_for_selector(".modal .cirow")
    assert page.locator("#ciUp").is_visible()      # no local member -> offer to create
    assert page.locator("#ciSync").count() == 0


def test_conn_info_offers_sync_on_local_env(page_localenv):
    page = page_localenv
    page.locator('.dbrow[data-db="shoploc"]').click()
    page.wait_for_selector("#esw .ep")
    page.locator('#esw .ep[data-env="local"]').click()
    page.wait_for_selector("#tbl-panel")
    page.locator("#ciBtn").click()
    page.wait_for_selector(".modal .cirow")
    assert page.locator("#ciSync").is_visible()    # on the local env -> offer sync
    assert page.locator("#ciUp").count() == 0


# ---------------------------------------------------------------------------
# 87. header: workspace manager — add/remove config.toml-registered
# workspaces (issue #15; the list used to be display-only)
# ---------------------------------------------------------------------------

def test_workspace_manager_add_and_remove(page, tmp_path, monkeypatch):
    monkeypatch.setenv("QUARRY_CONFIG", str(tmp_path / "config.toml"))
    page.locator("#wsBtn").click()
    page.wait_for_selector(".modal .wsadd")
    assert "No workspaces registered" in page.locator("#wsbody").inner_text()

    other_ws = str(tmp_path / "other_ws")  # never created on disk on purpose
    page.fill("#wsInput", other_ws)
    page.locator("#wsAddBtn").click()
    page.wait_for_selector(".wsrow")
    row = page.locator(".wslist").inner_text()
    assert other_ws in row
    assert "directory not found" in row  # missing dir is flagged, not hidden

    page.once("dialog", lambda d: d.accept())
    page.locator(".wsdel").click()
    page.wait_for_selector(".wsrow", state="detached")
    assert "No workspaces registered" in page.locator("#wsbody").inner_text()

    # click outside closes the modal, same as every other modal in the GUI
    page.mouse.click(5, 5)
    assert page.locator(".modal").count() == 0


def test_workspace_manager_shows_proxy_status_per_workspace(page, tmp_path, monkeypatch):
    """issue #101: the workspace manager reports each registered workspace's
    proxy toggle and the currently discovered system/env proxy — sourced from
    the server, not guessed client-side."""
    from quarry import proxy, workspace

    monkeypatch.setenv("QUARRY_CONFIG", str(tmp_path / "config.toml"))
    monkeypatch.setattr(workspace, "is_proxy_enabled", lambda home: True)
    monkeypatch.setattr(
        proxy, "discover_proxy",
        lambda: proxy.ProxyInfo(host="127.0.0.1", port=6152, source="system"),
    )

    other_ws = tmp_path / "other_ws"
    other_ws.mkdir()
    (other_ws / "connections.toml").write_text('[k]\nurl = "postgresql://x/y"\n', encoding="utf-8")

    page.locator("#wsBtn").click()
    page.wait_for_selector(".modal .wsadd")
    page.fill("#wsInput", str(other_ws))
    page.locator("#wsAddBtn").click()
    page.wait_for_selector(".wsrow")
    status = page.locator('[data-testid="ws-proxy-status"]').first
    assert "127.0.0.1:6152" in status.inner_text()


def test_workspace_manager_shows_proxy_off_when_toggle_disabled(page, tmp_path, monkeypatch):
    from quarry import workspace

    monkeypatch.setenv("QUARRY_CONFIG", str(tmp_path / "config.toml"))
    monkeypatch.setattr(workspace, "is_proxy_enabled", lambda home: False)

    other_ws = tmp_path / "other_ws"
    other_ws.mkdir()
    (other_ws / "connections.toml").write_text('[k]\nurl = "postgresql://x/y"\n', encoding="utf-8")

    page.locator("#wsBtn").click()
    page.wait_for_selector(".modal .wsadd")
    page.fill("#wsInput", str(other_ws))
    page.locator("#wsAddBtn").click()
    page.wait_for_selector(".wsrow")
    status = page.locator('[data-testid="ws-proxy-status"]').first
    assert "off" in status.inner_text().lower()


def test_workspace_manager_remove_unbinds_active_connection_immediately(page, tmp_path, monkeypatch):
    """PR #40 review r1-2: removing the workspace backing the currently ACTIVE
    connection must unbind right away — same "vanished connection never silently
    keeps showing" invariant as test_stale_tab_connection_unbinds_not_rebinds,
    but the vanishing is triggered by a workspace removal instead of a reload.

    Only reachable from a GUI session with no explicit --workspace pin (config.toml
    is what's actually being managed), so this drops the `page` fixture's explicit
    pin in favor of a config.toml-driven one that still keeps `testpg` visible."""
    from quarry import workspace

    monkeypatch.setenv("QUARRY_CONFIG", str(tmp_path / "config.toml"))
    workspace._write_config_workspaces([str(tmp_path)])
    workspace.configure_workspace(None)

    extra_ws = tmp_path / "extra_ws"
    extra_ws.mkdir()
    (extra_ws / "connections.toml").write_text(
        '[extra_db]\nurl = "postgresql://x/db"\nengine = "postgres"\n', encoding="utf-8")

    page.locator("#wsBtn").click()
    page.wait_for_selector(".modal .wsadd")
    page.fill("#wsInput", str(extra_ws))
    page.locator("#wsAddBtn").click()
    page.wait_for_selector(f'.wsrow:has-text("{extra_ws.name}")')
    page.mouse.click(5, 5)  # close the modal
    page.wait_for_selector(".modal", state="detached")

    page.wait_for_selector('.dbrow[data-db="extra_db"]')
    page.locator('.dbrow[data-db="extra_db"]').click()
    page.wait_for_function("document.querySelector('#qtitle').innerText === 'extra_db'")

    page.locator("#wsBtn").click()
    page.wait_for_selector(".wsrow")
    page.once("dialog", lambda d: d.accept())
    page.locator(f'.wsdel[data-dir="{extra_ws}"]').click()
    page.wait_for_selector(f'.wsrow:has-text("{extra_ws.name}")', state="detached")

    # The modal list and connection tree refresh independently after removal.
    # Wait for the actual unbinding; no reload or tab switch may be required.
    page.wait_for_function("/No connection|未选连接/.test(document.querySelector('#qtitle').innerText)")


def test_header_keepalive_badge_and_toggle(page, monkeypatch):
    """issue #112: header exposes compact keep-alive status + up/down control."""
    from quarry import keepalive

    state = {"running": False}

    def fake_status(_ws):
        return {
            "workspace": "/tmp/ws",
            "enabled": True,
            "reconnect": True,
            "keeper": {"running": state["running"], "pid": 4242 if state["running"] else None},
            "tunnels": [],
            "updatedAt": time.time(),
        }

    monkeypatch.setattr(keepalive, "status", fake_status)
    monkeypatch.setattr(keepalive, "start", lambda _ws: state.__setitem__("running", True) or (True, 4242))
    monkeypatch.setattr(keepalive, "stop", lambda _ws: state.__setitem__("running", False) or True)

    page.reload(wait_until="networkidle")
    page.wait_for_selector("#kaBadge")
    assert "down" in page.locator("#kaBadge").inner_text().lower() or "离线" in page.locator("#kaBadge").inner_text()

    from playwright.sync_api import expect
    badge = page.locator("#kaBadge")
    assert page.locator("#kaBtn").count() == 0
    assert badge.evaluate("el => el.tagName") == "BUTTON"
    expect(badge).to_have_attribute("aria-pressed", "false")
    badge.click()
    expect(badge).to_have_attribute("aria-pressed", "true")
    assert "up" in badge.inner_text().lower() or "在线" in badge.inner_text()
    assert badge.get_attribute("title") == badge.get_attribute("aria-label")
    badge.focus()
    badge.press("Enter")
    expect(badge).to_have_attribute("aria-pressed", "false")
    badge.press("Space")
    expect(badge).to_have_attribute("aria-pressed", "true")


def test_header_keepalive_badge_tracks_background_state(page, monkeypatch):
    """issue #112: badge state should refresh without manual button clicks."""
    from quarry import keepalive

    state = {"mode": "up"}

    def fake_status(_ws):
        tunnels = [] if state["mode"] == "up" else [{"connection": "shop", "env": "dev", "state": "reconnecting"}]
        return {
            "workspace": "/tmp/ws",
            "enabled": True,
            "reconnect": True,
            "keeper": {"running": True, "pid": 4242},
            "tunnels": tunnels,
            "updatedAt": time.time(),
        }

    monkeypatch.setattr(keepalive, "status", fake_status)
    monkeypatch.setattr(keepalive, "start", lambda _ws: (False, 4242))
    monkeypatch.setattr(keepalive, "stop", lambda _ws: True)

    page.reload(wait_until="networkidle")
    page.wait_for_selector("#kaBadge")
    assert "up" in page.locator("#kaBadge").inner_text().lower() or "在线" in page.locator("#kaBadge").inner_text()
    state["mode"] = "reconnecting"
    page.wait_for_function(
        """() => {
          const t = (document.querySelector('#kaBadge')?.textContent || '').toLowerCase();
          return t.includes('reconnecting') || t.includes('重连');
        }""",
        timeout=7000,
    )


# ---------------------------------------------------------------------------
# 88-91. Tabs: rename / drag-reorder / middle-click close / keyboard shortcut
# (issue #16 — formerly a "Design gaps" backlog item)
# ---------------------------------------------------------------------------

def _drag_tab(page, from_i, to_i):
    """Dispatch real dragstart/dragover/drop DOM events between two tabs, the
    same sequence a mouse drag produces, so this exercises the production
    ondragstart/ondragover/ondrop handlers rather than calling an internal fn."""
    page.evaluate(
        """([from, to]) => {
            const src = document.querySelector(`#tabs .tab[data-i="${from}"]`);
            const dst = document.querySelector(`#tabs .tab[data-i="${to}"]`);
            const dt = new DataTransfer();
            src.dispatchEvent(new DragEvent('dragstart', {bubbles: true, dataTransfer: dt}));
            dst.dispatchEvent(new DragEvent('dragover', {bubbles: true, dataTransfer: dt}));
            dst.dispatchEvent(new DragEvent('drop', {bubbles: true, dataTransfer: dt}));
            src.dispatchEvent(new DragEvent('dragend', {bubbles: true, dataTransfer: dt}));
        }""",
        [from_i, to_i],
    )


def test_tab_rename_persists_and_empty_reverts(page):
    _select_testpg(page)
    tab0 = page.locator('.tab[data-i="0"]')
    auto_title = tab0.locator(".lbl").inner_text()          # db(@env) auto title, e.g. "testpg@test"

    tab0.dblclick()
    page.locator('.tab[data-i="0"] input.rn').wait_for()
    page.fill('.tab[data-i="0"] input.rn', "scratch title")
    page.keyboard.press("Escape")                          # Escape discards the edit
    assert tab0.locator(".lbl").inner_text() == auto_title

    tab0.dblclick()
    page.fill('.tab[data-i="0"] input.rn', "blur title")
    page.locator("#sql").click()                            # blur (no Enter) must also commit
    page.wait_for_function(
        "document.querySelector('.tab[data-i=\"0\"] .lbl').textContent === 'blur title'")
    saved = page.evaluate("JSON.parse(localStorage.getItem('qy_tabs'))")
    assert saved[0]["title"] == "blur title"

    tab0.dblclick()
    page.fill('.tab[data-i="0"] input.rn', "scratch title")
    page.keyboard.press("Enter")                            # Enter commits
    page.wait_for_function(
        "document.querySelector('.tab[data-i=\"0\"] .lbl').textContent === 'scratch title'")
    saved = page.evaluate("JSON.parse(localStorage.getItem('qy_tabs'))")
    assert saved[0]["title"] == "scratch title"

    page.reload(wait_until="networkidle")                   # the custom title survives a reload
    page.wait_for_selector('.tab[data-i="0"] .lbl')
    assert page.locator('.tab[data-i="0"] .lbl').inner_text() == "scratch title"

    page.locator('.tab[data-i="0"]').dblclick()
    page.fill('.tab[data-i="0"] input.rn', "")               # empty name -> revert to auto title
    page.keyboard.press("Enter")
    page.wait_for_function(
        f"document.querySelector('.tab[data-i=\"0\"] .lbl').textContent === {auto_title!r}")


def test_tab_drag_reorder_moves_active_tab(page):
    _select_testpg(page)
    _run_sql(page, "select 1 as a")                          # tab 0: own result
    page.locator("#tabAdd").click()
    _run_sql(page, "select 2 as b")                          # tab 1: own result
    page.locator("#tabAdd").click()
    _run_sql(page, "select 3 as c")                          # tab 2 (active): own result
    page.wait_for_function("document.querySelectorAll('#tabs .tab[data-i]').length === 3")

    _drag_tab(page, 2, 0)                                    # drag the active tab to the front
    page.wait_for_function("document.querySelector('#sql').value === 'select 3 as c'")
    assert page.locator(".tab.on").get_attribute("data-i") == "0"   # follows by id, not old index
    order = page.evaluate("JSON.parse(localStorage.getItem('qy_tabs')).map(t => t.sql)")
    assert order == ["select 3 as c", "select 1 as a", "select 2 as b"]

    # the moved tab's OWN result travels with it — grid/export must not be index-shifted
    page.wait_for_selector('#grid td[data-v="3"]')
    with page.expect_download() as dl:
        page.locator("#csvBtn").click()
    assert "c\n3" in open(dl.value.path(), encoding="utf-8").read()
    # every other tab still shows its own result at its new index, never a shifted one
    page.locator('.tab[data-i="1"]').click()
    page.wait_for_selector('#grid td[data-v="1"]')
    page.locator('.tab[data-i="2"]').click()
    page.wait_for_selector('#grid td[data-v="2"]')
    page.locator('.tab[data-i="0"]').click()                # back to the moved tab before reloading
    page.wait_for_selector('#grid td[data-v="3"]')

    # TABRES (qy_tabres) was reordered in step with TABS, so this also survives a reload
    page.reload(wait_until="networkidle")
    page.wait_for_selector('.tab[data-i="0"]')
    page.wait_for_selector('#grid td[data-v="3"]')
    page.locator('.tab[data-i="1"]').click()
    page.wait_for_selector('#grid td[data-v="1"]')
    page.locator('.tab[data-i="2"]').click()
    page.wait_for_selector('#grid td[data-v="2"]')


def test_tab_middle_click_closes(page):
    _select_testpg(page)
    page.locator("#tabAdd").click()
    page.wait_for_function("document.querySelectorAll('#tabs .tab[data-i]').length === 2")
    page.locator('.tab[data-i="0"]').click(button="middle")
    page.wait_for_function("document.querySelectorAll('#tabs .tab[data-i]').length === 1")
    # the last remaining tab closes too, leaving the empty workbench
    page.locator('.tab[data-i="0"]').click(button="middle")
    page.wait_for_selector("#queryEmptyState")
    assert page.locator(".tab[data-i]").count() == 0


def test_tab_keyboard_shortcut_closes_active_tab(page):
    _select_testpg(page)
    page.locator("#tabAdd").click()
    page.wait_for_function("document.querySelectorAll('#tabs .tab[data-i]').length === 2")
    page.keyboard.press("Control+Shift+W")
    page.wait_for_function("document.querySelectorAll('#tabs .tab[data-i]').length === 1")
    # closing the last remaining tab via the shortcut leaves the empty workbench
    page.keyboard.press("Control+Shift+W")
    page.wait_for_selector("#queryEmptyState")
    assert page.locator(".tab[data-i]").count() == 0


# ---------------------------------------------------------------------------
# 94. Query deep link: copy/open, auto-run, tab reuse, invalid target guard
# ---------------------------------------------------------------------------

def _capture_query_requests(page):
    requests = []
    page.on('request', lambda request: requests.append({
        'path': urlparse(request.url).path, 'body': request.post_data_json,
    }) if request.method == 'POST' and
        urlparse(request.url).path in ('/api/query', '/api/run') else None)
    return requests


def _auto_run_preference_key(page, db, env, production):
    groups = page.request.get(page.url.split('/quarry/')[0] + '/api/connections').json()['groups']
    group = next(group for group in groups if any(item['db'] == db for item in group['items']))
    return json.dumps([group.get('ws'), db, env, production], separators=(',', ':'))


def _assert_run_mode(page, enabled):
    assert page.locator('#runModeBtn').inner_text().strip() == ('Auto' if enabled else 'Manual')


def _choose_run_mode(page, enabled):
    button = page.locator('#runModeBtn')
    assert button.get_attribute('aria-haspopup') == 'menu'
    button.click()
    assert button.get_attribute('aria-expanded') == 'true'
    option = page.locator('#runModeAuto' if enabled else '#runModeManual')
    assert option.get_attribute('role') == 'menuitemradio'
    option.click()
    assert button.get_attribute('aria-expanded') == 'false'
    assert page.locator('#runModeMenu').count() == 0
    _assert_run_mode(page, enabled)


@pytest.mark.parametrize(('lang', 'manual', 'auto'), [('en', 'Manual', 'Auto'), ('zh', '手动', '自动')])
def test_run_mode_menu_keyboard_dismissal_and_selection_never_execute(page, lang, manual, auto):
    _select_testpg(page)
    if lang == 'zh':
        page.locator('.vg-lang-switch').click()
        page.wait_for_function('''() => document.querySelector('#runLbl')?.textContent === '运行'
            && document.querySelector('#sql') && !document.querySelector('#runModeBtn')?.disabled''')
    _run_sql(page, 'select 1 as previous_result')
    page.locator('#grid td[data-v="1"]').click()
    assert page.locator('#grid td.sel').count() == 1
    _set_sql(page, 'select 42 as mode_menu_draft')
    requests = _capture_query_requests(page)
    button = page.locator('#runModeBtn')
    assert button.inner_text().strip() == auto
    assert button.get_attribute('aria-haspopup') == 'menu'
    assert button.get_attribute('aria-expanded') == 'false'
    button.focus()
    page.keyboard.press('Enter')
    assert button.get_attribute('aria-expanded') == 'true'
    assert page.locator('.modal').count() == 0
    assert page.locator('#grid td.sel[data-v="1"]').count() == 1
    assert page.locator('#runModeMenu').get_attribute('role') == 'menu'
    assert page.locator('#runModeManual').get_attribute('role') == 'menuitemradio'
    assert page.locator('#runModeManual').get_attribute('aria-checked') == 'false'
    assert page.locator('#runModeAuto').get_attribute('aria-checked') == 'true'
    page.keyboard.press('Home')
    assert page.locator('#runModeManual').evaluate('el => el === document.activeElement')
    page.keyboard.press('End')
    assert page.locator('#runModeAuto').evaluate('el => el === document.activeElement')
    page.keyboard.press('Escape')
    assert page.locator('#runModeMenu').count() == 0
    assert button.get_attribute('aria-expanded') == 'false'
    assert button.evaluate('el => el === document.activeElement')

    page.keyboard.press('Enter')
    page.locator('#runModeAuto').focus()
    page.keyboard.press('ArrowUp')
    assert page.locator('#runModeManual').evaluate('el => el === document.activeElement')
    page.keyboard.press('Enter')
    assert page.locator('#runModeMenu').count() == 0
    assert button.inner_text().strip() == manual
    button.click()
    assert page.locator('#runModeManual').get_attribute('aria-checked') == 'true'
    assert page.locator('#runModeAuto').get_attribute('aria-checked') == 'false'
    page.locator('#runModeManual').focus()
    page.keyboard.press('ArrowDown')
    assert page.locator('#runModeAuto').evaluate('el => el === document.activeElement')
    page.keyboard.press('Enter')
    assert button.inner_text().strip() == auto
    assert button.get_attribute('aria-expanded') == 'false'

    button.click()
    page.locator('#sql').click()
    assert page.locator('#runModeMenu').count() == 0
    assert button.get_attribute('aria-expanded') == 'false'
    assert page.locator('#sql').evaluate('el => el === document.activeElement')
    button.click()
    page.keyboard.press('Tab')
    assert page.locator('#runModeMenu').count() == 0
    assert button.get_attribute('aria-expanded') == 'false'
    assert page.locator('#fmtBtn').evaluate('el => el === document.activeElement')
    assert page.locator('#sql').input_value() == 'select 42 as mode_menu_draft'
    page.wait_for_timeout(200)
    assert requests == []
    assert page.locator('.modal').count() == 0
    assert page.locator('#grid td.sel[data-v="1"]').count() == 1
    page.locator('#runBtn').focus()
    page.keyboard.press('Enter')
    page.wait_for_selector('#grid td[data-v="42"]')
    assert len(requests) == 1 and requests[0]['body']['sql'] == 'select 42 as mode_menu_draft'
    assert page.locator('.modal').count() == 0
    button.focus()
    page.keyboard.press('Control+Shift+W')
    page.wait_for_selector('#queryEmptyState')
    assert page.locator('#runModeMenu').count() == 0
    assert len(requests) == 1


def test_run_mode_menu_closes_when_connection_or_tab_context_changes(page_envset):
    page = page_envset
    page.locator('.dbrow[data-db="shop"]').click()
    _choose_run_mode(page, False)
    _set_sql(page, 'select 42 as context_change_draft')
    requests = _capture_query_requests(page)
    page.locator('#runModeBtn').click()
    page.locator('#esw .ep[data-env="prod"]').click()
    assert page.locator('#runModeMenu').count() == 0
    assert page.locator('#runModeBtn').get_attribute('aria-expanded') == 'false'
    _assert_run_mode(page, False)
    page.locator('#runModeBtn').click()
    page.locator('#tabAdd').click()
    assert page.locator('#runModeMenu').count() == 0
    assert page.locator('#runModeBtn').get_attribute('aria-expanded') == 'false'
    page.locator('#runModeBtn').click()
    page.locator('#tabs .tab[data-i]').first.click()
    assert page.locator('#runModeMenu').count() == 0
    page.wait_for_timeout(200)
    assert requests == []


def test_auto_run_mode_controls_new_and_reused_table_previews(page):
    _select_testpg(page)
    _assert_run_mode(page, True)
    requests = _capture_query_requests(page)
    _choose_run_mode(page, False)
    for table in ('customers', 'orders', 'customers'):
        page.locator(f'#tbl-panel .tname[data-t="{table}"]').click()
        assert page.locator('#sql').input_value() == f'select * from {table}'
    page.wait_for_timeout(200)
    assert requests == []
    assert page.locator('#tabs .tab[data-i]').count() == 2
    assert page.locator('#grid table').count() == 0

    # Explicit keyboard Run still works while automatic execution is disabled.
    page.locator('#sql').focus()
    page.keyboard.press('ControlOrMeta+Enter')
    page.wait_for_selector('#grid table tbody tr')
    assert len(requests) == 1
    assert requests[0]['body']['sql'] == 'select * from customers'
    _set_sql(page, 'select 17 as unsent_draft')
    _choose_run_mode(page, True)
    _set_sql(page, 'select 18 as still_unsent_draft')
    page.wait_for_timeout(200)
    assert len(requests) == 1  # Neither choosing Auto nor typing executes SQL.
    page.locator('#tbl-panel .tname[data-t="orders"]').click()
    page.wait_for_selector('#grid table tbody tr')
    assert len(requests) == 2
    assert requests[-1]['body']['sql'] == 'select * from orders'
    assert page.locator('#tabs .tab[data-i]').count() == 2


def test_auto_run_preferences_follow_connection_environment_and_reload(page_explicit_production):
    page = page_explicit_production
    _select_testpg(page)
    _choose_run_mode(page, False)
    page.locator('.dbrow[data-db="shop"]').click()
    _assert_run_mode(page, True)
    _choose_run_mode(page, False)
    _set_sql(page, 'select 42 as environment_preference')
    requests = _capture_query_requests(page)

    # Explicit production metadata matters: jp starts off even though its name is not prod.
    page.locator('#esw .ep[data-env="jp"]').click()
    _assert_run_mode(page, False)
    assert page.locator('#sql').input_value() == 'select 42 as environment_preference'
    _choose_run_mode(page, True)
    page.wait_for_timeout(200)
    assert requests == []
    page.locator('#esw .ep[data-env="dev"]').click()
    _assert_run_mode(page, False)
    page.wait_for_timeout(200)
    assert requests == []
    page.locator('#esw .ep[data-env="jp"]').click()
    page.wait_for_selector('#grid td[data-v="42"]')
    assert len(requests) == 1 and requests[0]['body']['env'] == 'jp'

    preferences = page.evaluate('JSON.parse(localStorage.qy_auto_run)')
    for db, env, production, enabled in (
        ('testpg', 'test', False, False), ('shop', 'dev', False, False),
        ('shop', 'jp', True, True),
    ):
        assert preferences[_auto_run_preference_key(page, db, env, production)] is enabled
    page.reload(wait_until='networkidle')
    _assert_run_mode(page, True)
    assert len(requests) == 1
    page.locator('#esw .ep[data-env="dev"]').click()
    _assert_run_mode(page, False)
    _select_testpg(page)
    _assert_run_mode(page, False)
    page.locator('.dbrow[data-db="shop"]').click()
    _assert_run_mode(page, False)
    page.locator('#esw .ep[data-env="prod"]').click()
    _assert_run_mode(page, True)  # This environment is not production.
    page.wait_for_selector('#grid td[data-v="42"]')
    assert len(requests) == 2 and requests[-1]['body']['env'] == 'prod'


@pytest.mark.parametrize(('db', 'env', 'preference'), [
    ('testpg', 'test', False), ('shop', 'prod', None), ('shop', 'prod', True),
])
def test_query_deeplink_respects_auto_run_preference_and_production_default(page_envset, db, env, preference):
    page = page_envset
    if preference is not None:
        key = _auto_run_preference_key(page, db, env, db == 'shop')
        page.evaluate('([key, enabled]) => localStorage.setItem("qy_auto_run", JSON.stringify({[key]: enabled}))',
                      [key, preference])
    requests = _capture_query_requests(page)
    sql = 'select 42 as shared_auto_run'
    base = page.url.split('/quarry/')[0]
    page.goto(f'{base}/quarry/?{urlencode({"db": db, "env": env, "sql": sql})}', wait_until='networkidle')
    assert page.locator('#sql').input_value() == sql
    _assert_run_mode(page, preference is True)
    if preference is True:
        page.wait_for_selector('#grid td[data-v="42"]')
        assert len(requests) == 1 and requests[0]['body']['env'] == env
    else:
        assert requests == []
        assert page.locator('#grid table').count() == 0


def test_auto_run_saved_query_off_allows_manual_run_and_on_runs_new_query(page_noparam):
    page = page_noparam
    _select_testpg(page)
    _choose_run_mode(page, False)
    requests = _capture_query_requests(page)
    page.locator('.query-index-children [data-q="all-cust"]').click()
    assert page.locator('#sql').get_attribute('readonly') is not None
    page.wait_for_timeout(200)
    assert requests == []
    page.locator('#runBtn').click()
    page.wait_for_selector('#grid table tbody tr')
    assert len(requests) == 1 and requests[0]['path'] == '/api/run'
    assert page.locator('#grid tbody tr').count() == 3
    _choose_run_mode(page, True)
    page.wait_for_timeout(200)
    assert len(requests) == 1
    # Reopening the same saved query after closing its tab creates a fresh run.
    page.locator('#tabs .tab.on .x').click()
    page.locator('.query-index-children [data-q="all-cust"]').click()
    page.wait_for_selector('#grid table tbody tr')
    assert len(requests) == 2 and requests[-1]['path'] == '/api/run'


@pytest.mark.parametrize('action', ['mode', 'type'])
def test_auto_run_preference_or_edit_cancels_navigation_waiting_for_tables(page_explicit_production, action):
    page = page_explicit_production
    page.locator('.dbrow[data-db="shop"]').click()
    page.locator('#tbl-panel .tname[data-t="customers"]').wait_for()
    _set_sql(page, 'select 42 as delayed_navigation')
    requests = _capture_query_requests(page)
    pending = []
    page.route('**/api/tables?*', lambda route: pending.append(route))
    page.locator('#esw .ep[data-env="prod"]').click()
    page.wait_for_timeout(100)
    assert pending
    _assert_run_mode(page, True)
    if action == 'mode':
        _choose_run_mode(page, False)
        _choose_run_mode(page, True)
    else:
        _set_sql(page, 'select 43 as edited_during_navigation')
    for route in pending:
        route.fulfill(json={'engine': 'postgres', 'tables': ['customers', 'orders'], 'capped': False})
    page.locator('#tbl-panel .tname[data-t="customers"]').wait_for()
    page.wait_for_timeout(200)
    assert requests == []
    expected = 'select 42 as delayed_navigation' if action == 'mode' else 'select 43 as edited_during_navigation'
    assert page.locator('#sql').input_value() == expected
    page.locator('#tbl-panel .tname[data-t="customers"]').click()
    page.wait_for_selector('#grid table tbody tr')
    assert len(requests) == 1 and requests[0]['body']['env'] == 'prod'


def test_auto_run_is_disabled_when_production_metadata_is_unknown(page):
    def omit_production(route):
        response = route.fetch()
        data = response.json()
        for group in data['groups']:
            for item in group['items']:
                for env in item['envs']:
                    env.pop('production', None)
        route.fulfill(response=response, json=data)

    page.route('**/api/connections', omit_production)
    page.reload(wait_until='networkidle')
    _select_testpg(page)
    assert page.locator('#runModeBtn').is_disabled()
    _assert_run_mode(page, False)
    requests = _capture_query_requests(page)
    page.locator('#tbl-panel .tname[data-t="customers"]').click()
    page.wait_for_timeout(200)
    assert requests == []
    page.locator('#runBtn').click()
    page.wait_for_selector('#grid table tbody tr')
    assert len(requests) == 1


@pytest.mark.parametrize('change', ['production', 'workspace'])
def test_auto_run_opt_in_does_not_cross_connection_identity_change(page_envset, change):
    page = page_envset
    if change == 'production':
        _select_testpg(page)
        _choose_run_mode(page, False)  # Persist an explicit nonproduction opt-in.
    else:
        page.locator('.dbrow[data-db="shop"]').click()
        page.locator('#esw .ep[data-env="prod"]').click()
    _choose_run_mode(page, True)
    requests = _capture_query_requests(page)

    def changed_connection(route):
        response = route.fetch()
        data = response.json()
        for group in data['groups']:
            if change == 'workspace':
                group['ws'] = '/replacement-workspace'
            for item in group['items']:
                if change == 'production' and item['db'] == 'testpg':
                    for env in item['envs']:
                        env['production'] = True
        route.fulfill(response=response, json=data)

    page.route('**/api/connections', changed_connection)
    page.reload(wait_until='networkidle')
    if change == 'production':
        # A restored tab already selects testpg; its first row click only collapses the panel.
        page.locator('#prodBadge').wait_for()
    else:
        page.locator('.dbrow[data-db="shop"]').click()
        page.locator('#esw .ep[data-env="prod"]').click()
    _assert_run_mode(page, False)
    page.locator('#tbl-panel .tname[data-t="customers"]').click()
    page.wait_for_timeout(200)
    assert requests == []


def test_copy_query_link_copies_db_env_sql(page_clip):
    page = page_clip
    _select_testpg(page)
    sql = "select '中文' as note\nfrom customers where id = 1"
    _set_sql(page, sql)
    page.locator("#linkBtn").click()
    page.wait_for_selector("#toast", state="visible")
    copied = page.evaluate("navigator.clipboard.readText()")
    qs = parse_qs(urlparse(copied).query)
    assert qs["db"] == ["testpg"]
    assert qs["env"] == ["test"]
    assert qs["sql"] == [sql]


def test_query_deeplink_opens_existing_tab_and_autoruns(page):
    sql = "select 42 as shared_link_row"
    qs = urlencode({"db": "testpg", "env": "test", "sql": sql})
    base = page.url.split("/quarry/")[0]
    page.evaluate(
        """([seedSql]) => {
            localStorage.setItem("qy_tabs", JSON.stringify([
              { id: "t9", sql: seedSql, db: "testpg", env: "test" }
            ]));
            localStorage.setItem("qy_ati", "0");
            localStorage.removeItem("qy_tabres");
        }""",
        [sql],
    )
    page.goto(f"{base}/quarry/?{qs}", wait_until="networkidle")
    page.wait_for_selector('#grid td[data-v="42"]')
    assert page.locator(".tab[data-i]").count() == 1
    assert page.locator("#sql").input_value() == sql


def test_table_click_opens_new_tab_without_overwrite(page):
    _select_testpg(page)
    page.locator('#tbl-panel .tname[data-t="customers"]').click()
    page.wait_for_selector("#grid table tbody tr")
    page.locator('#tbl-panel .tname[data-t="orders"]').click()
    page.wait_for_function(
        "document.querySelector('#sql').value === 'select * from orders'")
    assert page.locator(".tab[data-i]").count() == 2
    page.locator('.tab[data-i="0"]').click()
    page.wait_for_function(
        "document.querySelector('#sql').value === 'select * from customers'")


def test_table_click_reuses_same_table_preview_tab(page):
    _select_testpg(page)
    page.locator('#tbl-panel .tname[data-t="customers"]').click()
    page.wait_for_selector("#grid table tbody tr")
    page.locator('#tbl-panel .tname[data-t="orders"]').click()
    page.wait_for_function(
        "document.querySelector('#sql').value === 'select * from orders'")
    page.locator('#tbl-panel .tname[data-t="customers"]').click()
    page.wait_for_function(
        "document.querySelector('#sql').value === 'select * from customers'")
    assert page.locator(".tab[data-i]").count() == 2


def test_selection_updates_url_and_title(page):
    _select_testpg(page)
    page.wait_for_function(
        "() => new URL(location.href).searchParams.get('db') === 'testpg'")
    page.locator('#tbl-panel .tname[data-t="customers"]').click()
    page.wait_for_selector("#grid table tbody tr")
    page.wait_for_function(
        """() => {
            const q = new URL(location.href).searchParams;
            return q.get('db') === 'testpg' && q.get('table') === 'customers';
        }""")
    assert "customers" in page.title()
    assert "testpg" in page.title()


def test_focus_url_opens_new_tab_when_active_tab_has_sql(page):
    queries = []
    page.on("request", lambda r: "/api/query" in r.url and queries.append(r.url))
    draft = "select 123 as draft_marker"
    page.evaluate(
        """([sql]) => {
            localStorage.setItem("qy_tabs", JSON.stringify([
              { id: "t1", sql, db: "testpg", env: "test" }
            ]));
            localStorage.setItem("qy_ati", "0");
            localStorage.removeItem("qy_tabres");
        }""",
        [draft],
    )
    base = page.url.split("/quarry/")[0]
    page.goto(f"{base}/quarry/?db=testpg&env=test&table=customers", wait_until="networkidle")
    page.wait_for_function(
        "document.querySelector('#sql').value === 'select * from customers'")
    assert page.locator(".tab[data-i]").count() == 2
    page.locator('.tab[data-i="0"]').click()
    page.wait_for_function(
        "document.querySelector('#sql').value.includes('draft_marker')")
    page.wait_for_timeout(400)
    assert queries == []


def test_handwritten_schema_sql_keeps_schema_in_focus_url(page):
    _select_testpg(page)
    _set_sql(page, "select * from qy_gui_schema.events")
    page.wait_for_function(
        """() => new URL(location.href).searchParams.get('table')
            === 'qy_gui_schema.events'""")
    assert "qy_gui_schema.events" in page.title()


def test_focus_url_restores_selection_without_autorun(page):
    queries = []
    page.on("request", lambda r: "/api/query" in r.url and queries.append(r.url))
    base = page.url.split("/quarry/")[0]
    page.evaluate(
        """() => {
            localStorage.removeItem("qy_tabs");
            localStorage.removeItem("qy_ati");
            localStorage.removeItem("qy_tabres");
        }"""
    )
    page.goto(f"{base}/quarry/?db=testpg&env=test&table=customers", wait_until="networkidle")
    page.wait_for_selector('.dbrow.on[data-db="testpg"]')
    page.wait_for_function(
        "document.querySelector('#sql').value === 'select * from customers'")
    page.wait_for_timeout(500)
    assert queries == []
    assert page.locator("#grid .empty").count() == 1
    assert "customers" in page.title()


def test_query_deeplink_invalid_env_shows_notice_and_skips_autorun(page_envset):
    page = page_envset
    queries = []
    page.on("request", lambda r: "/api/query" in r.url and queries.append(r.url))
    qs = urlencode({"db": "shop", "env": "ghost", "sql": "select 1 as bad_env"})
    base = page.url.split("/quarry/")[0]
    page.goto(f"{base}/quarry/?{qs}", wait_until="networkidle")
    page.wait_for_selector("#toast", state="visible")
    assert "link" in page.locator("#toast").inner_text().lower()
    page.wait_for_timeout(600)
    assert queries == []





# ---------------------------------------------------------------------------
# live event channel (/api/events) — these tests need the REAL SSE stream, so
# they build their own context WITHOUT conftest.stub_events and use explicit
# selector waits (an open SSE request makes "networkidle" unreachable).
# ---------------------------------------------------------------------------

def _mk_live_page(browser, url):
    ctx = english_browser_context(browser, viewport={"width": 1280, "height": 900})
    stub_cdn(ctx)
    pg = ctx.new_page()
    pg.goto(url, wait_until="load")
    return ctx, pg


def test_workspace_file_change_refreshes_sidebar_live(_pw_browser, tmp_path, monkeypatch):
    """Editing connections.toml on disk (CLI, agent, git pull…) must show up in
    the sidebar without a manual reload: watcher -> workspace_changed -> refetch."""
    from quarry import gui
    monkeypatch.setattr(gui, "WATCH_INTERVAL_SEC", 0.2)
    with _running_gui(tmp_path) as base:
        ctx, page = _mk_live_page(_pw_browser, base)
        try:
            page.wait_for_selector('.dbrow[data-db="testpg"]')
            with (tmp_path / "connections.toml").open("a", encoding="utf-8") as f:
                f.write('\n[latecomer]\nurl = "postgresql://u:p@127.0.0.1:5499/x"\n'
                        'engine = "postgres"\nenv = "dev"\n')
            page.wait_for_selector('.dbrow[data-db="latecomer"]', timeout=15000)
        finally:
            ctx.close()


def test_server_upgrade_prompts_reload_banner(_pw_browser, tmp_path, monkeypatch):
    """After the backend restarts with a new version (the user upgraded), the
    EventSource reconnect re-checks /api/version and raises the reload banner."""
    import socket as socket_mod
    import threading as threading_mod
    from http.server import ThreadingHTTPServer

    from quarry import gui, workspace
    from conftest import _write_ws

    _write_ws(tmp_path)
    workspace.configure_workspace(str(tmp_path))
    monkeypatch.setattr(gui.cache, "CACHE_FILE", tmp_path / "gui-cache.json")
    gui.cache._CACHE.clear()
    with socket_mod.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    httpd = ThreadingHTTPServer(("127.0.0.1", port), gui.Handler)
    threading_mod.Thread(target=lambda: httpd.serve_forever(poll_interval=0.02),
                         daemon=True).start()
    ctx = None
    httpd2 = None
    try:
        ctx, page = _mk_live_page(_pw_browser, f"http://127.0.0.1:{port}")
        page.wait_for_selector('.dbrow[data-db="testpg"]')
        page.wait_for_timeout(500)          # let the baseline /api/version land
        httpd.shutdown()
        httpd.server_close()
        monkeypatch.setattr(gui, "__version__", "99.0.0")
        httpd2 = ThreadingHTTPServer(("127.0.0.1", port), gui.Handler)
        threading_mod.Thread(target=lambda: httpd2.serve_forever(poll_interval=0.02),
                             daemon=True).start()
        # shutdown() stops the listener but the SSE handler thread still owns
        # its client socket — end the stream the way a real process exit would,
        # so the browser's EventSource actually reconnects (now against httpd2)
        gui._close_event_streams()
        page.wait_for_selector("#upgradeBanner", timeout=20000)
        assert "99.0.0" in page.locator("#upgradeBanner").inner_text()
    finally:
        if ctx is not None:
            ctx.close()
        for srv in (httpd2,):
            if srv is not None:
                srv.shutdown()
                srv.server_close()
        gui.cache._CACHE.clear()
        workspace.configure_workspace(None)


# ---------------------------------------------------------------------------
# update-check badge (GET /api/update) — driven by a mocked API response, so
# these never depend on real PyPI reachability or the background checker's
# throttle (both unit-tested against mocks in test_gui_backend.py).
# ---------------------------------------------------------------------------

def _mk_update_page(browser, url, update_body):
    ctx = english_browser_context(browser, viewport={"width": 1280, "height": 900})
    stub_cdn(ctx)
    stub_events(ctx)
    ctx.route("**/api/update", lambda route: route.fulfill(
        status=200, content_type="application/json", body=json.dumps(update_body)))
    pg = ctx.new_page()
    pg.goto(url, wait_until="networkidle")
    return ctx, pg


def test_update_badge_hidden_when_no_new_version(_pw_browser, gui_url):
    ctx, page = _mk_update_page(
        _pw_browser, gui_url, {"current": "0.5.1", "latest": "0.5.1", "available": False})
    try:
        page.wait_for_selector('.dbrow[data-db="testpg"]')
        assert page.locator("#updateBadge").count() == 0
    finally:
        ctx.close()


def test_update_badge_visible_and_dot_uses_accent_token_in_both_themes(_pw_browser, gui_url):
    """A new PyPI release makes the header badge appear; its dot resolves to
    the --accent design token in both themes (dark default, then light)."""
    ctx, page = _mk_update_page(
        _pw_browser, gui_url, {"current": "0.5.1", "latest": "9.9.9", "available": True})
    try:
        page.wait_for_selector("#updateBadge")

        def dot_color() -> str:
            return page.evaluate(
                "() => getComputedStyle("
                "document.querySelector('#updateBadge .update-dot')).backgroundColor")

        assert dot_color() == "rgb(192, 130, 79)"  # dark theme --accent (#c0824f)
        page.locator(".vg-switcher-mode").click()
        assert dot_color() == "rgb(176, 106, 52)"  # light theme --accent (#b06a34)
    finally:
        ctx.close()


def test_update_panel_shows_versions_and_upgrade_command(_pw_browser, gui_url):
    ctx, page = _mk_update_page(
        _pw_browser, gui_url, {"current": "0.5.1", "latest": "9.9.9", "available": True})
    try:
        page.wait_for_selector("#updateBadge")
        page.locator("#updateBadge").click()
        page.wait_for_selector("#updbox")
        text = page.locator("#updbox").inner_text()
        assert "0.5.1" in text and "9.9.9" in text
        assert page.locator("#updCmd").inner_text() == "pipx upgrade quarry-db"
        release_link = page.locator("#updbox a")
        assert release_link.get_attribute("href") == (
            "https://github.com/yiminspace/quarry/releases/tag/v9.9.9")
        assert release_link.get_attribute("target") == "_blank"
    finally:
        ctx.close()


# ---------------------------------------------------------------------------
# What's New panel (GET /api/changelog) — auto-shown once when the running
# __version__ (mocked via /api/version, so this never depends on the checked-
# out repo's actual CHANGELOG.md) differs from localStorage's
# qy_last_seen_version. See useEvents.ts's checkWhatsNew.
# ---------------------------------------------------------------------------

_WHATS_NEW_VERSION = {"name": "Quarry", "version": "9.9.9"}
# Three versions on purpose: current (9.9.9), an intermediate one (9.8.0) that
# sits strictly between a hypothetical lastSeen and current, and an older one
# (9.7.0) that must never be shown regardless of lastSeen — this is what lets
# the range-filter tests below actually exercise checkWhatsNew's interval
# logic instead of trivially passing with a single-version response.
_WHATS_NEW_CHANGELOG = [
    {"version": "9.9.9", "date": "2026-07-16", "entries": ["Added a shiny new feature"]},
    {"version": "9.8.0", "date": "2026-06-01", "entries": ["Older entry not shown"]},
    {"version": "9.7.0", "date": "2026-05-01", "entries": ["Even older entry not shown"]},
]


def _mk_whats_new_context(browser):
    ctx = english_browser_context(browser, viewport={"width": 1280, "height": 900})
    stub_cdn(ctx)
    stub_events(ctx)
    ctx.route("**/api/version", lambda route: route.fulfill(
        status=200, content_type="application/json", body=json.dumps(_WHATS_NEW_VERSION)))
    ctx.route("**/api/changelog", lambda route: route.fulfill(
        status=200, content_type="application/json", body=json.dumps(_WHATS_NEW_CHANGELOG)))
    ctx.route("**/api/update", lambda route: route.fulfill(
        status=200, content_type="application/json",
        body=json.dumps({"current": "9.9.9", "latest": None, "available": False})))
    return ctx


def test_whats_new_hidden_on_first_ever_load(_pw_browser, gui_url):
    """No qy_last_seen_version recorded yet -> just set the baseline silently,
    no popup (a fresh install has nothing to compare itself against)."""
    ctx = _mk_whats_new_context(_pw_browser)
    try:
        page = ctx.new_page()
        page.goto(gui_url, wait_until="networkidle")
        page.wait_for_selector('.dbrow[data-db="testpg"]')
        assert page.locator("#whatsNewBox").count() == 0
        assert page.evaluate("localStorage.getItem('qy_last_seen_version')") == "9.9.9"
    finally:
        ctx.close()


def test_whats_new_shows_entries_after_upgrade_then_stays_hidden_on_reload(_pw_browser, gui_url):
    ctx = _mk_whats_new_context(_pw_browser)
    try:
        page = ctx.new_page()
        page.goto(gui_url, wait_until="networkidle")
        page.evaluate("localStorage.setItem('qy_last_seen_version', '0.1.0')")
        page.reload(wait_until="networkidle")

        page.wait_for_selector("#whatsNewBox")
        text = page.locator("#whatsNewBox").inner_text()
        assert "9.9.9" in text and "Added a shiny new feature" in text
        assert page.evaluate("localStorage.getItem('qy_last_seen_version')") == "9.9.9"

        page.reload(wait_until="networkidle")
        page.wait_for_selector('.dbrow[data-db="testpg"]')
        assert page.locator("#whatsNewBox").count() == 0
    finally:
        ctx.close()


def test_whats_new_only_shows_entries_strictly_after_last_seen_version(_pw_browser, gui_url):
    """/api/changelog returns 3 versions (9.9.9 current, 9.8.0, 9.7.0). With
    lastSeen=9.8.0, only the 9.9.9 entry is strictly newer than lastSeen and
    not newer than current, so it's the only one that should render — 9.8.0
    itself (== lastSeen) and 9.7.0 (older) must be excluded."""
    ctx = _mk_whats_new_context(_pw_browser)
    try:
        page = ctx.new_page()
        page.goto(gui_url, wait_until="networkidle")
        page.evaluate("localStorage.setItem('qy_last_seen_version', '9.8.0')")
        page.reload(wait_until="networkidle")

        page.wait_for_selector("#whatsNewBox")
        text = page.locator("#whatsNewBox").inner_text()
        assert "Added a shiny new feature" in text
        assert "Older entry not shown" not in text
        assert "Even older entry not shown" not in text
        assert page.evaluate("localStorage.getItem('qy_last_seen_version')") == "9.9.9"
    finally:
        ctx.close()


def test_whats_new_background_uses_bg1_token_in_both_themes(_pw_browser, gui_url):
    ctx = _mk_whats_new_context(_pw_browser)
    try:
        page = ctx.new_page()
        page.goto(gui_url, wait_until="networkidle")
        page.evaluate("localStorage.setItem('qy_last_seen_version', '0.1.0')")
        page.reload(wait_until="networkidle")
        page.wait_for_selector("#whatsNewBox")

        def box_bg() -> str:
            return page.evaluate(
                "() => getComputedStyle(document.querySelector('#whatsNewBox')).backgroundColor")

        assert box_bg() == "rgb(28, 32, 40)"  # dark theme --bg1 (#1c2028)
        # The panel is a full-viewport modal overlay, so a real mouse click at
        # the header's mode-toggle coordinates would hit-test to the overlay
        # instead (and close it, via its click-outside-closes handler). Call
        # the DOM element's own .click() to toggle the theme without going
        # through native mouse hit-testing.
        page.evaluate("document.querySelector('.vg-switcher-mode').click()")
        assert box_bg() == "rgb(255, 255, 255)"  # light theme --bg1 (#ffffff)
    finally:
        ctx.close()


# Connection-scoped navigation, overflow and shared-state regression contracts.
def test_connection_tabs_restore_mru_table_and_env(page_envset):
    page = page_envset
    _select_testpg(page)
    page.locator('.tname[data-t="customers"]').click()
    page.wait_for_selector('#grid table')
    page.locator('.tname[data-t="orders"]').click()
    page.wait_for_selector('.tname.on[data-t="orders"]')
    page.locator('.tab[data-i="0"]').click()
    page.wait_for_selector('.tname.on[data-t="customers"]')
    page.fill('.tsearch', 'orders')
    page.locator('.tsearch').press('Escape')
    page.locator('.dbrow[data-db="shop"]').click()
    assert page.locator('#tabs .tab').count() == 1
    assert page.locator('#sql').input_value() == ''
    assert page.locator('.tname.on').count() == 0
    _set_sql(page, 'select 91 as dev_draft')
    page.locator('#esw .ep[data-env="prod"]').click()
    assert page.locator('#sql').input_value() == 'select 91 as dev_draft'
    _set_sql(page, 'select 92 as prod_draft')
    page.locator('.dbrow[data-db="testpg"]').click()
    assert page.locator('#tabs .tab').count() == 2
    page.wait_for_selector('.tname.on[data-t="customers"]')
    assert page.locator('.tsearch').input_value() == ''
    assert page.locator('#sql').input_value() == 'select * from customers'
    with page.expect_download() as dl:
        page.locator('#csvBtn').click()
    assert 'name' in open(dl.value.path(), encoding='utf-8').read()
    page.reload(wait_until='networkidle')
    page.wait_for_selector('.tname.on[data-t="customers"]')
    page.locator('.dbrow[data-db="shop"]').click()
    assert page.locator('#esw .ep.on').get_attribute('data-env') == 'prod'
    assert page.locator('#sql').input_value() == 'select 92 as prod_draft'
    page.locator('#esw .ep[data-env="dev"]').click()
    assert page.locator('#sql').input_value() == 'select 91 as dev_draft'


def test_tab_preview_selection_restores_and_preserves_edited_sql(page):
    _select_testpg(page)
    page.locator('.tname[data-t="customers"]').click()
    page.locator('.tname[data-t="orders"]').click()
    page.locator('#side [data-grp]').first.click()
    assert page.locator('#side .gbody').first.is_hidden()
    page.locator('.tab[data-i="0"]').click()
    page.wait_for_selector('.tname.on[data-t="customers"]')
    assert page.locator('#side .gbody').first.is_visible()
    _set_sql(page, 'select c.id from customers c join orders o on c.id = o.customer_id')
    assert page.locator('.tname.on').count() == 0
    page.locator('.tname[data-t="customers"]').click()
    assert page.locator('#tabs .tab').count() == 3
    page.locator('.tab[data-i="0"]').click()
    assert 'join orders' in page.locator('#sql').input_value()
    assert page.locator('.tname.on').count() == 0
    page.locator('.tname[data-t="customers"]').click()
    assert page.locator('#tabs .tab').count() == 3  # reuse, never duplicate the preview
    page.wait_for_selector('.tname.on[data-t="customers"]')


def test_tab_close_uses_mru_and_keeps_last_close_in_same_group(page_envset):
    page = page_envset
    _select_testpg(page)
    _set_sql(page, 'select 11 as other_database')
    page.locator('.dbrow[data-db="shop"]').click()
    for value in ['first', 'second', 'third']:
        if value != 'first':
            page.locator('#tabAdd').click()
        _set_sql(page, f'select 1 as {value}')
    page.locator('.tab[data-i="0"]').click()
    page.locator('.tab[data-i="2"]').click()
    page.locator('.tab.on .x').click()
    assert page.locator('#sql').input_value() == 'select 1 as first'
    page.locator('#tabList').click()
    page.locator('#closeOtherTabs').click()
    page.locator('.tab.on .x').click()
    assert page.locator('#qtitle').inner_text() == 'shop'
    assert page.locator('#queryEmptyState').is_visible()
    assert page.locator('#sql').count() == 0
    assert page.locator('#tabs .tab').count() == 0
    page.locator('#histBtn').click()
    for value in ['first', 'second', 'third']:
        assert page.locator('.modal .hitem', has_text=f'as {value}').count() == 1
    page.keyboard.press('Escape')
    page.locator('.dbrow[data-db="testpg"]').click()
    assert page.locator('#sql').input_value() == 'select 11 as other_database'


def test_tab_overflow_search_keyboard_and_scoped_bulk_close(page_envset):
    page = page_envset
    _select_testpg(page)
    _set_sql(page, 'select 10 as keep_other_group')
    page.locator('.dbrow[data-db="shop"]').click()
    for i in range(15):
        if i:
            page.locator('#tabAdd').click()
        _set_sql(page, f'select * from long_table_name_{i:02d}')
    page.set_viewport_size({'width': 1000, 'height': 800})
    assert page.locator('#tabs .tab').count() == 15
    assert page.eval_on_selector('#tabs', 'e => e.scrollWidth > e.clientWidth')
    assert page.eval_on_selector('#tabs .tab', 'e => e.getBoundingClientRect().width <= 240')
    assert page.eval_on_selector('#tabs', '''e => {
        const strip = e.getBoundingClientRect(), tab = e.querySelector('.tab').getBoundingClientRect();
        return tab.height === strip.height && strip.height === 35 && tab.top === strip.top && e.parentElement.getBoundingClientRect().height === 36;
    }''')
    assert page.eval_on_selector('#tabAdd', 'e => { const r=e.getBoundingClientRect(); return r.left >= 0 && r.right <= innerWidth; }')
    page.eval_on_selector('#tabs', 'e => { e.scrollLeft = e.scrollWidth; e.dispatchEvent(new Event("scroll")); }')
    page.wait_for_function('document.querySelector("#tabScrollRight").disabled')
    assert page.locator('#tabScrollLeft').is_enabled()
    before_scroll = page.eval_on_selector('#tabs', 'e => e.scrollLeft')
    page.locator('#tabScrollLeft').click()
    page.wait_for_function('(before) => document.querySelector("#tabs").scrollLeft < before', arg=before_scroll)
    page.wait_for_function('!document.querySelector("#tabScrollRight").disabled')
    page.locator('#tabScrollRight').click()
    page.wait_for_function('(before) => document.querySelector("#tabs").scrollLeft >= before - 1', arg=before_scroll)
    page.locator('#tabList').click()
    page.fill('.tab-menu input', 'name_03')
    assert page.locator('.tab-menu-items button').count() == 1
    page.locator('.tab-menu-items button').click()
    assert page.locator('#sql').input_value().endswith('name_03')
    assert page.eval_on_selector('#tabs', '''e => {
        const r=e.getBoundingClientRect(), a=e.querySelector('.on').getBoundingClientRect();
        return a.left >= r.left - 1 && a.right <= r.right + 1;
    }''')
    page.locator('.tab.on').focus()
    page.keyboard.press('ArrowRight')
    assert page.locator('#sql').input_value().endswith('name_04')
    page.keyboard.press('Home')
    assert page.locator('#sql').input_value().endswith('name_00')
    page.keyboard.press('End')
    assert page.locator('#sql').input_value().endswith('name_14')
    page.locator('#tabList').click()
    page.fill('.tab-menu input', 'no such tab')
    assert page.locator('.tab-menu-empty').is_visible()
    page.keyboard.press('Escape')
    assert page.locator('.tab-menu').get_attribute('open') is None
    page.locator('#tabList').click()
    page.locator('#sql').click()
    assert page.locator('.tab-menu').get_attribute('open') is None
    page.locator('#tabList').click()
    page.fill('.tab-menu input', 'name_03')
    page.locator('.tab-menu-items button').click()
    page.locator('#tabList').click()
    page.locator('#closeRightTabs').click()
    assert page.locator('#tabs .tab').count() == 4
    page.locator('#tabList').click()
    page.locator('#closeOtherTabs').click()
    assert page.locator('#tabs .tab').count() == 1
    page.locator('.dbrow[data-db="testpg"]').click()
    assert page.locator('#sql').input_value() == 'select 10 as keep_other_group'


def test_close_all_preserves_more_than_100_tab_drafts(page):
    tabs = [
        {
            'id': f't{i}', 'title': f'draft {i}', 'sql': f'select {i} as draft_{i}',
            'db': 'testpg', 'env': 'test', 'visited': i,
        }
        for i in range(105)
    ]
    page.evaluate(
        "([tabs]) => { localStorage.setItem('qy_tabs', JSON.stringify(tabs)); localStorage.setItem('qy_ati', '104'); }",
        [tabs],
    )
    page.reload(wait_until='networkidle')
    page.wait_for_function("document.querySelectorAll('#tabs .tab').length === 105")
    assert page.locator('#tabs .tab').count() == 105

    page.locator('#tabList').click()
    page.locator('#closeAllTabs').click()
    assert page.locator('#queryEmptyState').is_visible()
    history = page.evaluate("JSON.parse(localStorage.getItem('qy_hist') || '[]')")
    sql = {entry['sql'] for entry in history}
    assert 'select 0 as draft_0' in sql
    assert 'select 104 as draft_104' in sql


def test_tabs_do_not_move_between_workspaces_with_same_db(page_envset):
    page = page_envset
    page.locator('.dbrow[data-db="shop"]').click()
    _set_sql(page, 'select 88 as old_workspace_draft')
    response = page.request.get(page.url.split('/quarry/')[0] + '/api/connections').json()
    for group in response['groups']:
        group['ws'] = '/different/workspace'
    page.route('**/api/connections', lambda route: route.fulfill(json=response))
    page.goto(page.url.split('?')[0], wait_until='networkidle')
    assert page.locator('#sql').input_value() == 'select 88 as old_workspace_draft'
    assert page.locator('#qtitle').inner_text() == 'No connection selected'
    page.locator('.dbrow[data-db="shop"]').click()
    assert page.locator('#sql').input_value() == ''
    saved = page.evaluate("JSON.parse(localStorage.getItem('qy_tabs'))")
    assert saved[0]['sql'] == 'select 88 as old_workspace_draft'
    assert saved[-1]['workspace'] == '/different/workspace'


@pytest.mark.parametrize('close_method', ['button', 'middle', 'keyboard', 'all'])
def test_close_last_tab_shows_empty_and_survives_reload(page, close_method):
    _select_testpg(page)
    _run_sql(page, 'select 42 as final_tab_result')
    if close_method == 'button':
        page.locator('.tab.on .x').click()
    elif close_method == 'middle':
        page.locator('.tab.on').click(button='middle')
    elif close_method == 'keyboard':
        page.keyboard.press('Control+Shift+W')
    else:
        page.locator('#tabList').click()
        page.locator('#closeAllTabs').click()
    page.wait_for_selector('#queryEmptyState')
    assert page.locator('#tabs .tab').count() == 0
    assert page.locator('#sql, #grid, #status').count() == 0
    assert page.locator('.tname.on').count() == 0
    assert page.locator('#qtitle').inner_text() == 'testpg'
    assert page.evaluate("JSON.parse(localStorage.getItem('qy_tabs'))") == []
    assert page.evaluate("JSON.parse(localStorage.getItem('qy_tabres'))") == []
    queries = []
    page.on('request', lambda r: '/api/query' in r.url and queries.append(r.url))
    page.reload(wait_until='networkidle')
    page.wait_for_selector('#queryEmptyState')
    assert queries == []
    assert page.locator('#tabs .tab').count() == 0
    assert page.locator('#qtitle').inner_text() == 'testpg'
    page.locator('#histBtn').click()
    page.locator('.modal .hitem', has_text='final_tab_result').click()
    page.wait_for_selector('#sql')
    assert page.locator('#sql').input_value() == 'select 42 as final_tab_result'
    assert queries == []  # recalling a draft does not run it
    page.locator('.tab.on .x').click()
    page.locator('#emptyNewTab').click()
    assert page.locator('#tabs .tab').count() == 1
    assert page.locator('#sql').input_value() == ''


def test_close_all_tabs_is_scoped_and_empty_group_stays_empty(page_envset):
    page = page_envset
    _select_testpg(page)
    _set_sql(page, 'select 7 as other_connection')
    page.locator('.dbrow[data-db="shop"]').click()
    _set_sql(page, 'select 8 as dev_draft')
    page.locator('#tabAdd').click()
    _set_sql(page, 'select 9 as dev_draft_two')
    page.locator('#esw .ep[data-env="prod"]').click()
    _set_sql(page, 'select 10 as prod_draft')
    page.locator('#esw .ep[data-env="dev"]').click()
    page.locator('#tabList').click()
    page.locator('#closeAllTabs').click()
    page.wait_for_selector('#queryEmptyState')
    page.locator('#esw .ep[data-env="prod"]').click()
    assert page.locator('#sql').input_value() == 'select 10 as prod_draft'
    page.locator('#esw .ep[data-env="dev"]').click()
    page.wait_for_selector('#queryEmptyState')
    page.reload(wait_until='networkidle')
    page.wait_for_selector('#queryEmptyState')
    assert page.locator('#esw .ep.on').get_attribute('data-env') == 'dev'
    page.locator('.dbrow[data-db="testpg"]').click()
    assert page.locator('#sql').input_value() == 'select 7 as other_connection'
    page.locator('.dbrow[data-db="shop"]').click()
    # The last tab used on this db was prod, since dev is empty; explicitly revisit dev.
    page.locator('#esw .ep[data-env="dev"]').click()
    page.wait_for_selector('#queryEmptyState')
    page.locator('.tname[data-t="customers"]').click()
    page.wait_for_selector('#sql')
    page.wait_for_selector('.tname.on[data-t="customers"]')
    assert page.locator('#tabs .tab').count() == 1
    assert page.locator('#sql').input_value() == 'select * from customers'
    page.locator('#tabList').click()
    page.locator('#closeAllTabs').click()
    page.locator('#tabAdd').click()
    assert page.locator('#sql').input_value() == ''
    assert page.locator('#esw .ep.on').get_attribute('data-env') == 'dev'


def test_closed_query_response_cannot_resurrect_tab(page):
    _select_testpg(page)
    _set_sql(page, "select pg_sleep(0.7), 17 as late_result")
    page.locator('#runBtn').click()
    page.wait_for_selector('#grid .spin')
    page.locator('.tab.on .x').click()
    page.wait_for_selector('#queryEmptyState')
    page.wait_for_timeout(1000)
    assert page.locator('#tabs .tab').count() == 0
    assert page.evaluate("JSON.parse(localStorage.getItem('qy_tabs'))") == []
    page.locator('#emptyNewTab').click()
    assert page.locator('#grid table').count() == 0


def test_saved_query_opens_from_empty_group(page_noparam):
    page = page_noparam
    _select_testpg(page)
    page.locator('.tab.on .x').click()
    page.wait_for_selector('#queryEmptyState')
    page.locator('.qname[data-q="all-cust"]').click()
    page.wait_for_selector('#grid table tbody tr')
    assert page.locator('#tabs .tab').count() == 1
    assert page.locator('#qtitle').inner_text() == 'testpg'


def test_alt_table_opens_editor_from_empty_without_running(page):
    _select_testpg(page)
    page.locator('.tab.on .x').click()
    page.wait_for_selector('#queryEmptyState')
    requests = []
    page.on('request', lambda r: '/api/query' in r.url and requests.append(r.url))
    page.locator('.tname[data-t="customers"]').click(modifiers=['Alt'])
    page.wait_for_selector('#sql')
    assert page.locator('#sql').input_value() == 'select * from customers'
    assert requests == []
    assert page.locator('#grid table').count() == 0


def test_redis_key_opens_editor_from_empty_group(page_redis):
    page = page_redis
    page.locator('.dbrow[data-db="testredis"]').click()
    page.wait_for_selector('.tname[data-key="qygui:sess:1"]')
    page.locator('.tab.on .x').click()
    page.wait_for_selector('#queryEmptyState')
    page.locator('.tname[data-key="qygui:sess:1"]').click()
    page.wait_for_selector('#grid table tbody tr')
    assert page.locator('#tabs .tab').count() == 1
    assert page.locator('#sql').input_value() == '# qygui:sess:1'


@pytest.fixture()
def page_explicit_production(_pw_browser, tmp_path):
    config = ENVSET_TOML.replace('production = true', 'production = false') + f'''
[shop_jp]
url = "{TEST_DB_URL}"
engine = "postgres"
env = "jp"
production = true
db = "shop"
group = "acme"
'''
    with _running_gui(tmp_path, extra_conn=config) as url:
        ctx, page = _mk_page(_pw_browser, url)
        try:
            yield page
        finally:
            ctx.close()


def test_production_context_follows_connection_without_toasts(page_explicit_production):
    page = page_explicit_production
    page.locator('.dbrow[data-db="shop"]').click()
    _set_sql(page, 'select 42 as production_manual')
    queries = []
    page.on('request', lambda r: '/api/query' in r.url and queries.append(r.post_data_json))

    def assert_context(production):
        assert page.locator('.app-shell').get_attribute('data-production') == str(production).lower()
        assert page.locator('header #prodBadge, .qhead #prodBadge, .qhead .production-status').count() == 0
        assert page.locator('#productionHint').count() == 0
        assert page.locator('#runBtn').get_attribute('aria-describedby') is None
        assert page.locator('#toast').is_hidden()
        if production:
            assert page.locator('.tab-navigation .tab-context #prodBadge').inner_text().strip() == 'Production'
            assert page.locator('#prodBadge').get_attribute('title') == 'Production connection — Auto-run is off by default'
            assert page.locator('#sql').get_attribute('aria-describedby') == 'prodBadge'
        else:
            assert page.locator('#prodBadge').count() == 0
            assert page.locator('#sql').get_attribute('aria-describedby') is None
        assert page.locator('.tab-navigation .tab-context').count() == 1
        assert page.locator('.edwrap #prodBadge, .production-watermark').count() == 0

    assert_context(False)
    # Environment names are labels: jp is protected, while prod is not.
    page.locator('#esw .ep[data-env="jp"]').click()
    page.locator('#prodBadge').wait_for()
    assert_context(True)
    assert page.locator('#sql').input_value() == 'select 42 as production_manual'
    page.wait_for_timeout(250)
    assert queries == []
    page.locator('#runBtn').click()
    page.wait_for_selector('#grid table tbody tr')
    assert len(queries) == 1 and queries[0]['env'] == 'jp'
    assert_context(True)  # the context remains after an explicit run

    page.locator('#esw .ep[data-env="prod"]').click()
    page.wait_for_selector('#grid table tbody tr')
    assert len(queries) == 2 and queries[-1]['env'] == 'prod'
    assert_context(False)
    page.locator('#esw .ep[data-env="jp"]').click()
    assert_context(True)
    page.reload(wait_until='networkidle')
    page.locator('#prodBadge').wait_for()
    assert_context(True)
    assert len(queries) == 2

    # Closing the final editor does not disguise the connection's production state.
    page.locator('.tab.on .x').click()
    page.wait_for_selector('#queryEmptyState')
    assert page.locator('.app-shell').get_attribute('data-production') == 'true'
    assert page.locator('.tab-navigation .tab-context #prodBadge').is_visible()
    assert page.locator('header #prodBadge, .qhead #prodBadge').count() == 0
    assert page.locator('#productionHint').count() == 0
    page.reload(wait_until='networkidle')
    page.wait_for_selector('#queryEmptyState')
    assert page.locator('.app-shell').get_attribute('data-production') == 'true'
    assert page.locator('.tab-navigation .tab-context #prodBadge').is_visible()
    assert page.locator('header #prodBadge, .qhead #prodBadge').count() == 0
    assert page.locator('#productionHint').count() == 0
    assert page.locator('#toast').is_hidden()
    assert len(queries) == 2
    page.locator('#emptyNewTab').click()
    assert_context(True)
    _select_testpg(page)
    assert_context(False)


@pytest.mark.parametrize('width', [1280, 900])
@pytest.mark.parametrize('lang', ['en', 'zh'])
def test_production_context_has_stable_layout(page_envset, width, lang):
    page = page_envset
    page.set_viewport_size({'width': width, 'height': 900})
    if lang == 'zh':
        page.locator('.vg-lang-switch').click()
    page.locator('.dbrow[data-db="shop"]').click()
    page.locator('#esw .ep').first.wait_for()

    def geometry():
        return page.evaluate('''() => Object.fromEntries([
            '.qhead', '.connection-context', '#qtitle', '#esw', '#ciBtn',
            '#esw [data-env="dev"]', '#esw [data-env="prod"]',
            '#tabs', '.tab-context', '.edwrap', '#runBtn', '#runModeBtn', '#maxRows'
        ].map(selector => {
            const {x, y, width, height} = document.querySelector(selector).getBoundingClientRect();
            return [selector, {x, y, width, height}];
        }))''')

    baseline = geometry()
    for env in ('prod', 'dev', 'prod', 'dev'):
        page.locator(f'#esw .ep[data-env="{env}"]').click()
        page.wait_for_function('(production) => document.querySelector(".app-shell").dataset.production === production',
                               arg=str(env == 'prod').lower())
        current = geometry()
        for selector, dimensions in baseline.items():
            for dimension, value in dimensions.items():
                assert current[selector][dimension] == pytest.approx(value, abs=0.5), (width, lang, env, selector, dimension)
        if env == 'prod':
            label = page.locator('#prodBadge').bounding_box()
            editor = page.locator('.edwrap').bounding_box()
            context = page.locator('.tab-context').bounding_box()
            assert label['x'] >= context['x']
            assert label['x'] + label['width'] <= context['x'] + context['width']
            assert label['y'] >= context['y']
            assert label['y'] + label['height'] <= context['y'] + context['height']
            assert label['y'] + label['height'] <= editor['y']
            assert page.locator('#prodBadge').inner_text().strip() == ('生产环境' if lang == 'zh' else 'Production')
            assert page.locator('header #prodBadge, .qhead #prodBadge').count() == 0
    assert page.locator('#prodBadge, .production-status, .production-watermark').count() == 0
    assert page.locator('.tab-navigation .tab-context').count() == 1


def test_production_tab_context_leaves_sql_unobstructed_when_scrolled_or_resized(page_envset):
    page = page_envset
    page.locator('.dbrow[data-db="shop"]').click()
    page.locator('#esw .ep[data-env="prod"]').click()
    badge = page.locator('.tab-navigation .tab-context #prodBadge')
    badge.wait_for()
    assert page.locator('.edwrap #prodBadge, .production-watermark').count() == 0
    assert page.locator('#sql').get_attribute('aria-describedby') == 'prodBadge'
    requests = _capture_query_requests(page)

    # Connection context stays outside the editable SQL surface.
    box = page.locator('.edwrap').bounding_box()
    badge_before = badge.bounding_box()
    assert badge_before['y'] + badge_before['height'] <= box['y']
    x, y = box['x'] + box['width'] - 24, box['y'] + box['height'] - 24
    assert page.evaluate('([x, y]) => document.elementFromPoint(x, y)?.id', [x, y]) == 'sql'
    page.mouse.click(x, y)
    assert page.locator('#sql').evaluate('el => el === document.activeElement')
    sql = 'select 42 as context_probe'
    page.keyboard.insert_text(sql)
    assert page.locator('#sql').input_value() == sql
    page.keyboard.press('ControlOrMeta+Enter')
    page.wait_for_selector('#grid td[data-v="42"]')
    assert len(requests) == 1 and requests[0]['body']['sql'] == sql
    assert badge.is_visible()

    long_sql = '\n'.join(f'-- scroll line {index}' for index in range(60)) + '\n' + sql
    _set_sql(page, long_sql)
    page.locator('#sql').evaluate('el => { el.scrollTop = 0; el.dispatchEvent(new Event("scroll")); }')
    before_scroll = badge.bounding_box()
    page.locator('#sql').hover(position={'x': 20, 'y': 20})
    page.mouse.wheel(0, 600)
    page.wait_for_function('document.querySelector("#sql").scrollTop > 0')
    after_scroll = badge.bounding_box()
    for dimension in ('x', 'y', 'width', 'height'):
        assert after_scroll[dimension] == pytest.approx(before_scroll[dimension], abs=0.5)

    editor_before = page.locator('.edwrap').bounding_box()
    _drag(page, '#edresizer', 0, 70)
    editor_after = page.locator('.edwrap').bounding_box()
    assert editor_after['height'] >= editor_before['height'] + 50
    for dimension in ('x', 'y', 'width', 'height'):
        assert badge.bounding_box()[dimension] == pytest.approx(before_scroll[dimension], abs=0.5)
    assert page.locator('#sql').input_value() == long_sql
    assert len(requests) == 1  # Scrolling and resizing never execute the edited SQL.
    page.locator('#esw .ep[data-env="dev"]').click()
    assert page.locator('#prodBadge').count() == 0
    assert page.locator('#sql').get_attribute('aria-describedby') is None


def test_single_production_connection_context_is_localized(page):
    def production_connections(route):
        response = route.fetch()
        data = response.json()
        for group in data['groups']:
            for item in group['items']:
                for env in item['envs']:
                    env['production'] = True
        route.fulfill(response=response, json=data)

    page.route('**/api/connections', production_connections)
    page.reload(wait_until='networkidle')
    _select_testpg(page)
    assert page.locator('#esw .ep').count() == 0
    assert page.locator('.tab-navigation .tab-context #prodBadge').inner_text().strip() == 'Production'
    assert page.locator('header #prodBadge, .qhead #prodBadge').count() == 0
    assert page.locator('#productionHint').count() == 0
    page.locator('.vg-lang-switch').click()
    page.wait_for_function("document.querySelector('#prodBadge')?.textContent.trim() === '生产环境'")
    assert page.locator('#prodBadge').get_attribute('title') == '生产连接：自动运行默认关闭'
    assert page.locator('#runBtn').get_attribute('aria-describedby') is None
    assert page.locator('#sql').get_attribute('aria-describedby') == 'prodBadge'
    assert page.locator('.app-shell').get_attribute('data-production') == 'true'
    assert page.locator('#toast').is_hidden()

    def removed_connection(route):
        response = route.fetch()
        data = response.json()
        data['groups'] = []
        route.fulfill(response=response, json=data)

    page.route('**/api/connections', removed_connection)
    page.reload(wait_until='networkidle')
    assert page.locator('.app-shell').get_attribute('data-production') == 'false'
    assert page.locator('#prodBadge, #productionHint').count() == 0
    assert page.locator('#runBtn').get_attribute('aria-describedby') is None
    assert page.locator('#sql').get_attribute('aria-describedby') is None


def test_env_preview_autorun_uses_explicit_production(page_explicit_production):
    page = page_explicit_production
    page.locator('.dbrow[data-db="shop"]').click()
    page.locator('#tbl-panel .tname[data-t="customers"]').click(modifiers=['Alt'])
    queries = []
    page.on('request', lambda r: '/api/query' in r.url and queries.append(r.post_data_json))
    page.locator('#esw .ep[data-env="jp"]').click()
    page.locator('#prodBadge').wait_for(state='visible')
    assert page.locator('#sql').input_value() == 'select * from customers'
    page.wait_for_timeout(250)
    assert queries == []
    page.locator('#esw .ep[data-env="prod"]').click()
    page.locator('#prodBadge').wait_for(state='hidden')
    page.wait_for_selector('#grid table tbody tr')
    assert len(queries) == 1 and queries[0]['env'] == 'prod'
    page.locator('#esw .ep[data-env="jp"]').click()
    page.locator('#esw .ep[data-env="prod"]').click()
    page.wait_for_timeout(250)
    assert len(queries) == 1  # returning restores the saved result
    page.locator('#esw .ep[data-env="dev"]').click()
    page.wait_for_selector('#grid table tbody tr')
    assert len(queries) == 2 and queries[-1]['env'] == 'dev'


def test_env_manual_sql_carries_and_autoruns_except_production(page_explicit_production):
    page = page_explicit_production
    page.locator('.dbrow[data-db="shop"]').click()
    _set_sql(page, 'SELECT 42 AS handwritten')
    queries = []
    page.on('request', lambda r: '/api/query' in r.url and queries.append(r.post_data_json))
    page.locator('#esw .ep[data-env="jp"]').click()
    assert page.locator('#sql').input_value() == 'SELECT 42 AS handwritten'
    page.wait_for_timeout(250)
    assert queries == []
    page.locator('#esw .ep[data-env="prod"]').click()
    page.wait_for_selector('#grid table tbody tr')
    assert queries[-1]['sql'] == 'SELECT 42 AS handwritten'
    assert queries[-1]['env'] == 'prod'
    _set_sql(page, 'SELECT 7 AS destination_draft')
    page.locator('#esw .ep[data-env="dev"]').click()
    page.wait_for_function("document.querySelector('#grid').textContent.includes('handwritten')")
    assert page.locator('#sql').input_value() == 'SELECT 42 AS handwritten'
    page.locator('#esw .ep[data-env="prod"]').click()
    page.wait_for_function("document.querySelector('#grid').textContent.includes('handwritten')")
    assert page.locator('#sql').input_value() == 'SELECT 7 AS destination_draft'
    page.wait_for_timeout(300)
    assert len(queries) == 2  # draft and last executed result are both preserved
    with page.expect_download() as download:
        page.locator('#csvBtn').click()
    assert 'handwritten' in open(download.value.path(), encoding='utf-8').read()
    page.locator('#runBtn').click()
    page.wait_for_function("document.querySelector('#grid').textContent.includes('destination_draft')")
    assert len(queries) == 3  # only explicit Run replaces the saved result
    page.locator('#esw .ep[data-env="dev"]').click()
    page.locator('#esw .ep[data-env="prod"]').click()
    page.wait_for_timeout(300)
    assert page.locator('#sql').input_value() == 'SELECT 7 AS destination_draft'
    assert 'destination_draft' in page.locator('#grid').inner_text()
    assert len(queries) == 3


@pytest.mark.parametrize('mode', ['dark', 'light'])
def test_workbench_visual_hierarchy(page_envset, mode):
    page = page_envset
    page.locator('.dbrow[data-db="shop"]').click()
    page.evaluate('(mode) => document.documentElement.dataset.mode = mode', mode)
    def matches(selector, prop, value, pseudo=None):
        return page.evaluate("""([selector, prop, value, pseudo]) => {
            const expected = document.createElement('span');
            expected.style[prop] = value;
            document.body.append(expected);
            const color = getComputedStyle(expected)[prop]; expected.remove();
            return getComputedStyle(document.querySelector(selector), pseudo)[prop] === color;
        }""", [selector, prop, value, pseudo])
    assert matches('#tabs .on', 'backgroundColor', 'color-mix(in srgb,var(--acc) 12%,var(--surf-2))')
    assert page.eval_on_selector('#tabs .on', 'e => getComputedStyle(e).fontWeight') == '600'
    assert matches('#esw .on', 'backgroundColor', 'color-mix(in srgb,var(--acc) 20%,var(--surf-1))')
    assert matches('#esw .on', 'borderTopColor', 'var(--acc)')
    assert page.eval_on_selector('#esw .on', 'e => getComputedStyle(e).fontWeight') == '600'
    assert matches('#esw .on', 'color', 'var(--fg)')
    assert matches('.run-split', 'backgroundColor', 'var(--surf-1)')
    assert matches('#runBtn', 'backgroundColor', 'transparent')
    assert matches('#runBtn', 'color', 'var(--acc)')
    page.locator('#runBtn').hover()
    assert matches('#runBtn', 'backgroundColor', 'var(--surf-2)')
    assert matches('#runBtn', 'color', 'var(--acc)')


@pytest.mark.parametrize("lang", ["en", "zh"])
def test_icon_hover_hints_cover_header_workbench_and_modals(page, lang):
    from playwright.sync_api import expect

    if lang == "zh":
        page.locator(".vg-lang-switch").click()
        expect(page.locator("#runLbl")).to_have_text("运行")
    assert page.locator("#roBadge").count() == 0
    header_icons = ["#wsBtn", "#kaBadge", "#healthBtn", ".vg-lang-switch",
                    ".vg-switcher-mode", ".vg-switcher-trigger"]
    for selector in header_icons:
        button = page.locator(selector)
        button.hover()
        expect(button).to_have_attribute("title", button.get_attribute("aria-label"))
        assert button.get_attribute("title").strip()

    mode = page.locator(".vg-switcher-mode")
    before = mode.get_attribute("title")
    mode.click()
    expect(mode).not_to_have_attribute("title", before)
    mode.hover()
    expect(mode).to_have_attribute("title", mode.get_attribute("aria-label"))

    _select_testpg(page)
    for selector in ["#ciBtn", "#tabAdd", "#tabList", "#runBtn", "#fmtBtn",
                     "#csvBtn", "#jsonBtn", "#histBtn", "#linkBtn", ".treload"]:
        if selector == '.treload':
            page.locator('.dbrow.on').hover()
        button = page.locator(selector)
        button.hover()
        assert (button.get_attribute("title") or "").strip(), selector
    close = page.locator("#tabs button[aria-label^='Close tab'], #tabs button[aria-label^='关闭']").first
    close.hover()
    assert close.get_attribute("title")
    group = page.locator("[data-grp]").first
    group.hover()
    previous = group.get_attribute("title")
    group.click()
    expect(group).not_to_have_attribute("title", previous)
    group.click()

    page.locator("#ciBtn").click()
    for selector in ["#ciEye", "#ciCopy"]:
        button = page.locator(selector)
        button.hover()
        assert button.get_attribute("title")
    eye = page.locator("#ciEye")
    previous = eye.get_attribute("title")
    eye.click()
    expect(eye).not_to_have_attribute("title", previous)
    page.keyboard.press("Escape")
    page.locator("#wsBtn").click()
    for button in page.locator(".wsdel").all():
        button.hover()
        assert button.get_attribute("title")


def test_saved_queries_stay_in_owning_workspace(page_noparam):
    page = page_noparam
    tree = page.request.get(page.url.split('/quarry/')[0] + '/api/connections').json()
    item = next(i for g in tree['groups'] for i in g['items'] if i['db'] == 'testpg')
    tree['groups'] = [dict(ws='/ws/one', group='One', items=[item]),
                      dict(ws='/ws/two', group='Two', items=[])]
    tree['workspaces'] = ['/ws/one', '/ws/two', '/ws/only-queries']
    queries = [dict(name='same', db='testpg', ws=f'/ws/{ws}', queryId=ws,
                    desc='', sql=f'SELECT {n}', params=[])
               for n, ws in enumerate(['one', 'two', 'only-queries'], 1)]
    # Exercise the modal path as well as direct execution with identical names.
    queries[1]['params'] = [dict(name='x', type=None, required=False, default='1')]
    page.route('**/api/connections', lambda r: r.fulfill(json=tree))
    page.route('**/api/queries', lambda r: r.fulfill(json=queries))
    runs = []
    def run(route):
        body = route.request.post_data_json
        runs.append(body)
        route.fulfill(json=dict(columns=[dict(name='value', type='int4')], rows=[[1]],
                                sql='SELECT 1', db='testpg', env='test', elapsed_ms=1,
                                truncated=False))
    page.route('**/api/run', run)
    page.reload(wait_until='networkidle')
    one = page.locator('[data-saved-ws="/ws/one"]')
    two = page.locator('[data-saved-ws="/ws/two"]')
    only = page.locator('[data-saved-ws="/ws/only-queries"]')
    one.wait_for()
    assert only.is_visible()  # no connection group for this workspace
    assert page.locator('#side > div > [data-saved-ws]').count() == 0
    q1 = one.locator('xpath=..').locator('.qname')
    q2 = two.locator('xpath=..').locator('.qname')
    one.click()
    assert q1.is_hidden() and q2.is_visible()
    page.locator('[data-gkey="/ws/two::Two"]').click()
    assert two.is_hidden() and q2.is_hidden()
    page.reload(wait_until='networkidle')
    one.wait_for()
    assert q1.is_hidden() and two.is_hidden()
    page.locator('[data-gkey="/ws/two::Two"]').click()
    q2.click()
    page.locator('.modal').wait_for()
    page.locator('.modal input').first.press('Enter')
    page.wait_for_function('document.querySelector("#grid table") !== null')
    assert runs[-1]['queryId'] == 'two'
    one.click()
    with page.expect_response('**/api/run'):
        q1.click()
    assert runs[-1]['queryId'] == 'one'


def test_row_record_link_uses_result_context_and_does_not_execute(page_clip):
    page = page_clip
    _select_testpg(page)
    _run_sql(page, 'select * from customers order by id')
    source_sql = page.locator('#sql').input_value()
    first_id = page.locator('#grid tbody tr').first.locator('td').nth(1).inner_text()
    _set_sql(page, 'select 123 as later_editor_draft')
    original_tab = page.locator('#tabs [aria-selected="true"]').get_attribute('data-i')
    page.locator('#tabAdd').click()
    _run_sql(page, 'select * from customers where id = 2')
    page.locator('#tabs [data-i="' + original_tab + '"]').click()
    requests = []
    page.on('request', lambda req: requests.append(req.url) if req.method == 'POST'
            and (req.url.endswith('/api/query') or req.url.endswith('/api/run')) else None)
    page.locator('#grid tbody tr').first.locator('td').first.click()
    button = page.locator('#recordLinkBtn')
    from playwright.sync_api import expect
    expect(button).to_be_enabled()
    button.click()
    page.wait_for_function("navigator.clipboard.readText().then(s => s.includes('sql='))")
    link = page.evaluate('navigator.clipboard.readText()')
    params = parse_qs(urlparse(link).query)
    assert params['db'] == ['testpg']
    assert params['env'] == ['test']
    assert params['table'] == ['customers']
    assert params['sql'] == [f'SELECT * FROM "customers" WHERE "id" = {first_id};']
    assert urlparse(link).netloc == urlparse(page.url).netloc
    assert page.locator('#sql').input_value() == 'select 123 as later_editor_draft'
    assert not requests
    page.keyboard.press('Escape')
    # Round-trip through the existing link handler with default Auto enabled.
    page.goto(link, wait_until='networkidle')
    assert page.locator('#sql').input_value() == params['sql'][0]
    page.wait_for_selector('#grid tbody tr')
    assert page.locator('#grid tbody tr').count() == 1
    assert source_sql != params['sql'][0]


def test_row_record_link_disabled_reason_and_metadata_failure(page_clip):
    page = page_clip
    _select_testpg(page)
    _run_sql(page, 'select count(*) as total from customers')
    page.locator('#grid tbody tr').first.locator('td').first.click()
    from playwright.sync_api import expect
    expect(page.locator('#recordLinkBtn')).to_be_disabled()
    expect(page.get_by_role('status').filter(has_text='simple single-table SELECT')).to_be_visible()
    page.keyboard.press('Escape')
    _run_sql(page, 'select name from customers')
    page.locator('#grid tbody tr').first.locator('td').first.click()
    expect(page.get_by_role('status').filter(has_text='primary or unique key')).to_be_visible()
    page.keyboard.press('Escape')
    page.route('**/api/record-link', lambda route: route.fulfill(json={'reason': 'metadata_unavailable'}))
    _run_sql(page, 'select * from customers')
    page.locator('#grid tbody tr').first.locator('td').first.click()
    expect(page.get_by_role('status').filter(has_text='Could not verify')).to_be_visible()
    expect(page.locator('#recordLinkBtn')).to_be_disabled()


def test_row_record_link_ordinary_string_key_omits_escape_prefix(page_clip, pg_exec):
    page = page_clip
    rc, _, err = pg_exec('CREATE TABLE qy_record_link_strings (package_name text PRIMARY KEY); '
                        "INSERT INTO qy_record_link_strings VALUES ('jp.co.yahoo.android.news')")
    assert rc == 0, err
    try:
        _select_testpg(page)
        _run_sql(page, "select * from qy_record_link_strings where 'union,a;(select)' = 'union,a;(select)'")
        page.locator('#grid tbody tr').first.locator('td').first.click()
        from playwright.sync_api import expect
        expect(page.locator('#recordLinkBtn')).to_be_enabled()
        page.locator('#recordLinkBtn').click()
        page.wait_for_function("navigator.clipboard.readText().then(s => s.includes('sql='))")
        params = parse_qs(urlparse(page.evaluate('navigator.clipboard.readText()')).query)
        assert params['sql'] == ['SELECT * FROM "qy_record_link_strings" '
                                 'WHERE "package_name" = \'jp.co.yahoo.android.news\';']
    finally:
        pg_exec('DROP TABLE qy_record_link_strings')


# SQLite's browser coverage uses real, separate files for each environment and
# must remain runnable on machines without a Postgres server or Docker.
def _select_sqlite(page):
    page.locator('.dbrow[data-db="notes"]').click()
    page.locator('#tbl-panel [data-t="entries"]').wait_for()


def test_sqlite_browse_structure_quoted_tables_and_file_info(page_sqlite, tmp_path):
    page = page_sqlite
    _select_sqlite(page)
    assert page.locator('.dbrow[data-db="notes"] .vg-engine-tag').inner_text() == 'sqlite'
    assert page.locator('#esw .ep').evaluate_all('(els) => els.map(el => el.dataset.env)') == ['local', 'dev', 'prod']
    assert page.locator('#tbl-panel [data-t="recent_entries"]').is_visible()
    _set_sql(page, 'select 999 as keep_my_draft')
    page.locator('#tbl-panel [data-t="order"]').click()
    page.locator('#grid td[data-v="dev reserved"]').wait_for()
    assert page.locator('#sql').input_value() == 'select * from "order"'
    assert page.evaluate("JSON.parse(localStorage.qy_tabs).some(tab => tab.sql === 'select 999 as keep_my_draft')")
    page.locator('#tbl-panel [data-t="daily.events"]').click()
    page.locator('#grid td[data-v="dev dotted"]').wait_for()
    assert page.locator('#sql').input_value() == 'select * from "daily.events"'
    page.locator('#tbl-panel [data-t="daily.events"]').dblclick()
    page.locator('#structbox .cirow').first.wait_for()
    structure = page.locator('#structbox').inner_text()
    assert 'INTEGER' in structure and 'TEXT' in structure and 'note' in structure
    page.keyboard.press('Escape')
    page.locator('#ciBtn').click()
    page.locator('#cihealth.ok').wait_for()
    assert str((tmp_path / 'notes-dev.db').resolve()) in page.locator('#cibody').inner_text()
    assert page.locator('#ciUp, #ciSync').count() == 0
    assert 'host' not in page.locator('#cibody .cik').all_inner_texts()
    page.keyboard.press('Escape')
    # SQLite must not offer a Docker local environment even without a local sibling.
    page.locator('.dbrow[data-db="archive"]').click()
    page.locator('#ciBtn').click()
    page.locator('#cihealth.ok').wait_for()
    assert page.locator('#ciUp, #ciSync').count() == 0


def test_sqlite_load_more_sort_export_and_tab_restore(page_sqlite):
    from playwright.sync_api import expect

    page = page_sqlite
    _select_sqlite(page)
    page.select_option('#maxRows', '100')
    _run_sql(page, 'select * from entries order by id')
    expect(page.locator('#grid tbody tr')).to_have_count(100)
    page.locator('#grid th[data-i="0"]').click()
    page.locator('#grid th[data-i="0"]').click()
    page.locator('#loadMoreBtn').click()
    expect(page.locator('#grid tbody tr')).to_have_count(200)
    assert _col_values(page)[0] == '200'
    page.locator('#loadMoreBtn').click()
    expect(page.locator('#grid tbody tr')).to_have_count(250)
    assert _col_values(page) == [str(i) for i in range(250, 0, -1)]
    assert page.locator('#loadMoreBtn').count() == 0
    first_tab = page.locator('.tab[aria-selected="true"]').get_attribute('data-i')
    page.locator('#tabAdd').click()
    _run_sql(page, "select 'another tab' as note")
    page.locator('#grid td[data-v="another tab"]').wait_for()
    page.locator(f'.tab[data-i="{first_tab}"]').click()
    expect(page.locator('#grid tbody tr')).to_have_count(250)
    with page.expect_download() as download:
        page.locator('#jsonBtn').click()
    rows = json.loads(download.value.path().read_text(encoding='utf-8'))
    assert len(rows) == 250 and rows[0] == {'id': 1, 'note': 'dev note 1'}
    with page.expect_download() as download:
        page.locator('#csvBtn').click()
    csv = download.value.path().read_text(encoding='utf-8-sig')
    assert 'id,note' in csv and '250,dev note 250' in csv and 'another tab' not in csv
    page.reload(wait_until='networkidle')
    expect(page.locator('#grid tbody tr')).to_have_count(250)
    assert page.locator('#sql').input_value() == 'select * from entries order by id'


def test_sqlite_environment_files_and_production_autorun(page_sqlite):
    page = page_sqlite
    _select_sqlite(page)
    _run_sql(page, 'select note from entries where id = 1')
    page.locator('#grid td[data-v="dev note 1"]').wait_for()
    page.locator('#esw .ep[data-env="local"]').click()
    page.locator('#grid td[data-v="local note 1"]').wait_for()
    requests = []
    page.on('request', lambda req: requests.append(req.post_data_json)
            if req.method == 'POST' and req.url.endswith('/api/query') else None)
    page.locator('#esw .ep[data-env="prod"]').click()
    page.locator('#prodBadge').wait_for()
    _assert_run_mode(page, False)
    assert not requests
    assert page.locator('#grid td[data-v="local note 1"]').count() == 0
    _choose_run_mode(page, True)
    assert not requests
    page.locator('#runBtn').click()
    page.locator('#grid td[data-v="prod note 1"]').wait_for()
    assert requests[-1]['env'] == 'prod'
    page.locator('#esw .ep[data-env="dev"]').click()
    page.locator('#grid td[data-v="dev note 1"]').wait_for()
    with page.expect_download() as download:
        page.locator('#jsonBtn').click()
    assert json.loads(download.value.path().read_text()) == [{'note': 'dev note 1'}]


def test_sqlite_explain_query_plan_and_gui_readonly(page_sqlite):
    page = page_sqlite
    _select_sqlite(page)
    requests = []
    page.on('request', lambda req: requests.append(req.post_data_json)
            if req.method == 'POST' and req.url.endswith('/api/query') else None)
    _set_sql(page, 'select * from entries where id = 1')
    page.locator('#expBtn').click()
    page.wait_for_function("document.querySelector('#grid table')?.innerText.includes('detail')")
    assert requests[-1]['sql'] == 'EXPLAIN QUERY PLAN select * from entries where id = 1'
    assert 'SEARCH entries' in page.locator('#grid').inner_text()
    assert page.locator('#loadMoreBtn').count() == 0
    _set_sql(page, 'EXPLAIN QUERY PLAN select * from entries')
    page.locator('#expBtn').click()
    page.wait_for_function("document.querySelector('#grid table')?.innerText.includes('SCAN entries')")
    assert requests[-1]['sql'] == 'EXPLAIN QUERY PLAN select * from entries'
    _run_sql(page, 'delete from entries')
    page.locator('#grid .err').wait_for()
    assert 'read-only' in page.locator('#grid .err').inner_text().lower()
    _run_sql(page, 'select count(*) as n from entries')
    page.locator('#grid td[data-v="250"]').wait_for()


def test_sqlite_latest_response_stays_on_origin_tab(page_sqlite):
    page = page_sqlite
    _select_sqlite(page)
    delayed = []
    page.route('**/api/query', lambda route: delayed.append(route)
               if 'slow_result' in route.request.post_data_json['sql'] else route.continue_())
    _set_sql(page, 'select 1 as slow_result')
    page.locator('#runBtn').click()
    page.wait_for_function("document.querySelector('#grid .spin') !== null")
    _set_sql(page, 'select 2 as newest_result')
    page.locator('#sql').press('ControlOrMeta+Enter')
    page.locator('#grid td[data-v="2"]').wait_for()
    assert delayed
    delayed.pop().continue_()
    page.wait_for_timeout(150)
    assert page.locator('#grid td[data-v="1"]').count() == 0
    assert page.locator('#grid td[data-v="2"]').count() == 1
    _set_sql(page, 'select 1 as slow_result')
    page.locator('#runBtn').click()
    page.wait_for_function("document.querySelector('#grid .spin') !== null")
    page.locator('#tabAdd').click()
    _run_sql(page, 'select 3 as another_tab')
    page.locator('#grid td[data-v="3"]').wait_for()
    delayed.pop().continue_()
    page.wait_for_function("JSON.parse(localStorage.qy_tabres).some(s => s?.res?.rows[0]?.slow_result === 1)")
    assert page.locator('#grid td[data-v="1"]').count() == 0
    page.locator('.tab[data-i="0"]').click()
    page.locator('#grid td[data-v="1"]').wait_for()


def test_sqlite_record_link_uses_producing_sql_and_selects_one_row(page_sqlite):
    from playwright.sync_api import expect

    page = page_sqlite
    page.context.grant_permissions(['clipboard-read', 'clipboard-write'])
    _select_sqlite(page)
    page.locator('#tbl-panel [data-t="entries"]').click()
    expect(page.locator('#grid tbody tr')).to_have_count(250)
    # The result retains the executed, automatically capped SELECT, even when
    # the editor now contains an unrelated draft.
    draft = 'select 999 as later_editor_draft'
    _set_sql(page, draft)
    record_requests, queries = [], []
    page.on('request', lambda req: record_requests.append(req.post_data_json)
            if req.method == 'POST' and req.url.endswith('/api/record-link') else None)
    page.on('request', lambda req: queries.append(req.post_data_json)
            if req.method == 'POST' and req.url.endswith('/api/query') else None)
    page.locator('#grid tbody tr').nth(1).locator('td').first.click()
    expect(page.locator('#recordLinkBtn')).to_be_enabled()
    assert record_requests[-1]['row'] == {'id': 2, 'note': 'dev note 2'}
    assert 'from "entries"' in record_requests[-1]['sql'].lower()
    assert 'limit ' in record_requests[-1]['sql'].lower()
    assert draft not in record_requests[-1]['sql']
    page.locator('#recordLinkBtn').click()
    page.wait_for_function("navigator.clipboard.readText().then(s => s.includes('sql='))")
    params = parse_qs(urlparse(page.evaluate('navigator.clipboard.readText()')).query)
    assert params['db'] == ['notes'] and params['env'] == ['dev']
    assert params['table'] == ['entries']
    assert not queries
    assert page.locator('#sql').input_value() == draft
    response = page.request.post(page.url.split('/quarry/')[0] + '/api/query', data={
        'db': params['db'][0], 'env': params['env'][0], 'sql': params['sql'][0],
    })
    assert response.ok
    assert response.json()['rows'] == [{'id': 2, 'note': 'dev note 2'}]


def test_sqlite_pragma_load_more_does_not_repeat_first_page(page_sqlite):
    from playwright.sync_api import expect

    page = page_sqlite
    _select_sqlite(page)
    page.select_option('#maxRows', '100')
    _run_sql(page, 'PRAGMA table_info(wide_columns)')
    expect(page.locator('#grid tbody tr')).to_have_count(100)
    assert _col_values(page) == [str(i) for i in range(100)]
    page.locator('#loadMoreBtn').click()
    expect(page.locator('#grid tbody tr')).to_have_count(125)
    assert _col_values(page) == [str(i) for i in range(125)]
    assert page.locator('#loadMoreBtn').count() == 0


def test_sqlite_workspaces_resolve_same_relative_path_and_restore_own_results(page_sqlite_workspaces, tmp_path):
    from playwright.sync_api import expect

    page = page_sqlite_workspaces
    expect(page.locator('#ws')).to_contain_text('2 workspaces')
    requests = []
    page.on('request', lambda req: requests.append(req.post_data_json)
            if req.method == 'POST' and req.url.endswith('/api/query') else None)
    for owner in ('brain', 'yiminlab'):
        page.locator(f'.dbrow[data-db="{owner}_notes"]').click()
        page.locator('#tbl-panel [data-t="records"]').wait_for()
        assert page.locator('#sql').input_value() == ''
        assert page.locator('#grid tbody tr').count() == 0
        page.locator('#tbl-panel [data-t="records"]').click()
        page.locator(f'#grid td[data-v="{owner} own data"]').wait_for()
        expect(page.locator('#grid tbody tr')).to_have_count(1)
        page.locator('#ciBtn').click()
        page.locator('#cihealth.ok').wait_for()
        details = page.locator('#cibody').inner_text()
        assert str((tmp_path / owner / 'data' / 'local.db').resolve()) in details
        assert str((tmp_path / owner / 'connections.toml').resolve()) in details
        assert 'sqlite:///data/local.db' in details
        page.keyboard.press('Escape')
        if owner == 'brain':
            _set_sql(page, "select 'brain draft' as scratch")
    assert [(r['db'], r['env']) for r in requests] == [('brain_notes', 'local'), ('yiminlab_notes', 'local')]
    requests.clear()
    page.locator('.dbrow[data-db="brain_notes"]').click()
    page.locator('#grid td[data-v="brain own data"]').wait_for()
    assert page.locator('#sql').input_value() == "select 'brain draft' as scratch"
    with page.expect_download() as download:
        page.locator('#jsonBtn').click()
    assert download.value.suggested_filename == 'quarry-brain_notes.json'
    assert json.loads(download.value.path().read_text()) == [dict(id=1, owner='brain', note='brain own data')]
    page.reload(wait_until='networkidle')
    page.locator('#grid td[data-v="brain own data"]').wait_for()
    assert page.locator('#sql').input_value() == "select 'brain draft' as scratch"
    page.locator('.dbrow[data-db="yiminlab_notes"]').click()
    page.locator('#grid td[data-v="yiminlab own data"]').wait_for()
    assert page.locator('#sql').input_value() == 'select * from "records"'
    assert page.locator('#grid td[data-v="brain own data"]').count() == 0
    with page.expect_download() as download:
        page.locator('#csvBtn').click()
    assert download.value.suggested_filename == 'quarry-yiminlab_notes.csv'
    csv = download.value.path().read_text(encoding='utf-8-sig')
    assert 'yiminlab own data' in csv and 'brain' not in csv
    assert not requests  # Switching and reloading restored results, never executed drafts.
    tabs = page.evaluate('JSON.parse(localStorage.qy_tabs)')
    for owner in ('brain', 'yiminlab'):
        own_tabs = [tab for tab in tabs if tab['db'] == f'{owner}_notes']
        assert own_tabs and all(tab['workspace'] == str((tmp_path / owner).resolve()) for tab in own_tabs)


def test_sqlite_saved_query_parameters_do_not_persist_in_browser_storage(page_sqlite, tmp_path):
    import sqlite3
    from contextlib import closing

    from playwright.sync_api import expect

    page = page_sqlite
    private_token = 'sqlite-private-token-f9d0827c'
    # The parameter is only a predicate: the selected id proves substitution
    # worked without legitimately returning the private value in the result.
    with closing(sqlite3.connect(tmp_path / 'notes-dev.db')) as db, db:
        db.execute('CREATE TABLE private_tokens (id INTEGER PRIMARY KEY, token TEXT NOT NULL)')
        db.executemany('INSERT INTO private_tokens VALUES (?, ?)', [(1, 'other'), (42, private_token)])
    template = "SELECT id FROM private_tokens WHERE token = :'token'"
    (tmp_path / 'queries' / 'by-private-token.sql').write_text(
        '-- @name: by-private-token\n-- @db: notes_dev\n-- @param: token (text, required)\n'
        + template + '\n', encoding='utf-8')
    page.reload(wait_until='networkidle')
    saved_query = page.locator('.qname[data-q="by-private-token"]').first
    saved_query.click()
    page.locator('.modal input.pf[data-p="token"]').fill(private_token)
    page.keyboard.press('Enter')
    expect(page.locator('#grid tbody tr')).to_have_count(1)
    expect(page.locator('#grid td[data-v="42"]')).to_be_visible()
    assert page.locator('#sql').input_value() == template

    def assert_private_value_not_persisted():
        stored = page.evaluate("""Object.fromEntries(
            ['qy_tabres', 'qy_tabs', 'qy_hist'].map(key => [key, localStorage.getItem(key)]))""")
        for key, value in stored.items():
            assert private_token not in (value or ''), f'parameter value leaked into {key}'
        return stored

    stored = assert_private_value_not_persisted()
    result = next(item['res'] for item in json.loads(stored['qy_tabres']) if item)
    assert result['rows'] == [{'id': 42}]
    assert ":'token'" in result['sql']
    page.locator('#tabAdd').click()
    _run_sql(page, 'select 99 as other_tab')
    saved_query.click()
    expect(page.locator('#grid td[data-v="42"]')).to_be_visible()
    page.reload(wait_until='networkidle')
    expect(page.locator('#grid td[data-v="42"]')).to_be_visible()
    assert page.locator('#sql').input_value() == template
    assert_private_value_not_persisted()
    page.locator('.tab.on .x').click()  # Closing stashes the saved template in History.
    stored = assert_private_value_not_persisted()
    assert any(item['sql'] == template for item in json.loads(stored['qy_hist']))


# Workspace identity stays internal; URLs use the logical database and environment.
def test_quarry_path_tracks_connections_and_copied_links(page_sqlite_workspaces):
    page = page_sqlite_workspaces
    for name in ("brain", "yiminlab"):
        page.locator(f'.dbrow[data-db="{name}_notes"]').click()
        page.wait_for_function("name => location.pathname === '/quarry/' && new URL(location.href).searchParams.get('db') === name + '_notes'", arg=name)
        page.locator('#tbl-panel .tname[data-t="records"]').click()
        page.wait_for_selector("#grid table tbody tr")
    page.locator('.dbrow[data-db="brain_notes"]').click()
    page.wait_for_function("location.pathname === '/quarry/' && new URL(location.href).searchParams.get('db') === 'brain_notes'")
    assert "brain" in page.locator("#grid").inner_text()
    # The share action identifies the active database through query parameters.
    page.context.grant_permissions(["clipboard-read", "clipboard-write"])
    page.locator('#linkBtn').click()
    link = page.evaluate("navigator.clipboard.readText()")
    assert urlparse(link).path == "/quarry/"
    assert parse_qs(urlparse(link).query)["db"] == ["brain_notes"]
    page.reload(wait_until="networkidle")
    page.wait_for_selector('.dbrow.on[data-db="brain_notes"]')
    assert urlparse(page.url).path == "/quarry/"
    page.goto(link, wait_until="networkidle")
    page.wait_for_selector('#grid td[data-v="brain own data"]')
    assert urlparse(page.url).path == "/quarry/"


def test_quarry_focus_url_restores_table_without_running(page_sqlite_workspaces):
    page = page_sqlite_workspaces
    base = page.url.split("/quarry/")[0]
    queries = []
    page.on("request", lambda r: r.method == "POST" and "/api/query" in r.url and queries.append(r.url))
    page.goto(base + "/quarry/?db=yiminlab_notes&env=local&table=records", wait_until="networkidle")
    page.wait_for_function("document.querySelector('#sql').value === 'select * from \"records\"'")
    assert urlparse(page.url).path == "/quarry/"
    assert queries == []


# SQL-assistant browser coverage uses the real local conversation store with a
# deterministic model boundary. It never calls a configured provider or needs
# PostgreSQL. API/schema/provider policies are covered at their own layer.
class _AssistantBrowserStub:
    def __init__(self, page, tmp_path, monkeypatch, *, configured=True):
        from quarry import assistant_config, assistant_store
        monkeypatch.setattr(assistant_config, "system_reply_language", lambda: "en")
        self.store = assistant_store
        monkeypatch.setenv('QUARRY_AI_HOME', str(tmp_path / 'assistant-browser'))
        self.config_path = tmp_path / 'assistant-config.toml'
        monkeypatch.setenv('QUARRY_CONFIG', str(self.config_path))
        self.page = page
        self.configured = configured
        self.requests = []
        self.preferences_requests = []
        self.chats = []
        self.pending = []
        self.hold = False
        self.proposal = None
        self.response = 'This query selects the requested columns from entries.'
        self.error = None
        self.close_without_done = False
        page.route('**/api/assistant/**', self._route)

    def _route(self, route):
        parsed = urlparse(route.request.url)
        path = parsed.path.removeprefix('/api/assistant/')
        data = route.request.post_data_json if route.request.method == 'POST' else {}
        if path == 'preferences':
            self.preferences_requests.append((route.request.method, data))
            return route.continue_()
        self.requests.append((path, data))
        if path == 'config':
            return route.fulfill(json={'configured': self.configured, 'model': 'fixture-model'})
        if path == 'conversations':
            if route.request.method == 'POST':
                doc = self.store.create(db=data['db'], env=data.get('env'),
                                        workspace=data.get('workspace'), sql=data.get('sql', ''),
                                        fingerprint='fixture-connection')
            elif parse_qs(parsed.query).get('id'):
                doc = self.store.get(parse_qs(parsed.query)['id'][0])
            else:
                doc = {'conversations': self.store.list_conversations()}
            return route.fulfill(json=doc)
        if path == 'conversations/update':
            return route.fulfill(json=self.store.update(data['id'], data['revision'], data['sql']))
        if path == 'conversations/apply':
            return route.fulfill(json=self.store.apply(
                data['id'], data['revision'], data['sql'], data['messageId'],
                explicit=data.get('explicit', False)))
        if path == 'conversations/restore':
            return route.fulfill(json=self.store.restore(data['id'], data['revision'], data['checkpointId']))
        if path == 'cancel':
            return route.fulfill(json={'cancelled': True})
        if path == 'chat':
            self.chats.append(data)
            if self.hold:
                self.pending.append(route)
                return
            return self.complete(route)
        raise AssertionError(f'Unexpected assistant API: {route.request.method} {path}')

    def events(self, data):
        ident = {'requestId': data['requestId'], 'conversationId': data['conversationId']}
        self.store.append_message(data['conversationId'], 'user', data['prompt'])
        events = [dict(ident, type='request'), dict(ident, type='text_delta', delta=self.response)]
        if self.error:
            return events + [dict(ident, type='error', error=self.error)]
        if self.close_without_done:
            return events
        extra = {'baseRevision': data['revision'], 'status': 'complete'}
        if self.proposal and not data.get('explain'):
            extra['sql'] = self.proposal
        doc = self.store.append_message(data['conversationId'], 'assistant', self.response, **extra)
        message = doc['messages'][-1]
        if 'sql' in message:
            events.append(dict(ident, type='sql_proposal', sql=message['sql'], messageId=message['id']))
        events.append(dict(ident, type='done', conversation=doc))
        return events

    def complete(self, route=None):
        route = route or self.pending.pop(0)
        events = self.events(route.request.post_data_json)
        route.fulfill(status=200, content_type='application/x-ndjson',
                      body=''.join(json.dumps(event) + '\n' for event in events))


def _assistant_ready(page, tmp_path, monkeypatch, *, sql='SELECT id, note FROM entries LIMIT 10', configured=True):
    stub = _AssistantBrowserStub(page, tmp_path, monkeypatch, configured=configured)
    page.reload(wait_until='networkidle')
    _select_sqlite(page)
    _set_sql(page, sql)
    return stub


def _assistant_send(page, prompt):
    field = page.get_by_test_id('assistant-input')
    field.fill(prompt)
    field.press('Enter')


def _assistant_explain(page):
    field = page.get_by_test_id('assistant-input')
    field.focus()
    field.press('Tab')
    field.press('Enter')


def _assistant_query_requests(page):
    requests = []
    page.on('request', lambda req: requests.append(req.post_data_json)
            if req.method == 'POST' and urlparse(req.url).path in ('/api/query', '/api/run') else None)
    return requests


def _assistant_scrollbar_styles(page):
    return page.get_by_test_id('assistant-messages').evaluate('''el => {
      // Chromium's generic pseudo-element computed style omits :vertical.
      // Inspect that exact loaded rule while measuring the remaining chrome.
      let verticalTrackTop;
      const visit = rules => { for (const rule of rules) {
        if (rule.selectorText?.split(',').map(s => s.trim()).includes('.assistant-messages::-webkit-scrollbar-track:vertical'))
          verticalTrackTop = rule.style.marginTop;
        if (rule.cssRules) visit(rule.cssRules);
      }};
      for (const sheet of document.styleSheets) { try { visit(sheet.cssRules); } catch { /* Cross-origin styles. */ } }
      const thumb = getComputedStyle(el, '::-webkit-scrollbar-thumb');
      return {
        width: getComputedStyle(el, '::-webkit-scrollbar').width,
        trackTop: verticalTrackTop,
        thumb: thumb.backgroundColor,
        thumbBorderLeft: thumb.borderLeftWidth,
        thumbBorderRight: thumb.borderRightWidth,
        thumbBackgroundClip: thumb.backgroundClip,
        buttons: getComputedStyle(el, '::-webkit-scrollbar-button').display,
        standardWidth: getComputedStyle(el).scrollbarWidth,
        standardColor: getComputedStyle(el).scrollbarColor,
      };
    }''')


def _assistant_controlled_stream(page):
    """Deliver real incremental NDJSON chunks without calling a model."""
    page.evaluate('''() => {
      const original = window.fetch.bind(window);
      window.fetch = async (url, init) => {
        if (String(url).endsWith('/api/assistant/chat')) {
          window.__assistantRequest = JSON.parse(init.body);
          const stream = new ReadableStream({start(controller) { window.__assistantController = controller; }});
          return new Response(stream, {headers: {'Content-Type': 'application/x-ndjson'}});
        }
        return original(url, init);
      };
      window.__assistantChunk = events => window.__assistantController.enqueue(
        new TextEncoder().encode(events.map(event => JSON.stringify(event)).join('\\n') + '\\n'));
    }''')


_ASSISTANT_MARKDOWN = '''## **Query plan**

Read **only the requested rows** from `entries`.

- Select the identifier.
- Keep the note.

1. Filter recent records.
2. Sort by identifier.

> A code example is an explanation.

```sql
SELECT count(*), 3 FROM entries
WHERE note = 'sample' AND ready = true; -- Example
```

| Column | Meaning |
| --- | --- |
| id | Identifier |
| note | Entry text |

[SQL reference](https://www.sqlite.org/lang_select.html)
'''


def test_assistant_markdown_formats_reply_and_reopened_history_without_sql_changes(page_sqlite, tmp_path, monkeypatch):
    page = page_sqlite
    failed_requests = []
    page.on('requestfailed', lambda req: failed_requests.append((req.url, req.failure)))
    page.on('response', lambda response: failed_requests.append((response.url, response.status))
            if response.status >= 400 else None)
    stub = _assistant_ready(page, tmp_path, monkeypatch, sql='')
    stub.response = _ASSISTANT_MARKDOWN
    page.get_by_test_id('assistant-open').click()
    original = 'SELECT note FROM entries WHERE id = 17'
    _set_sql(page, original)
    queries = _assistant_query_requests(page)
    prompt = '**Keep this literal**\n\n`SELECT 1`\n\n# Not a heading'
    _assistant_send(page, prompt)
    markdown = page.locator('.assistant-message-assistant .assistant-markdown')

    def assert_reply():
        markdown.locator('h2').wait_for()
        assert markdown.locator('h2').inner_text() == 'Query plan'
        assert markdown.locator('p strong').inner_text() == 'only the requested rows'
        assert markdown.locator('p > code').inner_text() == 'entries'
        assert markdown.locator('ul > li').count() == 2
        assert markdown.locator('ol > li').count() == 2
        assert markdown.locator('blockquote').inner_text().strip() == 'A code example is an explanation.'
        code = markdown.locator('pre > code.language-sql')
        assert code.inner_text().strip() == "SELECT count(*), 3 FROM entries\nWHERE note = 'sample' AND ready = true; -- Example"
        assert code.locator('.hljs-keyword').count() >= 3
        assert code.locator('.hljs-string').inner_text() == "'sample'"
        assert code.locator('.hljs-number').inner_text() == '3'
        assert code.locator('.hljs-comment').inner_text() == '-- Example'
        assert markdown.locator('.assistant-table-scroll table th').all_text_contents() == ['Column', 'Meaning']
        assert markdown.locator('table tbody tr').count() == 2
        user = page.locator('.assistant-message-user')
        assert user.inner_text() == prompt
        assert user.locator('strong, code, h1, .assistant-markdown').count() == 0
        assert page.locator('#sql').input_value() == original
        assert page.get_by_test_id('assistant-apply').count() == 0
        assert page.get_by_test_id('assistant-restore').count() == 0
        assert queries == []

    assert_reply()
    conversation_id = stub.chats[0]['conversationId']
    system_text = '**System message stays literal** <b>unchanged</b>'
    stub.store.append_message(conversation_id, 'system', system_text)
    page.locator('.tab.on .x').click()
    page.locator('#queryEmptyState').wait_for()
    page.reload(wait_until='networkidle')
    try:
        page.locator('#histBtn').wait_for(timeout=5000)
    except Exception:
        page.screenshot(path=str(tmp_path / 'assistant-history-reload-failure.png'))
        print('History reload diagnostics:', json.dumps({
            'url': page.url, 'body': page.locator('body').inner_text(),
            'console': page._console_errors, 'requests': failed_requests,
        }))
        raise
    page.locator('#histBtn').click()
    page.get_by_test_id('history-conversations').click()
    page.get_by_test_id('conversation-item').first.click()
    assert_reply()
    system = page.locator('.assistant-message-system')
    assert system.inner_text() == system_text
    assert system.locator('strong, b, .assistant-markdown').count() == 0
    assert len(stub.chats) == 1
    page.screenshot(path=str(tmp_path / 'assistant-markdown-history.png'))


def test_assistant_markdown_blocks_html_unsafe_urls_and_remote_images(page_sqlite, tmp_path, monkeypatch):
    page = page_sqlite
    stub = _assistant_ready(page, tmp_path, monkeypatch, sql='')
    image_requests = []
    page.route('https://markdown-images.invalid/**', lambda route: (image_requests.append(route.request.url), route.abort()))
    page.evaluate('window.__markdownExecuted = false')
    stub.response = '''Safe **explanation**.

<script>window.__markdownExecuted = true</script>

<img src="https://markdown-images.invalid/raw.png" onerror="window.__markdownExecuted=true">

<iframe src="https://markdown-images.invalid/frame"></iframe>

[Unsafe script](javascript:alert%281%29)
[Unsafe data](data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==)
[Local route](/api/query)
[Protocol relative](//markdown-images.invalid/link)

![Diagram description](https://markdown-images.invalid/model.png)

[SQL documentation](https://www.sqlite.org/lang_select.html)
[Email](mailto:reader@example.com)
'''
    page.get_by_test_id('assistant-open').click()
    _assistant_send(page, 'Explain this safely')
    markdown = page.locator('.assistant-message-assistant .assistant-markdown')
    markdown.get_by_text('Diagram description', exact=True).wait_for()
    assert markdown.locator('script, img, iframe').count() == 0
    assert page.evaluate('window.__markdownExecuted') is False
    assert image_requests == []
    assert markdown.locator('.assistant-image-alt').inner_text() == 'Diagram description'
    assert markdown.locator('a').count() == 2
    assert markdown.locator('a').evaluate_all("links => links.every(a => ['https:', 'mailto:'].includes(new URL(a.href).protocol) && a.target === '_blank' && a.relList.contains('noopener') && a.relList.contains('noreferrer'))")
    for label in ('Unsafe script', 'Unsafe data', 'Local route', 'Protocol relative'):
        assert markdown.get_by_text(label, exact=True).evaluate('el => el.tagName') == 'SPAN'
    assert page.locator('#sql').input_value() == ''


def test_assistant_markdown_stream_closes_fence_and_keeps_composer_fixed(page_sqlite, tmp_path, monkeypatch):
    page = page_sqlite
    stub = _assistant_ready(page, tmp_path, monkeypatch, sql='')
    page.get_by_test_id('assistant-open').click()
    queries = _assistant_query_requests(page)
    _assistant_controlled_stream(page)
    _assistant_send(page, 'Explain a long SQL example')
    page.wait_for_function('Boolean(window.__assistantRequest && window.__assistantController)')
    request = page.evaluate('window.__assistantRequest')
    ident = {key: request[key] for key in ('requestId', 'conversationId')}
    composer_before = page.get_by_test_id('assistant-input').bounding_box()
    first = '## Explanation\n\n**Read each step.**\n\n```sql\n' + '\n'.join(f'-- Step {i}: inspect columns' for i in range(35)) + '\nSELECT id'
    page.evaluate('events => window.__assistantChunk(events)', [dict(ident, type='request'), dict(ident, type='text_delta', delta=first)])
    stream = page.get_by_test_id('assistant-stream')
    stream.locator('pre code.language-sql').wait_for()
    assert stream.locator('h2').inner_text() == 'Explanation'
    assert stream.locator('pre code').inner_text().endswith('SELECT id\n')
    page.wait_for_function('''() => { const el = document.querySelector('[data-testid="assistant-messages"]');
      return el.scrollHeight > el.clientHeight && el.scrollHeight - el.scrollTop - el.clientHeight < 10; }''')
    tail = ' FROM entries;\n```\n\n> Read complete.\n\n- Keep the current editor SQL.\n'
    page.evaluate('events => window.__assistantChunk(events)', [dict(ident, type='text_delta', delta=tail)])
    stream.locator('blockquote').wait_for()
    assert stream.locator('pre code').inner_text().endswith('SELECT id FROM entries;\n')
    stub.response = first + tail
    done = stub.events(request)[-1]
    page.evaluate('event => { window.__assistantChunk([event]); window.__assistantController.close(); }', done)
    stream.wait_for(state='detached')
    markdown = page.locator('.assistant-message-assistant .assistant-markdown')
    assert markdown.locator('blockquote').inner_text().strip() == 'Read complete.'
    assert markdown.locator('ul > li').count() == 1
    assert abs(page.get_by_test_id('assistant-input').bounding_box()['y'] - composer_before['y']) <= 2
    assert page.get_by_test_id('assistant-messages').evaluate('el => el.scrollHeight - el.scrollTop - el.clientHeight') < 10
    assert page.evaluate('window.scrollY') == 0
    assert page.locator('#sql').input_value() == ''
    assert queries == []


def test_assistant_markdown_wide_code_and_tables_scroll_inside_narrow_panel(page_sqlite, tmp_path, monkeypatch):
    page = page_sqlite
    page.set_viewport_size({'width': 1000, 'height': 800})
    stub = _assistant_ready(page, tmp_path, monkeypatch, sql='')
    stub.response = '## Wide examples\n\n```sql\nSELECT ' + ', '.join(f'column_{i}' for i in range(30)) + ' FROM entries;\n```\n\n'
    stub.response += '| Identifier | Source column with a long name | Destination column with a long name | Description |\n| --- | --- | --- | --- |\n| 1 | account_identifier | customer_identifier | Explain the relationship |\n'
    page.get_by_test_id('assistant-open').click()
    _assistant_send(page, 'Explain the columns')
    page.locator('.assistant-markdown table').wait_for()
    panel = page.get_by_test_id('assistant-panel')
    assert panel.bounding_box()['width'] < 340
    for selector in ('.assistant-markdown pre', '.assistant-table-scroll'):
        dimensions = page.locator(selector).evaluate('el => ({width:el.clientWidth, content:el.scrollWidth, overflow:getComputedStyle(el).overflowX})')
        assert dimensions['content'] > dimensions['width']
        assert dimensions['overflow'] == 'auto'
        assert page.locator(selector).evaluate('el => { el.scrollLeft = 80; return el.scrollLeft; }') > 0
    for selector in ('[data-testid="assistant-panel"]', '[data-testid="assistant-messages"]', '.assistant-markdown'):
        assert page.locator(selector).evaluate('el => el.scrollWidth <= el.clientWidth + 1'), selector
    assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
    page.screenshot(path=str(tmp_path / 'assistant-markdown-narrow.png'))


@pytest.mark.parametrize('viewport_width', [1280, 1000])
def test_assistant_layout_collapses_inside_editor_and_reopens(page_sqlite, tmp_path, monkeypatch, viewport_width):
    page = page_sqlite
    page.set_viewport_size({'width': viewport_width, 'height': 900})
    _assistant_ready(page, tmp_path, monkeypatch, configured=False)
    rail = page.get_by_test_id('assistant-open')
    editor = page.get_by_test_id('editor-workspace')
    rail_box, editor_box, grid_box = rail.bounding_box(), editor.bounding_box(), page.locator('#grid').bounding_box()
    assert rail.inner_text() == 'SQL Assistant'
    assert rail.locator('i').count() == 0
    assert rail.get_attribute('aria-label') == 'Open SQL assistant'
    assert abs(rail_box['y'] - editor_box['y']) <= 2
    assert rail_box['y'] + rail_box['height'] <= editor_box['y'] + editor_box['height'] + 2
    assert rail_box['width'] == 32
    label_box = rail.locator('span').bounding_box()
    assert label_box['y'] >= rail_box['y']
    assert label_box['y'] + label_box['height'] <= rail_box['y'] + rail_box['height']
    assert abs(rail_box['x'] - editor_box['x']) <= 1
    assert rail_box['x'] + rail_box['width'] <= page.locator('.editor-input').bounding_box()['x']
    rail.click()
    panel = page.get_by_test_id('assistant-panel')
    panel.wait_for()
    expanded = editor.bounding_box()
    assert expanded['height'] > editor_box['height']
    assert page.locator('#grid').bounding_box()['height'] < grid_box['height']
    assert not rail.is_visible()
    assert panel.get_by_text('SQL Assistant', exact=True).count() == 0
    assert panel.get_by_text('Current SQL', exact=True).count() == 0
    assert panel.get_by_role('button', name=re.compile(r'Send|Maximize|Collapse', re.I)).count() == 0
    assert panel.get_by_test_id('assistant-input').is_visible()
    toolbar_box = page.locator('.toolbar').bounding_box()
    panel_box = panel.bounding_box()
    composer_box = page.get_by_test_id('assistant-input').bounding_box()
    panel_bottom = panel_box['y'] + panel_box['height']
    assert abs(panel_bottom - toolbar_box['y'] - toolbar_box['height']) <= 2
    assert abs(panel_box['x'] - expanded['x']) <= 1
    assert panel_box['x'] + panel_box['width'] <= toolbar_box['x']
    close = page.get_by_test_id('assistant-close')
    close_box = close.bounding_box()
    icon_box = close.locator('.ti-chevrons-left').bounding_box()
    assert abs(close_box['y'] - panel_box['y'] - 8) <= 1
    assert close_box['height'] == 26 and close_box['width'] == 22
    assert icon_box['width'] <= 16 and icon_box['height'] <= 16
    assert close.locator('i').evaluate('el => getComputedStyle(el).borderTopWidth') == '0px'
    divider_box = page.get_by_test_id('assistant-divider').bounding_box()
    assert divider_box['width'] == page.locator('#edresizer').bounding_box()['height'] == 6
    assert abs(close_box['x'] + close_box['width'] / 2 - divider_box['x'] - divider_box['width'] / 2) <= 1
    assert abs(icon_box['y'] + icon_box['height'] / 2 - close_box['y'] - close_box['height'] / 2) <= 1
    messages = page.get_by_test_id('assistant-messages')
    assert abs(messages.bounding_box()['y'] - panel_box['y']) <= 1
    assert abs(messages.bounding_box()['x'] + messages.bounding_box()['width'] - divider_box['x']) <= 1
    assert messages.evaluate('el => getComputedStyle(el).marginRight') == '0px'
    assert messages.evaluate('el => getComputedStyle(el).paddingTop') == '14px'
    content_box = page.locator('.assistant-empty').bounding_box()
    assert content_box['x'] + content_box['width'] <= close_box['x']
    sql_text_left = page.locator('#sql').evaluate('el => el.getBoundingClientRect().left + parseFloat(getComputedStyle(el).paddingLeft)')
    assert close_box['x'] + close_box['width'] <= sql_text_left
    assert composer_box['x'] + composer_box['width'] <= panel_box['x'] + panel_box['width']
    assert 0 <= panel_bottom - composer_box['y'] - composer_box['height'] <= 20
    for selector in ('#runBtn', '#fmtBtn', '#expBtn', '#csvBtn', '#jsonBtn', '#maxRows', '#histBtn', '#linkBtn'):
        box = page.locator(selector).bounding_box()
        assert box['x'] >= toolbar_box['x']
        assert box['x'] + box['width'] <= toolbar_box['x'] + toolbar_box['width'] + 1
        assert box['y'] + box['height'] <= panel_bottom
    assert page.locator('#grid').bounding_box()['y'] >= panel_bottom
    assert abs(page.locator('#grid').bounding_box()['width'] - expanded['width']) <= 2
    page.screenshot(path=str(tmp_path / 'assistant-expanded.png'))
    page.get_by_test_id('assistant-close').click()
    assert rail.is_visible()
    assert editor.bounding_box()['height'] <= 90
    toolbar_box = page.locator('.toolbar').bounding_box()
    assert abs(toolbar_box['x'] - editor.bounding_box()['x']) <= 2
    assert abs(toolbar_box['width'] - editor.bounding_box()['width']) <= 2
    assert page.locator('#grid').bounding_box()['height'] >= grid_box['height'] - 2
    page.screenshot(path=str(tmp_path / 'assistant-collapsed.png'))
    print('Assistant screenshots:', tmp_path)
    rail.click()
    assert abs(editor.bounding_box()['height'] - expanded['height']) <= 2


def test_assistant_unconfigured_preserves_input_and_only_shows_cli_hint(page_sqlite, tmp_path, monkeypatch):
    page = page_sqlite
    stub = _assistant_ready(page, tmp_path, monkeypatch, configured=False)
    page.get_by_test_id('assistant-open').click()
    assert stub.chats == []
    assert 'Model not configured' not in page.get_by_test_id('assistant-panel').inner_text()
    _assistant_send(page, 'Explain the join')
    page.get_by_text(re.compile('Model not configured')).wait_for()
    assert 'qy ai configure' in page.get_by_test_id('assistant-panel').inner_text()
    assert page.get_by_test_id('assistant-input').input_value() == 'Explain the join'
    assert page.get_by_role('button', name='Configure', exact=True).count() == 0
    assert stub.chats == []


def test_assistant_suggested_explanation_requires_tab_then_enter(page_sqlite, tmp_path, monkeypatch):
    page = page_sqlite
    stub = _assistant_ready(page, tmp_path, monkeypatch)
    queries = _assistant_query_requests(page)
    before = page.locator('#sql').input_value()
    page.get_by_test_id('assistant-open').click()
    field = page.get_by_test_id('assistant-input')
    ghost = page.get_by_test_id('assistant-ghost')
    assert field.input_value() == ''
    assert field.get_attribute('placeholder') == ''
    assert ghost.is_visible()
    suggestion = ghost.inner_text()
    assert suggestion == 'Explain this SQL'
    assert ghost.evaluate('el => getComputedStyle(el).pointerEvents') == 'none'
    ghost_alignment = field.evaluate('''el => {
      const ghost = el.parentElement.querySelector('[data-testid="assistant-ghost"]');
      const inputRect = el.getBoundingClientRect(), ghostRect = ghost.getBoundingClientRect();
      const inputStyle = getComputedStyle(el), ghostStyle = getComputedStyle(ghost);
      return {
        textX: inputRect.x + parseFloat(inputStyle.borderLeftWidth) + parseFloat(inputStyle.paddingLeft),
        textY: inputRect.y + parseFloat(inputStyle.borderTopWidth) + parseFloat(inputStyle.paddingTop),
        ghostX: ghostRect.x, ghostY: ghostRect.y,
        matchingFont: inputStyle.fontSize === ghostStyle.fontSize && inputStyle.lineHeight === ghostStyle.lineHeight && inputStyle.fontFamily === ghostStyle.fontFamily
      };
    }''')
    assert abs(ghost_alignment['textX'] - ghost_alignment['ghostX']) <= 1
    assert abs(ghost_alignment['textY'] - ghost_alignment['ghostY']) <= 1
    assert ghost_alignment['matchingFont']
    assert stub.chats == []
    assert stub.requests == []
    field.hover()
    assert page.get_by_test_id('assistant-suggestion-hint').count() == 0
    assert page.get_by_test_id('assistant-panel').locator('kbd').count() == 0
    assert not re.search(r'\b(Tab|Enter)\b', page.get_by_test_id('assistant-panel').inner_text())
    field.focus()
    field.press('Enter')
    assert stub.chats == []
    field.press('Tab')
    assert field.input_value() == suggestion
    assert ghost.count() == 0
    assert field.evaluate('el => document.activeElement === el')
    assert stub.chats == []
    assert stub.requests == []
    field.press('Enter')
    page.get_by_test_id('assistant-messages').get_by_text(stub.response, exact=True).wait_for()
    assert len(stub.chats) == 1 and stub.chats[0]['explain'] is True
    assert stub.chats[0]['sql'] == before
    assert page.get_by_test_id('assistant-apply').count() == 0
    assert page.get_by_test_id('assistant-restore').count() == 0
    page.get_by_test_id('assistant-close').click()
    page.get_by_test_id('assistant-open').click()
    page.wait_for_timeout(200)
    assert len(stub.chats) == 1
    assert page.locator('#sql').input_value() == before
    assert queries == []


@pytest.mark.parametrize('ui_language', ['en', 'zh'])
def test_assistant_has_no_reply_language_settings_on_hover_or_focus(page_sqlite_default_language, tmp_path, monkeypatch, ui_language):
    page = page_sqlite_default_language
    page.evaluate("ui => { localStorage.setItem('qy_lang', ui); localStorage.setItem('qy_assistant_language', 'zh'); }", ui_language)
    stub = _assistant_ready(page, tmp_path, monkeypatch)
    queries = _assistant_query_requests(page)
    original = page.locator('#sql').input_value()
    with page.expect_response('**/api/assistant/preferences') as response:
        page.get_by_test_id('assistant-open').click()
    assert response.value.json()['replyLanguage'] == 'system'
    assert response.value.json()['resolvedReplyLanguage'] == 'en'
    field = page.get_by_test_id('assistant-input')
    before = [page.locator(selector).bounding_box() for selector in
              ('[data-testid="assistant-input"]', '[data-testid="assistant-messages"]', '.toolbar')]
    for action in (field.hover, field.focus):
        action()
        assert page.locator('[data-testid^="assistant-language"], .assistant-language-row').count() == 0
        assert page.get_by_test_id('assistant-ghost').inner_text() == 'Explain this SQL'
        after = [page.locator(selector).bounding_box() for selector in
                 ('[data-testid="assistant-input"]', '[data-testid="assistant-messages"]', '.toolbar')]
        assert before == after
    field.fill('Keep this draft')
    field.hover()
    field.focus()
    assert page.locator('[data-testid^="assistant-language"]').count() == 0
    assert page.locator('#sql').input_value() == original
    assert field.input_value() == 'Keep this draft'
    assert stub.requests == [] and queries == []
    assert all(method == 'GET' for method, _ in stub.preferences_requests)


def test_assistant_cli_reply_language_preserves_drafts_history_and_tab_state(page_sqlite, tmp_path, monkeypatch):
    from quarry.assistant_config import set_reply_language
    page = page_sqlite
    stub = _assistant_ready(page, tmp_path, monkeypatch)
    queries = _assistant_query_requests(page)
    original = page.locator('#sql').input_value()
    page.get_by_test_id('assistant-open').click()
    _assistant_send(page, 'Explain the join without changing the query')
    page.get_by_test_id('assistant-messages').get_by_text(stub.response, exact=True).wait_for()
    field = page.get_by_test_id('assistant-input')
    page.get_by_test_id('assistant-ghost').wait_for()
    history = page.get_by_test_id('assistant-messages').inner_text()
    field.focus()
    field.press('Tab')
    accepted = 'Explain this SQL'
    assert field.input_value() == accepted
    request_count = len(stub.requests)
    set_reply_language('zh')
    with page.expect_response('**/api/assistant/preferences'):
        page.evaluate("window.dispatchEvent(new Event('focus'))")
    assert field.input_value() == accepted
    assert page.get_by_test_id('assistant-messages').inner_text() == history
    assert len(stub.requests) == request_count
    assert page.locator('#sql').input_value() == original
    page.locator('#tabAdd').click()
    page.get_by_test_id('assistant-open').click()
    assert page.locator('#sql').input_value() == ''
    assert page.get_by_test_id('assistant-ghost').count() == 0
    field.fill('Keep this other tab draft')
    page.locator('#tabs .tab').first.click()
    assert field.input_value() == accepted
    assert page.get_by_test_id('assistant-messages').inner_text() == history
    field.press('Enter')
    page.wait_for_function("() => document.querySelectorAll('.assistant-message-assistant:not([data-testid=assistant-stream])').length === 2")
    assert len(stub.chats) == 2
    assert stub.chats[1]['prompt'] == accepted and stub.chats[1]['explain'] is True
    page.locator('#tabs .tab').nth(1).click()
    assert field.input_value() == 'Keep this other tab draft'
    assert page.locator('#sql').input_value() == ''
    assert page.locator('[data-testid^="assistant-language"]').count() == 0
    assert all(method == 'GET' for method, _ in stub.preferences_requests)
    assert queries == []


def test_assistant_reply_language_rereads_cli_and_system_changes(page_sqlite, tmp_path, monkeypatch):
    from quarry import assistant_config
    page = page_sqlite
    stub = _assistant_ready(page, tmp_path, monkeypatch)
    queries = _assistant_query_requests(page)
    assistant_config.set_reply_language('zh')
    page.get_by_test_id('assistant-open').click()
    ghost = page.get_by_test_id('assistant-ghost')
    page.wait_for_function("document.querySelector('[data-testid=assistant-ghost]')?.textContent === '解读当前 SQL'")
    for language, prompt in [('en', 'Explain this SQL'), ('zh', '解读当前 SQL'), ('system', 'Explain this SQL')]:
        assistant_config.set_reply_language(language)
        with page.expect_response('**/api/assistant/preferences'):
            if language == 'zh':
                page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
            else:
                page.evaluate("window.dispatchEvent(new Event('focus'))")
        page.wait_for_function("prompt => document.querySelector('[data-testid=assistant-ghost]')?.textContent === prompt", arg=prompt)
    # System resolution is owned by the backend, not the interface language.
    monkeypatch.setattr(assistant_config, 'system_reply_language', lambda: 'zh')
    page.get_by_test_id('assistant-close').click()
    with page.expect_response('**/api/assistant/preferences'):
        page.get_by_test_id('assistant-open').click()
    page.wait_for_function("document.querySelector('[data-testid=assistant-ghost]')?.textContent === '解读当前 SQL'")
    assistant_config.set_reply_language('en')
    page.locator('.vg-lang-switch').click()
    page.wait_for_function("document.querySelector('#runLbl')?.textContent === '运行'")
    with page.expect_response('**/api/assistant/preferences'):
        page.get_by_test_id('assistant-open').click()
    page.wait_for_function("document.querySelector('[data-testid=assistant-ghost]')?.textContent === 'Explain this SQL'")
    page.reload(wait_until='networkidle')
    with page.expect_response('**/api/assistant/preferences'):
        page.get_by_test_id('assistant-open').click()
    assert ghost.inner_text() == 'Explain this SQL'
    assert page.locator('[data-testid^="assistant-language"]').count() == 0
    assert stub.requests == [] and queries == []
    assert all(method == 'GET' for method, _ in stub.preferences_requests)


def test_assistant_explanation_suggestion_respects_drafts_empty_sql_and_composition(page_sqlite, tmp_path, monkeypatch):
    page = page_sqlite
    stub = _assistant_ready(page, tmp_path, monkeypatch, sql='')
    page.get_by_test_id('assistant-open').click()
    field = page.get_by_test_id('assistant-input')
    assert field.get_attribute('placeholder') == 'Ask about your SQL…'
    assert page.get_by_test_id('assistant-ghost').count() == 0
    field.focus()
    field.press('Tab')
    assert field.input_value() == ''
    _set_sql(page, 'SELECT id FROM entries LIMIT 10')
    assert field.get_attribute('placeholder') == ''
    assert page.get_by_test_id('assistant-ghost').inner_text() == 'Explain this SQL'
    field.focus()
    field.press('Shift+Tab')
    assert field.input_value() == ''
    field.focus()
    field.evaluate('''el => {
      el.dispatchEvent(new CompositionEvent('compositionstart', {bubbles: true}));
      el.dispatchEvent(new KeyboardEvent('keydown', {key:'Tab', code:'Tab', keyCode:229, isComposing:true, bubbles:true, cancelable:true}));
    }''')
    assert field.input_value() == ''
    field.evaluate("el => el.dispatchEvent(new CompositionEvent('compositionend', {bubbles:true}))")
    field.fill('Keep my draft')
    field.press('Tab')
    assert field.input_value() == 'Keep my draft'
    assert page.get_by_test_id('assistant-ghost').count() == 0
    assert stub.chats == []
    field.fill('')
    field.press('Tab')
    assert field.input_value() == 'Explain this SQL'
    field.fill('Actually, include the note column')
    field.press('Enter')
    page.get_by_test_id('assistant-messages').get_by_text(stub.response, exact=True).wait_for()
    assert len(stub.chats) == 1
    assert stub.chats[0].get('explain') is False
    assert stub.chats[0]['prompt'] == 'Actually, include the note column'


def test_assistant_unsent_draft_and_explanation_intent_survive_hide_and_tab_switch(page_sqlite, tmp_path, monkeypatch):
    page = page_sqlite
    stub = _assistant_ready(page, tmp_path, monkeypatch)
    queries = _assistant_query_requests(page)
    page.get_by_test_id('assistant-open').click()
    field = page.get_by_test_id('assistant-input')
    field.fill('Keep this unsent draft')
    page.get_by_test_id('assistant-close').click()
    page.get_by_test_id('assistant-open').click()
    assert field.input_value() == 'Keep this unsent draft'
    page.locator('#tabAdd').click()
    page.get_by_test_id('assistant-open').click()
    assert field.input_value() == ''
    field.fill('A different tab draft')
    page.locator('#tabs .tab').first.click()
    assert field.input_value() == 'Keep this unsent draft'
    field.fill('')
    field.press('Tab')
    assert field.input_value() == 'Explain this SQL'
    page.get_by_test_id('assistant-close').click()
    page.get_by_test_id('assistant-open').click()
    assert field.input_value() == 'Explain this SQL'
    page.locator('#tabs .tab').nth(1).click()
    assert field.input_value() == 'A different tab draft'
    page.locator('#tabs .tab').first.click()
    assert field.input_value() == 'Explain this SQL'
    assert stub.chats == []
    field.press('Enter')
    page.get_by_test_id('assistant-messages').get_by_text(stub.response, exact=True).wait_for()
    assert len(stub.chats) == 1 and stub.chats[0]['explain'] is True
    assert queries == []


@pytest.mark.parametrize('late_result', ['history', 'error'])
def test_assistant_late_history_load_cannot_replace_a_new_reply(page_sqlite, tmp_path, monkeypatch, late_result):
    page = page_sqlite
    stub = _assistant_ready(page, tmp_path, monkeypatch)
    page.get_by_test_id('assistant-open').click()
    _assistant_explain(page)
    page.get_by_test_id('assistant-messages').get_by_text(stub.response, exact=True).wait_for()
    stale = stub.store.get(stub.chats[0]['conversationId'])
    page.reload(wait_until='networkidle')
    pending = []

    def hold_open_history(route):
        if not pending:
            pending.append(route)
        else:
            stub._route(route)

    page.route('**/api/assistant/conversations?*', hold_open_history)
    page.get_by_test_id('assistant-open').click()
    page.wait_for_timeout(100)
    assert len(pending) == 1
    stub.response = 'The newer explanation must remain visible.'
    _assistant_send(page, 'Explain the selected columns in more detail')
    page.get_by_test_id('assistant-messages').get_by_text(stub.response, exact=True).wait_for()
    if late_result == 'history':
        pending[0].fulfill(json=stale)
    else:
        pending[0].fulfill(status=503, json={'error': 'Old history lookup failed'})
    page.wait_for_timeout(100)
    assert page.get_by_test_id('assistant-messages').get_by_text(stub.response, exact=True).is_visible()
    assert page.locator('.assistant-message').count() == 4
    assert page.get_by_role('alert').count() == 0
    assert len(stub.chats) == 2


def test_assistant_late_history_error_preserves_configuration_error_and_draft(page_sqlite, tmp_path, monkeypatch):
    page = page_sqlite
    stub = _assistant_ready(page, tmp_path, monkeypatch)
    page.get_by_test_id('assistant-open').click()
    _assistant_explain(page)
    page.get_by_test_id('assistant-messages').get_by_text(stub.response, exact=True).wait_for()
    page.reload(wait_until='networkidle')
    stub.configured = False
    pending = []
    page.route('**/api/assistant/conversations?*', lambda route: pending.append(route))
    page.get_by_test_id('assistant-open').click()
    page.wait_for_timeout(100)
    assert len(pending) == 1
    prompt = 'Explain the current SQL again'
    _assistant_send(page, prompt)
    error = page.get_by_role('alert').filter(has_text='qy ai configure')
    error.wait_for()
    before = error.inner_text()
    assert 'Model not configured' in before
    pending[0].fulfill(status=500, json={'error': 'Stale history request failed'})
    page.wait_for_timeout(100)
    assert error.inner_text() == before
    assert page.get_by_test_id('assistant-input').input_value() == prompt
    assert 'Stale history request failed' not in page.get_by_test_id('assistant-panel').inner_text()
    assert len(stub.chats) == 1


def test_assistant_enter_shift_enter_and_ime_composition(page_sqlite, tmp_path, monkeypatch):
    page = page_sqlite
    stub = _assistant_ready(page, tmp_path, monkeypatch, sql='')
    page.get_by_test_id('assistant-open').click()
    field = page.get_by_test_id('assistant-input')
    field.fill('Join entries')
    field.press('Shift+Enter')
    field.press('End')
    field.type('with their owners')
    assert field.input_value() == 'Join entries\nwith their owners'
    field.evaluate("el => el.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', code: 'Enter', keyCode: 229, isComposing: true, bubbles: true, cancelable: true}))")
    assert stub.chats == []
    field.press('Enter')
    page.get_by_test_id('assistant-messages').get_by_text(stub.response, exact=True).wait_for()
    assert len(stub.chats) == 1
    assert stub.chats[0]['prompt'] == 'Join entries\nwith their owners'
    assert field.input_value() == ''


def test_assistant_apply_restore_preserves_drafts_and_only_run_executes(page_sqlite, tmp_path, monkeypatch):
    page = page_sqlite
    stub = _assistant_ready(page, tmp_path, monkeypatch, sql='SELECT id FROM entries LIMIT 10')
    queries = _assistant_query_requests(page)
    page.get_by_test_id('assistant-open').click()
    _assistant_explain(page)
    page.get_by_test_id('assistant-messages').get_by_text(stub.response, exact=True).wait_for()
    original = page.locator('#sql').input_value()
    stub.proposal = 'SELECT id, note FROM entries LIMIT 3'
    _assistant_send(page, 'Include note and show only three rows')
    page.wait_for_function('sql => document.querySelector("#sql").value === sql', arg=stub.proposal)
    assert queries == []
    assert page.get_by_test_id('assistant-restore').count() == 1
    assert page.get_by_test_id('assistant-restore').inner_text().strip() == 'Restore previous SQL'
    assert original in page.evaluate("JSON.parse(localStorage.qy_hist).map(h => h.sql)")
    page.get_by_test_id('assistant-restore').click()
    page.wait_for_function('sql => document.querySelector("#sql").value === sql', arg=original)
    assert stub.proposal in page.evaluate("JSON.parse(localStorage.qy_hist).map(h => h.sql)")
    assert 'Include note and show only three rows' in page.get_by_test_id('assistant-messages').inner_text()
    assert queries == []
    page.locator('#runBtn').click()
    page.wait_for_selector('#grid tbody tr')
    assert len(queries) == 1


def test_assistant_restore_repeated_clicks_do_not_create_an_undo_chain(page_sqlite, tmp_path, monkeypatch):
    page = page_sqlite
    original = 'SELECT id FROM entries LIMIT 10'
    stub = _assistant_ready(page, tmp_path, monkeypatch, sql=original)
    page.get_by_test_id('assistant-open').click()
    _assistant_explain(page)
    page.get_by_test_id('assistant-messages').get_by_text(stub.response, exact=True).wait_for()
    stub.proposal = 'SELECT id, note FROM entries LIMIT 3'
    _assistant_send(page, 'Include note and show three rows')
    page.get_by_test_id('assistant-restore').wait_for()
    conversation_id = stub.chats[-1]['conversationId']
    doc = stub.store.get(conversation_id)
    # Pre-fix histories may contain an actionable-looking restore message.
    stub.store.append_message(conversation_id, 'system', 'Previous SQL restored.',
                              checkpointId=doc['checkpoints'][0]['id'])
    page.reload(wait_until='networkidle')
    page.get_by_test_id('assistant-open').click()
    restore = page.get_by_test_id('assistant-restore')
    restore.wait_for()
    assert restore.count() == 1
    assert page.locator('.assistant-message-system').count() == 0
    visible_messages = page.locator('.assistant-message').count()
    queries = _assistant_query_requests(page)
    before = stub.store.get(conversation_id)
    pending = []
    page.route('**/api/assistant/conversations/restore', lambda route: pending.append(route))
    restore.evaluate('el => { el.click(); el.click(); el.click(); }')
    page.wait_for_timeout(100)
    assert len(pending) == 1
    stub._route(pending.pop())
    page.wait_for_function('sql => document.querySelector("#sql").value === sql', arg=original)
    page.get_by_test_id('assistant-restored').wait_for()
    assert restore.count() == 0
    after = stub.store.get(conversation_id)
    assert page.locator('.assistant-message').count() == visible_messages
    assert [cp['id'] for cp in after['checkpoints']] == [cp['id'] for cp in before['checkpoints']]
    assert after['revision'] == before['revision'] + 1
    # A later manual edit can use the original checkpoint again, without
    # manufacturing a checkpoint or chat message for the restoration itself.
    manual = 'SELECT note FROM entries WHERE id = 27'
    _set_sql(page, manual)
    restore.wait_for()
    restore.click()
    page.wait_for_timeout(100)
    assert len(pending) == 1
    stub._route(pending.pop())
    page.wait_for_function('sql => document.querySelector("#sql").value === sql', arg=original)
    assert page.get_by_test_id('assistant-restored').count() == 1
    assert restore.count() == 0
    final = stub.store.get(conversation_id)
    assert page.locator('.assistant-message').count() == visible_messages
    assert [cp['id'] for cp in final['checkpoints']] == [cp['id'] for cp in before['checkpoints']]
    assert queries == []


def test_assistant_panel_stays_above_result_headers_after_resizing(page_sqlite, tmp_path, monkeypatch):
    page = page_sqlite
    page.set_viewport_size({'width': 1280, 'height': 900})
    _assistant_ready(page, tmp_path, monkeypatch, configured=False)
    page.locator('#runBtn').click()
    page.locator('#grid tbody tr').first.wait_for()
    page.get_by_test_id('assistant-open').click()

    def assert_boundaries():
        page.wait_for_function('size => innerWidth === size.width && innerHeight === size.height', arg=page.viewport_size)
        # Let the resize observer settle, then measure the shared row in one
        # browser frame so a window resize cannot split the samples.
        page.evaluate('() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))')
        boxes = page.evaluate('''() => Object.fromEntries(Object.entries({
          panel: '[data-testid="assistant-panel"]', toolbar: '.toolbar',
          header: '#grid thead th', composer: '[data-testid="assistant-input"]'
        }).map(([name, selector]) => [name, document.querySelector(selector).getBoundingClientRect().toJSON()]))''')
        panel, toolbar, header, composer = (boxes[key] for key in ('panel', 'toolbar', 'header', 'composer'))
        bottom = panel['y'] + panel['height']
        assert abs(bottom - toolbar['y'] - toolbar['height']) <= 2
        assert bottom <= header['y'] + 1
        assert composer['y'] >= panel['y'], {'panel': panel, 'composer': composer}
        assert composer['y'] + composer['height'] <= bottom, {'panel': panel, 'composer': composer}
        assert panel['x'] + panel['width'] <= page.viewport_size['width']
        assert page.get_by_test_id('assistant-messages').evaluate('el => el.getBoundingClientRect().bottom <= el.parentElement.getBoundingClientRect().bottom')

    assert_boundaries()
    _drag(page, '#edresizer', 0, 80)
    assert_boundaries()
    page.set_viewport_size({'width': 1000, 'height': 700})
    assert_boundaries()
    _drag(page, '#edresizer', 0, -120)
    assert_boundaries()
    field = page.get_by_test_id('assistant-input')
    box = field.bounding_box()
    page.mouse.move(box['x'] + box['width'] - 3, box['y'] + box['height'] - 3)
    page.mouse.down()
    page.mouse.move(box['x'] + box['width'] - 3, box['y'] + box['height'] + 100, steps=5)
    page.mouse.up()
    assert field.bounding_box()['height'] == 140
    assert_boundaries()
    _drag(page, '#edresizer', 0, -600)
    assert page.locator('.editor-input').bounding_box()['height'] == 200
    assert_boundaries()
    page.set_viewport_size({'width': 1280, 'height': 900})
    assert_boundaries()
    page.get_by_test_id('assistant-close').click()
    toolbar = page.locator('.toolbar').bounding_box()
    editor = page.get_by_test_id('editor-workspace').bounding_box()
    assert abs(toolbar['width'] - editor['width']) <= 2
    assert toolbar['y'] + toolbar['height'] <= page.locator('#grid thead th').first.bounding_box()['y']


@pytest.mark.parametrize('has_results', [True, False])
def test_assistant_editor_can_expand_to_status_bar_and_restore_results(page_sqlite, tmp_path, monkeypatch, has_results):
    page = page_sqlite
    page.set_viewport_size({'width': 1920, 'height': 1200})
    _assistant_ready(page, tmp_path, monkeypatch, configured=False)
    original = page.locator('#sql').input_value()
    if has_results:
        page.locator('#runBtn').click()
        page.locator('#grid tbody tr').first.wait_for()
        page.locator('#status').wait_for()
    else:
        assert page.locator('#status').is_hidden()
    queries = _assistant_query_requests(page)
    page.get_by_test_id('assistant-open').click()

    def assert_bottom_controls_fit(*, near_status=False):
        page.wait_for_function('''() => {
          const panel = document.querySelector('[data-testid="assistant-panel"]').getBoundingClientRect();
          const status = document.querySelector('#status');
          const limit = status.offsetHeight ? status.getBoundingClientRect().top : document.querySelector('.vg-section').getBoundingClientRect().bottom;
          return panel.bottom <= limit + 1 && limit <= window.innerHeight;
        }''')
        limit = page.evaluate('''() => { const status = document.querySelector('#status');
          return status.offsetHeight ? status.getBoundingClientRect().top : document.querySelector('.vg-section').getBoundingClientRect().bottom;
        }''')
        workspace = page.get_by_test_id('editor-workspace').bounding_box()
        panel = page.get_by_test_id('assistant-panel').bounding_box()
        toolbar = page.locator('.toolbar').bounding_box()
        composer = page.get_by_test_id('assistant-input').bounding_box()
        bottom = workspace['y'] + workspace['height']
        assert bottom <= limit + 1
        assert abs(panel['y'] + panel['height'] - bottom) <= 1
        assert abs(toolbar['y'] + toolbar['height'] - bottom) <= 1
        assert composer['y'] >= panel['y']
        assert composer['y'] + composer['height'] <= bottom
        assert limit <= page.viewport_size['height']
        if near_status:
            assert limit - bottom <= 12, {'workspace': workspace, 'limit': limit}
        for selector in ('#runBtn', '#fmtBtn', '#expBtn', '#csvBtn', '#jsonBtn', '#maxRows', '#histBtn', '#linkBtn'):
            control = page.locator(selector).bounding_box()
            assert control['y'] >= toolbar['y']
            assert control['y'] + control['height'] <= bottom
        assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')

    _drag(page, '#edresizer', 0, 2000)
    assert_bottom_controls_fit(near_status=True)
    assert page.locator('.editor-input').bounding_box()['height'] > 1200 * 0.7
    saved_height = page.evaluate('localStorage.qy_edh')
    expanded_toolbar_height = page.locator('.toolbar').bounding_box()['height']
    page.screenshot(path=str(tmp_path / 'assistant-full-height.png'))

    # A smaller viewport and wrapped toolbar must re-clamp the same saved
    # height to the actual room, rather than hiding the footer or chat input.
    page.set_viewport_size({'width': 1000, 'height': 700})
    assert_bottom_controls_fit(near_status=True)
    assert page.locator('.toolbar').bounding_box()['height'] > expanded_toolbar_height
    assert page.evaluate('localStorage.qy_edh') == saved_height
    page.screenshot(path=str(tmp_path / 'assistant-full-height-narrow.png'))

    # Hiding and reopening in a smaller window must retain the user's larger
    # preferred height, so returning to the original window restores it.
    page.get_by_test_id('assistant-close').click()
    assert page.locator('.editor-input').bounding_box()['height'] == 88
    page.get_by_test_id('assistant-open').click()
    assert_bottom_controls_fit(near_status=True)
    assert page.evaluate('localStorage.qy_edh') == saved_height
    page.set_viewport_size({'width': 1920, 'height': 1200})
    page.wait_for_function('height => Math.abs(document.querySelector(".editor-input").getBoundingClientRect().height - Number(height)) <= 1', arg=saved_height)
    assert_bottom_controls_fit(near_status=True)
    page.set_viewport_size({'width': 1000, 'height': 700})
    assert_bottom_controls_fit(near_status=True)

    _drag(page, '#edresizer', 0, -400)
    assert_bottom_controls_fit()
    grid = page.locator('#grid').bounding_box()
    assert grid['height'] > 100
    if has_results:
        header = page.locator('#grid thead th').first.bounding_box()
        assert header['y'] >= page.get_by_test_id('assistant-panel').bounding_box()['y'] + page.get_by_test_id('assistant-panel').bounding_box()['height']
        assert header['y'] + header['height'] <= page.locator('#status').bounding_box()['y']
    _drag(page, '#edresizer', 0, 2000)
    assert_bottom_controls_fit(near_status=True)
    small_grid_height = page.locator('#grid').bounding_box()['height']
    page.get_by_test_id('assistant-close').click()
    assert page.locator('.editor-input').bounding_box()['height'] == 88
    assert page.locator('#grid').bounding_box()['height'] > small_grid_height + 150
    if has_results:
        assert page.locator('#grid thead th').first.bounding_box()['y'] + header['height'] <= page.locator('#status').bounding_box()['y']
    assert page.locator('#sql').input_value() == original
    assert queries == []


def test_assistant_inflight_manual_edit_requires_explicit_application(page_sqlite, tmp_path, monkeypatch):
    page = page_sqlite
    stub = _assistant_ready(page, tmp_path, monkeypatch, sql='')
    page.get_by_test_id('assistant-open').click()
    stub.proposal = 'SELECT id FROM entries LIMIT 3'
    stub.hold = True
    _assistant_send(page, 'Write a short query')
    page.wait_for_timeout(100)
    assert len(stub.pending) == 1
    manual = 'SELECT note FROM entries WHERE id = 77'
    _set_sql(page, manual)
    stub.complete()
    page.get_by_test_id('assistant-apply').wait_for()
    assert page.locator('#sql').input_value() == manual
    assert page.get_by_test_id('assistant-restore').count() == 0
    page.get_by_test_id('assistant-apply').click()
    page.wait_for_function('sql => document.querySelector("#sql").value === sql', arg=stub.proposal)
    assert manual in page.evaluate("JSON.parse(localStorage.qy_hist).map(h => h.sql)")
    page.get_by_test_id('assistant-restore').click()
    page.wait_for_function('sql => document.querySelector("#sql").value === sql', arg=manual)


@pytest.mark.parametrize('navigation', ['tab', 'close', 'environment'])
def test_assistant_late_reply_cannot_overwrite_other_context(page_sqlite, tmp_path, monkeypatch, navigation):
    page = page_sqlite
    stub = _assistant_ready(page, tmp_path, monkeypatch, sql='')
    page.get_by_test_id('assistant-open').click()
    stub.proposal = 'SELECT id FROM entries LIMIT 7'
    stub.hold = True
    _assistant_send(page, 'Write seven rows')
    page.wait_for_timeout(100)
    assert len(stub.pending) == 1
    if navigation == 'tab':
        page.locator('#tabAdd').click()
        _set_sql(page, 'SELECT 55 AS other_tab')
    elif navigation == 'close':
        page.locator('.tab.on .x').click()
        page.locator('#queryEmptyState').wait_for()
    else:
        page.locator('#esw .ep[data-env="local"]').click()
        _set_sql(page, 'SELECT 66 AS other_environment')
    current = page.locator('#sql').input_value() if navigation != 'close' else None
    stub.complete()
    page.wait_for_timeout(250)
    if navigation == 'close':
        assert page.locator('#sql').count() == 0
        assert page.locator('#tabs .tab').count() == 0
    else:
        assert page.locator('#sql').input_value() == current
        if navigation == 'environment':
            assert page.locator('#esw .ep.on').get_attribute('data-env') == 'local'


def test_assistant_conversation_reopens_closed_tab_with_checkpoints(page_sqlite, tmp_path, monkeypatch):
    page = page_sqlite
    stub = _assistant_ready(page, tmp_path, monkeypatch, sql='')
    queries = _assistant_query_requests(page)
    page.get_by_test_id('assistant-open').click()
    stub.proposal = 'SELECT id FROM entries LIMIT 4'
    _assistant_send(page, 'Find four entries')
    page.wait_for_function('sql => document.querySelector("#sql").value === sql', arg=stub.proposal)
    page.get_by_test_id('assistant-restore').wait_for()
    page.locator('.tab.on .x').click()
    page.locator('#queryEmptyState').wait_for()
    page.reload(wait_until='networkidle')
    page.locator('#histBtn').click()
    page.get_by_test_id('history-conversations').click()
    page.get_by_test_id('conversation-item').filter(has_text='Find four entries').click()
    page.get_by_test_id('assistant-panel').wait_for()
    assert page.locator('#sql').input_value() == stub.proposal
    assert 'Find four entries' in page.get_by_test_id('assistant-messages').inner_text()
    assert page.get_by_test_id('assistant-restore').count() == 1
    assert queries == []
    count = page.locator('#tabs .tab').count()
    page.locator('#histBtn').click()
    page.get_by_test_id('history-conversations').click()
    page.get_by_test_id('conversation-item').filter(has_text='Find four entries').click()
    assert page.locator('#tabs .tab').count() == count
    assert len(stub.chats) == 1


@pytest.mark.parametrize('failure', ['provider', 'interrupted'])
def test_assistant_failed_reply_cannot_apply_partial_sql(page_sqlite, tmp_path, monkeypatch, failure):
    page = page_sqlite
    stub = _assistant_ready(page, tmp_path, monkeypatch, sql='')
    queries = _assistant_query_requests(page)
    page.get_by_test_id('assistant-open').click()
    stub.response = 'Here is a draft: SELECT id FROM'
    stub.proposal = 'SELECT id FROM entries LIMIT 2'
    if failure == 'provider':
        stub.error = 'Model temporarily unavailable. Try again.'
    else:
        stub.close_without_done = True
    _assistant_send(page, 'Write a query')
    page.get_by_text(re.compile('temporarily unavailable|interrupted')).wait_for()
    assert page.locator('#sql').input_value() == ''
    assert page.get_by_test_id('assistant-restore').count() == 0
    assert page.get_by_test_id('assistant-apply').count() == 0
    assert queries == []


def test_assistant_stream_scroll_keeps_composer_fixed_and_respects_reader(page_sqlite, tmp_path, monkeypatch):
    page = page_sqlite
    stub = _assistant_ready(page, tmp_path, monkeypatch, sql='')
    page.get_by_test_id('assistant-open').click()
    # A controllable browser ReadableStream exercises incremental rendering,
    # unlike route.fulfill(), which delivers its body all at once.
    page.evaluate('''() => {
      const original = window.fetch.bind(window);
      window.fetch = async (url, init) => {
        if (String(url).endsWith('/api/assistant/chat')) {
          window.__assistantRequest = JSON.parse(init.body);
          const stream = new ReadableStream({start(controller) { window.__assistantController = controller; }});
          return new Response(stream, {headers: {'Content-Type': 'application/x-ndjson'}});
        }
        return original(url, init);
      };
      window.__assistantChunk = events => window.__assistantController.enqueue(
        new TextEncoder().encode(events.map(event => JSON.stringify(event)).join('\\n') + '\\n'));
    }''')
    _assistant_send(page, 'Explain this step by step')
    page.wait_for_function('Boolean(window.__assistantRequest && window.__assistantController)')
    request = page.evaluate('window.__assistantRequest')
    ident = {key: request[key] for key in ('requestId', 'conversationId')}
    messages = page.get_by_test_id('assistant-messages')
    composer_before = page.get_by_test_id('assistant-input').bounding_box()
    first = '\n\n'.join(f'Step {i}: Read the relevant table and verify the join keys before choosing the query.' for i in range(40))
    page.evaluate('events => window.__assistantChunk(events)', [dict(ident, type='request'), dict(ident, type='text_delta', delta=first)])
    page.wait_for_function('''() => { const el = document.querySelector('[data-testid="assistant-messages"]');
      return el.scrollHeight > el.clientHeight && el.scrollHeight - el.scrollTop - el.clientHeight < 10; }''')
    scrollbar = _assistant_scrollbar_styles(page)
    assert scrollbar['trackTop'] == '38px'
    assert scrollbar['width'] == '4px'
    assert messages.evaluate('''(el, marginTop) => {
      const scrollport = el.getBoundingClientRect();
      const handle = document.querySelector('[data-testid="assistant-close"]').getBoundingClientRect();
      const divider = document.querySelector('[data-testid="assistant-divider"]').getBoundingClientRect();
      const trackTop = scrollport.top + parseFloat(marginTop);
      const hit = document.elementFromPoint(scrollport.right - 2, trackTop + 2);
      return Math.abs(scrollport.right - divider.left) <= 1 && trackTop >= handle.bottom
        && !hit?.closest('[data-testid="assistant-close"]')
        && getComputedStyle(el, '::-webkit-scrollbar').width === '4px';
    }''', scrollbar['trackTop'])
    assert page.locator('#sql').input_value() == ''
    messages.evaluate('el => { el.scrollTop = 0; el.dispatchEvent(new Event("scroll")); }')
    tail = '\n\nThe relationship is now clear.'
    page.evaluate('events => window.__assistantChunk(events)', [dict(ident, type='text_delta', delta=tail)])
    page.wait_for_function('''() => document.querySelector('[data-testid="assistant-messages"]').textContent.includes('The relationship is now clear.')''')
    assert messages.evaluate('el => el.scrollTop') < 5
    assert abs(page.get_by_test_id('assistant-input').bounding_box()['y'] - composer_before['y']) <= 2
    assert page.evaluate('window.scrollY') == 0
    stub.response = first + tail
    done = stub.events(request)[-1]
    page.evaluate('event => { window.__assistantChunk([event]); window.__assistantController.close(); }', done)


def test_assistant_divider_restores_width_after_collapse(page_sqlite, tmp_path, monkeypatch):
    page = page_sqlite
    _assistant_ready(page, tmp_path, monkeypatch, configured=False)
    page.get_by_test_id('assistant-open').click()
    panel = page.get_by_test_id('assistant-panel')
    before = panel.bounding_box()['width']
    divider = page.get_by_test_id('assistant-divider').bounding_box()
    page.mouse.move(divider['x'] + divider['width'] / 2, divider['y'] + divider['height'] / 2)
    page.mouse.down()
    page.mouse.move(divider['x'] + 80, divider['y'] + divider['height'] / 2, steps=8)
    page.mouse.up()
    resized = panel.bounding_box()['width']
    assert resized > before + 40
    close = page.get_by_test_id('assistant-close')
    assert close.evaluate('el => el.parentElement === document.querySelector("[data-testid=assistant-divider]").parentElement')
    close_box = close.bounding_box()
    x, y = close_box['x'] + close_box['width'] / 2, close_box['y'] + close_box['height'] / 2
    page.mouse.move(x, y)
    page.mouse.down()
    page.mouse.move(x + 6, y, steps=3)
    page.mouse.up()
    page.get_by_test_id('assistant-open').click()
    assert abs(panel.bounding_box()['width'] - resized) <= 2
    page.get_by_test_id('assistant-divider').focus()
    page.get_by_test_id('assistant-divider').press('ArrowRight')
    assert panel.bounding_box()['width'] > resized
    page.get_by_test_id('assistant-divider').press('ArrowLeft')
    assert abs(panel.bounding_box()['width'] - resized) <= 2
    page.get_by_test_id('assistant-divider').press('Tab')
    assert close.evaluate('el => document.activeElement === el')
    close.press('Enter')
    page.get_by_test_id('assistant-open').click()
    assert abs(panel.bounding_box()['width'] - resized) <= 2


def test_assistant_saved_query_edit_creates_draft_preserves_template(page_sqlite, tmp_path, monkeypatch):
    page = page_sqlite
    stub = _AssistantBrowserStub(page, tmp_path, monkeypatch)
    template = 'SELECT id, note FROM entries LIMIT 10'
    page.route('**/api/queries', lambda route: route.fulfill(json=[{
        'name': 'entries-template', 'queryId': 'assistant-template', 'db': 'notes',
        'desc': 'Recent entries template', 'sql': template, 'params': []}]))
    page.reload(wait_until='networkidle')
    _select_sqlite(page)
    page.locator('.query-index-children [data-q="entries-template"]').click()
    assert page.locator('#sql').get_attribute('readonly') is not None
    page.get_by_test_id('assistant-open').click()
    _assistant_explain(page)
    page.get_by_test_id('assistant-messages').get_by_text(stub.response, exact=True).wait_for()
    queries = _assistant_query_requests(page)
    stub.proposal = 'SELECT id, note FROM entries WHERE id > 20 LIMIT 5'
    _assistant_send(page, 'Filter to id greater than twenty and limit to five')
    page.wait_for_function('sql => document.querySelector("#sql").value === sql', arg=stub.proposal)
    assert page.locator('#sql').get_attribute('readonly') is None
    tabs = page.evaluate('JSON.parse(localStorage.qy_tabs)')
    assert next(tab for tab in tabs if tab.get('savedQueryId') == 'assistant-template')['sql'] == template
    assert any(tab['sql'] == stub.proposal and not tab.get('savedQueryId') for tab in tabs)
    assert queries == []


def test_assistant_close_flushes_latest_manual_sql_to_history(page_sqlite, tmp_path, monkeypatch):
    page = page_sqlite
    stub = _assistant_ready(page, tmp_path, monkeypatch)
    page.get_by_test_id('assistant-open').click()
    _assistant_explain(page)
    page.get_by_test_id('assistant-messages').get_by_text(stub.response, exact=True).wait_for()
    latest = 'SELECT note FROM entries WHERE id = 221'
    _set_sql(page, latest)
    page.locator('.tab.on .x').click()
    page.locator('#queryEmptyState').wait_for()
    page.locator('#histBtn').click()
    page.get_by_test_id('history-conversations').click()
    page.get_by_test_id('conversation-item').first.click()
    page.wait_for_function('sql => document.querySelector("#sql")?.value === sql', arg=latest)
    assert len(stub.chats) == 1


def test_assistant_manual_edit_during_apply_roundtrip_remains_restorable(page_sqlite, tmp_path, monkeypatch):
    page = page_sqlite
    stub = _assistant_ready(page, tmp_path, monkeypatch, sql='')
    page.get_by_test_id('assistant-open').click()
    pending = []
    page.route('**/api/assistant/conversations/apply', lambda route: pending.append(route))
    stub.proposal = 'SELECT id FROM entries LIMIT 8'
    _assistant_send(page, 'Show eight entries')
    page.wait_for_timeout(100)
    assert len(pending) == 1
    manual = 'SELECT note FROM entries WHERE id = 99'
    _set_sql(page, manual)
    stub._route(pending.pop())
    page.get_by_test_id('assistant-apply').wait_for()
    assert page.locator('#sql').input_value() == manual
    assert page.get_by_test_id('assistant-restore').count() == 0
    page.unroute('**/api/assistant/conversations/apply')
    page.get_by_test_id('assistant-apply').click()
    page.wait_for_function('sql => document.querySelector("#sql").value === sql', arg=stub.proposal)
    page.get_by_test_id('assistant-restore').click()
    page.wait_for_function('sql => document.querySelector("#sql").value === sql', arg=manual)


def test_assistant_history_search_empty_and_unavailable_connection(page_sqlite, tmp_path, monkeypatch):
    page = page_sqlite
    stub = _assistant_ready(page, tmp_path, monkeypatch, configured=False)
    queries = _assistant_query_requests(page)
    page.locator('#histBtn').click()
    page.get_by_test_id('history-conversations').click()
    page.get_by_text('No conversations yet.', exact=True).wait_for()
    page.keyboard.press('Escape')
    for title in ('Daily orders report', 'Session analysis'):
        stub.store.create(db='unavailable', env='dev', workspace='removed-workspace',
                          sql='SELECT 1', fingerprint='removed', title=title)
    page.locator('#histBtn').click()
    page.get_by_test_id('history-conversations').click()
    page.get_by_test_id('conversation-item').first.wait_for()
    assert page.get_by_test_id('conversation-item').count() == 2
    search = page.locator('.modal .hsearch')
    search.fill('orders')
    assert page.get_by_test_id('conversation-item').count() == 1
    assert 'Daily orders report' in page.get_by_test_id('conversation-item').inner_text()
    search.fill('does-not-exist')
    assert page.get_by_test_id('conversation-item').count() == 0
    page.get_by_text('No matching conversations.', exact=True).wait_for()
    search.fill('orders')
    original = page.locator('#sql').input_value()
    page.get_by_test_id('conversation-item').click()
    page.get_by_text("This conversation's connection is no longer available.", exact=True).wait_for()
    assert page.locator('#sql').input_value() == original
    assert queries == []
    page.get_by_test_id('history-queries').click()
    assert search.input_value() == ''


def test_assistant_existing_conversation_loads_when_model_is_unconfigured(page_sqlite, tmp_path, monkeypatch):
    page = page_sqlite
    stub = _assistant_ready(page, tmp_path, monkeypatch)
    page.get_by_test_id('assistant-open').click()
    _assistant_explain(page)
    page.get_by_test_id('assistant-messages').get_by_text(stub.response, exact=True).wait_for()
    stub.configured = False
    page.reload(wait_until='networkidle')
    page.get_by_test_id('assistant-open').click()
    page.get_by_test_id('assistant-messages').get_by_text(stub.response, exact=True).wait_for()
    assert len(stub.chats) == 1
    _assistant_send(page, 'Explain another detail')
    page.get_by_text(re.compile('Model not configured')).wait_for()
    assert page.get_by_test_id('assistant-input').input_value() == 'Explain another detail'
    assert stub.response in page.get_by_test_id('assistant-messages').inner_text()


def test_assistant_close_during_conversation_creation_keeps_latest_sql(page_sqlite, tmp_path, monkeypatch):
    page = page_sqlite
    stub = _assistant_ready(page, tmp_path, monkeypatch, sql='')
    page.get_by_test_id('assistant-open').click()
    pending = []
    page.route('**/api/assistant/conversations', lambda route: pending.append(route)
               if route.request.method == 'POST' else stub._route(route))
    _assistant_send(page, 'Help write a query')
    page.wait_for_timeout(100)
    assert len(pending) == 1
    latest = 'SELECT id FROM entries WHERE id = 159'
    _set_sql(page, latest)
    page.locator('.tab.on .x').click()
    page.locator('#queryEmptyState').wait_for()
    stub._route(pending.pop())
    page.locator('#histBtn').click()
    page.get_by_test_id('history-conversations').click()
    page.get_by_test_id('conversation-item').first.click()
    page.wait_for_function('sql => document.querySelector("#sql")?.value === sql', arg=latest)
    assert stub.chats == []


@pytest.mark.parametrize('action', ['apply', 'restore'])
def test_assistant_edit_cancels_pending_environment_autorun(page_sqlite, tmp_path, monkeypatch, action):
    page = page_sqlite
    original = 'SELECT id FROM entries LIMIT 10'
    stub = _assistant_ready(page, tmp_path, monkeypatch, sql=original)
    if action == 'restore':
        page.get_by_test_id('assistant-open').click()
        _assistant_explain(page)
        page.get_by_test_id('assistant-messages').get_by_text(stub.response, exact=True).wait_for()
        stub.proposal = 'SELECT id FROM entries LIMIT 2'
        _assistant_send(page, 'Show two entries')
        page.wait_for_function('sql => document.querySelector("#sql").value === sql', arg=stub.proposal)
        page.locator('#esw .ep[data-env="local"]').click()
        page.wait_for_selector('#grid tbody tr')
        # Reload clears the frontend metadata cache while preserving dev's
        # conversation. Navigating back must wait for its new table request.
        page.reload(wait_until='networkidle')
    requests = _assistant_query_requests(page)
    pending = []
    page.route('**/api/tables?*', lambda route: pending.append(route))
    destination = 'dev' if action == 'restore' else 'local'
    page.locator(f'#esw .ep[data-env="{destination}"]').click()
    page.wait_for_timeout(100)
    assert pending
    page.get_by_test_id('assistant-open').click()
    if action == 'restore':
        page.get_by_test_id('assistant-restore').click()
        expected = original
    else:
        _assistant_explain(page)
        page.get_by_test_id('assistant-messages').get_by_text(stub.response, exact=True).wait_for()
        stub.proposal = 'SELECT id, note FROM entries LIMIT 3'
        _assistant_send(page, 'Include the note and show three')
        expected = stub.proposal
    page.wait_for_function('sql => document.querySelector("#sql").value === sql', arg=expected)
    for route in pending:
        route.fulfill(json={'engine': 'sqlite', 'tables': ['entries'], 'capped': False})
    page.locator('#tbl-panel [data-t="entries"]').wait_for()
    page.wait_for_timeout(150)
    assert requests == []
    assert page.locator('#sql').input_value() == expected


def test_fresh_default_language_is_chinese_and_switch_persists(page_sqlite_default_language, tmp_path, monkeypatch):
    page = page_sqlite_default_language
    _AssistantBrowserStub(page, tmp_path, monkeypatch, configured=False)
    page.evaluate("localStorage.removeItem('qy_lang')")
    page.reload(wait_until='networkidle')
    assert page.locator('#runLbl').inner_text() == '运行'
    assert page.locator('.vg-lang-switch').inner_text() == '中'
    _select_sqlite(page)
    assert re.search(r'[\u4e00-\u9fff]', page.get_by_test_id('assistant-open').inner_text())
    page.locator('.vg-lang-switch').click()
    page.wait_for_function("document.querySelector('#runLbl')?.textContent === 'Run'")
    assert page.evaluate("localStorage.getItem('qy_lang')") == 'en'
    assert page.get_by_test_id('assistant-open').inner_text() == 'SQL Assistant'
    page.reload(wait_until='networkidle')
    assert page.locator('#runLbl').inner_text() == 'Run'
    page.locator('.vg-lang-switch').click()
    page.wait_for_function("document.querySelector('#runLbl')?.textContent === '运行'")
    assert page.evaluate("localStorage.getItem('qy_lang')") == 'zh'
    page.reload(wait_until='networkidle')
    assert page.locator('#runLbl').inner_text() == '运行'


@pytest.mark.parametrize('language', ['zh', 'en'])
def test_assistant_localizes_explanation_checkpoint_configuration_and_history(page_sqlite_default_language, tmp_path, monkeypatch, language):
    page = page_sqlite_default_language
    page.evaluate('(language) => localStorage.setItem("qy_lang", language)', language)
    stub = _assistant_ready(page, tmp_path, monkeypatch)
    from quarry.assistant_config import set_reply_language
    set_reply_language(language)
    if language == 'zh':
        stub.response = '这条查询从 entries 表读取 id 和 note，最多返回 10 行。'
    page.get_by_test_id('assistant-open').click()
    field = page.get_by_test_id('assistant-input')
    suggestion = '解读当前 SQL' if language == 'zh' else 'Explain this SQL'
    assert field.get_attribute('placeholder') == ''
    assert page.get_by_test_id('assistant-ghost').inner_text() == suggestion
    assert page.get_by_test_id('assistant-suggestion-hint').count() == 0
    assert stub.chats == []
    _assistant_explain(page)
    page.get_by_test_id('assistant-messages').get_by_text(stub.response, exact=True).wait_for()
    assert stub.chats[0]['prompt'] == suggestion
    field = page.get_by_test_id('assistant-input')
    if language == 'zh':
        assert re.search(r'[\u4e00-\u9fff]', page.get_by_test_id('assistant-ghost').inner_text())
        assert re.search(r'[\u4e00-\u9fff]', page.get_by_test_id('assistant-close').get_attribute('title'))
    else:
        assert page.get_by_test_id('assistant-ghost').inner_text() == suggestion
    stub.proposal = 'SELECT note FROM entries LIMIT 2'
    if language == 'zh':
        stub.response = '### **查询已更新**\n\n只读取 `note`，最多返回 **2 行**。\n\n```sql\nSELECT note FROM entries LIMIT 2;\n```'
    _assistant_send(page, '只显示两行' if language == 'zh' else 'Show two rows')
    page.wait_for_function('sql => document.querySelector("#sql").value === sql', arg=stub.proposal)
    restore_text = page.get_by_test_id('assistant-restore').inner_text()
    assert ('恢复' in restore_text) if language == 'zh' else restore_text.strip() == 'Restore previous SQL'
    field.hover()
    assert page.get_by_test_id('assistant-ghost').inner_text() == suggestion
    for mode in ('dark', 'light'):
        if page.evaluate('document.documentElement.dataset.mode') != mode:
            page.locator('.vg-switcher-mode').click()
        page.mouse.move(500, 30)
        page.screenshot(path=str(tmp_path / f'assistant-language-{language}-{mode}.png'))
        page.get_by_test_id('assistant-close').click()
        rail = page.get_by_test_id('assistant-open')
        label = rail.locator('span')
        assert label.inner_text() == ('SQL 助手' if language == 'zh' else 'SQL Assistant')
        assert label.evaluate('el => getComputedStyle(el).writingMode') == 'vertical-rl'
        page.screenshot(path=str(tmp_path / f'assistant-collapsed-{language}-{mode}.png'))
        page.get_by_test_id('assistant-open').click()
    stub.configured = False
    prompt = '继续解读' if language == 'zh' else 'Explain another detail'
    _assistant_send(page, prompt)
    page.get_by_role('alert').filter(has_text='qy ai configure').wait_for()
    assert field.input_value() == prompt
    error = page.get_by_role('alert').filter(has_text='qy ai configure').inner_text()
    assert ('Model not configured' not in error and re.search(r'[\u4e00-\u9fff]', error)) if language == 'zh' else 'Model not configured' in error
    page.locator('#histBtn').click()
    for testid in ('history-queries', 'history-conversations'):
        text = page.get_by_test_id(testid).inner_text()
        assert bool(re.search(r'[\u4e00-\u9fff]', text)) == (language == 'zh')
    page.get_by_test_id('history-conversations').click()
    page.get_by_test_id('conversation-item').first.wait_for()
