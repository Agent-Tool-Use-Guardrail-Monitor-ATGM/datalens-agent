"""Execution limits against real owned datasets, artifacts and bounded workers."""

import asyncio
import json
import threading

import pytest
from sqlalchemy import func, select

from statistics_agent.contracts.datasets import DatasetLimits, StatisticsLimits
from statistics_agent.datasets.csv import CsvValidationError
from statistics_agent.datasets.execution import DatasetParser
from statistics_agent.datasets.service import DatasetService
from statistics_agent.persistence.datasets import artifact_json
from statistics_agent.persistence.models import DatasetArtifact
from statistics_agent.tools.statistics import StatisticsTools
from support.database import run
from support.datasets import application, context, owner, proposal

pytestmark = pytest.mark.postgres
DATA = b"value,group\n100,A\n200,B"


async def artifact_count(database):
    async with database.session() as session:
        return await session.scalar(select(func.count()).select_from(DatasetArtifact))


@pytest.mark.parametrize(
    "field,boundary",
    [
        ("max_upload_bytes", len(DATA)),
        ("max_rows", 2),
        ("max_columns", 2),
        ("max_column_name", 5),
        ("max_cell_characters", 3),
    ],
)
@pytest.mark.parametrize("over_limit", [False, True])
def test_execution_dataset_limits_accept_boundary_and_reject_excess(
    migration_settings, field, boundary, over_limit
):
    async def exercise():
        async with application(migration_settings) as (database, store, worker, datasets):
            actor = owner()
            value = await datasets.ingest(actor, filename="fixture.csv", content=DATA)
            executor = StatisticsTools(
                datasets,
                worker=worker,
                artifacts=store,
                dataset_limits=DatasetLimits(**{field: boundary - int(over_limit)}),
            )
            result = await executor.execute(
                context(actor), proposal("inspect_dataset", value.dataset_id)
            )
            assert result.status == ("failed" if over_limit else "completed")
            if over_limit:
                assert json.loads(result.summary) == {"error": "dataset_limit_exceeded"}
                assert result.artifact is None
            assert await artifact_count(database) == int(not over_limit)

    run(exercise())


def test_metadata_limit_blocks_content_fetch_and_compute(migration_settings, monkeypatch):
    async def exercise():
        async with application(migration_settings) as (_, store, worker, datasets):
            actor = owner()
            value = await datasets.ingest(actor, filename="fixture.csv", content=DATA)

            async def forbidden(*args, **kwargs):
                pytest.fail("Rejected dataset must not load bytes or dispatch work")

            monkeypatch.setattr(datasets._store, "get_content", forbidden)
            monkeypatch.setattr(worker, "parse", forbidden)
            monkeypatch.setattr(worker, "compute", forbidden)
            executor = StatisticsTools(
                datasets, worker=worker, artifacts=store, dataset_limits=DatasetLimits(max_rows=1)
            )
            result = await executor.execute(
                context(actor), proposal("inspect_dataset", value.dataset_id)
            )
            assert json.loads(result.summary) == {"error": "dataset_limit_exceeded"}

    run(exercise())


def test_execution_cannot_relax_service_limits(migration_settings):
    async def exercise():
        async with application(migration_settings) as (database, store, worker, datasets):
            actor = owner()
            value = await datasets.ingest(actor, filename="fixture.csv", content=DATA)
            restricted = DatasetService(database, parser=worker, limits=DatasetLimits(max_rows=1))
            with pytest.raises(CsvValidationError):
                await restricted.load(actor, value.dataset_id, limits=DatasetLimits())
            result = await StatisticsTools(restricted, worker=worker, artifacts=store).execute(
                context(actor), proposal("inspect_dataset", value.dataset_id)
            )
            assert json.loads(result.summary) == {"error": "dataset_limit_exceeded"}
            assert await artifact_count(database) == 0

    run(exercise())


@pytest.mark.parametrize("maximum", [1, 2])
def test_admitted_group_limit_is_enforced_without_partial_artifacts(migration_settings, maximum):
    async def exercise():
        async with application(migration_settings) as (database, store, worker, datasets):
            actor = owner()
            value = await datasets.ingest(actor, filename="fixture.csv", content=DATA)
            result = await StatisticsTools(
                datasets,
                worker=worker,
                artifacts=store,
                limits=StatisticsLimits(max_groups=maximum),
            ).execute(
                context(actor),
                proposal(
                    "aggregate_statistics",
                    value.dataset_id,
                    column="value",
                    group_by="group",
                    operation="sum",
                    missing_policy="omit",
                ),
            )
            assert result.status == ("completed" if maximum == 2 else "failed")
            assert await artifact_count(database) == int(maximum == 2)
            if maximum == 1:
                assert json.loads(result.summary) == {"error": "group_limit_exceeded"}

    run(exercise())


def test_exact_result_byte_limit_preserves_evidence_and_rejects_one_byte_less(migration_settings):
    async def exercise():
        async with application(migration_settings) as (database, store, worker, datasets):
            actor = owner()
            value = await datasets.ingest(actor, filename="fixture.csv", content=DATA)
            call = proposal("inspect_dataset", value.dataset_id)
            first = await StatisticsTools(datasets, worker=worker, artifacts=store).execute(
                context(actor), call
            )
            artifact = await store.get_artifact(actor, first.artifact.artifact_id)
            maximum = len(artifact_json(artifact.payload, 16_384)[1])
            for size, expected in [(maximum, "completed"), (maximum - 1, "failed")]:
                result = await StatisticsTools(
                    datasets,
                    worker=worker,
                    artifacts=store,
                    limits=StatisticsLimits(max_result_bytes=size),
                ).execute(context(actor), call)
                assert result.status == expected
                if expected == "completed":
                    saved = await store.get_artifact(actor, result.artifact.artifact_id)
                    assert saved.payload == artifact.payload
                    assert result.summary == first.summary
                else:
                    assert result.artifact is None
                    assert json.loads(result.summary) == {"error": "result_limit_exceeded"}
            assert await artifact_count(database) == 2

    run(exercise())


def test_timed_out_computation_keeps_admission_until_actual_thread_completion(
    migration_settings, monkeypatch
):
    async def exercise():
        async with application(migration_settings) as (database, store, _, datasets):
            actor = owner()
            value = await datasets.ingest(actor, filename="fixture.csv", content=DATA)
            started, release = threading.Event(), threading.Event()
            from statistics_agent.tools import statistics

            original = statistics.inspect_dataset

            def blocked(*args):
                started.set()
                assert release.wait(5), "Synthetic computation was not released"
                return original(*args)

            monkeypatch.setattr(statistics, "inspect_dataset", blocked)
            async with DatasetParser(workers=1) as worker:
                executor = StatisticsTools(
                    DatasetService(database, parser=worker), worker=worker, artifacts=store
                )
                call = proposal("inspect_dataset", value.dataset_id)
                pending = asyncio.create_task(executor.execute(context(actor), call))
                try:
                    assert await asyncio.to_thread(started.wait, 2)
                    with pytest.raises(TimeoutError):
                        await asyncio.wait_for(pending, timeout=0.01)
                    # Repeated timeouts must not admit or queue another computation.
                    for _ in range(5):
                        result = await executor.execute(context(actor), call)
                        assert json.loads(result.summary) == {"error": "execution_unavailable"}
                    assert await artifact_count(database) == 0
                finally:
                    release.set()
                    if not pending.done():
                        pending.cancel()
                    await asyncio.gather(pending, return_exceptions=True)
            assert await artifact_count(database) == 0

    run(exercise())
