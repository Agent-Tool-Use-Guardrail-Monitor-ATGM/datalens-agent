"""CSV admission, original-data preservation and deterministic numeric inference."""

import csv
import io
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from uuid import uuid4

import pytest

from statistics_agent.contracts.datasets import MAX_UPLOAD_BYTES, DatasetLimits
from statistics_agent.datasets.csv import CsvValidationError, parse_csv


def test_bom_headers_missing_counts_and_original_bytes_are_preserved():
    raw = b'\xef\xbb\xbf price ,label\r\n 2 ,apple\r\n,""\r\n6, \r\n'
    parsed = parse_csv(raw)
    assert parsed.content == raw
    assert [column.name for column in parsed.columns] == ["price", "label"]
    assert [column.kind for column in parsed.columns] == ["numeric", "text"]
    assert [(c.non_missing_count, c.missing_count) for c in parsed.columns] == [(2, 1), (1, 2)]
    assert parsed.rows == ((" 2 ", "apple"), (None, None), ("6", None))
    identifier = uuid4()
    preview = parsed.preview(identifier)
    assert preview.dataset_id == identifier and preview.row_count == 3
    assert preview.rows == parsed.rows
    assert "apple" not in repr(parsed) and "label" not in repr(parsed)
    with pytest.raises(FrozenInstanceError):
        parsed.content = b"changed"


def test_quoted_delimiters_newlines_and_doubled_quotes():
    raw = b'id,note\r\n1,"one,two"\r\n2,"line\r\nline"\r\n3,"a ""quote"""\r\n'
    parsed = parse_csv(raw)
    assert parsed.rows == (("1", "one,two"), ("2", "line\r\nline"), ("3", 'a "quote"'))


@pytest.mark.parametrize("ending", ["\n", "\r", "\r\n", ""])
def test_supported_record_endings_and_missing_final_newline(ending):
    separator = ending or "\n"
    parsed = parse_csv(("x" + separator + "4" + ending).encode())
    assert parsed.rows == (("4",),)


@pytest.mark.parametrize("raw", [b"x", b"x\n", b"\xef\xbb\xbfx\r\n"])
def test_header_only_dataset_has_zero_rows_and_text_kind(raw):
    parsed = parse_csv(raw)
    assert parsed.row_count == 0 and parsed.rows == ()
    assert parsed.columns[0].kind == "text"
    assert parsed.columns[0].non_missing_count == parsed.columns[0].missing_count == 0


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"\xef\xbb\xbf",
        b"\xff",
        b"x\n\xff",
        b"x\nabc\x00def",
        b"\n1",
        b",b\n1,2",
        b"a, \n1,2",
        b"a,a\n1,2",
        b"a, a \n1,2",
        b"a,b\n1",
        b"a\n1,2",
        b"a\n\n",
        b'a\n"unclosed',
        b'a\n"closed"extra',
        b'a\nunquoted"quote',
        b'a\n "quoted"',
        b'a\n"closed" ',
    ],
)
def test_invalid_encoding_shape_headers_and_quoting_have_fixed_safe_failures(raw):
    with pytest.raises(CsvValidationError) as error:
        parse_csv(raw)
    assert error.value.failure.code == "invalid_csv"
    assert str(error.value) == "CSV data is invalid"
    assert error.value.__suppress_context__ or error.value.__context__ is None


def test_quoted_single_column_empty_values_are_missing_without_skipping_rows():
    parsed = parse_csv(b'x\n""\n" "\n')
    assert parsed.rows == ((None,), (None,)) and parsed.row_count == 2
    assert parsed.columns[0].kind == "text" and parsed.columns[0].missing_count == 2


@pytest.mark.parametrize(
    "token",
    ["NaN", "+nan", "-NAN", "inf", "-inf", "+Inf", "infinity", "-Infinity", "1e309", "-2E9999"],
)
def test_nonfinite_numeric_tokens_reject_whole_dataset_even_in_text_columns(token):
    raw = ("x\nordinary text\n" + token).encode()
    with pytest.raises(CsvValidationError) as error:
        parse_csv(raw)
    assert error.value.failure.code == "invalid_csv"
    assert token not in str(error.value)


@pytest.mark.parametrize("token", ["0", "-0", "+12", "1.", ".5", "-.25", "1e3", "+2.5E-2", " 2 "])
def test_finite_decimal_and_exponent_forms_are_numeric_without_changing_text(token):
    parsed = parse_csv(("x\n" + token).encode())
    assert parsed.columns[0].kind == "numeric"
    assert parsed.rows == ((token,),)


