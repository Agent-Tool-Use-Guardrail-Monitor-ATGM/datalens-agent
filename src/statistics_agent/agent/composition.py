"""Internal composition root; admission, durable jobs and HTTP belong to plan 7."""

from collections.abc import Awaitable, Callable
from datetime import datetime

from statistics_agent.agent.authorization import OwnedToolPolicy
from statistics_agent.agent.policy import PolicyRules
from statistics_agent.agent.provider import ModelAdapter
from statistics_agent.agent.statistics import StatisticsSession
from statistics_agent.contracts.agent import AgentContext, EventSink
from statistics_agent.contracts.common import ConfigurationReference
from statistics_agent.contracts.policy import ToolExecutionScope, ToolPolicyConfiguration
from statistics_agent.datasets.execution import DatasetParser
from statistics_agent.datasets.service import DatasetService
from statistics_agent.persistence.datasets import DatasetStore
from statistics_agent.tools.statistics import StatisticsTools


def create_statistics_session(
    *,
    scope: ToolExecutionScope,
    configuration: ToolPolicyConfiguration,
    datasets: DatasetService,
    worker: DatasetParser,
    artifacts: DatasetStore,
    adapter: ModelAdapter,
    toolset: ConfigurationReference,
    authorize: Callable[[AgentContext], Awaitable[bool]],
    sink: EventSink,
    sequence: Callable[[], int],
    deadline: datetime,
) -> StatisticsSession:
    """Bind one trusted execution to the real policy and numerical executor.

    Use the dataset service's lifespan-owned worker and the adapter's sink/sequence.
    Admission must supply owned dataset summaries to run(), not client metadata.
    No default allow policy, independent native CrewAI executor or public route.
    """
    scope = ToolExecutionScope.model_validate(scope, strict=True)
    rules = PolicyRules(configuration)

    async def current_access(context: AgentContext) -> bool:
        return context == scope.context and await authorize(context) is True

    policy = OwnedToolPolicy(scope=scope, rules=rules, datasets=datasets, authorize=current_access)
    executor = StatisticsTools(
        datasets,
        worker=worker,
        artifacts=artifacts,
        dataset_limits=rules.configuration.dataset_limits,
        limits=rules.configuration.statistics_limits,
    )
    return StatisticsSession(
        context=scope.context,
        adapter=adapter,
        toolset=toolset,
        policy_version=rules.configuration.reference,
        bindings=executor.bindings,
        policy=policy,
        executor=executor,
        authorize=current_access,
        sink=sink,
        sequence=sequence,
        deadline=deadline,
    )
