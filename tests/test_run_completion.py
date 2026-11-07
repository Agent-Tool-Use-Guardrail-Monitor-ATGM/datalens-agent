"""Real tools and PostgreSQL terminal writes; deterministic provider responses."""

import asyncio
import itertools
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4, uuid5

import httpx
import pytest
from sqlalchemy import event, select, text, update

from statistics_agent.agent.composition import create_statistics_session
from statistics_agent.agent.prompts import PROMPT
from statistics_agent.agent.provider import ModelAdapter
from statistics_agent.contracts.agent import AgentVersions, StatisticsAnswer
from statistics_agent.contracts.conversations import (
    ConversationRun,
    ConversationRunV2,
    DatasetSelection,
    RunSubmission,
)
from statistics_agent.contracts.policy import ToolExecutionScope, ToolPolicyConfiguration
from statistics_agent.core.model_settings import ModelProfile, ModelSettings
from statistics_agent.persistence.completions import finish_run
from statistics_agent.persistence.conversations import (
    AdmissionConfiguration,
    ConversationNotFoundError,
    ConversationStore,
    RunInputUnavailableError,
)
from statistics_agent.persistence.models import DatasetArtifact, RunJob, RunRecord
from statistics_agent.persistence.schema import migration_config
from support.agent import Sink
from support.database import apply, run
from support.datasets import CONTENT, application, context, owner

pytestmark = pytest.mark.postgres


def call(name, arguments, identity="call"):
    return httpx.Response(
        200,
        json={
            "choices": [
                {
                    "finish_reason": "tool_calls",
                    "message": {
                        "tool_calls": [
                            {
                                "id": identity,
                                "type": "function",
                                "function": {
                                    "name": name,
                                    "arguments": json.dumps(arguments),
                                },
                            }
                        ],
                    },
                }
            ]
        },
    )


async def setup(database, datasets):
    actor, store = owner(), ConversationStore(database)
    cid = (await store.create(actor, title="Analysis")).conversation_id
    data = await datasets.ingest(actor, filename="input.csv", content=CONTENT)
    selected = DatasetSelection(dataset_ids=(data.dataset_id,))
    await store.set_attachments(actor, cid, selected)
    settings = ModelSettings(SERVICE_MODEL_API_KEY="synthetic-completion-key", _env_file=None)
    reference = {"config_id": uuid4(), "version": "completion-test-v1"}
    configuration = AdmissionConfiguration(
        versions=AgentVersions(
            provider=settings.resolve().reference,
            prompt=PROMPT.reference,
            policy=reference,
            toolset=reference,
        ),
        prompt=PROMPT,
        profile=settings.resolve(),
        policy=ToolPolicyConfiguration(
            reference=reference,
            allowed_tools=(
                "inspect_dataset",
                "describe_columns",
                "aggregate_statistics",
                "correlate_columns",
            ),
        ),
    )
    return actor, store, cid, data, selected, settings, configuration


