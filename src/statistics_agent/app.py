"""FastAPI factory; importing this module does not load settings or infrastructure."""

from contextlib import asynccontextmanager
from typing import TextIO

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException

from statistics_agent.api.http import RequestMiddleware, http_error, validation_error
from statistics_agent.core.logging import LogEvent, ServiceLog
from statistics_agent.core.settings import Settings, load_settings


def create_app(settings: Settings | None = None, *, log_stream: TextIO | None = None) -> FastAPI:
    """Load settings once unless supplied; create an isolated service application."""
    settings = settings if settings is not None else load_settings()
    log = ServiceLog(settings, log_stream)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.started = True
        try:
            log.emit(LogEvent.STARTED)
            yield
        finally:
            app.state.started = False
            log.emit(LogEvent.STOPPED)

    development = settings.app_env in {"development", "test"}
    app = FastAPI(
        title="DataLens agent",
        version=settings.service_version,
        lifespan=lifespan,
        debug=False,
        docs_url="/docs" if development else None,
        redoc_url=None,
        openapi_url="/openapi.json" if development else None,
        redirect_slashes=False,
    )
    app.state.settings = settings
    app.state.started = False
    app.state.log = log
    app.add_exception_handler(HTTPException, http_error)
    app.add_exception_handler(RequestValidationError, validation_error)
    app.add_middleware(RequestMiddleware, log=log)
    return app
