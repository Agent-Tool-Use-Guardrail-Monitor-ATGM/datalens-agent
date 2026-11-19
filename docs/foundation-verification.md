# Foundation verification — step 1.10

Verified locally on 23 October 2026, Windows x64, CPython 3.12.15. Final sign-off
is pending the step 1.9 health-check PR and required GitHub CI. No business,
identity, frontend, vector-search or deployment functionality is added here.

## Source and environment isolation

The candidate was a clean file snapshot of committed `origin/main` at `dd332a0`
plus the six pending health-check files from `feature/health-checks`: README,
`api/http.py`, `app.py`, `tests/test_app.py`, `api/health.py` and
`tests/test_health.py`. It was not a clean Git checkout of a merged release.
Tracked files and the two explicitly named new health files were copied; Git
metadata, existing environments, caches and local secrets were not copied.

A fresh `.venv` was created in that disposable snapshot using the documented
standalone Python 3.12 runtime and `python -m venv .venv`. Dependencies were
installed from `requirements.txt`, followed by
`python -m pip install --no-deps --editable .`. The service's existing environment
and pending source files were not changed. No Conda, uv or additional lockfile
was used. The temporary environment and test container were removed afterward.

## Reproduce after the health-check PR is merged

Use a fresh checkout and run from its service/repository root. These commands
assume Python 3.12 is registered with the Windows launcher; the README also
documents the standalone-runtime alternative. Docker Desktop's Linux engine
must be running. Port 54330 must be free.

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m pip install --no-deps --editable .
python -m pip check
docker compose -p datalens-foundation-signoff -f compose.test.yaml up -d --wait
$env:TEST_DATABASE_URL = 'postgresql+psycopg://agent_test:test-only-password@127.0.0.1:54330/agent_test_db'
python -m ruff check .
python -m ruff format --check src tests
python -m pytest -q
```

All database tests must run. Missing TEST_DATABASE_URL skips them locally and
fails them in CI; a skipped database test is not successful verification.
Use only the disposable `agent_test_db`/`agent_test` fixture for these checks.
The persistent `agent_db`, its named volume and identity storage are not used.

After the suite, verify explicit migration and real API startup against that
same disposable database:

```powershell
$env:APP_ENV = 'test'
$env:DATABASE_URL = $env:TEST_DATABASE_URL
python -m statistics_agent.persistence.migrate upgrade
python -m statistics_agent.persistence.migrate check
python -m uvicorn statistics_agent.app:create_app --factory --loop statistics_agent.core.event_loop:create_event_loop --host 127.0.0.1 --port 8000 --no-access-log
```

In another terminal, request `/health/live` and `/health/ready` on
`http://127.0.0.1:8000`; both must return 200, with `alive` and `ready` respectively.
Stop the API with Ctrl+C. In the original terminal, remove the test resources
even if verification failed:

```powershell
docker compose -p datalens-foundation-signoff -f compose.test.yaml down
Remove-Item Env:TEST_DATABASE_URL, Env:DATABASE_URL, Env:APP_ENV -ErrorAction SilentlyContinue
```

The test container uses tmpfs, so removing it discards its test database. Do not
use the persistent-development Compose project or delete its volume for testing.
Disposable checkouts/environments can then be removed; keep the service's real
`.venv` and its base Python runtime. No Azure, LLM, Kafka or Keycloak secrets are
needed for this procedure.

## Recorded results

- Requirements installation and editable package installation succeeded in the
  fresh environment; `pip check` reported no broken requirements.
- `pytest -q`: **150 passed, no skips**, with CI=true and disposable PostgreSQL
  17.11. One existing Starlette TestClient/HTTPX deprecation warning remains.
- Ruff lint passed; Ruff format confirmed all 26 Python files were formatted.
- The suite proves import/factory isolation without network access, safe HTTP
  behavior, PostgreSQL transaction/time-out cleanup, migration round trips and
  liveness/readiness failure and recovery.
- After tests restored the public schema to base, migration `check` returned 1;
  explicit `upgrade` and `check` returned 0. A real Uvicorn subprocess then served
  200/alive and 200/ready with no-store headers and was stopped afterward.
- The unchanged required `tests` workflow provisions PostgreSQL and runs the
  complete pytest suite on PRs to main and main pushes. Configuration inspection
  confirms coverage; this is not proof of a new GitHub Actions run.

Resolved versions (evidence only, not another dependency specification):
alembic 1.20.0; annotated-doc 0.0.5; annotated-types 0.8.0; anyio 4.15.1;
certifi 2026.10.23; click 8.5.0; colorama 0.4.6;
datalens-statistics-agent 0.1.0; fastapi 0.158.0; greenlet 3.5.6; h11 0.16.0;
httpcore 1.0.9; httpx 0.28.1; idna 3.20; iniconfig 2.3.1; Mako 1.4.3;
MarkupSafe 3.0.4; opentelemetry-api 1.45.1; packaging 26.3; pip 26.2.2;
pluggy 1.7.0; psycopg 3.3.6; psycopg-binary 3.3.6; pydantic 2.14.1;
pydantic_core 2.50.1; pydantic-settings 2.16.1; Pygments 2.21.0; pytest 9.1.1;
python-dotenv 1.2.4; ruff 0.17.1; SQLAlchemy 2.0.55; starlette 1.9.0;
typing_extensions 4.16.0; typing-inspection 0.4.4; tzdata 2026.5; uvicorn 0.54.0.
Future installations may resolve different transitive versions and must rerun
verification. Linux/cloud runtime behavior and release automation belong to
plan 10; Windows results do not establish those guarantees.

## Delivery boundary

This document is on `chore/foundation-signoff`, based on committed main. It does
not copy or commit the pending health implementation. Merge the health PR with
required `tests` passing, update this branch to the resulting main, review its
documentation and require passing CI before merging it. Final foundation sign-off
must wait for that integrated revision. No commit, push, PR or merge was performed
during this local verification. Stop before plan 2 implementation.
