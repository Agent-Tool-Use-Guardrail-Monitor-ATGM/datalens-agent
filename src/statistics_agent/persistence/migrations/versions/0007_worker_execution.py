"""Add bounded worker leases and local evidence; retain schema on production rollback."""

from alembic import op

revision = "0007_worker_execution"
down_revision = "0006_run_completion"
branch_labels = depends_on = None

STATEMENTS = (
    """
CREATE TABLE agent_run_attempts (
    id UUID NOT NULL,
    run_id UUID NOT NULL,
    request_id UUID NOT NULL,
    next_sequence INTEGER NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    finished_at TIMESTAMP WITH TIME ZONE,
    reason VARCHAR(64),
    PRIMARY KEY (id),
    CONSTRAINT uq_attempt_run UNIQUE (run_id),
    CONSTRAINT ck_attempt_sequence CHECK (next_sequence BETWEEN 1 AND 1025),
    FOREIGN KEY(run_id) REFERENCES agent_runs (id) ON DELETE CASCADE
)
    """,
    """
CREATE TABLE agent_run_evidence (
    event_id UUID NOT NULL,
    attempt_id UUID NOT NULL,
    sequence INTEGER NOT NULL,
    envelope JSONB NOT NULL,
    PRIMARY KEY (event_id),
    CONSTRAINT uq_evidence_sequence UNIQUE (attempt_id, sequence),
    CONSTRAINT ck_evidence_bounds CHECK (sequence BETWEEN 1 AND 1024
        AND octet_length(envelope::text) <= 32768),
    FOREIGN KEY(attempt_id) REFERENCES agent_run_attempts (id) ON DELETE CASCADE
)
    """,
    """
CREATE TABLE agent_run_dispatches (
    attempt_id UUID NOT NULL,
    kind VARCHAR(5) NOT NULL,
    operation_id UUID NOT NULL,
    attempt_index INTEGER NOT NULL,
    outcome VARCHAR(8) NOT NULL,
    PRIMARY KEY (attempt_id, kind, operation_id, attempt_index),
    CONSTRAINT ck_dispatch_state CHECK (kind IN ('model','tool') AND attempt_index BETWEEN 1 AND 4
        AND outcome IN ('pending','observed','unknown')),
    FOREIGN KEY(attempt_id) REFERENCES agent_run_attempts (id) ON DELETE CASCADE
)
    """,
)


def upgrade():
    op.execute("ALTER TABLE agent_run_jobs ADD COLUMN auth_session_ref UUID")
    op.execute("ALTER TABLE agent_run_jobs ADD COLUMN claim_id UUID")
    op.execute("ALTER TABLE agent_run_jobs ADD COLUMN lease_expires_at TIMESTAMPTZ")
    op.execute(
        "ALTER TABLE agent_run_jobs ADD CONSTRAINT ck_job_lease "
        "CHECK ((claim_id IS NULL) = (lease_expires_at IS NULL))"
    )
    op.execute("CREATE INDEX ix_job_lease ON agent_run_jobs (lease_expires_at)")
    op.execute(
        "CREATE INDEX ix_job_pending ON agent_run_jobs (created_at, run_id) WHERE claim_id IS NULL"
    )
    for statement in STATEMENTS:
        op.execute(statement)


def downgrade():
    # Disposable tests only: discard executions which the old schema cannot fence.
    op.execute("DELETE FROM agent_runs WHERE id IN (SELECT run_id FROM agent_run_attempts)")
    for table in ("agent_run_dispatches", "agent_run_evidence", "agent_run_attempts"):
        op.execute("DROP TABLE " + table)
    op.execute("DROP INDEX ix_job_lease")
    op.execute("DROP INDEX ix_job_pending")
    op.execute("ALTER TABLE agent_run_jobs DROP CONSTRAINT ck_job_lease")
    for column in ("lease_expires_at", "claim_id", "auth_session_ref"):
        op.execute("ALTER TABLE agent_run_jobs DROP COLUMN " + column)
