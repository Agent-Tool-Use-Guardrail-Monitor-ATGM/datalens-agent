# Plan 3.8: Bearer authentication and minimal user profiles

Approved 8 November 2026. This migration supersedes the earlier target of storing
browser sessions and OAuth tokens inside the statistics service. Implement one
numbered step per branch/PR; existing runtime behavior changes only at cutover.
Step 3.8.1 is the only implementation authorized for the current branch.

**3.8.1 status:** complete locally on `fix/bearer-authentication-migration`
(renamed before push), required
PR tests/delivery pending. Focused profile/migration/auth-storage/outbox gate:
55 passed. Full non-browser regression: **1,077 passed, 17 browser tests
deselected**, 514 warnings, 149.66 seconds. Ruff/format/dependency/whitespace
checks passed. Disposable PostgreSQL only; no real identity/frontend/provider or
cloud deployment was changed. Browser tests were not rerun. No commit or push.
Stop before 3.8.2.

## Confirmed target

- Keycloak owns users, credentials, roles, identity sessions and token issuance.
- The frontend uses a public OAuth client, authorization-code flow with PKCE S256,
  and memory-only token storage. It refreshes tokens through Keycloak and sends
  access tokens as `Authorization: Bearer ...`. Reload may require a new Keycloak
  SSO redirect; do not promise silent login across browser privacy restrictions.
- The agent is a resource server. It verifies signature, issuer, audience,
  authorized party, expiry and required roles on every protected request. It does
  not store access/refresh tokens, OAuth login attempts or browser sessions in the
  final architecture. No gateway, broker authentication or per-request identity
  database access is introduced.
- API audience and role namespace remain `agent-api`; the future browser client
  is `agent-web`. Allowed authorized parties must be validated separately from
  the API audience. Do not turn the existing confidential client into a public
  client or relax audience checks to make frontend tokens work.
- Preserve current RS256, verified-email, admin-includes-user, ownership and
  bounded JWKS-trust rules. A valid token does not guarantee immediate revocation
  detection; new requests may remain authorized until expiry (currently 300s).
- The agent stores a display projection only: exact issuer/subject, nullable
  display name, username, email and profile-update timestamp. Email/name are not
  identity keys, login status or authorization authority. Missing display claims
  remain absent, not invented. Do not expose a public user directory.
- Accepted background work receives a server-created per-run authorization grant:
  actor, permitted scope, admission time, queue/start deadline and bounded execution
  deadline. No token is retained. A job can wait at most 10 minutes, then execute
  within the pinned profile limit (120s default, maximum 600s). Logout or token
  expiry does not revoke an already admitted grant. Fresh requests still require
  valid tokens. Grants cannot authorize unrelated runs or broaden dataset scope.
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
Profile data reflects the most recently processed accepted claim snapshot; an
older still-valid token can contain stale names/email. It never governs access.

This step adds no HTTP route and no automatic claim-to-profile synchronization.
The existing cookie/session authentication and worker continue unchanged until
later steps. Verify isolation, nullable fields, idempotent/concurrent updates,
rejection of credential fields, and preservation of old tables/data on upgrade.

### 3.8.2: Independent resource-server verifier

Separate JWT/JWKS validation from confidential-client token exchange and encrypted
session storage. Resource-server configuration must require no OAuth client secret
or token-encryption keys. Return verified request identity and separately bounded
profile claims. Validate `aud=agent-api` and allowed `azp=agent-web`, including
multi-audience cases, without loosening issuer/signature/time/key-trust checks.
Keep the old runtime operational until cutover; no optional-session workaround
that silently grants cookie-era authorization to a bearer request.

### 3.8.3: Public frontend client and frontend handoff

In a separate `datalens-identity` feature branch, configure public `agent-web` with
exact callback/logout/origin allowlists, mandatory PKCE S256, no client secret,
implicit flow or password grant. Issue `agent-api` audience/client-role claims and
the approved display claims; use required built-in/explicit scopes and mappers.
Preserve the confidential client's current behavior during the compatibility window.
Never allow wildcard production redirects or persist browser tokens in localStorage.

Update teammate plans and concrete request examples: memory-only tokens, refresh
coordination, refresh before expiry, bounded retry after 401, login on failed refresh,
403 without refresh loops, logout, CORS and safe error rendering. Frontend source
must render profile values as untrusted text, never raw HTML. Frontend source
is not in this workspace; do not claim implementation or browser cutover there.

### 3.8.4: Accepted-job grants independent of browser sessions

Add versioned, nonsecret admission grants and expiry fields. Admission must atomically
validate request actor/permissions, pin resource scope and write the grant with the
job. Workers authorize only that job/attempt/scope/deadline, preserving lease,
owner and checkpoint/terminal fences and interruption/partial-result semantics.
Use database time for persisted admission/start bounds. Queue timeout must terminalize
without executing tools or leaving the conversation busy. A worker restart never
extends an admitted deadline or silently replays interrupted operations.

During rollout, drain legacy queued/running jobs using old workers before switching
admission/workers. Do not fabricate grants from session references or stored user
profiles. Document failure/recovery and idempotency if draining cannot complete.

### 3.8.5: HTTP cutover and profile synchronization

Require a single well-formed Bearer header on protected routes; cookie credentials
alone must not authorize. Enforce verified roles and existing resource ownership.
After successful authentication/authorization, project only allowed profile claims.
Define the own-profile HTTP contract for the frontend without exposing other users.
Replace cookie/CSRF browser API contracts and security schemes with bearer contracts;
configure exact trusted CORS origins without cookie credentials. No authorization
decision may use a profile table or a caller-provided user/role header.

Switch together with frontend and grant-aware workers. Test fresh login, refresh,
expiry, logout semantics, audience/authorized-party separation, user isolation,
dependency outages, concurrent jobs and missing/stale profile data. Publish an
explicit compatibility/cutover runbook and rollback bounds.

### 3.8.6: Verify and retire legacy session/token storage

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
