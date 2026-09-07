"""Playwright browser e2e for the Quarry GUI frontend (the React app under
web/, served at /app — `/` redirects there). Every test drives the *built*
bundle in a headless Chromium against the live `testpg` workspace, so run
`cd web && npm run build` after changing web/src.

The React app is a drop-in replacement for the retired embedded-JS GUI: same
DOM (ids/classes), same i18n strings, same localStorage keys — which is why
this suite, originally written against the embedded GUI, still pins its
behavior. Each of the 18 concerns is its own test so failures localize.
"""

from __future__ import annotations

import json

import pytest

from conftest import _running_gui, requires_browser

# every test here needs a live browser + DB
pytestmark = [requires_browser, pytest.mark.browser]


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _select_testpg(page):
    """Click the testpg connection row and wait for its table panel to render."""
    page.locator('.dbrow[data-db="testpg"]').click()
    page.wait_for_selector('#tbl-panel .tname[data-t="customers"]')


def _set_sql(page, sql: str):
    """Set the editor value the way a user would: focus, set, fire 'input'.

    The value is written through the native prototype setter — React tracks a
    controlled input's value on the instance, and a plain `ta.value = v`
    updates that tracker too, making the subsequent 'input' event look like a
    no-op change that onChange then ignores."""
    page.evaluate(
        """(v) => {
            const ta = document.querySelector('#sql');
            ta.focus();
            Object.getOwnPropertyDescriptor(
                HTMLTextAreaElement.prototype, 'value').set.call(ta, v);
            ta.dispatchEvent(new Event('input', {bubbles: true}));
        }""",
        sql,
    )


def _run_result(page):
    """Wait for a result grid (or an error) to appear after a run."""
    page.wait_for_selector('#grid table, #grid .err', timeout=15000)


# ---------------------------------------------------------------------------
# 1. Load
# ---------------------------------------------------------------------------

@requires_browser
@pytest.mark.browser
def test_load_shows_brand_and_readonly_badge(page):
    assert page.title() == "Quarry"
    assert page.locator(".brand").inner_text() == "Quarry"
    # read-only badge is present and mentions read-only (en) or 只读 (zh)
    ro = page.locator("#roBadge")
    ro.wait_for()
    txt = ro.inner_text()
    assert ("read-only" in txt) or ("只读" in txt)
    # sidebar renders the testpg connection row
    page.wait_for_selector('.dbrow[data-db="testpg"]')
    assert page.locator('.dbrow[data-db="testpg"]').count() == 1


# ---------------------------------------------------------------------------
# 2. Select connection -> table list + health dot
# ---------------------------------------------------------------------------

@requires_browser
@pytest.mark.browser
def test_select_connection_lists_tables_and_greens_dot(page):
    _select_testpg(page)
    # both seed tables show up
    page.wait_for_selector('#tbl-panel .tname[data-t="orders"]')
    assert page.locator('#tbl-panel .tname[data-t="customers"]').count() == 1
    assert page.locator('#tbl-panel .tname[data-t="orders"]').count() == 1
    # a successful /api/tables call calls setHealth(db,true) -> .dot.ok on the active row
    page.wait_for_selector('.dbrow.on[data-db="testpg"] .dot.ok')
    assert page.locator('.dbrow.on[data-db="testpg"] .dot.ok').count() == 1


# ---------------------------------------------------------------------------
# 3. Table -> grid (3 rows, typed columns, status)
# ---------------------------------------------------------------------------

@requires_browser
@pytest.mark.browser
def test_click_table_renders_grid_with_types_and_status(page):
    _select_testpg(page)
    page.locator('#tbl-panel .tname[data-t="customers"]').click()
    page.wait_for_selector("#grid table tbody tr")
    # first-page preview: no baked-in LIMIT (toolbar max-rows bounds the result)
    assert page.locator("#sql").input_value() == "select * from customers"
    # 3 data rows
    assert page.locator("#grid table tbody tr").count() == 3
    # typed header: id integer, name text
    head = page.locator("#grid table thead").inner_text()
    assert "id" in head and "integer" in head
    assert "name" in head and "text" in head
    # status shows the row count '3' and 'ms'
    status = page.locator("#status")
    status.wait_for(state="visible")
    stxt = status.inner_text()
    assert "3" in stxt
    assert "ms" in stxt


