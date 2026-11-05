"""Full controlled runner, real owned CSV/tools/PostgreSQL; mocked model only."""

import itertools
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import func, select

from statistics_agent.agent.prompts import FINALIZE
from statistics_agent.agent.provider import ModelAdapter
from statistics_agent.agent.statistics import StatisticsSession
from statistics_agent.contracts.agent import DatasetSummary, ToolDecision
from statistics_agent.contracts.common import ConfigurationReference
from statistics_agent.contracts.datasets import StatisticsLimits
from statistics_agent.core.model_settings import ModelSettings
from statistics_agent.persistence.models import DatasetArtifact
from statistics_agent.tools.statistics import StatisticsTools
from support.agent import Sink
from support.database import run
from support.datasets import application, context, owner

pytestmark = pytest.mark.postgres
DATA = b"x,y,g\n1,2,A\n2,4,A\n3,6,B"
OPERATIONS = [
    ("inspect_dataset", {}),
    ("describe_columns", {"columns": ["x"], "missing_policy": "omit"}),
    (
        "aggregate_statistics",
        {"column": "x", "operation": "sum", "group_by": "g", "missing_policy": "omit"},
    ),
    (
        "correlate_columns",
        {
            "x_column": "x",
            "y_column": "y",
            "method": "pearson",
            "missing_policy": "pairwise_complete",
        },
    ),
]


async def execute(
    datasets,
    store,
    worker,
    principal,
    selected,
    target,
    name,
    arguments,
    *,
    allow=True,
    invalid_final=False,
    raw_arguments=None,
    question="Compute this statistic.",
    result_limit=None,
    access=True,
):
    sink, requests, reservations, allowed, saved = Sink(), [], [], [], []
    sequence = itertools.count(1).__next__
    actor = context(principal)
    executor = StatisticsTools(
        datasets,
        worker=worker,
        artifacts=store,
        limits=StatisticsLimits(max_result_bytes=result_limit) if result_limit else None,
    )

    class Policy:
        async def evaluate(self, current, proposal):
            assert current == actor
            assert any(
                e.event_type.value == "tool_call.requested"
                and e.data["tool_call_id"] == str(proposal.tool_call_id)
                for e in sink.events
            )
            if allow:
                allowed.append(proposal.tool_call_id)
            return ToolDecision(
                tool_call_id=proposal.tool_call_id,
                allowed=allow,
                reason="allowed" if allow else "denied",
            )

    class ObservedExecutor:
        async def execute(self, current, proposal):
            assert proposal.tool_call_id in allowed
            result = await executor.execute(current, proposal)
            if result.status == "completed":
                artifact = await store.get_artifact(principal, result.artifact.artifact_id)
                assert artifact.payload["tool_call_id"] == str(proposal.tool_call_id)
                saved.append(result)
            return result

    class Adapter(ModelAdapter):
        def input_reservation(self, request, *, tools=None):
            value = super().input_reservation(request, tools=tools)
            reservations.append(value)
            return value

    def handler(request):
        data = json.loads(request.content)
        requests.append(data)
        names = {tool["function"]["name"] for tool in data["tools"]}
        assert names == {b.name for b in executor.bindings} | {FINALIZE}
        assert reservations[-1] == len(request.content) + 256
        observations = [message for message in data["messages"] if message["role"] == "tool"]
        if not observations:
            proposed_name = name
            params = (
                raw_arguments
                if raw_arguments is not None
                else json.dumps({"dataset_id": str(target)} | arguments)
            )
        else:
            assert len(saved) == 1  # Verified artifact is committed before explanation.
            proposed_name = FINALIZE
            params = json.dumps(
                {
                    "status": "completed",
                    "explanation": "Computed results are available.",
                    "result_ids": [str(uuid4())]
                    if invalid_final
                    else [json.loads(observations[0]["content"])["tool_call_id"]],
                    "assumptions": [],
                }
            )
        return httpx.Response(
            200,
            json={
                "model": "fixture-model",
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "tool_calls": [
                                {
                                    "id": "fixture-call-" + str(len(requests)),
                                    "type": "function",
                                    "function": {"name": proposed_name, "arguments": params},
                                }
                            ]
                        },
                    }
                ],
            },
        )

    async def authorize(current):
        return current == actor and access

    reference = ConfigurationReference(config_id=uuid4(), version="test-only")
    adapter = Adapter(
        ModelSettings(
            SERVICE_MODEL_API_KEY="synthetic-integration-key", LLM_MAX_RETRIES=0, _env_file=None
        ),
        sink,
        sequence,
        transport=httpx.MockTransport(handler),
    )
    session = StatisticsSession(
        context=actor,
        adapter=adapter,
        toolset=reference,
        policy_version=reference,
        bindings=executor.bindings,
        policy=Policy(),
        executor=ObservedExecutor(),
        authorize=authorize,
        sink=sink,
        sequence=sequence,
        deadline=datetime.now(UTC) + timedelta(minutes=1),
    )
    summaries = tuple(
        DatasetSummary(
            dataset_id=m.dataset_id, row_count=m.row_count, columns=tuple(c.name for c in m.columns)
        )
        for m in selected
    )
    result = await session.run(question, summaries)
    return result, requests, sink.events, reservations


