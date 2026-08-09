# Testing

Quarry has a layered test suite so failures are easy to localize and the whole
thing stays green on a laptop with nothing installed (engine-dependent tests skip
rather than fail; CI provides the engines and runs everything).

## Layers

| Marker        | What it covers                                              | Needs |
|---------------|-------------------------------------------------------------|-------|
| `unit`        | pure logic + mocked externals (safety rails, SQL skeleton, param substitution, redis guard, formatters, URL parsing, cache, port logic) | nothing |
| `integration` | in-process against a real database, incl. the in-thread GUI HTTP server and in-process CLI/MCP dispatch | Postgres |
| `e2e`         | a real external process: the `qy` CLI and `qy mcp` stdio server as subprocesses | Postgres |
| `browser`     | the real GUI frontend driven in headless Chromium (Playwright) | Postgres + Playwright |

`browser` is a sub-kind of `e2e` (a test can carry both markers). Every test is
auto-assigned a layer from the fixtures it uses, so `pytest -m unit` etc. always
partition the whole suite.

## Running

```bash
make test              # unit → integration → e2e, with a per-layer PASS/FAIL summary
make test-unit         # one layer (also: test-integration, test-e2e, test-browser)
make cov               # coverage gate: unit + integration must be ≥ 95%
make test-browser      # headless-browser GUI suite (needs the browser installed once)
```

Under the hood these call `scripts/test.sh` (layered runner) and
`scripts/coverage.sh` (the gate). Plain `pytest` also works and prints an
environment header showing which engines were detected.

## One-time setup

```bash
make install                                   # pip install -e ".[dev]"
make seed                                       # create + seed the quarry_test database
# for the browser suite:
pip install -e ".[e2e]" && make browser-install # installs Playwright + headless Chromium
```

Override the database URL with `QUARRY_TEST_DB_URL` (defaults to
`postgresql://localhost:5432/quarry_test`). MySQL tests run only when
`QUARRY_TEST_MYSQL_URL` is set; Redis and MySQL execution paths are otherwise
covered by mocked tests so they run everywhere.

## Coverage

The gate (`make cov`) measures **unit + integration** coverage of the `quarry`
package and fails under 95%. It deliberately excludes the `e2e`/`browser` layers
so the number reflects what the fast in-process suites alone exercise. The
frontend lives in `web/` (React + TypeScript) and is covered by the `browser`
suite (behaviorally), not by Python line coverage. Genuinely
unreachable defensive code (blocking `serve_forever`, `# unreachable` lines after
`err(...)`) is excluded via `[tool.coverage.report]` in `pyproject.toml`.

## CI

`.github/workflows/ci.yml` runs four kinds of jobs: the layered suite on the
boundary Python versions 3.11 and 3.13 (with Postgres + Redis services), a
`coverage` gate job on 3.12 that also runs the e2e layer (so no version×layer
combination runs twice), a `browser` job (headless Chromium, cached between
runs), and a package `build` check. Every job builds the React shell (`web/`,
`npm ci && npm run build`) before Python tests so `/app` assets are present.

## The React frontend (`/app`)

The GUI frontend is a Vite + React + TypeScript package under `web/` that
builds into `src/quarry/web_dist/` and is served by `gui.py` at `/app` — the
default landing page (`/` redirects there). Node is dev/CI-only; the built
assets ship in the wheel. **The browser suite tests the built bundle: run
`cd web && npm run build` after changing `web/src`.**

The React app is a drop-in replacement for the retired embedded-JS GUI: same
DOM (ids/classes), same i18n strings, same localStorage keys and value formats
— so the feature-matrix suite below, originally written against the embedded
GUI, pins the React app unchanged. Packaging and React-only features live in
`test_gui_react_app.py`; visual design-token pins live in `test_gui_visual.py`:

