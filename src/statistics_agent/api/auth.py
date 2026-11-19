"""Browser-only code login; credentials never appear in application responses."""

import base64
import hashlib
import re
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from pydantic import SecretStr
from starlette.responses import RedirectResponse

from statistics_agent.api.http import error_response
from statistics_agent.contracts.auth import AuthFailure
from statistics_agent.core.oidc import AuthenticationError
from statistics_agent.persistence.auth import PendingLoginError

router = APIRouter(prefix="/auth", tags=["authentication"])


def binding_name(settings):
    return "__Host-agent-login" if settings.cookie_secure else "agent-login-dev"


def browser_credential(request, name):
    values = []
    for header in request.headers.getlist("cookie"):
        for part in header.split(";"):
            key, separator, value = part.strip().partition("=")
            if separator and key == name:
                values.append(value)
    if not values:
        return None
    if len(values) != 1 or not re.fullmatch(r"[A-Za-z0-9_-]{43}", values[0]):
        raise AuthenticationError()
    return SecretStr(values[0])


def failure(request, code):
    info = AuthFailure(code=code)
    return error_response(request.state.request_id, info.status_code, info.code, info.message)


def approved_origin(request):
    settings = request.app.state.auth_settings
    if str(request.url).split("?", 1)[0] != settings.public_origin + request.url.path:
        raise AuthenticationError()


@router.get("/login")
async def login(request: Request):
    settings = request.app.state.auth_settings
    try:
        approved_origin(request)
        if request.url.query:
            raise AuthenticationError()
        binding = browser_credential(request, binding_name(settings))
        endpoints = await request.app.state.oidc.endpoints()
        attempt = await request.app.state.auth_store.start_login(binding)
        proof = (
            base64.urlsafe_b64encode(
                hashlib.sha256(attempt.verifier.get_secret_value().encode()).digest()
            )
            .decode()
            .rstrip("=")
        )
        response = RedirectResponse(
            endpoints.authorization_endpoint
            + "?"
            + urlencode(
                {
                    "client_id": settings.client_id,
                    "redirect_uri": settings.callback_url,
                    "response_type": "code",
                    "scope": "openid email",
                    "response_mode": "query",
                    "state": attempt.state.get_secret_value(),
                    "nonce": attempt.nonce.get_secret_value(),
                    "code_challenge": proof,
                    "code_challenge_method": "S256",
                }
            ),
            status_code=303,
        )
        response.set_cookie(
            binding_name(settings),
            attempt.browser_binding.get_secret_value(),
            max_age=settings.login_attempt_seconds,
            httponly=True,
            secure=settings.cookie_secure,
            samesite=settings.cookie_samesite,
            path="/",
        )
        return response
    except PendingLoginError:
        return error_response(
            request.state.request_id, 409, "login_pending", "Login already pending"
        )
    except AuthenticationError as error:
        return failure(request, error.code)


@router.get("/callback")
async def callback(request: Request):
    settings = request.app.state.auth_settings
    try:
        approved_origin(request)
        query = request.query_params
        binding = browser_credential(request, binding_name(settings))
        if (
            len(request.scope.get("query_string", b"")) > 8192
            or binding is None
            or len(query.getlist("state")) != 1
            or not re.fullmatch(r"[A-Za-z0-9_-]{43}", query["state"])
        ):
            raise AuthenticationError()
        attempt = await request.app.state.auth_store.consume_login(
            SecretStr(query["state"]), binding
        )
        if attempt is None:
            raise AuthenticationError()
        # Consume valid state/binding even when the provider denied login or the
        # remaining response is malformed. Never retry a possibly consumed code.
        allowed = {"state", "iss", "code", "session_state", "error", "error_description"}
        if (
            set(query) - allowed
            or any(len(query.getlist(key)) != 1 for key in query)
            or query.get("iss") != settings.issuer
            or "error" in query
            or not 1 <= len(query.get("code", "")) <= 4096
            or not query["code"].isascii()
            or any(c.isspace() for c in query["code"])
        ):
            raise AuthenticationError()
        verified = await request.app.state.oidc.exchange(query["code"], attempt)
        created = await request.app.state.auth_store.create_session(
            verified.principal, verified.credentials
        )
        response = RedirectResponse(settings.public_origin + "/", status_code=303)
        response.set_cookie(
            settings.session_cookie_name,
            created.cookie.get_secret_value(),
            max_age=settings.session_max_seconds,
            httponly=True,
            secure=settings.cookie_secure,
            samesite=settings.cookie_samesite,
            path="/",
        )
    except AuthenticationError as error:
        response = failure(request, error.code)
    response.delete_cookie(
        binding_name(settings),
        path="/",
        secure=settings.cookie_secure,
        httponly=True,
        samesite=settings.cookie_samesite,
    )
    return response
