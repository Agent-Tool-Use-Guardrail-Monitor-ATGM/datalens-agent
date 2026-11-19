"""Atomic state/evidence/outbox boundaries and ordered publisher recovery."""

import asyncio
import json
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import delete, event, func, select, update

from statistics_agent.agent.telemetry import observe_proposal
from statistics_agent.contracts.agent import AgentMessage, ModelAttempt, ProviderRequest
from statistics_agent.contracts.events import EventEnvelope
from statistics_agent.contracts.run_events import validate_run_event
from statistics_agent.core.kafka_settings import KafkaSettings
from statistics_agent.persistence import outbox
from statistics_agent.persistence.models import EventOutbox, RunAttemptRecord, RunEvidence, RunJob
from statistics_agent.publisher import OutboxPublisher
from support.database import run
from support.worker import environment, expire, stored

pytestmark = pytest.mark.postgres


async def rows(env):
    async with env.db.session() as session:
        return (await session.scalars(select(EventOutbox).order_by(EventOutbox.position))).all()


def test_complete_trace_has_versions_features_policy_and_no_private_values(migration_settings):
    async def exercise():
        async with environment(migration_settings) as env:
            await env.worker.run_once()
            records = await rows(env)
            events = [
                validate_run_event(EventEnvelope.model_validate(row.envelope)) for row in records
            ]
            assert [item.sequence for item in events] == list(range(1, 9))
            assert (
                events[0].event_type.value == "session.started"
                and events[-1].event_type.value == "session.ended"
            )
            proposed = next(
                item for item in events if item.event_type.value == "tool_call.requested"
            )
            assert proposed.data["name"] == "describe_columns"
            assert proposed.data["features"]["dataset_selection"] == "selected"
            assert proposed.data["features"]["column_count"] == 1
            assert any(
                item.data.get("policy_evaluation", {}).get("status") == "allowed" for item in events
            )
            text = json.dumps([row.envelope for row in records])
            for private in (
                "price",
                "private-refresh",
                "synthetic-worker-key",
                str(env.actor.auth_session_ref),
            ):
                assert private not in text
            ids = [row.event_id for row in records]
            assert ids == [row.event_id for row in await rows(env)]
            async with env.db.session() as session:
                assert set(await session.scalars(select(RunEvidence.event_id))) == set(ids)
            await env.conversations.delete(env.actor, env.admitted.run.conversation_id)
            assert [row.event_id for row in await rows(env)] == ids, (
                "Chat deletion must not erase pending telemetry"
            )

    run(exercise())


def test_capacity_failure_rolls_back_claim_and_attempt(migration_settings, monkeypatch):
    async def exercise():
        async with environment(migration_settings) as env:
            monkeypatch.setattr(outbox, "MAX_PENDING", 0)
            with pytest.raises(outbox.OutboxFull):
                await env.jobs.claim()
            assert (await stored(env)).lifecycle == "queued" and not await rows(env)
            async with env.db.session() as session:
                assert await session.scalar(select(func.count()).select_from(RunAttemptRecord)) == 0
                assert (await session.get(RunJob, env.admitted.run.run_id)).claim_id is None

    run(exercise())


def test_terminal_outbox_failure_cannot_acknowledge_finished_state(migration_settings, monkeypatch):
    async def exercise():
        async with environment(migration_settings) as env:
            monkeypatch.setattr(outbox, "MAX_PENDING", 7)
            with pytest.raises(outbox.OutboxFull):
                await env.worker.run_once()
            current = await stored(env)
            assert current.lifecycle == "running" and len(current.results) == 1
            assert len(await env.conversations.messages(env.actor, current.conversation_id)) == 1
            async with env.db.session() as session:
                assert (await session.scalar(select(RunAttemptRecord))).finished_at is None
            monkeypatch.setattr(outbox, "MAX_PENDING", 10000)
            await expire(env)
            assert await env.jobs.recover_one()
            assert (await stored(env)).outcome == "incomplete"
            assert (await rows(env))[-1].envelope["event_type"] == "session.ended"

    run(exercise())


