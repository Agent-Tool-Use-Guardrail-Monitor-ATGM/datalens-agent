"""Bearer HTTP routes for owned datasets and durable conversation runs."""

from typing import Annotated, Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.routing import APIRoute
from pydantic import Field
from sqlalchemy.exc import SQLAlchemyError

from statistics_agent.agent.prompts import PROMPT
from statistics_agent.api.bearer import require_bearer_user
from statistics_agent.api.http import error_response
from statistics_agent.contracts.agent import AgentVersions
from statistics_agent.contracts.auth import VerifiedIdentity
from statistics_agent.contracts.common import ConfigurationReference, Contract
from statistics_agent.contracts.conversations import (
    Conversation,
    ConversationMessage,
    ConversationRun,
    ConversationRunV2,
    ConversationTitle,
    DatasetSelection,
    Digest,
    Page,
    ResultProvenance,
    RunSubmission,
)
from statistics_agent.contracts.datasets import MAX_UPLOAD_BYTES, DatasetMetadata, DatasetPreview
from statistics_agent.contracts.http import ErrorEnvelope
from statistics_agent.contracts.policy import ToolPolicyConfiguration
from statistics_agent.contracts.statistics import (
    AggregateStatisticsResult,
    CorrelationResult,
    DatasetInspection,
    DescribeColumnsResult,
)
from statistics_agent.core.model_settings import ModelProfile
from statistics_agent.datasets.csv import CsvValidationError
from statistics_agent.datasets.execution import DatasetParserBusyError
from statistics_agent.persistence.conversations import (
    AdmissionConfiguration,
    AdmissionUnavailableError,
    ConversationBusyError,
    ConversationNotFoundError,
    ConversationStore,
    IdempotencyConflictError,
    RunInputUnavailableError,
)
from statistics_agent.persistence.datasets import DatasetNotFoundError, DatasetStore

API_PREFIX = "/v1"
MAX_JSON_BYTES = 128 * 1024
POLICY = ToolPolicyConfiguration(
    reference=ConfigurationReference(
        config_id=uuid5(NAMESPACE_URL, "datalens:statistics-http-policy:v1"),
        version="statistics-http-v1",
    ),
    allowed_tools=(
        "inspect_dataset",
        "describe_columns",
        "aggregate_statistics",
        "correlate_columns",
    ),
)

Caller = Annotated[VerifiedIdentity, Depends(require_bearer_user)]
Limit = Annotated[int, Query(ge=1, le=100)]
Sequence = Annotated[int, Query(ge=0, le=2**63 - 1)]


def admission_configuration(profile: ModelProfile) -> AdmissionConfiguration:
    from statistics_agent.tools.statistics import STATISTICS_TOOLSET

    return AdmissionConfiguration(
        versions=AgentVersions(
            provider=profile.reference,
            prompt=PROMPT.reference,
            policy=POLICY.reference,
            toolset=STATISTICS_TOOLSET,
        ),
        profile=profile,
        prompt=PROMPT,
        policy=POLICY,
    )


class WorkspaceRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()
        bounded_json = self.body_field is not None

        async def handle(request: Request):
            request.state.protected = True
            if bounded_json:
                receive = request.receive
                total = 0

                async def bounded_receive():
                    nonlocal total
                    message = await receive()
                    total += len(message.get("body", b""))
                    if total > MAX_JSON_BYTES:
                        raise HTTPException(413)
                    return message

                request = Request(request.scope, receive=bounded_receive)

            try:
                return await handler(request)
            except CsvValidationError as error:
                code = error.failure.code
                status = 413 if code == "dataset_limit_exceeded" else 422
                message = "CSV rejected"
            except (DatasetNotFoundError, ConversationNotFoundError, RunInputUnavailableError):
                status, code, message = 404, "resource_unavailable", "Resource unavailable"
            except ConversationBusyError:
                status, code, message = 409, "conversation_busy", "Conversation has active work"
            except IdempotencyConflictError:
                status, code, message = 409, "idempotency_conflict", "Request key already used"
            except DatasetParserBusyError:
                status, code, message = 429, "dataset_parser_busy", "Dataset parser busy"
            except AdmissionUnavailableError:
                status, code, message = 503, "run_unavailable", "Run submission unavailable"
            except SQLAlchemyError:
                status, code, message = 503, "service_unavailable", "Service unavailable"
            return error_response(request.state.request_id, status, code, message)

        return handle


