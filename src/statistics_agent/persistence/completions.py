"""Internal terminal writer; no model calls, worker claims or user waiting."""

import hashlib
import json
from datetime import UTC, datetime
from uuid import uuid4, uuid5

from sqlalchemy import delete, select

from statistics_agent.contracts.agent import StatisticsRun
from statistics_agent.contracts.conversations import (
    ConversationRunV2,
    ImportedResult,
    ResultProvenance,
    RunCompletion,
)
from statistics_agent.persistence.conversations import (
    ConversationNotFoundError,
    RunInputUnavailableError,
    _admission,
)
from statistics_agent.persistence.models import (
    ConversationMessageRecord,
    ConversationRecord,
    Dataset,
    DatasetArtifact,
    RunAttemptRecord,
    RunJob,
    RunRecord,
)
from statistics_agent.persistence.ownership import owner_filter
from statistics_agent.tools.observations import verified_summary


async def _verified(session, principal, reference):
    # Dataset first matches deletion's parent/child lock order.
    digest = await session.scalar(
        select(Dataset.sha256)
        .where(
            Dataset.id == reference.dataset.dataset_id,
            owner_filter(Dataset.issuer, Dataset.subject, principal),
        )
        .with_for_update(read=True)
    )
    if digest is None:
        raise ValueError
    artifact = await session.scalar(
        select(DatasetArtifact)
        .where(
            DatasetArtifact.id == reference.artifact.artifact_id,
            DatasetArtifact.dataset_id == reference.dataset.dataset_id,
        )
        .with_for_update(read=True)
    )
    if artifact is None:
        raise ValueError
    raw = artifact.payload_json.encode("utf-8")
    if (
        digest != reference.dataset.sha256
        or artifact.sha256 != reference.artifact.sha256
        or artifact.media_type != reference.artifact.media_type
        or len(raw) != artifact.byte_count
        or hashlib.sha256(raw).hexdigest() != artifact.sha256
    ):
        raise ValueError
    verified_summary(
        json.loads(raw),
        tool_call_id=reference.tool_call_id,
        dataset_id=reference.dataset.dataset_id,
        sha256=digest,
    )


async def finish_run(database, execution: StatisticsRun, *, claim_id=None):
    """Persist trusted runner output after caller's access check, never client JSON.

    Recheck stored ownership and artifact identity in the terminal transaction.
    Claimed jobs require the matching live attempt, request and job actor.
    """
    from statistics_agent.persistence.jobs import (
        LeaseLost,
        finish_attempt,
        require_job_actor,
        require_lease,
    )

    execution = StatisticsRun.model_validate(execution, strict=True)
    context = execution.state.context
    async with database.session() as session:
        conversation = await session.scalar(
            select(ConversationRecord)
            .join(RunRecord)
            .where(
                RunRecord.id == context.run_id,
                owner_filter(
                    ConversationRecord.issuer, ConversationRecord.subject, context.principal
                ),
            )
            .with_for_update(of=ConversationRecord)
        )
        if conversation is None:
            raise ConversationNotFoundError
        row = await session.get(RunRecord, context.run_id, with_for_update=True)
        job = await session.get(RunJob, row.id, with_for_update=True)
        if claim_id is not None or (job is not None and job.claim_id is not None):
            await require_lease(session, row.id, claim_id)
            attempt = await session.get(RunAttemptRecord, claim_id)
            if context.session_id != claim_id or context.request_id != attempt.request_id:
                raise LeaseLost
            require_job_actor(job, context.principal)
        admission = _admission(row)
        if row.lifecycle == "finished":
            raise RunInputUnavailableError
        try:
            if execution.state.versions != admission.run.versions:
                raise ValueError
            ids = [result.tool_call_id for result in execution.results]
            if len(ids) != len(set(ids)):
                raise ValueError
            aliases = {
                uuid5(row.id, str(source.tool_call_id)): source
                for source in admission.submission.prior_results
            }
            selected = {item.dataset_id: item for item in admission.run.selection}
            imported, computed = [], []
            for result in execution.results:
                if result.status != "completed":
                    continue
                if result.artifact is None or result.summary is None:
                    raise ValueError
                source = aliases.get(result.tool_call_id)
                if source is not None:
                    previous = await session.get(RunRecord, source.run_id)
                    if (
                        previous is None
                        or previous.conversation_id != row.conversation_id
                        or source.model_dump(mode="json") not in previous.results
                        or result.artifact != source.artifact
                    ):
                        raise ValueError
                    await _verified(session, context.principal, source)
                    imported.append(ImportedResult(result_id=result.tool_call_id, source=source))
                else:
                    artifact = await session.get(DatasetArtifact, result.artifact.artifact_id)
                    if artifact is None or artifact.dataset_id not in selected:
                        raise ValueError
                    source = ResultProvenance(
                        conversation_id=row.conversation_id,
                        run_id=row.id,
                        tool_call_id=result.tool_call_id,
                        dataset=selected[artifact.dataset_id],
                        artifact=result.artifact,
                    )
                    await _verified(session, context.principal, source)
                    computed.append(source)
            now = max(datetime.now(UTC), row.started_at or row.created_at)
            completion = RunCompletion(answer=execution.answer, imported_results=tuple(imported))
            view = ConversationRunV2(
                **admission.run.model_dump(
                    exclude={
                        "schema_version",
                        "lifecycle",
                        "outcome",
                        "finished_at",
                        "results",
                        "clarification",
                    }
                ),
                lifecycle="finished",
                outcome=execution.answer.status,
                finished_at=now,
                results=tuple(computed),
                completion=completion,
                clarification=getattr(execution.answer, "clarification", None),
            )
            if "\x00" in execution.answer.explanation:
                raise ValueError
        except Exception:
            pass
        else:
            if claim_id is not None:
                await finish_attempt(session, row.id, claim_id, execution.terminal_reason)
            await write_terminal(session, row, conversation, view)
            await session.commit()
            return _admission(row)
    raise RunInputUnavailableError


async def write_terminal(session, row, conversation, view):
    """Shared atomic terminal mutation; caller owns fencing, validation and commit."""
    from statistics_agent.persistence.outbox import end_session

    await end_session(session, row, view)
    now = view.finished_at
    row.lifecycle, row.outcome, row.finished_at = "finished", view.outcome, now
    row.results = [item.model_dump(mode="json") for item in view.results]
    row.clarification = view.clarification.model_dump(mode="json") if view.clarification else None
    row.completion = view.completion.model_dump(mode="json")
    session.add(
        ConversationMessageRecord(
            id=uuid4(),
            conversation_id=row.conversation_id,
            run_id=row.id,
            sequence=conversation.next_sequence,
            role="assistant",
            content=view.completion.answer.explanation,
            created_at=now,
        )
    )
    conversation.next_sequence += 1
    await session.execute(delete(RunJob).where(RunJob.run_id == row.id))