def test_tool_state_outcome_and_outbox_commit_or_rollback_together(migration_settings):
    async def exercise():
        async with environment(migration_settings) as env:
            failed = False

            def fail(connection, cursor, statement, parameters, context, executemany):
                nonlocal failed
                if (
                    not failed
                    and statement.startswith("INSERT INTO agent_event_outbox")
                    and parameters.get("sequence") == 4
                ):
                    failed = True
                    raise RuntimeError("synthetic atomic checkpoint failure")

            event.listen(env.db.engine.sync_engine, "before_cursor_execute", fail)
            try:
                await env.worker.run_once()
            finally:
                event.remove(env.db.engine.sync_engine, "before_cursor_execute", fail)
            current, records = await stored(env), await rows(env)
            assert failed and current.outcome == "incomplete" and not current.results
            assert len(env.requests) == 1
            assert not any(row.envelope["event_type"] == "tool_call.completed" for row in records)
            assert any(row.envelope["data"].get("status") == "unknown" for row in records)

    run(exercise())


def test_recovery_emits_unknown_operations_then_terminal_in_one_transaction(migration_settings):
    async def exercise():
        async with environment(migration_settings) as env:
            claim, model_id, tool_id = await env.jobs.claim(), uuid4(), uuid4()
            request = ProviderRequest(
                attempt=ModelAttempt(
                    run_id=claim.run_id,
                    session_id=claim.attempt_id,
                    request_id=claim.request_id,
                    model_call_id=model_id,
                    attempt_index=1,
                ),
                profile=env.configuration.profile.reference,
                messages=(AgentMessage(role="user", content="Question"),),
                max_output_tokens=1,
                deadline=claim.deadline,
            )
            await env.jobs.before_model(claim, request)
            proposal = observe_proposal({}, tool_id, frozenset())
            await env.jobs.append(
                claim,
                EventEnvelope(
                    event_id=uuid4(),
                    event_type="tool_call.requested",
                    occurred_at=claim.deadline,
                    run_id=claim.run_id,
                    session_id=claim.attempt_id,
                    request_id=claim.request_id,
                    sequence=2,
                    data=proposal,
                ),
            )
            await expire(env)
            assert await env.jobs.recover_one()
            events = [row.envelope for row in await rows(env)]
            assert [item["event_type"] for item in events] == [
                "session.started",
                "tool_call.requested",
                "model_call.failed",
                "tool_call.failed",
                "session.ended",
            ]
            assert events[2]["data"]["model_call_id"] == str(model_id)
            assert (
                events[2]["data"]["outcome_unknown"] is True
                and "latency_ms" not in events[2]["data"]
            )
            assert (
                events[3]["data"]["tool_call_id"] == str(tool_id)
                and events[3]["data"]["status"] == "unknown"
            )

    run(exercise())


class FakeFactory:
    def __init__(self):
        self.records, self.producers = [], []
        self.fail_commit = False

    def __call__(self, **options):
        factory = self

        class Producer:
            def __init__(self):
                self.client = SimpleNamespace(close=self.stop)
                self.closed = False

            async def start(self):
                pass

            async def stop(self):
                self.closed = True

            async def begin_transaction(self):
                self.batch = []

            async def send_and_wait(self, topic, value, *, key):
                self.batch.append((key, json.loads(value)))

            async def commit_transaction(self):
                if factory.fail_commit:
                    raise RuntimeError("synthetic broker failure")
                factory.records.extend(self.batch)

        producer = Producer()
        self.producers.append(producer)
        return producer


def publisher(env, factory):
    return OutboxPublisher(
        env.db,
        KafkaSettings(KAFKA_BOOTSTRAP_SERVERS="localhost:19092", _env_file=None),
        producer_factory=factory,
    )


