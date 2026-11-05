The hosted workbench can now show a dismissible notice when a newer stable
Quarry image is available. The notice links to the release notes. The hosted
release descriptor is attached after both Linux image architectures pass
container acceptance and can be pulled anonymously.

The [hosting guide](https://github.com/yiminspace/quarry/blob/main/docs/hosting.md)
describes configuring the official Docker image with built-in or external IAM,
per-database grants, and optional desktop integration. The update also preserves
bundled icon fonts when a cached hosted workbench is reopened offline.

Deployment operators can use the image pinned in the
[hosted release descriptor](https://github.com/yiminspace/quarry/releases/download/v2.0.1/hosted-release.json).
See the [installation guide](https://github.com/yiminspace/quarry/blob/main/docs/install.md)
for standalone native and Python-package upgrades.
