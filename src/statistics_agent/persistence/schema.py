"""Read-only schema-version checks and secret-free Alembic configuration."""

from dataclasses import dataclass

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncConnection

from statistics_agent.core.settings import Settings


def migration_config() -> Config:
    """No credentials, working-directory assumptions or global logging setup."""
    config = Config()
    config.set_main_option("script_location", "statistics_agent.persistence:migrations")
    return config


def expected_heads() -> tuple[str, ...]:
    return tuple(sorted(ScriptDirectory.from_config(migration_config()).get_heads()))


def validate_migration_target(settings: Settings) -> None:
    """Only canonical agent databases are migration targets; never identity_db."""
    if make_url(settings.database_url.get_secret_value()).database not in {
        "agent_db",
        "agent_test_db",
    }:
        raise ValueError("Migrations require an agent-owned database")


@dataclass(frozen=True)
class SchemaStatus:
    current_heads: tuple[str, ...]
    expected_heads: tuple[str, ...]

    @property
    def is_current(self) -> bool:
        return bool(self.expected_heads) and self.current_heads == self.expected_heads


async def read_schema_status(
    connection: AsyncConnection, *, version_table_schema: str | None = None
) -> SchemaStatus:
    """Inspect without creating/stamping a version table or loading migration env."""

    def current(sync_connection):
        migration = MigrationContext.configure(
            sync_connection, opts={"version_table_schema": version_table_schema}
        )
        return tuple(sorted(migration.get_current_heads()))

    return SchemaStatus(await connection.run_sync(current), expected_heads())
