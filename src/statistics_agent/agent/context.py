"""Owned context preparation without CSV reads, model calls or budget increases."""

import json
from uuid import uuid5

from pydantic import ValidationError

from statistics_agent.agent.errors import AdapterError
from statistics_agent.contracts.agent import AgentMessage, DatasetSummary, ToolResult
from statistics_agent.contracts.context import HistoricalResult, PreparedContext
from statistics_agent.tools.observations import verified_summary

MAX_HISTORY = 12


class ContextUnavailableError(RuntimeError):
    def __init__(self):
        super().__init__("Conversation context is unavailable")


class ContextLimitError(RuntimeError):
    def __init__(self):
        super().__init__("Required conversation context exceeds the model budget")


async def build_context(*, context, versions, conversations, datasets, artifacts, reserve, budget):
    """Use only with trusted services/reservation callback; these values do not authenticate."""
    try:
        admitted = await conversations.get_run(context.principal, context.run_id)
        if admitted.run.versions != versions or admitted.run.lifecycle not in {"queued", "running"}:
            raise ValueError
        selected = []
        for snapshot in admitted.datasets:
            current = await datasets.get_metadata(context.principal, snapshot.dataset_id)
            if current != snapshot:
                raise ValueError
            selected.append(
                DatasetSummary(
                    dataset_id=current.dataset_id,
                    row_count=current.row_count,
                    columns=tuple(column.name for column in current.columns),
                )
            )
        historical = []
        for source in admitted.submission.prior_results:
            previous = await conversations.get_run(context.principal, source.run_id)
            if (
                previous.run.conversation_id != admitted.run.conversation_id
                or source not in previous.run.results
            ):
                raise ValueError
            current = await datasets.get_metadata(context.principal, source.dataset.dataset_id)
            artifact = await artifacts.get_artifact(context.principal, source.artifact.artifact_id)
            if (
                current.sha256 != source.dataset.sha256
                or artifact.reference != source.artifact
                or artifact.dataset_id != source.dataset.dataset_id
            ):
                raise ValueError
            summary = verified_summary(
                artifact.payload,
                tool_call_id=source.tool_call_id,
                dataset_id=source.dataset.dataset_id,
                sha256=source.dataset.sha256,
            )
            historical.append(
                HistoricalResult(
                    source=source,
                    result=ToolResult(
                        tool_call_id=uuid5(context.run_id, str(source.tool_call_id)),
                        status="completed",
                        artifact=source.artifact,
                        summary=summary,
                    ),
                )
            )
        history = await conversations.recent_messages(
            context.principal,
            admitted.run.conversation_id,
            before_run_id=context.run_id,
            limit=MAX_HISTORY + 1,
        )
    except Exception:
        # No rejected private value, driver detail or artifact body is returned.
        pass
    else:
        omitted = len(history) > MAX_HISTORY
        history = list(history[-MAX_HISTORY:])
        while True:
            try:
                content = json.dumps(
                    {
                        "question": admitted.submission.question,
                        "datasets": [item.model_dump(mode="json") for item in selected],
                        "verified_prior_results": [
                            {
                                "result_id": str(item.result.tool_call_id),
                                "dataset_id": str(item.source.dataset.dataset_id),
                                "summary": json.loads(item.result.summary),
                            }
                            for item in historical
                        ],
                        "unverified_history": [
                            {"role": item.role, "content": item.content} for item in history
                        ],
                        "history_omitted": omitted,
                    },
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                )
                messages = (
                    AgentMessage(role="system", content=admitted.configuration.prompt.instructions),
                    AgentMessage(role="user", content=content),
                )
                reservation = reserve(
                    messages, tuple(item.result.tool_call_id for item in historical)
                )
                if reservation <= budget:
                    return PreparedContext(
                        context=context,
                        versions=versions,
                        conversation_id=admitted.run.conversation_id,
                        question=admitted.submission.question,
                        datasets=tuple(selected),
                        messages=messages,
                        historical=tuple(historical),
                        history_omitted=omitted,
                        reservation=reservation,
                    )
            except (ValidationError, AdapterError):
                pass
            if not history:
                raise ContextLimitError
            history.pop(0)
            omitted = True
    raise ContextUnavailableError
