# YiminLab hosted and local integration

## Agreed product scope (2026-11-04)

Deliver one integrated workflow: the existing Quarry workbench in YiminLab
Console, IAM-authorized remote read-only database operations, native `ylab db`
capabilities embedded from Quarry Rust libraries, an explicitly paired local
companion, and PostgreSQL schema-only transfer from an authorized hosted resource
to a local database. Standalone Quarry remains available.

Personal drafts, tabs, SQL history, result caches, assistant messages, generated
SQL and restore checkpoints belong to a SQLite database on the user's computer.
They are separated by platform, IAM account and execution target. The companion
and CLI share that implementation. Hosted execution must not create a durable
personal history database. Results have a bounded cache budget. No shared SQL
library, platform personal-history persistence or cross-device synchronization
is included.

The GUI is usable when either connection is unavailable. Hosted and companion
connectivity are independent. Existing cached assets and recovery buffers allow
continued editing; unavailable capabilities show their state. Reconnection never
replays SQL, starts Docker, imports a schema or overwrites an unsaved draft.
First-time loading still requires fetching the application assets.

## Ownership and trust

- Quarry owns reusable execution, storage, HTTP contracts, workbench UI and local
  lifecycle. `ylab` embeds the Rust libraries, not a separately installed `qy`.
- Ylab owns Console identity, per-request IAM verification, resource registration
  and grants, CLI account/profile integration and release packaging.
- YiminLab owns AuthKit permissions, runtime secrets, private service networks and
  production deployment. Private configuration and local state never enter a
  public Quarry artifact.
- Hosted resources have stable IDs, project/database/environment identity,
  server-owned connection keys and an explicit principal/role ACL. Every target
  operation checks both live IAM permission/scope and resource access. Client
  paths/URLs cannot choose filesystem resources or database credentials.
- All query, metadata, cancellation and assistant operations bind to a specific
  identity and target. Cancellation cannot affect another user's execution.
- Hosted endpoints exclude connection credential reveal, arbitrary connection
  edits, local file access, Docker lifecycle and personal-history persistence.
- Pairing is explicit, origin/platform/account-bound, short-lived and revocable.
  The loopback companion exposes only declared operations. IAM login alone does
  not grant access to a computer. Pairing credentials never enter URL parameters.
- PostgreSQL structure export has separate permission. Local import preserves
  staging, recovery and prior-database behavior; database passwords and business
  rows are not transferred. Other engines do not claim schema-sync support.

## Permission contract

`lab:db:read` permits the hosted read-only workbench, subject to resource ACLs.
`lab:db:schema:export` additionally permits downloading PostgreSQL structure.
`lab:db:local` permits establishing the account-bound local companion pairing.
Assistant inference also requires `lab:ai:invoke`. Ordinary registration does
not grant these permissions; roles and resource ACLs must be assigned explicitly.

## Acceptance

Use disposable configuration, SQLite files and local engine fixtures. Preserve
installed Quarry, existing connection/query files, assistant histories, Docker
containers, volumes and custom ports. Required checks include denied cross-user
and cross-resource access, scoped credential restrictions, expired/revoked
pairing, origin rejection, independent disconnections, account-switch isolation,
SQLite persistence across browser/companion restarts, bounded result caching,
assistant generation/apply/restore without SQL execution, schema-only transfer
and failure recovery. Build the frontend before native browser verification;
update TESTING.md and run existence, capability and shared-state audits.

## Implemented transports

The host CLI embeds `quarry-cli::run_from` and supplies its own daemon dispatch
prefix; no `qy` subprocess is used. `quarry-platform` exports `hosted_router` and
`companion_router`, a host-provided per-operation `Authorizer`, a resource
registry and a compare-and-swap `PersonalStore`. Platform adapters deliberately
exclude shared saved queries and standalone secret/configuration management.
The independently distributed workbench stays in standalone mode unless its
hosted copy has a `quarry-mode=platform` meta marker.

Personal SQLite keys include the configured platform origin and IAM user ID.
Workbench state and bounded execution history use separate documents so a CLI
query does not overwrite an active GUI draft. History remains readable offline
from the same host CLI account. Browser recovery content is separately scoped to
that account and never silently reported as saved to SQLite while disconnected.

Initial pairing checks the live IAM identity, exact origin and one-use code.
After pairing, local-only operations use the expiring companion capability and
owner binding; they do not depend on cloud availability. Local revocation and
expiry stop them. Remote execution, model calls and schema retrieval always
check live IAM again. Thus revoking a cloud session stops cloud access; it does
not retroactively turn a separately granted local offline capability into a
network-dependent credential.

Companion-managed Docker containers, volumes, ports and runtime configuration
are isolated by platform, account and source resource. Lifecycle operations
resolve an owned Local resource before touching Docker; they never fall back to
the standalone Quarry engine containers or share Redis DB 0 across sources.

The companion obtains schema directly from its fixed authorized platform origin
using the current IAM credential and source resource ID. It rejects arbitrary
browser-supplied schema text. `LocalManager::import_schema` is a trusted-host API,
not a general untrusted SQL sandbox; PostgreSQL structures can include executable
functions/extensions. The companion must verify the actual authorized source,
not treat client text as a trusted dump.

## Local acceptance (2026-11-04)

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
