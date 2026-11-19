# Conversation, run, control and event contracts — step 7.1

Verified: 5 November 2026. Branch: `feature/conversation-run-contracts`. Plan 6
delivery is accepted from the user's pull confirmation. This step defines typed
values and a frontend contract proposal, not database tables, endpoints, workers
or changed runner behavior. Required PR tests/delivery remain pending.

## Identity, selection and messages

`contracts/conversations.py` defines immutable values with forbidden extra fields,
bounded text/collections, aware timestamps and strict scalar validation.

| Value | Meaning and boundary |
|---|---|
| Conversation | Owner-scoped persistent chat identity, title (1–120 nonblank characters), creation time |
| ConversationMessage | Message ID, conversation ID, logical run ID, positive conversation sequence, user/assistant role, nonblank content up to 16,384 characters, creation time |
| DatasetSelection | Zero to 16 unique dataset IDs; no caller-authored row counts, owner or hash |
| DatasetSnapshot | Server-derived immutable dataset ID and SHA-256 |
| ResultProvenance | Conversation/run/tool-call IDs, dataset snapshot and artifact ID/hash/media type |
| RunSubmission | UUID idempotency key, nonblank question up to 8,192 characters, selection and at most 16 unique prior tool-call references |

`conversation_id` persists across follow-up questions and browser logins. `run_id`
identifies one logical submission. Event-envelope `session_id` identifies an
immutable execution attempt; recovery may create a new one for the same run.
`auth_session_ref` identifies the private backend browser session. These identities
are not interchangeable. No OAuth tokens, cookies or browser-session references
belong in conversation request/response or event contracts.

The selection is explicit and may be empty, allowing missing-dataset clarification
later. Multiple datasets remain separate analyses; no join specification exists.
Admission must verify attachments/ownership and pin real snapshots. Provenance is
a reference to resolve, not proof: verify owner, conversation, original run/attempt,
successful tool call, artifact digest and dataset version/existence from storage.
Never trust a model message or client hash as a computed result. Run views reject
duplicate snapshots/results and references inconsistent with their run/selection.
Prior results in submissions may come from older runs in the same conversation;
plan 7.3 must resolve/re-authorize them and map them explicitly for the runner.

## Run lifecycle and clarification

`ConversationRun` uses `schema_version=conversation-run-v1` and pins AgentVersions,
dataset snapshots and result references. Assistant explanation text belongs in
stored conversation messages; artifact contents are fetched separately with owner
checks, not embedded in every polling response.

| Lifecycle | Required state |
|---|---|
| queued | No start/completion time, terminal outcome, clarification or results |
| running | Start time present; no completion time or terminal fields; verified partial results may exist |
| finished | Completion time and outcome present; timestamps cannot run backwards |

Terminal outcomes are `completed`, `needs_input`, `unsupported`, `incomplete`.
`completed` requires verified result references. `needs_input` requires exactly one
bounded RequestClarification; other outcomes cannot carry it. A run rejected before
execution may finish incomplete without a start time. Incomplete runs may retain
verified results; they must not be presented as completed analyses.

`RequestClarification` is the future `request_clarification` control argument
schema: one nonblank question up to 512 characters, zero to eight options, each
with a unique bounded key and nonblank text up to 256 characters. Empty options
allow a free-text reply. It is not a numerical tool. Its schema is available but
not registered in the running agent yet; plan 7.4 owns prompt/registry/behavior
changes. A reply will submit a new run with a new idempotency key in the same
conversation. No job remains active while awaiting input.

Existing StatisticsAnswer/statistics-answer-v1 is unchanged and still rejects
`needs_input`. Plan 7.4 must add an explicit compatible mapping/version transition;
it must not pretend legacy runner behavior already supports clarification.

## Admission and pagination semantics for later implementation

The following is a contract proposal for plans 7.2/7.7, not a delivered HTTP API:

- Normalize the transport idempotency key into RunSubmission.idempotency_key.
  Scope accepted keys to verified issuer/subject and conversation. Fingerprint
  canonical JSON of question, ordered selection and prior references; preserve
  text and array order. Exclude the key itself and newly resolved server versions.
- Same accepted key and fingerprint returns the original run, including its pinned
  versions; it must not append another message/job. Check replay before the busy
  rule. Same key with different content is `idempotency_conflict` (planned 409).
