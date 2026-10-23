"""Configuration behavior and credential-disclosure regression checks."""

import socket
import subprocess
import sys
import traceback
from pathlib import Path

import pytest
from pydantic import ValidationError

from statistics_agent.core.settings import ConfigurationError, Settings, load_settings

TEST_SECRET = "test-only-password-must-not-appear"
DATABASE_URL = f"postgresql+psycopg://agent_test:{TEST_SECRET}@localhost:5432/agent_db"


@pytest.fixture(autouse=True)
def isolated_configuration(monkeypatch, tmp_path):
    for name, field in Settings.model_fields.items():
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(str(field.validation_alias), raising=False)
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def database_environment(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL)


def test_minimum_settings_require_no_other_service_credentials(database_environment):
    settings = load_settings()
    assert settings.app_env == "development"
    assert settings.service_name == "datalens-agent-api"
    assert settings.database_url.get_secret_value() == DATABASE_URL
    assert settings.db_health_timeout_seconds < settings.db_connect_timeout_seconds
    assert settings.db_health_timeout_seconds < settings.db_pool_timeout_seconds
    assert settings.db_health_timeout_seconds * 1000 < settings.db_statement_timeout_ms
    with pytest.raises(ValidationError):
        settings.log_level = "DEBUG"


@pytest.mark.parametrize(
    "supplied,canonical",
    [
        ("agent-api", "datalens-agent-api"),
        ("agent-worker", "datalens-agent-worker"),
        ("datalens-agent-api", "datalens-agent-api"),
        ("datalens-agent-worker", "datalens-agent-worker"),
    ],
)
def test_service_names_are_canonical_in_settings_and_serialization(
    database_environment, monkeypatch, supplied, canonical
):
    monkeypatch.setenv("SERVICE_NAME", supplied)
    settings = load_settings()
    assert settings.service_name == canonical
    assert settings.model_dump()["service_name"] == canonical


def test_missing_database_url_fails_without_loading_local_dotenv():
    Path(".env").write_text(f"DATABASE_URL={DATABASE_URL}\n", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="DATABASE_URL"):
        load_settings()


def test_environment_overrides_explicit_file_and_file_overrides_defaults(monkeypatch):
    Path(".env").write_text(
        f"DATABASE_URL={DATABASE_URL}\nAPP_ENV=test\nDB_POOL_SIZE=3\nLOG_LEVEL=DEBUG\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("DB_POOL_SIZE", "7")
    monkeypatch.setenv("LOG_LEVEL", "WARNING")
    settings = load_settings(env_file=".env")
    assert settings.app_env == "test"
    assert settings.db_pool_size == 7
    assert settings.log_level == "WARNING"
    assert settings.db_connect_timeout_seconds == 5


@pytest.mark.parametrize(
    "database_url",
    [
        "",
        f"not-a-database-url-{TEST_SECRET}",
        f"sqlite:///{TEST_SECRET}",
        f"postgresql+asyncpg://user:{TEST_SECRET}@localhost/agent_db",
        f"postgresql+psycopg://user:{TEST_SECRET}@/agent_db",
        f"postgresql+psycopg://user:{TEST_SECRET}@localhost/",
        f"postgresql+psycopg://user:{TEST_SECRET}@localhost:invalid/agent_db",
        f"postgresql+psycopg://user:{TEST_SECRET}@localhost:65536/agent_db",
    ],
    ids=["empty", "malformed", "sqlite", "wrong-driver", "no-host", "no-db", "port", "range"],
)
def test_invalid_database_configuration_has_no_secret_exception_context(monkeypatch, database_url):
    monkeypatch.setenv("DATABASE_URL", database_url)
    with pytest.raises(ConfigurationError, match="DATABASE_URL") as caught:
        load_settings()
    error = caught.value
    assert error.__context__ is None
    assert error.__cause__ is None
    diagnostics = str(error) + repr(error) + "".join(traceback.format_exception(error))
    assert TEST_SECRET not in diagnostics


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("DB_POOL_SIZE", "0"),
        ("DB_POOL_SIZE", "21"),
        ("DB_CONNECT_TIMEOUT_SECONDS", "0"),
        ("DB_CONNECT_TIMEOUT_SECONDS", "61"),
        ("DB_POOL_TIMEOUT_SECONDS", "NaN"),
        ("DB_POOL_TIMEOUT_SECONDS", "inf"),
        ("DB_STATEMENT_TIMEOUT_MS", "0"),
        ("DB_STATEMENT_TIMEOUT_MS", "300001"),
        ("DB_HEALTH_TIMEOUT_SECONDS", "0"),
        ("DB_HEALTH_TIMEOUT_SECONDS", "11"),
        ("DB_HEALTH_TIMEOUT_SECONDS", "NaN"),
        ("APP_ENV", TEST_SECRET),
        ("LOG_LEVEL", TEST_SECRET),
        ("SERVICE_NAME", TEST_SECRET),
        ("SERVICE_VERSION", "invalid-version"),
    ],
)
def test_invalid_settings_fail_safely(database_environment, monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(ConfigurationError, match=name) as caught:
        load_settings()
    assert TEST_SECRET not in str(caught.value)
    assert caught.value.__context__ is None


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("DB_CONNECT_TIMEOUT_SECONDS", "2"),
        ("DB_POOL_TIMEOUT_SECONDS", "1"),
        ("DB_STATEMENT_TIMEOUT_MS", "1000"),
    ],
)
def test_health_budget_cannot_reach_normal_database_timeouts(
    database_environment, monkeypatch, name, value
):
    monkeypatch.setenv(name, value)
    with pytest.raises(ConfigurationError, match="DB_HEALTH_TIMEOUT_SECONDS"):
        load_settings()


def test_settings_serialization_and_repr_exclude_credentials(database_environment):
    settings = load_settings()
    assert "database_url" not in settings.model_dump()
    assert "DATABASE_URL" not in settings.model_dump(by_alias=True)
    for diagnostics in (repr(settings), str(settings), settings.model_dump_json()):
        assert TEST_SECRET not in diagnostics
        assert "postgresql" not in diagnostics


def test_missing_explicit_settings_file_does_not_silently_succeed(database_environment):
    with pytest.raises(ConfigurationError) as caught:
        load_settings(env_file=f"missing-{TEST_SECRET}.env")
    assert TEST_SECRET not in str(caught.value)
    assert caught.value.__context__ is None


def test_invalid_file_encoding_is_sanitized(database_environment):
    Path(".env").write_bytes(b"\xff\xfe\xfd" + TEST_SECRET.encode())
    with pytest.raises(ConfigurationError) as caught:
        load_settings(env_file=Path(".env"))
    assert TEST_SECRET not in "".join(traceback.format_exception(caught.value))
    assert caught.value.__context__ is None


def test_settings_import_has_no_configuration_or_network_side_effects():
    script = """
import socket
def deny_network(*args, **kwargs):
    raise AssertionError("Import attempted a network connection")
socket.create_connection = deny_network
socket.socket.connect = deny_network
import statistics_agent.core.settings
print("settings imported without DATABASE_URL or network access")
"""
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=15, check=True
    )
    assert "settings imported" in result.stdout


def test_loading_does_not_connect_to_a_database(database_environment, monkeypatch):
    def deny_network(*args, **kwargs):
        raise AssertionError("Configuration must not connect to infrastructure")

    monkeypatch.setattr(socket, "create_connection", deny_network)
    monkeypatch.setattr(socket.socket, "connect", deny_network)
    assert load_settings().database_url.get_secret_value() == DATABASE_URL
