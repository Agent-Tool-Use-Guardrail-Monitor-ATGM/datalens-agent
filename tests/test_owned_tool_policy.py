"""Real owned metadata authorization; no model, permissive runtime wiring or public API."""

import asyncio
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from statistics_agent.agent.authorization import OwnedToolPolicy, PolicyUnavailableError
from statistics_agent.agent.policy import PolicyRules
from statistics_agent.contracts.common import ConfigurationReference
from statistics_agent.contracts.policy import ToolExecutionScope, ToolPolicyConfiguration
from statistics_agent.persistence.models import DatasetArtifact
from statistics_agent.tools.statistics import StatisticsTools
from support.database import run
from support.datasets import application, context, owner, proposal

pytestmark = pytest.mark.postgres
DATA = b"x,y,g\n1,2,A\n2,4,A\n3,6,B"
OPERATIONS = [
    ("inspect_dataset", {}),
    ("describe_columns", {"columns": ["x"], "missing_policy": "omit"}),
    (
        "aggregate_statistics",
        {"column": "x", "group_by": "g", "operation": "sum", "missing_policy": "omit"},
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


def policy(actor, ids, datasets, *, authorize=None, enabled=None):
    async def current_access(current):
        return current == actor

    rules = PolicyRules(
        ToolPolicyConfiguration(
            reference=ConfigurationReference(config_id=uuid4(), version="owned-test-v1"),
            allowed_tools=tuple(name for name, _ in OPERATIONS) if enabled is None else enabled,
        )
    )
    return OwnedToolPolicy(
        scope=ToolExecutionScope(context=actor, dataset_ids=tuple(ids)),
        rules=rules,
        datasets=datasets,
        authorize=current_access if authorize is None else authorize,
    )


async def artifact_count(database):
    async with database.session() as session:
        return await session.scalar(select(func.count()).select_from(DatasetArtifact))


@pytest.mark.parametrize("roles", [("user",), ("admin",)])
@pytest.mark.parametrize("name,arguments", OPERATIONS)
def test_selected_owned_data_is_authorized_without_parsing_or_computing(
    migration_settings, monkeypatch, roles, name, arguments
):
    async def exercise():
        async with application(migration_settings) as (database, _, worker, datasets):
            actor = context(owner(roles=roles))
            metadata = await datasets.ingest(actor.principal, filename="fixture.csv", content=DATA)

            async def forbidden(*args, **kwargs):
                pytest.fail("Authorization must only read owned metadata")

            monkeypatch.setattr(worker, "parse", forbidden)
            monkeypatch.setattr(worker, "compute", forbidden)
            guard = policy(actor, [metadata.dataset_id], datasets)
            call = proposal(name, metadata.dataset_id, **arguments)
            decision = await guard.evaluate(actor, call)
            assert decision.allowed and decision.reason == "allowed"
            assert decision.tool_call_id == call.tool_call_id
            assert guard.scope.context == actor
            assert await artifact_count(database) == 0

    run(exercise())


@pytest.mark.parametrize(
    "changes",
    [
        {"subject": "other"},
        {"issuer": "https://another.example"},
        {"subject": "other", "roles": ["admin"]},
    ],
)
@pytest.mark.parametrize("name,arguments", OPERATIONS)
def test_selected_foreign_data_is_denied_including_admin(
    migration_settings, changes, name, arguments
):
    async def exercise():
        async with application(migration_settings) as (database, _, _, datasets):
            principal = owner()
            metadata = await datasets.ingest(principal, filename="fixture.csv", content=DATA)
            actor = context(owner(**changes))
            decision = await policy(actor, [metadata.dataset_id], datasets).evaluate(
                actor, proposal(name, metadata.dataset_id, **arguments)
            )
            assert not decision.allowed and decision.reason == "denied"
            assert await artifact_count(database) == 0

    run(exercise())


@pytest.mark.parametrize("state", ["missing", "deleted", "unselected"])
def test_unavailable_or_unselected_dataset_uses_same_safe_denial(migration_settings, state):
    async def exercise():
        async with application(migration_settings) as (_, _, _, datasets):
            actor = context(owner())
            metadata = await datasets.ingest(actor.principal, filename="fixture.csv", content=DATA)
            target = uuid4() if state == "missing" else metadata.dataset_id
            if state == "deleted":
                assert await datasets.delete(actor.principal, target)
            guard = policy(actor, [] if state == "unselected" else [target], datasets)
            result = await guard.evaluate(actor, proposal("inspect_dataset", target))
            assert result.reason == "denied" and not result.allowed

    run(exercise())


@pytest.mark.parametrize(
    "field", ["run_id", "session_id", "request_id", "principal", "auth_session_ref"]
)
def test_scope_cannot_be_reused_by_another_execution_or_identity(
    migration_settings, monkeypatch, field
):
    async def exercise():
        async with application(migration_settings) as (_, _, _, datasets):
            actor = context(owner())
            calls = []

            async def forbidden(*args):
                calls.append(args)
                pytest.fail("Mismatched scope must be rejected before access/storage")

            monkeypatch.setattr(datasets, "get_metadata", forbidden)
            if field == "principal":
                changed = actor.model_copy(update={"principal": owner(subject="other")})
            elif field == "auth_session_ref":
                changed = actor.model_copy(
                    update={
                        "principal": actor.principal.model_copy(
                            update={"auth_session_ref": uuid4()}
                        )
                    }
                )
            else:
                changed = actor.model_copy(update={field: uuid4()})
            target = uuid4()
            result = await policy(actor, [target], datasets, authorize=forbidden).evaluate(
                changed, proposal("inspect_dataset", target)
            )
            assert not result.allowed and not calls

    run(exercise())


@pytest.mark.parametrize("access", [False, 1, "true"])
def test_current_access_must_be_exactly_true_before_database_lookup(
    migration_settings, monkeypatch, access
):
    async def exercise():
        async with application(migration_settings) as (_, _, _, datasets):
            actor, calls = context(owner()), []

            async def authorize(_):
                return access

            async def unexpected(*args):
                calls.append(args)
                raise RuntimeError("Unexpected database read")

            monkeypatch.setattr(datasets, "get_metadata", unexpected)
            target = uuid4()
            result = await policy(actor, [target], datasets, authorize=authorize).evaluate(
                actor, proposal("inspect_dataset", target)
            )
            assert not result.allowed and not calls

    run(exercise())


@pytest.mark.parametrize("source", ["access", "database"])
def test_operational_failure_is_unavailable_not_a_denial_or_secret_leak(
    migration_settings, monkeypatch, source
):
    async def exercise():
        async with application(migration_settings) as (_, _, _, datasets):
            actor = context(owner())

            async def failure(*args):
                raise RuntimeError("private-backend-diagnostic")

            if source == "database":
                monkeypatch.setattr(datasets, "get_metadata", failure)
            target = uuid4()
            guard = policy(
                actor, [target], datasets, authorize=failure if source == "access" else None
            )
            call = proposal("inspect_dataset", target)
            with pytest.raises(PolicyUnavailableError) as caught:
                await guard.evaluate(actor, call)
            error = caught.value
            assert str(error) == "Tool authorization unavailable" and error.__context__ is None
            assert error.evaluation.status == "unavailable" and error.evaluation.decision is None
            assert error.evaluation.tool_call_id == call.tool_call_id
            assert error.evaluation.policy == guard.configuration.reference
            assert "private-backend" not in error.evaluation.model_dump_json()

    run(exercise())


@pytest.mark.parametrize("source", ["access", "database"])
def test_cancellation_propagates_without_a_fabricated_decision(
    migration_settings, monkeypatch, source
):
    async def exercise():
        async with application(migration_settings) as (_, _, _, datasets):
            actor = context(owner())

            async def cancel(*args):
                raise asyncio.CancelledError

            if source == "database":
                monkeypatch.setattr(datasets, "get_metadata", cancel)
            target = uuid4()
            with pytest.raises(asyncio.CancelledError):
                await policy(
                    actor, [target], datasets, authorize=cancel if source == "access" else None
                ).evaluate(actor, proposal("inspect_dataset", target))

    run(exercise())


@pytest.mark.parametrize(
    "name,arguments,allowed",
    [
        ("describe_columns", {"columns": ["missing"], "missing_policy": "omit"}, False),
        ("describe_columns", {"columns": ["g"], "missing_policy": "omit"}, False),
        (
            "aggregate_statistics",
            {"column": "x", "group_by": "missing", "operation": "sum", "missing_policy": "omit"},
            False,
        ),
        (
            "aggregate_statistics",
            {"column": "g", "operation": "sum", "missing_policy": "omit"},
            False,
        ),
        (
            "aggregate_statistics",
            {"column": "g", "operation": "count", "missing_policy": "omit"},
            True,
        ),
        (
            "correlate_columns",
            {
                "x_column": "x",
                "y_column": "g",
                "method": "pearson",
                "missing_policy": "pairwise_complete",
            },
            False,
        ),
    ],
)
def test_column_selection_matches_fixed_numerical_tool_semantics(
    migration_settings, name, arguments, allowed
):
    async def exercise():
        async with application(migration_settings) as (database, _, _, datasets):
            actor = context(owner())
            metadata = await datasets.ingest(actor.principal, filename="fixture.csv", content=DATA)
            result = await policy(actor, [metadata.dataset_id], datasets).evaluate(
                actor, proposal(name, metadata.dataset_id, **arguments)
            )
            assert result.allowed is allowed
            assert result.reason == ("allowed" if allowed else "invalid_arguments")
            assert await artifact_count(database) == 0

    run(exercise())


def test_all_missing_numeric_sample_remains_an_unavailable_computation_not_authorization_failure(
    migration_settings,
):
    async def exercise():
        async with application(migration_settings) as (_, _, _, datasets):
            actor = context(owner())
            metadata = await datasets.ingest(
                actor.principal, filename="fixture.csv", content=b'x\n""'
            )
            result = await policy(actor, [metadata.dataset_id], datasets).evaluate(
                actor,
                proposal(
                    "describe_columns", metadata.dataset_id, columns=["x"], missing_policy="omit"
                ),
            )
            assert result.allowed

    run(exercise())


def test_delete_after_policy_allow_is_still_denied_by_actual_executor(migration_settings):
    async def exercise():
        async with application(migration_settings) as (database, store, worker, datasets):
            actor = context(owner())
            metadata = await datasets.ingest(actor.principal, filename="fixture.csv", content=DATA)
            call = proposal("inspect_dataset", metadata.dataset_id)
            assert (
                await policy(actor, [metadata.dataset_id], datasets).evaluate(actor, call)
            ).allowed
            assert await datasets.delete(actor.principal, metadata.dataset_id)
            result = await StatisticsTools(datasets, worker=worker, artifacts=store).execute(
                actor, call
            )
            assert result.status == "failed" and result.artifact is None
            assert await artifact_count(database) == 0

    run(exercise())


@pytest.mark.parametrize(
    "name,arguments,enabled,reason",
    [
        ("unknown", {}, None, "unknown_tool"),
        ("finalize_statistics", {}, None, "unknown_tool"),
        ("request_clarification", {}, None, "unknown_tool"),
        ("inspect_dataset", {}, (), "denied"),
        (
            "inspect_dataset",
            {"dataset_ids": [str(uuid4()), str(uuid4())]},
            None,
            "invalid_arguments",
        ),
        (
            "describe_columns",
            {"columns": ["x", "x"], "missing_policy": "omit"},
            None,
            "invalid_arguments",
        ),
        (
            "aggregate_statistics",
            {"column": "x", "operation": "eval", "missing_policy": "omit"},
            None,
            "invalid_arguments",
        ),
    ],
)
def test_static_denials_cannot_reach_owned_storage(
    migration_settings, monkeypatch, name, arguments, enabled, reason
):
    async def exercise():
        async with application(migration_settings) as (database, _, _, datasets):
            actor, calls = context(owner()), []

            async def unexpected(*args):
                calls.append(args)
                raise RuntimeError("Unexpected storage access")

            monkeypatch.setattr(datasets, "get_metadata", unexpected)
            target = uuid4()
            result = await policy(actor, [target], datasets, enabled=enabled).evaluate(
                actor, proposal(name, target, **arguments)
            )
            assert not result.allowed and result.reason == reason and not calls
            assert await artifact_count(database) == 0

    run(exercise())


def test_metadata_lookup_cannot_substitute_a_different_dataset(migration_settings, monkeypatch):
    async def exercise():
        async with application(migration_settings) as (_, _, _, datasets):
            actor = context(owner())
            metadata = await datasets.ingest(actor.principal, filename="fixture.csv", content=DATA)

            async def substituted(*args):
                return metadata

            monkeypatch.setattr(datasets, "get_metadata", substituted)
            target = uuid4()
            with pytest.raises(PolicyUnavailableError) as caught:
                await policy(actor, [target], datasets).evaluate(
                    actor, proposal("inspect_dataset", target)
                )
            assert caught.value.__context__ is None
            assert caught.value.evaluation.status == "unavailable"

    run(exercise())
