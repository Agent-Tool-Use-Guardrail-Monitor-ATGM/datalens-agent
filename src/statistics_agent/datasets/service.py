"""Owner-scoped internal dataset lifecycle, with validation before storage."""

from dataclasses import dataclass, field
from uuid import UUID

from statistics_agent.contracts.auth import VerifiedIdentity
from statistics_agent.contracts.datasets import DatasetLimits, DatasetMetadata, DatasetPreview
from statistics_agent.datasets.csv import CsvValidationError, ParsedDataset
from statistics_agent.datasets.execution import DatasetParser
from statistics_agent.persistence.database import Database
from statistics_agent.persistence.datasets import DatasetStore


class DatasetIntegrityError(ValueError):
    def __init__(self):
        super().__init__("Stored dataset metadata does not match parsed content")


@dataclass(frozen=True)
class LoadedDataset:
    """Private authorized read snapshot, not a reusable authorization capability."""

    metadata: DatasetMetadata = field(repr=False)
    parsed: ParsedDataset = field(repr=False)


class DatasetService:
    """Consume a currently authorized principal; SQL still enforces ownership.

    Compose one shared parser/pool at application lifespan. The service does not
    refresh tokens, authenticate requests, mount routes or execute statistics.
    """

    def __init__(
        self, database: Database, *, parser: DatasetParser, limits: DatasetLimits | None = None
    ):
        self._limits = DatasetLimits.model_validate(
            limits if limits is not None else DatasetLimits()
        )
        self._parser = parser
        self._store = DatasetStore(database, limits=self._limits)

    async def ingest(
        self, principal: VerifiedIdentity, *, filename: str, content: bytes
    ) -> DatasetMetadata:
        parsed = await self._parser.parse(content, limits=self._limits)
        # Only actual parsed facts enter the transaction. No client-supplied row
        # counts, schema, owner or IDs are accepted by this method.
        return await self._store.create(
            principal,
            filename=filename,
            content=parsed.content,
            row_count=parsed.row_count,
            columns=parsed.columns,
        )

    async def get_metadata(self, principal: VerifiedIdentity, dataset_id: UUID) -> DatasetMetadata:
        return await self._store.get_metadata(principal, dataset_id)

    async def list_metadata(
        self, principal: VerifiedIdentity, *, limit: int = 50, after: UUID | None = None
    ) -> tuple[DatasetMetadata, ...]:
        return await self._store.list_metadata(principal, limit=limit, after=after)

    async def load(
        self,
        principal: VerifiedIdentity,
        dataset_id: UUID,
        *,
        limits: DatasetLimits | None = None,
    ) -> LoadedDataset:
        admitted = DatasetLimits.model_validate(limits if limits is not None else self._limits)
        # An execution may tighten admission, never relax the service's own limits.
        bounds = DatasetLimits.model_validate(
            {
                name: min(value, getattr(admitted, name)) if type(value) is int else value
                for name, value in self._limits.model_dump().items()
            }
        )
        metadata = await self._store.get_metadata(principal, dataset_id)
        if (
            metadata.byte_count > bounds.max_upload_bytes
            or metadata.row_count > bounds.max_rows
            or len(metadata.columns) > bounds.max_columns
            or any(len(column.name) > bounds.max_column_name for column in metadata.columns)
        ):
            raise CsvValidationError("dataset_limit_exceeded")
        content = await self._store.get_content(principal, dataset_id)
        parsed = await self._parser.parse(content, limits=bounds)
        # Parsing holds no DB transaction. Re-read ownership/existence after it,
        # so a deletion during parsing cannot return a successfully loaded view.
        current = await self._store.get_metadata(principal, dataset_id)
        if (
            current != metadata
            or current.row_count != parsed.row_count
            or current.columns != parsed.columns
        ):
            raise DatasetIntegrityError
        return LoadedDataset(current, parsed)

    async def preview(self, principal: VerifiedIdentity, dataset_id: UUID) -> DatasetPreview:
        loaded = await self.load(principal, dataset_id)
        return loaded.parsed.preview(loaded.metadata.dataset_id)

    async def delete(self, principal: VerifiedIdentity, dataset_id: UUID) -> bool:
        return await self._store.delete(principal, dataset_id)
