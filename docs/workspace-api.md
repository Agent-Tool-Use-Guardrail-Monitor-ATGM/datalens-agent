# DataLens workspace API

Plan 7.7 exposes the statistics workspace in Bearer mode. In development, use
`/docs` or generate frontend types from `GET /openapi.json` on the same agent
revision. Send the Keycloak access token in `Authorization: Bearer <token>`.
In Fetch, use `credentials: "omit"`. Set the exact frontend origin in
`AUTH_CORS_ORIGINS`. Protected responses use `Cache-Control: no-store`.

Start PostgreSQL, apply the [database migrations](../README.md#alembic-baseline-and-schema-gate--step-18)
and run the API and the [worker](durable-worker.md). Both processes need the
same agent database and server-side LLM profile and limits. Keep
`SERVICE_MODEL_API_KEY` in the backend environment. If the API starts with
invalid or missing model configuration, reads and matching submission retries
remain available. New runs return 503 `run_unavailable` until the settings are
fixed and the API is restarted. The current worker CLI still takes the legacy
authentication configuration files to support older jobs during cutover.
Runs accepted by this API use the persisted Bearer actor grant.

## Routes

| Method and path | Request and success response |
| --- | --- |
| `POST /v1/datasets?filename=sales.csv` | Send the raw CSV bytes with `Content-Type: text/csv`. Returns 201 and dataset metadata. |
| `GET /v1/datasets` | List owned dataset metadata. Use `limit` and `after`. |
| `GET /v1/datasets/{dataset_id}` | Read metadata and inferred columns. |
| `GET /v1/datasets/{dataset_id}/preview` | Read up to 20 sample rows and metadata. |
| `DELETE /v1/datasets/{dataset_id}` | Returns 204 after removing the dataset, bytes, artifacts and attachment links. |
| `POST /v1/conversations` | Send `{"title":"Sales"}`. Returns 201 with the new conversation. |
| `GET /v1/conversations` | List owned conversations. Use `limit` and `after`. |
| `GET /v1/conversations/{conversation_id}` | Read one conversation. |
| `DELETE /v1/conversations/{conversation_id}` | Returns 204 after removing a conversation and its history. Active runs cause 409. |
| `GET /v1/conversations/{conversation_id}/attachments` | Read current attached dataset metadata. |
| `PUT /v1/conversations/{conversation_id}/attachments` | Replace the selection with `{"dataset_ids":["<dataset UUID>"]}`. An explicit empty array detaches every dataset. |
| `GET /v1/conversations/{conversation_id}/messages` | Read history in sequence order. Use `limit` and `after_sequence`. |
| `POST /v1/conversations/{conversation_id}/runs` | Submit a question. Returns 202 with `run_id` and `replayed: false`. A matching retry returns 200 and the original ID with `replayed: true`. |
| `GET /v1/runs/{run_id}` | Poll run lifecycle, outcome, clarification and result references. |
| `GET /v1/runs/{run_id}/results/{result_id}` | Read one verified numerical artifact referenced by the run. The response contains provenance and a payload discriminated by `tool`. |

Paged routes return `{items, next_cursor}`. Default page size is 50 and the
maximum is 100. Pass the returned cursor as `after` or `after_sequence`
depending on the route. A full page can be followed by an empty final page.
CSV uploads allow up to 5 MiB, 10,000 data rows, 128 columns and 1,024 characters
per cell. Use UTF-8, comma separation and a header row. JSON request bodies
allow up to 128 KiB.

## Submit and poll

Attach the selected datasets before submitting. Generate a UUID per deliberate
submission and reuse it with the same body if the network outcome is unknown.

```json
{
  "idempotency_key": "60338a20-1801-47d5-9ec6-bd5eebcb6b18",
  "question": "What is the mean price?",
  "selection": {"dataset_ids": ["c5635a04-3aaf-4407-93f0-344b5a0630c3"]}
}
```

Omit `prior_results` unless intentionally reusing stored results from this
conversation. Send the server-issued provenance values unchanged. Use the
computed result's `tool_call_id` or the imported result's `result_id` when
requesting a run result.

Poll while `lifecycle` is `queued` or `running`. For `finished`, use
`outcome` to render `completed`, `needs_input`, `unsupported` or
`incomplete`. A clarification reply starts a new run in the same conversation
with a new key. Show verified results on incomplete runs with their incomplete
status. Treat historical results whose source dataset was deleted as unavailable.
Deleting a dataset preserves historical messages and run records. Deleting a
conversation preserves independently owned datasets and artifacts.

Errors use `{code, message, request_id, details}`. Missing or invalid credentials
return 401 and denied roles return 403. Foreign and missing records share a 404,
including for application admins. A busy conversation returns 409
`conversation_busy`. A reused key with different content returns 409
`idempotency_conflict`. Never generate a new key automatically to retry an
uncertain submission.

## Backend acceptance

On the 7.8 verification branch, run the HTTP workspace and outbox suites
against the [disposable PostgreSQL fixture](../README.md#disposable-database-tests-and-ci).

```powershell
$env:PYTHON_DOTENV_DISABLED = '1'
$env:OTEL_SDK_DISABLED = 'true'
$env:TEST_DATABASE_URL = 'postgresql+psycopg://agent_test:test-only-password@127.0.0.1:54330/agent_test_db'
python -m pytest -q tests/test_workspace_http.py tests/test_outbox.py
```

The workspace test uploads two CSVs through the public API, asks for
clarification, submits the reply and verifies separate means of 4 and 20.
It uses signed test JWTs, a deterministic provider fixture, PostgreSQL and
the real numerical tools, policy and worker. It checks owner-only access,
history, partial results and the corresponding ordered outbox evidence.
The outbox suite simulates Kafka failure and recovery. It verifies that
failed delivery stays pending and a lost acknowledgement can replay the
same event ID. [Kafka topic and delivery contract](kafka-telemetry.md)

Frontend clients generate types from the development OpenAPI schema on
the target agent revision. Run polling uses `conversation-run-v1` or
`conversation-run-v2`, and result payloads use `statistics-result-v1`.
The outbox uses envelope `1.0` with payload `agent-run-events-v2`.
The automated acceptance uses a synthetic provider key and sends no live LLM
requests. Keep real provider keys in the agent's server environment. Live
provider verification and hosted frontend acceptance are separate checks.
