Quarry 2.3.0 lets hosted deployments run Local databases on the server.

A hosted workbench can give each user personal PostgreSQL and Redis Locals on a
Docker-in-Docker daemon owned by the deployment. Create one from a database's
connection details, pick any allowed image version, and optionally copy the
source schema without its rows. Queries from the workbench stay read-only. To
connect your own services, copy the command from the workbench and run
`qy local connect`: it opens a loopback port and forwards the database protocol
unchanged through an authenticated WebSocket, so drivers, transactions,
extensions and migration tools work as with a desktop Local. Each Local has its
own Docker network and password, and only its owner can reach it.

Hosts that launch the workbench from their CLI can open one database per
browser session with in-memory expiry.

Result columns can show image URLs as thumbnails when you choose Load images
from the column header.

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
