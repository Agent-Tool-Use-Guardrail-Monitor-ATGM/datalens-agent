"""Baseline deployment and read-only schema gates, using disposable PostgreSQL."""

import asyncio
import io
import os
import shutil
from contextlib import asynccontextmanager
from importlib.resources import files
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.engine import make_url

from statistics_agent.core.event_loop import create_event_loop
from statistics_agent.core.settings import Settings
from statistics_agent.persistence.database import Database
from statistics_agent.persistence.migrate import execute, main
from statistics_agent.persistence.schema import (
    SchemaStatus,
    expected_heads,
    migration_config,
    read_schema_status,
    validate_migration_target,
)

BASELINE = "0001_agent_baseline"
HEAD = "0003_refresh_claim"


def run(coroutine):
    with asyncio.Runner(loop_factory=create_event_loop) as runner:
        return runner.run(coroutine)


@pytest.fixture
def settings():
    raw = os.environ.get("TEST_DATABASE_URL")
    if not raw:
        if os.environ.get("CI"):
            pytest.fail("CI must supply TEST_DATABASE_URL; migration checks cannot be skipped")
        pytest.skip("Set TEST_DATABASE_URL to the disposable agent_test_db fixture")
    url = make_url(raw)
    if url.database != "agent_test_db" or url.username != "agent_test":
        pytest.fail("Migration tests require the dedicated agent_test user and agent_test_db")
    return Settings(app_env="test", database_url=raw)


