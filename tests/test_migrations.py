"""Baseline deployment and read-only schema gates, using disposable PostgreSQL."""

import io
import shutil
from importlib.resources import files

import pytest
from alembic import command
from sqlalchemy import text

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
from support.database import apply, run, sandbox, status

BASELINE = "0001_agent_baseline"
HEAD = "0010_identity_projection"


def test_single_packaged_head_needs_no_settings_or_network(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert expected_heads() == (HEAD,)
    assert migration_config().get_main_option("sqlalchemy.url") is None


@pytest.mark.postgres
def test_profile_upgrade_and_rollback_preserve_existing_login_records(migration_settings):
    async def exercise():
        async with sandbox(migration_settings) as (database, schema, config):
            await apply(database, schema, config, "upgrade", "0008_event_outbox")
            async with database.session() as session:
                await session.execute(
                    text(f"""
                    INSERT INTO {schema}.auth_login_attempts
                    (id, state_hash, browser_hash, issuer, return_target,
                     nonce_ciphertext, verifier_ciphertext, created_at, expires_at)
                    VALUES ('00000000-0000-0000-0000-000000000001',
                            repeat('a', 64), repeat('b', 64),
                            'https://identity.example/realms/datalens', '/',
                            'synthetic-encrypted-nonce', 'synthetic-encrypted-verifier',
                            now(), now() + interval '5 minutes')
                """)
                )
                await session.commit()
                previous = await session.scalar(
                    text(f"SELECT to_jsonb(t) FROM {schema}.auth_login_attempts t")
                )
            await apply(database, schema, config, "upgrade", "head")
            await apply(database, schema, config, "downgrade", "0008_event_outbox")
            async with database.session() as session:
                assert (
                    await session.scalar(
                        text(f"SELECT to_jsonb(t) FROM {schema}.auth_login_attempts t")
                    )
                    == previous
                )
                assert (
                    await session.scalar(
                        text("SELECT to_regclass(:table)"),
                        {"table": f"{schema}.agent_user_profiles"},
                    )
                    is None
                )
            await apply(database, schema, config, "upgrade", "head")
            assert (await status(database, schema)).is_current

    run(exercise())


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
    migration_settings = Settings(
        database_url=f"postgresql+psycopg://user:private-value@localhost/{database}"
    )
    with pytest.raises(ValueError, match="agent-owned"):
        validate_migration_target(migration_settings)


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
    migration_settings = Settings(
        app_env="development", database_url="postgresql+psycopg://user:pass@localhost/agent_db"
    )
    with pytest.raises(ValueError, match="disposable test"):
        run(execute(migration_settings, "downgrade-test"))


@pytest.mark.postgres
def test_empty_database_upgrades_repeats_downgrades_and_reupgrades(migration_settings):
    async def exercise():
        async with sandbox(migration_settings) as (database, schema, config):
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
                assert set(tables) == {
                    "agent_conversations",
                    "agent_conversation_datasets",
                    "agent_runs",
                    "agent_messages",
                    "agent_run_jobs",
                    "agent_run_attempts",
                    "agent_run_evidence",
                    "agent_run_dispatches",
                    "agent_event_outbox",
                    "agent_user_profiles",
                    "agent_identity_inbox",
                    "agent_identity_state",
                    "agent_identity_revoked_sessions",
                    "alembic_version",
                    "auth_login_attempts",
                    "auth_sessions",
                    "agent_datasets",
                    "agent_dataset_contents",
                    "agent_artifacts",
                }
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
def test_unknown_database_revision_fails_check_instead_of_stamping_or_repairing(migration_settings):
    async def exercise():
        async with sandbox(migration_settings) as (database, schema, config):
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
def test_failed_upgrade_rolls_back_ddl_and_preserves_previous_revision(
    migration_settings, tmp_path
):
    scripts = tmp_path / "migrations"
    shutil.copytree(files("statistics_agent.persistence").joinpath("migrations"), scripts)
    (scripts / "versions/0002_failure.py").write_text(
        """
from alembic import op
revision = "0002_failure"
down_revision = "0010_identity_projection"
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
        async with sandbox(migration_settings) as (database, schema, config):
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
    migration_settings, tmp_path, monkeypatch, capsys
):
    for name, field in Settings.model_fields.items():
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(str(field.validation_alias), raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "APP_ENV=test\nDATABASE_URL=" + migration_settings.database_url.get_secret_value() + "\n",
        encoding="utf-8",
    )

    async def current():
        database = Database(migration_settings)
        try:
            async with database.engine.connect() as connection:
                return await read_schema_status(connection)
        finally:
            await database.close()

    before = run(current())
    assert before.current_heads in {
        (),
        (BASELINE,),
        ("0002_auth_storage",),
        ("0003_refresh_claim",),
        (HEAD,),
    }
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
