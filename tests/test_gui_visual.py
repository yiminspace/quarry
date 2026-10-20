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


def test_query_sidebar_tokens(page):
    page.route('**/api/queries', lambda route: route.fulfill(json=[{
        'name': 'sample', 'db': 'testpg', 'desc': '查看客户',
        'sql': 'SELECT * FROM customers', 'params': []}]))
    page.reload(wait_until='networkidle')
    _select_testpg(page)
    for mode in ('dark', 'light'):
        if page.evaluate('document.documentElement.dataset.mode') != mode:
            page.locator('.vg-switcher-mode').click()
        button = page.locator('[data-saved-ws]').first
        page.mouse.move(1000, 800)
        assert button.evaluate("el => { const probe = document.createElement('span'); probe.style.color = 'var(--fg3)'; el.append(probe); const match = getComputedStyle(el).color === getComputedStyle(probe).color; probe.remove(); return match; }")


def test_sidebar_compact_collapse_indent_and_stable_gutter(page, tmp_path):
    page.route('**/api/queries', lambda route: route.fulfill(json=[{
        'name': 'sample', 'db': 'testpg', 'desc': '查看客户',
        'sql': 'SELECT * FROM customers', 'params': []}]))
    page.reload(wait_until='networkidle')
    _select_testpg(page)
    collapse = page.get_by_role('button', name='Collapse all', exact=True)
    assert page.locator('.sidebar-searchbar .collapse-all').count() == 1
    assert page.locator('.qhead .collapse-all').count() == 0
    assert collapse.is_visible()
    heading_icon = page.locator('[data-saved-ws] .ti').first.bounding_box()
    source = page.locator('.query-index-children .vg-qsrc').first.evaluate('el => { const range = document.createRange(); range.selectNodeContents(el); return { x: range.getBoundingClientRect().x }; }')
    child_icon = page.locator('.query-index-children .query-item .ti').first.bounding_box()
    assert source['x'] > heading_icon['x']
    assert child_icon['x'] > source['x']
    assert page.locator('#side').evaluate('el => getComputedStyle(el).scrollbarGutter') == 'stable'
    widths = page.locator('#side').evaluate('''el => {
      const row = el.querySelector('.dbrow');
      el.style.height = '2000px'; const before = row.getBoundingClientRect().width;
      el.style.height = '100px'; const after = row.getBoundingClientRect().width;
      el.style.height = ''; return [before, after];
    }''')
    assert widths[0] == widths[1]
    page.screenshot(path=str(tmp_path / 'compact-sidebar.png'))


def test_default_theme_is_dark_with_legacy_palette(page):
    assert page.evaluate("document.documentElement.dataset.mode") == "dark"
    assert _style(page, "body", "backgroundColor") == DARK["bg0"]
    assert _style(page, "body", "color") == DARK["fg"]
    assert _style(page, "header", "backgroundColor") == DARK["bg1"]
    # Run stays quieter than the active document until hover.
    assert _style(page, ".run-split", "backgroundColor") == DARK["bg1"]
    assert _style(page, "#runBtn", "backgroundColor") == "rgba(0, 0, 0, 0)"
    assert _style(page, "#runBtn", "color") == DARK["accent"]


def test_light_theme_matches_legacy_palette(page):
    page.locator(".vg-switcher-mode").click()
    assert page.evaluate("document.documentElement.dataset.mode") == "light"
    assert _style(page, "body", "backgroundColor") == LIGHT["bg0"]
    assert _style(page, "body", "color") == LIGHT["fg"]
    assert _style(page, "header", "backgroundColor") == LIGHT["bg1"]
    assert _style(page, ".run-split", "backgroundColor") == LIGHT["bg1"]
    assert _style(page, "#runBtn", "backgroundColor") == "rgba(0, 0, 0, 0)"
    assert _style(page, "#runBtn", "color") == LIGHT["accent"]


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
    assert _style(page, selector, "padding") == "8px 14px"
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


def _token_color(page, name: str) -> str:
    return page.evaluate(
        """(name) => {
          const probe = document.createElement('span');
          probe.style.color = `var(${name})`;
          document.body.appendChild(probe);
          const value = getComputedStyle(probe).color;
          probe.remove();
          return value;
        }""",
        name,
    )


def _token_bg(page, name: str) -> str:
    return page.evaluate(
        """(name) => {
          const probe = document.createElement('span');
          probe.style.backgroundColor = `var(${name})`;
          document.body.appendChild(probe);
          const value = getComputedStyle(probe).backgroundColor;
          probe.remove();
          return value;
        }""",
        name,
    )


