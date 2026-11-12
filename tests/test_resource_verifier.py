"""Real RSA access-token checks without client secrets, refresh or session storage."""

import asyncio
import base64
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx
import jwt
import pytest
from pydantic import ValidationError
from sqlalchemy.exc import OperationalError

from statistics_agent.contracts.auth import AuthFailureCode
from statistics_agent.core.bearer_access import BearerAccess
from statistics_agent.core.jwt_verifier import AuthenticationError, ExpiredAccess
from statistics_agent.core.resource_auth import ResourceAuthSettings, load_resource_auth_settings
from statistics_agent.core.resource_verifier import ResourceServerVerifier
from statistics_agent.core.settings import ConfigurationError
from statistics_agent.persistence.identity_projection import IdentityProjection
from support.auth import SyntheticProvider
from support.database import run
from support.datasets import storage


@asynccontextmanager
async def verifier():
    source = SyntheticProvider()
    client = ResourceServerVerifier(
        ResourceAuthSettings(app_env="test", _env_file=None),
        transport=httpx.MockTransport(source.handle),
    )
    try:
        yield source, client
    finally:
        await client.close()


def token(source, *, lifetime=300, not_before_in=None, **changes):
    # Anchor relative claims together at minting, never at test collection.
    now = int(datetime.now(UTC).timestamp())
    claims = {"azp": "agent-web", "iat": now, "exp": now + lifetime}
    if not_before_in is not None:
        claims["nbf"] = now + not_before_in
    return source.token(changes=claims | changes)


@pytest.mark.postgres
def test_bearer_access_applies_committed_revocations_and_replay(migration_settings):
    async def exercise():
        async with (
            storage(migration_settings) as (database, _),
            verifier() as (source, client),
        ):
            projection = IdentityProjection(database, client.settings.issuer)
            bearer = BearerAccess(client, projection)
            old = token(source, sid="old-session")
            access = await bearer.verify_access(old)

            def event(kind, data):
                return json.dumps(
                    {
                        "schema_version": 1,
                        "event_id": str(uuid4()),
                        "event_type": kind,
                        "issuer": access.identity.issuer,
                        "subject": access.identity.subject,
                        "occurred_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                        "data": data,
                    }
                ).encode()

            body = event(
                "user.session.revoked",
                {"security_revision": 1, "session_id": "old-session", "reason": "logout"},
            )
            assert await projection.apply(body)
            assert not await projection.apply(body)
            with pytest.raises(AuthenticationError, match="login_required"):
                await bearer.verify_access(old)

            other = token(source, sid="other-session", iat=access.issued_at, exp=access.expires_at)
            await bearer.verify_access(other)
            assert await projection.apply(
                event(
                    "user.sessions.revoked",
                    {
                        "security_revision": 2,
                        "tokens_issued_at_or_before": access.issued_at,
                        "reason": "logout_all",
                    },
                )
            )
            with pytest.raises(AuthenticationError, match="login_required"):
                await bearer.verify_access(other)

    run(exercise())


def test_bearer_access_requires_a_verified_session_and_fails_closed(monkeypatch):
    async def exercise():
        async with verifier() as (source, client):
            projection = IdentityProjection(None, client.settings.issuer)
            reads = []

            async def failed_read(identity, **kwargs):
                reads.append((identity, kwargs))
                raise OperationalError(None, None, RuntimeError("private database error"))

            monkeypatch.setattr(projection, "is_revoked", failed_read)
            bearer = BearerAccess(client, projection)
            for bad in ("invalid-jwt", token(source), token(source, sid="", aud="wrong")):
                with pytest.raises(AuthenticationError, match="login_required"):
                    await bearer.verify_access(bad)
            assert not reads

            with pytest.raises(AuthenticationError, match="identity_unavailable"):
                await bearer.verify_access(token(source, sid="verified-session"))
            assert len(reads) == 1

    asyncio.run(exercise())


