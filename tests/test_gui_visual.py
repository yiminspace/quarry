"""Visual-parity pins for the React GUI (epic v2 requirement): the design
tokens must resolve to EXACTLY the retired embedded GUI's "Slate & Copper"
hex values, in both themes, on real rendered elements — asserted via
getComputedStyle so a repaint/regression is caught by CI, not by eyeballing
screenshots. Values below are the legacy INDEX_HTML <style> hexes verbatim.
"""

from __future__ import annotations

import pytest

from conftest import requires_browser
from test_gui_browser import _run_result, _select_testpg, _set_sql

pytestmark = [requires_browser, pytest.mark.browser]


def _style(page, selector: str, prop: str) -> str:
    return page.evaluate(
        "([sel, prop]) => getComputedStyle(document.querySelector(sel))[prop]",
        [selector, prop],
    )


def _r_btn(page) -> str:
    return page.evaluate(
        "getComputedStyle(document.documentElement).getPropertyValue('--r-btn').trim()"
    )


# legacy hexes -> the rgb() strings getComputedStyle returns
DARK = {
    "bg0": "rgb(14, 17, 22)",      # #0e1116
    "bg1": "rgb(28, 32, 40)",      # #1c2028
    "fg": "rgb(238, 240, 243)",    # #eef0f3
    "accent": "rgb(192, 130, 79)", # #c0824f
    "accent_ink": "rgb(36, 20, 7)",# #241407
    "ok": "rgb(143, 180, 140)",    # #8fb48c
    "ok_bg": "rgb(35, 47, 36)",    # #232f24
}
LIGHT = {
    "bg0": "rgb(244, 243, 238)",   # #f4f3ee
    "bg1": "rgb(255, 255, 255)",   # #ffffff
    "fg": "rgb(28, 28, 25)",       # #1c1c19
    "accent": "rgb(176, 106, 52)", # #b06a34
    "accent_ink": "rgb(255, 255, 255)",  # #ffffff
}


def test_default_theme_is_dark_with_legacy_palette(page):
    assert page.evaluate("document.documentElement.dataset.mode") == "dark"
    assert _style(page, "body", "backgroundColor") == DARK["bg0"]
    assert _style(page, "body", "color") == DARK["fg"]
    assert _style(page, "header", "backgroundColor") == DARK["bg1"]
    # primary Run button carries the copper accent + its ink color
    assert _style(page, "#runBtn", "backgroundColor") == DARK["accent"]
    assert _style(page, "#runBtn", "color") == DARK["accent_ink"]
    # read-only badge: ok-green on ok-bg
    assert _style(page, "#roBadge", "color") == DARK["ok"]
    assert _style(page, "#roBadge", "backgroundColor") == DARK["ok_bg"]


def test_light_theme_matches_legacy_palette(page):
    page.locator(".vg-switcher-mode").click()
    assert page.evaluate("document.documentElement.dataset.mode") == "light"
    assert _style(page, "body", "backgroundColor") == LIGHT["bg0"]
    assert _style(page, "body", "color") == LIGHT["fg"]
    assert _style(page, "header", "backgroundColor") == LIGHT["bg1"]
    assert _style(page, "#runBtn", "backgroundColor") == LIGHT["accent"]
    assert _style(page, "#runBtn", "color") == LIGHT["accent_ink"]


def _run_select_1(page):
    _select_testpg(page)
    _set_sql(page, "select 1 as v")
    page.locator("#runBtn").click()
    _run_result(page)
    page.locator("#dlSize").wait_for()


def test_status_bar_download_speed_reuse_existing_text_color(page):
    # issue #106: the new download-size/avg-speed status bar entries must not
    # introduce a new color — they carry no class/style of their own, same as
    # the pre-existing elapsed-time entry, so they inherit #status's token in
    # both themes instead of hardcoding one.
    _run_select_1(page)
    status_color = _style(page, "#status", "color")
    assert _style(page, "#dlSize", "color") == status_color
    assert _style(page, "#avgSpeed", "color") == status_color

    page.locator(".vg-switcher-mode").click()  # light theme
    light_status_color = _style(page, "#status", "color")
    assert _style(page, "#dlSize", "color") == light_status_color
    assert _style(page, "#avgSpeed", "color") == light_status_color


