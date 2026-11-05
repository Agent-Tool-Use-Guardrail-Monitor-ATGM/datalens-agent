# Tool policy configuration and decisions — step 6.1

Verified: 5 November 2026. Branch: `feature/tool-policy-config`. Complete locally;
required PR `tests` and delivery remain pending. The preceding module refactor is
accepted as delivered from the user's pull confirmation.

## Available interfaces

`contracts/policy.py` defines `ToolPolicyConfiguration`, an immutable server-owned
snapshot containing a configuration UUID/version, exact numerical-tool allowlist,
aggregation-operation and correlation-method allowlists, and the existing
`DatasetLimits`/`StatisticsLimits`. Configuration cannot increase existing hard
ceilings. Unknown fields/names, duplicate entries and invalid limits are rejected.

The default tool allowlist is empty: creating a configuration does not enable any
tool. Aggregate operations default to count/sum/mean/min/max and correlation methods
to Pearson/Spearman; these lists matter only when their corresponding tool is
explicitly enabled. They can be narrowed or emptied. Python allowlists require
tuples; strict JSON parsing accepts JSON arrays. There is no new file loader,
environment setting, policy database or administration endpoint.

For example, trusted composition code can construct a restricted snapshot:

```python
from uuid import UUID

from statistics_agent.agent.policy import PolicyRules
from statistics_agent.contracts.common import ConfigurationReference
from statistics_agent.contracts.policy import ToolPolicyConfiguration

rules = PolicyRules(ToolPolicyConfiguration(
    reference=ConfigurationReference(
        config_id=UUID("13ac128a-acdc-493d-85e3-7760d1906458"),
        version="statistics-policy-v1",
    ),
    allowed_tools=("inspect_dataset", "aggregate_statistics"),
    aggregate_operations=("count", "sum"),
))
```

This example UUID is illustrative, not a default production policy. Operators must
assign a new reference/version when changing rules. There is no persistent catalog
that proves uniqueness of references across independently supplied configurations.

`agent/policy.py` provides synchronous `PolicyRules.check(proposal)`. It revalidates
the snapshot at construction and validates proposals and enabled-tool arguments
using the existing strict schemas. It rejects unknown/control names, disabled
tools, malformed arguments, disallowed operations/methods and describe-column
selection above the configured column ceiling. Each result keeps the actual
tool-call ID and pinned snapshot reference; constructing new rules does not
change an existing instance's frozen snapshot. No framework, database or provider
is loaded by these modules.

## Evidence states

`PolicyEvaluation` is a separate internal contract; existing `ToolDecision` and
event payloads are unchanged:

| Status | Required evidence |
|---|---|
| `allowed` | Matching allowed ToolDecision, call ID and policy reference |
| `denied` | Matching denied ToolDecision, call ID and policy reference |
| `unavailable` | No decision; the policy could not be obtained/evaluated |
| `not_evaluated` | No decision; no policy evaluation was performed |

Unavailable/not-evaluated records may carry a known expected reference or none.
They cannot carry a fabricated denial/allow decision. A mismatched call ID,
status/decision contradiction or missing evaluated-policy reference fails validation.
Records contain no raw arguments, dataset contents, caller identity or secrets.
The future runtime must supply unavailable/not-evaluated evidence at the actual
failure/skip point; static checks do not invent an outage or emit events.

Both `finalize_statistics` and `request_clarification` are reserved control names.
The runner rejects numerical bindings using either name before infrastructure
use. This does not implement clarification; its handler remains plan 7.

## Deliberate execution boundary

An `allowed` result means **static rules permit the typed request**, not that its
dataset exists or belongs to the caller. `PolicyRules.check` deliberately does
not implement the asynchronous `ToolPolicy.evaluate(context, proposal)` execution
interface. Step 6.2 must combine static rules with owned-resource authorization;
6.3 must enforce actual data/group/output limits. Declaring limits in a snapshot
does not enforce them against unseen CSV data. Storage ownership remains enforced
by existing dataset reads and artifact writes.

Step 6.4 will compose the complete policy with the controlled runner and pin the
same reference/snapshot for the admitted execution. Current production execution
is not newly wired, and no public run route is enabled. Versioned durable verdict
payloads/outbox/Kafka remain plan 7. Operational failures/denials are not automatic
fraud labels. Roles, JWT validation, numerical computation and model budgets are
unchanged.

## Verification and delivery

Added **44 tests** covering default denial, all four permitted shapes, unknown and
reserved names, registry shadowing, strict/duplicate configuration, invalid and
disallowed operations/methods, selected-column ceilings, frozen/tampered snapshots,
strict JSON input, evidence identity/state consistency and framework-free imports.

Baseline offline gate: **559 passed**, 172 infrastructure tests deselected.
Initial focused policy/neighbor gate: **95 passed**. Final complete non-browser
gate: **758 passed, 17 browser tests deselected**, 214 upstream warnings, 37.60
seconds, including real disposable PostgreSQL tests. Python Ruff/formatting,
dependency consistency and whitespace checks pass. Browser tests were not rerun
for this step; required CI still includes them. No live LLM/identity-provider or
cloud calls, new dependencies, migration or public HTTP change.

Disposable PostgreSQL removed; developer/identity data untouched. No commit/push.
Parent plan/progress/README changes are outside the service repository and PR.
Stop before **6.2 — Owned execution authorization**. Security follow-up 3.7 remains
unimplemented and required before public hosting.
