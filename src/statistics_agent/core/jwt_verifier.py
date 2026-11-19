"""Shared JWT verification and bounded JWKS trust; no credentials or session storage."""

import asyncio
import json
from time import monotonic

import httpx
import jwt

from statistics_agent.contracts.auth import AuthFailureCode, VerifiedIdentity
from statistics_agent.core.json_validation import unique_object
from statistics_agent.core.settings import ConfigurationError


class AuthenticationError(Exception):
    """Only a fixed failure code may cross the HTTP boundary."""

    def __init__(self, code=AuthFailureCode.LOGIN_REQUIRED):
        self.code = code
        super().__init__(code.value)


class ExpiredAccess(AuthenticationError):
    """All signature, identity and permission checks passed; only expiry failed."""

    def __init__(self, identity):
        super().__init__()
        self.identity = identity


class JwtVerifier:
    """One client and coalesced cache per API lifespan; no startup network calls."""

    def __init__(self, settings, *, transport=None):
        self.settings = settings
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
            event_hooks={"request": [self._no_cookies]},
        )
        self._endpoints = None
        self._keys = {}
        self._trusted_until = 0.0
        self._last_fetch = float("-inf")
        self._last_failed = False
        self._lock = asyncio.Lock()

    @staticmethod
    async def _no_cookies(request):
        # A shared provider client must never forward one user's browser cookies.
        request.headers.pop("cookie", None)

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
            endpoints = self._metadata(metadata)
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

    async def _decode(self, token: str, *, access: bool, allow_expired_access=False):
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
        except (ValueError, TypeError, RecursionError, jwt.PyJWTError):
            raise AuthenticationError() from None
        key = await self._ensure_keys(kid)
        try:
            options = {"require": ["iss", "sub", "aud", "iat", "exp"]}
            expired = False
            try:
                claims = jwt.decode(
                    token,
                    key,
                    algorithms=list(self.settings.allowed_algorithms),
                    issuer=self.settings.issuer,
                    audience=self._audience(access),
                    leeway=0,
                    options=options,
                )
            except jwt.ExpiredSignatureError:
                if not access or not allow_expired_access:
                    raise
                # Verify again without exp only: wrong signature/audience/nbf/etc
                # must never be classified as a refreshable expired credential.
                claims = jwt.decode(
                    token,
                    key,
                    algorithms=list(self.settings.allowed_algorithms),
                    issuer=self.settings.issuer,
                    audience=self._audience(access),
                    leeway=0,
                    options={**options, "verify_exp": False},
                )
                expired = True
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
            self._authorized_party(claims, access=access)
            if access and claims.get("typ") != "Bearer":
                raise ValueError
            return claims, expired
        except (ValueError, TypeError, KeyError, RecursionError, jwt.PyJWTError):
            pass
        raise AuthenticationError()

    async def _access(self, token: str) -> tuple[dict, VerifiedIdentity]:
        """Reusable local validation; this does not load or authorize a browser session."""
        claims, expired = await self._decode(token, access=True)
        verified = claims.get("email_verified")
        resources = claims.get("resource_access", {})
        if type(verified) is not bool or not isinstance(resources, dict):
            raise AuthenticationError()
        client = resources.get(self._audience(True), {})
        roles = client.get("roles", []) if isinstance(client, dict) else None
        if not isinstance(roles, list) or any(not isinstance(role, str) for role in roles):
            raise AuthenticationError()
        if not verified or not {"user", "admin"}.intersection(roles):
            raise AuthenticationError(AuthFailureCode.ACCESS_DENIED)
        permitted = {role for role in roles if role in {"user", "admin"}}
        if "admin" in permitted:
            permitted.add("user")
        identity = VerifiedIdentity(
            issuer=claims["iss"],
            subject=claims["sub"],
            roles=frozenset(permitted),
            email_verified=True,
        )
        if expired:
            raise ExpiredAccess(identity)
        return claims, identity

    async def validate_access(self, token: str) -> VerifiedIdentity:
        return (await self._access(token))[1]

    def _audience(self, access):
        return self.settings.audience

    def _metadata(self, metadata):
        return self.settings.validate_metadata(metadata)

    def _authorized_party(self, claims, *, access):
        if claims.get("azp") not in self.settings.authorized_parties:
            raise ValueError
