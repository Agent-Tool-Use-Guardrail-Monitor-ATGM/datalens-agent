"""Owned history and atomic admission; no HTTP, worker claims or model calls."""

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID, uuid4

from pydantic import model_validator
from sqlalchemy import delete, select

from statistics_agent.contracts.agent import AgentVersions, PromptDefinition
from statistics_agent.contracts.auth import AuthorizedPrincipal
from statistics_agent.contracts.common import Contract
from statistics_agent.contracts.conversations import (
    Conversation,
    ConversationMessage,
    ConversationRun,
    DatasetSelection,
    DatasetSnapshot,
    PageRequest,
    RunSubmission,
)
from statistics_agent.contracts.datasets import DatasetMetadata
from statistics_agent.contracts.policy import ToolPolicyConfiguration
from statistics_agent.core.model_settings import ModelProfile
from statistics_agent.persistence.database import Database
from statistics_agent.persistence.models import (
    ConversationDataset,
    ConversationMessageRecord,
    ConversationRecord,
    Dataset,
    DatasetArtifact,
    RunJob,
    RunRecord,
)
from statistics_agent.persistence.ownership import owner_filter


class ConversationNotFoundError(LookupError):
    def __init__(self):
        super().__init__("Conversation resource unavailable")


class ConversationBusyError(RuntimeError):
    def __init__(self):
        super().__init__("Conversation has active work")


class IdempotencyConflictError(RuntimeError):
    def __init__(self):
        super().__init__("Idempotency key content conflict")


class RunInputUnavailableError(LookupError):
    def __init__(self):
        super().__init__("Selected resource unavailable")


class AdmissionConfiguration(Contract):
    """Server-owned non-secret snapshots; never accept ModelSettings or API keys."""

    versions: AgentVersions
    policy: ToolPolicyConfiguration
    profile: ModelProfile
    prompt: PromptDefinition

    @model_validator(mode="after")
    def matching_versions(self):
        if (
            self.versions.provider != self.profile.reference
            or self.versions.policy != self.policy.reference
            or self.versions.prompt != self.prompt.reference
        ):
            raise ValueError("Configuration versions do not match snapshots")
        return self


@dataclass(frozen=True)
class StoredAdmission:
    run: ConversationRun
    submission: RunSubmission = field(repr=False)
    configuration: AdmissionConfiguration = field(repr=False)
    datasets: tuple[DatasetMetadata, ...] = field(repr=False)
    conversation_title: str = field(repr=False)
    attachment_version: int
    replayed: bool = False


def _conversation(row):
    return Conversation(conversation_id=row.id, title=row.title, created_at=row.created_at)


def _admission(row, *, replayed=False):
    configuration = AdmissionConfiguration.model_validate_json(json.dumps(row.configuration))
    datasets = tuple(DatasetMetadata.model_validate(item) for item in row.datasets)
    view = ConversationRun(
        run_id=row.id,
        conversation_id=row.conversation_id,
        lifecycle=row.lifecycle,
        outcome=row.outcome,
        created_at=row.created_at,
        started_at=row.started_at,
        finished_at=row.finished_at,
        versions=configuration.versions,
        selection=tuple(
            DatasetSnapshot(dataset_id=item.dataset_id, sha256=item.sha256) for item in datasets
        ),
        results=row.results,
        clarification=row.clarification,
    )
    return StoredAdmission(
        view,
        RunSubmission.model_validate_json(json.dumps(row.submission)),
        configuration,
        datasets,
        row.conversation_title,
        row.attachment_version,
        replayed,
    )


