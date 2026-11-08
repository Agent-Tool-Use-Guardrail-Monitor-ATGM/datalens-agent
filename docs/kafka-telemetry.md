# Kafka telemetry delivery

Step 7.6 is in progress. The team owns the Kafka broker and topic provisioning;
this repository contains only the producer and delivery-buffer implementation.
It does not create a Kafka service, topic, or broker test container. CI uses
mocked producers for delivery tests and PostgreSQL for persistence tests.

The worker writes redacted events to `agent_event_outbox` in its own PostgreSQL
database, in the same transaction as the corresponding state/evidence change.
`agent_run_evidence` is the separate local execution journal used for recovery
and diagnostics. The outbox is a delivery buffer, not an analytics database.
Migration 0008 is required before running the updated worker/publisher. Stop old
workers before deployment; old attempts are not backfilled as feature-rich events.

An independent publisher process reads pending events and sends them to
`agent.events.v1`, keyed by session ID, with stable event IDs and per-session
sequence numbers. Configure the teammate-provided Kafka connection through the
`KAFKA_*` settings documented in `.env.example`, then run:

```powershell
python -m statistics_agent.publisher --env-file .env
```

If delivery fails, the publisher prints a redacted warning, keeps pending rows,
and retries with backoff from 1 to 30 seconds. The agent does not contact Kafka
for each computation. Without connection settings, publisher configuration fails
with a fixed diagnostic; workers can still populate the outbox.

Delivery is at least once: a broker acknowledgement followed by a database failure
can cause duplicate delivery. Consumers must deduplicate by event ID and use
`read_committed`. All replicas sharing one outbox must use the same stable Kafka
transactional ID and broker/topic. Different outbox databases require distinct
transactional IDs. The publisher requires a direct or session-pooled PostgreSQL
connection for its session advisory lock.

Pending rows survive application restarts and conversation deletion. Published
rows can be removed by the bounded cleanup command below. The existing pending
limit is 10,000 events. At capacity, writes
requiring more telemetry fail rather than drop evidence; new work can be blocked.
The user has not yet confirmed this capacity behavior, so step 7.6 remains open.

## Published-event retention job

Run one cleanup batch using the same application image/database configuration:

```powershell
python -m statistics_agent.cleanup --env-file .env
```

Defaults: retain published events for 7 days; delete at most 1,000 older rows per
invocation. Age is measured from `published_at` using the database clock. Pending
events, recent published events, run evidence, conversations and results are not
deleted. The database schema gate must pass first. A partial index supports
published-date lookup; row locks with `SKIP LOCKED` allow overlapping job runs
without waiting for each other's selected rows.

Optional bounds are `--retention-days 1..365` and `--limit 1..1000`. The command
prints only a deletion count and exits; database/configuration failure returns a
nonzero exit code with a redacted message. Failed transactions roll back and can
be retried. This does not contact Kafka or require broker credentials.

Schedule this command hourly in the deployment scheduler. At default settings,
one hourly invocation can delete up to 24,000 eligible rows per day; increase
frequency if the retained backlog grows. No scheduler or cloud resource is
created by this step, and cleanup is not started inside API/worker processes.
Without an external schedule or manual invocation, no automatic cleanup occurs.
The job deletes database delivery copies after acknowledgement; it neither
changes Kafka retention nor proves downstream consumer processing.

Tests cover mocked failures/retries, stable-ID redelivery, ordering, ownership of
the publisher, atomic persistence and redaction. Real teammate-broker connectivity,
TLS/ACL configuration and end-to-end consumer compatibility remain unverified.

8 November verification after Docker Desktop was started: **106 tests passed**
in 60.55 seconds (135 warnings). The focused gate covers cleanup, telemetry,
mocked publisher delivery, PostgreSQL outbox/migrations, worker and terminal
completion. Cleanup checks prove bounded oldest-first deletion, preservation of
pending/recent events and run evidence, and skipping concurrently locked rows.
Ruff, formatting, dependency and whitespace checks pass. An earlier attempted
database gate was interrupted while Docker was stopped; the later gate above
supersedes that blocked check. Browser tests and a real Kafka broker were not
run in this follow-up. No commit or push was performed.