# ---------------------------------------------------------------------------
# 4. Custom SQL run (both run key + button)
# ---------------------------------------------------------------------------

@requires_browser
@pytest.mark.browser
def test_custom_sql_via_run_button(page):
    _select_testpg(page)
    _set_sql(page, "select 1 as one")
    page.locator("#runBtn").click()
    page.wait_for_selector("#grid table tbody tr")
    assert page.locator("#grid table tbody tr").count() == 1
    # the single cell (besides the rownum) holds 1
    cell = page.locator("#grid table tbody tr td:not(.rownum)").first
    assert cell.inner_text().strip() == "1"


@requires_browser
@pytest.mark.browser
def test_custom_sql_via_run_keyboard(page):
    _select_testpg(page)
    _set_sql(page, "select 2 as two")
    ta = page.locator("#sql")
    ta.focus()
    # the JS binds (metaKey||ctrlKey)+Enter -> run(); try both so it works on any platform
    ta.press("Meta+Enter")
    try:
        page.wait_for_selector("#grid table tbody tr", timeout=4000)
    except Exception:
        ta.focus()
        ta.press("Control+Enter")
        page.wait_for_selector("#grid table tbody tr", timeout=8000)
    assert page.locator("#grid table tbody tr").count() == 1
    cell = page.locator("#grid table tbody tr td:not(.rownum)").first
    assert cell.inner_text().strip() == "2"


# ---------------------------------------------------------------------------
# 5. Write blocked
# ---------------------------------------------------------------------------

@requires_browser
@pytest.mark.browser
def test_write_is_blocked_with_readonly_error(page):
    _select_testpg(page)
    _set_sql(page, "delete from customers")
    page.locator("#runBtn").click()
    # showErr renders '.err' inside the grid — never a table
    err = page.locator("#grid .err")
    err.wait_for(timeout=15000)
    etxt = err.inner_text().lower()
    assert "read-only" in etxt or "write" in etxt
    # no success grid rendered
    assert page.locator("#grid table").count() == 0


# ---------------------------------------------------------------------------
# 6. EXPLAIN modal
# ---------------------------------------------------------------------------

@requires_browser
@pytest.mark.browser
def test_explain_opens_plan_modal_and_escape_closes(page):
    _select_testpg(page)
    _set_sql(page, "select * from customers")
    page.locator("#expBtn").click()
    modal = page.locator(".modal")
    modal.wait_for(timeout=15000)
    mtxt = modal.inner_text()
    # single-column postgres plan renders in a modal whose header says EXPLAIN and
    # whose <pre> holds the plan body (a Seq Scan cost line for this table scan)
    assert "EXPLAIN" in mtxt
    assert "cost=" in mtxt or "Scan" in mtxt
    page.keyboard.press("Escape")
    modal.wait_for(state="detached", timeout=5000)
    assert page.locator(".modal").count() == 0


# ---------------------------------------------------------------------------
# 7. Format button
# ---------------------------------------------------------------------------

@requires_browser
@pytest.mark.browser
def test_format_button_uppercases_and_newlines(page):
    _select_testpg(page)
    _set_sql(page, "select   *   from customers")
    before = page.locator("#sql").input_value()
    page.locator("#fmtBtn").click()
    # setSQL updates the textarea synchronously; read it back
    after = page.locator("#sql").input_value()
    assert after != before
    assert "SELECT" in after
    # FROM is uppercased and pushed onto its own line
    assert "\nFROM" in after


# ---------------------------------------------------------------------------
# 8. Export buttons don't throw
# ---------------------------------------------------------------------------

