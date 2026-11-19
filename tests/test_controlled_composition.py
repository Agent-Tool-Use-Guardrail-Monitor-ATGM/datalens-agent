"""Real composition, policy, CSV and artifacts; only the model API is mocked."""

import itertools
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import func, select

from statistics_agent.agent.composition import create_statistics_session
from statistics_agent.agent.prompts import FINALIZE
from statistics_agent.agent.provider import ModelAdapter
from statistics_agent.contracts.agent import DatasetSummary
from statistics_agent.contracts.common import ConfigurationReference
from statistics_agent.contracts.datasets import DatasetLimits, StatisticsLimits
from statistics_agent.contracts.policy import (
    PolicyEvaluation,
    ToolExecutionScope,
    ToolPolicyConfiguration,
)
from statistics_agent.core.model_settings import ModelSettings
from statistics_agent.persistence.models import DatasetArtifact
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
        {
            "column": "x",
            "operation": "sum",
            "group_by": "g",
            "missing_policy": "omit",
        },
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
    actor,
    selected,
    target,
    name,
    arguments,
    *,
    configuration=None,
    raw=None,
    invalid_final=False,
    revoke_on_final=False,
    scope_ids=None,
):
    reference = ConfigurationReference(config_id=uuid4(), version="integration-v1")
    configuration = configuration or ToolPolicyConfiguration(
        reference=reference, allowed_tools=tuple(name for name, _ in OPERATIONS)
    )
    sink, requests = Sink(), []
    sequence = itertools.count(1).__next__
    access = True

    async def authorize(current):
        assert current == actor
        return access

    def handler(request):
        nonlocal access
        body = json.loads(request.content)
        requests.append(body)
        observations = [m for m in body["messages"] if m["role"] == "tool"]
        if observations:
            access = not revoke_on_final
            call_name = FINALIZE
            params = json.dumps(
                {
                    "status": "completed",
                    "explanation": "Verified results are available.",
                    "result_ids": [str(uuid4())]
                    if invalid_final
                    else [json.loads(observations[-1]["content"])["tool_call_id"]],
                }
            )
        else:
            call_name = name
            params = raw if raw is not None else json.dumps({"dataset_id": str(target)} | arguments)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "tool_calls": [
                                {
                                    "id": "synthetic-" + str(len(requests)),
                                    "type": "function",
                                    "function": {"name": call_name, "arguments": params},
                                }
                            ]
                        },
                    }
                ],
            },
        )

    adapter = ModelAdapter(
        ModelSettings(SERVICE_MODEL_API_KEY="synthetic-composition-key", _env_file=None),
        sink,
        sequence,
        transport=httpx.MockTransport(handler),
    )
    session = create_statistics_session(
        scope=ToolExecutionScope(
            context=actor,
            dataset_ids=tuple(m.dataset_id for m in selected) if scope_ids is None else scope_ids,
        ),
        configuration=configuration,
        datasets=datasets,
        worker=worker,
        artifacts=store,
        adapter=adapter,
        toolset=reference,
        authorize=authorize,
        sink=sink,
        sequence=sequence,
        deadline=datetime.now(UTC) + timedelta(minutes=1),
    )
    result = await session.run(
        "Compute the requested statistic.",
        tuple(
            DatasetSummary(
                dataset_id=m.dataset_id,
                row_count=m.row_count,
                columns=tuple(c.name for c in m.columns),
            )
            for m in selected
        ),
    )
    assert not session.agent.tools and not session.agent.allow_delegation
    assert result.state.versions.policy == configuration.reference
    return result, sink.events, requests, configuration


def verdicts(events):
    return [
        PolicyEvaluation.model_validate(e.data["policy_evaluation"])
        for e in events
        if "policy_evaluation" in e.data
    ]


async def artifact_count(database):
    async with database.session() as session:
        return await session.scalar(select(func.count()).select_from(DatasetArtifact))


@pytest.mark.parametrize("name,arguments", OPERATIONS)
def test_real_composition_executes_and_finalizes_without_native_executor(
    migration_settings, name, arguments
):
    async def exercise():
        async with application(migration_settings) as (_, store, worker, datasets):
            actor = context(owner())
            value = await datasets.ingest(actor.principal, filename="fixture.csv", content=DATA)
            result, events, requests, config = await execute(
                datasets, store, worker, actor, [value], value.dataset_id, name, arguments
            )
            assert result.answer.status == "completed" and len(result.results) == 1
            artifact = await store.get_artifact(
                actor.principal, result.results[0].artifact.artifact_id
            )
            assert artifact.payload["tool"] == name
            evaluations = verdicts(events)
            assert len(evaluations) == 1 and evaluations[0].status == "allowed"
            assert evaluations[0].policy == config.reference
            assert evaluations[0].tool_call_id == result.results[0].tool_call_id
            requested = next(
                i for i, e in enumerate(events) if e.event_type == "tool_call.requested"
            )
            verdict = next(i for i, e in enumerate(events) if "policy_evaluation" in e.data)
            assert requested < verdict
            final = next(e for e in events if e.data.get("reason") == "answer_validated")
            assert "policy_evaluation" not in final.data
            assert len(requests) == 2
            encoded = json.dumps([e.model_dump(mode="json") for e in events])
            assert "synthetic-composition-key" not in encoded
            assert actor.principal.subject not in encoded

    run(exercise())


