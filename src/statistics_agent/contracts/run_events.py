"""Opt-in typed v2 payloads for plan 7; existing producers/envelope stay compatible."""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, model_validator

from statistics_agent.contracts.agent import AgentVersions, TokenUsage
from statistics_agent.contracts.common import Contract
from statistics_agent.contracts.conversations import Digest, RunOutcome
from statistics_agent.contracts.events import EventEnvelope, EventType
from statistics_agent.contracts.policy import NumericalTool, PolicyEvaluation

type Nonnegative = Annotated[int, Field(strict=True, ge=0, le=2**63 - 1)]


class RunEventPayload(Contract):
    payload_version: Literal["agent-run-events-v2"] = "agent-run-events-v2"


class SessionStarted(RunEventPayload):
    conversation_id: UUID
    versions: AgentVersions
    dataset_count: Annotated[int, Field(strict=True, ge=0, le=16)]


class ToolFeatures(Contract):
    """Categorical/count features only: no column names, values or argument text."""

    argument_shape: Literal["valid", "invalid_json", "invalid_schema", "not_checked"]
    dataset_selection: Literal["selected", "unselected", "absent", "invalid", "not_checked"]
    # Capture over-limit proposals too; this is evidence, not a tool admission limit.
    column_count: Annotated[int, Field(strict=True, ge=0, le=16384)] = 0
    operation: Literal["count", "sum", "mean", "min", "max"] | None = None
    method: Literal["pearson", "spearman"] | None = None
    missing_policy: Literal["omit", "error", "pairwise_complete"] | None = None


class ToolRequested(RunEventPayload):
    tool_call_id: UUID
    kind: Literal["numerical", "control", "unknown"]
    name: NumericalTool | Literal["finalize_statistics", "request_clarification"] | None = None
    arguments_sha256: Digest
    features: ToolFeatures

    @model_validator(mode="after")
    def consistent_kind(self):
        controls = {"finalize_statistics", "request_clarification"}
        if (
            (self.kind == "unknown" and self.name is not None)
            or (self.kind != "unknown" and self.name is None)
            or (self.kind == "control" and self.name not in controls)
            or (self.kind == "numerical" and self.name in controls)
        ):
            raise ValueError("Tool name and kind disagree")
        return self


class ToolCompleted(RunEventPayload):
    tool_call_id: UUID
    status: Literal["completed"] = "completed"
    policy_evaluation: PolicyEvaluation | None = None
    latency_ms: Nonnegative | None = None

    @model_validator(mode="after")
    def matching_policy(self):
        if (
            self.policy_evaluation is not None
            and self.policy_evaluation.tool_call_id != self.tool_call_id
        ):
            raise ValueError("Policy call identity mismatch")
        if (
            self.status == "completed"
            and self.policy_evaluation is not None
            and self.policy_evaluation.status != "allowed"
        ):
            raise ValueError("Completed numerical work requires policy allow evidence")
        return self


class ToolFailed(ToolCompleted):
    status: Literal["failed", "timed_out", "unknown", "skipped", "rejected"]
    reason: Literal[
        "unknown_tool",
        "invalid_arguments",
        "access_denied",
        "policy_denied",
        "policy_unavailable",
        "execution_result",
        "invalid_final_answer",
        "limit_exceeded",
        "interrupted",
        "not_dispatched",
        "invalid_clarification",
        "invalid_tool_identity",
        "mixed_finalization_batch",
    ]


class ModelCompleted(RunEventPayload):
    model_call_id: UUID
    attempt_index: Annotated[int, Field(strict=True, ge=1)]
    latency_ms: Nonnegative
    usage: TokenUsage


class ModelFailed(RunEventPayload):
    model_call_id: UUID
    attempt_index: Annotated[int, Field(strict=True, ge=1)]
    latency_ms: Nonnegative | None = None
    category: Literal[
        "authentication",
        "rate_limit",
        "quota",
        "timeout",
        "network",
        "provider_error",
        "invalid_response",
        "interrupted",
    ]
    outcome_unknown: Annotated[bool, Field(strict=True)] = False


class SessionEnded(RunEventPayload):
    lifecycle: Literal["finished"] = "finished"
    outcome: RunOutcome
    verified_result_count: Annotated[int, Field(strict=True, ge=0, le=128)]

    @model_validator(mode="after")
    def completed_results(self):
        if self.outcome == "completed" and not self.verified_result_count:
            raise ValueError("Completed statistics require verified results")
        return self


PAYLOADS = {
    EventType.SESSION_STARTED: SessionStarted,
    EventType.TOOL_CALL_REQUESTED: ToolRequested,
    EventType.TOOL_CALL_COMPLETED: ToolCompleted,
    EventType.TOOL_CALL_FAILED: ToolFailed,
    EventType.MODEL_CALL_COMPLETED: ModelCompleted,
    EventType.MODEL_CALL_FAILED: ModelFailed,
    EventType.SESSION_ENDED: SessionEnded,
}


def validate_run_event(event: EventEnvelope) -> EventEnvelope:
    """Validate opt-in payload semantics and the existing envelope size/privacy shape."""
    event = EventEnvelope.model_validate(event)
    if event.data.get("payload_version") != "agent-run-events-v2":
        raise ValueError("Explicit agent-run-events-v2 payload version required")
    payload = PAYLOADS[event.event_type].model_validate(event.data)
    return EventEnvelope.model_validate(
        event.model_dump() | {"data": payload.model_dump(mode="json", exclude_none=True)}
    )
