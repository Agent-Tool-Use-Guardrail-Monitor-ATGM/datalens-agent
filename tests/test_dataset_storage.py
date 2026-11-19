"""Owned storage and additive migration proof on disposable PostgreSQL schemas."""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import event, func, select, text, update
from sqlalchemy.exc import IntegrityError

from statistics_agent.contracts.datasets import DatasetLimits, StatisticsLimits
from statistics_agent.persistence.datasets import DatasetNotFoundError, DatasetStore, artifact_json
from statistics_agent.persistence.models import (
    Base,
    Dataset,
    DatasetArtifact,
    DatasetContent,
    LoginAttempt,
)
from support.database import apply, run, sandbox
from support.datasets import COLUMNS, CONTENT, owner, storage


async def create(store, principal):
    return await store.create(
        principal, filename="private.csv", content=CONTENT, row_count=3, columns=COLUMNS
    )


@pytest.mark.parametrize(
    "payload",
    [
        {"value": float("nan")},
        {"value": float("inf")},
        {"value": b"bytes"},
        {1: "not-a-string-key"},
        {"key": "nul\x00value"},
        {"nul\x00key": 1},
        {"value": "x" * 16384},
        {"value": "\ud800"},
        [1, 2],
    ],
)
def test_artifact_serialization_rejects_non_json_or_oversized_payloads(payload):
    with pytest.raises(ValueError):
        artifact_json(payload, 16384)


def test_artifact_serialization_bounds_depth_cycles_and_utf8_bytes():
    cycle = {}
    cycle["child"] = cycle
    with pytest.raises(ValueError, match="structure"):
        artifact_json(cycle, 16384)
    with pytest.raises(ValueError):
        artifact_json({"value": "\u20ac" * 6000}, 16384)
    encoded, raw = artifact_json({"mean": 4.0}, 16384)
    assert encoded == '{"mean":4.0}' and raw == encoded.encode("utf-8")


