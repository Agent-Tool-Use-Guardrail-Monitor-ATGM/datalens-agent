"""Hand-calculated numerical expectations, not NumPy-versus-itself assertions."""

import math
from uuid import uuid4

import pytest
from pydantic import ValidationError

from statistics_agent.contracts.datasets import DescribeColumnsArguments
from statistics_agent.contracts.statistics import ColumnDescription, DescriptiveMetrics
from statistics_agent.datasets.csv import parse_csv
from statistics_agent.tools.descriptive import (
    StatisticsToolError,
    describe_columns,
    inspect_dataset,
)


def describe(raw, *, columns=("x",), missing_policy="omit"):
    return describe_columns(
        parse_csv(raw),
        DescribeColumnsArguments(
            dataset_id=uuid4(),
            columns=columns,
            missing_policy=missing_policy,
        ),
    )


def test_inspection_reports_real_dimensions_types_and_missing_counts_in_order():
    data = parse_csv(b"x,label\n2,a\n,b\n4,")
    identifier = uuid4()
    result = inspect_dataset(data, identifier)
    assert result.dataset_id == identifier and result.row_count == 3
    assert [(c.name, c.kind, c.non_missing_count, c.missing_count) for c in result.columns] == [
        ("x", "numeric", 2, 1),
        ("label", "text", 2, 1),
    ]


@pytest.mark.parametrize(
    "raw,expected",
    [
        (b"x\n2\n4\n6", (2.0, 6.0, 4.0, 4.0, math.sqrt(8 / 3), 3.0, 5.0)),
        (b"x\n1\n2\n3\n4", (1.0, 4.0, 2.5, 2.5, math.sqrt(1.25), 1.75, 3.25)),
        (b"x\n7\n7\n7", (7.0, 7.0, 7.0, 7.0, 0.0, 7.0, 7.0)),
        (b"x\n7", (7.0, 7.0, 7.0, 7.0, 0.0, 7.0, 7.0)),
        (b"x\n-5\n-1\n3", (-5.0, 3.0, -1.0, -1.0, math.sqrt(32 / 3), -3.0, 1.0)),
    ],
)
def test_descriptions_match_hand_calculated_population_and_linear_quartiles(raw, expected):
    result = describe(raw)
    column = result.columns[0]
    assert column.status == "available" and column.reason is None
    assert tuple(column.metrics.model_dump().values()) == pytest.approx(expected)
    assert result.std_ddof == 0 and result.quantile_method == "linear"
    assert result.dtype == "float64" and result.backend == "numpy"


def test_explicit_missing_policy_omits_only_missing_values_and_reports_counts():
    result = describe(b'x\n1\n""\n3')
    column = result.columns[0]
    assert (column.valid_count, column.missing_count) == (2, 1)
    assert column.metrics.mean == 2.0 and column.metrics.population_std == 1.0
    with pytest.raises(StatisticsToolError) as error:
        describe(b'x\n1\n""\n3', missing_policy="error")
    assert error.value.failure.code == "missing_values"


@pytest.mark.parametrize("raw,missing", [(b"x", 0), (b'x\n""\n" "', 2)])
def test_empty_or_all_missing_samples_have_explicit_unavailable_metrics(raw, missing):
    column = describe(raw).columns[0]
    assert column.status == "unavailable" and column.reason == "insufficient_data"
    assert column.valid_count == 0 and column.missing_count == missing
    assert column.metrics is None


@pytest.mark.parametrize(
    "raw,columns,code",
    [
        (b"x\n1", ("absent",), "invalid_column"),
        (b"x\ntext", ("x",), "non_numeric_column"),
        (b"x\n1\ntext", ("x",), "non_numeric_column"),
        (b"x\n1e308\n1e308", ("x",), "numeric_range_exceeded"),
    ],
)
def test_unsupported_columns_and_numerical_overflow_fail_without_fabricated_values(
    raw, columns, code
):
    with pytest.raises(StatisticsToolError) as error:
        describe(raw, columns=columns)
    assert error.value.failure.code == code
    assert str(error.value) == "Statistical operation could not be completed"


def test_selected_column_order_is_preserved_and_unselected_text_does_not_fail_numeric_work():
    result = describe(b"x,y,label\n2,10,a\n4,20,b", columns=("y", "x"))
    assert [c.name for c in result.columns] == ["y", "x"]
    assert [c.metrics.mean for c in result.columns] == [15.0, 3.0]


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_result_contract_rejects_nonfinite_metrics(value):
    with pytest.raises(ValidationError):
        DescriptiveMetrics(
            minimum=1.0, maximum=3.0, mean=value, median=2.0, population_std=1.0, q1=1.5, q3=2.5
        )


@pytest.mark.parametrize(
    "data",
    [
        {"valid_count": 0, "status": "available"},
        {"valid_count": 1, "status": "unavailable", "reason": "insufficient_data"},
        {"valid_count": 0, "status": "unavailable"},
    ],
)
def test_availability_cannot_claim_values_for_an_empty_sample(data):
    with pytest.raises(ValidationError):
        ColumnDescription(name="x", missing_count=0, **data)
