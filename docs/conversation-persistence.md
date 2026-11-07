# Conversation history and atomic admission — step 7.2

Current step 7.5 adds [worker execution](durable-worker.md), migration 0007,
session-bound jobs, leases, local evidence and running result checkpoints. The
7.2 evidence below describes the initial storage boundary, not current worker
capabilities. No public conversation/run API is delivered yet.

Branch: `feature/conversation-persistence`. Step 7.1 delivery is accepted from
the user's pull confirmation. This step adds internal PostgreSQL storage only;
there is no new public route, model dispatch, worker or Kafka publisher.
Step 7.2 is complete locally; required PR tests/delivery remain pending.

## Schema and deployment

Migration `0005_conversation_history`, following `0004_dataset_storage`, adds:

| Table | Purpose and integrity |
|---|---|
| agent_conversations | Exact issuer/subject owner, title, creation time, next message sequence, explicit attachment-replacement version; owner/ID index |
| agent_conversation_datasets | Composite conversation/dataset primary key; both foreign keys cascade on deletion |
| agent_runs | Submission fingerprint and UUID key; lifecycle/outcome/times; immutable admission snapshots and result/clarification storage |
| agent_messages | Conversation/run, unique conversation sequence, role, protected text and creation time; composite FK prevents a run/message conversation mismatch |
| agent_run_jobs | One row per run, containing only run ID and creation time; FK cascades with the run |

Runs have unique `(conversation_id, idempotency_key)` and a partial unique index
on conversation ID where lifecycle is queued/running. The latter protects the
single-active-run invariant even if application locking is bypassed. Other checks
bound JSON snapshots, enforce lifecycle/timestamp/terminal/result relationships
and require valid fingerprint shape. Configuration and submission snapshots cap
at 65,536 JSON-text bytes each; dataset metadata snapshots cap at 1 MiB/16 entries.

The migration is a frozen SQL snapshot, independent of mutable application models.
It preserves existing auth, dataset, content and artifact tables/data. After merge,
the existing migration job must apply the new head before readiness can succeed.
For a configured development environment, the existing command is:

```powershell
python -m statistics_agent.persistence.migrate upgrade
```

No developer database was migrated during verification. Disposable rollback to
0004 removes the five new tables/history, retaining existing datasets/auth data.
Production application rollback should retain the additive schema, not downgrade
or delete history. No change to image publication/deployment workflows is included.

## Internal repository interface

`persistence.conversations.ConversationStore` provides:

- `create`, `get`, `list`: owned conversation metadata; list uses bounded UUID
  keyset pagination (50 default, 100 maximum).
- `set_attachments`: replace up to 16 unique owned references atomically, while
  holding the conversation lock. Validate every dataset before deleting old links.
  The explicit replacement version increments; an already admitted run is not
  rewritten. `attachments` returns current live metadata, ordered by dataset UUID.
- `admit`: accept a validated RunSubmission and server-owned AdmissionConfiguration,
  returning StoredAdmission. `get_run` reads it through the owned conversation.
- `messages`: history ordered by positive conversation sequence, with bounded
  limit and exclusive nonnegative after_sequence cursor.
- `delete`: remove an owned conversation only when no run is queued/running.

The repository consumes an already-authorized principal. Every resource query
includes exact owner filtering; admin membership does not bypass it. Conversation
history survives browser-session renewal/relogin because ownership uses issuer/
subject, not the browser reference. It performs no JWT refresh or Keycloak lookup.
Future API routes must apply the existing authentication/role/CSRF requirements.
Its UUID/integer cursors are internal positions; opaque signed/owner-bound wire
cursor handling and public error/response mapping remain 7.7.

## Admission transaction and replay

1. Validate strict submission/configuration values. Fingerprint canonical JSON of
   question, ordered selection and prior references, excluding the idempotency key.
   Preserve question whitespace and array order. Do not hash new server versions
   into the client-content fingerprint.
2. Lock the owner-filtered conversation row. Recheck the same conversation/key.
   Matching content returns the accepted run with replayed=true, including its
   original snapshots; changed content raises IdempotencyConflictError. Replay
   precedes the busy and current attachment checks, including after detachment.
