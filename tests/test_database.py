"""Real PostgreSQL transaction, timeout and lifespan checks; no SQLite stand-in."""

import asyncio
import io
import os
from contextlib import asynccontextmanager
from time import perf_counter
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError, IntegrityError, OperationalError, TimeoutError

from app_support import create_app
from statistics_agent.core.event_loop import create_event_loop
from statistics_agent.core.settings import Settings
from statistics_agent.persistence.database import Database

pytestmark = pytest.mark.postgres


def run(coroutine):
    with asyncio.Runner(loop_factory=create_event_loop) as runner:
        return runner.run(coroutine)


@pytest.fixture
def settings():
    raw = os.environ.get("TEST_DATABASE_URL")
    if not raw:
        if os.environ.get("CI"):
            pytest.fail("CI must supply TEST_DATABASE_URL; database checks cannot be skipped")
        pytest.skip("Set TEST_DATABASE_URL to the disposable agent_test_db fixture")
    url = make_url(raw)
    if url.database != "agent_test_db" or url.username != "agent_test":
        pytest.fail("Database tests require the dedicated agent_test user and agent_test_db")
    return Settings(
        app_env="test",
        database_url=raw,
        db_pool_size=2,
        db_connect_timeout_seconds=2,
        db_pool_timeout_seconds=1,
        db_statement_timeout_ms=1000,
        db_health_timeout_seconds=0.05,
    )


@asynccontextmanager
async def storage(settings):
    database = Database(settings)
    # Server-owned random identifiers, never submitted SQL or a production table.
    schema = "foundation_" + uuid4().hex
    table = f"{schema}.probe"
    try:
        async with database.session() as session:
            assert await session.scalar(text("SELECT current_database()")) == "agent_test_db"
            await session.execute(text(f"CREATE SCHEMA {schema}"))
            await session.execute(text(f"CREATE TABLE {table} (id integer PRIMARY KEY)"))
            await session.commit()
        yield database, table
    finally:
        try:
            async with database.engine.begin() as connection:
                await connection.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
        finally:
            await database.close()


def test_commit_is_visible_to_a_different_concurrent_session(settings):
    async def exercise():
        async with storage(settings) as (database, table):
            async with database.session() as writer, database.session() as reader:
                await writer.execute(text(f"INSERT INTO {table} VALUES (1)"))
                assert await reader.scalar(text(f"SELECT count(*) FROM {table}")) == 0
                await writer.commit()
                assert await reader.scalar(text(f"SELECT count(*) FROM {table}")) == 1

    run(exercise())


@pytest.mark.parametrize("outcome", ["no_commit", "explicit_rollback", "exception"])
def test_uncommitted_work_is_rolled_back_and_connection_reused(settings, outcome):
    async def exercise():
        async with storage(settings) as (database, table):
            try:
                async with database.session() as session:
                    await session.execute(text(f"INSERT INTO {table} VALUES (1)"))
                    if outcome == "explicit_rollback":
                        await session.rollback()
                    if outcome == "exception":
                        raise ValueError("Controlled operation failure")
            except ValueError:
                assert outcome == "exception"
            async with database.session() as session:
                assert await session.scalar(text(f"SELECT count(*) FROM {table}")) == 0
                await session.execute(text(f"INSERT INTO {table} VALUES (2)"))
                await session.commit()

    run(exercise())


def test_database_error_releases_connection_without_partial_commit(settings):
    async def exercise():
        async with storage(settings) as (database, table):
            with pytest.raises(IntegrityError):
                async with database.session() as session:
                    await session.execute(text(f"INSERT INTO {table} VALUES (1)"))
                    await session.execute(text(f"INSERT INTO {table} VALUES (1)"))
                    await session.commit()
            async with database.session() as session:
                assert await session.scalar(text(f"SELECT count(*) FROM {table}")) == 0

    run(exercise())


def test_pool_exhaustion_is_bounded_without_overflow_connections(settings):
    async def exercise():
        bounded = settings.model_copy(update={"db_pool_size": 1, "db_pool_timeout_seconds": 0.15})
        database = Database(bounded)
        try:
            async with database.session() as occupied:
                await occupied.execute(text("SELECT 1"))
                started = perf_counter()
                with pytest.raises(TimeoutError):
                    async with database.session() as waiting:
                        await waiting.execute(text("SELECT 1"))
                assert 0.1 <= perf_counter() - started < 3
            async with database.session() as reused:
                assert await reused.scalar(text("SELECT 1")) == 1
        finally:
            await database.close()

    run(exercise())


