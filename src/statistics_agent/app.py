"""FastAPI factory; importing this module does not load settings or infrastructure."""

from contextlib import asynccontextmanager
from typing import TextIO

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException
from starlette.middleware.cors import CORSMiddleware

from statistics_agent.api.auth import failure as authentication_failure
from statistics_agent.api.auth import router as auth_router
from statistics_agent.api.bearer import router as bearer_router
from statistics_agent.api.health import router as health_router
from statistics_agent.api.http import RequestMiddleware, http_error, validation_error
from statistics_agent.api.session import router as session_router
from statistics_agent.api.workspace import API_PREFIX, admission_configuration
from statistics_agent.api.workspace import router as workspace_router
from statistics_agent.core.auth import AuthSettings, load_auth_settings
from statistics_agent.core.auth_secrets import AuthSecrets, load_auth_secrets
from statistics_agent.core.bearer_access import BearerAccess
from statistics_agent.core.jwt_verifier import AuthenticationError
from statistics_agent.core.logging import LogEvent, ServiceLog
from statistics_agent.core.model_settings import load_model_settings
from statistics_agent.core.oidc import OidcProvider
from statistics_agent.core.resource_auth import ResourceAuthSettings, load_resource_auth_settings
from statistics_agent.core.resource_verifier import ResourceServerVerifier
from statistics_agent.core.settings import ConfigurationError, Settings, load_settings
from statistics_agent.core.token_encryption import TokenEncryption
from statistics_agent.datasets.execution import DatasetParser
from statistics_agent.datasets.service import DatasetService
from statistics_agent.persistence.auth import AuthStore
from statistics_agent.persistence.database import Database
from statistics_agent.persistence.identity_projection import IdentityProjection


class BearerCors:
    def __init__(self, app, *, origins):
        options = dict(
            allow_origins=origins,
            allow_headers=("Authorization", "X-Request-ID"),
            expose_headers=("X-Request-ID", "WWW-Authenticate"),
        )
        self.read_only = CORSMiddleware(app, allow_methods=("GET",), **options)
        self.workspace = CORSMiddleware(
            app, allow_methods=("GET", "POST", "PUT", "DELETE"), **options
        )

    async def __call__(self, scope, receive, send):
        target = (
            self.workspace if scope.get("path", "").startswith(API_PREFIX + "/") else self.read_only
        )
        await target(scope, receive, send)


def create_app(
    settings: Settings | None = None,
    *,
    log_stream: TextIO | None = None,
    auth_settings: AuthSettings | None = None,
    auth_secrets: AuthSecrets | None = None,
    resource_auth_settings: ResourceAuthSettings | None = None,
    oidc_transport=None,
) -> FastAPI:
    """Load settings once unless supplied; create an isolated service application."""
    settings = settings if settings is not None else load_settings()
    legacy = settings.http_auth_mode == "legacy"
    if legacy:
        auth_settings = auth_settings if auth_settings is not None else load_auth_settings()
        access_settings = auth_settings
    else:
        resource_auth_settings = resource_auth_settings or load_resource_auth_settings()
        access_settings = resource_auth_settings
    if access_settings.app_env != settings.app_env:
        raise ConfigurationError("Application and authentication environments must match")
    if legacy and auth_secrets is None:
        auth_secrets = load_auth_secrets(auth_settings)
    log = ServiceLog(settings, log_stream)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        database = Database(settings)
        app.state.database = database
        parser = None
        if legacy:
            provider = OidcProvider(auth_settings, auth_secrets, transport=oidc_transport)
            app.state.oidc = provider
            app.state.auth_store = AuthStore(database, auth_settings, TokenEncryption(auth_secrets))
        else:
            provider = ResourceServerVerifier(resource_auth_settings, transport=oidc_transport)
            app.state.bearer_access = BearerAccess(
                provider, IdentityProjection(database, resource_auth_settings.issuer)
            )
            parser = DatasetParser()
            app.state.datasets = DatasetService(database, parser=parser)
            try:
                app.state.admission_configuration = admission_configuration(
                    load_model_settings().resolve()
                )
            except ConfigurationError:
                # History and datasets remain available during model configuration outages.
                app.state.admission_configuration = None
        app.state.started = True
        try:
            log.emit(LogEvent.STARTED)
            yield
        finally:
            app.state.started = False
            try:
                try:
                    if parser is not None:
                        await parser.aclose()
                finally:
                    await provider.close()
            finally:
                try:
                    await database.close()
                finally:
                    app.state.database = None
                    app.state.oidc = None
                    app.state.auth_store = None
                    app.state.bearer_access = None
                    app.state.datasets = None
                    app.state.admission_configuration = None
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
    app.state.bearer_access = None
    app.state.database = None
    app.state.started = False
    app.state.log = log
    app.add_exception_handler(HTTPException, http_error)
    app.add_exception_handler(RequestValidationError, validation_error)

    async def auth_error(request, error):
        response = authentication_failure(request, error.code)
        if not legacy and response.status_code == 401:
            response.headers["WWW-Authenticate"] = "Bearer"
        return response

    app.add_exception_handler(AuthenticationError, auth_error)
    app.add_middleware(RequestMiddleware, log=log)
    app.include_router(health_router)
    if legacy:
        app.include_router(auth_router)
        app.include_router(session_router)
    else:
        app.add_middleware(BearerCors, origins=resource_auth_settings.cors_origins)
        app.include_router(bearer_router)
        app.include_router(workspace_router)
    return app
