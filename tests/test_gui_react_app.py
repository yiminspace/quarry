"""Browser + API checks for the React GUI at /app — the only frontend quarry
ships; `gui.py` is backend-only (http.server + /api/* + serving web_dist).

The general feature matrix lives in test_gui_browser.py /
test_gui_browser_features.py (the suite originally written against the
retired embedded GUI — the React app is a drop-in for its DOM and behavior).
This file keeps the packaging checks plus the React-only table-structure
browser (issue #11): double-click a sidebar table name to see its columns and
types without running anything.
"""

from __future__ import annotations

import re
import urllib.request
import zipfile

import pytest

from conftest import REPO, TEST_DB_URL, _running_gui, requires_browser, stub_events
from quarry import __version__
from test_gui_browser import _select_testpg

pytestmark = [requires_browser, pytest.mark.browser]


def _mk_page(browser, url):
    ctx = browser.new_context(viewport={"width": 1280, "height": 900})
    stub_events(ctx)
    pg = ctx.new_page()
    pg._console_errors = []
    pg.on("console", lambda m: m.type == "error" and pg._console_errors.append(m.text))
    pg.on("pageerror", lambda e: pg._console_errors.append(str(e)))
    pg.goto(url, wait_until="networkidle")
    return ctx, pg


def test_react_app_mounts_at_app_path(_pw_browser, tmp_path):
    """The React app is served at /app/ directly (not only via the / redirect)."""
    with _running_gui(tmp_path) as base:
        ctx, page = _mk_page(_pw_browser, f"{base}/app/")
        try:
            page.wait_for_selector(".brand")
            assert page.locator(".brand").inner_text() == "Quarry"
            assert page.title() == "Quarry"
        finally:
            ctx.close()


def _struct_rows(page) -> dict[str, str]:
    rows = page.locator("#structbox .cirow")
    return {
        rows.nth(i).locator(".civ").inner_text(): rows.nth(i).locator(".cik").inner_text()
        for i in range(rows.count())
    }


def test_table_structure_modal_shows_columns_and_types(page):
    """Issue #11: double-click a table name -> a modal lists its columns+types
    (no query is run, the editor keeps whatever the click generated)."""
    _select_testpg(page)
    page.locator('#tbl-panel .tname[data-t="customers"]').dblclick()
    page.wait_for_selector("#structbox .cirow")
    cells = _struct_rows(page)
    assert cells["id"] == "integer"
    assert cells["email"] == "text"
    assert cells["created_at"] == "timestamp with time zone"
    page.keyboard.press("Escape")
    page.wait_for_selector("#structbox", state="detached")


def test_table_structure_modal_switching_tables_replaces_columns(_pw_browser, tmp_path):
    """Opening a second table's structure must show ITS columns, not a stale
    or merged mix — even when the first table's /api/columns response is slow
    and lands after the second modal opened (latest-wins).

    The delay is injected as an in-page `fetch` override (init script) — a
    Python-side route handler that sleeps would serialize with the other
    intercepted requests and mask the race this test needs to create."""
    delay_columns_for_customers = """
    (() => {
        const origFetch = window.fetch;
        window.fetch = (input, init) => {
            const url = typeof input === "string" ? input : input.url;
            if (url.includes("/api/columns") && url.includes("table=customers")) {
                return new Promise((resolve) => {
                    setTimeout(() => resolve(origFetch(input, init)), 600);
                });
            }
            return origFetch(input, init);
        };
    })();
    """
    with _running_gui(tmp_path) as base:
        ctx = _pw_browser.new_context(viewport={"width": 1280, "height": 900})
        stub_events(ctx)
        page = ctx.new_page()
        try:
            page.add_init_script(delay_columns_for_customers)
            page.goto(base, wait_until="networkidle")
            _select_testpg(page)
            page.locator('#tbl-panel .tname[data-t="customers"]').dblclick()  # slow, in flight
            page.keyboard.press("Escape")
            page.locator('#tbl-panel .tname[data-t="orders"]').dblclick()     # fast
            page.wait_for_selector("#structbox .cirow")
            page.wait_for_timeout(800)            # let the stale customers response land
            text = page.locator("#structbox").inner_text()
            assert "amount" in text and "status" in text
            assert "email" not in text
        finally:
            ctx.close()


def test_table_structure_quoted_special_char_table(page, pg_exec):
    """A table name needing quoting (dash + space) must still show its real
    columns — regression: a character-stripping sanitizer used to match it
    against a mangled, non-existent identifier and render an empty schema."""
    pg_exec('DROP TABLE IF EXISTS "qy-review weird"')
    rc, _, err = pg_exec('CREATE TABLE "qy-review weird" (id serial PRIMARY KEY, note text)')
    assert rc == 0, err
    try:
        _select_testpg(page)
        sel = '#tbl-panel .tname[data-t="qy-review weird"]'
        page.wait_for_selector(sel)
        page.locator(sel).dblclick()
        page.wait_for_selector("#structbox .cirow")
        assert "note" in _struct_rows(page)
    finally:
        pg_exec('DROP TABLE IF EXISTS "qy-review weird"')


