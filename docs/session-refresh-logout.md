# Coordinated refresh and logout — step 3.5

Expired access tokens now renew automatically in the API backend. The browser
keeps the same opaque HttpOnly cookie and CSRF proof; it never receives OAuth
tokens. `POST /auth/logout` revokes the local session before attempting Keycloak
logout and clears the cookie. No frontend, agent tools or business routes are
introduced here.

## Migration and configuration

Apply `0003_refresh_claim` with the existing explicit migration command before
running this revision against an older database:

```powershell
.\.venv\Scripts\python.exe -m statistics_agent.persistence.migrate upgrade --env-file .env
.\.venv\Scripts\python.exe -m statistics_agent.persistence.migrate check --env-file .env
```

PostgreSQL must already be running. This implementation does not migrate the
persistent development database. The migration adds nullable refresh claim ID
and start-time columns plus a consistency constraint; existing sessions remain.
It touches only agent-owned storage. Downgrade revokes sessions with pending
claims before dropping those columns, so rollback cannot make a possibly consumed
refresh token reusable. Existing CLI downgrade remains test-only.

No dependency or setting was added. Replicas must share the agent database,
issuer/client, encryption keyring and timeout configuration. The entire owner
refresh operation has a budget of three times `AUTH_HTTP_DEADLINE_SECONDS`
(default 30 seconds); individual HTTP phase limits still apply. The durable claim
lease adds one second and a waiting request is bounded by the budget plus two.
DB lock/connection waits retain the existing pool/statement/connect limits.

## Authorization and coordination

Only an access token that passes signature, algorithm, issuer, audience, subject,
claim types, lifetime, email and role checks can be classified as expired. A second
verification disables only expiry to establish this classification. Other failures
never trigger renewal. An expired token lacking the requested operation role is
denied rather than refreshed to repair that permission. Unsafe requests must
pass Origin/CSRF checks before any renewal side effect.

After expiry is established, a short PostgreSQL row lock rereads local activity,
revocation, version and pending claim. One replica commits an independent claim
before sending the refresh credential. Other replicas wait for its stored result;
they do not call the token endpoint with the same credential. No database
transaction or row lock spans an HTTP request. Session deadlines retain their
existing application clock; claim lease comparisons use PostgreSQL's clock.

The token endpoint is trusted configured metadata, not caller input. The backend
validates the replacement access JWT and identity/permissions before storing the
rotated credentials atomically. If an ID token is returned, its signature,
issuer/audience/subject and nonce/authentication-time/hash context are checked.
The final update requires the matching claim/version and an active local session.
Successful refresh increments the version, clears the claim and keeps the cookie.
A refreshed user token may deny an admin operation without ending permitted user
access. Valid unexpired requests continue to use local keys without introspection.

A denied renewal requires login. A lost/unknown response, cancellation, deadline,
invalid replacement or failed persistence does not retry the old refresh token;
the backend attempts local revocation and returns a fixed safe failure. If a
process crashes, the committed pending claim remains. Once its lease expires,
another request revokes the session instead of taking over that credential.
Database unavailability prevents confirming revocation, so requests fail safely;
the durable marker prevents retrying an old credential when its claim remains.
Do not manually clear pending claims or reset versions to recover a login.

This durable marker is a deliberate refinement of the original row-lock sketch:
a lock alone disappears after a crash and cannot record an uncertain remote
rotation. It also keeps the canonical no-network-inside-DB-transaction rule.

## Logout contract

Send the configured Origin, opaque session cookie and `/auth/me`'s
`X-CSRF-Token` to `POST /auth/logout`. Duplicate/missing/wrong proofs or origins
are denied before changing local state. Logout uses local owner binding, not
current JWT permissions, and therefore works with expired tokens, removed roles,
unreadable token ciphertext or a Keycloak outage. A retained proof is required.

After committing local revocation, the backend attempts standard RP-Initiated
Logout using POST with an ID-token hint and client ID in the form body. It sends
no browser/provider cookies and follows no redirects. Secrets never enter URLs
or application response bodies. Missing/unreadable hints or unavailable/uncertain
provider responses are reported honestly:

```json
{"local_logged_out": true, "provider_logout_succeeded": false}
```

The successful local response clears the host-only session cookie regardless of
the provider outcome. An old cookie cannot regain local access. A concurrent
refresh cannot restore a revoked row. An already admitted operation cannot be
undone. Local logout still needs the agent DB; it does not depend on identity
availability or an LLM decision. Provider logout success is not an unconditional
guarantee about every federated identity provider or an in-flight remote action.

The fixed Keycloak implementation supports this standard POST path separately
from its legacy refresh-token logout API; see the pinned
[Keycloak logout endpoint](https://github.com/keycloak/keycloak/blob/26.8.1/services/src/main/java/org/keycloak/protocol/oidc/endpoints/LogoutEndpoint.java).

## Verification and scope

Lifecycle tests use real migrated PostgreSQL, independent connection pools,
provider clients and two FastAPI instances. Controlled signed responses cover
one exchange across replicas, fresh permissions, invalid expired credentials,
cancel/timeout/logout races, crash claims, commit failure/lost acknowledgement
and safe downgrade. Real Chromium/Keycloak tests additionally wait for natural
30-second access-token expiry, verify token rotation with unchanged cookie/CSRF,
then confirm local logout and provider rejection of the saved refresh token.

The short-lived-token realm and its keys/accounts are synthetic and disposable.
That test uses bounded 15/30-second provider limits and a 45-second API-client
limit to accommodate a cold Keycloak logout page; production defaults remain
unchanged. One initial remote logout was unconfirmed within its shorter fixture
timeout, while local logout still succeeded. The dedicated bounded fixture and
real provider-invalidated-token assertion retain coverage of remote success.

The user-requested discard of the earlier disabled-login visibility change was
honored. That original check remains unchanged. Its prior CI rerun/merge is
accepted from the user's confirmation. No other user edits were reverted.

Branch: `feature/session-refresh-logout`. No commit/push or remote CI result is
claimed. Parent planning files remain outside the service repository/PR. Stop
before step 3.6, which owns the broader retained-storage and frontend handoff.

Verified on 28 October 2026: **331 tests passed**, no skips, including 26 lifecycle
cases and nine real browser scenarios. Ruff, touched-file formatting and pip
consistency checks passed. One existing Starlette/HTTPX deprecation warning
remains. Initial observer-loop and mixed-clock fixture issues were corrected;
remote logout was confirmed by a subsequent refresh rejection at Keycloak.
No persistent developer migration, real account/secret change or identity
repository edit occurred. Required PR/CI and publication are pending.

Step 3.5 disposable PostgreSQL/Keycloak containers and networks were removed
after verification. Persistent development volumes were untouched. Docker Desktop
remains running.
