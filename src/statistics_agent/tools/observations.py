"""Bounded deterministic observations shared by live tools and verified history."""

import json

MAX_SUMMARY_BYTES = 1024


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
