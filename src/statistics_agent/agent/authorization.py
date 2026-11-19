"""Execution-bound numerical authorization; actual tool loads still enforce ownership."""

from collections.abc import Awaitable, Callable

from statistics_agent.agent.policy import PolicyRules, parse_tool_arguments
from statistics_agent.contracts.agent import AgentContext, ToolDecision, ToolProposal
from statistics_agent.contracts.datasets import (
    AggregateStatisticsArguments,
    CorrelateColumnsArguments,
    DatasetMetadata,
    DescribeColumnsArguments,
)
from statistics_agent.contracts.policy import (
    PolicyEvaluation,
    ToolExecutionScope,
    ToolPolicyConfiguration,
)
from statistics_agent.datasets.service import DatasetService
from statistics_agent.persistence.datasets import DatasetNotFoundError


class PolicyUnavailableError(RuntimeError):
    """Safe operational evidence; no original callback/database error is retained."""

    def __init__(self, proposal: ToolProposal, rules: PolicyRules):
        self.evaluation = PolicyEvaluation(
            tool_call_id=proposal.tool_call_id,
            policy=rules.configuration.reference,
            status="unavailable",
        )
        super().__init__("Tool authorization unavailable")


class OwnedToolPolicy:
    """One trusted execution scope; admin does not override dataset ownership.

    The caller must supply a real current-access callback. Constructing context
    contracts alone does not authenticate anyone. No policy decision replaces
    the executor's owned load or artifact persistence checks.
    """

    def __init__(
        self,
        *,
        scope: ToolExecutionScope,
        rules: PolicyRules,
        datasets: DatasetService,
        authorize: Callable[[AgentContext], Awaitable[bool]],
    ):
        self._scope = ToolExecutionScope.model_validate(scope, strict=True)
        self._rules, self._datasets, self._authorize = rules, datasets, authorize

    @property
    def scope(self) -> ToolExecutionScope:
        return self._scope

    @property
    def configuration(self) -> ToolPolicyConfiguration:
        return self._rules.configuration

    async def evaluate(self, context: AgentContext, proposal: ToolProposal) -> ToolDecision:
        context = AgentContext.model_validate(context, strict=True)
        proposal = ToolProposal.model_validate(proposal, strict=True)

        def deny(reason="denied"):
            return ToolDecision(tool_call_id=proposal.tool_call_id, allowed=False, reason=reason)

        if context != self.scope.context:
            return deny()
        try:
            access = await self._authorize(context)
        except Exception:
            pass
        else:
            if access is not True:
                return deny()
            evaluation = self._rules.check(proposal)
            if evaluation.status != "allowed":
                return evaluation.decision
            arguments = parse_tool_arguments(proposal)
            if arguments.dataset_id not in self.scope.dataset_ids:
                return deny()
            try:
                metadata = DatasetMetadata.model_validate(
                    await self._datasets.get_metadata(context.principal, arguments.dataset_id),
                    strict=True,
                )
                if metadata.dataset_id != arguments.dataset_id:
                    raise ValueError("Dataset identity mismatch")
            except DatasetNotFoundError:
                return deny()
            except Exception:
                pass
            else:
                columns = {column.name: column for column in metadata.columns}
                selected, numeric = (), ()
                if isinstance(arguments, DescribeColumnsArguments):
                    selected = numeric = arguments.columns
                elif isinstance(arguments, AggregateStatisticsArguments):
                    selected = (arguments.column,)
                    if arguments.group_by is not None:
                        selected += (arguments.group_by,)
                    if arguments.operation != "count":
                        numeric = (arguments.column,)
                elif isinstance(arguments, CorrelateColumnsArguments):
                    selected = numeric = (arguments.x_column, arguments.y_column)
                if any(name not in columns for name in selected) or any(
                    columns[name].non_missing_count and columns[name].kind != "numeric"
                    for name in numeric
                ):
                    return deny("invalid_arguments")
                return evaluation.decision
        # Raise outside exception handlers so private callback/SQL errors do not chain.
        raise PolicyUnavailableError(proposal, self._rules)
