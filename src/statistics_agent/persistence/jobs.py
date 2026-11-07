"""Short leased transactions, local evidence and conservative interrupted-run recovery."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import func, select, update

from statistics_agent.contracts.agent import StatisticsAnswer
from statistics_agent.contracts.common import AuthenticatedPrincipal
from statistics_agent.contracts.conversations import (
    ConversationRunV2,
    ResultProvenance,
    RunCompletion,
)
from statistics_agent.contracts.events import EventEnvelope, EventType
from statistics_agent.persistence.completions import _verified, write_terminal
from statistics_agent.persistence.conversations import _admission
from statistics_agent.persistence.models import (
    ConversationRecord,
    DatasetArtifact,
    RunAttemptRecord,
    RunDispatch,
    RunEvidence,
    RunJob,
    RunRecord,
)


class LeaseLost(RuntimeError):
    def __init__(self):
        super().__init__("Worker lease unavailable")


@dataclass(frozen=True)
class JobClaim:
    run_id: UUID
    attempt_id: UUID
    request_id: UUID
    auth_session_ref: UUID | None
    owner: AuthenticatedPrincipal
    deadline: datetime


async def require_lease(session, run_id, claim_id, *, expired=False):
    job = await session.get(RunJob, run_id, with_for_update=True)
    now = await session.scalar(select(func.clock_timestamp()))
    if (
        claim_id is None
        or job is None
        or job.claim_id != claim_id
        or job.lease_expires_at is None
        or ((job.lease_expires_at > now) if expired else (job.lease_expires_at <= now))
    ):
        raise LeaseLost
    return job, now


async def finish_attempt(session, run_id, claim_id, reason):
    _, now = await require_lease(session, run_id, claim_id)
    attempt = await session.get(RunAttemptRecord, claim_id, with_for_update=True)
    attempt.finished_at, attempt.reason = now, reason if len(reason) <= 64 else "incomplete"
    await session.execute(
        update(RunDispatch)
        .where(
            RunDispatch.attempt_id == claim_id,
            RunDispatch.outcome == "pending",
        )
        .values(outcome="unknown")
    )


class JobStore:
    """One global active lease initially; replicas share this admission guard."""

    def __init__(self, database, *, lease_seconds=30):
        if type(lease_seconds) is not int or not 3 <= lease_seconds <= 300:
            raise ValueError("Worker lease must be 3 to 300 seconds")
        self.database, self.lease_seconds = database, lease_seconds

    @staticmethod
    async def _candidate(session, predicate):
        conversation = await session.scalar(
            select(ConversationRecord)
            .join(RunRecord)
            .join(RunJob)
            .where(predicate)
            .order_by(RunJob.created_at, RunJob.run_id)
            .limit(1)
            .with_for_update(of=ConversationRecord, skip_locked=True)
        )
        if conversation is None:
            return None
        row = await session.scalar(
            select(RunRecord)
            .join(RunJob)
            .where(
                RunRecord.conversation_id == conversation.id,
                predicate,
            )
            .with_for_update(of=RunRecord)
        )
        return conversation, row

    async def claim(self):
        async with self.database.session() as session:
            # Serialize admission across replicas, never across remote calls.
            if not await session.scalar(select(func.pg_try_advisory_xact_lock(714005))):
                return None
            now = await session.scalar(select(func.clock_timestamp()))
            if await session.scalar(
                select(RunJob.run_id)
                .where(
                    RunJob.lease_expires_at > now,
                )
                .limit(1)
            ):
                return None
            selected = await self._candidate(
                session, ((RunJob.claim_id.is_(None)) & (RunRecord.lifecycle == "queued"))
            )
            if selected is None:
                return None
            conversation, row = selected
            job = await session.get(RunJob, row.id, with_for_update=True)
            attempt_id, request_id = uuid4(), uuid4()
            admitted = _admission(row)
            row.lifecycle, row.started_at = "running", max(now, row.created_at)
            job.claim_id = attempt_id
            job.lease_expires_at = now + timedelta(seconds=self.lease_seconds)
            session.add(
                RunAttemptRecord(
                    id=attempt_id,
                    run_id=row.id,
                    request_id=request_id,
                    next_sequence=1,
                    created_at=now,
                )
            )
            await session.commit()
            return JobClaim(
                row.id,
                attempt_id,
                request_id,
                job.auth_session_ref,
                AuthenticatedPrincipal(issuer=conversation.issuer, subject=conversation.subject),
                datetime.now(UTC)
                + timedelta(seconds=admitted.configuration.profile.run_timeout_seconds),
            )

    async def heartbeat(self, claim):
        async with self.database.session() as session:
            job, now = await require_lease(session, claim.run_id, claim.attempt_id)
            job.lease_expires_at = now + timedelta(seconds=self.lease_seconds)
            await session.commit()

    async def before_model(self, claim, request):
        if (
            request.attempt.run_id != claim.run_id
            or request.attempt.session_id != claim.attempt_id
            or request.attempt.request_id != claim.request_id
        ):
            raise LeaseLost
        async with self.database.session() as session:
            await require_lease(session, claim.run_id, claim.attempt_id)
            session.add(
                RunDispatch(
                    attempt_id=claim.attempt_id,
                    kind="model",
                    operation_id=request.attempt.model_call_id,
                    attempt_index=request.attempt.attempt_index,
                    outcome="pending",
                )
            )
            await session.commit()

    async def append(self, claim, event):
        event = EventEnvelope.model_validate_json(event.model_dump_json())
        if (
            event.run_id != claim.run_id
            or event.session_id != claim.attempt_id
            or event.request_id != claim.request_id
        ):
            raise LeaseLost
        async with self.database.session() as session:
            await require_lease(session, claim.run_id, claim.attempt_id)
            attempt = await session.get(RunAttemptRecord, claim.attempt_id, with_for_update=True)
            if event.sequence != attempt.next_sequence or event.sequence > 1024:
                raise ValueError("Worker evidence sequence unavailable")
            attempt.next_sequence += 1
            session.add(
                RunEvidence(
                    event_id=event.event_id,
                    attempt_id=claim.attempt_id,
                    sequence=event.sequence,
                    envelope=event.model_dump(mode="json"),
                )
            )
            kind = event.event_type
            if kind == EventType.TOOL_CALL_REQUESTED:
                session.add(
                    RunDispatch(
                        attempt_id=claim.attempt_id,
                        kind="tool",
                        operation_id=UUID(event.data["tool_call_id"]),
                        attempt_index=1,
                        outcome="pending",
                    )
                )
            else:
                is_model = kind in {EventType.MODEL_CALL_COMPLETED, EventType.MODEL_CALL_FAILED}
                operation_id = event.data.get("model_call_id" if is_model else "tool_call_id")
                if operation_id is not None:
                    await session.execute(
                        update(RunDispatch)
                        .where(
                            RunDispatch.attempt_id == claim.attempt_id,
                            RunDispatch.kind == ("model" if is_model else "tool"),
                            RunDispatch.operation_id == UUID(operation_id),
                            RunDispatch.attempt_index
                            == (event.data["attempt_index"] if is_model else 1),
                        )
                        .values(outcome="observed")
                    )
            await session.commit()

    async def checkpoint(self, claim, context, result):
        if (
            context.run_id != claim.run_id
            or context.session_id != claim.attempt_id
            or context.request_id != claim.request_id
            or context.principal.auth_session_ref != claim.auth_session_ref
        ):
            raise LeaseLost
        async with self.database.session() as session:
            conversation = await session.scalar(
                select(ConversationRecord)
                .join(RunRecord)
                .where(
                    RunRecord.id == claim.run_id,
                )
                .with_for_update(of=ConversationRecord)
            )
            row = await session.get(RunRecord, claim.run_id, with_for_update=True)
            await require_lease(session, claim.run_id, claim.attempt_id)
            if (
                context.principal.issuer != conversation.issuer
                or context.principal.subject != conversation.subject
                or result.status != "completed"
                or result.artifact is None
            ):
                raise ValueError("Worker result unavailable")
            intent = await session.get(
                RunDispatch, (claim.attempt_id, "tool", result.tool_call_id, 1)
            )
            artifact = await session.get(DatasetArtifact, result.artifact.artifact_id)
            selected = {item.dataset_id: item for item in _admission(row).run.selection}
            if intent is None or artifact is None or artifact.dataset_id not in selected:
                raise ValueError("Worker result unavailable")
            reference = ResultProvenance(
                conversation_id=row.conversation_id,
                run_id=row.id,
                tool_call_id=result.tool_call_id,
                dataset=selected[artifact.dataset_id],
                artifact=result.artifact,
            )
            await _verified(session, context.principal, reference)
            if len(row.results) >= 128 or any(
                item["tool_call_id"] == str(result.tool_call_id) for item in row.results
            ):
                raise ValueError("Worker result checkpoint limit")
            row.results = [*row.results, reference.model_dump(mode="json")]
            await session.commit()

    async def interrupt(self, claim, reason="interrupted", *, expired=False):
        async with self.database.session() as session:
            conversation = await session.scalar(
                select(ConversationRecord)
                .join(RunRecord)
                .where(
                    RunRecord.id == claim.run_id,
                )
                .with_for_update(of=ConversationRecord)
            )
            row = await session.get(RunRecord, claim.run_id, with_for_update=True)
            _, now = await require_lease(session, claim.run_id, claim.attempt_id, expired=expired)
            if row.lifecycle != "running":
                raise LeaseLost
            admitted = _admission(row)
            results = []
            for result in admitted.run.results:
                try:
                    await _verified(session, claim.owner, result)
                except (ValueError, KeyError):
                    continue
                results.append(result)
            view = ConversationRunV2(
                **admitted.run.model_dump(
                    exclude={
                        "schema_version",
                        "lifecycle",
                        "outcome",
                        "finished_at",
                        "results",
                    }
                ),
                lifecycle="finished",
                outcome="incomplete",
                finished_at=max(now, row.started_at or row.created_at),
                results=tuple(results),
                completion=RunCompletion(
                    answer=StatisticsAnswer(
                        status="incomplete",
                        explanation="The analysis stopped; unfinished operations were not retried.",
                    )
                ),
            )
            attempt = await session.get(RunAttemptRecord, claim.attempt_id, with_for_update=True)
            attempt.finished_at, attempt.reason = now, reason
            await session.execute(
                update(RunDispatch)
                .where(
                    RunDispatch.attempt_id == claim.attempt_id,
                    RunDispatch.outcome == "pending",
                )
                .values(outcome="unknown")
            )
            await write_terminal(session, row, conversation, view)
            await session.commit()

    async def recover_one(self):
        async with self.database.session() as session:
            now = await session.scalar(select(func.clock_timestamp()))
            selected = await self._candidate(session, RunJob.lease_expires_at <= now)
            if selected is None:
                return False
            conversation, row = selected
            job = await session.get(RunJob, row.id)
            attempt = await session.get(RunAttemptRecord, job.claim_id)
            claim = JobClaim(
                row.id,
                job.claim_id,
                attempt.request_id,
                job.auth_session_ref,
                AuthenticatedPrincipal(issuer=conversation.issuer, subject=conversation.subject),
                now,
            )
        # Reacquire and recheck expiry: heartbeat/recovery may have won since selection.
        try:
            await self.interrupt(claim, expired=True)
        except LeaseLost:
            return False
        return True
