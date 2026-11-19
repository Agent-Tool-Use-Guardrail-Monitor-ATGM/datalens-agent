# PostgreSQL worker and recovery — step 7.5

The separate worker process executes admitted jobs with the existing controlled
CrewAI runner, real typed tools and original browser-session authorization.
PostgreSQL dispatches work. No Kafka command topic, new business HTTP endpoint,
container image or cloud resource is introduced here.

## Process and admission

After the existing deployment migration gate succeeds, run from the service's
virtual environment:

```powershell
python -m statistics_agent.persistence.migrate upgrade
python -m statistics_agent.persistence.migrate check
python -m statistics_agent.worker --env-file .env
```

Use an existing explicit environment file, or omit `--env-file` when settings
are supplied by the environment. Importing the module does not read credentials.
Worker startup requires current agent schema, the existing authentication settings,
client-secret/encryption-key files and `SERVICE_MODEL_API_KEY`. API and worker
share the agent database and encryption keyring; neither reads Keycloak's database.
Starting the worker consumes jobs from the shared PostgreSQL database.
Submit work through the [Bearer workspace API](workspace-api.md).

The initial capacity is one valid active lease across all worker replicas in the
agent database. A short transaction uses a shared advisory admission guard and
`SKIP LOCKED` conversation selection, then atomically sets running state, claims
the job and inserts a stable execution-attempt/request identity. No transaction
spans model HTTP, numerical work or a user wait. Idle polling is 500 ms; the default
lease is 30 seconds, renewed every 10 seconds. Each process owns one bounded
statistics parser. Per-request, whole-run and tool budgets remain unchanged.

Lease decisions use PostgreSQL time. Admission timestamps also use database time;
legacy host-clock timestamps are kept ordered rather than weakening constraints.
The executing process's whole-run deadline remains bounded independently of lease
renewals. Heartbeats cannot resurrect expired leases.

## Identity and pinned configuration

Admission records the original noncredential `auth_session_ref`. The worker reads
that local session by ID, checks active/idle/absolute/revoked state and exact
issuer/subject, then uses the delivered JWT validation/renewal lifecycle. It checks
again at execution boundaries and before model dispatch/finalization. A session ID
alone never authenticates. Browser idle time is not extended by background work.

Known signing keys permit local JWT checks during a warm identity outage. Cold key
discovery and required token renewal still need Keycloak. No per-call introspection
or immediate remote role-revocation guarantee is added. Removed, expired, revoked,
foreign or missing local sessions finish incomplete without new dispatch.

Queued jobs predating migration 0007 have no reconstructable session binding and
therefore fail closed. Never infer permission from conversation ownership alone.
Idempotent replay retains the original binding; a fresh user-requested execution
requires a new idempotency key.

The worker requires exact pinned model-profile/prompt/toolset compatibility.
`STATISTICS_TOOLSET` supplies the server-owned numerical registry version; policy
snapshots still use real ownership and admitted limits. Mismatches finish
incomplete instead of silently running old jobs under new configuration.

## Durable boundaries and fencing

Migration `0007_worker_execution` adds three nullable job fields and three tables:

- `agent_run_jobs`: original session reference, claim ID and lease expiry; pending
  order and expiry indexes support admission/recovery. The session reference has
  no cascading foreign key: session cleanup must deny work, not erase queued jobs.
- `agent_run_attempts`: one attempt per logical run, stable request identity,
  bounded sequence counter, start/end time and fixed disposition.
- `agent_run_evidence`: existing redacted envelopes, unique event IDs and ordered
  per-attempt sequence, at most 1,024 records of bounded size.
- `agent_run_dispatches`: model/tool operation IDs and retry indexes with pending,
  observed or unknown outcome. No prompts, arguments, CSVs or credentials.

The model adapter commits its attempt intent immediately before HTTP, including
each permitted retry. Tool proposal evidence commits before lookup, policy or
execution. A failed intent/evidence commit prevents the next dispatch. Existing
safe provider/policy outcome evidence is journaled under the same claim.

Successful numerical artifacts are checked against owner, selected immutable
dataset/version and original typed body, then checkpointed in the running run
before explanation. A failed checkpoint prevents continuation. Heartbeat, journal,
checkpoint and terminal operations reject missing, mismatched or expired claims.
The terminal write rechecks its fence, preserves the 7.4 provenance/answer rules,
records attempt disposition, saves assistant history and removes the job atomically.

This is a local execution journal, not the transactional Kafka outbox. Session
start/end delivery and typed feature-rich event publication remain step 7.6.
Current proposal evidence is still digest-only; do not present it as sufficient
classifier features or turn unknown/operational outcomes into attack labels.

## Interruption contract

Expired leases, shutdown and execution failures finish incomplete; unfinished
model/tool calls are never automatically replayed. Even a crash immediately after
claiming may require an explicit new user submission. Pending dispatches become
unknown, which means an outcome was not durably observed, not proof that an
external operation executed or violated policy.

Recovery revalidates only checkpointed numerical artifacts and retains valid
original references. Missing/corrupt artifacts are omitted; database failures leave
work recoverable rather than acknowledging a terminal write. Imported historical
results are not relabeled as new computations. A new authorized user submission
can explicitly reference a retained original result; no calculation is repeated
to reconstruct it. Uncheckpointed artifacts may remain unreferenced; storage
retention/quota cleanup remains a separate pre-public-deployment requirement.

SIGINT/SIGTERM stops admission, cancels the active async continuation and attempts
fenced terminalization. Parser shutdown waits for existing bounded native work;
there is no hard thread cancellation. If PostgreSQL is unavailable or the process
is killed, the expired durable lease lets another worker reconcile later.

Fencing prevents stale durable writes and later admitted steps. It cannot undo an
already sent HTTP request or stop a native operation immediately. Capacity counts
valid leases, not physically cancelable remote calls. No exactly-once external
execution claim is made.

## Rollback and next step

Existing auth, dataset, history and completion data is preserved by upgrade. Keep
the additive schema for a production application rollback. Disposable-test
downgrade deletes runs with new execution attempts before dropping worker tables
and lease columns; it must never be used for production data rollback.

Step 7.6 must build ordered at-least-once publication/outbox transactions on these
fenced boundaries, reconcile missing/unknown outcomes and preserve privacy.
This step does not connect Kafka, deploy containers, expose HTTP routes or require
frontend JWT handling. Verification evidence follows below. Parent plans/progress are outside the
service repository and PR.

## Verification

Focused worker/CLI gate: 26 passed; three final execution-binding cases passed
separately. Final current-code backend gate: **1,010 passed, 17 browser tests
deselected**, 481 warnings, 116.48 seconds. Ruff/format/pip/whitespace checks pass.
An initial combined gate had 1,022 passes and two existing synthetic-browser-login
failures (552.46 seconds); both failed cases passed unchanged under safe
stage/status diagnostics (2 passed, 50.54 seconds). No cause was proven, and no
authentication rules, assertions, timeouts or retries were changed. Final browser
group: **17 passed, 1,010 deselected**, 377.63 seconds, with safe temporary login
stage/status instrumentation only. The final sequential groups cover all 1,027
current tests; the initial combined run remains recorded as a failure. Treat the
unreproduced login failures as an unresolved flake risk for required remote CI.
Added 29 worker/CLI/binding cases overall; no browser test source was changed.

No commit/push or live LLM/cloud call; remote PR tests/delivery remain pending.
