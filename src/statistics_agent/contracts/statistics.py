"""Typed finite statistical results; no numerical dependencies."""

from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import Field, model_validator

from statistics_agent.contracts.common import Contract
from statistics_agent.contracts.datasets import (
    CellText,
    ColumnName,
    DatasetColumn,
    MissingPolicy,
    RowCount,
)

type FiniteNumber = Annotated[float, Field(strict=True, allow_inf_nan=False)]


class DescriptiveMetrics(Contract):
    minimum: FiniteNumber
    maximum: FiniteNumber
    mean: FiniteNumber
    median: FiniteNumber
    population_std: Annotated[float, Field(strict=True, ge=0, allow_inf_nan=False)]
    q1: FiniteNumber
    q3: FiniteNumber


class ColumnDescription(Contract):
    name: ColumnName = Field(repr=False)
    valid_count: RowCount
    missing_count: RowCount
    status: Literal["available", "unavailable"]
    reason: Literal["insufficient_data"] | None = None
    metrics: DescriptiveMetrics | None = Field(default=None, repr=False)

    @model_validator(mode="after")
    def consistent_availability(self) -> Self:
        if self.status == "available":
            if self.valid_count == 0 or self.metrics is None or self.reason is not None:
                raise ValueError("Available metrics require a nonempty sample")
        elif (
            self.valid_count != 0 or self.metrics is not None or self.reason != "insufficient_data"
        ):
            raise ValueError("Unavailable metrics require an empty sample and explicit reason")
        return self


class DatasetInspection(Contract):
    dataset_id: UUID
    row_count: RowCount
    columns: tuple[DatasetColumn, ...] = Field(min_length=1, max_length=128, repr=False)


class DescribeColumnsResult(Contract):
    dataset_id: UUID
    missing_policy: MissingPolicy
    backend: Literal["numpy"] = "numpy"
    backend_version: Annotated[str, Field(strict=True, min_length=1, max_length=128)]
    dtype: Literal["float64"] = "float64"
    std_ddof: Literal[0] = 0
    quantile_method: Literal["linear"] = "linear"
    columns: tuple[ColumnDescription, ...] = Field(min_length=1, max_length=128, repr=False)


class AggregateGroup(Contract):
    key: CellText | None = Field(repr=False)
    valid_count: RowCount
    missing_count: RowCount
    status: Literal["available", "unavailable"]
    value: Annotated[int, Field(strict=True, ge=0, le=10000)] | FiniteNumber | None = Field(
        repr=False
    )
    reason: Literal["insufficient_data"] | None = None

    @model_validator(mode="after")
    def consistent_value(self) -> Self:
        if self.status == "available":
            if self.value is None or self.reason is not None:
                raise ValueError("Available aggregation requires a value")
        elif self.value is not None or self.valid_count != 0 or self.reason != "insufficient_data":
            raise ValueError("Unavailable aggregation requires an empty sample")
        return self


class AggregateStatisticsResult(Contract):
    dataset_id: UUID
    column: ColumnName = Field(repr=False)
    operation: Literal["count", "sum", "mean", "min", "max"]
    group_by: ColumnName | None = Field(repr=False)
    missing_policy: MissingPolicy
    row_count: RowCount
    valid_count: RowCount
    missing_value_count: RowCount
    missing_group_count: RowCount
    dropped_row_count: RowCount
    backend: Literal["numpy"] = "numpy"
    backend_version: Annotated[str, Field(strict=True, min_length=1, max_length=128)]
    groups: tuple[AggregateGroup, ...] = Field(max_length=50, repr=False)

    @model_validator(mode="after")
    def consistent_groups(self) -> Self:
        if self.valid_count + self.dropped_row_count != self.row_count:
            raise ValueError("Aggregated and dropped counts must account for every row")
        if sum(group.valid_count for group in self.groups) != self.valid_count:
            raise ValueError("Group counts must match the total")
        if self.group_by is None:
            if len(self.groups) != 1 or self.groups[0].key is not None or self.missing_group_count:
                raise ValueError("Ungrouped aggregation requires one unkeyed result")
        elif any(g.key is None for g in self.groups) or len({g.key for g in self.groups}) != len(
            self.groups
        ):
            raise ValueError("Grouped results require distinct nonmissing keys")
        if self.operation == "count" and any(
            type(g.value) is not int or g.value != g.valid_count for g in self.groups
        ):
            raise ValueError("Count must report exact nonmissing observations")
        return self


class CorrelationResult(Contract):
    dataset_id: UUID
    x_column: ColumnName = Field(repr=False)
    y_column: ColumnName = Field(repr=False)
    method: Literal["pearson", "spearman"]
    missing_policy: Literal["pairwise_complete", "error"]
    row_count: RowCount
    sample_size: RowCount
    dropped_pair_count: RowCount
    status: Literal["available", "unavailable"]
    coefficient: Annotated[float, Field(strict=True, ge=-1, le=1, allow_inf_nan=False)] | None = (
        Field(repr=False)
    )
    reason: Literal["insufficient_data", "constant_input"] | None = None
    numpy_version: Annotated[str, Field(strict=True, min_length=1, max_length=128)]
    scipy_version: Annotated[str, Field(strict=True, min_length=1, max_length=128)]
    rank_method: Literal["average"] | None = None
    dtype: Literal["float64"] = "float64"

    @model_validator(mode="after")
    def consistent_correlation(self) -> Self:
        if (
            self.x_column == self.y_column
            or self.sample_size + self.dropped_pair_count != self.row_count
        ):
            raise ValueError("Correlation requires distinct columns and consistent pair counts")
        if self.rank_method != ("average" if self.method == "spearman" else None):
            raise ValueError("Rank method must match the correlation method")
        if self.status == "available":
            if self.sample_size < 3 or self.coefficient is None or self.reason is not None:
                raise ValueError("Available correlation requires three complete nonconstant pairs")
        elif self.coefficient is not None or self.reason is None:
            raise ValueError(
                "Unavailable correlation requires an explicit reason and no coefficient"
            )
        elif (self.reason == "insufficient_data") != (self.sample_size < 3):
            raise ValueError("Unavailable reason must match sample size")
        return self