def test_large_result_session_badge_inherits_status_token(page):
    _select_testpg(page)
    _set_sql(page, "select repeat('x', 600000) as payload")
    page.locator("#runBtn").click()
    page.locator("#resultNotSaved").wait_for()
    assert _style(page, "#resultNotSaved", "color") == _style(page, "#status", "color")

    page.locator(".vg-switcher-mode").click()
    assert _style(page, "#resultNotSaved", "color") == _style(page, "#status", "color")


def test_typography_matches_legacy(page):
    # app-wide 14px sans stack; the editor runs on the mono stack
    assert _style(page, "body", "fontSize") == "14px"
    assert "-apple-system" in _style(page, "body", "fontFamily")
    sql_font = _style(page, "#sql", "fontFamily")
    assert "Menlo" in sql_font or "monospace" in sql_font


def test_icons_use_selfhosted_tabler_font(page):
    # icon glyphs render through the vendored tabler-icons webfont (no CDN)
    fam = _style(page, "#healthBtn .ti", "fontFamily")
    assert "tabler-icons" in fam
    loaded = page.evaluate("document.fonts.check('16px tabler-icons')")
    assert loaded, "tabler-icons webfont did not load"


def test_header_icon_controls_share_uniform_box(page):
    # Voyage keeps lang / theme-mode / palette-trigger buttons on one shared
    # `.vg-iconbtn` box spec; only their content differs.
    r_btn = _r_btn(page)
    selectors = (".vg-lang-switch", ".vg-switcher-mode", ".vg-switcher-trigger")
    for prop in ("height", "minWidth", "borderRadius"):
        values = {sel: _style(page, sel, prop) for sel in selectors}
        assert len(set(values.values())) == 1, (prop, values)
    assert _style(page, ".vg-lang-switch", "borderRadius") == r_btn


def test_header_iconbtn_radius_follows_style_axis(page):
    # Header icon buttons must track --r-btn across the style axis.
    try:
        for style in ("classic", "sharp", "soft"):
            page.evaluate(f"document.documentElement.setAttribute('data-style', {style!r})")
            assert _style(page, ".vg-switcher-trigger", "borderRadius") == _r_btn(page)
        page.evaluate("document.documentElement.setAttribute('data-style', 'sharp')")
        sharp = _style(page, ".vg-switcher-trigger", "borderRadius")
        page.evaluate("document.documentElement.setAttribute('data-style', 'soft')")
        soft = _style(page, ".vg-switcher-trigger", "borderRadius")
        assert sharp != soft
    finally:
        page.evaluate("document.documentElement.setAttribute('data-style', 'classic')")


def test_ciact_iconbtn_fixed_size_not_stretched_by_min_width(page):
    # `.ciact .iconbtn` only overrode `width` before, so the new `.vg-iconbtn`
    # `min-width: 26px` (voyage 0.7.0) stretched these smaller CI action
    # buttons back up. width/height must both stay 22px.
    _select_testpg(page)
    page.locator("#ciBtn").click()
    page.wait_for_selector("#ciEye")
    for sel in ("#ciEye", "#ciCopy"):
        assert _style(page, sel, "width") == "22px"
        assert _style(page, sel, "height") == "22px"


def test_header_topbar_has_no_query_toolbar_chrome_or_account_placeholder(page):
    assert page.locator("header .vg-topbar").count() == 1
    assert page.locator("header .vg-toolbar").count() == 0
    assert page.locator("header .vg-topbar-account").count() == 0
    assert _style(page, "header .vg-topbar", "padding") == "0px"
    assert _style(page, "header .vg-topbar", "borderBottomWidth") == "0px"
    assert _style(page, "header .vg-topbar", "backgroundColor") == "rgba(0, 0, 0, 0)"


def test_query_toolbar_keeps_its_own_chrome(page):
    # The query action row remains the card-toolbar shape after VoyageToolbar
    # moved the header arrangement onto its dedicated `.vg-topbar` class.
    selector = ".vg-toolbar.toolbar"
    assert _style(page, selector, "padding") == "9px 14px"
    assert _style(page, selector, "backgroundColor") == DARK["bg1"]
    assert _style(page, selector, "borderBottomWidth") == "1px"


