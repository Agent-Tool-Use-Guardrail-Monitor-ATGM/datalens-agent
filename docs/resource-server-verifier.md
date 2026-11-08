# Plan 3.8.2 — Session-independent JWT verification

## CI correction — collection-time token timestamps

The user's CI report had 1,135 passes and one failure in the new excessive-lifetime
test. Its `exp` was generated at module import/collection, but the signed token's
`iat` was generated later during execution. After about six minutes of earlier
tests, the intended 601-second token could instead have a valid lifetime below
300 seconds; the verifier correctly accepted it. The future `nbf` parameter had
the same collection-age dependency. This failure is separate from browser login.

The exact `DID NOT RAISE AuthenticationError` was reproduced in 0.60 seconds by
aging the collected expiry parameter 360 seconds, without waiting. A boundary
regression also failed before the fix. The test helper now sets issued-at,
expiry and optional not-before from one minting-time clock, while parameter tables
contain relative durations. Temporal cases have descriptive pytest IDs.
No production verifier, timeout or assertion was weakened to reject valid tokens.

After correction, resource/legacy verifier tests: **75 passed**. Importing the
test module with its collection clock 360 seconds and 660 seconds behind, then
restoring execution time, gave **44 passed** in each case. These ephemeral
diagnostic harnesses wrote no source files and added no sleep/retry to tests.
Ruff/format/whitespace checks pass. No new dependency or database change. Full
browser/database gates were not rerun; the prior evidence below remains historical
and the required remote PR `tests` gate still needs to pass after pushing this fix.

Implemented internally on `fix/bearer-authentication-migration`, 8 November 2026.
This step supplies verification prerequisites, not the HTTP/worker cutover.

`ResourceAuthSettings` and `ResourceServerVerifier.verify_access()` require no
client secret, encryption keys, cookies, login sessions, token database or
identity-database connection. Configuration is explicit; see
[.env.resource-auth.example](../.env.resource-auth.example). It is an example,
not a startup file automatically consumed by the current application.

```python
from statistics_agent.core.resource_auth import load_resource_auth_settings
from statistics_agent.core.resource_verifier import ResourceServerVerifier

settings = load_resource_auth_settings(env_file=".env.resource-auth")
verifier = ResourceServerVerifier(settings)
try:
    access = await verifier.verify_access(received_access_token)
    identity = access.identity
finally:
    await verifier.close()
```

In a service, create one verifier per application lifespan and reuse it; do not
construct a new HTTP client/key cache for every request. The snippet shows the
ownership/close boundary, not a delivered HTTP route or a complete application.

## Verification contract

- Only RS256 and trusted RSA signing keys of at least 2048 bits. Reject untrusted
  JWT key URLs/extensions and malformed headers before fetching keys.
- Exact configured canonical realm issuer and audience (`agent-api` by default).
  Hosted staging/production issuers must use HTTPS. HTTP is restricted to explicit
  local development/test loopback issuers.
- A required `azp` must belong to `OIDC_AUTHORIZED_PARTIES` (default `agent-web`).
  Audience and authorized party are separate. A multi-audience token can include
  `agent-api` and `account` while being issued to `agent-web`; a token for only
  `agent-web`, or from an unallowed client, is rejected.
- Required issuer, subject, audience, issued-at and expiry. Validate time claims,
  zero expiry leeway, future not-before rejection and at most 300-second access
  lifetime. Expired tokens fail as `AuthenticationError`; the resource verifier
  never returns a refreshable expired identity or performs token refresh.
- Require payload `typ=Bearer`, boolean verified email and at least user/admin
  from `resource_access[agent-api].roles`. Admin includes user; frontend-client
  roles and realm roles do not substitute for the API role namespace.
- Return `VerifiedAccess`: immutable verified identity, integer issued/expiry
  timestamps, optional bounded `sid`, and whitelisted bounded display hints.
  No raw access/refresh/ID token is returned or stored. Optional malformed profile
  hints/session IDs fail safely; no image/picture claim is exported.

