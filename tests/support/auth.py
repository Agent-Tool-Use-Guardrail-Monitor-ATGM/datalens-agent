"""Shared synthetic auth fixtures; no test-module imports."""

import io
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Annotated
from uuid import uuid4

import httpx
import jwt
import jwt.api_jwt
from alembic import command
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import Depends
from pydantic import SecretStr
from sqlalchemy import event, text

from app_support import create_legacy_app as create_app
from statistics_agent.api.access import require_admin, require_user
from statistics_agent.contracts.auth import AuthorizedPrincipal
from statistics_agent.contracts.common import AuthenticatedPrincipal
from statistics_agent.contracts.sessions import ConsumedLogin, Credentials
from statistics_agent.core.auth import AuthSettings
from statistics_agent.core.oidc import OidcProvider
from statistics_agent.core.token_encryption import TokenEncryption
from statistics_agent.persistence.auth import AuthStore
from statistics_agent.persistence.database import Database
from statistics_agent.persistence.schema import migration_config
from support.crypto import keyring


def auth_settings():
    return AuthSettings(
        app_env="test",
        client_secret_file="unused-client",
        encryption_keys_file="unused-ring",
    )


def credentials(suffix="first"):
    return Credentials(
        SecretStr("private-access-" + suffix),
        SecretStr("private-refresh-" + suffix),
        SecretStr("private-id-" + suffix),
    )


@asynccontextmanager
async def storage(settings):
    database = Database(settings)
    schema = "auth_" + uuid4().hex
    config = migration_config()
    config.attributes["version_table_schema"] = schema
    ring = keyring()
    try:
        async with database.engine.begin() as connection:
            await connection.execute(text(f"CREATE SCHEMA {schema}"))
            await connection.execute(text(f"SET LOCAL search_path TO {schema}"))

            def migrate(connection):
                config.attributes["connection"] = connection
                command.upgrade(config, "head")

            await connection.run_sync(migrate)

        # Each pooled checkout gets the server-generated isolated schema; no
        # production table or persistent developer database is used as a fixture.
        def set_schema(connection, record, proxy):
            cursor = connection.cursor()
            cursor.execute(f"SET search_path TO {schema}")
            connection.commit()
            cursor.close()

        event.listen(database.engine.sync_engine, "checkout", set_schema)
        store = AuthStore(database, auth_settings(), TokenEncryption(ring))
        yield database, store, ring
    finally:
        try:
            async with database.engine.begin() as connection:
                await connection.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
        finally:
            await database.close()


def principal(subject="user-one"):
    return AuthenticatedPrincipal(issuer=auth_settings().issuer, subject=subject)


class SyntheticProvider:
    def __init__(self):
        self.settings = auth_settings()
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.calls = []
        self.offline = False
        self.changes = {}
        self.result_changes = {}
        self.kid = "first"
        self.jwks_changes = None
        self.attempt = ConsumedLogin(
            __import__("uuid").uuid4(),
            self.settings.issuer,
            "/",
            SecretStr("synthetic-nonce"),
            SecretStr("synthetic-pkce-verifier"),
        )

    def token(self, access=True, changes=None, headers=None):
        now = int(datetime.now(UTC).timestamp())
        claims = {
            "iss": self.settings.issuer,
            "sub": "synthetic-user",
            "aud": "agent-api",
            "iat": now,
            "exp": now + 300,
            "azp": "agent-api",
        }
        if access:
            claims.update(
                typ="Bearer",
                email_verified=True,
                resource_access={"agent-api": {"roles": ["user"]}},
            )
        else:
            claims.update(nonce=self.attempt.nonce.get_secret_value())
        claims.update(changes or {})
        return jwt.encode(claims, self.key, algorithm="RS256", headers=headers or {"kid": self.kid})

    def handle(self, request):
        self.calls.append(request)
        if self.offline:
            raise httpx.ConnectError("private-provider-diagnostic", request=request)
        if request.url.path.endswith("openid-configuration"):
            suffixes = {
                "authorization_endpoint": "/auth",
                "token_endpoint": "/token",
                "introspection_endpoint": "/token/introspect",
                "jwks_uri": "/certs",
                "end_session_endpoint": "/logout",
            }
            return httpx.Response(
                200,
                json={
                    "issuer": self.settings.issuer,
                    "id_token_signing_alg_values_supported": ["RS256"],
                    "authorization_response_iss_parameter_supported": True,
                    **{
                        name: self.settings.issuer + "/protocol/openid-connect" + suffix
                        for name, suffix in suffixes.items()
                    },
                },
            )
        if request.url.path.endswith("certs"):
            public = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.key.public_key()))
            public.update(kid=self.kid, alg="RS256", use="sig")
            return httpx.Response(200, json=self.jwks_changes or {"keys": [public]})
        if request.url.path.endswith("token"):
            return httpx.Response(
                200,
                json={
                    "token_type": "Bearer",
                    "access_token": self.token(changes=self.changes),
                    "id_token": self.token(False),
                    "refresh_token": "synthetic-refresh-token",
                    **self.result_changes,
                },
            )
        raise AssertionError("Unexpected endpoint")

    def client(self):
        return OidcProvider(self.settings, keyring(), transport=httpx.MockTransport(self.handle))


@asynccontextmanager
async def application(settings):
    async with storage(settings) as (database, store, ring):
        source = SyntheticProvider()
        output = io.StringIO()
        app = create_app(
            settings,
            auth_settings=source.settings,
            auth_secrets=ring,
            oidc_transport=httpx.MockTransport(source.handle),
            log_stream=output,
        )

        @app.get("/test/admin")
        async def admin(principal: Annotated[AuthorizedPrincipal, Depends(require_admin)]):
            return {"subject": principal.subject}

        @app.post("/test/write")
        async def write(principal: Annotated[AuthorizedPrincipal, Depends(require_user)]):
            return {"subject": principal.subject}

        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app),
                base_url=source.settings.public_origin,
            ) as client,
        ):
            app.state.auth_store = store
            yield app, client, source, store, database, output


async def new_session(store, source, *, changes=None, subject="synthetic-user", token=None):
    token = token or source.token(changes=changes)
    return await store.create_session(
        AuthenticatedPrincipal(issuer=source.settings.issuer, subject=subject),
        Credentials(SecretStr(token), SecretStr("private-refresh-token")),
    )


def headers(created):
    return {"Cookie": "agent-session-dev=" + created.cookie.get_secret_value()}
