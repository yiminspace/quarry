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

from conftest import _running_gui, requires_browser, stub_cdn, stub_events, REDIS_OK, TEST_DB_URL
from test_gui_browser import _run_result, _select_testpg, _set_sql
from test_gui_browser import page_saved  # noqa: F401  (fixture reused below)

pytestmark = [requires_browser, pytest.mark.browser]


# ---------------------------------------------------------------------------
# helpers + fixtures
# ---------------------------------------------------------------------------

def _mk_page(browser, url):
    ctx = browser.new_context(viewport={"width": 1280, "height": 900})
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
    pages = [request.getfixturevalue(n)
             for n in ("page", "page_envset", "page_envset_local", "page_noparam",
                       "page_redis", "page_redis_capped", "page_dead", "page_clip",
                       "page_saved")
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
db = "shop"
group = "acme"
"""


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
    ctx = _pw_browser.new_context(viewport={"width": 1280, "height": 900})
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

def test_table_click_preserves_draft_in_history(page):
    _select_testpg(page)
    _set_sql(page, "select 123 as draft_marker")     # hand-written, never run
    page.locator('#tbl-panel .tname[data-t="customers"]').click()
    page.wait_for_selector("#grid table tbody tr")
    # editor now holds the generated query…
    assert "from customers" in page.locator("#sql").input_value()
    # …and the draft is recoverable from History
    page.locator("#histBtn").click()
    page.wait_for_selector(".modal .hitem")
    assert page.locator(".modal .hitem", has_text="draft_marker").count() == 1


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
    match = re.fullmatch(r"rgba?\((\d+), (\d+), (\d+)(?:, [^)]+)?\)", rgb)
    assert match, rgb
    channels = [int(value) / 255 for value in match.groups()]
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
    # sidebar pills: dev selected by default, prod styled as prod
    assert "on" in page.locator('.pill[data-db="shop"][data-env="dev"]').first.get_attribute("class").split()
    assert "prod" in page.locator('.pill[data-db="shop"][data-env="prod"]').first.get_attribute("class").split()
    page.locator('.dbrow[data-db="shop"]').click()
    page.wait_for_selector("#esw .ep")               # header env switcher appears
    assert page.locator("#prodBadge").is_hidden()
    page.locator('#esw .ep[data-env="prod"]').click()
    page.wait_for_selector("#prodBadge", state="visible")


def test_selected_prod_pills_keep_accessible_contrast_in_light_themes(page_envset):
    page = page_envset
    page.locator('.dbrow[data-db="shop"]').click()
    page.wait_for_selector("#esw .ep")
    page.locator('#esw .ep[data-env="prod"]').click()
    page.locator(".vg-switcher-mode").click()
    assert page.evaluate("document.documentElement.dataset.mode") == "light"

    selectors = (
        '.pill[data-db="shop"][data-env="prod"]',
        '#esw .ep[data-env="prod"]',
    )
    for theme, expected_bg in (("slate", "rgb(178, 59, 52)"),
                               ("signal", "rgb(179, 45, 45)")):
        if theme == "signal":
            page.locator(".vg-switcher-trigger").click()
            page.get_by_role("menuitemradio", name="Signal").click()
        assert page.evaluate("document.documentElement.dataset.theme") == theme
        for selector in selectors:
            fg, bg, ratio = _contrast_ratio(page.locator(selector).first)
            assert fg == "rgb(255, 255, 255)"
            assert bg == expected_bg
            assert ratio >= 4.5, (theme, selector, fg, bg, ratio)


def test_prod_env_switch_does_not_autorun(page_envset):
    page = page_envset
    page.wait_for_selector('.dbrow[data-db="shop"]')
    page.locator('.dbrow[data-db="shop"]').click()
    page.wait_for_selector("#esw .ep")
    _run_sql(page, "select 1 as a")
    queries = []
    page.on("request", lambda r: "/api/query" in r.url and queries.append(r.url))
    page.locator('#esw .ep[data-env="prod"]').click()
    page.wait_for_selector("#toast", state="visible")     # notice instead of a run
    assert "prod" in page.locator("#toast").inner_text().lower()
    page.wait_for_timeout(700)
    assert queries == []                                   # no auto-run happened
    assert page.locator("#grid table").count() == 1        # old result still painted


def test_nonprod_env_switch_autoruns(page_envset):
    page = page_envset
    page.wait_for_selector('.dbrow[data-db="shop"]')
    page.locator('.dbrow[data-db="shop"]').click()
    page.wait_for_selector("#esw .ep")
    _run_sql(page, "select 1 as a")
    page.locator('#esw .ep[data-env="prod"]').click()      # -> prod (no run)
    page.wait_for_selector("#toast", state="visible")
    queries = []
    page.on("request", lambda r: "/api/query" in r.url and queries.append(r.url))
    page.locator('#esw .ep[data-env="dev"]').click()       # -> dev auto-runs
    page.wait_for_selector("#grid table tbody tr")
    page.wait_for_timeout(300)
    assert len(queries) == 1


def test_local_env_sorts_first_and_is_default_without_dev(page_envset_local):
    # "stash" registers prod before local, and has no dev env — local must
    # still be the leftmost pill/tab and the default-selected env (issue #44).
    page = page_envset_local
    page.wait_for_selector('.dbrow[data-db="stash"]')
    pills = page.locator('.pill[data-db="stash"]')
    assert [pills.nth(i).get_attribute("data-env") for i in range(pills.count())] == ["local", "prod"]
    assert "on" in pills.nth(0).get_attribute("class").split()
    page.locator('.dbrow[data-db="stash"]').click()
    page.wait_for_selector("#esw .ep")
    eps = page.locator("#esw .ep")
    assert [eps.nth(i).get_attribute("data-env") for i in range(eps.count())] == ["local", "prod"]
    assert "on" in eps.nth(0).get_attribute("class").split()


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
db = "shop"
group = "acme"
"""


@contextmanager
def _proxied_envset_page(browser, tmp_path, monkeypatch, *, enabled: bool, discovered: bool, reachable: bool = True):
    """Like page_envset, but shop_dev carries ssh_host and `tunnel.list_tunnels()`
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
    monkeypatch.setattr(tunnel, "list_tunnels", lambda: [live_tunnel] if state["proxied"] else [])
    with _running_gui(tmp_path, extra_conn=PROXY_ENVSET_TOML) as url:
        ctx, pg = _mk_page(browser, url)
        try:
            yield pg, state
        finally:
            ctx.close()


def test_proxy_badge_shown_only_on_the_tunneled_env_when_proxy_active(_pw_browser, tmp_path, monkeypatch):
    with _proxied_envset_page(_pw_browser, tmp_path, monkeypatch, enabled=True, discovered=True) as (page, _state):
        page.wait_for_selector('.dbrow[data-db="shop"]')
        # shop_dev has a live, actually-proxied tunnel -> badge visible
        page.wait_for_selector('[data-testid="proxy-badge"][data-db="shop"][data-env="dev"]')
        # shop_prod has no ssh_host at all -> never a candidate for the badge
        assert page.locator('[data-testid="proxy-badge"][data-db="shop"][data-env="prod"]').count() == 0


def test_proxy_badge_hidden_when_workspace_toggle_off(_pw_browser, tmp_path, monkeypatch):
    with _proxied_envset_page(_pw_browser, tmp_path, monkeypatch, enabled=False, discovered=True) as (page, _state):
        page.wait_for_selector('.dbrow[data-db="shop"]')
        assert page.locator('[data-testid="proxy-badge"]').count() == 0


def test_proxy_badge_hidden_when_nothing_discovered(_pw_browser, tmp_path, monkeypatch):
    with _proxied_envset_page(_pw_browser, tmp_path, monkeypatch, enabled=True, discovered=False) as (page, _state):
        page.wait_for_selector('.dbrow[data-db="shop"]')
        assert page.locator('[data-testid="proxy-badge"]').count() == 0


def test_proxy_badge_follows_server_state_across_reload(_pw_browser, tmp_path, monkeypatch):
    """The badge is a live reflection of server state (not cached client-side):
    the underlying tunnel fact changing and reloading must flip the badge too."""
    with _proxied_envset_page(_pw_browser, tmp_path, monkeypatch, enabled=True, discovered=True) as (page, state):
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
    page.locator("#tbl-panel .tsearch").fill("cust")
    assert page.locator('#tbl-panel .tname[data-t="customers"]').is_visible()
    assert page.locator('#tbl-panel .tname[data-t="orders"]').is_hidden()
    page.locator("#tbl-panel .tsearch").fill("")
    assert page.locator('#tbl-panel .tname[data-t="orders"]').is_visible()


# ---------------------------------------------------------------------------
# 6. Grid states: empty result, truncation badge
# ---------------------------------------------------------------------------

def test_zero_rows_empty_state(page):
    _select_testpg(page)
    _set_sql(page, "select * from customers where false")
    page.locator("#runBtn").click()
    page.wait_for_selector('#grid .empty:has-text("0")')   # 0-row state, not a table


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

def test_history_empty_toast(page):
    page.locator("#histBtn").click()
    page.wait_for_selector("#toast", state="visible")
    assert page.locator(".modal").count() == 0


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
    tree.locator("summary").click()
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


def test_saved_query_result_persisted_under_producing_connection(page_saved_multi):
    """A saved query runs on ITS OWN connection (testpg). When launched from a tab
    bound to a different connection (shop@dev), the result must be tagged & persisted
    under the producing connection (testpg), never the tab's previous connection."""
    page = page_saved_multi
    page.wait_for_selector('.dbrow[data-db="shop"]')
    page.locator('.dbrow[data-db="shop"]').click()            # bind the active tab to shop@dev
    page.wait_for_selector("#esw .ep")
    page.wait_for_selector('.qname[data-q="all-cust"]')
    page.locator('.qname[data-q="all-cust"]').click()         # run the saved query (lives on testpg)
    page.wait_for_selector("#grid table tbody tr")
    saved = page.evaluate("JSON.parse(localStorage.getItem('qy_tabres'))")
    assert saved[0]["db"] == "testpg"                         # producing conn, not shop
    tabs = page.evaluate("JSON.parse(localStorage.getItem('qy_tabs'))")
    assert tabs[0]["db"] == "testpg"                          # tab re-pointed to producing conn
    # and it survives a reload under the producing connection
    page.reload(wait_until="networkidle")
    page.wait_for_selector('.dbrow[data-db="testpg"].on')
    page.wait_for_selector("#grid table tbody tr")
    assert page.locator("#grid tbody tr").count() == 3


# ---------------------------------------------------------------------------
# 13. Autocomplete: table.column via /api/columns
# ---------------------------------------------------------------------------

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
    # tree: qygui -> sess -> leaves; TTL badge on sess:1; type badge on jobs
    leaf1 = page.locator('#tbl-panel .tname[data-key="qygui:sess:1"]')
    assert leaf1.locator(".rbadge.ttl").count() == 1
    jobs = page.locator('#tbl-panel .tname[data-key="qygui:jobs"]')
    assert "list" in jobs.locator(".rbadge").first.inner_text()
    # filter narrows the tree
    page.locator("#tbl-panel .tsearch").fill("jobs")
    page.wait_for_selector('#tbl-panel .tname[data-key="qygui:sess:1"]', state="detached")
    assert page.locator('#tbl-panel .tname[data-key="qygui:jobs"]').count() == 1
    page.locator("#tbl-panel .tsearch").fill("")
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
    page.locator('.qname[data-q="cust-by-id"]').click()   # reopen
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
    assert "只读" in page.locator("#roBadge").inner_text()


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
        "localStorage.setItem('qy_ati','0');")
    page.reload(wait_until="networkidle")
    page.wait_for_selector('.tab[data-i="1"]')
    page.locator('.tab[data-i="1"]').click()              # switch to the ghost tab
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

