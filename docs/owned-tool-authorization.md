# Owned execution authorization — step 6.2

Verified: 5 November 2026. Branch: `feature/owned-tool-authorization`. Complete
locally; required PR `tests` and delivery pending. Step 6.1 delivery is accepted
from the user's pull confirmation; no remote CI audit was repeated.

## Scope and interface

`ToolExecutionScope` in `contracts/policy.py` pins an immutable AgentContext and
at most 16 unique selected dataset UUIDs. Empty selection permits no numerical
dataset call. Construct scopes from trusted admission state, never model-supplied
ownership claims. Another principal, browser session, run, session or request
cannot reuse this policy instance by supplying the same dataset ID.

`OwnedToolPolicy` in `agent/authorization.py` implements the existing asynchronous
`evaluate(context, proposal) -> ToolDecision` interface. Its constructor takes
the scope, pinned PolicyRules, existing DatasetService and a required current-access
callback. The callback must return exactly True; truthy integers, strings or
principal objects do not grant permission. Constructing identity contracts does
not authenticate a caller. No new token validation, introspection or role cache
is introduced.

Trusted composition can construct:

```python
policy = OwnedToolPolicy(
    scope=ToolExecutionScope(context=current_context, dataset_ids=selected_ids),
    rules=rules,
    datasets=dataset_service,
    authorize=check_current_access,
)
decision = await policy.evaluate(current_context, normalized_proposal)
```

This is an internal interface, not application wiring. Step 6.4 will compose
the policy with the runner after 6.3 resource guards. No public route is enabled.

## Checks and failure behavior

Evaluation revalidates typed inputs, matches the exact admitted context, checks
current access, applies static rules, requires a selected dataset ID, reads current
metadata through issuer/subject-filtered SQL, and checks the dataset identity and
selected column names/types. The shared argument parser uses the same original
strict schemas. Extra dataset selectors/joins and arbitrary operation/code fields
fail validation. Admin does not bypass ownership.

Only metadata is read. No CSV parsing, computation, artifact writing or long-held
transaction occurs. Missing, deleted, foreign and unselected datasets all return
denied; invalid columns return invalid_arguments without echoing private values.
Description/correlation and non-count aggregation require numeric nonempty columns.
Text count and all-missing unavailable computations remain supported. Group columns
need only exist. Actual group/row/output limits remain 6.3 and numerical outcomes
remain the tools' responsibility.

Callback, database or metadata-integrity failures raise PolicyUnavailableError
with a fixed message and redacted PolicyEvaluation(status="unavailable"), preserving
the actual call ID and pinned policy reference. Original errors are not chained.
Cancellation propagates without fabricating a denial. The runner's existing error
boundary fails closed; durable evidence integration remains 6.4/7.

Control functions are not numerical operations. Existing runner finalization
continues checking current access and successful current-run result references;
its access-loss/invalid-reference/mixed-batch regressions pass. Clarification is
still reserved and not implemented. No duplicate control dispatcher is added.

Dataset IDs are immutable through the supported service; no update path is added.
A preflight allow can nevertheless become stale. Actual loads and artifact writes
retain independent ownership/existence checks. A tested deletion after approval
fails at the actual executor without creating a success artifact. No dataset lock
is held through model execution, and running work is not promised instantaneous
revocation. The admission scope is not a permanent authorization capability.

## Verification and delivery

Added 56 tests: users/admins, issuer isolation, execution identity, selection and
deleted/missing data, strict current access, malformed/control calls, column/type
semantics, all-missing data, operational failures, cancellation, tampered scopes,
metadata substitution, delete-after-allow and lightweight authorization imports.
Ownership checks use real disposable PostgreSQL; no model request is made.

Initial focused gate: 108 passed. Final non-browser gate: **814 passed, 17 browser
tests deselected**, 214 upstream warnings, 42.12 seconds. Python Ruff/format,
dependency consistency and whitespace checks pass. Browser-auth code is unchanged
and was not rerun; required PR CI still includes it. No migration, dependency,
public HTTP, JWT, numerical or cloud change.

Disposable test PostgreSQL removed; developer/identity data untouched. No commit
or push. Parent plans, canonical progress and workspace README are outside this
repository and its PR. Stop before 6.3 resource/output guards; production wiring
remains 6.4 and security follow-up 3.7 is pending.
