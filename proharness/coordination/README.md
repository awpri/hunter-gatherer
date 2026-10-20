# Durable local coordination

Six plain MCP tool families (`proharness_workspace`, `task`, `handoff`,
`artifact`, `message`, and `coordination`, all with the `proharness_` prefix)
select operations through an `action` enum. Workspaces and actor labels organize
one trusted user's local clients. They are **not tenant authorization or proven
identities**. Stored prose and addressed messages never execute, wake another
product, fetch paths/URLs, or grant user permission.

Create a workspace with `action=create`, `actor`, `idempotency_key`, and `name`.
Create a task with `workspace_id`, objective, constraints (strings),
`acceptance_criteria` (`[{"id":"test","description":"Check behavior"}]`),
optional dependencies and references. Claim it with its `expected_revision`,
actor and a fresh request key. The claim returns a top-level `claim_token` and
`fence`; keep both privately. Get/list/history/events never disclose tokens.

Checkpoint, update, renew, release and completion require current token/fence,
unexpired lease and expected revision. Checkpoints retain completed_steps,
findings, blockers and attempted_side_effects as string lists; next_action and
verification_notes as strings; references as a list. Every checkpoint is retained
in immutable task revision history. `task:history` paginates with
`before_revision`. Lease expiry does not delete progress. A fresh claimant can
adopt it; a late prior owner cannot write even if it guesses the new revision.
Fencing protects only server state, not arbitrary external side effects.

Handoff `offer` requires the owner claim and persists recipient/packet. The
recipient `accept` supplies `expected_revision` for the offer and
`expected_task_revision` for the task. Acceptance increments the task fence and
returns a new claim token. Rejection/expiration preserves the source task.
If the task changes after an offer, that packet is stale and cannot be accepted.

Completion maps each criterion to stored references or an explicit `waiver`
string. `criterion_results` use `criterion_id`, `references`, `waiver` and
optional `verification_notes`. External path/URI descriptors alone cannot meet
a criterion. The server checks evidence integrity/availability, not the truth of
an actor's report or whether tests really executed. Explicit waivers are actor
assertions recorded in this trusted-local-client system, not newly inferred user
authorization.

Publish artifacts with bounded JSON/text, producer, references, task_id, and
verification_notes. A new artifact starts revision 1; publishing with artifact_id
requires expected_revision. Content hashes are SHA-256 of canonical sorted,
compact, UTF-8 JSON, including JSON string quoting. Older revisions remain
addressable. References use exactly one of memory_id, chunk_id, artifact_id plus
revision, uri, or path. Document/chunk references may include document_id and
an exact source quote. A scoped memory's metadata.workspace_id must match.
Source integrity and privacy are checked again on every read, history and retry.
Missing or newly private evidence redacts dependent prose as a whole. Sensitive
opt-in never restores removed/corrupt sources; readers do not initialize the
archive. Parent task privacy also applies to associated artifacts/handoffs.

`message:send` persists an addressed body with 1 second to 7 days expiry.
`inbox` requires recipient and excludes acknowledged/expired messages; only the
recipient label may `ack`. Expired bodies are redacted even in retry receipts.
Audit rows remain retained; expiry is delivery expiration, not secure deletion.
No inbox operation messages an external app.

All mutations require idempotency_key. Scope is workspace+actor+key (workspace
creation uses an empty precreation scope). Same operation+arguments retries
return the original receipt with replayed=true, after live visibility checks.
Different arguments with the same key conflict. Mutation, immutable version,
event and receipt commit together under SQLite BEGIN IMMEDIATE. Failed requests
roll back and do not consume a key. Optional null fields are normalized away.

`coordination:changes` returns only durable sequence/object identifiers, kind,
revision, operation and time. Events deliberately contain no copied prose,
references or claim token. `after_cursor=0` starts retained history; a cursor from
another workspace, a missing event or a future cursor returns invalid_cursor
and snapshot_required. `coordination:snapshot` atomically reads bounded current
tasks/artifacts and a cursor; paginated lists may require follow-up pages.
Messages require an explicit inbox recipient. Standard MCP notifications may
be added later as a fast path; this application feed is durable authority.

`CoordinationStore(store, clock=...)` shares MemoryStore's SQLite and RLock and
creates only additive coordination tables. `task_get(workspace_id, task_id,
include_sensitive=False)` returns found/task; `task_list` returns tasks and
next_cursor. Mutation methods accept flat kwargs; `execute(action, **kwargs)` is
also available. Limits are 100 records per page, 16 KiB artifact/checkpoint
payload, 32 KiB request field helpers and 64 KiB total stored object/request.
