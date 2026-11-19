# Dataset and statistics-tool contracts — step 5.1

Updated: 4 November 2026. **Step 5.1 delivered per user pull confirmation.** The following is its historical contract-only implementation record; subsequent storage work is documented in [step 5.2](dataset-storage.md).
No CSV parser, database migration, numerical executor, dataset HTTP API,
conversation control, provider request or cloud deployment was added.

## Implemented models

All models live in `statistics_agent.contracts.datasets` and inherit the existing
immutable, extra-fields-forbidden Contract. They import no agent framework or
numerical libraries. Existing DatasetSummary and runner behavior are unchanged.

| Model | Purpose |
|---|---|
| DatasetLimits | Server-owned CSV admission ceilings, optionally lowered |
| StatisticsLimits | Server-owned numerical semantics and result-size ceilings |
| DatasetColumn | Trimmed unique-name candidate, numeric/text kind and nonmissing/missing counts |
| DatasetMetadata | Server-produced dataset UUID, display filename, checksum, byte/row counts, columns and aware creation time |
| DatasetPreview | Dataset UUID, total row count, unique columns and bounded rectangular string/null cells |
| DatasetFailure | Fixed internal code only, without arbitrary error text or raw values |
| InspectDatasetArguments | Exactly one dataset_id |
| DescribeColumnsArguments | One dataset_id, unique selected columns and explicit missing_policy |
| AggregateStatisticsArguments | One dataset_id, column, allowed operation, optional group_by and explicit missing_policy |
| CorrelateColumnsArguments | One dataset_id, two distinct columns, allowed method and explicit missing_policy |

These are internal boundaries, not frozen upload/OpenAPI request schemas. IDs and
metadata do not authenticate a caller or prove ownership. Reuse the verified
principal and owner-scoped service queries in subsequent steps; never accept
an owner identity from model arguments or dataset requests.

## Limits and invariants

- Dataset ceilings: 5 MiB raw CSV, 10,000 data rows, 128 columns, 128 characters
  per column name, 1,024 characters per cell and 20 preview rows. Integer
  limits/counts reject booleans, strings and fractional values.
- CSV format, comma delimiter and utf-8-sig encoding are declared. utf-8-sig
  supports UTF-8 with an optional BOM when the parser is implemented in 5.3.
  Declaring a limit does not enforce it on a real upload yet.
- Contract column names must already be trimmed, nonempty and bounded. Metadata
  names are unique and each column's nonmissing + missing count equals row_count.
  Header-only datasets may have zero rows. Parser normalization belongs to 5.3.
- Metadata creation time must include timezone information. Checksum shape is
  validated; computing/checking that checksum against bytes belongs to storage/
  ingestion. Filenames are display data, never filesystem paths.
- Preview rows must match the declared column width and cannot exceed the total
  row count or preview ceiling. Cells accept only bounded strings or null, not
  numbers, booleans or nonfinite floating-point values. Null represents missing;
  parsing/normalizing actual CSV cells remains 5.3 work.
- Statistics defaults: at most 50 groups and 16,384 bytes of numerical result
  JSON. Population standard deviation uses ddof=0; quartiles use linear
  interpolation; correlation needs at least three complete pairs. Fixed
  semantics cannot be overridden through tool arguments.

The numerical-result byte ceiling is not a promise that a result fits the model
input reservation. Tools must emit smaller bounded observations/artifact
references as needed; full schema/history overhead still counts toward the
existing 4096-unit request guard. Byte/result enforcement arrives with real
tools, not merely by constructing StatisticsLimits.

## Explicit statistical input semantics

Description and aggregation require missing_policy equal to omit or error.
Omit excludes missing selected observations; error rejects a calculation with
missing selected inputs. Grouped aggregation omits a row with a missing selected
value or group key when omit is selected; report omitted counts in the future
typed result. Nonempty nonnumeric text never silently becomes missing numeric
data. Count means nonmissing selected values, not an arbitrary SQL expression.

Aggregation permits count, sum, mean, min and max, with at most one group column.
Only count may operate on nonnumeric selected values; actual type/value checking
requires the authorized dataset and is therefore implemented with the tools.

Correlation permits pearson or spearman and missing_policy pairwise_complete or
error. Its x_column and y_column must differ and belong to the same dataset.
No extra dataset_ids, joins, free-form SQL/code, method on aggregation, grouping
on correlation or invented operation is accepted. Data-dependent errors such as
missing columns, insufficient pairs or constant input are deferred to execution.

Each numerical call takes one dataset even when a conversation attaches several.
See [architecture](agent-architecture.md): persistent context, clarification and
needs_input remain plan 7, and are not introduced by this contract step.

## Privacy and failures

DatasetFailure permits dataset_not_found, invalid_csv, dataset_limit_exceeded,
invalid_column, non_numeric_column, missing_values, insufficient_data,
constant_input, result_limit_exceeded and (added in 5.5) numeric_range_exceeded. Unknown and cross-owner resources must
later share dataset_not_found; the model does not implement that lookup itself.
Transport/status mappings remain plan 7.

Filename/column/preview values are hidden from normal model repr, and inherited
validation errors hide rejected inputs in their string rendering. This does not
make model_dump(), validation error dictionaries, previews or arbitrary logging
safe: they can contain private data. Existing safe HTTP error handling must be
reused when routes are delivered. No PII scanner or telemetry payload change.

## Verification and delivery

Added **61 contract tests** covering valid JSON/strict tool round trips, counts,
ceilings/exact boundaries, immutable views, column consistency, preview shapes,
unknown/unsafe fields, explicit missing policies, operation compatibility and
existing runner DatasetSummary compatibility.

Initial focused gate: 99 passed before six additional boundary cases. Final
offline regression gate: **409 passed, 102 PostgreSQL/browser tests deselected**,
148 upstream warnings, 11.85 seconds. Ruff format/check and pip consistency pass.
No dependency pin, existing source file or migration changed. No live credentials
or model APIs used. Required GitHub tests still apply to the PR revision; local
offline success is not a full database/browser or remote-CI result.

The architecture/plan handoff docs were already uncommitted when this step began;
they are preserved on the feature branch, not claimed as previously merged.
Parent canonical note and detailed plans live outside the service Git repository.
Next implementation is 5.2 owned persistence after 5.1 delivery; stop here.

Delivery update, 4 November 2026: 5.1 delivery is accepted from user confirmation;
5.2 storage is now complete locally, with PR tests/delivery pending. Earlier
pending/stop notes are historical. Current next implementation is 5.3 after
5.2 delivery. No contract semantics were changed by the storage step.

Step 5.5 adds [typed finite statistical outputs](descriptive-tools.md) and the
numeric_range_exceeded failure code. Existing dataset inputs/ownership contract
semantics are unchanged; output contracts are still internal, not an HTTP API.

Step 5.6 adds group_limit_exceeded and [typed aggregate/correlation outputs](aggregation-correlation.md).
Existing dataset/tool argument shapes remain unchanged. Counts, finite coefficients
and unavailable-result invariants are internal contracts, not new HTTP schemas.
