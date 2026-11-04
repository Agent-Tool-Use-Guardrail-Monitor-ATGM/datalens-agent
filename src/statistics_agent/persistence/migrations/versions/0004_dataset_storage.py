"""Add owned immutable datasets/content/artifacts without changing auth data.

Downgrade removes dataset/artifact data. It is for disposable test rollback;
production application rollback should retain this additive schema.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0004_dataset_storage"
down_revision = "0003_refresh_claim"
branch_labels = depends_on = None


def upgrade():
    op.create_table(
        "agent_datasets",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("issuer", sa.String(2048), nullable=False),
        sa.Column("subject", sa.String(255), nullable=False),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("byte_count", sa.Integer(), nullable=False),
        sa.Column("row_count", sa.Integer(), nullable=False),
        sa.Column("columns", postgresql.JSONB(), nullable=False),
        sa.Column("format", sa.String(3), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("format = 'csv'", name="ck_dataset_format"),
        sa.CheckConstraint("byte_count BETWEEN 1 AND 5242880", name="ck_dataset_bytes"),
        sa.CheckConstraint("row_count BETWEEN 0 AND 10000", name="ck_dataset_rows"),
        sa.CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="ck_dataset_digest"),
        sa.CheckConstraint(
            "jsonb_typeof(columns) = 'array' AND jsonb_array_length(columns) BETWEEN 1 AND 128",
            name="ck_dataset_columns",
        ),
    )
    op.create_index("ix_dataset_owner", "agent_datasets", ["issuer", "subject", "id"])
    op.create_table(
        "agent_dataset_contents",
        sa.Column(
            "dataset_id",
            sa.Uuid(),
            sa.ForeignKey("agent_datasets.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("data", sa.LargeBinary(), nullable=False),
        sa.CheckConstraint("octet_length(data) BETWEEN 1 AND 5242880", name="ck_content_bytes"),
    )
    op.create_table(
        "agent_artifacts",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "dataset_id",
            sa.Uuid(),
            sa.ForeignKey("agent_datasets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("media_type", sa.String(128), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("byte_count", sa.Integer(), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("media_type = 'application/json'", name="ck_artifact_media_type"),
        sa.CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="ck_artifact_digest"),
        sa.CheckConstraint(
            "byte_count BETWEEN 1 AND 16384 AND octet_length(payload_json) = byte_count",
            name="ck_artifact_bytes",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(payload_json::jsonb) = 'object'", name="ck_artifact_object"
        ),
    )
    op.create_index("ix_artifact_dataset", "agent_artifacts", ["dataset_id", "id"])


def downgrade():
    op.drop_table("agent_artifacts")
    op.drop_table("agent_dataset_contents")
    op.drop_table("agent_datasets")
