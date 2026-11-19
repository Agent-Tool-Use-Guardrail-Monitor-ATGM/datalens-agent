"""One-shot, bounded outbox retention job; scheduling belongs to deployment."""

import argparse
import asyncio

from statistics_agent.core.event_loop import create_event_loop
from statistics_agent.core.settings import ConfigurationError, load_settings
from statistics_agent.persistence.database import Database
from statistics_agent.persistence.outbox import cleanup_published
from statistics_agent.persistence.schema import read_schema_status, validate_migration_target


async def clean(env_file=None, *, retention_days=7, limit=1000):
    settings = load_settings(env_file=env_file)
    validate_migration_target(settings)
    database = Database(settings)
    try:
        async with database.engine.connect() as connection:
            if not (await read_schema_status(connection)).is_current:
                raise ConfigurationError("Cleanup schema is not current")
        return await cleanup_published(database, retention_days=retention_days, limit=limit)
    finally:
        await database.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Delete old published outbox events")
    parser.add_argument("--env-file", default=None)
    parser.add_argument(
        "--retention-days", type=int, choices=range(1, 366), default=7, metavar="1..365"
    )
    parser.add_argument(
        "--limit", type=int, choices=range(1, 1001), default=1000, metavar="1..1000"
    )
    args = parser.parse_args(argv)
    try:
        with asyncio.Runner(loop_factory=create_event_loop) as runner:
            removed = runner.run(
                clean(args.env_file, retention_days=args.retention_days, limit=args.limit)
            )
        print(f"Published outbox events removed: {removed}")
        return 0
    except (Exception, KeyboardInterrupt):
        print("Outbox cleanup failed; check configuration and database availability")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
