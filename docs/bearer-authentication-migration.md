# Plan 3.8: Bearer authentication and minimal user profiles

**Current, 9 November 2026:** user confirms 3.8.2 delivery. Additive 3.8.3 public
client tooling is prepared in the separate identity repository on
`fix/bearer-authentication-migration`; 109 identity tests passed in 377.38 seconds.
Required PR tests/delivery remain pending. No agent runtime
change is included. See identity `docs/frontend_pkce_handoff.md` in that repository
for exact callbacks, administrator setup and the native password sign-out limitation.
The older current-step statements below are historical. Stop before 3.8.4.

3.8.4 must enforce password-change logout of other identity sessions even when
the native checkbox is unchecked, then durably publish its revocation event.
Use password `UPDATE_CREDENTIAL` semantics without double-publishing deprecated
password events. A RabbitMQ event alone cannot invalidate a live refresh session.

Approved 8 November 2026. This migration supersedes the earlier target of storing
browser sessions and OAuth tokens inside the statistics service. Implement one
numbered implementation step per PR; existing runtime behavior changes only at
cutover. User confirmed 3.8.1 was merged/pulled and explicitly asked to reuse
`fix/bearer-authentication-migration`. Current authorized step is **3.8.2**;
stop before 3.8.3. Preserve prior planning changes. Later implementation normally
uses task-named `fix/` branches unless the user explicitly requests reuse.

**Historical 3.8.1 verification:** prepared on `fix/bearer-authentication-migration`
(renamed before push), required
PR tests/delivery pending. Focused profile/migration/auth-storage/outbox gate:
55 passed. Full non-browser regression: **1,077 passed, 17 browser tests
deselected**, 514 warnings, 149.66 seconds. Ruff/format/dependency/whitespace
checks passed. Disposable PostgreSQL only; no real identity/frontend/provider or
cloud deployment was changed. Browser tests were not rerun. No commit or push.
Its delivery is now accepted from user merge/pull confirmation.

**Plan-only follow-up:** the user confirmed RabbitMQ identity integration,
event-driven revocation with 300-second expiry fallback, Account Console actions,
accepted-job completion, no profile images and hosted/exact-CORS configuration.
The numbered target now runs through 3.8.9. Local links/code fences in seven key
plan files and service Git whitespace were checked. Runtime code and the verified
3.8.1 scope were not expanded; no runtime tests were rerun for documentation.

## Confirmed target

- Keycloak owns users, credentials, roles, identity sessions and token issuance.
- The frontend uses a public OAuth client, authorization-code flow with PKCE S256,
  and memory-only token storage. It refreshes tokens through Keycloak and sends
  access tokens as `Authorization: Bearer ...`. Reload may require a new Keycloak
  SSO redirect; do not promise silent login across browser privacy restrictions.
- The agent is a resource server. It verifies signature, issuer, audience,
  authorized party, expiry and required roles on every protected request. It does
  not store access/refresh tokens, OAuth login attempts or browser sessions in the
  final architecture. No gateway or per-request identity database access is
  introduced. RabbitMQ distributes identity changes, not request credentials.
- API audience and role namespace remain `agent-api`; the future browser client
  is `agent-web`. Allowed authorized parties must be validated separately from
  the API audience. Do not turn the existing confidential client into a public
  client or relax audience checks to make frontend tokens work.
- Preserve current RS256, verified-email, admin-includes-user, ownership and
  bounded JWKS-trust rules. Apply consumed RabbitMQ revocation metadata to new
  requests/admissions. While delivery is delayed, previously issued tokens can
  remain accepted until expiry (maximum 300s, zero expiry leeway). Event-driven
  rejection is fast when healthy, not a guarantee of immediate revocation.
- The agent stores a display projection only: exact issuer/subject, nullable
  display name, username, email and profile-update timestamp. Email/name are not
  identity keys, login status or authorization authority. Missing display claims
  remain absent, not invented. Registration/admin-created-account and subsequent
  profile changes must update this projection through identity events, even if
  the user never calls the agent. No profile pictures, avatar fields/uploads,
  image URLs or avatar storage. Do not expose a public user directory.
- Agent-owned integration tables may contain deduplication/version/tombstone
  metadata and minimal revocation cutoffs/session identifiers. These are separate
  from display profiles and contain no passwords, tokens or login sessions.
  API replicas use shared agent storage for revocation decisions; a consumer
  updating only its own process cache is insufficient for other replicas.
