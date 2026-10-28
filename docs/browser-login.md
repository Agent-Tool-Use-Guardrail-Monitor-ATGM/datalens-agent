# Browser login — step 3.3

The API now implements `GET /auth/login` and `GET /auth/callback`. It uses
Keycloak's login page and creates an opaque browser session after validating the
code exchange. There is no frontend, `/auth/me`, protected business endpoint or
automatic refresh yet. Successful login redirects to `/`, which currently returns
404 until a landing page/frontend is added; the session cookie is still created.

## Operator setup

Use the existing Python 3.12 `.venv`, requirements.txt and agent PostgreSQL.
Apply the existing migrations explicitly before accepting traffic. The persistent
local Keycloak realm/client must be onboarded using the identity repository's
runbook; browser tests create a separate synthetic realm, not real team accounts.

The app factory now requires authentication settings and both secret files.
There is no switch to disable authentication. Missing files/configuration stop
startup with a fixed diagnostic. Imports and startup perform no provider request;
configured liveness remains independent of Keycloak availability.

1. Add the settings/references from `.env.auth.example` to your existing ignored
   `.env`, or export them as process environment variables. Do not overwrite
   `DATABASE_URL`. The factory does not implicitly load a separate `.env.auth`.
2. Obtain the `agent-api` client secret through Keycloak's protected console and
   save it in the referenced protected file. Use the formats in
   [authentication configuration](auth-configuration.md).
3. Provision the separate Fernet keyring through a protected channel. Keep stable
   keys across restarts; do not print them or add them to Git. Existing keyrings
   should be reused. No real secrets are provisioned by tests or this step.
4. Start PostgreSQL, apply migrations and run the API from this service directory:

```powershell
docker compose --env-file .env.postgres up -d --wait
.\.venv\Scripts\python.exe -m statistics_agent.persistence.migrate upgrade --env-file .env
.\.venv\Scripts\python.exe -m uvicorn statistics_agent.app:create_app --factory --loop statistics_agent.core.event_loop:create_event_loop --env-file .env --host 127.0.0.1 --port 8000 --no-access-log
```

Then open `http://localhost:8000/auth/login` in a browser. Use the configured
hostname exactly, including `localhost` versus `127.0.0.1`. The trusted callback
is `http://localhost:8000/auth/callback`; request Host values cannot choose another
issuer, endpoint or redirect. Keep proxy/access logs free of callback queries,
cookies and credentials. Cloud TLS/proxy routing remains deployment work.

## Flow and failure behavior

Login saves a five-minute browser-bound attempt, then redirects to Keycloak with
fresh state, nonce and S256 PKCE. A browser with a pending attempt receives 409
without silently replacing its binding. Only the configured callback/return path
is allowed; no user-supplied redirect option is supported.

The callback requires exactly one state, a matching HttpOnly browser binding,
the configured response issuer and a single authorization code. Valid state is
consumed before the exchange, even if later validation fails. Duplicate or
unexpected parameters, replay and invalid bindings fail. A failed exchange is
not retried; start a fresh login.

The backend sends the client secret and verifier only to the validated fixed
token endpoint, with bounded HTTP timeouts and no redirects, proxy-environment
inheritance or automatic retries. It validates ID and access JWT signatures using
configured RS256, exact issuer/client audience, subject, strict issue/expiry
claims and zero expiry leeway. ID nonce and optional access-token hash are checked;
subjects must match. Access tokens must be Bearer tokens with lifetime at most
300 seconds, boolean verified email and the signed `user` or `admin` client role.
ID-token claims do not authorize agent use.

Only then does storage create a new independent session credential and encrypt
the OAuth tokens. The browser receives the opaque HttpOnly, SameSite=Lax,
host-only cookie with Path=/; HTTPS uses Secure and production uses
`__Host-agent-session`. Callback completion clears the temporary login cookie.
OAuth tokens never appear in response bodies, browser JavaScript or application
logs. Authentication responses, including safe errors, use `Cache-Control:
no-store` and `Referrer-Policy: no-referrer`.

