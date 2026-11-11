# Plan 3.8.4–3.8.9: Identity events and hosted connectivity

**3.8.3 prerequisite, 9 November 2026:** pinned Keycloak 26.8.1 native password
sign-out checkbox defaults unchecked. Step 3.8.4 must enforce logout of other
identity sessions regardless of that choice, in addition to durable revocation
publication; RabbitMQ alone cannot invalidate still-active refresh tokens.
Consume canonical `UPDATE_CREDENTIAL` password detail once, avoiding duplicate
publication from deprecated password events. Test checked and unchecked console
flows and administrative resets. The separate identity repository's
`docs/frontend_pkce_handoff.md` records pinned primary sources and opt-in evidence.

Approved design, 8 November 2026. Planning on the reused
`fix/bearer-authentication-migration` branch. Profile foundation 3.8.1 is delivered
per user confirmation; current 3.8.2 adds only JWT verification prerequisites.
This document expands the [migration sequence](bearer-authentication-migration.md),
not a new top-level plan. No broker, Keycloak provider, consumer, CORS middleware
or Azure resource is created by this documentation update.

## Service ownership and flow

`datalens-identity` owns Keycloak accounts, credentials, identity sessions,
profile/security changes and their durable integration publisher. RabbitMQ has a
dedicated identity scope. `datalens-agent` owns its own display projection,
consumer receipts/revisions/tombstones and minimal local revocation metadata.
Kafka `agent.events.v1` remains agent lifecycle telemetry; identity messages must
not use its seven-event envelope or topic.

Planned logical process names are `identity-publisher` and
`agent-identity-consumer`; the broker service is `rabbitmq`, owned by the separate
`datalens-messaging` infrastructure repository. That repository owns broker storage,
topology, permissions and deployment; identity owns its source/outbox/publisher
and schemas, while the agent owns its consumer/projection. The earlier
`identity-rabbitmq` planning name is superseded. Split 3.8.4.4 into broker foundation
(3.8.4.4.1, messaging) and publisher (3.8.4.4.2, identity); 3.8.4.5 coordinates
transport acceptance. This boundary revision does not provision a broker. The agent
consumer shares the agent repository/image/database, like its API/worker/Kafka
publisher, not a new account service. These names are planning contracts, not
already accepted runtime `SERVICE_NAME` values or provisioned containers.

After account creation commits, the identity outbox retains a `user.created`
snapshot. Publisher sends it to RabbitMQ. Agent consumer upserts the profile and
receipt/version state transactionally, then acknowledges. No first agent request
is required. The same flow updates username/name/email changes; frontend token
refresh alone is not the database synchronization mechanism.

No profile-image attribute, avatar URL, upload, storage, rendering or external
image fetch is planned. Profile payloads whitelist only approved display fields.
The frontend/agent never handle user passwords or persist refresh tokens.

## 3.8.4 — Identity producer and RabbitMQ

Implement in the identity repository on a task-named `fix/` branch after requesting
that step. Prefer a pinned Keycloak Event Listener SPI provider with its own JPA
outbox/revision entities participating in the identity mutation transaction.
This is new integration code: stock Keycloak is not already a RabbitMQ publisher.
Verify supported custom entity packaging and transaction enlistment against the
pinned image before choosing exact table/schema names. Do not patch, delete or
depend on undocumented Keycloak table layouts. If atomic durable intent cannot
be proven, do not substitute an in-memory or after-commit-only send and claim
reliable delivery. Revisit the producer design before implementing the consumer.

The listener maps both user and administrator changes; admin-created accounts
must work although self-registration remains disabled. Capture canonical
post-mutation display snapshots and durable deletion/security intent, never raw
administrator representations, passwords, reset links or token payloads.
Role changes that require invalidating old permissions produce revocation
signals, not a copied authoritative role directory in the agent.