@pytest.mark.parametrize("name,arguments", OPERATIONS)
def test_full_registry_executes_real_owned_tool_and_validates_finalization(
    migration_settings, name, arguments
):
    async def exercise():
        async with application(migration_settings) as (_, store, worker, datasets):
            principal = owner()
            metadata = await datasets.ingest(principal, filename="fixture.csv", content=DATA)
            result, requests, events, reservations = await execute(
                datasets, store, worker, principal, [metadata], metadata.dataset_id, name, arguments
            )
            assert result.answer.status == "completed", (result.terminal_reason, reservations)
            assert result.state.completed_attempts == len(requests) == 2
            assert result.answer.result_ids == (result.results[0].tool_call_id,)
            assert len(result.results) == 1 and len(result.state.artifacts) == 1
            assert max(reservations) <= 4096
            assert [e.sequence for e in events] == list(range(1, len(events) + 1))
            assert events[-1].data["reason"] == "answer_validated"
            artifact = await store.get_artifact(principal, result.results[0].artifact.artifact_id)
            output = artifact.payload["result"]
            observation = next(m for m in requests[-1]["messages"] if m["role"] == "tool")
            seen = json.loads(observation["content"])["summary"]["result"]
            omitted = {
                "dataset_id",
                "backend",
                "backend_version",
                "numpy_version",
                "scipy_version",
                "dtype",
                "column",
                "operation",
                "group_by",
                "x_column",
                "y_column",
            }
            assert seen == {k: v for k, v in output.items() if k not in omitted}
            if name == "describe_columns":
                assert output["columns"][0]["metrics"]["mean"] == 2.0
            if name == "aggregate_statistics":
                assert [g["value"] for g in output["groups"]] == [3.0, 3.0]
            if name == "correlate_columns":
                assert output["coefficient"] == pytest.approx(1.0)
            assert str(principal.auth_session_ref) not in json.dumps(requests)
            assert "synthetic-integration-key" not in json.dumps(requests)
            # Current telemetry must not suddenly include numerical results/raw CSV.
            assert "population_std" not in json.dumps([e.model_dump(mode="json") for e in events])

    run(exercise())


@pytest.mark.parametrize(
    "options",
    [
        {"allow": False},
        {"access": False},
        {"raw_arguments": "not-json"},
        {"result_limit": 1},
    ],
)
def test_denied_malformed_or_oversized_work_cannot_create_success_artifacts(
    migration_settings, options
):
    async def exercise():
        async with application(migration_settings) as (database, store, worker, datasets):
            principal = owner()
            metadata = await datasets.ingest(principal, filename="fixture.csv", content=DATA)
            result, requests, events, _ = await execute(
                datasets,
                store,
                worker,
                principal,
                [metadata],
                metadata.dataset_id,
                "inspect_dataset",
                {},
                **options,
            )
            assert result.answer.status == "incomplete"
            assert not result.state.artifacts
            async with database.session() as session:
                assert await session.scalar(select(func.count()).select_from(DatasetArtifact)) == 0
            assert len(requests) <= 1

    run(exercise())


def test_invalid_final_explanation_preserves_previously_verified_results(migration_settings):
    async def exercise():
        async with application(migration_settings) as (_, store, worker, datasets):
            principal = owner()
            metadata = await datasets.ingest(principal, filename="fixture.csv", content=DATA)
            result, requests, events, _ = await execute(
                datasets,
                store,
                worker,
                principal,
                [metadata],
                metadata.dataset_id,
                "inspect_dataset",
                {},
                invalid_final=True,
            )
            assert result.answer.status == "incomplete" and len(requests) == 2
            assert len(result.results) == 1 and result.results[0].status == "completed"
            assert await store.get_artifact(principal, result.results[0].artifact.artifact_id)
            assert events[-1].data["reason"] == "invalid_final_answer"

    run(exercise())


def test_large_question_stops_before_http_dispatch_without_raising_input_cap(migration_settings):
    async def exercise():
        async with application(migration_settings) as (_, store, worker, datasets):
            principal = owner()
            metadata = await datasets.ingest(principal, filename="fixture.csv", content=DATA)
            result, requests, events, _ = await execute(
                datasets,
                store,
                worker,
                principal,
                [metadata],
                metadata.dataset_id,
                "inspect_dataset",
                {},
                question="x" * 5000,
            )
            assert result.terminal_reason == "request_input_limit" and not requests
            assert not result.results and not events

    run(exercise())


