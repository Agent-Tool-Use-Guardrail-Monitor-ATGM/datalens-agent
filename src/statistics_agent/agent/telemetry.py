"""Bounded, non-gating proposal observations; no raw names/arguments in telemetry."""

import hashlib
import json
from uuid import UUID

from statistics_agent.contracts.events import EventEnvelope, EventType
from statistics_agent.contracts.run_events import (
    ModelCompleted,
    ModelFailed,
    ToolCompleted,
    ToolFailed,
    ToolFeatures,
    ToolRequested,
    validate_run_event,
)

NUMERICAL = frozenset(
    {"inspect_dataset", "describe_columns", "aggregate_statistics", "correlate_columns"}
)
CONTROLS = frozenset({"finalize_statistics", "request_clarification"})


def observe_proposal(raw, call_id, selected):
    """Observe JSON shape only; actual schema/policy admission follows durable evidence."""
    function = raw.get("function") if isinstance(raw, dict) else None
    name = function.get("name") if isinstance(function, dict) else None
    name = name if isinstance(name, str) and name in NUMERICAL | CONTROLS else None
    arguments = function.get("arguments") if isinstance(function, dict) else None
    digest = hashlib.sha256(
        json.dumps(arguments, ensure_ascii=True, sort_keys=True).encode()
    ).hexdigest()
    values, decoded = None, False
    try:
        if isinstance(arguments, str):
            values, decoded = json.loads(arguments), True
    except (ValueError, TypeError):
        pass
    shape = (
        "not_checked"
        if isinstance(values, dict)
        else ("invalid_schema" if decoded else "invalid_json")
    )
    selection, count, operation, method, missing = "not_checked", 0, None, None, None
    if isinstance(values, dict):
        selection = "absent"
        if "dataset_id" in values:
            try:
                dataset_id = (
                    UUID(values["dataset_id"]) if isinstance(values["dataset_id"], str) else None
                )
                selection = (
                    "invalid"
                    if dataset_id is None
                    else ("selected" if dataset_id in selected else "unselected")
                )
            except ValueError:
                selection = "invalid"
        columns = values.get("columns")
        count = (
            min(len(columns), 16384)
            if isinstance(columns, list)
            else sum(key in values for key in ("column", "x_column", "y_column"))
        )
        operation = values.get("operation")
        operation = (
            operation
            if isinstance(operation, str) and operation in {"count", "sum", "mean", "min", "max"}
            else None
        )
        method = values.get("method")
        method = method if isinstance(method, str) and method in {"pearson", "spearman"} else None
        missing = values.get("missing_policy")
        missing = (
            missing
            if isinstance(missing, str) and missing in {"omit", "error", "pairwise_complete"}
            else None
        )
    return ToolRequested(
        tool_call_id=call_id,
        kind="unknown" if name is None else ("control" if name in CONTROLS else "numerical"),
        name=name,
        arguments_sha256=digest,
        features=ToolFeatures(
            argument_shape=shape,
            dataset_selection=selection,
            column_count=count,
            operation=operation,
            method=method,
            missing_policy=missing,
        ),
    ).model_dump(mode="json", exclude_none=True)


def public_event(event):
    """Strict allowlist bridge for existing observed provider/runner envelopes."""
    data, kind = event.data, event.event_type
    latency = round(max(0, data["duration_ms"])) if "duration_ms" in data else None
    if kind == EventType.TOOL_CALL_REQUESTED:
        payload = ToolRequested.model_validate(
            {key: data[key] for key in ToolRequested.model_fields if key in data}
        )
    elif kind == EventType.TOOL_CALL_COMPLETED:
        payload = ToolCompleted(
            tool_call_id=data["tool_call_id"], policy_evaluation=data.get("policy_evaluation")
        )
    elif kind == EventType.TOOL_CALL_FAILED:
        payload = ToolFailed(
            tool_call_id=data["tool_call_id"],
            status=data["status"],
            reason=data["reason"],
            policy_evaluation=data.get("policy_evaluation"),
        )
    elif kind == EventType.MODEL_CALL_COMPLETED:
        payload = ModelCompleted(
            model_call_id=data["model_call_id"],
            attempt_index=data["attempt_index"],
            latency_ms=latency,
            usage=data["usage"],
        )
    elif kind == EventType.MODEL_CALL_FAILED:
        payload = ModelFailed(
            model_call_id=data["model_call_id"],
            attempt_index=data["attempt_index"],
            latency_ms=latency,
            category=data.get("error_category", data.get("category")),
            outcome_unknown=data.get("outcome_unknown", False),
        )
    else:
        return validate_run_event(event)
    return validate_run_event(
        EventEnvelope.model_validate(
            event.model_dump()
            | {
                "data": payload.model_dump(mode="json", exclude_none=True),
            }
        )
    )