@pytest.mark.parametrize('theme', ['slate', 'signal'])
@pytest.mark.parametrize('mode', ['dark', 'light'])
@pytest.mark.parametrize('lang', ['en', 'zh'])
def test_production_context_tokens(page, theme, mode, lang):
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
    if lang == 'zh':
        page.locator('.vg-lang-switch').click()
        page.wait_for_function("document.querySelector('#runLbl')?.textContent === '运行'")
        page.wait_for_function("document.querySelector('#runModeBtn')?.disabled === false")
    page.evaluate('''([theme, mode]) => {
        document.documentElement.dataset.theme = theme;
        document.documentElement.dataset.mode = mode;
    }''', [theme, mode])
    page.mouse.move(1000, 800)
    assert page.locator('.app-shell').get_attribute('data-production') == 'true'
    # Production borrows the existing copper and muted text tones, rather than
    # the destructive-action red. Pin the resolved blends in every theme.
    for token, blend in [
        ('--production-accent', 'color-mix(in srgb,var(--accent) 65%,var(--fg2))'),
        ('--production-fg', 'color-mix(in srgb,var(--accent) 25%,var(--fg2))'),
    ]:
        assert page.evaluate('''([token, blend]) => {
            const expected = document.createElement('span');
            const actual = document.createElement('span');
            expected.style.color = blend;
            actual.style.color = `var(${token})`;
            document.body.append(expected, actual);
            const equal = getComputedStyle(expected).color === getComputedStyle(actual).color;
            expected.remove(); actual.remove(); return equal;
        }''', [token, blend])
    for selector, prop, token in [
        ('header', 'backgroundColor', '--surf-1'),
        ('.qhead', 'backgroundColor', '--surf-1'),
        ('.qhead', 'borderBottomColor', '--line'),
        ('.edwrap', 'backgroundColor', '--production-surface'),
        ('.dbrow.on', 'backgroundColor', '--production-selection'),
        ('.dbrow.on', 'color', '--fg'),
        ('#prodBadge', 'color', '--production-fg'),
        ('#prodBadge', 'backgroundColor', '--production-selection'),
        ('#runBtn', 'color', '--production-fg'),
        ('.run-split', 'backgroundColor', '--surf-1'),
        ('.run-split', 'borderTopColor', '--line'),
    ]:
        assert _style(page, selector, prop) == _token_color(page, token), (theme, mode, selector, prop)
    assert page.locator('.dbrow.on').evaluate('''el =>
        getComputedStyle(el, '::before').backgroundColor
    ''') == _token_color(page, '--production-accent')
    assert _style(page, 'header', 'boxShadow') == 'none'
    assert _style(page, '.edwrap', 'boxShadow') == 'none'
    assert page.locator('#tabs .on').evaluate('''el =>
        getComputedStyle(el, '::after').display
    ''') != 'none'
    assert page.locator('#tabs .on').evaluate('''el =>
        getComputedStyle(el, '::after').backgroundColor
    ''') == _token_color(page, '--production-accent')
    assert page.locator('#tabs .on').evaluate('''el =>
        getComputedStyle(el, '::after').height
    ''') == '2px'
    assert page.locator('.tab-navigation .tab-context #prodBadge').count() == 1
    assert page.locator('header #prodBadge, .qhead #prodBadge, .edwrap #prodBadge').count() == 0
    assert _style(page, '.tab-context', 'width') == '104px'
    assert _style(page, '#prodBadge', 'borderTopWidth') == '0px'
    assert _style(page, '#prodBadge', 'fontSize') == '11px'
    assert _style(page, '#prodBadge', 'fontWeight') == '500'
    assert _style(page, '#runBtn', 'borderTopWidth') == '0px'
    assert _style(page, '#runBtn', 'backgroundColor') == 'rgba(0, 0, 0, 0)'
    slot = page.locator('.tab-context').bounding_box()
    badge = page.locator('#prodBadge').bounding_box()
    assert slot['x'] <= badge['x'] and badge['x'] + badge['width'] <= slot['x'] + slot['width']
    assert slot['y'] <= badge['y'] and badge['y'] + badge['height'] <= slot['y'] + slot['height']

    # Resolve mixed CSS colors through the browser's sRGB canvas and compare
    # text against its actual opaque surface, including the compact context label.
    for selector in ('#prodBadge', '#runBtn'):
        contrast = page.locator(selector).evaluate('''el => {
            const ctx = document.createElement('canvas').getContext('2d');
            function rgba(color) {
                ctx.clearRect(0, 0, 1, 1); ctx.fillStyle = color;
                ctx.fillRect(0, 0, 1, 1); return [...ctx.getImageData(0, 0, 1, 1).data];
            }
            function luminance(color) {
                const c = color.slice(0, 3).map(v => v / 255).map(v =>
                    v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4);
                return c[0] * 0.2126 + c[1] * 0.7152 + c[2] * 0.0722;
            }
            const fg = rgba(getComputedStyle(el).color);
            let ancestor = el, bg;
            do { bg = rgba(getComputedStyle(ancestor).backgroundColor); ancestor = ancestor.parentElement; }
            while (bg[3] === 0 && ancestor);
            if (fg[3] !== 255 || bg[3] !== 255) throw new Error('Expected opaque text and surface');
            const a = luminance(fg), b = luminance(bg);
            return (Math.max(a, b) + .05) / (Math.min(a, b) + .05);
        }''')
        assert contrast >= 4.5, (theme, mode, selector, contrast)


