# Inspection and descriptive statistics — step 5.5

Updated: 4 November 2026. **Step 5.5 delivered per user pull confirmation.** Historical two-tool evidence below used feature/descriptive-tools. Step 5.4 delivery is accepted from the
user's pull confirmation. Two real numerical tools are available internally;
aggregation/correlation, production policy and public run APIs remain pending.

## Bindings and execution

`statistics_agent.tools.statistics.STATISTICS_BINDINGS` contains inspect_dataset
and describe_columns, using the existing strict argument contracts. StatisticsTools
implements the ToolExecutor protocol and exposes those bindings for later controlled
runner composition. Control functions such as finalize_statistics are not numerical
tools and cannot dispatch through this executor.

Execution validates the known tool name/JSON arguments, then loads through the
owner-scoped DatasetService. It offloads one closed, server-selected calculation,
validates typed output, applies result bounds and persists an owned JSON artifact
before returning a completed ToolResult. No model-generated function, eval, SQL,
file path or code argument is accepted. Each call targets one dataset.

The caller still owns the runner's matching policy decision, current session
authorization, pinned configuration and required request evidence. This executor
does not supply an allow-all production policy, expose a route or enable CrewAI's
native executor. Plan 6 supplies policy; plan 5.7 verifies the full agent/tool
composition and model-input guard; plan 7 supplies durable runs/APIs/Kafka.

## Numerical behavior

| Tool | Output |
|---|---|
| inspect_dataset | Actual row count and ordered column names, numeric/text kinds, nonmissing/missing counts |
| describe_columns | Selected-column valid/missing counts, availability and min/max/mean/median/population standard deviation/Q1/Q3 |

Description uses **NumPy 2.5.4**, now pinned directly in requirements.txt. This
version was already installed and was verified locally; pandas/SciPy are not
introduced while unused. Computation converts validated nonmissing decimal text
explicitly to float64. Dataset source bytes/text remain preserved; results are
floating-point approximations, not exact-decimal arithmetic.

Population standard deviation uses ddof=0. Quartiles/median use NumPy quantile
with method linear: position (n-1)*p with linear interpolation. For 1,2,3,4,
Q1=1.75, median=2.5 and Q3=3.25. Singleton/constant samples have population
standard deviation zero. Backend/version, dtype, ddof and quantile method are
included in the descriptive result for reproducibility.

The selected-column order is preserved. Reject an absent column or a selected
text column containing nonmissing values. An unrelated unselected text column
does not prevent numerical work. missing_policy omit excludes only missing
values and reports their count; error rejects missing selected inputs. No text
is silently coerced to missing data.

Empty/all-missing samples with omit return a successfully computed result whose
column status is unavailable, reason insufficient_data and metrics null. This
also applies to zero-observation text-kind columns, which have no numeric values
to infer. With error, actual missing inputs remain a missing_values failure.
No NaN/Infinity value is used as a placeholder. NumPy overflow/invalid/divide
errors produce numeric_range_exceeded; finite inputs can exceed arithmetic range,
so not every accepted numeric dataset is guaranteed describable. Typed metrics
also reject nonfinite output. No sample standard deviation, hypothesis test,
chart, data cleaning, grouping or cross-dataset statistic is added.

## Typed results, provenance and bounds

New contracts in statistics_agent.contracts.statistics define DatasetInspection,
DescribeColumnsResult, ColumnDescription and DescriptiveMetrics. Availability
requires consistent sample count/metrics/reason. Existing DatasetFailure adds
numeric_range_exceeded; other dataset argument contracts retain their semantics.

Each persisted statistics-result-v1 payload records the actual tool_call_id,
tool name, immutable dataset checksum and typed result with dataset ID. Return
the exact same call ID and server-issued ArtifactReference in ToolResult. Artifact
creation rechecks the owned parent: deletion after computation prevents publishing
a successful result. The finalizer's own call ID is never a computation result.

Full result JSON must fit StatisticsLimits.max_result_bytes (default/max 16,384).
Exceeding the bound returns failed/result_limit_exceeded before creating a success
artifact; no silent column truncation. Configure executor/store limits consistently
when composing them. Some valid very wide datasets can exceed result bounds.

Observations are at most **1,024 UTF-8 bytes**. Small payloads are included in full.
Larger admitted results produce an explicit details_in_artifact notice with tool,
dataset ID and column count; the complete numbers remain in the owned artifact.
That notice does not mean the model saw the full table. It must not invent numbers
from it. Future UI/results APIs must present authoritative typed artifacts, and
plan 5.7 must verify entire tool-schema/history overhead under existing model
input limits. No budget was increased here.

Known validation/data errors return fixed failed summaries without raw arguments
or data. Unknown names, including control names, return unknown_tool. Invalid
JSON/extra arguments return invalid_arguments. Busy/closed processing returns
execution_unavailable. Unexpected storage/system failures may propagate to the
existing controlled runner's failure boundary; they are not logged by this module.
Cancellation still propagates. Failures are operational evidence, not attack labels.

## CPU admission and integration limits

DatasetParser.compute now shares the existing cancellation-safe bounded pool
with parsing. Only server-owned callables reach it. Keep one shared worker
instance for DatasetService and StatisticsTools per process/lifespan; constructor
injection does not enforce a process-global registry. Legacy parser busy/closed
exception names are retained for compatibility and now describe that shared pool.
No DB transaction spans the calculation. Cancellation cannot kill a thread;
its slot remains held until actual completion. Cancellation during an artifact
commit can leave an unknown outcome, requiring plan 7 reconciliation.

No direct FastAPI/app/lifespan integration or live Groq call occurs in this step.
Persistent histories/jobs and full event evidence remain future work. Tool result
serialization is private data; do not log summaries/artifacts as telemetry. The
current seven event types/payload implementation are unchanged.

## Verification and stop

Focused numerical/executor/shared-worker gate: **41 passed**. Added 34 tests across
hand-calculated arithmetic, linear quartiles, population std, constant/singleton/
empty/missing samples, nonnumeric columns, overflow, finite result contracts,
real owned storage/artifact provenance, admin isolation, full versus artifact
observations, oversized rejection and deletion after computation. Shared compute
admission/offload is tested with existing parser cancellation regressions.

Final real-PostgreSQL non-browser regression: **638 passed, 17 browser tests
deselected**, 151 upstream warnings, 30.16 seconds. Ruff format/check and pip
consistency pass. Only NumPy was added as a direct pin; no package installation
was needed locally. No schema, persistent developer/identity data, auth/provider
setting, API or cloud changes. Disposable test PostgreSQL removed after checks;
Docker Desktop remains running. Required PR tests/delivery pending; no commit,
push or PR performed. Parent plans remain outside service Git/PRs.
Stop before 5.6 aggregation/correlation.

Step 5.6 update — 5 November 2026: [aggregation/correlation](aggregation-correlation.md)
now extend the executor to all four bindings. Existing descriptive semantics and
artifact guards are preserved; full model/tool integration remains 5.7.
