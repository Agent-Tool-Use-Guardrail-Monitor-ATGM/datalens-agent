"""FastAPI factory; importing this module does not load settings or infrastructure."""

from contextlib import asynccontextmanager
from typing import TextIO

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException

from statistics_agent.api.auth import router as auth_router
from statistics_agent.api.health import router as health_router
from statistics_agent.api.http import RequestMiddleware, http_error, validation_error
from statistics_agent.core.auth import AuthSettings, load_auth_settings
from statistics_agent.core.auth_secrets import AuthSecrets, load_auth_secrets
from statistics_agent.core.logging import LogEvent, ServiceLog
from statistics_agent.core.oidc import OidcProvider
from statistics_agent.core.settings import ConfigurationError, Settings, load_settings
from statistics_agent.core.token_encryption import TokenEncryption
from statistics_agent.persistence.auth import AuthStore
from statistics_agent.persistence.database import Database


def create_app(
    settings: Settings | None = None,
    *,
    log_stream: TextIO | None = None,
    auth_settings: AuthSettings | None = None,
    auth_secrets: AuthSecrets | None = None,
    oidc_transport=None,
) -> FastAPI:
    """Load settings once unless supplied; create an isolated service application."""
    settings = settings if settings is not None else load_settings()
    auth_settings = auth_settings if auth_settings is not None else load_auth_settings()
    if auth_settings.app_env != settings.app_env:
        raise ConfigurationError("Application and authentication environments must match")
    auth_secrets = auth_secrets if auth_secrets is not None else load_auth_secrets(auth_settings)
    log = ServiceLog(settings, log_stream)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        database = Database(settings)
        provider = OidcProvider(auth_settings, auth_secrets, transport=oidc_transport)
        app.state.database = database
        app.state.oidc = provider
        app.state.auth_store = AuthStore(database, auth_settings, TokenEncryption(auth_secrets))
        app.state.started = True
        try:
            log.emit(LogEvent.STARTED)
            yield
        finally:
            app.state.started = False
            try:
                await provider.close()
            finally:
                try:
                    await database.close()
                finally:
                    app.state.database = None
                    app.state.oidc = None
                    app.state.auth_store = None
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
    app.state.auth_settings = auth_settings
    app.state.oidc = None
    app.state.auth_store = None
    app.state.database = None
    app.state.started = False
    app.state.log = log
    app.add_exception_handler(HTTPException, http_error)
    app.add_exception_handler(RequestValidationError, validation_error)
    app.add_middleware(RequestMiddleware, log=log)
    app.include_router(health_router)
    app.include_router(auth_router)
    return app
