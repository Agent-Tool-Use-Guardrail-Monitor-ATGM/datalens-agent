"""Real owned CSV/tools/artifacts; no permissive production policy or model calls."""

import json
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from statistics_agent.contracts.datasets import StatisticsLimits
from statistics_agent.persistence.models import DatasetArtifact
from statistics_agent.tools.statistics import MAX_SUMMARY_BYTES, StatisticsTools
from support.database import run
from support.datasets import CONTENT, application, context, owner, proposal

pytestmark = pytest.mark.postgres


@pytest.mark.parametrize("name", ["inspect_dataset", "describe_columns"])
def test_real_tools_persist_typed_results_with_exact_call_and_dataset_provenance(
    migration_settings, name
):
    async def exercise():
        async with application(migration_settings) as (_, store, worker, datasets):
            principal = owner()
            metadata = await datasets.ingest(principal, filename="private.csv", content=CONTENT)
            tools = StatisticsTools(datasets, worker=worker, artifacts=store)
            arguments = (
                {}
                if name == "inspect_dataset"
                else {"columns": ["price"], "missing_policy": "omit"}
            )
            request = proposal(name, metadata.dataset_id, **arguments)
            result = await tools.execute(context(principal), request)
            assert result.status == "completed" and result.tool_call_id == request.tool_call_id
            assert len(result.summary.encode("utf-8")) <= MAX_SUMMARY_BYTES
            artifact = await store.get_artifact(principal, result.artifact.artifact_id)
            payload = artifact.payload
            assert payload["tool_call_id"] == str(request.tool_call_id)
            assert payload["dataset_sha256"] == metadata.sha256
            assert payload["result"]["dataset_id"] == str(metadata.dataset_id)
            if name == "describe_columns":
                assert payload["result"]["columns"][0]["metrics"]["mean"] == 4.0
                assert json.loads(result.summary)["result"]["columns"][0]["metrics"]["mean"] == 4.0
            else:
                assert payload["result"]["row_count"] == 3
            assert str(principal.auth_session_ref) not in json.dumps(payload)
            assert principal.issuer not in json.dumps(payload)
            assert {binding.name for binding in tools.bindings} == {
                "inspect_dataset",
                "describe_columns",
                "aggregate_statistics",
                "correlate_columns",
            }
            assert all(
                b.schema()["function"]["parameters"]["additionalProperties"] is False
                for b in tools.bindings
            )

    run(exercise())


@pytest.mark.parametrize(
    "raw,columns,policy,code",
    [
        (b"price\ntext", ["price"], "omit", "non_numeric_column"),
        (CONTENT, ["unknown"], "omit", "invalid_column"),
        (b'price\n1\n""', ["price"], "error", "missing_values"),
        (b"price\n1e308\n1e308", ["price"], "omit", "numeric_range_exceeded"),
        (CONTENT, ["price", "price"], "omit", "invalid_arguments"),
    ],
)
def test_invalid_or_unrepresentable_work_never_creates_a_success_artifact(
    migration_settings, raw, columns, policy, code
):
    async def exercise():
        async with application(migration_settings) as (database, store, worker, datasets):
            principal = owner()
            value = await datasets.ingest(principal, filename="data.csv", content=raw)
            request = proposal(
                "describe_columns", value.dataset_id, columns=columns, missing_policy=policy
            )
            result = await StatisticsTools(datasets, worker=worker, artifacts=store).execute(
                context(principal), request
            )
            assert result.status == "failed" and result.artifact is None
            assert json.loads(result.summary) == {"error": code}
            async with database.session() as session:
                assert await session.scalar(select(func.count()).select_from(DatasetArtifact)) == 0

    run(exercise())


