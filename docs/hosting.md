# Host Quarry

[中文](hosting.zh-CN.md) · [Standalone installation](install.md) · [Rust architecture](rust-preview.md)

One official Docker image serves the workbench, sign-in, read-only database API
and query executor. Supply database and access configuration; do not write a Rust
adapter or rebuild the frontend. YiminLab uses the same image with its existing IAM.

Standalone Quarry remains independent: `qy`, `qy gui` and `qy mcp` need neither
this server nor `ylab` nor platform sign-in. Existing local configuration and
records are not forcibly migrated.

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

The server calls `verify_url` for live identity/permission checks. `headers` mode
reads identity headers returned by trusted IAM; `json` mode reads `id`, `roles`
and `permissions`. Every operation needs both the relevant permission and its
resource `readers` grant. Browser-supplied identity headers or decoded-only tokens
are insufficient. Permission names are `lab:db:read`, `lab:db:local`,
`lab:db:schema:export` and `lab:ai:invoke`.

Browser login, refresh and optional `logout_path` are same-origin paths served
by your existing platform, through a reverse proxy when needed. Session shape:

```json
{"access_token":"...","refresh_token":"...","expires_in":3600,"user":{"id":"stable-account-id"}}
```

The frontend reads public runtime settings from `/api/hosted/config`, then uses
that session key and authentication paths. YiminLab supplies
`ylab.console.session.v1`, its existing Console login route and IAM refresh
endpoint, without a separate database image or custom frontend build.
Private-network HTTP verification requires explicit `allow_insecure_http: true`;
public connections should use HTTPS.

## Personal records stay on the user's computer

The server owns connections, access grants and query execution. Drafts, tabs,
history, bounded result caches, assistant conversations, generated SQL and restore
points live in the user's local SQLite, shared with the CLI.

`ylab db connect` opens the workbench and connects/saves automatically, without
address, pairing-code or database-administration forms. Standalone hosting uses
a one-use sign-in handoff; existing platforms retain their account confirmation.
The handoff code travels only in the URL fragment and is removed before requests;
long-lived Bearer tokens never travel in URLs. Local capabilities remain bound
to the platform, account and origin, with expiry and revocation and no arbitrary
filesystem access.

With the local service disconnected, remote querying and editing remain usable;
a bounded browser recovery copy protects pending work. Reconnection retries
saving automatically. Revision conflicts keep the current draft and require an
explicit export/restore choice rather than overwriting another record.
Reconnecting never executes SQL, starts a database or synchronizes schema.

A previously loaded and cached shell can reopen offline; the first visit still
needs to load the assets. Shared SQL, cloud personal history and cross-device
synchronization are outside this release.

## Assistant and acceptance

Configure both `assistant_base_url` and `assistant_model` to enable the assistant,
or omit both to disable it. The model gateway must authorize the current user's
Bearer credential. Suggestions can be applied and restored, never auto-executed.
Remote queries transmit SQL to the service; model requests transmit the necessary
context to the gateway. The deployment owns its platform logging/retention policy.

Release acceptance covers a real container and persistent volume, administrator
login/refresh/logout, external IAM and resource denial, real read-only queries,
cancellation, automatic companion SQLite writes and outage recovery, and the
standalone CLI/GUI/MCP regression suite. Mock previews and successful builds are
separate evidence, not substitutes.

The Rust libraries remain available for advanced embedding; see
[Rust architecture](rust-preview.md) and [YiminLab integration](ylab-integration.md).
