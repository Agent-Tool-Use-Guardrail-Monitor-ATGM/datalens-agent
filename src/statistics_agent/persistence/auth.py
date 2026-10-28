"""Short, explicit DB transactions for browser state; no identity network calls."""

import hashlib
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from pydantic import SecretStr
from sqlalchemy import delete, or_, select, update
from sqlalchemy.exc import IntegrityError

from statistics_agent.contracts.common import AuthenticatedPrincipal
from statistics_agent.core.auth import AuthSettings
from statistics_agent.core.token_encryption import SecretContext, TokenEncryption
from statistics_agent.persistence.database import Database
from statistics_agent.persistence.models import BrowserSession, LoginAttempt


class PendingLoginError(ValueError):
    """The browser must complete or wait for its pending login to expire."""


def opaque_secret() -> SecretStr:
    return SecretStr(secrets.token_urlsafe(32))


def credential_hash(value: SecretStr) -> str:
    raw = value.get_secret_value()
    if not 32 <= len(raw) <= 512 or not raw.isascii():
        raise ValueError("Invalid opaque credential")
    return hashlib.sha256(raw.encode()).hexdigest()


def instant(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("An aware timestamp is required")
    return value.astimezone(UTC)


@dataclass(frozen=True)
class LoginStart:
    attempt_id: UUID
    expires_at: datetime
    state: SecretStr = field(repr=False)
    browser_binding: SecretStr = field(repr=False)
    nonce: SecretStr = field(repr=False)
    verifier: SecretStr = field(repr=False)


@dataclass(frozen=True)
class ConsumedLogin:
    attempt_id: UUID
    issuer: str
    return_target: str
    nonce: SecretStr = field(repr=False)
    verifier: SecretStr = field(repr=False)


@dataclass(frozen=True)
class Credentials:
    """Already-verified provider credentials; storage does not authenticate JWTs."""

    access_token: SecretStr = field(repr=False)
    refresh_token: SecretStr = field(repr=False)
    id_token: SecretStr | None = field(default=None, repr=False)


@dataclass(frozen=True)
class StoredSession:
    session_id: UUID
    issuer: str
    subject: str
    idle_expires_at: datetime
    absolute_expires_at: datetime
    refresh_version: int
    credentials: Credentials = field(repr=False)


@dataclass(frozen=True)
class NewSession:
    session: StoredSession
    cookie: SecretStr = field(repr=False)


@dataclass(frozen=True)
class RefreshClaim:
    session: StoredSession = field(repr=False)
    claim_id: UUID | None
    waiting: bool


@dataclass(frozen=True)
class LogoutSession:
    session_id: UUID
    issuer: str
    subject: str
    credentials: Credentials | None = field(repr=False)


class AuthStore:
    """Each operation owns its commit. Returned records are not authorization decisions.

    Pass a trusted UTC clock only from server code. Production callers omit now;
    explicit instants support deterministic boundary tests. Never accept client time.
    """

    def __init__(self, database: Database, settings: AuthSettings, encryption: TokenEncryption):
        self._database = database
        self._settings = settings
        self._encryption = encryption

    def _context(self, row, kind: str) -> SecretContext:
        return SecretContext(row.id, row.issuer, getattr(row, "subject", ""), kind)

    def _credentials(self, row: BrowserSession, credentials: Credentials) -> dict:
        return {
            "access_ciphertext": self._encryption.seal(
                credentials.access_token, self._context(row, "access")
            ),
            "refresh_ciphertext": self._encryption.seal(
                credentials.refresh_token, self._context(row, "refresh")
            ),
            "id_ciphertext": self._encryption.seal(credentials.id_token, self._context(row, "id"))
            if credentials.id_token is not None
            else None,
        }

    def _snapshot(self, row: BrowserSession) -> StoredSession:
        return StoredSession(
            row.id,
            row.issuer,
            row.subject,
            row.idle_expires_at,
            row.absolute_expires_at,
            row.refresh_version,
            Credentials(
                self._encryption.open(row.access_ciphertext, self._context(row, "access")),
                self._encryption.open(row.refresh_ciphertext, self._context(row, "refresh")),
                self._encryption.open(row.id_ciphertext, self._context(row, "id"))
                if row.id_ciphertext is not None
                else None,
            ),
        )

    async def start_login(
        self, browser_binding: SecretStr | None = None, *, now: datetime | None = None
    ) -> LoginStart:
        current = instant(now)
        browser = opaque_secret() if browser_binding is None else browser_binding
        binding_hash = credential_hash(browser)
        state, nonce, verifier = opaque_secret(), opaque_secret(), opaque_secret()
        row = LoginAttempt(
            id=uuid4(),
            state_hash=credential_hash(state),
            browser_hash=binding_hash,
            issuer=self._settings.issuer,
            return_target="/",
            created_at=current,
            expires_at=current + timedelta(seconds=self._settings.login_attempt_seconds),
        )
        row.nonce_ciphertext = self._encryption.seal(nonce, self._context(row, "nonce"))
        row.verifier_ciphertext = self._encryption.seal(verifier, self._context(row, "pkce"))
        conflict = False
        try:
            async with self._database.session() as session:
                # Retire only an expired attempt; never overwrite a live browser binding.
                await session.execute(
                    update(LoginAttempt)
                    .where(
                        LoginAttempt.browser_hash == binding_hash,
                        LoginAttempt.consumed_at.is_(None),
                        LoginAttempt.expires_at <= current,
                    )
                    .values(consumed_at=current)
                )
                session.add(row)
                await session.commit()
        except IntegrityError:
            conflict = True
        if conflict:
            raise PendingLoginError("A login is already pending for this browser")
        return LoginStart(row.id, row.expires_at, state, browser, nonce, verifier)

    async def consume_login(
        self, state: SecretStr, browser_binding: SecretStr, *, now: datetime | None = None
    ) -> ConsumedLogin | None:
        current = instant(now)
        async with self._database.session() as session:
            row = (
                await session.execute(
                    update(LoginAttempt)
                    .where(
                        LoginAttempt.state_hash == credential_hash(state),
                        LoginAttempt.browser_hash == credential_hash(browser_binding),
                        LoginAttempt.issuer == self._settings.issuer,
                        LoginAttempt.consumed_at.is_(None),
                        LoginAttempt.expires_at > current,
                        LoginAttempt.created_at <= current,
                    )
                    .values(consumed_at=current)
                    .returning(LoginAttempt)
                )
            ).scalar_one_or_none()
            await session.commit()
        if row is None:
            return None
        # Commit consumption before decrypting or performing a future token exchange.
        # A corrupt credential or a failed callback cannot replay the attempt.
        return ConsumedLogin(
            row.id,
            row.issuer,
            row.return_target,
            self._encryption.open(row.nonce_ciphertext, self._context(row, "nonce")),
            self._encryption.open(row.verifier_ciphertext, self._context(row, "pkce")),
        )

    async def create_session(
        self,
        principal: AuthenticatedPrincipal,
        credentials: Credentials,
        *,
        now: datetime | None = None,
    ) -> NewSession:
        if principal.issuer != self._settings.issuer:
            raise ValueError("Unexpected session issuer")
        current = instant(now)
        cookie = opaque_secret()
        row = BrowserSession(
            id=uuid4(),
            cookie_hash=credential_hash(cookie),
            issuer=principal.issuer,
            subject=principal.subject,
            created_at=current,
            last_seen_at=current,
            idle_expires_at=current + timedelta(seconds=self._settings.session_idle_seconds),
            absolute_expires_at=current + timedelta(seconds=self._settings.session_max_seconds),
            refresh_version=0,
        )
        for name, value in self._credentials(row, credentials).items():
            setattr(row, name, value)
        async with self._database.session() as session:
            session.add(row)
            await session.commit()
        return NewSession(self._snapshot(row), cookie)

    def _active(self, current: datetime) -> tuple:
        return (
            BrowserSession.issuer == self._settings.issuer,
            BrowserSession.revoked_at.is_(None),
            BrowserSession.created_at <= current,
            BrowserSession.idle_expires_at > current,
            BrowserSession.absolute_expires_at > current,
        )

    async def read_session(
        self, cookie: SecretStr, *, now: datetime | None = None
    ) -> StoredSession | None:
        current = instant(now)
        async with self._database.session() as session:
            row = (
                await session.execute(
                    select(BrowserSession).where(
                        BrowserSession.cookie_hash == credential_hash(cookie),
                        *self._active(current),
                    )
                )
            ).scalar_one_or_none()
        return None if row is None else self._snapshot(row)

    async def touch_session(self, session_id: UUID, *, now: datetime | None = None) -> bool:
        """Only call after authorization; an arbitrary cookie cannot extend idle time."""
        from sqlalchemy import func

        current = instant(now)
        async with self._database.session() as session:
            result = await session.execute(
                update(BrowserSession)
                .where(
                    BrowserSession.id == session_id,
                    *self._active(current),
                    BrowserSession.last_seen_at <= current,
                )
                .values(
                    last_seen_at=current,
                    idle_expires_at=func.least(
                        BrowserSession.absolute_expires_at,
                        current + timedelta(seconds=self._settings.session_idle_seconds),
                    ),
                )
            )
            await session.commit()
            return result.rowcount == 1

    async def replace_credentials(
        self,
        session_id: UUID,
        expected_version: int,
        credentials: Credentials,
        *,
        now: datetime | None = None,
    ) -> bool:
        """Compare-and-swap persistence only; not a distributed refresh coordinator."""
        current = instant(now)
        async with self._database.session() as session:
            row = (
                await session.execute(
                    select(BrowserSession).where(
                        BrowserSession.id == session_id,
                        *self._active(current),
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                return False
            values = self._credentials(row, credentials)
            result = await session.execute(
                update(BrowserSession)
                .where(
                    BrowserSession.id == session_id,
                    *self._active(current),
                    BrowserSession.refresh_version == expected_version,
                    BrowserSession.refresh_claim_id.is_(None),
                )
                .values(**values, refresh_version=BrowserSession.refresh_version + 1)
            )
            await session.commit()
            return result.rowcount == 1

    async def claim_refresh(self, session_id: UUID, expected_version: int, lease_seconds: float):
        """Commit a durable claim before HTTP; row locks never span remote calls."""
        from sqlalchemy import func

        async with self._database.session() as session:
            database_now = await session.scalar(select(func.clock_timestamp()))
            current = instant(None)
            row = (
                await session.execute(
                    select(BrowserSession)
                    .where(
                        BrowserSession.id == session_id,
                        *self._active(current),
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            if row.refresh_claim_id is not None:
                if row.refresh_started_at + timedelta(seconds=lease_seconds) <= database_now:
                    row.revoked_at = current
                    await session.commit()
                    return None
                return RefreshClaim(self._snapshot(row), None, True)
            if row.refresh_version != expected_version:
                return RefreshClaim(self._snapshot(row), None, False)
            claim_id = uuid4()
            row.refresh_claim_id = claim_id
            row.refresh_started_at = database_now
            snapshot = self._snapshot(row)
            await session.commit()
            return RefreshClaim(snapshot, claim_id, False)

    async def finish_refresh(
        self, session_id: UUID, claim_id: UUID, version: int, credentials: Credentials
    ):
        current = instant(None)
        async with self._database.session() as session:
            row = (
                await session.execute(
                    select(BrowserSession)
                    .where(
                        BrowserSession.id == session_id,
                        *self._active(current),
                        BrowserSession.refresh_claim_id == claim_id,
                        BrowserSession.refresh_version == version,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if row is None:
                return False
            for name, value in self._credentials(row, credentials).items():
                setattr(row, name, value)
            row.refresh_version += 1
            row.refresh_claim_id = None
            row.refresh_started_at = None
            await session.commit()
            return True

    async def read_logout_session(self, cookie: SecretStr):
        """Local logout must work despite expired JWTs or unreadable token ciphertext."""
        async with self._database.session() as session:
            row = (
                await session.execute(
                    select(BrowserSession).where(
                        BrowserSession.cookie_hash == credential_hash(cookie),
                        BrowserSession.issuer == self._settings.issuer,
                    )
                )
            ).scalar_one_or_none()
        if row is None:
            return None
        try:
            credentials = self._snapshot(row).credentials
        except ValueError:
            credentials = None
        return LogoutSession(row.id, row.issuer, row.subject, credentials)

    async def revoke_session(self, session_id: UUID, *, now: datetime | None = None) -> bool:
        current = instant(now)
        async with self._database.session() as session:
            result = await session.execute(
                update(BrowserSession)
                .where(
                    BrowserSession.id == session_id,
                    BrowserSession.issuer == self._settings.issuer,
                    BrowserSession.revoked_at.is_(None),
                )
                .values(revoked_at=current)
            )
            await session.commit()
            return result.rowcount == 1

    async def cleanup(self, *, limit: int = 100, now: datetime | None = None) -> int:
        """Delete at most limit expired/consumed/revoked rows across both tables."""
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("Cleanup limit must be between 1 and 1000")
        current = instant(now)
        removed = 0
        async with self._database.session() as session:
            for model, predicate in (
                (
                    LoginAttempt,
                    or_(LoginAttempt.expires_at <= current, LoginAttempt.consumed_at.is_not(None)),
                ),
                (
                    BrowserSession,
                    or_(
                        BrowserSession.idle_expires_at <= current,
                        BrowserSession.absolute_expires_at <= current,
                        BrowserSession.revoked_at.is_not(None),
                    ),
                ),
            ):
                ids = list(
                    (
                        await session.scalars(
                            select(model.id)
                            .where(predicate)
                            .order_by(model.id)
                            .limit(limit - removed)
                            .with_for_update(skip_locked=True)
                        )
                    ).all()
                )
                if ids:
                    await session.execute(delete(model).where(model.id.in_(ids)))
                    removed += len(ids)
                if removed == limit:
                    break
            await session.commit()
        return removed
