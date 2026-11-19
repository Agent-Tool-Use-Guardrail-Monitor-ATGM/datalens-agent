"""Bearer request authorization and the caller's display profile."""

import asyncio
import re
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Request
from fastapi.security import HTTPBearer
from sqlalchemy.exc import SQLAlchemyError

from statistics_agent.contracts.auth import AuthFailureCode, VerifiedIdentity
from statistics_agent.contracts.common import AuthenticatedPrincipal
from statistics_agent.contracts.users import UserProfile
from statistics_agent.core.jwt_verifier import AuthenticationError
from statistics_agent.persistence.users import UserProfileStore

router = APIRouter(prefix="/auth", tags=["authentication"])
_BEARER_JWT = re.compile(r"(?i:Bearer) +([A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)")


class BearerToken(HTTPBearer):
    async def __call__(self, request: Request) -> str:
        request.state.protected = True
        headers = request.headers.getlist("authorization")
        if len(headers) == 1 and len(headers[0]) <= 32768 + 32:
            match = _BEARER_JWT.fullmatch(headers[0])
            if match is not None and len(match[1]) <= 32768:
                return match[1]
        raise AuthenticationError()


_token = BearerToken(bearerFormat="JWT")


def require_bearer_role(role: Literal["user", "admin"]):
    async def authorize(
        request: Request, token: Annotated[str, Depends(_token)]
    ) -> VerifiedIdentity:
        access = await request.app.state.bearer_access.verify_access(token)
        if role not in access.identity.roles:
            raise AuthenticationError(AuthFailureCode.ACCESS_DENIED)
        return access.identity

    return authorize


require_bearer_user = require_bearer_role("user")
require_bearer_admin = require_bearer_role("admin")


class OwnProfile(AuthenticatedPrincipal):
    roles: tuple[Literal["user", "admin"], ...]
    email_verified: Literal[True]
    profile_status: Literal["ready", "synchronizing"]
    profile: UserProfile | None


@router.get("/me", response_model=OwnProfile)
async def me(
    request: Request, principal: Annotated[VerifiedIdentity, Depends(require_bearer_user)]
):
    try:
        async with asyncio.timeout(2):
            profile = await UserProfileStore(request.app.state.database).get(
                principal, projected_only=True
            )
    except (SQLAlchemyError, TimeoutError):
        raise AuthenticationError(AuthFailureCode.IDENTITY_UNAVAILABLE) from None
    return OwnProfile(
        issuer=principal.issuer,
        subject=principal.subject,
        roles=tuple(sorted(principal.roles)),
        email_verified=True,
        profile_status="synchronizing" if profile is None else "ready",
        profile=profile,
    )
