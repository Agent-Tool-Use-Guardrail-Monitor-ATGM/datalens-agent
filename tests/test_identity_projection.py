"""Ordered identity effects and durable receipts on disposable PostgreSQL."""

import asyncio
import json
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from statistics_agent.contracts.common import AuthenticatedPrincipal
from statistics_agent.contracts.identity_events import IdentityEventError, decode_identity_event
from statistics_agent.contracts.users import UserProfileClaims
from statistics_agent.persistence.identity_projection import IdentityProjection
from statistics_agent.persistence.users import UserProfileStore
from support.database import apply, run, sandbox
from support.datasets import storage

ISSUER = "https://identity.example/realms/datalens"
OWNER = AuthenticatedPrincipal(issuer=ISSUER, subject="subject-1")


def event(kind, data, *, event_id=None):
    return json.dumps(
        {
            "schema_version": 1,
            "event_id": str(event_id or uuid4()),
            "event_type": kind,
            "issuer": OWNER.issuer,
            "subject": OWNER.subject,
            "occurred_at": "2026-11-11T12:00:00Z",
            "data": data,
        }
    ).encode()


def profile(revision, name, *, event_id=None):
    return event(
        "user.profile.updated",
        {"profile_revision": revision, "display_name": name, "username": None, "email": None},
        event_id=event_id,
    )


def test_strict_contract_rejects_wrong_issuer_duplicate_keys_and_unknown_fields():
    valid = profile(1, "Name")
    assert decode_identity_event(valid, ISSUER)["subject"] == OWNER.subject
    with pytest.raises(IdentityEventError):
        decode_identity_event(valid, "https://wrong.example/realms/datalens")
    for invalid in (
        valid.replace(b'"data":', b'"subject":"duplicate","data":'),
        valid.replace(b'"username"', b'"access_token"'),
        valid.replace(b'"profile_revision": 1', b'"profile_revision": 1.0'),
        b" " * 16_385,
    ):
        with pytest.raises(IdentityEventError):
            decode_identity_event(invalid, ISSUER)


@pytest.mark.postgres
def test_concurrent_projection_replay_and_revocation_order(migration_settings):
    async def exercise():
        async with storage(migration_settings) as (database, _):
            projection = IdentityProjection(database, ISSUER)
            profiles = UserProfileStore(database)
            unique = uuid4()
            latest = profile(3, "x" * 256, event_id=unique)
            outcomes = await asyncio.gather(
                projection.apply(latest),
                projection.apply(profile(2, "Old")),
                projection.apply(latest),
            )
            assert sorted(outcomes) == [False, True, True]
            assert (await profiles.get(OWNER)).display_name == "x" * 256
            assert (
                await profiles.upsert(
                    UserProfileClaims(issuer=ISSUER, subject=OWNER.subject, display_name="Claims")
                )
            ).display_name == "x" * 256
            with pytest.raises(IdentityEventError):
                await projection.apply(profile(3, "Conflicting retry", event_id=unique))

            await projection.apply(event("user.enabled", {"security_revision": 9}))
            await projection.apply(
                event("user.disabled", {"security_revision": 8, "tokens_issued_at_or_before": 200})
            )
            for revision, sid in ((12, "newer-session"), (10, "older-session")):
                await projection.apply(
                    event(
                        "user.session.revoked",
                        {"security_revision": revision, "session_id": sid, "reason": "logout"},
                    )
                )
            assert await projection.is_revoked(OWNER, issued_at=200, session_id=None)
            assert not await projection.is_revoked(OWNER, issued_at=201, session_id=None)
            for sid in ("newer-session", "older-session"):
                assert await projection.is_revoked(OWNER, issued_at=201, session_id=sid)
            await asyncio.gather(
                projection.apply(profile(4, "In flight")),
                projection.apply(
                    event(
                        "user.deleted", {"security_revision": 13, "tokens_issued_at_or_before": 300}
                    )
                ),
            )
            await projection.apply(profile(5, "Delayed"))
            await projection.apply(event("user.enabled", {"security_revision": 14}))
            assert await profiles.get(OWNER) is None
            assert (
                await profiles.upsert(
                    UserProfileClaims(issuer=ISSUER, subject=OWNER.subject, display_name="Claims")
                )
                is None
            )
            assert await projection.is_revoked(OWNER, issued_at=301, session_id=None)

    run(exercise())


