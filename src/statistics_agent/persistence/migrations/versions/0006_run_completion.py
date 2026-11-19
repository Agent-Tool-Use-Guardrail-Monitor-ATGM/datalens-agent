"""Add versioned terminal answers/citation mappings without changing old runs.

Production application rollback must retain this additive column. Disposable
test downgrade removes v2-only rows before restoring the v1 constraint.
"""

from alembic import op

revision = "0006_run_completion"
down_revision = "0005_conversation_history"
branch_labels = depends_on = None


def upgrade():
    op.execute("ALTER TABLE agent_runs ADD COLUMN completion JSONB")
    op.execute("ALTER TABLE agent_runs DROP CONSTRAINT ck_run_results")
    op.execute("""
        ALTER TABLE agent_runs ADD CONSTRAINT ck_run_results CHECK (
            jsonb_typeof(results) = 'array' AND jsonb_array_length(results) <= 128 AND
            (outcome IS DISTINCT FROM 'completed' OR jsonb_array_length(results) > 0 OR
             coalesce(jsonb_array_length(completion->'imported_results'),0) > 0) AND
            (lifecycle <> 'queued' OR jsonb_array_length(results) = 0))
    """)
    op.execute("""
        ALTER TABLE agent_runs ADD CONSTRAINT ck_run_completion CHECK (
            completion IS NULL OR (lifecycle = 'finished' AND
            jsonb_typeof(completion) = 'object' AND octet_length(completion::text) <= 262144))
    """)


def downgrade():
    # Only the existing disposable-test downgrade command permits this data loss.
    op.execute("DELETE FROM agent_runs WHERE completion IS NOT NULL")
    op.execute("ALTER TABLE agent_runs DROP CONSTRAINT ck_run_completion")
    op.execute("ALTER TABLE agent_runs DROP CONSTRAINT ck_run_results")
    op.execute("ALTER TABLE agent_runs DROP COLUMN completion")
    op.execute("""
        ALTER TABLE agent_runs ADD CONSTRAINT ck_run_results CHECK (
            jsonb_typeof(results) = 'array' AND jsonb_array_length(results) <= 128 AND
            (outcome IS DISTINCT FROM 'completed' OR jsonb_array_length(results) > 0) AND
            (lifecycle <> 'queued' OR jsonb_array_length(results) = 0))
    """)
