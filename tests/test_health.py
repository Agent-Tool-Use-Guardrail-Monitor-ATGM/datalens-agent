"""Health behavior through HTTP, with controlled failures and real PostgreSQL."""

import asyncio
import io
import json
import os
import socket
from contextlib import asynccontextmanager
from time import perf_counter
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.engine import make_url

from app_support import create_app
from statistics_agent.core.event_loop import create_event_loop
from statistics_agent.core.settings import Settings
from statistics_agent.persistence.database import Database
from statistics_agent.persistence.migrate import execute
from statistics_agent.persistence.schema import expected_heads, read_schema_status

SECRET = "database-private-value"


def run(coroutine):
    with asyncio.Runner(loop_factory=create_event_loop) as runner:
        return runner.run(coroutine)


@pytest.fixture
def settings():
    return Settings(
        database_url=f"postgresql+psycopg://user:{SECRET}@localhost/agent_db",
        db_health_timeout_seconds=0.05,
    )


@pytest.fixture
def postgres_settings():
    raw = os.environ.get("TEST_DATABASE_URL")
    if not raw:
        if os.environ.get("CI"):
            pytest.fail("CI must supply TEST_DATABASE_URL; health checks cannot be skipped")
        pytest.skip("Set TEST_DATABASE_URL to disposable PostgreSQL")
    url = make_url(raw)
    if url.database != "agent_test_db" or url.username != "agent_test":
        pytest.fail("Health tests require agent_test_db and agent_test")
    return Settings(app_env="test", database_url=raw, db_pool_size=1, db_health_timeout_seconds=0.2)


class BrokenDatabase:
    @property
    def engine(self):
        raise RuntimeError(f"postgresql://user:{SECRET}@private-host/agent_db")


@pytest.mark.parametrize("environment", ["development", "production"])
def test_live_does_not_touch_database_and_successful_probes_do_not_spam_logs(settings, environment):
    output = io.StringIO()
    app = create_app(settings.model_copy(update={"app_env": environment}), log_stream=output)
    with TestClient(app) as client:
        app.state.database = BrokenDatabase()
        for _ in range(4):
            response = client.get("/health/live")
            assert response.status_code == 200
            assert response.json() == {"status": "alive"}
            assert response.headers["Cache-Control"] == "no-store"
            UUID(response.headers["X-Request-ID"])
    assert [json.loads(line)["event"] for line in output.getvalue().splitlines()] == [
        "service_started",
        "service_stopped",
    ]


def test_ready_before_lifespan_is_safe_503_without_creating_connections(settings):
    app = create_app(settings, log_stream=io.StringIO())
    with TestClient(app) as client:
        app.state.started = False
        app.state.database = BrokenDatabase()
        assert client.get("/health/live").status_code == 200
        assert client.get("/health/ready").status_code == 503


def test_database_failure_503_is_correlated_safe_uncached_and_logged(settings):
    output = io.StringIO()
    app = create_app(settings, log_stream=output)
    identifier = str(uuid4())
    with TestClient(app) as client:
        app.state.database = BrokenDatabase()
        response = client.get("/health/ready", headers={"X-Request-ID": identifier})
        assert client.get("/health/live").status_code == 200
    assert response.status_code == 503
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["X-Request-ID"] == identifier
    assert response.json() == {
        "code": "not_ready",
        "message": "Service not ready",
        "request_id": identifier,
        "details": [],
    }
    assert SECRET not in response.text + output.getvalue()
    assert "private-host" not in response.text + output.getvalue()
    event = next(
        json.loads(line)
        for line in output.getvalue().splitlines()
        if json.loads(line)["event"] == "http_completed"
    )
    assert event["request_id"] == identifier
    assert event["status"] == 503


def test_ready_without_lifecycle_database_returns_503(settings):
    app = create_app(settings, log_stream=io.StringIO())
    with TestClient(app) as client:
        app.state.database = None
        assert client.get("/health/ready").status_code == 503


def test_probe_deadline_covers_connection_acquisition_and_cancels_it(settings):
    cancelled = []

    @asynccontextmanager
    async def connect():
        try:
            await asyncio.Event().wait()
            yield
        finally:
            cancelled.append(True)

    app = create_app(settings, log_stream=io.StringIO())
    with TestClient(app) as client:
        app.state.database = SimpleNamespace(engine=SimpleNamespace(connect=connect))
        start = perf_counter()
        response = client.get("/health/ready")
        assert perf_counter() - start < 1
    assert response.status_code == 503
    assert cancelled == [True]


def test_development_openapi_lists_health_but_no_business_endpoints(settings):
    app = create_app(settings, log_stream=io.StringIO())
    with TestClient(app) as client:
        paths = client.get("/openapi.json").json()["paths"]
        assert set(paths) == {
            "/health/live",
            "/health/ready",
            "/auth/login",
            "/auth/callback",
            "/auth/me",
            "/auth/logout",
        }


