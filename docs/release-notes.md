Install Quarry from GitHub Releases. The native archive includes the CLI, local
workbench and MCP server. Installer-managed copies can apply a verified update
at the next launch or immediately with `qy update`.

The [installation guide](https://github.com/yiminspace/quarry/blob/main/docs/install.md)
covers switching from Python packages while keeping your existing configuration,
saved queries and data.

YLab's embedded copy of Quarry updates with YLab. Operators can update the
hosted workbench using the Docker image pinned in the stable release's
`hosted-release.json` descriptor. See the
[hosting guide](https://github.com/yiminspace/quarry/blob/main/docs/hosting.md)
for IAM and database access configuration.
