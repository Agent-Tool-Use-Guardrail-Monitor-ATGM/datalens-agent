"""Immutable browser-state records shared by OIDC and persistence."""

from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from pydantic import SecretStr


@dataclass(frozen=True)
class LoginStart:
    attempt_id: UUID
    expires_at: datetime
    state: SecretStr = field(repr=False)
    browser_binding: SecretStr = field(repr=False)
    nonce: SecretStr = field(repr=False)
    verifier: SecretStr = field(repr=False)


@dataclass(frozen=True)
class ConsumedLogin:
    attempt_id: UUID
    issuer: str
    return_target: str
    nonce: SecretStr = field(repr=False)
    verifier: SecretStr = field(repr=False)


@dataclass(frozen=True)
class Credentials:
    """Already-verified provider credentials; storage does not authenticate JWTs."""

    access_token: SecretStr = field(repr=False)
    refresh_token: SecretStr = field(repr=False)
    id_token: SecretStr | None = field(default=None, repr=False)


@dataclass(frozen=True)
class StoredSession:
    session_id: UUID
    issuer: str
    subject: str
    idle_expires_at: datetime
    absolute_expires_at: datetime
    refresh_version: int
    credentials: Credentials = field(repr=False)


@dataclass(frozen=True)
class NewSession:
    session: StoredSession
    cookie: SecretStr = field(repr=False)


@dataclass(frozen=True)
class RefreshClaim:
    session: StoredSession = field(repr=False)
    claim_id: UUID | None
    waiting: bool


@dataclass(frozen=True)
class LogoutSession:
    session_id: UUID
    issuer: str
    subject: str
    credentials: Credentials | None = field(repr=False)