Publisher is a separate process/entry point owned by the identity integration,
not a new credential authority. It reads only the integration outbox, uses broker
publisher confirms and retries unacknowledged sends with stable event IDs.
It does not hold the identity mutation transaction open across broker I/O.
Pending events survive restarts and account deletion. Broker failure must not
lose changes; if the durable outbox cannot accept a required change, surface the
failure rather than falsely acknowledge delivery. Define bounded backlog,
published-row retention and alarms when implementing this step.

Proposed transport names (finalized/tested in 3.8.4): durable topic exchange
`identity.events.v1`, durable agent queue `agent.identity.v1`, and restricted
dead-letter queue `agent.identity.dead.v1`. Keep these outside the Kafka namespace.
Use persistent messages, publisher confirms, manual consumer acknowledgements,
bounded retries and an operator-visible dead-letter policy. One queue may have
competing consumers because projection/revocation state is in the shared agent
database, not only the consuming replica's memory. Durable is not equivalent to
high availability: managed/self-hosted broker, storage and replication are Plan 10
choices, not an implied production guarantee from a single container.

## Proposed identity event contract

Separate versioned envelope: `schema_version`, stable `event_id`, `event_type`,
trusted `issuer`, `subject`, `occurred_at` and bounded typed `data`. Identity
events have no fabricated agent run/request IDs. Validate issuer and body size;
accept messages only from the dedicated authenticated/authorized publisher.

| Event | Data and effect |
|---|---|
| `user.created` | Canonical optional display name/username/email plus source profile revision; insert profile without an agent request |
| `user.profile.updated` | Full display snapshot and increasing profile revision; missing fields explicitly clear prior values |
| `user.session.revoked` | Session ID, conservative revocation time and reason; reject that session's tokens on new requests |
| `user.sessions.revoked` | User token-not-before cutoff and reason such as logout-all/password change; reject older tokens |
| `user.disabled` / `user.enabled` | Ordered account-state revision; disabling denies new requests; enabling must not restore old revoked sessions/tokens |
| `user.deleted` | Clear display PII, retain minimal deletion/version tombstone and deny new requests; never resurrect from an older delivery |

Identity owns source revisions. Processing time is not a source version. Use
separate profile and security ordering rules: a later security event must not
discard an unseen profile update. Revoked-session sets are merged idempotently;
do not drop a revocation for session A just because a newer event for session B
arrived first. Account deletion dominates stale create/update deliveries.
Account reenabling does not clear historical token cutoffs. Specify/test cutoff
precision against JWT second-resolution `iat`; conservatively reject ambiguous
same-second tokens and retry fresh authentication rather than admitting old tokens.
Require a trustworthy session identifier when session-level rejection is enabled.

No plaintext secrets, access/refresh tokens, password hashes, model prompts,
datasets or image attributes are allowed. Email is display PII, so restrict broker
permissions, logs, retention and operator access; it is not an authorization key.

## 3.8.5 — Agent projection, revocation and recovery

1. **3.8.5.1 Durable projection:** validate the wire contract, record receipts
   and apply ordered profile and revocation updates in PostgreSQL.
2. **3.8.5.2 RabbitMQ consumer:** deliver messages to the projection and
   acknowledge after commit. Add retry, quarantine and operational signals.
3. **3.8.5.3 Backfill and revocation integration:** reconcile existing identities
   from supported source snapshots and connect the local revocation read to
   verified access. Complete retention and replay acceptance before cutover.

### Durable projection

Focused verification passed 39 checks, including PostgreSQL concurrency,
rollback, serialization retry, migration preservation and schema parity.
The non-browser suite passed **1,126 tests**. Seventeen browser tests
remain for the required Linux PR gate.

Migration `0010_identity_projection` preserves existing profiles and adds
an inbox keyed by event ID, per-owner revision and account state, and indexed
session revocations. The bundled wire schema matches the identity schema at
`e1f3fc8`. Receipts store a SHA-256 digest without retaining message bodies.
A repeated ID with changed bytes is rejected. Every new delivery reserves its
receipt, locks the exact issuer/subject row, then updates profile or session
state in the same transaction. Claim-based display writes use that same owner
lock and defer to source-owned profiles.

