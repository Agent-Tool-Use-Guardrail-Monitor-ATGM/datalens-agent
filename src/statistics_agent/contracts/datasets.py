"""Dataset boundaries only: no parsing, persistence, authorization or computation."""

from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import AfterValidator, AwareDatetime, Field, StringConstraints, model_validator

from statistics_agent.contracts.common import Contract

MAX_UPLOAD_BYTES = 5 * 1024 * 1024
MAX_ROWS = 10_000
MAX_COLUMNS = 128
MAX_COLUMN_NAME = 128
MAX_CELL_CHARACTERS = 1024
MAX_PREVIEW_ROWS = 20


def _normalized_name(value: str) -> str:
    if not value.strip() or value != value.strip():
        raise ValueError("Column names must be nonempty and already trimmed")
    return value


type ColumnName = Annotated[
    str,
    StringConstraints(strict=True, min_length=1, max_length=MAX_COLUMN_NAME),
    AfterValidator(_normalized_name),
]
type RowCount = Annotated[int, Field(strict=True, ge=0, le=MAX_ROWS)]
type CellText = Annotated[str, StringConstraints(strict=True, max_length=MAX_CELL_CHARACTERS)]
type PreviewRow = Annotated[tuple[CellText | None, ...], Field(max_length=MAX_COLUMNS)]
type MissingPolicy = Literal["omit", "error"]


class DatasetLimits(Contract):
    """Server-owned admission limits; may be lowered, never raised past v1 ceilings."""

    max_upload_bytes: Annotated[int, Field(strict=True, ge=1, le=MAX_UPLOAD_BYTES)] = (
        MAX_UPLOAD_BYTES
    )
    max_rows: Annotated[int, Field(strict=True, ge=1, le=MAX_ROWS)] = MAX_ROWS
    max_columns: Annotated[int, Field(strict=True, ge=1, le=MAX_COLUMNS)] = MAX_COLUMNS
    max_column_name: Annotated[int, Field(strict=True, ge=1, le=MAX_COLUMN_NAME)] = MAX_COLUMN_NAME
    max_cell_characters: Annotated[int, Field(strict=True, ge=1, le=MAX_CELL_CHARACTERS)] = (
        MAX_CELL_CHARACTERS
    )
    max_preview_rows: Annotated[int, Field(strict=True, ge=1, le=MAX_PREVIEW_ROWS)] = (
        MAX_PREVIEW_ROWS
    )
    format: Literal["csv"] = "csv"
    encoding: Literal["utf-8-sig"] = "utf-8-sig"
    delimiter: Literal[","] = ","


class StatisticsLimits(Contract):
    """Numerical semantics/output ceilings, not model-selectable tool arguments."""

    max_groups: Annotated[int, Field(strict=True, ge=1, le=50)] = 50
    max_result_bytes: Annotated[int, Field(strict=True, ge=1, le=16_384)] = 16_384
    standard_deviation_ddof: Annotated[int, Field(strict=True, ge=0, le=0)] = 0
    quantile_method: Literal["linear"] = "linear"
    minimum_correlation_pairs: Annotated[int, Field(strict=True, ge=3, le=3)] = 3


class DatasetColumn(Contract):
    name: ColumnName = Field(repr=False)
    kind: Literal["numeric", "text"]
    non_missing_count: RowCount
    missing_count: RowCount


class DatasetMetadata(Contract):
    """Service-produced view, not upload input or proof of dataset ownership."""

    dataset_id: UUID
    filename: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=255)] = Field(
        repr=False
    )
    sha256: Annotated[str, StringConstraints(strict=True, pattern=r"^[0-9a-f]{64}$")]
    byte_count: Annotated[int, Field(strict=True, ge=1, le=MAX_UPLOAD_BYTES)]
    row_count: RowCount
    columns: tuple[DatasetColumn, ...] = Field(min_length=1, max_length=MAX_COLUMNS, repr=False)
    created_at: AwareDatetime
    format: Literal["csv"] = "csv"

    @model_validator(mode="after")
    def consistent_columns(self) -> Self:
        names = [column.name for column in self.columns]
        if len(set(names)) != len(names):
            raise ValueError("Column names must be unique")
        if any(c.non_missing_count + c.missing_count != self.row_count for c in self.columns):
            raise ValueError("Column counts must match dataset row count")
        return self


class DatasetPreview(Contract):
    """Owner-authorized UI data; never automatically included in model context."""

    dataset_id: UUID
    row_count: RowCount
    columns: tuple[ColumnName, ...] = Field(min_length=1, max_length=MAX_COLUMNS, repr=False)
    rows: tuple[PreviewRow, ...] = Field(default=(), max_length=MAX_PREVIEW_ROWS, repr=False)

    @model_validator(mode="after")
    def consistent_rows(self) -> Self:
        if len(set(self.columns)) != len(self.columns):
            raise ValueError("Column names must be unique")
        if len(self.rows) > self.row_count:
            raise ValueError("Preview cannot contain more rows than the dataset")
        if any(len(row) != len(self.columns) for row in self.rows):
            raise ValueError("Preview rows must match the column count")
        return self


class DatasetFailure(Contract):
    """Fixed internal codes, without raw cells, filenames or arbitrary error text."""

    code: Literal[
        "dataset_not_found",
        "invalid_csv",
        "dataset_limit_exceeded",
        "invalid_column",
        "non_numeric_column",
        "missing_values",
        "insufficient_data",
        "constant_input",
        "result_limit_exceeded",
        "numeric_range_exceeded",
        "group_limit_exceeded",
    ]


class InspectDatasetArguments(Contract):
    dataset_id: UUID


class DescribeColumnsArguments(Contract):
    dataset_id: UUID
    columns: tuple[ColumnName, ...] = Field(min_length=1, max_length=MAX_COLUMNS, repr=False)
    missing_policy: MissingPolicy

    @model_validator(mode="after")
    def unique_columns(self) -> Self:
        if len(set(self.columns)) != len(self.columns):
            raise ValueError("Selected columns must be unique")
        return self


class AggregateStatisticsArguments(Contract):
    dataset_id: UUID
    column: ColumnName = Field(repr=False)
    operation: Literal["count", "sum", "mean", "min", "max"]
    group_by: ColumnName | None = Field(default=None, repr=False)
    missing_policy: MissingPolicy


class CorrelateColumnsArguments(Contract):
    dataset_id: UUID
    x_column: ColumnName = Field(repr=False)
    y_column: ColumnName = Field(repr=False)
    method: Literal["pearson", "spearman"]
    missing_policy: Literal["pairwise_complete", "error"]

    @model_validator(mode="after")
    def distinct_columns(self) -> Self:
        if self.x_column == self.y_column:
            raise ValueError("Correlation requires two distinct columns")
        return self
