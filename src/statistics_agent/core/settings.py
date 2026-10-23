"""Validated service settings without import-time loading or network access."""

from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, ValidationError, ValidationInfo, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict, SettingsError
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError


class ConfigurationError(ValueError):
    """A startup error containing field names only, never rejected input."""


class Settings(BaseSettings):
    """Immutable settings; credentials are excluded from repr and serialization."""

    model_config = SettingsConfigDict(
        env_file=None,
        case_sensitive=True,
        populate_by_name=True,
        extra="ignore",
        frozen=True,
        hide_input_in_errors=True,
    )

    app_env: Literal["development", "test", "staging", "production"] = Field(
        default="development", validation_alias="APP_ENV"
    )
    service_name: Literal["datalens-agent-api", "datalens-agent-worker"] = Field(
        default="datalens-agent-api", validation_alias="SERVICE_NAME"
    )
    service_version: str = Field(
        default="0.1.0",
        validation_alias="SERVICE_VERSION",
        pattern=r"^[0-9][0-9A-Za-z.+_-]{0,63}$",
    )
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = Field(
        default="INFO", validation_alias="LOG_LEVEL"
    )
    database_url: SecretStr = Field(validation_alias="DATABASE_URL", exclude=True, repr=False)
    db_pool_size: int = Field(default=5, ge=1, le=20, validation_alias="DB_POOL_SIZE")
    db_connect_timeout_seconds: int = Field(
        default=5, ge=1, le=60, validation_alias="DB_CONNECT_TIMEOUT_SECONDS"
    )
    db_pool_timeout_seconds: float = Field(
        default=10,
        ge=0.1,
        le=60,
        allow_inf_nan=False,
        validation_alias="DB_POOL_TIMEOUT_SECONDS",
    )
    db_statement_timeout_ms: int = Field(
        default=10_000, ge=100, le=300_000, validation_alias="DB_STATEMENT_TIMEOUT_MS"
    )
    db_health_timeout_seconds: float = Field(
        default=2,
        ge=0.05,
        le=10,
        allow_inf_nan=False,
        validation_alias="DB_HEALTH_TIMEOUT_SECONDS",
    )

    @field_validator("service_name", mode="before")
    @classmethod
    def normalize_service_name(cls, value: object) -> object:
        # Existing local env files remain usable while all emitted names are canonical.
        if isinstance(value, str):
            return {
                "agent-api": "datalens-agent-api",
                "agent-worker": "datalens-agent-worker",
            }.get(value, value)
        return value

    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, value: SecretStr) -> SecretStr:
        raw = value.get_secret_value()
        if not 1 <= len(raw) <= 4096:
            raise ValueError("DATABASE_URL must contain 1 to 4096 characters")
        try:
            url = make_url(raw)
        except (ArgumentError, ValueError):
            raise ValueError("DATABASE_URL must be a PostgreSQL Psycopg URL") from None
        if (
            url.drivername != "postgresql+psycopg"
            or not url.host
            or not url.database
            or not url.username
            or not url.host.strip()
            or not url.database.strip()
            or not url.username.strip()
            or (url.port is not None and not 1 <= url.port <= 65535)
        ):
            raise ValueError("DATABASE_URL requires the Psycopg driver, host, user and database")
        return value

    @field_validator("db_health_timeout_seconds")
    @classmethod
    def validate_health_timeout(cls, value: float, info: ValidationInfo) -> float:
        fields = info.data
        names = (
            "db_connect_timeout_seconds",
            "db_pool_timeout_seconds",
            "db_statement_timeout_ms",
        )
        if all(name in fields for name in names):
            normal_timeout = min(
                fields["db_connect_timeout_seconds"],
                fields["db_pool_timeout_seconds"],
                fields["db_statement_timeout_ms"] / 1000,
            )
            if value >= normal_timeout:
                raise ValueError("Health timeout must be shorter than normal database timeouts")
        return value


def load_settings(*, env_file: str | Path | None = None) -> Settings:
    """Load explicit settings sources and expose only sanitized startup errors.

    By default, only process environment variables and defaults are read.
    Local callers may pass ``env_file=".env"`` explicitly. Environment variables
    override that file. The future application factory owns loading once and
    passing this immutable object to its components.
    """
    invalid_fields: tuple[str, ...] = ()
    try:
        if env_file is not None and not Path(env_file).is_file():
            raise OSError("Explicit settings file is unavailable")
        return Settings(_env_file=env_file, _env_file_encoding="utf-8")
    except ValidationError as error:
        public_names = {
            str(field.validation_alias): str(field.validation_alias)
            for field in Settings.model_fields.values()
        }
        public_names.update(
            {name: str(field.validation_alias) for name, field in Settings.model_fields.items()}
        )
        invalid_fields = tuple(
            sorted(
                {
                    public_names[item["loc"][0]]
                    for item in error.errors(
                        include_input=False, include_context=False, include_url=False
                    )
                    if item["loc"] and item["loc"][0] in public_names
                }
            )
        )
    except (SettingsError, OSError, UnicodeError):
        pass

    # Raise outside the except block so a raw validation/source error is not
    # retained as exception context for startup traceback/log formatting.
    message = "Invalid application configuration"
    if invalid_fields:
        message += "; check: " + ", ".join(invalid_fields)
    raise ConfigurationError(message)
