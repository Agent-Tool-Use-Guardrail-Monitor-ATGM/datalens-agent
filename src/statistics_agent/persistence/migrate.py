"""Explicit migration/check command; never invoked by API or worker startup."""

import argparse
import asyncio
from collections.abc import Sequence
from pathlib import Path

from alembic import command
from sqlalchemy.engine import make_url

from statistics_agent.core.event_loop import create_event_loop
from statistics_agent.core.settings import Settings, load_settings
from statistics_agent.persistence.database import Database
from statistics_agent.persistence.schema import (
    migration_config,
    read_schema_status,
    validate_migration_target,
)


async def execute(settings: Settings, action: str) -> bool:
    """Run one bounded operation with the same driver/timeouts as the agent."""
    validate_migration_target(settings)
    if action not in {"upgrade", "check", "downgrade-test"}:
        raise ValueError("Unsupported migration action")
    if action == "downgrade-test" and (
        settings.app_env != "test"
        or make_url(settings.database_url.get_secret_value()).database != "agent_test_db"
    ):
        raise ValueError("Downgrade verification requires a disposable test database")
    database = Database(settings)
    try:
        async with database.engine.begin() as connection:
            if action == "check":
                return (await read_schema_status(connection)).is_current

            def apply(sync_connection):
                config = migration_config()
                config.attributes["connection"] = sync_connection
                if action == "upgrade":
                    command.upgrade(config, "head")
                else:
                    command.downgrade(config, "base")

            await connection.run_sync(apply)
            return True
    finally:
        await database.close()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Explicit agent database migrations")
    parser.add_argument("action", choices=("upgrade", "check", "downgrade-test"))
    parser.add_argument(
        "--env-file", type=Path, help="Explicit local dotenv file; never auto-loaded"
    )
    args = parser.parse_args(argv)
    try:
        settings = load_settings(env_file=args.env_file)
        with asyncio.Runner(loop_factory=create_event_loop) as runner:
            success = runner.run(execute(settings, args.action))
    except Exception:
        # No driver exception, URL, SQL, rejected input or traceback on stdout/stderr.
        print("Database operation failed; check configuration, connectivity and migration files.")
        return 1
    print(
        "Database operation succeeded."
        if success
        else "Database schema does not match this release."
    )
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
