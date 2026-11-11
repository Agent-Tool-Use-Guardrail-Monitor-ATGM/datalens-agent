"""Display projection keyed by verified identity; never an authorization source."""

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert

from statistics_agent.contracts.common import AuthenticatedPrincipal
from statistics_agent.contracts.users import UserProfile, UserProfileClaims
from statistics_agent.persistence.models import UserProfileRecord
from statistics_agent.persistence.ownership import owner_filter


async def lock_identity(session, key):
    await session.execute(
        text("""
            INSERT INTO agent_identity_state (issuer, subject) VALUES (:issuer, :subject)
            ON CONFLICT (issuer, subject) DO NOTHING
        """),
        key,
    )
    result = await session.execute(
        text("""
            SELECT profile_revision, status_revision, deleted FROM agent_identity_state
            WHERE issuer = :issuer AND subject = :subject FOR UPDATE
        """),
        key,
    )
    return result.one()


async def write_profile(session, fields, timestamp):
    statement = insert(UserProfileRecord).values(**fields, profile_updated_at=timestamp)
    statement = statement.on_conflict_do_update(
        index_elements=[UserProfileRecord.issuer, UserProfileRecord.subject],
        set_={
            name: getattr(statement.excluded, name)
            for name in ("display_name", "username", "email", "profile_updated_at")
        },
    ).returning(UserProfileRecord)
    return (await session.scalars(statement)).one()


class UserProfileStore:
    def __init__(self, database):
        self.database = database

    async def upsert(self, claims: UserProfileClaims) -> UserProfile | None:
        """Write verified claims only before the event stream takes ownership."""
        claims = UserProfileClaims.model_validate(claims)
        fields = claims.model_dump()
        async with self.database.session() as session:
            state = await lock_identity(
                session, {"issuer": claims.issuer, "subject": claims.subject}
            )
            if state.deleted:
                return None
            if state.profile_revision:
                record = await session.get(UserProfileRecord, (claims.issuer, claims.subject))
            else:
                record = await write_profile(session, fields, func.clock_timestamp())
            await session.commit()
            return (
                None if record is None else UserProfile.model_validate(record, from_attributes=True)
            )

    async def get(self, principal: AuthenticatedPrincipal) -> UserProfile | None:
        """Read only the caller-supplied trusted identity, never by email/name."""
        async with self.database.session() as session:
            record = await session.scalar(
                select(UserProfileRecord).where(
                    owner_filter(UserProfileRecord.issuer, UserProfileRecord.subject, principal)
                )
            )
            return (
                None if record is None else UserProfile.model_validate(record, from_attributes=True)
            )
