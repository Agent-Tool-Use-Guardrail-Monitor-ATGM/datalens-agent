"""Display-only identity projection; these contracts never authenticate a caller."""

from typing import Annotated

from pydantic import AwareDatetime, StringConstraints, field_validator

from statistics_agent.contracts.common import AuthenticatedPrincipal

type ProfileName = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=255)]
type ProfileEmail = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=320)]


class UserProfileClaims(AuthenticatedPrincipal):
    """Only a trusted verifier may supply these claims to the profile store."""

    display_name: ProfileName | None = None
    username: ProfileName | None = None
    email: ProfileEmail | None = None

    @field_validator("display_name", "username", "email")
    @classmethod
    def printable_profile_field(cls, value):
        if value is not None and (not value.strip() or not value.isprintable()):
            raise ValueError("Profile fields must be nonblank printable text")
        return value


class UserProfile(UserProfileClaims):
    display_name: (
        Annotated[str, StringConstraints(strict=True, min_length=1, max_length=256)] | None
    ) = None
    username: (
        Annotated[str, StringConstraints(strict=True, min_length=1, max_length=256)] | None
    ) = None
    profile_updated_at: AwareDatetime
