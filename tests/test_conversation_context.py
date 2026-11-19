"""Owned bounded context and historical citation mapping; model HTTP is mocked."""

import asyncio
import itertools
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import update

from statistics_agent.agent.composition import create_statistics_session
from statistics_agent.agent.context import ContextLimitError, ContextUnavailableError, build_context
from statistics_agent.agent.prompts import PROMPT
from statistics_agent.agent.provider import ModelAdapter
from statistics_agent.contracts.agent import AgentVersions
from statistics_agent.contracts.common import ConfigurationReference
from statistics_agent.contracts.conversations import (
    DatasetSelection,
    DatasetSnapshot,
    ResultProvenance,
    RunSubmission,
)
from statistics_agent.contracts.policy import ToolExecutionScope, ToolPolicyConfiguration
from statistics_agent.core.model_settings import ModelSettings
from statistics_agent.persistence.conversations import AdmissionConfiguration, ConversationStore
from statistics_agent.persistence.models import (
    ConversationMessageRecord,
    ConversationRecord,
    DatasetArtifact,
    RunRecord,
)
from statistics_agent.tools.statistics import StatisticsTools
from support.agent import Sink
from support.database import run
from support.datasets import CONTENT, application, context, owner, proposal

pytestmark = pytest.mark.postgres


def config():
    settings = ModelSettings(SERVICE_MODEL_API_KEY="synthetic-context-key", _env_file=None)
    ref = ConfigurationReference(config_id=uuid4(), version="context-test-v1")
    return settings, AdmissionConfiguration(
        versions=AgentVersions(
            provider=settings.resolve().reference, prompt=PROMPT.reference, toolset=ref, policy=ref
        ),
        policy=ToolPolicyConfiguration(
            reference=ref,
            allowed_tools=(
                "inspect_dataset",
                "describe_columns",
                "aggregate_statistics",
                "correlate_columns",
            ),
        ),
        profile=settings.resolve(),
        prompt=PROMPT,
    )


async def setup(
    database,
    artifacts,
    worker,
    datasets,
    *,
    previous="Earlier question",
    question="Explain the verified result.",
    with_prior=True,
    source_tool="describe_columns",
):
    actor = owner()
    store = ConversationStore(database)
    cid = (await store.create(actor, title="Analysis")).conversation_id
    content = b"price,y\n2,4\n4,8\n6,12" if source_tool == "correlate_columns" else CONTENT
    data = await datasets.ingest(actor, filename="private.csv", content=content)
    selected = DatasetSelection(dataset_ids=(data.dataset_id,))
    await store.set_attachments(actor, cid, selected)
    settings, configuration = config()
    original = await store.admit(
        actor,
        cid,
        RunSubmission(
            idempotency_key=uuid4(),
            question=previous,
            selection=selected,
        ),
        configuration,
    )
    arguments = {
        "inspect_dataset": {},
        "describe_columns": {"columns": ["price"], "missing_policy": "omit"},
        "aggregate_statistics": {"column": "price", "operation": "mean", "missing_policy": "omit"},
        "correlate_columns": {
            "x_column": "price",
            "y_column": "y",
            "method": "pearson",
            "missing_policy": "pairwise_complete",
        },
    }
    call = proposal(source_tool, data.dataset_id, **arguments[source_tool])
    computed = await StatisticsTools(datasets, worker=worker, artifacts=artifacts).execute(
        context(actor).model_copy(update={"run_id": original.run.run_id}), call
    )
    prior = ResultProvenance(
        conversation_id=cid,
        run_id=original.run.run_id,
        tool_call_id=call.tool_call_id,
        dataset=DatasetSnapshot(dataset_id=data.dataset_id, sha256=data.sha256),
        artifact=computed.artifact,
    )
    # Persisted real tool result, with synthetic worker completion; no runtime worker added.
    async with database.session() as session:
        await session.execute(
            update(RunRecord)
            .where(RunRecord.id == original.run.run_id)
            .values(
                lifecycle="finished",
                outcome="completed",
                finished_at=datetime.now(UTC),
                results=[prior.model_dump(mode="json")],
            )
        )
        await session.commit()
    admitted = await store.admit(
        actor,
        cid,
        RunSubmission(
            idempotency_key=uuid4(),
            question=question,
            selection=selected,
            prior_results=(prior,) if with_prior else (),
        ),
        configuration,
    )
    return actor, store, data, settings, configuration, admitted, prior


