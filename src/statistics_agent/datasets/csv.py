"""Pure bounded UTF-8 CSV validation; no database, network or dataframe creation."""

import csv
import io
import math
import re
from dataclasses import dataclass, field
from typing import Literal
from uuid import UUID

from statistics_agent.contracts.datasets import (
    DatasetColumn,
    DatasetFailure,
    DatasetLimits,
    DatasetPreview,
)

_NUMBER = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")
_NONFINITE = frozenset(
    {"nan", "+nan", "-nan", "inf", "+inf", "-inf", "infinity", "+infinity", "-infinity"}
)


class CsvValidationError(ValueError):
    """Fixed safe failure; never include source text, csv.Error or decode details."""

    def __init__(self, code: Literal["invalid_csv", "dataset_limit_exceeded"]):
        self.failure = DatasetFailure(code=code)
        super().__init__(
            "CSV data is invalid" if code == "invalid_csv" else "CSV data exceeds its limits"
        )


@dataclass(frozen=True)
class ParsedDataset:
    """Validated immutable values, still requiring owner checks before any use."""

    content: bytes = field(repr=False)
    columns: tuple[DatasetColumn, ...]
    rows: tuple[tuple[str | None, ...], ...] = field(repr=False)
    preview_rows: int = 20

    @property
    def row_count(self) -> int:
        return len(self.rows)

    def preview(self, dataset_id: UUID) -> DatasetPreview:
        """Build a UI view only after the caller has authorized the dataset ID."""
        return DatasetPreview(
            dataset_id=dataset_id,
            row_count=self.row_count,
            columns=tuple(column.name for column in self.columns),
            rows=self.rows[: self.preview_rows],
        )


def _check_syntax_and_limits(source: str, limits: DatasetLimits) -> None:
    """Preflight decoded field/record bounds before csv.reader allocates a row.

    csv.reader(strict=True) permits quotes in unquoted fields; reject those
    explicitly. Never change csv.field_size_limit(), which is process-global.
    """
    state = "start"
    size, columns, records = 0, 1, 0
    started = skip_lf = False
    for character in source:
        if skip_lf:
            skip_lf = False
            if character == "\n":
                continue
        started = True
        if state == "quoted":
            if character == '"':
                state = "closed"
            else:
                size += 1
        elif character in "\r\n":
            records += 1
            if records - 1 > limits.max_rows:
                raise CsvValidationError("dataset_limit_exceeded")
            state, size, columns = "start", 0, 1
            started, skip_lf = False, character == "\r"
        elif character == ",":
            columns += 1
            if columns > limits.max_columns:
                raise CsvValidationError("dataset_limit_exceeded")
            state, size = "start", 0
        elif character == '"':
            if state == "start":
                state = "quoted"
            elif state == "closed":
                state = "quoted"
                size += 1  # Doubled quote represents one decoded character.
            else:
                raise CsvValidationError("invalid_csv")
        elif state == "closed":
            raise CsvValidationError("invalid_csv")
        else:
            state = "unquoted"
            size += 1
        maximum = limits.max_column_name if records == 0 else limits.max_cell_characters
        if size > maximum:
            raise CsvValidationError("dataset_limit_exceeded")
    if state == "quoted":
        raise CsvValidationError("invalid_csv")
    if started and records > limits.max_rows:
        raise CsvValidationError("dataset_limit_exceeded")


def _is_numeric(value: str) -> bool:
    if value.casefold() in _NONFINITE:
        raise CsvValidationError("invalid_csv")
    if _NUMBER.fullmatch(value) is None:
        return False
    if not math.isfinite(float(value)):
        raise CsvValidationError("invalid_csv")
    return True


def parse_csv(content: bytes, *, limits: DatasetLimits | None = None) -> ParsedDataset:
    """Validate a complete bounded byte value synchronously and return no partial data.

    DatasetService offloads this work and persists only after validation succeeds.
    No file path, URL, stream or persistence callback accepted.
    """
    bounds = DatasetLimits.model_validate(limits if limits is not None else DatasetLimits())
    if type(content) is not bytes or not content:
        raise CsvValidationError("invalid_csv")
    if len(content) > bounds.max_upload_bytes:
        raise CsvValidationError("dataset_limit_exceeded")
    try:
        source = content.decode(bounds.encoding, errors="strict")
    except UnicodeError:
        raise CsvValidationError("invalid_csv") from None
    if not source or "\x00" in source:
        raise CsvValidationError("invalid_csv")
    _check_syntax_and_limits(source, bounds)
    try:
        reader = csv.reader(io.StringIO(source, newline=""), delimiter=",", strict=True)
        header = next(reader, None)
        if not header:
            raise CsvValidationError("invalid_csv")
        names = tuple(name.strip() for name in header)
        if any(not name for name in names) or len(set(names)) != len(names):
            raise CsvValidationError("invalid_csv")
        width = len(names)
        missing, present = [0] * width, [0] * width
        numeric = [True] * width
        rows = []
        for row in reader:
            if len(row) != width:
                raise CsvValidationError("invalid_csv")
            cells = []
            for index, value in enumerate(row):
                stripped = value.strip()
                if not stripped:
                    missing[index] += 1
                    cells.append(None)
                else:
                    present[index] += 1
                    is_numeric = _is_numeric(stripped)
                    numeric[index] = numeric[index] and is_numeric
                    cells.append(value)
            rows.append(tuple(cells))
    except csv.Error:
        raise CsvValidationError("invalid_csv") from None
    columns = tuple(
        DatasetColumn(
            name=name,
            kind="numeric" if numeric[index] and present[index] else "text",
            non_missing_count=present[index],
            missing_count=missing[index],
        )
        for index, name in enumerate(names)
    )
    return ParsedDataset(content, columns, tuple(rows), bounds.max_preview_rows)
