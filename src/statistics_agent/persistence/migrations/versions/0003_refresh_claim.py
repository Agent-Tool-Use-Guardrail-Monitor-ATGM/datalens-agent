"""Durable refresh claims; rollback revokes pending sessions before dropping claims."""

import sqlalchemy as sa
from alembic import op

revision = "0003_refresh_claim"
down_revision = "0002_auth_storage"
branch_labels = depends_on = None


def upgrade():
    op.add_column("auth_sessions", sa.Column("refresh_claim_id", sa.Uuid(), nullable=True))
    op.add_column(
        "auth_sessions", sa.Column("refresh_started_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_check_constraint(
        "ck_session_refresh_claim",
        "auth_sessions",
        "(refresh_claim_id IS NULL) = (refresh_started_at IS NULL)",
    )


def downgrade():
    # Never make a possibly consumed refresh credential usable again on rollback.
    op.execute(
        "UPDATE auth_sessions SET revoked_at = CURRENT_TIMESTAMP "
        "WHERE refresh_claim_id IS NOT NULL AND revoked_at IS NULL"
    )
    op.drop_constraint("ck_session_refresh_claim", "auth_sessions", type_="check")
    op.drop_column("auth_sessions", "refresh_started_at")
    op.drop_column("auth_sessions", "refresh_claim_id")
