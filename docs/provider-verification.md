# Provider verification and integration handoff — step 4.6

Updated: 29 October 2026. Branch: `test/provider-verification`.
Status: **In progress**. Verification tooling and offline/auth handoff checks are
locally verified; full live Groq compatibility remains unverified after provider/
connectivity failures. Azure is not configured. This does not complete plan 4.

## Repeatable provider task

The opt-in command executes `statistics_agent.verification.provider_check`.
It exposes no production endpoint or dataset tool. The fixture has constant
values `(2.0, 4.0, 6.0)`; its restricted verification_mean tool actually computes
`statistics.mean`, expected **4.0**, rather than returning a hardcoded fake mean.
The model receives synthetic dataset metadata and the computed summary, not
real uploaded data, user credentials or authentication/session identifiers.

The task uses the configured profile and the actual StatisticsSession/CrewAI LLM
boundary. The probe caps model attempts at three, disables automatic retries and
uses the earlier run cap or a 90-second caller deadline. It must select the tool,
receive its result, return valid answer JSON and cite one computed result ID to
pass. A model explanation is not treated as authoritative numerical evidence.

Run explicitly from the service folder:

```powershell
.\.venv\Scripts\python.exe -m statistics_agent.verification.provider_check --live --env-file .env
```

The `--live` flag is mandatory before loading the key. Default CI never executes
this command; tests use MockTransport and synthetic credentials. It prints a
curated JSON report and exits nonzero on failure. No raw prompts, arguments,
provider bodies, authorization headers or keys are printed. No credentials are
created, rotated or written by the probe. Model profile selection still happens
in server configuration; the same key cannot authenticate unrelated providers.

The standalone CLI uses an explicitly synthetic principal and fixture-only access
callback. It verifies provider compatibility, **not real-user authentication**.
The reusable check_provider function takes a caller-supplied AuthorizedPrincipal
and authorization callback, allowing the actual session boundary to be tested
separately. Do not promote the CLI's fixture callback into a public API.

## Observed live evidence

All observations below were made on 29 October 2026 with the configured Groq
profile and synthetic data only. These are point-in-time observations, not a
claim of stable service availability.

| Check | Result |
|---|---|
| Requested model | `openai/gpt-oss-20b` |
| Returned model identifier | `openai/gpt-oss-20b` |
| Immutable model snapshot/version | Not reported; cannot infer one from the model name |
| Initial inference and tool proposal | Observed successfully |
| Restricted tool execution | Observed mean 4.0 for three fixed rows |
| Full tool replay and final answer | Failed; not verified |
| Subsequent provider connectivity | HTTPX ConnectError, including the models endpoint |
| Azure live check | Not run: resource/deployment not configured |

First probe at 13:15 UTC: two attempts, one successful model response followed by
normalized provider_error; the fixture computed 4.0 and four correlated events
were recorded with contiguous sequence. Provider-reported usage for the first
response: **516 input, 295 output, including 242 reasoning tokens**. Reasoning is
a subset of output, not another 242 tokens to add. The raw provider error body
was deliberately not retained, so its cause/status must not be invented.

A subsequent bounded probe observed **515 input, 234 output, including 182
reasoning tokens**, computed 4.0, then ended with model_network. Another attempt
failed before inference. A final small synthetic replay request and a model-list
connectivity check both ended with ConnectError. We stopped live requests rather
than treating repeated connectivity failures as proof of a payload bug.

A separate diagnostic transport also failed; it is not production code and its
failure alone says nothing about provider compatibility. The normal client
produced the successful initial responses above and later connectivity failures.
The key was never printed or changed. No successful final structured live answer
is claimed, and no permanent provider/API defect is diagnosed from these results.

Rerun once connectivity is restored. Record full success/failure, actual reported
model, usage and package versions. Investigate a reproducible follow-up HTTP
error with sanitized diagnostics if it remains. Do not mark Groq supported merely
because the first model request worked. Azure remains optional/unverified until
its own credentials/deployment/quota and the same check are available.

