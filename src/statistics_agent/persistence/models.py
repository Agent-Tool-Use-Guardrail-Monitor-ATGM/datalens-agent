"""Agent-owned browser state; never Keycloak accounts or authorization snapshots."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, Index, String, Text, Uuid, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """All future agent models share this metadata, never Keycloak's models."""


class LoginAttempt(Base):
    __tablename__ = "auth_login_attempts"
    __table_args__ = (
        CheckConstraint("expires_at > created_at", name="ck_login_deadlines"),
        Index("ix_login_expiry", "expires_at"),
        Index(
            "uq_login_browser_pending",
            "browser_hash",
            unique=True,
            postgresql_where=text("consumed_at IS NULL"),
        ),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    state_hash: Mapped[str] = mapped_column(String(64), unique=True)
    browser_hash: Mapped[str] = mapped_column(String(64))
    issuer: Mapped[str] = mapped_column(String(2048))
    return_target: Mapped[str] = mapped_column(String(1), default="/")
    nonce_ciphertext: Mapped[str] = mapped_column(Text)
    verifier_ciphertext: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class BrowserSession(Base):
    __tablename__ = "auth_sessions"
    __table_args__ = (
        CheckConstraint(
            "created_at <= last_seen_at AND last_seen_at < idle_expires_at "
            "AND idle_expires_at <= absolute_expires_at",
            name="ck_session_deadlines",
        ),
        CheckConstraint("refresh_version >= 0", name="ck_session_version"),
        CheckConstraint(
            "(refresh_claim_id IS NULL) = (refresh_started_at IS NULL)",
            name="ck_session_refresh_claim",
        ),
        Index("ix_session_idle_expiry", "idle_expires_at"),
        Index("ix_session_absolute_expiry", "absolute_expires_at"),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    cookie_hash: Mapped[str] = mapped_column(String(64), unique=True)
    issuer: Mapped[str] = mapped_column(String(2048))
    subject: Mapped[str] = mapped_column(String(255))
    access_ciphertext: Mapped[str] = mapped_column(Text)
    refresh_ciphertext: Mapped[str] = mapped_column(Text)
    id_ciphertext: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    idle_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    absolute_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    refresh_version: Mapped[int] = mapped_column(default=0)
    refresh_claim_id: Mapped[UUID | None] = mapped_column(Uuid)
    refresh_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