def test_single_packaged_head_needs_no_settings_or_network(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert expected_heads() == (HEAD,)
    assert migration_config().get_main_option("sqlalchemy.url") is None


def test_offline_baseline_sql_needs_no_credentials_or_database(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    output = io.StringIO()
    config = migration_config()
    config.output_buffer = output
    command.upgrade(config, "head", sql=True)
    sql = output.getvalue()
    assert "CREATE TABLE alembic_version" in sql
    assert BASELINE in sql
    assert "CREATE TABLE users" not in sql
    assert "postgresql://" not in sql


@pytest.mark.parametrize("database", ["identity_db", "postgres", "unrelated_db"])
def test_migration_target_rejects_identity_and_unrelated_databases(database):
    settings = Settings(
        database_url=f"postgresql+psycopg://user:private-value@localhost/{database}"
    )
    with pytest.raises(ValueError, match="agent-owned"):
        validate_migration_target(settings)


@pytest.mark.parametrize(
    "current,expected",
    [((), False), ((BASELINE,), True), (("unknown",), False), ((BASELINE, "other"), False)],
)
def test_schema_gate_rejects_empty_unknown_or_additional_heads(current, expected):
    assert SchemaStatus(current, (BASELINE,)).is_current is expected


def test_cli_missing_configuration_is_safe(monkeypatch, capsys):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert main(["upgrade"]) == 1
    output = capsys.readouterr()
    assert "Database operation failed" in output.out
    assert "Traceback" not in output.out + output.err


def test_cli_rejects_identity_before_contacting_it(monkeypatch, capsys):
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql+psycopg://user:private-value@localhost/identity_db"
    )
    assert main(["upgrade"]) == 1
    output = capsys.readouterr()
    assert "private-value" not in output.out + output.err
    assert "Database operation failed" in output.out


def test_manual_downgrade_rejects_persistent_agent_database():
    settings = Settings(
        app_env="development", database_url="postgresql+psycopg://user:pass@localhost/agent_db"
    )
    with pytest.raises(ValueError, match="disposable test"):
        run(execute(settings, "downgrade-test"))


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


@pytest.mark.postgres
def test_empty_database_upgrades_repeats_downgrades_and_reupgrades(settings):
    async def exercise():
        async with sandbox(settings) as (database, schema, config):
            initial = await status(database, schema)
            assert initial.current_heads == ()
            assert not initial.is_current
            async with database.session() as session:
                assert (
                    await session.scalar(
                        text("SELECT to_regclass(:table)"), {"table": f"{schema}.alembic_version"}
                    )
                    is None
                )  # Reading status must not create bookkeeping tables.
            await apply(database, schema, config, "upgrade", "head")
            assert (await status(database, schema)).is_current
            await apply(database, schema, config, "upgrade", "head")
            assert (await status(database, schema)).current_heads == (HEAD,)
            async with database.session() as session:
                tables = (
                    (
                        await session.execute(
                            text(
                                "SELECT table_name FROM information_schema.tables "
                                "WHERE table_schema = :schema"
                            ),
                            {"schema": schema},
                        )
                    )
                    .scalars()
                    .all()
                )
                assert set(tables) == {"alembic_version", "auth_login_attempts", "auth_sessions"}
            await apply(database, schema, config, "downgrade", BASELINE)
            assert (await status(database, schema)).current_heads == (BASELINE,)
            async with database.session() as session:
                assert (
                    await session.scalar(
                        text("SELECT to_regclass(:name)"), {"name": f"{schema}.auth_sessions"}
                    )
                    is None
                )
            await apply(database, schema, config, "downgrade", "base")
            assert (await status(database, schema)).current_heads == ()
            assert not (await status(database, schema)).is_current
            await apply(database, schema, config, "upgrade", "head")
            assert (await status(database, schema)).is_current

    run(exercise())


@pytest.mark.postgres
def test_unknown_database_revision_fails_check_instead_of_stamping_or_repairing(settings):
    async def exercise():
        async with sandbox(settings) as (database, schema, config):
            await apply(database, schema, config, "upgrade", "head")
            async with database.session() as session:
                await session.execute(
                    text(f"UPDATE {schema}.alembic_version SET version_num = 'unknown'")
                )
                await session.commit()
            assert not (await status(database, schema)).is_current
            assert (await status(database, schema)).current_heads == ("unknown",)

    run(exercise())


@pytest.mark.postgres
def test_failed_upgrade_rolls_back_ddl_and_preserves_previous_revision(settings, tmp_path):
    scripts = tmp_path / "migrations"
    shutil.copytree(files("statistics_agent.persistence").joinpath("migrations"), scripts)
    (scripts / "versions/0002_failure.py").write_text(
        """
from alembic import op
revision = "0002_failure"
down_revision = "0003_refresh_claim"
branch_labels = depends_on = None
def upgrade():
    op.execute("CREATE TABLE rollback_probe (id integer)")
    raise RuntimeError("Controlled failed migration")
def downgrade():
    pass
""",
        encoding="utf-8",
    )

    async def exercise():
        async with sandbox(settings) as (database, schema, config):
            await apply(database, schema, config, "upgrade", HEAD)
            config.set_main_option("script_location", str(scripts))
            with pytest.raises(RuntimeError, match="Controlled failed"):
                await apply(database, schema, config, "upgrade", "head")
            assert (await status(database, schema)).current_heads == (HEAD,)
            async with database.session() as session:
                assert (
                    await session.scalar(
                        text("SELECT to_regclass(:table)"), {"table": f"{schema}.rollback_probe"}
                    )
                    is None
                )

    run(exercise())


@pytest.mark.postgres
def test_explicit_cli_upgrade_and_check_with_local_env_file(
    settings, tmp_path, monkeypatch, capsys
):
    for name, field in Settings.model_fields.items():
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(str(field.validation_alias), raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "APP_ENV=test\nDATABASE_URL=" + settings.database_url.get_secret_value() + "\n",
        encoding="utf-8",
    )

    async def current():
        database = Database(settings)
        try:
            async with database.engine.connect() as connection:
                return await read_schema_status(connection)
        finally:
            await database.close()

    before = run(current())
    assert before.current_heads in {(), (BASELINE,), ("0002_auth_storage",), (HEAD,)}
    try:
        assert main(["upgrade", "--env-file", str(env_file)]) == 0
        assert main(["check", "--env-file", str(env_file)]) == 0
        assert main(["upgrade", "--env-file", str(env_file)]) == 0
        assert run(current()).is_current
    finally:
        # Only restore the explicitly disposable test DB if it started at base.
        if not before.is_current:
            assert main(["downgrade-test", "--env-file", str(env_file)]) == 0
    if not before.is_current:
        assert main(["check", "--env-file", str(env_file)]) == 1
    output = capsys.readouterr()
    assert "test-only-password" not in output.out + output.err
    assert "Traceback" not in output.out + output.err
