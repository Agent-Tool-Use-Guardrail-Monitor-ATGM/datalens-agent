"""Apply an identity delivery atomically before the caller acknowledges it."""

import asyncio
import hashlib
import random
from datetime import datetime
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from statistics_agent.contracts.identity_events import IdentityEventError, decode_identity_event
from statistics_agent.persistence.users import lock_identity, write_profile


def display_value(value):
    return value if value and value.strip() and value.isprintable() else None


class IdentityProjection:
    def __init__(self, database, expected_issuer):
        self.database = database
        self.expected_issuer = expected_issuer

    async def apply(self, body: bytes, *, delivery: tuple[str | None, str] | None = None) -> bool:
        """Return true for a new committed receipt and false for a replay."""
        event = decode_identity_event(body, self.expected_issuer)
        if delivery is not None and delivery != (event["event_id"], event["event_type"]):
            raise IdentityEventError("RabbitMQ message metadata does not match the event")
        digest = hashlib.sha256(body).digest()
        for attempt in range(3):
            try:
                return await self._apply(event, digest)
            except DBAPIError as error:
                if getattr(error.orig, "sqlstate", None) not in {"40P01", "40001"} or attempt == 2:
                    raise
                await asyncio.sleep(random.uniform(0.02, 0.08) * (attempt + 1))

    async def _apply(self, event, digest):
        event_id = UUID(event["event_id"])
        key = {"issuer": event["issuer"], "subject": event["subject"]}
        async with self.database.session() as session, session.begin():
            await session.execute(text("SET LOCAL lock_timeout = '2s'"))
            receipt = await session.scalar(
                text("""
                    INSERT INTO agent_identity_inbox (event_id, payload_digest)
                    VALUES (:event_id, :digest) ON CONFLICT DO NOTHING RETURNING event_id
                """),
                {"event_id": event_id, "digest": digest},
            )
            if receipt is None:
                previous = await session.scalar(
                    text("SELECT payload_digest FROM agent_identity_inbox WHERE event_id = :id"),
                    {"id": event_id},
                )
                if previous != digest:
                    raise IdentityEventError("Identity event ID reused with different content")
                return False

            state = await lock_identity(session, key)
            data = event["data"]
            kind = event["event_type"]
            if kind in {"user.created", "user.profile.updated"}:
                if not state.deleted and data["profile_revision"] > state.profile_revision:
                    fields = {
                        name: display_value(data[name])
                        for name in ("display_name", "username", "email")
                    }
                    await write_profile(
                        session, key | fields, datetime.fromisoformat(event["occurred_at"])
                    )
                    await session.execute(
                        text("""
                            UPDATE agent_identity_state SET profile_revision = :revision
                            WHERE issuer = :issuer AND subject = :subject
                        """),
                        key | {"revision": data["profile_revision"]},
                    )
            elif kind == "user.session.revoked":
                await session.execute(
                    text("""
                        INSERT INTO agent_identity_revoked_sessions (issuer, subject, session_id)
                        VALUES (:issuer, :subject, :sid) ON CONFLICT DO NOTHING
                    """),
                    key | {"sid": data["session_id"]},
                )
            else:
                await session.execute(
                    text("""
                        UPDATE agent_identity_state SET
                            token_cutoff = greatest(token_cutoff, :cutoff),
                            disabled = CASE WHEN :status AND :revision > status_revision
                                THEN :disabled ELSE disabled END,
                            status_revision = CASE WHEN :status
                                THEN greatest(status_revision, :revision) ELSE status_revision END,
                            deleted = deleted OR :deleted
                        WHERE issuer = :issuer AND subject = :subject
                    """),
                    key
                    | {
                        "cutoff": data.get("tokens_issued_at_or_before"),
                        "revision": data["security_revision"],
                        "status": kind in {"user.enabled", "user.disabled"},
                        "disabled": kind == "user.disabled",
                        "deleted": kind == "user.deleted",
                    },
                )
                if kind == "user.deleted":
                    await session.execute(
                        text("""
                            DELETE FROM agent_user_profiles
                            WHERE issuer = :issuer AND subject = :subject
                        """),
                        key,
                    )
            return True

    async def is_revoked(self, principal, *, issued_at: int, session_id: str | None) -> bool:
        """Check indexed local state for an already verified access token."""
        async with self.database.session() as session:
            return bool(
                await session.scalar(
                    text("""
                    SELECT EXISTS (
                        SELECT FROM agent_identity_state
                        WHERE issuer=:issuer AND subject=:subject
                            AND (deleted OR disabled OR token_cutoff >= :issued_at)
                    ) OR EXISTS (
                        SELECT FROM agent_identity_revoked_sessions
                        WHERE issuer=:issuer AND subject=:subject AND session_id=:session_id
                    )
                """),
                    {
                        "issuer": principal.issuer,
                        "subject": principal.subject,
                        "issued_at": issued_at,
                        "session_id": session_id,
                    },
                )
            )
