# Bounded CSV parsing and validation — step 5.3

Updated: 4 November 2026. **Complete locally on feature/csv-validation;
required PR tests/delivery pending.** Step 5.2 delivery is accepted from the
user's pull confirmation. No ingestion service or business HTTP route is added.

## Entry point and result

`statistics_agent.datasets.csv.parse_csv(content, limits=...)` accepts a complete
bytes value and optional server-owned DatasetLimits. It is a synchronous pure
function: no file paths, remote URLs, database access, callbacks or model calls.
It validates the entire value before returning a ParsedDataset with:

- Exact original content bytes, including an optional UTF-8 BOM and original
  quoting, line endings and whitespace; these bytes remain the storage source.
- Ordered DatasetColumn models with normalized names, numeric/text kind and
  nonmissing/missing counts derived from the actual file.
- Immutable rows of original nonmissing strings or None for missing cells.
- row_count and preview(dataset_id), with the configured preview row limit.

The preview helper does not authorize its supplied dataset ID. The caller must
first establish ownership; parsed rows/previews are user data and are never
automatically model context or telemetry. Raw content/rows and column names are
excluded from normal result repr, but explicit access/serialization is sensitive.

## Accepted CSV syntax

Decode UTF-8 strictly, allowing one optional leading BOM. Use comma delimiters,
double-quoted fields and doubled quotes as the only quote escape. Accept LF, CR
and CRLF record endings and a final record without a terminating newline.
Quoted commas and embedded line endings are preserved inside field text.

Reject decoding failures, NULs, quotes inside unquoted fields, unfinished quotes,
or any character after a closing quote other than a delimiter, record ending or
another quote forming an escape. There is no delimiter guessing, automatic date
parsing, backslash escape mode or automatic whitespace skipping after commas.

One nonempty header row is required. Trim header names, then reject empty or
duplicate names; matching remains case-sensitive. Enforce the raw decoded header
field length before trimming, so padding cannot bypass admission limits. A
header-only file is valid with zero data rows. Every subsequent record must
match the header width. Blank physical records are rejected rather than skipped;
use a quoted empty field to represent a missing value in a one-column row.
Blank lines inside a quoted field remain part of that field.

## Limits and allocation

Reuse DatasetLimits ceilings: 5 MiB original bytes, 10,000 data records,
128 columns, 128 decoded characters per header field, 1,024 decoded characters
per data cell and 20 preview rows. Lower configured ceilings are honored.
The byte check happens before decoding. A lexical preflight checks strict
quoting, decoded field sizes, column count and logical record count before
csv.reader constructs rows. Doubled quotes count as one decoded character;
embedded CRLF counts as two. Logical rows, not physical lines, count toward
the row limit.

Use Python 3.12's standard csv/io/re/math modules and existing contract models;
no new package pin or dataframe is introduced. The implementation never changes
process-global csv.field_size_limit. No unbounded streaming reader or upload
handler exists yet: plan 7 must also bound request reading before materializing
content, and plan 5.4 must run this synchronous CPU work outside the async request
loop using bounded admission. A thread timeout is not hard computation termination.

## Missing values and type inference

An empty or whitespace-only cell becomes None. Every other cell retains its
original text, including surrounding whitespace. Infer type using a trimmed copy
without changing that stored text. A column is numeric only when it has at least
one nonmissing cell and every nonmissing value matches an ASCII decimal/scientific
number form with a finite Python float interpretation. Signed integers, decimal
points and exponents are supported; numeric-looking strings are not converted
in the returned rows.

Boolean/date-like values, underscores in digits, fractions and arbitrary text
remain text. A mixed numeric/text column is text with all nonblank cells still
counted as nonmissing; no coercion of malformed values to NaN. All-missing and
header-only columns have kind text and zero nonmissing values.

Reject case-insensitive standalone signed NaN/Inf/Infinity tokens and numeric
forms overflowing to infinity, even after a column has otherwise become text.
Embedded phrases such as 'NaN value' remain ordinary text. Inference is not a
precision guarantee or statistical computation: original decimal text survives,
and numerical conversion/operation semantics belong to the later typed tools.

## Errors, storage and integration boundary

CsvValidationError contains the existing fixed DatasetFailure code invalid_csv
or dataset_limit_exceeded. Its string message is fixed; underlying decode/csv
exceptions are suppressed rather than exposed. No row text, cell value, header,
filename or raw parser exception is printed/logged by this module.

Rejected parsing returns no partial result and performs no writes. Existing
DatasetStore.create remains a separate trusted internal storage boundary; it
does not automatically call the parser. Step 5.4 must compose parse-first,
store-after-success using the parser's original bytes, actual counts and column
metadata, with owner checks and transaction/rejection tests. Do not accept
client-supplied counts as proven facts or claim end-to-end upload validation yet.

## Verification and stop

Added **65 parser tests** for UTF-8/BOM, quoting/escapes/newlines, supported record
endings, empty/header-only data, missing values, mixed types, finite/nonfinite
numbers, malformed input, configured/default boundaries, UTF-8 byte sizes,
preservation, bounded previews and concurrent parses without global CSV-limit
mutation. Focused parser + contract gate: **126 passed**.

Final offline regression: **484 passed, 115 database/browser tests deselected**,
148 upstream warnings, 11.75 seconds. Ruff format/check and pip consistency pass.
No Docker/database, schema migration, dependency, auth/provider setting or live
LLM request changed in this task. Required PR tests remain pending; offline
success does not claim PostgreSQL/browser or remote-CI execution.

Plans/README/frontend handoff record these internal capabilities only. Parent
planning files remain outside service Git/PRs. Stop before step 5.4 ingestion and
dataset lifecycle composition. See [dataset/tools](dataset-tools.md) and the
[architecture](agent-architecture.md) for subsequent responsibilities.
