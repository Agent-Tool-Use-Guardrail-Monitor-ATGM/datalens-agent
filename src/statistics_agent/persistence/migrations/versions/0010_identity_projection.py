"""Add durable identity receipts and revocation state."""

from alembic import op

revision = "0010_identity_projection"
down_revision = "0009_user_profiles"
branch_labels = depends_on = None


def upgrade():
    for column in ("display_name", "username"):
        op.execute(f"ALTER TABLE agent_user_profiles ALTER COLUMN {column} TYPE VARCHAR(256)")
    op.execute("""
        CREATE TABLE agent_identity_inbox (
            event_id UUID PRIMARY KEY,
            payload_digest BYTEA NOT NULL CHECK (octet_length(payload_digest) = 32)
        )
    """)
    op.execute("""
        CREATE TABLE agent_identity_state (
            issuer VARCHAR(2048) NOT NULL,
            subject VARCHAR(255) NOT NULL,
            profile_revision BIGINT NOT NULL DEFAULT 0,
            status_revision BIGINT NOT NULL DEFAULT 0,
            token_cutoff BIGINT,
            disabled BOOLEAN NOT NULL DEFAULT FALSE,
            deleted BOOLEAN NOT NULL DEFAULT FALSE,
            PRIMARY KEY (issuer, subject)
        )
    """)
    op.execute("""
        CREATE TABLE agent_identity_revoked_sessions (
            issuer VARCHAR(2048) NOT NULL,
            subject VARCHAR(255) NOT NULL,
            session_id VARCHAR(255) NOT NULL,
            PRIMARY KEY (issuer, subject, session_id)
        )
    """)


def downgrade():
    # Disposable rollback only. Live rollback leaves the revocation state intact.
    op.execute("""
        DO $$ BEGIN
        IF EXISTS (
            SELECT FROM agent_user_profiles
            WHERE length(display_name) > 255 OR length(username) > 255
        ) THEN
            RAISE EXCEPTION 'Downgrade would narrow existing identity display data';
        END IF;
        END $$;
    """)
    for column in ("display_name", "username"):
        op.execute(f"ALTER TABLE agent_user_profiles ALTER COLUMN {column} TYPE VARCHAR(255)")
    op.execute("DROP TABLE agent_identity_revoked_sessions")
    op.execute("DROP TABLE agent_identity_state")
    op.execute("DROP TABLE agent_identity_inbox")
