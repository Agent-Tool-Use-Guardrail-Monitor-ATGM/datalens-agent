"""Real PostgreSQL atomicity, ownership, snapshots and restart persistence."""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import event, func, insert, select, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError

from statistics_agent.agent.prompts import PROMPT
from statistics_agent.contracts.agent import AgentVersions
from statistics_agent.contracts.common import ConfigurationReference
from statistics_agent.contracts.conversations import (
    DatasetSelection,
    DatasetSnapshot,
    ResultProvenance,
    RunSubmission,
)
from statistics_agent.contracts.policy import ToolPolicyConfiguration
from statistics_agent.core.model_settings import ModelSettings
from statistics_agent.persistence.conversations import (
    AdmissionConfiguration,
    ConversationBusyError,
    ConversationNotFoundError,
    ConversationStore,
    IdempotencyConflictError,
    RunAdmissionGrant,
    RunInputUnavailableError,
)
from statistics_agent.persistence.models import (
    ConversationMessageRecord,
    ConversationRecord,
    Dataset,
    RunJob,
    RunRecord,
)
from statistics_agent.persistence.schema import migration_config
from support.database import apply, run, sandbox
from support.datasets import CONTENT, application, owner

pytestmark = pytest.mark.postgres


def configuration():
    profile = ModelSettings(SERVICE_MODEL_API_KEY="synthetic-storage-key", _env_file=None).resolve()
    reference = ConfigurationReference(config_id=uuid4(), version="storage-v1")
    return AdmissionConfiguration(
        versions=AgentVersions(
            provider=profile.reference, prompt=PROMPT.reference, toolset=reference, policy=reference
        ),
        policy=ToolPolicyConfiguration(reference=reference, allowed_tools=("inspect_dataset",)),
        profile=profile,
        prompt=PROMPT,
    )


def submission(*ids, key=None, question="Describe data.", prior_results=()):
    return RunSubmission(
        idempotency_key=key or uuid4(),
        question=question,
        selection=DatasetSelection(dataset_ids=ids),
        prior_results=prior_results,
    )


async def counts(database):
    async with database.session() as session:
        return tuple(
            [
                await session.scalar(select(func.count()).select_from(model))
                for model in (RunRecord, ConversationMessageRecord, RunJob)
            ]
        )


async def fixture(database, datasets):
    actor = owner()
    store = ConversationStore(database)
    conversation = await store.create(actor, title="Private statistics")
    data = await datasets.ingest(actor, filename="private.csv", content=CONTENT)
    await store.set_attachments(
        actor, conversation.conversation_id, DatasetSelection(dataset_ids=(data.dataset_id,))
    )
    return actor, store, conversation.conversation_id, data


async def finish_for_fixture(database, run_id):
    # Storage fixture only; production worker transitions/leases belong to 7.5.
    async with database.session() as session:
        await session.execute(
            update(RunRecord)
            .where(RunRecord.id == run_id)
            .values(lifecycle="finished", outcome="incomplete", finished_at=datetime.now(UTC))
        )
        await session.commit()


@pytest.mark.parametrize("same_key", [True, False])
def test_concurrent_admission_creates_exactly_one_message_run_and_job(migration_settings, same_key):
    async def exercise():
        async with application(migration_settings) as (database, _, _, datasets):
            actor, store, cid, data = await fixture(database, datasets)
            key, config = uuid4(), configuration()
            requests = [
                submission(data.dataset_id, key=key if same_key else None) for _ in range(6)
            ]
            outcomes = await asyncio.gather(
                *[
                    ConversationStore(database).admit(actor, cid, request, config)
                    for request in requests
                ],
                return_exceptions=True,
            )
            successes = [item for item in outcomes if not isinstance(item, BaseException)]
            assert len(successes) == (6 if same_key else 1)
            assert len({item.run.run_id for item in successes}) == 1
            assert sum(not item.replayed for item in successes) == 1
            assert all(
                isinstance(item, ConversationBusyError)
                for item in outcomes
                if isinstance(item, BaseException)
            )
            assert await counts(database) == (1, 1, 1)
            assert [message.sequence for message in await store.messages(actor, cid)] == [1]

    run(exercise())


