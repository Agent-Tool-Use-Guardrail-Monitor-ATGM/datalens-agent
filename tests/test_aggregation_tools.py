"""Manual aggregation/correlation expectations and real owned executor checks."""

import json
import math
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select

from statistics_agent.contracts.datasets import (
    AggregateStatisticsArguments,
    CorrelateColumnsArguments,
    StatisticsLimits,
)
from statistics_agent.contracts.statistics import CorrelationResult
from statistics_agent.datasets.csv import parse_csv
from statistics_agent.persistence.models import DatasetArtifact
from statistics_agent.tools.aggregation import aggregate_statistics, correlate_columns
from statistics_agent.tools.descriptive import StatisticsToolError
from statistics_agent.tools.statistics import MAX_SUMMARY_BYTES, StatisticsTools
from test_dataset_service import application
from test_dataset_storage import owner
from test_migrations import run
from test_migrations import settings as settings
from test_statistics_tool_executor import context, proposal


def aggregate(
    raw, *, operation="sum", group_by=None, missing_policy="omit", limits=None, column="x"
):
    return aggregate_statistics(
        parse_csv(raw),
        AggregateStatisticsArguments(
            dataset_id=uuid4(),
            column=column,
            operation=operation,
            group_by=group_by,
            missing_policy=missing_policy,
        ),
        limits or StatisticsLimits(),
    )


def correlate(raw, *, method="pearson", missing_policy="pairwise_complete", **updates):
    return correlate_columns(
        parse_csv(raw),
        CorrelateColumnsArguments(
            **(
                {
                    "dataset_id": uuid4(),
                    "x_column": "x",
                    "y_column": "y",
                    "method": method,
                    "missing_policy": missing_policy,
                }
                | updates
            )
        ),
        StatisticsLimits(),
    )


@pytest.mark.parametrize(
    "operation,expected", [("count", 3), ("sum", 12.0), ("mean", 4.0), ("min", 2.0), ("max", 6.0)]
)
def test_ungrouped_operations_match_hand_arithmetic(operation, expected):
    result = aggregate(b"x\n2\n4\n6", operation=operation)
    assert len(result.groups) == 1 and result.groups[0].key is None
    assert result.groups[0].value == expected
    assert result.groups[0].status == "available" and result.valid_count == 3
    assert result.dropped_row_count == 0
    if operation == "count":
        assert type(result.groups[0].value) is int


def test_grouped_missing_counts_include_union_without_double_counting():
    result = aggregate(b'g,x\nA,1\nA,\nB,3\n,4\nB,5\n,""', group_by="g")
    assert [(g.key, g.value, g.valid_count, g.missing_count) for g in result.groups] == [
        ("A", 1.0, 1, 1),
        ("B", 8.0, 2, 0),
    ]
    assert (
        result.row_count,
        result.valid_count,
        result.missing_value_count,
        result.missing_group_count,
        result.dropped_row_count,
    ) == (6, 3, 2, 2, 3)


def test_group_keys_preserve_text_and_first_encounter_order_without_cleaning():
    result = aggregate(b"g,x\nB,2\n A ,3\nA,4\nB,5", group_by="g")
    assert [(g.key, g.value) for g in result.groups] == [("B", 7.0), (" A ", 3.0), ("A", 4.0)]


def test_count_accepts_text_and_all_missing_groups_have_explicit_outcomes():
    result = aggregate(b"g,x\nG,\nG,\nH,text", operation="count", group_by="g")
    assert [(g.key, g.value, g.valid_count, g.missing_count) for g in result.groups] == [
        ("G", 0, 0, 2),
        ("H", 1, 1, 0),
    ]
    missing = aggregate(b"g,x\nG,\nG,\nH,2", group_by="g")
    assert missing.groups[0].value is None and missing.groups[0].reason == "insufficient_data"
    assert missing.groups[0].status == "unavailable" and missing.groups[1].value == 2.0


@pytest.mark.parametrize("operation", ["sum", "mean", "min", "max"])
def test_empty_numeric_aggregations_are_unavailable_not_zero(operation):
    result = aggregate(b"x", operation=operation)
    assert result.groups[0].value is None and result.groups[0].reason == "insufficient_data"
    assert result.valid_count == result.dropped_row_count == 0
    assert aggregate(b"x", operation="count").groups[0].value == 0


@pytest.mark.parametrize("raw", [b"g,x\nA,", b"g,x\n,2"])
def test_missing_value_or_group_with_error_policy_rejects(raw):
    with pytest.raises(StatisticsToolError) as error:
        aggregate(raw, group_by="g", missing_policy="error")
    assert error.value.failure.code == "missing_values"


