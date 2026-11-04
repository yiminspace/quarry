Quarry 2.0.0 replaces the Python runtime with a reusable Rust kernel for the
CLI, browser workbench and MCP. Python 1.11.0 is preserved in its existing tag
and the locked archive/python-1.x branch. Python import APIs are retired;
existing workspaces, saved queries, assistant history and browser drafts remain.

Supported artifacts: macOS/Linux arm64/x86_64; Linux glibc 2.28+. Install the
same Rust executable through PyPI platform wheels or checksum-verified native
archives. No Python/Rust toolchain is needed for native execution. See
[installation and upgrade](https://github.com/yiminspace/quarry/blob/main/docs/install.md)
for installer ownership, GUI/keeper restart and rollback steps.

Candidate acceptance includes native archive and wheel installation, upgrade
from the published Python 1.11.0, shared-core tests, real PostgreSQL/MySQL/Redis,
strict forwarded TLS and the existing workbench browser matrix. SSH, Docker
and PostgreSQL schema-sync tools remain optional system dependencies. Neptune
remains experimental; this release does not promise all AWS/IAM graph setups.
