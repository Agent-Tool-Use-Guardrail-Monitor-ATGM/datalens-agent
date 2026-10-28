"""HTTP protocol failures and startup requirements without a live database."""

import asyncio
import io
from datetime import UTC, datetime
from types import SimpleNamespace
from urllib.parse import parse_qs, urlencode, urlsplit
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr

from statistics_agent.app import create_app
from statistics_agent.core.auth import AuthSettings
from statistics_agent.core.settings import ConfigurationError, Settings
from statistics_agent.persistence.auth import LoginStart, PendingLoginError
from test_oidc_provider import SyntheticProvider
from test_token_encryption import keyring


class ProtocolStore:
    """Only isolates HTTP behavior; PostgreSQL correctness has separate real tests."""

    def __init__(self, source):
        self.source = source
        self.consumed = False
        self.pending = False
        self.created = 0

    async def start_login(self, binding):
        if self.pending:
            raise PendingLoginError("Login pending")
        self.pending = True
        return LoginStart(
            uuid4(),
            datetime.now(UTC),
            SecretStr("s" * 43),
            SecretStr("b" * 43),
            self.source.attempt.nonce,
            self.source.attempt.verifier,
        )

    async def consume_login(self, state, binding):
        if (
            self.consumed
            or state.get_secret_value() != "s" * 43
            or binding.get_secret_value() != "b" * 43
        ):
            return None
        self.consumed = True
        return self.source.attempt

    async def create_session(self, principal, credentials):
        self.created += 1
        return SimpleNamespace(cookie=SecretStr("c" * 43))


@pytest.mark.parametrize(
    "fault",
    [
        "issuer",
        "duplicate-issuer",
        "duplicate-code",
        "missing-code",
        "provider-error",
        "unknown-field",
        "duplicate-state",
        "bad-state",
        "missing-binding",
        "duplicate-binding",
        "token-outage",
    ],
)
def test_bad_callbacks_never_create_a_session_and_do_not_echo_inputs(fault):
    async def exercise():
        source = SyntheticProvider()
        output = io.StringIO()
        settings = Settings(
            app_env="test",
            database_url="postgresql+psycopg://unused:unused@localhost/agent_test_db",
        )
        app = create_app(
            settings,
            auth_settings=source.settings,
            auth_secrets=keyring(),
            oidc_transport=httpx.MockTransport(source.handle),
            log_stream=output,
        )
        store = ProtocolStore(source)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app),
                base_url=source.settings.public_origin,
            ) as client,
        ):
            app.state.auth_store = store
            started = await client.get("/auth/login")
            assert started.status_code == 303
            assert parse_qs(urlsplit(started.headers["location"]).query)[
                "code_challenge_method"
            ] == ["S256"]
            query = [("state", "s" * 43), ("iss", source.settings.issuer), ("code", "private-code")]
            headers = {"Cookie": "agent-login-dev=" + "b" * 43}
            if fault == "issuer":
                query[1] = ("iss", "https://wrong.example")
            elif fault == "duplicate-issuer":
                query.append(query[1])
            elif fault == "duplicate-code":
                query.append(query[2])
            elif fault == "missing-code":
                query.pop()
            elif fault == "provider-error":
                query.pop()
                query.extend([("error", "access_denied"), ("error_description", "private-error")])
            elif fault == "unknown-field":
                query.append(("return_to", "https://wrong.example"))
            elif fault == "duplicate-state":
                query.append(query[0])
            elif fault == "bad-state":
                query[0] = ("state", "private-invalid-state")
            elif fault == "missing-binding":
                headers = {"Cookie": "unrelated=value"}
            elif fault == "duplicate-binding":
                headers["Cookie"] += "; agent-login-dev=" + "b" * 43
            else:
                source.offline = True
            response = await client.get("/auth/callback?" + urlencode(query), headers=headers)
            assert response.status_code == (503 if fault == "token-outage" else 401)
            assert response.headers["cache-control"] == "no-store"
            assert response.headers["referrer-policy"] == "no-referrer"
            assert store.created == 0
            assert source.settings.session_cookie_name not in response.cookies
            assert "private-code" not in response.text + output.getvalue()
            assert "private-error" not in response.text + output.getvalue()
            if fault not in {
                "duplicate-state",
                "bad-state",
                "missing-binding",
                "duplicate-binding",
            }:
                assert store.consumed

    asyncio.run(exercise())