| # | Area | Feature | Covered by | ✓ |
|---|------|---------|------------|---|
| R1 | react | `/app/` mounts the app directly (not only via the `/` redirect) | test_gui_react_app:test_react_app_mounts_at_app_path | ✅ |
| R2 | react | `/api/version` JSON endpoint; `/` redirects to `/app/` | test_gui_react_app:test_api_version, test_gui_api:test_root_redirects_to_react_app | ✅ |
| R3 | react | wheel includes `quarry/web_dist/index.html` and every hashed `/app/assets/*` file referenced by that index; sdist excludes node_modules | test_gui_react_app:test_wheel_includes_web_dist, test_sdist_excludes_node_modules, CI build job | ✅ |
| R4 | react | table-structure browser (issue #11): double-click a sidebar table name → modal lists column names + types (incl. quoted special-char table names) | test_gui_react_app:test_table_structure_modal_shows_columns_and_types, test_table_structure_quoted_special_char_table | ✅ |
| R5 | react | switching tables replaces the structure modal's columns — latest-wins even when the first table's /api/columns response lands late | test_gui_react_app:test_table_structure_modal_switching_tables_replaces_columns | ✅ |
| R6 | react | a stale /api/tables response for a connection the user switched away from never repaints the table panel | test_gui_react_app:test_stale_tables_response_does_not_overwrite | ✅ |
| R7 | visual | design tokens resolve to the legacy "Slate & Copper" hex values in BOTH themes (body/header/Run-button/badge via getComputedStyle); dark is the default | test_gui_visual:test_default_theme_is_dark_with_legacy_palette, test_light_theme_matches_legacy_palette | ✅ |
| R8 | visual | 14px sans app chrome + mono editor; icons render through the self-hosted tabler-icons webfont (no CDN — closes #14) | test_gui_visual:test_typography_matches_legacy, test_icons_use_selfhosted_tabler_font | ✅ |
| R9 | visual | header's lang/theme/palette buttons share one uniform `.vg-iconbtn` box (equal height/min-width/border-radius, radius tracks `--r-btn` across the style axis — voyage 0.7.0); `.ciact .iconbtn`'s smaller CI-modal icons stay 22×22px, not stretched by voyage's fixed min-width | test_gui_visual:test_header_icon_controls_share_uniform_box, test_header_iconbtn_radius_follows_style_axis, test_ciact_iconbtn_fixed_size_not_stretched_by_min_width | ✅ |
| R10 | visual | header uses Voyage 0.15's dedicated `.vg-topbar`: DOM order is fixed 语言→明暗→调色板; lang switching causes no horizontal reflow; all three controls share the full box spec; the topbar has transparent/zero chrome, contains no query `.vg-toolbar`, and omitting `account` renders no account placeholder; `ResultWorkbench`'s `.vg-toolbar.toolbar` independently retains its padding/surface/border chrome | test_gui_visual:test_header_toolbar_dom_order_is_lang_mode_palette, test_lang_switch_fixed_width_no_reflow, test_header_toolbar_buttons_share_full_box_spec, test_header_topbar_has_no_query_toolbar_chrome_or_account_placeholder, test_query_toolbar_keeps_its_own_chrome | ✅ |
| R11 | visual | Voyage 0.15's Signal theme card is selectable; it resolves to `signal × current mode × classic × quiet`, renders true black/white body and header surfaces in dark/light modes, persists across reload without console errors, and does not disturb the pinned Slate baselines | test_gui_visual:test_signal_theme_four_axes_colors_and_reload_persistence, test_default_theme_is_dark_with_legacy_palette, test_light_theme_matches_legacy_palette | ✅ |

## GUI feature matrix

The GUI is a React + TypeScript SPA (`web/src/`) served under `/app` by the
stdlib-only backend in `src/quarry/gui.py` (`http.server` + `/api/*`). Because
the feature surface lives in a handful of components and stores, it can be
enumerated *exhaustively* — this matrix is that enumeration, and it is the
source of truth for frontend coverage. The backend (API endpoints, cache,
health TTL, port takeover, Host/Origin guard) is fully covered by
`test_gui_backend.py` / `test_gui_api.py` / `test_cov_gui.py` and is not
repeated here.

### Keeping the matrix honest: three audits

The matrix stays trustworthy only if three *different* audits all close over
it — each catches a class of gap the others are structurally blind to. (The
blind spots are real: audit 1 alone shipped a matrix that said "tabs ✅" while
tab-switching silently kept — and exported — another tab's result set.)

1. **Existence audit** (code → matrix). Every interaction binding in
   `web/src/*.tsx` (`grep 'onClick\|onKeyDown\|onChange\|onMouseDown\|onDoubleClick'`),
   every `localStorage` key (`qy_lang qy_theme qy_sw qy_edh qy_tabs qy_ati
   qy_tabres qy_ui qy_maxrows qy_collapsed qy_hist qy_result` — same keys and
   value formats as the retired embedded GUI, so existing users' state carries
   over), and every `/api/*` endpoint the frontend fetches must map to a row.
   *Catches:* implemented-but-untested behavior. *Blind to:* features that
   should exist but don't, and cross-feature state bugs.
2. **Capability audit** (design → matrix). For each UI region (header, sidebar,
   tabs, editor, toolbar, grid), enumerate what a user of a SQL client would
   *expect* there (use DataGrip / TablePlus conventions as the reference), diff
   against the implementation, and file every miss in the **Design gaps** table
   below — scheduled or explicitly "won't do", never undocumented. *Catches:*
   missing features (this audit found the per-tab-result gap).
3. **Shared-state audit** (state × features). Store state in `web/src/store/`
   (`connStore`: current connection / health / table-list cache; `tabsStore`:
   tabs + per-tab result snapshots; `uiStore`: prefs) plus workbench-local
   state (sort/selection, request-tracking guards, history hook), plus the
   localStorage keys above. When a change writes any of these, check **every
   reader** before shipping; any two features sharing state need an
   interaction test. *Catches:* cross-feature bugs invisible to per-feature
   rows (tab switch updated the current connection while the grid still held
   the other tab's rows → CSV exported wrong data under the new tab's
   filename).

One more rule from a real regression: when adding a UX invariant ("SQL is never
silently lost"), grep for **all** write sites of the protected state and cover
each — the invariant first landed on 4 of the 5 editor-overwrite sites; the 5th
(closing a tab) shipped unprotected.

Status: ✅ covered · 🟡 partial · ❌ uncovered. Tests live in
`tests/test_gui_browser.py` (B) and `tests/test_gui_browser_features.py` (F).
Pure frontend logic that doesn't need a browser also gets a `web/src/**/*.test.ts`
vitest unit test (`cd web && npm run test:unit`), referenced by its file name.

| # | Area | Feature | Covered by | ✓ |
|---|------|---------|------------|---|
| 1 | header | brand + workspace label; multi-workspace count + tooltip | B:test_load_shows_brand_and_readonly_badge (single-ws only) | 🟡 |
| 2 | header | read-only badge | B:test_load_shows_brand_and_readonly_badge | ✅ |
| 3 | header | prod badge visibility | F:test_env_pills_default_dev_and_prod_badge | ✅ |
| 4 | header | health-check button: probe all, dots update, error tooltip | F:test_health_button_paints_ok_and_down_dots | ✅ |
| 5 | header | language toggle 中/EN (reload, full chrome, persistence) | B:test_language_toggle_switches_run_label, F:test_language_toggle_full_chrome | ✅ |
| 6 | header | mode toggle + persistence; palette selection includes Signal with persisted four-axis prefs | B:test_theme_toggle_flips_data_theme, F:test_theme_persists_after_reload, test_gui_visual:test_signal_theme_four_axes_colors_and_reload_persistence | ✅ |
| 7 | sidebar | connection groups + workspace origin label | B:test_load_shows_brand_and_readonly_badge (row presence only) | 🟡 |
| 8 | sidebar | group collapse/expand + persistence | F:test_group_collapse_persists_after_reload | ✅ |
| 9 | sidebar | health dot states (ok/down; dimmed row; error tooltip) | F:test_health_button_paints_ok_and_down_dots | ✅ |
| 10 | sidebar | instant dot paint from backend cache (`cached=1`) | F:test_health_dots_repaint_from_cache_after_reload | ✅ |
| 11 | sidebar | env pills render (default dev; prod styling); selected prod pills in both sidebar and query switcher keep white text and ≥4.5:1 contrast under Slate/Signal light themes | F:test_env_pills_default_dev_and_prod_badge, F:test_selected_prod_pills_keep_accessible_contrast_in_light_themes | ✅ |
| 12 | sidebar | pill click switches env; pill + header switcher sync | F:test_prod_env_switch_does_not_autorun, F:test_nonprod_env_switch_autoruns | ✅ |
| 13 | sidebar | env-switch auto-rerun — **never on prod** (toast instead) | F:test_prod_env_switch_does_not_autorun, F:test_nonprod_env_switch_autoruns | ✅ |
| 14 | sidebar | row click selects + opens table panel; re-click toggles panel | F:test_reclick_connection_toggles_panel | ✅ |
| 15 | sidebar | table filter box; filter survives the SWR repaint | F:test_table_filter_box (repaint-survival unasserted) | 🟡 |
| 16 | sidebar | table panel connection-error state / `no tables` empty state | F:test_dead_connection_click_shows_error_panel (error only) | 🟡 |
| 17 | sidebar | TCACHE instant paint + SWR background refresh | F:test_swr_refreshes_stale_table_list | ✅ |
| 18 | sidebar | redis key tree: `:` hierarchy, fold, count badges | F:test_redis_key_tree_badges_filter_and_inspect | ✅ |
| 19 | sidebar | redis type + TTL badges | F:test_redis_key_tree_badges_filter_and_inspect | ✅ |
| 20 | sidebar | redis key filter; key click → inspect grid | F:test_redis_key_tree_badges_filter_and_inspect | ✅ |
| 21 | sidebar | saved-query list (param badge, desc tooltip); paramless runs on click | F:test_saved_query_without_params_runs_directly | ✅ |
| 22 | sidebar | saved-query param modal (required/default, Enter submits, click-out closes) | B:test_saved_query_param_modal…, F:test_param_modal_enter_submits_and_clickout_closes | ✅ |
| 23 | sidebar | sidebar width drag + persistence | F:test_sidebar_width_drag_persists | ✅ |
| 24 | editor | SQL highlight overlay + scroll sync | F:test_sql_highlight_overlay (scroll sync unasserted) | 🟡 |
| 25 | editor | placeholder states (no conn / sql / redis) | F:test_placeholder_states, F:test_redis_key_tree… (redis) | ✅ |
| 26 | editor | Cmd/Ctrl+Enter runs | B:test_custom_sql_via_run_keyboard | ✅ |
| 27 | editor | Cmd/Ctrl+↑↓ history walk; draft stashed and restored at the bottom | F:test_history_nav_stashes_and_restores_draft | ✅ |
| 28 | editor | draft pushed to History before any overwrite (table/key/saved/recall) | F:test_table_click_preserves_draft_in_history | ✅ |
| 29 | editor | autocomplete: keywords + tables | B:test_autocomplete_keyword_and_table | ✅ |
| 30 | editor | autocomplete: `table.column` via /api/columns | F:test_autocomplete_columns_after_table_dot | ✅ |
| 31 | editor | autocomplete: FROM/JOIN table-only filter; dedup; 12-item cap | F:test_autocomplete_from_narrows_to_tables (dedup/cap unasserted) | 🟡 |
| 32 | editor | autocomplete keyboard nav (↑↓ Tab Enter Esc) + mouse pick | B/F autocomplete tests (partial) | 🟡 |
| 33 | editor | editor height drag + persistence | F:test_editor_height_drag_persists | ✅ |
| 34 | editor | tabs: add/switch/close/persist | B:test_tabs_add_switch_restore_and_close | ✅ |
| 35 | editor | tab title rule (SQL main-table, incl. quoted/backtick identifiers > first words > `db@env` > placeholder), live-updates as SQL changes, distinguishes same-connection tabs; legacy `qy_ui` migration | tabsStore.test.ts, F:test_tab_title_shows_db_at_env, F:test_tab_title_derives_from_sql_table_and_distinguishes_tabs, F:test_tab_title_updates_when_sql_switches_table, F:test_tab_title_distinguishes_quoted_mixed_case_tables, F:test_legacy_qy_ui_migrates_into_tabs | ✅ |
| 36 | toolbar | Run button + loading spinner | B:test_custom_sql_via_run_button, F:test_run_shows_loading_spinner | ✅ |
| 37 | toolbar | overlapping runs are latest-wins (stale response discarded) | F:test_stale_slow_response_does_not_overwrite | ✅ |
| 38 | toolbar | Format (uppercase + newlines) | B:test_format_button_uppercases_and_newlines | ✅ |
| 39 | toolbar | EXPLAIN: single-column plan modal + Esc closes | B:test_explain_opens_plan_modal_and_escape_closes | ✅ |
| 40 | toolbar | EXPLAIN guards: no-conn toast / redis toast / multi-col grid / disabled while running | F:test_explain_without_connection_toasts, F:test_redis_key_tree… (multi-col + disabled unasserted) | 🟡 |
| 41 | toolbar | CSV export: `quarry-<db>.csv`, UTF-8 BOM, quoting/escaping | F:test_csv_export_content_bom_and_escaping | ✅ |
| 42 | toolbar | JSON export: `quarry-<db>.json`, row content | F:test_json_export_content | ✅ |
| 43 | toolbar | history modal: list + recall into editor; Esc closes | B:test_history_lists_runs…, F:test_history_modal_escape_closes | ✅ |
| 44 | toolbar | history search / ago timestamps / empty toast / 100 cap | F:test_history_search_filters, F:test_history_empty_toast (ago + cap unasserted) | 🟡 |
| 45 | grid | render: sticky rownum, typed headers, zebra rows | B:test_click_table_renders_grid_with_types_and_status | ✅ |
| 46 | grid | cell type coloring (num/uuid/ts/bool/json/null) | F:test_cell_type_coloring, F:test_cell_json_opens_tree_modal | ✅ |
| 47 | grid | sort asc/desc, numeric-aware; 3rd click restores original order; arrow | B:test_sort_column_toggles_arrow_and_reorders, F:test_sort_numeric_strings_and_third_click_restores | ✅ |
| 48 | grid | sort state resets on a new result | F:test_new_result_resets_sort_state | ✅ |
| 49 | grid | column width drag | F:test_column_width_drag | ✅ |
| 50 | grid | cell select; bounded preview for large values; dblclick long→chunked modal / short→copy; explicit copy always uses the full raw value (honest toast) | B:test_cell_doubleclick_no_error, F:test_cell_copy_via_keyboard_and_dblclick, F:test_large_cell_is_bounded_in_dom_and_kept_session_only, F:test_large_cell_copy_uses_full_value_not_preview; cellValue.test.ts | ✅ |
| 51 | grid | JSON tree modal + copy; branches start collapsed and mount children only when expanded | F:test_cell_json_opens_tree_modal | ✅ |
| 52 | grid | row-detail modal (rownum click), with the same bounded large-cell previews as the grid | B:test_rownum_click_opens_row_detail_modal, F:test_large_cell_is_bounded_in_dom_and_kept_session_only | ✅ |
| 53 | grid | keyboard nav: arrows move selection, Enter opens, Cmd+C copies | F:test_grid_keyboard_nav_and_enter_opens_modal, F:test_cell_copy_via_keyboard_and_dblclick | ✅ |
| 54 | grid | status bar: rows / elapsed / download size / avg speed (≈ + tooltip when estimated) / truncated / target | B:test_click_table…, F:test_truncated_badge_shows, F:test_status_bar_shows_download_size_and_speed, F:test_load_more_accumulates_download_size_and_speed | ✅ |
| 55 | grid | 0-row empty state | F:test_zero_rows_empty_state | ✅ |
| 56 | grid | error pane (`.err`); network failures show a readable message | B:test_write_is_blocked…, F:test_network_error_shows_readable_message | ✅ |
| 57 | grid | bounded result persisted to localStorage and restored after reload; results over 512 KiB remain in-session only with a visible status notice | F:test_editor_and_result_restored_after_reload, F:test_large_cell_is_bounded_in_dom_and_kept_session_only; tabsStore.test.ts | ✅ |
| 58 | grid | Escape closes the topmost modal | B:test_explain…, F:test_cell_json…, F:test_history_modal_escape_closes | ✅ |
| 59 | global | toast styles + durations (ok vs error) | F:test_cell_copy_via_keyboard_and_dblclick (ok style) + error-toast presence in guards | ✅ |
| 60 | global | read-only rail end-to-end | B:test_write_is_blocked_with_readonly_error | ✅ |
| 61 | global | full state restore after reload (conn + sql + result + widths + collapse) | F:test_editor_and_result_restored…, F:test_group_collapse…, F:test_sidebar_width…, F:test_editor_height… | ✅ |
| 62 | global | zero console errors as an invariant | F autouse `_console_clean`; B:test_no_console_errors_after_normal_flow | 🟡 |
| 63 | sidebar | redis key list cap notice ("showing first N keys") | F:test_redis_capped_key_list_shows_notice | ✅ |
| 64 | sidebar | generated table SQL quotes mixed-case/reserved identifiers | F:test_mixed_case_table_click_is_quoted | ✅ |
| 65 | toolbar | max-rows selector: caps results, persisted across reloads | F:test_max_rows_selector_caps_and_persists | ✅ |
| 66 | header | icon-only controls carry aria-labels | — (set in the i18n block; no axe pass yet) | 🟡 |
| 67 | tabs | per-tab result isolation: grid / status / export always reflect the active tab | F:test_tab_switch_isolates_results | ✅ |
| 68 | tabs | closing a tab pushes its SQL to History (active + inactive close) | F:test_close_tab_preserves_sql_in_history | ✅ |
| 69 | tabs | tab with a vanished connection unbinds (never silently rebinds) | F:test_stale_tab_connection_unbinds_not_rebinds | ✅ |
| 70 | sidebar | current-table highlight; cleared when custom SQL runs | F:test_table_click_highlights_current_table | ✅ |
| 71 | sidebar | manual list refresh button (tables + redis keys); filter survives refresh | F:test_table_list_manual_refresh (redis button + filter-survival unasserted) | 🟡 |
| 72 | sidebar | table list cap notice at 5000 | backend: test_api_tables_capped_flag_at_5000 (UI note unasserted) | 🟡 |
| 73 | sidebar | Alt+click inserts generated SQL without running | F:test_alt_click_inserts_without_running | ✅ |
| 74 | tabs | per-tab result persistence: every bounded tab result survives a reload (`qy_tabres`), each restored under its own connection; unchanged results are not reserialized on SQL input/tab switches | F:test_per_tab_results_persist_across_reload; tabsStore.test.ts | ✅ |
| 75 | tabs | an in-flight request that lands after a tab switch is stored on its origin tab, never the now-active one | F:test_slow_response_routes_to_origin_tab_not_active | ✅ |
| 76 | tabs | a result is tagged with its producing connection; re-pointing a tab to another db/env never restores the old grid on reload — incl. the legacy `qy_result` upgrade path (env, not just db, must match) | F:test_result_not_restored_after_tab_rebound_to_prod, F:test_legacy_qy_result_env_mismatch_not_restored, F:test_legacy_qy_result_env_match_restored | ✅ |
| 77 | tabs | an in-flight request whose own tab is switched to another env of the same db is dropped, never repainted/persisted as the new env | F:test_inflight_response_dropped_when_same_tab_switches_env | ✅ |
| 78 | toolbar | EXPLAIN single-column modal is suppressed if its tab was switched / re-pointed while the plan was in flight | implemented (`#expBtn` handler audits TABREQ/tab/connection); browser test tracked in #18 | 🟡 |
| 79 | tabs | a saved query runs on its OWN connection; launched from a tab bound to a different connection, its result is tagged/persisted under the producing connection (and the tab re-pointed to it), never the tab's previous one — for a concrete `@db`; consistency when `@db` is a logical env-set is tracked in #18 | F:test_saved_query_result_persisted_under_producing_connection | ✅ |
| 80 | header | connection-info button (`#ciBtn`): visible only when a connection is selected; opens the resolved-config modal | F:test_conn_info_modal_shows_resolved_config_and_health | ✅ |
| 81 | header | connection-info modal (`/api/conninfo`): resolved key/engine/env/host/port/database/source file; URL password always masked; live reachability probe with the raw error on failure | F:test_conn_info_modal_shows_resolved_config_and_health (ok path; error text asserted at API level in test_gui_api.py) | ✅ |
| 82 | header | conn-info url row: eye toggles masked↔revealed (`?reveal=1`); copy puts the real URL on the clipboard | F:test_conn_info_url_eye_toggles_and_copy_copies_real_url | ✅ |
| 83 | header | conn-info action: "Create local env" (`POST /api/local/up`) offered only when the env-set has no local member (postgres/redis); a fresh postgres env auto-runs the first schema sync, and a sync failure is reported without undoing the up | F:test_conn_info_offers_create_local_when_set_has_none (visibility), A:test_api_local_up_orchestration, A:test_api_local_up_reports_sync_failure_without_undoing_up; container path covered by test_local_docker.py on the underlying functions | 🟡 |
| 84 | header | conn-info action: "Sync schema from {env}" (`POST /api/local/sync`) offered only ON the local env (postgres); confirm-gated; refuses non-local targets with the CLI's exit-code-9 invariant | F:test_conn_info_offers_sync_on_local_env (visibility), A:test_local_sync_endpoint_refuses_non_local; swap behavior covered by test_local_sync_docker.py | 🟡 |
| 85 | grid | real pagination: a truncated result offers "load more" (same SQL, growing `OFFSET`), appending rows until the tail page isn't truncated; only offered for postgres/mysql results produced by Run (not saved-query params, not redis/neptune, which can't page this way) | F:test_load_more_paginates_truncated_result, A:test_query_offset_pages_through_results | ✅ |
| 86 | grid | "load more" on an already-sorted grid re-sorts the combined rows (not just the new page) so the active sort + its arrow stay correct across pages | F:test_load_more_keeps_active_sort_applied | ✅ |
| 87 | header | workspace manager (`#wsBtn`): list config.toml-registered workspaces (flags missing dir / no connections.toml), add a new one, remove one (confirm-gated); takes effect immediately without dropping an explicit `--workspace` session; removing the workspace behind the currently active connection unbinds it right away (no tab switch needed) | F:test_workspace_manager_add_and_remove, F:test_workspace_manager_remove_unbinds_active_connection_immediately, A:test_api_workspace_add_and_remove_round_trip, A:test_api_workspace_add_and_remove_keep_explicit_workspace_session, A:test_workspaces_endpoints_through_http | ✅ |
| 88 | tabs | double-click a tab renames it (Enter/blur commits, Escape reverts); an empty name reverts to the automatic db@env / SQL title; the custom title persists across reloads | F:test_tab_rename_persists_and_empty_reverts | ✅ |
| 89 | tabs | drag-and-drop reorders tabs; the active tab (and its per-tab result/SQL) follows its id, not its old index | F:test_tab_drag_reorder_moves_active_tab | ✅ |
| 90 | tabs | middle-click closes a tab (same as the × glyph); disabled when it is the only tab left | F:test_tab_middle_click_closes | ✅ |
| 91 | tabs | Cmd/Ctrl+Shift+W closes the active tab (Cmd+W-style; real Ctrl/Cmd+W can't be intercepted from a page); disabled when it is the only tab left | F:test_tab_keyboard_shortcut_closes_active_tab | ✅ |
| 92 | sidebar | env-set ordering: `local` always sorts first regardless of connection-registration order (sidebar pills + header switcher); with no `local` env, registration order is unchanged; default-selected env is `dev` if present, else `local`, else the first registered env | F:test_local_env_sorts_first_and_is_default_without_dev, A:test_local_env_always_sorts_first, A:test_group_structure (registration order preserved without local) | ✅ |
| 93 | sidebar | clicking a table generates+runs a preview query capped at `limit 5` (was `limit 100`) | F:test_click_table_renders_grid_with_types_and_status | ✅ |
| 94 | toolbar/tabs | query deep links: copy current `db/env/sql` as a shareable URL; opening it reuses an identical tab or creates one, restores SQL/connection, auto-runs, and guards invalid link targets with an explicit toast (no silent auto-run failure) | F:test_copy_query_link_copies_db_env_sql, F:test_query_deeplink_opens_existing_tab_and_autoruns, F:test_query_deeplink_invalid_env_shows_notice_and_skips_autorun | ✅ |
| 95 | app shell | live workspace refresh: `/api/events` SSE + backend file watcher — editing `connections.toml` / `queries/**/*.sql` on disk refreshes the sidebar without a manual reload (+ confirmation toast) | F:test_workspace_file_change_refreshes_sidebar_live (toast unasserted; watcher + SSE framing unit-covered in test_gui_backend.py) | ✅ |
| 96 | app shell | upgrade-reload banner: after the backend restarts with a new version, the EventSource reconnect re-checks `/api/version` and shows a persistent "reload page" banner | F:test_server_upgrade_prompts_reload_banner | ✅ |
| 97 | header | update-check badge: `GET /api/update` (backed by a throttled PyPI-polling thread — 24h interval, `QUARRY_UPDATE_CHECK=0` disables it, editable installs skip it, network failures stay silent) drives a header badge only when a newer release exists; its panel shows current/latest version, the `pipx upgrade quarry-db` command, and a GitHub release-notes link | F:test_update_badge_hidden_when_no_new_version, F:test_update_badge_visible_and_dot_uses_accent_token_in_both_themes, F:test_update_panel_shows_versions_and_upgrade_command; backend throttle/semver/disable/editable/silent-failure unit-covered in test_gui_backend.py, `/api/update` HTTP-covered in test_gui_api.py | ✅ |
| 98 | header | What's New panel: `GET /api/changelog` serves CHANGELOG.md (shipped in the wheel) parsed into version sections; a `__version__` change from the `qy_last_seen_version` recorded in localStorage auto-shows the panel with the entries for the new version(s), marks it seen immediately, and never reappears on a plain reload (a first-ever load with no recorded version just sets the baseline silently) | F:test_whats_new_hidden_on_first_ever_load, F:test_whats_new_shows_entries_after_upgrade_then_stays_hidden_on_reload, F:test_whats_new_background_uses_bg1_token_in_both_themes; CHANGELOG parsing unit-covered in test_gui_backend.py, `/api/changelog` HTTP-covered in test_gui_api.py, wheel packaging covered by test_wheel_includes_changelog | ✅ |
| 99 | sidebar | env pill shows a proxy badge (`data-testid="proxy-badge"`) only for connections with a currently-live SSH tunnel that's actually routed through the proxy right now (an observed tunnel-pool fact, not a prediction) — server-computed, not guessed client-side; hidden when the workspace toggle is off, nothing was discovered, or no tunnel has been established for that connection yet | F:test_proxy_badge_shown_only_on_the_tunneled_env_when_proxy_active, F:test_proxy_badge_hidden_when_workspace_toggle_off, F:test_proxy_badge_hidden_when_nothing_discovered, F:test_proxy_badge_follows_server_state_across_reload; `/api/connections`'s `proxied` field unit-covered in test_gui_api.py | ✅ |
| 100 | header | workspace manager shows each registered workspace's proxy toggle state and the currently discovered proxy address (`data-testid="ws-proxy-status"`) | F:test_workspace_manager_shows_proxy_status_per_workspace, F:test_workspace_manager_shows_proxy_off_when_toggle_disabled; `/api/workspaces`'s `proxyEnabled`/`proxyDiscovered` fields unit-covered in test_gui_api.py | ✅ |
| 101 | header | compact tunnel keep-alive control: live status badge (`down`/`up`/`reconnecting`) + start/stop button backed by `/api/keepalive*`, matching `qy status` semantics | F:test_header_keepalive_badge_and_toggle, F:test_header_keepalive_badge_tracks_background_state; `/api/keepalive`, `/api/keepalive/up`, `/api/keepalive/down` unit-covered in test_gui_api.py | ✅ |
| 102 | grid/tabs | multi-megabyte cells never enter grid/row-detail DOM attributes or text in full; grid preview is 512 chars, inspector text grows in 20k chunks, JSON children mount lazily, and >512 KiB results skip synchronous persistence while remaining fully copyable/exportable in the current session | F:test_large_cell_is_bounded_in_dom_and_kept_session_only, F:test_large_cell_copy_uses_full_value_not_preview, test_gui_visual:test_large_result_session_badge_inherits_status_token; cellValue.test.ts, tabsStore.test.ts | ✅ |

### Design gaps (capability-audit output — missing on purpose until scheduled)

| Region | Missing capability | Decision |
|--------|--------------------|----------|
| sidebar | row-count / size hints next to tables | backlog (low) |

Safety/performance-relevant UX invariants that must never regress (rows 13,
27–28, 37, 47, 102):
draft SQL is never silently lost; switching to prod never auto-runs; stale
responses never overwrite newer results; sort is numeric-aware and restorable;
large raw values stay copyable/exportable but never materialize in full in the
grid DOM or synchronous result persistence.

The Tabler icon webfont is vendored (self-hosted via `@tabler/icons-webfont`,
bundled by Vite) — no CDN dependency; `test_gui_visual.py` asserts the font
actually loads. This closed the long-open jsdelivr gap (#14).

## Adding tests

- Put pure logic in a `unit` test (no fixtures that touch a DB/server).
- Use `ws`/`pg_exec` for in-process DB work, `gui_server` for the GUI HTTP API,
  `qy`/`mcp` for CLI/MCP subprocess e2e, and `page` for browser e2e.
- Never mutate the shared `customers`/`orders` seed tables; create scratch tables
  with a per-file prefix and drop them in the test.
