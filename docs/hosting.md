# Host Quarry

[中文](hosting.zh-CN.md) · [Standalone installation](install.md) · [Rust architecture](rust-preview.md)

One official Docker image serves the workbench, sign-in, read-only database API,
and query executor. Configure your databases and access grants, then choose the
built-in account or connect your identity provider.

## Choose a workbench

| Entry point | Connections | Personal records |
| --- | --- | --- |
| Hosted `/quarry/` | Server connections authorized for the signed-in account | Drafts and SQL history in account-scoped browser storage |
| Embedded localhost GUI | Local workspaces and authorized server connections | Workbench state and execution history in local SQLite |
| Standalone `qy gui` | Quarry workspaces and registered cloud sources | Local SQLite when using the protected workbench, browser storage in pure local mode |

All three share Quarry's query engine. The embedding client runs local queries
on your computer and forwards cloud requests under the selected account. The
hosted service keeps connection credentials and checks authorization on each
cloud request.

## Start the image

Use the [Compose file](../deploy/hosted/compose.yaml) and copy
[hosted.example.json](../deploy/hosted/hosted.example.json) into its adjacent
`config/hosted.json`. This single file configures sign-in, databases and grants:

```json
{
  "listen": "0.0.0.0:8080",
  "state_dir": "/var/lib/quarry",
  "auth": {
    "mode": "builtin",
    "username": "admin",
    "password_hash": "REPLACE_WITH_GENERATED_ARGON2ID_HASH"
  },
  "databases": [{
    "id": "notes",
    "project": "demo",
    "db": "notes",
    "env": "production",
    "url": "postgresql://readonly-user:REPLACE_PASSWORD@database:5432/notes",
    "production": true,
    "readers": ["admin"]
  }]
}
```

Generate the administrator password hash, place it in the file, then start:

```sh
docker run --rm -it ghcr.io/yiminspace/quarry:hosted hash-password
docker compose up -d
```

Open `http://127.0.0.1:8080/quarry/` and sign in with the configured administrator.
`username` is the sign-in name; optional `user_id` defaults to `admin` and is the
stable authorization/personal-state identity. Set explicit resource `readers`;
an empty list grants access to nobody. Connections marked `production: true`
default to manual execution.

Compose binds only loopback by default; publish through your HTTPS reverse proxy.
Pin `QUARRY_IMAGE_TAG=sha-<full-source-SHA>` in production. Configuration is mounted
read-only and must be readable by image UID 10001. The `quarry-state` volume
persists authentication state. `/health` returns service/version; the image's
`check` command validates configuration. Never bake connection credentials,
password hashes or deployment keys into the image or commit them publicly.
Existing standalone workspaces remain supported through the advanced
`config_path`, `workspaces` and `resources` configuration.

## Build and update the hosted image

The [Dockerfile](../Dockerfile.hosted) builds the React assets with Vite and
embeds them into the Rust `quarry-server` binary at compile time. The runtime
image contains that binary and its runtime dependencies. CI tests the Linux
amd64 and arm64 images before publishing them to GHCR under the source SHA.

The workbench links to the release notes when a newer stable hosted release is
available. Each browser can dismiss the notice for that version. Use the release
notes to select the new image, update `QUARRY_IMAGE_TAG` in your Compose
environment, then run `docker compose pull quarry` and `docker compose up -d quarry`.
Verify the running version at `/health`.

