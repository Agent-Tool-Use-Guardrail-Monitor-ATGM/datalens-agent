"""Session security and concurrency on migrated, disposable PostgreSQL schemas."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.exc import IntegrityError

from statistics_agent.core.token_encryption import SecretEnvelopeError, TokenEncryption
from statistics_agent.persistence.auth import AuthStore, PendingLoginError, opaque_secret
from statistics_agent.persistence.models import BrowserSession, LoginAttempt
from support.auth import auth_settings, credentials, principal, storage
from support.database import run

pytestmark = pytest.mark.postgres
NOW = datetime(2026, 10, 27, 12, tzinfo=UTC)


def test_login_is_browser_bound_one_use_and_committed_before_decryption(auth_storage_settings):
    async def exercise():
        async with storage(auth_storage_settings) as (database, store, ring):
            attempt = await store.start_login(now=NOW)
            assert await store.consume_login(attempt.state, opaque_secret(), now=NOW) is None
            assert (
                await store.consume_login(opaque_secret(), attempt.browser_binding, now=NOW) is None
            )
            results = await asyncio.gather(
                *(
                    store.consume_login(attempt.state, attempt.browser_binding, now=NOW)
                    for _ in range(2)
                )
            )
            assert sum(result is not None for result in results) == 1
            consumed = next(result for result in results if result is not None)
            assert consumed.nonce == attempt.nonce and consumed.verifier == attempt.verifier
            assert consumed.return_target == "/"
            second = await store.start_login(attempt.browser_binding, now=NOW)
            async with database.session() as session:
                await session.execute(
                    update(LoginAttempt)
                    .where(LoginAttempt.id == second.attempt_id)
                    .values(nonce_ciphertext="corrupt")
                )
                await session.commit()
            with pytest.raises(SecretEnvelopeError):
                await store.consume_login(second.state, second.browser_binding, now=NOW)
            assert await store.consume_login(second.state, second.browser_binding, now=NOW) is None

    run(exercise())


def test_pending_browser_login_is_unique_under_concurrency_and_expires_at_boundary(
    auth_storage_settings,
):
    async def exercise():
        async with storage(auth_storage_settings) as (_, store, _ring):
            binding = opaque_secret()
            results = await asyncio.gather(
                *(store.start_login(binding, now=NOW) for _ in range(2)), return_exceptions=True
            )
            assert sum(isinstance(result, PendingLoginError) for result in results) == 1
            attempt = next(result for result in results if not isinstance(result, Exception))
            assert (
                await store.consume_login(attempt.state, binding, now=NOW + timedelta(seconds=300))
                is None
            )
            replacement = await store.start_login(binding, now=NOW + timedelta(seconds=300))
            assert replacement.attempt_id != attempt.attempt_id
            assert await store.consume_login(
                replacement.state, binding, now=NOW + timedelta(seconds=300)
            )

    run(exercise())


def test_session_roundtrip_restart_no_plaintext_and_no_positive_authorization_cache(
    auth_storage_settings,
):
    async def exercise():
        async with storage(auth_storage_settings) as (database, store, ring):
            created = await store.create_session(principal(), credentials(), now=NOW)
            assert await store.read_session(opaque_secret(), now=NOW) is None
            restarted = AuthStore(database, auth_settings(), TokenEncryption(ring))
            found = await restarted.read_session(created.cookie, now=NOW)
            assert found.credentials == credentials()
            assert found.issuer == principal().issuer and found.subject == "user-one"
            assert found.refresh_version == 0
            async with database.session() as session:
                rows = (
                    (
                        await session.execute(
                            text("SELECT row_to_json(s)::text FROM auth_sessions s")
                        )
                    )
                    .scalars()
                    .all()
                )
            evidence = "".join(rows) + repr(created) + repr(found) + repr(credentials())
            for value in (
                created.cookie,
                credentials().access_token,
                credentials().refresh_token,
                credentials().id_token,
            ):
                assert value.get_secret_value() not in evidence
            assert "roles" not in BrowserSession.__table__.columns
            assert "email_verified" not in BrowserSession.__table__.columns
            assert not hasattr(created.session, "cookie")
            attempt = await store.start_login(now=NOW)
            async with database.session() as session:
                raw = await session.scalar(
                    text("SELECT row_to_json(a)::text FROM auth_login_attempts a")
                )
            for secret in (attempt.state, attempt.browser_binding, attempt.nonce, attempt.verifier):
                assert secret.get_secret_value() not in raw + repr(attempt)

    run(exercise())


def test_idle_absolute_deadlines_and_revocation_cannot_be_extended(auth_storage_settings):
    async def exercise():
        async with storage(auth_storage_settings) as (_, store, _ring):
            created = await store.create_session(principal(), credentials(), now=NOW)
            assert await store.read_session(created.cookie, now=NOW - timedelta(seconds=1)) is None
            assert await store.touch_session(
                created.session.session_id, now=NOW + timedelta(seconds=1799)
            )
            assert await store.read_session(created.cookie, now=NOW + timedelta(seconds=1800))
            assert (
                await store.read_session(created.cookie, now=NOW + timedelta(seconds=3599)) is None
            )
            assert not await store.touch_session(
                created.session.session_id, now=NOW + timedelta(seconds=3599)
            )
            long_lived = await store.create_session(principal(), credentials(), now=NOW)
            for seconds in range(1700, 28800, 1700):
                assert await store.touch_session(
                    long_lived.session.session_id, now=NOW + timedelta(seconds=seconds)
                )
            final = await store.read_session(long_lived.cookie, now=NOW + timedelta(seconds=28799))
            assert final.idle_expires_at == final.absolute_expires_at
            assert (
                await store.read_session(long_lived.cookie, now=NOW + timedelta(seconds=28800))
                is None
            )
            assert not await store.touch_session(
                long_lived.session.session_id, now=NOW + timedelta(seconds=28800)
            )
            revoked = await store.create_session(principal(), credentials(), now=NOW)
            assert await store.revoke_session(revoked.session.session_id, now=NOW)
            assert not await store.revoke_session(revoked.session.session_id, now=NOW)
            assert await store.read_session(revoked.cookie, now=NOW) is None
            assert not await store.replace_credentials(
                revoked.session.session_id, 0, credentials("new"), now=NOW
            )

    run(exercise())


def test_concurrent_credential_updates_have_one_winner_and_preserve_cookie(auth_storage_settings):
    async def exercise():
        async with storage(auth_storage_settings) as (_, store, _ring):
            created = await store.create_session(principal(), credentials(), now=NOW)
            results = await asyncio.gather(
                *(
                    store.replace_credentials(
                        created.session.session_id, 0, credentials(str(i)), now=NOW
                    )
                    for i in range(2)
                )
            )
            assert sorted(results) == [False, True]
            found = await store.read_session(created.cookie, now=NOW)
            assert found.refresh_version == 1
            assert found.credentials in (credentials("0"), credentials("1"))
            assert not await store.replace_credentials(
                created.session.session_id, 0, credentials("stale"), now=NOW
            )

    run(exercise())


def test_cross_session_ciphertext_substitution_fails_without_resetting_data(auth_storage_settings):
    async def exercise():
        async with storage(auth_storage_settings) as (database, store, _ring):
            first = await store.create_session(principal(), credentials(), now=NOW)
            second = await store.create_session(principal("user-two"), credentials("two"), now=NOW)
            async with database.session() as session:
                ciphertext = await session.scalar(
                    select(BrowserSession.access_ciphertext).where(
                        BrowserSession.id == first.session.session_id
                    )
                )
                await session.execute(
                    update(BrowserSession)
                    .where(BrowserSession.id == second.session.session_id)
                    .values(access_ciphertext=ciphertext)
                )
                await session.commit()
            with pytest.raises(SecretEnvelopeError):
                await store.read_session(second.cookie, now=NOW)
            assert (await store.read_session(first.cookie, now=NOW)).credentials == credentials()

    run(exercise())


def test_cleanup_is_bounded_and_preserves_live_rows(auth_storage_settings):
    async def exercise():
        async with storage(auth_storage_settings) as (database, store, _ring):
            for _ in range(3):
                await store.start_login(now=NOW - timedelta(seconds=301))
            expired = await store.create_session(
                principal(), credentials(), now=NOW - timedelta(seconds=1800)
            )
            live = await store.create_session(principal(), credentials(), now=NOW)
            await store.start_login(now=NOW)
            assert await store.cleanup(limit=2, now=NOW) == 2
            assert await store.cleanup(limit=2, now=NOW) == 2
            assert await store.cleanup(limit=2, now=NOW) == 0
            assert await store.read_session(expired.cookie, now=NOW) is None
            assert await store.read_session(live.cookie, now=NOW)
            async with database.session() as session:
                assert len((await session.scalars(select(LoginAttempt))).all()) == 1
            with pytest.raises(ValueError):
                await store.cleanup(limit=1001, now=NOW)

    run(exercise())


def test_database_enforces_cookie_uniqueness_and_deadlines(auth_storage_settings):
    async def exercise():
        async with storage(auth_storage_settings) as (database, store, _ring):
            first = await store.create_session(principal(), credentials(), now=NOW)
            second = await store.create_session(principal(), credentials(), now=NOW)
            async with database.session() as session:
                first_hash = await session.scalar(
                    select(BrowserSession.cookie_hash).where(
                        BrowserSession.id == first.session.session_id
                    )
                )
            for changes in (
                {"cookie_hash": first_hash},
                {"idle_expires_at": NOW},
                {"refresh_version": -1},
            ):
                with pytest.raises(IntegrityError):
                    async with database.session() as session:
                        await session.execute(
                            update(BrowserSession)
                            .where(BrowserSession.id == second.session.session_id)
                            .values(**changes)
                        )
                        await session.commit()
            assert await store.read_session(second.cookie, now=NOW)

    run(exercise())
