"""Owner-scoped internal dataset lifecycle, with validation before storage."""

from dataclasses import dataclass, field
from uuid import UUID

from statistics_agent.contracts.auth import AuthorizedPrincipal
from statistics_agent.contracts.datasets import DatasetLimits, DatasetMetadata, DatasetPreview
from statistics_agent.datasets.csv import ParsedDataset
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
        self, principal: AuthorizedPrincipal, *, filename: str, content: bytes
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

    async def get_metadata(
        self, principal: AuthorizedPrincipal, dataset_id: UUID
    ) -> DatasetMetadata:
        return await self._store.get_metadata(principal, dataset_id)

    async def list_metadata(
        self, principal: AuthorizedPrincipal, *, limit: int = 50, after: UUID | None = None
    ) -> tuple[DatasetMetadata, ...]:
        return await self._store.list_metadata(principal, limit=limit, after=after)

    async def load(self, principal: AuthorizedPrincipal, dataset_id: UUID) -> LoadedDataset:
        metadata = await self._store.get_metadata(principal, dataset_id)
        content = await self._store.get_content(principal, dataset_id)
        parsed = await self._parser.parse(content, limits=self._limits)
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

    async def preview(self, principal: AuthorizedPrincipal, dataset_id: UUID) -> DatasetPreview:
        loaded = await self.load(principal, dataset_id)
        return loaded.parsed.preview(loaded.metadata.dataset_id)

    async def delete(self, principal: AuthorizedPrincipal, dataset_id: UUID) -> bool:
        return await self._store.delete(principal, dataset_id)
