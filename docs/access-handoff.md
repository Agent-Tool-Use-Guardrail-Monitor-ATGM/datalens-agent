# Access integration and frontend/agent handoff — step 3.6

The implemented boundary is Keycloak-owned accounts/roles plus agent-owned
encrypted browser sessions. No frontend, CrewAI agent, dataset/run API or cloud
deployment is implemented by this step. Its changes are tests, isolated test
infrastructure and documentation; application authentication behavior is unchanged.

## Local provisioning

1. Provision the persistent identity service using its own repository/runbook:
   administrator, `datalens` realm, user/admin client roles, confidential `agent-api`
   code-flow client, exact callbacks/audience and email activation. Account and
   role administration stays in Keycloak's console. Test fixture accounts are
   not real team accounts and do not provision this environment.
2. Configure the agent's existing ignored `.env`/process environment, client-secret
   file and external encryption keyring. Follow [browser setup](browser-login.md)
   and [secret formats](auth-configuration.md). Reuse stable keys across restarts
   and API replicas; never generate them on every startup.
3. Start the agent-owned PostgreSQL and explicitly apply migrations through
   `0003_refresh_claim`; use the existing migration check before serving traffic.
   No command in the verification work migrates persistent developer storage.
4. Run the API on its configured origin and open `/auth/login`. The existing
   local topology uses issuer `http://localhost:8081/realms/datalens`, agent
   origin `http://localhost:8000` and callback `/auth/callback`. Use those exact
   hostnames; switching between localhost and 127.0.0.1 changes origin/issuer.

```powershell
docker compose --env-file .env.postgres up -d --wait
.\.venv\Scripts\python.exe -m statistics_agent.persistence.migrate upgrade --env-file .env
.\.venv\Scripts\python.exe -m statistics_agent.persistence.migrate check --env-file .env
.\.venv\Scripts\python.exe -m uvicorn statistics_agent.app:create_app --factory --loop statistics_agent.core.event_loop:create_event_loop --env-file .env --host 127.0.0.1 --port 8000 --no-access-log
```

Keycloak owns its independent PostgreSQL schema; agent Alembic never targets it.
Staging/production require configured HTTPS, Secure host-only cookies and trusted
public origins. Azure, DNS/TLS, Vercel routing, production SMTP and backups remain
plans 8/10; local HTTP exceptions do not enable a production bypass.

## Browser contract for plan 8

Use same-origin API routing. Navigate the browser to `GET /auth/login`; Keycloak
handles credentials/activation and returns to the backend. After successful
callback, the backend sets an opaque HttpOnly cookie and redirects to `/`.
That path currently returns 404 until the frontend/landing page exists. Opening
`/auth/me` after login verifies the session now.

`GET /auth/me` returns exactly issuer, subject, known roles, verified-email status
and CSRF proof. Keep the CSRF proof in UI memory; never obtain/store OAuth access,
ID or refresh tokens in JavaScript/localStorage. Automatic JWT refresh is entirely
server-side and keeps the browser cookie/CSRF unchanged.

```javascript
const response = await fetch("/auth/me", {
  credentials: "same-origin", cache: "no-store"
});
if (!response.ok) throw new Error(`Session unavailable (${response.status})`);
const session = await response.json();
const logout = await fetch("/auth/logout", {
  method: "POST", credentials: "same-origin",
  headers: { "X-CSRF-Token": session.csrf_token }
});
```

The browser supplies Origin for writes; JavaScript should not try to set that
forbidden request header. Postman/other cookie API clients must explicitly send
the configured Origin and the proof for state-changing operations. A browser and
Postman do not automatically share cookie jars. No broad CORS is configured.

401 requires login, 403 displays denied access without assuming all user work is
forbidden, and 503 reports temporary unavailability with bounded retry behavior.
Do not refresh OAuth tokens in the UI or loop login on every 403/503. A pending
login produces 409; its binding is not silently overwritten. Unexpected failures
use the existing safe 500 envelope. Errors contain a correlation request ID and
fixed messages, never provider/SQL bodies. Authentication/protected responses use
no-store/no-referrer and cookie-authenticated writes require Origin plus CSRF.

Logout reports local success separately from provider confirmation. A false
`provider_logout_succeeded` does not undo local logout; identity-provider SSO may
still permit a subsequent login. No unconditional federated-global-logout promise
is made. See [refresh/logout](session-refresh-logout.md).

## Agent/API contract for later plans

Every protected operation must use `require_user` or `require_admin`. The returned
immutable `AuthorizedPrincipal` contains verified issuer/subject, known roles,
verified-email status and an internal session reference, never OAuth tokens.
Admin includes user permission, not Keycloak system administration or cross-user
data access. Apply `owner_filter` inside resource SQL queries and return 404 on
an owner-filtered miss; client-supplied owner IDs cannot choose an identity.

