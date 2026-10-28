"""Browser-safe status for the frontend; authorization remains server-owned."""

from typing import Annotated

from fastapi import APIRouter, Depends, Request

from statistics_agent.api.access import require_user
from statistics_agent.contracts.auth import AuthorizedPrincipal, SessionView

router = APIRouter(prefix="/auth", tags=["authentication"])


@router.get("/me", response_model=SessionView)
async def me(request: Request, principal: Annotated[AuthorizedPrincipal, Depends(require_user)]):
    return SessionView(
        issuer=principal.issuer,
        subject=principal.subject,
        roles=tuple(sorted(principal.roles)),
        email_verified=principal.email_verified,
        csrf_token=request.state.csrf_token,
    )
