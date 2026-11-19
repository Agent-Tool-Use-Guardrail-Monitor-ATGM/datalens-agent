# Resource and output guards — step 6.3

Verified: 5 November 2026. Branch: `feature/tool-resource-guards`. Step 6.2
delivery is accepted from the user's pull confirmation. Step 6.3 is complete
locally; required PR `tests` and delivery remain pending.

## Dataset execution limits

`StatisticsTools` accepts a validated, immutable `dataset_limits: DatasetLimits`
snapshot alongside its existing `limits: StatisticsLimits`. It forwards dataset
limits to `DatasetService.load(..., limits=...)` for every numerical invocation.
Load takes the minimum of service and execution limits for every integer bound;
neither a default executor nor a permissive execution snapshot can relax service
limits. Existing callers without an override keep the service's limits.

After owner-filtered metadata lookup, byte, row, column and header-name limits
reject oversized datasets before content fetch or worker dispatch. The owned
content is then parsed under the combined bounds, including actual byte, record,
header and cell limits. This protects datasets ingested under an older or more
permissive configuration. Existing hash/integrity and post-parse ownership checks
remain active. A failure returns `dataset_limit_exceeded` and creates no artifact;
unknown/foreign datasets keep their existing indistinguishable lookup failure.

Composition in 6.4 must pass the admitted policy's dataset and statistics snapshots
to the real executor. This step provides and tests the enforcement interface;
it does not install a production policy or expose public run endpoints.

## Reused execution and output safeguards

- One shared `DatasetParser` per process bounds parsing and numerical work to
  1–4 worker slots, rejecting excess work immediately. Share this instance between
  the dataset service and statistics executor; do not create one per request.
- Existing runner tool/run deadlines and model budgets remain authoritative.
  There are no new retries, timers, queues or alternate execution paths.
- Cancelling or timing out an await cannot terminate a Python thread. Its slot
  remains occupied until the concurrent future actually finishes. Repeated
  attempts are rejected while busy; a cancelled executor continuation cannot
  persist or return a success artifact. Shutdown waits for admitted work.
- Aggregation enforces admitted `max_groups` (ceiling 50) without partial results.
  Complete JSON artifacts enforce admitted `max_result_bytes` (ceiling 16,384)
  before owner-scoped persistence. Exact byte boundaries are inclusive; exceeding
  the bound fails rather than truncating numerical evidence.
- Model observations remain at most 1,024 UTF-8 bytes. A larger admitted result
  yields an explicit artifact notice, while the complete numerical evidence stays
  in the owned artifact. This notice does not expose the artifact's unseen values
  to the LLM. Existing runner contracts validate observations/call IDs; artifact
  writes and later reads remain owner-filtered. No arbitrary artifact-fetch tool
  or new model access is introduced.

Hard CPU termination, cross-replica admission and isolated process workers remain
deployment concerns for plan 10. Aggregate per-user storage quotas and planned
authentication cleanup remain separate work; these guards do not implement them.

## Verification and handoff

Added 16 real PostgreSQL tests: dataset boundaries and excess, early rejection
before byte reads, service/execution intersection, group boundaries, exact result
bytes, and repeated admission attempts after a real threaded computation times
out. Existing parser, tool, runner-budget, ownership, oversized-observation and
artifact tests remain in the regression gate.

Focused gate: 52 passed. Final non-browser gate: **831 passed, 17 browser tests
deselected**, 214 upstream warnings, 51.40 seconds. Ruff, formatting, dependency
and whitespace checks pass. Browser/auth code is unchanged; browser tests were
not rerun for this step and required CI still includes them. No dependency,
migration, numerical formula, public HTTP or live model/cloud change.

No commit or push. Stop before 6.4. Parent plans, canonical progress and workspace
README live outside this service Git repository and are not part of its PR.