@pytest.mark.parametrize('theme', ['slate', 'signal'])
@pytest.mark.parametrize('mode', ['dark', 'light'])
@pytest.mark.parametrize('lang', ['en', 'zh'])
def test_run_mode_menu_tokens_and_geometry(page, theme, mode, lang):
    _select_testpg(page)
    if lang == 'zh':
        page.locator('.vg-lang-switch').click()
        page.wait_for_function("document.querySelector('#runLbl')?.textContent === '运行'")
        page.wait_for_function("document.querySelector('#runModeBtn')?.disabled === false")
    page.evaluate('''([theme, mode]) => {
        document.documentElement.dataset.theme = theme;
        document.documentElement.dataset.mode = mode;
    }''', [theme, mode])
    mode_button = page.locator('#runModeBtn')
    assert mode_button.is_enabled()
    assert page.locator('#autoRunToggle').count() == 0
    assert page.locator('.run-controls > .run-split #runBtn').count() == 1
    assert page.locator('.run-controls > .run-split #runModeBtn').count() == 1
    assert _style(page, '.run-controls', 'position') == 'relative'
    assert _style(page, '.run-split', 'height') == '30px'
    assert _style(page, '.run-split', 'borderTopWidth') == '1px'
    assert _style(page, '.run-split', 'borderRadius') == '6px'
    assert _style(page, '#runBtn', 'borderTopWidth') == '0px'
    assert _style(page, '#runBtn', 'backgroundColor') == 'rgba(0, 0, 0, 0)'
    assert _style(page, '#runModeBtn', 'borderLeftWidth') == '1px'
    assert _style(page, '#runModeBtn', 'width') == '86px'
    assert _style(page, '.tab-context', 'width') == '104px'
    assert page.locator('.tab-context').inner_text() == ''
    geometry = None
    for enabled in (False, True):
        selected_id = '#runModeAuto' if enabled else '#runModeManual'
        other_id = '#runModeManual' if enabled else '#runModeAuto'
        mode_button.click()
        page.locator(selected_id).click()
        assert page.locator('#runModeMenu').is_hidden()
        page.mouse.move(1000, 800)
        current_geometry = {
            'group': page.locator('.run-split').bounding_box(),
            'run': page.locator('#runBtn').bounding_box(),
            'mode': mode_button.bounding_box(),
            'format': page.locator('#fmtBtn').bounding_box(),
        }
        if geometry is None:
            geometry = current_geometry
        else:
            assert current_geometry == geometry, (theme, mode, lang, current_geometry, geometry)
        for selector, prop, token in [
            ('.run-split', 'backgroundColor', '--surf-1'),
            ('.run-split', 'borderTopColor', '--line'),
            ('#runBtn', 'color', '--acc'),
            ('#runModeBtn', 'color', '--acc' if enabled else '--fg2'),
            ('#runModeBtn', 'borderLeftColor', '--line'),
        ]:
            assert _style(page, selector, prop) == _token_color(page, token), (
                theme, mode, enabled, selector, prop,
            )
        mode_button.click()
        page.mouse.move(1000, 800)
        assert page.locator(selected_id).get_attribute('role') == 'menuitemradio'
        assert page.locator(other_id).get_attribute('role') == 'menuitemradio'
        assert page.locator(selected_id).get_attribute('aria-checked') == 'true'
        assert page.locator(other_id).get_attribute('aria-checked') == 'false'
        for selector, prop, token in [
            ('#runModeMenu', 'backgroundColor', '--surf-1'),
            ('#runModeMenu', 'borderTopColor', '--line'),
            (selected_id, 'backgroundColor', '--surf-2'),
            (selected_id, 'color', '--fg'),
            (selected_id + ' .ti-check', 'color', '--acc'),
        ]:
            assert _style(page, selector, prop) == _token_color(page, token), (
                theme, mode, enabled, selector, prop,
            )
        mode_button.click()


@pytest.mark.parametrize('lang', ['en', 'zh'])
@pytest.mark.parametrize('mode', ['dark', 'light'])
def test_row_limit_selector_geometry(page, lang, mode):
    _select_testpg(page)
    if lang == 'zh':
        with page.expect_navigation(wait_until='networkidle'):
            page.locator('.vg-lang-switch').click()
        page.wait_for_function("document.querySelector('#runLbl')?.textContent === '运行'")
    page.evaluate('(mode) => document.documentElement.dataset.mode = mode', mode)
    arrow = page.locator('.row-limit > .ti-chevron-down')
    assert arrow.count() == 1
    assert arrow.get_attribute('aria-hidden') == 'true'
    assert _style(page, '#maxRows', 'appearance') == 'none'
    assert _style(page, '.row-limit > .ti-chevron-down', 'pointerEvents') == 'none'
    assert _style(page, '.row-limit > .ti-chevron-down', 'color') == _token_color(page, '--fg2')
    width = page.locator('#maxRows').bounding_box()['width']
    for value in ('100', '5000'):
        page.select_option('#maxRows', value)
        layout = page.locator('.row-limit').evaluate('''el => {
            const select = el.querySelector('select');
            const arrow = el.querySelector('.ti-chevron-down');
            const rect = select.getBoundingClientRect();
            const icon = arrow.getBoundingClientRect();
            const style = getComputedStyle(select);
            const ctx = document.createElement('canvas').getContext('2d');
            ctx.font = `${style.fontWeight} ${style.fontSize} ${style.fontFamily}`;
            const text = ctx.measureText(select.selectedOptions[0].textContent.trim()).width;
            const textRight = rect.x + parseFloat(style.borderLeftWidth) + parseFloat(style.paddingLeft) + text;
            return {width: rect.width, gap: icon.x - textRight,
                    rightInset: rect.right - icon.right,
                    centerOffset: Math.abs((rect.top + rect.bottom - icon.top - icon.bottom) / 2)};
        }''')
        assert layout['width'] == width
        assert layout['gap'] >= 4, (lang, mode, value, layout)
        assert layout['rightInset'] >= 8, (lang, mode, value, layout)
        assert layout['centerOffset'] <= 0.5, (lang, mode, value, layout)