@pytest.mark.postgres
class TestDatasetStorage:
    def test_committed_roundtrip_and_metadata_reads_do_not_load_raw_data(self, migration_settings):
        async def exercise():
            async with storage(migration_settings) as (database, store):
                principal = owner()
                created = await create(store, principal)
                reopened = DatasetStore(database)
                statements = []

                def capture(connection, cursor, statement, parameters, context, many):
                    statements.append(statement)

                event.listen(database.engine.sync_engine, "before_cursor_execute", capture)
                try:
                    assert await reopened.get_metadata(principal, created.dataset_id) == created
                    assert await reopened.list_metadata(principal) == (created,)
                finally:
                    event.remove(database.engine.sync_engine, "before_cursor_execute", capture)
                assert all("agent_dataset_contents" not in sql for sql in statements)
                assert await reopened.get_content(principal, created.dataset_id) == CONTENT
                assert created.sha256 == hashlib.sha256(CONTENT).hexdigest()
                assert created.byte_count == len(CONTENT)

        run(exercise())

    @pytest.mark.parametrize(
        "other",
        [
            {"subject": "other"},
            {"issuer": "https://another.example/realms/datalens"},
            {"subject": "admin", "roles": ["admin", "user"]},
        ],
    )
    def test_owner_isolation_and_unknown_id_parity_including_admins(
        self, migration_settings, other
    ):
        async def exercise():
            async with storage(migration_settings) as (_, store):
                principal, intruder = owner(), owner(**other)
                value = await create(store, principal)
                artifact = await store.create_artifact(principal, value.dataset_id, {"mean": 4})
                assert not await store.list_metadata(intruder)
                for dataset_id in (value.dataset_id, uuid4()):
                    for operation in (
                        lambda identifier=dataset_id: store.get_metadata(intruder, identifier),
                        lambda identifier=dataset_id: store.get_content(intruder, identifier),
                        lambda identifier=dataset_id: store.create_artifact(
                            intruder, identifier, {"mean": 4}
                        ),
                    ):
                        with pytest.raises(DatasetNotFoundError) as error:
                            await operation()
                        assert str(error.value) == "Dataset resource unavailable"
                    assert await store.delete(intruder, dataset_id) is False
                for artifact_id in (artifact.reference.artifact_id, uuid4()):
                    with pytest.raises(DatasetNotFoundError, match="resource unavailable"):
                        await store.get_artifact(intruder, artifact_id)
                    assert await store.delete_artifact(intruder, artifact_id) is False
                assert await store.get_content(principal, value.dataset_id) == CONTENT

        run(exercise())

    def test_artifact_bytes_checksum_and_numbers_roundtrip_without_jsonb_normalization(
        self, migration_settings
    ):
        async def exercise():
            async with storage(migration_settings) as (database, store):
                principal = owner()
                value = await create(store, principal)
                payload = {"mean": 4.0, "negative_zero": -0.0, "large": 1e20, "unit": "\u20ac"}
                artifact = await store.create_artifact(principal, value.dataset_id, payload)
                read = await DatasetStore(database).get_artifact(
                    principal, artifact.reference.artifact_id
                )
                assert read == artifact and read.payload == payload
                assert str(read.payload["negative_zero"]) == "-0.0"
                assert "\u20ac" not in repr(read)
                async with database.session() as session:
                    row = await session.get(DatasetArtifact, artifact.reference.artifact_id)
                    raw = row.payload_json.encode("utf-8")
                    assert row.byte_count == len(raw)
                    assert hashlib.sha256(raw).hexdigest() == artifact.reference.sha256
                    assert json.loads(raw) == payload
                assert await store.delete_artifact(principal, artifact.reference.artifact_id)
                assert not await store.delete_artifact(principal, artifact.reference.artifact_id)

        run(exercise())

    def test_bounded_keyset_listing_and_distinct_duplicate_uploads(self, migration_settings):
        async def exercise():
            async with storage(migration_settings) as (_, store):
                principal = owner()
                values = [await create(store, principal) for _ in range(3)]
                expected = sorted(v.dataset_id for v in values)
                seen, after = [], None
                while page := await store.list_metadata(principal, limit=1, after=after):
                    after = page[0].dataset_id
                    seen.append(after)
                assert seen == expected and len(set(seen)) == 3
                for limit in (0, 101, True, "1"):
                    with pytest.raises(ValueError):
                        await store.list_metadata(principal, limit=limit)

        run(exercise())

    def test_failed_content_insert_rolls_back_metadata_and_releases_connection(
        self, migration_settings
    ):
        async def exercise():
            async with storage(migration_settings) as (database, store):
                principal = owner()

                def fail(connection, cursor, statement, parameters, context, many):
                    if statement.startswith("INSERT INTO agent_dataset_contents"):
                        raise RuntimeError("Controlled content failure")

                event.listen(database.engine.sync_engine, "before_cursor_execute", fail)
                try:
                    with pytest.raises(RuntimeError, match="Controlled content"):
                        await create(store, principal)
                finally:
                    event.remove(database.engine.sync_engine, "before_cursor_execute", fail)
                assert await store.list_metadata(principal) == ()
                async with database.session() as session:
                    assert (
                        await session.scalar(select(func.count()).select_from(DatasetContent)) == 0
                    )
                assert await create(store, principal)

        run(exercise())

    def test_parent_deletion_cascades_raw_content_and_artifacts(self, migration_settings):
        async def exercise():
            async with storage(migration_settings) as (database, store):
                principal = owner()
                value = await create(store, principal)
                artifact = await store.create_artifact(principal, value.dataset_id, {"mean": 4})
                assert await store.delete(principal, value.dataset_id)
                assert not await store.delete(principal, value.dataset_id)
                with pytest.raises(DatasetNotFoundError):
                    await store.get_artifact(principal, artifact.reference.artifact_id)
                async with database.session() as session:
                    for model in (Dataset, DatasetContent, DatasetArtifact):
                        assert await session.scalar(select(func.count()).select_from(model)) == 0

        run(exercise())

    def test_lowered_limits_are_applied_before_writes(self, migration_settings):
        async def exercise():
            async with storage(migration_settings) as (database, store):
                principal = owner()
                bounded = DatasetStore(database, DatasetLimits(max_upload_bytes=1))
                with pytest.raises(ValueError):
                    await create(bounded, principal)
                bounded = DatasetStore(database, DatasetLimits(max_rows=2))
                with pytest.raises(ValueError):
                    await create(bounded, principal)
                assert await store.list_metadata(principal) == ()
                value = await create(store, principal)
                bounded = DatasetStore(
                    database, statistics_limits=StatisticsLimits(max_result_bytes=8)
                )
                with pytest.raises(ValueError):
                    await bounded.create_artifact(principal, value.dataset_id, {"mean": 4})
                async with database.session() as session:
                    assert (
                        await session.scalar(select(func.count()).select_from(DatasetArtifact)) == 0
                    )

        run(exercise())

    def test_integrity_mismatches_fail_without_returning_corrupted_data(self, migration_settings):
        async def exercise():
            async with storage(migration_settings) as (database, store):
                principal = owner()
                value = await create(store, principal)
                artifact = await store.create_artifact(principal, value.dataset_id, {"mean": 4})
                async with database.session() as session:
                    await session.execute(update(DatasetContent).values(data=b"x" * len(CONTENT)))
                    await session.execute(update(DatasetArtifact).values(payload_json='{"mean":5}'))
                    await session.commit()
                with pytest.raises(ValueError, match="dataset integrity"):
                    await store.get_content(principal, value.dataset_id)
                with pytest.raises(ValueError, match="artifact integrity"):
                    await store.get_artifact(principal, artifact.reference.artifact_id)

        run(exercise())

    def test_database_foreign_keys_and_payload_checks_enforce_storage_boundaries(
        self, migration_settings
    ):
        async def exercise():
            async with storage(migration_settings) as (database, store):
                principal = owner()
                value = await create(store, principal)
                artifact = await store.create_artifact(principal, value.dataset_id, {"mean": 4})
                for change in ({"dataset_id": uuid4()}, {"payload_json": "[]", "byte_count": 2}):
                    with pytest.raises(IntegrityError):
                        async with database.session() as session:
                            await session.execute(update(DatasetArtifact).values(**change))
                            await session.commit()
                assert await store.get_artifact(principal, artifact.reference.artifact_id)

        run(exercise())

    def test_migrated_schema_matches_models(self, migration_settings):
        async def exercise():
            async with storage(migration_settings) as (database, _):
                async with database.engine.connect() as connection:

                    def differences(sync):
                        return compare_metadata(MigrationContext.configure(sync), Base.metadata)

                    assert await connection.run_sync(differences) == []

        run(exercise())

    def test_additive_upgrade_and_test_rollback_preserve_existing_auth_rows(
        self, migration_settings
    ):
        async def exercise():
            async with sandbox(migration_settings) as (database, schema, config):
                await apply(database, schema, config, "upgrade", "0003_refresh_claim")
                identifier = uuid4()
                now = datetime.now(UTC)
                async with database.session() as session:
                    await session.execute(text(f"SET LOCAL search_path TO {schema}"))
                    session.add(
                        LoginAttempt(
                            id=identifier,
                            state_hash="1" * 64,
                            browser_hash="2" * 64,
                            issuer="https://identity.example",
                            return_target="/",
                            nonce_ciphertext="synthetic-preserved-ciphertext",
                            verifier_ciphertext="synthetic-verifier",
                            created_at=now,
                            expires_at=now + timedelta(minutes=5),
                        )
                    )
                    await session.commit()
                for action, revision in (
                    ("upgrade", "head"),
                    ("upgrade", "head"),
                    ("downgrade", "0003_refresh_claim"),
                    ("upgrade", "head"),
                ):
                    await apply(database, schema, config, action, revision)
                    async with database.session() as session:
                        await session.execute(text(f"SET LOCAL search_path TO {schema}"))
                        row = await session.get(LoginAttempt, identifier)
                        assert row.nonce_ciphertext == "synthetic-preserved-ciphertext"

        run(exercise())
