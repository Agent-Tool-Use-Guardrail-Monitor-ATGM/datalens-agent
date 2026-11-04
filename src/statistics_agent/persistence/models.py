"""Agent-owned persistence; never Keycloak accounts or authorization snapshots."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    LargeBinary,
    String,
    Text,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
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


class Dataset(Base):
    __tablename__ = "agent_datasets"
    __table_args__ = (
        CheckConstraint("format = 'csv'", name="ck_dataset_format"),
        CheckConstraint("byte_count BETWEEN 1 AND 5242880", name="ck_dataset_bytes"),
        CheckConstraint("row_count BETWEEN 0 AND 10000", name="ck_dataset_rows"),
        CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="ck_dataset_digest"),
        CheckConstraint(
            "jsonb_typeof(columns) = 'array' AND jsonb_array_length(columns) BETWEEN 1 AND 128",
            name="ck_dataset_columns",
        ),
        Index("ix_dataset_owner", "issuer", "subject", "id"),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    issuer: Mapped[str] = mapped_column(String(2048))
    subject: Mapped[str] = mapped_column(String(255))
    filename: Mapped[str] = mapped_column(String(255))
    sha256: Mapped[str] = mapped_column(String(64))
    byte_count: Mapped[int]
    row_count: Mapped[int]
    columns: Mapped[list[dict]] = mapped_column(JSONB)
    format: Mapped[str] = mapped_column(String(3))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class DatasetContent(Base):
    __tablename__ = "agent_dataset_contents"
    __table_args__ = (
        CheckConstraint("octet_length(data) BETWEEN 1 AND 5242880", name="ck_content_bytes"),
    )
    dataset_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("agent_datasets.id", ondelete="CASCADE"), primary_key=True
    )
    data: Mapped[bytes] = mapped_column(LargeBinary)


class DatasetArtifact(Base):
    __tablename__ = "agent_artifacts"
    __table_args__ = (
        CheckConstraint("media_type = 'application/json'", name="ck_artifact_media_type"),
        CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="ck_artifact_digest"),
        CheckConstraint(
            "byte_count BETWEEN 1 AND 16384 AND octet_length(payload_json) = byte_count",
            name="ck_artifact_bytes",
        ),
        CheckConstraint("jsonb_typeof(payload_json::jsonb) = 'object'", name="ck_artifact_object"),
        Index("ix_artifact_dataset", "dataset_id", "id"),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    dataset_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("agent_datasets.id", ondelete="CASCADE")
    )
    media_type: Mapped[str] = mapped_column(String(128))
    sha256: Mapped[str] = mapped_column(String(64))
    byte_count: Mapped[int]
    # Preserve canonical JSON text exactly for byte/digest verification. JSONB
    # can normalize numbers; this payload is fetched by ID, not queried by value.
    payload_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
