"""Executable dataset/tool contracts; no CSV, database, provider or numerical runtime."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from statistics_agent.contracts.datasets import (
    AggregateStatisticsArguments,
    CorrelateColumnsArguments,
    DatasetColumn,
    DatasetFailure,
    DatasetLimits,
    DatasetMetadata,
    DatasetPreview,
    DescribeColumnsArguments,
    InspectDatasetArguments,
    StatisticsLimits,
)


def metadata(**updates):
    data = {
        "dataset_id": uuid4(),
        "filename": "private-table.csv",
        "sha256": "a" * 64,
        "byte_count": 32,
        "row_count": 3,
        "columns": [
            {"name": "price", "kind": "numeric", "non_missing_count": 2, "missing_count": 1}
        ],
        "created_at": datetime(2026, 11, 3, tzinfo=UTC),
    }
    return DatasetMetadata(**(data | updates))


def arguments(model, **updates):
    data = {"dataset_id": uuid4()}
    if model is DescribeColumnsArguments:
        data.update(columns=["price"], missing_policy="omit")
    elif model is AggregateStatisticsArguments:
        data.update(column="price", operation="mean", missing_policy="omit")
    elif model is CorrelateColumnsArguments:
        data.update(
            x_column="price",
            y_column="quantity",
            method="pearson",
            missing_policy="pairwise_complete",
        )
    return model(**(data | updates))


TOOLS = (
    InspectDatasetArguments,
    DescribeColumnsArguments,
    AggregateStatisticsArguments,
    CorrelateColumnsArguments,
)


def test_initial_limits_and_fixed_numerical_semantics():
    limits = DatasetLimits()
    assert (limits.max_upload_bytes, limits.max_rows, limits.max_columns) == (
        5 * 1024 * 1024,
        10000,
        128,
    )
    assert (limits.max_column_name, limits.max_cell_characters, limits.max_preview_rows) == (
        128,
        1024,
        20,
    )
    assert (limits.format, limits.encoding, limits.delimiter) == ("csv", "utf-8-sig", ",")
    stats = StatisticsLimits()
    assert (stats.max_groups, stats.max_result_bytes) == (50, 16384)
    assert stats.standard_deviation_ddof == 0 and stats.quantile_method == "linear"
    assert stats.minimum_correlation_pairs == 3
    assert DatasetLimits(max_rows=10).max_rows == 10


@pytest.mark.parametrize(
    "updates",
    [
        {"standard_deviation_ddof": 1},
        {"standard_deviation_ddof": False},
        {"minimum_correlation_pairs": 2},
        {"minimum_correlation_pairs": "3"},
        {"quantile_method": "nearest"},
    ],
)
def test_fixed_statistical_semantics_cannot_be_overridden(updates):
    with pytest.raises(ValidationError):
        StatisticsLimits(**updates)


def test_preview_accepts_exact_row_column_and_cell_boundaries():
    columns = tuple(f"c{i}" for i in range(128))
    row = ("x" * 1024,) + (None,) * 127
    preview = DatasetPreview(dataset_id=uuid4(), row_count=10000, columns=columns, rows=[row] * 20)
    assert len(preview.rows) == 20 and len(preview.columns) == 128
    with pytest.raises(ValidationError):
        DatasetPreview(dataset_id=uuid4(), row_count=1, columns=(*columns, "extra"))


@pytest.mark.parametrize(
    "model,field,ceiling",
    [
        (DatasetLimits, "max_upload_bytes", 5 * 1024 * 1024),
        (DatasetLimits, "max_rows", 10000),
        (DatasetLimits, "max_columns", 128),
        (DatasetLimits, "max_column_name", 128),
        (DatasetLimits, "max_cell_characters", 1024),
        (DatasetLimits, "max_preview_rows", 20),
        (StatisticsLimits, "max_groups", 50),
        (StatisticsLimits, "max_result_bytes", 16384),
    ],
)
def test_server_limits_cannot_exceed_ceilings_or_coerce_counts(model, field, ceiling):
    for value in (0, ceiling + 1, True, "1", 1.5):
        with pytest.raises(ValidationError):
            model(**{field: value})


def test_metadata_and_preview_roundtrip_with_explicit_missing_cells():
    value = metadata()
    assert DatasetMetadata.model_validate_json(value.model_dump_json()) == value
    preview = DatasetPreview(
        dataset_id=value.dataset_id, row_count=3, columns=["price"], rows=[["2"], [None], ["6"]]
    )
    assert preview.rows == (("2",), (None,), ("6",))
    assert DatasetPreview.model_validate_json(preview.model_dump_json()) == preview
    assert value.filename not in repr(value) and "price" not in repr(value)
    with pytest.raises(ValidationError):
        value.row_count = 4


@pytest.mark.parametrize("name", ["", " ", " price", "price ", "x" * 129, 42])
def test_columns_require_normalized_bounded_names(name):
    with pytest.raises(ValidationError):
        DatasetColumn(name=name, kind="numeric", non_missing_count=1, missing_count=0)


@pytest.mark.parametrize(
    "updates",
    [
        {"row_count": 4},
        {"row_count": True},
        {"row_count": 10001},
        {"byte_count": 0},
        {"byte_count": 5 * 1024 * 1024 + 1},
        {"created_at": datetime(2026, 11, 3)},
        {"sha256": "not-a-checksum"},
        {"columns": []},
        {"format": "xlsx"},
        {"owner": "another-user"},
    ],
)
def test_metadata_rejects_inconsistent_or_unapproved_fields(updates):
    with pytest.raises(ValidationError):
        metadata(**updates)


def test_duplicate_metadata_names_rejected_and_header_only_dataset_allowed():
    column = DatasetColumn(name="price", kind="numeric", non_missing_count=2, missing_count=1)
    with pytest.raises(ValidationError):
        metadata(columns=[column, column])
    empty = DatasetColumn(name="price", kind="text", non_missing_count=0, missing_count=0)
    assert metadata(row_count=0, columns=[empty]).row_count == 0


@pytest.mark.parametrize(
    "columns,rows,count",
    [
        (["price"], [["1", "2"]], 1),
        (["price", "price"], [["1", "2"]], 1),
        (["price"], [["1"]], 0),
        (["price"], [["1"]] * 21, 21),
        (["price"], [["x" * 1025]], 1),
        (["price"], [[float("nan")]], 1),
        (["price"], [[True]], 1),
        ([], [], 0),
    ],
)
def test_preview_rejects_ragged_oversized_or_nontext_cells(columns, rows, count):
    with pytest.raises(ValidationError):
        DatasetPreview(dataset_id=uuid4(), row_count=count, columns=columns, rows=rows)


@pytest.mark.parametrize("model", TOOLS)
def test_tool_arguments_roundtrip_and_forbid_unknown_fields(model):
    value = arguments(model)
    assert model.model_validate_json(value.model_dump_json(), strict=True) == value
    assert model.model_json_schema()["additionalProperties"] is False
    for field in ("owner", "code", "sql", "dataset_ids", "join", "api_key"):
        with pytest.raises(ValidationError) as error:
            arguments(model, **{field: "private-input"})
        assert "private-input" not in str(error.value)


@pytest.mark.parametrize(
    "model,updates",
    [
        (DescribeColumnsArguments, {"columns": []}),
        (DescribeColumnsArguments, {"columns": ["price", "price"]}),
        (DescribeColumnsArguments, {"columns": [f"c{i}" for i in range(129)]}),
        (DescribeColumnsArguments, {"missing_policy": "guess"}),
        (AggregateStatisticsArguments, {"operation": "eval"}),
        (AggregateStatisticsArguments, {"method": "pearson"}),
        (AggregateStatisticsArguments, {"missing_policy": "pairwise_complete"}),
        (CorrelateColumnsArguments, {"x_column": "quantity"}),
        (CorrelateColumnsArguments, {"method": "causality"}),
        (CorrelateColumnsArguments, {"group_by": "region"}),
        (CorrelateColumnsArguments, {"missing_policy": "omit"}),
    ],
)
def test_incompatible_parameters_and_duplicate_columns_fail(model, updates):
    with pytest.raises(ValidationError):
        arguments(model, **updates)


@pytest.mark.parametrize("model", TOOLS[1:])
def test_missing_value_strategy_must_be_explicit(model):
    data = arguments(model).model_dump(mode="json")
    data.pop("missing_policy")
    with pytest.raises(ValidationError):
        model.model_validate(data)


def test_approved_operations_methods_and_fixed_error_codes():
    for operation in ("count", "sum", "mean", "min", "max"):
        assert arguments(AggregateStatisticsArguments, operation=operation, group_by="region")
    assert arguments(CorrelateColumnsArguments, method="spearman", missing_policy="error")
    assert arguments(DescribeColumnsArguments, missing_policy="error")
    assert DatasetFailure(code="dataset_not_found").model_dump() == {"code": "dataset_not_found"}
    with pytest.raises(ValidationError):
        DatasetFailure(code="dataset_not_found", message="private-cell")


def test_existing_runner_dataset_summary_remains_compatible():
    from statistics_agent.agent.statistics import DatasetSummary

    value = metadata()
    summary = DatasetSummary(
        dataset_id=value.dataset_id,
        row_count=value.row_count,
        columns=tuple(c.name for c in value.columns),
    )
    assert summary.model_dump(mode="json") == {
        "dataset_id": str(value.dataset_id),
        "row_count": 3,
        "columns": ["price"],
    }