@pytest.mark.parametrize("state", ["unowned", "deleted", "missing"])
def test_unavailable_dataset_cannot_be_loaded_through_agent(migration_settings, state):
    async def exercise():
        async with application(migration_settings) as (_, store, worker, datasets):
            principal = owner()
            metadata = await datasets.ingest(principal, filename="fixture.csv", content=DATA)
            if state == "unowned":
                principal = owner(subject="another-user")
            elif state == "deleted":
                assert await datasets.delete(principal, metadata.dataset_id)
            target = uuid4() if state == "missing" else metadata.dataset_id
            result, requests, events, _ = await execute(
                datasets, store, worker, principal, [], target, "inspect_dataset", {}
            )
            assert result.answer.status == "incomplete" and len(requests) == 1
            assert not result.state.artifacts
            assert result.results[0].status == "failed"
            assert json.loads(result.results[0].summary) == {"error": "dataset_not_found"}
            assert events[-1].event_type.value == "tool_call.failed"

    run(exercise())


@pytest.mark.parametrize(
    "name,arguments,data,code",
    [
        ("not_a_tool", {}, DATA, "unknown_tool"),
        (
            "describe_columns",
            {"columns": ["x", "x"], "missing_policy": "omit"},
            DATA,
            "invalid_arguments",
        ),
        (
            "describe_columns",
            {"columns": ["x"], "missing_policy": "omit"},
            b"x\ntext",
            "non_numeric_column",
        ),
        (
            "describe_columns",
            {"columns": ["x"], "missing_policy": "error"},
            b'x\n1\n""',
            "missing_values",
        ),
        (
            "describe_columns",
            {"columns": ["x"], "missing_policy": "omit"},
            b"x\n1e308\n1e308",
            "numeric_range_exceeded",
        ),
    ],
)
def test_invalid_or_numerically_failed_agent_work_never_becomes_a_verified_result(
    migration_settings, name, arguments, data, code
):
    async def exercise():
        async with application(migration_settings) as (_, store, worker, datasets):
            principal = owner()
            metadata = await datasets.ingest(principal, filename="fixture.csv", content=data)
            result, requests, events, _ = await execute(
                datasets, store, worker, principal, [metadata], metadata.dataset_id, name, arguments
            )
            assert result.answer.status == "incomplete" and len(requests) == 1
            assert not result.state.artifacts
            if result.results:
                assert result.results[0].status == "failed"
                assert json.loads(result.results[0].summary) == {"error": code}
            assert events[-1].event_type.value == "tool_call.failed"
            if code in {"unknown_tool", "invalid_arguments"}:
                assert events[-1].data["reason"] == code

    run(exercise())


def test_separate_owned_datasets_produce_distinct_results_without_joins(migration_settings):
    async def exercise():
        async with application(migration_settings) as (_, store, worker, datasets):
            principal = owner()
            metadata = [
                await datasets.ingest(principal, filename="fixture.csv", content=data)
                for data in [b"x\n1\n2\n3", b"x\n10\n20\n30"]
            ]
            observed = []
            for target in metadata:
                result, requests, _, _ = await execute(
                    datasets,
                    store,
                    worker,
                    principal,
                    metadata,
                    target.dataset_id,
                    "describe_columns",
                    {"columns": ["x"], "missing_policy": "omit"},
                )
                assert result.answer.status == "completed" and len(requests) == 2
                artifact = await store.get_artifact(
                    principal, result.results[0].artifact.artifact_id
                )
                assert artifact.payload["result"]["dataset_id"] == str(target.dataset_id)
                observed.append(artifact.payload["result"]["columns"][0]["metrics"]["mean"])
            assert observed == [2.0, 20.0]

    run(exercise())


def test_accumulated_input_limit_preserves_real_artifact_and_skips_next_model_call(
    migration_settings,
):
    async def exercise():
        async with application(migration_settings) as (_, store, worker, datasets):
            principal = owner()
            metadata = await datasets.ingest(principal, filename="fixture.csv", content=DATA)
            result, requests, _, reservations = await execute(
                datasets,
                store,
                worker,
                principal,
                [metadata],
                metadata.dataset_id,
                "inspect_dataset",
                {},
                question="q" * 600,
            )
            assert result.terminal_reason == "request_input_limit"
            assert result.answer.status == "incomplete" and len(requests) == 1
            assert reservations[0] <= 4096 < reservations[-1]
            assert len(result.results) == 1 and result.results[0].status == "completed"
            assert await store.get_artifact(principal, result.results[0].artifact.artifact_id)

    run(exercise())
