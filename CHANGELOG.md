# Changelog

All notable changes to Quarry are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[SemVer](https://semver.org/) (pre-1.0: minor bumps may break).

## [Unreleased]

### Added

- **`qy speedtest <db>` tunnel-path benchmark**: generate a bounded
  PostgreSQL/MySQL result payload without printing it, repeat the same
  end-to-end query path, and report per-run plus median elapsed time and
  effective download speed. `--bytes`, `--runs`, `--env`, `--timeout`, and
  machine-readable `--format json` make proxy-node comparisons reproducible;
  the probe deliberately skips the GUI's separate column-type lookup.

- **Workspace tunnel keep-alive keeper + shared tunnel attach** (#112): `qy
  up/down/status` now manage a workspace-owned keeper that keeps SSH tunnels
  warm and reconnects dropped forwards with exponential backoff. CLI/GUI/MCP
  processes now attach to an already-live registry entry for the same
  ssh/db/proxy dimension instead of spawning duplicate tunnels, so warm
  forwards are reused cross-process. Keep-alive/reconnect opt-ins are persisted
  per workspace in `config.toml`; `qy status` and the GUI header expose
  `up/reconnecting/down` state; and `qy exec`/`qy run` print a one-line hint
  when keep-alive is configured but the keeper is down. Neptune tunnel defaults
  now align with its standard port (`8182`) and the same `ssh_*` participation
  model.

- **`qy ping <connection>|--all`** (#110): a lightweight reachability probe
  for configured connections, distinct from `connections test`'s richer
  "connected to \<db\>" report. PG/MySQL run `select 1`, Redis runs `PING`,
  Neptune reuses `connections test`'s existing probe — no new per-engine
  protocol. Reports ok/fail and elapsed time per connection (`--all` walks
  every configured connection and adds a reachable-count summary); exits 0
  when every probed connection is reachable, 1 if any failed (including a
  timeout), with the failure reason included. Probe timeout defaults to 10s,
  overridable with `--timeout`.

- **GUI result status bar shows download size and average speed** (#106):
  alongside row count and elapsed time, the status bar now reports the
  download size (auto-scaled B/KB/MB) and average speed (auto-scaled
  B/KB/MB per second), building on #104's `downloadBytes`/`sizeIsEstimated`.
  Estimated sizes (Postgres/MySQL/Redis) are marked with `≈` and a tooltip;
  "Load more" accumulates both figures across pages instead of resetting them.

- **`qy exec`/`qy run` print a query stats summary** (#105): after the result
  data, a one-line stderr summary reports elapsed time, download size
  (auto-scaled B/KB/MB), and average speed (auto-scaled KB/s/MB/s), building
  on #104's `elapsed_ms`/`download_bytes`/`size_is_estimated`. Postgres/MySQL/
  Redis mark the size and speed with `≈` since their byte counts are
  approximations; Neptune's is exact. The summary never touches stdout, so
  `--format json/csv/ndjson` piping is unaffected.

- **Query results now report download size** (#104): `QueryResult` (used by
  the GUI, MCP, and `--format json`) carries `downloadBytes` and
  `sizeIsEstimated` alongside `elapsedMs` — an exact response-body byte count
  for Neptune, and an approximation (subprocess/serialized-rows size) for
  Postgres/MySQL/Redis. Display of these values (e.g. average speed) is left
  to downstream CLI/GUI work.

- **Proxy effect is now observable, not just configurable** (#101): when a
  workspace has the proxy enabled but a query still ran direct, `qy
  exec`/`qy run` print a one-line reason to stderr (no proxy discovered /
  discovered but unreachable / target in the exceptions list) — suppressed
  by `--no-proxy`. `qy proxy` now also lists every pooled SSH tunnel (ssh
  target, local port, whether it's actually proxied and through which
  address, and whether the `ssh` process is still alive), with a matching
  `tunnels` array under `--format json`. The GUI shows the same
  server-computed state: a badge on proxied connections' env pills, and each
  workspace's proxy toggle plus the currently discovered proxy address in
  the workspace manager. Flipping a workspace's proxy toggle now also tears
  down the stale-dimension SSH tunnel for any connection already pooled
  under the old toggle state, instead of leaving it running until process
  exit.

- **SSH tunnels can route through the system's HTTP(S) proxy, per workspace**
  (#96): a cross-border `ssh -L` forward can throttle to a crawl even though
  the handshake itself connects fine, so `qy` can now tunnel the same SSH
  session through your machine's proxy instead. `qy proxy` shows the
  discovered proxy (macOS system settings via `scutil`, falling back to
  `ALL_PROXY`/`HTTPS_PROXY`) and each workspace's toggle state; `qy proxy
  on|off [--workspace <dir>]` persists the toggle to `config.toml` (never
  touches `connections.toml`); `--no-proxy` overrides an enabled toggle for a
  single `qy exec`/`qy run` call. The toggle only applies to connections with
  an `ssh_host` (routed via `ProxyCommand`) and to Neptune's direct HTTPS
  requests — `qy connections add/set` now warns if the proxy is enabled but
  the connection has neither. If the proxy is enabled but nothing is
  listening on its port, `qy` silently falls back to a direct connection
  rather than erroring; the system proxy's exceptions list (loopback +
  private CIDR ranges) is always respected.

- **Configurable query timeout, split into connect/execute phases, with a
  database-side backstop** (#94): `qy exec`/`qy run` gained `--timeout N`
  (seconds, must be positive); it's also settable via the `QUARRY_TIMEOUT`
  env var or a per-connection `timeout` field in `connections.toml`
  (`qy connections add/set --timeout N`) — priority: `--timeout` >
  `QUARRY_TIMEOUT` > connection setting > default. Connection establishment
  (including SSH tunnel setup) is now capped independently (15s) from query
  execution (300s default for CLI/GUI, 120s for MCP), so an unreachable host
  fails fast instead of eating the whole query budget — this now overrides
  any `connect_timeout` already present in a PostgreSQL connection URL, since
  libpq would otherwise honor the URL's value over ours. On PostgreSQL, `qy`
  also sets a server-side `statement_timeout` (~90% of the execute timeout);
  on MySQL/MariaDB it sets the equivalent session variable best-effort
  (`MAX_EXECUTION_TIME` / `max_statement_time`, whichever the server
  supports) — so the database cancels a runaway query itself and reports the
  real reason, instead of leaving a zombie query running after the client
  gives up. Every timeout error now says how to raise it.

### Changed

- **Large GUI results no longer freeze the browser main thread**: grid and
  row-detail cells render a bounded 512-character preview instead of copying
  multi-megabyte values into text, `title`, and `data-*` DOM slots. Full values
  remain available for copy/export and open in a chunked inspector; JSON trees
  now mount branches only when expanded. Results above 512 KiB stay available
  for the current session but skip synchronous `localStorage` persistence (with
  a visible status notice), and editing SQL or switching tabs no longer
  reserializes unchanged result snapshots.

- **`qy schema`/`describe-table` and the MCP server now cache table and
  column lookups like the GUI already did** (#97): repeat metadata lookups
  over a slow SSH-tunneled connection used to re-query the database every
  time from the CLI or an MCP-connected agent, even though the GUI's schema
  panel was already caching the same information. All three now share one
  on-disk cache (`~/.cache/quarry/gui-cache.json`, unchanged location and
  format — an existing cache file from before this change keeps working),
  so a lookup already made in one is instant in the others. This covers
  `qy schema`'s default (plain, unflagged) output too, not just `--format
  json`. The MCP server's Redis key listing now shares the GUI's cached key
  list rather than always re-scanning, capped at the same 400 keys.
  Connectivity checks (`qy connections test`, the GUI's status dots) still
  self-invalidate automatically whenever a connection's URL, SSH settings, or
  proxy toggle changes — no stale "connected" dot after an edit.

- **Upgraded `@yiminlab/voyage` to 0.8.0** (#92): the header's language,
  theme, and palette buttons now render through the new `VoyageToolbar`
  composite instead of two separately-arranged components, so their DOM
  order (language → theme → palette) is fixed by the design system rather
  than left to the host's JSX. The language button's width is also locked
  regardless of its "中"/"EN" text, so switching language no longer nudges
  the theme/palette buttons sideways. Purely structural — no visual change.
  (Voyage's `.vg-toolbar` class name is shared between the new header
  composite and the existing query toolbar's chrome; the header now resets
  the query-toolbar's padding/background/border-bottom so it doesn't leak
  into the header row.)

- **Upgraded `@yiminlab/voyage` to 0.7.0** (#90): the header's language,
  theme, and palette buttons now share one unified `.vg-iconbtn` box spec
  (same height, min-width, and border-radius — the radius follows the
  current style axis instead of being hardcoded), so all three sit
  pixel-aligned instead of the language button being a differently-shaped
  badge. The connection-info modal's smaller action icons (`.ciact
  .iconbtn`) got their `min-width` override added alongside `width` so they
  no longer get stretched back up by voyage's new fixed-box sizing.

- **The header's language toggle now uses `@yiminlab/voyage`'s
  `VoyageLangSwitcher`** (#88) instead of bare "中"/"EN" text stuffed into
  the circular icon-button slot, matching the rest of the header's icon
  styling. Behavior is unchanged: click to switch locale and reload.

### Fixed

- **`pip install -e` no longer breaks `qy`**: the bundled CHANGELOG.md (#80)
  was also force-included into editable wheels, creating a stray
  `site-packages/quarry/` directory that shadowed the editable install's
  redirect to the source tree (`ModuleNotFoundError: No module named
  'quarry.cli'`). CHANGELOG.md now ships in standard wheels only; editable
  installs keep reading the repo-root copy.

### Added

- **The GUI now shows what changed after you upgrade** (#80): CHANGELOG.md
  ships inside the package, and a new `GET /api/changelog` parses it into
  structured version entries. When the running version differs from the one
  last recorded in the browser, the header's What's New panel (sharing the
  update badge's panel styling from #79) opens automatically with the
  changelog entries for the new version(s), then stays quiet until the next
  real upgrade.

- **The GUI now tells you when a new Quarry release is out** (#79): a
  throttled background check (once per 24h, `QUARRY_UPDATE_CHECK=0` to
  disable) polls PyPI for `quarry-db` and, when a newer version exists, shows
  a badge in the header — click it for the current/latest version, the exact
  `pipx upgrade quarry-db` command, and a link to the release notes. Editable/
  dev installs are skipped automatically, and any network hiccup stays
  completely silent.

- **The GUI now notices the world changing under it** (#78): a new
  `GET /api/events` SSE channel plus a backend file watcher. Editing
  `connections.toml` or `queries/**/*.sql` on disk (CLI, agent, `git pull`…)
  refreshes the sidebar lists in the open page without a manual reload, and
  after upgrading Quarry a restarted backend triggers a persistent
  "reload page" banner instead of leaving a stale UI running. Events are
  refetch hints (`{type, ts}`), never data carriers — the contract future
  update/What's-New notifications build on.

- **`qy connections add`/`set` now catch local-dev-.env misconfigurations
  before writing to `connections.toml`** (#76): a new connection whose
  `host:port` is already claimed by another entry is rejected by default
  (with the occupant's key and purpose printed) unless `--force` is passed;
  a loopback host (`127.0.0.1`/`localhost`) with no `ssh_host` prints a
  non-blocking reminder to double-check the target isn't actually meant to
  be a remote server (fixable in place with the new `--ssh-host`/
  `--ssh-user`/`--ssh-key`/`--ssh-port` flags); and an `env=local` key that
  doesn't follow the `<name>_local` convention gets a naming suggestion.

<!-- version list -->

## v0.21.0 (2026-07-31)

### Features

- Add tunnel speedtest command ([#114](https://github.com/yiminspace/quarry/pull/114),
  [`87c0995`](https://github.com/yiminspace/quarry/commit/87c0995c7c37f33a9bcdd800eb17e5f4c99557bd))


## v0.20.0 (2026-07-28)

### Bug Fixes

- Address keepalive review findings on PR #113
  ([#113](https://github.com/yiminspace/quarry/pull/113),
  [`40350d0`](https://github.com/yiminspace/quarry/commit/40350d0d1d2ff4d97606c88f72db03424dffeb0b))

### Features

- Add workspace tunnel keeper and shared tunnel attach
  ([#113](https://github.com/yiminspace/quarry/pull/113),
  [`40350d0`](https://github.com/yiminspace/quarry/commit/40350d0d1d2ff4d97606c88f72db03424dffeb0b))

### Testing

- Add keepalive unit coverage for CI gate ([#113](https://github.com/yiminspace/quarry/pull/113),
  [`40350d0`](https://github.com/yiminspace/quarry/commit/40350d0d1d2ff4d97606c88f72db03424dffeb0b))


## v0.19.0 (2026-07-23)

### Features

- Add qy ping subcommand for connection health checks
  ([#111](https://github.com/yiminspace/quarry/pull/111),
  [`88985c8`](https://github.com/yiminspace/quarry/commit/88985c89c411938b848f2c38679b0cdd1970892e))


## v0.18.0 (2026-07-22)

### Features

- **gui**: Show download size and avg speed in result status bar
  ([#109](https://github.com/yiminspace/quarry/pull/109),
  [`ad42eee`](https://github.com/yiminspace/quarry/commit/ad42eeea70a14405619aeb29a6773b5ae9e079fa))


## v0.17.0 (2026-07-22)

### Features

- Report elapsed time, download size, and avg speed on qy exec/run
  ([#108](https://github.com/yiminspace/quarry/pull/108),
  [`c04a515`](https://github.com/yiminspace/quarry/commit/c04a515b4e85b44fde33ff4cf38489538178db0e))


## v0.16.0 (2026-07-22)

### Features

- Report download size and CLI query timing (#104)
  ([#107](https://github.com/yiminspace/quarry/pull/107),
  [`4643c53`](https://github.com/yiminspace/quarry/commit/4643c53bca5176a60b0a285202c6e1d3d1033ad3))


## v0.15.0 (2026-07-21)

### Bug Fixes

- Don't let proxy-status enrichment crash the connections listing
  ([#102](https://github.com/yiminspace/quarry/pull/102),
  [`b8bf9fd`](https://github.com/yiminspace/quarry/commit/b8bf9fd983609afd3d989d647b48a8f846473ed6))

- Make tunnel state and proxy badges reflect real facts, not per-process guesses
  ([#102](https://github.com/yiminspace/quarry/pull/102),
  [`b8bf9fd`](https://github.com/yiminspace/quarry/commit/b8bf9fd983609afd3d989d647b48a8f846473ed6))

- Scope cross-process tunnel registry entries by pid
  ([#102](https://github.com/yiminspace/quarry/pull/102),
  [`b8bf9fd`](https://github.com/yiminspace/quarry/commit/b8bf9fd983609afd3d989d647b48a8f846473ed6))

### Features

- Proxy-effect observability across CLI, status, and GUI
  ([#102](https://github.com/yiminspace/quarry/pull/102),
  [`b8bf9fd`](https://github.com/yiminspace/quarry/commit/b8bf9fd983609afd3d989d647b48a8f846473ed6))


## v0.14.1 (2026-07-21)

### Bug Fixes

- 修复浏览器 e2e 用例里过期的缓存属性引用 ([#100](https://github.com/yiminspace/quarry/pull/100),
  [`a55b609`](https://github.com/yiminspace/quarry/commit/a55b60968f7bd1d651a14a9e9f6e5406a0cd51d8))

### Refactoring

- 查询元数据缓存下沉到 core，CLI/MCP 与 GUI 共用 ([#100](https://github.com/yiminspace/quarry/pull/100),
  [`a55b609`](https://github.com/yiminspace/quarry/commit/a55b60968f7bd1d651a14a9e9f6e5406a0cd51d8))


## v0.14.0 (2026-07-21)

### Bug Fixes

- Address PR #98 review findings on config.toml preservation and per-workspace proxy resolution
  ([#98](https://github.com/yiminspace/quarry/pull/98),
  [`6296af4`](https://github.com/yiminspace/quarry/commit/6296af4613bd29e9063dc92ec67bc23ffea066e2))

- SSH tunnels can route through the system proxy, per workspace
  ([#98](https://github.com/yiminspace/quarry/pull/98),
  [`6296af4`](https://github.com/yiminspace/quarry/commit/6296af4613bd29e9063dc92ec67bc23ffea066e2))

### Features

- SSH tunnels can route through the system proxy, per workspace
  ([#98](https://github.com/yiminspace/quarry/pull/98),
  [`6296af4`](https://github.com/yiminspace/quarry/commit/6296af4613bd29e9063dc92ec67bc23ffea066e2))


## v0.13.0 (2026-07-21)

### Bug Fixes

- Avoid TypeError flake in lang-switch reload wait
  ([#95](https://github.com/yiminspace/quarry/pull/95),
  [`1bb32ff`](https://github.com/yiminspace/quarry/commit/1bb32ff9331bc97bb1d1cefc672fc1759665e2d8))

- Honor URL connect_timeout override, add MySQL server-side execution cap, validate timeout>0
  ([#95](https://github.com/yiminspace/quarry/pull/95),
  [`1bb32ff`](https://github.com/yiminspace/quarry/commit/1bb32ff9331bc97bb1d1cefc672fc1759665e2d8))

### Features

- Configurable query timeout, connect/execute split, PG backstop
  ([#95](https://github.com/yiminspace/quarry/pull/95),
  [`1bb32ff`](https://github.com/yiminspace/quarry/commit/1bb32ff9331bc97bb1d1cefc672fc1759665e2d8))

- Configurable query timeout, connect/execute split, PG statement_timeout backstop
  ([#95](https://github.com/yiminspace/quarry/pull/95),
  [`1bb32ff`](https://github.com/yiminspace/quarry/commit/1bb32ff9331bc97bb1d1cefc672fc1759665e2d8))


## v0.12.0 (2026-07-19)

### Bug Fixes

- **gui**: 隔离顶栏 VoyageToolbar 与查询工具条共用的 .vg-toolbar chrome
  ([#93](https://github.com/yiminspace/quarry/pull/93),
  [`fe14288`](https://github.com/yiminspace/quarry/commit/fe14288004acf804c04380455e8dd17a11837837))

### Features

- **gui**: 顶栏改用 VoyageToolbar 组合组件，升级 voyage 到 0.8.0
  ([#93](https://github.com/yiminspace/quarry/pull/93),
  [`fe14288`](https://github.com/yiminspace/quarry/commit/fe14288004acf804c04380455e8dd17a11837837))


## v0.11.0 (2026-07-19)

### Features

- **gui**: 升级 @yiminlab/voyage 到 0.7.0，采用顶栏控件统一盒子规格
  ([#91](https://github.com/yiminspace/quarry/pull/91),
  [`e8df624`](https://github.com/yiminspace/quarry/commit/e8df624b05786e51140693bdb14580454ee264d3))


## v0.10.0 (2026-07-19)

### Features

- **gui**: 语言切换按钮改用 voyage VoyageLangSwitcher 组件
  ([#89](https://github.com/yiminspace/quarry/pull/89),
  [`a44c0c3`](https://github.com/yiminspace/quarry/commit/a44c0c3be754eee7b8535385b4b1c47443944ec1))


## v0.9.1 (2026-07-18)

### Bug Fixes

- **build**: Ship CHANGELOG.md in standard wheels only, not editable installs
  ([`a69a4fe`](https://github.com/Wangggym/quarry/commit/a69a4fe76240a18c9c8788d27b9a90896f3b3a66))


## v0.9.0 (2026-07-16)

### Chores

- **deps**: Lock @yiminlab/voyage 0.4.0 (npm 已发布)
  ([`6fa67a8`](https://github.com/Wangggym/quarry/commit/6fa67a8f47fa25acb65dedfde5959758679795c8))

- **gui**: Web_dist 随 voyage 主题色板换装重建 (8 套开源经典配色)
  ([`c722b6c`](https://github.com/Wangggym/quarry/commit/c722b6c895f07104ba15ab6094c8a4b8f8ebfa19))

- **gui**: Web_dist 随 voyage 间距/字体修正重建
  ([`3d600c1`](https://github.com/Wangggym/quarry/commit/3d600c12b25796fae4d56334f1fdf0e76a9b6504))

### Features

- **gui**: 主题切换器接入 voyage 0.4.0 策展预设 + Tabler 图标插槽
  ([`c43bfd6`](https://github.com/Wangggym/quarry/commit/c43bfd659f9c984dad805438e488dc1e10da51d9))

- **gui**: 主题切换器接入 voyage locale — 面板文案随 中/EN 切换
  ([`27782ec`](https://github.com/Wangggym/quarry/commit/27782ecead64b88e7e959cc5a65ff62cf8339e19))


## v0.8.0 (2026-07-16)

### Bug Fixes

- **test**: Test_gui_visual.py 里漏改的 dataset.theme -> dataset.mode
  ([`6dbe55c`](https://github.com/Wangggym/quarry/commit/6dbe55c652c22588ef9e513b24d8248c487e807e))

### Features

- **gui**: 接入 @yiminlab/voyage 样式系统，视觉基本不变 + 主题切换器
  ([`067528d`](https://github.com/Wangggym/quarry/commit/067528d94702ca78c8ef992ea67615ff5f43fcc8))


## v0.7.0 (2026-07-16)

### Bug Fixes

- **gui**: What's New 面板按版本区间过滤 changelog 条目 ([#84](https://github.com/Wangggym/quarry/pull/84),
  [`517e7a7`](https://github.com/Wangggym/quarry/commit/517e7a70baa379d148c5d54a61d2429c33bf8218))

### Features

- **gui**: What's New 面板——随包发布 CHANGELOG 并在升级后展示版本间变更
  ([#84](https://github.com/Wangggym/quarry/pull/84),
  [`517e7a7`](https://github.com/Wangggym/quarry/commit/517e7a70baa379d148c5d54a61d2429c33bf8218))


## v0.6.0 (2026-07-15)

### Bug Fixes

- **gui**: /api/update 基于当前版本重新计算是否有更新 ([#83](https://github.com/Wangggym/quarry/pull/83),
  [`343d4be`](https://github.com/Wangggym/quarry/commit/343d4bed264cdd439e3262679a9c7642a2692594))

### Features

- **gui**: PyPI 新版本检测与 Header 升级提示 badge ([#83](https://github.com/Wangggym/quarry/pull/83),
  [`343d4be`](https://github.com/Wangggym/quarry/commit/343d4bed264cdd439e3262679a9c7642a2692594))


## v0.5.1 (2026-07-15)

### Bug Fixes

- **release**: __version__ 常量纳入 semantic-release 同步
  ([`89d330b`](https://github.com/Wangggym/quarry/commit/89d330bb2aaf25640d678145e412217259f95ee6))


## v0.5.0 (2026-07-15)

### Features

- **gui**: /api/events 事件通道——workspace 文件变更实时刷新与升级后刷新提示
  ([`67561ed`](https://github.com/Wangggym/quarry/commit/67561edda258f9e4a39ca3a99e4966ba21bdac18))


## v0.4.0 (2026-07-14)

### Bug Fixes

- 端口/回环校验覆盖 Neptune 裸端点 ([#77](https://github.com/Wangggym/quarry/pull/77),
  [`668aadf`](https://github.com/Wangggym/quarry/commit/668aadf9551434247a17cbc687acc468e9cd9c59))

### Features

- Connections add/set 增加本地误配与端口冲突检测 ([#77](https://github.com/Wangggym/quarry/pull/77),
  [`668aadf`](https://github.com/Wangggym/quarry/commit/668aadf9551434247a17cbc687acc468e9cd9c59))


## v0.3.1 (2026-07-14)

### Bug Fixes

- Neptune 回环端点自动跳过 TLS 主机名校验
  ([`3db7486`](https://github.com/Wangggym/quarry/commit/3db7486f81bd55c58d45c14d4f309bde0203cbc4))


## v0.3.0 (2026-07-13)

### Bug Fixes

- 历史召回后再覆盖不再丢失原手写草稿 (#48 评审) ([#57](https://github.com/Wangggym/quarry/pull/57),
  [`9192497`](https://github.com/Wangggym/quarry/commit/91924974049345d6b51eda2d48cf234a23d9db70))

- 避免首次 semantic-release 生成重复 0.3.0 章节 ([#74](https://github.com/Wangggym/quarry/pull/74),
  [`12f302b`](https://github.com/Wangggym/quarry/commit/12f302b753ce97d8a22bda56673a305762ed72ac))

- **gui**: Schema browser table names + stale response guard
  ([#38](https://github.com/Wangggym/quarry/pull/38),
  [`710631f`](https://github.com/Wangggym/quarry/commit/710631f72ffd2f0aad626cd2e73cfa7eae27a768))

- **gui**: Workspace manager keeps explicit --workspace pin, unbinds stale active connection
  ([#40](https://github.com/Wangggym/quarry/pull/40),
  [`f193d6e`](https://github.com/Wangggym/quarry/commit/f193d6ee5eab945196838f07cbb8553aab7558fb))

- **gui**: 修复多个 GUI UX 缺陷，建立可穷举的功能测试矩阵
  ([`98b959e`](https://github.com/Wangggym/quarry/commit/98b959e319493d6246629c9e3ae9bfb0e86597b2))

- **local**: Survive the postgres image's init-phase server restart
  ([`aafdb02`](https://github.com/Wangggym/quarry/commit/aafdb025341136d6f6005ec894ec0cad8f2a5322))

- **local**: 修复本地容器生命周期与连接注册中的一批健壮性问题
  ([`ebfaaa6`](https://github.com/Wangggym/quarry/commit/ebfaaa6624b7e3479f34e7cef2c8b8632300e8a4))

- **local**: 注册本地连接时保留其他连接的非字符串字段并让精确 key 优先
  ([`33edd28`](https://github.com/Wangggym/quarry/commit/33edd28c10b1ea15fb3ae7f27661f5590e2a9759))

- **local sync**: Auto-discover a new-enough pg_dump; survive \restrict dumps
  ([`c4a54a0`](https://github.com/Wangggym/quarry/commit/c4a54a0ca63f4237eb519adfcad98af1c3bbc3a4))

- **local sync**: Reset database-level objects and derive pg_dump from QUARRY_PSQL
  ([`46c2e22`](https://github.com/Wangggym/quarry/commit/46c2e22083f5b0b013e765deba9f61e75f505184))

- **local sync**: Wipe all user schemas before applying pg_dump
  ([`7040b0c`](https://github.com/Wangggym/quarry/commit/7040b0c008f62389c7105fd3ee01c99ce81e627d))

- **packaging**: Sdist 排除 web/node_modules ([#35](https://github.com/Wangggym/quarry/pull/35),
  [`a6e18f0`](https://github.com/Wangggym/quarry/commit/a6e18f0f99a8f9f8cf60249a202ae03bb3824bf6))

- **react grid**: Restore limit-5 table preview, Escape closes modal, cover missed interactions
  ([#56](https://github.com/Wangggym/quarry/pull/56),
  [`65ccdb9`](https://github.com/Wangggym/quarry/commit/65ccdb95d2b8947ce82cfded37b8016673f8b1cb))

### Continuous Integration

- Remove duplicated 3.12 test job, cache pip/Playwright, skip preinstalled clients
  ([`45025fa`](https://github.com/Wangggym/quarry/commit/45025fa807dd709bfedbca02a549dd60a8eef1fb))

- 引入 Conventional Commits 校验与 semantic-release 自动发版
  ([#74](https://github.com/Wangggym/quarry/pull/74),
  [`12f302b`](https://github.com/Wangggym/quarry/commit/12f302b753ce97d8a22bda56673a305762ed72ac))

- 引入 Conventional Commits 校验与自动发版流水线 ([#74](https://github.com/Wangggym/quarry/pull/74),
  [`12f302b`](https://github.com/Wangggym/quarry/commit/12f302b753ce97d8a22bda56673a305762ed72ac))

### Documentation

- Add coverage/test badges + Testing section to READMEs
  ([`0777d87`](https://github.com/Wangggym/quarry/commit/0777d874bb3ea78af5a5b4b090a694ab9e88ccdd))

- Record repo-evolve parity E2E verification in CHANGELOG
  ([#32](https://github.com/Wangggym/quarry/pull/32),
  [`795a9ec`](https://github.com/Wangggym/quarry/commit/795a9ecac12b39e7a2dc52800d369d16c1ffb79c))

- 诚实化功能矩阵范围, 连接隔离长尾转 #18 ([#19](https://github.com/Wangggym/quarry/pull/19),
  [`f4a7eac`](https://github.com/Wangggym/quarry/commit/f4a7eac2450b944968c8b99b6184af087e37d508))

### Features

- React SQL 编辑器接管高亮 + 自动补全 + 历史 ([#57](https://github.com/Wangggym/quarry/pull/57),
  [`9192497`](https://github.com/Wangggym/quarry/commit/91924974049345d6b51eda2d48cf234a23d9db70))

- React 结果网格接管 SQL 执行与只读展示 ([#56](https://github.com/Wangggym/quarry/pull/56),
  [`65ccdb9`](https://github.com/Wangggym/quarry/commit/65ccdb95d2b8947ce82cfded37b8016673f8b1cb))

- **cli**: Qy local up/down/status 本地容器生命周期管理
  ([`898ceda`](https://github.com/Wangggym/quarry/commit/898cedaa4a6f014fc1bf0bce59af6018e35401f9))

- **gui**: Connection-info panel — resolved config + live reachability probe
  ([`e13d19a`](https://github.com/Wangggym/quarry/commit/e13d19a0bba6a32670c2689a0a7e4729d0103bea))

- **gui**: React+TS 脚手架，/app 占位页入 wheel ([#35](https://github.com/Wangggym/quarry/pull/35),
  [`a6e18f0`](https://github.com/Wangggym/quarry/commit/a6e18f0f99a8f9f8cf60249a202ae03bb3824bf6))

- **gui**: Table structure browser in the React shell
  ([#38](https://github.com/Wangggym/quarry/pull/38),
  [`710631f`](https://github.com/Wangggym/quarry/commit/710631f72ffd2f0aad626cd2e73cfa7eae27a768))

- **gui**: Url copy/reveal + create-local-env and sync buttons in conn-info
  ([`4f8234b`](https://github.com/Wangggym/quarry/commit/4f8234b574838c48a46c7fa10db6e664bf9da8fb))

- **gui**: Workspace manager — add/remove config.toml workspaces from the header
  ([#40](https://github.com/Wangggym/quarry/pull/40),
  [`f193d6e`](https://github.com/Wangggym/quarry/commit/f193d6ee5eab945196838f07cbb8553aab7558fb))

- **gui**: 每个标签页的查询结果刷新后可恢复 ([#19](https://github.com/Wangggym/quarry/pull/19),
  [`f4a7eac`](https://github.com/Wangggym/quarry/commit/f4a7eac2450b944968c8b99b6184af087e37d508))

### Refactoring

- **local sync**: Staging db + rename swap instead of in-place wipe
  ([`ae8ea7f`](https://github.com/Wangggym/quarry/commit/ae8ea7f237d619f863c7b18cea15830b9e817528))

### Testing

- Cover blur-commit rename and per-tab result following drag reorder
  ([`8d3146b`](https://github.com/Wangggym/quarry/commit/8d3146b06d95f153921e7e0a2420400017b28703))

- Fix flaky schema-browser table-switch browser test
  ([`1b8ed63`](https://github.com/Wangggym/quarry/commit/1b8ed6375729564da726fb14d60a4bfbe84d424d))

- Fix port race flakiness in local_sync_docker suite
  ([#37](https://github.com/Wangggym/quarry/pull/37),
  [`00bee18`](https://github.com/Wangggym/quarry/commit/00bee184ff1c1fecae0f1d7325cd4e8797a51ee9))

- **gui**: Assert the URL password SLOT is masked, not substring absence
  ([`dfbd793`](https://github.com/Wangggym/quarry/commit/dfbd793a5f747c956e06c56b143de58f6696b97e))

- **gui**: GuiClient 禁用系统 HTTP 代理避免 Host 头误路由
  ([`411e1f4`](https://github.com/Wangggym/quarry/commit/411e1f4925edeed9ce84248ddb2ca272a22d7535))


### GUI — the React frontend takes over (visual parity with the classic GUI)

- **`/app` (the React GUI) is now the only frontend and the default landing
  page** (#65): `/` and `/index.html` redirect to `/app/`, `qy gui` opens the
  browser there, and `gui.py` is backend-only from here on (`http.server` +
  `/api/*` + serving the built `web/` app).
- **Pixel-level visual parity with the classic GUI**: the React app renders
  the same DOM (ids/classes), the same "Slate & Copper" palette (dark default
  + explicit light toggle), the same 14px/mono density, the same Tabler
  icons (self-hosted — no CDN, closes #14), and the same i18n strings. The
  design tokens are pinned in both themes by `getComputedStyle` assertions in
  CI (`tests/test_gui_visual.py`), not by eyeballing.
- **Full feature parity, proven by the classic GUI's own test suite**: all 93
  feature-matrix rows (sidebar tree/health/env pills, tabs with per-tab
  result isolation, SQL editor with highlight/autocomplete/history, toolbar
  incl. EXPLAIN/exports/max-rows, data grid with sort/keyboard nav/pagination,
  conn-info and workspace-manager modals, saved queries, redis key tree, …)
  pass unchanged against the React app — including every safety invariant
  (prod never auto-runs, drafts never silently lost, stale responses never
  repaint, latest-wins per tab).
- **State carries over seamlessly**: the React app reads and writes the same
  `localStorage` keys and value formats as the classic GUI (tabs, per-tab
  results, history, theme, language, layout sizes, collapsed groups), so an
  existing user keeps their session on first load — no migration step.
- **Table-structure browser** (#11) reshaped to fit the classic layout:
  double-click a sidebar table name to open a column-name + type modal
  without running anything.

### GUI — sidebar

- **`local` always sorts first among a db's env tags** (#44): sidebar pills
  and the header env switcher now always show `local` leftmost, regardless of
  connection-registration order. The default-selected env stays `dev` when
  present, and now falls back to `local` (instead of whichever env was
  registered first, e.g. `prod`) when there's no `dev`.
- **Table-click preview defaults to `limit 5`** (#44): clicking a table in the
  sidebar now generates and runs `select * from <table> limit 5` instead of
  `limit 100`.

### GUI — query deep links

- **Shareable query links** (#69): the toolbar now has **Copy query link**,
  which captures the active tab's `db` / `env` / `sql` into a URL-safe link.
  Opening that link reuses an identical tab if one already exists (otherwise
  creates a new tab), restores the connection + SQL, and auto-runs once so the
  result grid is immediately populated. Invalid or unavailable `db`/`env`
  targets are handled explicitly with a toast and no silent auto-run.

### Packaging

- **Editable installs point at exact source paths** (#42): `pip install -e .`
  now maps each package individually instead of appending a directory to
  `sys.path`. If the installed-from checkout (e.g. a `git worktree`) is later
  removed, `qy`/`quarry` now fail with a `FileNotFoundError` naming the
  missing path instead of an opaque `ModuleNotFoundError: No module named
  'quarry.cli'` — see CONTRIBUTING.md for the reinstall command.

### GUI — tabs

- **Auto tab titles now show the table you're querying, not just the
  connection** (#70): tabs on the same connection used to all display the
  same `db@env` label, making it easy to run a query in the wrong tab. The
  title now derives from the current SQL's main table (`FROM`/`UPDATE`/
  `INSERT INTO`/`DELETE FROM`, including quoted/backtick-quoted mixed-case
  names) and updates live as you edit; it only falls back to `db@env` when
  the tab has no SQL. Manual tab renames are unaffected.
- **Rename, drag-reorder, middle-click close, keyboard shortcut** (#16):
  double-click a tab to rename it (Enter/blur commits, Escape cancels, an
  empty name reverts to the automatic title); drag a tab to reorder it;
  middle-click a tab to close it; `Cmd/Ctrl+Shift+W` closes the active tab.
  All four respect the existing "at least one tab stays open" rule.

### GUI — workspace manager

- **Manage workspaces from the header** (#15): a new gear button next to the
  workspace label opens a modal listing every workspace registered in
  `config.toml` (flagging a missing directory or a directory with no
  `connections.toml`), lets you add another one, and remove one (confirm-gated,
  only removes the registration — files are untouched). Changes take effect
  immediately, same as `qy workspace add|remove`, without dropping an explicit
  `--workspace` session. Removing the workspace behind the currently active
  connection unbinds it right away instead of waiting for the next tab switch.
  That list used to be display-only.

### GUI — grid pagination

- **Real pagination ("load more")**: when a query result hits the max-rows cap,
  the grid now offers a "Load more" button that fetches the next page (same
  SQL, growing offset) and appends it, instead of only ever showing the first
  page. Available for postgres/mysql queries run from the editor. If the grid
  is already sorted, loading more re-sorts the combined rows so the active
  sort stays correct across pages.

### GUI — React scaffold (strangler-fig step 1)

- New `web/` package (Vite + React + TypeScript). `npm run build` writes static
  assets to `src/quarry/web_dist/`, included in the wheel so `pip install` stays
  zero-Node for end users.
- Placeholder UI at `/app` shows **Quarry** and the package version (via
  `/api/version`). The existing embedded-JS GUI at `/` is unchanged.
- **Table structure browser** (#11): `/app` now lets you pick a connection,
  browse its tables, and see each column's name and type. `/api/columns`
  gained a `types` field (column name → data type) alongside its existing
  `columns` name list.

### Local dev containers (`qy local`)

- **`qy local up [--engine postgres|redis|all]`** starts a local Postgres/Redis
  in a docker container on a fixed port (postgres `5433`, redis `6380`) with a
  named data volume, so a service running locally can talk only to `localhost`
  instead of a shared remote database. It's idempotent — repeat runs never spawn
  a duplicate container. The image tag is overridable with `--image`.
- **`qy local up <key>`** additionally auto-registers an `env=local` connection
  in `connections.toml` (one logical database per key inside the shared Postgres
  container) and joins it to the existing env-set, so `qy connections` shows the
  new `local` environment. Re-running never overwrites a local connection you've
  hand-edited.
- **`qy local down`** stops the container but keeps the data volume; **`down
  --purge`** also deletes the volume so the next `up` starts from an empty
  database.
- **`qy local status`** shows whether each container is running, its port, and
  its image, and points to `qy local up` when nothing is running.
- **`qy local sync <key> [--from dev]`** copies the source environment's Postgres
  schema into the matching `env=local` connection via `pg_dump --schema-only`
  (no migration tool). The dump is applied to a fresh `<db>__staging` database
  and swapped in with two renames, so the live local database is never mutated
  in place: a mid-sync failure leaves it untouched, the service-facing database
  name stays stable, and the previous copy is kept as `<db>__prev` until the
  next sync. Connections held on the local database are terminated during the
  swap so a dev server holding its pool cannot block the sync. Refuses to run
  unless the resolved target is `env=local` **and** points at a loopback host
  without an SSH tunnel (no `--force`).
- `qy local up <key>` now registers local **redis** connections with the same
  database index as the env-set's remote member (previously always `/0`), so a
  service's connection string ports over with only host:port changed.
- Readable errors when docker is missing, the daemon is down, or the port is
  already in use — no raw docker stack traces.

### GUI

- **Connection-info panel**: an ⓘ button next to the env switcher opens a modal
  with the *resolved* configuration for the current connection — key, engine,
  env, host, port, database, SSH tunnel, and which `connections.toml` the entry
  came from (password always masked) — plus a live reachability probe that
  shows the raw error when the connection fails. Answers "it won't connect and
  I don't know why" without leaving the GUI. (`GET /api/conninfo`)
- **Conn-info url row**: an eye toggles the password mask off/on
  (`?reveal=1` — localhost-only, same value as your own connections.toml), and
  a copy button puts the real URL on the clipboard for pasting into a service
  env file.
- **Local envs from the GUI**: the conn-info modal offers **Create local env**
  (`POST /api/local/up` — starts the shared docker container and registers the
  `env=local` connection) when the env-set has none, and **Sync schema from
  dev** (`POST /api/local/sync`, confirm-gated) when you're on the local env —
  same staging-swap + safety gates as the CLI. Creating a postgres local env
  **auto-runs the first schema sync** from the remote sibling (an empty shell
  is never what you wanted); a sync failure is reported without undoing the
  successful `up`.
- **`pg_dump` auto-discovery**: sync now scans every pg_dump on the machine
  (QUARRY_PSQL bin dir → PATH → Homebrew kegs → `/usr/lib/postgresql/*`) and
  picks one at least as new as the source server, so having `postgresql@17`
  installed is enough — no PATH or `QUARRY_PSQL` surgery. The readable
  version error remains for machines that genuinely lack a new-enough client.

### GUI UX fixes

- **Hand-written SQL is never silently lost.** Clicking a table / redis key /
  saved query / history entry used to overwrite the editor; the draft is now
  pushed to History first, and Cmd/Ctrl+↓ at the end of a history walk restores
  the in-flight draft instead of clearing the editor.
- **Switching the env pill to `prod` no longer auto-runs the current SQL** — it
  shows a notice and waits for an explicit Run (non-prod env switches still
  re-run, as before).
- **Overlapping queries are latest-wins**: a slow, older response can no longer
  overwrite the result of a newer run/inspect after it painted.
- **Column sort is numeric-aware** (`'10'` sorts after `'9'` in text columns), a
  third click on the same column restores the original row order, and the sort
  arrow resets when a new result arrives.
- **Max-rows selector** in the toolbar (100/500/2000/5000, persisted) — results
  were previously hard-capped at 500 with no way to raise or lower the cap.
- **CSV export** now writes `quarry-<db>.csv` with a UTF-8 BOM (Excel no longer
  garbles non-ASCII); JSON export is named `quarry-<db>.json`.
- **Health dots carry the failure reason** as a row tooltip (was: a red dot with
  no explanation), and clicking an unreachable connection shows the error in the
  table panel.
- **"Copied" toast is honest** — it only shows after the clipboard write
  succeeds; failures show a copy-failed toast.
- **Network failures show a readable error** (was: `{}` from a stringified
  TypeError when the server was unreachable).
- **Generated table queries quote mixed-case/reserved identifiers** (postgres
  `"Name"`, mysql backticks) — clicking such a table no longer errors.
- **Redis key list cap is visible**: when the 400-key cap is hit the panel says
  "showing only the first N keys" instead of silently truncating.
- Escape now closes the **topmost** modal (was: the oldest); the table-filter
  text survives the background (SWR) list refresh; icon-only header controls
  carry `aria-label`s.
- **Editor tabs got real isolation.** Switching tabs now switches the result
  grid/status too, and CSV/JSON exports always contain the *active* tab's data
  (previously the grid kept showing another tab's result, and an export could
  write tab A's rows under tab B's filename). Closing a tab pushes its SQL to
  History — the "never silently lose SQL" invariant now covers all five editor
  overwrite sites. A tab whose connection no longer exists unbinds cleanly
  instead of silently rebinding to whatever was selected before.
- **Every tab's result now survives a reload**, not just the active tab's:
  switching to a background tab after reopening the page shows its last grid
  again (still isolated — a tab never shows another tab's data).
- **Saved queries persist under their own connection.** A saved query runs on
  the connection it declares (`@db`), not the tab's current one; when launched
  from a tab bound to a different connection its result is now tagged and
  persisted under the producing connection (and the tab re-pointed to it), so a
  reload restores it under the right connection instead of mislabeling it.
  (Consistency when the saved query's `@db` is a logical env-set is tracked
  separately in #18.)
- **Table list**: the currently open table is highlighted (cleared when custom
  SQL runs); a refresh button re-fetches the list on demand (tables and redis
  keys); Alt+click inserts the generated SQL without running it; lists that hit
  the 5000-table cap say so instead of silently truncating.

### Testing

- `TESTING.md` documents the **three-audit method** (existence / capability /
  shared-state) that keeps the feature matrix honest, plus a Design-gaps table
  for capabilities that are known-missing on purpose.
- New browser-e2e module `tests/test_gui_browser_features.py` (53 tests):
  env-set pills + prod guard, draft preservation, request-race, numeric sort,
  redis key tree + cap notice (auto-spawns an ephemeral `redis-server` when none
  is running), health-dot flows against a dead connection, SWR refresh, layout
  drags, export content (BOM/escaping), clipboard paths, persistence across
  reloads, grid keyboard nav, autocomplete columns, and more. Console errors are
  an autouse invariant in that module.
- Browser fixtures now stub the icon-font CDN with an empty local response, so
  the whole browser suite is hermetic (no external network) — this removed an
  intermittent `networkidle` timeout and cut the full-suite wall time in half.
- `TESTING.md` now carries a **GUI feature matrix** (66 rows) mapping every
  frontend feature point to its covering test; `AGENTS.md` documents the rule
  that keeps it current.

### Security / correctness fixes

- **Read-only rail could be bypassed** — now closed. `EXPLAIN SELECT 1; DROP TABLE t`
  previously passed the read-only check and executed the `DROP`; data-modifying CTEs
  (`WITH d AS (DELETE … RETURNING *) SELECT …`) slipped through the same way. The guard
  now rejects multiple statements and data-modifying CTEs across the CLI, GUI, and MCP
  faces (backed by a comment/string/dollar-quote-aware SQL skeleton).
- Auto-`LIMIT` no longer corrupts `FETCH FIRST … ROWS ONLY` or `… FOR UPDATE` queries,
  and `LIMIT` inside a string literal is no longer mistaken for a real limit.
- `qy run --limit/--full` no longer produces invalid SQL on nested/subquery `LIMIT`s
  (depth-aware, outer-only rewrite).
- `qy --max-rows N` now returns exactly N rows (was N+1).
- Redis read-only guard now blocks write-via-subclause commands (`SORT … STORE`,
  `GETEX`, `BITFIELD … SET`, `*STORE`, blocking pops, admin commands).
- `serialize_row` no longer crashes on a bare `date` (MySQL `DATE` / Neptune dates);
  `bytearray`/`memoryview` (pymysql BLOB/BINARY) now decode like `bytes`.
- Named-parameter substitution is single-pass — a value containing `:name` is no longer
  re-substituted.

### Fixed

- MySQL / Neptune driver failures now surface as clean errors with correct exit codes
  instead of raw tracebacks (CLI) or `-32603` protocol crashes (MCP).
- MCP `list_tables` no longer returns empty on MySQL 8 (case-insensitive `table_name`);
  malformed tool calls return a tool `isError` result, not a protocol crash.
- GUI: `/api/inspect` rejects non-Redis connections; missing/invalid request fields
  return clean `400`s instead of tracebacks; the health cache honors a 120s TTL, so a
  transient failure no longer pins a connection red forever; `_reclaim_port` never
  SIGTERMs a foreign process whose command merely ends in `gui`.
- `qy save` / `qy validate` resolve a logical env-set db like `qy run` does; `qy validate`
  refuses to validate a non-read-only saved query (validation stays side-effect-free);
  `qy exec/save --file <missing>` and MySQL/Neptune connection errors give clean messages.

### Changed (behavior — note when upgrading)

- The read-only rail is **stricter**: multiple statements and data-modifying CTEs are now
  rejected without `--write`. Scripts that relied on the previous (unsafe) pass-through
  will be blocked (exit `8`) — pass `--write` if the writes are intended.

### Added — tests & tooling

- Layered test suite (unit / integration / e2e / browser): **723 tests**, up from 69.
- Playwright headless-browser GUI e2e covering the real frontend (grid, run, EXPLAIN,
  export, tabs, theme/language, saved-query params, autocomplete, console-cleanliness).
- Coverage gate: unit + integration ≥ 95% (currently 99.6%) via `make cov`.
- `make test` (layered summary) / `make test-browser`; CI `coverage` + `browser` jobs
  (+ a Redis service); `TESTING.md` documents the architecture. See also `scripts/`.

### Internal

- repo-evolve parity E2E verification (2026-07-08): CHANGELOG-only change to
  validate the flywheel end-to-end in place of quarry-evolve parity phase B.

## [0.2.2] — 2026-07-02

- Fix MCP Registry name casing (`io.github.Wangggym/quarry`)

## [0.2.1] — 2026-07-02

- MCP Registry listing (`io.github.wangggym/quarry`) — README ownership marker
- Promo site polish: bilingual pages, hero showcase, nav fixes

## [0.2.0] — 2026-07-02

### Added
- **MCP face** (`qy mcp`): a Model Context Protocol server over stdio, pure
  stdlib. Six tools: `list_connections`, `list_tables`, `describe_table`,
  `exec_sql`, `list_saved_queries`, `run_saved_query`. Graduated write policy:
  server `--write` flag + per-call `write: true` + `confirm_prod: true` for prod.
- GUI: multi-tab editor (per-tab SQL + connection, persisted across restarts)
- GUI: EXPLAIN button (plan modal for postgres, grid for tabular plans)
- GUI: searchable query history with connection name and relative time
- GUI: grid keyboard navigation (arrow keys + Enter to inspect)
- GUI: collapsible JSON tree in the cell inspector

### Fixed
- SQL errors were silently swallowed into empty results (psql now runs with
  `ON_ERROR_STOP`; failed statements correctly exit with code 3)
- `EXPLAIN` / `SHOW` statements now work through `run_query` (previously broken
  by the JSON subquery wrapper) and are exempt from the auto-LIMIT injection

## [0.1.0] — 2026-07-02

First public release.

### Core
- Multi-engine query kernel (`quarry.core`): PostgreSQL (via system `psql`),
  MySQL (optional `pymysql`), Redis (via `redis-cli`)
- Structured result contract: `{columns, rows, rowCount, truncated, elapsedMs, engine, sql}`
- Safety rails in the kernel: read-only by default (`--write` to allow),
  automatic `LIMIT 500` row cap, graduated prod confirmation
- Stable exit-code contract: `0` ok / `2` connection / `3` SQL / `8` safety block
- Workspace-as-code: `connections.toml` + named queries (`queries/**/*.sql`
  with `-- @meta` headers); multi-workspace aggregation via
  `~/.config/quarry/config.toml`
- Connection groups and env-sets (same logical db across dev/prod/…,
  `--env` switch, dev default)
- SSH tunnels via system `ssh` (`ssh_*` connection fields)

### CLI (`qy`)
- `connections` (list/add/set/remove/test), `exec`, `run`, `save`, `list`,
  `describe`, `schema`, `validate`, `fingerprint`, `audit`, `remove`, `edit`,
  `workspace` (list/add/remove), `gui`
- Output formats: table / json / ndjson / csv

### GUI (`qy gui`)
- Local zero-build web GUI, light/dark theme
- Grouped sidebar tree with environment switcher (prod highlighted red)
- SQL editor with syntax highlighting and local autocomplete (keywords/tables/columns)
- Data grid: type-aware coloring, sorting, column resize, cell/row inspection
- CSV/JSON export, query history, saved-query library
- TYPE-aware Redis key browsing
- State persists across restarts (selected connection, SQL, results cache)
