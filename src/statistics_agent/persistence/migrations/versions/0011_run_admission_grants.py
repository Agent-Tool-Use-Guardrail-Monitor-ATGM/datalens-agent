"""Add session-independent grant data alongside the legacy job binding."""

from alembic import op

revision = "0011_run_admission_grants"
down_revision = "0010_identity_projection"
branch_labels = depends_on = None


def upgrade():
    op.execute("""
        ALTER TABLE agent_run_jobs
        ADD COLUMN admission_grant JSONB,
        ADD COLUMN queue_expires_at TIMESTAMPTZ,
        ADD CONSTRAINT ck_job_admission_grant CHECK (
            (admission_grant IS NULL AND queue_expires_at IS NULL)
            OR (
                admission_grant IS NOT NULL AND jsonb_typeof(admission_grant) = 'object'
                AND queue_expires_at IS NOT NULL
                AND queue_expires_at = created_at + INTERVAL '10 minutes'
            )
        )
    """)


def downgrade():
    op.execute("LOCK TABLE agent_run_jobs IN ACCESS EXCLUSIVE MODE")
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT FROM agent_run_jobs WHERE admission_grant IS NOT NULL) THEN
                RAISE EXCEPTION 'Drain granted jobs before downgrading';
            END IF;
        END $$
    """)
    op.execute("""
        ALTER TABLE agent_run_jobs
        DROP CONSTRAINT ck_job_admission_grant,
        DROP COLUMN queue_expires_at,
        DROP COLUMN admission_grant
    """)
