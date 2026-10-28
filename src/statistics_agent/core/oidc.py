"""Fixed-provider OAuth exchange and bounded signing-key trust; no introspection."""

import asyncio
import base64
import hashlib
import hmac
import json
from dataclasses import dataclass, field
from time import monotonic

import httpx
import jwt
from pydantic import SecretStr

from statistics_agent.contracts.auth import AuthFailureCode
from statistics_agent.contracts.common import AuthenticatedPrincipal
from statistics_agent.core.auth import AuthSettings
from statistics_agent.core.auth_secrets import AuthSecrets, unique_object
from statistics_agent.core.settings import ConfigurationError
from statistics_agent.persistence.auth import Credentials


class AuthenticationError(Exception):
    """Only a fixed failure code may cross the HTTP boundary."""

    def __init__(self, code=AuthFailureCode.LOGIN_REQUIRED):
        self.code = code
        super().__init__(code.value)


@dataclass(frozen=True)
class VerifiedLogin:
    principal: AuthenticatedPrincipal
    credentials: Credentials = field(repr=False)


class OidcProvider:
    """One client and coalesced cache per API lifespan; no startup network calls."""

    def __init__(self, settings: AuthSettings, secrets: AuthSecrets, *, transport=None):
        self.settings = settings
        self._secret = secrets.client_secret
        self._http = httpx.AsyncClient(
            trust_env=False,
            follow_redirects=False,
            transport=transport,
            timeout=httpx.Timeout(
                settings.read_timeout,
                connect=settings.connect_timeout,
                pool=settings.pool_timeout,
                write=settings.read_timeout,
            ),
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
        )
        self._endpoints = None
        self._keys = {}
        self._trusted_until = 0.0
        self._last_fetch = float("-inf")
        self._last_failed = False
        self._lock = asyncio.Lock()

    async def close(self):
        await self._http.aclose()

    async def _json(self, method, url, *, data=None, exchange=False):
        failure = AuthFailureCode.IDENTITY_UNAVAILABLE
        try:
            async with asyncio.timeout(self.settings.request_timeout):
                async with self._http.stream(method, url, data=data) as response:
                    if response.status_code != 200:
                        if exchange and response.status_code in {400, 401}:
                            failure = AuthFailureCode.LOGIN_REQUIRED
                        raise ValueError
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > 131072:
                            raise ValueError
                    result = json.loads(body, object_pairs_hook=unique_object)
                    if not isinstance(result, dict):
                        raise ValueError
                    return result
        except (httpx.HTTPError, TimeoutError, ValueError, UnicodeError, RecursionError):
            pass
        raise AuthenticationError(failure)

    async def _fetch_keys(self):
        started = monotonic()
        self._last_fetch = started
        self._last_failed = True
        metadata = await self._json("GET", self.settings.discovery_url)
        try:
            endpoints = self.settings.validate_metadata(metadata)
            if metadata.get("authorization_response_iss_parameter_supported") is not True:
                raise ValueError
        except (ConfigurationError, ValueError, TypeError):
            raise AuthenticationError(AuthFailureCode.IDENTITY_UNAVAILABLE) from None
        document = await self._json("GET", endpoints.jwks_uri)
        try:
            entries = document.get("keys")
            if not isinstance(entries, list) or not 1 <= len(entries) <= 32:
                raise ValueError
            keys = {}
            for entry in entries:
                if not isinstance(entry, dict):
                    raise ValueError
                if entry.get("kty") != "RSA" or entry.get("use", "sig") != "sig":
                    continue
                if entry.get("alg", "RS256") != "RS256":
                    continue
                if "d" in entry or ("key_ops" in entry and entry["key_ops"] != ["verify"]):
                    raise ValueError
                kid = entry.get("kid")
                if not isinstance(kid, str) or not 1 <= len(kid) <= 128 or kid in keys:
                    raise ValueError
                key = jwt.PyJWK.from_dict(entry, algorithm="RS256").key
                if key.key_size < 2048:
                    raise ValueError
                keys[kid] = key
            if not keys:
                raise ValueError
        except (ValueError, TypeError, KeyError, jwt.PyJWTError):
            raise AuthenticationError(AuthFailureCode.IDENTITY_UNAVAILABLE) from None
        self._keys = keys
        self._endpoints = endpoints
        self._trusted_until = started + 3600
        self._last_failed = False

    async def endpoints(self):
        if self._endpoints is not None and monotonic() < self._trusted_until:
            return self._endpoints
        await self._ensure_keys(None)
        return self._endpoints

    async def _ensure_keys(self, kid):
        if kid in self._keys and monotonic() < self._trusted_until:
            return self._keys[kid]
        async with self._lock:
            if kid in self._keys and monotonic() < self._trusted_until:
                return self._keys[kid]
            if kid is None and self._endpoints and monotonic() < self._trusted_until:
                return None
            if monotonic() - self._last_fetch >= 5:
                try:
                    await self._fetch_keys()
                finally:
                    self._last_fetch = monotonic()
            if monotonic() >= self._trusted_until or self._last_failed:
                raise AuthenticationError(AuthFailureCode.IDENTITY_UNAVAILABLE)
            if kid is not None and kid not in self._keys:
                raise AuthenticationError()
            return self._keys.get(kid)

    async def _decode(self, token: str, *, access: bool):
        try:
            if not isinstance(token, str) or not 1 <= len(token) <= 32768:
                raise ValueError
            header = jwt.get_unverified_header(token)
            if header.get("alg") not in self.settings.allowed_algorithms:
                raise ValueError
            kid = header.get("kid")
            if not isinstance(kid, str) or not 1 <= len(kid) <= 128:
                raise ValueError
            if header.get("crit") or header.get("jku") or header.get("x5u"):
                raise ValueError
        except (ValueError, TypeError, jwt.PyJWTError):
            raise AuthenticationError() from None
        key = await self._ensure_keys(kid)
        try:
            claims = jwt.decode(
                token,
                key,
                algorithms=list(self.settings.allowed_algorithms),
                issuer=self.settings.issuer,
                audience=self.settings.client_id,
                leeway=0,
                options={"require": ["iss", "sub", "aud", "iat", "exp"]},
            )
            if (
                type(claims["iat"]) is not int
                or type(claims["exp"]) is not int
                or claims["exp"] <= claims["iat"]
                or (access and claims["exp"] - claims["iat"] > 300)
                or not isinstance(claims["sub"], str)
                or not claims["sub"].strip()
                or len(claims["sub"]) > 255
            ):
                raise ValueError
            audience = claims["aud"]
            if not isinstance(audience, str) and (
                not isinstance(audience, list)
                or not audience
                or any(not isinstance(item, str) for item in audience)
            ):
                raise ValueError
            if "azp" in claims and claims["azp"] != self.settings.client_id:
                raise ValueError
            if (
                isinstance(audience, list)
                and len(audience) > 1
                and claims.get("azp") != self.settings.client_id
            ):
                raise ValueError
            if access and claims.get("typ") != "Bearer":
                raise ValueError
            return claims
        except (ValueError, TypeError, KeyError, jwt.PyJWTError):
            pass
        raise AuthenticationError()

    async def validate_access(self, token: str) -> AuthenticatedPrincipal:
        """Reusable local validation; this does not load or authorize a browser session."""
        claims = await self._decode(token, access=True)
        verified = claims.get("email_verified")
        resources = claims.get("resource_access", {})
        if type(verified) is not bool or not isinstance(resources, dict):
            raise AuthenticationError()
        client = resources.get(self.settings.client_id, {})
        roles = client.get("roles", []) if isinstance(client, dict) else None
        if not isinstance(roles, list) or any(not isinstance(role, str) for role in roles):
            raise AuthenticationError()
        if not verified or not {"user", "admin"}.intersection(roles):
            raise AuthenticationError(AuthFailureCode.ACCESS_DENIED)
        return AuthenticatedPrincipal(issuer=claims["iss"], subject=claims["sub"])

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
        identity = await self._decode(result["id_token"], access=False)
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
