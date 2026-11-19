"""Persist the execution deadline of a claimed job."""

from alembic import op

revision = "0012_worker_execution_deadline"
down_revision = "0011_run_admission_grants"
branch_labels = depends_on = None


def upgrade():
    op.execute("ALTER TABLE agent_run_jobs ADD COLUMN execution_expires_at TIMESTAMPTZ")


def downgrade():
    op.execute("LOCK TABLE agent_run_jobs IN ACCESS EXCLUSIVE MODE")
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT FROM agent_run_jobs WHERE execution_expires_at IS NOT NULL) THEN
                RAISE EXCEPTION 'Drain claimed jobs before downgrading';
            END IF;
        END $$
    """)
    op.execute("ALTER TABLE agent_run_jobs DROP COLUMN execution_expires_at")