def test_login_rejects_redirects_host_spoofing_and_pending_overwrite_and_outage_keeps_liveness():
    async def exercise():
        source = SyntheticProvider()
        settings = Settings(
            app_env="test",
            database_url="postgresql+psycopg://unused:unused@localhost/agent_test_db",
        )
        app = create_app(
            settings,
            auth_settings=source.settings,
            auth_secrets=keyring(),
            oidc_transport=httpx.MockTransport(source.handle),
            log_stream=io.StringIO(),
        )
        store = ProtocolStore(source)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app),
                base_url=source.settings.public_origin,
            ) as client,
        ):
            app.state.auth_store = store
            assert (
                await client.get("/auth/login?return_to=https://wrong.example")
            ).status_code == 401
            assert (
                await client.get("/auth/login", headers={"Host": "wrong.example"})
            ).status_code == 401
            assert not source.calls
            assert (await client.get("/auth/login")).status_code == 303
            pending = await client.get("/auth/login")
            assert pending.status_code == 409 and "set-cookie" not in pending.headers
            app.state.oidc._trusted_until = 0
            app.state.oidc._last_fetch = float("-inf")
            source.offline = True
            assert (await client.get("/auth/login")).status_code == 503
            assert (await client.get("/health/live")).status_code == 200

    asyncio.run(exercise())


def test_production_cookie_is_host_only_secure_and_httponly():
    async def exercise():
        source = SyntheticProvider()
        source.settings = AuthSettings(
            app_env="production",
            issuer="https://identity.example/realms/test",
            public_origin="https://agent.example",
            client_secret_file="unused-client",
            encryption_keys_file="unused-ring",
        )
        settings = Settings(
            app_env="production",
            database_url="postgresql+psycopg://unused:unused@localhost/agent_db",
        )
        app = create_app(
            settings,
            auth_settings=source.settings,
            auth_secrets=keyring(),
            oidc_transport=httpx.MockTransport(source.handle),
            log_stream=io.StringIO(),
        )
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app),
                base_url=source.settings.public_origin,
            ) as client,
        ):
            app.state.auth_store = ProtocolStore(source)
            response = await client.get("/auth/login")
            cookie = response.headers["set-cookie"]
            assert cookie.startswith("__Host-agent-login=")
            assert all(
                value in cookie for value in ("HttpOnly", "Secure", "Path=/", "SameSite=lax")
            )
            assert "Domain=" not in cookie
            response = await client.get(
                "/auth/callback",
                params={
                    "state": "s" * 43,
                    "iss": source.settings.issuer,
                    "code": "synthetic-code",
                },
            )
            assert response.status_code == 303
            session_cookie = response.headers.get_list("set-cookie")[0]
            assert session_cookie.startswith("__Host-agent-session=")
            assert all(
                value in session_cookie
                for value in ("HttpOnly", "Secure", "Path=/", "SameSite=lax")
            )
            assert "Domain=" not in session_cookie

    asyncio.run(exercise())


def test_factory_requires_auth_configuration_and_secrets(monkeypatch):
    for name, field in AuthSettings.model_fields.items():
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(str(field.validation_alias), raising=False)
    settings = Settings(
        app_env="test", database_url="postgresql+psycopg://unused:unused@localhost/agent_test_db"
    )
    with pytest.raises(ConfigurationError, match="Invalid authentication configuration"):
        create_app(settings)
    auth = AuthSettings(
        app_env="test", client_secret_file="missing-client", encryption_keys_file="missing-ring"
    )
    with pytest.raises(ConfigurationError, match="Invalid authentication secrets"):
        create_app(settings, auth_settings=auth)
