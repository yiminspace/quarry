Quarry 2.2.0 brings local and authorized cloud connections into one catalog.
Compatible providers can register authorized connections for discovery in
`qy connections` or `qy gui`. Remote queries use the provider account,
and cloud drafts and history stay scoped to the signed-in account.

Use `qy connections reconcile` to verify PostgreSQL connections that reach the
same database. Add `--apply` to group confirmed routes under the local name.
Existing sources can register again using the shared provider protocol.

The standalone workbench can reopen the matching running session and can be
stopped with `qy gui stop`.

Install Quarry from GitHub Releases. The native archive includes the CLI, local
workbench and MCP server. Installer-managed copies can apply a verified update
at the next launch or immediately with `qy update`.

The [installation guide](https://github.com/yiminspace/quarry/blob/main/docs/install.md)
covers switching from Python packages while keeping your existing configuration,
saved queries and data.

Embedded copies update with their host. Operators can update the
hosted workbench using the Docker image pinned in the stable release's
`hosted-release.json` descriptor. See the
[hosting guide](https://github.com/yiminspace/quarry/blob/main/docs/hosting.md)
for IAM and database access configuration.
