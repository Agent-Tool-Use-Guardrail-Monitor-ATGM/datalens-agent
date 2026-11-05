"""Add conversation history and atomic run admission; preserve existing auth/datasets.

Downgrade drops only the new history/job tables, for disposable test rollback.
Application rollback in production should retain this additive schema.
"""

from alembic import op

revision = "0005_conversation_history"
down_revision = "0004_dataset_storage"
branch_labels = depends_on = None

# Frozen SQL snapshot; applying this revision never imports mutable application models.
STATEMENTS = (
    """
    CREATE TABLE agent_conversations (
    id UUID NOT NULL,
    issuer VARCHAR(2048) NOT NULL,
    subject VARCHAR(255) NOT NULL,
    title VARCHAR(120) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    next_sequence INTEGER NOT NULL,
    attachment_version INTEGER NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT ck_conversation_counters CHECK (next_sequence >= 1 AND attachment_version >=
    0),
    CONSTRAINT ck_conversation_title CHECK (length(btrim(title)) BETWEEN 1 AND 120)
    )
    """,
    """
    CREATE INDEX ix_conversation_owner ON agent_conversations (issuer, subject, id)
    """,
    """
    CREATE TABLE agent_conversation_datasets (
    conversation_id UUID NOT NULL,
    dataset_id UUID NOT NULL,
    PRIMARY KEY (conversation_id, dataset_id),
    FOREIGN KEY(conversation_id) REFERENCES agent_conversations (id) ON DELETE CASCADE,
    FOREIGN KEY(dataset_id) REFERENCES agent_datasets (id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE agent_runs (
    id UUID NOT NULL,
    conversation_id UUID NOT NULL,
    idempotency_key UUID NOT NULL,
    fingerprint VARCHAR(64) NOT NULL,
    lifecycle VARCHAR(16) NOT NULL,
    outcome VARCHAR(16),
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    started_at TIMESTAMP WITH TIME ZONE,
    finished_at TIMESTAMP WITH TIME ZONE,
    attachment_version INTEGER NOT NULL,
    conversation_title VARCHAR(120) NOT NULL,
    submission JSONB NOT NULL,
    configuration JSONB NOT NULL,
    datasets JSONB NOT NULL,
    results JSONB NOT NULL,
    clarification JSONB,
    PRIMARY KEY (id),
    CONSTRAINT uq_run_idempotency UNIQUE (conversation_id, idempotency_key),
    CONSTRAINT uq_run_conversation_identity UNIQUE (id, conversation_id),
    CONSTRAINT ck_run_lifecycle CHECK (lifecycle IN ('queued','running','finished')),
    CONSTRAINT ck_run_terminal CHECK ((lifecycle = 'finished' AND outcome IS NOT NULL AND
    outcome IN ('completed','needs_input','unsupported','incomplete') AND finished_at IS NOT
    NULL) OR (lifecycle <> 'finished' AND outcome IS NULL AND finished_at IS NULL AND
    clarification IS NULL)),
    CONSTRAINT ck_run_times CHECK ((started_at IS NULL OR started_at >= created_at) AND
    (finished_at IS NULL OR finished_at >= coalesce(started_at,created_at)) AND (lifecycle
    <> 'running' OR started_at IS NOT NULL) AND (lifecycle <> 'queued' OR started_at IS
    NULL)),
    CONSTRAINT ck_run_clarification CHECK ((outcome = 'needs_input' AND clarification IS NOT
    NULL) OR (outcome IS DISTINCT FROM 'needs_input' AND clarification IS NULL)),
    CONSTRAINT ck_run_results CHECK (jsonb_typeof(results) = 'array' AND
    jsonb_array_length(results) <= 128 AND (outcome IS DISTINCT FROM 'completed' OR
    jsonb_array_length(results) > 0) AND (lifecycle <> 'queued' OR
    jsonb_array_length(results) = 0)),
    CONSTRAINT ck_run_datasets CHECK (jsonb_typeof(datasets) = 'array' AND
    jsonb_array_length(datasets) <= 16 AND octet_length(datasets::text) <= 1048576),
    CONSTRAINT ck_run_snapshots CHECK (jsonb_typeof(submission) = 'object' AND
    octet_length(submission::text) <= 65536 AND jsonb_typeof(configuration) = 'object' AND
    octet_length(configuration::text) <= 65536),
    CONSTRAINT ck_run_admission CHECK (fingerprint ~ '^[0-9a-f]{64}$' AND attachment_version
    >= 0),
    FOREIGN KEY(conversation_id) REFERENCES agent_conversations (id) ON DELETE CASCADE
    )
    """,
    """
    CREATE INDEX ix_run_conversation ON agent_runs (conversation_id, id)
    """,
    """
    CREATE UNIQUE INDEX uq_run_active ON agent_runs (conversation_id) WHERE lifecycle IN
    ('queued','running')
    """,
    """
    CREATE TABLE agent_messages (
    id UUID NOT NULL,
    conversation_id UUID NOT NULL,
    run_id UUID NOT NULL,
    sequence INTEGER NOT NULL,
    role VARCHAR(16) NOT NULL,
    content TEXT NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT fk_message_run_conversation FOREIGN KEY(run_id, conversation_id) REFERENCES
    agent_runs (id, conversation_id) ON DELETE CASCADE,
    CONSTRAINT uq_message_sequence UNIQUE (conversation_id, sequence),
    CONSTRAINT ck_message_identity CHECK (sequence >= 1 AND role IN ('user','assistant')),
    CONSTRAINT ck_message_content CHECK (length(content) BETWEEN 1 AND 16384),
    FOREIGN KEY(conversation_id) REFERENCES agent_conversations (id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE agent_run_jobs (
    run_id UUID NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    PRIMARY KEY (run_id),
    FOREIGN KEY(run_id) REFERENCES agent_runs (id) ON DELETE CASCADE
    )
    """,
)


def upgrade():
    for statement in STATEMENTS:
        op.execute(statement)


def downgrade():
    op.drop_table("agent_run_jobs")
    op.drop_table("agent_messages")
    op.drop_table("agent_runs")
    op.drop_table("agent_conversation_datasets")
    op.drop_table("agent_conversations")