def test_replay_precedes_busy_and_preserves_original_config_and_attachment_snapshot(
    migration_settings,
):
    async def exercise():
        async with application(migration_settings) as (database, _, _, datasets):
            actor, store, cid, data = await fixture(database, datasets)
            request, config = submission(data.dataset_id), configuration()
            first = await store.admit(actor, cid, request, config)
            await store.set_attachments(actor, cid, DatasetSelection())
            new_session = owner(roles=["admin", "user"])
            replay = await store.admit(new_session, cid, request, configuration())
            assert replay.replayed and replay.configuration == config
            assert replay.run == first.run and replay.datasets == (data,)
            async with database.session() as session:
                job = await session.get(RunJob, first.run.run_id)
                assert job.admission_grant == {
                    "schema_version": 1,
                    "actor": {
                        "issuer": actor.issuer,
                        "subject": actor.subject,
                        "roles": ["user"],
                        "email_verified": True,
                    },
                }
                assert RunAdmissionGrant.model_validate(job.admission_grant).actor.roles == {"user"}
                assert job.auth_session_ref == actor.auth_session_ref
                assert job.queue_expires_at == first.run.created_at + timedelta(minutes=10)
            assert replay.attachment_version == 1
            assert await store.attachments(actor, cid) == ()
            with pytest.raises(IdempotencyConflictError):
                await store.admit(
                    actor,
                    cid,
                    submission(key=request.idempotency_key, question="Changed question"),
                    config,
                )
            with pytest.raises(ConversationBusyError):
                await store.admit(actor, cid, submission(), config)
            assert await counts(database) == (1, 1, 1)

    run(exercise())


def test_admission_grant_migration_preserves_legacy_job_and_guards_downgrade(migration_settings):
    async def exercise():
        async with application(migration_settings) as (database, _, _, datasets):
            actor, store, cid, data = await fixture(database, datasets)
            accepted = await store.admit(actor, cid, submission(data.dataset_id), configuration())
            run_id = accepted.run.run_id
            config = migration_config()
            async with database.session() as session:
                schema = await session.scalar(select(func.current_schema()))
            config.attributes["version_table_schema"] = schema
            previous = "0010_identity_projection"
            with pytest.raises(DBAPIError, match="Drain granted jobs"):
                await apply(database, schema, config, "downgrade", previous)
            async with database.session() as session:
                # A mixed-version writer can leave the new columns empty.
                await session.execute(
                    update(RunJob)
                    .where(RunJob.run_id == run_id)
                    .values(admission_grant=None, queue_expires_at=None)
                )
                await session.commit()
            await apply(database, schema, config, "downgrade", previous)
            await apply(database, schema, config, "upgrade", "head")
            async with database.session() as session:
                job = await session.get(RunJob, run_id)
                assert job.auth_session_ref == actor.auth_session_ref
                assert job.created_at == accepted.run.created_at
                assert job.admission_grant is None and job.queue_expires_at is None
                assert await session.scalar(text("SELECT count(*) FROM agent_runs")) == 1

    run(exercise())


def test_job_insert_failure_rolls_back_message_run_and_sequence(migration_settings):
    async def exercise():
        async with application(migration_settings) as (database, _, _, datasets):
            actor, store, cid, data = await fixture(database, datasets)

            def fail_job(connection, cursor, statement, parameters, context, many):
                if statement.startswith("INSERT INTO agent_run_jobs"):
                    raise RuntimeError("Synthetic job insert failure")

            event.listen(database.engine.sync_engine, "before_cursor_execute", fail_job)
            try:
                with pytest.raises(RuntimeError, match="Synthetic"):
                    await store.admit(actor, cid, submission(data.dataset_id), configuration())
            finally:
                event.remove(database.engine.sync_engine, "before_cursor_execute", fail_job)
            assert await counts(database) == (0, 0, 0)
            async with database.session() as session:
                assert (await session.get(ConversationRecord, cid)).next_sequence == 1
            assert not (
                await store.admit(actor, cid, submission(data.dataset_id), configuration())
            ).replayed

    run(exercise())