def session_for(
    actor,
    store,
    data,
    settings,
    configuration,
    admitted,
    prior,
    *,
    datasets,
    worker,
    artifacts,
    cite="alias",
):
    requests = []
    sequence = itertools.count(1).__next__
    sink = Sink()

    async def authorize(current):
        return current.principal == actor

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        supplied = json.loads(body["messages"][1]["content"])
        reference = (
            supplied["verified_prior_results"][0]["result_id"]
            if cite == "alias"
            else str(prior.tool_call_id)
        )
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "tool_calls": [
                                {
                                    "id": "synthetic-final",
                                    "type": "function",
                                    "function": {
                                        "name": "finalize_statistics",
                                        "arguments": json.dumps(
                                            {
                                                "status": "completed",
                                                "explanation": "Mean is 4.",
                                                "result_ids": [reference],
                                            }
                                        ),
                                    },
                                }
                            ],
                        },
                    }
                ]
            },
        )

    adapter = ModelAdapter(settings, sink, sequence, transport=httpx.MockTransport(handler))
    execution = context(actor).model_copy(update={"run_id": admitted.run.run_id})
    session = create_statistics_session(
        scope=ToolExecutionScope(
            context=execution, dataset_ids=tuple(item.dataset_id for item in admitted.datasets)
        ),
        configuration=configuration.policy,
        datasets=datasets,
        worker=worker,
        artifacts=artifacts,
        adapter=adapter,
        toolset=configuration.versions.toolset,
        authorize=authorize,
        sink=sink,
        sequence=sequence,
        deadline=datetime.now(UTC) + timedelta(minutes=1),
    )
    return session, requests, sink


@pytest.mark.parametrize("cite", ["alias", "raw_historical"])
def test_verified_prior_result_is_mapped_without_disabling_finalizer_provenance(
    migration_settings, monkeypatch, cite
):
    async def exercise():
        async with application(migration_settings) as (database, artifacts, worker, datasets):
            values = await setup(database, artifacts, worker, datasets)
            actor, store, data, settings, configuration, admitted, prior = values
            session, requests, sink = session_for(
                *values, datasets=datasets, worker=worker, artifacts=artifacts, cite=cite
            )

            async def forbidden(*args, **kwargs):
                pytest.fail("Context must never load CSV bytes or run numerical work")

            monkeypatch.setattr(datasets._store, "get_content", forbidden)
            monkeypatch.setattr(worker, "compute", forbidden)
            prepared = await session.prepare_context(store, datasets, artifacts)
            assert not requests and not sink.events
            supplied = json.loads(prepared.messages[1].content)
            assert supplied["question"] == admitted.submission.question
            assert supplied["unverified_history"] == [
                {"role": "user", "content": "Earlier question"}
            ]
            assert (
                supplied["verified_prior_results"][0]["summary"]["result"]["columns"][0]["metrics"][
                    "mean"
                ]
                == 4.0
            )
            mapped = prepared.historical[0]
            assert mapped.source == prior and mapped.result.tool_call_id != prior.tool_call_id
            result = await session.run(prepared.question, prepared.datasets, prepared=prepared)
            assert result.answer.status == ("completed" if cite == "alias" else "incomplete")
            assert result.results[0].tool_call_id == mapped.result.tool_call_id
            assert (
                prepared.reservation
                == len(
                    json.dumps(
                        requests[0], ensure_ascii=False, separators=(",", ":"), allow_nan=False
                    ).encode()
                )
                + 256
            )
            assert len(requests) == 1
            assert "synthetic-context-key" not in prepared.messages[1].content
            assert actor.issuer not in prepared.messages[1].content

    run(exercise())


def test_oversized_unicode_history_is_omitted_whole_and_current_question_is_kept(
    migration_settings,
):
    async def exercise():
        async with application(migration_settings) as (database, artifacts, worker, datasets):
            values = await setup(
                database,
                artifacts,
                worker,
                datasets,
                previous="\u00e9" * 2000,
                with_prior=False,
                question="Describe price.",
            )
            session, _, _ = session_for(
                *values, datasets=datasets, worker=worker, artifacts=artifacts
            )
            prepared = await session.prepare_context(values[1], datasets, artifacts)
            supplied = json.loads(prepared.messages[1].content)
            assert prepared.history_omitted and supplied["unverified_history"] == []
            assert supplied["question"] == "Describe price."
            assert prepared.reservation <= values[4].profile.input_token_budget

    run(exercise())


def test_required_question_cannot_be_silently_trimmed_to_fit(migration_settings):
    async def exercise():
        async with application(migration_settings) as (database, artifacts, worker, datasets):
            values = await setup(database, artifacts, worker, datasets, question="x" * 8192)
            session, requests, _ = session_for(
                *values, datasets=datasets, worker=worker, artifacts=artifacts
            )
            with pytest.raises(ContextLimitError):
                await session.prepare_context(values[1], datasets, artifacts)
            assert requests == []

    run(exercise())