- A different key while a run is queued/running yields `conversation_busy`
  (planned 409). Do not queue the follow-up. PostgreSQL must enforce this atomically
  in 7.2; these value types provide no lock or uniqueness constraint.
- Successful submission/replay returns the accepted identity (planned 202).
  Polling an owned run returns its current lifecycle/outcome (planned 200).
  Missing/foreign resources share `resource_unavailable` (planned 404). Existing
  authentication/CSRF/role failures retain their established 401/403 handling.
- RunError provides fixed codes plus request correlation; future routes map them
  into the existing safe ErrorEnvelope. Other codes are `invalid_request`,
  `context_limit_exceeded`, `access_denied`. Never forward raw validation inputs or
  database/provider exceptions. Context overflow is not permission to raise model
  budgets or silently omit the current question.
- PageRequest defaults to 50 items and caps at 100; cursor length is 1–512 nonblank
  characters. Page[T] caps items at 100 and returns next_cursor or null. Cursors
  are opaque server-issued keyset positions bound to owner, collection and query;
  the repository/API must verify them. Message ordering follows conversation
  sequence; metadata/history pagination must use stable ordering. These contracts
  do not implement cursor integrity, expiry or pagination queries.

No business route names or cancellation capability are introduced here.

## Versioned event payload proposal

`contracts/run_events.py` defines one typed payload for each existing event name.
`validate_run_event` explicitly opts an EventEnvelope into payload semantics.
The outer envelope remains schema_version 1.0; payloads require the explicit
`payload_version=agent-run-events-v2`. Legacy events remain valid through the
existing EventEnvelope and are not silently reinterpreted as v2.

| Event | Payload observations |
|---|---|
| session.started | Conversation ID, pinned configuration versions, dataset count; execution/run IDs remain in envelope |
| tool_call.requested | Server tool-call ID, numerical/control/unknown kind, allowlisted name or null for unknown, argument digest, categorical/count features |
| tool_call.completed | Completed status, optional correlated policy verdict and nonnegative latency |
| tool_call.failed | Failed/timed_out/unknown/skipped/rejected status, fixed safe reason, optional correlated policy verdict/latency |
| model_call.completed | Model-call ID, attempt index, latency and existing reported/unavailable TokenUsage |
| model_call.failed | Model-call ID, attempt index, latency, fixed provider failure category and outcome-unknown flag |
| session.ended | Finished lifecycle, terminal outcome and verified-result count |

ToolFeatures includes argument-shape category, selected/unselected/absent/invalid/
not_checked dataset category, column count and allowlisted operation/method/missing
policy. There are no raw column names, argument values, prompts, CSV cells, unknown
tool names, credentials or mock labels. Feature extraction is not implemented
here. Session/model/tool counts are observations, not attack labels. Policy allow
does not imply execution success; unavailable policy is not a policy violation.
Controls have no fabricated numerical verdict. Completed work cannot carry a
denied/unavailable/not_evaluated policy verdict or mismatched tool-call identity.

[Seven synthetic examples](../tests/fixtures/run_events_v2.json) are independent
payload examples, not a complete causal trace. Existing producers still emit
their current payloads. Plan 7.6 must explicitly map/version producers and consumers,
add durable outbox/ordering/deduplication, and enforce the typed payload boundary.
The planned topic stays agent.events.v1; it does not imply payload-version equality.

## Verification and delivery

Added 50 tests including two lightweight import-boundary cases. Focused contract/
compatibility gate: 135 passed. PostgreSQL/non-browser gate: **897 passed, 17 browser
tests deselected**, 262 upstream warnings, 56.54 seconds. A final session-start
conversation-ID fixture/schema adjustment passed all 14 payload tests. Ruff,
formatting, dependency and whitespace checks pass. No auth/browser code changed;
browser tests were not rerun and required CI still includes them.

Frontend handoff/8.4 now describe the proposed values and readiness. This is local
contract review/preparation; teammate acceptance and generated TypeScript/OpenAPI
integration remain pending delivered APIs. No dependency, migration, public route,
model call, worker, event producer or Kafka behavior changed. Stop before 7.2.
No commit/push. Parent plans/progress/frontend handoff remain outside the service PR.
