"""Bounded deterministic observations shared by live tools and verified history."""

import json

from statistics_agent.contracts.statistics import (
    AggregateStatisticsResult,
    CorrelationResult,
    DatasetInspection,
    DescribeColumnsResult,
)

RESULTS = {
    "inspect_dataset": DatasetInspection,
    "describe_columns": DescribeColumnsResult,
    "aggregate_statistics": AggregateStatisticsResult,
    "correlate_columns": CorrelationResult,
}

MAX_SUMMARY_BYTES = 1024


def verified_summary(payload, *, tool_call_id, dataset_id, sha256):
    """Validate stored computation identity and typed result before quoting it."""
    if (
        set(payload) != {"schema_version", "tool", "tool_call_id", "dataset_sha256", "result"}
        or payload["schema_version"] != "statistics-result-v1"
        or payload["tool_call_id"] != str(tool_call_id)
        or payload["dataset_sha256"] != sha256
    ):
        raise ValueError("Stored computation identity mismatch")
    result = RESULTS[payload["tool"]].model_validate(payload["result"])
    if result.dataset_id != dataset_id:
        raise ValueError("Stored computation dataset mismatch")
    return statistics_summary(payload["tool"], result)


def statistics_summary(tool, result) -> str:
    """Keep every numerical fact or return an explicit complete-artifact notice."""
    values = result.model_dump(mode="json")
    summary = {
        key: value
        for key, value in values.items()
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
        {"result": summary}, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    )
    if len(candidate.encode("utf-8")) <= MAX_SUMMARY_BYTES:
        return candidate
    notice = {"tool": tool, "dataset_id": str(result.dataset_id), "details_in_artifact": True}
    if tool in {"inspect_dataset", "describe_columns"}:
        notice["column_count"] = len(result.columns)
    elif tool == "aggregate_statistics":
        notice["group_count"] = len(result.groups)
    else:
        notice["sample_size"] = result.sample_size
    return json.dumps(notice, separators=(",", ":"))
