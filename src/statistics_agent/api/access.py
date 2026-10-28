"""Cookie-session authorization dependencies and session-bound CSRF protection."""

import hashlib
import hmac
import json
from typing import Literal

from fastapi import HTTPException, Request
from sqlalchemy.exc import SQLAlchemyError

from statistics_agent.api.auth import approved_origin, browser_credential
from statistics_agent.contracts.auth import AuthFailureCode, AuthorizedPrincipal, SessionOwner
from statistics_agent.core.oidc import AuthenticationError
from statistics_agent.core.session_lifecycle import SessionLifecycle


def csrf_token(cookie, principal: SessionOwner) -> str:
    """HMAC uses the independent 256-bit opaque cookie as a per-session secret.

    The DB never stores this cookie or CSRF value. Domain-separated context binds
    the proof to the exact session/owner; it does not reveal the cookie to JS.
    """
    message = json.dumps(
        ["DataLens-CSRF-v1", str(principal.auth_session_ref), principal.issuer, principal.subject],
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode()
    return hmac.new(cookie.get_secret_value().encode(), message, hashlib.sha256).hexdigest()


def require_role(role: Literal["user", "admin"]):
    """Use on every protected operation; no permission/identity cache across requests."""
    if role not in {"user", "admin"}:
        raise ValueError("Unknown application role")

    async def authorize(request: Request) -> AuthorizedPrincipal:
        request.state.protected = True
        approved_origin(request)
        origins = request.headers.getlist("origin")
        trusted_origin = request.app.state.auth_settings.public_origin
        unsafe = request.method not in {"GET", "HEAD", "OPTIONS"}
        if (origins and origins != [trusted_origin]) or (unsafe and origins != [trusted_origin]):
            raise AuthenticationError(AuthFailureCode.ACCESS_DENIED)
        cookie = browser_credential(request, request.app.state.auth_settings.session_cookie_name)
        if cookie is None:
            raise AuthenticationError()
        try:
            session = await request.app.state.auth_store.read_session(cookie)
        except ValueError:
            raise AuthenticationError() from None
        except SQLAlchemyError:
            raise HTTPException(503) from None
        if session is None:
            raise AuthenticationError()
        owner = SessionOwner(
            issuer=session.issuer, subject=session.subject, auth_session_ref=session.session_id
        )
        if unsafe:
            proofs = request.headers.getlist("x-csrf-token")
            if len(proofs) != 1 or not hmac.compare_digest(
                proofs[0].encode(), csrf_token(cookie, owner).encode()
            ):
                raise AuthenticationError(AuthFailureCode.ACCESS_DENIED)
        identity = await SessionLifecycle(
            request.app.state.auth_store,
            request.app.state.oidc,
            request.app.state.auth_settings,
        ).authorize(session, required_role=role)
        if identity.issuer != session.issuer or identity.subject != session.subject:
            raise AuthenticationError()
        if role not in identity.roles:
            raise AuthenticationError(AuthFailureCode.ACCESS_DENIED)
        principal = AuthorizedPrincipal(
            **identity.model_dump(),
            auth_session_ref=session.session_id,
        )
        try:
            # Reread deadlines/revocation in the conditional update, closing the
            # DB read/validation race. Only a successful authorization extends idle time.
            active = await request.app.state.auth_store.touch_session(session.session_id)
        except SQLAlchemyError:
            raise HTTPException(503) from None
        if not active:
            raise AuthenticationError()
        request.state.csrf_token = csrf_token(cookie, principal)
        return principal

    return authorize


require_user = require_role("user")
require_admin = require_role("admin")
