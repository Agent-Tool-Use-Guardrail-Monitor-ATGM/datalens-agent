# Clarification and partial-result integration — step 7.4

Step 7.5 adds [leased execution and recovery](durable-worker.md). Claimed terminal
writes now require the matching live claim, execution/request IDs and original
browser binding. Migration 0007 supersedes the schema head described below;
standalone 7.4 behavior remains historical evidence.

Branch: `feature/clarification-partial-results`. This is internal runner/storage
integration. No new HTTP endpoints, worker process or Kafka publisher are enabled.

## Execution controls

The four numerical functions remain unchanged. The model also receives
`finalize_statistics` and `request_clarification` on every attempt, including the
exact same registry used for input reservation. Clarification is a control
function, never a numerical calculation or policy-violation label.

`request_clarification` accepts a nonblank question of at most 512 characters and
up to eight options with unique keys and bounded text. Access is checked again
before accepting it. Invalid arguments produce a fixed incomplete response;
required evidence delivery must succeed before a control can finish. A control
cannot share a batch with another control or a numerical call. Nothing in a mixed
batch executes.

A valid clarification returns `ClarificationAnswer(status="needs_input")` and ends
the single-use session immediately. It does not hold a transaction, poll for a
reply or preserve an active worker waiting for the user. After the terminal write,
the user replies through a new submission/run in the same conversation. Known
unavailable operations, including joins, remain unsupported. Prompt
`statistics-v4` distinguishes these cases; the controlled registry cannot execute
arbitrary code, joins or unregistered tools. Prompt instructions do not guarantee
that generated prose is mathematically true.

## Versioned answers and provenance

`StatisticsAnswer` remains the unchanged v1 numerical final-answer contract; it
still rejects `needs_input` and completed answers without computed-result IDs.
`StatisticsRun.answer` additionally accepts the separate `ClarificationAnswer`.

Stored legacy/active runs still read as `conversation-run-v1`. New terminal writes
read as `conversation-run-v2`, with a `completion` containing the typed answer and
`imported_results`. Each imported item has a current-run `result_id` alias and
the complete original `ResultProvenance`. Aliases are deterministically derived
from the current run ID and original tool-call ID.

`results` contains only computations belonging to the current run. Original
artifacts keep their original tool-call IDs. A history-only completed answer has
empty current `results`, explicit verified imported mappings and answer IDs
matching those mappings. The v1 current-run-result rule is unchanged. New output
must not be coerced into v1 or presented as fresh computation. To reuse an imported
result again, submit its original `source`, not the intermediate alias.

Full source mappings stay in server storage. Model context receives the alias,
dataset ID and verified summary; redundant original run/call IDs are omitted from
model-facing JSON. Raw old conversation prose never becomes numerical proof.

## Terminal transaction and partial results

`persistence.completions.finish_run(database, execution)` is an internal server
operation for trusted runner output, never client-supplied JSON or an alternative
authentication endpoint. The caller must have current application access.

The writer locks the owned conversation/run and checks execution versions,
accepted prior references, selected dataset snapshots, unique call identities,
live dataset ownership and artifact identity/digest/byte length. Shared typed
payload validation checks the original call, dataset version and all four result
schemas. Only completed results with valid stored artifacts become references.
Missing, changed, foreign or malformed evidence fails with a fixed resource error;
no terminal history is written. An already finished run cannot be overwritten.

One short transaction records the terminal outcome, answer, new result provenance,
imported mappings and assistant message, advances the conversation sequence and
removes the pending job. The finished run releases the existing active-run guard.
Concurrent terminal writers cannot append duplicate messages. A failed write
rolls back all terminal changes; it does not erase numerical artifacts.

Real numerical tools already commit their immutable artifacts before returning
an observation to the model. If explanation/finalization fails afterwards, the
runner returns an explicit incomplete answer while preserving those verified
tool outputs. The terminal writer retains their references alongside the fixed
incomplete explanation. Never render these as completed. Reads/downloads exposed
later must still apply current authorization and handle deleted datasets.

## Migration and rollback

Migration `0006_run_completion`, following `0005_conversation_history`, adds one
nullable JSONB column and bounded completion/result constraints. Existing rows,
datasets, auth sessions and v1 provenance are preserved. The completion column is
bounded to 256 KiB, covering the existing bounded answer/assumption/citation values.
No new tables, credentials or numerical values are copied into the completion.

After merge, run the existing deployment migration gate before application
readiness:

```powershell
python -m statistics_agent.persistence.migrate upgrade
python -m statistics_agent.persistence.migrate check
```

For production application rollback, retain the additive schema. Disposable-test
downgrade deletes v2 terminal runs and their cascading messages/jobs before
removing the column; it must never be used to roll back production data.

## Explicit request-budget revision

Adding the sixth function exceeded the old 4,096-byte-plus-reserve allowance on
ordinary calculations and historical follow-ups. A synthetic calculation required
4,716 reserved bytes even after redundant schema/context metadata was removed.
New model profiles are explicitly `model-profile-v4`, with a 6,144 per-request
reservation cap. This supersedes the 7.3 handoff's unchanged-cap expectation.
It is not a tokenizer estimate: the adapter still reserves actual serialized UTF-8
request bytes plus 256, including every offered function.

Total run input remains 32,768; output, attempt, concurrency, wire-size and deadline
limits are unchanged. Stored v3 profiles retain their explicit 4,096 cap and remain
readable, with distinct configuration identities. Do not silently reconfigure old
queued work. Step 7.5 must execute compatible pinned versions or terminate safely.

Schema compaction now resolves nested acyclic definitions and omits duplicated
model documentation. Required fields, types, enums, UUID formats and extra-field
rejection remain in guidance; complete Pydantic validation remains authoritative.
Recursive definitions remain references with their definitions intact.

## Next boundary

Step 7.5 supplies worker claims, attempt/lease fencing, recovery and automatic
terminalization of preparation/storage failures. This writer must be fenced by
the current execution attempt before multiple runtime workers are introduced.
No model call or user wait belongs inside a database transaction. Step 7.6 supplies
durable telemetry/outbox behavior; step 7.7 exposes authenticated HTTP contracts.
Frontend work remains mock-only for conversation/run endpoints until then.

Verification: full gate 995 passed, including all 17 browser tests, in 386.63
seconds. Final focused gate 74 passed after the last lock/validation adjustments;
final current-code non-browser gate 981 passed, 17 browser tests deselected, in
71.63 seconds. Added 35 cases overall. Ruff/format/pip/whitespace checks pass.
No live LLM/cloud call or dependency change. Required remote PR tests remain
pending; no commit/push. Parent planning files are outside this repository/PR.