def test_single_publisher_and_broker_failure_retain_ordered_pending_events(migration_settings):
    async def exercise():
        async with environment(migration_settings) as env:
            await env.worker.run_once()
            factory = FakeFactory()
            factory.fail_commit = True
            first, second = publisher(env, factory), publisher(env, factory)
            with pytest.raises(RuntimeError, match="synthetic broker"):
                async with first.epoch() as epoch:
                    async with second.epoch() as losing:
                        assert losing is None and len(factory.producers) == 1
                    await first.publish_one(epoch)
            assert not factory.records and all(row.published_at is None for row in await rows(env))
            factory.fail_commit = False
            async with second.epoch() as epoch:
                while await second.publish_one(epoch):
                    pass
            assert [body["sequence"] for _, body in factory.records] == list(range(1, 9))
            assert all(key == body["session_id"].encode() for key, body in factory.records)
            assert all(row.published_at is not None for row in await rows(env))
            assert all(item.closed for item in factory.producers)

    run(exercise())


def test_broker_commit_before_database_failure_redelivers_same_id_before_later_events(
    migration_settings,
):
    async def exercise():
        async with environment(migration_settings) as env:
            await env.worker.run_once()
            factory, failed = FakeFactory(), False

            def fail(connection, cursor, statement, parameters, context, executemany):
                nonlocal failed
                if not failed and statement.startswith("UPDATE agent_event_outbox"):
                    failed = True
                    raise RuntimeError("synthetic acknowledgement loss")

            event.listen(env.db.engine.sync_engine, "before_cursor_execute", fail)
            instance = publisher(env, factory)
            try:
                with pytest.raises(RuntimeError, match="acknowledgement loss"):
                    async with instance.epoch() as epoch:
                        await instance.publish_one(epoch)
            finally:
                event.remove(env.db.engine.sync_engine, "before_cursor_execute", fail)
            assert (await rows(env))[0].published_at is None
            async with instance.epoch() as epoch:
                while await instance.publish_one(epoch):
                    pass
            assert [body["sequence"] for _, body in factory.records] == [1, *range(1, 9)]
            assert factory.records[0][1]["event_id"] == factory.records[1][1]["event_id"]

    run(exercise())


def test_full_backlog_prevents_another_model_dispatch(migration_settings, monkeypatch):
    async def exercise():
        async with environment(migration_settings) as env:
            monkeypatch.setattr(outbox, "MAX_PENDING", 1)
            with pytest.raises(outbox.OutboxFull):
                await env.worker.run_once()
            assert not env.requests and (await stored(env)).lifecycle == "running"
            monkeypatch.setattr(outbox, "MAX_PENDING", 10000)
            await expire(env)
            assert await env.jobs.recover_one()
            assert [row.envelope["event_type"] for row in await rows(env)] == [
                "session.started",
                "session.ended",
            ]

    run(exercise())


def test_old_attempt_is_recovered_without_relabeling_legacy_journal(migration_settings):
    async def exercise():
        async with environment(migration_settings) as env:
            claim = await env.jobs.claim()
            async with env.db.session() as session:
                await session.execute(
                    update(RunAttemptRecord).values(telemetry_version=None, next_sequence=1)
                )
                await session.execute(delete(EventOutbox))
                await session.execute(delete(RunEvidence))
                await session.commit()
            await env.jobs.interrupt(claim)
            assert (await stored(env)).outcome == "incomplete"
            assert not await rows(env)

    run(exercise())


def test_unknown_tool_still_produces_safe_classifier_evidence(migration_settings):
    async def exercise():
        from support.worker import call

        async def handler(body):
            return call("private-malicious-tool", {"private-argument": "private-cell"})

        async with environment(migration_settings, handler) as env:
            await env.worker.run_once()
            records = await rows(env)
            assert (await stored(env)).outcome == "incomplete"
            proposed = next(
                row.envelope["data"]
                for row in records
                if row.envelope["event_type"] == "tool_call.requested"
            )
            assert proposed["kind"] == "unknown" and "name" not in proposed
            assert "private-" not in json.dumps([row.envelope for row in records])
            failure = next(
                row.envelope["data"]
                for row in records
                if row.envelope["event_type"] == "tool_call.failed"
            )
            assert failure["reason"] == "unknown_tool"
            assert failure["policy_evaluation"]["status"] == "not_evaluated"

    run(exercise())


