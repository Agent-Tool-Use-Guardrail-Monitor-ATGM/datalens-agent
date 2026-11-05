"""Server-owned numerical policy snapshots and redacted decision evidence."""

from typing import Literal, Self
from uuid import UUID

from pydantic import Field, model_validator

from statistics_agent.contracts.agent import ToolDecision
from statistics_agent.contracts.common import ConfigurationReference, Contract
from statistics_agent.contracts.datasets import DatasetLimits, StatisticsLimits

type NumericalTool = Literal[
    "inspect_dataset", "describe_columns", "aggregate_statistics", "correlate_columns"
]
type AggregateOperation = Literal["count", "sum", "mean", "min", "max"]
type CorrelationMethod = Literal["pearson", "spearman"]


class ToolPolicyConfiguration(Contract):
    """No tools are enabled implicitly; limits cannot exceed existing ceilings."""

    reference: ConfigurationReference
    allowed_tools: tuple[NumericalTool, ...] = Field(default=(), strict=True, max_length=4)
    aggregate_operations: tuple[AggregateOperation, ...] = Field(
        default=("count", "sum", "mean", "min", "max"), strict=True, max_length=5
    )
    correlation_methods: tuple[CorrelationMethod, ...] = Field(
        default=("pearson", "spearman"), strict=True, max_length=2
    )
    dataset_limits: DatasetLimits = Field(default_factory=DatasetLimits)
    statistics_limits: StatisticsLimits = Field(default_factory=StatisticsLimits)

    @model_validator(mode="after")
    def unique_entries(self) -> Self:
        for values in (self.allowed_tools, self.aggregate_operations, self.correlation_methods):
            if len(values) != len(set(values)):
                raise ValueError("Policy allowlists must not contain duplicates")
        return self


class PolicyEvaluation(Contract):
    """Unavailable/unevaluated policy must never masquerade as an actual denial."""

    tool_call_id: UUID
    status: Literal["allowed", "denied", "unavailable", "not_evaluated"]
    policy: ConfigurationReference | None = None
    decision: ToolDecision | None = None

    @model_validator(mode="after")
    def consistent_evidence(self) -> Self:
        if self.status in {"allowed", "denied"}:
            if (
                self.policy is None
                or self.decision is None
                or self.decision.tool_call_id != self.tool_call_id
                or self.decision.allowed != (self.status == "allowed")
            ):
                raise ValueError("Evaluated policy requires matching decision and version")
        elif self.decision is not None:
            raise ValueError("Unavailable or unevaluated policy cannot carry a decision")
        return self
