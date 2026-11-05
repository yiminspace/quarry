# YiminLab hosted and local integration

YiminLab deploys the official Quarry image with its existing IAM verification
and resource ACLs. The same React workbench also ships inside the Rust `ylab`
binary.

| Entry | Connections | Personal records |
| --- | --- | --- |
| YLab `/quarry/` or `ylab db open` | Authorized cloud connections | Drafts and SQL history in account-scoped browser storage |
| `ylab db connect` | Existing Quarry workspaces plus authorized cloud connections | Workbench state and execution history in local SQLite |

## Local workbench

`ylab db connect` serves the complete Quarry GUI on loopback. Workspace
configuration, environment switching, saved queries, tunnels and local database
tools use the normal Quarry service. Cloud environments have an explicit source
label. Their resource IDs remain distinct from local environment names.

The native host implements `quarry_cli::http::CloudBackend` and supplies it to
`quarry_cli::run_gui_from`. The desktop router accepts a short-lived, one-use
launch code from the URL fragment and exchanges it for a local browser session.
The fragment is cleared before the exchange. The host's cloud bearer remains in
the native process. YLab loads and refreshes credentials only when forwarding
cloud operations, using the account selected when the workbench started.

Users can start the local workbench before signing in. To add cloud access, run
`ylab auth login` and restart `ylab db connect`. Previously signed-in users can
restart offline with their local workspaces and personal SQLite. A cloud
connection always requires live server authorization. Reconnecting refreshes
the catalog without replaying SQL.

`--config` and `--workspace` select existing local configuration explicitly.
For upgrades, YLab can reuse a legacy account workspace when the standard
Quarry configuration is absent. Personal SQLite continues to use the existing
account namespace and document keys. Browser recovery protects edits when
local storage fails or a saved document has a revision conflict.

## Hosted boundary and rollout

Hosted endpoints use the existing `lab:db:read` permission and per-resource ACLs.
Cloud operations use stable resource IDs and a fixed YLab origin. The server
holds connection secrets and performs read-only execution. Browser storage,
local files and personal SQLite belong to their respective clients.

Release the new native client against the current hosted API before switching
the hosted GUI to browser-only storage. Older clients launch the old hosted
pairing flow and should be upgraded before that cutover. Existing database
connection files are selected for migration separately from the client upgrade.

Verify the built browser bundle and the native CLI against disposable local and
hosted databases. Cover localhost startup, independent target identities,
history and draft restoration, offline restart, account isolation and denial of
a cloud query after revocation. Preserve existing workspaces and volumes during
testing.

## Historical acceptance of the paired workbench (2026-11-04)

- Actual AuthKit sessions, database roles and scope intersection ran against
  disposable PostgreSQL/Redis. Its library suite passed 115 tests; four legacy
  ignored tests were not counted as execution evidence.
- The real-process harness passed eight grouped checks: IAM/resource ACL and
  read-only execution; offline embedded CLI; local pairing/shared SQLite/CAS;
  assistant inference and local conversation persistence; PostgreSQL cross-end
  structure import; restricted applications and refresh; remote revocation;
  cloud-outage local history and queries. Model replies used a deterministic
  loopback fixture, not a production model. No Google OAuth browser flow or
  production database access is claimed.
- The split PostgreSQL export/import test additionally verified source rows
  unchanged, no business rows in the local replacement, the prior database
  retained, and failed schema import leaving the live and prior databases intact.
- Platform native integration tests cover device identity stability, separate
  computers/accounts, origin restrictions, offline local authorization and
  request-scoped assistant admission/cancellation. Standalone compatibility and
  built-native browser regressions are recorded in TESTING.md.

Installed user binaries, registered workspaces, histories, containers and volumes
were not replaced by this acceptance run. Deployment/publishing remains separate
from local acceptance; the production promotion manifest is unchanged.

Final built-native GUI acceptance passed 19 platform browser cases, one visual
case covering both themes, and 70 standalone SQLite/assistant/draft/auto-run
regressions. Frontend build/lint and 102 unit tests passed. A real disposable
Redis fixture verified two sources use independent containers, volumes, ports
and data: stopping one source leaves the other running; restarting retains its
data. Rejected lifecycle operations issued no Docker command. A native Neptune
companion process test verified the embedded host launcher, health and owned stop.