def test_engine_tag_differs_from_workspace_origin(page):
    _select_testpg(page)
    page.locator(".dbrow small.vg-engine-tag").wait_for()
    engine = page.locator(".dbrow small.vg-engine-tag").first
    assert _style(page, ".dbrow small.vg-engine-tag", "color") == _token_color(page, "--fg3")
    assert _style(page, ".dbrow small.vg-engine-tag", "backgroundColor") in ("rgba(0, 0, 0, 0)", "transparent")
    fam = engine.evaluate("el => getComputedStyle(el).fontFamily")
    assert "monospace" in fam.lower() or "mono" in fam.lower() or "Menlo" in fam
    origin = page.locator(".vg-ws-note")
    if origin.count():
        assert _style(page, ".vg-ws-note", "color") == _token_color(page, "--fg3")
        origin_fam = origin.first.evaluate("el => getComputedStyle(el).fontFamily")
        assert fam != origin_fam


def test_table_filter_uses_surface_tokens(page):
    _select_testpg(page)
    page.locator(".sidebar-searchbar .tsearch").wait_for()
    assert _style(page, ".vg-tfilter", "backgroundColor") == _token_bg(page, "--bg2")
    assert _style(page, ".sidebar-searchbar .tsearch", "backgroundColor") in ("rgba(0, 0, 0, 0)", "transparent")
    assert page.locator(".vg-tfilter .ti-search").count() == 1
    assert page.locator(".dbrow.on .treload").count() == 1


@pytest.mark.parametrize('mode', ['dark', 'light'])
def test_database_refresh_reveals_without_layout_shift(page, mode, tmp_path):
    _select_testpg(page)
    if page.evaluate('document.documentElement.dataset.mode') != mode:
        page.locator('.vg-switcher-mode').click()
    row = page.locator('.dbrow.on')
    button = row.locator('.treload')
    page.mouse.move(1000, 800)
    before = row.bounding_box()
    assert button.evaluate('el => getComputedStyle(el).opacity') == '0'
    assert page.locator('#tbl-panel .trow').count() == 0
    row.hover()
    assert button.evaluate('el => getComputedStyle(el).opacity') == '1'
    assert row.bounding_box() == before
    page.mouse.move(1000, 800)
    button.focus()
    assert button.evaluate('el => getComputedStyle(el).opacity') == '1'
    assert button.get_attribute('title')
    assert button.evaluate('el => getComputedStyle(el).color') == _token_color(page, '--fg3')
    page.screenshot(path=str(tmp_path / 'inline-refresh.png'))


@pytest.mark.parametrize('mode', ['dark', 'light'])
def test_tab_menu_uses_surface_tokens_and_fixed_controls(page, mode):
    page.evaluate("mode => document.documentElement.dataset.mode = mode", mode)
    page.locator('#tabList').click()
    for selector, prop, token in [
        ('.tab-menu-panel', 'backgroundColor', '--surf-1'),
        ('.tab-menu-panel input', 'backgroundColor', '--surf-2'),
        ('.tab-menu-panel input', 'color', '--fg'),
        ('.tab-controls', 'borderLeftColor', '--line'),
    ]:
        assert page.evaluate('''([selector, prop, token]) => {
            const expected=document.createElement('span');
            expected.style.color=`var(${token})`; document.body.append(expected);
            const color=getComputedStyle(expected).color; expected.remove();
            return getComputedStyle(document.querySelector(selector))[prop] === color;
        }''', [selector, prop, token])
    assert page.evaluate("""() => {
        const expected = document.createElement('div');
        expected.style.color = 'var(--acc)'; document.body.append(expected);
        const color = getComputedStyle(expected).color; expected.remove();
        const tab = document.querySelector('#tabs .on');
        return getComputedStyle(tab, '::after').backgroundColor === color &&
          getComputedStyle(tab).borderRadius === '0px' &&
          getComputedStyle(document.querySelector('#tabs')).scrollbarWidth === 'none';
    }""")
    assert page.locator('#tabScrollLeft').count() == 0
    assert page.locator('#tabAdd').bounding_box()['width'] >= 24
    controls = ['#tabAdd', '#tabAdd .ti', '#tabList', '#tabList .ti', '#tabList span']
    boxes = [page.locator(selector).bounding_box() for selector in controls]
    centers = [box['y'] + box['height'] / 2 for box in boxes]
    assert max(centers) - min(centers) <= 1
    assert boxes[0]['height'] == boxes[2]['height'] == 26
    assert _style(page, '#tabAdd .ti', 'fontSize') == _style(page, '#tabList .ti', 'fontSize') == '14px'
    assert page.locator('#tabAdd .ti-plus').count() == 1


