"""Verified access metadata without bearer credentials or a browser-session binding."""

from typing import Annotated

from pydantic import Field, StringConstraints, model_validator

from statistics_agent.contracts.auth import VerifiedIdentity
from statistics_agent.contracts.common import Contract
from statistics_agent.contracts.users import UserProfileClaims


class VerifiedAccess(Contract):
    """Only the verifier supplies trusted values; constructing this type is not authentication."""

    identity: VerifiedIdentity
    issued_at: int = Field(strict=True, ge=0)
    expires_at: int = Field(strict=True, ge=1)
    identity_session_id: (
        Annotated[str, StringConstraints(strict=True, min_length=1, max_length=255)] | None
    ) = None
    profile_hints: UserProfileClaims

    @model_validator(mode="after")
    def consistent_identity(self):
        if not 0 < self.expires_at - self.issued_at <= 300:
            raise ValueError("Invalid access-token lifetime")
        if (self.identity.issuer, self.identity.subject) != (
            self.profile_hints.issuer,
            self.profile_hints.subject,
        ):
            raise ValueError("Identity and profile hints must refer to the same owner")
        if self.identity_session_id is not None and (
            not self.identity_session_id.strip() or not self.identity_session_id.isprintable()
        ):
            raise ValueError("Identity session ID must be nonblank printable text")
        return self