def test_header_toolbar_dom_order_is_lang_mode_palette(page):
    # VoyageToolbar fixes the order (language -> mode -> palette) in its own
    # DOM structure, rather than leaving it up to the host's JSX.
    order = page.evaluate(
        """() => [...document.querySelectorAll(
            '.vg-topbar .vg-lang-switch, .vg-topbar .vg-switcher-mode, .vg-topbar .vg-switcher-trigger'
        )].map((el) => el.className)"""
    )
    assert len(order) == 3
    assert "vg-lang-switch" in order[0]
    assert "vg-switcher-mode" in order[1]
    assert "vg-switcher-trigger" in order[2]


def test_lang_switch_fixed_width_no_reflow(page):
    # --vg-lang-w keeps unequal-width glyphs ("中" vs "EN") in the same box,
    # so switching language cannot shove the mode/palette buttons sideways.
    en_width = page.evaluate("document.querySelector('.vg-lang-switch').getBoundingClientRect().width")
    mode_x = page.evaluate("document.querySelector('.vg-switcher-mode').getBoundingClientRect().x")
    trigger_x = page.evaluate("document.querySelector('.vg-switcher-trigger').getBoundingClientRect().x")

    page.locator(".vg-lang-switch").click()  # -> zh, reloads
    # optional chaining: mid-reload, #runLbl briefly doesn't exist yet, and
    # querySelector(...).textContent (without ?.) throws instead of just
    # evaluating falsy, which aborts wait_for_function's polling outright
    # (issue #94 CI flake) instead of retrying until the reload settles.
    page.wait_for_function("document.querySelector('#runLbl')?.textContent === '运行'")

    zh_width = page.evaluate("document.querySelector('.vg-lang-switch').getBoundingClientRect().width")
    assert zh_width == en_width
    assert page.evaluate("document.querySelector('.vg-switcher-mode').getBoundingClientRect().x") == mode_x
    assert page.evaluate("document.querySelector('.vg-switcher-trigger').getBoundingClientRect().x") == trigger_x


def test_header_toolbar_buttons_share_full_box_spec(page):
    # All three topbar buttons agree on the complete box spec pairwise.
    selectors = (".vg-lang-switch", ".vg-switcher-mode", ".vg-switcher-trigger")
    for prop in ("height", "width", "borderRadius", "boxSizing"):
        values = {sel: _style(page, sel, prop) for sel in selectors}
        assert len(set(values.values())) == 1, (prop, values)


def test_signal_theme_four_axes_colors_and_reload_persistence(page):
    page.locator(".vg-switcher-trigger").click()
    signal = page.get_by_role("menuitemradio", name="Signal")
    assert signal.is_visible()
    signal.click()

    axes = page.evaluate(
        """() => {
            const d = document.documentElement.dataset;
            return [d.theme, d.mode, d.style, d.tone];
        }"""
    )
    assert axes == ["signal", "dark", "classic", "quiet"]
    assert _style(page, "body", "backgroundColor") == "rgb(0, 0, 0)"
    assert _style(page, "header", "backgroundColor") == "rgb(0, 0, 0)"

    page.locator(".vg-switcher-mode").click()
    assert page.evaluate("document.documentElement.dataset.mode") == "light"
    assert _style(page, "body", "backgroundColor") == "rgb(255, 255, 255)"
    assert _style(page, "header", "backgroundColor") == "rgb(255, 255, 255)"

    page.reload()
    page.locator("#runBtn").wait_for()
    axes = page.evaluate(
        """() => {
            const d = document.documentElement.dataset;
            return [d.theme, d.mode, d.style, d.tone];
        }"""
    )
    assert axes == ["signal", "light", "classic", "quiet"]
    assert _style(page, "body", "backgroundColor") == "rgb(255, 255, 255)"
    assert _style(page, "header", "backgroundColor") == "rgb(255, 255, 255)"
    assert page._console_errors == []