@pytest.mark.parametrize(
    "identity", [{"subject": "other"}, {"subject": "admin", "roles": ["user", "admin"]}]
)
def test_tool_executor_cannot_compute_or_persist_other_owners_data(
    migration_settings, monkeypatch, identity
):
    async def exercise():
        async with application(migration_settings) as (_, store, worker, datasets):
            principal = owner()
            value = await datasets.ingest(principal, filename="data.csv", content=CONTENT)

            async def forbidden(*args, **kwargs):
                raise AssertionError("Unowned numerical computation dispatched")

            monkeypatch.setattr(worker, "compute", forbidden)
            result = await StatisticsTools(datasets, worker=worker, artifacts=store).execute(
                context(owner(**identity)), proposal("inspect_dataset", value.dataset_id)
            )
            assert result.status == "failed" and json.loads(result.summary) == {
                "error": "dataset_not_found"
            }

    run(exercise())


def test_empty_sample_has_persisted_explicit_unavailable_metrics(migration_settings):
    async def exercise():
        async with application(migration_settings) as (_, store, worker, datasets):
            principal = owner()
            value = await datasets.ingest(principal, filename="empty.csv", content=b"price")
            result = await StatisticsTools(datasets, worker=worker, artifacts=store).execute(
                context(principal),
                proposal(
                    "describe_columns", value.dataset_id, columns=["price"], missing_policy="omit"
                ),
            )
            assert result.status == "completed"
            artifact = await store.get_artifact(principal, result.artifact.artifact_id)
            column = artifact.payload["result"]["columns"][0]
            assert column["status"] == "unavailable" and column["metrics"] is None
            assert column["reason"] == "insufficient_data"

    run(exercise())


def test_large_results_use_explicit_artifact_observation_and_oversize_is_rejected(
    migration_settings,
):
    async def exercise():
        async with application(migration_settings) as (database, store, worker, datasets):
            principal = owner()
            names = [f"c{i}" for i in range(12)]
            raw = (",".join(names) + "\n" + ",".join(["2"] * 12)).encode()
            value = await datasets.ingest(principal, filename="wide.csv", content=raw)
            request = proposal(
                "describe_columns", value.dataset_id, columns=names, missing_policy="omit"
            )
            result = await StatisticsTools(datasets, worker=worker, artifacts=store).execute(
                context(principal), request
            )
            assert result.status == "completed"
            assert json.loads(result.summary)["details_in_artifact"] is True
            assert len(result.summary.encode()) <= MAX_SUMMARY_BYTES
            artifact = await store.get_artifact(principal, result.artifact.artifact_id)
            assert len(artifact.payload["result"]["columns"]) == 12
            bounded = StatisticsTools(
                datasets,
                worker=worker,
                artifacts=store,
                limits=StatisticsLimits(max_result_bytes=1),
            )
            failed = await bounded.execute(context(principal), request)
            assert failed.status == "failed" and json.loads(failed.summary) == {
                "error": "result_limit_exceeded"
            }
            async with database.session() as session:
                assert await session.scalar(select(func.count()).select_from(DatasetArtifact)) == 1

    run(exercise())


def test_unknown_control_name_is_not_executed_as_a_numerical_tool(migration_settings):
    async def exercise():
        async with application(migration_settings) as (_, store, worker, datasets):
            result = await StatisticsTools(datasets, worker=worker, artifacts=store).execute(
                context(owner()), proposal("finalize_statistics", uuid4())
            )
            assert result.status == "failed" and json.loads(result.summary) == {
                "error": "unknown_tool"
            }

    run(exercise())


def test_deleted_dataset_after_computation_cannot_publish_a_success_artifact(
    migration_settings, monkeypatch
):
    async def exercise():
        async with application(migration_settings) as (_, store, worker, datasets):
            principal = owner()
            value = await datasets.ingest(principal, filename="data.csv", content=CONTENT)
            original = worker.compute

            async def delete_after_compute(operation):
                computed = await original(operation)
                assert await datasets.delete(principal, value.dataset_id)
                return computed

            monkeypatch.setattr(worker, "compute", delete_after_compute)
            result = await StatisticsTools(datasets, worker=worker, artifacts=store).execute(
                context(principal), proposal("inspect_dataset", value.dataset_id)
            )
            assert result.status == "failed" and result.artifact is None
            assert json.loads(result.summary) == {"error": "dataset_not_found"}

    run(exercise())