## Authorized-user integration evidence

A new test-only protected POST route uses the existing require_user dependency,
real encrypted PostgreSQL session state, RSA-signed JWT validation, Origin and
CSRF checks. Its authorization callback reuses that real boundary before each
model/tool operation. It verifies missing session denial, missing CSRF denial,
and the successful synthetic tool/result flow with mocked model responses.

The identity-provider responses in this test are the existing controlled RSA/OIDC
fixture. This is not a new live Keycloak browser login or a live-provider request
using a real human account. Existing plan 3 browser evidence remains separate.
No test route or fixture authorization bypass is registered in production.

## Handoff acceptance cases for plans 7 and 10

These cases are specified here, not implemented as durable infrastructure:

| Owning plan | Trigger | Required behavior/evidence |
|---|---|---|
| 7 | Cancel while queued/backing off | Admit no new attempt; preserve already-recorded attempts and cancellation state |
| 7 | Cancel during inference | Release local admission; retain unknown provider outcome and reconcile the interrupted attempt |
| 7 | Worker interruption after provider response | Resume/reconcile persisted evidence without inventing a new logical history |
| 7 | Interruption between durable tool request and execution | Persist stable request/call IDs and define replay/idempotency before retrying side effects |
| 7 | Interruption after execution, before outcome commit | Treat outcome as unknown; do not assume rollback or automatically replay the operation |
| 7 | Kafka outage | Persist to transactional outbox before acknowledging tool-request evidence; fail closed if local persistence fails |
| 7 | Broker redelivery/restart | Reuse persisted event IDs; consumers deduplicate at-least-once deliveries |
| 7 | Concurrent publishers/retries | Allocate/persist one session sequence and preserve model-call ID plus attempt index; do not rebuild fresh IDs on delivery |
| 7 | Session invalidation/expiry before resumed work | Recheck authorization at execution boundaries; job payloads hold references, never OAuth/provider keys |
| 10 | Multiple worker processes/Container Apps replicas | Bound aggregate provider load or provide distributed admission; process semaphore is not an organization quota limiter |
| 10 | Cold start/secrets rotation/deployment | Preserve storage/keys, pin image/config snapshots, run migrations/readiness and use explicit provider revalidation |
| 10 | Model/deployment retargeting | Record actual returned model/version when available; old name alone is not proof of immutable weights |

No Kafka-outage, durable restart, cloud deployment or deduplication runtime proof
is claimed. Digest-only tool events cannot reconstruct raw classifier features;
plan 7 still owns the agreed redacted payload/restricted artifact evidence.

## Environment and regression evidence

The existing .venv had a missing Python launcher and incomplete pip/package
files. Restored launcher files from the existing Python 3.12 archive and
reinstalled the unchanged pinned requirements in the same .venv. No Conda/uv
workflow was introduced and no real .env values were modified. Direct checked
versions remain CrewAI 1.15.33, Pydantic 2.12.5, HTTPX 0.28.1 and FastAPI 0.158.0;
requirements.txt remains unchanged. Provider reports include actual direct versions.

Docker Desktop was started for verification. Only the fixed disposable
`datalens-agent-tests` PostgreSQL fixture was used; developer databases and volumes
were not touched. The test fixture was removed afterward; Docker Desktop remains
running. No persistent identity or Keycloak configuration changed.

**427 tests passed, 17 browser tests deselected**, 133 upstream CrewAI deprecation
warnings. This includes six offline verifier checks (Groq/Azure contract doubles,
denials, safe failures and explicit opt-in) and one real PostgreSQL authorization
handoff check. Ruff, formatting, pip consistency and Git whitespace checks pass.
Required full CI remains the delivery gate. Do not advance to plan 5 or mark plan
4 complete from this document; live verification acceptance remains open.


## Pre-merge recheck — 29 October 2026

