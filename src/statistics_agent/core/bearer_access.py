"""Verify a browser access token, then enforce locally projected revocations."""

import asyncio

from sqlalchemy.exc import SQLAlchemyError

from statistics_agent.contracts.auth import AuthFailureCode
from statistics_agent.contracts.resource_auth import VerifiedAccess
from statistics_agent.core.jwt_verifier import AuthenticationError
from statistics_agent.core.resource_verifier import ResourceServerVerifier
from statistics_agent.persistence.identity_projection import IdentityProjection


class BearerAccess:
    def __init__(self, verifier: ResourceServerVerifier, projection: IdentityProjection):
        self.verifier = verifier
        self.projection = projection

    async def verify_access(self, token: str) -> VerifiedAccess:
        access = await self.verifier.verify_access(token)
        if access.identity_session_id is None:
            raise AuthenticationError()

        try:
            async with asyncio.timeout(2):
                revoked = await self.projection.is_revoked(
                    access.identity,
                    issued_at=access.issued_at,
                    session_id=access.identity_session_id,
                )
        except (SQLAlchemyError, TimeoutError):
            raise AuthenticationError(AuthFailureCode.IDENTITY_UNAVAILABLE) from None
        if revoked:
            raise AuthenticationError()
        return access