@pytest.mark.parametrize(
    "scenario",
    [
        "deleted_dataset",
        "deleted_artifact",
        "corrupt_artifact",
        "changed_metadata",
        "foreign",
        "source_changed",
    ],
)
def test_stale_unowned_or_unverified_context_fails_before_model_dispatch(
    migration_settings, monkeypatch, scenario
):
    async def exercise():
        async with application(migration_settings) as (database, artifacts, worker, datasets):
            values = await setup(database, artifacts, worker, datasets)
            actor, store, data, _, _, admitted, prior = values
            if scenario == "foreign":
                values = (owner(subject="other", roles=["user", "admin"]), *values[1:])
            session, requests, _ = session_for(
                *values, datasets=datasets, worker=worker, artifacts=artifacts
            )
            if scenario == "deleted_dataset":
                await datasets.delete(actor, data.dataset_id)
            elif scenario == "deleted_artifact":
                await artifacts.delete_artifact(actor, prior.artifact.artifact_id)
            elif scenario == "changed_metadata":

                async def changed(*args, **kwargs):
                    return data.model_copy(update={"sha256": "0" * 64})

                monkeypatch.setattr(datasets, "get_metadata", changed)
            elif scenario in {"corrupt_artifact", "source_changed"}:
                async with database.session() as sql:
                    if scenario == "corrupt_artifact":
                        await sql.execute(
                            update(DatasetArtifact)
                            .where(DatasetArtifact.id == prior.artifact.artifact_id)
                            .values(
                                payload_json='{"private":"do-not-leak"}',
                                byte_count=len(b'{"private":"do-not-leak"}'),
                            )
                        )
                    else:
                        await sql.execute(
                            update(RunRecord)
                            .where(RunRecord.id == prior.run_id)
                            .values(outcome="incomplete", results=[])
                        )
                    await sql.commit()
            with pytest.raises(ContextUnavailableError) as error:
                await session.prepare_context(store, datasets, artifacts)
            assert str(error.value) == "Conversation context is unavailable"
            assert error.value.__context__ is None and requests == []

    run(exercise())


def test_exact_reservation_boundary_and_schema_cost_are_enforced(migration_settings):
    async def exercise():
        async with application(migration_settings) as (database, artifacts, worker, datasets):
            values = await setup(
                database, artifacts, worker, datasets, previous="old " * 1500, with_prior=False
            )
            session, _, _ = session_for(
                *values, datasets=datasets, worker=worker, artifacts=artifacts
            )
            prepared = await session.prepare_context(values[1], datasets, artifacts)
            arguments = dict(
                context=prepared.context,
                versions=prepared.versions,
                conversations=values[1],
                datasets=datasets,
                artifacts=artifacts,
                reserve=session._context_reservation,
            )
            exact = await build_context(**arguments, budget=prepared.reservation)
            assert exact.messages == prepared.messages
            with pytest.raises(ContextLimitError):
                await build_context(**arguments, budget=prepared.reservation - 1)
            assert prepared.reservation > sum(len(m.content.encode()) for m in prepared.messages)

    run(exercise())


@pytest.mark.parametrize("mismatch", ["question", "run", "version"])
def test_prepared_context_cannot_be_reused_with_other_inputs(migration_settings, mismatch):
    async def exercise():
        async with application(migration_settings) as (database, artifacts, worker, datasets):
            values = await setup(database, artifacts, worker, datasets)
            session, _, _ = session_for(
                *values, datasets=datasets, worker=worker, artifacts=artifacts
            )
            prepared = await session.prepare_context(values[1], datasets, artifacts)
            admitted = values[5]
            if mismatch == "run":
                admitted = replace(
                    admitted, run=admitted.run.model_copy(update={"run_id": uuid4()})
                )
            other_values = (*values[:5], admitted, values[6])
            other, requests, _ = session_for(
                *other_values, datasets=datasets, worker=worker, artifacts=artifacts
            )
            changed = prepared
            if mismatch == "question":
                changed = prepared.model_copy(update={"question": "Different question"})
            elif mismatch == "version":
                changed = prepared.model_copy(
                    update={
                        "versions": prepared.versions.model_copy(
                            update={
                                "policy": ConfigurationReference(
                                    config_id=uuid4(), version="other-v1"
                                )
                            }
                        )
                    }
                )
            result = await other.run(prepared.question, prepared.datasets, prepared=changed)
            assert result.answer.status == "incomplete" and not result.results and not requests

    run(exercise())


