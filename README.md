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

The application factory will load and share one settings object in step 1.6;
timeout enforcement and database connections are wired in step 1.7. This step
validates their configuration only. There is no startup command yet.

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
values or exception context. The HTTP handlers in step 1.6 will supply safe
messages and locations rather than copying raw validation errors.
`resolve_request_id` accepts canonical 36-character UUID headers and otherwise
generates a new UUID. Header wiring and response propagation also belong to 1.6.

`tests/fixtures/events_v1.json` contains seven independent wire examples for
serialization compatibility, not a complete execution trace. Unknown usage
remains `null`. Unknown top-level fields/versions are rejected; adding fields
requires compatible readers before producer rollout, or a schema-version change.

## Development status

Steps 1.1–1.4 are locally complete; their remote merge status is unverified.
Step 1.5 is in progress: the tests-only workflow exists locally, while its first
GitHub run and required-check enforcement still need verification.
FastAPI lifecycle/errors/logging moves from 1.5 to 1.6; the other former steps
1.6–1.9 become 1.7–1.10. API functionality remains pending.
Configuration has 34 focused checks covering valid/invalid values, explicit
dotenv loading, precedence, timeout relationships, secret redaction and absence
of import/load network side effects. Contracts add 53 checks, bringing the
current suite to 87 passing tests. From the service directory:

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
`main`. The persistent rules are in [AGENTS.md](AGENTS.md).

For a new task, start from a clean working tree and an updated `main`:

```powershell
git status
git switch main
git pull --ff-only origin main
git switch -c feat/fastapi-foundation
```

Use `feat/<topic>`, `fix/<topic>`, `docs/<topic>` or `chore/<topic>` as appropriate.
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

**Pending:** push branch `chore/tests-ci` and open a PR targeting `main`, then
verify that GitHub runs `tests` successfully. This branch includes the previous
local contracts commit because it is based on `chore/github-workflow-policy`;
review the whole PR diff if that commit is not yet on remote `main`.
No commit, push, PR, merge or repository-settings change was performed here.
Configure the `main` rule to require PRs and the actual `tests` status check,
require an up-to-date branch, and restrict direct pushes/bypasses. Remote rules
have not been inspected; local passes do not prove enforcement. Step 1.5 remains
in progress until the GitHub run and merge gate are verified. Database
integration checks join the gate when steps 1.7–1.9 introduce that functionality.

Planning documents in the parent workspace are outside this Git repository and
will not appear in its PR. Changes to them are reported separately until they
have their own version-control location.
