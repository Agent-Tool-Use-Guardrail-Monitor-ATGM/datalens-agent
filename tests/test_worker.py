"""Real PostgreSQL/JWT/tools with mocked model/identity HTTP; no live providers."""

import asyncio
import json
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import event, func, select, update

from statistics_agent.agent.prompts import PROMPT
from statistics_agent.contracts.agent import AgentVersions
from statistics_agent.contracts.auth import AuthorizedPrincipal
from statistics_agent.contracts.common import AuthenticatedPrincipal
from statistics_agent.contracts.conversations import DatasetSelection, RunSubmission
from statistics_agent.contracts.policy import ToolPolicyConfiguration
from statistics_agent.contracts.sessions import Credentials
from statistics_agent.core.model_settings import ModelSettings
from statistics_agent.core.token_encryption import TokenEncryption
from statistics_agent.persistence.auth import AuthStore
from statistics_agent.persistence.conversations import AdmissionConfiguration, ConversationStore
from statistics_agent.persistence.jobs import JobStore, LeaseLost
from statistics_agent.persistence.models import (
    BrowserSession,
    DatasetArtifact,
    RunAttemptRecord,
    RunDispatch,
    RunEvidence,
    RunJob,
    RunRecord,
)
from statistics_agent.tools.statistics import STATISTICS_TOOLSET
from statistics_agent.worker import AgentWorker, WorkerAccess
from support.auth import SyntheticProvider, new_session
from support.crypto import keyring
from support.database import run
from support.datasets import CONTENT, application

pytestmark = pytest.mark.postgres