@pytest.mark.parametrize('mode', ['dark', 'light'])
def test_empty_workbench_uses_tokens(page, tmp_path, mode):
    page.evaluate("mode => document.documentElement.dataset.mode = mode", mode)
    _select_testpg(page)
    page.locator('.tab.on .x').click()
    page.wait_for_selector('#queryEmptyState')
    assert page.evaluate('''() => {
        const empty=getComputedStyle(document.querySelector('#queryEmptyState'));
        const probe=document.createElement('span');
        probe.style.color='var(--fg3)'; document.body.append(probe);
        const fg=getComputedStyle(probe).color;
        probe.style.color='var(--bg0)'; const bg=getComputedStyle(probe).color;
        probe.remove();
        return empty.color === fg && empty.backgroundColor === bg;
    }''')
    box = page.locator('#queryEmptyState').bounding_box()
    action = page.locator('#emptyNewTab').bounding_box()
    assert box['x'] <= action['x'] and action['x'] + action['width'] <= box['x'] + box['width']
    assert page.locator('#sql, #grid').count() == 0
    page.screenshot(path=str(tmp_path / f'empty-{mode}.png'))


@pytest.mark.parametrize("mode", ["dark", "light"])
def test_workbench_compact_spacing(page, mode):
    page.evaluate("mode => document.documentElement.dataset.mode = mode", mode)
    assert _style(page, '.qhead', 'padding') == '8px 14px'
    assert _style(page, '.tab-navigation', 'height') == '36px'
    assert _style(page, '.run-split', 'height') == '30px'
    assert _style(page, '#runBtn', 'fontSize') == '12px'
    for selector in ['#fmtBtn', '#expBtn', '#csvBtn', '#jsonBtn', '#histBtn', '#linkBtn']:
        assert _style(page, selector, 'height') == '30px'
        assert _style(page, selector, 'fontSize') == '12px'
    assert _style(page, '#sql', 'paddingLeft') == '14px'
    assert _style(page, '.toolbar', 'paddingLeft') == '14px'


@pytest.mark.parametrize('mode', ['dark', 'light'])
def test_record_link_chrome_uses_theme_tokens(page, mode):
    _select_testpg(page)
    _set_sql(page, 'select * from customers')
    page.locator('#runBtn').click()
    _run_result(page)
    page.locator('#grid tbody tr').first.locator('td').first.click()
    from playwright.sync_api import expect
    expect(page.locator('#recordLinkBtn')).to_be_enabled()
    page.evaluate("mode => document.documentElement.dataset.mode = mode", mode)
    page.mouse.move(0, 0)
    assert _style(page, '#recordLinkBtn', 'color') == _token_color(page, '--fg')
    header = page.locator('.modal .mh').bounding_box()
    button = page.locator('#recordLinkBtn').bounding_box()
    assert button['x'] > header['x'] + header['width'] / 2
    assert button['x'] + button['width'] <= header['x'] + header['width'] + 1
    page.keyboard.press('Escape')
    _set_sql(page, 'select count(*) as total from customers')
    page.locator('#runBtn').click()
    _run_result(page)
    page.locator('#grid tbody tr').first.locator('td').first.click()
    expect(page.locator('.modal [role="status"]')).to_be_visible()
    assert _style(page, '.modal [role="status"]', 'color') == _token_color(page, '--fg2')


@pytest.mark.parametrize('mode', ['dark', 'light'])
def test_keeper_control_tokens(page, mode):
    page.evaluate("mode => document.documentElement.dataset.mode = mode", mode)
    for state, token_fg, token_bg in [('ok', '--ok', '--ok-bg'), ('err', '--red-fg', '--red-bg'), ('', '--fg2', '--bg1')]:
        page.locator('#kaBadge').evaluate('''(el, state) => {
            el.classList.remove('ok', 'err'); if (state) el.classList.add(state);
        }''', state)
        page.mouse.move(0, 500)
        assert _style(page, '#kaBadge', 'color') == _token_color(page, token_fg)
        assert _style(page, '#kaBadge', 'backgroundColor') == _token_bg(page, token_bg)
    page.locator('#kaBadge').focus()
    assert _style(page, '#kaBadge', 'outlineColor') == _token_color(page, '--accent')
    assert _style(page, '#kaBadge', 'outlineStyle') == 'solid'