@pytest.mark.postgres
def test_failed_projection_rolls_back_receipt(migration_settings):
    async def exercise():
        async with storage(migration_settings) as (database, _):
            projection = IdentityProjection(database, ISSUER)
            delivery = profile(1, "Name")
            async with database.session() as session, session.begin():
                await session.execute(
                    text("""
                    ALTER TABLE agent_user_profiles ADD CONSTRAINT projection_test_failure
                    CHECK (false) NOT VALID
                """)
                )
            with pytest.raises(DBAPIError):
                await projection.apply(delivery)
            async with database.session() as session, session.begin():
                assert await session.scalar(text("SELECT count(*) FROM agent_identity_inbox")) == 0
                assert await session.scalar(text("SELECT count(*) FROM agent_identity_state")) == 0
                await session.execute(
                    text("""
                    ALTER TABLE agent_user_profiles DROP CONSTRAINT projection_test_failure
                """)
                )
            assert await projection.apply(delivery)
            assert not await projection.apply(delivery)

    run(exercise())


@pytest.mark.postgres
def test_serialization_retry_replays_whole_transaction(migration_settings):
    async def exercise():
        async with storage(migration_settings) as (database, _):
            async with database.session() as session, session.begin():
                await session.execute(text("CREATE SEQUENCE projection_test_attempt"))
                await session.execute(
                    text("""
                    CREATE FUNCTION projection_test_retry() RETURNS trigger
                    LANGUAGE plpgsql AS $$ BEGIN
                        IF nextval('projection_test_attempt') <= 2 THEN
                            RAISE EXCEPTION 'Controlled serialization failure'
                                USING ERRCODE = '40001';
                        END IF;
                        RETURN NEW;
                    END $$;
                """)
                )
                await session.execute(
                    text("""
                    CREATE TRIGGER projection_test_retry BEFORE INSERT ON agent_user_profiles
                    FOR EACH ROW EXECUTE FUNCTION projection_test_retry()
                """)
                )
            projection = IdentityProjection(database, ISSUER)
            assert await projection.apply(profile(1, "Retried"))
            async with database.session() as session:
                assert (
                    await session.scalar(text("SELECT last_value FROM projection_test_attempt"))
                    == 3
                )
                assert await session.scalar(text("SELECT count(*) FROM agent_identity_inbox")) == 1

    run(exercise())


@pytest.mark.postgres
def test_projection_migration_preserves_preexisting_display_rows(migration_settings):
    async def exercise():
        async with sandbox(migration_settings) as (database, schema, config):
            await apply(database, schema, config, "upgrade", "0009_user_profiles")
            async with database.engine.begin() as connection:
                await connection.execute(
                    text(f"""
                    INSERT INTO {schema}.agent_user_profiles
                        (issuer, subject, username, profile_updated_at)
                    VALUES (:issuer, :subject, 'Existing', now())
                """),
                    {"issuer": ISSUER, "subject": OWNER.subject},
                )
                old = await connection.scalar(
                    text(f"SELECT to_jsonb(profile) FROM {schema}.agent_user_profiles profile")
                )
            for action, revision in (("upgrade", "head"), ("downgrade", "0009_user_profiles")):
                await apply(database, schema, config, action, revision)
                async with database.engine.connect() as connection:
                    current = await connection.scalar(
                        text(f"SELECT to_jsonb(profile) FROM {schema}.agent_user_profiles profile")
                    )
                    assert current == old

    run(exercise())
