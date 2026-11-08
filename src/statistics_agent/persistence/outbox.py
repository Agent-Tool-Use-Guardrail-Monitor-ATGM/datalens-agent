"""Atomic typed telemetry and bounded pending backlog; no broker I/O here."""

from datetime import timedelta
from uuid import uuid4

from sqlalchemy import delete, func, select

from statistics_agent.agent.telemetry import public_event
from statistics_agent.contracts.events import EventEnvelope
from statistics_agent.contracts.run_events import (
    ModelFailed,
    SessionEnded,
    SessionStarted,
    ToolFailed,
)
from statistics_agent.persistence.models import (
    EventOutbox,
    RunAttemptRecord,
    RunDispatch,
    RunEvidence,
)

PAYLOAD_VERSION = "agent-run-events-v2"
MAX_PENDING = 10000


async def cleanup_published(database, *, retention_days=7, limit=1000):
    """Delete one bounded batch, measured from broker acknowledgement time."""
    if type(retention_days) is not int or not 1 <= retention_days <= 365:
        raise ValueError("Retention must be between 1 and 365 days")
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError("Cleanup limit must be between 1 and 1000")
    async with database.session() as session:
        now = await session.scalar(select(func.clock_timestamp()))
        ids = list(
            await session.scalars(
                select(EventOutbox.event_id)
                .where(EventOutbox.published_at < now - timedelta(days=retention_days))
                .order_by(EventOutbox.published_at, EventOutbox.position)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        )
        if ids:
            await session.execute(delete(EventOutbox).where(EventOutbox.event_id.in_(ids)))
        await session.commit()
        return len(ids)


class OutboxFull(RuntimeError):
    def __init__(self):
        super().__init__("Telemetry backlog is full")


async def record_event(session, attempt, event):
    """Caller holds the attempt/job fence and owns the surrounding transaction."""
    if event.sequence != attempt.next_sequence or event.sequence > 1024:
        raise ValueError("Worker evidence sequence unavailable")
    typed = public_event(event) if attempt.telemetry_version == PAYLOAD_VERSION else None
    if typed is not None:
        await ensure_capacity(session)
        session.add(
            EventOutbox(
                event_id=event.event_id,
                session_id=event.session_id,
                sequence=event.sequence,
                envelope=typed.model_dump(mode="json"),
            )
        )
    attempt.next_sequence += 1
    session.add(
        RunEvidence(
            event_id=event.event_id,
            attempt_id=attempt.id,
            sequence=event.sequence,
            envelope=event.model_dump(mode="json"),
        )
    )


async def lifecycle_event(session, attempt, row, kind, payload):
    now = await session.scalar(select(func.clock_timestamp()))
    event = EventEnvelope(
        event_id=uuid4(),
        event_type=kind,
        occurred_at=now,
        run_id=row.id,
        session_id=attempt.id,
        request_id=attempt.request_id,
        sequence=attempt.next_sequence,
        data=payload.model_dump(mode="json", exclude_none=True),
    )
    await record_event(session, attempt, event)


async def start_session(session, attempt, row, admission):
    await lifecycle_event(
        session,
        attempt,
        row,
        "session.started",
        SessionStarted(
            conversation_id=row.conversation_id,
            versions=admission.run.versions,
            dataset_count=len(admission.datasets),
        ),
    )


async def end_session(session, row, view):
    attempt = await session.scalar(
        select(RunAttemptRecord)
        .where(
            RunAttemptRecord.run_id == row.id,
        )
        .with_for_update()
    )
    if attempt is None or attempt.telemetry_version != PAYLOAD_VERSION:
        return
    unknown = await session.scalars(
        select(RunDispatch)
        .where(
            RunDispatch.attempt_id == attempt.id,
            RunDispatch.outcome == "unknown",
        )
        .order_by(RunDispatch.kind, RunDispatch.operation_id, RunDispatch.attempt_index)
    )
    for item in unknown:
        payload = (
            ModelFailed(
                model_call_id=item.operation_id,
                attempt_index=item.attempt_index,
                category="interrupted",
                outcome_unknown=True,
            )
            if item.kind == "model"
            else ToolFailed(
                tool_call_id=item.operation_id,
                status="unknown",
                reason="interrupted",
            )
        )
        await lifecycle_event(
            session,
            attempt,
            row,
            "model_call.failed" if item.kind == "model" else "tool_call.failed",
            payload,
        )
    await lifecycle_event(
        session,
        attempt,
        row,
        "session.ended",
        SessionEnded(
            outcome=view.outcome,
            verified_result_count=len(view.results) + len(view.completion.imported_results),
        ),
    )


async def ensure_capacity(session):
    await session.execute(select(func.pg_advisory_xact_lock(714007)))
    # Count includes this transaction's earlier inserts, before acknowledging another event.
    await session.flush()
    pending = await session.scalar(
        select(func.count())
        .select_from(EventOutbox)
        .where(
            EventOutbox.published_at.is_(None),
        )
    )
    if pending >= MAX_PENDING:
        raise OutboxFull
