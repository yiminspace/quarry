Quarry 2.3.1 fixes copying PostgreSQL schemas on Debian and Ubuntu.

Creating a Local with the source schema, and schema export in hosted Quarry,
failed with an empty schema error where `pg_dump` is a `pg_wrapper` symlink.
Quarry now runs `pg_dump` by the path that passed its version check.

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
for IAM, database access and cloud Local configuration.