@requires_browser
@pytest.mark.browser
def test_csv_export_triggers_download(page):
    _select_testpg(page)
    page.locator('#tbl-panel .tname[data-t="customers"]').click()
    page.wait_for_selector("#grid table tbody tr")
    with page.expect_download() as dl:
        page.locator("#csvBtn").click()
    download = dl.value
    assert download.suggested_filename == "quarry-testpg.csv"
    assert not page._console_errors


@requires_browser
@pytest.mark.browser
def test_json_export_triggers_download(page):
    _select_testpg(page)
    page.locator('#tbl-panel .tname[data-t="customers"]').click()
    page.wait_for_selector("#grid table tbody tr")
    with page.expect_download() as dl:
        page.locator("#jsonBtn").click()
    download = dl.value
    assert download.suggested_filename == "quarry-testpg.json"
    assert not page._console_errors


# ---------------------------------------------------------------------------
# 9. Theme toggle
# ---------------------------------------------------------------------------

@requires_browser
@pytest.mark.browser
def test_theme_toggle_flips_data_theme(page):
    before = page.evaluate("document.documentElement.dataset.mode")
    page.locator(".vg-switcher-mode").click()
    after = page.evaluate("document.documentElement.dataset.mode")
    assert {before, after} == {"dark", "light"}
    # toggling again restores
    page.locator(".vg-switcher-mode").click()
    assert page.evaluate("document.documentElement.dataset.mode") == before


# ---------------------------------------------------------------------------
# 10. Language toggle (reloads, switches chrome text)
# ---------------------------------------------------------------------------

@requires_browser
@pytest.mark.browser
def test_language_toggle_switches_run_label(page):
    # default lang is 'en' -> Run label; the lang switcher shows the current locale 'EN'
    run_label = page.locator("#runLbl")
    run_label.wait_for()
    assert run_label.inner_text() == "Run"
    assert page.locator(".vg-lang-switch").inner_text() == "EN"
    # clicking reloads into zh
    page.locator(".vg-lang-switch").click()
    page.wait_for_load_state("networkidle")
    page.wait_for_selector("#runLbl")
    assert page.locator("#runLbl").inner_text() == "运行"
    assert page.locator(".vg-lang-switch").inner_text() == "中"
    # toggle back to en so localStorage state is clean
    page.locator(".vg-lang-switch").click()
    page.wait_for_load_state("networkidle")
    page.wait_for_selector("#runLbl")
    assert page.locator("#runLbl").inner_text() == "Run"


# ---------------------------------------------------------------------------
# 11. Tabs
# ---------------------------------------------------------------------------

@requires_browser
@pytest.mark.browser
def test_tabs_add_switch_restore_and_close(page):
    _select_testpg(page)
    _set_sql(page, "select 111 as a")
    # add a new tab
    page.locator("#tabAdd").click()
    page.wait_for_function("document.querySelectorAll('#tabs .tab[data-i]').length === 2")
    # new tab is active + empty; type different SQL
    _set_sql(page, "select 222 as b")
    assert page.locator("#sql").input_value() == "select 222 as b"
    # switch back to the first tab -> its SQL is restored
    page.locator('#tabs .tab[data-i="0"]').click()
    page.wait_for_function(
        "document.querySelector('#sql').value === 'select 111 as a'"
    )
    assert page.locator("#sql").input_value() == "select 111 as a"
    # close the added (second) tab via its × — click the close glyph on tab index 1
    page.locator('#tabs .tab[data-i="1"] .x').click()
    page.wait_for_function("document.querySelectorAll('#tabs .tab[data-i]').length === 1")
    assert page.locator("#tabs .tab[data-i]").count() == 1


# ---------------------------------------------------------------------------
# 12. Sort
# ---------------------------------------------------------------------------

