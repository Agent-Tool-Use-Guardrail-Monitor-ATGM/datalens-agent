# Owned dataset and artifact persistence — step 5.2

Updated: 4 November 2026. **Step 5.2 delivered per user pull confirmation.** Historical implementation evidence below used feature/dataset-storage. Step 5.1 delivery is accepted from the
user's pull confirmation. No CSV parser, statistical executor or business HTTP
endpoint is added here. Only disposable PostgreSQL was migrated during testing.

## Schema and ownership

Migration **0004_dataset_storage**, following 0003_refresh_claim, adds:

| Table | Contents |
|---|---|
| agent_datasets | Server UUID, exact owner issuer/subject, display filename, SHA-256, byte/row counts, JSONB column metadata, CSV format and aware creation time |
| agent_dataset_contents | One bounded BYTEA value per dataset; separate from metadata reads |
| agent_artifacts | Server UUID, parent dataset reference, application/json media type, digest/byte count, canonical JSON text and creation time |

Artifacts derive ownership from their dataset rather than duplicating owner
columns. Reads join the parent with the exact issuer/subject predicate inside
SQL; artifact writes lock that owned parent before insertion. Role membership,
including admin, never broadens ownership. Unknown and unauthorized resource
reads/attachment attempts raise the same DatasetNotFoundError with fixed text;
unknown/unauthorized deletes both return false. The current session/role checks
must already have supplied AuthorizedPrincipal; constructing one is not proof
of authentication or continuing access.

Deleting a dataset cascades to its raw content and artifacts through foreign
keys. Standalone artifact deletion is also owner-filtered. There is no run
foreign key yet: plan 7 must settle history retention/deletion semantics before
exposing deletion and historical result access together.

## Repository methods

`statistics_agent.persistence.datasets.DatasetStore` reuses Database's bounded
async pool. Each method owns a short session/transaction; it never performs an
identity/provider call or holds a transaction for computation.

| Method | Behavior |
|---|---|
| create | Accept already validated file/column facts; generate UUID/time, compute digest/size from exact bytes and atomically commit metadata plus content |
| get_metadata | Owned metadata only, without loading raw CSV |
| list_metadata | Owned UUID-ordered keyset page; default 50, maximum 100, optional after UUID |
| get_content | Owned raw bytes; verify actual length and SHA-256 against metadata |
| delete | Owned dataset deletion with dependent cleanup; return whether it existed |
| create_artifact | Validate bounded JSON, lock owned parent and commit a server-generated artifact reference |
| get_artifact | Owned StoredArtifact with ArtifactReference, parent dataset ID, creation time and decoded payload; verify digest/length |
| delete_artifact | Owner-filtered artifact deletion with existence parity |

The repository exposes no content-update operation. Duplicate uploads receive
different IDs even if their digests match. UUID pagination is deterministic but
is not creation-time sorting or a snapshot of concurrent insertions. Callers must
pass the next cursor from the last returned item and apply UI pagination only
after the HTTP contract is delivered.

## Bounds, serialization and integrity

Reuse step 5.1 DatasetLimits/StatisticsLimits; lower configured admission ceilings
are enforced too. Content must be nonempty bytes, at most 5 MiB. Metadata models
check column counts, row counts and checksum shape; storage computes the checksum
itself. PostgreSQL has additional format, size, digest, JSON shape and foreign-key
constraints. NUL-containing metadata that PostgreSQL cannot store is rejected.

Artifacts are JSON objects, at most 16,384 UTF-8 bytes and depth 16. Reject
nonstring keys, non-JSON Python values, nonfinite numbers, cycles, NULs, invalid
Unicode and oversized structure/text before writing. Serialize with sorted keys,
compact separators and no nonfinite values. Persist that exact JSON text because
JSONB numeric normalization can change byte representation and invalidate a
digest of the originally serialized payload. Metadata columns remain JSONB;
artifact payloads are fetched by ID rather than queried by numerical value.
Database checks enforce JSON-object shape and byte-count consistency.

Reads reject digest/length mismatches instead of returning corrupt values. These
checks detect accidental corruption; they do not authenticate data against an
attacker who can rewrite both payload and digest. Decoded artifact payloads are
fresh values: mutating a returned dictionary does not update stored data.

This storage step does **not** parse CSV, verify that supplied row/column counts
match file contents, infer types or compute statistics. Its create method is an
internal trusted-ingestion boundary, not a user-facing upload handler. Step 5.3
must validate actual CSV facts before step 5.4 composes ingestion with storage.
No numeric result is proven correct merely by persisting arbitrary JSON.

Data/driver errors can contain private data; do not log raw database exceptions,
ORM objects or model_dump output. Existing safe HTTP error handling must be used
when business routes arrive. Filenames and column names are not filesystem paths
or log-safe telemetry. No event payload, access role or token storage changed.

## Upgrade, readiness and rollback

The forward change is additive: no existing auth table/column/data is modified.
Fresh installs and repeated upgrades use the existing explicit migration runner;
app startup still does not create/recreate schema. The new release expects schema
head 0004_dataset_storage and reports not-ready on an older database. After PR
delivery, apply the normal migration command to the explicitly configured agent
database, then check it before starting the new release:

```powershell
.\.venv\Scripts\python.exe -m statistics_agent.persistence.migrate upgrade --env-file .env
.\.venv\Scripts\python.exe -m statistics_agent.persistence.migrate check --env-file .env
```

These commands require the existing configured agent DATABASE_URL and running
database. They were not run on the developer database in this task. Keycloak
keeps its separate database and lifecycle.

The old application's auth SQL remains compatible with the additive tables, but
its exact-head readiness check rejects a newer schema. Do not claim an arbitrary
old-image rollback or zero-downtime mixed-version rollout is validated. Plan 10
must sequence migration and compatible application readiness explicitly.

Downgrading 0004 to 0003 **deletes all dataset/content/artifact rows** by dropping
the new tables; it preserves auth rows. This path is tested only in disposable
schemas. For a production application rollback, retain the additive schema and
use a compatible release/rollout plan; do not apply a destructive downgrade to
recover an older image. The existing CLI downgrade-test action remains restricted
to the explicitly designated test database; it is not a production undo command.

## Verification and stop

Focused storage/migration gate: **39 passed**. Final regression with real
PostgreSQL: **517 passed, 17 browser tests deselected**, 151 upstream warnings,
42.79 seconds. Ruff formatting/checks and pip consistency pass. Coverage includes
fresh/repeated migration, failed-DDL rollback, model/schema agreement, existing
auth-row preservation through upgrade/test downgrade/reupgrade, content-write
rollback, owner/admin/issuer isolation, listing, exact bytes/digests, foreign keys,
dependent deletion, malformed/oversized JSON and corruption rejection.

The disposable datalens-agent-tests project is removed after verification; Docker
Desktop remains running. No persistent identity/developer data, dependency pins,
authentication code, production settings or cloud resources changed. Browser/
Keycloak cases and remote CI were not rerun locally; required PR tests remain the
delivery gate. Parent canonical/planning files remain outside service Git/PRs.
Stop before step 5.3 CSV parsing.

Step 5.3 update: the [CSV parser](csv-validation.md) is now complete locally,
but is not automatically called by DatasetStore. Step 5.4 must compose validated
parsing and storage before ingestion is exposed. No storage/schema behavior
changed in the parser step.
