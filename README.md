# DataLens statistics-agent development

[Plan 3 integration evidence and frontend/agent handoff](docs/access-handoff.md)
records the verified access contract and recovery/test procedures.

Current startup requires auth settings and protected client-secret/encryption-key
files, in addition to DATABASE_URL. See [browser login setup](docs/browser-login.md)
before running the API. Missing configuration fails startup; there is no bypass.
Current request/session API: [local authorization and ownership](docs/request-authorization.md),
[automatic refresh/logout and required migration](docs/session-refresh-logout.md).

## Environment and setup

Each Python service has its own standard virtual environment in its `.venv/`
folder. For this service, the path is `services/datalens-agent/.venv/`.
The environment is displayed as `.venv` in an activated terminal; its full path
identifies which service owns it. It is ignored by Git.

The current environment uses Python 3.12.15. To use it from the workspace root (`ost-sm-project/`):

```powershell
Set-Location services/datalens-agent
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m pip install --no-deps --editable .
python -m pip check
```

`pip install` installs into the Python environment being used; it does not create
an environment. Creation is a separate one-time step. On a machine with a
registered Python 3.12 installation, run from this service directory:

```powershell
py -3.12 -m venv .venv
```

On this workstation, Python 3.12 is provided by an ignored standalone runtime at
`.tools/python312/python/python.exe` under the workspace root. To create the
service environment using that runtime, run from the workspace root (`ost-sm-project/`):

```powershell
& .\.tools\python312\python\python.exe -m venv services/datalens-agent/.venv
```

