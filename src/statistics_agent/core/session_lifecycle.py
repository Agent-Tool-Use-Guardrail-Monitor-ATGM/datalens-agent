"""Durable cross-replica renewal; uncertain rotation never retries old credentials."""

import asyncio

from sqlalchemy.exc import SQLAlchemyError

from statistics_agent.contracts.auth import AuthFailureCode
from statistics_agent.core.oidc import AuthenticationError, ExpiredAccess


class SessionLifecycle:
    def __init__(self, store, provider, settings):
        self.store = store
        self.provider = provider
        self.budget = settings.request_timeout * 3

    @staticmethod
    def _owner(identity, stored):
        if identity.issuer != stored.issuer or identity.subject != stored.subject:
            raise AuthenticationError()

    async def authorize(self, stored, *, required_role="user"):
        try:
            identity = await self.provider.validate_access(
                stored.credentials.access_token.get_secret_value()
            )
        except ExpiredAccess as expired:
            self._owner(expired.identity, stored)
            if required_role not in expired.identity.roles:
                raise AuthenticationError(AuthFailureCode.ACCESS_DENIED) from None
            identity = await self._renew(stored)
        self._owner(identity, stored)
        if required_role not in identity.roles:
            raise AuthenticationError(AuthFailureCode.ACCESS_DENIED)
        return identity

    async def _revoke(self, session_id):
        try:
            await asyncio.shield(self.store.revoke_session(session_id))
        except Exception:
            # A durable claim still blocks reusing the old refresh token if the
            # DB is unavailable. No authorization can succeed without its DB.
            pass

    async def _renew(self, initial):
        try:
            async with asyncio.timeout(self.budget + 2):
                while True:
                    claim = await self.store.claim_refresh(
                        initial.session_id,
                        initial.refresh_version,
                        self.budget + 1,
                    )
                    if claim is None:
                        raise AuthenticationError()
                    if claim.waiting:
                        await asyncio.sleep(0.05)
                        continue
                    if claim.claim_id is None:
                        identity = await self.provider.validate_access(
                            claim.session.credentials.access_token.get_secret_value()
                        )
                        self._owner(identity, claim.session)
                        return identity
                    try:
                        async with asyncio.timeout(self.budget):
                            # Validate again after the locked reread. Never renew
                            # a credential whose only failure is not expiry.
                            try:
                                await self.provider.validate_access(
                                    claim.session.credentials.access_token.get_secret_value()
                                )
                            except ExpiredAccess as expired:
                                self._owner(expired.identity, claim.session)
                            else:
                                raise AuthenticationError()
                            renewed = await self.provider.refresh(
                                claim.session.credentials,
                                claim.session.issuer,
                                claim.session.subject,
                            )
                            committed = await self.store.finish_refresh(
                                initial.session_id,
                                claim.claim_id,
                                claim.session.refresh_version,
                                renewed,
                            )
                            if not committed:
                                raise AuthenticationError()
                            identity = await self.provider.validate_access(
                                renewed.access_token.get_secret_value()
                            )
                            self._owner(identity, claim.session)
                            return identity
                    except asyncio.CancelledError:
                        await self._revoke(initial.session_id)
                        raise
                    except Exception as error:
                        await self._revoke(initial.session_id)
                        if isinstance(error, AuthenticationError):
                            raise
                        raise AuthenticationError(AuthFailureCode.IDENTITY_UNAVAILABLE) from None
        except TimeoutError:
            raise AuthenticationError(AuthFailureCode.IDENTITY_UNAVAILABLE) from None
        except SQLAlchemyError:
            await self._revoke(initial.session_id)
            raise AuthenticationError(AuthFailureCode.IDENTITY_UNAVAILABLE) from None
        except ValueError:
            await self._revoke(initial.session_id)
            raise AuthenticationError() from None
