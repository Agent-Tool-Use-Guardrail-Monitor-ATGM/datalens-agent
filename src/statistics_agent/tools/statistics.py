"""Four numerical bindings/executor; caller still owns policy and event gates."""

import json
from functools import partial

from pydantic import ValidationError

from statistics_agent.agent.statistics import ToolBinding
from statistics_agent.contracts.agent import AgentContext, ToolProposal, ToolResult
from statistics_agent.contracts.datasets import (
    AggregateStatisticsArguments,
    CorrelateColumnsArguments,
    DescribeColumnsArguments,
    InspectDatasetArguments,
    StatisticsLimits,
)
from statistics_agent.datasets.csv import CsvValidationError
from statistics_agent.datasets.execution import (
    DatasetParser,
    DatasetParserBusyError,
    DatasetParserClosedError,
)
from statistics_agent.datasets.service import DatasetIntegrityError, DatasetService
from statistics_agent.persistence.datasets import DatasetNotFoundError, DatasetStore, artifact_json
from statistics_agent.tools.aggregation import aggregate_statistics, correlate_columns
from statistics_agent.tools.descriptive import (
    StatisticsToolError,
    describe_columns,
    inspect_dataset,
)

STATISTICS_BINDINGS = (
    ToolBinding(
        "inspect_dataset",
        "Inspect dimensions, types and missing counts.",
        InspectDatasetArguments,
    ),
    ToolBinding(
        "describe_columns",
        "Describe numeric columns; explicit missing policy.",
        DescribeColumnsArguments,
    ),
    ToolBinding(
        "aggregate_statistics",
        "Aggregate a column; optional grouping.",
        AggregateStatisticsArguments,
    ),
    ToolBinding(
        "correlate_columns",
        "Correlate two columns; no hypothesis test.",
        CorrelateColumnsArguments,
    ),
)
MAX_SUMMARY_BYTES = 1024


def _failed(proposal: ToolProposal, code: str) -> ToolResult:
    return ToolResult(
        tool_call_id=proposal.tool_call_id, status="failed", summary=json.dumps({"error": code})
    )


class StatisticsTools:
    """Invoke only after the runner's matching allow decision and request evidence.

    Owned loading and typed arguments remain enforced here. This is not a
    production policy or a public endpoint. Share the dataset service's worker
    and configure artifact/store limits consistently at composition time.
    """

    bindings = STATISTICS_BINDINGS

    def __init__(
        self,
        datasets: DatasetService,
        *,
        worker: DatasetParser,
        artifacts: DatasetStore,
        limits: StatisticsLimits | None = None,
    ):
        self._datasets, self._worker, self._artifacts = datasets, worker, artifacts
        self._limits = StatisticsLimits.model_validate(
            limits if limits is not None else StatisticsLimits()
        )
        self._bindings = {binding.name: binding for binding in self.bindings}

    async def execute(self, context: AgentContext, proposal: ToolProposal) -> ToolResult:
        binding = self._bindings.get(proposal.name)
        if binding is None:
            return _failed(proposal, "unknown_tool")
        try:
            arguments = binding.arguments.model_validate_json(proposal.raw_arguments, strict=True)
        except ValidationError:
            return _failed(proposal, "invalid_arguments")
        try:
            loaded = await self._datasets.load(context.principal, arguments.dataset_id)
            operations = {
                "inspect_dataset": partial(inspect_dataset, loaded.parsed, arguments.dataset_id),
                "describe_columns": partial(describe_columns, loaded.parsed, arguments),
                "aggregate_statistics": partial(
                    aggregate_statistics, loaded.parsed, arguments, self._limits
                ),
                "correlate_columns": partial(
                    correlate_columns, loaded.parsed, arguments, self._limits
                ),
            }
            operation = operations[proposal.name]
            result = await self._worker.compute(operation)
            payload = {
                "schema_version": "statistics-result-v1",
                "tool": proposal.name,
                "tool_call_id": str(proposal.tool_call_id),
                "dataset_sha256": loaded.metadata.sha256,
                "result": result.model_dump(mode="json"),
            }
            try:
                artifact_json(payload, self._limits.max_result_bytes)
            except ValueError:
                return _failed(proposal, "result_limit_exceeded")
            # Provenance and implementation metadata remain in the complete artifact.
            # Keep every numerical value, missing count and availability reason.
            summary_result = {
                key: value
                for key, value in payload["result"].items()
                if key
                not in {
                    "dataset_id",
                    "backend",
                    "backend_version",
                    "numpy_version",
                    "scipy_version",
                    "dtype",
                    "column",
                    "operation",
                    "group_by",
                    "x_column",
                    "y_column",
                }
            }
            candidate = json.dumps(
                {"result": summary_result},
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            )
            if len(candidate.encode("utf-8")) <= MAX_SUMMARY_BYTES:
                summary = candidate
            else:
                notice = {
                    "tool": proposal.name,
                    "dataset_id": str(arguments.dataset_id),
                    "details_in_artifact": True,
                }
                if proposal.name in {"inspect_dataset", "describe_columns"}:
                    notice["column_count"] = len(result.columns)
                elif proposal.name == "aggregate_statistics":
                    notice["group_count"] = len(result.groups)
                else:
                    notice["sample_size"] = result.sample_size
                summary = json.dumps(notice, separators=(",", ":"))
            artifact = await self._artifacts.create_artifact(
                context.principal, arguments.dataset_id, payload
            )
            return ToolResult(
                tool_call_id=proposal.tool_call_id,
                status="completed",
                summary=summary,
                artifact=artifact.reference,
            )
        except StatisticsToolError as error:
            return _failed(proposal, error.failure.code)
        except DatasetNotFoundError:
            return _failed(proposal, "dataset_not_found")
        except CsvValidationError as error:
            return _failed(proposal, error.failure.code)
        except DatasetIntegrityError:
            return _failed(proposal, "dataset_integrity_failed")
        except (DatasetParserBusyError, DatasetParserClosedError):
            return _failed(proposal, "execution_unavailable")
