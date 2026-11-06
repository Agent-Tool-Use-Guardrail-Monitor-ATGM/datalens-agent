# Bounded conversation context — step 7.3

Verified: 5 November 2026. Branch: `feature/bounded-conversation-context`. Step 7.2
delivery is accepted from the user's pull confirmation. Step 7.3 is complete
locally; required PR tests/delivery remain pending. No new migration, route,
worker, history writer, provider request during preparation or event producer.

## Internal flow

Use the real composed StatisticsSession with the stored run's pinned configuration:

```python
prepared = await session.prepare_context(conversations, datasets, artifacts)
result = await session.run(prepared.question, prepared.datasets, prepared=prepared)
```

The caller still owns resources, authentication, run admission and eventual
persistence. Preparation checks current access and the remaining run deadline;
the normal runner rechecks access and charges the actual request budget at model
dispatch. Preparation consumes no model tokens or steps and makes no provider call.
It does not claim/finish jobs or append assistant messages.

`agent.context.build_context` resolves the current owned admission from the exact
run ID. The run must be queued/running and all pinned versions must match the
session. Every selected dataset is reread through owner-filtered metadata and must
match the admitted snapshot. No CSV bytes are read, no joins are inferred and no
numerical operation is executed. Multiple datasets retain explicit IDs and ordered
metadata even when their columns have identical names; ambiguous input is not
resolved by silently selecting a table. Clarification behavior belongs to 7.4.

## History and privacy

The repository's `recent_messages` uses bounded SQL to fetch at most 13 messages
strictly before the admitted user question, within the owned conversation. The
builder considers only the newest 12, in chronological order. It removes complete
oldest optional turns until the final request fits, setting `history_omitted` when
anything was omitted, including history older than that window. Repeated preparation
of unchanged storage produces the same context/reservation.

Only two initial model messages are created: the pinned system instructions and
one user JSON object with current question, selected metadata, verified prior
results, `unverified_history` and the omission flag. Old assistant/user prose is
untrusted data inside that JSON, never replayed as a system/tool instruction or
treated as numerical provenance. No full CSV, complete-history replay, identity
issuer/subject, browser-session reference, OAuth token or API key is added.
History text remains untrusted user-visible text, not automatically redacted data.

PreparedContext is a private immutable server-built snapshot, not a client
authorization credential. Do not expose an endpoint accepting it. Preparation is
an authorized read snapshot, not a perpetual access/deletion capability: reprepare
when execution is delayed/recovered and keep live access/tool/artifact checks at
their normal boundaries. Deleted data is not reconstructed from old prose.

## Verified historical results and citation mapping

For each explicitly admitted prior reference, preparation:

1. Resolves its original owned run in the same conversation and requires that exact
   persisted result provenance. Missing or substituted results are unavailable.
2. Rechecks live dataset ownership/version and owner-filtered artifact identity,
   dataset binding, media type, digest and actual artifact bytes through DatasetStore.
3. Requires the statistics-result-v1 envelope with exact fields and matching
   original tool-call ID/dataset hash. The result is validated against one of the
   four existing typed statistics result models, including the dataset ID.
4. Builds the same deterministic bounded observation as live tools. The shared
   `tools.observations.statistics_summary` preserves all numerical values or emits
   the existing explicit complete-artifact notice, never a silently truncated result.
   Summary ceiling remains 1,024 UTF-8 bytes; unseen artifact values are not sent.
5. Assigns a run-local citation ID using UUID5(current logical run ID, original
   tool-call ID). HistoricalResult retains both the imported ToolResult and its
   complete original ResultProvenance. Explicit prior-result reads do not enable
   new numerical execution against unselected datasets or bypass tool policy.

Only validated imported aliases enter the runner's accepted-result set. The
existing finalizer guard still rejects unknown IDs and raw historical IDs. The
runner validates the prepared context against exact execution identity, versions,
question, dataset metadata and system instructions before importing any result.
Legacy `run(question, datasets)` remains unchanged and imports no historical IDs.

Imported results are verified references, not fresh tool executions. Preparation
emits no tool/model events, and model finalization does not fabricate numerical
request/completion events. Original artifacts keep their original call identity.

## Exact budget and failure semantics

Reservation uses ModelAdapter.input_reservation: actual compact UTF-8 request
bytes plus the existing 256 reserve, including the complete currently offered tool/
control registry, dynamic finalizer citation enum and configured model parameters.
The same schema-construction helper is used by preparation and actual dispatch.
The effective preparation limit is the smaller of the existing per-request input
cap and remaining run input budget. Request-byte/message/continuation limits and
all existing output, retry and step budgets remain unchanged.

Current instructions/question, selected schemas and explicitly requested verified
results are required context. They are never shortened or dropped to make room
for optional history. If they cannot fit, ContextLimitError reports the fixed
message `Required conversation context exceeds the model budget`, before model
dispatch. Stale/foreign/deleted/malformed inputs raise ContextUnavailableError
with `Conversation context is unavailable`; no private exception chain/body is
retained. External cancellation propagates. Neither error is an attack label.

## Handoff and boundaries for 7.4/7.5

The internal StatisticsRun may include imported aliases in its results/answer
citations. Keep PreparedContext.historical when handling that output. Persist new
computations separately from imported citations, resolve aliases back to the
original provenance and never relabel an old artifact as a newly executed call.
Mapping can be derived from the already stored prior_results and logical run ID.

ConversationRun-v1's completed/current-run-result invariant is unchanged. Step
7.4 must explicitly bridge/version the stored/public result representation for
history-only answers, rather than stuffing aliases into original artifact
provenance or weakening v1 guards. Public HTTP result/history mapping is not
delivered here. The worker must prepare immediately for its execution attempt,
handle preparation failures, persist outcomes and retain current authorization.

RequestClarification remains a contract only until 7.4 registers its behavior.
When adding a control, include its schema in this same reservation path; do not
raise the request cap or invent a separate smaller registry for estimation.

## Verification and delivery

Added 25 tests: 22 real PostgreSQL context cases and three lightweight import
boundaries. Focused context/tool/finalizer/import gate: 49 passed before the final
four historical-result schema cases. Final PostgreSQL/non-browser gate:
**946 passed, 17 browser tests deselected**, 337 upstream warnings, 66.11 seconds.
Ruff, formatting, dependency and whitespace checks pass. Existing live-tool
observation tests verify the shared renderer preserves behavior.

Coverage includes follow-ups, ordered/ambiguous multi-dataset metadata, bounded
recent history and injection-like old claims, exact complete-schema byte budgets,
Unicode omission, oversized required question/schema rejection, artifact integrity,
all four result schemas, stale/foreign/deleted references, raw-ID rejection,
cross-run/input/version reuse and cancellation without provider dispatch.

No auth/browser code or schema changed; browser tests were not rerun and required
CI still includes them. No live model/cloud request, dependency or public API
change. Disposable test PostgreSQL is removed after verification; developer data
is untouched. No commit/push. Stop before 7.4. Parent plans, progress and frontend
handoff remain outside the service repository/PR.
