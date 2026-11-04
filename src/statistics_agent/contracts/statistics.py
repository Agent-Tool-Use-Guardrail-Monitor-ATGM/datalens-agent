"""Typed finite inspection and descriptive results; no numerical dependencies."""

from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import Field, model_validator

from statistics_agent.contracts.common import Contract
from statistics_agent.contracts.datasets import ColumnName, DatasetColumn, MissingPolicy, RowCount

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
