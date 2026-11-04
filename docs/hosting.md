# Integrate Quarry into your platform

[中文](hosting.zh-CN.md) · [Standalone installation](install.md) · [Rust architecture](rust-preview.md)

Quarry can run as an independent CLI, GUI and MCP server, or supply the database
workbench inside an existing platform. The platform owns accounts, permissions,
resource registration, secrets and deployment. Quarry supplies Rust execution
libraries, a read-only hosted API, the workbench UI and an explicitly paired
desktop companion.

The hosted and companion APIs described here are available from the current
source SDK, separately from the standalone 2.0.0 release. This is a library
integration, not a one-command hosted edition. The public
`quarry-platform` backend accepts a host-provided IAM adapter. The current
browser adapter follows Ylab's session contract, so another platform must adapt
that contract as well. Ylab's server executable and deployment configuration are
host-owned; they are not included in the public Quarry distribution.

## Choose the entry point

| Need | Entry point |
| --- | --- |
| A workbench on your own computer | Install `qy`, configure a workspace and run `qy gui` |
| Database access for an agent | Launch `qy mcp` over stdio |
| Execution inside an existing Rust application | Embed `quarry-service::QuarryService` |
| Quarry commands inside your own CLI | Embed `quarry-cli::run_from`; supply host daemon dispatch |
| A platform workbench with IAM and local personal records | Integrate `quarry-platform`, its hosted UI adapter and a companion in your CLI |

Use a pinned Quarry source revision. The embedding crates live in this repository;
building your host does not require an installed `qy` or a Python subprocess.
Build `web/` before compiling a binary that embeds the GUI. See the executable
[service example](../crates/quarry-service/examples/embedded_host.rs) and
[build instructions](rust-preview.md).

## 1. Register resources on the server

Create `QuarryService` with explicit configuration, workspace and state paths.
Keep the workspace and database credentials private to the server. Register each
exposed target with `HostedResource`: a stable ID, project, database, environment,
server-side connection key and explicit `readers` ACL. An ACL accepts principal
IDs or `role:<role-name>` entries; an empty ACL grants access to nobody. Mark
production connections explicitly with `production = true`.

The browser selects a registered resource ID. It cannot supply a workspace path,
connection URL or database password. Remote query execution stays read-only;
metadata, cancellation and schema export are also limited to authorized targets.

## 2. Connect your IAM

Implement the async `Authorizer` trait. On **every invocation**, verify the bearer
session is still valid, verify the requested permission and token scope, and
return a stable `Principal` with its current roles. Do not trust identity headers
from a browser or accept a decoded token without checking the session's validity.

The current adapter requests these permission names. Map them to your IAM policy
inside `Authorizer` if your platform uses different names.

| Permission | Capability |
| --- | --- |
| `lab:db:read` | Remote read-only queries, metadata and cancellation |
| `lab:db:schema:export` | PostgreSQL schema export; also requires read permission |
| `lab:db:local` | Initial pairing and cloud-dependent Local operations |
| `lab:ai:invoke` | Assistant model requests, in addition to target access |

An operation needs both IAM authorization and the resource ACL. Granting an
account a general database permission does not expose every registered resource.
Start with one account and an explicit resource allowlist.

Wire the service, your IAM implementation and registry into the public router:

```rust
use std::sync::Arc;
use axum::Router;
use quarry_platform::{Authorizer, HostedResource, HostedState, hosted_router};
use quarry_service::QuarryService;

fn database_api(
    service: Arc<QuarryService>,
    iam: Arc<dyn Authorizer>,
    resources: Vec<HostedResource>,
) -> quarry_platform::Result<Router> {
    let state = HostedState::new(service, iam, resources)?;
    Ok(hosted_router(state))
}
```

This function supplies API routes; your application still owns the listener,
HTTPS gateway, IAM implementation and UI serving. The router expects paths such
as `/api/platform`, `/api/query` and `/api/schema/export`. A non-Rust platform can
run this thin Rust executor behind its gateway and keep IAM in its existing
service, provided the adapter verifies each operation against that authority.