@asynccontextmanager
async def migrated(settings):
    database = Database(settings)
    async with database.engine.connect() as connection:
        before = await read_schema_status(connection)
    assert before.current_heads in {(), expected_heads()}
    try:
        await execute(settings, "upgrade")
        yield
    finally:
        # Repair only the isolated test DB version row if the test changed it.
        async with database.engine.begin() as connection:
            await connection.execute(
                text("UPDATE alembic_version SET version_num = :head"),
                {"head": expected_heads()[0]},
            )
        if not before.is_current:
            await execute(settings, "downgrade-test")
        await database.close()


@pytest.mark.postgres
def test_ready_requires_current_schema_and_recovers_after_live_schema_repair(postgres_settings):
    async def exercise():
        output = io.StringIO()
        async with migrated(postgres_settings):
            app = create_app(postgres_settings, log_stream=output)
            async with (
                app.router.lifespan_context(app),
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app), base_url="http://test"
                ) as client,
            ):
                response = await client.get("/health/ready")
                assert response.status_code == 200
                assert response.json() == {"status": "ready"}
                assert response.headers["Cache-Control"] == "no-store"
                async with app.state.database.session() as session:
                    await session.execute(
                        text("UPDATE alembic_version SET version_num = 'unknown'")
                    )
                    await session.commit()
                assert (await client.get("/health/ready")).status_code == 503
                assert (await client.get("/health/live")).status_code == 200
                async with app.state.database.session() as session:
                    await session.execute(
                        text("UPDATE alembic_version SET version_num = :head"),
                        {"head": expected_heads()[0]},
                    )
                    await session.commit()
                assert (await client.get("/health/ready")).status_code == 200
        assert "unknown" not in output.getvalue()
        assert "0001_agent_baseline" not in output.getvalue()
        # Only the failing probe, not healthy requests, generated an HTTP log.
        assert (
            sum(
                json.loads(line)["event"] == "http_completed"
                for line in output.getvalue().splitlines()
            )
            == 1
        )

    run(exercise())


@pytest.mark.postgres
def test_ready_pool_timeout_and_recovery_without_application_restart(postgres_settings):
    async def exercise():
        async with migrated(postgres_settings):
            app = create_app(postgres_settings, log_stream=io.StringIO())
            async with (
                app.router.lifespan_context(app),
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app), base_url="http://test"
                ) as client,
            ):
                async with app.state.database.engine.connect():
                    start = perf_counter()
                    assert (await client.get("/health/ready")).status_code == 503
                    assert perf_counter() - start < 1.5
                    assert (await client.get("/health/live")).status_code == 200
                assert (await client.get("/health/ready")).status_code == 200

    run(exercise())


@pytest.mark.postgres
def test_ready_query_timeout_releases_connection_and_recovers(postgres_settings):
    async def exercise():
        async with migrated(postgres_settings):
            app = create_app(postgres_settings, log_stream=io.StringIO())
            locker = Database(postgres_settings)
            try:
                async with (
                    app.router.lifespan_context(app),
                    httpx.AsyncClient(
                        transport=httpx.ASGITransport(app), base_url="http://test"
                    ) as client,
                ):
                    async with locker.engine.begin() as lock:
                        await lock.execute(
                            text("LOCK TABLE alembic_version IN ACCESS EXCLUSIVE MODE")
                        )
                        start = perf_counter()
                        assert (await client.get("/health/ready")).status_code == 503
                        assert perf_counter() - start < 1.5
                        assert (await client.get("/health/live")).status_code == 200
                    assert (await client.get("/health/ready")).status_code == 200
            finally:
                await locker.close()

    run(exercise())


@pytest.mark.postgres
def test_ready_connection_failure_is_bounded_while_liveness_stays_available(postgres_settings):
    async def exercise():
        # Reserve a local port without listening; do not target an unrelated DB.
        with socket.socket() as refused:
            refused.bind(("127.0.0.1", 0))
            url = make_url(postgres_settings.database_url.get_secret_value()).set(
                host="127.0.0.1", port=refused.getsockname()[1]
            )
            settings = postgres_settings.model_copy(
                update={"database_url": SecretStr(url.render_as_string(hide_password=False))}
            )
            app = create_app(settings, log_stream=io.StringIO())
            async with (
                app.router.lifespan_context(app),
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app), base_url="http://test"
                ) as client,
            ):
                start = perf_counter()
                assert (await client.get("/health/ready")).status_code == 503
                assert perf_counter() - start < 1.5
                assert (await client.get("/health/live")).status_code == 200

    run(exercise())


@pytest.mark.postgres
def test_ready_does_not_apply_missing_migrations_and_recovers_after_explicit_upgrade(
    postgres_settings,
):
    async def exercise():
        async with migrated(postgres_settings):
            app = create_app(postgres_settings, log_stream=io.StringIO())
            async with (
                app.router.lifespan_context(app),
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app), base_url="http://test"
                ) as client,
            ):
                await execute(postgres_settings, "downgrade-test")
                assert (await client.get("/health/ready")).status_code == 503
                assert (await client.get("/health/live")).status_code == 200
                async with app.state.database.session() as session:
                    assert await session.scalar(text("SELECT count(*) FROM alembic_version")) == 0
                await execute(postgres_settings, "upgrade")
                assert (await client.get("/health/ready")).status_code == 200

    run(exercise())