The runtime is CPython 3.12.15 from Astral's `python-build-standalone` release
20261020, Windows x64 `install_only` archive. It was downloaded directly and its
published SHA-256 checked before extraction; no uv tool is used. Source:
[Python standalone builds](https://github.com/astral-sh/python-build-standalone/releases/tag/20261020).
Archive SHA-256: `5e7d27ae6181fcf1f992c5a221b8fa7a320e7fd576c476434886292ccf2e20fb`.
A virtual environment still needs its base Python runtime, so keep this runtime
while the environment uses it. Other machines can use any installed Python 3.12.
Virtual environments are recreated on each checkout/machine, not copied or moved.

Activation is optional. If PowerShell does not allow the activation script, run
`.\.venv\Scripts\python.exe` instead of `python` from this service directory.
No global execution-policy change is required.

## Dependencies and package

`requirements.txt` is the only application/development dependency list. It pins
checked direct versions; pip resolves their transitive dependencies. It does not
record Python, operating-system libraries or exact transitive versions. Fresh
setup is verified in step 1.10 and Linux deployment in plan 10. No Conda YAML or
additional dependency lockfiles are maintained.

`pyproject.toml` contains package/build metadata and pytest/Ruff configuration,
without duplicate application dependency lists. The editable local distribution
is `datalens-statistics-agent`; Python imports use `statistics_agent` from
`src/statistics_agent/`. Install requirements before installing the local package.
The future API and worker share this service environment; a separate Python
microservice would have its own `.venv/` and requirements file. Keycloak and the
frontend keep their own toolchains.

## Configuration — step 1.3

Use `statistics_agent.core.settings.load_settings()` to load process environment
variables and defaults. It performs no network/database operations. Local dotenv
loading is explicit and relative to the current working directory. From this
service directory, copy `.env.example` to ignored `.env` and replace its database
placeholders when a local database is available. Do not commit that file.

```python
from statistics_agent.core.settings import load_settings

settings = load_settings(env_file=".env")  # Local development only.
```

For deployment, inject environment variables and call `load_settings()` without
an env file. Process variables override dotenv values; missing optional values
use the defaults below. An explicitly requested missing/unreadable file is an
error. Only recognized fields are consumed. No LLM, SMTP, identity, Vercel or
Kafka credentials are needed for foundation configuration.

| Variable | Default | Accepted value / unit |
|---|---|---|
| `DATABASE_URL` | Required | `postgresql+psycopg://...` with host, username and database; 1–4096 characters |
| `APP_ENV` | `development` | `development`, `test`, `staging`, `production` |
| `SERVICE_NAME` | `agent-api` | `agent-api` or future `agent-worker` process role |
| `SERVICE_VERSION` | `0.1.0` | 1–64 characters, starting with a digit; version-safe letters/numbers and `.+_-` |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL` |
| `DB_POOL_SIZE` | `5` | 1–20 connections |
| `DB_CONNECT_TIMEOUT_SECONDS` | `5` | 1–60 seconds |
| `DB_POOL_TIMEOUT_SECONDS` | `10` | 0.1–60 seconds |
| `DB_STATEMENT_TIMEOUT_MS` | `10000` | 100–300000 milliseconds |
| `DB_HEALTH_TIMEOUT_SECONDS` | `2` | 0.05–10 seconds; strictly below connect, pool and statement timeouts |

Process names are `agent-api` and `agent-worker`, independent of the repository name. Legacy prefixed SERVICE_NAME values
`datalens-agent-api` and `datalens-agent-worker` remain accepted as input aliases and normalize to
`agent-api` and `agent-worker` in settings, serialization and
logs. New configuration must use the canonical values above. Python imports
remain `statistics_agent`; database identifiers remain `agent_db`/`agent_test_db`.

Settings are immutable. The database URL is secret-bearing and excluded from
settings repr/dumps/JSON. `load_settings` raises `ConfigurationError` with safe
field names rather than raw values or source exceptions. Application components
should use this loader instead of logging raw Pydantic validation errors. Only
the database integration should explicitly extract the URL with
`settings.database_url.get_secret_value()`; never log or serialize that value.

The application factory now loads and shares one settings object per app.
Lifespan now creates/disposes the agent database pool; connections are lazy.
Step 1.7 enforces pool/connect/statement limits when database work is performed.

## Shared contracts — step 1.4

`statistics_agent.contracts` provides immutable principal/auth-session,
configuration and artifact references; HTTP error envelopes and request-ID
resolution; and the versioned event envelope. Importing contracts does not load
settings or contact infrastructure. Identity references must still come from
verified claims in plan 3; constructing a principal is not authentication.

`EventEnvelope` schema version `"1.0"` supports exactly the seven lifecycle names
in `EventType`. Callers supply stable event/run/session UUIDs and a positive
session sequence; parsing never invents a missing event ID. Timezone-aware
timestamps normalize to UTC. Provider IDs belong in separate payload fields,
not the application identifier fields. Sequence allocation and durable delivery
remain plan 7 work.

The `data` object is ordinary JSON, limited to 16384 encoded UTF-8 bytes and
nesting depth 16. Non-finite numbers and non-JSON objects are rejected. Size,
nesting and JSON checks also run at serialization to catch nested mutations.
Oversized evidence must use a restricted artifact reference in its owning plan;
it is not silently truncated. Unknown tool names and malformed argument strings
are valid evidence and are preserved verbatim. Event-specific payload schemas
are defined in plans 4/7; no executable tool validation happens here. Producers
must redact credentials and sensitive data before constructing events; these
structural contracts cannot detect secrets embedded in arbitrary text.

`ErrorEnvelope` contains `code`, server-authored `message`, UUID `request_id`
and at most 20 validation details. Each detail has a trusted schema location of
at most eight field-name/index parts and a safe error code, without rejected
values or exception context. The HTTP handlers from step 1.6 supply safe
messages and source-only locations rather than copying raw validation errors.
`resolve_request_id` accepts canonical 36-character UUID headers and otherwise
generates a new UUID. Header wiring and response propagation are implemented in 1.6.

`tests/fixtures/events_v1.json` contains seven independent wire examples for
serialization compatibility, not a complete execution trace. Unknown usage
remains `null`. Unknown top-level fields/versions are rejected; adding fields
requires compatible readers before producer rollout, or a schema-version change.

## FastAPI shell — step 1.6

`statistics_agent.app.create_app(settings=None, log_stream=None)` returns an
isolated FastAPI application. Supply validated settings in tests or let the
factory load the process environment once. Imports and construction do not
contact infrastructure. Lifespan tracks startup/shutdown and emits service events;
step 1.7 now creates the lazy database pool here and disposes it on shutdown.

From the service directory with its `.venv` activated, create an ignored `.env`
from `.env.example` if needed, replace placeholder configuration, and start:

```powershell
python -m uvicorn statistics_agent.app:create_app --factory --loop statistics_agent.core.event_loop:create_event_loop --env-file .env --host 127.0.0.1 --port 8000 --no-access-log
```

The explicit `--env-file` is local development only; deployment injects variables
and omits it. The PostgreSQL URL must be structurally valid, but the shell opens
connections only when an operation needs one. `/docs` and `/openapi.json` exist only in development/test;
staging/production disable them. Other paths return safe 404 envelopes. There are
no business/authentication endpoints yet; health probes are documented below. HTTPX/TestClient test routes
are registered only in the tests, never in the factory.

`X-Request-ID` accepts one canonical UUID, otherwise generates a fresh UUID. The
response header, safe error body and JSON request log share it; concurrent
requests have separate state/context. Handlers return canned errors for HTTP
exceptions, safe 422 validation envelopes, and safe 500 envelopes for unexpected
exceptions before headers are sent. Validation details contain only a bounded
source category such as `body` or `query`, never submitted dictionary keys or
values; at most 20 details are returned. Raw exception details and arbitrary
exception headers are omitted; controlled 401/405 protocol headers are retained.
A failure after streaming starts cannot replace the sent status/body: it logs
`http_failed` with the already-sent status and aborts with a sanitized exception.

Application JSON logs include UTC time, severity, service/version, environment,
event, request ID and, for requests, method, route template, status and duration.
Unmatched paths use `unmatched`. No request/response bodies, query strings, raw
paths, headers, credentials or exception text/tracebacks are recorded. The logger
accepts controlled events and metadata; it is not a universal secret detector or
a configuration for every third-party logger. Use the documented `--no-access-log`
flag: default server access logs can include raw URLs/query strings. Service logs
use stdout at `LOG_LEVEL`; logger instances and streams do not alter root logging.

Lifespan follows the [FastAPI context-manager interface](https://fastapi.tiangolo.com/advanced/events/).
The existing CI job runs the new tests automatically when their branch is opened
as a PR. No dependencies or workflow changes were needed for this step.

## PostgreSQL foundation — step 1.7

The agent owns its schema and application SQL in this repository. PostgreSQL is
an independent container/process with private credentials and persistent storage;
it is not embedded in the application image or replicated per API/worker replica.
API and worker share `agent_db`. Keycloak has its own database/deployment; neither
service reads the other's tables. A future infrastructure repository may own
cloud deployment configuration; models and migrations stay with agent code.
No separate database code repository or custom PostgreSQL image is introduced.

### Local database

[compose.yaml](compose.yaml) uses the official `postgres:17.11-bookworm` image,
service `agent-postgres`, database `agent_db`, user `agent_local`, and named volume
`datalens-agent-local_agent-postgres-data`. The host port is bound to loopback
only, default `127.0.0.1:54329`. Local Docker users can still administer containers;
loopback binding is not a replacement for database authentication.

Ignored `.env.postgres` and `.env` were created on this workstation with a generated
password, and the healthy local database was left running. No secret values were
printed or committed. For a fresh checkout, from the service directory:

```powershell
Copy-Item .env.postgres.example .env.postgres
Copy-Item .env.example .env
```

Choose a local password in `.env.postgres` and put its URL-encoded equivalent in
`.env`'s DATABASE_URL, using user `agent_local`, port 54329 and database `agent_db`.
Change both files consistently. Start Docker Desktop's Linux engine, then run:

```powershell
docker compose --env-file .env.postgres up -d --wait
```

The command starts only PostgreSQL, not the API. Use the FastAPI command above
for the API. The custom event-loop factory selects the Windows Selector loop
required by Psycopg; it uses the normal asyncio loop on Linux. No global event
loop policy or additional dependency is installed.

`POSTGRES_USER` is a bootstrap superuser in the official image. These local/test
fixtures are development infrastructure only. Production runtime/migrator users,
TLS, backups and least-privilege deployment belong to plan 10. Never reuse the
fixed disposable-test credentials outside tests.

The named volume preserves local data across container replacement and normal
`docker compose --env-file .env.postgres down`. Password/database initialization
variables apply only when storage is empty; editing the env file does not rotate
an existing database password. Do not delete the volume as a routine fix. Do not
run `down --volumes` unless intentionally discarding local database data.

### Sessions, transactions and lifecycle

Lifespan provides `app.state.database`, an instance of
`statistics_agent.persistence.database.Database`. Imports, app construction and
pool construction perform no database I/O; the first operation checks out a
connection. Startup may succeed during an outage; readiness now reports 503 until the database and schema are usable.
Shutdown disposes idle connections after operations finish and clears the state.
A closed Database rejects future sessions; each process/lifespan gets its own pool.

```python
from sqlalchemy import text

async with app.state.database.session() as session:
    result = await session.scalar(text("SELECT 1"))
    # Future writes must explicitly call await session.commit().
```

Each operation owns its session and explicitly commits or rolls back. Leaving
without committing, an exception, or task cancellation rolls back pending work
and returns the connection. Never share a session across concurrent tasks or hold
one across LLM, identity or remote-tool calls. `DB_POOL_SIZE` is the hard pool cap
(`max_overflow=0`); acquisition waits use DB_POOL_TIMEOUT_SECONDS. Psycopg receives
DB_CONNECT_TIMEOUT_SECONDS, and each connection sets PostgreSQL statement_timeout
from DB_STATEMENT_TIMEOUT_MS. libpq connection timeouts apply per connection
attempt/address and have a two-second minimum. Pre-ping checks stale connections;
SQL echo is disabled and bound parameters are hidden in SQLAlchemy errors. Raw
SQL/driver exceptions must still not be logged or exposed by callers. Existing
safe HTTP handling contains database failures. DB_HEALTH_TIMEOUT_SECONDS now bounds the readiness probe work. No schema creation runs at startup.

### Disposable database tests and CI

[compose.test.yaml](compose.test.yaml) creates a separate `agent_test_db` under
user `agent_test`, bound to `127.0.0.1:54330`, with tmpfs storage and fixed test-only
credentials. It never mounts the persistent local volume. From this directory:

```powershell
docker compose -f compose.test.yaml up -d --wait
$env:TEST_DATABASE_URL = 'postgresql+psycopg://agent_test:test-only-password@127.0.0.1:54330/agent_test_db'
python -m pytest -q
Remove-Item Env:TEST_DATABASE_URL
docker compose -f compose.test.yaml down
```

Only use an explicitly disposable PostgreSQL instance with these test database
and role names. Tests create random `foundation_<uuid>` schemas and drop only
those schemas; they never create business models or use SQLite. A missing URL
skips marked `postgres` checks locally, but fails those checks in CI. An invalid
URL, unavailable server or wrong database/role fails rather than skips.

The existing required `tests` job now starts its own PostgreSQL service and sets
TEST_DATABASE_URL. It still runs pytest only after dependency installation;
no new workflow/check name or lint gate is introduced. The local full suite
proved commit visibility, implicit/explicit/error rollback, pool/statement/connect
bounds, cancellation cleanup, backend connection disposal and safe HTTP failures.
Local `agent_db` connection and empty public schema were also verified. The
GitHub run for this branch remains pending; local passes do not prove CI execution.
The disposable local test container/network were removed after verification;
the persistent local agent database remains running.

Sources: [SQLAlchemy async sessions](https://docs.sqlalchemy.org/en/20/orm/extensions/asyncio.html),
[official PostgreSQL image and initialization](https://hub.docker.com/_/postgres),
[Psycopg Windows asyncio requirements](https://www.psycopg.org/psycopg3/docs/advanced/async.html).
Step 1.8 now supplies an explicit empty baseline; no business tables were added.

## Alembic baseline and schema gate — step 1.8

Migration scripts live inside `src/statistics_agent/persistence/migrations/`, so
they ship in the application package. `alembic.ini` points to that package and
contains no credentials. `Base.metadata` in `persistence/models.py` is currently
empty; future agent models use it. Keycloak manages its own schema separately.
The explicit baseline revision is `0001_agent_baseline`. Upgrading an empty
agent database creates only `alembic_version`; no user, identity, dataset, run or
outbox tables are created. Repeating the upgrade is harmless at this revision.
No migration, stamp or schema repair runs during API/worker startup.

From the service directory with `.venv` activated, initialize local agent storage:

```powershell
python -m statistics_agent.persistence.migrate upgrade --env-file .env
python -m statistics_agent.persistence.migrate check --env-file .env
```

Without `--env-file`, commands use injected process variables only. Commands
return exit code 0 for success and 1 for a mismatch/failure. Output uses fixed
safe messages, never URLs, SQL, raw exceptions or rejected values. The check
is read-only: it compares database heads with the packaged migration heads,
without creating a version table or silently stamping an unknown revision.
`read_schema_status(connection)` is used by the readiness route added in 1.9. Offline SQL generation needs no DB:

```powershell
python -m alembic upgrade head --sql
```

The wrapper accepts only canonical agent database names `agent_db` and
`agent_test_db`; the online Alembic environment also validates current_database()
before DDL. Identity/unrelated targets are rejected. This is a configuration
safeguard, not a substitute for separate deployment credentials/network access.
No identity service/database was contacted during verification.

Downgrade verification belongs only on the explicitly disposable test database.
With APP_ENV=test and DATABASE_URL pointing to agent_test_db, the wrapper offers
`python -m statistics_agent.persistence.migrate downgrade-test`. This returns to
base and may leave an empty Alembic version table. The wrapper rejects this
operation for persistent agent_db. Raw Alembic administration remains powerful;
never use it to casually downgrade deployed data. Later revisions must specify
compatibility and recovery; rolling back an app image does not undo a migration.

The release design runs one migration job before rolling out the application,
not a migration in every API replica. The deployment pipeline must serialize
release/migration jobs. That Azure/Kubernetes automation is not implemented here.

Verification: **138 tests pass**, including 16 new migration checks. Real
PostgreSQL tests cover empty-schema upgrade, repeated upgrade, downgrade and
re-upgrade, an unknown revision, failed-DDL rollback and explicit dotenv-backed
CLI execution. Tests use disposable agent_test_db and isolated random schemas;
production/local agent data is never used for rollback tests. The wheel was
built and inspected for env/template/baseline files, and revision generation
from the template succeeded. Ruff lint/format and `pip check` pass. One existing
TestClient/HTTPX warning remains. The existing required `tests` CI job discovers
these checks automatically; this branch's GitHub run remains pending.

During 1.8, the local database was checked read-only and was then unmigrated.
At 1.9 verification it already matched the expected schema; both local probes
returned 200. No persistent migration or data change was performed in 1.9.
Fresh databases still need the explicit upgrade command before readiness passes. Disposable test resources
were removed afterward. No new dependency, Dockerfile, deployment or health
route was introduced. Implementation branch: `feature/alembic-baseline`.

Sources: [Alembic async/connection sharing](https://alembic.sqlalchemy.org/en/latest/cookbook.html#using-asyncio-with-alembic),
[revision inspection](https://alembic.sqlalchemy.org/en/latest/api/runtime.html#alembic.runtime.migration.MigrationContext.get_current_heads).

## Liveness and readiness — step 1.9

- `GET /health/live` returns 200 with `{"status":"alive"}` when the process can
  serve HTTP. It never accesses PostgreSQL, Keycloak, Kafka or an LLM.
- `GET /health/ready` returns 200 with `{"status":"ready"}` only after lifespan
  started, a database connection/SELECT 1 succeeds, and the recorded Alembic heads
  exactly match this release's packaged heads. Otherwise it returns a safe 503
  error envelope with code `not_ready`, message `Service not ready`, request ID
  and empty details. Missing migrations are not applied by the probe.

Both endpoints are available without login in all environments, return
`Cache-Control: no-store` and preserve X-Request-ID correlation. Development
OpenAPI lists these foundation routes and the readiness 503 error model;
staging/production still disable API documentation. No credentials, addresses,
revision strings or driver failures appear in the health responses.

The readiness work uses DB_HEALTH_TIMEOUT_SECONDS for pool acquisition, SQL and
schema inspection. Connection context managers cancel/rollback/close the probe
operation on failure/timeout. External task cancellation is not converted into
an authorization or database result. Each probe checks current state rather than
using cached positive readiness, so repair can restore readiness without an app
restart. Successful GET health requests are excluded from normal request logs;
unhealthy probes still emit safe status/correlation metadata. Requests to other
paths and invalid methods retain normal logging.

Kubernetes should use liveness for process restart decisions and readiness for
traffic eligibility. A database outage must not trigger liveness restart loops.
These endpoint semantics follow [Kubernetes probes](https://kubernetes.io/docs/concepts/workloads/pods/probes/).
Kubernetes manifests, deployment rollout policy and monitoring remain plan 10.

From the service directory, start the API using the documented command, then:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health/live
Invoke-RestMethod http://127.0.0.1:8000/health/ready
```

Fresh local storage first needs the explicit migration upgrade from step 1.8.
Read-only local smoke checks returned 200/alive and 200/ready during 1.9; no
persistent data was changed. The full disposable-PostgreSQL suite passes 150
tests, including 12 new health checks covering safe failures, timeout/cancellation,
missing/wrong schema, no automatic migration, pool/query recovery, connection
failure, development documentation and low-volume probe logging. Ruff lint/format
and dependency consistency pass; one existing TestClient/HTTPX warning remains.
No dependency, Docker image, cloud resource or additional CI job was introduced.

## Naming update — 23 October 2026

On `refactor/datalens-service-names`, all owned process/Compose service names
use `datalens-`: API/worker, persistent database and disposable test database.
The identity repository folder is now `services/datalens-identity/`.
This does not rename upstream image repositories, database identifiers or the
Python package, and it does not add business functionality.

The volume keeps its existing Compose ownership key; its explicit physical name
is `datalens-agent-local_agent-postgres-data`, preserving existing local data and
ownership labels. Rendering was checked without changing the persistent database. An already
running old-name container is left untouched; after merging, stop the local API
and use `docker compose --env-file .env.postgres down --remove-orphans` followed by
`docker compose --env-file .env.postgres up -d --wait` when ready to switch it.
Never add `--volumes`/`-v` to that operation.

Before the rename, all 150 tests passed. Afterward, **154 tests pass**, including
four cases proving canonical and legacy process inputs normalize correctly.
Ruff lint/format pass; one existing TestClient/HTTPX warning remains. The tests
used the renamed disposable PostgreSQL service; persistent data was not changed.
No implementation commit, push or PR was performed; required GitHub CI is pending.

## Development status

Steps 1.1–1.9 are merged into main; health PR #6 reached `58da046` with
successful required tests. The naming branch has 154 passing tests; main's
pre-rename suite had 150. Step 1.10 has local fresh-environment evidence, but its
separate verification document is absent from fetched main; documentation
delivery/final sign-off remain pending. This naming PR does not publish it or
implement the next identity step.
Missing TEST_DATABASE_URL skips marked checks locally and fails them in CI;
skipped database tests must not be reported as passed. From the service directory:

```powershell
python -m ruff check .
python -m ruff format --check src tests
python -m pytest -q
```

Identity configuration belongs in `services/datalens-identity/` when plan 2 starts.
Keycloak owns the separate `identity_db`; agent migrations do not manage it.
Credentials belong in ignored local configuration or deployment secret injection.

## Version control

This service folder is its own Git repository; `.git/` already exists here.
The parent workspace contains planning documents and other services outside
this repository. Do not initialize the parent workspace just to track this agent.
The service-level `.gitignore` excludes `.venv`, caches, build/coverage output,
logs and dotenv secrets while retaining `.env.example`.

The GitHub remote is already configured at
[datalens-agent](https://github.com/Agent-Tool-Use-Guardrail-Monitor-ATGM/datalens-agent).
Every change, including basic setup, documentation and workflow files, uses a
separate branch and a PR targeting `main`. Never commit or push directly to
`main`. Follow the branch and PR rules in this section.

For a new task, start from a clean working tree and an updated `main`:

```powershell
git status
git switch main
git pull --ff-only origin main
git switch -c feature/fastapi-foundation
```

Use `<type>/<short-kebab-case-description>`. Prefixes are `feature/` for new
functionality, `fix/` for bugs, `refactor/` for behavior-preserving restructuring,
`docs/` for documentation-only work, `test/` for tests-only work, `ci/` for workflows
and `chore/` for maintenance such as dependency, ignore-file or tooling updates.
Choose the main purpose; a feature branch includes its own tests and documentation.
Use `feature/` consistently instead of `feat/`. All prefixes require PR/CI checks.
If there is uncommitted work, preserve it on its task branch before updating the
base; do not discard it or mix unrelated changes. These commands are for the
next clean task, not for switching away from the current pending work.

Run the checks listed above, review the diff, and stage only intended files.
When authorized to publish, commit on the task branch and push that branch:

```powershell
git branch --show-current
git diff --cached
git commit -m "Add FastAPI service foundation"
git push -u origin HEAD
```

Confirm the displayed branch is the task branch before committing. Open a PR
with base `main` using GitHub or `gh pr create --base main`. Merge only after all
required checks pass for the latest PR revision and the branch satisfies the
repository rules. Changes to a PR must pass the checks again. After merge, update
local `main` and create a new task branch for the next step.

### GitHub Actions and merge-gate status

The tests-only [ci.yml](.github/workflows/ci.yml) runs job `tests` on every PR to
`main` and again on main after merges, with read-only permissions and a ten-minute
timeout. It uses Python 3.12 and PostgreSQL 17.11 on Ubuntu 24.04. Dependency and
package installation precede pytest. No application secrets or local `.venv`
are used; the database service uses disposable test-only credentials. No path
filters skip documentation PRs. Ruff and `pip check` remain local checks.

PR #2 delivered the initial workflow and contracts with passing GitHub checks;
step 1.6 is also merged into origin/main. The user confirmed prerequisite branch
protection; effective remote rules were not independently audited. Keep `tests`
required, require PRs and an up-to-date branch, and restrict direct-push/bypass
permissions. Steps 1.7–1.9 are merged into fetched origin/main. The naming refactor requires
its own PR and latest-revision tests; nothing was staged, committed, pushed or
merged during this change.

Planning documents in the parent workspace are outside this Git repository and
will not appear in its PR. Changes to them are reported separately until they
have their own version-control location.


Internal service names are logical and omit the product prefix. Repository and
folder names retain `datalens-`; Compose projects retain environment scope.
Existing physical volume names are preserved to avoid replacing stored data.


## Authentication configuration — step 3.1

Implemented locally on `feature/auth-configuration`. See
[configuration, secret formats and verification](docs/auth-configuration.md).
New opt-in `AuthSettings`, explicit secret-file loading and immutable safe
principal/failure contracts are available. PyJWT/cryptography are pinned in the
existing requirements.txt; `.env.auth.example` contains references only and
`secrets/` is ignored. No real credentials were generated or read.

At completion of step 3.1, the FastAPI factory did not load auth configuration. No new route, session
storage, migration, login, refresh or permission enforcement is implemented;
existing health behavior remains intact. Docker stayed closed. The local
`datalens` realm still needs operator onboarding before live integration.

Focused tests: **47 passed**. Complete non-database regression selection:
**181 passed; 20 PostgreSQL tests deselected**. Ruff/format/pip checks and in-memory
RS256/Fernet compatibility checks passed. One existing Starlette/HTTPX warning
remains. No live Keycloak/database suite or prior remote checks were repeated.

Pre-existing naming edits in this checkout were preserved. No changes were staged,
committed or pushed. Separate those edits from this feature before publication;
do not assume the whole working-tree diff belongs to 3.1. Service documentation
and implementation belong in its PR; parent planning files remain outside Git.
Step 3.1 delivery is now accepted from the user's pull/check confirmation.


## Planned local token authorization — 27 October 2026

Per the revised plan, protected requests will validate signed JWTs locally using
trusted cached keys and local session state. No per-request Keycloak introspection.
Token lifetime stays at most five minutes (initial expiry leeway zero); remote
role/email/account/logout changes may take until expiry to affect access. Local
application logout still invalidates its session immediately. Known keys within
a maximum one-hour trust lease support valid-token requests through identity
outage; expired tokens, missing/untrusted keys, login and renewal cannot bypass
necessary provider checks. Unknown kid/lease expiry triggers bounded key retrieval.
No new gateway, broker or revocation service. Keep encrypted backend tokens and
opaque cookie/CSRF design. See [the configuration guide](docs/auth-configuration.md)
and updated identity handoff. This policy revision changed documentation only;
active agent login and authorization remain unimplemented.

## Encrypted session storage — step 3.2

Implemented locally on `feature/session-storage`. See
[storage behavior, migration and verification](docs/auth-storage.md).
Migration `0002_auth_storage` adds agent-owned pending logins and browser sessions.
Random browser credentials/state are hashed; OAuth tokens and nonce/PKCE material
are encrypted with context-bound Fernet envelopes and external active/read keys.
Repositories enforce one-use login consumption, browser binding, session deadlines,
local revocation, version-checked credential updates and bounded explicit cleanup.

All **221 tests passed**, including real disposable PostgreSQL migration and
concurrency checks; Ruff and pip consistency checks passed. Existing required CI
already runs the new tests. No runtime dependency or identity change was needed.
No auth route or startup wiring was added; persistent development data was not
migrated. Readiness requires the new migration before running this revision
against an existing database. No real credentials were created.

Existing local-token-policy documentation edits were preserved in this checkout;
they are compatible with this step and are not claimed as separately published.
No commit/push occurred. Parent plans are outside this repository/PR.
Step 3.2 delivery/pull is accepted from the user confirmation; subsequent 3.3
implementation is recorded below.


## Browser login and callback — step 3.3

Implemented locally on `feature/browser-login`. See [setup, flow and fixture
commands](docs/browser-login.md). GET `/auth/login` redirects to Keycloak with
browser-bound state, nonce and S256 PKCE. GET `/auth/callback` consumes the attempt,
exchanges the code, verifies ID/access JWTs and signed email/role claims, then
stores encrypted tokens and sets a fresh opaque HttpOnly session cookie. No
OAuth tokens reach application responses or JavaScript. Auth responses use
no-store/no-referrer; errors are fixed 401/403/503, or 409 for a pending login.

The factory now requires auth configuration/secrets. Discovery/signing keys are
fetched lazily, coalesced and cached for at most one hour; refresh attempts are
limited to once per five seconds per process. Algorithms/endpoints are trusted
configuration, never token-supplied URLs. No introspection is performed. Health
semantics remain unchanged and startup performs no identity network request.

Verification: **274 tests passed**, no skips, including eight real Chromium +
Keycloak + PostgreSQL scenarios, 31 provider/signature/cache tests and 14 HTTP
protocol checks. Ruff, touched-file formatting and pip consistency checks passed.
One existing Starlette/HTTPX deprecation warning remains. Existing `tests` CI now
installs Chromium, starts disposable Keycloak and always cleans it up. The browser
fixture uses synthetic identities and temporary H2 storage; production identity
PostgreSQL and real accounts were untouched. Earlier failed fixture runs involved
a realm-import filename, redirect interception and an error-theme selector; these
were corrected. A stale health-only OpenAPI assertion was updated for auth routes.

At completion of 3.3 there was no frontend, `/auth/me`, protected-operation CSRF,
ownership, refresh or logout. Step 3.4 below now adds request authorization. Successful login redirects to `/`,
which currently returns 404; the session cookie is created. Public protected-route
behavior and automatic refresh remain steps 3.4/3.5, not claimed by login success.
No real secret files were generated and no persistent database was migrated.

No commit/push/remote CI result is claimed. Publish this branch through its PR
with required `tests`. Parent plans/README remain outside service Git/PRs. Stop
before 3.4; it requires a separate request.


## Protected requests and ownership — step 3.4

Implemented locally on `feature/token-authorization`. Step 3.3 delivery/pull is
accepted from the user's confirmation. See [request behavior and route integration](docs/request-authorization.md).
GET `/auth/me` returns safe identity/roles, verified-email status and a session-
bound CSRF token. It never returns the browser credential or OAuth tokens.
Reusable user/admin dependencies validate the local session and signed JWT on
every operation, match issuer/subject, enforce roles and conditionally update
idle activity only after successful authorization. Admin includes user permission.

Cookie-authenticated writes require one exact trusted Origin and one session-bound
X-CSRF-Token header. Responses from protected dependencies use no-store/no-referrer.
The SQL ownership helper filters both issuer and subject inside the query; admin
cannot bypass ownership. Resource tables/routes used to prove this remain test-only.

All **305 tests passed**, no skips, including 31 new PostgreSQL-backed access
checks and real Chromium `/auth/me` after Keycloak login. The expiry-boundary
check also passed a focused rerun after a slow-CI timing assumption was removed.
Ruff, touched formatting and pip consistency checks passed. One pre-existing
Starlette/HTTPX warning remains. Existing required `tests` CI collects the new
cases; no job, migration, dependency or configuration addition was necessary.

Ordinary valid-token requests use cached trusted keys without introspection or
identity lookup. Invalid/expired local state or tokens return 401, permission
failures 403 and required key/DB outages safe 503. Role changes may lag until
JWT expiry; local session revocation denies the old cookie. At completion of 3.4, automatic refresh/logout remained pending; step 3.5 below
now implements them. Dataset/run APIs and frontend remain unimplemented. Persistent data,
real accounts and identity repository were untouched.

No commit/push/remote CI result is claimed. Publish `feature/token-authorization`
through a PR and required `tests`; parent plans stay outside service Git/PRs.
Step 3.4 delivery/pull is now accepted from the user confirmation.


## Automatic refresh and logout — step 3.5

Implemented locally on `feature/session-refresh-logout`; see
[protocol, migration and recovery](docs/session-refresh-logout.md). An otherwise-
valid expired JWT renews in the backend, keeping the same opaque cookie and CSRF
proof. Fresh signed identity/role claims are validated before committing rotated
credentials. Permission/signature/owner failures do not initiate renewal.

Migration `0003_refresh_claim` adds a durable claim protected by short PostgreSQL
row locks. One replica exchanges a refresh credential; others use its stored
replacement. No network call holds a DB transaction open. A crash, uncertain
response or failed commit never causes reuse of the old refresh token. Stale
claims revoke the session; recovery requires login. Required claims use an explicit
bounded lease and operation deadline derived from existing HTTP settings.

POST `/auth/logout` checks exact Origin/session-bound CSRF, commits local
revocation, attempts standard Keycloak RP-Initiated Logout POST and clears the
cookie. It reports provider failure without restoring local access. Logout works
with expired JWTs or lost roles and during provider outage; it still needs the
agent DB. An in-flight operation cannot be undone and a concurrent refresh cannot
restore a revoked local session. Provider cookies/tokens are never forwarded to
the browser or put in URLs.

Verification: **331 tests passed**, no skips, including 26 lifecycle checks and
nine real Chromium/Keycloak scenarios. Two FastAPI instances share one renewal;
real natural expiry rotates credentials with stable cookie/CSRF, then logout
causes Keycloak to reject the saved refresh token. Crash/timeout/cancel/logout
races, claim/credential commit uncertainty, role changes and safe migration
rollback are covered. Ruff/format/pip checks passed; one existing Starlette/HTTPX
warning remains. No new dependency, real secrets, persistent development migration
or identity repository change. Broader retained-storage/provider-state handoff is 3.6.

The earlier CI visibility patch was discarded exactly as requested. User confirms
3.4 succeeded and was pulled; that disabled-login check remains unchanged.
No commit/push/remote CI result is claimed. Publish this branch through a PR and
required `tests`. Parent plans remain outside service Git/PRs. Apply the new
migration before running this revision against an older persistent database.
Step 3.5 delivery/pull is now accepted from the user confirmation; subsequent
3.6 verification is recorded below.


## Access integration and handoff — step 3.6

Completed locally on `test/access-integration`; see [complete handoff](docs/access-handoff.md).
Eight additional real integration cases prove captured email activation, actual
API process restarts, encryption read-key rotation/loss/restoration, corrupt/
missing key startup failure, actual Keycloak role/email/account/logout changes
at JWT expiry, and real identity outages through warm/cold keys, expiry and local
logout. The same old cookie remains denied after local revocation and restart.

The isolated identity fixture now has its own PostgreSQL database on tmpfs and
Mailpit, separate from the agent fixture/developer storage. It contains synthetic
realms/users and a test-only administrator. Outage control targets only the fixed
test Compose project. CI keeps the required job named `tests`, uses a bounded
240-second identity startup and always cleans that explicit project afterward.
No browser/mail/OAuth artifacts are published.

Full regression: **339 passed**, no skips, in 289.31 seconds. Ruff, touched-file
formatting and pip checks passed; one existing Starlette/HTTPX warning remains.
Application code, dependencies and migrations were unchanged by this step.
No persistent developer DB, real account/key or identity repository was touched.
All prior protocol, ownership, CSRF, RSA/JWKS, two-instance and durable-refresh
checks remain in the same gate. Actual Keycloak signing-key rotation, production
SMTP/TLS, backup restoration and cloud delivery are not claimed.

The handoff documents local provisioning, same-origin frontend cookie/CSRF use,
401/403/503 handling, agent principal/ownership rules, restart/key recovery,
cleanup and outage bounds. No frontend or business agent was implemented.

Step 3.6 delivery is now accepted from the user pull confirmation. Parent
plans/README remain outside service Git/PRs.


## Agent interfaces — step 4.1

Complete locally on `feature/agent-contracts`; PR/required `tests` CI pending.
[Agent contracts and remaining handoffs](docs/agent-contracts.md) define provider
request/response, usage/failure, authorized context, versioned prompt/state and
async provider/tool/policy/event interfaces. Unknown usage stays null; original
malformed tool proposals retain identity and arguments before validation.

Verification: **263 passed, 101 database/browser tests deselected**; one existing
Starlette/HTTPX warning. No new dependencies, migrations, runtime endpoints or
provider calls. CrewAI integration and real tools remain later steps.

Stop before 4.2 provider profiles/secrets. User publishes this branch through a
PR to main; no commit, push or remote CI success is claimed here.