router = APIRouter(
    prefix=API_PREFIX,
    route_class=WorkspaceRoute,
    responses={status: {"model": ErrorEnvelope} for status in (401, 403, 404, 413, 422, 503)},
)


class CreateConversation(Contract):
    title: ConversationTitle


class AttachmentBody(DatasetSelection):
    dataset_ids: tuple[UUID, ...] = Field(max_length=16)


class SubmissionBody(RunSubmission):
    selection: AttachmentBody = Field(default_factory=lambda: AttachmentBody(dataset_ids=()))
    prior_results: tuple[ResultProvenance, ...] = Field(default=(), max_length=16)


class RunAccepted(Contract):
    run_id: UUID
    replayed: bool


class ResultPayload(Contract):
    schema_version: Literal["statistics-result-v1"]
    tool_call_id: UUID
    dataset_sha256: Digest


class InspectionPayload(ResultPayload):
    tool: Literal["inspect_dataset"]
    result: DatasetInspection


class DescriptionPayload(ResultPayload):
    tool: Literal["describe_columns"]
    result: DescribeColumnsResult


class AggregationPayload(ResultPayload):
    tool: Literal["aggregate_statistics"]
    result: AggregateStatisticsResult


class CorrelationPayload(ResultPayload):
    tool: Literal["correlate_columns"]
    result: CorrelationResult


class RunResult(Contract):
    provenance: ResultProvenance
    payload: Annotated[
        InspectionPayload | DescriptionPayload | AggregationPayload | CorrelationPayload,
        Field(discriminator="tool"),
    ]


def _page(items, limit, cursor):
    next_cursor = str(cursor(items[-1])) if len(items) == limit else None
    return {"items": items, "next_cursor": next_cursor}


def conversations(request: Request) -> ConversationStore:
    return ConversationStore(request.app.state.database)


@router.post(
    "/datasets",
    status_code=201,
    tags=["datasets"],
    responses={
        413: {"model": ErrorEnvelope},
        415: {"model": ErrorEnvelope},
        429: {"model": ErrorEnvelope},
    },
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {"text/csv": {"schema": {"type": "string", "format": "binary"}}},
        },
    },
)
async def upload_dataset(
    request: Request,
    caller: Caller,
    filename: Annotated[str, Query(min_length=1, max_length=255)],
) -> DatasetMetadata:
    if request.headers.get("content-type", "").partition(";")[0].strip().lower() != "text/csv":
        raise HTTPException(415)
    if "\x00" in filename:
        raise HTTPException(422)
    content = bytearray()
    async for chunk in request.stream():
        if len(content) + len(chunk) > MAX_UPLOAD_BYTES:
            raise HTTPException(413)
        content.extend(chunk)
    return await request.app.state.datasets.ingest(
        caller, filename=filename, content=bytes(content)
    )


@router.get("/datasets", tags=["datasets"])
async def list_datasets(
    request: Request, caller: Caller, limit: Limit = 50, after: UUID | None = None
) -> Page[DatasetMetadata]:
    items = await request.app.state.datasets.list_metadata(caller, limit=limit, after=after)
    return _page(items, limit, lambda item: item.dataset_id)


@router.get("/datasets/{dataset_id}", tags=["datasets"])
async def get_dataset(request: Request, caller: Caller, dataset_id: UUID) -> DatasetMetadata:
    return await request.app.state.datasets.get_metadata(caller, dataset_id)


@router.get("/datasets/{dataset_id}/preview", tags=["datasets"])
async def preview_dataset(request: Request, caller: Caller, dataset_id: UUID) -> DatasetPreview:
    return await request.app.state.datasets.preview(caller, dataset_id)


@router.delete("/datasets/{dataset_id}", status_code=204, tags=["datasets"])
async def delete_dataset(request: Request, caller: Caller, dataset_id: UUID) -> Response:
    if not await request.app.state.datasets.delete(caller, dataset_id):
        raise DatasetNotFoundError
    return Response(status_code=204)


