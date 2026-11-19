"""Encrypted browser sessions and one-use login attempts in agent_db only.

Downgrade deletes all local browser sessions and pending logins. It does not
delete Keycloak accounts or change identity_db. Users must sign in again.
"""

import sqlalchemy as sa
from alembic import op

revision = "0002_auth_storage"
down_revision = "0001_agent_baseline"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "auth_login_attempts",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("state_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("browser_hash", sa.String(64), nullable=False),
        sa.Column("issuer", sa.String(2048), nullable=False),
        sa.Column("return_target", sa.String(1), nullable=False),
        sa.Column("nonce_ciphertext", sa.Text(), nullable=False),
        sa.Column("verifier_ciphertext", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("expires_at > created_at", name="ck_login_deadlines"),
    )
    op.create_index("ix_login_expiry", "auth_login_attempts", ["expires_at"])
    op.create_index(
        "uq_login_browser_pending",
        "auth_login_attempts",
        ["browser_hash"],
        unique=True,
        postgresql_where=sa.text("consumed_at IS NULL"),
    )
    op.create_table(
        "auth_sessions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("cookie_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("issuer", sa.String(2048), nullable=False),
        sa.Column("subject", sa.String(255), nullable=False),
        sa.Column("access_ciphertext", sa.Text(), nullable=False),
        sa.Column("refresh_ciphertext", sa.Text(), nullable=False),
        sa.Column("id_ciphertext", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("idle_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("absolute_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("refresh_version", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "created_at <= last_seen_at AND last_seen_at < idle_expires_at "
            "AND idle_expires_at <= absolute_expires_at",
            name="ck_session_deadlines",
        ),
        sa.CheckConstraint("refresh_version >= 0", name="ck_session_version"),
    )
    op.create_index("ix_session_idle_expiry", "auth_sessions", ["idle_expires_at"])
    op.create_index("ix_session_absolute_expiry", "auth_sessions", ["absolute_expires_at"])


def downgrade() -> None:
    op.drop_table("auth_sessions")
    op.drop_table("auth_login_attempts")