async def execute(values, admission, datasets, worker, artifacts, handler):
    actor, store, _, _, _, settings, configuration = values
    sink, sequence = Sink(), itertools.count(1).__next__
    adapter = ModelAdapter(settings, sink, sequence, transport=httpx.MockTransport(handler))

    async def authorize(current):
        return current.principal == actor

    session = create_statistics_session(
        scope=ToolExecutionScope(
            context=context(actor).model_copy(update={"run_id": admission.run.run_id}),
            dataset_ids=tuple(item.dataset_id for item in admission.datasets),
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
    prepared = await session.prepare_context(store, datasets, artifacts)
    execution = await session.run(prepared.question, prepared.datasets, prepared=prepared)
    return execution, prepared


def calculation(request):
    body = json.loads(request.content)
    observations = [item for item in body["messages"] if item["role"] == "tool"]
    if not observations:
        data = json.loads(body["messages"][1]["content"])["datasets"][0]
        return call(
            "describe_columns",
            {"dataset_id": data["dataset_id"], "columns": ["price"], "missing_policy": "omit"},
        )
    return call(
        "finalize_statistics",
        {
            "status": "completed",
            "explanation": "Computed mean is 4.",
            "result_ids": [json.loads(observations[0]["content"])["tool_call_id"]],
        },
    )


def test_clarification_persists_releases_busy_and_reply_starts_new_run(migration_settings):
    async def exercise():
        async with application(migration_settings) as (db, artifacts, worker, datasets):
            values = await setup(db, datasets)
            actor, store, cid, _, selected, _, configuration = values
            first = await store.admit(
                actor,
                cid,
                RunSubmission(idempotency_key=uuid4(), question="Analyze"),
                configuration,
            )
            execution, _ = await execute(
                values,
                first,
                datasets,
                worker,
                artifacts,
                lambda _: call("request_clarification", {"question": "Which dataset?"}),
            )
            finished = await finish_run(db, execution)
            assert finished.run.schema_version == "conversation-run-v2"
            assert finished.run.lifecycle == "finished" and finished.run.outcome == "needs_input"
            assert finished.run.clarification.question == "Which dataset?"
            assert isinstance(finished.run, ConversationRunV2)
            assert (await store.get_run(actor, first.run.run_id)).run == finished.run
            assert [item.role for item in await store.messages(actor, cid)] == ["user", "assistant"]
            async with db.session() as session:
                assert await session.get(RunJob, first.run.run_id) is None
            reply = await store.admit(
                actor,
                cid,
                RunSubmission(idempotency_key=uuid4(), question="Mean price.", selection=selected),
                configuration,
            )
            second, prepared = await execute(
                values, reply, datasets, worker, artifacts, calculation
            )
            assert "Which dataset?" in prepared.messages[1].content
            result = await finish_run(db, second)
            assert result.run.outcome == "completed" and len(result.run.results) == 1
            assert reply.run.run_id != first.run.run_id

    run(exercise())


@pytest.mark.parametrize("history_only", [False, True])
def test_computed_and_imported_results_preserve_original_identity(migration_settings, history_only):
    async def exercise():
        async with application(migration_settings) as (db, artifacts, worker, datasets):
            values = await setup(db, datasets)
            actor, store, cid, _, selected, _, configuration = values
            first = await store.admit(
                actor,
                cid,
                RunSubmission(idempotency_key=uuid4(), question="Mean?", selection=selected),
                configuration,
            )
            execution, _ = await execute(values, first, datasets, worker, artifacts, calculation)
            completed = await finish_run(db, execution)
            source = completed.run.results[0]
            artifact = await artifacts.get_artifact(actor, source.artifact.artifact_id)
            assert artifact.payload["tool_call_id"] == str(source.tool_call_id)
            if not history_only:
                assert completed.run.completion.imported_results == ()
                return
            second = await store.admit(
                actor,
                cid,
                RunSubmission(
                    idempotency_key=uuid4(),
                    question="Explain again",
                    prior_results=(source,),
                ),
                configuration,
            )

            def historical(request):
                supplied = json.loads(json.loads(request.content)["messages"][1]["content"])
                return call(
                    "finalize_statistics",
                    {
                        "status": "completed",
                        "explanation": "Previous mean is 4.",
                        "result_ids": [supplied["verified_prior_results"][0]["result_id"]],
                    },
                )

            followup, _ = await execute(values, second, datasets, worker, artifacts, historical)
            stored = await finish_run(db, followup)
            assert stored.run.outcome == "completed" and not stored.run.results
            mapping = stored.run.completion.imported_results[0]
            assert mapping.source == source
            assert mapping.result_id == uuid5(second.run.run_id, str(source.tool_call_id))
            assert stored.run.completion.answer.result_ids == (mapping.result_id,)
            assert (await artifacts.get_artifact(actor, source.artifact.artifact_id)) == artifact
            assert (await store.get_run(actor, second.run.run_id)).run == stored.run
            # The original v1 guard still rejects a completed run without new results.
            with pytest.raises(ValueError):
                ConversationRun(**stored.run.model_dump(exclude={"schema_version", "completion"}))

    run(exercise())


def test_invalid_explanation_keeps_real_persisted_partial_results(migration_settings):
    async def exercise():
        async with application(migration_settings) as (db, artifacts, worker, datasets):
            values = await setup(db, datasets)
            actor, store, cid, _, selected, _, configuration = values
            admitted = await store.admit(
                actor,
                cid,
                RunSubmission(idempotency_key=uuid4(), question="Mean?", selection=selected),
                configuration,
            )
            count = 0

            def handler(request):
                nonlocal count
                count += 1
                if count == 1:
                    return calculation(request)
                return call(
                    "finalize_statistics",
                    {
                        "status": "completed",
                        "explanation": "Invented",
                        "result_ids": [str(uuid4())],
                    },
                )

            execution, _ = await execute(values, admitted, datasets, worker, artifacts, handler)
            assert execution.answer.status == "incomplete"
            # Numerical artifact already exists before terminal explanation is written.
            assert (
                await artifacts.get_artifact(actor, execution.results[0].artifact.artifact_id)
            ).payload["result"]["columns"][0]["metrics"]["mean"] == 4.0
            finished = await finish_run(db, execution)
            assert finished.run.outcome == "incomplete" and len(finished.run.results) == 1
            assert finished.run.completion.answer.explanation != "Invented"
            assert "incomplete" == (await store.get_run(actor, admitted.run.run_id)).run.outcome

    run(exercise())


@pytest.mark.parametrize(
    "damage", ["digest", "body_identity", "deleted", "foreign", "version", "citation"]
)
def test_invalid_completion_cannot_write_terminal_history(migration_settings, damage):
    async def exercise():
        async with application(migration_settings) as (db, artifacts, worker, datasets):
            values = await setup(db, datasets)
            actor, store, cid, data, selected, _, configuration = values
            admitted = await store.admit(
                actor,
                cid,
                RunSubmission(idempotency_key=uuid4(), question="Mean?", selection=selected),
                configuration,
            )
            execution, _ = await execute(values, admitted, datasets, worker, artifacts, calculation)
            if damage in {"digest", "body_identity"}:
                import hashlib

                artifact = await artifacts.get_artifact(
                    actor, execution.results[0].artifact.artifact_id
                )
                payload = dict(artifact.payload)
                payload["tool_call_id"] = str(uuid4())
                body = json.dumps(payload)
                updates = {"payload_json": body, "byte_count": len(body.encode())}
                if damage == "body_identity":
                    digest = hashlib.sha256(body.encode()).hexdigest()
                    updates["sha256"] = digest
                    reference = execution.results[0].artifact.model_copy(update={"sha256": digest})
                    execution = execution.model_copy(
                        update={
                            "results": (
                                execution.results[0].model_copy(update={"artifact": reference}),
                            )
                        }
                    )
                async with db.session() as session:
                    await session.execute(
                        update(DatasetArtifact)
                        .where(DatasetArtifact.id == artifact.reference.artifact_id)
                        .values(**updates)
                    )
                    await session.commit()
            elif damage == "deleted":
                await artifacts.delete(actor, data.dataset_id)
            elif damage == "foreign":
                execution = execution.model_copy(
                    update={
                        "state": execution.state.model_copy(
                            update={
                                "context": execution.state.context.model_copy(
                                    update={"principal": owner(subject="other")}
                                )
                            }
                        )
                    }
                )
            elif damage == "version":
                execution = execution.model_copy(
                    update={
                        "state": execution.state.model_copy(
                            update={
                                "versions": execution.state.versions.model_copy(
                                    update={"toolset": {"config_id": uuid4(), "version": "wrong"}}
                                )
                            }
                        )
                    }
                )
            else:
                execution = execution.model_copy(
                    update={
                        "answer": StatisticsAnswer(
                            status="completed", explanation="Fake", result_ids=(uuid4(),)
                        )
                    }
                )
            with pytest.raises((RunInputUnavailableError, ConversationNotFoundError)):
                await finish_run(db, execution)
            assert (await store.get_run(actor, admitted.run.run_id)).run.lifecycle == "queued"
            assert len(await store.messages(actor, cid)) == 1

    run(exercise())


def test_terminal_write_failure_rolls_back_message_result_and_busy_release(migration_settings):
    async def exercise():
        async with application(migration_settings) as (db, artifacts, worker, datasets):
            values = await setup(db, datasets)
            actor, store, cid, _, selected, _, configuration = values
            admitted = await store.admit(
                actor,
                cid,
                RunSubmission(idempotency_key=uuid4(), question="Mean?", selection=selected),
                configuration,
            )
            execution, _ = await execute(values, admitted, datasets, worker, artifacts, calculation)

            def fail(connection, cursor, statement, parameters, context, executemany):
                if statement.startswith("INSERT INTO agent_messages"):
                    raise RuntimeError("synthetic storage failure")

            event.listen(db.engine.sync_engine, "before_cursor_execute", fail)
            try:
                with pytest.raises(RuntimeError, match="synthetic storage failure"):
                    await finish_run(db, execution)
            finally:
                event.remove(db.engine.sync_engine, "before_cursor_execute", fail)
            assert (await store.get_run(actor, admitted.run.run_id)).run.lifecycle == "queued"
            assert len(await store.messages(actor, cid)) == 1
            async with db.session() as session:
                assert (
                    await session.scalar(
                        select(RunJob.run_id).where(RunJob.run_id == admitted.run.run_id)
                    )
                    == admitted.run.run_id
                )
            assert (await finish_run(db, execution)).run.outcome == "completed"

    run(exercise())


def test_two_terminal_writers_cannot_duplicate_history(migration_settings):
    async def exercise():
        async with application(migration_settings) as (db, artifacts, worker, datasets):
            values = await setup(db, datasets)
            actor, store, cid, _, selected, _, configuration = values
            admitted = await store.admit(
                actor,
                cid,
                RunSubmission(idempotency_key=uuid4(), question="Mean?", selection=selected),
                configuration,
            )
            execution, _ = await execute(values, admitted, datasets, worker, artifacts, calculation)
            results = await asyncio.gather(
                finish_run(db, execution),
                finish_run(db, execution),
                return_exceptions=True,
            )
            assert sum(isinstance(item, RunInputUnavailableError) for item in results) == 1
            assert len(await store.messages(actor, cid)) == 2
            assert (await store.get_run(actor, admitted.run.run_id)).run.outcome == "completed"

    run(exercise())


def test_migration_retains_v1_data_and_old_profile_snapshot(migration_settings):
    async def exercise():
        async with application(migration_settings) as (db, artifacts, worker, datasets):
            values = await setup(db, datasets)
            actor, store, cid, data, selected, _, configuration = values
            old_profile = ModelProfile.model_validate(
                configuration.profile.model_dump()
                | {
                    "configuration_version": "model-profile-v3",
                    "input_token_budget": 4096,
                }
            )
            old_config = configuration.model_copy(
                update={
                    "profile": old_profile,
                    "versions": configuration.versions.model_copy(
                        update={"provider": old_profile.reference}
                    ),
                }
            )
            first = await store.admit(
                actor,
                cid,
                RunSubmission(idempotency_key=uuid4(), question="Old run"),
                old_config,
            )
            # Legacy writer fixture, before v2 completions existed.
            async with db.session() as session:
                await session.execute(
                    update(RunRecord)
                    .where(RunRecord.id == first.run.run_id)
                    .values(
                        lifecycle="finished",
                        outcome="unsupported",
                        finished_at=datetime.now(UTC),
                    )
                )
                schema = await session.scalar(text("SELECT current_schema()"))
                await session.commit()
            old = await store.get_run(actor, first.run.run_id)
            second = await store.admit(
                actor,
                cid,
                RunSubmission(idempotency_key=uuid4(), question="Mean?", selection=selected),
                configuration,
            )
            execution, _ = await execute(values, second, datasets, worker, artifacts, calculation)
            await finish_run(db, execution)
            config = migration_config()
            config.attributes["version_table_schema"] = schema
            await apply(db, schema, config, "downgrade", "0005_conversation_history")
            await apply(db, schema, config, "upgrade", "head")
            assert await store.get_run(actor, first.run.run_id) == old
            assert old.configuration.profile.input_token_budget == 4096
            assert (await datasets.get_metadata(actor, data.dataset_id)) == data
            with pytest.raises(ConversationNotFoundError):
                await store.get_run(actor, second.run.run_id)

    run(exercise())
