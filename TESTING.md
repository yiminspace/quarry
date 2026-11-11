# Testing

## Rust runtime

The Rust runtime has a native library/process/real-engine suite; see
[commands and evidence](docs/rust-verification.md). Set `QUARRY_RUST_BINARY` to
run the existing browser matrix against an actual Rust HTTP process. The
frontend feature rows below retain their meaning; transport-specific backend
stubs provide a development baseline. The release workflow verifies the
workbench and its assets from the native GitHub archives.

Quarry has a layered test suite so failures are easy to localize and the whole
thing stays green on a laptop with nothing installed (engine-dependent tests skip
rather than fail; CI provides the engines and runs everything).

## Layers

| Marker        | What it covers                                              | Needs |
|---------------|-------------------------------------------------------------|-------|
| `unit`        | pure logic + mocked externals (safety rails, SQL skeleton, param substitution, redis guard, formatters, URL parsing, cache, port logic) | nothing |
| `integration` | in-process against a real database, incl. the in-thread GUI HTTP server and in-process CLI/MCP dispatch | Postgres or stdlib SQLite |
| `e2e`         | a real external process: the `qy` CLI and `qy mcp` stdio server as subprocesses | Postgres or stdlib SQLite |
| `browser`     | the real GUI frontend driven in headless Chromium (Playwright) | Playwright; Postgres for PostgreSQL fixtures, no service for SQLite fixtures |

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
make install          # install test dependencies
make seed             # create + seed the quarry_test database
make browser-install  # download headless Chromium for Playwright
```

Override the database URL with `QUARRY_TEST_DB_URL` (defaults to
`postgresql://localhost:5432/quarry_test`). Real MySQL release-contract tests run only when
`QUARRY_TEST_MYSQL_URL` is set; Redis and MySQL execution paths are otherwise
covered by mocked tests so they run everywhere.

## Coverage

The gate (`make cov`) measures **unit + integration** coverage of the `quarry`
package and fails under 95%. It deliberately excludes the `e2e`/`browser` layers
so the number reflects what the fast in-process suites alone exercise. The
frontend lives in `web/` (React + TypeScript) and is covered by the `browser`
suite (behaviorally), not by Python line coverage. Genuinely
unreachable defensive code (blocking `serve_forever`, `# unreachable` lines after
`err(...)`) is excluded via `[report]` in `.coveragerc`.

## CI

`.github/workflows/ci.yml` builds the React shell before the Rust applications
and runs Cargo checks, real-engine tests (Postgres, MySQL and Redis), and the
browser matrix against native `qy` in headless Chromium. A separate check
compiles the workspace with the declared minimum Rust version.

Direct pull requests into `main` and pushes to `main` can reuse browser
coverage from a successful CI run on their exact base commit. CI skips only
Chromium setup and the browser matrix when the diff contains approved Markdown
documentation or a Cargo version stamp with matching lockfile versions. The
embedded `CHANGELOG.md` may only add the heading for that version. CI runs the
browser matrix for other changes and when it cannot verify the diff or base.

## The React frontend (`/quarry`)

The GUI frontend is a Vite + React + TypeScript package under `web/` that
builds into `src/quarry/web_dist/`. Both Rust binaries embed the assets and
serve the workbench at `/quarry/`. Node is needed for the build.
**The browser suite tests the built bundle: run
`cd web && npm run build` after changing `web/src`.**

The React app is a drop-in replacement for the retired embedded-JS GUI: same
DOM (ids/classes), same i18n strings, same localStorage keys and value formats
— so the feature-matrix suite below, originally written against the embedded
GUI, also covers the React app. React-only features live in
`test_gui_react_app.py`; visual design-token pins live in `test_gui_visual.py`:

