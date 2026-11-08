"""Display projection keyed by verified identity; never an authorization source."""

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from statistics_agent.contracts.common import AuthenticatedPrincipal
from statistics_agent.contracts.users import UserProfile, UserProfileClaims
from statistics_agent.persistence.models import UserProfileRecord
from statistics_agent.persistence.ownership import owner_filter


class UserProfileStore:
    def __init__(self, database):
        self.database = database

    async def upsert(self, claims: UserProfileClaims) -> UserProfile:
        """Caller has verified claims; replace missing display fields with NULL."""
        claims = UserProfileClaims.model_validate(claims)
        fields = claims.model_dump()
        statement = insert(UserProfileRecord).values(
            **fields, profile_updated_at=func.clock_timestamp()
        )
        statement = statement.on_conflict_do_update(
            index_elements=[UserProfileRecord.issuer, UserProfileRecord.subject],
            set_={
                name: getattr(statement.excluded, name)
                for name in ("display_name", "username", "email", "profile_updated_at")
            },
        ).returning(UserProfileRecord)
        async with self.database.session() as session:
            record = (await session.scalars(statement)).one()
            profile = UserProfile.model_validate(record, from_attributes=True)
            await session.commit()
            return profile

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