Standalone installs from releases with self-update support use
[`qy update`](install.md#update). An embedded workbench ships with its host
application and follows that application's release channel.

## Use an existing identity provider

Keep the same image and change `auth` to `external`:

```json
{
  "mode": "external",
  "verify_url": "https://iam.example.com/api/authorize",
  "response": "headers",
  "session_key": "company.session.v1",
  "login_url": "/login?return=/quarry/",
  "refresh_path": "/api/auth/refresh"
}
```

For each authorization check, Quarry sends a GET request to `verify_url` with
the Bearer token, `X-Required-Permission`, and `X-Required-Scope`. The identity
provider verifies the session and permission before returning HTTP 200. In `json`
mode, Quarry reads `id`, `roles`, and `permissions` from the response. In `headers`
mode, it reads `X-User-Id`, `X-User-Roles`, and `X-User-Permissions`, with roles and
permissions separated by commas. Quarry then checks the resource's `readers` grant.
Permission names are `lab:db:read`, `lab:db:local`,
`lab:db:schema:export` and `lab:ai:invoke`.

Browser login, refresh and optional `logout_path` are same-origin paths served
by your existing platform, through a reverse proxy when needed. Session shape:

```json
{"access_token":"...","refresh_token":"...","expires_in":3600,"user":{"id":"stable-account-id"}}
```

The frontend reads the session key and authentication paths from
`/api/hosted/config`. After sign-in, your platform writes the session to that key
and returns the browser to `/quarry/`. Route `/quarry/`, `/api/hosted/*`, and
`/api/db/*` to the hosted service. Route your sign-in, refresh, and sign-out paths
to the platform endpoints that handle them.
### CLI-launched browser sessions

Set `auth.cli_handoff` to `true` on a standalone external-auth server to enable the single-database workbench.
The CLI sends its external bearer and `{"resourceId":"…"}` to `POST /api/db/browser-ticket`.
It opens `/quarry/#quarry_handoff=<code>` using the returned one-use code.
The browser exchanges the code for a path-scoped, Secure, HttpOnly session cookie.
Tickets expire in 60 seconds and browser sessions expire in 30 minutes. Both are local to the server process.
Use HTTPS and a single replica for this mode. The browser's database access is rechecked against external IAM.

Private-network HTTP verification requires explicit `allow_insecure_http: true`;
public connections should use HTTPS.

## Cloud Local

The hosted server can run personal PostgreSQL and Redis debugging databases for each user, with
the same experience as a desktop Local: choose any allowed image version, full privileges inside
the database, and direct connections from your own services. Databases run inside a separate
Docker-in-Docker service that only Quarry can reach; their ports are not published on the host.

```sh
cp hosted.cloud-local.example.json config/hosted.json   # then fill in accounts and databases
docker compose -f compose.yaml -f compose.cloud-local.yaml up -d
```

The `cloud_local` section of `hosted.json`:

| Field | Meaning |
| --- | --- |
| `docker_host` / `docker_cert_path` | Docker-in-Docker address and TLS client certificate directory; `tcp://docker:2376` and `/certs/client` in the Compose overlay |
| `connect_host` | Host name Quarry uses to reach database containers; `docker` in Compose |
| `ports` | Port range allocated inside Docker-in-Docker; default `[20000, 20999]` |
| `max_per_user` | Locals per user; default 5 |
| `memory` / `cpus` | Per-container limits such as `"1g"` and `"1"` |
| `images` | Allowed image prefixes; defaults to `postgres:`, `postgis/postgis:`, `pgvector/pgvector:`, `redis:`, `redis/redis-stack-server:` and `valkey/valkey:` |
| `ticket_hours` | Tunnel ticket lifetime; default 12 hours |

To use it:

1. Open connection details for a PostgreSQL or Redis database and choose Create cloud Local,
   optionally with an image.
2. The Local appears as that database's Cloud Local environment. Query it from the web and CLI like
   any hosted database.
3. To connect your own service, choose Connect from this computer and run the command shown:

   ```sh
   qy local connect https://quarry.example.com/quarry/ --port 5433 --ticket qt_...
   ```

   Then point the service at `postgresql://quarry:quarry@127.0.0.1:5433/<database>`. The tunnel
   forwards bytes unchanged, so transactions, prepared statements, extensions and migration tools work.

Cloud Local requires `lab:db:local`. Each Local belongs to the account that created it; tickets are
bound to that account and Local and are revoked when it is deleted. Web queries stay read-only;
writes go through the tunnel, as with a desktop Local. A PostgreSQL Local can sync its schema from
its source database without copying rows.

Docker-in-Docker needs a privileged container. Run it on a trusted host or use the Sysbox runtime.
Cloud Local data lives on the deployment's server (the `cloud-local-docker` volume); backups and
retention are the deployer's responsibility.

## Hosted and local workbenches

The hosted web workbench shows the cloud connections authorized for the signed-in
account. Drafts and SQL history stay in that account's browser storage. Query
results remain in the current page. A previously cached shell can reopen for
editing when the network is unavailable.

To embed the localhost workbench in a Rust client, build the web assets and call
`quarry_cli::run_gui_from` with a host-provided `CloudBackend` for the cloud catalog
and operations. The host selects the account and manages its session credentials.

## Assistant and acceptance

Configure both `assistant_base_url` and `assistant_model` to enable the assistant,
or omit both to disable it. The model gateway must authorize the current user's
Bearer credential. Suggestions can be applied and restored, never auto-executed.
Remote queries transmit SQL to the service; model requests transmit the necessary
context to the gateway. The deployment owns its platform logging/retention policy.

Release acceptance covers a real container and persistent volume, administrator
login/refresh/logout, external IAM and resource denial, real read-only queries,
cancellation, local SQLite persistence and offline restart, and the
standalone CLI/GUI/MCP regression suite. Mock previews and successful builds are
separate evidence, not substitutes.

See [Rust architecture](rust-preview.md) to embed Quarry in another application.