| # | Area | Feature | Covered by | ✓ |
|---|------|---------|------------|---|
| R1 | react | `/quarry/` mounts the app directly (not only via the `/` redirect) | test_gui_react_app:test_react_app_mounts_at_app_path, test_gui_api:test_quarry_route_has_no_workspace_segment | ✅ |
| R2 | react | `/api/version` JSON endpoint; `/` redirects to `/quarry/` | test_gui_react_app:test_api_version, test_gui_api:test_root_redirects_to_react_app | ✅ |
| R3 | react | the native release binary serves `/quarry/` and each bundled asset referenced by its index | scripts/verify_gui_install.py, release.yml native verification | ✅ |
| R4 | react | table-structure browser (issue #11): double-click a sidebar table name → modal lists column names + types (incl. quoted special-char table names) | test_gui_react_app:test_table_structure_modal_shows_columns_and_types, test_table_structure_quoted_special_char_table | ✅ |
| R5 | react | switching tables replaces the structure modal's columns — latest-wins even when the first table's /api/columns response lands late | test_gui_react_app:test_table_structure_modal_switching_tables_replaces_columns | ✅ |
| R6 | react | a stale /api/tables response for a connection the user switched away from never repaints the table panel | test_gui_react_app:test_stale_tables_response_does_not_overwrite | ✅ |
| R7 | visual | design tokens resolve to the legacy "Slate & Copper" hex values in BOTH themes (body/header/Run-button/badge via getComputedStyle); dark is the default | test_gui_visual:test_default_theme_is_dark_with_legacy_palette, test_light_theme_matches_legacy_palette | ✅ |
| R8 | visual | 14px sans app chrome + mono editor; icons render through the self-hosted tabler-icons webfont (no CDN — closes #14) | test_gui_visual:test_typography_matches_legacy, test_icons_use_selfhosted_tabler_font | ✅ |
| R9 | visual | header's lang/theme/palette buttons share one uniform `.vg-iconbtn` box (equal height/min-width/border-radius, radius tracks `--r-btn` across the style axis — voyage 0.7.0); `.ciact .iconbtn`'s smaller CI-modal icons stay 22×22px, not stretched by voyage's fixed min-width | test_gui_visual:test_header_icon_controls_share_uniform_box, test_header_iconbtn_radius_follows_style_axis, test_ciact_iconbtn_fixed_size_not_stretched_by_min_width | ✅ |
| R10 | visual | header uses Voyage's dedicated `.vg-topbar`: language, mode, and palette keep their order and the GitHub link follows them inside the toolbar; lang switching causes no horizontal reflow; all four controls share the full box spec; the topbar has transparent/zero chrome, contains no query `.vg-toolbar`, and omitting `account` renders no account placeholder; `ResultWorkbench`'s `.vg-toolbar.toolbar` independently retains its padding/surface/border chrome | test_gui_visual:test_header_toolbar_dom_order_is_lang_mode_palette, test_lang_switch_fixed_width_no_reflow, test_header_toolbar_buttons_share_full_box_spec, test_header_topbar_has_no_query_toolbar_chrome_or_account_placeholder, test_query_toolbar_keeps_its_own_chrome, test_workbench_compact_spacing | ✅ |
| R11 | visual | Voyage 0.15's Signal theme card is selectable; it resolves to `signal × current mode × classic × quiet`, renders true black/white body and header surfaces in dark/light modes, persists across reload without console errors, and does not disturb the pinned Slate baselines | test_gui_visual:test_signal_theme_four_axes_colors_and_reload_persistence, test_default_theme_is_dark_with_legacy_palette, test_light_theme_matches_legacy_palette | ✅ |
| R12 | launch | A second standalone `qy gui` opens the running workbench for the same configuration and workspace selection. A different application or workbench on the requested port is left running and Quarry binds a nearby port. Re-entry keeps that browser origin after the requested port becomes free. | Rust process tests in `apps/qy/tests/gui_launch.rs` | ✅ |
| R13 | launch | `qy gui stop` gracefully shuts down the matching standalone GUI, including on a fallback port or with a protected cloud catalog. Other workbenches and listeners are left running. Repeating the command reports no match, and a discovery proof cannot authorize shutdown. | Rust process tests in `apps/qy/tests/gui_launch.rs`; YLab cross-binary database process test for the protected workbench | ✅ |
| R14 | federation | A uniquely matching local workspace and cloud project share the local heading. Unassociated targets retain distinct rows and routes, and cloud-only rows carry a localized source badge. An exact association adopts the local database row name even when the cloud name differs, retaining both routes and the existing cloud draft. A different account keeps its own row and records. Ambiguous workspace or provider scopes stay separate. | Rust `federation_shares_a_workspace_heading_but_only_links_explicit_targets` and `federation_keeps_ambiguous_workspace_names_in_source_groups`; F:test_native_cloud_only_row_shares_workspace_group_and_shows_source, F:test_federated_account_change_keeps_local_result_and_rejects_old_inflight_result | ✅ |

## GUI feature matrix

The GUI is a React + TypeScript SPA (`web/src/`) served under `/quarry` by the
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
   qy_tabres qy_ui qy_maxrows qy_collapsed qy_hist qy_result qy_auto_run` —
   legacy keys retain their value formats; `qy_auto_run` adds scoped execution
   preferences; the obsolete `qy_assistant_language` key is ignored),
   and every `/api/*` endpoint the frontend fetches must map to a row.
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
`tests/test_gui_browser.py` (B), `tests/test_gui_browser_features.py` (F), and
`tests/test_gui_visual.py` (V).
Pure frontend logic that doesn't need a browser also gets a `web/src/**/*.test.ts`
vitest unit test (`cd web && npm run test:unit`), referenced by its file name.

| # | Area | Feature | Covered by | ✓ |
|---|------|---------|------------|---|
| 1 | header | brand + workspace label; multi-workspace count + tooltip | B:test_load_shows_brand_without_readonly_badge (single-ws only) | 🟡 |
| 2 | header | no permanent read-only / auto LIMIT badge; query safety is unchanged | B:test_load_shows_brand_without_readonly_badge, F:test_icon_hover_hints_cover_header_workbench_and_modals | ✅ |
| 3 | app shell/workbench | explicit production configuration drives muted SQL-editor/connection/tab/Run chrome; selected tabs retain one short underline, editor has no extra accent line; localized Production label occupies a fixed slot at the end of the tab bar, outside the editor; stays visible during SQL scrolling/resizing and in the zero-tab state; header stays neutral without a production label and env switching keeps controls in place | F:test_production_context_follows_connection_without_toasts, F:test_single_production_connection_context_is_localized, F:test_production_context_has_stable_layout, F:test_production_tab_context_leaves_sql_unobstructed_when_scrolled_or_resized, F:test_env_pills_default_dev_and_prod_badge, F:test_env_preview_autorun_uses_explicit_production, V:test_production_context_tokens | ✅ |
| 4 | header | health-check button: probe all, dots update, error tooltip; one batch at a time, disabled/busy during checks, ten-second request/body deadline and retry after a timeout | F:test_health_button_paints_ok_and_down_dots, F:test_health_timeout_releases_button_and_allows_a_new_batch; Rust runtime: `slow_ssh_setup_does_not_block_unrelated_tunnels_and_cannot_survive_close` covers per-target setup isolation, deduplication, cancellation recovery and shutdown | ✅ |
| 5 | header/assistant | fresh profiles default to Chinese; language toggle 中/EN reloads full chrome and persists the choice, including assistant controls and conversation history | B:test_language_toggle_switches_run_label, F:test_language_toggle_full_chrome, F:test_fresh_default_language_is_chinese_and_switch_persists, F:test_assistant_localizes_explanation_checkpoint_configuration_and_history | ✅ |
| 6 | header | mode toggle + persistence; palette selection includes Signal with persisted four-axis prefs | B:test_theme_toggle_flips_data_theme, F:test_theme_persists_after_reload, test_gui_visual:test_signal_theme_four_axes_colors_and_reload_persistence | ✅ |
| 7 | sidebar | connection groups; an ungrouped env sibling inherits the logical DB's sole group instead of creating a duplicate under OTHER; workspace origin label prefers the majority workspace when a group spans directories | B:test_load_shows_brand_without_readonly_badge (origin single-ws only), F:test_missing_group_env_sibling_stays_in_one_sidebar_env_set, A:test_ungrouped_env_member_inherits_its_only_sibling_group, A:test_group_workspace_origin_prefers_majority_source | 🟡 |
| 8 | sidebar | group collapse/expand + persistence | F:test_group_collapse_persists_after_reload | ✅ |
| 9 | sidebar | health dot states (ok/down; dimmed row; error tooltip) | F:test_health_button_paints_ok_and_down_dots | ✅ |
| 10 | sidebar | instant dot paint from backend cache (`cached=1`) | F:test_health_dots_repaint_from_cache_after_reload | ✅ |
| 11 | header | env switcher lists every env of the selected connection; pill font weight stays constant when selection changes; selected production uses a muted theme-accent tint and readable text, with ≥4.5:1 contrast pinned under Slate/Signal light themes; the sidebar never renders env pills | F:test_env_pills_default_dev_and_prod_badge, F:test_production_context_has_stable_layout, F:test_selected_prod_pills_keep_accessible_contrast_in_light_themes | ✅ |
| 12 | header | env switcher click switches env | F:test_prod_env_switch_does_not_autorun, F:test_nonprod_env_switch_restores_without_autorun, F:test_env_preview_autorun_uses_explicit_production, F:test_env_manual_sql_carries_and_autoruns_except_production, F:test_auto_run_preferences_follow_connection_environment_and_reload | ✅ |
| 13 | sidebar | explicit env switch restores destination SQL or copies source SQL into an empty editor, then executes only if Auto-run is enabled and no saved result exists; returning restores SQL and result without a request; production defaults to retaining SQL without executing and allows explicit scoped opt-in; closed groups remain closed | F:test_prod_env_switch_does_not_autorun, F:test_nonprod_env_switch_restores_without_autorun, F:test_env_preview_autorun_uses_explicit_production, F:test_env_manual_sql_carries_and_autoruns_except_production, F:test_auto_run_preferences_follow_connection_environment_and_reload, F:test_auto_run_preference_or_edit_cancels_navigation_waiting_for_tables | ✅ |
| 14 | sidebar | row click selects + opens table panel; re-click toggles panel | F:test_reclick_connection_toggles_panel | ✅ |
| 15 | sidebar | sticky global search + Collapse all; searches all database names and query names/descriptions plus cached tables/keys; no fetch on typing; empty state, Escape restoration, cross-connection result navigation and draft preservation | F:test_sidebar_search_is_global_restores_tree_and_does_not_fetch, F:test_table_filter_box, V:test_table_filter_uses_surface_tokens | ✅ |
| 16 | sidebar | table panel connection-error state / `no tables` empty state | F:test_dead_connection_click_shows_error_panel (error only) | 🟡 |
| 17 | sidebar | TCACHE instant paint + SWR background refresh | F:test_swr_refreshes_stale_table_list | ✅ |
| 18 | sidebar | redis key tree: `:` hierarchy, fold, count badges | F:test_redis_key_tree_badges_filter_and_inspect | ✅ |
| 19 | sidebar | redis type + TTL badges | F:test_redis_key_tree_badges_filter_and_inspect | ✅ |
| 20 | sidebar | global search includes cached Redis keys; key click → inspect grid | F:test_redis_key_tree_badges_filter_and_inspect | ✅ |
| 21 | sidebar | All queries index inside its owning workspace (including query-only workspaces), collapses with the workspace and independently per query section; duplicate names open the selected file; grouped by logical database; initial paramless index selection runs when Auto-run is enabled, repeated selection restores the fixed tab; explicit Run retains parameter confirmation | F:test_saved_query_without_params_runs_directly, F:test_saved_queries_group_by_logical_db, F:test_saved_queries_stay_in_owning_workspace, F:test_param_modal_enter_submits_and_clickout_closes, sidebarLayout.test.ts, F:test_auto_run_saved_query_off_allows_manual_run_and_on_runs_new_query | ✅ |
| 22 | sidebar | saved-query param modal (required/default, Enter submits, click-out closes) | B:test_saved_query_param_modal…, F:test_param_modal_enter_submits_and_clickout_closes | ✅ |
| 21a | sidebar/editor | no query counts; whole table rows expand children and open/reuse previews; indented All queries index; two-line titles and selected wrapped descriptions; saved queries reuse read-only tabs per file/environment, Copy as new query creates editable SQL; table-linked paramless queries do not auto-run; Collapse all shares the sticky sidebar search row and preserves active SQL/results; stable scrollbar gutters; Neptune label grouping; unassociated queries remain in All queries | F:test_table_query_disclosure_preserves_draft_and_does_not_execute, F:test_fixed_query_history_shortcuts_restore_editable_tab, F:test_graph_query_discovery_rows, F:test_query_titles_expand_description_without_counts, tabsStore.test.ts, tablePreview.test.ts, V:test_query_sidebar_tokens, V:test_sidebar_compact_collapse_indent_and_stable_gutter, F:test_saved_query_waits_for_tables_before_anchoring | ✅ |
| 23 | sidebar | sidebar width drag + persistence | F:test_sidebar_width_drag_persists | ✅ |
| 24 | editor | SQL highlight overlay + scroll sync | F:test_sql_highlight_overlay (scroll sync unasserted) | 🟡 |
| 25 | editor | placeholder states (no conn / sql / redis) | F:test_placeholder_states, F:test_redis_key_tree… (redis) | ✅ |
| 26 | editor | Cmd/Ctrl+Enter runs | B:test_custom_sql_via_run_keyboard | ✅ |
| 27 | editor | Cmd/Ctrl+↑↓ history walk; draft stashed and restored at the bottom | F:test_history_nav_stashes_and_restores_draft | ✅ |
| 28 | editor | draft is never silently lost: table click with existing SQL opens a new tab (previous tab keeps the draft); remaining overwrite sites (Alt+click / key / saved / recall / tab close) still push History first | F:test_table_click_preserves_draft_on_previous_tab | ✅ |
| 29 | editor | autocomplete: keywords + tables | B:test_autocomplete_keyword_and_table | ✅ |
| 30 | editor | autocomplete: `table.column` via /api/columns | F:test_autocomplete_columns_after_table_dot | ✅ |
| 31 | editor | autocomplete: FROM/JOIN table-only filter; dedup; 12-item cap | F:test_autocomplete_from_narrows_to_tables (dedup/cap unasserted) | 🟡 |
| 32 | editor | autocomplete keyboard nav (↑↓ Tab Enter Esc) + mouse pick | B/F autocomplete tests; F:test_autocomplete_escape_blocks_late_refresh covers queued input + late column lookup, then typing resumes (other keys partial) | 🟡 |
| 33 | editor | editor height drag + persistence | F:test_editor_height_drag_persists | ✅ |
| 34 | editor | tabs: add/switch/close/persist | B:test_tabs_add_switch_restore_and_close | ✅ |
| 35 | editor | tab title rule (SQL main-table, incl. quoted/backtick identifiers > first words > `db@env` > placeholder), live-updates as SQL changes, distinguishes same-connection tabs; legacy `qy_ui` migration | tabsStore.test.ts, F:test_tab_title_shows_db_at_env, F:test_tab_title_derives_from_sql_table_and_distinguishes_tabs, F:test_tab_title_updates_when_sql_switches_table, F:test_tab_title_distinguishes_quoted_mixed_case_tables, F:test_legacy_qy_ui_migrates_into_tabs | ✅ |
| 36 | toolbar | Run button + loading spinner | B:test_custom_sql_via_run_button, F:test_run_shows_loading_spinner | ✅ |
| 37 | toolbar | overlapping runs are latest-wins (stale response discarded) | F:test_stale_slow_response_does_not_overwrite | ✅ |
| 38 | toolbar | Format (uppercase + newlines) | B:test_format_button_uppercases_and_newlines | ✅ |
| 39 | toolbar | EXPLAIN: single-column plan modal + Esc closes | B:test_explain_opens_plan_modal_and_escape_closes | ✅ |
| 40 | toolbar | EXPLAIN guards: no-conn toast / redis toast / multi-col grid / disabled while running | F:test_explain_without_connection_toasts, F:test_redis_key_tree…, F:test_sqlite_explain_query_plan_and_gui_readonly (SQLite multi-column plan covered; disabled state unasserted) | 🟡 |
| 41 | toolbar | CSV export: `quarry-<db>.csv`, UTF-8 BOM, quoting/escaping | F:test_csv_export_content_bom_and_escaping, F:test_lossless_numeric_sort_and_export, F:test_empty_columns_and_duplicate_names_export | ✅ |
| 42 | toolbar | JSON export: `quarry-<db>.json`, row content | F:test_json_export_content, F:test_lossless_numeric_sort_and_export | ✅ |
| 43 | toolbar | history modal: list + recall into editor; Esc closes | B:test_history_lists_runs…, F:test_history_modal_escape_closes | ✅ |
| 44 | toolbar | history search / ago timestamps / empty Queries view with Conversations still available / 100 cap | F:test_history_search_filters, F:test_history_empty_keeps_conversations_accessible (ago + cap unasserted) | 🟡 |
| 45 | grid | render: sticky rownum, typed headers, zebra rows | B:test_click_table_renders_grid_with_types_and_status | ✅ |
| 46 | grid | cell type coloring (num/uuid/ts/bool/json/null) | F:test_cell_type_coloring, F:test_cell_json_opens_tree_modal | ✅ |
| 47 | grid | sort asc/desc, exact for bigint/decimal strings; 3rd click restores original order; arrow | B:test_sort_column_toggles_arrow_and_reorders, F:test_sort_numeric_strings_and_third_click_restores, F:test_lossless_numeric_sort_and_export | ✅ |
| 48 | grid | sort state resets on a new result | F:test_new_result_resets_sort_state | ✅ |
| 49 | grid | column width drag | F:test_column_width_drag | ✅ |
| 50 | grid | cell select; bounded preview for large values; dblclick long→chunked modal / short→copy; explicit copy always uses the full raw value (honest toast) | B:test_cell_doubleclick_no_error, F:test_cell_copy_via_keyboard_and_dblclick, F:test_large_cell_is_bounded_in_dom_and_kept_session_only, F:test_large_cell_copy_uses_full_value_not_preview, F:test_result_image_url_text_keeps_existing_double_click_actions; cellValue.test.ts | ✅ |
| 51 | grid | JSON tree modal + copy; branches start collapsed and mount children only when expanded | F:test_cell_json_opens_tree_modal | ✅ |
| 52 | grid | row-detail modal (rownum click), bounded large-cell previews; Copy record link uses the producing result connection/SQL and verified primary or unique key, copies encoded db/env/table/sql without editing or executing; ordinary PostgreSQL string literals omit E unless backslashes require escaping; loading and disabled reasons for ambiguous queries, missing keys or metadata failure | B:test_rownum_click_opens_row_detail_modal, F:test_large_cell_is_bounded_in_dom_and_kept_session_only, F:test_row_record_link_uses_result_context_and_does_not_execute, F:test_row_record_link_disabled_reason_and_metadata_failure, V:test_record_link_chrome_uses_theme_tokens, F:test_row_record_link_ordinary_string_key_omits_escape_prefix, test_row_link.py | ✅ |
| 53 | grid | keyboard nav: arrows move selection, Enter opens, Cmd+C copies | F:test_grid_keyboard_nav_and_enter_opens_modal, F:test_cell_copy_via_keyboard_and_dblclick | ✅ |
| 54 | grid | status bar: rows / elapsed / download size / avg speed (≈ + tooltip when estimated) / truncated / target; each metric (and Load more / max-rows) has a hover explanation; large results keep the session-only notice | B:test_click_table…, F:test_truncated_badge_shows, F:test_status_bar_shows_download_size_and_speed, F:test_status_bar_metric_tooltips, F:test_load_more_accumulates_download_size_and_speed | ✅ |
| 55 | grid | 0-row empty state retains available column headers | F:test_zero_rows_empty_state, F:test_empty_columns_and_duplicate_names_export | ✅ |
| 56 | grid | error pane (`.err`); network failures show a readable message | B:test_write_is_blocked…, F:test_network_error_shows_readable_message | ✅ |
| 57 | grid | bounded result persisted to localStorage and restored after reload; results over 512 KiB remain in-session only with a visible status notice | F:test_editor_and_result_restored_after_reload, F:test_large_cell_is_bounded_in_dom_and_kept_session_only; tabsStore.test.ts | ✅ |
| 58 | grid | Escape closes the topmost modal | B:test_explain…, F:test_cell_json…, F:test_history_modal_escape_closes | ✅ |
| 59 | global | toast styles + durations (ok vs error) | F:test_cell_copy_via_keyboard_and_dblclick (ok style) + error-toast presence in guards | ✅ |
| 60 | global | read-only rail end-to-end | B:test_write_is_blocked_with_readonly_error | ✅ |
| 61 | global | full state restore after reload (conn + sql + result + widths + collapse) | F:test_editor_and_result_restored…, F:test_group_collapse…, F:test_sidebar_width…, F:test_editor_height… | ✅ |
| 62 | global | zero console errors as an invariant | F autouse `_console_clean`; B:test_no_console_errors_after_normal_flow | 🟡 |
| 63 | sidebar | redis key list cap notice ("showing first N keys") | F:test_redis_capped_key_list_shows_notice | ✅ |
| 64 | sidebar | generated table SQL quotes mixed-case/reserved identifiers; SQLite always quotes each full table name, including literal dots | F:test_mixed_case_table_click_is_quoted, F:test_sqlite_browse_structure_quoted_tables_and_file_info, tablePreview.test.ts | ✅ |
| 65 | toolbar | max-rows selector: localized row-count labels, a decorative inset chevron with clear text separation, native keyboard operation, and 100/500/2000/5000 result caps persisted across reloads | F:test_max_rows_selector_caps_and_persists, F:test_row_limit_selector_has_readable_label_and_keyboard_control, V:test_row_limit_selector_geometry | ✅ |
| 66 | header / controls | native hover hints for header, tabs, toolbar, sidebar folds and modal actions; Voyage hints follow locale and mode | F:test_icon_hover_hints_cover_header_workbench_and_modals (en/zh, mode and reveal state), F:test_cell_json_opens_tree_modal, F:test_redis_key_tree_badges_filter_and_inspect; overflow arrows audited in source only | 🟡 |
| 67 | tabs | per-tab result isolation: grid / status / export always reflect the active tab | F:test_tab_switch_isolates_results | ✅ |
| 68 | tabs | closing a tab pushes its SQL to History (active + inactive close) | F:test_close_tab_preserves_sql_in_history | ✅ |
| 69 | tabs | tab with a vanished connection unbinds (never silently rebinds) | F:test_stale_tab_connection_unbinds_not_rebinds | ✅ |
| 70 | sidebar | current-table highlight follows exact table previews on tab/connection changes and reload; edited/custom SQL clears it | F:test_table_click_highlights_current_table, F:test_tab_preview_selection_restores_and_preserves_edited_sql | ✅ |
| 71 | sidebar | list refresh on active database row, shown on hover/focus without layout shift; no standalone toolbar row; click preserves SQL, connection and open panel; 30s timeout ends loading, failed refresh restores cached lists | F:test_table_list_timeout_ends_spinner, F:test_failed_refresh_keeps_cached_tables_and_stops_loading, F:test_table_list_manual_refresh, V:test_database_refresh_reveals_without_layout_shift (Redis fetch path shared; Redis refresh unasserted) | 🟡 |
| 72 | sidebar | table list cap notice at 5000 | backend: test_api_tables_capped_flag_at_5000 (UI note unasserted) | 🟡 |
| 73 | sidebar | Alt+click inserts generated SQL without running | F:test_alt_click_inserts_without_running | ✅ |
| 74 | tabs | per-tab result persistence: every bounded tab result survives a reload (`qy_tabres`), each restored under its own connection; unchanged results are not reserialized on SQL input/tab switches | F:test_per_tab_results_persist_across_reload; tabsStore.test.ts | ✅ |
| 75 | tabs | an in-flight request that lands after a tab switch is stored on its origin tab, never the now-active one | F:test_slow_response_routes_to_origin_tab_not_active | ✅ |
| 76 | tabs | a result is tagged with its producing connection; switching to another env never restores the old group's grid on reload — incl. the legacy `qy_result` upgrade path (env, not just db, must match) | F:test_result_stays_in_dev_group_after_prod_reload, F:test_legacy_qy_result_env_mismatch_not_restored, F:test_legacy_qy_result_env_match_restored | ✅ |
| 77 | tabs | switching environment during an in-flight request routes the response to its original group and restores it on return, never repainting the new environment | F:test_inflight_response_returns_to_origin_env_group | ✅ |
| 78 | toolbar | EXPLAIN single-column modal is suppressed if its tab was switched / re-pointed while the plan was in flight | implemented (`#expBtn` handler audits TABREQ/tab/connection); browser test tracked in #18 | 🟡 |
| 79 | tabs | a saved query runs on its OWN connection; launched from another connection, it opens/selects the producing group and preserves the original group's draft; its result is tagged/persisted under the producing connection. The owning workspace wins over an unrelated selected workspace with the same database label. Exact requests must target the owner or a verified environment sibling. | F:test_saved_query_result_persisted_under_producing_connection, F:test_saved_query_prefers_owner_over_same_name_selected_workspace, Rust `exact_saved_query_target_respects_owner_and_allows_related_environment` | ✅ |
| 80 | header | connection-info button (`#ciBtn`): visible only when a connection is selected; opens the resolved-config modal | F:test_conn_info_modal_shows_resolved_config_and_health | ✅ |
| 81 | header | connection-info modal (`/api/conninfo`): resolved key/engine/env/host/port/database/source file; SQLite shows its absolute database-file path and no server host/port; URL password always masked; live reachability probe with the raw error on failure | F:test_conn_info_modal_shows_resolved_config_and_health, F:test_sqlite_browse_structure_quoted_tables_and_file_info (ok path; error text asserted at API level in test_gui_api.py) | ✅ |
| 82 | header | conn-info url row: eye toggles masked↔revealed (`?reveal=1`); copy puts the real URL on the clipboard | F:test_conn_info_url_eye_toggles_and_copy_copies_real_url | ✅ |
| 83 | header | conn-info action “Create local env” (`POST /api/local/up`) is offered for a configured Postgres or Redis environment set without a local member. The exact workspace routes used to distinguish same-name local targets hide this action. Creation uses the persisted engine port, writes a new local beside the source connection, reuses it within the same project scope, and namespaces the physical Postgres DB by group/workspace. A fresh Postgres environment runs the initial schema sync and reports a sync failure without undoing creation. | F:test_conn_info_offers_create_local_when_set_has_none (unambiguous route visibility), A:test_api_local_up_orchestration, A:test_api_local_up_routes_registration_to_source_workspace, A:test_api_local_up_reports_sync_failure_without_undoing_up. test_local_docker.py covers the underlying container functions. The exact workspace route exclusion has no browser assertion. | 🟡 |
| 84 | header | conn-info action “Sync schema from {env}” (`POST /api/local/sync`) is offered on a local Postgres environment with an eligible source. The exact workspace routes used to distinguish same-name local targets hide this action. Sync requires confirmation and rejects non-local targets with the CLI's exit-code-9 invariant. | F:test_conn_info_offers_sync_on_local_env (unambiguous route visibility), A:test_local_sync_endpoint_refuses_non_local. test_local_sync_docker.py covers swap behavior. The exact workspace route exclusion has no browser assertion. | 🟡 |
| 85 | grid | real pagination: a truncated result offers "load more" (same SQL, growing `OFFSET`), appending rows until the tail page isn't truncated; only offered for postgres/mysql/sqlite results produced by Run (not saved-query params, not redis/neptune, which can't page this way) | F:test_load_more_paginates_truncated_result, F:test_sqlite_load_more_sort_export_and_tab_restore, A:test_query_offset_pages_through_results, A:test_sqlite_api_metadata_and_pagination | ✅ |
| 86 | grid | "load more" on an already-sorted grid re-sorts the combined rows (not just the new page) so the active sort + its arrow stay correct across pages | F:test_load_more_keeps_active_sort_applied | ✅ |
| 87 | header | workspace manager (`#wsBtn`): list config.toml-registered workspaces (flags missing dir / no connections.toml), add a new one, remove one (confirm-gated); takes effect without dropping an explicit `--workspace` session; removing the workspace behind the currently active connection unbinds it after the connection tree refresh (no reload or tab switch needed) | F:test_workspace_manager_add_and_remove, F:test_workspace_manager_remove_unbinds_active_connection_immediately, A:test_api_workspace_add_and_remove_round_trip, A:test_api_workspace_add_and_remove_keep_explicit_workspace_session, A:test_workspaces_endpoints_through_http; Rust service: `workspace_status_resolves_tilde_and_relative_paths_without_hiding_missing_entries` covers resolved paths, missing entries and proxy state without rewriting configuration | ✅ |
| 88 | tabs | double-click a tab renames it (Enter/blur commits, Escape reverts); an empty name reverts to the automatic db@env / SQL title; the custom title persists across reloads | F:test_tab_rename_persists_and_empty_reverts | ✅ |
| 89 | tabs | drag-and-drop reorders tabs; the active tab (and its per-tab result/SQL) follows its id, not its old index | F:test_tab_drag_reorder_moves_active_tab | ✅ |
| 90 | tabs | middle-click closes a tab (same as the × glyph); the last tab closes to the empty workbench | F:test_tab_middle_click_closes | ✅ |
| 91 | tabs | Cmd/Ctrl+Shift+W closes the active tab (Cmd+W-style; real Ctrl/Cmd+W can't be intercepted from a page); the last tab closes to the empty workbench | F:test_tab_keyboard_shortcut_closes_active_tab | ✅ |
| 92 | header | env-set ordering: `local` always sorts first regardless of connection-registration order (header switcher); with no `local` env, registration order is unchanged; default-selected env is `dev` if present, else `local`, else the first registered env | F:test_local_env_sorts_first_and_is_default_without_dev, A:test_local_env_always_sorts_first, A:test_group_structure (registration order preserved without local) | ✅ |
| 93 | sidebar | clicking a table generates+runs `select * from <table>` (no baked-in LIMIT); the toolbar max-rows cap (default 500) plus "load more" bound the result | B:test_click_table_renders_grid_with_types_and_status | ✅ |
| 94 | toolbar/tabs | query deep links: copy current `db/env/sql` as a shareable URL; opening it reuses an identical tab or creates one, restores SQL/connection, runs only when the destination Auto-run preference is enabled, and guards invalid link targets with an explicit toast (no silent auto-run failure) | F:test_copy_query_link_copies_db_env_sql, F:test_query_deeplink_opens_existing_tab_and_autoruns, F:test_query_deeplink_invalid_env_shows_notice_and_skips_autorun, F:test_query_deeplink_respects_auto_run_preference_and_production_default | ✅ |
| 95 | app shell | live workspace refresh: `/api/events` SSE + backend file watcher — editing `connections.toml` / `queries/**/*.sql` on disk refreshes the sidebar without a manual reload (+ confirmation toast) | F:test_workspace_file_change_refreshes_sidebar_live (toast unasserted; watcher + SSE framing unit-covered in test_gui_backend.py) | ✅ |
| 96 | app shell | upgrade-reload banner: after the backend restarts with a new version, the EventSource reconnect re-checks `/api/version` and shows a persistent "reload page" banner | F:test_server_upgrade_prompts_reload_banner | ✅ |
| 97 | header | `GET /api/update` checks GitHub stable releases for standalone `qy` with a 30-minute cache; `QUARRY_UPDATE_CHECK=0` disables the notice, and embedded workbenches follow the host release. The badge opens current/latest versions, `qy update` for managed installs, the native installation guide and release notes in new tabs | F:test_update_badge_hidden_when_no_new_version, F:test_update_badge_visible_and_dot_uses_accent_token_in_both_themes, F:test_update_panel_shows_upgrade_path_for_install; Rust HTTP contract embedded_workbench_hides_standalone_updates | ✅ |
| 98 | header | What's New panel: `GET /api/changelog` serves the embedded CHANGELOG.md parsed into version sections; a version change from the `qy_last_seen_version` recorded in localStorage auto-shows the panel with the entries for the new version(s), marks it seen immediately, and stays hidden on reload (the first load sets the baseline silently) | F:test_whats_new_hidden_on_first_ever_load, F:test_whats_new_shows_entries_after_upgrade_then_stays_hidden_on_reload, F:test_whats_new_background_uses_bg1_token_in_both_themes; Rust changelog parsing test, scripts/verify_gui_install.py native archive check | ✅ |
| 99 | header | env switcher shows a proxy badge (`data-testid="proxy-badge"`) only for connections with a currently-live SSH tunnel that's actually routed through the proxy right now (an observed tunnel-pool fact, not a prediction) — server-computed, not guessed client-side; hidden when the workspace toggle is off, nothing was discovered, or no tunnel has been established for that connection yet | F:test_proxy_badge_shown_only_on_the_tunneled_env_when_proxy_active, F:test_proxy_badge_hidden_when_workspace_toggle_off, F:test_proxy_badge_hidden_when_nothing_discovered, F:test_proxy_badge_follows_server_state_across_reload; `/api/connections`'s `proxied` field unit-covered in test_gui_api.py; Rust runtime: `proxy_badge_requires_exact_live_tunnel_including_shared_owner` covers unopened/missing-key connections, shared live owners, changed target/proxy dimensions and stale PID rejection | ✅ |
| 100 | header | workspace manager shows each registered workspace's proxy toggle state and the currently discovered proxy address (`data-testid="ws-proxy-status"`) | F:test_workspace_manager_shows_proxy_status_per_workspace, F:test_workspace_manager_shows_proxy_off_when_toggle_disabled; `/api/workspaces`'s `proxyEnabled`/`proxyDiscovered` fields unit-covered in test_gui_api.py | ✅ |
| 101 | header | compact tunnel keep-alive control: single clickable status badge (`down`/`up`/`reconnecting`) toggles start/stop with mouse, Enter or Space; action tooltip and pressed state; no separate play/stop icon backed by `/api/keepalive*`, matching `qy status` semantics across GUI/keeper locale and timezone differences; unverifiable legacy PID and tunnel records fail closed | F:test_header_keepalive_badge_and_toggle, F:test_header_keepalive_badge_tracks_background_state, V:test_keeper_control_tokens; U:test_process_identity_is_independent_of_caller_locale_and_timezone, test_legacy_keeper_identity_needs_pid_file_start_time_proof, test_legacy_reused_pid_never_signalled_even_if_ps_text_matches, test_pre_v2_registry_identity_is_not_reused_even_with_open_port; `/api/keepalive`, `/api/keepalive/up`, `/api/keepalive/down` unit-covered in test_gui_api.py | ✅ |
| 102 | grid/tabs | multi-megabyte cells never enter grid/row-detail DOM attributes or text in full; grid preview is 512 chars, inspector text grows in 20k chunks, JSON children mount lazily, and >512 KiB results skip synchronous persistence while remaining fully copyable/exportable in the current session | F:test_large_cell_is_bounded_in_dom_and_kept_session_only, F:test_large_cell_copy_uses_full_value_not_preview, test_gui_visual:test_large_result_session_badge_inherits_status_token; cellValue.test.ts, tabsStore.test.ts | ✅ |
| 103 | sidebar/editor | PostgreSQL table browser includes non-system schemas as `schema.table`; qualified tables can be clicked to run a preview, double-clicked for columns, and used for qualified column completion; public-table short names remain compatible | F:test_non_public_schema_table_is_visible_and_clickable, A:test_list_and_describe_non_public_postgres_table, A:test_list_tables_postgres | ✅ |
| 104 | app shell | browser chrome uses the crystal-in-rock SVG favicon served from the GUI's `/quarry/` base | F:test_app_uses_distinct_svg_favicon, A:test_gui_html_and_favicon_asset | ✅ |
| 105 | sidebar/tabs | clicking a table reuses an empty active tab or an existing same-table preview tab; otherwise opens a new tab and never overwrites a tab that already has SQL | F:test_table_click_opens_new_tab_without_overwrite, F:test_table_click_reuses_same_table_preview_tab, tablePreview.test.ts, F:test_auto_run_mode_controls_new_and_reused_table_previews | ✅ |
| 106 | app shell | address bar uses `/quarry/` with no workspace segment and `document.title` tracks the active `db`/`env`/`table` (schema-qualified when the SQL is); opening a focus URL restores that selection in a new/reused/empty tab without auto-running or rebinding a nonempty draft | F:test_selection_updates_url_and_title, F:test_focus_url_restores_selection_without_autorun, F:test_focus_url_opens_new_tab_when_active_tab_has_sql, F:test_handwritten_schema_sql_keeps_schema_in_focus_url, F:test_quarry_path_tracks_connections_and_copied_links, F:test_quarry_focus_url_restores_table_without_running, queryLink.test.ts, tablePreview.test.ts, tabsStore.test.ts | ✅ |
| 107 | sidebar/tabs | selecting Neptune opens/reuses a connection-bound tab with `MATCH (n) RETURN n LIMIT 25`; initial connection selection waits for explicit Run; explicit environment switches execute SQL only with Auto-run enabled (non-production defaults on); configured production shows a Production label at the end of the tab bar; existing drafts and loaded starter results are preserved | F:test_neptune_connection_opens_starter_without_autorun, connectionStarter.test.ts | ✅ |
| 108 | sidebar | mixed-engine workspace groups list connections in postgres → mysql → sqlite → redis → neptune order, with a quiet mono engine suffix (not a filled chip or section header) distinct from the workspace origin path | F:test_sidebar_groups_mixed_engines_under_workspace_group, test_gui_visual:test_engine_tag_differs_from_workspace_origin, sidebarLayout.test.ts | ✅ |

| 109 | tabs/sidebar | tabs scoped by workspace/db/env; connection selection restores most recently used tab and environment; Escape clears global search, collapsed table group reopens on tab navigation and table selection returns, with SQL/results/export preserved across reload | F:test_connection_tabs_restore_mru_table_and_env, F:test_tabs_do_not_move_between_workspaces_with_same_db | ✅ |
| 110 | tabs | no automatic tab-count cap; single-row horizontal scroll with minimum width, active-tab reveal, fixed add/list controls with matching 26px heights, icon sizes and vertical centers; searchable list, no-match state, outside/Escape dismissal, Arrow/Home/End navigation | F:test_tab_overflow_search_keyboard_and_scoped_bulk_close; test_gui_visual:test_tab_menu_uses_surface_tokens_and_fixed_controls | ✅ |
| 111 | tabs | active close selects same-group MRU; last close leaves a zero-tab empty workbench; close all/others/right stays in group and saves every closed draft in History | F:test_tab_close_uses_mru_and_keeps_last_close_in_same_group, F:test_tab_overflow_search_keyboard_and_scoped_bulk_close, F:test_close_last_tab_shows_empty_and_survives_reload, F:test_close_all_tabs_is_scoped_and_empty_group_stays_empty, F:test_closed_query_response_cannot_resurrect_tab | ✅ |

| 112 | tabs | zero-tab workbench hides editor/results, keeps connection and sidebar, offers New tab and History; new/recall/table/saved/key entry points create an editor on demand | F:test_close_last_tab_shows_empty_and_survives_reload, F:test_close_all_tabs_is_scoped_and_empty_group_stays_empty, F:test_saved_query_opens_from_empty_group, F:test_alt_table_opens_editor_from_empty_without_running, F:test_redis_key_opens_editor_from_empty_group; test_gui_visual:test_empty_workbench_uses_tokens | ✅ |

| 113 | toolbar/editor | Split Run button with a fixed-width Manual/Auto mode menu controls new/reused table previews, environment navigation, query links and opening saved queries; persisted per workspace/db/env/production flag, production defaults off with explicit opt-in, unknown metadata stays off; changing the preference or typing never executes SQL or replays pending navigation; explicit Run remains available | F:test_auto_run_mode_controls_new_and_reused_table_previews, F:test_auto_run_preferences_follow_connection_environment_and_reload, F:test_query_deeplink_respects_auto_run_preference_and_production_default, F:test_auto_run_saved_query_off_allows_manual_run_and_on_runs_new_query, F:test_auto_run_preference_or_edit_cancels_navigation_waiting_for_tables, F:test_auto_run_is_disabled_when_production_metadata_is_unknown, V:test_run_mode_menu_tokens_and_geometry, F:test_auto_run_opt_in_does_not_cross_connection_identity_change, F:test_run_mode_menu_keyboard_dismissal_and_selection_never_execute, F:test_run_mode_menu_closes_when_connection_or_tab_context_changes | ✅ |

| 114 | SQLite | file connections share environment groups; local/dev/prod point to distinct files; explicit production disables initial Auto-run and opt-in never executes SQL; table/view browsing, reserved/dotted table previews preserve drafts, structure types, absolute path info, no Docker actions | F:test_sqlite_browse_structure_quoted_tables_and_file_info, F:test_sqlite_environment_files_and_production_autorun, A:test_sqlite_connection_info_has_resolved_file_and_no_server, A:test_sqlite_api_metadata_and_pagination | ✅ |
| 115 | SQLite | tabular EXPLAIN QUERY PLAN, GUI write rejection, real SELECT/PRAGMA pagination with sort reapplied, active-tab exports and persisted results, latest-wins and origin-tab routing; copied record links use the producing SQL and select the expected row | F:test_sqlite_explain_query_plan_and_gui_readonly, F:test_sqlite_load_more_sort_export_and_tab_restore, F:test_sqlite_pragma_load_more_does_not_repeat_first_page, F:test_sqlite_latest_response_stays_on_origin_tab, F:test_sqlite_record_link_uses_producing_sql_and_selects_one_row, A:test_sqlite_gui_api_cannot_enable_writes | ✅ |
| 116 | SQLite/workspaces | two real workspaces with distinct connection keys/db names resolve the same `sqlite:///data/local.db` against their own source; conn-info shows each database/config path; connection switches and reload preserve each workspace's draft/result, JSON/CSV export uses the active workspace and does not execute drafts | F:test_sqlite_workspaces_resolve_same_relative_path_and_restore_own_results | ✅ |
| 117 | SQLite/saved queries | supplied parameter values filter the real database but stay out of persisted SQL in `qy_tabres`, `qy_tabs` and `qy_hist`; the parameterized template and selected result survive tab switching/reload, and closing the tab saves only the template to History | F:test_sqlite_saved_query_parameters_do_not_persist_in_browser_storage | ✅ |
| 118 | editor/assistant | collapsed 32px rail with the full vertical SQL Assistant / SQL 助手 label stays on the left inside the SQL input; expanded chat is left of the editor and reaches the query toolbar's bottom without overlapping result headers, including editor/window/composer resizing; expanded editor minimum 200px keeps the composer visible, collapsed input remains 88px; query controls remain below the right editor; dragging down can fill the available height above status (or section bottom without results), with controls visible after window/toolbar changes; dragging back or hiding restores results; hide restores a full-width toolbar, minimizes editor and expands results; reopen restores height; 6px vertical divider shares the horizontal bar's colors, transposed grip and hover feedback; its 1px border is on the left and the right border is absent; its 1px bottom border matches and joins the toolbar border without a gap; keyboard focus has an accent outline, and mouse/keyboard resizing preserves width; 22×26px close handle centered at the top of the divider; messages begin at panel top with normal 14px inner spacing; click and keyboard close are independent of divider resizing, no repeated title/send/context/maximize controls | F:test_assistant_layout_collapses_inside_editor_and_reopens, F:test_assistant_panel_stays_above_result_headers_after_resizing, F:test_assistant_editor_can_expand_to_status_bar_and_restore_results, F:test_assistant_divider_restores_width_after_collapse, V:test_assistant_chrome_tracks_theme_tokens, V:test_assistant_markdown_chrome_tracks_theme_tokens | ✅ |
| 119 | assistant/composer | Enter sends, Shift+Enter adds a line, IME confirmation does not send; unsent draft and accepted explanation intent survive hide/reopen and tab switches; missing model preserves the attempted message with a CLI configuration hint; no model configuration GUI | F:test_assistant_enter_shift_enter_and_ime_composition, F:test_assistant_unsent_draft_and_explanation_intent_survive_hide_and_tab_switch, F:test_assistant_unconfigured_preserves_input_and_only_shows_cli_hint | ✅ |
| 120 | assistant/context | opening existing SQL makes no model request; an empty composer offers a faint localized overlay showing the complete explanation prompt with no keyboard instructions; Tab fills exactly the displayed text only, Enter sends explain=true; empty SQL, existing drafts and IME do not accept the suggestion; explanation never changes or executes SQL | F:test_assistant_suggested_explanation_requires_tab_then_enter, F:test_assistant_explanation_suggestion_respects_drafts_empty_sql_and_composition | ✅ |
| 121 | assistant/stream | replies render incrementally with compact line/paragraph spacing in an independently scrolling history with a fixed composer; follows new text at the bottom even when a queued scroll event arrives as the reply grows; respects a reader scrolling upward; provider failures/interrupted streams cannot apply partial SQL | F:test_assistant_stream_scroll_keeps_composer_fixed_and_respects_reader, F:test_assistant_markdown_stream_closes_fence_and_keeps_composer_fixed, F:test_assistant_failed_reply_cannot_apply_partial_sql, V:test_assistant_chrome_tracks_theme_tokens | ✅ |
| 122 | assistant/editor | completed edits apply only to unchanged origin SQL and create a checkpoint; Restore previous SQL keeps chat and both drafts in SQL history, marks the original change as restored and does not create a restore chain even with rapid clicks or legacy history; a lost successful Apply response can be retried without duplicate checkpoints; changing SQL never executes, existing Run does | F:test_assistant_apply_restore_preserves_drafts_and_only_run_executes, F:test_assistant_restore_repeated_clicks_do_not_create_an_undo_chain, F:test_assistant_inflight_manual_edit_requires_explicit_application, F:test_assistant_manual_edit_during_apply_roundtrip_remains_restorable, F:test_assistant_edit_cancels_pending_environment_autorun, F:test_assistant_apply_lost_response_retry_preserves_drafts_and_checkpoints | ✅ |
| 123 | assistant/tabs | late replies after manual editing, tab/environment navigation or closing cannot overwrite the new context or resurrect a closed tab; an edited origin needs explicit Apply SQL; late history reads/errors cannot replace a newer reply or configuration error | F:test_assistant_inflight_manual_edit_requires_explicit_application, F:test_assistant_late_reply_cannot_overwrite_other_context, F:test_assistant_late_history_load_cannot_replace_a_new_reply, F:test_assistant_late_history_error_preserves_configuration_error_and_draft | ✅ |
| 124 | history/conversations | conversations persist independently of tabs on the local backend; History → Conversations remains accessible with zero tabs, restores SQL/messages/checkpoints without execution, reuses an open conversation without replacing its running or newer reply/checkpoint and flushes latest manual SQL on close | F:test_assistant_conversation_reopens_closed_tab_with_checkpoints, F:test_assistant_close_flushes_latest_manual_sql_to_history, F:test_assistant_history_search_empty_and_unavailable_connection, F:test_assistant_existing_conversation_loads_when_model_is_unconfigured, F:test_assistant_close_during_conversation_creation_keeps_latest_sql, F:test_assistant_history_reopen_preserves_live_session | ✅ |
| 125 | assistant/saved queries | changing an immutable saved-query template creates an editable draft and leaves the source template intact, without executing either query | F:test_assistant_saved_query_edit_creates_draft_preserves_template | ✅ |
| 126 | assistant/Markdown | assistant replies render headings, emphasis, lists, inline/fenced code, tables and blockquotes in streams and recovered history; user/system messages stay literal; code examples cannot apply or run SQL; raw HTML, executable/local URLs and remote image requests are blocked; external links open separately; code/tables scroll within a narrow panel; headings use the theme accent and SQL keywords/strings/numbers/comments use distinct theme tokens | F:test_assistant_markdown_formats_reply_and_reopened_history_without_sql_changes, F:test_assistant_markdown_blocks_html_unsafe_urls_and_remote_images, F:test_assistant_markdown_stream_closes_fence_and_keeps_composer_fixed, F:test_assistant_markdown_wide_code_and_tables_scroll_inside_narrow_panel, V:test_assistant_markdown_chrome_tracks_theme_tokens | ✅ |
| 127 | assistant/language | no reply-language controls appear on hover or keyboard focus; the GUI reads the CLI-owned preference and backend-resolved system language, independently of interface language and obsolete localStorage; reopening/focus/visibility/reload reads CLI updates without changing drafts/SQL/history or sending model/query requests; the preference affects GUI replies only | F:test_assistant_has_no_reply_language_settings_on_hover_or_focus, F:test_assistant_cli_reply_language_preserves_drafts_history_and_tab_state, F:test_assistant_reply_language_rereads_cli_and_system_changes, V:test_assistant_chrome_tracks_theme_tokens | ✅ |
| 128 | native connection sources | a merged cloud environment keeps its human label and Cloud badge even when a configured connection has the same environment name. Workspace settings and Tunnel Keeper remain available. Metadata, execution and cancellation use the distinct cloud route ID. Cloud connection info omits credentials and local administration actions. Each environment restores its own SQL and result. The SQL assistant stays available on unambiguous configured local routes. Cloud routes and exact workspace routes used to distinguish same-name local targets disable it. | F:test_native_cloud_source_separates_same_name_targets_and_cancel covers the cloud/local split and verifies assistant availability on an unambiguous local route. V:test_native_cloud_badge_uses_theme_tokens covers badge styling. Row 129c covers exact workspace selection and state, with no assertion for its disabled assistant. | 🟡 |
| 129 | native cloud availability | adding a cloud dev environment preserves the original configured default; focus refresh updates the cloud catalog and status; a temporarily missing cloud target retains its draft and binding without falling back to a local connection; local queries work through the outage and reconnect never replays SQL | F:test_native_cloud_outage_preserves_drafts_and_local_queries | ✅ |
| 129a | provider accounts | changing the cloud account hides the previous owner's SQL, rows, and history while leaving the local draft and result available. Returning to the verified owner restores its private draft. A request revoked during the change cannot repaint the grid. | F:test_federated_account_change_keeps_local_result_and_rejects_old_inflight_result; Rust `workbench_projection_keeps_other_accounts_drafts_private_on_update` | ✅ |
| 129b | provider sources | the native workbench labels registered providers and routes same-name local and cloud databases independently, even when two providers use the same upstream resource ID. A cloud database shares a local database row only after association with its exact local endpoint. Replacing a local endpoint invalidates its association. | Rust `federation_shares_a_workspace_heading_but_only_links_explicit_targets`; F:test_registered_providers_keep_same_name_database_routes_separate, F:test_native_cloud_source_separates_same_name_targets_and_cancel | ✅ |
| 129c | local workspace identity | same-name database environments display workspace-qualified pills. Selecting one opens its exact connection without copying or replaying another workspace's SQL. A renamed display label retains the exact target's draft and result. | F:test_duplicate_workspace_targets_are_selectable_and_survive_label_changes | ✅ |
| 130 | header | the logo and Quarry name share a link to the English or Chinese project homepage; a labeled GitHub icon inside VoyageToolbar follows the palette control and links to the project repository; both open in new tabs | F:test_header_project_links_open_in_new_tabs, V:test_header_project_links_use_theme_tokens, V:test_header_toolbar_buttons_share_full_box_spec | ✅ |
| 131 | themes | Trace Paper, Exam Hall, and IELTS Lab are selectable in the Voyage palette; each applies its four-axis preset and background, keeps the selected theme across a mode toggle and reload, and shows the active palette card | F:test_voyage_theme_presets_apply_and_persist | ✅ |

### Result image coverage

| ID | Region | Behavior | Evidence | Status |
|----|--------|----------|----------|--------|
| 132 | grid | Visible HTTP(S) URLs are lazily loaded for browser image decoding; Auto/Image/Text controls appear only after a URL in that column loads with nonzero natural dimensions and stay scoped to tab and column schema; enlarge, copy URL, open original and Escape preserve raw sort/export values | `imageDetection` via resultImage.test.ts (23 unit cases, including single-pass indexing and isolated column confirmation); F:test_result_images_preview_modes_sort_export_and_tab_isolation, F:test_result_image_controls_follow_column_names_and_values, F:test_result_images_keep_confirmed_control_when_text_mode_loads_more | ✅ |
| 133 | grid/images | viewport-only requests; 15-second timeout and failure stop automatic loading and expose retry; original URLs stay visible; standard browser HTTP caching reuses full signed URLs without stripping parameters; referrer omitted | F:test_result_images_lazy_load_failure_timeout_and_retry, F:test_result_images_use_http_cache_and_keep_signed_urls_distinct | ✅ |
| 134 | grid/images | borderless thumbnails preserve the same row height as text mode; mode control and enlarged preview use theme tokens in dark/light mode; modal fits a narrow viewport | V:test_result_image_chrome_uses_theme_tokens | ✅ |

### Hosted workbench matrix

The following rows cover the platform-marked built bundle. Standalone rows above
continue to apply to standalone mode; hosted mode deliberately excludes workspace
administration, credential reveal and shared saved-query management.

| # | Area | Feature | Covered by | ✓ |
|---|------|---------|------------|---|
| H1 | offline/editor | editor reopens with cloud disconnected; browser records use origin/account scope and never load into another account | F:test_platform_disconnected_editor_keeps_account_scoped_draft_without_queries | ✅ |
| H2 | targets/results | authorized cloud resources use stable IDs; a cloud query and its current result use the hosted API, and the query is recalled from the same browser after reload | F:test_platform_cloud_query_history_stays_in_browser | ✅ |
| H3 | connections | background retry restores the authorized cloud directory without clicks, SQL replay or lost drafts | F:test_platform_background_reconnect_preserves_draft_without_query_replay | ✅ |
| H4 | assistant | request-scoped cloud generation; proposals/apply/restore/reload use browser conversation history without automatic execution | F:test_platform_assistant_conversation_proposal_restore_stays_in_browser; F:test_platform_late_assistant_reply_preserves_manual_sql_after_history_reopen; F:test_platform_closing_tab_during_assistant_stream_keeps_latest_manual_sql | ✅ |
| H5 | Local/schema administration | hosted workbench exposes only cloud resources and no Local administration, pairing, or companion access | F:test_platform_cloud_only_discards_legacy_companion_launch_and_saved_pairing; platform backend tests | ✅ |
| H6 | browser storage failure | failed browser writes retain current drafts in memory, disclose the failure, and offer explicit recovery export | F:test_platform_browser_storage_failure_preserves_and_exports_draft | ✅ |
| H7 | browser history | history and drafts stay in this browser, while query rows stay in the open page. Clearing browser storage removes access to those records. Query results do not evict query SQL or the current draft | F:test_platform_cloud_query_history_stays_in_browser; F:test_platform_browser_storage_keeps_sql_without_oversize_results | ✅ |
| H8 | cancellation | request-ID cancellation and latest-wins suppress a response after cancellation; a delayed cancellation acknowledgement cannot clear a newer query's pending state or discard its result on the same tab | F:test_platform_cancel_query_never_repaints_grid_from_late_response; F:test_platform_late_cancel_ack_preserves_new_query_pending_and_result | ✅ |
| H9 | status | header badge shows cloud connection and browser storage only, with no connection or administration dialog. Both themes and languages are covered | V:test_platform_status_chrome_uses_theme_tokens; F:test_platform_status_follows_language_toggle_without_connection_controls | ✅ |
| H10 | offline assets | previously loaded static shell and versioned bundled icon fonts reopen with the entire network offline, including an existing worker upgrade; actual font responses come from the service worker with HTTP cache disabled; arbitrary asset queries, API and personal responses remain outside CacheStorage | F:test_platform_cached_shell_reopens_with_entire_network_offline; F:test_platform_offline_icon_font_keeps_version_query_and_rejects_other_queries (fresh + existing worker) | ✅ |
| H11 | IAM refresh | assistant cloud denial and refresh failure preserve browser drafts. External refresh uses the configured account session | F:test_platform_cloud_failure_does_not_lose_browser_history; F:test_platform_external_runtime_session_and_configured_refresh | ✅ |
| H12 | hosted labels | database rows omit the current project prefix, retain full identity in tooltips and dispatch queries to the original resource; connection details are collapsed by default in the header | F:test_platform_header_connection_badge_and_group_relative_labels | ✅ |
| H13 | legacy launch | existing pairing keys and legacy companion fragments are discarded without fetching localhost, displaying pairing UI, or executing SQL | F:test_platform_cloud_only_discards_legacy_companion_launch_and_saved_pairing | ✅ |
| H14 | hosted sign-in | the image's built-in sign-in handles failed credentials, successful query access and logout; anonymous external mode offers the configured platform login without querying resources | F:test_platform_official_builtin_login_failure_success_and_logout; F:test_platform_external_anonymous_entry_uses_configured_login_link; V:test_platform_builtin_login_chrome_uses_theme_tokens | ✅ |
| H15 | runtime identity | hosted session keys and refresh paths come from validated same-origin runtime configuration; invalid cross-origin paths fail closed rather than using the Console session | F:test_platform_external_runtime_session_and_configured_refresh; F:test_platform_rejects_cross_origin_runtime_auth_without_using_console_session | ✅ |
| H16 | browser handoff | fresh built-in CLI handoff enters the connected workbench without credentials in URL requests or a sign-in form; expired handoff stays on sign-in; a different browser account is never replaced | F:test_platform_builtin_cli_handoff_opens_fresh_browser_without_login_form; F:test_platform_builtin_handoff_never_replaces_another_browser_account | ✅ |
| H17 | standalone boundary | local GUI never loads hosted authentication/configuration and still executes a local SQLite query even when hosted credentials exist in browser storage | F:test_standalone_keeps_local_queries_independent_of_hosted_auth | ✅ |
| H18 | hosted release notice | an advisory sidebar card checks `/api/hosted/update` on open, focus and at a low frequency. Newer available hosted versions link to release notes. Close remembers that version in the current browser origin. The card leaves queries and drafts alone, and the workbench remains usable if the check is unavailable | F:test_hosted_update_notice_tracks_available_and_dismissed_releases; F:test_hosted_update_check_failure_leaves_workbench_usable; V:test_hosted_update_notice_uses_theme_tokens | ✅ |

Backend resource ACL, scoped credentials, per-user cancellation/admission,
loopback Origin/Host validation, pairing expiry/revoke, SQLite CAS and cloud
outage behavior are verified by `crates/quarry-platform/tests/platform.rs`.
`crates/quarry-local/tests/hosted_schema.rs` requires an explicit disposable
PostgreSQL URL and verifies split schema export/import, no source row transfer,
prior-database preservation and failed import safety. The separate YiminLab
`database-integration.py` harness exercises actual AuthKit/gateway/native binary
processes against disposable fixtures; its model replies are fixtures.

### SQLite integration audit (2026-10-12)

- **Existence:** audited all JSX interaction bindings, localStorage uses and API
  consumers with `rg`. SQLite extends existing table selection/structure
  (64, R4), EXPLAIN (39–40), conn-info (81), pagination (85–86), engine order
  (108), and shared behavior (114–117). No new interaction binding,
  persistence key or endpoint; `/api/conninfo` adds `databasePath`.
  The database-file label and engine suffix reuse existing chrome/tokens.
- **Capability:** header expects accurate environment/production/file context;
  sidebar expects table/view inventory and quoted previews; tabs/editor expect
  draft preservation; toolbar expects read-only execution and an engine-specific
  plan; grid expects pagination, sort, export, record links and restoration. All are covered
  by 114–117. File creation/import, browsing remote files, SQLite attachments,
  schema editing and a GUI connection editor are deliberately deferred below.
  Autocomplete retains its existing bare-identifier limitation; quoted-name
  completion is recorded below rather than claiming full dialect completion.
- **Shared state:** there are no new store writers or UX invariants. Generated
  preview SQL continues through table-click, environment seeding and focus-link
  restoration; readers are the editor, sidebar selection, tab titles and URL.
  EXPLAIN and Load more still use `startReq`/`applyTabResult`/`endReq` and the
  origin tab's result snapshot; grid/status/export/persistence share that
  snapshot. Added SQLite browser tests combine pagination with sort, tab
  switching, both exports and reload; PRAGMA pagination covers the adapter's
  offset path without repeating its first page; distinct-file environment switching with
  production opt-in; previews with draft preservation; stale responses with
  origin-tab routing; record-link copy uses the result SQL after an editor edit
  and re-executes to the expected single record. Existing SQL write sites remain editor/history navigation,
  table/Alt+table, key inspect, saved-query load/response, Format, History recall,
  environment seeding and focus/query links, with close paths stashing drafts.
- **Independent coverage:** `page_sqlite` and `sqlite_gui_server` create disposable
  SQLite files using the standard library and replace the default Postgres
  fixture config. SQLite browser tests require only Playwright/Chromium. Existing
  PostgreSQL browser tests keep their database availability skip.
  `page_sqlite_workspaces` additionally loads configured local workspace directories
  through the normal workspace resolver and serves real query/API responses;
  identical relative file URLs, distinct owner rows, source paths and persisted
  active-workspace exports are checked together in row 116.
- **Parameterized-result persistence:** row 117 checks a predicate-only private
  token against real SQLite rows, then tab switching, reload and close-to-History.
  The storage write sites are `tabsStore.persistTabs` (`qy_tabs`),
  `tabsStore.persistResults` (`qy_tabres`) and `useSqlHistory.persist` (`qy_hist`).
  Saved-query responses feed `updateTab` and `setTabResult`; closing the tab feeds
  `pushHist`. Each sink is asserted without placing the token in selected columns.
  No frontend store writer or persistence format changed.
- **Test isolation:** live-SSE browser tests run the real workspace watcher tick
  in a per-test thread that is stopped and joined at teardown. This prevents a
  previous workspace watcher from overwriting the next test's configuration.
  Workspace-removal coverage waits for the resulting connection-tree refresh
  to unbind the active tab, since the modal list updates independently.
- **Validation:** rebuilt the shipped bundle; frontend unit tests passed
  (61), and lint reported no errors (one existing Sidebar Fast Refresh warning).
  SQLite API/browser tests passed (12) with Postgres deliberately unreachable.
  Restoring the old rendered result SQL through a temporary test plugin makes
  row 117 fail specifically on the parameter value leaking into `qy_tabres`.
  Before that parameter regression was added, the complete six-module GUI suite
  passed (416, one existing skip because `hatchling` is unavailable); wheel
  asset/changelog and sdist checks passed.

### Tab navigation audit (2026-09-10)

- **Existence:** tab add/switch/rename/reorder/close remain rows 34–35,
  67–69 and the existing rename/drag rows. New list search, no-match,
  dismissal, keyboard navigation and bulk close map to 110–111. Connection
  selection, environment pills and table clicks map to 12–14, 70, 105,
  107, 109. No new API endpoint: `qy_tabs` gains optional
  `workspace` and `visited` fields; legacy entries acquire their workspace
  on connection load. `qy_ati=-1` represents no active editor; `qy_tabs=[]`
  remains empty on reload. `qy_empty_tab_groups` remembers deliberately closed
  workspace/db/env groups (written only by add/close and read by group selection),
  so env switching and URL restoration cannot reopen them. Index-aligned
  `qy_tabres` stays compatible.
- **Capability:** sidebar needs selected-object reveal, tabs need readable
  overflow and fast lookup, editor needs draft preservation, result/toolbar
  need active-tab exports. Empty workbench needs New tab, table-click and
  History recovery without automatic execution; covered by 111–112.
  Covered by 28, 67, 70, 109–112. Table metadata hints,
  pinned tabs and explicit transfer of a query to another connection remain
  listed below as deliberate gaps.
- **Shared state / all write sites:** `tabsStore` owns `readInitial`,
  `claimWorkspaces`, `selectGroup`, `addTab`, `switchTab`, `closeTab`,
  `reorderTab`, `renameTab`, `updateTab` (including `updateActiveTab`),
  and `setTabResult`. Readers are TabBar, workbench editor/results, URL/title,
  request guards and persistence. Workbench connection writers are
  `selectDb`, initial/tree reload, focus/query-link restoration,
  `unbindActiveTab`, saved-query response and tab switch/close revalidation.
  `uiStore.collapsedGroups` is also written by tab navigation to reveal the selected table (preview-selection interaction test).
  SQL writers are editor/history navigation, table/Alt+table, key inspect,
  saved-query load/response, Format, History recall and focus/query links.
  Table-selection writers are connection/unbind/link transitions, table
  click, saved/EXPLAIN transitions and the exact-preview derivation effect;
  readers are sidebar highlight/reveal and URL/title. The empty-active-tab sentinel
  is written only by close/group selection/restoration; workbench and TabBar
  no longer fall back to another group's first tab. `setSql`, table click and
  saved-query loading create a tab when needed. Tests 109–112 interact
  with SQL, filters, result export, persistence and History; existing race,
  deep-link, saved-query and draft tests cover the other entry points.

### Release contract regression coverage

`test_release_contracts.py` verifies actual write/readback behavior (Postgres and
MySQL), read-only function/EXPLAIN protection, Redis prod confirmation and error/
null/text responses, outer limit pagination, duplicate names, precision and the
MCP startup argument order. Redis scan-mode coverage includes real CLI output,
cursor pagination and whitespace/newline keys; Cypher coverage distinguishes
property/map/label identifiers from mutation clauses. Set `QUARRY_TEST_MYSQL_URL` and
`QUARRY_TEST_REDIS_URL` for isolated real services; CI supplies MySQL 8.4 and Redis 7.
The release workflow reuses CI against its exact tag before publishing to PyPI.

### Design gaps (capability-audit output — missing on purpose until scheduled)

| Region | Missing capability | Decision |
|--------|--------------------|----------|
| platform | shared SQL library and cloud/cross-device personal history sync | explicitly excluded; hosted records stay in the browser and the native workbench uses local SQLite |
| hosted/offline | first visit with no cached application assets | application must be downloaded once; cached static shell supports later offline visits |
| native cloud targets | connection details cannot reveal cloud credentials or configure the remote database; the local schema-sync button and local SQL assistant require a locally configured connection | cloud connections expose provider and reachability. Hosted SQL assistant availability follows the hosted server configuration |
| same-name local targets | the SQL assistant and the create-local and schema-sync actions are unavailable on exact workspace routes | querying uses the selected workspace's exact target. The assistant and lifecycle actions still require an unambiguous configured database and environment name. |
| SQLite | create/import databases, remote file access, attached databases, schema editing and GUI connection configuration | first release opens configured existing local files; connections remain CLI/TOML managed |
| editor | autocomplete does not quote reserved/special table names or parse quoted column qualifiers | backlog; table-click previews are dialect-quoted, while these autocomplete cases need manual quoting |
| header | long workspace paths squeeze status badges on narrow windows | backlog; outside workbench spacing scope |
| header | per-tunnel permanent failure details (`blocked`) are available in CLI/API but not expanded in the keeper badge | badge reports daemon state; a running keeper does not prove database reachability |
| sidebar | row-count / size hints next to tables | backlog (low) |
| sidebar | complete SQL/Cypher parsing and live Neptune schema navigation | query associations are hints from saved query text; complex/ambiguous references may remain under Other; labels shown here are not a complete live inventory |
| grid/images | generated server-side thumbnails, authenticated custom-header image requests and saved column display preferences across reloads | current preview fetches the original URL directly, follows browser/server cache headers, and keeps column display overrides in-session per tab/schema |
| grid | record links for JOINs, computed/aliased projections, aggregates, nullable or absent unique keys | disabled with an explanation; conservative original-column single-table SELECT support |
| tabs | pin a tab; explicitly copy a query into another connection | backlog; environment switches seed empty editors but preserve existing drafts |
| tabs | browse inactive drafts whose workspace/connection has been removed | backlog; persisted drafts remain stored, active missing connection is unbound |
| toolbar | arbitrary result-cap values beyond 100/500/2000/5000 | deferred; the row-limit selector deliberately retains its existing presets |
| assistant | model settings form, query execution from chat, maximizing the panel and a separate checkpoint manager | intentionally excluded; CLI configuration, existing Run and inline Restore previous SQL are the supported controls |
| assistant | cross-connection joins, Redis/Cypher generation and result-row analysis | first assistant release is scoped to SQL/schema on the current PostgreSQL/MySQL/SQLite connection; query result rows are not sent automatically |
| assistant/history | conversation deletion, export and full-text search of message bodies | deferred; History provides local conversation recovery while retaining the existing query-history search |

Safety/performance-relevant UX invariants that must never regress (rows 13,
27–28, 37, 47, 102):
draft SQL is never silently lost; configured production connections default to Auto-run off and require scoped opt-in; toggling Auto-run never executes SQL; stale
responses never overwrite newer results; sort is numeric-aware and restorable;
large raw values stay copyable/exportable but never materialize in full in the
grid DOM or synchronous result persistence.

The Tabler icon webfont is vendored (self-hosted via `@tabler/icons-webfont`,
bundled by Vite) — no CDN dependency; `test_gui_visual.py` asserts the font
actually loads. This closed the long-open jsdelivr gap (#14).

### Tunnel status contract audit (2026-09-12)

- Existence: `web/src/api.ts` adds `blocked` to the existing keep-alive response
  type (row 101). No new interaction bindings, endpoints, or localStorage keys.
- Capability: the existing header still summarizes keeper process status and
  reconnection activity; detailed per-tunnel failures remain a recorded gap above.
  Proxy badges use verified matching tunnel facts (row 99).
- Shared state: no state write sites change. ResultWorkbench polling and Header
  start/stop responses still call `setKeepAlive`; Header is the status reader. The type-only change
  introduces no result, selection, draft, or export state interaction.

## Adding tests

- Put pure logic in a `unit` test (no fixtures that touch a DB/server).
- Use `ws`/`pg_exec` for in-process DB work, `gui_server` for the GUI HTTP API,
  `qy`/`mcp` for CLI/MCP subprocess e2e, and `page` for browser e2e.
- Never mutate the shared `customers`/`orders` seed tables; create scratch tables
  with a per-file prefix and drop them in the test.

### Production/navigation audit (2026-09-10)

- Existence: existing env/database selection bindings consume `/api/connections`
  `production`; Header badge and env pill use the same flag (rows 12–13).
  No new persistence keys or endpoints. Conn info exposes the metadata for API clients.
- Capability: header classification, sidebar table focus, scoped tabs, editor draft
  retention, manual Run and cached grid/export remain covered by their matrix rows.
  Production configuration is file/CLI managed; an in-GUI settings editor is deferred.
- Shared state: `selectDb` is the sole non-null current-connection writer; unbind,
  vanished/query-link and tab revalidation paths can clear it. Production metadata is
  refreshed with the connection tree. Navigation seeds only newly created blank tabs,
  never existing drafts or deliberately closed groups. Only one navigation intent is
  consumed after the target table list arrives; pending and cached queries are skipped.
  Query results still flow through startReq/applyTabResult/endReq guards.
- Production policy writers: Connection default, TOML loader, CLI add/upsert and set.
  Readers: grouped connections API, conn-info API, GUI selector/header, CLI confirmation,
  MCP write policy. Tests cover jp=true, prod=false, malformed flags and flag updates.

### Tab spacing audit (2026-09-10)

- Existence: existing tab strip scrolling, keyboard navigation and fixed controls
  retain their bindings; no new state, persistence keys or API consumers.
- Capability: tabs use natural widths capped at 240px in a 44px flat strip. Native
  scrollbars are hidden; overflow-only arrow controls scroll without changing selection. The existing overflow
  browser test now asserts that geometry as well as scrolling and menu controls.
- Shared state: CSS-only layout change; active-tab visibility still uses the
  existing resize and selection effects. Overflow navigation and both theme token
  checks cover the affected tab region.

### Flat tab strip audit (2026-09-10)

- Existence: `tabScrollLeft` and `tabScrollRight` scroll the existing strip, with
  translated accessible labels and disabled boundary states. Covered by
  `test_tab_overflow_search_keyboard_and_scoped_bulk_close`.
- Capability: tabs retain mouse/trackpad scrolling, keyboard selection, rename,
  close and searchable overflow menu. Arrow controls appear only when needed.
  `test_tab_menu_uses_surface_tokens_and_fixed_controls` pins the active underline
  in both themes and absence of arrows when tabs fit.
- Shared state: overflow state is measured on resize, scroll and tab changes;
  arrow clicks alter scroll position only, never activeId or SQL/results. Existing
  active-tab reveal still runs on selection and resize. No API or storage changes.

### Workbench emphasis audit (2026-09-10)

- Existence: styling only for active tabs, existing environment pills and Run;
  no interaction, persistence or endpoint changes.
- Capability: active document uses a warm filled selection and stronger title;
  environment selection uses a 20% accent tint, accent border and semibold label; production
  retains red warnings. Run retains accent text/outline, hover fill and keyboard focus.
- Shared state: existing `.on` state and production flag remain authoritative.
  `test_workbench_visual_hierarchy` covers colors and selection in both modes;
  production contrast and tab navigation tests retain the interaction coverage.
- Workspace removal is the other writer that can invalidate the active tab's
  connection. It immediately clears that tab binding plus `connStore.current`
  and `currentTable`; the subsequent connection-tree reload remains the source
  of truth for every other tab. The existing workspace-removal browser test
  covers this write site.

- Environment-selection refinement: existing `.on:not(.prod)` binding uses a
  stronger tinted fill, accent border and 600-weight text, pinned in both modes
  by `test_workbench_visual_hierarchy`. Production styling and all state writes,
  interaction bindings, persistence and API consumers are unchanged.
- Bulk-close draft retention now keeps 1000 history entries so a group with
  more than 100 nonempty tabs does not discard its earliest drafts. The
  browser suite closes 105 populated tabs and verifies both ends of the group
  remain recoverable in `qy_hist`.

### Workbench spacing and environment execution audit (2026-09-10)

- Existence: existing env pill binding and navigation effect map to rows 12–13;
  no new API or storage key. Compact context, 36px tabs and 30px toolbar controls
  are covered by `test_gui_visual:test_workbench_compact_spacing` (both themes),
  alongside row 110 overflow and F:test_workbench_visual_hierarchy color coverage.
- Capability: shared 14px inset and smaller secondary controls keep SQL readable;
  toolbar and environment controls wrap on narrow windows. Environment switching
  now carries handwritten SQL to empty editors and restores previously executed queries without re-running.
  Existing destination drafts win; deliberately closed groups stay closed.
- Shared state / write sites: `selectDb` alone seeds destination SQL for env clicks;
  `selectGroup`, `addTab`, `updateActiveTab` retain separate group identities.
  The navigation intent is consumed once and requires production=false; query writes
  still use startReq/applyTabResult/endReq. SQL editing, history recall, saved query,
  table/key entry, focus restoration, tab switch/close and workspace revalidation
  keep their existing paths. Browser tests cover source/destination draft retention,
  prod=false, jp=true, cached restoration, closed groups and in-flight response isolation.
  Cached results always suppress navigation auto-run, even for edited SQL; explicit Run updates the result. Export reads the restored snapshot. The SQL/result/navigation write sites listed above remain unchanged; browser coverage verifies round trips, edited draft plus old result, export and explicit refresh.


### Icon hover audit (2026-09-11)

- Existence: inspected every icon and interaction in Header, Sidebar, TabBar,
  SqlEditor, ResultWorkbench, Modals, WorkspaceModal, ConnInfoModal, UpdatePanel,
  WhatsNewPanel and App. Removed header badge row 2; hints extend rows 8, 24,
  38–44, 66 and the existing tab/connection/workspace/JSON-tree controls.
  Decorative icons beside labels are not independent actions. No new API,
  storage key or interaction action was added.
- Capability: all icon actions have native title hints; the Voyage language,
  mode and palette controls now mirror their accessible names. Sidebar and
  JSON-tree disclosure hints describe expand/collapse. Existing Design gaps
  remain unchanged. Browser tests verify native title text after hover, not
  OS-rendered tooltip pixels (native tooltips are outside the DOM).
- Shared-state: no connection, query, result, history or preference write sites
  changed. A header-scoped observer mirrors Voyage aria-label changes to title;
  its only DOM write is title, and it disconnects on unmount. Browser coverage
  checks mode, locale and credential-reveal hint changes. No CSS/color changes.

### Table query discovery audit

- History follow-up (21a, history navigation): Cmd/Ctrl+Up/Down is handled before
  the read-only edit guard. Existence: existing keyboard actions still call
  navigateHistory; no new API or storage key. Capability: both recalled SQL and
  a stashed handwritten draft remain recoverable from a fixed query tab.
  Shared-state: onChange goes through ResultWorkbench.setSql, which creates an
  editable tab before writing SQL; the saved template remains immutable. Browser
  tests cover both directions, original template retention and no execution.

- Loading follow-up (71): fetchTables has a 30-second AbortSignal deadline. Failed
  fresh requests restore the cached panel before showing an error; background
  refreshes now check both connection key and request sequence. Existence: same
  endpoint, new timeout/error paths tested by test_table_list_timeout_ends_spinner
  and test_failed_refresh_keeps_cached_tables_and_stops_loading. Capability: errors
  end the spinner and cached tables remain usable. Shared-state: panel, tcache and
  health readers audited; late responses cannot repaint a newer same-db request.
  Redis metadata now uses one bounded internal TYPE/TTL script; arbitrary EVAL
  remains blocked. Redis engine tests cover result mapping, ACL fallback and key
  escaping; browser Redis type/TTL and capped-list tests cover the GUI contract.

- Inline-refresh follow-up (71): the same refresh binding moved from TablePanel
  into the active database row. Click propagation is stopped before connection
  selection; hover/focus swaps engine label for the icon within the existing row
  width, with an always-visible control on devices without hover. Existence: no
  new endpoint/key; onRefresh still refreshes the current db/env table/key cache.
  Capability: keyboard focus exposes the button, and no extra row or width shift
  is introduced. Shared-state: existing loadTables updates panel/tcache/health;
  regression verifies refresh preserves editor SQL, active connection and panel
  visibility, while theme tests pin hover/focus layout and token styling.

- Global-search follow-up (rows 15, 18, 20, 21a, 109): top sticky input and collapse
  button replace the in-database input. Search is component-local and reads all
  connection groups, saved query metadata and tcache for each known environment;
  it does not fetch remote schema or execute SQL. Scope is visible in results.
  Existence: input/Escape, result database/object/query clicks and Collapse all map
  to row 15. No new endpoint or persistence key. Capability: results keep workspace,
  database and environment context; unloaded objects are explicitly outside scope.
  Shared-state: normal tree remains mounted while search results are shown, preserving
  local expansion state. Typing only writes search; clear/Escape restores the tree.
  Collapse clears search and uses existing collapseToken/collapsedGroups/panelOpen
  writes. Explicit object clicks select the correct connection/environment before
  existing preview/key handlers run; saved queries use their immutable identity.
  Browser coverage checks no requests on typing, cached results across connections,
  query descriptions, no-match state, expansion restoration, and draft-safe navigation.

- Selection follow-up: removed duplicate Other queries UI and its expansion writes.
  Table reveal waits for successful list loading and recognizes unqualified public
  table names. Delayed schema browser coverage proves the selected saved query
  expands under its actual table. Existence: Collapse all moved to ResultWorkbench;
  its counter feeds Sidebar/TablePanel, and existing collapsedGroups writes remain
  unchanged. Capability: unassociated queries stay accessible via All queries;
  read-only textarea has a prohibited cursor and a localized copy-to-edit hint.
  Shared-state: activeId/savedQueryId readers drive selection after asynchronous
  panel updates; collapse changes visibility only. Editor readOnly and SQL guards
  remain unchanged and are covered together with cursor/title in row 21a.

- Compact-layout follow-up (row 21a): Collapse all is in the workbench toolbar, outside workspace headings. All queries database
  labels and query items use successive indentation. Stable scrollbar gutters
  prevent overflow from changing sidebar row width. Covered by
  `V:test_sidebar_compact_collapse_indent_and_stable_gutter`; existing collapse
  interaction coverage still proves editor/result preservation. Existence: no
  new binding, key or endpoint. Capability: header space and tree hierarchy fixed.
  Shared-state: only control placement and CSS changed; all collapse writes/readers
  remain as documented below.

- Existence: full table rows and keyboard Enter/Space expand children and open
  previews (21a); All queries retains its full-row toggle (21). Collapse all uses
  existing workspace collapse persistence plus local table expansion state. Copy
  as new query adds an editable tab. No new API or localStorage key; `qy_tabs`
  persists `savedQueryId` alongside SQL. Two-line titles and selected full
  descriptions are covered by `F:test_query_titles_expand_description_without_counts`.
- Capability: query counts and inner disclosure arrows are removed. Query titles
  remain readable, joined queries may appear under multiple tables, and unmatched queries remain in All queries.
  Neptune labels are inferred from saved queries, not a complete live schema.
  SQL association is a navigation heuristic, not a full SQL parser; comma joins,
  dynamic SQL and ambiguous identifiers remain a design gap. Header, toolbar,
  result grid and export capabilities are unchanged.
- Shared-state: `openSavedTab` creates immutable template SQL and reuses file +
  connection + environment identity. `updateTab` guards SQL for fixed tabs and is
  also the sink for `updateActiveTab`: editor input, formatting, table/key insertion,
  history recall, focus restore and saved-run response cannot overwrite templates.
  `setSql` routes intentional replacement of a fixed template to a new draft;
  Copy as new query uses addTab/updateActiveTab. Persisted tabs restore identity.
  Readers include editor readOnly/Format, Run parameter lookup, preview matching,
  sidebar selection and description expansion. Selecting a tab reveals its query;
  selecting the query reuses its tab without execution. Collapse all changes only
  visibility. Result writes still use per-tab request guards, and production never
  auto-runs previews. Browser interaction tests cover draft retention, immutable
  reuse, copy, selection and collapse together; store tests cover both update entry
  points and file/environment isolation. Parameter modal behavior is separately pinned.
  `F:test_fixed_query_uses_explicit_environment_without_production_autorun`
  checks that a saved file pinned to a concrete connection key reuses that
  environment and does not auto-run when it is marked production.

### Workspace query sidebar audit

- Existence: workspace query sections reuse group toggles and `/api/queries`; file
  identities flow through saved selection, parameter modal and `/api/run` (row 21–22).
- Capability: workspace ownership, query-only workspaces, database grouping,
  independent collapse, and same-name selection are covered; no new design gap.
- Shared-state: `collapsedGroups` is initialized from storage in `uiStore` and written by
  `toggleCollapsedGroup`, called by sidebar headers and `handleTabSwitch`. Query
  sections use distinct keys, so tab switching expands only the connection group. Connection, editor, draft, request and result
  write sites are unchanged; saved selection/modal now carry the file identity.
  Collapse/reload and selected-file execution are covered together by browser tests.

### Integrated production context and row-limit audit (2026-10-03)

The tab-bar production label and execution preference in the 2026-10-04 audit below
supersede the header tint, heading label and unconditional production auto-run
guard described here.

- Existence: row 3 covers the shell treatment and the permanently reserved
  104px `.production-status` region at the right of the connection heading;
  row 107 covers the same context for Neptune. Its conditional `#prodBadge`
  shows localized Production text and retains the `production_tip` native
  hover hint, while the slot remains present in non-production. The left
  `.connection-context` contains the title, environment pills and connection
  information control. Run retains its existing interaction and loading state
  (row 36), with no extra guidance label or accessible-description link.
  Row 11 covers the environment pills' constant font weight and production text
  contrast. These are presentation elements with no new interaction binding,
  API consumer or persistence key. The sole `prod_no_autorun` toast in
  `ResultWorkbench.selectDb` is removed; other toast producers are unchanged.
  Existing environment, Neptune, result-restoration and in-flight browser tests
  assert persistent context instead of the removed toast. Row 65 covers the
  existing `#maxRows` select's `onChange`, localized option labels and decorative
  Tabler chevron; the chevron is hidden from assistive technology and ignores
  pointer events. The select keeps native keyboard behavior, numeric option
  values, `qy_maxrows` storage and existing API request fields.
- Capability: the header supplies a muted peripheral cue using the current
  theme's accent blended with secondary text; no error-red token seeds production
  styling. The sidebar and active tab identify the selected target. The heading
  displays the production label, including with no query tabs open; its native
  hover hint explains the execution policy. Permanently reserved space and constant pill weights keep connection
  switching from shifting controls. The toolbar offers readable row-count units,
  an inset chevron and keyboard selection. The editor, grid and exports retain
  their existing behavior. Visible localized text complements color; dark/light
  token and geometry tests cover the changed chrome. Arbitrary row limits remain
  unavailable and are recorded in Design gaps; existing header-width and
  connection-configuration limitations remain unchanged.
- Shared-state: `.app-shell[data-production=true]` is derived directly from
  `connStore.current?.production === true`; the wrapper uses `display: contents`
  to retain the app layout. Production is neither inferred from an environment
  name nor copied into a second state/persistence field. The complete existing
  current-connection write inventory is the store's initial `null` and
  `setCurrent` setter; `selectDb` supplies non-null metadata; `unbindActiveTab`,
  the missing-connection query-link path, `revalidateTab`'s unbound-tab path and
  `WorkspaceModal.doRemove` clear it. Connection-tree reload revalidates through
  `selectDb`. No write site changes. Readers include the new shell, workbench
  connection heading/toolbar, sidebar selection, navigation/preview and saved
  query guards, editor context, query/EXPLAIN targets, URL/title and result/export
  connection checks. The global Header no longer reads the production flag.
  `test_production_context_follows_connection_without_toasts` checks the new
  state-to-chrome interaction; existing explicit-production tests retain
  `jp=true`/`prod=false`, draft/result preservation and execution coverage.
  `test_production_context_tokens` pins the coordinated chrome in both modes.
  `test_single_production_connection_context_is_localized` checks single-env
  connections, English/Chinese labels and clearing the context after removal.
  `test_production_context_has_stable_layout` checks title, environment and toolbar
  geometry when production context appears/disappears, plus context without tabs.
  The complete max-rows write inventory remains `uiStore` initialization through
  `readMaxRows()` and `setMaxRows()`, whose sole UI caller is the selector's
  `onChange`; the setter also writes `qy_maxrows`. Readers are the controlled
  select and the request caps for regular SQL, query-link execution, saved query
  execution, EXPLAIN and Load more in `ResultWorkbench`; `api.runSaved` passes the
  cap to `/api/run`, while `api.runQuery` passes it to `/api/query`. All paths keep
  the existing numeric value and preset options. The existing cap/reload browser
  test checks persistence and actual result limiting; the new keyboard/label and
  geometry tests cover the presentation change. No max-rows writer or consumer
  changed semantics.
  Query execution, request guards and per-tab result writers are unchanged.


### SQL-editor production context and execution-mode audit (2026-10-04)

- Existence: row 3 covers the muted `.edwrap` surface and localized
  `#prodBadge.production-tag` in the fixed 104px `.tab-context` slot. `TabBar`
  and `SqlEditor.isProduction` read the current connection flag; the textarea
  references the badge through `aria-describedby`. The tab bar keeps the label
  visible with no editor, and the header stays neutral. The active tab retains
  one short underline. Row 113 covers `RunControls`: `#runBtn` executes once;
  `#runModeBtn` opens/closes the menu, exposes its expanded state and always
  displays Manual/Auto; `#runModeManual` and `#runModeAuto` are checked radio menu
  items with descriptions and a scoped connection footer. Arrow/Home/End move
  focus, Enter/Space select, Escape returns focus, Tab exits and outside click
  dismisses. Connection/tab identity changes remount the control and close it.
  `qy_auto_run` stores a boolean map keyed by workspace, database, environment
  and production flag. No endpoint is added: table/navigation/deep-link execution
  uses `/api/query`, saved-query execution uses `/api/run`, and metadata still
  uses `/api/tables`. A changed production classification or replacement workspace
  has its own default.
- Capability: the header identifies the connection; sidebar and tabs identify
  the selected object/draft; the editor uses a restrained production tint, with
  its label alongside the tabs. The toolbar exposes explicit Run and a readable
  mode in one group, whose width stays fixed during mode changes. Grid and export
  retain the active tab's cached result. Production defaults to Manual and can be
  explicitly changed to Auto; non-production defaults to Auto; missing metadata
  disables the mode control and automatic execution. Selecting either mode never
  executes current SQL or replays pending navigation; typing never auto-runs.
  Table-linked paramless saved queries retain preview-only behavior. Explicit Run,
  keyboard Run, parameter submission, EXPLAIN, Load more and Redis key inspection
  remain deliberate actions. No new capability gaps; existing gaps remain deferred.
- Shared-state and complete write inventory: preference initialization is solely
  `readAutoRunPreferences`; the sole mutation/persistence writer remains
  `uiStore.setAutoRun`, called by `RunControls.onModeChange` in `ResultWorkbench`.
  Readers are the controlled mode label/menu and `canAutoRun` at query-link
  restoration, deferred environment/navigation execution, both reused/new
  table-preview paths and newly opened saved queries. Each reads the live
  destination connection after `selectDb`, including workspace identity; a source
  environment's preference cannot authorize another. `previewNavigationRef`
  writers are initialization, `selectDb` clear/stage, navigation-effect
  consumption, and mode/editor-change clears. Clearing before writing preferences
  prevents delayed metadata from executing a previously selected or edited query.
  Menu `open` is local to `RunControls`: trigger/arrow-open write true; trigger,
  outside pointer, focus exit, Escape/Tab, selection and explicit Run close it;
  context-key remount resets it. Run-control key events do not propagate to grid
  shortcuts, so a selected cell cannot intercept Enter on Run or the mode trigger.
  No result/request-guard writer changes: pending requests retain origin-tab and
  latest-wins routing, and cached results suppress navigation execution.
  Interaction tests cover all automatic entry points, explicit execution,
  persistence/scope isolation, delayed navigation, mode selection without SQL,
  keyboard/outside dismissal, selected-cell shortcut isolation and connection/tab
  context changes.
  `test_production_context_tokens` and `test_run_mode_menu_tokens_and_geometry`
  cover Slate/Signal, dark/light and EN/ZH, including label contrast, the single
  selected-tab underline, the split button/menu and stable mode geometry.
  Production browser tests cover unobstructed editing/execution, scrolling,
  resizing, context changes and closing the final tab. This refinement adds no
  store, persistence or query-result write sites.

### GitHub identity and release-link audit (2026-10-06)

- Existence: row 97 covers the update badge, panel, upgrade command and release
  link. The existing panel browser test now asserts the canonical repository
  URL, requested version and new-tab target. No endpoint or storage key is added.
- Capability: users can inspect the current/latest versions, copy the upgrade
  command and open release notes for that exact version. Clicking the backdrop
  or pressing Escape still closes the panel; no capability gap is introduced.
- Shared-state: `useEvents.refreshUpdateInfo` still writes through
  `uiStore.setUpdateInfo`; `Header` reads that state and passes it to the panel.
  The panel derives its URL from `info.latest` without writing shared state.
  Badge/panel visibility, query drafts, results and persistence are unchanged.

### Record-link audits (2026-10-10)

- Existence: row 52 maps `#recordLinkBtn`, its clipboard action, loading/error
  status, and POST `/api/record-link`. No localStorage key is added. Existing
  query-link navigation and Auto-run remain in rows 113 and the link rows.
- Capability: row details retain full field inspection and offer a uniquely
  keyed single-record query link; unsupported query shapes are listed in Design
  gaps. Primary keys precede unique keys; composite keys include every column.
  Null/missing/unsafe numeric keys, MySQL binary/bit keys and unavailable metadata
  disable copying. Query-tail checks ignore quoted string contents; MySQL key
  projection and row names are matched without case sensitivity. Typed binary
  metadata permits a text unique-key fallback without interpreting display markers.
- Shared-state: no store writes added. The row-opening write to local `modal`
  snapshots `queryDb`, `queryEnv`, `result.sql`, columns and the clicked row from
  the active result, rather than editor SQL/current table. Async metadata writes
  only local RowDetailModal state; its effect cleanup ignores closed/replaced
  requests. Existing clipboard success/failure feedback is reused. The browser
  test changes the editor after execution, runs another tab, restores the original
  tab and verifies the link still identifies
  the displayed row, preserves the draft, makes no query request, and round-trips
  to one record. New chrome reuses button tokens and `--fg2`; no colors added.

### Merged keeper-control audits (2026-10-10)

- Existence: row 101 owns the single `#kaBadge` click binding, native keyboard
  activation, action hover hint and `aria-pressed`; the former `#kaBtn` is removed.
  No API or storage keys change. Row 66 hover-hint coverage uses the merged badge.
- Capability: the header retains status visibility, start/stop and reconnection
  updates in one control. Detailed per-tunnel failures stay in the Design gaps.
  Sidebar, editor, tabs, toolbar and grid capabilities are unaffected.
- Shared-state: all `keepAlive` write sites remain `connStore.setKeepAlive`,
  Header's start/stop response and ResultWorkbench's background polling. Header
  remains the only reader. Tests cover click and keyboard state transitions plus
  polling-driven reconnection labels on the same button. No query or draft state
  writes change. Both themes pin badge colors and keyboard focus to existing tokens.

### Tab-control alignment audits (2026-10-10)

- Existence: row 110 covers existing add/list/scroll bindings and their shared
  geometry. The text plus becomes the existing icon font's plus glyph; no new
  binding, API or storage key is introduced.
- Capability: tab creation and list discovery retain the same accessible labels,
  keyboard navigation and menu actions; the two fixed controls now share height,
  icon size and vertical center. Existing Design gaps remain unchanged.
- Shared-state: markup/CSS only; no state writers or readers change. The existing
  overflow/browser interaction test covers fixed controls and list navigation;
  the dark/light geometry pin checks both icons and the count against their controls.


### GUI route audit (2026-10-18)

- Existence: `/quarry/` is the landing page. Active connection/tab changes
  update `db/env/table` in the query string; query and record links use this
  same tool path. Rows 94/106 cover links; R1 covers serving. No API endpoint,
  localStorage key, interaction binding or visible color is added. `/app/`
  and workspace-name paths return 404.
- Capability: header workspace management, sidebar, tabs/drafts, editor,
  toolbar links and grid record links keep their existing behavior. URLs use
  the configured logical database and environment, independent of workspace
  folder names. Focus links restore without executing. Existing Design gaps
  remain unchanged.
- Shared-state: URL/title writes stay in the single `replaceState` effect;
  focus/query restoration retains its existing tab/connection write sites.
  Readers are sidebar selection, tabs, editor, results and copy actions.
  Browser tests cover cross-workspace connection switching, copied query links,
  reopening links, reload and focus restoration without execution. No shared
  store, result, draft or production-preference write site changes.

### SQL assistant audits (2026-10-18)

- **Existence:** `EditorWorkspace` owns open/hide, pointer resizing in both
  directions and keyboard resizing of the split (118). Both directions reuse
  `vg-hresizer` chrome, with vertical geometry, a left-side border and the inherited bottom border joining the toolbar (118). `SqlAssistant` owns
  composer input, Tab acceptance, Enter/Shift+Enter/IME handling (119–120),
  read-only reply-preference refresh (127),
  scroll-follow state (121), Apply SQL and Restore previous SQL (122–123).
  `HistoryModal` adds Queries/Conversations selection, conversation filtering
  and recovery (124). Model configuration remains CLI-only. `/api/assistant/config`,
  `/conversations` (list/get/create), `/conversations/update`,
  `/conversations/apply`, `/conversations/restore`, `/chat` and `/cancel` map to
  119–124. Chat uses its own NDJSON stream rather than the notification SSE
  channel. GET `/api/assistant/preferences` (127) reads the CLI-owned
  `[gui.assistant] reply_language` setting and resolved system language. There
  is no preferences POST or GUI language-setting interaction. The legacy `qy_assistant_language`
  key is no longer read or written. `qy_tabs` gains `conversationId`
  and `sqlRevision`; `qy_edh` still stores editor height, and `qy_lang` controls
  interface language. Messages and checkpoints live in the local backend
  conversation store. Markdown links and local overflow scrolling map to 126;
  rendering adds no API, storage key, SQL writer or execution binding.
- **Capability / current interface:** chat opens on the left of the SQL editor.
  Its 32px collapsed rail displays the complete SQL 助手/SQL Assistant label
  vertically, without a separate icon. The expanded close control is a
  22×26px handle centered on the 6px divider, 8px from its top. Messages start
  at the panel top with normal 14px inner spacing. The separator and close
  button are siblings, so clicking or keyboard-activating close does not start
  a resize. The scrollport reaches the divider with no right-side margin. In
  Chromium/WebKit its scrollbar is 4px wide and the track starts 38px below
  the panel top, clearing the close handle locally. Standard thin scrollbars
  remain the fallback. Text keeps its 16px right padding (12px on small screens).
  The vertical divider reuses the horizontal resize bar's colors and hover
  feedback; both are 6px thick, with the 30×2px grip transposed vertically.
  Its 1px boundary is on the left, with no right boundary; its inherited 1px
  bottom border matches and joins the toolbar border without a gap. The grip's offset
  compensates for the left border so its center stays in place. Normal and
  hover backgrounds, border color, grip color and opacity match. Keyboard focus adds an accent
  outline and uses the hover colors. Dragging right or pressing ArrowRight grows
  chat. The query toolbar is rendered
  once below the right editor, with the chat composer aligned to its lower
  edge. Collapsing restores a full-width toolbar, an 88px editor and more
  result space. Expanded dragging keeps at least 200px for a composer resized
  to 140px, and can use the available height above status (or the section
  bottom without results). Resizing the window, wrapping the toolbar and
  returning from a smaller window preserve the manual height preference.
  Opening loads reply preferences and existing local conversation history;
  it makes no model-configuration, conversation-create or model request. With
  SQL and an empty, idle composer, faint text shows the complete localized explanation prompt
  aligned to the textarea's font and padding. There is no GUI reply-language
  setting, including on hover or keyboard focus. `qy ai language zh|en|system`
  controls all GUI replies; its default follows the language of the system
  running Quarry (Chinese resolves to zh, other languages to en). Interface
  language and obsolete browser-local preferences do not override it. Existing
  drafts, SQL and history remain unchanged. Tab accepts the exact neutral
  explanation suggestion and Enter sends `explain=true`; Shift+Tab, existing
  drafts and IME retain normal behavior. Editing the accepted text clears
  explanation-only intent. The suggestion uses the backend-resolved reply
  language and contains no explicit language instruction. This preference does
  not affect CLI replies or model settings; legacy auto is a system alias.
  The assistant can explain or propose SQL using the selected SQL connection's
  metadata. The existing Run action remains the sole query execution path.
  Missing model configuration preserves input and points to the CLI. History
  remains reachable with zero tabs and reopens SQL, messages and checkpoints
  without executing. Markdown assistant replies support formatted explanations
  and bounded wide code/tables; user/system messages remain literal. HTML is
  skipped, images are not fetched, and only HTTP(S)/mailto links navigate with
  `noopener noreferrer`. Headings and SQL token colors use theme variables.
  GUI configuration, chat-triggered execution, maximize, cross-connection joins,
  non-SQL engines, result-row analysis and conversation deletion/export remain
  explicit Design gaps.
- **Shared state / complete write inventory:** existing SQL writers are
  initial/legacy restoration; editor input and history navigation; table and
  Alt+table previews; key inspection; saved-query open/response and Copy as new
  query; Format; History recall; environment seeding; focus/query links.
  New writers are `useSqlAssistant.commitSql` (completed auto-apply, explicit
  Apply SQL and Restore) and conversation recovery into a new tab. Normal tab
  writes pass through `tabsStore.updateTab`, which increments a monotonic SQL
  revision; saved templates remain immutable and assistant changes create a
  draft. `bindConversation` is the only live conversation-ID writer, with
  hydration/persistence carrying that ID. `commitSql` compares origin tab,
  workspace, database, environment, SQL and revision and calls `keepDraft`
  before writing. Readers include the editor, sidebar/table selection, tab
  titles, URL/title synchronization, history, request baselines and persistence.
  Query request tracking and each tab's result snapshot remain separate:
  assistant streams never feed result grids, exports or Run. Tests combine
  asynchronous replies with manual edits, tab/environment switches and close;
  checkpoint restoration with SQL history; saved templates with editable
  drafts; and conversation persistence with close/reload/reopen. Assistant
  writes clear pending origin-tab navigation so delayed table metadata cannot
  auto-run an edit or Restore. Coverage holds metadata until after each write
  and observes no query call. The apply-response race delays persistence,
  introduces a manual edit, explicitly applies the retained proposal and
  verifies Restore returns that manual SQL. Apply marks its proposal pending
  before the request; a lost successful HTTP response and a later autosave 409
  cannot hide Apply or mislabel the editor as restored. Retrying unchanged SQL
  reuses its checkpoint; applying after a manual edit preserves that draft in
  the new checkpoint. The browser test commits through the real HTTP handler
  before dropping the first response and verifies both retry paths without Run.
  Closing while conversation creation
  is in flight still flushes the latest SQL. Restore reuses the original
  assistant checkpoint, shows a non-clickable restored state while its target
  SQL is current, and cannot create an undo chain through rapid clicks or
  legacy system records. Backend restore audit messages remain persisted;
  visible messages and actionable checkpoint identities do not grow.
  `setDraft(tabId)` owns in-memory composer text and explanation intent; both
  survive hide/reopen and tab switches without backend draft persistence.
  `assistantPreferencesStore.refresh` writes only the latest server GET result
  on mount/reopen, window focus and document visibility. It is the sole frontend
  preference-state writer; no browser key or backend configuration is written.
  `qy ai language` uses `set_reply_language` as the CLI-owned persisted writer.
  Consumers are the neutral ghost language and GUI provider reply rule; CLI
  requests do not consume that GUI rule. None calls `setDraft` or changes SQL,
  history or results. Browser coverage combines CLI updates with accepted
  drafts, per-tab state, interface changes and reloads, and verifies that a
  stale browser-local language value cannot override resolved server state.
  Late history reads/errors can publish only for the original binding before
  a newer conversation arrives; an existing send/configuration error and
  unsent text survive a later history failure. History recovery into an already
  live session only opens the panel: it cannot reset generation state, replace
  a newer reply/checkpoint, or unlock another send while the reply is running.
  The regression holds a History GET across both active and completed generation.
  Height writers are store
  hydration, `EditorWorkspace` open/hide restoration and vertical drag, plus
  the unused non-embedded `SqlEditor` fallback. Window reclamping changes the
  visible height without replacing `editorHeight`/`qy_edh`. Resizing writes no
  SQL, conversation, result or execution state. Markdown and syntax highlighting
  only consume conversation/stream text and never call Apply, Restore or Run.
  Moving close into the divider slot adds no state, storage keys, API consumers
  or state writers. The existing close and resize handlers remain separate;
  a pointer move within the close handle must not change the retained width.
- **Test boundary and completed validation:** browser tests stub the assistant
  HTTP/model boundary and use the real isolated local conversation store.
  A controlled `ReadableStream` covers partial Markdown, completion and scroll
  behavior. Provider, metadata and persistence policies are tested at the
  Python API/service layer. Independent Live Verify runs cover the real
  backend/model route, including default Chinese and requested English;
  browser tests establish request language and UI behavior, not model fluency.
  Earlier broad browser validation covered 291 cases across feature/visual
  tests and the remaining browser modules, including packaging. The legacy
  105-tab case needed an explicit hydration readiness wait and passed its
  combined rerun. Subsequent assistant changes passed all 38 assistant cases
  and all 54 then-collected visual cases, plus the neighboring fresh-profile
  language-persistence check. These runs cover manual suggestion acceptance,
  draft/IME guards, delayed history outcomes, safe Markdown, restore races,
  history recovery, editor sizing and both theme palettes. A former 13px
  composer-clipping case is pinned by the expanded 200px minimum test. Geometry
  checks wait for the target viewport/resize observation and read rectangles
  together, retaining the 2px alignment limit. After the final CSS-only
  restoration of the vertical entry, six focused cases
  passed: two layout widths, result-header resizing,
  divider resizing, and Chinese/English chrome checks covering both themes.
  Chrome checks now own close-control colors and focus behavior previously
  duplicated in the Markdown test; both full localized labels fit the 88px
  collapsed height. Eight final locale/theme/state screenshots
  were checked. Unchanged Markdown, language behavior and request logic were
  not rerun for that CSS revision. Python compilation and `git diff --check`
  passed. Divider checks also cover mouse/keyboard width preservation; no
  browser check calls a configured model.
  The CLI-only reply-language revision passed 11 focused browser/visual cases
  using an isolated real preferences GET endpoint and stubbed model responses.
  Both interface languages verify that hover/focus reveals no language setting,
  changes no geometry and ignores the obsolete browser preference. CLI and
  simulated system-language updates are read on reopen/focus/visibility/reload,
  independently of interface language, while accepted drafts, per-tab text,
  SQL and history remain intact. Both locale chrome cases cover dark/light
  tokens; related neutral-ghost, localized-history and minimum-panel geometry
  checks passed. A completion wait excludes the in-flight stream placeholder
  before counting saved replies. Python compilation and `git diff --check`
  passed; no browser test called a real model.
  Moving the close control to the divider passed seven focused cases on the
  final bundle: two layout widths, pointer/keyboard divider interaction,
  result-header/composer resizing, streaming scroll behavior and both locale
  chrome checks (each covers dark and light). Messages start at the panel top;
  the handle is centered on the divider and clears both chat and SQL text.
  The scrolling region reaches the divider; its narrow track starts below the
  handle, and a hit test confirms that track area is not intercepted. Moving the pointer within the close
  handle does not change the saved width; divider drag/arrows and keyboard
  close still work. Python compilation and `git diff --check` passed.
  Removing the full-height scrollbar gap passed six focused cases: two layout
  widths, streaming scroll behavior, independent close/divider interaction and
  both locale chrome checks across dark/light themes. The scrollport meets the
  divider without a margin; Chromium's scrollbar measures 4px, its thumb has
  zero left/right border and border-box paint so the visible thumb is not eaten
  by inherited transparent borders. The exact loaded `:vertical` track rule
  pins the 38px top clearance (generic pseudo computed style omits that state);
  scroll geometry and hit testing confirm the track area clears the handle.
  Actual browser rendering independently confirmed a visible narrow thumb and
  scrolling with zero right gap. This CSS change adds no handlers or state
  writers. Python compilation and `git diff --check` passed.
  The shared divider geometry passed six focused layout, interaction, scrolling
  and locale/theme cases. The final border-position revision keeps the original
  normal/hover colors, moves the 1px vertical boundary to the left and offsets
  the grip to preserve its center. Shared state writers remain unchanged.
  Both locale chrome cases (each covers dark/light) and the divider/close
  interaction case passed. Tests pin left border 1px/right border 0px, the
  preserved grip center, matching normal/hover palettes and unchanged keyboard
  focus, resizing and retained width. The three cases also passed after restoring
  the bottom border: its 1px width, solid style, color and bottom/right coordinates
  join the toolbar border without a gap. This CSS-only adjustment adds no state writers.
  Python compilation and `git diff --check`
  passed.


### MCP query management and diagnostics

`tests/test_mcp_management.py` covers detail lookup, save-without-execution,
explicit overwrite, parameter round-tripping, metadata/path injection rejection,
symlink containment, multi-workspace targeting, duplicate-name ambiguity,
SQLite probe success/failure, bounded timeout arguments and diagnostic redaction.
`tests/test_mcp.py::test_saved_query_lifecycle_and_ping_over_stdio` drives a real
MCP subprocess through save → describe → run → ping → overwrite against an
isolated SQLite database. CLI save/probe regression coverage remains in
`test_cli_integration.py` and `test_cov_cli.py`; probes and file persistence share
core helpers across the two interfaces. Engine-specific probe behavior is covered
for PostgreSQL, MySQL, Redis and Neptune using deterministic engine stubs; this
does not establish remote SSH/auth recovery or live Neptune availability.


### Health-check recovery audits (2026-11-03)

- Existence: matrix row 4 owns Header's health click, disabled/aria-busy state,
  synchronous `checking` reentry guard and `finally` reset. `fetchHealth` retains
  the same `/api/health` query parameters and adds a ten-second abort signal for
  both receiving headers and consuming JSON. No endpoint or storage key is added.
- Capability: a hung request finishes as a visible failed check, leaves a useful
  error tooltip and allows another batch. The existing three-worker limit,
  selected default environment and connection scope remain unchanged. The new
  browser regression simulates a stalled health response against the isolated
  SQLite fixture, checks duplicate-click suppression, timeout recovery and retry.
- Shared state: Header is still the only writer of `checking`; Header and Sidebar
  read it for the button and pending dots. Health writers remain Header,
  ResultWorkbench (table loading and cached health) and ConnInfoModal; Sidebar
  reads health for dots, dimming and tooltips. The test covers the Header-to-Sidebar
  failure/retry interaction. Database-only health keys and all existing readers
  and writers are preserved; no tab, editor, result or persistence state changes.


### Native PostgreSQL/MySQL health transport

Health checks authenticate using the same TLS identity, CA and explicit SSH
forwarding construction as normal queries, then issue PostgreSQL Sync or MySQL
COM_PING. They send no query SQL or transaction/session setup. MySQL's driver-only
session normalization is disabled only for this protocol-only connection; normal
queries retain their existing initialization, read-only transaction and cancellation
behavior. HTTP's six-second and browser's ten-second deadlines remain unchanged.

`postgres_ping_uses_sync_over_verified_forwarded_tls_and_rejects_wrong_hostname`
and `mysql_ping_uses_com_ping_over_verified_forwarded_tls_and_rejects_wrong_hostname`
inspect the TLS wire and reject wrong certificate names before ping. The fixtures
fail on Query/Parse/COM_QUERY, catching a return to the former multi-round-trip path.
`protocol_ping_deadline_includes_stalled_authentication` verifies the total probe
budget covers a TCP peer that never finishes authentication and rejects zero budgets.


Native update audit for row 97:

- **Existence.** `useEvents` reads `/api/update` and updates `uiStore.updateInfo`
  on load and `update_available`. The header badge, upgrade guide, release
  notes, and modal dismissal use the existing bindings. The panel adds a
  conditional command based on `managed`.
- **Capability.** The panel shows a runnable command for managed native
  installs and the migration guide for other standalone installs. Embedded
  workbenches defer to the host's updater.
- **Shared-state.** The header reads `updateInfo.available` and passes the same
  response to the panel. The managed and unmanaged cases exercise that flow
  without adding a writer or storage key.


### 2026-11-04 hosted/local integration audits

- **Existence:** `PlatformStatus` owns explicit pair, revoke, reconnect, Local
  create/stop/schema sync, conflict export and reload bindings (H1–H9). Platform
  requests map to `/api/db/platform`, `connections`, `tables`, `columns`, `health`,
  `query`, `cancel`, `inspect`, `record-link`, `assistant/config`, `assistant/chat`.
  Companion routes are `/api/platform`, `pair`, `unpair`, `personal`, `local/up`,
  `local/down`, `schema/import` and the scoped query/metadata routes. Platform
  unsupported standalone endpoints return deliberate local capability responses,
  not server configuration access. Account-scoped recovery/pairing keys are
  `qy_platform:[origin,account]:recovery/pairing`; personal values retain legacy
  `qy_tabs`, `qy_tabres`, `qy_hist`, `qy_auto_run` and UI preferences, plus
  `qy_assistant_conversations`, `qy_assistant_prompt_drafts`,
  `qy_pending_query_history`. SQLite documents are `workbench/workspace` and
  `query-history/entries`. H1–H11 cover interactions, disconnection and ownership.
- **Capability:** the editor must open without either backend, preserve unsaved
  SQL, explain where it is saved, reveal unavailable actions and reconnect
  without execution. Paired Local creation/status/stop and PostgreSQL structure
  transfer are exposed explicitly. Existing engine limits remain: managed Local
  supports PG/Redis/Neptune; schema sync is PG-only. Platform shared SQL and cloud
  personal-history synchronization are excluded by the agreed scope. Browser
  caches contain static assets only; a first-ever offline visit cannot download
  an application it has never loaded.
- **Shared state / all write sites:** `platform.ts` writes recovery values via
  `personalStorage.setItem/removeItem`, SQLite scheduling/flush, initial hydration,
  explicit conflict reload and canonical history enqueue/flush. Account changes
  stop the old page before applying an in-flight response. Readers are existing
  tabs, history, UI stores, assistant state and connection controls; platform
  target IDs include project/database plus placement/resource ID. Tab SQL write
  sites remain `tabsStore.updateTab/updateActiveTab`, editor typing, table and
  Alt-table previews, key inspect, Format, history/link restoration, saved-query
  loading (standalone only), assistant apply/restore, and tab close through
  `keepDraft/pushHist`. Result writes remain per-tab snapshots with request guards.
  H2/H4/H6/H7/H8 interact across those readers, recovery, results and assistant
  checkpoints; the existing standalone overwrite/race/production-auto-run tests
  continue to protect their original paths.

### 2026-11-04 hosted schema/cancellation contract corrections

- **Existence:** H5 asserts the actual schema-import request carries
  `sourceResourceId` and target `resourceId`; its fixture rejects a missing or
  mismatched target just as the companion does. H8 covers a second query started
  before the first query's cancellation acknowledgement. No interaction bindings,
  API routes or storage keys were added.
- **Capability:** explicit schema transfer can reach the selected Local target.
  Cancelling one execution leaves a newer execution on the same tab running and
  able to publish its result. Other connection-dialog payloads were checked
  against the companion: pair (`code`), unpair (empty object), Local creation
  (`db`, `engine`, `project`, `sourceResourceId`), stop (`engine`, `resourceId`),
  and personal read/reload/save (`kind`, `key`, `value`, `revision`) agree.
  No additional capability gap was introduced.
- **Shared state / all write sites:** request sequence writes are `startReq` and
  the guarded cancellation acknowledgement; request-ID writes are `startReq`.
  Pending-state writers are `startReq` (true), `endReq` (false only for the current
  sequence), and the cancellation acknowledgement (false only for its captured
  sequence). Result/error readers and writers continue through `isCurrentReq`,
  `applyTabResult` and `reqFailed`. The cancellation-ID set in `api.ts` still
  adds/removes only the cancelled request's UUID. H8 interacts across both
  requests, the pending control and active-tab results, including an old response
  arriving after the newer result.

### Hosted header and database labels (2026-11-05)

For manual inspection, run `make preview-hosted` and open the printed loopback
URL. It builds the latest assets and serves demo hosted resources without IAM
credentials. SQL returns a fixed demo row and browser storage retains drafts.
Add `PREVIEW_ARGS='--disconnected'` to preview cloud unavailability. Assistant
and backend authorization require separate acceptance.

- Existence: the header connection badge expands remote/local/save status only; covered by F:test_platform_header_connection_badge_and_group_relative_labels and the existing H3/H9 tests. Hosted rows show group-relative labels with full identity in the title.
- Capability: connection status reuses the standalone keeper position; details remain available without a second full-width toolbar. Existing standalone keeper behavior is unchanged.
- Shared state: no store keys or state writers changed. Only labels are shortened; selection, query dispatch, result ownership, history and search continue using full database IDs. The browser interaction checks dispatch to the original resource after selecting a shortened label. Theme coverage remains V:test_platform_status_chrome_uses_theme_tokens.

### Automatic local connection and persistence (2026-11-05, superseded)

This section describes the former cloud-origin companion. See the cloud-only and native workbench audits for the current behavior.

- Existence: one-use `quarry_local`/`quarry_code` launch fragment parameters enter the existing pairing path and are removed before any await (H13). Connection/address/code forms, reconnect/revoke/save-now controls and the combined local administration dialog have been removed. H9 covers the remaining read-only status. Recovery export appears only on errors/conflicts, and explicit reload only on conflicts (H6). H5 honestly records the removed GUI administration capability; backend and CLI capabilities remain separate.
- Capability: launching from `ylab db connect` connects and saves without a setup interface. A five-second background retry and the browser online event restore failed connections. Remote querying and browser draft recovery remain available without a companion. Connection retry never creates databases, synchronizes schema or executes SQL. Native scope/revocation checks remain enforced.
- Shared state / all writers: launch calls `pairCompanion` (pairing memory and scoped storage), `initializePlatform` loads the saved pairing, and the retained non-GUI `unpairCompanion` clears it. `reconnectRemote`/`reconnectLocal` publish connection/resource status and are invoked by initialization, launch, periodic retry and the online event; one retry guard prevents overlapping background attempts. Account checks run before retry. `scheduleSave` and `flushPersonal` retain browser recovery, SQLite CAS and bounded history; `hydratePersonal` may load unchanged local records or surface a conflict, while `loadPersonalFromCompanion` remains an explicit conflict choice. H3 tests a real failed save followed by unattended retry; H2 tests resource/result ownership across refresh, H6 protects conflicts, H13 covers launch plus typing/saving. No new storage key was introduced.


### Official hosted authentication (2026-11-05)

- Existence: `/api/hosted/config` supplies the validated `apiBase`, session key and same-origin login/refresh/logout paths. Built-in login/logout and one-use handoff consumption use `/api/hosted/auth/*`. The new public `qy_hosted_config:v1` cache stores only non-sensitive runtime settings, never tokens or personal data. H14–H16 cover the new bindings and consumed endpoints. The login card's colors are tokens and checked in both modes.
- Capability: a generic image can authenticate its configured administrator without a host-specific frontend build. Existing platforms use their configured identity session; no password form appears for external authentication. Standalone remains fully independent (H17). Local connection and SQLite saving continue automatically; no pairing form was added. The existing offline shell continues to work from a cached runtime document and account-scoped recovery.
- Shared-state / all writers: `signIn`, `consumeHostedHandoff` and `refreshSession` write the configured session via `saveHostedSession`; `signOut` removes only that key. Before accepting handoff, an existing different account is rejected. `loadHostedConfig` writes the public config cache and validates live and cached paths; a malformed live config fails closed. `readSession`, refresh/current-session guards, the storage event and periodic retry all read the same configured session key. Account-scoped recovery/SQLite writers are unchanged. H14 exercises sign-in/query/logout, H15 verifies refresh leaves another session key untouched, H16 covers fresh/expired/cross-account handoffs, and H17 verifies hosted state does not affect the standalone query path.

### Offline icon fonts and cached application upgrades (2026-11-05)

- Existence: no interaction binding, localStorage key or API endpoint changed. H10 now covers the bundled Tabler font's actual `?v<SemVer>` URL, with HTTP cache disabled and real service-worker font responses on two offline reloads. Other asset queries, extra font parameters and API responses remain excluded from CacheStorage.
- Capability: header, sidebar, editor, toolbar and grid icons must remain visible beside their text when a previously loaded editor reopens offline. The regression asserts the actual FontFace is loaded and an icon's pseudo-element contains its glyph, for both a fresh worker and an application replacing an already active worker. The existing first-ever-offline limitation in Design gaps remains unchanged.
- Shared state / all writers: `rememberShell`, the warm-message handler and the fetch handler are the three `cache.put` sites; activation is the only cache-deletion site. Only public shell/assets enter those writes. `prepareOfflineShell` now targets the newly registered worker after bounded activation, removes its state listener on every terminal path, and writes only its warm message. No connection, SQL, result, draft, history, assistant or identity store writer changed. H10's existing draft restoration test and the new fresh/upgrade font interaction cover the cache readers and the first warm after an upgrade.

#### Hosted release notice

- Existence: H18 maps the same-origin `/api/hosted/update` request, focus and interval checks, `qy_hosted_update_dismissed` storage key, release notes link, close button and translated labels. The component mounts from the hosted sidebar. The browser test verifies standalone isolation, refresh on focus, dismissal across reload, a later version and clearance after upgrading. The visual test checks token resolution in both themes.
- Capability: users can see the available hosted version, open its release notes and dismiss the notice while keeping the workbench available. The notice follows the release reported by the server and presents release information for the operator's existing deployment process.
- Shared state and write sites: the close handler is the sole writer of `qy_hosted_update_dismissed`. Initial state and the same-origin storage event read it. The fetch effect only writes component-local release state. H18 verifies a draft survives the advisory checks and dismissal without any SQL execution.

#### Assistant reply scrolling

- Existence: the existing scroll and submit handlers and the pin after rendering are covered by rows 121 and 126.
- Capability: the assistant follows a growing reply from the bottom and leaves the scroll position to a reader who has moved upward. The browser tests exercise both behaviors.
- Shared state and write sites: the scroll handler and prompt submission write the component's follow flag. The layout effect reads it and writes the message list's scroll position.

### Cloud-only hosted workbench audit

- Existence. The status toggle reports the cloud connection and browser storage
  (H9). The conditional recovery export is exercised by H6, while sign-in and
  sign-out still use H14–H16. The existing account/origin-scoped
  `qy_platform:…:recovery` document stores browser records (H1, H6, H7).
  Initialization strips the obsolete launch fragment and discards the scoped
  pairing key (H13). The periodic retry and browser online event only refresh
  `/api/db/platform` (H3). Hosted resource metadata, queries, cancellation and
  assistant requests all use the discovered cloud API base (H2, H4, H8, H12).
- Capability. The hosted sidebar lists authorized cloud resources and the header
  describes cloud availability. Editor tabs, history, and assistant conversations
  use browser storage. Active-tab result/export behavior and request identity
  remain shared with the standalone workbench. The hosted-only limitations for
  offline first load, local resource access and shared history are in H5, H10 and
  Design gaps.
- Shared state. `hydrateBrowserRecords` initializes the hosted document before
  loading the workbench stores. Every later browser-record change passes through
  `personalStorage.setItem` or `removeItem`, then `saveBrowserRecords` checks the
  current account. Tabs, history, assistant conversations and preferences still
  read the same facade. H1 checks account isolation, H6 checks recovery under a
  failed write, H7 checks page-only results alongside live drafts, and H4
  checks assistant checkpoint restoration with manual edits. Only cloud bootstrap
  replaces the authorized catalog. Reconnection updates status and the sidebar
  without replaying SQL or changing the editor (H3).

### Native cloud sources audit

- Existence. Rows 128–129 cover the extra environment badge, catalog
  `cloudStatus` field and native focus listener, source-sensitive connection
  info, and the new same-origin `/api/cancel` binding. Connection selection,
  SQL execution, metadata and optional connection fields use the existing
  `/api/connections`, `/api/query`, `/api/tables`, `/api/conninfo` and health
  operations. The assistant rail uses the same source field to disable the local assistant for cloud targets without changing tab or conversation state. Deep links and saved runs now carry the request ID through the
  same execution contract. The source badge does not introduce a storage key.
- Capability. The native header keeps workspace settings and Tunnel Keeper
  while reporting cloud status. The environment switcher shows the cloud
  source even for a database with one environment. Search, history, tabs,
  the browser title and result status show its display name; the URL carries
  its route identity. Local configuration remains available in connection
  info; cloud credential and schema-sync limits are in Design gaps.
- Shared state. Initial/workspace catalog load and focus refresh are the
  catalog and cloud-status writers. Only a changed catalog revalidates
  the selected target. Environment selection, deep links, tab restore and
  tab switching keep the route ID in connection, tab and result state, and
  clear the active connection while preserving drafts for missing cloud
  routes. Run, navigation, metadata, health, assistant context and exports
  still use that identity; formatting labels never changes it. The same-name
  browser test checks execution, metadata, separate results and drafts,
  and a late result after cancellation. The outage test checks that catalog
  refresh does not replay or retarget the retained draft.

#### Federated accounts and exact targets

- Existence. A timer and focus listener refresh the protected catalog through `/api/connections`. The catalog carries source labels, endpoint identities and connection status. A running pure-local GUI shows how to reopen when a provider registers. Native links and requests carry the producing target, and tabs and result snapshots store it without adding browser storage keys.
- Capability. Separate provider groups remain readable under repeated database names. Explicit association adds a cloud environment to the matching local item. Connection details and local SQL assistant availability follow the selected source. The status control reports disconnected, signed-out, forbidden and newly registered provider states. A protected launch opens a nearby loopback listener with a one-use link.
- Shared state. Catalog load and refresh determine which cloud routes can render. The tab store tracks exact identities for new drafts and results. A missing route clears its visible SQL and results, invalidates pending request sequences, and retains its persisted draft. The active tab, history, grid, result modals and export controls all use the same visibility check. Personal hydration reads a scoped projection. Revisioned writes preserve hidden drafts and browser recovery excludes cloud SQL and rows. The account-change browser test covers local result continuity, private history and a late result after the previous account becomes active again.

### Native workbench personal records audit

| # | Area | Feature | Covered by | ✓ |
|---|------|---------|------------|---|
| NP1 | editor/tabs | Protected launch hydrates the configuration's SQLite workbench document before constructing the editor. Drafts survive reload without browser records. Provider drafts are projected for the verified account. | F:test_native_workbench_records_reopen_from_sqlite_without_browser_records; Rust `workbench_projection_keeps_other_accounts_drafts_private_on_update` | ✅ |
| NP2 | persistence/recovery | Failed hydration makes no SQLite writes. A concurrent write preserves the current draft and offers export and confirmed reload. On reopen, an older recovery with matching tabs and no additional content yields to the newer saved document. | F:test_native_workbench_failed_read_keeps_recovery_without_writing_sqlite; F:test_native_workbench_conflict_keeps_draft_and_loads_saved_copy_explicitly; F:test_native_workbench_reopen_uses_newer_records_when_recovery_adds_no_content | ✅ |
| NP3 | launch and history | Protected launch clears the one-use fragment, exchanges it for a loopback session, and reopens with that session. Quarry and YLab use the same SQLite history for the selected configuration. Local history stays available through cloud disconnection. | F:test_native_workbench_records_reopen_from_sqlite_without_browser_records; Rust native history tests; YLab native process tests | ✅ |
| NP4 | large workbench documents | Saves use the existing 16 MiB SQLite budget in UTF-8 bytes. Optional result snapshots are omitted first. A larger draft stays available for editing and recovery, and shortening it resumes saving at the same revision. | F:test_native_workbench_large_save_prioritizes_drafts_over_results; F:test_native_workbench_oversized_draft_can_resume_saving; Rust `native_personal_http_preserves_sqlite_document_budget_and_revision` | ✅ |

- Existence. Native startup reads `/api/personal` after validating the loopback
  session via `/api/workbench/session`. SQL recall reads `/api/history` at startup
  and on focus. The save path sends only a revisioned workbench document. The
  scoped `qy_native:…:recovery` key keeps the current window copy in session
  storage and a browser backup when the shared slot is available. Updating or
  clearing this window's recovery preserves a different window's backup. The save warning, export and confirmed load action
  are exercised in NP2 and NP4. NP4 adds a size-specific status to the same
  recovery control, with no new endpoint or storage key.
- Capability. Drafts, tabs, bounded tab results and editor preferences reopen
  with the native account without a cloud connection. The warning makes failed
  saves recoverable. Multiple windows can recover or explicitly load a newer
  revision. Legacy SQLite documents retain their 16 MiB budget. Larger
  documents keep draft SQL in the editor and can resume saving after an edit
  reduces their size. Automatic merging of conflicting drafts is deferred.
- Shared state. `initializeNativeRecords` loads the saved document before
  tab/history/preferences readers initialize. The storage facade's two write
  sites update the in-memory document and browser recovery. `flushNativeRecords`
  clears recovery only for the revision it saved, and retains edits made while
  saving. The save body can omit result snapshots without changing active-tab
  state or dropping draft SQL. A size refusal leaves the pending revision and
  recovery intact. On startup, matching tab records and absence of additional
  recovery content allow hydration from a newer revision, including its
  authorized cloud scopes. Divergent recovery still requires confirmed load
  to discard. Separate query-history entries are outside this CAS document.

### Header project-link audits (2026-11-05)

- Existence: the brand and GitHub anchors are covered by row 130. They add no
  event handlers, browser storage keys or Quarry API calls. The brand reads the
  same locale as the existing Voyage language control. Voyage 0.19 exposes three
  new palette cards through the existing selection binding (row 131).
- Capability: project discovery and source access are available from the
  standalone and hosted headers. Native links support pointer and keyboard
  activation, and GitHub follows the palette control with the same icon box.
  The existing narrow-window header gap is recorded in Design gaps. Each new
  palette card applies its preset in both modes and remains selected on reload.
- Shared state: both links open a separate tab and write no workbench state.
  Quarry supplies the repository URL via VoyageToolbar.githubHref.
  Connection selection, SQL drafts, active results and theme preferences stay
  with the original workbench.
  Palette selection and mode use the existing Voyage preference write path.
  The theme attributes and selected-card state read those preferences,
  including after reload.

### Result image audit (2026-11-11)

- Existence audit maps column mode changes, thumbnail enlargement, retry, Copy URL, Open original, backdrop/Escape dismissal and modal focus controls to rows 132–134. Image display adds no API endpoint or localStorage key. Existing cell click/double-click and keyboard shortcuts remain rows 50/53.
- Capability audit checks inline visibility, original value access, failure recovery, large preview, text fallback and browser caching. Server-side resizing, custom authentication headers and persisted display overrides are recorded in Design gaps. The original image is downloaded even for a small thumbnail.
- Shared-state audit enumerates every new write site. `imageModes` is initialized in ResultWorkbench and updated only by the column selector, keyed by active tab ID and result column names. Its reader supplies both the selector value and URL detection. `imagePreview` is initialized there, written by thumbnail activation, and cleared on close or tab change. Its reader only mounts the matching tab's modal. ImageLoad owns visibility, status and retry attempt locally, with URL-keyed mounts isolating sort/query changes. Query snapshots, original cell values and export sources are untouched. Rows 132/133 exercise the overlapping controls, sorting, tab restoration and raw JSON export.
- Validation includes the frontend build, eslint, all 125 frontend unit tests, native `cargo build -p quarry-cli`, and all eight image browser/visual cases passing against the built Rust binary. Both dark/light visual cases verify equal Image/Text row heights, zero thumbnail border and narrow-viewport modal geometry. The HTTP-cache case uses an actual local image server and removes Playwright routes so the browser cache stays enabled.

- Image layout follow-up uses one inherited line-height for thumbnail and cell content, with no surrounding border. Candidate controls derive from original result URLs confirmed by successful browser decoding, so sorting and Text mode preserve the available selector. Ordinary text/number columns stay compact. Browser/visual cases cover selector absence on id and HTML URLs even with icon_url names or image extensions and equal row heights after toggling Image/Text.

- Decode-confirmation uses positive naturalWidth/naturalHeight after image load. Column names and file extensions do not establish image type. The browser case holds an extensionless response to prove its control is absent until decoding succeeds, while a text/html response named icon_url and ending in .png never gets a control.
- Image indexing runs in `tabsStore.setTabResult` for accepted snapshots, and lazily when displaying a restored result. A WeakMap keyed by the immutable result object reuses the derived index on tab return. Each scan skips empty/non-string values, checks HTTP prefixes before URL parsing, and parses duplicate values once. Each column maps original row objects to candidate URLs, so sorting reads the same entries. Rendering does not parse URLs. Each ResultImageColumn records successful decoding against the original row in a WeakMap, confirms its column once, and notifies only subscribed ResultImageModeSelect headers through useSyncExternalStore. Appended results carry confirmation from retained row objects, including while Text mode suppresses image mounts. An explicit Text/Image override keeps its column selector available when rerunning a query with the same column schema, before the new result has decoded any image. ImageLoad state stays local. The index and confirmation metadata are not serialized with query data. Existence audit maps these readers/writers to rows 132–134. Capability and Design gaps remain as listed. Unit cases pin single-pass parsing and isolated notification, while browser cases cover sort/export, mode selection, tab return, retry and actual decode gating.

- Double-click propagation is stopped only inside the thumbnail/large-image host. Adjacent URL text reaches the existing cell handler for short-value copy and long-value inspection, including URLs whose responses contain HTML. F:test_result_image_url_text_keeps_existing_double_click_actions covers both text paths and thumbnail enlargement.
