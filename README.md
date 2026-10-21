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
setup is verified in step 1.9 and Linux deployment in plan 10. No Conda YAML or
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

The application factory will load and share one settings object in step 1.5;
timeout enforcement and database connections are wired in step 1.6. This step
validates their configuration only. There is no startup command yet.

## Development status

Steps 1.1–1.3 are complete. Step 1.4 and later functionality remain pending.
Configuration has 34 focused checks covering valid/invalid values, explicit
dotenv loading, precedence, timeout relationships, secret redaction and absence
of import/load network side effects. From the service directory:

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

From `services/datalens-agent/`, review and create the first commit:

```powershell
git status
git add .gitignore .env.example pyproject.toml requirements.txt README.md src tests
git diff --cached --stat
git commit -m "Initialize DataLens agent foundation"
```

To publish, create an empty remote repository, for example `datalens-agent`, in
your Git hosting account. Connect its provided URL with `git remote add origin
<REPOSITORY_URL>`, then push the current branch using `git push -u origin HEAD`.
Replace the placeholder URL before running the command. No remote, staging,
commit or push is performed by adding the ignore file.
