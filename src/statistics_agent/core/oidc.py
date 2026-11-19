"""Fixed-provider OAuth exchanges; legacy cookie integration remains operational."""

import asyncio
import base64
import hashlib
import hmac
from dataclasses import dataclass, field

import httpx
import jwt
from pydantic import SecretStr

from statistics_agent.contracts.common import AuthenticatedPrincipal
from statistics_agent.contracts.sessions import Credentials
from statistics_agent.core.auth import AuthSettings
from statistics_agent.core.auth_secrets import AuthSecrets
from statistics_agent.core.jwt_verifier import AuthenticationError as AuthenticationError
from statistics_agent.core.jwt_verifier import ExpiredAccess as ExpiredAccess
from statistics_agent.core.jwt_verifier import JwtVerifier


@dataclass(frozen=True)
class VerifiedLogin:
    principal: AuthenticatedPrincipal
    credentials: Credentials = field(repr=False)


class OidcProvider(JwtVerifier):
    """Legacy confidential OAuth client; current cookie runtime stays unchanged."""

    def __init__(self, settings: AuthSettings, secrets: AuthSecrets, *, transport=None):
        super().__init__(settings, transport=transport)
        self._secret = secrets.client_secret

    def _audience(self, access):
        return self.settings.client_id

    def _metadata(self, metadata):
        endpoints = self.settings.validate_metadata(metadata)
        if metadata.get("authorization_response_iss_parameter_supported") is not True:
            raise ValueError
        return endpoints

    def _authorized_party(self, claims, *, access):
        if "azp" in claims and claims["azp"] != self.settings.client_id:
            raise ValueError
        if (
            isinstance(claims["aud"], list)
            and len(claims["aud"]) > 1
            and claims.get("azp") != self.settings.client_id
        ):
            raise ValueError

    async def _decode(self, token: str, *, access: bool):
        return await super()._decode(token, access=access, allow_expired_access=access)

    async def exchange(self, code: str, attempt) -> VerifiedLogin:
        endpoints = await self.endpoints()
        result = await self._json(
            "POST",
            endpoints.token_endpoint,
            exchange=True,
            data={
                "grant_type": "authorization_code",
                "client_id": self.settings.client_id,
                "client_secret": self._secret.get_secret_value(),
                "redirect_uri": self.settings.callback_url,
                "code": code,
                "code_verifier": attempt.verifier.get_secret_value(),
            },
        )
        try:
            if result.get("token_type", "").lower() != "bearer":
                raise ValueError
            for name in ("access_token", "refresh_token", "id_token"):
                if not isinstance(result.get(name), str) or not 1 <= len(result[name]) <= 32768:
                    raise ValueError
        except (ValueError, AttributeError):
            raise AuthenticationError() from None
        identity, _ = await self._decode(result["id_token"], access=False)
        nonce = identity.get("nonce")
        if not isinstance(nonce, str) or not hmac.compare_digest(
            nonce.encode(), attempt.nonce.get_secret_value().encode()
        ):
            raise AuthenticationError()
        if "at_hash" in identity:
            expected = (
                base64.urlsafe_b64encode(
                    hashlib.sha256(result["access_token"].encode()).digest()[:16]
                )
                .decode()
                .rstrip("=")
            )
            if not isinstance(identity["at_hash"], str) or not hmac.compare_digest(
                identity["at_hash"].encode(), expected.encode()
            ):
                raise AuthenticationError()
        principal = await self.validate_access(result["access_token"])
        if principal.subject != identity["sub"]:
            raise AuthenticationError()
        return VerifiedLogin(
            principal,
            Credentials(
                SecretStr(result["access_token"]),
                SecretStr(result["refresh_token"]),
                SecretStr(result["id_token"]),
            ),
        )

    async def refresh(self, credentials: Credentials, issuer: str, subject: str) -> Credentials:
        endpoints = await self.endpoints()
        result = await self._json(
            "POST",
            endpoints.token_endpoint,
            exchange=True,
            data={
                "grant_type": "refresh_token",
                "client_id": self.settings.client_id,
                "client_secret": self._secret.get_secret_value(),
                "refresh_token": credentials.refresh_token.get_secret_value(),
            },
        )
        if (
            not isinstance(result.get("token_type"), str)
            or result["token_type"].lower() != "bearer"
            or any(
                not isinstance(result.get(name), str) or not 1 <= len(result[name]) <= 32768
                for name in ("access_token", "refresh_token")
            )
        ):
            raise AuthenticationError()
        identity = await self.validate_access(result["access_token"])
        if identity.issuer != issuer or identity.subject != subject:
            raise AuthenticationError()
        id_token = credentials.id_token
        if "id_token" in result:
            if not isinstance(result["id_token"], str):
                raise AuthenticationError()
            renewed, _ = await self._decode(result["id_token"], access=False)
            if renewed["sub"] != subject:
                raise AuthenticationError()
            if "nonce" in renewed or "auth_time" in renewed:
                # Original ID token was verified at login and is authenticated in
                # encrypted storage; this read supplies comparison context only.
                original = (
                    jwt.decode(
                        credentials.id_token.get_secret_value(), options={"verify_signature": False}
                    )
                    if credentials.id_token
                    else {}
                )
                for claim in ("nonce", "auth_time"):
                    if claim in renewed and renewed[claim] != original.get(claim):
                        raise AuthenticationError()
            if "at_hash" in renewed:
                expected = (
                    base64.urlsafe_b64encode(
                        hashlib.sha256(result["access_token"].encode()).digest()[:16]
                    )
                    .decode()
                    .rstrip("=")
                )
                if renewed["at_hash"] != expected:
                    raise AuthenticationError()
            id_token = SecretStr(result["id_token"])
        return Credentials(
            SecretStr(result["access_token"]), SecretStr(result["refresh_token"]), id_token
        )

    async def logout(self, credentials: Credentials | None) -> bool:
        """RP-Initiated Logout POST; no token in URLs or browser-cookie forwarding."""
        if credentials is None or credentials.id_token is None:
            return False
        try:
            async with asyncio.timeout(self.settings.request_timeout):
                endpoints = await self.endpoints()
                async with self._http.stream(
                    "POST",
                    endpoints.end_session_endpoint,
                    data={
                        "id_token_hint": credentials.id_token.get_secret_value(),
                        "client_id": self.settings.client_id,
                    },
                ) as response:
                    if response.status_code not in {200, 204}:
                        return False
                    size = 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > 131072:
                            return False
                    return True
        except (AuthenticationError, httpx.HTTPError, TimeoutError):
            return False