def test_result_not_restored_after_tab_rebound_to_prod(page_envset):
    page = page_envset
    page.wait_for_selector('.dbrow[data-db="shop"]')
    page.locator('.dbrow[data-db="shop"]').click()
    page.wait_for_selector("#esw .ep")
    _run_sql(page, "select 42 as dev_only")               # result produced on shop@dev
    page.wait_for_selector('#grid td[data-v="42"]')
    page.locator('#esw .ep[data-env="prod"]').click()     # rebind this tab to shop@prod (no autorun)
    page.wait_for_selector("#toast", state="visible")
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
# 31f. Tabs: a request in flight whose OWN tab is switched to another env of the
#      same db (no new request) must be dropped, never repainted as the new env.
# ---------------------------------------------------------------------------

def test_inflight_response_dropped_when_same_tab_switches_env(page_envset):
    page = page_envset
    page.wait_for_selector('.dbrow[data-db="shop"]')
    page.locator('.dbrow[data-db="shop"]').click()
    page.wait_for_selector("#esw .ep")                    # on shop@dev
    _set_sql(page, "select pg_sleep(1.2), 42 as devval")
    page.locator("#runBtn").click()                       # slow query in flight on shop@dev
    page.wait_for_selector("#grid .spin")
    page.locator('#esw .ep[data-env="prod"]').click()     # same tab -> prod (no autorun)
    page.wait_for_selector("#toast", state="visible")
    page.wait_for_timeout(1600)                            # let the dev response land
    # the dev rows must never surface under the now-prod tab
    assert page.locator('#grid td[data-v="42"]').count() == 0
    saved = page.evaluate("JSON.parse(localStorage.getItem('qy_tabres'))")
    assert saved[0] is None                                # nothing persisted for the tab either


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
        page.locator("#tbl-panel .treload").click()       # manual fresh fetch
        page.wait_for_selector('#tbl-panel .tname[data-t="qyref_zzz"]', timeout=20000)
    finally:
        pg_exec("DROP TABLE IF EXISTS qyref_zzz")


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
            "select * from qy_gui_schema.events limit 5"
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
    page.locator('.pill[data-db="shoploc"][data-env="local"]').click()
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

    txt = page.locator("#qtitle").inner_text()
    assert "No connection" in txt or "未选连接" in txt  # unbound immediately, no tab switch needed


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

    page.locator("#kaBtn").click()
    page.wait_for_timeout(300)
    assert "up" in page.locator("#kaBadge").inner_text().lower() or "在线" in page.locator("#kaBadge").inner_text()


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
    # guard: middle-click on the only remaining tab is a no-op (same rule as the × glyph)
    page.locator('.tab[data-i="0"]').click(button="middle")
    page.wait_for_timeout(150)
    assert page.locator(".tab[data-i]").count() == 1