Invalid responses/tokens return fixed 401 errors. Valid tokens without verified
email/application roles return 403. Required provider discovery/key retrieval or
exchange outages return 503. These responses never include provider diagnostics.
Session creation does not implement ongoing request authorization: step 3.4 owns
that check, `/auth/me`, CSRF and ownership. Until 3.5 there is no refresh; expired
credentials will require a fresh login when protected access is implemented.

## Signing-key cache

Each API process has one HTTP client and coalesced metadata/JWKS cache. Endpoints
must match the configured Keycloak issuer paths and advertise issuer response
support. Only public RSA signing keys of at least 2048 bits are accepted; unknown
critical JWT extensions and token-provided key URLs are rejected without fetching.

Keys have a maximum one-hour trust lease, measured from retrieval start. A known
key within that lease needs no provider call. Unknown kids or expired trust can
trigger a refresh; attempts are limited to once per five seconds per process,
including failures. Concurrent waiters share the result. Optional refresh failure
does not discard a still-trusted known key. Unknown keys after a successful fetch
are invalid; missing/expired trust during an outage cannot authorize. No token
validation result is cached and no introspection endpoint is called.

This permits later protected-request checks to use valid tokens offline, once
step 3.4 wires them. It does not guarantee immediate Keycloak revocation. Remote
permission changes follow token expiry; emergency signing-key revocation has a
separate cache-trust window.

## Reproducible tests

The existing required `tests` job now installs Chromium and starts the isolated
Keycloak fixture before pytest; missing required services fail CI instead of
silently skipping. Cleanup runs even on failure. For local full verification:

```powershell
.\.venv\Scripts\python.exe -m playwright install chromium
docker compose -f compose.test.yaml up -d --wait
docker compose -f compose.auth.test.yaml up -d --wait --wait-timeout 180
$env:TEST_DATABASE_URL = 'postgresql+psycopg://agent_test:test-only-password@127.0.0.1:54330/agent_test_db'
$env:AUTH_BROWSER_TESTS = '1'
.\.venv\Scripts\python.exe -m pytest -q
docker compose -f compose.auth.test.yaml down
docker compose -f compose.test.yaml down
```

Both fixtures are disposable. Keycloak uses its development H2 database on tmpfs,
synthetic users/secrets and an explicit realm import. It does not connect to the
identity repository or persistent identity PostgreSQL. Its client has explicit
role scopes, code flow and S256 PKCE; password/implicit/service-account grants are
disabled. This fixture proves browser/OIDC interoperability, not production
deployment, invitation setup or identity-database recovery. Those retain their
existing owning tests/plans. No saved browser traces, screenshots, mail or secret
artifacts are created.

Implementation follows [OpenID Connect ID-token validation](https://openid.net/specs/openid-connect-core-1_0.html#IDTokenValidation),
[PyJWT's fixed-algorithm verification guidance](https://pyjwt.readthedocs.io/en/stable/api.html),
and [Keycloak's discovery/token/key endpoints](https://www.keycloak.org/securing-apps/oidc-layers).

Verified on 27 October 2026: **274 tests passed**, no skips, including eight real
browser scenarios (success, state, binding, nonce, PKCE, unverified email, missing
roles and disabled account). Provider tests additionally reject bad issuers,
audiences, signatures, lifetimes, nonce/subject/hash mismatches and key-URL attacks;
HTTP tests cover duplicate parameters, wrong response issuer, Host/redirect checks,
secure cookies and outages. Ruff, formatting and pip checks passed. One existing
Starlette/HTTPX deprecation warning remains. Windows sandbox runs stalled and were
stopped; permitted verification outside it passed. Only synthetic test fixtures
were started; no persistent development migration or real secret provisioning.
Branch: `feature/browser-login`. Required PR/CI is pending, with no commit/push.

Step 3.3 disposable PostgreSQL/Keycloak containers and their networks were removed
after verification. Persistent development volumes were untouched; Docker Desktop
remains running.
