# Local database providers

An installed helper can add a remote database catalog to the same `qy gui` and
`qy connections` used for local workspaces. A provider owns its login and remote
requests. Quarry stores the registered command and authorized resource metadata
in a private registry scoped to the Quarry configuration file.

## Register a helper

Run the provider's login first, then register its local executable and fixed
arguments. The provider can invoke the same command from its own CLI.

```sh
qy provider register --id example --label 'Example Cloud' \
  --origin https://db.example.com --profile dev --owner account-id \
  --executable /absolute/path/to/example -- db provider
```

Quarry checks the helper's current account and fetches its authorized catalog
before saving the registration. Repeating registration for the same provider
ID, origin and profile replaces that registration. The owner and upstream
resource ID also contribute to the execution target ID. Labels and executable
paths can change independently of the target ID. A workbench that was already
open in local-only mode must be reopened after the first registration.

## Helper protocol

Quarry runs the registered executable directly with the fixed arguments. A
browser request, workspace or remote catalog cannot choose the command. Each
invocation reads one bounded JSON object from stdin and writes one JSON response
to stdout. The following request checks the current identity.

```json
{
  "version": 1,
  "provider": {
    "id": "example",
    "label": "Example Cloud",
    "origin": "https://db.example.com/",
    "profile": "dev",
    "owner": "account-id"
  },
  "method": "GET",
  "path": "/identity",
  "pairs": [],
  "body": null
}
```

Use `{"value":{"id":"account-id"}}` for identity and
`{"value":{"principal":{"id":"account-id"},"resources":[...]}}` for
`GET /api/platform`. Each resource supplies `id`, `project`, `db`, `env`,
`engine` and `production`. Query and metadata requests use the upstream
`resourceId`. Query strings are represented as key-value pairs and JSON POST
arguments go in `body`. The operation allowlist and argument rules are defined
by [`ProviderRequest`](../apps/qy/src/federation.rs).

Write `{"error":{"status":401,"code":"unauthorized","message":"Sign in to the provider"}}`
for a handled failure and exit successfully so Quarry can read it. Helpers run
non-interactively. They validate their provider identity, check that the login
used for an operation matches the requested owner, and use their configured
remote service. Credentials stay inside the helper and never appear in its
output. The remote service authorizes every operation.
