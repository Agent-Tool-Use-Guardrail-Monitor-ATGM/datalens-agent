# Execution limits and observed retries — step 4.5

Updated: 29 October 2026. Step 4.5 delivery accepted from user pull confirmation.
Subsequent [4.6 provider verification](provider-verification.md) remains in progress;
this document preserves the execution-control implementation evidence. No real inference request was performed.

## Configuration

The explicit model settings loader accepts these additional server-owned fields:

| Variable | Default | Allowed range |
|---|---|---|
| LLM_RUN_INPUT_BUDGET | 32768 | 1–262144 |
| LLM_RUN_OUTPUT_BUDGET | 8192 | 1–65536 |
| LLM_MAX_STEPS | 8 | 1–32 model attempts, including retries |
| LLM_RUN_TIMEOUT_SECONDS | 120 | 0.01–600 seconds |
| LLM_TOOL_TIMEOUT_SECONDS | 10 | 0.01–60 seconds per execution |
| LLM_RETRY_MAX_DELAY_SECONDS | 10 | 0–60 seconds |

Existing LLM_MAX_RETRIES (default 2, maximum 3) permits at most that many extra
attempts per logical model call. Existing LLM_CONCURRENCY (default 1, range 1–4)
now controls process-level HTTP admission. The public configuration snapshot is
model-profile-v2; its deterministic reference includes these values. Key rotation
alone does not change that reference. The real ignored .env was not modified.

## Retry ownership and evidence

StatisticsSession is the only retry owner. ModelAdapter still makes one physical
attempt per invocation. CrewAI's automatic wrapping remains disabled, and no SDK
retry or provider fallback is introduced.

- Retry HTTP 429 and selected 500/502/503/504 failures only.
- Do not retry authentication errors, invalid responses, network failures or
  timeouts with unknown provider outcomes. Tools are never retried automatically.
- Preserve one logical model_call_id across retries with increasing attempt_index;
  each attempt gets its own model terminal event before any delay. A new model
  conversation round gets another logical ID.
- Honor finite nonnegative numeric Retry-After values. If a delay exceeds the
  configured wait limit, stop rather than retrying earlier. HTTP-date Retry-After
  parsing is not implemented. Invalid/missing values use bounded exponential
  delays of 1, 2, 4 seconds, capped by the configured maximum.
- Recheck authorization before every admitted attempt, including after backoff.
  Enforce remaining attempt and usage budgets before another request.

A server error can follow billable provider work. Reservations bound our retry
admission; no billing refund or exactly-once provider execution is promised.
Provider/model profile remains pinned; switching provider requires a new session.

## Input/output accounting

The provider performs real tokenization. We do not claim exact tokenizer support.
For admission, ModelAdapter estimates input from UTF-8 JSON payload bytes plus
256 framing units (including tool schemas and conversation replay). This is a
conservative application heuristic, not a universal upper bound on provider
billing tokens. A request exceeding the existing 4,096-unit per-request cap or
remaining run-input budget is rejected before HTTP. A small real-token request
can therefore be rejected conservatively.

Before an attempt, reserve input estimate and requested maximum completion tokens.
The output allowance is capped by both the profile and remaining run budget.
Unknown/missing/failed-attempt usage retains its full reservations. Known input
and output totals refund the difference; cached input and reasoning counts remain
subsets and are not added twice. Public usage continues to report null for
unreported counts; reservation accounting does not fabricate reported usage.

If reported input or output exceeds its reservation, debit the reported count
with a zero floor, stop with reported_usage_exceeded_reservation and dispatch no
new tool. The existing response already consumed whatever the provider reports;
we cannot reverse it. An exact spend cap requires provider-side controls and,
where available, model-specific token counting. Estimated USD cost remains null.

AgentState stores the remaining debited budgets and completed observed attempts.
An interrupted admitted attempt can lack a completed-attempt count/terminal event;
plan 7 must reconcile interrupted jobs and durable evidence rather than pretending
that a cancelled request never reached the provider.

## Deadlines, cancellation and admission

The session deadline is the earlier caller deadline or configured run cap from
session construction. It bounds authorization, queue waiting, model operations,
retry backoff, policy checks, tools and event-sink waits. A run deadline returns
an explicit incomplete outcome with terminal_reason=deadline_exceeded. Explicit
caller cancellation propagates as CancelledError rather than masquerading as a
successful or fully reconciled run.

Each tool additionally has an asynchronous timeout. A timed-out tool receives
status timed_out and a failed tool event; it is not replayed. Cancellation is
cooperative: a synchronous CPU task or remote side effect cannot be undone by
asyncio timeout. Real tool implementations must respect cancellation and enforce
CPU/process/remote limits in plans 5–6. This step provides no process sandbox.

ModelLimiter uses cancellation-safe nonblocking semaphore acquisition and releases
slots in finally. Default limiters are shared by provider across sessions/event
loops in this Python process. Admission time is included in the request deadline;
a queue timeout before HTTP has outcome_unknown=false. Restart to change a live
provider's concurrency limit; conflicting in-process configuration is rejected.

This is not an organization-wide RPM/TPM/daily limiter. Separate worker processes,
Container Apps replicas and other teammates' clients can multiply concurrency and
share provider quotas. Plan 10 must choose bounded worker/replica counts (start
with one) or an explicit distributed admission design. Provider 429 handling
remains required even with one replica.

## Internal result and verification

StatisticsRun now includes terminal_reason alongside the unchanged answer status.
Examples: model_authentication, model_timeout, model_retry_exhausted,
retry_delay_exceeded, access_unavailable, input_budget_exhausted,
output_budget_exhausted, request_input_limit, step_budget_exhausted and
reported_usage_exceeded_reservation. Success/unsupported answers use their status
as the reason. Existing tool-policy failures can retain generic incomplete with
a typed tool result/redacted failure event; the future HTTP contract is not frozen.
No new event types, API endpoints, migrations or dependencies were added.

Twenty-one new offline checks verify bounded retries, per-attempt identity,
no silent provider changes, permanent/unknown failures, long Retry-After,
reservation/usage behavior, reauthorization, input/step/output admission,
tool timeout, deadline/backoff/authorization and process-shared cancellation-safe
concurrency. All model requests use MockTransport and synthetic credentials.

Regression: **336 passed, 101 PostgreSQL/browser tests deselected**, 115 upstream
CrewAI deprecation warnings. Ruff/formatting, pip consistency and whitespace
checks pass. Full required `tests` CI remains the delivery gate. No real model
compatibility, cloud deployment, actual statistics implementation or durable
worker behavior is claimed. Stop before 4.6 live-provider verification.


Step 4.6 continuation correction preserves these budgets and admission rules.
A reserved final-answer function adds schema/history overhead to the same input
reservation; it receives no separate execution or budget bypass. A deterministic
three-round fixture computes twice, then stops at request_input_limit before
the third model call. No token budget was increased to make the fix pass.
