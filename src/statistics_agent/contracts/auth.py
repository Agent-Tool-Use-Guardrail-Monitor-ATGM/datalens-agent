"""Safe authentication boundaries; constructing a contract never authenticates."""

from enum import StrEnum
from typing import Literal

from pydantic import Field, field_validator

from statistics_agent.contracts.common import AuthenticatedPrincipal, AuthSessionReference, Contract


class VerifiedIdentity(AuthenticatedPrincipal):
    """Signed identity/roles verified by the provider adapter, without a session."""

    roles: frozenset[Literal["user", "admin"]] = Field(min_length=1)
    email_verified: bool = Field(strict=True)

    @field_validator("email_verified")
    @classmethod
    def require_verified(cls, value: bool) -> bool:
        if value is not True:
            raise ValueError("Verified email is required")
        return value


class AuthorizedPrincipal(VerifiedIdentity, AuthSessionReference):
    """Only current session and verified token checks may supply this principal."""


class SessionView(Contract):
    """Browser-safe session status; contains neither cookie nor OAuth tokens."""

    issuer: str
    subject: str
    roles: tuple[Literal["user", "admin"], ...]
    email_verified: bool
    csrf_token: str = Field(repr=False)


class AuthFailureCode(StrEnum):
    LOGIN_REQUIRED = "login_required"
    ACCESS_DENIED = "access_denied"
    IDENTITY_UNAVAILABLE = "identity_unavailable"


class AuthFailure(Contract):
    """Fixed public failure information, never a provider response or exception."""

    code: AuthFailureCode

    @property
    def status_code(self) -> int:
        return {
            AuthFailureCode.LOGIN_REQUIRED: 401,
            AuthFailureCode.ACCESS_DENIED: 403,
            AuthFailureCode.IDENTITY_UNAVAILABLE: 503,
        }[self.code]

    @property
    def message(self) -> str:
        return {
            AuthFailureCode.LOGIN_REQUIRED: "Authentication required",
            AuthFailureCode.ACCESS_DENIED: "Access denied",
            AuthFailureCode.IDENTITY_UNAVAILABLE: "Identity service unavailable",
        }[self.code]