@requires_browser
@pytest.mark.browser
def test_sort_column_toggles_arrow_and_reorders(page):
    _select_testpg(page)
    # deterministic multi-row set to sort
    _set_sql(page, "select id, name from customers order by id")
    page.locator("#runBtn").click()
    page.wait_for_selector("#grid table tbody tr")

    def first_id():
        return page.locator(
            "#grid table tbody tr:first-child td:not(.rownum)"
        ).first.inner_text().strip()

    top_before = first_id()
    # click the 'id' header (data-i=0) to sort; then again to reverse
    id_header = page.locator('#grid th[data-i="0"]')
    id_header.click()
    # arrow indicator appears
    page.wait_for_selector('#grid th[data-i="0"] .ar')
    assert page.locator('#grid th[data-i="0"] .ar').count() == 1
    # click again -> descending; the top id must change from the ascending top
    id_header.click()
    page.wait_for_function(
        """(prev) => {
            const c = document.querySelector('#grid table tbody tr:first-child td:not(.rownum)');
            return c && c.textContent.trim() !== prev;
        }""",
        arg=top_before,
    )
    assert first_id() != top_before


# ---------------------------------------------------------------------------
# 13. Row detail modal
# ---------------------------------------------------------------------------

@requires_browser
@pytest.mark.browser
def test_rownum_click_opens_row_detail_modal(page):
    _select_testpg(page)
    page.locator('#tbl-panel .tname[data-t="customers"]').click()
    page.wait_for_selector("#grid table tbody tr")
    page.locator("#grid td.rownum").first.click()
    modal = page.locator(".modal")
    modal.wait_for(timeout=5000)
    mtxt = modal.inner_text()
    # the detail table lists column names as field labels
    assert "email" in mtxt and "name" in mtxt
    page.keyboard.press("Escape")
    modal.wait_for(state="detached", timeout=5000)
    assert page.locator(".modal").count() == 0


# ---------------------------------------------------------------------------
# 14. Cell dblclick (copy short / modal long) — must not error
# ---------------------------------------------------------------------------

@requires_browser
@pytest.mark.browser
def test_cell_doubleclick_no_error(page):
    # grant clipboard write so copy()'s navigator.clipboard.writeText doesn't raise
    # a headless permission error (that path is what a short cell dblclick triggers).
    page.context.grant_permissions(["clipboard-read", "clipboard-write"])
    _select_testpg(page)
    page.locator('#tbl-panel .tname[data-t="customers"]').click()
    page.wait_for_selector("#grid table tbody tr")
    # A short cell (the 'name' column, <=60 chars, not [{-prefixed) hits the copy()
    # branch of ondblclick; a long/JSON cell would open a modal. Either must not raise.
    name_cell = page.locator("#grid table tbody tr:first-child td:not(.rownum)").nth(1)
    name_cell.dblclick()
    # copy() shows a toast; the modal branch shows '.modal'. Assert one of them, no error.
    page.wait_for_selector("#toast, .modal", timeout=5000)
    assert not page._console_errors


# ---------------------------------------------------------------------------
# 15. History
# ---------------------------------------------------------------------------

@requires_browser
@pytest.mark.browser
def test_history_lists_runs_and_reloads_into_editor(page):
    _select_testpg(page)
    # run two distinct queries so both enter history
    _set_sql(page, "select 7 as seven")
    page.locator("#runBtn").click()
    _run_result(page)
    _set_sql(page, "select 8 as eight")
    page.locator("#runBtn").click()
    _run_result(page)
    # open history modal
    page.locator("#histBtn").click()
    modal = page.locator(".modal")
    modal.wait_for(timeout=5000)
    items = page.locator(".modal .hitem")
    items.first.wait_for()
    assert items.count() >= 2
    mtxt = modal.inner_text()
    assert "select 7 as seven" in mtxt and "select 8 as eight" in mtxt
    # click the older entry (select 7 ...) to load it back into the editor.
    page.locator(".modal .hitem", has_text="select 7 as seven").first.click()
    page.wait_for_function(
        "document.querySelector('#sql').value === 'select 7 as seven'"
    )
    assert page.locator("#sql").input_value() == "select 7 as seven"