def test_stale_tables_response_does_not_overwrite(_pw_browser, tmp_path):
    """A slow /api/tables response for a connection the user has already
    switched AWAY from must never repaint the table panel — latest-wins on
    the connection axis.

    The delay is injected as an in-page `fetch` override (via an init script)
    rather than intercepted on the Python side: a Python route handler that
    sleeps can serialize with other concurrently-intercepted requests on the
    driver's dispatch thread, which defeats the very race this test creates.
    """
    extra = f'\n[testpg2]\nurl = "{TEST_DB_URL}"\nengine = "postgres"\nenv = "test"\n'
    delay_tables_for_testpg2 = """
    (() => {
        const origFetch = window.fetch;
        window.fetch = (input, init) => {
            const url = typeof input === "string" ? input : input.url;
            if (url.includes("/api/tables") && url.includes("db=testpg2")) {
                return new Promise((resolve) => {
                    setTimeout(() => {
                        resolve(new Response(JSON.stringify(
                            {engine: "postgres", tables: ["synthetic_stale_table"], capped: false}
                        ), {status: 200, headers: {"Content-Type": "application/json"}}));
                    }, 600);
                });
            }
            return origFetch(input, init);
        };
    })();
    """
    with _running_gui(tmp_path, extra_conn=extra) as base:
        ctx = _pw_browser.new_context(viewport={"width": 1280, "height": 900})
        stub_events(ctx)
        page = ctx.new_page()
        try:
            page.add_init_script(delay_tables_for_testpg2)
            page.goto(base, wait_until="networkidle")
            page.locator('.dbrow[data-db="testpg2"]').click()   # slow, in flight
            page.locator('.dbrow[data-db="testpg"]').click()    # fast, resolves first
            page.wait_for_selector('#tbl-panel .tname[data-t="customers"]')
            page.wait_for_timeout(800)              # let the stale testpg2 response land
            tables_text = page.locator("#tbl-panel").inner_text()
            assert "synthetic_stale_table" not in tables_text
            assert "customers" in tables_text
        finally:
            ctx.close()


@pytest.mark.integration
def test_api_version(gui_server):
    code, body = gui_server.get("/api/version")
    assert code == 200
    assert body == {"name": "Quarry", "version": __version__}


@pytest.mark.integration
def test_react_app_index_served(gui_server):
    with urllib.request.urlopen(gui_server.base + "/app/") as resp:
        html = resp.read().decode()
    assert resp.status == 200
    assert 'id="root"' in html


@pytest.mark.unit
def test_wheel_includes_web_dist(tmp_path):
    """Wheel index and every hashed asset it references must ship together."""
    import subprocess
    import sys

    pytest.importorskip("build")
    dist = tmp_path / "dist"
    subprocess.run(
        [sys.executable, "-m", "build", "--outdir", str(dist)],
        cwd=REPO,
        check=True,
        capture_output=True,
        text=True,
    )
    wheels = list(dist.glob("*.whl"))
    assert wheels, "expected a wheel in dist/"
    with zipfile.ZipFile(wheels[0]) as zf:
        names = set(zf.namelist())
        indexes = [n for n in names
                   if n.startswith("quarry/web_dist/") and n.endswith("index.html")]
        assert len(indexes) == 1
        index_name = indexes[0]
        html = zf.read(index_name).decode()

    asset_refs = set(re.findall(r'(?:src|href)="(/app/assets/[^"]+)"', html))
    assert asset_refs, "expected hashed JS/CSS references in the built index"
    web_dist_prefix = index_name.removesuffix("index.html")
    missing = {
        web_dist_prefix + ref.removeprefix("/app/")
        for ref in asset_refs
    } - names
    assert not missing, f"wheel is missing index-referenced assets: {sorted(missing)}"


@pytest.mark.unit
def test_wheel_includes_changelog(tmp_path):
    """Built wheel must ship CHANGELOG.md next to gui.py (build hook in
    hatch_build.py) — GET /api/changelog reads it at runtime for the What's
    New panel; without it, installed (non-editable) users would get an empty
    panel forever."""
    import subprocess
    import sys

    pytest.importorskip("build")
    dist = tmp_path / "dist"
    subprocess.run(
        [sys.executable, "-m", "build", "--outdir", str(dist)],
        cwd=REPO,
        check=True,
        capture_output=True,
        text=True,
    )
    wheels = list(dist.glob("*.whl"))
    assert wheels, "expected a wheel in dist/"
    with zipfile.ZipFile(wheels[0]) as zf:
        names = zf.namelist()
        content = zf.read("quarry/CHANGELOG.md").decode()
    assert "quarry/CHANGELOG.md" in names
    assert "## v" in content  # a real, non-empty changelog, not a placeholder


@pytest.mark.unit
def test_sdist_excludes_node_modules(tmp_path):
    """sdist must not ship npm install trees — Node is dev/CI-only."""
    import subprocess
    import sys
    import tarfile

    pytest.importorskip("build")
    dist = tmp_path / "dist"
    subprocess.run(
        [sys.executable, "-m", "build", "--outdir", str(dist)],
        cwd=REPO,
        check=True,
        capture_output=True,
        text=True,
    )
    sdists = list(dist.glob("*.tar.gz"))
    assert sdists, "expected an sdist in dist/"
    with tarfile.open(sdists[0], "r:gz") as tf:
        names = tf.getnames()
    assert not any("node_modules" in n for n in names)
    assert not any(n.endswith("tsconfig.tsbuildinfo") for n in names)
