# DataLens statistics-agent development

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
no business/authentication or health endpoints yet. HTTPX/TestClient test routes
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
connection. Startup may succeed during an outage; readiness is implemented in 1.9.
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
safe HTTP handling contains database failures. DB_HEALTH_TIMEOUT_SECONDS is
configuration only until readiness in 1.9. No schema creation runs at startup.

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
Alembic migrations remain step 1.8; no business tables were added in 1.7.

## Development status

Steps 1.1–1.6 are merged into `main`. Step 1.7 is locally complete on
`feature/postgres-foundation`, awaiting commit/PR/CI delivery. Local verification
passes 122 tests: 111 earlier checks and 11 real PostgreSQL integration checks.
Ruff lint/format and `pip check` pass. One existing upstream TestClient/HTTPX
warning remains. Next implementation, when requested, is 1.8 (Alembic baseline).
Use the database test setup below for the full suite; without TEST_DATABASE_URL,
local database tests are explicitly skipped and must not be counted as passed. From the service directory:

```powershell
python -m ruff check .
python -m ruff format --check src tests
python -m pytest -q
```

Identity configuration belongs in `services/identity/` when plan 2 starts.
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
permissions. Step 1.7 on `feature/postgres-foundation` has local evidence only
until its own PR runs CI; nothing was staged, committed, pushed or merged here.

Planning documents in the parent workspace are outside this Git repository and
will not appear in its PR. Changes to them are reported separately until they
have their own version-control location.
