---
name: quarry
description: >
  Query PostgreSQL, MySQL, Redis and Neptune with Quarry (qy), compare data
  across environments, maintain reusable queries, and manage connections or
  the database workbench. Use for database evidence or explicit Quarry tasks,
  not general database design discussions.
---

# Quarry

[简体中文](SKILL.zh-CN.md) · Use either edition; their rules are equivalent.

You choose the query and interpret results. The CLI owns execution, query
files, metadata, validation and skill-directory links.

## Call the CLI

Use the installed SKILL.md's directory; pass global options before the command:

```sh
qy --skill-dir "<skill-dir>" --workspace "<workspace-dir>" <command> ...
```

`--workspace` selects a workspace; otherwise config.toml registrations apply,
falling back to the current directory. Select the destination when saving:
with multiple workspaces, writes default to the first, not the owner of `--db`.
Do not use the skill directory as a workspace.

`--skill-dir` creates `skill/queries/<workspace-name>/` links to workspace-owned
queries, excluding connection credentials. Pass it each time to recreate
missing links. Let the CLI manage files and links; report conflicts without
overwriting user files. Use the project's installation instructions if `qy`
or this option is unavailable.

Choose commands as needed; consult `qy <command> --help` for options:

- Discover: `connections`, `workspace list`, `schema`.
- Query: `exec`; reuse with `list`, `describe`, `run`.
- Maintain: `save`, `validate`, `fingerprint`, `audit`, `edit`, `remove`.
- Operate: `gui`, `connections`, `proxy`, `up/down/status`, `speedtest`, `local`.

## Query well

- Reuse a matching query when useful; explicit SQL and one-off questions need
  no saved-query search. Resolve ambiguous names with `--workspace`.
- Inspect live schema or available project models as needed. Source helps with
  business meaning but may differ from deployment; no source checkout is required.
- Select the database and environment deliberately. Use the engine's language:
  SQL, Redis commands or Neptune openCypher. Keep query parameters consistent
  when comparing environments and identify each target.
- Bound exploratory `exec` and `run` output with `--max-rows` and select needed
  fields. Use aggregates for totals: returned row limits do not bound query cost.
- Use `:'name'` for quoted values; bare `:name` only for trusted raw substitutions.
- Writes need authorization and CLI write flags; production writes need explicit
  confirmation unless already authorized. Verify the resulting data change.
- Report findings with database/environment and any truncation. Use JSON for
  inspection or CSV/NDJSON for requested artifacts. Share GUI queries through
  its copy-query-link button.

## Keep reusable queries

Use `save` for queries worth reusing or explicitly requested, with a descriptive
name and variable values declared via `--param`. Declare actually consulted
schema files via `--schema-source`; live-schema-only queries need none.
Use `--overwrite` only for intended replacements.

Validation checks executability, not business correctness. Failed save validation
may leave a file; fix and revalidate before claiming success. Fingerprints check
source changes, not live-schema drift; use them when relevant, `audit` for a
collection, and `validate` to check current executability.

Diagnose errors before retrying: distinguish query, connection and authorization
failures, keep retries bounded, and avoid unrelated configuration changes.