@pytest.mark.parametrize(
    "identity",
    [
        {"subject": "other"},
        {"subject": "admin", "roles": ["user", "admin"]},
        {"issuer": "https://other.example/realms/datalens"},
    ],
)
def test_every_history_operation_filters_issuer_subject_and_does_not_bypass_for_admin(
    migration_settings, identity
):
    async def exercise():
        async with application(migration_settings) as (database, _, _, datasets):
            actor, store, cid, data = await fixture(database, datasets)
            config = configuration()
            admitted = await store.admit(actor, cid, submission(data.dataset_id), config)
            stranger = owner(**identity)
            assert await store.list(stranger) == ()
            operations = [
                lambda: store.get(stranger, cid),
                lambda: store.messages(stranger, cid),
                lambda: store.attachments(stranger, cid),
                lambda: store.set_attachments(stranger, cid, DatasetSelection()),
                lambda: store.get_run(stranger, admitted.run.run_id),
                lambda: store.admit(stranger, cid, submission(), config),
                lambda: store.delete(stranger, cid),
            ]
            for operation in operations:
                with pytest.raises(ConversationNotFoundError):
                    await operation()
            assert await counts(database) == (1, 1, 1)

    run(exercise())


@pytest.mark.parametrize("scenario", ["unattached", "foreign", "missing", "deleted"])
def test_unavailable_dataset_admission_leaves_no_partial_history(migration_settings, scenario):
    async def exercise():
        async with application(migration_settings) as (database, _, _, datasets):
            actor, store, cid, data = await fixture(database, datasets)
            if scenario in {"foreign", "unattached"}:
                data = await datasets.ingest(
                    owner(subject="other") if scenario == "foreign" else actor,
                    filename="other.csv",
                    content=CONTENT,
                )
            if scenario == "deleted":
                await datasets.delete(actor, data.dataset_id)
            target = uuid4() if scenario == "missing" else data.dataset_id
            with pytest.raises(RunInputUnavailableError):
                await store.admit(actor, cid, submission(target), configuration())
            assert await counts(database) == (0, 0, 0)

    run(exercise())


def test_invalid_attachment_replacement_rolls_back_existing_selection(migration_settings):
    async def exercise():
        async with application(migration_settings) as (database, _, _, datasets):
            actor, store, cid, data = await fixture(database, datasets)
            foreign = await datasets.ingest(
                owner(subject="other"), filename="foreign.csv", content=CONTENT
            )
            with pytest.raises(RunInputUnavailableError):
                await store.set_attachments(
                    actor, cid, DatasetSelection(dataset_ids=(foreign.dataset_id,))
                )
            assert await store.attachments(actor, cid) == (data,)

    run(exercise())


def test_history_and_replay_survive_new_store_and_connection_pool(migration_settings):
    async def exercise():
        async with application(migration_settings) as (database, _, _, datasets):
            actor, store, cid, data = await fixture(database, datasets)
            request, config = submission(data.dataset_id), configuration()
            first = await store.admit(actor, cid, request, config)
            await database.engine.dispose()
            fresh = ConversationStore(database)
            assert (await fresh.get_run(actor, first.run.run_id)).run == first.run
            assert (await fresh.admit(actor, cid, request, config)).replayed
            assert (await fresh.messages(actor, cid))[0].content == request.question
            # History belongs to identity, not to one browser login or role snapshot.
            same_identity = owner(roles=["user", "admin"])
            assert (await fresh.get_run(same_identity, first.run.run_id)).run == first.run
            assert "synthetic-storage-key" not in first.configuration.model_dump_json()
            assert {column.name for column in RunJob.__table__.columns} == {
                "run_id",
                "created_at",
                "auth_session_ref",
                "admission_grant",
                "queue_expires_at",
                "claim_id",
                "lease_expires_at",
            }

    run(exercise())