3. Reject other queued/running work as ConversationBusyError. Load selected
   attached datasets through owner predicates, in deterministic UUID lock order,
   holding shared row locks against deletion until admission commits. Missing,
   foreign, deleted and unattached data use the same RunInputUnavailableError.
4. For prior references, require the original persisted result in the same owned
   conversation and matching live owned dataset/artifact identity and stored
   digest/media type. This is admission validation, not the context/provenance
   resolver: 7.3 must still verify artifact bytes, current existence/ownership and
   successful original execution before putting prior values into model context.
5. Pin the conversation title, attachment-replacement version, ordered complete
   dataset metadata, request and non-secret configuration. AdmissionConfiguration
   includes policy, ModelProfile, prompt definition and AgentVersions; provider,
   prompt and policy references must match their actual snapshots. Toolset is
   versioned by reference to the server-owned registry, not serialized callables.
6. Insert a queued run, user message, pending job identity and incremented message
   sequence in one transaction. Commit before returning success. Any failed insert
   rolls back every part. Other conversations can admit independently; there is
   no process-local busy flag or hidden retry loop.

Jobs contain no question, provider key, OAuth token, role or authorization snapshot.
Server admission snapshots are private history, not HTTP responses or Kafka
payloads; StoredAdmission hides text/configuration/metadata in repr. Do not pass
ModelSettings or request-supplied credentials as configuration. Secret lookup and
current session/access checks remain execution-time responsibilities in 7.5.
History and admission snapshots use ordinary PostgreSQL text/JSON with owner
checks; this step does not add field-level encryption for those values.

## Retention and boundaries before HTTP exposure

History currently persists until explicit owned conversation deletion. Active
conversations cannot be deleted; no cancellation/lease logic is invented here.
Deleting a terminal conversation cascades its runs/messages/jobs/attachment links,
but retains independently owned CSVs and artifacts. Removing an attachment does
not delete its CSV, artifacts, old messages or immutable admission metadata.

Existing dataset deletion still deletes its bytes/artifacts and current attachment
links. Historical snapshot metadata and text remain in conversation history; they
are not restored input data. Prior references become unavailable and must not be
reused for new computations without live revalidation. Dataset deletion through
FK cascade does not increment the explicit replacement counter; the counter must
never substitute for live ownership/existence checks.

There is no automatic one-day CSV expiry or history/artifact cleanup scheduler in
this step. Retention automation/quotas and redaction of existing historical prose
need explicit policy before public API exposure. No claim is made that old text is
automatically erased when its source CSV is deleted. Jobs remain pending records:
claims, leases, transitions, recovery and authenticated execution are 7.5; bounded
context is 7.3, assistant/clarification result persistence is 7.4/7.5, and public
APIs are 7.7. The tests seed terminal rows as storage fixtures, not runtime workers.

## Verification and delivery — 5 November 2026

Added 24 storage integration tests. Initial migration/storage gate: 55 passed;
expanded storage gate: 23 passed before the final multi-dataset case. Complete
final gate: **938 passed, no skips**, including all 17 browser tests, with 262
upstream warnings in 370.89 seconds. Ruff, formatting, dependency and whitespace
checks pass. Existing migration tests now expect head 0005 and the five additive
tables; model/migration metadata comparison is unchanged and passes.

Proof includes concurrent same/different-key admission, direct unique-index
enforcement, replay before busy, changed fingerprints, rollback on job failure,
exact owner/admin/issuer isolation, missing/foreign/unattached/deleted datasets,
ordered immutable multi-dataset snapshots, changed attachments/configuration,
prior-result provenance/digests, new-pool persistence, history pagination, terminal
deletion and upgrade/disposable downgrade/re-upgrade preserving existing data.

Disposable PostgreSQL/Keycloak fixtures are removed after verification; developer
databases and identity data are untouched. No live model/cloud call, dependency,
public route or authentication behavior change. No commit/push. Stop before 7.3.
Parent plans/progress/frontend handoff remain outside the service Git repository
and are not included in its PR. Apply the migration through the existing deployment
gate after merge; local verification is not a remote GitHub CI result.
