"""Display projection validation, identity isolation and retry-safe persistence."""

import asyncio

import pytest
from pydantic import ValidationError
from sqlalchemy import func, inspect, select

from statistics_agent.contracts.common import AuthenticatedPrincipal
from statistics_agent.contracts.users import UserProfileClaims
from statistics_agent.persistence.models import UserProfileRecord
from statistics_agent.persistence.users import UserProfileStore
from support.database import run
from support.datasets import storage


def claims(**updates):
    return UserProfileClaims.model_validate(
        {"issuer": "https://identity.example/realms/datalens", "subject": "person-1"} | updates
    )


@pytest.mark.parametrize(
    "updates",
    [
        {"display_name": " "},
        {"username": "name\nother"},
        {"email": "\x00"},
        {"display_name": "x" * 256},
        {"username": "x" * 256},
        {"email": "x" * 321},
        {"display_name": 123},
        {"roles": ["admin"]},
        {"access_token": "private"},
        {"refresh_token": "private"},
        {"logged_in": True},
        {"password": "private"},
    ],
)
def test_profile_rejects_invalid_text_and_authentication_fields(updates):
    with pytest.raises(ValidationError):
        claims(**updates)


def test_profile_supports_missing_fields_without_inventing_identity_data():
    value = claims()
    assert value.display_name is value.username is value.email is None
    assert claims(display_name="Maxim", email="person@example.test").email == "person@example.test"


@pytest.mark.postgres
def test_profile_upsert_is_retry_safe_updates_display_data_and_clears_missing_claims(
    migration_settings,
):
    async def exercise():
        async with storage(migration_settings) as (database, _):
            store = UserProfileStore(database)
            identity = AuthenticatedPrincipal(**claims().model_dump(include={"issuer", "subject"}))
            assert await store.get(identity) is None
            first = await store.upsert(claims(display_name="Old", email="old@example.test"))
            assert first.profile_updated_at.utcoffset().total_seconds() == 0
            updated = await store.upsert(claims(username="new-name"))
            assert updated.display_name is updated.email is None
            assert updated.username == "new-name"
            assert updated.profile_updated_at >= first.profile_updated_at
            assert await store.get(identity) == updated
            async with database.session() as session:
                assert (
                    await session.scalar(select(func.count()).select_from(UserProfileRecord)) == 1
                )

    run(exercise())


@pytest.mark.postgres
def test_profiles_are_isolated_by_issuer_and_subject_not_email_or_username(migration_settings):
    async def exercise():
        async with storage(migration_settings) as (database, _):
            store = UserProfileStore(database)
            owners = [
                claims(email="shared@example.test", username="same"),
                claims(subject="person-2", email="shared@example.test", username="same"),
                claims(issuer="https://other.example/realms/datalens", email="shared@example.test"),
            ]
            for owner in owners:
                await store.upsert(owner)
            for owner in owners:
                identity = AuthenticatedPrincipal(**owner.model_dump(include={"issuer", "subject"}))
                profile = await store.get(identity)
                assert (profile.issuer, profile.subject) == (owner.issuer, owner.subject)
            assert (
                await store.get(AuthenticatedPrincipal(issuer=owners[0].issuer, subject="unknown"))
                is None
            )
            async with database.session() as session:
                assert (
                    await session.scalar(select(func.count()).select_from(UserProfileRecord)) == 3
                )

    run(exercise())


@pytest.mark.postgres
def test_concurrent_profile_requests_share_one_row_without_authentication_state(migration_settings):
    async def exercise():
        async with storage(migration_settings) as (database, _):
            store = UserProfileStore(database)
            await asyncio.gather(*(store.upsert(claims(display_name=str(i))) for i in range(5)))
            async with database.session() as session:
                assert (
                    await session.scalar(select(func.count()).select_from(UserProfileRecord)) == 1
                )
                record = await session.scalar(select(UserProfileRecord))
                assert record.display_name in {str(i) for i in range(5)}
                names = await session.run_sync(
                    lambda current: inspect(current.connection()).get_columns("agent_user_profiles")
                )
                assert {item["name"] for item in names} == {
                    "issuer",
                    "subject",
                    "display_name",
                    "username",
                    "email",
                    "profile_updated_at",
                }

    run(exercise())
