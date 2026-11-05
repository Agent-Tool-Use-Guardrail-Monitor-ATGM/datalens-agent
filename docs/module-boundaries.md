# Module boundary refactor

Verified: 5 November 2026. Branch: `refactor/module-boundaries`. Implementation is
complete locally; required PR `tests` and delivery remain pending.

## Scope and ownership

This is a behavior-preserving maintenance change after the four-tool integration,
not another feature-plan step. No authentication, SQL ownership, migrations,
numerical formulas, prompt version, model limits, event payloads or HTTP contract
were changed. No dependency, plugin framework or extra service was introduced.

Shared definitions now have explicit homes:

| Module | Responsibility |
|---|---|
| `agent/tool_schema.py` | ToolBinding and provider-facing schema compaction, without importing the runner or CrewAI |
| `agent/prompts.py` | Existing versioned prompt, role text and finalizer name |
| `agent/errors.py` | Shared adapter/evidence exception types without provider construction |
| `contracts/agent.py` | Existing agent contracts plus DatasetSummary, StatisticsAnswer and StatisticsRun |
| `contracts/sessions.py` | Immutable credential/login/session records shared by OIDC and persistence |

Numerical tools import the lightweight binding definition, rather than the full
runner. OIDC imports shared credential records rather than PostgreSQL storage.
Old imports from `agent.statistics`, `agent.provider` and `persistence.auth`
continue to resolve to the same shared objects; internal callers use their new
locations. There are no duplicate model or exception implementations.

`StatisticsSession.run` remains the supported controlled execution interface.
Its existing tool-execution and finalization bodies are separate private methods;
authorization, policy decisions, result references, event order and accounting
remain intact. CrewAI `call/acall` remain low-level compatibility methods, not
independently authorized run entry points. They were not removed or promoted to
public routes. Their deeper audit coverage remains separate work.

## Shared test support

`tests/support/` owns reusable agent, authentication, encryption, database and
dataset fixtures. Test files no longer import helpers from another `test_*.py`.
`tests/conftest.py` registers distinct `auth_storage_settings` and
`migration_settings` fixtures, preserving the prior settings and pool sizes.
Tests with their own specialized local settings retain them.

Agent fixtures inject custom responses through the existing ModelAdapter
transport constructor. Retry and finalization tests no longer replace a private
transport after session construction. Deliberate private state/fault injection
for deadlines, execution failures and shared admission remains where it tests
that specific invariant; no production configuration options were added for it.

## Verification

Before structural edits, the offline baseline passed **554 tests** and the
database/non-browser baseline passed **709 tests**. Browser binaries were missing
initially; matching Chromium binaries were installed using the existing pinned
Playwright package without changing requirements. The browser baseline then
passed 16 tests; its existing expired-token/identity-outage login test failed
once and passed when isolated. No assertion or timeout was weakened to hide it.

After the refactor, the non-browser gate passed **714 tests**. The final complete
gate passed **731 tests, no skips**, including all **17 browser tests**, in
363.89 seconds, with 214 upstream warnings. This used actual disposable
PostgreSQL, Keycloak, Mailpit and Chromium with synthetic accounts and mocked
model responses. No live LLM request, real credential or cloud deployment was used.

Five new checks cover framework-free/environment-neutral imports and legacy
import identity. A separate read-only AST comparison verified 25 moved
definitions or unchanged implementations, including AuthStore, OidcProvider,
ModelAdapter, StatisticsTools and the runner's authorization/accounting methods.
Ruff checks and formatting for `src`/`tests`, dependency consistency and whitespace
checks for the refactor pass. A concurrent user edit to `.gitignore` contains an
extra blank line; that unrelated edit and user-added skills were left untouched.

Only the explicitly named disposable test projects were started and removed.
Persistent development databases/volumes were not used. Required GitHub `tests`
must still pass on the PR; local Windows verification does not replace Linux CI.

No commit or push was performed. Parent plans, canonical progress and workspace
README are outside this repository and are not published by its PR. Production
policy step 6.1 and the planned security follow-up 3.7 remain unimplemented.