def test_resource_verifier_needs_no_private_secret_files_or_database(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Resource verifier must not read private files")

    monkeypatch.setattr(Path, "open", forbidden)
    monkeypatch.setenv("OIDC_CLIENT_SECRET_FILE", "does-not-exist")
    monkeypatch.setenv("AUTH_TOKEN_ENCRYPTION_KEYS_FILE", "does-not-exist")
    settings = load_resource_auth_settings()
    assert settings.audience == "agent-api" and settings.authorized_parties == ("agent-web",)
    assert not {"client_secret_file", "encryption_keys_file"}.intersection(
        type(settings).model_fields
    )

    async def exercise():
        async with verifier() as (source, client):
            assert not source.calls
            result = await client.verify_access(token(source, sid="identity-session"))
            assert result.identity.subject == "synthetic-user"
            assert result.identity_session_id == "identity-session"
            assert not hasattr(result.identity, "auth_session_ref")
            assert not hasattr(client, "exchange") and not hasattr(client, "refresh")
            assert all(call.method == "GET" for call in source.calls)

    asyncio.run(exercise())


def test_multi_audience_separate_authorized_party_roles_and_bounded_profile_hints():
    async def exercise():
        async with verifier() as (source, client):
            value = token(
                source,
                aud=["agent-api", "account"],
                name="José Analyst",
                preferred_username="analyst",
                email="analyst@example.test",
                picture="ignored",
                resource_access={"agent-api": {"roles": ["admin", "unrelated"]}},
            )
            result = await client.verify_access(value)
            assert result.identity.roles == frozenset({"user", "admin"})
            assert result.profile_hints.display_name == "José Analyst"
            assert result.profile_hints.username == "analyst"
            assert result.profile_hints.email == "analyst@example.test"
            assert result.identity_session_id is None
            assert not hasattr(result.profile_hints, "picture")
            assert set(result.model_dump()) == {
                "identity",
                "issued_at",
                "expires_at",
                "identity_session_id",
                "profile_hints",
            }
            assert value not in repr(result)

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "changes",
    [
        {"aud": "agent-web"},
        {"aud": ["account"]},
        {"azp": "agent-api"},
        {"azp": None},
        {"azp": ["agent-web"]},
        {"iss": "https://wrong.example/realms/datalens"},
        {"typ": "ID"},
        {"iat": True},
        {"nbf": True},
        {"nbf": "123"},
        pytest.param({"not_before_in": 600}, id="future-not-before"),
        {"sub": " "},
        pytest.param({"lifetime": 601}, id="excessive-lifetime"),
        {"resource_access": {"agent-api": {"roles": "admin"}}},
        {"email_verified": "true"},
        {"sid": ""},
        {"sid": 12},
        {"sid": "x" * 256},
    ],
)
def test_invalid_signed_tokens_are_rejected(changes):
    async def exercise():
        async with verifier() as (source, client):
            with pytest.raises(AuthenticationError) as error:
                await client.verify_access(token(source, **changes))
            assert error.value.code == AuthFailureCode.LOGIN_REQUIRED

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "lifetime,allowed",
    [pytest.param(300, True, id="maximum-valid"), pytest.param(301, False, id="too-long")],
)
def test_lifetime_limit_is_measured_from_token_minting(lifetime, allowed):
    async def exercise():
        async with verifier() as (source, client):
            value = token(source, lifetime=lifetime)
            if allowed:
                result = await client.verify_access(value)
                assert result.expires_at - result.issued_at == lifetime
            else:
                with pytest.raises(AuthenticationError):
                    await client.verify_access(value)

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "changes",
    [
        {"email_verified": False},
        {"resource_access": {}},
        {"resource_access": {"agent-web": {"roles": ["admin"]}}},
    ],
)
def test_roles_are_scoped_to_the_api_not_the_frontend_client(changes):
    async def exercise():
        async with verifier() as (source, client):
            with pytest.raises(AuthenticationError) as error:
                await client.verify_access(token(source, **changes))
            assert error.value.code == AuthFailureCode.ACCESS_DENIED

    asyncio.run(exercise())


def test_missing_azp_and_expired_credentials_are_not_refreshable_by_resource_server():
    async def exercise():
        async with verifier() as (source, client):
            claims = jwt.decode(token(source), options={"verify_signature": False})
            claims.pop("azp")
            missing = jwt.encode(claims, source.key, algorithm="RS256", headers={"kid": source.kid})
            now = int(datetime.now(UTC).timestamp())
            for value in (missing, token(source, iat=now - 301, exp=now - 1)):
                with pytest.raises(AuthenticationError) as error:
                    await client.verify_access(value)
                assert type(error.value) is AuthenticationError
                assert not isinstance(error.value, ExpiredAccess)

    asyncio.run(exercise())


