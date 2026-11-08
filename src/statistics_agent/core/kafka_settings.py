"""Explicit Kafka telemetry configuration; no import-time network or secret reads."""

import ssl
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from statistics_agent.core.settings import ConfigurationError


class KafkaSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=None, case_sensitive=True, extra="ignore", frozen=True, hide_input_in_errors=True
    )
    app_env: Literal["development", "test", "staging", "production"] = Field(
        default="development", validation_alias="APP_ENV"
    )
    bootstrap: str = Field(max_length=2048, validation_alias="KAFKA_BOOTSTRAP_SERVERS")
    topic: Literal["agent.events.v1"] = Field(
        default="agent.events.v1", validation_alias="KAFKA_TOPIC"
    )
    transactional_id: str = Field(
        default="datalens-agent-events-v1",
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9_.-]+$",
        validation_alias="KAFKA_TRANSACTIONAL_ID",
    )
    protocol: Literal["PLAINTEXT", "SASL_SSL"] = Field(
        default="PLAINTEXT", validation_alias="KAFKA_SECURITY_PROTOCOL"
    )
    username: str | None = Field(
        default=None,
        exclude=True,
        repr=False,
        max_length=256,
        validation_alias="KAFKA_SASL_USERNAME",
    )
    password: SecretStr | None = Field(
        default=None, exclude=True, repr=False, validation_alias="KAFKA_SASL_PASSWORD"
    )
    mechanism: Literal["PLAIN", "SCRAM-SHA-256", "SCRAM-SHA-512"] = Field(
        default="SCRAM-SHA-512", validation_alias="KAFKA_SASL_MECHANISM"
    )
    ca_file: Path | None = Field(default=None, validation_alias="KAFKA_CA_FILE")
    timeout_seconds: int = Field(default=10, ge=1, le=60, validation_alias="KAFKA_TIMEOUT_SECONDS")

    @model_validator(mode="after")
    def valid_connection(self):
        addresses = self.bootstrap.split(",")
        if not 1 <= len(addresses) <= 8:
            raise ValueError("Expected at most eight brokers")
        for address in addresses:
            parsed = urlsplit("//" + address.strip())
            if (
                not parsed.hostname
                or parsed.port is None
                or not 1 <= parsed.port <= 65535
                or parsed.username
                or parsed.password
                or parsed.path
                or parsed.query
                or parsed.fragment
                or any(character.isspace() for character in address.strip())
            ):
                raise ValueError("Expected broker host:port without credentials or URL")
        if self.app_env in {"staging", "production"} and self.protocol != "SASL_SSL":
            raise ValueError("Hosted Kafka requires encrypted authenticated transport")
        if self.protocol == "SASL_SSL":
            if not self.username or not self.password or not self.password.get_secret_value():
                raise ValueError("Kafka authentication credentials required")
        elif self.username is not None or self.password is not None or self.ca_file is not None:
            raise ValueError("Credentials and TLS settings require SASL_SSL")
        return self

    def producer_options(self):
        options = dict(
            bootstrap_servers=[item.strip() for item in self.bootstrap.split(",")],
            transactional_id=self.transactional_id,
            enable_idempotence=True,
            acks="all",
            security_protocol=self.protocol,
            request_timeout_ms=self.timeout_seconds * 1000,
            transaction_timeout_ms=30000,
        )
        if self.protocol == "SASL_SSL":
            options.update(
                ssl_context=ssl.create_default_context(cafile=self.ca_file),
                sasl_mechanism=self.mechanism,
                sasl_plain_username=self.username,
                sasl_plain_password=self.password.get_secret_value(),
            )
        return options


def load_kafka_settings(*, env_file=None):
    try:
        if env_file is not None and not Path(env_file).is_file():
            raise ValueError
        return KafkaSettings(_env_file=env_file, _env_file_encoding="utf-8")
    except Exception:
        pass
    raise ConfigurationError("Invalid Kafka telemetry configuration")