def test_publisher_never_uses_a_second_pool_connection_or_an_open_broker_transaction(
    migration_settings, monkeypatch
):
    async def exercise():
        async with environment(migration_settings) as env:
            await env.worker.run_once()
            factory = FakeFactory()
            instance = publisher(env, factory)
            async with instance.epoch() as epoch:
                owner, producer = epoch
                original = producer.send_and_wait

                async def send(*args, **kwargs):
                    assert not owner.in_transaction()
                    return await original(*args, **kwargs)

                def forbidden():
                    pytest.fail("Publisher must use its already owned connection")

                monkeypatch.setattr(producer, "send_and_wait", send)
                monkeypatch.setattr(env.db, "session", forbidden)
                assert await instance.publish_one(epoch)

    run(exercise())


def test_publisher_retries_a_fresh_epoch_and_stops_cleanly(migration_settings, capsys):
    async def exercise():
        async with environment(migration_settings) as env:
            await env.worker.run_once()
            failed, stop = asyncio.Event(), asyncio.Event()

            class Retrying(FakeFactory):
                def __call__(self, **options):
                    producer = super().__call__(**options)
                    original = producer.commit_transaction

                    async def commit():
                        if self.fail_commit:
                            failed.set()
                        await original()
                        if len(self.records) == 8:
                            stop.set()

                    producer.commit_transaction = commit
                    return producer

            factory = Retrying()
            factory.fail_commit = True
            task = asyncio.create_task(publisher(env, factory).run(stop))
            await asyncio.wait_for(failed.wait(), 5)
            factory.fail_commit = False
            await asyncio.wait_for(task, 5)
            assert len(factory.records) == 8 and len(factory.producers) == 2
            assert all(item.closed for item in factory.producers)
            output = capsys.readouterr().out
            assert "pending events retained for retry" in output
            assert "synthetic broker failure" not in output

    run(exercise())


def test_cleanup_removes_only_old_published_rows_in_bounded_batches(migration_settings):
    async def exercise():
        async with environment(migration_settings) as env:
            await env.worker.run_once()
            records = await rows(env)
            async with env.db.session() as session:
                now = await session.scalar(select(func.clock_timestamp()))
                for record, age in zip(records[:4], [9, 8, 1, -1], strict=True):
                    await session.execute(
                        update(EventOutbox)
                        .where(EventOutbox.event_id == record.event_id)
                        .values(published_at=now - timedelta(days=age))
                    )
                await session.commit()
            assert await outbox.cleanup_published(env.db, limit=1) == 1
            assert [row.event_id for row in await rows(env)] == [r.event_id for r in records[1:]]
            assert await outbox.cleanup_published(env.db) == 1
            assert await outbox.cleanup_published(env.db) == 0
            remaining = await rows(env)
            assert [row.event_id for row in remaining] == [r.event_id for r in records[2:]]
            assert sum(row.published_at is None for row in remaining) == 4
            async with env.db.session() as session:
                assert await session.scalar(select(func.count()).select_from(RunEvidence)) == 8
            assert (await stored(env)).lifecycle == "finished"

    run(exercise())


def test_cleanup_skips_locked_published_rows_and_preserves_pending_events(migration_settings):
    async def exercise():
        async with environment(migration_settings) as env:
            await env.worker.run_once()
            records = await rows(env)
            async with env.db.session() as session:
                now = await session.scalar(select(func.clock_timestamp()))
                await session.execute(
                    update(EventOutbox)
                    .where(EventOutbox.event_id.in_([r.event_id for r in records[:2]]))
                    .values(published_at=now - timedelta(days=10))
                )
                await session.commit()
            async with env.db.session() as locked:
                await locked.execute(
                    select(EventOutbox)
                    .where(EventOutbox.event_id == records[0].event_id)
                    .with_for_update()
                )
                assert await outbox.cleanup_published(env.db) == 1
                assert await outbox.cleanup_published(env.db) == 0
                await locked.rollback()
            assert await outbox.cleanup_published(env.db) == 1
            assert len(await rows(env)) == 6
            assert all(row.published_at is None for row in await rows(env))

    run(exercise())
