# quarry-assistant

Reusable SQL assistant for Quarry, ylab and bcli. It owns model transport,
restricted metadata tool orchestration, proposals, cancellation, and durable
conversation history. It does not own a Tokio runtime, discover a user's home,
open arbitrary databases, or execute model-generated SQL.

## Embedding

Construct `ConfigManager::new(config_path, explicit_environment, system_language)`
and `ConversationStore::new(history_sqlite_path)`, then
`AssistantService::new(store, config, Arc<dyn MetadataProvider>)`.
The host implements `resolve`, `find_tables` and `describe_tables` on
`MetadataProvider`; `ResolvedContext.fingerprint` must identify the actual
selected connection, including SQLite file identity. Recheck context never
sends a connection URL to the model. Configuration and history paths are
explicit and can be isolated for an IAM principal or local profile by the host.

The system prompt and tool schemas in `assets/` are copied unchanged from the
validated Python assistant. Hosts should expose catalog results in the same
shape: `find_tables -> {tables: string[], capped: boolean}`;
`describe_tables -> {tables: [{name, columns: [{name,type,nullable,comment?}],
primaryKey: string[], foreignKeys: [{columns,targetTable,targetColumns,
targetSchema?}], capped}]}`. SQL execution belongs to a separate, explicit host
operation; `propose_sql` only offers text and `apply_conversation` only updates
the editor document.

## HTTP adapter contract

The crate has no HTTP server dependency. Existing GUI DTOs remain usable:

| Route | Service operation |
| --- | --- |
| GET `/api/assistant/config` | `config().status()` |
| GET `/api/assistant/preferences` | `config().preferences()` |
| GET `/api/assistant/conversations` | `list_conversations()` |
| GET `/api/assistant/conversations?id=...` | `get_conversation(id)` |
| POST `/api/assistant/conversations` | `create_conversation(body)` |
| POST `/api/assistant/conversations/update` | `update_conversation(body)` |
| POST `/api/assistant/conversations/apply` | `apply_conversation(body)` |
| POST `/api/assistant/conversations/restore` | `restore_conversation(body)` |
| POST `/api/assistant/chat` | `start_chat(body, true)` |
| POST `/api/assistant/cancel` | `cancel(conversationId, requestId)` |

Create accepts `{db, env?, workspace?, sql?, title?}`. Update uses
`{id, revision, sql}`. Apply uses `{id, revision, sql, messageId, explicit?}`;
restore uses `{id, revision, checkpointId}`. Chat uses
`{conversationId, requestId?, revision, sql?, prompt?, explain?}`.
The returned `ChatStream` yields JSON values; serialize one JSON value plus
newline per event, with `application/x-ndjson`, `Cache-Control: no-store` and
`X-Accel-Buffering: no`. Dropping the stream cancels pending model I/O. Explicit
cancellation requires both IDs so an old browser request cannot cancel a new
one. Call `close()` to cancel all active turns on graceful shutdown.
`AssistantError` carries HTTP `status`, stable `code` and safe `message`.

## State and limits

The SQLite `conversations(id TEXT PRIMARY KEY, document TEXT NOT NULL)` layout
opens Python 1.x histories directly, retains unknown fields and private
fingerprints, and hides underscore-prefixed fields from public DTOs. The host
must retain the old fingerprint algorithm when importing an existing history;
`crates/quarry-service/src/lib.rs` includes the Python-compatible SHA-256 JSON identity
implementation and SQLite canonical-path/device/inode checks.
Transactions use immediate SQLite writes and compare-and-swap revisions. Apply
requires a complete matching assistant proposal and a valid editor revision;
restore only accepts original assistant apply checkpoints. Retries are
idempotent. Canceled, disconnected, truncated and timed-out replies are saved
as interrupted and cannot be applied.

Each service admits four active conversations. Each turn allows six model
rounds and sixteen tool calls, with bounded context, output, wire bytes and
channel backpressure. The total turn deadline is 180 seconds; model request
and read timeouts are separately configurable. The model HTTP client never
follows redirects and never exposes upstream response bodies or URLs in error
messages. Stored model keys are kept in a sibling private credential file,
bound to the configured endpoint; supplied environment keys are read only from
the explicit map passed by the host.

`cargo test -p quarry-assistant` covers real loopback SSE fragmentation and
JSON fallback, Bifrost headers, tool restrictions, proposal/apply separation,
network cancellation, total service close, admission limits, header timeout,
truncated streams, endpoint redirection, historical state, restore semantics,
concurrent SQLite CAS and configuration/credential handling.


## Refreshable local credentials

A trusted local TOML configuration may use `auth = "command"` with a
`credential_command` array. The first element must be an absolute executable;
arguments are passed directly without a shell. The command must write exactly
one printable bearer token, optionally followed by a newline, to stdout.

The provider executes immediately before every model HTTP request, allowing an
existing host CLI to refresh its own IAM session. Output is bounded, failures
are redacted, and execution obeys cancellation plus a ten-second maximum.
Tokens are not cached in the assistant, saved to configuration/history or
included in model messages. Status inspection does not execute the command.
Command settings are available only through trusted local configuration or
embedding code; the HTTP model-configuration adapter cannot select commands.
Set `bifrost = false` when an IAM gateway supplies upstream credentials itself.