@pytest.mark.parametrize(
    "updates,raw,code",
    [
        ({"column": "absent"}, b"x\n1", "invalid_column"),
        ({"group_by": "absent"}, b"x\n1", "invalid_column"),
        ({}, b"x\ntext", "non_numeric_column"),
        ({}, b"x\n1e308\n1e308", "numeric_range_exceeded"),
    ],
)
def test_aggregation_rejects_bad_columns_types_and_overflow(updates, raw, code):
    with pytest.raises(StatisticsToolError) as error:
        aggregate(raw, **updates)
    assert error.value.failure.code == code


def test_group_limit_has_exact_and_lowered_boundaries_including_empty_groups():
    raw = ("g,x\n" + "\n".join(f"g{i},1" for i in range(50))).encode()
    assert len(aggregate(raw, group_by="g").groups) == 50
    for content, limits in (
        (raw + b"\ng50,1", StatisticsLimits()),
        (b"g,x\nA,1\nB,2\nC,3", StatisticsLimits(max_groups=2)),
        (b"g,x\nA,\nB,\nC,", StatisticsLimits(max_groups=2)),
    ):
        with pytest.raises(StatisticsToolError) as error:
            aggregate(content, group_by="g", limits=limits)
        assert error.value.failure.code == "group_limit_exceeded"


@pytest.mark.parametrize(
    "raw,method,expected",
    [
        (b"x,y\n1,2\n2,4\n3,6", "pearson", 1.0),
        (b"x,y\n1,6\n2,4\n3,2", "pearson", -1.0),
        (b"x,y\n-1,1\n0,0\n1,1", "pearson", 0.0),
        (b"x,y\n1,1\n2,4\n3,9", "pearson", 4 * math.sqrt(3) / 7),
        (b"x,y\n1,1\n2,4\n3,9", "spearman", 1.0),
        (b"x,y\n1,1\n1,2\n2,2\n3,3", "spearman", 5 / 6),
    ],
)
def test_correlations_match_known_coefficients_and_average_tie_ranks(raw, method, expected):
    result = correlate(raw, method=method)
    assert result.coefficient == pytest.approx(expected, abs=1e-12)
    assert result.status == "available" and result.reason is None
    assert result.rank_method == ("average" if method == "spearman" else None)
    assert "p_value" not in result.model_dump()


def test_pairwise_complete_missing_data_reports_sample_and_dropped_pairs():
    raw = b'x,y\n1,2\n"",4\n3,""\n4,8\n5,10'
    result = correlate(raw)
    assert (result.row_count, result.sample_size, result.dropped_pair_count) == (5, 3, 2)
    assert result.coefficient == pytest.approx(1.0)
    with pytest.raises(StatisticsToolError) as error:
        correlate(raw, missing_policy="error")
    assert error.value.failure.code == "missing_values"


@pytest.mark.parametrize(
    "raw,reason,pairs",
    [
        (b"x,y", "insufficient_data", 0),
        (b"x,y\n1,2", "insufficient_data", 1),
        (b"x,y\n1,2\n2,4", "insufficient_data", 2),
        (b"x,y\n1,1\n1,2\n1,3", "constant_input", 3),
        (b"x,y\n1,1\n2,1\n3,1", "constant_input", 3),
        (b"x,y\n,1\n,2\n,3", "insufficient_data", 0),
    ],
)
def test_undefined_correlations_have_explicit_unavailable_results(raw, reason, pairs):
    result = correlate(raw)
    assert result.status == "unavailable" and result.reason == reason
    assert result.coefficient is None and result.sample_size == pairs


@pytest.mark.parametrize(
    "raw,updates,code",
    [
        (b"x,y\n1,2", {"x_column": "absent"}, "invalid_column"),
        (b"x,y\ntext,2", {}, "non_numeric_column"),
        (b"x,y\n1e308,0\n-1e308,1\n0,2", {}, "numeric_range_exceeded"),
    ],
)
def test_correlation_invalid_types_and_overflow_do_not_emit_nonfinite_coefficients(
    raw, updates, code
):
    with pytest.raises(StatisticsToolError) as error:
        correlate(raw, **updates)
    assert error.value.failure.code == code


def test_correlation_result_contract_rejects_nonfinite_or_inconsistent_outputs():
    valid = correlate(b"x,y\n1,2\n2,4\n3,6").model_dump()
    for updates in (
        {"coefficient": float("nan")},
        {"coefficient": 1.1},
        {"sample_size": 2},
        {"status": "unavailable"},
        {"p_value": 0.01},
    ):
        with pytest.raises(ValidationError):
            CorrelationResult.model_validate(valid | updates)


