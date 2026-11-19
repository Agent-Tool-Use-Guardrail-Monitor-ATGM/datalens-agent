# Local request authorization — step 3.4

The API now exposes `GET /auth/me` and reusable `require_user`/`require_admin`
dependencies. There are no dataset/run endpoints or frontend yet. No migration,
new dependency, secret file or configuration setting is required by this step.
Use the existing [browser login setup](browser-login.md), then open
`http://localhost:8000/auth/me` in the same browser. Login still redirects to `/`,
which remains 404 until a landing page/frontend is added.

## Decision and response

Each protected operation reads the opaque cookie's local session, checks its
revocation and idle/absolute deadlines, decrypts the stored access token and
validates its signature and signed claims using the existing trusted-key cache.
Issuer and subject must match the session owner. Roles come from the current
verified JWT, never a persisted positive authorization snapshot. Application
admin includes user permission, without granting access to other users' records.

After role and CSRF checks, a conditional DB update rechecks local session
activity and extends idle time only within the absolute deadline. Revocation
between the initial read and this update denies the request. Authorization cannot
undo work already admitted before a subsequent revocation; later operations must
authorize again. The dependency returns an immutable `AuthorizedPrincipal`, not
cookies or OAuth tokens.

`/auth/me` returns only `issuer`, `subject`, sorted known `roles`,
`email_verified: true` and `csrf_token`. It does not return session credentials,
refresh/access/ID tokens or private provider data. A cookie is required; a bearer
Authorization header alone does not log in a browser session. Duplicate/malformed
session cookies are rejected. The configured public origin remains authoritative,
not an arbitrary Host/forwarded-host value.

Missing, expired or revoked sessions, invalid tokens, mismatched ownership and
unreadable encrypted credentials return fixed 401 errors. Valid tokens lacking
the operation role or verified email return 403. Required signing-key retrieval
outages return 503; DB unavailability returns the existing generic safe 503.
Denial does not silently revoke a valid user session because an admin operation
was forbidden or identity was temporarily unreachable.

Valid tokens with still-trusted cached signing keys require no Keycloak call and
continue during provider outage. Unknown keys after a successful retrieval are
invalid; unknown keys during outage or expired trust fail safely. No introspection
or identity-user lookup is performed. Remote role/account/email/logout changes
may lag until JWT expiry, at most 300 seconds with zero expiry leeway. A fresh
stored JWT is checked immediately. Step 3.5 now renews an otherwise-valid expired token using a durable cross-replica
claim; see [refresh/logout](session-refresh-logout.md). Invalid identity/permission
checks never initiate renewal.

## CSRF and future routes

Protected non-GET/HEAD/OPTIONS operations require exactly one Origin equal to the
configured public origin and one `X-CSRF-Token` matching `/auth/me`. Missing,
foreign, null or duplicate origins and missing/wrong/duplicate proofs return 403.
A supplied foreign Origin is rejected on safe methods too. No broad CORS or
Referer-only fallback is introduced. Non-browser API clients must explicitly
supply the configured Origin and CSRF header for cookie-authenticated writes.

The proof is a domain-separated HMAC-SHA256 over session UUID, issuer and subject,
keyed by that session's independent 256-bit opaque cookie. It is reproducible
across API replicas without storing a new secret in SQL or adding a deployment
key. JavaScript receives only the proof, never its HttpOnly cookie key. A proof
from another session fails. The proof stays stable while the same session cookie
is retained, supporting future server-side JWT refresh. New login creates a new
cookie and proof. Do not log CSRF values or put them in URLs.

All future protected routes must use the dependency; an unprotected FastAPI route
does not become protected merely by being registered. For example:

```python
from typing import Annotated
from fastapi import Depends
from statistics_agent.api.access import require_user
from statistics_agent.contracts.auth import AuthorizedPrincipal

async def operation(principal: Annotated[AuthorizedPrincipal, Depends(require_user)]):
    return {"subject": principal.subject}
```

The dependency protects writes before the operation runs. OAuth callback instead
retains its state/browser-binding/nonce/PKCE protections. Authentication responses
and responses from these protected dependencies use no-store/no-referrer, including
denials. The design follows the session-binding, custom-header and origin-check
principles in [OWASP's CSRF guidance](https://cheatsheetseries.owasp.org/cheatsheets/Cross-Site_Request_Forgery_Prevention_Cheat_Sheet.html).

## Ownership and limits

`persistence.ownership.owner_filter(issuer_column, subject_column, principal)`
combines both exact verified owner fields inside the SQL query. Use trusted model
columns and the dependency-produced principal; never accept a client-supplied
owner. Admin does not widen this filter. A filtered miss must return the same 404
as an absent resource. This step proves that pattern with disposable test-only
resource tables/routes; it adds no production resource table or CRUD API.

Verification on 28 October 2026: **305 tests passed**, no skips, including 31 new
PostgreSQL-backed access checks and the existing real Chromium/Keycloak flow,
now also checking authenticated `/auth/me`. Coverage includes expired/tampered
tokens, mismatched identity, corrupted storage, role changes, key rollover,
outages, revocation races, CSRF and two-user/two-issuer ownership including admin.
The deterministic expiry boundary was also rerun after removing a slow-CI timing
assumption. Synthetic signed-token replacement simulates changed roles here;
real Keycloak refresh-state verification remains steps 3.5/3.6.

Ruff, touched-file formatting and dependency checks passed. One pre-existing
Starlette/HTTPX deprecation warning remains. Tests use disposable PostgreSQL
schemas and the synthetic Keycloak fixture, with no real accounts, persistent
development migration, real-secret provisioning or identity repository change.
Existing required `tests` CI collects the new tests without another check name.

Branch: `feature/token-authorization`. No commit/push or remote CI result is
claimed. Parent plans are outside this Git repository and its PR. This evidence records original 3.4 scope. Step 3.5 now implements coordinated
refresh and logout; see [its guide](session-refresh-logout.md).

Step 3.4 disposable PostgreSQL/Keycloak containers and networks were removed
after verification; persistent development volumes were untouched. Docker Desktop
remains running.