- Accepted background work receives a server-created per-run authorization grant:
  actor, permitted scope, admission time, queue/start deadline and bounded execution
  deadline. No token is retained. A job can wait at most 10 minutes, then execute
  within the pinned profile limit (120s default, maximum 600s). Logout, token
  expiry, password change and account disable/delete do not revoke an already
  admitted grant. No identity/profile/revocation lookup is added at each model or
  tool step. Existing lease, scope, resource and terminal-write checks remain.
  Actually deleted datasets/runs must fail safely, never be recreated by a worker.
  Fresh requests still require valid, nonrevoked tokens. Grants cannot authorize
  unrelated runs or broaden dataset scope. Account deletion is not an implicit
  instruction to erase every dataset/history row; erasure needs its own policy.
- RabbitMQ is dedicated to identity integration; Kafka remains agent telemetry
  only. The identity producer and agent consumer require separate contracts,
  credentials and durability logic, not the seven agent-event envelope.
- Account/password/username/session management uses Keycloak Account Console,
  reached through frontend buttons. No custom password API or credential form.
  Enable only intended username editing and prove the password-change/logout-all
  behavior in the pinned Keycloak version; it is not assumed from defaults.
- Hosted connections use explicit HTTPS service URLs and AMQPS broker settings.
  Browser CORS has exact frontend origins; it is not service authentication.
  See the [identity events/connectivity plan](identity-events-and-connectivity-plan.md).
- PostgreSQL jobs/outbox and Kafka telemetry remain unchanged in responsibility.
  Auth cleanup scheduling is paused pending this migration, not moved into Keycloak.

## Numbered implementation sequence

### 3.8.1: Additive profile foundation — current step

Add strict display contracts, `agent_user_profiles`, a concurrency-safe internal
store and migration 0009. Composite primary key `(issuer, subject)` supplies the
only profile index. Email/username are nullable and deliberately nonunique. No
foreign keys rewrite existing resource ownership or require profiles for old data.
No passwords, roles, tokens, login status, session IDs or credential fields exist
in this table. Validate bounded printable display text, but do not treat stored
email as proof of ownership or a delivery address verified independently here.

The internal upsert accepts only claims supplied by trusted application code; a
Pydantic instance alone does not prove token verification. Do not wire it to
unverified request bodies. Missing fields replace old display fields with NULL.
The current 3.8.1 store has no source revision: it reflects the last processed
snapshot, so an older JWT could overwrite a newer name if wired indiscriminately.
This is a known limitation of this unused foundation, not the planned sync policy.
Step 3.8.5 must add ordered event projection before wiring runtime synchronization;
do not repeatedly persist JWT display claims over event-owned profile values.

This step adds no HTTP route and no automatic claim-to-profile synchronization.
The existing cookie/session authentication and worker continue unchanged until
later steps. Verify isolation, nullable fields, idempotent/concurrent updates,
rejection of credential fields, and preservation of old tables/data on upgrade.

### 3.8.2: Independent resource-server verifier

CI follow-up: collection-time temporal parameters in the new test could age into
valid tokens during the full suite. The minting helper now anchors relative time
claims together; production verification is unchanged. 75 resource/legacy tests
and two simulated collection-delay runs (44 passed each at 360s/660s) pass, with
explicit 300/301-second boundary coverage. See the verifier record for the exact
red/green proof. No next migration step is implemented by this correction.

Complete locally; required remote PR tests/delivery pending. See
[usage and security boundaries](resource-server-verifier.md). No HTTP/worker
cutover or new database mutation is included in this step.

Evidence: 132 focused checks; final non-browser 1,119 passed and browser 17 passed
in sequential groups. The first browser group had one login 503 (16 passed);
isolated/final repeats passed with safe diagnostics, without test/timeout changes.
The unreproduced failure remains a documented flake risk. Ruff/format/dependency/
whitespace and plan-link checks pass. No commit/push. Stop before 3.8.3.

Separate JWT/JWKS validation from confidential-client token exchange and encrypted
session storage. Resource-server configuration must require no OAuth client secret
or token-encryption keys. Return verified request identity and separately bounded
profile claims. Validate `aud=agent-api` and allowed `azp=agent-web`, including
multi-audience cases, without loosening issuer/signature/time/key-trust checks.
Keep the old runtime operational until cutover; no optional-session workaround
that silently grants cookie-era authorization to a bearer request.

### 3.8.3: Public frontend client and frontend handoff

In a separate `datalens-identity` `fix/` branch, configure public `agent-web` with
exact callback/logout/origin allowlists, mandatory PKCE S256, no client secret,
implicit flow or password grant. Issue `agent-api` audience/client-role claims and
the approved display claims; use required built-in/explicit scopes and mappers.
Preserve the confidential client's current behavior during the compatibility window.
Never allow wildcard production redirects or persist browser tokens in localStorage.