def call(name, arguments):
    return httpx.Response(
        200,
        json={
            "choices": [
                {
                    "finish_reason": "tool_calls",
                    "message": {
                        "tool_calls": [
                            {
                                "id": "synthetic-call",
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


def answer(body):
    observations = [item for item in body["messages"] if item["role"] == "tool"]
    if observations:
        return call(
            "finalize_statistics",
            {
                "status": "completed",
                "explanation": "Mean is 4.",
                "result_ids": [json.loads(observations[-1]["content"])["tool_call_id"]],
            },
        )
    dataset = json.loads(body["messages"][1]["content"])["datasets"][0]
    return call(
        "describe_columns",
        {
            "dataset_id": dataset["dataset_id"],
            "columns": ["price"],
            "missing_policy": "omit",
        },
    )


@asynccontextmanager
async def environment(settings, handler=None, *, lease=30):
    async with application(settings) as (database, artifacts, parser, datasets):
        source = SyntheticProvider()
        provider = source.client()
        auth = AuthStore(database, source.settings, TokenEncryption(keyring()))
        created = await auth.create_session(
            AuthenticatedPrincipal(issuer=source.settings.issuer, subject="synthetic-user"),
            Credentials(
                SecretStr(source.token()),
                SecretStr("private-refresh-token"),
                SecretStr(source.token(False)),
            ),
        )
        identity = await provider.validate_access(
            created.session.credentials.access_token.get_secret_value()
        )
        actor = AuthorizedPrincipal(
            **identity.model_dump(), auth_session_ref=created.session.session_id
        )
        conversations = ConversationStore(database)
        conversation = await conversations.create(actor, title="Worker test")
        data = await datasets.ingest(actor, filename="input.csv", content=CONTENT)
        selection = DatasetSelection(dataset_ids=(data.dataset_id,))
        await conversations.set_attachments(actor, conversation.conversation_id, selection)
        model = ModelSettings(SERVICE_MODEL_API_KEY="synthetic-worker-key", _env_file=None)
        policy = ToolPolicyConfiguration(
            reference={"config_id": uuid4(), "version": "worker-policy-v1"},
            allowed_tools=(
                "inspect_dataset",
                "describe_columns",
                "aggregate_statistics",
                "correlate_columns",
            ),
        )
        configuration = AdmissionConfiguration(
            versions=AgentVersions(
                provider=model.resolve().reference,
                prompt=PROMPT.reference,
                policy=policy.reference,
                toolset=STATISTICS_TOOLSET,
            ),
            profile=model.resolve(),
            prompt=PROMPT,
            policy=policy,
        )
        admitted = await conversations.admit(
            actor,
            conversation.conversation_id,
            RunSubmission(
                idempotency_key=uuid4(),
                question="Mean price?",
                selection=selection,
            ),
            configuration,
        )
        requests = []
        held = ContextVar("transaction_held", default=False)
        original_session = database.session

        @asynccontextmanager
        async def tracked_session():
            async with original_session() as current:
                token = held.set(True)
                try:
                    yield current
                finally:
                    held.reset(token)

        database.session = tracked_session

        async def handle(request):
            assert not held.get(), "No transaction may span model HTTP"
            requests.append(json.loads(request.content))
            async with database.session() as session:
                pending = await session.scalar(
                    select(func.count())
                    .select_from(RunDispatch)
                    .where(
                        RunDispatch.kind == "model",
                        RunDispatch.outcome == "pending",
                    )
                )
                assert pending == 1, "Model intent must be committed before HTTP"
            return await handler(requests[-1]) if handler else answer(requests[-1])

        jobs = JobStore(database, lease_seconds=lease)
        worker = AgentWorker(
            jobs=jobs,
            access=WorkerAccess(auth, provider, source.settings),
            datasets=datasets,
            artifacts=artifacts,
            parser=parser,
            model_settings=model,
            transport=httpx.MockTransport(handle),
        )
        try:
            yield SimpleNamespace(
                db=database,
                artifacts=artifacts,
                datasets=datasets,
                source=source,
                provider=provider,
                auth=auth,
                actor=actor,
                conversations=conversations,
                data=data,
                selection=selection,
                configuration=configuration,
                admitted=admitted,
                jobs=jobs,
                worker=worker,
                requests=requests,
                created=created,
            )
        finally:
            await provider.close()


async def expire(env):
    async with env.db.session() as session:
        await session.execute(
            update(RunJob)
            .where(RunJob.run_id == env.admitted.run.run_id)
            .values(
                lease_expires_at=func.clock_timestamp() - timedelta(seconds=1),
            )
        )
        await session.commit()


async def stored(env):
    return (await env.conversations.get_run(env.actor, env.admitted.run.run_id)).run


def test_worker_executes_with_real_auth_tools_checkpoint_and_committed_evidence(migration_settings):
    async def exercise():
        async with environment(migration_settings) as env:
            env.source.offline = True  # Cached JWT keys must not require identity HTTP.
            assert await env.worker.run_once()
            result = await stored(env)
            assert result.outcome == "completed" and len(result.results) == 1
            assert len(env.requests) == 2
            assert not await env.worker.run_once()
            async with env.db.session() as session:
                evidence = (
                    await session.scalars(select(RunEvidence).order_by(RunEvidence.sequence))
                ).all()
                attempts = (await session.scalars(select(RunAttemptRecord))).all()
                assert len(attempts) == 1 and attempts[0].finished_at is not None
                assert [item.sequence for item in evidence] == list(range(1, 7))
                assert (await session.scalar(select(func.count()).select_from(RunDispatch))) == 4
                assert await session.get(RunJob, result.run_id) is None
                text = json.dumps([item.envelope for item in evidence])
                assert "private-refresh" not in text and "synthetic-worker-key" not in text
                assert str(env.actor.auth_session_ref) not in text and "population_std" not in text
            assert len(await env.conversations.messages(env.actor, result.conversation_id)) == 2
            assert not any(request.url.path.endswith("introspect") for request in env.source.calls)

    run(exercise())


@pytest.mark.parametrize("denial", ["revoked", "expired", "missing", "foreign", "deleted"])
def test_invalid_original_session_never_dispatches(migration_settings, denial):
    async def exercise():
        async with environment(migration_settings) as env:
            if denial == "revoked":
                await env.auth.revoke_session(env.actor.auth_session_ref)
            elif denial == "deleted":
                from sqlalchemy import delete

                async with env.db.session() as session:
                    await session.execute(delete(BrowserSession))
                    await session.commit()
            elif denial == "expired":
                from datetime import UTC, datetime, timedelta

                old = datetime.now(UTC) - timedelta(hours=2)
                async with env.db.session() as session:
                    await session.execute(
                        update(BrowserSession).values(
                            created_at=old,
                            last_seen_at=old,
                            idle_expires_at=old + timedelta(seconds=1),
                        )
                    )
                    await session.commit()
            else:
                reference = None
                if denial == "foreign":
                    other = await new_session(env.auth, env.source, subject="other")
                    reference = other.session.session_id
                async with env.db.session() as session:
                    await session.execute(update(RunJob).values(auth_session_ref=reference))
                    await session.commit()
            await env.worker.run_once()
            assert not env.requests
            assert (await stored(env)).outcome == "incomplete"
            async with env.db.session() as session:
                assert await session.get(RunJob, env.admitted.run.run_id) is None

    run(exercise())


def test_worker_renews_expired_jwt_through_existing_session_lifecycle(migration_settings):
    async def exercise():
        async with environment(migration_settings) as env:
            from datetime import UTC, datetime

            now = int(datetime.now(UTC).timestamp())
            old = env.created.session.credentials
            expired = env.source.token(changes={"iat": now - 100, "exp": now - 1})
            assert await env.auth.replace_credentials(
                env.actor.auth_session_ref,
                0,
                type(old)(SecretStr(expired), old.refresh_token, old.id_token),
            )
            await env.worker.run_once()
            assert (await stored(env)).outcome == "completed"
            assert any(request.url.path.endswith("token") for request in env.source.calls)

    run(exercise())


def test_revocation_between_model_and_tool_denies_execution(migration_settings):
    async def exercise():

        async def handler(body):
            await env.auth.revoke_session(env.actor.auth_session_ref)
            return answer(body)

        async with environment(migration_settings, handler) as env:
            await env.worker.run_once()
            assert len(env.requests) == 1
            assert (await stored(env)).outcome == "incomplete"
            async with env.db.session() as session:
                assert await session.scalar(select(func.count()).select_from(DatasetArtifact)) == 0

    run(exercise())


def test_two_replicas_admit_only_one_live_claim_and_recover_after_restart(migration_settings):
    async def exercise():
        async with environment(migration_settings) as env:
            other = JobStore(env.db)
            claims = await asyncio.gather(env.jobs.claim(), other.claim())
            assert sum(item is not None for item in claims) == 1
            claim = next(item for item in claims if item is not None)
            await env.jobs.heartbeat(claim)
            assert not await other.claim()
            await expire(env)
            with pytest.raises(LeaseLost):
                await env.jobs.heartbeat(claim)
            assert await other.recover_one()
            assert not await other.recover_one()
            assert (await stored(env)).outcome == "incomplete"
            assert not env.requests
            with pytest.raises(LeaseLost):
                await env.jobs.heartbeat(claim)

    run(exercise())


@pytest.mark.parametrize("partial", [False, True])
def test_expired_worker_is_fenced_and_unknown_http_is_not_replayed(migration_settings, partial):
    async def exercise():
        entered = asyncio.Event()
        waiting = asyncio.Event()

        async def handler(body):
            if partial and len(env.requests) == 1:
                return answer(body)
            if partial:
                assert len((await stored(env)).results) == 1, "Checkpoint must precede explanation"
            entered.set()
            await waiting.wait()
            return answer(body)

        async with environment(migration_settings, handler) as env:
            task = asyncio.create_task(env.worker.run_once())
            await asyncio.wait_for(entered.wait(), 15)
            await expire(env)
            assert await JobStore(env.db).recover_one()
            await asyncio.wait_for(task, 15)
            result = await stored(env)
            assert result.outcome == "incomplete" and len(result.results) == int(partial)
            assert len(env.requests) == (2 if partial else 1)
            assert not await env.worker.run_once()
            async with env.db.session() as session:
                assert (
                    await session.scalar(
                        select(func.count())
                        .select_from(RunDispatch)
                        .where(
                            RunDispatch.kind == "model",
                            RunDispatch.outcome == "unknown",
                        )
                    )
                    == 1
                )
            assert len(await env.conversations.messages(env.actor, result.conversation_id)) == 2

            if partial:
                reused_requests = []

                def reuse(request):
                    body = json.loads(request.content)
                    reused_requests.append(body)
                    supplied = json.loads(body["messages"][1]["content"])
                    return call(
                        "finalize_statistics",
                        {
                            "status": "completed",
                            "explanation": "Previously computed mean is 4.",
                            "result_ids": [supplied["verified_prior_results"][0]["result_id"]],
                        },
                    )

                env.worker.transport = httpx.MockTransport(reuse)
                reply = await env.conversations.admit(
                    env.actor,
                    result.conversation_id,
                    RunSubmission(
                        idempotency_key=uuid4(),
                        question="Explain the saved result.",
                        prior_results=result.results,
                    ),
                    env.configuration,
                )
                await env.worker.run_once()
                imported = (await env.conversations.get_run(env.actor, reply.run.run_id)).run
                assert imported.outcome == "completed" and not imported.results
                assert imported.completion.imported_results[0].source == result.results[0]
                assert len(reused_requests) == 1

    run(exercise())


@pytest.mark.parametrize("failure", ["model_intent", "tool_request", "checkpoint"])
def test_failed_durable_boundary_prevents_next_dispatch(migration_settings, monkeypatch, failure):
    async def exercise():
        async with environment(migration_settings) as env:

            async def fail(*args, **kwargs):
                raise RuntimeError("private failure detail")

            if failure == "model_intent":
                monkeypatch.setattr(env.jobs, "before_model", fail)
            elif failure == "checkpoint":
                monkeypatch.setattr(env.jobs, "checkpoint", fail)
            else:
                original = env.jobs.append

                async def append(claim, evidence):
                    if evidence.event_type.value == "tool_call.requested":
                        await fail()
                    await original(claim, evidence)

                monkeypatch.setattr(env.jobs, "append", append)
            await env.worker.run_once()
            assert len(env.requests) == (0 if failure == "model_intent" else 1)
            result = await stored(env)
            assert result.outcome == "incomplete" and not result.results
            assert "private failure" not in result.model_dump_json()

    run(exercise())


def test_actual_intent_commit_failure_never_sends_http(migration_settings):
    async def exercise():
        async with environment(migration_settings) as env:

            def fail(connection, cursor, statement, parameters, context, executemany):
                if statement.startswith("INSERT INTO agent_run_dispatches"):
                    raise RuntimeError("synthetic journal commit failure")

            event.listen(env.db.engine.sync_engine, "before_cursor_execute", fail)
            try:
                await env.worker.run_once()
            finally:
                event.remove(env.db.engine.sync_engine, "before_cursor_execute", fail)
            assert not env.requests and (await stored(env)).outcome == "incomplete"

    run(exercise())


def test_graceful_shutdown_cancels_wait_and_records_unknown_outcome(migration_settings):
    async def exercise():
        entered, never, stop = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def handler(body):
            entered.set()
            await never.wait()

        async with environment(migration_settings, handler) as env:
            task = asyncio.create_task(env.worker.run(stop))
            await asyncio.wait_for(entered.wait(), 15)
            stop.set()
            await asyncio.wait_for(task, 15)
            assert (await stored(env)).outcome == "incomplete"
            async with env.db.session() as session:
                attempt = await session.scalar(select(RunAttemptRecord))
                assert attempt.reason == "shutdown"
                assert await session.scalar(select(RunDispatch.outcome)) == "unknown"

    run(exercise())


def test_pinned_profile_mismatch_and_missing_dataset_finish_without_http(migration_settings):
    async def exercise():
        for mismatch in (True, False):
            async with environment(migration_settings) as env:
                if mismatch:
                    env.worker.model_settings = ModelSettings(
                        SERVICE_MODEL_API_KEY="synthetic", LLM_MAX_STEPS=7, _env_file=None
                    )
                else:
                    await env.artifacts.delete(env.actor, env.data.dataset_id)
                await env.worker.run_once()
                assert (await stored(env)).outcome == "incomplete" and not env.requests

    run(exercise())


def test_clarification_finishes_and_reply_is_new_worker_job(migration_settings):
    async def exercise():
        async def clarify(body):
            return call("request_clarification", {"question": "Which column?"})

        async with environment(migration_settings, clarify) as env:
            await env.worker.run_once()
            first = await stored(env)
            assert first.outcome == "needs_input" and first.lifecycle == "finished"
            reply = await env.conversations.admit(
                env.actor,
                first.conversation_id,
                RunSubmission(
                    idempotency_key=uuid4(),
                    question="Price.",
                    selection=env.selection,
                ),
                env.configuration,
            )
            assert reply.run.run_id != first.run_id
            await env.worker.run_once()
            assert (
                await env.conversations.get_run(env.actor, reply.run.run_id)
            ).run.outcome == "needs_input"

    run(exercise())


def test_global_admission_applies_across_conversations(migration_settings):
    async def exercise():
        async with environment(migration_settings) as env:
            second = await env.conversations.create(env.actor, title="Second")
            await env.conversations.admit(
                env.actor,
                second.conversation_id,
                RunSubmission(idempotency_key=uuid4(), question="Other"),
                env.configuration,
            )
            claim = await env.jobs.claim()
            assert claim is not None
            assert await JobStore(env.db).claim() is None
            await env.jobs.interrupt(claim)
            assert await JobStore(env.db).claim() is not None

    run(exercise())


def test_old_clock_skewed_admission_can_finish_safely(migration_settings):
    async def exercise():
        async with environment(migration_settings) as env:
            async with env.db.session() as session:
                await session.execute(
                    update(RunRecord).values(
                        created_at=func.clock_timestamp() + timedelta(seconds=20),
                    )
                )
                await session.commit()
            claim = await env.jobs.claim()
            await expire(env)
            assert await env.jobs.recover_one()
            result = await stored(env)
            assert result.finished_at >= result.started_at >= result.created_at
            assert result.outcome == "incomplete"
            with pytest.raises(LeaseLost):
                await env.jobs.heartbeat(claim)

    run(exercise())


def test_expired_claim_cannot_commit_a_previously_valid_terminal_answer(
    migration_settings, monkeypatch
):
    async def exercise():
        async with environment(migration_settings) as env:
            import statistics_agent.worker as runtime
            from statistics_agent.persistence.completions import finish_run

            async def finish(database, execution, *, claim_id):
                await expire(env)
                with pytest.raises(LeaseLost):
                    await finish_run(database, execution, claim_id=claim_id)
                raise LeaseLost

            monkeypatch.setattr(runtime, "finish_run", finish)
            await env.worker.run_once()
            assert (await stored(env)).lifecycle == "running"
            assert await env.jobs.recover_one()
            result = await stored(env)
            assert result.outcome == "incomplete" and len(result.results) == 1

    run(exercise())


def test_live_heartbeat_renews_without_extending_browser_idle_time(migration_settings):
    async def exercise():
        entered, release = asyncio.Event(), asyncio.Event()

        async def handler(body):
            entered.set()
            await release.wait()
            return call("request_clarification", {"question": "Which column?"})

        async with environment(migration_settings, handler) as env:
            original = await env.auth.read_session_id(env.actor.auth_session_ref)
            task = asyncio.create_task(env.worker.run_once())
            await asyncio.wait_for(entered.wait(), 15)
            async with env.db.session() as session:
                initial = await session.scalar(select(RunJob.lease_expires_at))
            async with asyncio.timeout(15):
                while True:
                    await asyncio.sleep(0.05)
                    async with env.db.session() as session:
                        current = await session.scalar(select(RunJob.lease_expires_at))
                    if current > initial:
                        break
            release.set()
            await asyncio.wait_for(task, 15)
            assert (await stored(env)).outcome == "needs_input"
            assert (
                await env.auth.read_session_id(env.actor.auth_session_ref)
            ).idle_expires_at == original.idle_expires_at

    run(exercise())


def test_expired_claim_rejects_every_new_durable_boundary(migration_settings):
    async def exercise():
        async with environment(migration_settings) as env:
            from statistics_agent.contracts.agent import (
                AgentContext,
                AgentMessage,
                ModelAttempt,
                ProviderRequest,
                ToolResult,
            )
            from statistics_agent.contracts.events import EventEnvelope

            claim = await env.jobs.claim()
            await expire(env)
            attempt = ModelAttempt(
                run_id=claim.run_id,
                session_id=claim.attempt_id,
                request_id=claim.request_id,
                model_call_id=uuid4(),
                attempt_index=1,
            )
            request = ProviderRequest(
                attempt=attempt,
                profile=env.configuration.profile.reference,
                messages=(AgentMessage(role="user", content="Question"),),
                max_output_tokens=1,
                deadline=claim.deadline,
            )
            event = EventEnvelope(
                event_id=uuid4(),
                event_type="tool_call.requested",
                occurred_at=claim.deadline,
                run_id=claim.run_id,
                session_id=claim.attempt_id,
                request_id=claim.request_id,
                sequence=1,
                data={"tool_call_id": str(uuid4())},
            )
            context = AgentContext(
                principal=env.actor,
                run_id=claim.run_id,
                session_id=claim.attempt_id,
                request_id=claim.request_id,
            )
            with pytest.raises(LeaseLost):
                await env.jobs.before_model(claim, request)
            with pytest.raises(LeaseLost):
                await env.jobs.append(claim, event)
            with pytest.raises(LeaseLost):
                await env.jobs.checkpoint(
                    claim, context, ToolResult(tool_call_id=uuid4(), status="completed")
                )
            async with env.db.session() as session:
                assert await session.scalar(select(func.count()).select_from(RunDispatch)) == 0
                assert await session.scalar(select(func.count()).select_from(RunEvidence)) == 0

    run(exercise())


def test_evidence_sequence_failure_rolls_back_without_a_gap(migration_settings):
    async def exercise():
        async with environment(migration_settings) as env:
            from statistics_agent.contracts.events import EventEnvelope

            claim = await env.jobs.claim()
            event = EventEnvelope(
                event_id=uuid4(),
                event_type="tool_call.requested",
                occurred_at=claim.deadline,
                run_id=claim.run_id,
                session_id=claim.attempt_id,
                request_id=claim.request_id,
                sequence=2,
                data={"tool_call_id": str(uuid4())},
            )
            with pytest.raises(ValueError, match="sequence unavailable"):
                await env.jobs.append(claim, event)
            await env.jobs.append(claim, event.model_copy(update={"sequence": 1}))
            with pytest.raises(ValueError, match="sequence unavailable"):
                await env.jobs.append(claim, event.model_copy(update={"sequence": 1}))
            async with env.db.session() as session:
                assert await session.scalar(select(RunAttemptRecord.next_sequence)) == 2
                assert await session.scalar(select(func.count()).select_from(RunEvidence)) == 1

    run(exercise())


@pytest.mark.parametrize("field", ["session_id", "request_id", "auth_session_ref"])
def test_terminal_requires_original_execution_and_browser_binding(
    migration_settings, monkeypatch, field
):
    async def exercise():
        async with environment(migration_settings) as env:
            import statistics_agent.worker as runtime
            from statistics_agent.persistence.completions import finish_run

            async def finish(database, execution, *, claim_id):
                context = execution.state.context
                if field == "auth_session_ref":
                    context = context.model_copy(
                        update={
                            "principal": context.principal.model_copy(update={field: uuid4()}),
                        }
                    )
                else:
                    context = context.model_copy(update={field: uuid4()})
                swapped = execution.model_copy(
                    update={
                        "state": execution.state.model_copy(update={"context": context}),
                    }
                )
                with pytest.raises(LeaseLost):
                    await finish_run(database, swapped, claim_id=claim_id)
                return await finish_run(database, execution, claim_id=claim_id)

            monkeypatch.setattr(runtime, "finish_run", finish)
            await env.worker.run_once()
            assert (await stored(env)).outcome == "completed"

    run(exercise())
