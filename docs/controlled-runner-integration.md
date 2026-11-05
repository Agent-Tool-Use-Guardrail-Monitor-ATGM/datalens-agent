# Controlled runner integration — step 6.4

Verified: 5 November 2026. Branch: `feature/controlled-runner-integration`.
Step 6.3 delivery is accepted from the user's pull confirmation. Step 6.4 is
complete locally; required PR `tests` and delivery remain pending.

## Composition interface

`agent.composition.create_statistics_session(...)` constructs the existing
single-use `StatisticsSession` with real `PolicyRules`, `OwnedToolPolicy` and
`StatisticsTools`. It validates the trusted execution scope and policy snapshot,
pins the same policy reference in runner state, and passes that snapshot's dataset
and statistics limits to the executor. No permissive fallback policy exists.

The caller supplies scope, policy configuration, dataset service, worker, artifact
store, model adapter, toolset reference, current-access callback, event sink,
sequence allocator and deadline. Share the dataset service's lifespan-owned
worker, and use the same sink/sequence for the adapter and runner. The factory
does not own or close these process-scoped resources. ModelAdapter retains its
existing per-attempt HTTP-client lifecycle.

Call `session.run(question, summaries)` once. Admission must authenticate the
principal and load the selected dataset summaries through owner-filtered metadata
reads; caller-supplied summaries are not authorization evidence. The factory is
an internal composition boundary, not an HTTP endpoint or a replacement for plan
7 admission. Numerical policy still checks the actual selected ID and owned
metadata, and the executor independently checks owned content and artifacts.

Current access is bound to the exact admitted AgentContext. Finalization retains
its separate current-access and successful-result-reference checks; it is not
passed to numerical policy. Unknown/malformed proposals fail safely before
policy execution. Native CrewAI tools remain empty, delegation/code execution
remain disabled, and the existing controlled continuation remains authoritative.
Clarification behavior is still plan 7.4, not newly implemented here.

## Redacted verdict handoff to plan 7

Numerical `tool_call.completed`/`tool_call.failed` data now includes additive
`policy_evaluation`, serialized from the existing `PolicyEvaluation` contract:

- `tool_call_id` identifies the same server-assigned proposal.
- `policy` contains the pinned non-secret configuration UUID and version.
- `status` is `allowed`, `denied`, `unavailable` or `not_evaluated`.
- `decision` appears only for an evaluated allow/deny, with matching call ID,
  strict `allowed` boolean and the existing bounded reason enum.

Registry/argument/access rejection before policy is `not_evaluated`. Exceptions
or invalid decision contracts are `unavailable`; raw errors are not retained in
event data. A policy allow followed by execution failure stays `allowed`, paired
with the failed tool outcome. This distinguishes authorization from successful
computation. Finalizer outcomes carry their control reason without pretending to
have a numerical policy verdict.

Existing event names, top-level envelope schema and outcome fields are preserved.
Policy evidence contains no prompts, raw arguments, CSV values, credentials or
fraud labels. Requested evidence still precedes execution; required sink failures
stop dispatch. The current sink supplies no durability by itself. Plan 7 must
persist evidence/outbox records, version its full payload contract, and publish
through Kafka. Digest-only proposal telemetry still lacks sufficient behavioral
features for classifier training; richer redacted argument features and separate
mock ground truth remain plan 7 work. No operational failure is an attack label.

## Verification and delivery

Added 16 integration cases using real PostgreSQL, CSV parsing, policy checks and
artifact persistence with only model HTTP mocked. Cases cover all four numerical
tools and finalization, disabled tools, unselected/foreign datasets, malformed/
unknown calls, unavailable policy, limits after allow, incorrect final references,
access removed before finalization and preserved verified partial results. Tests
validate policy version/call correlation, ordering, redaction and no artifact on
denial. Existing runner, policy, timeout and resource regressions remain active.

New focused cases: 16 passed. Final non-browser gate: **847 passed, 17 browser tests
deselected**, 262 upstream warnings, 55.59 seconds. Ruff, formatting, dependency
and whitespace checks pass. Browser/auth code is unchanged and was not rerun;
required CI includes browser tests. No dependency, migration, numerical formula,
live model/cloud call or public route changed. No commit or push.

Stop before plan 7. Parent plans, progress and workspace README are outside this
repository and its PR. Plan 6 is complete locally; merge/required CI are still
needed before treating its final step as delivered.