Account deletion permanently suppresses the subject's display data. Disable
and enable events order by status revision. Cutoffs only increase, and older
session revocations still apply to their individual session IDs. The local
revocation read is available for a verified access token.

The migration widens stored display name and username to the source limit of
256 characters. Blank or nonprintable display fields project as NULL. Legacy
claims retain their existing input limits. Apply the migration before running
the new code and upgrade profile writers before enabling message delivery.
Keep the expanded schema on live rollback to retain receipts and tombstones.
Disposable downgrades refuse to truncate existing display data.

Follow [database concurrency/recovery](database-concurrency-and-recovery.md) for
the exact lock-order, source-version and whole-transaction retry requirements.
Do not confuse atomic profile upsert with stale-event protection, or a process
mutex with cross-container database coordination.

Add explicit source-version/inbox/tombstone metadata through new migrations.
Migration 0009 remains the original additive display table. Token claims must
not overwrite newer event-owned profile data. Never use username/email to merge user identities.
Owner identity stays the exact `(issuer, subject)` pair.

Apply a delivery's profile/security effects and dedup receipt in one transaction;
acknowledge after commit. If commit succeeds but acknowledgement is lost, retry
must be harmless. Retry transient database failures; quarantine malformed or
unsupported messages with safe diagnostics. Do not silently ignore security
events. Alert on delivery lag, reconnect failures, backlog and dead letters.

Bootstrap existing users from an identity-owned supported snapshot/backfill
process; then reconcile safely with the event stream using source revisions.
Use the same versioned contract, not an agent read of identity database tables.
Define tombstone/revocation/inbox retention against maximum token lifetime and
the permitted replay/backfill window before deleting metadata. Late messages
must never recreate deleted PII after tombstone cleanup.

Authentication still requires a valid signed access token. An event cannot grant
access without one. After JWT checks, use a small indexed local revocation read
for new requests/admissions, shared across API replicas. Do not put that metadata
into `agent_user_profiles` or require a display row to establish authentication.
No synchronous Keycloak/introspection call is added per ordinary request.

Healthy event delivery should reject a revoked token promptly after commit to
the agent database. There is no zero-delay guarantee. During broker/publisher/
consumer outage, known revocations still apply; unknown changes are bounded by
the 300-second access-token lifetime. Keycloak must actually terminate affected
refresh/SSO sessions so those old sessions cannot mint replacement tokens. Local
revocation-storage failure must not silently bypass known required checks.
Verify the outage behavior and recovery before claiming revocation support.

## Session actions and accepted jobs

Frontend login/refresh uses Keycloak authorization-code PKCE and rotating refresh
tokens held only in memory. Logout clears browser tokens/protected caches and
uses supported Keycloak logout; Account Console provides session/account actions.
Logout-all must terminate all targeted identity sessions. Password changes must
terminate the other sessions and require current-browser fresh tokens or login;
prove actual behavior rather than assuming password modification does this by
default. Admin password reset/disable/delete must also have tested mappings.
Username edits update the projection; they do not change subject/ownership.

Accepted jobs keep their immutable per-run authorization grant: up to 10 minutes
queued, then the configured execution bound (120s default, at most 600s). Logout,
password change and account disable/delete reject future requests/admissions but
do not cancel existing bounded work. No user/profile/Keycloak check is added at
every model/tool call. Existing job leases, pinned scope and dataset/run checks
remain mandatory; this policy does not weaken concurrent-worker fencing.

If a dataset/run was actually deleted, stop safely through its normal lookup/
commit path, preserve only still-existing authorized results and never recreate
deleted records. Emit the existing incomplete terminal event when durable run
state permits it; otherwise record a safe operational diagnostic. Do not promise
to write an event/history row through a deleted parent. Deleting an identity is
not itself a complete data-erasure policy; that needs a separate retention design.