def test_tab_keyboard_shortcut_closes_active_tab(page):
    _select_testpg(page)
    page.locator("#tabAdd").click()
    page.wait_for_function("document.querySelectorAll('#tabs .tab[data-i]').length === 2")
    page.keyboard.press("Control+Shift+W")
    page.wait_for_function("document.querySelectorAll('#tabs .tab[data-i]').length === 1")
    # guard: closing the last remaining tab via the shortcut is a no-op
    page.keyboard.press("Control+Shift+W")
    page.wait_for_timeout(150)
    assert page.locator(".tab[data-i]").count() == 1


# ---------------------------------------------------------------------------
# 94. Query deep link: copy/open, auto-run, tab reuse, invalid target guard
# ---------------------------------------------------------------------------

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
    base = page.url.split("/app/")[0]
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
    page.goto(f"{base}/app/?{qs}", wait_until="networkidle")
    page.wait_for_selector('#grid td[data-v="42"]')
    assert page.locator(".tab[data-i]").count() == 1
    assert page.locator("#sql").input_value() == sql


def test_query_deeplink_invalid_env_shows_notice_and_skips_autorun(page_envset):
    page = page_envset
    queries = []
    page.on("request", lambda r: "/api/query" in r.url and queries.append(r.url))
    qs = urlencode({"db": "shop", "env": "ghost", "sql": "select 1 as bad_env"})
    base = page.url.split("/app/")[0]
    page.goto(f"{base}/app/?{qs}", wait_until="networkidle")
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
    ctx = browser.new_context(viewport={"width": 1280, "height": 900})
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
    ctx = browser.new_context(viewport={"width": 1280, "height": 900})
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
    ctx = browser.new_context(viewport={"width": 1280, "height": 900})
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
