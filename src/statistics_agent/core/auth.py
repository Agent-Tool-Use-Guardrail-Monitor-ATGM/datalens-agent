"""Explicit authentication configuration; no file reads or network at import time."""

import ipaddress
import re
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, ValidationError, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict, SettingsError

from statistics_agent.contracts.common import Contract
from statistics_agent.core.settings import ConfigurationError


def canonical_url(value: str, *, origin: bool = False) -> str:
    """Keep identity URLs exact; reject ambiguous credentials, paths and delimiters."""
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.port == 0
        or any(c.isspace() or not c.isprintable() for c in value)
        or any(c in value for c in ("\\", "%", "?", "#"))
        or parsed.hostname != parsed.hostname.lower()
        or value.endswith("/")
        or "//" in parsed.path
        or any(part in {".", ".."} for part in parsed.path.split("/"))
        or (origin and parsed.path)
        or (parsed.scheme == "http" and parsed.port == 80)
        or (parsed.scheme == "https" and parsed.port == 443)
    ):
        raise ValueError("A canonical HTTP(S) URL is required")
    try:
        ipaddress.ip_address(parsed.hostname)
    except ValueError:
        if not re.fullmatch(
            r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*",
            parsed.hostname,
        ):
            raise ValueError("A canonical DNS hostname is required") from None
    # Force validation of out-of-range ports and require an unambiguous host spelling.
    host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    authority = host if parsed.port is None else f"{host}:{parsed.port}"
    if parsed.netloc != authority or value != parsed.geturl():
        raise ValueError("A canonical URL authority is required")
    return value


class ProviderEndpoints(Contract):
    """Validated discovery values; this object neither discovers nor authenticates."""

    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    introspection_endpoint: str
    jwks_uri: str
    end_session_endpoint: str


class AuthSettings(BaseSettings):
    """Required factory settings; validation itself performs no infrastructure I/O."""

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
    client_id: str = Field(
        default="agent-api", pattern=r"^[A-Za-z0-9_.-]{1,64}$", validation_alias="OIDC_CLIENT_ID"
    )
    public_origin: str = Field(
        default="http://localhost:8000", validation_alias="AUTH_PUBLIC_ORIGIN"
    )
    callback_url: str | None = Field(default=None, validation_alias="AUTH_CALLBACK_URL")
    client_secret_file: Path = Field(
        validation_alias="OIDC_CLIENT_SECRET_FILE", exclude=True, repr=False
    )
    encryption_keys_file: Path = Field(
        validation_alias="AUTH_TOKEN_ENCRYPTION_KEYS_FILE", exclude=True, repr=False
    )
    allowed_algorithms: tuple[Literal["RS256"], ...] = Field(
        default=("RS256",), validation_alias="OIDC_ALLOWED_ALGORITHMS"
    )
    cookie_secure: bool | None = Field(default=None, validation_alias="AUTH_COOKIE_SECURE")
    cookie_samesite: Literal["lax"] = Field(default="lax", validation_alias="AUTH_COOKIE_SAMESITE")
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
    session_idle_seconds: int = Field(
        default=1800, ge=60, le=1800, validation_alias="AUTH_SESSION_IDLE_SECONDS"
    )
    session_max_seconds: int = Field(
        default=28800, ge=60, le=28800, validation_alias="AUTH_SESSION_MAX_SECONDS"
    )
    login_attempt_seconds: int = Field(
        default=300, ge=30, le=300, validation_alias="AUTH_LOGIN_ATTEMPT_SECONDS"
    )

    @model_validator(mode="after")
    def validate_policy(self):
        canonical_url(self.issuer)
        canonical_url(self.public_origin, origin=True)
        if not re.fullmatch(r"/realms/[A-Za-z0-9._-]+", urlsplit(self.issuer).path):
            raise ValueError("An explicit realm issuer is required")
        for value in (self.issuer, self.public_origin):
            parsed = urlsplit(value)
            if parsed.scheme == "http" and (
                self.app_env not in {"development", "test"}
                or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
            ):
                raise ValueError("HTTP is restricted to explicit local development/test origins")
        expected_callback = self.public_origin + "/auth/callback"
        if self.callback_url is not None and self.callback_url != expected_callback:
            raise ValueError("Callback must match the public origin and registered path")
        object.__setattr__(self, "callback_url", expected_callback)
        secure = urlsplit(self.public_origin).scheme == "https"
        if self.cookie_secure is not None and self.cookie_secure != secure:
            raise ValueError("Cookie security must match the public scheme")
        object.__setattr__(self, "cookie_secure", secure)
        if self.allowed_algorithms != ("RS256",):
            raise ValueError("Only the configured RS256 policy is supported")
        if self.request_timeout < max(self.connect_timeout, self.read_timeout, self.pool_timeout):
            raise ValueError("Total HTTP deadline must cover individual phase limits")
        if self.session_idle_seconds > self.session_max_seconds:
            raise ValueError("Idle session limit must not exceed absolute lifetime")
        for path in (self.client_secret_file, self.encryption_keys_file):
            if str(path) in {".", ".."}:
                raise ValueError("Explicit secret file paths are required")
        if self.client_secret_file.absolute() == self.encryption_keys_file.absolute():
            raise ValueError("Client and encryption secrets require separate files")
        return self

    @property
    def session_cookie_name(self) -> str:
        return (
            "__Host-agent-session"
            if self.app_env in {"staging", "production"}
            else "agent-session-dev"
        )

    @property
    def discovery_url(self) -> str:
        return self.issuer + "/.well-known/openid-configuration"

    def validate_metadata(self, metadata: dict) -> ProviderEndpoints:
        """Validate the fixed Keycloak endpoint contract before sharing credentials."""
        paths = {
            "authorization_endpoint": "/protocol/openid-connect/auth",
            "token_endpoint": "/protocol/openid-connect/token",
            "introspection_endpoint": "/protocol/openid-connect/token/introspect",
            "jwks_uri": "/protocol/openid-connect/certs",
            "end_session_endpoint": "/protocol/openid-connect/logout",
        }
        if not isinstance(metadata, dict) or metadata.get("issuer") != self.issuer:
            raise ConfigurationError("Invalid identity metadata")
        if any(metadata.get(name) != self.issuer + path for name, path in paths.items()):
            raise ConfigurationError("Invalid identity metadata")
        algorithms = metadata.get("id_token_signing_alg_values_supported")
        if not isinstance(algorithms, list) or "RS256" not in algorithms:
            raise ConfigurationError("Invalid identity metadata")
        return ProviderEndpoints(issuer=self.issuer, **{name: metadata[name] for name in paths})


def load_auth_settings(*, env_file: str | Path | None = None) -> AuthSettings:
    """Load explicit sources and raise a fixed diagnostic without input/context."""
    try:
        if env_file is not None and not Path(env_file).is_file():
            raise OSError
        return AuthSettings(_env_file=env_file, _env_file_encoding="utf-8")
    except (ValidationError, SettingsError, OSError, UnicodeError, ValueError):
        pass
    raise ConfigurationError("Invalid authentication configuration")
