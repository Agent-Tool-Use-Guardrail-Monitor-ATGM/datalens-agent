# Agent/tool integration — step 5.7

Updated: 5 November 2026. Complete locally; required PR tests/delivery pending. Branch: `test/agent-tool-integration`.

## Available composition

The integration tests compose the existing controlled `StatisticsSession` with
all four real `StatisticsTools` bindings, an owned `DatasetService`, shared bounded
parser/compute worker and PostgreSQL artifact store. Only the provider is mocked;
CSV parsing, numerical work, ownership filtering and artifact commits are real.
The explicit test policy is not production authorization. There is no new run,
dataset or result HTTP endpoint, automatic app wiring, durable job, or Kafka sender.
Production policy remains plan 6; jobs/outbox and business APIs remain plan 7.

Each call computes against one immutable owned dataset. Tests analyze separately
loaded datasets with different results; they do not implement joins, conversation
storage or cross-dataset statistics. Finalization references the actual successful
tool-call ID. It cannot turn an unknown reference or numerical failure into a
verified result. Artifacts are committed before the model receives observations,
so a later invalid final answer or input-budget stop retains computed results.
Authorized retrieval for a frontend still requires plan 7 APIs.

## Bounded model requests

The previous full registry and verbose instructions could exhaust the existing
input allowance before useful continuation. Prompt `statistics-v3` and lean wire
schemas remove redundant presentation overhead. The provider sees all four
numerical functions plus `finalize_statistics` on every request; additional
calculations remain possible while budgets permit. No tool or previous message
is silently removed to make a request fit.

Wire schemas retain types, required fields, enums, UUID formats and extra-field
rejection. Titles, defaults and string/array length hints are omitted; leaf
schema definitions are inlined where possible. Original Pydantic models remain
authoritative and still reject oversized, duplicate, malformed or extra inputs.
Provider guidance is not a substitute for runtime validation.

Reservation uses the compact UTF-8 JSON encoding of the actual pinned HTTPX
request plus the existing 256 framing units. Tests compare reservations against
captured request bytes. The per-request limit remains **4,096 units**, the hard
payload limit remains **65,536 bytes**, and all run/output/step limits remain
unchanged. This is a conservative admission heuristic, not a provider tokenizer
or a universal token-cost bound. No dependency/profile or limit was increased.

Small fixture runs for every tool now compute and finalize with the entire
registry. This does not guarantee that every admitted CSV, long question, wide
metadata set, or multi-step transcript fits the model context allowance. Oversized
initial requests stop before HTTP. Accumulated overhead may stop a later request
as `request_input_limit`, preserving successful artifacts and returning incomplete.
A smaller synthetic two-computation fixture can now finish its third model turn;
that is a serialization improvement, not a budget bypass.

## Observations and authoritative results

`ToolResult.summary` remains a string internally. Structured JSON summaries are
embedded as JSON in the model observation rather than double-escaped strings;
legacy plain-text summaries remain supported. An observation carries the actual
tool-call ID and only the artifact UUID. Full artifact references stay in local
run state; storage metadata is not needed by the model.

A small statistics summary contains `result` with numerical values, missing/drop
counts, availability reasons and relevant method semantics. It omits duplicated
dataset/column/operation selectors already present in the correlated tool request,
and backend/version/dtype metadata. The complete typed result, selectors, dataset
checksum, schema version and actual call ID remain in the persisted artifact.
Tests compare summary values with this authoritative payload. No number is rounded,
recomputed or silently truncated for the model.

The summary limit remains **1,024 UTF-8 bytes**. Larger results use an explicit
`details_in_artifact` notice; that notice exposes no omitted numerical values.
The full result must still fit **16,384 bytes** or fail before success persistence.
A valid explanation/reference proves structural provenance, not that arbitrary
LLM prose is numerically or semantically correct. Consumers should display the
verified artifacts independently of generated prose.

## Events and delivery boundary

The existing seven event types remain unchanged. These tests verify contiguous
model/tool lifecycle events, request evidence before policy/execution, and the
separate finalizer event. Numerical outputs, raw CSV, authentication identifiers
and synthetic provider keys are not added to telemetry. Session boundaries,
production payloads, durable evidence delivery and Kafka outbox remain plan 7.
The existing digest-only observation/classifier evidence limitation remains.

## Verification

New coverage checks all four real tools through the runner, committed provenance,
model-visible numbers, distinct datasets, invalid final references, access/policy
denial, unknown tools, strict arguments, missing/deleted/cross-owner datasets,
numerical errors, result bounds and both initial/accumulated input limits.
Offline provider fixtures now expect structured observations. No live provider
request or real credential is used. Azure and current live Groq compatibility
are not newly verified by this step.

Required GitHub `tests` remains the PR delivery gate, including its browser tests.
Parent plans, canonical note and frontend handoff live outside this Git repository
and are not published by the service PR. Stop before plan 6.1.


Verification: **709 passed, 17 browser tests deselected**, 214 upstream warnings,
33.58 seconds, with real disposable PostgreSQL and mock model transport. Added
26 tests; Python Ruff checks/formatting (`src` and `tests`), pip consistency and
whitespace checks pass. Browser/auth behavior was not changed; required GitHub
`tests` still includes browser coverage. No live LLM call, new dependency,
migration, auth/HTTP change or cloud deployment. Only the fixed disposable test
PostgreSQL was used and removed; developer/identity databases remain untouched.
No commit or push performed. Required PR checks/delivery remain pending.