No production resource CRUD exists yet; ownership is proven using test-only tables
and routes. Agent prompts, model/tool arguments, logs and Kafka events must not
receive browser/OAuth credentials. Plan 7 owns repeated job/tool authorization and
fixed fallback at later execution boundaries. An already authorized operation
cannot be undone by a later revocation; no LLM decides permissions.

## Restart, key management and outages

API process restart loses only the in-memory public-key cache. With the same DB
and encryption keyring, persisted browser sessions remain readable and valid;
cold signing-key retrieval may still require Keycloak. Restart does not mint a
new browser credential or revive revoked sessions.

For encryption rotation, add a new key ID/material, make it active and retain old
read keys on every replica. New writes/refreshes use the active key; existing rows
are not bulk-reencrypted automatically. Keep old keys until their remaining
sessions/login attempts expire or a separately reviewed re-encryption procedure
completes. Removing a required read key denies that session; it does not reset
the DB. Restoring the key can restore decryptability, while local revocation
continues to deny access. Missing/corrupt keyring/client-secret files stop startup.
Keep protected key backups separate from SQL backups. Secret files are loaded at
app creation, not hot-reloaded. See the existing identity recovery runbook for
client-secret rotation; no real key/secret was provisioned by verification.

Valid unexpired access tokens work through identity outage with still-trusted
cached public keys. Key trust lasts at most one hour; permission changes may lag
until JWT expiry, at most 300 seconds with zero expiry leeway. Cold starts,
unknown/expired key trust and expired-token renewal still need identity and fail
safely when it is unavailable. The local DB remains required for every session
decision and for confirmed local logout; public liveness is independent of it.
Readiness checks the agent DB/migration state, not Keycloak availability.

Durable pending refresh claims prevent retrying a possibly consumed credential.
A stale claim causes revocation, not takeover; never clear it manually to repair
a login. Cleanup is the existing explicit bounded `AuthStore.cleanup` operation
(at most 1000 rows per call), not a new scheduler. Production scheduling and backup
restore verification belong to deployment work. No such deployment is claimed.

## Reproduce the required test gate

Use Python 3.12 and this service's `.venv`/requirements.txt. The required job is
still named `tests`; it prepares Chromium and isolated identity services before
running pytest, with always-run Compose cleanup. Missing enabled fixtures fail
CI instead of silently skipping.

```powershell
.\.venv\Scripts\python.exe -m playwright install chromium
docker compose -p datalens-agent-tests -f compose.test.yaml up -d --wait
docker compose -p datalens-agent-auth-tests -f compose.auth.test.yaml up -d --wait --wait-timeout 240
$env:TEST_DATABASE_URL = 'postgresql+psycopg://agent_test:test-only-password@127.0.0.1:54330/agent_test_db'
$env:AUTH_BROWSER_TESTS = '1'
.\.venv\Scripts\python.exe -m pytest -q
docker compose -p datalens-agent-auth-tests -f compose.auth.test.yaml down
docker compose -p datalens-agent-tests -f compose.test.yaml down
```

The agent fixture and identity fixture have separate PostgreSQL instances. The
identity database uses tmpfs, with no developer volume or published DB port.
Keycloak imports synthetic realms and a disposable administrator; Mailpit captures
activation to `.invalid` addresses without external delivery. Native API processes
receive only explicit synthetic settings and temporary test keys. Their logs are
not published; no screenshots, traces, OAuth tokens, invitation links or mail
artifacts are saved. Outage control names only the fixed test Compose project and
Keycloak service; the identity PostgreSQL remains alive during those checks.

Run these integration cases sequentially, as CI does: they share fixed fixture
ports and deliberately restart disposable Keycloak. Do not enable test parallelism
against the same fixtures without separately isolating projects/ports. The test
profile is not production identity configuration or proof of backup restoration.

## Acceptance evidence boundary

The eight new cases cover real email activation, actual API process restarts,
read-key rotation/removal/restoration and invalid-key startup, actual Keycloak
role/email changes, account disable/re-enable, identity-side logout and real
outages through cached/cold keys, expiry and local logout. Earlier still-current
tests cover RSA/JWKS rollover/unknown-key handling, CSRF, ownership, two API
instances and durable refresh/commit races. Real Keycloak signing-key rotation,
production SMTP/TLS, cloud deployment and backup restore are not claimed.

Branch: `test/access-integration`. Required PR/CI on the delivered revision is
still needed before final plan 3 sign-off. Nothing is committed/pushed by this
verification. Parent plans/README are outside the service repository and its PR.
Stop before plan 4 implementation; it requires a separate request.

Verified on 28 October 2026: **339 tests passed**, no skips, in 289.31 seconds,
including all eight new integration cases. Ruff, touched-file formatting and pip
consistency checks passed. One pre-existing Starlette/HTTPX deprecation warning
remains. No application source, runtime dependency, migration, persistent
operator database, real account/key or identity repository was changed.

Step 3.6 disposable agent/identity PostgreSQL, Keycloak and Mailpit containers
and their networks were removed after verification. Persistent developer volumes
were untouched. Test API processes exited; Docker Desktop remains running.