Configure Account Console access for password/username changes and session
management. Keep administrator-only APIs/privileges out of the frontend. Verify
ordinary logout, logout-all, password-change invalidation, username edits and
display claims against the actual pinned runtime. No profile-image capability.

Update teammate plans and concrete request examples: memory-only tokens, refresh
coordination, refresh before expiry, bounded retry after 401, login on failed refresh,
403 without refresh loops, logout, CORS and safe error rendering. Render profile
values as untrusted text, never raw HTML. Frontend source
is not in this workspace; do not claim implementation or browser cutover there.

### 3.8.4: Identity events and RabbitMQ delivery

Implement the identity-owned transaction-aware listener/outbox/publisher, dedicated
RabbitMQ exchange/queues and versioned registration/profile/security contracts in
the [integration plan](identity-events-and-connectivity-plan.md). Cover both
administrator account creation and supported self-service events. A RabbitMQ
installation by itself does not produce Keycloak events. Identity changes must
commit with durable delivery intent; retries cannot lose changes or block login
on a broker network call. No direct deletion of Keycloak-managed tables.

### 3.8.5: Agent profile consumer and local revocation metadata

Follow the [database concurrency/recovery requirements](database-concurrency-and-recovery.md):
conditional source-version writes, transactional inbox/state effects, consistent
lock ordering and bounded retries for whole idempotent database transactions.
Never retry model/tool operations through a generic transaction wrapper.

Consume identity events, updating profiles/version state and inbox receipts in one
agent database transaction; acknowledge only after commit. Add ordered profile
projection, delete tombstones and minimal shared revocation metadata. Registration
must create the profile without an agent request. Handle events delayed before
registration, duplicates, reversals, broker outages and existing-user backfill.
An old token/profile event must not undo a newer username/deletion/security event.
Do not silently drop independent session revocations because another newer event
was already seen. No token/password/role authority is stored in display profiles.