# ---------------------------------------------------------------------------
# 16. Saved query with params (needs a workspace seeded with a param query)
# ---------------------------------------------------------------------------

@pytest.fixture()
def page_saved(_pw_browser, tmp_path):
    q = (
        "-- @name: cust-by-id\n-- @db: testpg\n-- @param: id (int, required)\n"
        "SELECT * FROM customers WHERE id = :id\n"
    )
    with _running_gui(tmp_path, seed_queries={"cust-by-id": q}) as url:
        ctx = _pw_browser.new_context(viewport={"width": 1280, "height": 900})
        from conftest import stub_cdn, stub_events
        stub_cdn(ctx)
        stub_events(ctx)
        pg = ctx.new_page()
        pg._console_errors = []
        pg.on("console", lambda m: m.type == "error" and pg._console_errors.append(m.text))
        pg.on("pageerror", lambda e: pg._console_errors.append(str(e)))
        pg.goto(url, wait_until="networkidle")
        try:
            yield pg
        finally:
            ctx.close()


@requires_browser
@pytest.mark.browser
def test_saved_query_param_modal_runs_and_returns_one_row(page_saved):
    page = page_saved
    # the saved query shows in the sidebar under "saved queries"
    page.wait_for_selector('.qname[data-q="cust-by-id"]')
    page.locator('.qname[data-q="cust-by-id"]').click()
    # a param modal opens (query has a required :id param)
    modal = page.locator(".modal")
    modal.wait_for(timeout=5000)
    # fill id=1 and click Run
    page.locator('.modal input.pf[data-p="id"]').fill("1")
    page.locator("#pgo").click()
    modal.wait_for(state="detached", timeout=10000)
    page.wait_for_selector("#grid table tbody tr")
    assert page.locator("#grid table tbody tr").count() == 1


# ---------------------------------------------------------------------------
# 17. Autocomplete
# ---------------------------------------------------------------------------

@requires_browser
@pytest.mark.browser
def test_autocomplete_keyword_and_table(page):
    _select_testpg(page)
    ta = page.locator("#sql")
    # clear and type 'sele' char-by-char so the suggestion update fires on real input
    _set_sql(page, "")
    ta.focus()
    ta.type("sele")
    # the .acbox popup appears with a SELECT keyword item
    page.wait_for_selector(".acbox", state="visible", timeout=5000)
    page.wait_for_function(
        "[...document.querySelectorAll('.acbox .acitem')].some(e => e.textContent.includes('SELECT'))"
    )
    assert page.locator(".acbox").is_visible()
    # Escape closes it
    ta.press("Escape")
    page.wait_for_function(
        "getComputedStyle(document.querySelector('.acbox')).display === 'none'"
    )
    # now type a from-clause fragment -> a 'customers' table suggestion
    _set_sql(page, "")
    ta.focus()
    ta.type("select * from cus")
    page.wait_for_selector(".acbox", state="visible", timeout=5000)
    page.wait_for_function(
        "[...document.querySelectorAll('.acbox .acitem')].some(e => e.textContent.includes('customers'))"
    )
    assert page.locator(".acbox .acitem", has_text="customers").count() >= 1


# ---------------------------------------------------------------------------
# 18. Console cleanliness after a normal run flow
# ---------------------------------------------------------------------------

@requires_browser
@pytest.mark.browser
def test_no_console_errors_after_normal_flow(page):
    _select_testpg(page)
    page.locator('#tbl-panel .tname[data-t="customers"]').click()
    page.wait_for_selector("#grid table tbody tr")
    _set_sql(page, "select 1 as one")
    page.locator("#runBtn").click()
    _run_result(page)
    # give any late console events a tick to land
    page.wait_for_timeout(200)
    assert page._console_errors == [], f"console errors: {page._console_errors}"
