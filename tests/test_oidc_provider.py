"""Real RSA signatures with controlled provider failures; no unsigned JWT mocks."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime

import httpx
import jwt
import pytest
from pydantic import SecretStr

from statistics_agent.contracts.auth import AuthFailureCode
from statistics_agent.core.oidc import AuthenticationError, OidcProvider
from support.auth import SyntheticProvider
from support.crypto import keyring


def test_exchange_validates_signatures_and_sends_only_fixed_endpoint_credentials():
    async def exercise():
        source = SyntheticProvider()
        provider = source.client()
        try:
            login = await provider.exchange("synthetic-code", source.attempt)
            assert login.principal.subject == "synthetic-user"
            assert "synthetic-refresh-token" not in repr(login)
            request = source.calls[-1]
            assert request.method == "POST"
            assert b"code_verifier=synthetic-pkce-verifier" in request.content
            assert not any("introspect" in str(call.url) for call in source.calls)
        finally:
            await provider.close()

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "change",
    [
        {"iss": "https://wrong.example"},
        {"aud": "another-client"},
        {"sub": ""},
        {"iat": True},
        {"exp": False},
        {"exp": int(datetime.now(UTC).timestamp()) - 1},
        {"exp": int(datetime.now(UTC).timestamp()) + 3600},
        {"typ": "ID"},
        {"azp": "other-client"},
        {"email_verified": "true"},
        {"resource_access": {"agent-api": {"roles": "admin"}}},
    ],
    ids=[
        "issuer",
        "audience",
        "subject",
        "iat-type",
        "exp-type",
        "expired",
        "lifetime",
        "token-kind",
        "authorized-party",
        "email-type",
        "role-type",
    ],
)
def test_invalid_access_claims_are_denied(change):
    async def exercise():
        source = SyntheticProvider()
        provider = source.client()
        try:
            with pytest.raises(AuthenticationError) as caught:
                await provider.validate_access(source.token(changes=change))
            assert caught.value.code == AuthFailureCode.LOGIN_REQUIRED
        finally:
            await provider.close()

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "change",
    [
        {"email_verified": False},
        {"resource_access": {}},
        {"resource_access": {"agent-api": {"roles": []}}},
    ],
)
def test_valid_token_without_permission_is_forbidden(change):
    async def exercise():
        source = SyntheticProvider()
        provider = source.client()
        try:
            with pytest.raises(AuthenticationError) as caught:
                await provider.validate_access(source.token(changes=change))
            assert caught.value.code == AuthFailureCode.ACCESS_DENIED
        finally:
            await provider.close()

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "fault",
    [
        "nonce",
        "id-audience",
        "id-issuer",
        "subject",
        "signature",
        "at-hash",
        "missing-token",
        "token-type",
        "algorithm",
    ],
)
def test_malformed_exchange_never_creates_verified_login(fault):
    async def exercise():
        source = SyntheticProvider()
        if fault == "nonce":
            source.attempt = replace(source.attempt, nonce=SecretStr("wrong-nonce"))
            source.result_changes["id_token"] = source.token(False, {"nonce": "original-nonce"})
        elif fault in {"id-audience", "id-issuer", "subject", "at-hash"}:
            changed = {
                "id-audience": {"aud": "other"},
                "id-issuer": {"iss": "https://other.example"},
                "subject": {"sub": "other"},
                "at-hash": {"at_hash": "wrong"},
            }[fault]
            source.result_changes["id_token"] = source.token(False, changed)
        elif fault == "signature":
            other = SyntheticProvider()
            source.result_changes["access_token"] = other.token()
        elif fault == "missing-token":
            source.result_changes["refresh_token"] = None
        elif fault == "token-type":
            source.result_changes["token_type"] = "MAC"
        else:
            source.result_changes["access_token"] = jwt.encode(
                {"sub": "fake"}, "x" * 40, algorithm="HS256", headers={"kid": "first"}
            )
        provider = source.client()
        try:
            with pytest.raises(AuthenticationError):
                await provider.exchange("synthetic-code", source.attempt)
        finally:
            await provider.close()

    asyncio.run(exercise())


def test_cached_keys_outage_unknown_kid_coalescing_rollover_and_expired_trust():
    async def exercise():
        source = SyntheticProvider()
        provider = source.client()
        try:
            original = source.token()
            await asyncio.gather(*(provider.validate_access(original) for _ in range(5)))
            assert len(source.calls) == 2
            source.offline = True
            await provider.validate_access(original)
            assert len(source.calls) == 2
            unknown = source.token(headers={"kid": "unknown"})
            with pytest.raises(AuthenticationError):
                await provider.validate_access(unknown)
            assert len(source.calls) == 2
            provider._last_fetch = float("-inf")
            with pytest.raises(AuthenticationError) as caught:
                await provider.validate_access(unknown)
            assert caught.value.code == AuthFailureCode.IDENTITY_UNAVAILABLE
            await provider.validate_access(original)  # Optional failure preserves known-key trust.
            source.offline = False
            source.kid = "rotated"
            provider._last_fetch = float("-inf")
            await provider.validate_access(source.token())
            source.offline = True
            provider._trusted_until = 0
            provider._last_fetch = float("-inf")
            with pytest.raises(AuthenticationError) as caught:
                await provider.validate_access(source.token())
            assert caught.value.code == AuthFailureCode.IDENTITY_UNAVAILABLE
        finally:
            await provider.close()

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "headers",
    [
        {"kid": "first", "jku": "https://untrusted.example/keys"},
        {"kid": "first", "x5u": "https://untrusted.example/cert"},
        {"kid": "first", "crit": ["unknown-extension"]},
        {"typ": "JWT"},
    ],
)
def test_untrusted_header_key_urls_and_extensions_are_rejected_before_fetch(headers):
    async def exercise():
        source = SyntheticProvider()
        provider = source.client()
        try:
            with pytest.raises(AuthenticationError):
                await provider.validate_access(source.token(headers=headers))
            assert not source.calls
        finally:
            await provider.close()

    asyncio.run(exercise())


def test_coalesced_unknown_kid_refresh_does_not_fetch_per_waiter():
    async def exercise():
        source = SyntheticProvider()
        provider = source.client()
        try:
            await provider.validate_access(source.token())
            provider._last_fetch = float("-inf")
            unknown = source.token(headers={"kid": "not-in-provider"})
            results = await asyncio.gather(
                *(provider.validate_access(unknown) for _ in range(8)),
                return_exceptions=True,
            )
            assert all(isinstance(result, AuthenticationError) for result in results)
            assert all(result.code == AuthFailureCode.LOGIN_REQUIRED for result in results)
            assert len(source.calls) == 4  # One initial and one coalesced metadata/JWKS fetch.
        finally:
            await provider.close()

    asyncio.run(exercise())


def test_metadata_endpoint_substitution_cannot_receive_client_credentials():
    async def exercise():
        source = SyntheticProvider()

        def malicious(request):
            response = source.handle(request)
            metadata = response.json()
            metadata["token_endpoint"] = "https://untrusted.example/token"
            return httpx.Response(200, json=metadata)

        provider = OidcProvider(
            source.settings, keyring(), transport=httpx.MockTransport(malicious)
        )
        try:
            with pytest.raises(AuthenticationError) as caught:
                await provider.exchange("synthetic-code", source.attempt)
            assert caught.value.code == AuthFailureCode.IDENTITY_UNAVAILABLE
            assert len(source.calls) == 1 and source.calls[0].method == "GET"
        finally:
            await provider.close()

    asyncio.run(exercise())
