"""Private server-built context snapshots, never accepted as client authorization."""

from uuid import UUID, uuid5

from pydantic import Field, model_validator

from statistics_agent.contracts.agent import (
    AgentContext,
    AgentMessage,
    AgentVersions,
    DatasetSummary,
    ToolResult,
)
from statistics_agent.contracts.common import Contract
from statistics_agent.contracts.conversations import Question, ResultProvenance


class HistoricalResult(Contract):
    source: ResultProvenance
    result: ToolResult


class PreparedContext(Contract):
    context: AgentContext
    versions: AgentVersions
    conversation_id: UUID
    question: Question = Field(repr=False)
    datasets: tuple[DatasetSummary, ...] = Field(max_length=16)
    messages: tuple[AgentMessage, ...] = Field(min_length=2, max_length=2, repr=False)
    historical: tuple[HistoricalResult, ...] = Field(default=(), max_length=16, repr=False)
    history_omitted: bool = Field(strict=True)
    reservation: int = Field(strict=True, ge=1)

    @model_validator(mode="after")
    def consistent_context(self):
        if tuple(message.role for message in self.messages) != ("system", "user"):
            raise ValueError("Prepared context must keep untrusted history inside user data")
        for item in self.historical:
            if (
                item.source.conversation_id != self.conversation_id
                or item.result.tool_call_id
                != uuid5(self.context.run_id, str(item.source.tool_call_id))
                or item.result.status != "completed"
                or item.result.summary is None
                or item.result.artifact != item.source.artifact
            ):
                raise ValueError("Historical result mapping is inconsistent")
        if len({item.result.tool_call_id for item in self.historical}) != len(self.historical):
            raise ValueError("Historical result mappings must be unique")
        return self