@pytest.mark.parametrize('language', ['zh', 'en'])
def test_assistant_chrome_tracks_theme_tokens(page, tmp_path, monkeypatch, language):
    from test_gui_browser_features import _assistant_scrollbar_styles
    monkeypatch.setenv("QUARRY_CONFIG", str(tmp_path / "assistant-config.toml"))
    page.route('**/api/assistant/config', lambda route: route.fulfill(json={'configured': False}))
    page.evaluate('language => localStorage.setItem("qy_lang", language)', language)
    page.reload(wait_until='networkidle')
    _select_testpg(page)
    _set_sql(page, 'SELECT 1')
    page.get_by_test_id('assistant-open').click()
    page.get_by_test_id('assistant-close').click()

    def token_color(selector, prop, token):
        return page.locator(selector).evaluate('''(el, [prop, token]) => {
          const probe = document.createElement('span');
          probe.style.color = token.startsWith('--') ? `var(${token})` : token;
          el.parentElement.append(probe);
          const expected = getComputedStyle(probe).color;
          const actual = getComputedStyle(el)[prop];
          probe.remove();
          return {actual, expected};
        }''', [prop, token])

    def resize_chrome(selector, edge):
        return page.locator(selector).evaluate('''(el, edge) => {
          const style = getComputedStyle(el), grip = getComputedStyle(el, '::after');
          return {
            background: style.backgroundColor,
            borderColor: style[`border${edge}Color`],
            borderWidth: style[`border${edge}Width`],
            borderStyle: style[`border${edge}Style`],
            gripColor: grip.backgroundColor, gripOpacity: grip.opacity,
            gripWidth: grip.width, gripHeight: grip.height,
          };
        }''', edge)

    for mode in ('dark', 'light'):
        if page.evaluate('document.documentElement.dataset.mode') != mode:
            page.locator('.vg-switcher-mode').click()
        page.mouse.move(500, 30)
        rail = '[data-testid="assistant-open"]'
        assert page.locator(rail + ' .assistant-rail-name').inner_text() == ('SQL 助手' if language == 'zh' else 'SQL Assistant')
        assert page.locator(rail + ' i').count() == 0
        bounds = page.locator(rail).bounding_box()
        assert bounds['height'] == 88
        child = page.locator(rail + ' .assistant-rail-name')
        box = child.bounding_box()
        assert bounds['x'] <= box['x'] < box['x'] + box['width'] <= bounds['x'] + bounds['width']
        assert bounds['y'] <= box['y'] < box['y'] + box['height'] <= bounds['y'] + bounds['height']
        assert child.evaluate('el => getComputedStyle(el).writingMode') == 'vertical-rl'
        assert _style(page, rail + ' .assistant-rail-name', 'fontSize') == '12px'
        page.screenshot(path=str(tmp_path / f'assistant-chrome-{language}-{mode}-collapsed.png'))
        colors = token_color(rail + ' .assistant-rail-name', 'color', '--acc')
        assert colors['actual'] == colors['expected']
        colors = token_color(rail, 'backgroundColor', 'color-mix(in srgb,var(--acc) 8%,var(--surf-2))')
        assert colors['actual'] == colors['expected']
        page.locator(rail).hover()
        colors = token_color(rail, 'backgroundColor', 'color-mix(in srgb,var(--acc) 14%,var(--surf-2))')
        assert colors['actual'] == colors['expected']
        assert _style(page, rail, 'color') == _token_color(page, '--acc')
        page.locator(rail).click()
        page.mouse.move(500, 30)
        for selector, prop, token in (
            ('[data-testid="assistant-panel"]', 'backgroundColor', '--surf-2'),
            ('[data-testid="assistant-close"]', 'color', '--fg3'),
            ('[data-testid="assistant-close"]', 'backgroundColor', '--surf-2'),
            ('[data-testid="assistant-close"]', 'borderTopColor', '--line'),
            ('[data-testid="assistant-input"]', 'color', '--fg'),
            ('[data-testid="assistant-input"]', 'backgroundColor', '--surf-2'),
            ('[data-testid="assistant-input"]', 'borderTopColor', '--line'),
            ('[data-testid="assistant-divider"]', 'backgroundColor', '--surf-1'),
            ('[data-testid="assistant-ghost"]', 'color', '--fg3'),
        ):
            colors = token_color(selector, prop, token)
            assert colors['actual'] == colors['expected'], (mode, selector, colors)
        assert _style(page, '[data-testid="assistant-ghost"]', 'opacity') == '0.75'
        assert _style(page, '[data-testid="assistant-panel"]', 'paddingTop') == '0px'
        assert page.get_by_test_id('assistant-messages').bounding_box()['y'] == page.get_by_test_id('assistant-panel').bounding_box()['y']
        assert _style(page, '[data-testid="assistant-messages"]', 'paddingTop') == '14px'
        assert _style(page, '[data-testid="assistant-messages"]', 'paddingRight') == '16px'
        assert _style(page, '[data-testid="assistant-messages"]', 'marginRight') == '0px'
        scrollbar = _assistant_scrollbar_styles(page)
        assert scrollbar == {'width': '4px', 'trackTop': '38px',
                             'thumb': _token_color(page, '--line2'), 'buttons': 'none',
                             'thumbBorderLeft': '0px', 'thumbBorderRight': '0px',
                             'thumbBackgroundClip': 'border-box',
                             'standardWidth': 'auto', 'standardColor': 'auto'}
        assert _style(page, '[data-testid="assistant-input"]', 'boxShadow') == 'none'
        vertical = resize_chrome('[data-testid="assistant-divider"]', 'Left')
        horizontal = resize_chrome('#edresizer', 'Bottom')
        for key in ('background', 'borderColor', 'borderWidth', 'borderStyle', 'gripColor', 'gripOpacity'):
            assert vertical[key] == horizontal[key], (mode, key, vertical, horizontal)
        assert vertical['borderWidth'] == '1px' and vertical['borderStyle'] == 'solid'
        assert vertical['gripOpacity'] == '0.4'
        assert horizontal['gripColor'] == _token_color(page, '--fg3')
        assert horizontal['gripOpacity'] == '0.4'
        assert _style(page, '[data-testid="assistant-divider"]', 'borderLeftWidth') == '1px'
        assert _style(page, '[data-testid="assistant-divider"]', 'borderRightWidth') == '0px'
        for prop in ('borderBottomWidth', 'borderBottomColor', 'borderBottomStyle'):
            assert _style(page, '[data-testid="assistant-divider"]', prop) == _style(page, '.toolbar', prop)
        assert _style(page, '[data-testid="assistant-divider"]', 'borderBottomWidth') == '1px'
        assert _style(page, '[data-testid="assistant-divider"]', 'borderBottomStyle') == 'solid'
        assert page.evaluate('''() => {
          const divider = document.querySelector('[data-testid="assistant-divider"]').getBoundingClientRect();
          const toolbar = document.querySelector('.toolbar').getBoundingClientRect();
          return Math.abs(divider.right - toolbar.left) <= 1 && Math.abs(divider.bottom - toolbar.bottom) <= 1;
        }''')
        assert page.get_by_test_id('assistant-divider').evaluate('''el => {
          const style = getComputedStyle(el), grip = getComputedStyle(el, '::after');
          return parseFloat(style.borderLeftWidth) + parseFloat(grip.left) + parseFloat(grip.width) / 2;
        }''') == 3
        assert vertical['gripWidth'] == horizontal['gripHeight'] == '2px'
        assert vertical['gripHeight'] == horizontal['gripWidth'] == '30px'
        assert page.get_by_test_id('assistant-divider').bounding_box()['width'] == page.locator('#edresizer').bounding_box()['height'] == 6
        field = page.get_by_test_id('assistant-input')
        field.hover()
        assert page.locator('[data-testid^="assistant-language"], .assistant-language-row').count() == 0
        assert _style(page, '[data-testid="assistant-input"]', 'paddingBottom') == '10px'
        assert _style(page, '[data-testid="assistant-input"]', 'minHeight') == '70px'
        assert page.get_by_test_id('assistant-close').evaluate('el => getComputedStyle(el, "::before").content') == 'none'
        page.screenshot(path=str(tmp_path / f'assistant-chrome-{language}-{mode}-expanded.png'))
        assert _style(page, '.assistant-close i', 'borderTopWidth') == '0px'
        assert _style(page, '.assistant-close i', 'borderRadius') == '0px'
        field.focus()
        assert page.locator('[data-testid^="assistant-language"]').count() == 0
        field.fill('')
        field.evaluate('el => el.blur()')
        field.hover()
        assert _style(page, '[data-testid="assistant-input"]', 'borderTopColor') == _token_color(page, '--line2')
        field.focus()
        colors = token_color('[data-testid="assistant-input"]', 'borderTopColor', 'color-mix(in srgb,var(--acc) 55%,var(--line2))')
        assert colors['actual'] == colors['expected']
        assert _style(page, '[data-testid="assistant-input"]', 'boxShadow') == 'none'
        field.evaluate('el => el.blur()')
        divider = page.get_by_test_id('assistant-divider')
        page.locator('#edresizer').hover()
        horizontal_hover = resize_chrome('#edresizer', 'Bottom')
        divider.hover()
        vertical_hover = resize_chrome('[data-testid="assistant-divider"]', 'Left')
        for key in ('background', 'borderColor', 'borderWidth', 'borderStyle', 'gripColor', 'gripOpacity'):
            assert vertical_hover[key] == horizontal_hover[key], (mode, key, vertical_hover, horizontal_hover)
        assert vertical_hover['borderColor'] == vertical['borderColor']
        assert vertical_hover['gripColor'] == _token_color(page, '--acc')
        assert vertical_hover['gripOpacity'] == '1'
        assert vertical_hover['gripWidth'] == horizontal_hover['gripHeight'] == '2px'
        assert vertical_hover['gripHeight'] == horizontal_hover['gripWidth'] == '30px'
        colors = token_color('[data-testid="assistant-divider"]', 'backgroundColor', '--bg3')
        assert colors['actual'] == colors['expected']
        assert _style(page, '[data-testid="assistant-divider"]', 'borderRightWidth') == '0px'
        page.screenshot(path=str(tmp_path / f'assistant-divider-{language}-{mode}-hover.png'))
        divider.focus()
        divider.press('ArrowRight')
        divider.press('ArrowLeft')
        page.mouse.move(500, 30)
        assert divider.evaluate('el => el.matches(":focus-visible")')
        assert _style(page, '[data-testid="assistant-divider"]', 'outlineColor') == _token_color(page, '--acc')
        assert _style(page, '[data-testid="assistant-divider"]', 'outlineWidth') == '2px'
        assert _style(page, '[data-testid="assistant-divider"]', 'outlineStyle') == 'solid'
        assert _style(page, '[data-testid="assistant-divider"]', 'outlineOffset') == '-2px'
        focused = resize_chrome('[data-testid="assistant-divider"]', 'Left')
        assert focused['background'] == _token_color(page, '--bg3')
        assert focused['gripColor'] == _token_color(page, '--acc') and focused['gripOpacity'] == '1'
        page.get_by_test_id('assistant-close').hover()
        assert _style(page, '.assistant-close', 'backgroundColor') == _token_color(page, '--bg3')
        assert _style(page, '.assistant-close', 'color') == _token_color(page, '--fg')
        assert _style(page, '.assistant-close', 'borderTopColor') == _token_color(page, '--line2')
        page.get_by_test_id('assistant-close').click()
        page.get_by_test_id('assistant-open').click()
        page.mouse.move(500, 30)
        assert _style(page, '.assistant-close', 'outlineStyle') == 'none'
        assert page.get_by_test_id('assistant-close').evaluate('el => document.activeElement !== el')
        page.get_by_test_id('assistant-close').click()


