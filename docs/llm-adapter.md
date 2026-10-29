# LLM adapter — step 4.3

Updated: 29 October 2026. Step 4.3 delivery accepted from user pull confirmation.
Subsequent [4.4 Agent setup](statistics-agent.md) adds the controlled typed continuation;
the historical scope/evidence below describes 4.3.

## Implemented boundaries

- `agent.provider.ModelAdapter` accepts explicit model settings, an EventSink,
  a sequence allocator and optionally a test HTTP transport. `complete(request,
  tools=...)` performs exactly one non-streaming Chat Completions request.
- `agent.crewai_llm.StatisticsLLM` subclasses the actual CrewAI BaseLLM. Supply
  the adapter, a server-authorized AgentContext and an aware execution deadline.
  Use `acall` in asynchronous execution; synchronous `call` is for a worker
  without a running event loop. No global mutable agent or hidden provider client.
- Groq and Azure use their resolved public v1 endpoints and explicit model or
  deployment names. The adapter unwraps SERVICE_MODEL_API_KEY only for the
  Authorization header. No provider SDK retries, redirects, environment proxies
  or alternate endpoints are used. HTTPX closes the request client afterward.
- The profile reference must match the pinned snapshot. Output allowance is
  capped by that profile. Request JSON is capped at 65,536 bytes, response content
  at 1,048,576 bytes; overall network time is capped by the earlier request
  deadline/profile timeout. These byte limits are not a tokenizer or run budget.

No FastAPI endpoints/startup integration, CrewAI statistics Agent, numerical tool,
retry loop, token-budget enforcement, persistent event store or Kafka producer is
introduced here. `ModelProfile.concurrency` and retry settings remain inputs to
later execution orchestration, not promises of enforcement in this module.

## Proposal and attempt evidence

For each bounded provider tool proposal, assign an internal UUID and append
`tool_call.requested` before validating its shape/name/arguments. All bounded
proposals are observed before validating the first one. Retain original provider
call ID, name and raw argument string in the restricted ProviderResponse. Unknown
names and malformed JSON argument strings are not repaired or filtered.

Public events deliberately contain proposal hashes/byte counts, not raw names,
arguments, prompts, model text or provider errors. Returned model/provider request
identities stay in the internal response; public evidence includes their hashes.
Missing usage stays null; reported zero remains zero. Cached input/reasoning
counts remain subsets, never extra tokens added to the totals. Unknown pricing
stays `estimated_cost_usd: null` with `pricing_version: unpriced`.

Each completed attempt emits `model_call.completed` or `model_call.failed`, with
run/session/request/model-call IDs, attempt index, timestamps, receive latency,
HTTP status, usage/finish or a fixed failure category. Numeric Retry-After is
accepted only when finite and within 0–3,600 seconds. No arbitrary provider error
body is put in exceptions or telemetry. HTTP 429 is rate_limit; quota-specific
provider-body classification is not implemented. Timeout/network failures retain
unknown-outcome evidence. Cancellation propagates to the caller; interrupted
attempt reconciliation belongs to plan 7.

The caller supplies a session-wide sequence allocator. The adapter does not
create a competing global sequence, retry the sink, or claim durability. A sink
failure raises a sanitized EvidenceDeliveryError and returns no dispatchable
response. Raw full proposal evidence is currently in-memory only. Digest-only
telemetry cannot reconstruct classifier features: plan 7 must agree a redacted
payload/restricted artifact store before claiming complete durable evidence.
Oversized/unrepresentable provider responses fail explicitly, never execute tools.

## CrewAI integration limits

CrewAI 1.15.33 BaseLLM wraps subclass calls with its own rate-limit retries.
StatisticsLLM overrides that pinned wrapping seam; tests verify both call methods
are unwrapped. Each current framework call gets a new logical model-call ID,
attempt index 1. Observed retry orchestration is step 4.5.

The bridge never invokes CrewAI's `available_functions`. A provider proposal
raises the fixed-message ToolDispatchRequired carrying the restricted response.
Plan 4.4 must bind this continuation to the authorized policy/tool dispatcher;
plan 7 must persist the request before execution. This is intentionally not an
end-to-end functioning Crew yet. Rich native framework tool-result messages,
callbacks and structured response models are not silently supported: use typed
ProviderRequest messages for correlated replay, and reject unsupported modes.
Tool identities are not inferred from names.

Before importing CrewAI, the bridge disables implicit dotenv loading and
OpenTelemetry SDK export for the process. Explicit Pydantic configuration loading
still works. The same opt-outs are in CI/example environment. No third-party
framework tracing of prompts or arguments is enabled. Revisit tracing deliberately
in plan 10 rather than enabling automatic framework export.

## Dependencies and verification

Pinned CrewAI **1.15.33** requires Pydantic below 2.13, so the service pin changed
from 2.14.1 to **2.12.5**. Existing FastAPI/settings dependencies accept it, and
`pip check` passes. CrewAI brings transitive CLI, model-client and storage
packages, including uv and vector libraries; none is used as our environment
manager or a deployed database. Keep Python 3.12, `.venv` and requirements.txt.
No framework memory store/vector database was created.

[Custom BaseLLM documentation](https://docs.crewai.com/v1.15.33/en/learn/custom-llm)
and the installed version's BaseLLM/retry source were checked. Tests exercise the
real BaseLLM subclass with HTTPX MockTransport and synthetic keys only. No live
Groq/Azure inference request was performed.

**302 non-infrastructure tests passed; 101 PostgreSQL/browser tests deselected.**
Ruff, formatting, dependency consistency and Git whitespace checks passed.
Docker Desktop's Linux engine was unavailable, so the full integration gate is
not claimed locally. CI must install the updated requirements and pass its full
required `tests` job, especially existing auth/database checks after the Pydantic
change. Step 4.6 still owns real provider compatibility checks.

No model key needs changing now. Stop before step 4.4.