## 3. Integrate the browser session

Serve the workbench under your platform's HTTPS origin. Standalone assets do not
read platform identity unless their hosted copy includes:

```html
<meta name="quarry-mode" content="platform">
```

Use [`web/src/platform.ts`](../web/src/platform.ts) as the current adapter,
then implement your platform's login, refresh, account-switch and logout behavior.
Its existing Ylab contract is:

- Session key `ylab.console.session.v1`, containing `access_token`, `refresh_token`,
  `expires_in` and `user.id`.
- Token refresh at `/api/iam/auth/refresh`.
- Workbench calls at `/api/db/*`, forwarded to the executor's `/api/*` paths
  (for example, `/api/db/platform` → `/api/platform`).
- Bearer authorization for remote calls and explicit companion pairing for
  loopback calls. Preserve origin checks and handle the browser's local-network
  permission flow.

Adding the meta marker alone is insufficient. Adapt session handling and gateway
routing together, and keep personal recovery/pairing data isolated on account
changes. An existing platform login must never silently authorize a computer.

## 4. Add the desktop companion to your CLI

Embed `quarry-cli::run_from` for standalone command capabilities. Add your own
companion command using `CompanionConfig`, `CompanionState` and `companion_router`.
Configure the exact trusted platform origin, logged-in IAM user, an absolute
SQLite state path and a loopback port. Let the host CLI own its account/profile,
packaging, process lifecycle and any keeper/Neptune dispatch.

Show the one-use pairing code only in the user's terminal. The webpage pairs
explicitly with that computer, origin and account. Pairing grants an expiring,
revocable local capability; it does not expose a general filesystem, database
credential or Docker control endpoint. Keep containers, volumes, ports and Local
resource state isolated by platform, account and source resource.

| Data or operation | Location and behavior |
| --- | --- |
| Credentials and resource registry | Host server; not returned to the browser |
| Drafts, tabs, history, bounded result caches, conversations, generated SQL and restore checkpoints | Computer SQLite, scoped by platform, account and target; shared with the CLI |
| Disconnected page edits | Limited account-scoped browser recovery copy; shown separately from a successful SQLite save |
| Concurrent personal edits | Revision checks report a conflict instead of silently overwriting another writer |
| Local PostgreSQL / Redis | Explicit Docker lifecycle through the paired companion |
| PostgreSQL schema transfer | Companion fetches the authorized source, imports into staging and retains the previous Local database; no business rows or source passwords |

After the first successful page load, retained cached assets allow offline
re-entry and editing. Remote and local connection states are independent.
Already-paired local-only operations remain available while the local capability
is valid. Remote queries, model requests and remote schema retrieval require live
IAM. Reconnection never replays SQL, starts Docker, imports a schema or replaces
an unsaved draft.

## 5. Configure the assistant and verify the boundary

Optionally attach `AssistantOptions` with a trusted model gateway and model.
The platform must support the caller's IAM authorization at that gateway.
Assistant proposals can be applied or restored, and never execute automatically.
Persistent personal records stay in the companion SQLite; actual remote queries
send SQL to the host, and model requests send required context to the gateway.
Host, database and model log retention follow your platform policy.

Before exposing the workbench, exercise your real IAM and gateway: denied
anonymous/cross-account/cross-resource requests, scoped and revoked sessions,
cancel ownership, rejected writes, expired/revoked pairing, wrong origins,
account switching, independent disconnections, SQLite restart persistence and
concurrent-edit conflicts. Verify PostgreSQL transfer contains no business rows,
preserves the previous Local database and recovers from failed imports. Check the
deployed HTTPS page can pair with the supported desktop browsers.

Hosted shared SQL, cloud personal history and cross-device synchronization are
not implemented. Standalone local `.sql` query files remain available. Schema
transfer is PostgreSQL-only. Neptune/openCypher remains experimental; its local
empty endpoint is not a graph database. See [compatibility](../COMPATIBILITY.md)
and the [YiminLab integration contract](ylab-integration.md) for further boundaries.