Display hints are not event-owned profile updates or an authorization directory.
They must not overwrite a newer RabbitMQ projection. A valid signed token alone
does not yet enforce future local revocation metadata: step 3.8.5 supplies that
state, and step 3.8.7 must compose its checks before request/job admission.
The optional session ID must become required by the integration policy before
claiming session-specific revocation; absence is not fabricated into a session.

## Shared key cache and compatibility

The existing JWT/JWKS implementation is shared with the legacy `OidcProvider`;
no second hand-written signature verifier was added. `OidcProvider` retains its
public constructor, exchange/refresh/logout methods, expired-token refresh
behavior and confidential-client authorized-party rules. Existing routes still
use it and their current cookie/CSRF contracts. The new verifier is not installed
into the app factory, and there is no public `agent-web` client delivered yet.

Caches remain per process, with one asyncio mutex coalescing concurrent refreshes,
a one-hour maximum key trust lease and a five-second minimum refresh interval.
Known still-trusted keys can verify during an optional fetch outage. Unknown keys
or expired key trust fail closed when refresh is unavailable. HTTP is bounded,
redirects/proxy-environment forwarding are disabled, provider response sizes are
limited and duplicate JSON object keys are rejected. No cookies are forwarded.
Cancellation releases the refresh mutex; no database transaction/lock spans key
discovery or verification. This asyncio lock protects the process key cache,
not database data shared between replicas.

Missing/invalid tokens and denied permissions preserve fixed failure codes;
provider/key-material failures are safe unavailable errors. Do not log token
strings, exception bodies or full profile hints. Importing/constructing settings
does no network/secret-file I/O. First verification may fetch trusted metadata/keys.

## Database concurrency boundary

No database mutation, table or lock is added by 3.8.2. Current atomic profile
upserts already serialize same-key writes through PostgreSQL. Ordered event
projection needs source revisions and transactional inbox/state updates, not a
process mutex or table-wide lock. See
[database concurrency and recovery](database-concurrency-and-recovery.md) for the
later 3.8.4–3.8.6 requirements and proof gates. This is not a claim that all future
workflows are deadlock-free.

## Verification status

Pre-extraction legacy provider baseline: 31 passed. Post-extraction legacy
provider/configuration/encryption gate: 90 passed. Resource/legacy focused gate:
132 passed, including concurrent refresh, outage/trust expiry/rotation, cancelled
refresh recovery, wrong issuer/audience/azp, expired credentials, bounded claims
and no private-file access. Full non-browser regression: 1,119 passed, 17 browser
tests deselected, 514 warnings, 147.35 seconds. The first browser group had 16
passes and one existing success-login 503 failure (443.06 seconds). That case
passed unchanged with temporary phase/timing-only diagnostics (1 passed,
5.42 seconds; discovery 266ms, JWKS/token phases succeeded). No cause was proven;
no authentication rules, assertions or timeouts were relaxed. Final complete
browser group: **17 passed, 1,119 deselected**, 314.12 seconds, using temporary
phase/outcome/timing diagnostics only. Those final sequential groups cover all
1,136 current tests; the earlier failed browser run remains recorded as a failure.
Successful diagnostic phases included discovery at 3,625ms and token exchange at
4,500ms, but these observations do not prove the earlier failure's cause.
Treat that unreproduced login 503 as an unresolved flake risk for required CI.
No browser test source, assertions, retries or timeout settings were changed.

Ruff/check/format (152 files), dependency and whitespace checks pass. The resource
configuration example loads with no secrets/network access. Four plan/usage
documents and ten local links were checked. Required remote PR `tests` and
delivery remain pending; no commit/push or live LLM/team-identity/RabbitMQ/Azure
call. No database migration or new database lock was added. Stop before 3.8.3.

References: [JWT access-token validation](https://www.rfc-editor.org/rfc/rfc9068.html#section-4),
[approved migration](bearer-authentication-migration.md).
