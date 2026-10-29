# Model configuration — step 4.2

Delivery through 4.2 is accepted from user pull confirmation. For subsequent
local step 4.3 adapter work and dependency changes, see [adapter handoff](llm-adapter.md).

`statistics_agent.core.model_settings.load_model_settings(env_file=".env")` reads
an explicitly selected UTF-8 file, with process environment taking precedence.
Without that argument, it reads environment variables only. It performs no
network requests. Application startup is not wired to this loader yet: the model
adapter/composition work belongs to 4.3. Existing auth/health startup does not
suddenly require a model secret.

## Server configuration

- `LLM_PROFILE`: `groq_free` (default) or `azure_student`.
- `SERVICE_MODEL_API_KEY`: required nonempty single-line secret. It is excluded
  from repr and model serialization; only the later adapter explicitly unwraps it.
- `LLM_TIMEOUT_SECONDS`: 1–120, default 30.
- `LLM_MAX_RETRIES`: 0–3, default 2. This configures a later observed retry loop;
  no SDK retry layer or retry behavior exists yet.
- `LLM_CONCURRENCY`: 1–4, default 1. Later execution must enforce this, including
  an appropriate global quota strategy if Azure runs multiple replicas.
- Azure additionally requires `LLM_AZURE_RESOURCE` and `LLM_AZURE_DEPLOYMENT`.
  Supply resource/deployment names, not URLs. Remove these when selecting Groq.

Groq resolves to `https://api.groq.com/openai/v1` with `openai/gpt-oss-20b`.
Azure resolves to `https://<resource>.openai.azure.com/openai/v1`; the deployment
name is the request model. Azure v1 does not require a dated api-version query.
Only this public Azure endpoint family is configured initially. Other clouds,
custom endpoints, models, providers and Entra authentication need explicit support.

Sources checked 28 October 2026:
[Groq compatibility](https://console.groq.com/docs/openai),
[Azure v1](https://learn.microsoft.com/en-us/azure/foundry/openai/api-version-lifecycle).

## Snapshots and capabilities

`settings.resolve()` returns a non-secret immutable `ModelProfile`. Its reference
uses a deterministic UUID derived from the full public snapshot; configuration
changes produce a new ID while key rotation alone does not. Preserve that snapshot
for a session. A reference alone cannot reconstruct an old snapshot: plan 7 must
persist the actual public values. Azure deployment names can be retargeted remotely;
record actual returned model/version later rather than promising immutable weights.

Initial application caps are 4,096 input tokens and 1,024 output tokens; these are
not advertised context maxima or account quotas. Tool calling and JSON mode are
expected profile capabilities; strict JSON-schema output and parallel tools remain
disabled conservatively. `profile.require(structured_output=True)` rejects the
unsupported requirement. Live behavior/model-specific Azure capability must be
verified before declaring a profile supported in 4.6. JSON mode is not schema
validation; later code must validate responses. Pricing is explicitly `unpriced`;
no zero-cost estimate or balance is invented.

## Secret handling and delivery

Your existing ignored `.env` uses `SERVICE_MODEL_API_KEY`. This step did not read,
print, alter or send its value. Inject the same variable using a Container Apps
secret when cloud deployment is implemented. Restart/reload configuration deliberately
for rotation; no hot reload or automatic cross-provider fallback exists.

Do not serialize the secret or unwrap it into logs, prompts or error messages.
The loader raises a fixed sanitized ConfigurationError outside the underlying
validation handler. Manually constructing settings is an internal operation;
use the loader for safe startup failures. Secret masking does not make arbitrary
logging of environment variables or validation error input safe.

Fourteen new configuration tests use synthetic keys. Non-infrastructure regression:
**277 passed, 101 database/browser tests deselected**, one existing Starlette/HTTPX
warning. Ruff and pip checks pass. Existing Pydantic settings and HTTPX dependencies
are sufficient for this step; no SDK or CrewAI dependency is needed until 4.3.
No migrations, provider calls or runtime endpoints changed. Required PR `tests`
remains the delivery gate. Stop before 4.3.