Use `BearerAccess.verify_access()` at the 3.8.7 HTTP cutover so JWT verification,
the required `sid`, and the indexed local revocation read share one entry point.
See the [revocation check and backfill sequence](identity-events-and-connectivity-plan.md#bearer-revocation-check).

### 3.8.6: Accepted-job grants independent of browser sessions

**3.8.6a — Admission and storage.** Migration `0011_run_admission_grants`
adds a versioned, nonsecret actor snapshot and `queue_expires_at`. The job's
run ID links the grant to its pinned datasets and execution configuration.
Admission writes the run, user message, job and grant in one transaction.
The queue deadline is ten minutes from database admission time. Idempotent
retries return the existing run without renewing the deadline or replacing
the original actor permissions.

Existing jobs retain their session reference and have empty grant fields.
This step keeps the legacy worker behavior. Startup requires the binary's
matching schema revision. If rolling back, use the current worker to drain
granted jobs before downgrading. The migration refuses to drop live grants.
Focused verification passed 69 migration, admission and legacy worker checks.

**3.8.6b — Worker enforcement.** Persist the execution deadline at claim
time, authorize only the granted run and its pinned scope, and terminalize
expired queued jobs before dispatch.

Migration `0012_worker_execution_deadline` stores the claim deadline using
PostgreSQL time and the pinned run timeout. Heartbeats cannot extend it.
Recovery finalizes expired work as incomplete and keeps verified partial
results. Queued grants that expire finish without an execution attempt and
release their conversation. A fresh submission uses a new idempotency key.

Granted jobs use the original actor snapshot, job ownership and live attempt
at authorization, checkpoint and completion. Browser logout and token expiry
do not interrupt a granted job. Older jobs without grants still use the
legacy session check. During rollback, drain claimed jobs with the matching
worker before downgrading 0012. The migration refuses to remove a deadline
while a claimed job is present.

Preserve lease, ownership and checkpoint/terminal fences, including partial
results. Use database time for execution bounds. Queue expiry must release
the conversation without dispatching work. Worker restarts must honor the
original execution deadline and preserve the interrupted operation outcome.

During rollout, drain legacy queued/running jobs using old workers before switching
admission/workers. Do not fabricate grants from session references or stored user
profiles. Document failure/recovery and idempotency if draining cannot complete.

### 3.8.7: HTTP cutover, CORS and frontend integration

Require a single well-formed Bearer header on protected routes; cookie credentials
alone must not authorize. Enforce verified roles and existing resource ownership.
Check consumed revocation metadata after JWT verification, before admitting new
requests/work. Define the own-profile HTTP contract without exposing other users.
Read event-owned display profiles rather than overwriting them from stale JWTs.
Valid users may reach the agent before profile delivery; expose a clear profile-
synchronizing state without using profile existence as authentication authority.
Replace cookie/CSRF browser API contracts and security schemes with bearer contracts;
configure exact trusted CORS origins without cookie credentials. No authorization
decision may use display fields or a caller-provided user/role header. Use exact
configured frontend origins, explicit methods/headers and `allow_credentials=False`.
Allow unauthenticated browser preflight, never unauthenticated business requests.
Server-to-server access uses TLS, scoped credentials and ingress/firewall controls;
an Origin header cannot prove which service sent a request.

Switch together with frontend and grant-aware workers. Test fresh login, refresh,
expiry, logout semantics, audience/authorized-party separation, user isolation,
dependency outages, concurrent jobs and missing/stale profile data. Publish an
explicit compatibility/cutover runbook and rollback bounds.

#### HTTP contract and rollout

`AUTH_HTTP_MODE=bearer` is the default. The API reads the realm issuer,
`agent-api` audience, `agent-web` authorized party and exact origins from
[the resource auth settings](../.env.resource-auth.example). Set
`AUTH_CORS_ORIGINS` to a JSON array of deployed frontend origins. An empty
array disables cross-origin reads. The current route accepts GET with
`Authorization` and optional `X-Request-ID`. Browser requests omit credentials
and may read `WWW-Authenticate` and `X-Request-ID` on responses.

`GET /auth/me` returns `issuer`, `subject`, `roles`, `email_verified`,
`profile_status` and `profile`. The profile contains the projected display
name, username, email and update time for that exact issuer and subject.
`synchronizing` means the event-owned profile is pending and `profile` is null.
The browser handles a `401 login_required` with one safe refresh/retry. A `403
access_denied` indicates missing access. A `503 identity_unavailable` indicates
temporarily unavailable signing keys or identity projection storage. The API
never refreshes tokens and exposes the Bearer scheme in development OpenAPI.

Before switching traffic, apply the current agent migration, run grant-aware
workers, configure the Keycloak public client and exact frontend redirects,
and reconcile identity backfill and consumer delivery. Switch API and frontend
traffic together and verify PKCE, refresh, logout and projection lag. The old
login, callback and logout routes are available only in rollback mode.

For rollback, switch the API to `AUTH_HTTP_MODE=legacy` and restore the matching
cookie frontend together. This requires the old Keycloak client, encryption
keys and legacy tables. Keep the current database schema and grant-aware
workers during this application-level rollback. Bearer tokens and cookie
sessions are separate sign-ins. Retiring legacy storage is step 3.8.8.

### 3.8.8: Verify and retire legacy session/token storage

Only after cutover proof and a separately requested contraction step, remove old
login/callback/session/logout handlers, token exchange/refresh/encryption plumbing,
secret mounts, session references and obsolete tests. Remove `auth_login_attempts`
and `auth_sessions` through an explicit migration, never by modifying old migrations.
Do not delete datasets, conversations, messages, results or event evidence.

Before dropping tables, retain the additive schema as the rollback path and record
that old/new clients cannot be rolled back independently after public-client cutover.
After deleting session data, old cookie sessions cannot be restored without a backup
and users must log in again. Keep the outbox cleanup command; Keycloak handles its
own session lifecycle. Service-specific scheduling remains deployment work.

### 3.8.9: Hosted connectivity and deployment acceptance

Implement reviewed per-environment HTTPS origins/issuer/callback/logout URLs and
AMQPS endpoints/secrets, without fixed Docker hostnames in hosted configuration.
Keep local/test profiles separate. Plan 10 owns actual Azure/container/broker
provisioning and persistent storage. Verify the real public-address topology,
certificate/hostname checks, exact CORS, restricted broker/management access,
restarts/backfill and outage recovery before claiming hosted delivery. This step
does not imply a deployment or extra cloud cost has already been approved.

## Verification and delivery

Every implementation step uses a task-named branch, PR and required `tests` gate.
Do not commit directly to main. Migration 0009 is additive: rollback application
code may leave the unused table in place; test downgrade removes only profile data.
The migration has no backfill from email/name, sessions or identity database.

Parent canonical plans are outside this service repository/PR. This tracked document
keeps the approved migration reviewable inside the agent PR. No cloud changes,
identity configuration changes or frontend implementation are part of 3.8.1.

References: [OAuth browser application architecture](https://www.rfc-editor.org/rfc/rfc10017.html#section-6),
[JWT access-token validation](https://www.rfc-editor.org/rfc/rfc9068.html#section-4).