@pytest.mark.postgres
class TestAggregateCorrelationExecutor:
    @pytest.mark.parametrize(
        "name,args,raw",
        [
            (
                "aggregate_statistics",
                {"column": "x", "operation": "sum", "group_by": "g", "missing_policy": "omit"},
                b"g,x\nA,2\nA,4\nB,6",
            ),
            (
                "correlate_columns",
                {
                    "x_column": "x",
                    "y_column": "y",
                    "method": "spearman",
                    "missing_policy": "pairwise_complete",
                },
                b"x,y\n1,1\n2,4\n3,9",
            ),
        ],
    )
    def test_new_bindings_persist_correct_owned_typed_artifacts(self, settings, name, args, raw):
        async def exercise():
            async with application(settings) as (_, store, worker, datasets):
                principal = owner()
                value = await datasets.ingest(principal, filename="data.csv", content=raw)
                request = proposal(name, value.dataset_id, **args)
                result = await StatisticsTools(datasets, worker=worker, artifacts=store).execute(
                    context(principal), request
                )
                assert result.status == "completed" and result.tool_call_id == request.tool_call_id
                assert len(result.summary.encode()) <= MAX_SUMMARY_BYTES
                artifact = await store.get_artifact(principal, result.artifact.artifact_id)
                assert artifact.payload["tool_call_id"] == str(request.tool_call_id)
                typed = artifact.payload["result"]
                if name == "aggregate_statistics":
                    assert [group["value"] for group in typed["groups"]] == [6.0, 6.0]
                else:
                    assert typed["coefficient"] == pytest.approx(1.0) and "p_value" not in typed

        run(exercise())

    @pytest.mark.parametrize(
        "name,args,expected",
        [
            (
                "aggregate_statistics",
                {"column": "x", "operation": "sum", "group_by": "g", "missing_policy": "omit"},
                "group_limit_exceeded",
            ),
            (
                "correlate_columns",
                {"x_column": "x", "y_column": "y", "method": "pearson", "missing_policy": "error"},
                "missing_values",
            ),
            (
                "aggregate_statistics",
                {"column": "x", "operation": "sum", "missing_policy": "omit", "join": "other"},
                "invalid_arguments",
            ),
            (
                "correlate_columns",
                {
                    "x_column": "x",
                    "y_column": "y",
                    "method": "pearson",
                    "missing_policy": "pairwise_complete",
                    "y_dataset_id": "other",
                },
                "invalid_arguments",
            ),
        ],
    )
    def test_rejected_calls_create_no_success_artifacts(self, settings, name, args, expected):
        async def exercise():
            async with application(settings) as (database, store, worker, datasets):
                principal = owner()
                value = await datasets.ingest(
                    principal, filename="data.csv", content=b"g,x,y\nA,1,2\nB,2,\nC,3,6"
                )
                tools = StatisticsTools(
                    datasets, worker=worker, artifacts=store, limits=StatisticsLimits(max_groups=2)
                )
                result = await tools.execute(
                    context(principal), proposal(name, value.dataset_id, **args)
                )
                assert result.status == "failed" and json.loads(result.summary) == {
                    "error": expected
                }
                async with database.session() as session:
                    assert (
                        await session.scalar(select(func.count()).select_from(DatasetArtifact)) == 0
                    )

        run(exercise())

    def test_grouped_large_result_uses_complete_artifact_and_explicit_notice(self, settings):
        async def exercise():
            async with application(settings) as (_, store, worker, datasets):
                principal = owner()
                raw = ("g,x\n" + "\n".join(f"G{i},1" for i in range(20))).encode()
                value = await datasets.ingest(principal, filename="groups.csv", content=raw)
                request = proposal(
                    "aggregate_statistics",
                    value.dataset_id,
                    column="x",
                    operation="count",
                    group_by="g",
                    missing_policy="omit",
                )
                result = await StatisticsTools(datasets, worker=worker, artifacts=store).execute(
                    context(principal), request
                )
                assert result.status == "completed"
                notice = json.loads(result.summary)
                assert notice["details_in_artifact"] is True and notice["group_count"] == 20
                artifact = await store.get_artifact(principal, result.artifact.artifact_id)
                assert len(artifact.payload["result"]["groups"]) == 20
                assert all(group["value"] == 1 for group in artifact.payload["result"]["groups"])

        run(exercise())

    @pytest.mark.parametrize(
        "name,args",
        [
            ("aggregate_statistics", {"column": "x", "operation": "sum", "missing_policy": "omit"}),
            (
                "correlate_columns",
                {
                    "x_column": "x",
                    "y_column": "y",
                    "method": "pearson",
                    "missing_policy": "pairwise_complete",
                },
            ),
        ],
    )
    def test_new_tools_enforce_owner_boundary(self, settings, name, args):
        async def exercise():
            async with application(settings) as (_, store, worker, datasets):
                value = await datasets.ingest(
                    owner(), filename="data.csv", content=b"x,y\n1,2\n2,4\n3,6"
                )
                result = await StatisticsTools(datasets, worker=worker, artifacts=store).execute(
                    context(owner(subject="other")), proposal(name, value.dataset_id, **args)
                )
                assert result.status == "failed" and json.loads(result.summary) == {
                    "error": "dataset_not_found"
                }

        run(exercise())
