"""Planned conversation/run wire values; no admission, persistence or execution."""

from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, Field, StringConstraints, model_validator

from statistics_agent.contracts.agent import AgentVersions
from statistics_agent.contracts.common import ArtifactReference, Contract, ShortReference

type Question = Annotated[
    str, StringConstraints(strict=True, min_length=1, max_length=8192, pattern=r"\S")
]
type MessageText = Annotated[
    str, StringConstraints(strict=True, min_length=1, max_length=16384, pattern=r"\S")
]
type Digest = Annotated[str, StringConstraints(strict=True, pattern=r"^[0-9a-f]{64}$")]
type RunOutcome = Literal["completed", "needs_input", "unsupported", "incomplete"]


class DatasetSelection(Contract):
    dataset_ids: tuple[UUID, ...] = Field(default=(), strict=True, max_length=16)

    @model_validator(mode="after")
    def unique_ids(self) -> Self:
        if len(set(self.dataset_ids)) != len(self.dataset_ids):
            raise ValueError("Dataset selection must be unique")
        return self


class DatasetSnapshot(Contract):
    """Server-derived immutable identity; never proof of ownership."""

    dataset_id: UUID
    sha256: Digest


class ResultProvenance(Contract):
    """Resolve/recheck every reference against owned persisted results before use."""

    conversation_id: UUID
    run_id: UUID
    tool_call_id: UUID
    dataset: DatasetSnapshot
    artifact: ArtifactReference


class RunSubmission(Contract):
    idempotency_key: UUID
    question: Question = Field(repr=False)
    selection: DatasetSelection = Field(default_factory=DatasetSelection)
    prior_results: tuple[ResultProvenance, ...] = Field(default=(), strict=True, max_length=16)

    @model_validator(mode="after")
    def unique_results(self) -> Self:
        ids = [result.tool_call_id for result in self.prior_results]
        if len(ids) != len(set(ids)):
            raise ValueError("Prior result references must be unique")
        return self


class Conversation(Contract):
    conversation_id: UUID
    title: Annotated[
        str, StringConstraints(strict=True, min_length=1, max_length=120, pattern=r"\S")
    ] = Field(repr=False)
    created_at: AwareDatetime


class ConversationMessage(Contract):
    message_id: UUID
    conversation_id: UUID
    run_id: UUID
    sequence: Annotated[int, Field(strict=True, ge=1)]
    role: Literal["user", "assistant"]
    content: MessageText = Field(repr=False)
    created_at: AwareDatetime


class ClarificationOption(Contract):
    key: ShortReference
    text: Annotated[
        str, StringConstraints(strict=True, min_length=1, max_length=256, pattern=r"\S")
    ] = Field(repr=False)


class RequestClarification(Contract):
    """Arguments for request_clarification; control only, not a numerical tool."""

    question: Annotated[
        str, StringConstraints(strict=True, min_length=1, max_length=512, pattern=r"\S")
    ] = Field(repr=False)
    options: tuple[ClarificationOption, ...] = Field(default=(), max_length=8)

    @model_validator(mode="after")
    def unique_options(self) -> Self:
        keys = [option.key for option in self.options]
        if len(keys) != len(set(keys)):
            raise ValueError("Clarification option keys must be unique")
        return self


class ConversationRun(Contract):
    schema_version: Literal["conversation-run-v1"] = "conversation-run-v1"
    run_id: UUID
    conversation_id: UUID
    lifecycle: Literal["queued", "running", "finished"]
    outcome: RunOutcome | None = None
    created_at: AwareDatetime
    started_at: AwareDatetime | None = None
    finished_at: AwareDatetime | None = None
    versions: AgentVersions
    selection: tuple[DatasetSnapshot, ...] = Field(default=(), max_length=16)
    results: tuple[ResultProvenance, ...] = Field(default=(), max_length=128)
    clarification: RequestClarification | None = Field(default=None, repr=False)

    @model_validator(mode="after")
    def consistent_state(self) -> Self:
        if self.lifecycle == "finished":
            if self.outcome is None or self.finished_at is None:
                raise ValueError("Finished runs require an outcome and completion time")
        elif (
            self.outcome is not None
            or self.finished_at is not None
            or self.clarification is not None
        ):
            raise ValueError("Active runs cannot have terminal fields")
        if self.lifecycle == "queued" and (self.started_at is not None or self.results):
            raise ValueError("Queued runs have not started computing")
        if self.lifecycle == "running" and self.started_at is None:
            raise ValueError("Running runs require a start time")
        if self.started_at is not None and self.started_at < self.created_at:
            raise ValueError("Start time precedes creation")
        if self.finished_at is not None and self.finished_at < (self.started_at or self.created_at):
            raise ValueError("Completion time precedes execution")
        if (self.outcome == "needs_input") != (self.clarification is not None):
            raise ValueError("Only needs_input requires clarification")
        if self.outcome == "completed" and not self.results:
            raise ValueError("Completed statistics require verified results")
        datasets = {item.dataset_id: item.sha256 for item in self.selection}
        if len(datasets) != len(self.selection):
            raise ValueError("Dataset snapshots must be unique")
        calls = set()
        for result in self.results:
            if (
                result.conversation_id != self.conversation_id
                or result.run_id != self.run_id
                or datasets.get(result.dataset.dataset_id) != result.dataset.sha256
                or result.tool_call_id in calls
            ):
                raise ValueError("Result provenance does not match this run")
            calls.add(result.tool_call_id)
        return self


class PageRequest(Contract):
    limit: Annotated[int, Field(strict=True, ge=1, le=100)] = 50
    cursor: (
        Annotated[str, StringConstraints(strict=True, min_length=1, max_length=512, pattern=r"\S")]
        | None
    ) = Field(default=None, repr=False)


class Page[T](Contract):
    items: tuple[T, ...] = Field(max_length=100)
    next_cursor: (
        Annotated[str, StringConstraints(strict=True, min_length=1, max_length=512, pattern=r"\S")]
        | None
    ) = Field(default=None, repr=False)


class RunError(Contract):
    """Fixed safe codes; map to the existing HTTP envelope at the future API layer."""

    code: Literal[
        "conversation_busy",
        "idempotency_conflict",
        "resource_unavailable",
        "invalid_request",
        "context_limit_exceeded",
        "access_denied",
    ]
    request_id: UUID
