# Statistics Agent, prompts and run isolation — step 4.4

Updated: 29 October 2026. Step 4.4 delivery accepted from user pull confirmation.
Subsequent [4.5 execution limits](execution-limits.md) replace the placeholder
budget/deadline behavior described in this historical step record. This is an internal execution component, not an HTTP run API.

## Execution architecture

`agent.statistics.StatisticsSession` creates one actual pinned CrewAI Agent and
StatisticsLLM per session. Its role, goal and backstory are fixed. Delegation,
code execution, memory, caching, verbose output, context compaction and framework
retries are disabled. A single-use controlled runner calls the bound BaseLLM's
typed continuation and owns tool dispatch. It does not call CrewAI's native
`Agent.execute_task`/Crew/Task executor or its available_functions mechanism.
This choice keeps the observed provider boundary and policy checks on every tool
path; it does not claim an unrestricted native CrewAI executor is safe.

Construction requires explicit server-owned dependencies:
- Verified AgentContext from the existing session/JWT boundary.
- ModelAdapter, versioned toolset/policy references and a tuple of ToolBinding
  definitions (name, description, Pydantic argument model forbidding extra fields).
- ToolPolicy and ToolExecutor implementations plus an async authorization check.
- EventSink, the same session-wide sequence allocator used by the ModelAdapter,
  and an aware deadline. All consumers must share the sink/sequence discipline.

Only controlled tool/policy implementations exist in tests. Plans 5 and 6 must
supply real numerical tools, dataset ownership and production policy evaluation.
The authorization callback must check the real session; building an AgentContext
alone does not authenticate. No allow-all production policy or HTTP entry point
was added. Do not expose this runner before those integration boundaries exist.

## Inputs and versioned prompt

`run(question, datasets)` accepts bounded dataset metadata (dataset UUID, row
count and column names), not full dataframes or authentication credentials.
DatasetSummary must be supplied after owner checks; it is not itself an ownership
proof. At most 16 datasets and 128 columns each are accepted. Backend tools will
load authorized data using those references in plan 5.

PROMPT is version `statistics-v1`; response schema is `statistics-answer-v1`.
It instructs the model to treat supplied content as untrusted data, use only the
declared tools and never invent measurements. Model messages contain instructions,
question, dataset metadata and bounded tool observations. Issuer/subject, auth
session references, JWTs, cookies and provider key are not rendered. This is a
structural separation, not a claim that arbitrary user text is a secret scanner.

Each run pins provider/prompt/toolset/policy references in AgentState. Conversations,
pending tool identities and artifact/result references are per-session values.
Sessions are single use; create another instance for another run/user. Existing
structural bounds remain: at most 8 model rounds and 128 conversation messages.
Token counters are placeholders until 4.5; no retry/token/concurrency enforcement
is claimed here. The provider still applies its own request timeout/size caps.

## Controlled tool continuation

1. Recheck authorization before each model attempt. Adapter emits proposal
   request evidence before validation, using the unchanged seven event types.
2. Reject incomplete tool requests and missing/duplicate provider call identities
   before executing any tool. Reject unknown names and invalid/extra arguments.
3. Recheck access, evaluate typed policy and verify its call UUID matches the
   proposal. A denial, unavailable policy or invalid decision stops execution.
4. Invoke the injected executor; validate the typed ToolResult and matching UUID.
   Emit a redacted completion/failure event. No dynamic eval, generated Python,
   SQL execution or external MCP client is introduced.
5. Append only a successful bounded summary and artifact reference as a correlated
   tool observation. Replay preserves original provider identities. Failures stop
   the analysis with a fixed incomplete outcome, without another model call to
   invent a numerical answer.

ToolResult now has optional `summary`, hidden from repr. It remains internal and
may contain dataset results; serialization is not a safe public-log format.
Tool events contain IDs/status/fixed reasons only. Sink errors prevent further
execution and propagate as sanitized EvidenceDeliveryError. Already completed
operations cannot be rolled back by a later sink failure. Durability/transactional
outbox and reconciliation of unattempted proposals remain plan 7.

## Results and limitations

StatisticsAnswer has `status` (completed/unsupported/incomplete), explanation,
result_ids and assumptions. A completed answer needs at least one successful
internal tool-call result ID; invented references are rejected. Model JSON is
validated locally rather than relying on provider structured-output support.
A truncated, malformed or unsupported final response becomes explicit incomplete.

References prove which tools ran, not the truth of every sentence in the model's
explanation. Authoritative numbers must come from typed tool results/artifacts;
plan 5 owns numerical correctness and later result presentation must preserve it.
No persistent multi-turn chat API, upload, result download or run-history API is
implemented. Model failures and access denials produce no invented statistics.

## Verification and next handoffs

Thirteen controlled tests cover real CrewAI configuration, two-call tool/result
replay, malformed/unknown/extra arguments, policy/access denial and access loss,
missing tool IDs, truncated output, provider/tool errors, invented result IDs,
unsupported operations, artifact references, single-use behavior and isolation.
HTTPX MockTransport supplies responses; the executor is a deterministic double,
not a real pandas calculation. No real Groq/Azure request was made.

Regression: **315 passed; 101 PostgreSQL/browser tests deselected**. Final targeted
run after artifact-state handoff: **13 passed**. Ruff, formatting, pip consistency
and whitespace checks pass. CrewAI emits 43 upstream deprecation warnings around
legacy fields during Agent construction; they are not suppressed. No dependencies
or database migrations changed in this step. Required full CI remains the delivery
gate; no repeat remote audit was performed after the user confirmed 4.3 pulled.

4.5 implements observed retries and token/step/deadline/concurrency behavior;
4.6 verifies live model compatibility. Plans 5–7 supply actual tools/policies,
owned datasets, durable runs/outbox and HTTP APIs. Stop before 4.5.
