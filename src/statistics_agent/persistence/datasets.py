"""Short owned transactions for bounded data; no CSV parsing or tool execution."""

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID, uuid4

from pydantic import JsonValue
from sqlalchemy import delete, select

from statistics_agent.contracts.auth import VerifiedIdentity
from statistics_agent.contracts.common import ArtifactReference
from statistics_agent.contracts.datasets import (
    DatasetColumn,
    DatasetLimits,
    DatasetMetadata,
    StatisticsLimits,
)
from statistics_agent.persistence.database import Database
from statistics_agent.persistence.models import Dataset, DatasetArtifact, DatasetContent
from statistics_agent.persistence.ownership import owner_filter


class DatasetNotFoundError(LookupError):
    """Unknown and unauthorized resources have the same fixed internal failure."""

    def __init__(self):
        super().__init__("Dataset resource unavailable")


@dataclass(frozen=True)
class StoredArtifact:
    reference: ArtifactReference
    dataset_id: UUID
    created_at: datetime
    payload: dict[str, JsonValue] = field(repr=False)


def artifact_json(payload: dict[str, JsonValue], maximum: int) -> tuple[str, bytes]:
    """Bound JSON depth/size and reject values PostgreSQL JSON cannot represent."""
    if type(payload) is not dict:
        raise ValueError("Artifact must be a bounded JSON object")
    pending = [(payload, 0)]
    visited = 0
    characters = 0
    while pending:
        value, depth = pending.pop()
        visited += 1
        if depth > 16 or visited > maximum:
            raise ValueError("Artifact structure exceeds its limit")
        if type(value) is dict:
            if any(type(k) is not str or "\x00" in k or len(k) > maximum for k in value):
                raise ValueError("Artifact object keys are invalid")
            characters += sum(len(key) for key in value)
            if visited + len(pending) + len(value) > maximum:
                raise ValueError("Artifact structure exceeds its limit")
            pending.extend((child, depth + 1) for child in value.values())
        elif type(value) is list:
            if visited + len(pending) + len(value) > maximum:
                raise ValueError("Artifact structure exceeds its limit")
            pending.extend((child, depth + 1) for child in value)
        elif type(value) is str:
            if "\x00" in value or len(value) > maximum:
                raise ValueError("Artifact string is invalid")
            characters += len(value)
        elif value is not None and type(value) not in (bool, int, float):
            raise ValueError("Artifact values must be JSON values")
        if characters > maximum:
            raise ValueError("Artifact payload exceeds its byte limit")
    try:
        encoded = json.dumps(
            payload, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
        )
        raw = encoded.encode("utf-8")
    except (ValueError, UnicodeError, OverflowError):
        raise ValueError("Artifact JSON is invalid") from None
    if len(raw) > maximum:
        raise ValueError("Artifact payload exceeds its byte limit")
    return encoded, raw


def _metadata(row: Dataset) -> DatasetMetadata:
    return DatasetMetadata(
        dataset_id=row.id,
        filename=row.filename,
        sha256=row.sha256,
        byte_count=row.byte_count,
        row_count=row.row_count,
        columns=row.columns,
        created_at=row.created_at,
        format=row.format,
    )