## 3.8.7 / 3.8.9 — Public addresses, CORS and Azure

Keep per-environment values for frontend origin, agent HTTPS base URL, canonical
Keycloak HTTPS issuer/account/logout endpoints, exact OAuth callback/post-logout
URLs, and secret AMQPS connection settings. Do not hard-code localhost or Compose
service names into hosted builds. No actual hostnames have been provided/provisioned;
examples such as `https://agent.example` are placeholders, never live defaults.
Frontend public settings contain no broker/client/model secrets. Validate schemes,
hostnames and trusted certificate chains; do not disable hostname/TLS verification.

| Connection | Browser/network rules |
|---|---|
| Frontend browser to agent | Exact configured frontend origins, explicit methods/headers including Authorization and Content-Type, required correlation/idempotency headers only when contracts define them; no wildcard, `allow_credentials=False` |
| Frontend browser to Keycloak | Exact frontend web origins plus exact callback/logout registration on public `agent-web`; no browser client secret |
| Agent to Keycloak | HTTPS discovery/JWKS from configured canonical issuer; server authentication/authorization where required, not a CORS allowlist |
| Identity publisher and agent consumer to RabbitMQ | AMQPS, distinct least-privilege credentials/vhost permissions; no browser access or public management UI |
| Backend to its own PostgreSQL | Private connectivity preferred; if a public endpoint is unavoidable, verified TLS, narrow firewall rules and separate database credentials; never browser access |

CORS controls browser cross-origin access, not service identity. Anyone outside
the browser can construct an Origin header. JWT/service credentials and network
policy protect actual endpoints; do not claim that only the named service can
reach a public endpoint because CORS is configured. Requests without an Origin
still require normal authentication. Preflight must not require an access token.

Use external HTTPS ingress only for intended browser-facing agent/identity APIs.
Protect administrative endpoints and keep management/metrics ports restricted.
Keep local/test issuer/ports in their own profiles rather than loosening their
validators globally. RabbitMQ uses AMQP/TCP, not HTTP CORS; its hosted transport
and persistent storage must be designed explicitly. No database or broker should
scale to zero without supported durability/recovery. Do not embed either in the
agent application image. Plan 10 owns endpoint provisioning, certificate/secret
delivery, ingress/firewalls, persistence, cost review and deployment acceptance.

## Acceptance and implementation status

Plan tests for admin-created/self-service users, registration before first agent
request, profile updates without token refresh, old JWT overwrite attempts,
duplicate/out-of-order delivery, separate session revocations, deletion/backfill,
producer transaction rollback, broker/database outage, restart recovery, stale
messages and shared enforcement across API replicas. Verify password/logout-all
refresh denial separately from old access-token rejection. Verify exact-origin
preflight, disallowed origins, wrong audience/issuer/azp, endpoint/TLS validation,
and accepted-job completion without identity calls. No image fields may cross
contracts, forms, events or storage.

These capabilities are planned, not tested or implemented on the current branch.
The 3.8.1 test evidence in the migration plan proves only the additive profile
foundation. Keep its historical evidence distinct from future integration gates.

References: [Keycloak event transaction contract](https://www.keycloak.org/docs-api/latest/javadocs/org/keycloak/events/EventListenerProvider.html),
[Keycloak extension compatibility](https://www.keycloak.org/docs/latest/server_development/#_events),
[Keycloak token/session configuration](https://www.keycloak.org/docs/latest/server_admin/index.html#_timeouts),
[RabbitMQ reliability](https://www.rabbitmq.com/docs/reliability),
[FastAPI CORS](https://fastapi.tiangolo.com/tutorial/cors/),
[Azure ingress](https://learn.microsoft.com/en-us/azure/container-apps/ingress-overview).