def test_pagination_history_sequence_and_terminal_deletion_preserve_datasets(migration_settings):
    async def exercise():
        async with application(migration_settings) as (database, artifacts, _, datasets):
            actor, store, cid, data = await fixture(database, datasets)
            for _ in range(3):
                admitted = await store.admit(
                    actor, cid, submission(data.dataset_id), configuration()
                )
                with pytest.raises(ConversationBusyError):
                    await store.delete(actor, cid)
                await finish_for_fixture(database, admitted.run.run_id)
            seen, cursor = [], 0
            while page := await store.messages(actor, cid, limit=1, after_sequence=cursor):
                seen.extend(message.sequence for message in page)
                cursor = page[-1].sequence
            assert seen == [1, 2, 3]
            for _ in range(2):
                await store.create(actor, title="Another")
            conversations, after = [], None
            while page := await store.list(actor, limit=1, after=after):
                conversations.extend(item.conversation_id for item in page)
                after = page[-1].conversation_id
            assert len(set(conversations)) == 3
            artifact = await artifacts.create_artifact(actor, data.dataset_id, {"result": 2})
            await store.delete(actor, cid)
            assert await counts(database) == (0, 0, 0)
            assert await datasets.get_metadata(actor, data.dataset_id) == data
            assert await artifacts.get_artifact(actor, artifact.reference.artifact_id)

    run(exercise())


def test_database_active_index_and_terminal_checks_cannot_be_bypassed(migration_settings):
    async def exercise():
        async with application(migration_settings) as (database, _, _, datasets):
            actor, store, cid, data = await fixture(database, datasets)
            admitted = await store.admit(actor, cid, submission(data.dataset_id), configuration())
            async with database.session() as session:
                row = await session.get(RunRecord, admitted.run.run_id)
                clone = {
                    column.name: getattr(row, column.name) for column in RunRecord.__table__.columns
                }
            clone.update(id=uuid4(), idempotency_key=uuid4())
            with pytest.raises(IntegrityError) as error:
                async with database.session() as session:
                    await session.execute(insert(RunRecord).values(**clone))
                    await session.commit()
            assert error.value.orig.diag.constraint_name == "uq_run_active"
            for change in [
                dict(lifecycle="finished", finished_at=datetime.now(UTC)),
                dict(lifecycle="running"),
                dict(outcome="completed"),
            ]:
                with pytest.raises(IntegrityError):
                    async with database.session() as session:
                        await session.execute(
                            update(RunRecord)
                            .where(RunRecord.id == admitted.run.run_id)
                            .values(**change)
                        )
                        await session.commit()
            assert await counts(database) == (1, 1, 1)

    run(exercise())


def test_upgrade_and_disposable_rollback_preserve_existing_datasets(migration_settings):
    async def exercise():
        async with sandbox(migration_settings) as (database, schema, config):
            await apply(database, schema, config, "upgrade", "0004_dataset_storage")
            from sqlalchemy import text

            # Fixture data only, before/after the additive history migration.
            async with database.session() as session:
                await session.execute(text(f"SET LOCAL search_path TO {schema}"))
                session.add(
                    Dataset(
                        id=uuid4(),
                        issuer="https://identity.example",
                        subject="analyst",
                        filename="data.csv",
                        sha256="0" * 64,
                        byte_count=1,
                        row_count=0,
                        columns=[
                            {
                                "name": "x",
                                "kind": "text",
                                "missing_count": 0,
                                "non_missing_count": 0,
                            }
                        ],
                        format="csv",
                        created_at=datetime.now(UTC),
                    )
                )
                await session.commit()
            for action, revision in [
                ("upgrade", "head"),
                ("downgrade", "0004_dataset_storage"),
                ("upgrade", "head"),
            ]:
                await apply(database, schema, config, action, revision)
                async with database.session() as session:
                    await session.execute(text(f"SET LOCAL search_path TO {schema}"))
                    assert await session.scalar(select(func.count()).select_from(Dataset)) == 1

    run(exercise())


