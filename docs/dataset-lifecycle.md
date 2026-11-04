# Internal dataset lifecycle — step 5.4

Updated: 4 November 2026. **Step 5.4 delivered per user pull confirmation.** Historical evidence below used feature/dataset-lifecycle. The previous CSV-validation branch is
included in locally cached origin/main; no fresh remote CI audit was performed.
This is internal application code, not an upload API or an LLM-callable tool.

## Application service

`statistics_agent.datasets.service.DatasetService` composes the existing parser
and DatasetStore with one consistent server-owned DatasetLimits configuration.
Constructor dependencies are Database and an explicitly shared DatasetParser.

| Method | Responsibility |
|---|---|
| ingest(principal, filename, content) | Offload full validation; store original bytes and actual parsed counts/columns only after success |
| get_metadata(principal, dataset_id) | Owner-filtered metadata without raw CSV loading |
| list_metadata(principal, limit, after) | Owned keyset page; default 50, maximum 100 |
| load(principal, dataset_id) | Read owned metadata and integrity-checked bytes, offload parsing, recheck ownership/existence and metadata consistency |
| preview(principal, dataset_id) | Build the bounded string/null UI preview from an authorized loaded dataset |
| delete(principal, dataset_id) | Owner-filtered deletion with existing raw-content/artifact cascades |

Ingestion accepts no client-supplied row count, schema, owner or ID. Rejected CSV
never reaches storage. Successful validation supplies exact original bytes and
metadata to the existing atomic dataset/content transaction; write failure still
rolls back metadata. A filename/metadata validation error also creates no rows.
No filesystem temporary files, object storage adapter or new migration is added.

Callers must supply a currently authorized principal from the existing session/
JWT boundary. The service is not a second authentication service and performs no
Keycloak lookup or OAuth refresh. SQL predicates still enforce exact issuer and
subject ownership, including admins; unknown and unowned reads share the existing
fixed not-found error and deletes return false. API/worker composition in plans
6/7 must recheck current session/access at the appropriate boundaries. Retaining
an old principal or loaded object is not authorization to reuse it indefinitely.

## Loading, previews and immutable data

LoadedDataset contains private DatasetMetadata and ParsedDataset values, hidden
from its normal repr. Raw bytes are verified by DatasetStore's digest/length
checks. Reparse them and compare actual row/column facts to stored metadata;
mismatch raises a fixed DatasetIntegrityError without echoing data. Both initial
and final metadata reads are owner-filtered. If deletion occurs during parsing,
the final read fails rather than returning a successful loaded object.

No transaction/row lock stays open during parsing. A loaded value is a read
snapshot at the final successful ownership/existence check, not a lock against
future deletion or a cross-user cache. Recheck authorization before later uses.
The service does not repair corrupt metadata or silently change immutable data.

Preview uses the same authorized loading path, with at most the configured
preview limit (default 20 rows). It can therefore reparse the complete bounded
file; there is no result cache or indexed preview storage in this step. Lowered
limits also apply to loading existing files. Do not send loaded rows/previews to
the model automatically. Model context remains bounded metadata/observations.

Dataset deletion cascades stored raw content and artifacts. There is no durable
conversation/run history yet; plan 7 must finalize its retention/deletion policy
before exposing combined history and deletion APIs. No public URL/path or new
business OpenAPI schema is implemented by these internal method signatures.

## Bounded parsing execution

`statistics_agent.datasets.execution.DatasetParser` owns a ThreadPoolExecutor.
Default two workers; server-owned capacity must be an integer from one to four.
The application must create **one shared instance per process/lifespan**, then
inject it into services; creating a pool per request bypasses aggregate limits.
Current FastAPI routes do not instantiate or expose these services yet.

Admission is nonblocking: a full pool raises fixed DatasetParserBusyError instead
of accumulating waiting requests. Input type/size is checked before submission.
Actual parsing runs off the event loop with propagated contextvars. A thread-safe
slot remains attached to the concurrent future until work really finishes.

Shield the admitted future from caller cancellation: cancellation does not kill
the thread or release its slot early. It also cannot accumulate cancelled entries
in the executor queue through repeated submit/cancel cycles. Observe background
exceptions without logging source data. Once a caller is cancelled while awaiting
parsing, its ingest coroutine does not continue into storage, even if parsing
finishes successfully. Tests cover this phase; cancellation during a later DB
commit can have an unknown outcome and is not an undo guarantee.

Use async context management or await aclose() to reject new parsing work and
wait off-loop for admitted threads to finish. Closed parsing raises fixed
DatasetParserClosedError. Stop accepting/drain or cancel service operations
before closing dependencies; pool shutdown drains parser work, not every later
database operation. Close the pool before disposing the database. A repeated
close is safe. No forced thread termination, distributed quota or hard process
memory/CPU isolation is claimed; those controls belong to plans 6/7/10.

The bound is per shared pool, not a registry that prevents another instance from
being constructed. Plan 7 must compose the shared dependency correctly. Future
HTTP request reading/admission also needs explicit bounds; this method receives
already materialized bytes. No env variable or HTTP busy-status mapping is added
here. Step 5.5 now extends this pool with server-owned compute(), sharing the same admission/cancellation guards; see the descriptive tool handoff.

## Verification and delivery

Added **22 focused tests**, all passed. They cover true thread offload, busy
admission, cancellation retaining a slot, background error observation, shutdown/
recovery, real PostgreSQL ingestion/preview/loading, rejected-file zero writes,
write rollback, lowered limits, user/admin/issuer isolation before parsing,
pagination, dependent deletion, metadata mismatch and deletion during loading.

Full non-browser regression with real PostgreSQL: **604 passed, 17 browser tests
deselected**, 151 upstream warnings, 32.14 seconds. Ruff format/check and pip
consistency pass. No live provider, identity database, developer data, dependency
or schema change. Only the fixed disposable datalens-agent-tests project was used
and removed after verification; Docker Desktop remains running.

Service README and parent plans/canonical note/frontend tracker distinguish this
internal capability from pending plan 7 HTTP integration. Parent planning files
are outside service Git/PRs. Required GitHub tests remain the delivery gate; no
commit/push/PR was performed. Stop before 5.5 real inspection/descriptive tools.

Step 5.5 update: [real descriptive tools](descriptive-tools.md) now use compute()
on the same shared pool as parsing. This service still owns loading/ownership;
production policy and full runner/API composition remain later steps.
