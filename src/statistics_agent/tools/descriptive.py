"""Deterministic statistics over a previously validated, authorized dataset."""

from uuid import UUID

import numpy as np

from statistics_agent.contracts.datasets import DatasetFailure, DescribeColumnsArguments
from statistics_agent.contracts.statistics import (
    ColumnDescription,
    DatasetInspection,
    DescribeColumnsResult,
    DescriptiveMetrics,
)
from statistics_agent.datasets.csv import ParsedDataset


class StatisticsToolError(ValueError):
    def __init__(self, code: str):
        self.failure = DatasetFailure(code=code)
        super().__init__("Statistical operation could not be completed")


def inspect_dataset(data: ParsedDataset, dataset_id: UUID) -> DatasetInspection:
    return DatasetInspection(dataset_id=dataset_id, row_count=data.row_count, columns=data.columns)


def describe_columns(
    data: ParsedDataset, arguments: DescribeColumnsArguments
) -> DescribeColumnsResult:
    index = {column.name: position for position, column in enumerate(data.columns)}
    descriptions = []
    for name in arguments.columns:
        if name not in index:
            raise StatisticsToolError("invalid_column")
        position = index[name]
        column = data.columns[position]
        if column.non_missing_count and column.kind != "numeric":
            raise StatisticsToolError("non_numeric_column")
        if column.missing_count and arguments.missing_policy == "error":
            raise StatisticsToolError("missing_values")
        if column.non_missing_count == 0:
            descriptions.append(
                ColumnDescription(
                    name=name,
                    valid_count=0,
                    missing_count=column.missing_count,
                    status="unavailable",
                    reason="insufficient_data",
                )
            )
            continue
        # parse_csv preserved all nonmissing source text. This explicit float64
        # conversion is permitted only after the numeric/missing checks above.
        values = np.array(
            [float(row[position].strip()) for row in data.rows if row[position] is not None],
            dtype=np.float64,
        )
        try:
            with np.errstate(over="raise", invalid="raise", divide="raise"):
                quartiles = np.quantile(values, [0.25, 0.5, 0.75], method="linear")
                metrics = DescriptiveMetrics(
                    minimum=float(np.min(values)),
                    maximum=float(np.max(values)),
                    mean=float(np.mean(values)),
                    median=float(quartiles[1]),
                    population_std=float(np.std(values, ddof=0)),
                    q1=float(quartiles[0]),
                    q3=float(quartiles[2]),
                )
        except (FloatingPointError, OverflowError):
            raise StatisticsToolError("numeric_range_exceeded") from None
        descriptions.append(
            ColumnDescription(
                name=name,
                valid_count=column.non_missing_count,
                missing_count=column.missing_count,
                status="available",
                metrics=metrics,
            )
        )
    return DescribeColumnsResult(
        dataset_id=arguments.dataset_id,
        missing_policy=arguments.missing_policy,
        backend_version=np.__version__,
        columns=tuple(descriptions),
    )
