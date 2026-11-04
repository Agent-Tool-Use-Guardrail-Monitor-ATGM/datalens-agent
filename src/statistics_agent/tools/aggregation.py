"""Bounded grouping and pairwise correlation; no joins or hypothesis testing."""

import math

import numpy as np
import scipy
from scipy.stats import rankdata

from statistics_agent.contracts.datasets import (
    AggregateStatisticsArguments,
    CorrelateColumnsArguments,
    StatisticsLimits,
)
from statistics_agent.contracts.statistics import (
    AggregateGroup,
    AggregateStatisticsResult,
    CorrelationResult,
)
from statistics_agent.datasets.csv import ParsedDataset
from statistics_agent.tools.descriptive import StatisticsToolError


def aggregate_statistics(
    data: ParsedDataset, arguments: AggregateStatisticsArguments, limits: StatisticsLimits
) -> AggregateStatisticsResult:
    index = {column.name: position for position, column in enumerate(data.columns)}
    if arguments.column not in index or (
        arguments.group_by is not None and arguments.group_by not in index
    ):
        raise StatisticsToolError("invalid_column")
    value_index = index[arguments.column]
    group_index = index[arguments.group_by] if arguments.group_by is not None else None
    column = data.columns[value_index]
    if arguments.operation != "count" and column.non_missing_count and column.kind != "numeric":
        raise StatisticsToolError("non_numeric_column")
    values = {None: []} if group_index is None else {}
    missing = {None: 0} if group_index is None else {}
    valid_count = missing_values = missing_groups = dropped = 0
    for row in data.rows:
        value = row[value_index]
        key = row[group_index] if group_index is not None else None
        absent_group = group_index is not None and key is None
        missing_values += value is None
        missing_groups += absent_group
        if arguments.missing_policy == "error" and (value is None or absent_group):
            raise StatisticsToolError("missing_values")
        if absent_group:
            dropped += 1
            continue
        if key not in values:
            if len(values) >= limits.max_groups:
                raise StatisticsToolError("group_limit_exceeded")
            values[key], missing[key] = [], 0
        if value is None:
            missing[key] += 1
            dropped += 1
        else:
            values[key].append(value)
            valid_count += 1
    groups = []
    for key, cells in values.items():
        count = len(cells)
        if arguments.operation == "count":
            value = count
        elif not count:
            groups.append(
                AggregateGroup(
                    key=key,
                    valid_count=0,
                    missing_count=missing[key],
                    status="unavailable",
                    value=None,
                    reason="insufficient_data",
                )
            )
            continue
        else:
            array = np.array([float(cell.strip()) for cell in cells], dtype=np.float64)
            operations = {"sum": np.sum, "mean": np.mean, "min": np.min, "max": np.max}
            try:
                with np.errstate(over="raise", invalid="raise", divide="raise"):
                    value = float(operations[arguments.operation](array))
                if not math.isfinite(value):
                    raise FloatingPointError
            except (FloatingPointError, OverflowError):
                raise StatisticsToolError("numeric_range_exceeded") from None
        groups.append(
            AggregateGroup(
                key=key,
                valid_count=count,
                missing_count=missing[key],
                status="available",
                value=value,
            )
        )
    return AggregateStatisticsResult(
        dataset_id=arguments.dataset_id,
        column=arguments.column,
        operation=arguments.operation,
        group_by=arguments.group_by,
        missing_policy=arguments.missing_policy,
        row_count=data.row_count,
        valid_count=valid_count,
        missing_value_count=missing_values,
        missing_group_count=missing_groups,
        dropped_row_count=dropped,
        backend_version=np.__version__,
        groups=tuple(groups),
    )


def correlate_columns(
    data: ParsedDataset, arguments: CorrelateColumnsArguments, limits: StatisticsLimits
) -> CorrelationResult:
    index = {column.name: position for position, column in enumerate(data.columns)}
    for name in (arguments.x_column, arguments.y_column):
        if name not in index:
            raise StatisticsToolError("invalid_column")
        column = data.columns[index[name]]
        if column.non_missing_count and column.kind != "numeric":
            raise StatisticsToolError("non_numeric_column")
    x_index, y_index = index[arguments.x_column], index[arguments.y_column]
    pairs, dropped = [], 0
    for row in data.rows:
        x, y = row[x_index], row[y_index]
        if x is None or y is None:
            if arguments.missing_policy == "error":
                raise StatisticsToolError("missing_values")
            dropped += 1
        else:
            pairs.append((float(x.strip()), float(y.strip())))
    reason, coefficient = None, None
    if len(pairs) < limits.minimum_correlation_pairs:
        reason = "insufficient_data"
    else:
        array = np.array(pairs, dtype=np.float64)
        x, y = array[:, 0], array[:, 1]
        if np.all(x == x[0]) or np.all(y == y[0]):
            reason = "constant_input"
        else:
            try:
                with np.errstate(over="raise", invalid="raise", divide="raise"):
                    if arguments.method == "spearman":
                        x, y = rankdata(x, method="average"), rankdata(y, method="average")
                    coefficient = float(np.corrcoef(x, y)[0, 1])
                if not math.isfinite(coefficient):
                    raise FloatingPointError
            except (FloatingPointError, OverflowError):
                raise StatisticsToolError("numeric_range_exceeded") from None
    return CorrelationResult(
        dataset_id=arguments.dataset_id,
        x_column=arguments.x_column,
        y_column=arguments.y_column,
        method=arguments.method,
        missing_policy=arguments.missing_policy,
        row_count=data.row_count,
        sample_size=len(pairs),
        dropped_pair_count=dropped,
        status="unavailable" if reason else "available",
        coefficient=coefficient,
        reason=reason,
        numpy_version=np.__version__,
        scipy_version=scipy.__version__,
        rank_method="average" if arguments.method == "spearman" else None,
    )