class ConversationStore:
    """Each operation owns a short transaction; owner predicates stay inside SQL."""

    def __init__(self, database: Database):
        self._database = database

    @staticmethod
    def _owner(principal):
        return owner_filter(ConversationRecord.issuer, ConversationRecord.subject, principal)

    async def _owned(self, session, principal, conversation_id, *, lock=False):
        query = select(ConversationRecord).where(
            ConversationRecord.id == conversation_id, self._owner(principal)
        )
        if lock:
            query = query.with_for_update()
        row = await session.scalar(query)
        if row is None:
            raise ConversationNotFoundError
        return row

    async def create(self, principal: AuthorizedPrincipal, *, title: str) -> Conversation:
        value = Conversation(conversation_id=uuid4(), title=title, created_at=datetime.now(UTC))
        async with self._database.session() as session:
            session.add(
                ConversationRecord(
                    id=value.conversation_id,
                    issuer=principal.issuer,
                    subject=principal.subject,
                    title=value.title,
                    created_at=value.created_at,
                    next_sequence=1,
                    attachment_version=0,
                )
            )
            await session.commit()
        return value

    async def get(self, principal, conversation_id) -> Conversation:
        async with self._database.session() as session:
            return _conversation(await self._owned(session, principal, conversation_id))

    async def list(self, principal, *, limit=50, after: UUID | None = None):
        PageRequest(limit=limit)
        if after is not None and not isinstance(after, UUID):
            raise ValueError("Conversation cursor must be a UUID")
        query = select(ConversationRecord).where(self._owner(principal))
        if after is not None:
            query = query.where(ConversationRecord.id > after)
        async with self._database.session() as session:
            return tuple(
                _conversation(row)
                for row in await session.scalars(query.order_by(ConversationRecord.id).limit(limit))
            )

    async def _datasets(self, session, principal, ids, *, conversation_id=None):
        query = select(Dataset).where(
            Dataset.id.in_(ids), owner_filter(Dataset.issuer, Dataset.subject, principal)
        )
        if conversation_id is not None:
            query = query.join(
                ConversationDataset, ConversationDataset.dataset_id == Dataset.id
            ).where(ConversationDataset.conversation_id == conversation_id)
        rows = (
            await session.scalars(query.order_by(Dataset.id).with_for_update(read=True, of=Dataset))
        ).all()
        if len(rows) != len(ids):
            raise RunInputUnavailableError
        by_id = {
            row.id: DatasetMetadata(
                dataset_id=row.id,
                filename=row.filename,
                sha256=row.sha256,
                byte_count=row.byte_count,
                row_count=row.row_count,
                columns=row.columns,
                created_at=row.created_at,
                format=row.format,
            )
            for row in rows
        }
        return tuple(by_id[item] for item in ids)

    async def set_attachments(self, principal, conversation_id, selection: DatasetSelection):
        selection = DatasetSelection.model_validate(selection, strict=True)
        async with self._database.session() as session:
            conversation = await self._owned(session, principal, conversation_id, lock=True)
            datasets = await self._datasets(session, principal, selection.dataset_ids)
            await session.execute(
                delete(ConversationDataset).where(
                    ConversationDataset.conversation_id == conversation_id
                )
            )
            session.add_all(
                ConversationDataset(conversation_id=conversation_id, dataset_id=item.dataset_id)
                for item in datasets
            )
            conversation.attachment_version += 1
            await session.commit()
        return datasets

    async def attachments(self, principal, conversation_id):
        async with self._database.session() as session:
            await self._owned(session, principal, conversation_id)
            ids = tuple(
                await session.scalars(
                    select(ConversationDataset.dataset_id)
                    .where(ConversationDataset.conversation_id == conversation_id)
                    .order_by(ConversationDataset.dataset_id)
                )
            )
            return await self._datasets(session, principal, ids)

    async def admit(
        self,
        principal,
        conversation_id,
        submission: RunSubmission,
        configuration: AdmissionConfiguration,
    ) -> StoredAdmission:
        submission = RunSubmission.model_validate(submission, strict=True)
        configuration = AdmissionConfiguration.model_validate(configuration, strict=True)
        fingerprint = hashlib.sha256(
            json.dumps(
                submission.model_dump(mode="json", exclude={"idempotency_key"}),
                sort_keys=True,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()
        async with self._database.session() as session:
            conversation = await self._owned(session, principal, conversation_id, lock=True)
            existing = await session.scalar(
                select(RunRecord).where(
                    RunRecord.conversation_id == conversation_id,
                    RunRecord.idempotency_key == submission.idempotency_key,
                )
            )
            if existing is not None:
                if existing.fingerprint != fingerprint:
                    raise IdempotencyConflictError
                return _admission(existing, replayed=True)
            if (
                await session.scalar(
                    select(RunRecord.id).where(
                        RunRecord.conversation_id == conversation_id,
                        RunRecord.lifecycle.in_(("queued", "running")),
                    )
                )
                is not None
            ):
                raise ConversationBusyError
            datasets = await self._datasets(
                session,
                principal,
                submission.selection.dataset_ids,
                conversation_id=conversation_id,
            )
            for prior in submission.prior_results:
                source = await session.scalar(
                    select(RunRecord).where(
                        RunRecord.id == prior.run_id,
                        RunRecord.conversation_id == conversation_id,
                    )
                )
                if (
                    prior.conversation_id != conversation_id
                    or source is None
                    or prior.model_dump(mode="json") not in source.results
                ):
                    raise RunInputUnavailableError
                artifact = await session.scalar(
                    select(DatasetArtifact)
                    .join(Dataset)
                    .where(
                        DatasetArtifact.id == prior.artifact.artifact_id,
                        DatasetArtifact.dataset_id == prior.dataset.dataset_id,
                        DatasetArtifact.sha256 == prior.artifact.sha256,
                        DatasetArtifact.media_type == prior.artifact.media_type,
                        Dataset.sha256 == prior.dataset.sha256,
                        owner_filter(Dataset.issuer, Dataset.subject, principal),
                    )
                    .with_for_update(read=True, of=Dataset)
                )
                if artifact is None:
                    raise RunInputUnavailableError
            now, run_id = datetime.now(UTC), uuid4()
            row = RunRecord(
                id=run_id,
                conversation_id=conversation_id,
                idempotency_key=submission.idempotency_key,
                fingerprint=fingerprint,
                lifecycle="queued",
                outcome=None,
                created_at=now,
                started_at=None,
                finished_at=None,
                attachment_version=conversation.attachment_version,
                conversation_title=conversation.title,
                submission=submission.model_dump(mode="json"),
                configuration=configuration.model_dump(mode="json"),
                datasets=[item.model_dump(mode="json") for item in datasets],
                results=[],
                clarification=None,
            )
            session.add(row)
            await session.flush()
            message = ConversationMessage(
                message_id=uuid4(),
                conversation_id=conversation_id,
                run_id=run_id,
                sequence=conversation.next_sequence,
                role="user",
                content=submission.question,
                created_at=now,
            )
            session.add(
                ConversationMessageRecord(
                    id=message.message_id,
                    conversation_id=conversation_id,
                    run_id=run_id,
                    sequence=message.sequence,
                    role=message.role,
                    content=message.content,
                    created_at=now,
                )
            )
            session.add(RunJob(run_id=run_id, created_at=now))
            conversation.next_sequence += 1
            result = _admission(row)
            await session.commit()
            return result

    async def get_run(self, principal, run_id) -> StoredAdmission:
        async with self._database.session() as session:
            row = await session.scalar(
                select(RunRecord)
                .join(ConversationRecord)
                .where(
                    RunRecord.id == run_id,
                    self._owner(principal),
                )
            )
            if row is None:
                raise ConversationNotFoundError
            return _admission(row)

    async def messages(self, principal, conversation_id, *, limit=50, after_sequence=0):
        PageRequest(limit=limit)
        if type(after_sequence) is not int or after_sequence < 0:
            raise ValueError("Message cursor must be a nonnegative sequence")
        async with self._database.session() as session:
            await self._owned(session, principal, conversation_id)
            rows = await session.scalars(
                select(ConversationMessageRecord)
                .where(
                    ConversationMessageRecord.conversation_id == conversation_id,
                    ConversationMessageRecord.sequence > after_sequence,
                )
                .order_by(ConversationMessageRecord.sequence)
                .limit(limit)
            )
            return tuple(
                ConversationMessage(
                    message_id=row.id,
                    conversation_id=row.conversation_id,
                    run_id=row.run_id,
                    sequence=row.sequence,
                    role=row.role,
                    content=row.content,
                    created_at=row.created_at,
                )
                for row in rows
            )

    async def delete(self, principal, conversation_id):
        async with self._database.session() as session:
            conversation = await self._owned(session, principal, conversation_id, lock=True)
            if (
                await session.scalar(
                    select(RunRecord.id).where(
                        RunRecord.conversation_id == conversation_id,
                        RunRecord.lifecycle.in_(("queued", "running")),
                    )
                )
                is not None
            ):
                raise ConversationBusyError
            await session.delete(conversation)
            await session.commit()

    async def recent_messages(self, principal, conversation_id, *, before_run_id, limit=13):
        """Bounded newest history strictly before the admitted question, not a full replay."""
        PageRequest(limit=limit)
        async with self._database.session() as session:
            await self._owned(session, principal, conversation_id)
            boundary = await session.scalar(
                select(ConversationMessageRecord.sequence).where(
                    ConversationMessageRecord.conversation_id == conversation_id,
                    ConversationMessageRecord.run_id == before_run_id,
                    ConversationMessageRecord.role == "user",
                )
            )
            if boundary is None:
                raise ConversationNotFoundError
            rows = (
                await session.scalars(
                    select(ConversationMessageRecord)
                    .where(
                        ConversationMessageRecord.conversation_id == conversation_id,
                        ConversationMessageRecord.sequence < boundary,
                    )
                    .order_by(ConversationMessageRecord.sequence.desc())
                    .limit(limit)
                )
            ).all()
            return tuple(
                ConversationMessage(
                    message_id=row.id,
                    conversation_id=row.conversation_id,
                    run_id=row.run_id,
                    sequence=row.sequence,
                    role=row.role,
                    content=row.content,
                    created_at=row.created_at,
                )
                for row in reversed(rows)
            )
