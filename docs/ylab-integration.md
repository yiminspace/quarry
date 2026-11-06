# YiminLab hosted and local integration

YiminLab deploys the official Quarry image with its existing IAM verification
and resource ACLs. The same React workbench also ships inside the Rust `ylab` binary.

| Entry | Connections | Personal records |
| --- | --- | --- |
| YLab Console `/quarry/` | Authorized cloud connections | Account-scoped browser storage |
| `ylab db gui` and a federated `qy gui` | Quarry workspaces and registered cloud connections | Shared, configuration-scoped SQLite on this computer |

## Local workbench

Run `ylab auth login`, then load the catalog with `ylab db connections` or
`ylab db gui`. YLab registers the authorized catalog in the private Quarry
registry under `~/.config/quarry/federation/`. The snapshot contains resource
metadata and account scope. `qy connections`, `qy exec` and `qy gui` use the same
registry when they use the same Quarry configuration. If a pure local Quarry
window is already open when a source is first registered, run `qy gui` again
to open the protected workbench.

To verify which configured PostgreSQL routes reach the same database, run
`qy connections reconcile --project yiminlab`. The command compares
server-provided database and instance metadata through the existing authorized
routes. It does not require matching display names. Add `--apply` to save
the verified pairs that have no existing association. Routes that cannot be
verified or have multiple matches stay separate. Verification is on demand.
If an endpoint changes, remove any outdated link with `qy connections unlink`
and verify again.

Use an exact target ID when a short name is ambiguous. Run
`qy connections candidates` to review potential associations, then
`qy connections link <cloud-id> <local-id> ...` to confirm them. A link changes
the grouping in the directory. Each target keeps its own execution route and
permissions. For PostgreSQL, `pg_control_system().system_identifier` and the
current database's `pg_database.oid` can help verify a candidate when both
connections can read them. A physical copy can preserve those values, so
review the source and environment before linking.

The desktop workbench accepts a short-lived, one-use launch code and exchanges
it for a loopback browser session. YLab uses the [local provider protocol](database-providers.md).
Quarry calls the installed helper with a bounded database operation and resource
ID. YLab reads and refreshes its own credentials. The hosted gateway continues
to authorize every operation.

Local drafts and history stay available across cloud account changes. Cloud
records keep their original account and target scope. A disconnected target
remains unavailable for execution, and losing access never selects a same-name
local connection. Reconnecting refreshes the directory without replaying SQL.

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