def test_statement_timeout_cancels_slow_query_and_pool_recovers(settings):
    async def exercise():
        database = Database(settings.model_copy(update={"db_statement_timeout_ms": 150}))
        try:
            started = perf_counter()
            with pytest.raises(DBAPIError) as caught:
                async with database.session() as session:
                    await session.execute(text("SELECT pg_sleep(5)"))
            assert caught.value.orig.sqlstate == "57014"
            assert perf_counter() - started < 3
            async with database.session() as reused:
                assert await reused.scalar(text("SELECT 1")) == 1
                assert await reused.scalar(text("SHOW statement_timeout")) == "150ms"
        finally:
            await database.close()

    run(exercise())


def test_lifespan_disposes_connections_and_prohibits_reusing_closed_database(settings):
    async def exercise():
        app = create_app(settings, log_stream=io.StringIO())
        assert app.state.database is None
        async with app.router.lifespan_context(app):
            database = app.state.database
            async with database.session() as session:
                backend = await session.scalar(text("SELECT pg_backend_pid()"))
        assert app.state.database is None
        assert not app.state.started
        with pytest.raises(RuntimeError, match="lifecycle has ended"):
            async with database.session():
                pass
        observer = Database(settings)
        try:
            async with observer.session() as session:
                assert (
                    await session.scalar(
                        text("SELECT count(*) FROM pg_stat_activity WHERE pid = :pid"),
                        {"pid": backend},
                    )
                    == 0
                )
        finally:
            await observer.close()

    run(exercise())


def test_cancelled_operation_rolls_back_and_releases_pool_slot(settings):
    async def exercise():
        async with storage(settings.model_copy(update={"db_pool_size": 1})) as (database, table):
            entered = asyncio.Event()

            async def operation():
                async with database.session() as session:
                    await session.execute(text(f"INSERT INTO {table} VALUES (1)"))
                    entered.set()
                    await asyncio.Event().wait()

            task = asyncio.create_task(operation())
            await asyncio.wait_for(entered.wait(), 3)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            async with database.session() as session:
                assert await session.scalar(text(f"SELECT count(*) FROM {table}")) == 0

    run(exercise())


def test_connection_handshake_timeout_is_bounded(settings):
    async def exercise():
        # A controlled local socket accepts TCP and never answers PostgreSQL's
        # handshake. No packets or credentials go to an unrelated remote host.
        clients = []

        async def silent(reader, writer):
            clients.append(writer)
            await reader.read()

        server = await asyncio.start_server(silent, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        url = make_url(settings.database_url.get_secret_value()).set(host="127.0.0.1", port=port)
        database = Database(
            settings.model_copy(
                update={
                    "database_url": settings.database_url.__class__(
                        url.render_as_string(hide_password=False)
                    )
                }
            )
        )
        try:
            started = perf_counter()
            with pytest.raises(OperationalError):
                async with database.session() as session:
                    await session.execute(text("SELECT 1"))
            assert perf_counter() - started < 7
        finally:
            await database.close()
            server.close()
            for writer in clients:
                writer.close()
                await writer.wait_closed()
            await server.wait_closed()

    run(exercise())


def test_database_failure_is_safely_reported_through_http(settings):
    async def exercise():
        output = io.StringIO()
        app = create_app(settings, log_stream=output)
        secret = "private-database-parameter"

        @app.get("/test-database-error")
        async def fail():
            async with app.state.database.session() as session:
                await session.execute(text("SELECT CAST(:value AS integer)"), {"value": secret})

        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client,
        ):
            response = await client.get("/test-database-error")
            assert response.status_code == 500
            assert response.json()["code"] == "internal_error"
            evidence = response.text + output.getvalue()
            assert secret not in evidence
            assert "test-only-password" not in evidence
            assert "CAST" not in evidence
            async with app.state.database.session() as session:
                assert await session.scalar(text("SELECT 1")) == 1

    run(exercise())
