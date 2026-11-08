"""Resource-server trust configuration; no OAuth secrets, cookies or token storage."""

import re
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import Field, StringConstraints, ValidationError, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict, SettingsError

from statistics_agent.contracts.common import Contract
from statistics_agent.core.auth import canonical_url
from statistics_agent.core.settings import ConfigurationError

type ClientIdentifier = Annotated[
    str, StringConstraints(strict=True, min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
]


class SigningKeyEndpoint(Contract):
    issuer: str
    jwks_uri: str


class ResourceAuthSettings(BaseSettings):
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
    issuer: str = Field(
        default="http://localhost:8081/realms/datalens", validation_alias="OIDC_ISSUER"
    )
    audience: ClientIdentifier = Field(default="agent-api", validation_alias="OIDC_AUDIENCE")
    authorized_parties: tuple[ClientIdentifier, ...] = Field(
        default=("agent-web",),
        min_length=1,
        max_length=8,
        validation_alias="OIDC_AUTHORIZED_PARTIES",
    )
    connect_timeout: float = Field(
        default=3, ge=0.1, le=10, allow_inf_nan=False, validation_alias="AUTH_HTTP_CONNECT_SECONDS"
    )
    read_timeout: float = Field(
        default=5, ge=0.1, le=15, allow_inf_nan=False, validation_alias="AUTH_HTTP_READ_SECONDS"
    )
    pool_timeout: float = Field(
        default=1, ge=0.1, le=5, allow_inf_nan=False, validation_alias="AUTH_HTTP_POOL_SECONDS"
    )
    request_timeout: float = Field(
        default=10,
        ge=0.5,
        le=30,
        allow_inf_nan=False,
        validation_alias="AUTH_HTTP_DEADLINE_SECONDS",
    )

    @model_validator(mode="after")
    def validate_trust(self):
        canonical_url(self.issuer)
        parsed = urlsplit(self.issuer)
        if not re.fullmatch(r"/realms/[A-Za-z0-9._-]+", parsed.path):
            raise ValueError("An explicit realm issuer is required")
        if parsed.scheme == "http" and (
            self.app_env not in {"development", "test"}
            or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
        ):
            raise ValueError("HTTP is restricted to local development/test issuers")
        if len(set(self.authorized_parties)) != len(self.authorized_parties):
            raise ValueError("Authorized parties must be unique")
        if self.request_timeout < max(self.connect_timeout, self.read_timeout, self.pool_timeout):
            raise ValueError("HTTP deadline must cover individual phase limits")
        return self

    @property
    def allowed_algorithms(self):
        return ("RS256",)

    @property
    def discovery_url(self):
        return self.issuer + "/.well-known/openid-configuration"

    def validate_metadata(self, metadata):
        expected = self.issuer + "/protocol/openid-connect/certs"
        if not isinstance(metadata, dict) or metadata.get("issuer") != self.issuer:
            raise ConfigurationError("Invalid resource-server identity metadata")
        if metadata.get("jwks_uri") != expected:
            raise ConfigurationError("Invalid resource-server signing-key endpoint")
        return SigningKeyEndpoint(issuer=self.issuer, jwks_uri=expected)


def load_resource_auth_settings(*, env_file: str | Path | None = None):
    try:
        if env_file is not None and not Path(env_file).is_file():
            raise OSError
        return ResourceAuthSettings(_env_file=env_file, _env_file_encoding="utf-8")
    except (ValidationError, SettingsError, OSError, UnicodeError, ValueError):
        pass
    raise ConfigurationError("Invalid resource-server authentication configuration")