@pytest.mark.parametrize(
    "scenario", ["valid", "deleted_artifact", "wrong_digest", "other_conversation"]
)
def test_prior_references_require_same_conversation_persisted_result_and_owned_artifact(
    migration_settings, scenario
):
    async def exercise():
        async with application(migration_settings) as (database, artifacts, _, datasets):
            actor, store, cid, data = await fixture(database, datasets)
            first = await store.admit(actor, cid, submission(data.dataset_id), configuration())
            artifact = await artifacts.create_artifact(actor, data.dataset_id, {"mean": 4})
            prior = ResultProvenance(
                conversation_id=cid,
                run_id=first.run.run_id,
                tool_call_id=uuid4(),
                dataset=DatasetSnapshot(dataset_id=data.dataset_id, sha256=data.sha256),
                artifact=artifact.reference,
            )
            # Synthetic completed-result storage fixture, not a worker/finalizer implementation.
            await finish_for_fixture(database, first.run.run_id)
            async with database.session() as session:
                await session.execute(
                    update(RunRecord)
                    .where(RunRecord.id == first.run.run_id)
                    .values(results=[prior.model_dump(mode="json")])
                )
                await session.commit()
            if scenario == "deleted_artifact":
                await datasets.delete(actor, data.dataset_id)
            elif scenario == "wrong_digest":
                raw = prior.model_dump()
                raw["artifact"] = artifact.reference.model_dump() | {"sha256": "0" * 64}
                prior = ResultProvenance(**raw)
            elif scenario == "other_conversation":
                cid = (await store.create(actor, title="Another chat")).conversation_id
            request = submission(prior_results=(prior,))
            if scenario == "valid":
                accepted = await store.admit(actor, cid, request, configuration())
                assert accepted.submission.prior_results == (prior,)
                assert await counts(database) == (2, 2, 2)
            else:
                with pytest.raises(RunInputUnavailableError):
                    await store.admit(actor, cid, request, configuration())
                assert await counts(database) == (1, 1, 1)

    run(exercise())


@pytest.mark.parametrize("change", ["question", "selection", "prior_results"])
def test_idempotency_fingerprint_includes_all_client_submission_content(migration_settings, change):
    async def exercise():
        async with application(migration_settings) as (database, _, _, datasets):
            actor, store, cid, data = await fixture(database, datasets)
            request = submission(data.dataset_id)
            accepted = await store.admit(actor, cid, request, configuration())
            raw = request.model_dump()
            if change == "question":
                raw["question"] = request.question + " "
            elif change == "selection":
                raw["selection"] = DatasetSelection()
            else:
                from statistics_agent.contracts.common import ArtifactReference

                raw["prior_results"] = (
                    ResultProvenance(
                        conversation_id=cid,
                        run_id=accepted.run.run_id,
                        tool_call_id=uuid4(),
                        dataset=DatasetSnapshot(dataset_id=data.dataset_id, sha256=data.sha256),
                        artifact=ArtifactReference(
                            artifact_id=uuid4(), sha256="0" * 64, media_type="application/json"
                        ),
                    ),
                )
            with pytest.raises(IdempotencyConflictError):
                await store.admit(actor, cid, RunSubmission(**raw), configuration())
            assert await counts(database) == (1, 1, 1)

    run(exercise())


def test_multiple_dataset_snapshots_keep_explicit_submission_order(migration_settings):
    async def exercise():
        async with application(migration_settings) as (database, _, _, datasets):
            actor, store, cid, first = await fixture(database, datasets)
            second = await datasets.ingest(actor, filename="second.csv", content=b"x\n9")
            selected = DatasetSelection(dataset_ids=(first.dataset_id, second.dataset_id))
            await store.set_attachments(actor, cid, selected)
            accepted = await store.admit(
                actor, cid, submission(second.dataset_id, first.dataset_id), configuration()
            )
            assert accepted.datasets == (second, first)
            assert [item.dataset_id for item in accepted.run.selection] == [
                second.dataset_id,
                first.dataset_id,
            ]

    run(exercise())
