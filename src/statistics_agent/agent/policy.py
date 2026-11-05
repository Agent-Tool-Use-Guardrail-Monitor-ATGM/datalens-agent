"""Static policy rules only; owned execution authorization remains a separate step."""

from pydantic import ValidationError

from statistics_agent.contracts.agent import ToolDecision, ToolProposal
from statistics_agent.contracts.datasets import (
    AggregateStatisticsArguments,
    CorrelateColumnsArguments,
    DescribeColumnsArguments,
    InspectDatasetArguments,
)
from statistics_agent.contracts.policy import PolicyEvaluation, ToolPolicyConfiguration

_ARGUMENTS = {
    "inspect_dataset": InspectDatasetArguments,
    "describe_columns": DescribeColumnsArguments,
    "aggregate_statistics": AggregateStatisticsArguments,
    "correlate_columns": CorrelateColumnsArguments,
}


class PolicyRules:
    """Pin a validated immutable snapshot; check() does not authorize execution.

    No database, identity provider, framework or mutable configuration lookup.
    Plan 6.2 must combine this result with owned-resource authorization before
    returning the runner's ToolDecision. Actual data/output guards remain 6.3.
    """

    def __init__(self, configuration: ToolPolicyConfiguration):
        self._configuration = ToolPolicyConfiguration.model_validate(configuration, strict=True)

    @property
    def configuration(self) -> ToolPolicyConfiguration:
        return self._configuration

    def check(self, proposal: ToolProposal) -> PolicyEvaluation:
        proposal = ToolProposal.model_validate(proposal, strict=True)
        configuration = self.configuration
        arguments_type = _ARGUMENTS.get(proposal.name)
        if arguments_type is None:
            reason = "unknown_tool"
        elif proposal.name not in configuration.allowed_tools:
            reason = "denied"
        else:
            try:
                arguments = arguments_type.model_validate_json(proposal.raw_arguments, strict=True)
            except ValidationError:
                reason = "invalid_arguments"
            else:
                reason = "allowed"
                if isinstance(arguments, DescribeColumnsArguments):
                    if len(arguments.columns) > configuration.dataset_limits.max_columns:
                        reason = "limit_exceeded"
                elif isinstance(arguments, AggregateStatisticsArguments):
                    if arguments.operation not in configuration.aggregate_operations:
                        reason = "denied"
                elif isinstance(arguments, CorrelateColumnsArguments):
                    if arguments.method not in configuration.correlation_methods:
                        reason = "denied"
        decision = ToolDecision(
            tool_call_id=proposal.tool_call_id, allowed=reason == "allowed", reason=reason
        )
        return PolicyEvaluation(
            tool_call_id=proposal.tool_call_id,
            policy=configuration.reference,
            status="allowed" if decision.allowed else "denied",
            decision=decision,
        )
