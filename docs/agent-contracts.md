# Agent interfaces — step 4.1

Implemented in `statistics_agent.contracts.agent`. These are internal, immutable
Pydantic value contracts and async Python protocols, not new HTTP endpoints.
Importing them creates no database, provider or framework connection.

| Boundary | Contract |
|---|---|
| Authorized execution | `AgentContext` reuses plan 3's `AuthorizedPrincipal`, including the non-secret auth-session reference, plus request/run/session IDs |
| Pinned configuration | `AgentVersions` references provider, prompt, toolset and policy versions; `PromptDefinition` versions instructions and response schema |
| Model attempt | `ProviderRequest` contains correlation IDs, a profile reference, bounded messages, output limit and deadline; `ProviderResponse` preserves attempt identity and original proposals |
| Usage/failure | `TokenUsage` keeps unknown counts null and cached/reasoning counts as subsets; `ProviderFailure` accepts fixed categories, not raw provider exception bodies |
| Run state | `AgentState` contains immutable bounded messages, pending proposals, artifact references, counters, remaining budgets and deadline |
| Later execution | `Provider`, `ToolPolicy`, `ToolExecutor` and `EventSink` define async integration seams; controlled implementations exist only in tests |

Tool proposals preserve their internal UUID, optional original provider ID,
unrecognized name and raw arguments (including malformed JSON). Capture them
before lookup, argument validation or framework filtering. A tool result retains
the same internal tool-call UUID. Message/tool argument contents are hidden from
Python representations, but **serialization still contains them**: these values
are restricted internal data, not safe logs or Kafka payloads.

Each text field is limited to 16,384 characters; messages to 128 per request or
state, proposals to 32 per response/message, and state artifacts to 128. These
are structural bounds, not token budgeting or the existing event's 16,384-byte
JSON payload limit. The adapter must reject unsupported oversized output or
retain full evidence in a restricted artifact; it must not silently truncate it.

Authorization context rejects extra fields such as cookies, OAuth tokens, keys
and passwords, including inside the principal. Construction does not authenticate
or scan arbitrary strings for secrets. Only the existing session/JWT checks may
supply this context; later execution must recheck access at its defined boundaries.
Provider requests intentionally have no identity/session-token fields. Callers
must not insert credentials into otherwise valid text fields.

## Remaining handoffs

- **4.2:** resolve server-owned profiles/secrets and capabilities from references.
- **4.3:** normalize provider responses, verify echoed attempt IDs, preserve original
  proposals, redact evidence and map to the unchanged seven event types. These
  contracts do not perform response/request correlation checks across calls.
- **4.4–4.5:** bind CrewAI, render prompts, enforce conversation/tool ordering,
  enforce budgets/deadlines and classify retries. No execution engine exists yet.
- **5–6:** implement typed statistical tools, tool schemas, ownership and policies.
  Decisions must match the proposed call ID before dispatch; protocol types alone
  cannot enforce this. No arbitrary generated Python execution is introduced.
- **7:** implement run APIs, storage/checkpoints, cancellation and transactional
  outbox. `EventSink.append` must acknowledge only after persistence. Record a
  redacted `tool_call.requested` before policy/dispatch; failure prevents execution.
  The in-memory test sink proves only interface compatibility, not durability.

## Verification

Step 4.1 adds 25 controlled contract tests. The non-infrastructure suite passed
**263 tests**, with **101 database/browser tests deselected** and one existing
Starlette/HTTPX deprecation warning. No live LLM calls, CrewAI dependency changes,
database changes or Kafka publishing were performed. Required PR `tests` remains
the full delivery gate; local evidence does not imply remote CI success.