@pytest.mark.parametrize('mode', ['dark', 'light'])
def test_assistant_markdown_chrome_tracks_theme_tokens(page, tmp_path, monkeypatch, mode):
    from test_gui_browser_features import _ASSISTANT_MARKDOWN, _AssistantBrowserStub, _assistant_send, _drag
    page.set_viewport_size({'width': 1280, 'height': 1200})
    stub = _AssistantBrowserStub(page, tmp_path, monkeypatch)
    stub.response = _ASSISTANT_MARKDOWN
    _select_testpg(page)
    _set_sql(page, '')
    page.evaluate('mode => document.documentElement.dataset.mode = mode', mode)
    page.get_by_test_id('assistant-open').click()
    _assistant_send(page, 'Explain the example')
    page.locator('.assistant-markdown table').wait_for()
    page.mouse.move(500, 30)
    for selector, prop, token in (
        ('.assistant-markdown h2', 'color', '--acc'),
        ('.assistant-markdown h2 strong', 'color', '--acc'),
        ('.assistant-markdown p strong', 'color', '--fg'),
        ('.assistant-markdown p > code', 'color', '--fg'),
        ('.assistant-markdown p > code', 'backgroundColor', '--bg3'),
        ('.assistant-markdown pre', 'backgroundColor', '--bg3'),
        ('.assistant-markdown pre', 'borderTopColor', '--line'),
        ('.assistant-markdown pre code', 'color', '--fg'),
        ('.assistant-markdown pre code .hljs-keyword', 'color', '--acc'),
        ('.assistant-markdown pre code .hljs-string', 'color', '--ok'),
        ('.assistant-markdown pre code .hljs-number', 'color', '--num'),
        ('.assistant-markdown pre code .hljs-built_in', 'color', '--num'),
        ('.assistant-markdown pre code .hljs-literal', 'color', '--bool'),
        ('.assistant-markdown pre code .hljs-comment', 'color', '--fg3'),
        ('.assistant-markdown blockquote', 'color', '--fg3'),
        ('.assistant-markdown blockquote', 'borderLeftColor', '--line2'),
        ('.assistant-markdown a', 'color', '--acc'),
        ('.assistant-table-scroll', 'borderTopColor', '--line'),
        ('.assistant-markdown th', 'backgroundColor', '--bg3'),
        ('.assistant-markdown th', 'color', '--fg'),
        ('.assistant-markdown td', 'color', '--fg2'),
        ('.assistant-markdown td', 'borderBottomColor', '--line'),
    ):
        assert _style(page, selector, prop) == _token_color(page, token), (mode, selector, prop)
    assert page.locator('.assistant-message-assistant').evaluate('el => Math.abs(parseFloat(getComputedStyle(el).lineHeight) / parseFloat(getComputedStyle(el).fontSize) - 1.55) < 0.01')
    assert _style(page, '.assistant-message-assistant', 'marginBottom') == '16px'
    assert _style(page, '.assistant-markdown p', 'marginBottom') == '8px'
    assert _style(page, '.assistant-markdown ul', 'marginBottom') == '8px'
    assert _style(page, '.assistant-markdown h2', 'marginBottom') == '6px'

    _drag(page, '#edresizer', 0, 280)
    page.get_by_test_id('assistant-messages').evaluate('el => { el.scrollTop = 0; }')
    page.screenshot(path=str(tmp_path / f'assistant-markdown-{mode}.png'))