def test_cache_fetch_is_coalesced_unknown_keys_are_throttled_and_outages_preserve_trust():
    async def exercise():
        async with verifier() as (source, client):
            original = token(source)
            await asyncio.gather(*(client.verify_access(original) for _ in range(8)))
            assert len(source.calls) == 2
            source.offline = True
            await client.verify_access(original)
            unknown = source.token(changes={"azp": "agent-web"}, headers={"kid": "unknown"})
            client._last_fetch = float("-inf")
            errors = await asyncio.gather(
                *(client.verify_access(unknown) for _ in range(8)),
                return_exceptions=True,
            )
            assert all(isinstance(error, AuthenticationError) for error in errors)
            assert len(source.calls) == 3
            await client.verify_access(original)
            client._trusted_until = 0
            with pytest.raises(AuthenticationError):
                await client.verify_access(original)
            source.offline, source.kid, client._last_fetch = False, "rotated", float("-inf")
            await client.verify_access(token(source))
            assert len(source.calls) == 5

    asyncio.run(exercise())


def test_wrong_signature_and_untrusted_header_urls_never_authorize():
    async def exercise():
        async with verifier() as (source, client):
            bad_header = source.token(
                changes={"azp": "agent-web"},
                headers={"kid": "first", "jku": "https://untrusted.example/keys"},
            )
            with pytest.raises(AuthenticationError):
                await client.verify_access(bad_header)
            assert not source.calls
            other = SyntheticProvider()
            with pytest.raises(AuthenticationError):
                await client.verify_access(token(other))

    asyncio.run(exercise())


def test_recursive_untrusted_header_is_rejected_without_fetching_keys():
    async def exercise():
        async with verifier() as (source, client):
            raw = '{"alg":"RS256","kid":"first","nested":' + "[" * 2000 + "0" + "]" * 2000 + "}"
            encoded = base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")
            with pytest.raises(AuthenticationError):
                await client.verify_access(encoded + ".e30.signature")
            assert not source.calls

    asyncio.run(exercise())


def test_cancelled_key_refresh_releases_mutex_and_next_verification_can_recover():
    async def exercise():
        source, entered, hold = SyntheticProvider(), asyncio.Event(), asyncio.Event()

        async def delayed(request):
            if not hold.is_set():
                entered.set()
                await hold.wait()
            return source.handle(request)

        client = ResourceServerVerifier(
            ResourceAuthSettings(app_env="test", _env_file=None),
            transport=httpx.MockTransport(delayed),
        )
        try:
            pending = asyncio.create_task(client.verify_access(token(source)))
            await asyncio.wait_for(entered.wait(), 1)
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
            assert not client._lock.locked()
            hold.set()
            client._last_fetch = float("-inf")
            await asyncio.wait_for(client.verify_access(token(source)), 1)
        finally:
            await client.close()

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "options",
    [
        {"issuer": "http://public.example/realms/datalens"},
        {"issuer": "https://identity.example/realms/../admin"},
        {"issuer": "https://identity.example/realms/datalens/"},
        {"app_env": "production"},
        {"audience": "*"},
        {"authorized_parties": ()},
        {"authorized_parties": ("agent-web", "agent-web")},
        {"authorized_parties": ("*",)},
        {"read_timeout": float("nan")},
        {"request_timeout": 1},
    ],
)
def test_trust_configuration_rejects_unsafe_urls_parties_and_limits(options):
    with pytest.raises(ValidationError):
        ResourceAuthSettings(**options, _env_file=None)


def test_hosted_https_fixed_jwks_and_loader_errors_are_safe(monkeypatch):
    settings = ResourceAuthSettings(
        app_env="production", issuer="https://identity.example/realms/datalens", _env_file=None
    )
    assert (
        settings.validate_metadata(
            {
                "issuer": settings.issuer,
                "jwks_uri": settings.issuer + "/protocol/openid-connect/certs",
            }
        ).issuer
        == settings.issuer
    )
    with pytest.raises(ConfigurationError):
        settings.validate_metadata({"issuer": settings.issuer, "jwks_uri": "https://evil.example"})
    monkeypatch.setenv("OIDC_ISSUER", "https://private:password@identity.example/realms/datalens")
    with pytest.raises(ConfigurationError) as error:
        load_resource_auth_settings()
    assert str(error.value) == "Invalid resource-server authentication configuration"
    assert error.value.__context__ is None
