# Plan 3.8.5 — Database concurrency and deadlock recovery

Planning addition, 8 November 2026. This expands the existing identity
[integration plan](identity-events-and-connectivity-plan.md); it is not another
service, broker or current database migration. Current implementation step 3.8.2
is a JWT verifier with no database access. Implement the following database work
only in its requested producer/consumer/grant steps.

## Existing protections to preserve

- Profile composite primary key and `INSERT ... ON CONFLICT DO UPDATE`: atomic
  same-owner create/update, already tested with concurrent writers. It prevents
  duplicate profiles but does not yet reject stale source revisions.
- Conversation admission/claim/checkpoint/terminal paths use row locks and live
  attempt/lease checks. Dataset reads needed for durable verified results use
  parent/child locking; ownership stays issuer/subject. Do not remove these
  consistency checks while removing browser-session dependencies.
- Worker claim and outbox capacity use short transaction advisory locks.
  Kafka publisher ownership uses a separate session lock, without keeping a
  database transaction open during broker I/O. Preserve that distinction.
- Published-event/auth cleanup use bounded selection with `SKIP LOCKED`; pending
  events and active sessions are excluded by their predicates. A skipped row is
  deferred, not considered deleted or successfully processed.
- Database connection, pool and statement timeouts are bounded. The JWT key-cache
  asyncio mutex is not a database lock and cannot coordinate multiple containers.

No existing concurrency failure was reproduced in this review. Do not add global
table locks, a new Redis/distributed lock service, blanket Serializable isolation
or arbitrary retries to every repository method without evidence.

## Identity source transactions — step 3.8.4

The Keycloak mutation, integration source revision and identity outbox insertion
must commit or roll back together through the supported provider transaction.
Serialize per-owner revision changes with a database row/conditional atomic
update, never a Java/Python process-only mutex. Handle first-owner-row creation
with a unique constraint/upsert rather than read-then-insert races.

The source publisher uses short bounded claims and broker confirms outside the
mutation transaction. Reclaimed claims retain event IDs/order; neither lease
expiry nor retry may create a new logical event or lose an unconfirmed event.
Do not retain a database transaction while contacting RabbitMQ or a remote API.

## Agent consumer transactions — step 3.8.5

Process one event/owner per transaction initially. Fix one lock order for every
consumer path: reserve/check unique inbox receipt, lock/create owner integration
state, then update profile/revocation/tombstone rows in deterministic key order.
If multiple child/session rows are needed, sort keys before acquiring locks.
No other path may acquire these resources in reverse order. Document the exact
SQL/table order when implementing the schema, including its interactions with
cleanup and tombstone compaction.

Do not perform an unlocked read followed by an unconditional write. Apply profile
snapshots only when the source profile revision is newer, using conditional upsert
or a locked owner row. Persist the accepted revision and deduplication receipt in
the same transaction. A missing profile field clears it only in an accepted full
snapshot, not because an unrelated security event omitted display fields.

Profile/security streams have distinct ordering rules. Merge revocation cutoffs
monotonically and session-revocation records idempotently; a newer event for one
session does not erase or suppress a revocation for another. Account status uses
its own source version, reenabling does not restore revoked tokens, and deletion
tombstones prevent an older create/profile event from recreating cleared PII.

Commit before acknowledging RabbitMQ. A crash after commit but before ack must
produce harmless redelivery. A retry must repeat the entire transaction, not just
the last failed statement or broker acknowledgement. Keep network/publisher/model
calls outside these transactions. No full user/profile lookup is added at every
model/tool step; existing job/data fences remain as already designed.

## Deadlocks and transient failures

Prevent cycles through stable lock order and small transactions; do not try to
create deadlocks in production. PostgreSQL may abort a conflicting transaction
anyway. When consumer/source integration is implemented, retry known deadlock
(`40P01`) and serialization (`40001`) failures at most three attempts with bounded
backoff/jitter, starting a fresh transaction each time. Preserve the stable event
ID and never acknowledge an uncommitted effect. Do not retry authorization/schema/
validation errors as transient concurrency failures.

Configure/test a short transaction-local lock timeout for event handling (target
2 seconds, below the existing statement deadline). Treat lock exhaustion and
retry exhaustion as retryable delivery failure with observable backlog, not
successful processing or a tight infinite requeue loop. Exact driver error
classification and delay budget must be implemented and tested in 3.8.4/3.8.5.
Do not change global server timeouts without checking all worker/cleanup paths.

Never wrap model calls, tool execution or run orchestration in a generic database
retry loop: that could repeat paid calls or side effects. Retain current unknown/
incomplete outcome handling for interrupted durable execution. Read/commit paths
must not recreate actually deleted datasets/runs, bypass leases or weaken owner
checks to recover from contention.

## Required proof before delivery

Use disposable PostgreSQL with two or more real connections to prove concurrent
first profile creation, same-owner old/new revisions in either order, distinct
owners progressing independently, duplicate commit/ack-loss, deletion/update races,
distinct session revocations, rollback-before-ack and lock/retry exhaustion.
Use barriers/events rather than timing assumptions. A deliberately induced
deadlock is permitted only in the isolated test fixture to prove bounded whole-
transaction retry; never against development/production data. Verify the retry
does not resend a model/tool operation or acknowledge a failed transaction.

The existing profile tests remain useful but do not prove these future revision/
consumer rules. No new database lock/retry framework is implemented in 3.8.2.

References: [PostgreSQL locking/deadlocks](https://www.postgresql.org/docs/17/explicit-locking.html),
[transaction isolation and retry](https://www.postgresql.org/docs/17/transaction-iso.html).
