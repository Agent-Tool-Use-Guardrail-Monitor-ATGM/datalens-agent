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
Database connections and timeout enforcement remain step 1.7 work.

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
actual database resource creation/disposal remains step 1.7 work.

From the service directory with its `.venv` activated, create an ignored `.env`
from `.env.example` if needed, replace placeholder configuration, and start:

```powershell
python -m uvicorn statistics_agent.app:create_app --factory --env-file .env --host 127.0.0.1 --port 8000 --no-access-log
```

The explicit `--env-file` is local development only; deployment injects variables
and omits it. The PostgreSQL URL must be structurally valid, but the shell does
not connect to it yet. `/docs` and `/openapi.json` exist only in development/test;
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

## Development status

Steps 1.1–1.5 are complete. PR #2 delivered contracts and CI with passing tests;
the user confirmed repository protection before authorizing step 1.6. The remote
rules were not independently audited. Step 1.6 is locally complete on
`feature/fastapi-foundation`, awaiting commit, PR and GitHub verification.
Next implementation is 1.7, PostgreSQL and transaction lifecycle, when requested.
The full suite passes 111 tests (34 settings, 53 contracts, 24 application checks).
Ruff lint/format and `pip check` pass. TestClient emits one upstream warning about
HTTPX integration; it is not suppressed and dependencies were not changed. From the service directory:

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

**Implemented locally — 23 October 2026:** [ci.yml](.github/workflows/ci.yml)
runs the `tests` job on PRs targeting `main` and pushes to `main` after merges.
There are no path filters, so documentation-only PRs also run it. The workflow
checks out the repository, prepares Python 3.12 on Ubuntu 24.04, installs
`requirements.txt` and the local package, then runs `python -m pytest -q`.
It has read-only repository permissions and a ten-minute job timeout. No
application secrets, database or local `.venv` are required. Actions follow
their official [checkout](https://github.com/actions/checkout/tree/v7) and
[setup-python](https://github.com/actions/setup-python/tree/v7) interfaces.

At the user's request, CI currently runs tests only. Dependency installation is
setup; lint, formatting and `pip check` remain local commands, not CI checks.
The earlier proposed `quality` check is replaced by `tests` for this scope.
Local verification passed all 87 tests on Windows. The sandbox's temporary
directory initially prevented test setup; the unrestricted rerun passed without
code changes. Linux execution and Actions orchestration await the first PR run.

**Delivered:** [PR #2](https://github.com/Agent-Tool-Use-Guardrail-Monitor-ATGM/datalens-agent/pull/2)
merged the contracts and workflow into `main` with a successful `tests` run.
The user confirmed repository settings were ready before step 1.6; effective
remote rules were not independently audited. The old policy branch was removed
and PR #1 closed. Keep `tests` required, require a PR and an up-to-date branch,
and restrict direct pushes/bypasses. Step 1.6 has only local verification until
its own PR runs CI. Database integration checks join the gate in steps 1.7–1.9.

Planning documents in the parent workspace are outside this Git repository and
will not appear in its PR. Changes to them are reported separately until they
have their own version-control location.
