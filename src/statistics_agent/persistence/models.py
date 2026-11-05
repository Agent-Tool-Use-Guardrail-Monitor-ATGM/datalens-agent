"""Agent-owned persistence; never Keycloak accounts or authorization snapshots."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
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


class ConversationRecord(Base):
    __tablename__ = "agent_conversations"
    __table_args__ = (
        CheckConstraint(
            "next_sequence >= 1 AND attachment_version >= 0", name="ck_conversation_counters"
        ),
        CheckConstraint("length(btrim(title)) BETWEEN 1 AND 120", name="ck_conversation_title"),
        Index("ix_conversation_owner", "issuer", "subject", "id"),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    issuer: Mapped[str] = mapped_column(String(2048))
    subject: Mapped[str] = mapped_column(String(255))
    title: Mapped[str] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    next_sequence: Mapped[int] = mapped_column(default=1)
    attachment_version: Mapped[int] = mapped_column(default=0)


class ConversationDataset(Base):
    __tablename__ = "agent_conversation_datasets"
    conversation_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("agent_conversations.id", ondelete="CASCADE"), primary_key=True
    )
    dataset_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("agent_datasets.id", ondelete="CASCADE"), primary_key=True
    )


class RunRecord(Base):
    __tablename__ = "agent_runs"
    __table_args__ = (
        UniqueConstraint("conversation_id", "idempotency_key", name="uq_run_idempotency"),
        UniqueConstraint("id", "conversation_id", name="uq_run_conversation_identity"),
        CheckConstraint("lifecycle IN ('queued','running','finished')", name="ck_run_lifecycle"),
        CheckConstraint(
            "(lifecycle = 'finished' AND outcome IS NOT NULL AND "
            "outcome IN ('completed','needs_input','unsupported','incomplete') "
            "AND finished_at IS NOT NULL) OR (lifecycle <> 'finished' AND outcome IS NULL "
            "AND finished_at IS NULL AND clarification IS NULL)",
            name="ck_run_terminal",
        ),
        CheckConstraint(
            "(started_at IS NULL OR started_at >= created_at) AND "
            "(finished_at IS NULL OR finished_at >= coalesce(started_at,created_at)) AND "
            "(lifecycle <> 'running' OR started_at IS NOT NULL) AND "
            "(lifecycle <> 'queued' OR started_at IS NULL)",
            name="ck_run_times",
        ),
        CheckConstraint(
            "(outcome = 'needs_input' AND clarification IS NOT NULL) OR "
            "(outcome IS DISTINCT FROM 'needs_input' AND clarification IS NULL)",
            name="ck_run_clarification",
        ),
        CheckConstraint(
            "jsonb_typeof(results) = 'array' AND jsonb_array_length(results) <= 128 AND "
            "(outcome IS DISTINCT FROM 'completed' OR jsonb_array_length(results) > 0) AND "
            "(lifecycle <> 'queued' OR jsonb_array_length(results) = 0)",
            name="ck_run_results",
        ),
        CheckConstraint(
            "jsonb_typeof(datasets) = 'array' AND jsonb_array_length(datasets) <= 16 AND "
            "octet_length(datasets::text) <= 1048576",
            name="ck_run_datasets",
        ),
        CheckConstraint(
            "jsonb_typeof(submission) = 'object' AND octet_length(submission::text) <= 65536 AND "
            "jsonb_typeof(configuration) = 'object' AND octet_length(configuration::text) <= 65536",
            name="ck_run_snapshots",
        ),
        CheckConstraint(
            "fingerprint ~ '^[0-9a-f]{64}$' AND attachment_version >= 0", name="ck_run_admission"
        ),
        Index("ix_run_conversation", "conversation_id", "id"),
        Index(
            "uq_run_active",
            "conversation_id",
            unique=True,
            postgresql_where=text("lifecycle IN ('queued','running')"),
        ),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    conversation_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("agent_conversations.id", ondelete="CASCADE")
    )
    idempotency_key: Mapped[UUID] = mapped_column(Uuid)
    fingerprint: Mapped[str] = mapped_column(String(64))
    lifecycle: Mapped[str] = mapped_column(String(16))
    outcome: Mapped[str | None] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attachment_version: Mapped[int]
    conversation_title: Mapped[str] = mapped_column(String(120))
    submission: Mapped[dict] = mapped_column(JSONB)
    configuration: Mapped[dict] = mapped_column(JSONB)
    datasets: Mapped[list] = mapped_column(JSONB)
    results: Mapped[list] = mapped_column(JSONB)
    clarification: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))


class ConversationMessageRecord(Base):
    __tablename__ = "agent_messages"
    __table_args__ = (
        ForeignKeyConstraint(
            ["run_id", "conversation_id"],
            ["agent_runs.id", "agent_runs.conversation_id"],
            ondelete="CASCADE",
            name="fk_message_run_conversation",
        ),
        UniqueConstraint("conversation_id", "sequence", name="uq_message_sequence"),
        CheckConstraint(
            "sequence >= 1 AND role IN ('user','assistant')", name="ck_message_identity"
        ),
        CheckConstraint("length(content) BETWEEN 1 AND 16384", name="ck_message_content"),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    conversation_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("agent_conversations.id", ondelete="CASCADE")
    )
    run_id: Mapped[UUID] = mapped_column(Uuid)
    sequence: Mapped[int]
    role: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class RunJob(Base):
    """Durable pending identity only; leases/claims are introduced in 7.5."""

    __tablename__ = "agent_run_jobs"
    run_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("agent_runs.id", ondelete="CASCADE"), primary_key=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