@router.post("/conversations", status_code=201, tags=["conversations"])
async def create_conversation(
    request: Request, caller: Caller, body: CreateConversation
) -> Conversation:
    if "\x00" in body.title:
        raise HTTPException(422)
    return await conversations(request).create(caller, title=body.title)


@router.get("/conversations", tags=["conversations"])
async def list_conversations(
    request: Request, caller: Caller, limit: Limit = 50, after: UUID | None = None
) -> Page[Conversation]:
    items = await conversations(request).list(caller, limit=limit, after=after)
    return _page(items, limit, lambda item: item.conversation_id)


@router.get("/conversations/{conversation_id}", tags=["conversations"])
async def get_conversation(request: Request, caller: Caller, conversation_id: UUID) -> Conversation:
    return await conversations(request).get(caller, conversation_id)


@router.delete("/conversations/{conversation_id}", status_code=204, tags=["conversations"])
async def delete_conversation(request: Request, caller: Caller, conversation_id: UUID) -> Response:
    await conversations(request).delete(caller, conversation_id)
    return Response(status_code=204)


@router.get("/conversations/{conversation_id}/attachments", tags=["conversations"])
async def list_attachments(
    request: Request, caller: Caller, conversation_id: UUID
) -> tuple[DatasetMetadata, ...]:
    return await conversations(request).attachments(caller, conversation_id)


@router.put("/conversations/{conversation_id}/attachments", tags=["conversations"])
async def replace_attachments(
    request: Request, caller: Caller, conversation_id: UUID, body: AttachmentBody
) -> tuple[DatasetMetadata, ...]:
    return await conversations(request).set_attachments(caller, conversation_id, body)


@router.get("/conversations/{conversation_id}/messages", tags=["conversations"])
async def list_messages(
    request: Request,
    caller: Caller,
    conversation_id: UUID,
    limit: Limit = 50,
    after_sequence: Sequence = 0,
) -> Page[ConversationMessage]:
    items = await conversations(request).messages(
        caller, conversation_id, limit=limit, after_sequence=after_sequence
    )
    return _page(items, limit, lambda item: item.sequence)


@router.post(
    "/conversations/{conversation_id}/runs",
    status_code=202,
    responses={200: {"model": RunAccepted}, 409: {"model": ErrorEnvelope}},
    tags=["runs"],
)
async def submit_run(
    request: Request,
    caller: Caller,
    conversation_id: UUID,
    body: SubmissionBody,
    response: Response,
) -> RunAccepted:
    if "\x00" in body.question:
        raise HTTPException(422)
    admitted = await conversations(request).admit(
        caller, conversation_id, body, request.app.state.admission_configuration
    )
    if admitted.replayed:
        response.status_code = 200
    return RunAccepted(run_id=admitted.run.run_id, replayed=admitted.replayed)


@router.get("/runs/{run_id}", tags=["runs"])
async def get_run(
    request: Request, caller: Caller, run_id: UUID
) -> ConversationRunV2 | ConversationRun:
    return (await conversations(request).get_run(caller, run_id)).run


@router.get("/runs/{run_id}/results/{result_id}", tags=["runs"])
async def get_result(request: Request, caller: Caller, run_id: UUID, result_id: UUID) -> RunResult:
    run = (await conversations(request).get_run(caller, run_id)).run
    provenance = next((item for item in run.results if item.tool_call_id == result_id), None)
    if provenance is None and isinstance(run, ConversationRunV2):
        provenance = next(
            (
                item.source
                for item in run.completion.imported_results
                if item.result_id == result_id
            ),
            None,
        )
    if provenance is None:
        raise RunInputUnavailableError
    artifact = await DatasetStore(request.app.state.database).get_artifact(
        caller, provenance.artifact.artifact_id
    )
    if (
        artifact.reference != provenance.artifact
        or artifact.dataset_id != provenance.dataset.dataset_id
    ):
        raise RunInputUnavailableError
    result = RunResult(provenance=provenance, payload=artifact.payload)
    if (
        result.payload.tool_call_id != provenance.tool_call_id
        or result.payload.dataset_sha256 != provenance.dataset.sha256
        or result.payload.result.dataset_id != provenance.dataset.dataset_id
    ):
        raise RunInputUnavailableError
    return result
