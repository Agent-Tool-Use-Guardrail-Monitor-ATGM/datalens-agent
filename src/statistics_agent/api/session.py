"""Browser-safe status for the frontend; authorization remains server-owned."""

import hmac
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.exc import SQLAlchemyError
from starlette.responses import JSONResponse

from statistics_agent.api.access import csrf_token, require_user
from statistics_agent.api.auth import approved_origin, browser_credential
from statistics_agent.contracts.auth import (
    AuthFailureCode,
    AuthorizedPrincipal,
    LogoutView,
    SessionOwner,
    SessionView,
)
from statistics_agent.core.oidc import AuthenticationError

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


@router.post("/logout", response_model=LogoutView)
async def logout(request: Request):
    approved_origin(request)
    settings = request.app.state.auth_settings
    if request.headers.getlist("origin") != [settings.public_origin]:
        raise AuthenticationError(AuthFailureCode.ACCESS_DENIED)
    cookie = browser_credential(request, settings.session_cookie_name)
    if cookie is None:
        raise AuthenticationError()
    try:
        stored = await request.app.state.auth_store.read_logout_session(cookie)
        if stored is None:
            raise AuthenticationError()
        owner = SessionOwner(
            issuer=stored.issuer, subject=stored.subject, auth_session_ref=stored.session_id
        )
        proofs = request.headers.getlist("x-csrf-token")
        if len(proofs) != 1 or not hmac.compare_digest(
            proofs[0].encode(), csrf_token(cookie, owner).encode()
        ):
            raise AuthenticationError(AuthFailureCode.ACCESS_DENIED)
        await request.app.state.auth_store.revoke_session(stored.session_id)
    except SQLAlchemyError:
        raise HTTPException(503) from None
    # Commit local revocation before any provider request, even if JWTs expired
    # or permissions changed. Report provider failure without restoring access.
    remote = await request.app.state.oidc.logout(stored.credentials)
    response = JSONResponse(LogoutView(provider_logout_succeeded=remote).model_dump())
    response.delete_cookie(
        settings.session_cookie_name,
        path="/",
        secure=settings.cookie_secure,
        httponly=True,
        samesite=settings.cookie_samesite,
    )
    return response
