# Aggregation and correlation — step 5.6

Current step 7.4 explicitly versions the per-request reservation to 6,144 for new
model-profile-v4 configurations. The 4,096 limit in the evidence below describes
the prior implementation. [Current behavior](clarification-and-partial-results.md).

Updated: 5 November 2026. **Complete locally on feature/aggregation-correlation;
required PR tests/delivery pending.** Step 5.5 delivery is accepted from the
user's pull confirmation. All four numerical bindings now dispatch through
StatisticsTools. No full model/tool composition, production policy or HTTP API
is added by this step.

## Aggregation

aggregate_statistics accepts one dataset/value column, one operation (count, sum,
mean, min, max), optional group_by column and explicit omit/error missing policy.
Count reports nonmissing selected values and permits text. Other operations
require numeric data and use NumPy float64. Nonmissing text is never coerced away.

Without grouping, return one unkeyed result (key null denotes the whole dataset).
With grouping, preserve exact nonmissing cell text and first-encounter order:
'A', ' A ', '01' and '1' are distinct keys. No data cleaning, numeric key coercion,
sorting option, join, filter expression, SQL or generated-code execution.

For omit, missing selected values do not contribute to an operation. Rows with a
missing group key are excluded entirely; do not invent an unknown-key bucket.
Retain a present group key even when all of that group's selected values are
missing, so its missing counts and unavailable result remain visible. Such groups
also count toward the group limit. Count on an empty group/sample is zero;
sum/mean/min/max are unavailable with reason insufficient_data and value null,
not an invented zero. If every key is missing, grouped output has no groups and
reports all rows as dropped. error rejects any missing selected value/group key.

Report total row_count, valid_count, missing_value_count, missing_group_count and
dropped_row_count. Dropped rows are the union of missing value/key rows, not the
sum of overlapping counts. Each retained group reports valid/missing counts.
Nonmissing selected values under a missing group key are not included in a
group's valid count. The result contract verifies total/group count consistency.

Reject a new distinct present key after StatisticsLimits.max_groups (default/
maximum 50), including groups without valid selected values. A lowered server
limit is honored. Return group_limit_exceeded; never truncate group output.
The new fixed code is added to DatasetFailure. All other argument semantics
and existing dataset ownership checks are unchanged.

## Correlation

correlate_columns accepts two distinct numeric column names from **one dataset**,
pearson or spearman, and explicit pairwise_complete/error missing policy. An
additional dataset reference or unsupported argument is rejected before execution.
Pairwise complete drops any row with either value missing and reports sample_size
plus dropped_pair_count. error rejects missing inputs. Selected nonmissing text
is rejected even if a row might otherwise be dropped.

Fewer than three complete pairs returns status unavailable, reason
insufficient_data, coefficient null. At least three pairs with a constant input
after float64 conversion returns unavailable/constant_input. No NaN placeholder
or fabricated coefficient is emitted. Successful finite coefficients are bounded
to [-1,1] by the typed result contract. Pair counts account for every input row.

Pearson uses NumPy corrcoef on complete float64 pairs. Spearman first computes
SciPy rankdata with method average for ties, then Pearson on the ranks. Both
NumPy 2.5.4 and newly installed/directly pinned **SciPy 1.18.1** are recorded in
correlation results; Spearman also records rank_method average. No p-value,
confidence interval, hypothesis test or causal claim is produced. In particular,
the implementation does not call pearsonr/spearmanr functions that calculate
significance alongside coefficients. The tools do not establish causation.

Source text stays preserved. Numeric work is approximate float64: conversion can
make very close values equal, and extreme values can overflow arithmetic. NumPy
overflow/invalid/divide errors become numeric_range_exceeded; these are explicit
failures, not missing observations or a successful nonfinite statistic.

## Executor, bounds and integration

Both tools use the same owned DatasetService load, shared bounded compute pool,
typed result validation and artifact persistence as inspection/description.
The four bindings map to explicit server-selected functions. AggregateStatisticsResult,
AggregateGroup and CorrelationResult are added to internal result contracts.
Dataset deletion before artifact creation prevents publishing a successful
result; no cross-owner artifact is created.

Reuse statistics-result-v1 provenance (actual tool_call_id, dataset checksum and
typed result), the 16,384-byte full result ceiling and 1,024-byte observation
ceiling. Larger admitted results get an explicit details_in_artifact notice with
group_count or sample_size as appropriate. The full group table is retained in
the owned artifact; oversized JSON fails result_limit_exceeded without truncation.
The model must not invent unseen details from an artifact-only notice.

No parser/storage/migration, auth/token, current app/lifespan, event payload or
cloud changes. Adding two numerical actions does not supply production ToolPolicy
or durable request evidence. Plans 5.7/6/7 still own those integrations. Full tool
schema/history overhead under the existing model-input cap remains a 5.7 gate;
no input budget increased and no live provider call made here.

## Verification and stop

Added **45 tests** for arithmetic, text count, grouped missing overlap, exact key
preservation/order, empty groups, exact/lowered group ceilings, Pearson positive/
negative/zero/nonlinear fixtures, Spearman monotonic/tied ranks, pairwise drops,
constant/insufficient inputs, overflow, finite contracts, actual artifact values,
ownership, disallowed joins and complete artifact notices. Known coefficients
are checked against manual expectations with absolute tolerance 1e-12, not by
comparing the implementation against the same library call.

Focused new-tool + existing-executor gate: **58 passed**. Full real-PostgreSQL
non-browser regression: **683 passed, 17 browser tests deselected**, 151 upstream
warnings, 32.38 seconds. Ruff formatting/checks and pip consistency pass. SciPy
1.18.1 was installed from official PyPI as a binary wheel in the existing .venv;
NumPy remains pinned at 2.5.4 and no unrelated package was upgraded.

Only fixed disposable PostgreSQL used and removed afterward; Docker Desktop
remains running. Developer/identity databases and provider credentials untouched.
No commit/push/PR performed. Required PR tests remain the delivery gate; parent
plans/canonical note/frontend tracker are outside service Git/PRs.
Stop before **5.7 — full agent/tool integration and handoff**.


## Subsequent integration — step 5.7

The [full registry integration](agent-tool-integration.md) now tests real numerical
execution and persisted artifacts through the controlled runner with a mock model.
Small observations omit repeated selectors and implementation versions while
retaining computed values/counts/availability; full typed artifacts remain unchanged.
The input cap remains 4096 units and can still stop accumulated conversations.
Production policy and durable API/event integration remain plans 6 and 7.