@pytest.mark.parametrize(
    "token", ["true", "False", "2026-11-04", "1_000", "1/2", "oops", "NaN value"]
)
def test_mixed_or_nonnumeric_text_is_never_silently_coerced_to_missing(token):
    parsed = parse_csv(("x\n2\n" + token).encode())
    assert parsed.columns[0].kind == "text"
    assert parsed.columns[0].non_missing_count == 2 and parsed.columns[0].missing_count == 0
    assert parsed.rows[-1] == (token,)


@pytest.mark.parametrize(
    "limits,accepted,rejected",
    [
        (DatasetLimits(max_upload_bytes=3), b"x\n1", b"x\n12"),
        (DatasetLimits(max_rows=2), b"x\n1\n2", b"x\n1\n2\n3"),
        (DatasetLimits(max_columns=2), b"a,b\n1,2", b"a,b,c\n1,2,3"),
        (DatasetLimits(max_column_name=2), b"ab\n1", b"abc\n1"),
        (DatasetLimits(max_cell_characters=2), b"a\nxx", b"a\nxxx"),
    ],
)
def test_each_configurable_limit_accepts_boundary_and_rejects_excess(limits, accepted, rejected):
    assert parse_csv(accepted, limits=limits)
    with pytest.raises(CsvValidationError) as error:
        parse_csv(rejected, limits=limits)
    assert error.value.failure.code == "dataset_limit_exceeded"


def test_quoted_field_limits_count_decoded_quotes_and_embedded_newlines():
    limits = DatasetLimits(max_cell_characters=3)
    assert parse_csv(b'x\n"a""b"', limits=limits).rows == (('a"b',),)
    assert parse_csv(b'x\n"a\nb"', limits=limits).rows == (("a\nb",),)
    with pytest.raises(CsvValidationError):
        parse_csv(b'x\n"a\r\nb"', limits=limits)


def test_default_row_column_name_cell_and_preview_boundaries():
    assert parse_csv(b"x\n" + b"1\n" * 10000).row_count == 10000
    with pytest.raises(CsvValidationError, match="limits"):
        parse_csv(b"x\n" + b"1\n" * 10001)
    names = ",".join(f"c{i}" for i in range(128))
    assert len(parse_csv(names.encode()).columns) == 128
    with pytest.raises(CsvValidationError, match="limits"):
        parse_csv((names + ",extra").encode())
    assert parse_csv(b"x" * 128).columns[0].name == "x" * 128
    with pytest.raises(CsvValidationError, match="limits"):
        parse_csv(b"x" * 129)
    assert parse_csv(b"x\n" + b"a" * 1024).rows[0][0] == "a" * 1024
    with pytest.raises(CsvValidationError, match="limits"):
        parse_csv(b"x\n" + b"a" * 1025)
    parsed = parse_csv(b"x\n" + b"1\n" * 21)
    assert len(parsed.preview(uuid4()).rows) == 20 and parsed.row_count == 21
    assert (
        len(parse_csv(b"x\n1\n2", limits=DatasetLimits(max_preview_rows=1)).preview(uuid4()).rows)
        == 1
    )


def test_oversized_bytes_and_nonbyte_inputs_fail_before_decoding():
    with pytest.raises(CsvValidationError, match="limits"):
        parse_csv(b"\xff" * (MAX_UPLOAD_BYTES + 1))
    for value in ("x\n1", bytearray(b"x\n1"), None):
        with pytest.raises(CsvValidationError, match="invalid"):
            parse_csv(value)


def test_unicode_size_is_measured_in_bytes_and_data_is_preserved():
    raw = "x\n\u20ac".encode()
    parsed = parse_csv(raw, limits=DatasetLimits(max_upload_bytes=5, max_cell_characters=1))
    assert parsed.rows == (("\u20ac",),)
    with pytest.raises(CsvValidationError, match="limits"):
        parse_csv(raw, limits=DatasetLimits(max_upload_bytes=4))


def test_csv_writer_roundtrip_preserves_supported_cells():
    values = [
        ("a,b", 'a"b', "line\nline"),
        ("line\r\nline", "  kept  ", ""),
        ("\u20ac", ",", '"'),
    ]
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    writer.writerow(["a", "b", "c"])
    writer.writerows(values)
    result = parse_csv(buffer.getvalue().encode())
    assert result.rows == tuple(
        tuple(cell if cell.strip() else None for cell in row) for row in values
    )


def test_concurrent_parses_do_not_change_process_global_csv_limit():
    original = csv.field_size_limit()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(parse_csv, (b'x\n"one,two"', b"x\n4")))
    assert results[0].rows == (("one,two",),) and results[1].rows == (("4",),)
    assert csv.field_size_limit() == original
