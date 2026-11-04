"""Real PostgreSQL parse-first ingestion, ownership and lifecycle integration."""

import asyncio
import threading
from contextlib import asynccontextmanager
from uuid import uuid4

import pytest
from sqlalchemy import event, func, select

from statistics_agent.contracts.datasets import DatasetLimits
from statistics_agent.datasets.csv import CsvValidationError
from statistics_agent.datasets.execution import DatasetParser, DatasetParserBusyError
from statistics_agent.datasets.service import DatasetIntegrityError, DatasetService
from statistics_agent.persistence.datasets import DatasetNotFoundError
from statistics_agent.persistence.models import Dataset, DatasetArtifact, DatasetContent
from test_dataset_storage import COLUMNS, CONTENT, owner, storage
from test_migrations import run
from test_migrations import settings as settings

pytestmark = pytest.mark.postgres


@asynccontextmanager
async def application(settings, *, limits=None):
    async with storage(settings) as (database, store):
        async with DatasetParser(workers=1) as parser:
            yield database, store, parser, DatasetService(database, parser=parser, limits=limits)


def test_ingestion_derives_metadata_and_roundtrips_owned_preview_and_content(settings):
    async def exercise():
        async with application(settings) as (database, store, parser, service):
            principal = owner()
            raw = b'\xef\xbb\xbf price ,label\r\n 2 ,apple\r\n,""\r\n6,pear\r\n'
            metadata = await service.ingest(principal, filename="data.csv", content=raw)
            assert metadata.row_count == 3
            assert [c.name for c in metadata.columns] == ["price", "label"]
            assert metadata.columns[0].missing_count == 1
            assert await service.get_metadata(principal, metadata.dataset_id) == metadata
            assert await service.list_metadata(principal) == (metadata,)
            assert await store.get_content(principal, metadata.dataset_id) == raw
            fresh = DatasetService(database, parser=parser)
            loaded = await fresh.load(principal, metadata.dataset_id)
            assert loaded.metadata == metadata and loaded.parsed.content == raw
            preview = await fresh.preview(principal, metadata.dataset_id)
            assert preview.rows == ((" 2 ", "apple"), (None, None), ("6", "pear"))
            assert repr(loaded) == "LoadedDataset()"

    run(exercise())


@pytest.mark.parametrize("raw", [b'a\n"unclosed', b"a,b\n1", b"a\n\xff", b"a\nNaN"])
def test_rejected_ingestion_creates_no_partial_rows_and_service_recovers(settings, raw):
    async def exercise():
        async with application(settings) as (database, _, _, service):
            principal = owner()
            with pytest.raises(CsvValidationError):
                await service.ingest(principal, filename="private.csv", content=raw)
            async with database.session() as session:
                for model in (Dataset, DatasetContent, DatasetArtifact):
                    assert await session.scalar(select(func.count()).select_from(model)) == 0
            assert await service.ingest(principal, filename="valid.csv", content=CONTENT)

    run(exercise())


def test_lowered_limits_and_preview_cap_apply_to_real_ingestion(settings):
    async def exercise():
        limits = DatasetLimits(max_rows=2, max_preview_rows=1)
        async with application(settings, limits=limits) as (_, _, _, service):
            principal = owner()
            with pytest.raises(CsvValidationError):
                await service.ingest(principal, filename="too-many.csv", content=CONTENT)
            assert await service.list_metadata(principal) == ()
            value = await service.ingest(principal, filename="valid.csv", content=b"price\n2\n4")
            preview = await service.preview(principal, value.dataset_id)
            assert preview.row_count == 2 and preview.rows == (("2",),)

    run(exercise())


@pytest.mark.parametrize(
    "identity",
    [
        {"subject": "other"},
        {"issuer": "https://another.example"},
        {"subject": "admin", "roles": ["admin", "user"]},
    ],
)
def test_cross_owner_access_is_denied_before_parsing_including_admins(
    settings, monkeypatch, identity
):
    async def exercise():
        async with application(settings) as (_, _, parser, service):
            principal, other = owner(), owner(**identity)
            value = await service.ingest(principal, filename="data.csv", content=CONTENT)

            async def forbidden(*args, **kwargs):
                raise AssertionError("Unowned content was dispatched to parser")

            monkeypatch.setattr(parser, "parse", forbidden)
            assert await service.list_metadata(other) == ()
            for identifier in (value.dataset_id, uuid4()):
                for operation in (service.get_metadata, service.load, service.preview):
                    with pytest.raises(DatasetNotFoundError, match="resource unavailable"):
                        await operation(other, identifier)
                assert await service.delete(other, identifier) is False

    run(exercise())


