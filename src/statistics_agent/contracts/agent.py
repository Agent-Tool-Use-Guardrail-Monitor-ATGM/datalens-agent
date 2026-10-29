"""Internal agent boundaries. No provider, framework or infrastructure is loaded."""

from typing import Annotated, Literal, Protocol, Self
from uuid import UUID

from pydantic import AwareDatetime, Field, StringConstraints, model_validator

from statistics_agent.contracts.auth import AuthorizedPrincipal
from statistics_agent.contracts.common import (
    ArtifactReference,
    ConfigurationReference,
    Contract,
    PositiveSequence,
    ShortReference,
)
from statistics_agent.contracts.events import EventEnvelope

type TokenCount = Annotated[int, Field(strict=True, ge=0, le=2**63 - 1)]
type BoundedText = Annotated[str, StringConstraints(strict=True, max_length=16_384)]


class AgentContext(Contract):
    """Server-supplied authorization reference, never proof of ongoing access."""

    principal: AuthorizedPrincipal
    run_id: UUID
    session_id: UUID
    request_id: UUID


class AgentVersions(Contract):
    """Pin non-secret configuration identities for one execution session."""

    provider: ConfigurationReference
    prompt: ConfigurationReference
    toolset: ConfigurationReference
    policy: ConfigurationReference


class PromptDefinition(Contract):
    reference: ConfigurationReference
    instructions: BoundedText = Field(min_length=1, repr=False)
    response_schema_version: ShortReference


class ToolProposal(Contract):
    """Capture before lookup/validation, including unknown names and invalid JSON.

    These are restricted internal contents, not safe Kafka payloads. Oversized
    evidence needs a restricted artifact in the adapter, not silent truncation.
    """

    tool_call_id: UUID
    provider_tool_call_id: BoundedText | None = None
    name: BoundedText = Field(repr=False)
    raw_arguments: BoundedText = Field(repr=False)


class AgentMessage(Contract):
    role: Literal["system", "user", "assistant", "tool"]
    content: BoundedText = Field(repr=False)
    proposals: tuple[ToolProposal, ...] = Field(default=(), max_length=32, repr=False)
    tool_call_id: UUID | None = None

    @model_validator(mode="after")
    def check_tool_binding(self) -> Self:
        if (self.role == "tool") != (self.tool_call_id is not None):
            raise ValueError("Only tool messages require a tool-call identity")
        if self.proposals and self.role != "assistant":
            raise ValueError("Only assistant messages may contain tool proposals")
        return self


class TokenUsage(Contract):
    """None means unreported. Subset counts must not be added to totals."""

    source: Literal["unavailable", "provider"] = "unavailable"
    input_tokens: TokenCount | None = None
    output_tokens: TokenCount | None = None
    cached_input_tokens: TokenCount | None = None
    reasoning_tokens: TokenCount | None = None

    @model_validator(mode="after")
    def check_counts(self) -> Self:
        counts = (
            self.input_tokens,
            self.output_tokens,
            self.cached_input_tokens,
            self.reasoning_tokens,
        )
        if (self.source == "unavailable") != all(value is None for value in counts):
            raise ValueError("Usage source must agree with reported counts")
        for subset, total in (
            (self.cached_input_tokens, self.input_tokens),
            (self.reasoning_tokens, self.output_tokens),
        ):
            if subset is not None and total is not None and subset > total:
                raise ValueError("Token subset exceeds its reported total")
        return self


class ModelAttempt(Contract):
    run_id: UUID
    session_id: UUID
    request_id: UUID
    model_call_id: UUID
    attempt_index: PositiveSequence


class ProviderRequest(Contract):
    """Provider input excludes user identity and authentication/session material."""

    attempt: ModelAttempt
    profile: ConfigurationReference
    messages: tuple[AgentMessage, ...] = Field(min_length=1, max_length=128, repr=False)
    max_output_tokens: PositiveSequence
    deadline: AwareDatetime


class ProviderResponse(Contract):
    attempt: ModelAttempt
    provider_request_id: BoundedText | None = None
    actual_model: BoundedText | None = None
    content: BoundedText = Field(default="", repr=False)
    proposals: tuple[ToolProposal, ...] = Field(default=(), max_length=32, repr=False)
    finish_reason: Literal["stop", "tool_calls", "length", "filtered", "unknown"]
    usage: TokenUsage = Field(default_factory=TokenUsage)


class ProviderFailure(Contract):
    """Safe categories only; never raw exception messages or response bodies."""

    attempt: ModelAttempt
    category: Literal[
        "authentication",
        "rate_limit",
        "quota",
        "timeout",
        "network",
        "provider_error",
        "invalid_response",
    ]
    http_status: Annotated[int, Field(strict=True, ge=100, le=599)] | None = None
    retry_after_seconds: Annotated[float, Field(ge=0)] | None = None
    outcome_unknown: bool = Field(default=False, strict=True)


class AgentState(Contract):
    """Bounded immutable value snapshot; not a database checkpoint implementation."""

    context: AgentContext
    versions: AgentVersions
    messages: tuple[AgentMessage, ...] = Field(default=(), max_length=128, repr=False)
    artifacts: tuple[ArtifactReference, ...] = Field(default=(), max_length=128)
    pending_tool_calls: tuple[ToolProposal, ...] = Field(default=(), max_length=32, repr=False)
    completed_attempts: TokenCount = 0
    remaining_steps: TokenCount
    remaining_input_tokens: TokenCount
    remaining_output_tokens: TokenCount
    deadline: AwareDatetime


class ToolDecision(Contract):
    tool_call_id: UUID
    allowed: bool = Field(strict=True)
    reason: Literal["allowed", "unknown_tool", "invalid_arguments", "denied", "limit_exceeded"]

    @model_validator(mode="after")
    def check_decision(self) -> Self:
        if self.allowed != (self.reason == "allowed"):
            raise ValueError("Tool decision and reason must agree")
        return self


class ToolResult(Contract):
    tool_call_id: UUID
    status: Literal["completed", "failed", "timed_out", "unknown"]
    artifact: ArtifactReference | None = None
    summary: BoundedText | None = Field(default=None, repr=False)


class Provider(Protocol):
    async def complete(self, request: ProviderRequest) -> ProviderResponse | ProviderFailure:
        """Perform one attempt; the later adapter owns retry policy."""
        ...


class ToolPolicy(Protocol):
    async def evaluate(self, context: AgentContext, proposal: ToolProposal) -> ToolDecision:
        """Resolve ownership, tool validity and policy before execution."""
        ...


class ToolExecutor(Protocol):
    async def execute(self, context: AgentContext, proposal: ToolProposal) -> ToolResult:
        """Called only after a matching allowed decision and durable request record."""
        ...


class EventSink(Protocol):
    async def append(self, event: EventEnvelope) -> None:
        """Accept redacted evidence; plan 7 must persist before acknowledging.

        Exceptions prevent dispatch. This declaration supplies no durability.
        """
        ...
