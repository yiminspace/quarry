# Agent rules for this repo

Guidance for AI coding agents (and humans) working on Quarry.

## GUI changes (`web/` frontend + `apps/qy/src/http.rs` backend)

The production runtime is Rust: `apps/qy` adapts the shared crates to CLI,
HTTP and MCP. The React/TypeScript SPA in `web/` is embedded from
`src/quarry/web_dist` at compile time. Build `web/` before Cargo when UI changes.
Python source is a development/test baseline only and is not shipped as runtime.

1. **The GUI feature matrix in [TESTING.md](TESTING.md) is the source of truth
   for frontend coverage.** Any change that adds, alters, or removes a
   user-visible GUI behavior must update the matching matrix row(s) in the same
   PR — including honest status downgrades (✅→🟡/❌) when behavior changed and
   the old test no longer proves it.
2. **New feature points get a browser test** in
   `tests/test_gui_browser_features.py` (page fixtures come from
   `tests/conftest.py`; console errors are an autouse invariant in that
   module). Backend/API logic belongs in `test_gui_backend.py` /
   `test_gui_api.py` — do not re-test engine behavior through the browser.
3. **Design tokens are law**: every visible color in `web/src` must reference
   the CSS variables defined at the top of `web/src/App.css` (the legacy
   "Slate & Copper" palette, dark default + `html[data-theme=light]`). Never
   introduce a literal color value in a component or stylesheet body.
   `tests/test_gui_visual.py` pins the tokens via `getComputedStyle` in both
   themes — extend it when styling new chrome.
4. **Run all three audits after touching `web/src/`** (details + state
   inventory in TESTING.md "Keeping the matrix honest"):
   - *Existence*: every interaction binding, localStorage key, and consumed
     `/api/*` endpoint maps to a matrix row (catches untested code);
   - *Capability*: per UI region, list what a SQL-client user would expect,
     diff against reality, file misses in the Design-gaps table (catches
     missing features — existence auditing can never see these);
   - *Shared-state*: when writing any store state (`connStore`, `tabsStore`,
     `uiStore`, the per-tab result snapshots, sort/selection state, …), check
     every reader; features sharing state need an interaction test (catches
     cross-feature bugs like "export uses another tab's result").
5. **When adding a UX invariant, enumerate ALL write sites** of the protected
   state (grep, list them in the PR) and cover each — partial rollout of an
   invariant is how "closing a tab loses SQL" shipped.
6. **UX invariants that must never regress** (each is pinned by a browser test):
   - hand-written editor SQL is never silently lost — a table click with
     existing SQL opens a new tab (the draft stays on the previous one);
     every remaining editor overwrite (Alt+click table / key inspect /
     saved query / history recall / tab close) goes through `keepDraft()`/
     `pushHist()` in `useSqlHistory`; Cmd/Ctrl+↓ restores the stash;
   - connections configured with **`production = true` default to Auto-run off**;
     automatic execution requires an explicit opt-in for that workspace/database/
     environment and production classification. Changing the mode never runs
     the current SQL or a pending navigation; environment names (including `prod`
     and `jp`) are labels only;
   - overlapping query responses are **latest-wins** (the per-tab
     `startReq`/`isCurrentReq`/`endReq` request-tracking guard) — a stale
     response must never repaint the grid;
   - the grid, status bar, and exports always reflect the **active tab's**
     result (its own snapshot in `tabsStore`), never another tab's;
   - column sort is numeric-aware, and a third click restores original order;
   - lists are never silently truncated (redis 400-key cap, 5000-table cap →
     visible notice).

### Frontend dev loop

`web/` is a standalone npm project: `npm install && npm run dev` for a hot-reload
dev server, `npm run build` to produce `src/quarry/web_dist/` (what ships in the
wheel — end users still `pip install` with zero runtime dependencies; Node is
dev/CI-only). `npm run lint` runs eslint. CI builds `web/` before running the
Python test suite so `/quarry` assets exist for the browser layer.

**The browser suite tests the BUILT bundle, not `web/src`** — after changing
anything under `web/src`, run `cd web && npm run build` before `pytest`, or you
will be testing the previous build.

## Output & docs

- User-visible changes get a `CHANGELOG.md` entry under `[Unreleased]`.
- External output (PR titles/bodies, commit messages) carries business-relevant
  content only — no internal debugging-tool narration.


## Releases

`main` owns Rust 2.x. `archive/python-1.x` is locked at the last Python release.
Versions must agree: Cargo SemVer `2.0.0-rc.N`, PyPI PEP 440 `2.0.0rcN`, MCP
server SemVer plus its nested PyPI package version. Run scripts/validate_release.py.
Tag releases explicitly; never publish a stable tag until the rc platform-wheel,
native archive, fresh-install and Python-upgrade gates pass. Never bundle local
configuration, tokens, workspaces or verification artifacts. CI uses disposable
containers, never the user's Local data volumes.