class DatasetStore:
    """Accept an already authorized principal; no role or identity network lookup.

    Every resource read/delete has ownership inside SQL. No updates to dataset
    content are exposed. Each method owns and commits its own short transaction.
    """

    def __init__(
        self,
        database: Database,
        limits: DatasetLimits | None = None,
        statistics_limits: StatisticsLimits | None = None,
    ):
        self._database = database
        self._limits = DatasetLimits.model_validate(limits or DatasetLimits())
        self._statistics_limits = StatisticsLimits.model_validate(
            statistics_limits or StatisticsLimits()
        )

    @staticmethod
    def _owner(principal: VerifiedIdentity):
        return owner_filter(Dataset.issuer, Dataset.subject, principal)

    async def create(
        self,
        principal: VerifiedIdentity,
        *,
        filename: str,
        content: bytes,
        row_count: int,
        columns: tuple[DatasetColumn, ...],
    ) -> DatasetMetadata:
        if type(content) is not bytes or not 1 <= len(content) <= self._limits.max_upload_bytes:
            raise ValueError("Dataset content exceeds its byte limit or is invalid")
        value = DatasetMetadata(
            dataset_id=uuid4(),
            filename=filename,
            sha256=hashlib.sha256(content).hexdigest(),
            byte_count=len(content),
            row_count=row_count,
            columns=columns,
            created_at=datetime.now(UTC),
        )
        if (
            value.row_count > self._limits.max_rows
            or len(value.columns) > self._limits.max_columns
            or any(len(c.name) > self._limits.max_column_name for c in value.columns)
            or "\x00" in value.filename
            or any("\x00" in c.name for c in value.columns)
        ):
            raise ValueError("Dataset metadata exceeds its limits or is invalid")
        row = Dataset(
            id=value.dataset_id,
            issuer=principal.issuer,
            subject=principal.subject,
            filename=value.filename,
            sha256=value.sha256,
            byte_count=value.byte_count,
            row_count=value.row_count,
            columns=[c.model_dump(mode="json") for c in value.columns],
            format=value.format,
            created_at=value.created_at,
        )
        async with self._database.session() as session:
            session.add(row)
            await session.flush()
            session.add(DatasetContent(dataset_id=row.id, data=content))
            await session.commit()
        return value

    async def get_metadata(self, principal: VerifiedIdentity, dataset_id: UUID) -> DatasetMetadata:
        async with self._database.session() as session:
            row = await session.scalar(
                select(Dataset).where(Dataset.id == dataset_id, self._owner(principal))
            )
            if row is None:
                raise DatasetNotFoundError
            return _metadata(row)

    async def list_metadata(
        self, principal: VerifiedIdentity, *, limit: int = 50, after: UUID | None = None
    ) -> tuple[DatasetMetadata, ...]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Page size must be between 1 and 100")
        if after is not None and not isinstance(after, UUID):
            raise ValueError("Page cursor must be a dataset UUID")
        query = select(Dataset).where(self._owner(principal))
        if after is not None:
            query = query.where(Dataset.id > after)
        async with self._database.session() as session:
            rows = (await session.scalars(query.order_by(Dataset.id).limit(limit))).all()
            return tuple(_metadata(row) for row in rows)

    async def get_content(self, principal: VerifiedIdentity, dataset_id: UUID) -> bytes:
        async with self._database.session() as session:
            row = (
                await session.execute(
                    select(DatasetContent.data, Dataset.sha256, Dataset.byte_count)
                    .join(Dataset, Dataset.id == DatasetContent.dataset_id)
                    .where(Dataset.id == dataset_id, self._owner(principal))
                )
            ).one_or_none()
            if row is None:
                raise DatasetNotFoundError
            content, digest, size = row
            if len(content) != size or hashlib.sha256(content).hexdigest() != digest:
                raise ValueError("Stored dataset integrity check failed")
            return content

    async def delete(self, principal: VerifiedIdentity, dataset_id: UUID) -> bool:
        async with self._database.session() as session:
            removed = await session.scalar(
                delete(Dataset)
                .where(Dataset.id == dataset_id, self._owner(principal))
                .returning(Dataset.id)
            )
            await session.commit()
            return removed is not None

    async def create_artifact(
        self, principal: VerifiedIdentity, dataset_id: UUID, payload: dict[str, JsonValue]
    ) -> StoredArtifact:
        encoded, raw = artifact_json(payload, self._statistics_limits.max_result_bytes)
        row = DatasetArtifact(
            id=uuid4(),
            dataset_id=dataset_id,
            media_type="application/json",
            sha256=hashlib.sha256(raw).hexdigest(),
            byte_count=len(raw),
            payload_json=encoded,
            created_at=datetime.now(UTC),
        )
        async with self._database.session() as session:
            found = await session.scalar(
                select(Dataset.id)
                .where(Dataset.id == dataset_id, self._owner(principal))
                .with_for_update()
            )
            if found is None:
                raise DatasetNotFoundError
            session.add(row)
            await session.commit()
        return self._artifact(row)

    def _artifact(self, row: DatasetArtifact) -> StoredArtifact:
        raw = row.payload_json.encode("utf-8")
        if len(raw) != row.byte_count or hashlib.sha256(raw).hexdigest() != row.sha256:
            raise ValueError("Stored artifact integrity check failed")
        payload = json.loads(row.payload_json)
        artifact_json(payload, self._statistics_limits.max_result_bytes)
        return StoredArtifact(
            reference=ArtifactReference(
                artifact_id=row.id, sha256=row.sha256, media_type=row.media_type
            ),
            dataset_id=row.dataset_id,
            created_at=row.created_at,
            payload=payload,
        )

    async def get_artifact(self, principal: VerifiedIdentity, artifact_id: UUID) -> StoredArtifact:
        async with self._database.session() as session:
            row = await session.scalar(
                select(DatasetArtifact)
                .join(Dataset, Dataset.id == DatasetArtifact.dataset_id)
                .where(DatasetArtifact.id == artifact_id, self._owner(principal))
            )
            if row is None:
                raise DatasetNotFoundError
            return self._artifact(row)

    async def delete_artifact(self, principal: VerifiedIdentity, artifact_id: UUID) -> bool:
        owned = select(Dataset.id).where(self._owner(principal))
        async with self._database.session() as session:
            removed = await session.scalar(
                delete(DatasetArtifact)
                .where(DatasetArtifact.id == artifact_id, DatasetArtifact.dataset_id.in_(owned))
                .returning(DatasetArtifact.id)
            )
            await session.commit()
            return removed is not None