def test_owned_delete_removes_raw_bytes_artifacts_and_future_access(settings):
    async def exercise():
        async with application(settings) as (_, store, _, service):
            principal = owner()
            value = await service.ingest(principal, filename="data.csv", content=CONTENT)
            artifact = await store.create_artifact(principal, value.dataset_id, {"mean": 4})
            assert await service.delete(principal, value.dataset_id)
            assert not await service.delete(principal, value.dataset_id)
            for operation in (
                service.get_metadata,
                service.load,
                service.preview,
                store.get_content,
            ):
                with pytest.raises(DatasetNotFoundError):
                    await operation(principal, value.dataset_id)
            with pytest.raises(DatasetNotFoundError):
                await store.get_artifact(principal, artifact.reference.artifact_id)

    run(exercise())


def test_valid_parse_followed_by_write_failure_rolls_back_dataset_metadata(settings):
    async def exercise():
        async with application(settings) as (database, _, _, service):
            principal = owner()

            def fail(connection, cursor, statement, parameters, context, many):
                if statement.startswith("INSERT INTO agent_dataset_contents"):
                    raise RuntimeError("Controlled write failure")

            event.listen(database.engine.sync_engine, "before_cursor_execute", fail)
            try:
                with pytest.raises(RuntimeError, match="Controlled write"):
                    await service.ingest(principal, filename="valid.csv", content=CONTENT)
            finally:
                event.remove(database.engine.sync_engine, "before_cursor_execute", fail)
            assert await service.list_metadata(principal) == ()

    run(exercise())


def test_loading_detects_stored_metadata_that_disagrees_with_actual_csv(settings):
    async def exercise():
        async with application(settings) as (_, store, _, service):
            principal = owner()
            value = await store.create(
                principal,
                filename="inconsistent.csv",
                content=b"price\n2",
                row_count=3,
                columns=COLUMNS,
            )
            with pytest.raises(DatasetIntegrityError, match="does not match"):
                await service.load(principal, value.dataset_id)

    run(exercise())


def test_deletion_during_parse_prevents_successful_loaded_result(settings, monkeypatch):
    async def exercise():
        async with application(settings) as (_, _, parser, service):
            principal = owner()
            value = await service.ingest(principal, filename="data.csv", content=CONTENT)
            original = parser.parse

            async def delete_after_parse(*args, **kwargs):
                parsed = await original(*args, **kwargs)
                assert await service.delete(principal, value.dataset_id)
                return parsed

            monkeypatch.setattr(parser, "parse", delete_after_parse)
            with pytest.raises(DatasetNotFoundError):
                await service.load(principal, value.dataset_id)

    run(exercise())


def test_service_keyset_listing_is_bounded_and_owned(settings):
    async def exercise():
        async with application(settings) as (_, _, _, service):
            principal = owner()
            values = [
                await service.ingest(principal, filename="data.csv", content=CONTENT)
                for _ in range(3)
            ]
            seen, after = [], None
            while page := await service.list_metadata(principal, limit=1, after=after):
                after = page[0].dataset_id
                seen.append(after)
            assert seen == sorted(v.dataset_id for v in values)
            assert await service.list_metadata(owner(subject="other")) == ()

    run(exercise())


def test_busy_or_cancelled_during_parse_ingestion_does_not_write(settings, monkeypatch):
    started, release = threading.Event(), threading.Event()
    from statistics_agent.datasets.csv import parse_csv

    def controlled(content, *, limits):
        started.set()
        assert release.wait(5), "Test did not release parser"
        return parse_csv(content, limits=limits)

    monkeypatch.setattr("statistics_agent.datasets.execution.parse_csv", controlled)

    async def exercise():
        async with application(settings) as (database, _, parser, service):
            principal = owner()
            task = asyncio.create_task(
                service.ingest(principal, filename="data.csv", content=CONTENT)
            )
            try:
                assert await asyncio.to_thread(started.wait, 3)
                assert database.engine.pool.checkedout() == 0
                with pytest.raises(DatasetParserBusyError):
                    await service.ingest(principal, filename="busy.csv", content=CONTENT)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            finally:
                release.set()
            await parser.aclose()
            assert await service.list_metadata(principal) == ()

    run(exercise())