Verification-only request; no product code fixes were authorized or applied.
At 13:28:05 UTC, the normal opt-in Groq probe again selected the tool and computed
4.0, then failed. This time the safe report recorded HTTP **200, 400**. The first
response reported 513 input and 159 output tokens, including 109 reasoning tokens.
A separate bounded sanitized diagnostic reproduced HTTP 400 with provider code
**tool_use_failed** on the follow-up. It reported validation-related markers,
without publishing the provider message, generated content or credentials.

This is stronger evidence than the earlier connectivity failures: restored
connectivity alone did not make the full live flow pass. The precise prompt/model/
tool-continuation correction still requires diagnosis and a fix, followed by the same live check. Do not merge as a completed provider
verification or mark plan 4 complete on the strength of offline tests.


Full pre-merge local gate completed on the same working tree: **444 passed, no
skips, 133 upstream CrewAI deprecation warnings**, in 342.37 seconds. This includes
real PostgreSQL, Keycloak/Mailpit and Chromium browser expiry/outage cases.
Ruff, pip consistency, whitespace and .env Git-ignore checks passed.
Only disposable test projects were used and removed afterward; Docker Desktop
remains running. No product code was changed during this validation pass.

**Merge recommendation: hold.** The offline/auth/browser suite passes, but the
separate live Groq continuation fails reproducibly with HTTP 400 tool_use_failed.
Required GitHub CI still applies to the pushed PR revision; local success is
not a remote CI result. Step 4.6/live sign-off remains incomplete.


## Continuation fix — 29 October 2026 (supersedes earlier live blocker)

The rejected generation contained answer fields inside function arguments. The
runner now advertises a distinct finalize_statistics answer channel, keeps
calculation schemas separate, validates successful computation references and
rejects mixed finalization/execution. Access is rechecked before finalization;
unknown functions remain rejected. Prompt statistics-v2 and model-profile-v3
record this protocol and Groq temperature 0.6/reasoning_effort low. No hidden
retry, provider switch, disabled tool validation or budget increase was added.
Azure omits those generation parameters and remains unconfigured/unverified.

Latest three consecutive explicit live checks at 13:56:13, 13:56:17 and 13:57:24
UTC passed with Groq openai/gpt-oss-20b. Each made two HTTP 200 requests, computed
mean 4.0, validated a successful result reference and emitted six contiguous
events. Input/output token pairs were 488/68 then 620/67; 487/66 then 618/78; and
487/65 then 624/101. No immutable model snapshot version was reported. Earlier
probes included rejected unknown function names; three successes do not guarantee
all future generations. Invalid outputs continue to fail closed. A temperature-
zero experiment exhausted reasoning output and was discarded.

The live CLI uses a synthetic principal and constant dataset. Separately, real
PostgreSQL/JWT/CSRF authorization tests use mocked provider inference. This is
not proof of an authenticated human session calling live Groq, production tools,
durable Kafka delivery or cloud deployment. No credentials were printed/changed.

Full post-fix gate: **449 passed, one existing disabled-account browser assertion
failed**, 151 upstream warnings, 424.75 seconds. That exact browser case passed
on isolated rerun (5.82 seconds); likely render timing, not an established root
cause. Do not describe this as a clean full-suite pass. Ruff, formatting, pip
consistency and whitespace checks pass. No dependencies or migrations changed.

The reproduced live provider blocker is corrected locally. Step 4.6 remains
**In progress pending clean required PR tests/delivery** on test/provider-verification.
Do not start plan 5. This record supersedes the earlier live-failure hold; it does
not bypass the required CI gate.

Focused post-failure rerun: **47 passed**, 36 upstream warnings, 59.78 seconds.
Includes the entire browser-login module plus native finalization, model adapter,
provider verification and real PostgreSQL/JWT/CSRF handoff tests. No auth code
or browser test was changed to obtain this result. Both disposable Docker test
projects were removed after verification; Docker Desktop remains running.
