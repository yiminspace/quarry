Quarry 2 replaces the Python runtime with a reusable Rust kernel shared by the
standalone CLI, browser workbench and MCP. Existing workspaces, saved queries
and browser drafts are retained. Python import APIs are retired in this major
version; Python 1.11.0 remains available in the archive branch and existing tags.

Install from a platform wheel or the checksum-verified native archive. Supported:
macOS/Linux, arm64/x86_64; Linux requires glibc 2.28+. See docs/install.md for
Python-to-Rust upgrade steps, SSH/Docker/schema-sync dependencies and rollback.

The rc is a prerelease for installation and upgrade acceptance. Stable promotion
requires all platform artifacts and acceptance checks to pass.
