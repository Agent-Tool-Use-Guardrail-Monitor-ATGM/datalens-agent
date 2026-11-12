"""Session-independent access verification; no request routing, refresh or database I/O."""

from pydantic import ValidationError

from statistics_agent.contracts.resource_auth import VerifiedAccess
from statistics_agent.contracts.users import UserProfileClaims
from statistics_agent.core.jwt_verifier import AuthenticationError, JwtVerifier
from statistics_agent.core.resource_auth import ResourceAuthSettings


class ResourceServerVerifier(JwtVerifier):
    def __init__(self, settings: ResourceAuthSettings, *, transport=None):
        super().__init__(settings, transport=transport)

    async def verify_access(self, token: str) -> VerifiedAccess:
        claims, identity = await self._access(token)
        try:
            hints = UserProfileClaims(
                issuer=identity.issuer,
                subject=identity.subject,
                display_name=claims.get("name"),
                username=claims.get("preferred_username"),
                email=claims.get("email"),
            )
        except ValidationError:
            # Display metadata can be stale or malformed without invalidating access.
            hints = UserProfileClaims(issuer=identity.issuer, subject=identity.subject)
        try:
            if "nbf" in claims and type(claims["nbf"]) is not int:
                raise ValueError
            return VerifiedAccess(
                identity=identity,
                issued_at=claims["iat"],
                expires_at=claims["exp"],
                identity_session_id=claims.get("sid"),
                profile_hints=hints,
            )
        except (ValueError, TypeError, KeyError):
            pass
        raise AuthenticationError()
