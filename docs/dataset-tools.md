# Dataset storage and statistics tools — plan 5 handoff

Updated: 4 November 2026. **5.1–5.4 available per prior checks/user pull confirmation; 5.5 inspection/descriptions complete locally, PR/tests delivery pending; 5.6–5.7 not started.**
Plan 4.6 delivery is accepted from the user's pull confirmation. The provider
verification fixture is not a production dataset tool or upload API.

## Ordered implementation

1. **5.1 — Contracts:** strict dataset/column/preview models, limits, internal
   errors and arguments for four approved statistical tools.
2. **5.2 — Persistence:** owner-scoped dataset bytes/metadata and computed
   artifacts in the existing agent PostgreSQL, with a sequential migration.
3. **5.3 — CSV validation:** bounded deterministic parsing and missing-value/
   numeric semantics; add tested dependencies only when used.
4. **5.4 — Dataset service:** internal ingestion, paginated listing, preview,
   deletion and authorized loading. No HTTP routes in this plan.
5. **5.5 — Inspection/description:** real inspect_dataset and describe_columns.
6. **5.6 — Aggregation/correlation:** real aggregate_statistics and
   correlate_columns with explicit operations and numerical edge-case behavior.
7. **5.7 — Integration/handoff:** real stored data and tools through the existing
   runner with mocked inference, bounded summaries/artifacts and ownership proof.

Step 5.1 delivery is accepted per user confirmation; see
[contract semantics and verification](dataset-contracts.md). Step 5.2 delivery is
accepted per user confirmation; [storage handoff](dataset-storage.md) records
ownership, transactions and migration 0004_dataset_storage. Step 5.3 parser is
included in locally cached origin/main; [parser handoff](csv-validation.md)
records syntax/type/limit semantics. Step 5.4 delivery is accepted per user pull
confirmation; [lifecycle handoff](dataset-lifecycle.md) documents owned ingestion.
Step 5.5 is complete locally on feature/descriptive-tools; [tool handoff](descriptive-tools.md)
documents the two real numerical bindings/executor and result semantics.
Steps 5.6–5.7 remain Not started. Next implementation is 5.6 after 5.5 delivery.

## Initial design decisions

Use the current service, PostgreSQL, principal/ownership helper and typed tool
interfaces. Start with immutable UTF-8 CSV (optional BOM, comma delimiter), a
5 MiB upload cap, 10,000 rows, 128 columns, 128 characters per column name and
1,024 characters per cell. Reject malformed shape, duplicate headers and
nonfinite numbers. Step 5.1 now declares strict limits/metadata/tool inputs;
the separate step 5.3 parser now validates bytes/values, while actual ingestion
composition remains pending step 5.4.

Store bounded raw CSV in BYTEA; keep JSONB metadata and canonical JSON-text
artifacts separate. A storage interface permits a later Blob Storage adapter without
adding one now. Admin users still read only their own datasets. A preview is
at most 20 rows; the model gets authorized metadata and bounded computed
observations, not an automatic copy of CSV/preview data.

Missing cells are explicit; nonempty text is never silently converted to missing
numeric data. Numeric description uses population standard deviation (ddof=0).
Aggregation has an operation allowlist and at most 50 groups; correlation uses
Pearson/Spearman with pairwise complete samples and explicit unavailable results
for insufficient or constant samples. No generated code or arbitrary SQL.

## Boundaries

Python 3.12 and the existing .venv/requirements.txt remain the workflow. Add
pandas/NumPy/SciPy in their owning implementation steps after compatibility
checks. No new database server, microservice, Redis, vectors or MCP dependency.
Steps 5.1–5.2 delivered contracts, repositories and an additive dataset migration.
Step 5.3 adds a pure bounded CSV parser using the standard library. Step 5.4 now
connects parsing and storage through an internal service with bounded offload;
no HTTP endpoint or current app/lifespan wiring is added. Step 5.5 adds two
numerical tools/executor using directly pinned NumPy 2.5.4. Aggregation/correlation
remain pending; there is no configuration or API change. Only disposable
PostgreSQL was used; schema remains 0004_dataset_storage.

Plan 6 owns production tool policies. Plan 7 owns dataset/run/result HTTP APIs,
durable execution and Kafka/outbox; plan 8 owns frontend integration. Existing
seven lifecycle event types remain unchanged. Do not show upload/run endpoints
as available until their owning step delivers and verifies them.

The detailed canonical plan is a parent-workspace file at
docs/plans/5_dataset_storage_and_statistics_tools.md, outside this Git repository.
This handoff is the publishable service summary, not evidence of implementation.

## Confirmed conversation integration

The [shared architecture](agent-architecture.md) permits multiple datasets per
conversation, analyzed separately without joins. Each numerical call accepts
one owned immutable dataset. Plan 7 stores history/builds bounded context, adds
request_clarification/needs_input, and preserves verified partial results if
final explanation fails. These control functions are not additional numerical
tools. Plan 5 makes numerical artifacts independently persistable and bounded;
it does not implement conversations, workers or public APIs.
