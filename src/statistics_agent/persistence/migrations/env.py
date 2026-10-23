"""Alembic environment supporting explicit async execution and shared connections."""

import asyncio

from alembic import context
from sqlalchemy import text

from statistics_agent.core.event_loop import create_event_loop
from statistics_agent.core.settings import load_settings
from statistics_agent.persistence.database import Database
from statistics_agent.persistence.models import Base
from statistics_agent.persistence.schema import validate_migration_target


def apply(connection):
    # Validate server identity before any DDL, including for shared connections.
    if connection.scalar(text("SELECT current_database()")) not in {"agent_db", "agent_test_db"}:
        raise ValueError("Migrations require an agent-owned database")
    context.configure(
        connection=connection,
        target_metadata=Base.metadata,
        compare_type=True,
        version_table_schema=context.config.attributes.get("version_table_schema"),
    )
    with context.begin_transaction():
        context.run_migrations()


async def online():
    settings = load_settings()
    validate_migration_target(settings)
    database = Database(settings)
    try:
        async with database.engine.begin() as connection:
            await connection.run_sync(apply)
    finally:
        await database.close()


if context.is_offline_mode():
    # SQL generation uses a dialect, not a secret-bearing URL or live connection.
    context.configure(dialect_name="postgresql", target_metadata=Base.metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    connection = context.config.attributes.get("connection")
    if connection is not None:
        apply(connection)
    else:
        with asyncio.Runner(loop_factory=create_event_loop) as runner:
            runner.run(online())
