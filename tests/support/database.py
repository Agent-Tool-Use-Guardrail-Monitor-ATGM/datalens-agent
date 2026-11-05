"""Shared synthetic database fixtures; no test-module imports."""

import asyncio
import os
from contextlib import asynccontextmanager
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.engine import make_url

from statistics_agent.core.event_loop import create_event_loop
from statistics_agent.core.settings import Settings
from statistics_agent.persistence.database import Database
from statistics_agent.persistence.schema import (
    migration_config,
    read_schema_status,
)


def run(coroutine):
    with asyncio.Runner(loop_factory=create_event_loop) as runner:
        return runner.run(coroutine)


@pytest.fixture
def migration_settings():
    raw = os.environ.get("TEST_DATABASE_URL")
    if not raw:
        if os.environ.get("CI"):
            pytest.fail("CI must supply TEST_DATABASE_URL; migration checks cannot be skipped")
        pytest.skip("Set TEST_DATABASE_URL to the disposable agent_test_db fixture")
    url = make_url(raw)
    if url.database != "agent_test_db" or url.username != "agent_test":
        pytest.fail("Migration tests require the dedicated agent_test user and agent_test_db")
    return Settings(app_env="test", database_url=raw)


@asynccontextmanager
async def sandbox(settings):
    database = Database(settings)
    schema = "migration_" + uuid4().hex
    config = migration_config()
    config.attributes["version_table_schema"] = schema
    try:
        async with database.engine.begin() as connection:
            await connection.execute(text(f"CREATE SCHEMA {schema}"))
        yield database, schema, config
    finally:
        try:
            async with database.engine.begin() as connection:
                await connection.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
        finally:
            await database.close()


async def apply(database, schema, config, action, revision):
    async with database.engine.begin() as connection:
        await connection.execute(text(f"SET LOCAL search_path TO {schema}"))

        def migrate(sync_connection):
            config.attributes["connection"] = sync_connection
            getattr(command, action)(config, revision)

        await connection.run_sync(migrate)


async def status(database, schema):
    async with database.engine.connect() as connection:
        return await read_schema_status(connection, version_table_schema=schema)


@pytest.fixture
def auth_storage_settings():
    raw = os.environ.get("TEST_DATABASE_URL")
    if not raw:
        if os.environ.get("CI"):
            pytest.fail("CI must supply TEST_DATABASE_URL; auth storage cannot be skipped")
        pytest.skip("Set TEST_DATABASE_URL to disposable agent_test_db")
    url = make_url(raw)
    if url.database != "agent_test_db" or url.username != "agent_test":
        pytest.fail("Auth tests require agent_test and agent_test_db")
    return Settings(app_env="test", database_url=raw, db_pool_size=4)
