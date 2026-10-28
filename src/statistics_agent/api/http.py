"""Request correlation, bounded safe errors and body-free completion logs."""

from time import perf_counter
from uuid import UUID

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from statistics_agent.contracts.http import ErrorEnvelope, ValidationDetail, resolve_request_id
from statistics_agent.core.logging import LogEvent, ServiceLog, request_id_context


def error_response(
    request_id: UUID,
    status: int,
    code: str,
    message: str,
    details: tuple[ValidationDetail, ...] = (),
) -> JSONResponse:
    envelope = ErrorEnvelope(code=code, message=message, request_id=request_id, details=details)
    return JSONResponse(envelope.model_dump(mode="json"), status_code=status)


async def validation_error(request: Request, error: RequestValidationError) -> JSONResponse:
    # Even location components may contain submitted dictionary keys. Retain only
    # the trusted source category, never raw input, messages, context or field keys.
    sources = {"body", "query", "path", "header", "cookie"}
    details = []
    for item in error.errors()[:20]:
        location = item.get("loc", ())
        source = location[0] if location and location[0] in sources else "request"
        details.append(ValidationDetail(location=(source,), code="invalid_value"))
    return error_response(
        request.state.request_id,
        422,
        "validation_error",
        "Request validation failed",
        tuple(details),
    )


async def http_error(request: Request, error: HTTPException) -> JSONResponse:
    messages = {
        400: ("bad_request", "Invalid request"),
        401: ("unauthorized", "Authentication required"),
        403: ("forbidden", "Access denied"),
        404: ("not_found", "Resource not found"),
        405: ("method_not_allowed", "Method not allowed"),
        429: ("rate_limited", "Too many requests"),
        503: ("service_unavailable", "Service unavailable"),
    }
    code, message = messages.get(error.status_code, ("http_error", "Request failed"))
    response = error_response(request.state.request_id, error.status_code, code, message)
    if error.status_code == 401:
        response.headers["WWW-Authenticate"] = "Bearer"
    if error.status_code == 405 and error.headers:
        allowed = error.headers.get("Allow", "")
        if allowed and all(part.strip() in METHODS for part in allowed.split(",")):
            response.headers["Allow"] = allowed
    return response


METHODS = {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE", "CONNECT"}


class RequestMiddleware:
    """Pure ASGI middleware with per-request state, including safe 500 handling."""

    def __init__(self, app: ASGIApp, log: ServiceLog) -> None:
        self.app = app
        self.log = log

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        supplied = [
            value for key, value in scope.get("headers", []) if key.lower() == b"x-request-id"
        ]
        header = supplied[0].decode("latin-1") if len(supplied) == 1 else None
        request_id = resolve_request_id(header)
        scope.setdefault("state", {})["request_id"] = request_id
        token = request_id_context.set(str(request_id))
        started = False
        status = 500
        failed = False
        beginning = perf_counter()

        async def correlated_send(message: Message) -> None:
            nonlocal started, status
            if message["type"] == "http.response.start":
                started = True
                status = message["status"]
                message = dict(message)
                message["headers"] = [
                    (key, value)
                    for key, value in message.get("headers", [])
                    if key.lower() != b"x-request-id"
                ] + [(b"x-request-id", str(request_id).encode("ascii"))]
                if scope.get("path", "").startswith("/auth/"):
                    message["headers"] = [
                        (key, value)
                        for key, value in message["headers"]
                        if key.lower() not in {b"cache-control", b"referrer-policy"}
                    ] + [(b"cache-control", b"no-store"), (b"referrer-policy", b"no-referrer")]
            await send(message)

        try:
            try:
                await self.app(scope, receive, correlated_send)
            except Exception:
                failed = True
            if failed:
                if started:
                    # Headers cannot be rewritten after streaming begins. Abort
                    # with a fresh exception outside the original except context.
                    raise RuntimeError("Response failed after headers were sent")
                response = error_response(
                    request_id, 500, "internal_error", "Internal server error"
                )
                await response(scope, receive, correlated_send)
        finally:
            try:
                route = getattr(scope.get("route"), "path", "unmatched")
                method = scope.get("method", "")
                healthy_probe = (
                    method == "GET"
                    and route in {"/health/live", "/health/ready"}
                    and status == 200
                    and not failed
                )
                if not healthy_probe:
                    self.log.emit(
                        LogEvent.HTTP_FAILED if failed else LogEvent.HTTP_COMPLETED,
                        status=status,
                        duration_ms=(perf_counter() - beginning) * 1000,
                        route=route,
                        method=method if method in METHODS else "OTHER",
                    )
            finally:
                request_id_context.reset(token)
