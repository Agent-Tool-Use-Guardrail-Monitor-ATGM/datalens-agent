"""Add display-only profiles without changing existing authentication or ownership."""

from alembic import op

revision = "0009_user_profiles"
down_revision = "0008_event_outbox"
branch_labels = depends_on = None


def upgrade():
    op.execute("""
CREATE TABLE agent_user_profiles (
    issuer VARCHAR(2048) NOT NULL,
    subject VARCHAR(255) NOT NULL,
    display_name VARCHAR(255),
    username VARCHAR(255),
    email VARCHAR(320),
    profile_updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
    PRIMARY KEY (issuer, subject),
    CONSTRAINT ck_profile_nonblank CHECK (
        length(btrim(issuer)) > 0 AND length(btrim(subject)) > 0
        AND (display_name IS NULL OR length(btrim(display_name)) > 0)
        AND (username IS NULL OR length(btrim(username)) > 0)
        AND (email IS NULL OR length(btrim(email)) > 0)
    )
)
""")


def downgrade():
    # Test rollback only; production rollback may leave this additive table in place.
    op.execute("DROP TABLE agent_user_profiles")