def test_recent_history_is_bounded_ordered_unverified_and_deterministic(migration_settings):
    async def exercise():
        async with application(migration_settings) as (database, artifacts, worker, datasets):
            values = await setup(database, artifacts, worker, datasets, with_prior=False)
            actor, store, _, _, _, admitted, prior = values
            async with database.session() as sql:
                await sql.execute(
                    update(ConversationMessageRecord)
                    .where(ConversationMessageRecord.run_id == admitted.run.run_id)
                    .values(sequence=20)
                )
                await sql.execute(
                    update(ConversationRecord)
                    .where(ConversationRecord.id == admitted.run.conversation_id)
                    .values(next_sequence=21)
                )
                sql.add_all(
                    ConversationMessageRecord(
                        id=uuid4(),
                        conversation_id=admitted.run.conversation_id,
                        run_id=prior.run_id,
                        sequence=i,
                        role="assistant",
                        content=f"Unverified turn {i}: mean is 9999",
                        created_at=datetime.now(UTC),
                    )
                    for i in range(2, 19)
                )
                await sql.commit()
            session, _, _ = session_for(
                *values, datasets=datasets, worker=worker, artifacts=artifacts
            )
            prepared = await session.prepare_context(store, datasets, artifacts)
            repeated = await session.prepare_context(store, datasets, artifacts)
            assert prepared == repeated and prepared.history_omitted
            history = json.loads(prepared.messages[1].content)["unverified_history"]
            assert 1 <= len(history) <= 12
            assert history[-1]["content"].startswith("Unverified turn 18:")
            assert all(item["role"] == "assistant" for item in history)
            assert not prepared.historical
            assert tuple(message.role for message in prepared.messages) == ("system", "user")
            assert await store.recent_messages(
                actor, admitted.run.conversation_id, before_run_id=admitted.run.run_id, limit=1
            )

    run(exercise())


@pytest.mark.parametrize("wide", [False, True])
def test_multiple_datasets_keep_ids_and_ambiguous_names_or_reject_required_wide_schema(
    migration_settings, wide
):
    async def exercise():
        async with application(migration_settings) as (database, artifacts, worker, datasets):
            values = await setup(database, artifacts, worker, datasets, with_prior=False)
            actor, store, first, settings, configuration, admitted, prior = values
            if wide:
                names = [f"column_{i}_" + "x" * 90 for i in range(64)]
                content = (",".join(names) + "\n" + ",".join("1" for _ in names)).encode()
            else:
                content = b"price\n9"
            second = await datasets.ingest(actor, filename="other.csv", content=content)
            selection = DatasetSelection(
                dataset_ids=(second.dataset_id,) if wide else (second.dataset_id, first.dataset_id)
            )
            await store.set_attachments(actor, admitted.run.conversation_id, selection)
            async with database.session() as sql:
                await sql.execute(
                    update(RunRecord)
                    .where(RunRecord.id == admitted.run.run_id)
                    .values(
                        lifecycle="finished", outcome="incomplete", finished_at=datetime.now(UTC)
                    )
                )
                await sql.commit()
            current = await store.admit(
                actor,
                admitted.run.conversation_id,
                RunSubmission(
                    idempotency_key=uuid4(),
                    question="Describe price.",
                    selection=selection,
                ),
                configuration,
            )
            session, requests, _ = session_for(
                actor,
                store,
                second,
                settings,
                configuration,
                current,
                prior,
                datasets=datasets,
                worker=worker,
                artifacts=artifacts,
            )
            if wide:
                with pytest.raises(ContextLimitError):
                    await session.prepare_context(store, datasets, artifacts)
            else:
                prepared = await session.prepare_context(store, datasets, artifacts)
                supplied = json.loads(prepared.messages[1].content)
                assert [item["dataset_id"] for item in supplied["datasets"]] == [
                    str(second.dataset_id),
                    str(first.dataset_id),
                ]
                assert all(item["columns"] == ["price"] for item in supplied["datasets"])
            assert not requests

    run(exercise())


def test_context_cancellation_propagates_without_provider_dispatch(migration_settings, monkeypatch):
    async def exercise():
        async with application(migration_settings) as (database, artifacts, worker, datasets):
            values = await setup(database, artifacts, worker, datasets)
            session, requests, _ = session_for(
                *values, datasets=datasets, worker=worker, artifacts=artifacts
            )
            entered = asyncio.Event()

            async def blocked(*args, **kwargs):
                entered.set()
                await asyncio.Event().wait()

            monkeypatch.setattr(values[1], "get_run", blocked)
            pending = asyncio.create_task(session.prepare_context(values[1], datasets, artifacts))
            await entered.wait()
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
            assert not requests

    run(exercise())


@pytest.mark.parametrize(
    "tool", ["inspect_dataset", "describe_columns", "aggregate_statistics", "correlate_columns"]
)
def test_all_four_historical_result_schemas_are_verified(migration_settings, tool):
    async def exercise():
        async with application(migration_settings) as (database, artifacts, worker, datasets):
            values = await setup(database, artifacts, worker, datasets, source_tool=tool)
            session, requests, _ = session_for(
                *values, datasets=datasets, worker=worker, artifacts=artifacts
            )
            prepared = await session.prepare_context(values[1], datasets, artifacts)
            assert len(prepared.historical) == 1 and prepared.historical[0].source == values[-1]
            assert len(prepared.historical[0].result.summary.encode()) <= 1024
            assert not requests

    run(exercise())