@pytest.mark.parametrize(
    "scenario",
    [
        "disabled",
        "unselected",
        "foreign",
        "unknown",
        "malformed",
        "extra",
        "unavailable",
    ],
)
def test_real_composition_denies_without_computation_or_success_artifact(
    migration_settings, monkeypatch, scenario
):
    async def exercise():
        async with application(migration_settings) as (database, store, worker, datasets):
            actor = context(owner())
            value = await datasets.ingest(actor.principal, filename="fixture.csv", content=DATA)
            target = value
            if scenario in {"unselected", "foreign"}:
                target = await datasets.ingest(
                    owner(subject="another-user") if scenario == "foreign" else actor.principal,
                    filename="other.csv",
                    content=DATA,
                )
            selected = [value]

            async def forbidden(*args, **kwargs):
                pytest.fail("Denied proposal reached deterministic execution")

            monkeypatch.setattr(worker, "parse", forbidden)
            monkeypatch.setattr(worker, "compute", forbidden)
            if scenario == "unavailable":

                async def unavailable(*args, **kwargs):
                    raise RuntimeError("private-database-detail")

                monkeypatch.setattr(datasets, "get_metadata", unavailable)
            reference = ConfigurationReference(config_id=uuid4(), version="denial-v1")
            config = ToolPolicyConfiguration(
                reference=reference,
                allowed_tools=() if scenario == "disabled" else ("inspect_dataset",),
            )
            raw = (
                "not-json"
                if scenario == "malformed"
                else (
                    json.dumps({"dataset_id": str(target.dataset_id), "code": "private-argument"})
                    if scenario == "extra"
                    else None
                )
            )
            result, events, requests, _ = await execute(
                datasets,
                store,
                worker,
                actor,
                selected,
                target.dataset_id,
                "unknown_operation" if scenario == "unknown" else "inspect_dataset",
                {},
                configuration=config,
                raw=raw,
                scope_ids=(target.dataset_id,) if scenario == "foreign" else None,
            )
            assert result.answer.status == "incomplete" and not result.results
            assert await artifact_count(database) == 0 and len(requests) == 1
            (evaluation,) = verdicts(events)
            expected = (
                "unavailable"
                if scenario == "unavailable"
                else (
                    "not_evaluated" if scenario in {"unknown", "malformed", "extra"} else "denied"
                )
            )
            assert evaluation.status == expected and evaluation.policy == reference
            encoded = json.dumps([e.model_dump(mode="json") for e in events])
            assert "private-database-detail" not in encoded + result.answer.explanation
            assert "private-argument" not in encoded

    run(exercise())


@pytest.mark.parametrize("limit", ["dataset", "groups", "result"])
def test_composition_uses_admitted_limits_after_policy_allow(migration_settings, limit):
    async def exercise():
        async with application(migration_settings) as (database, store, worker, datasets):
            actor = context(owner())
            value = await datasets.ingest(actor.principal, filename="fixture.csv", content=DATA)
            config = ToolPolicyConfiguration(
                reference=ConfigurationReference(config_id=uuid4(), version="bounded-v1"),
                allowed_tools=("aggregate_statistics",),
                dataset_limits=DatasetLimits(max_rows=1) if limit == "dataset" else DatasetLimits(),
                statistics_limits=StatisticsLimits(
                    max_groups=1 if limit == "groups" else 50,
                    max_result_bytes=1 if limit == "result" else 16_384,
                ),
            )
            result, events, _, _ = await execute(
                datasets,
                store,
                worker,
                actor,
                [value],
                value.dataset_id,
                OPERATIONS[2][0],
                OPERATIONS[2][1],
                configuration=config,
            )
            assert verdicts(events)[0].status == "allowed"
            assert result.answer.status == "incomplete" and result.results[0].status == "failed"
            assert await artifact_count(database) == 0

    run(exercise())


@pytest.mark.parametrize("scenario", ["bad_reference", "access_removed"])
def test_finalizer_keeps_control_guards_and_verified_partial_result(migration_settings, scenario):
    async def exercise():
        async with application(migration_settings) as (database, store, worker, datasets):
            actor = context(owner())
            value = await datasets.ingest(actor.principal, filename="fixture.csv", content=DATA)
            result, events, _, _ = await execute(
                datasets,
                store,
                worker,
                actor,
                [value],
                value.dataset_id,
                "inspect_dataset",
                {},
                invalid_final=scenario == "bad_reference",
                revoke_on_final=scenario == "access_removed",
            )
            assert result.answer.status == "incomplete" and len(result.results) == 1
            assert await artifact_count(database) == 1
            assert len(verdicts(events)) == 1
            expected = "invalid_final_answer" if scenario == "bad_reference" else "access_denied"
            final = next(e for e in events if e.data.get("reason") == expected)
            assert "policy_evaluation" not in final.data

    run(exercise())
