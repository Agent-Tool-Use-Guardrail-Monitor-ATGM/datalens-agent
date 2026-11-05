"""Opt-in versioned payloads; legacy envelopes and current producers stay intact."""

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from statistics_agent.contracts.events import EventEnvelope, EventType
from statistics_agent.contracts.policy import PolicyEvaluation
from statistics_agent.contracts.run_events import (
    PAYLOADS,
    ModelFailed,
    SessionEnded,
    ToolCompleted,
    ToolFeatures,
    ToolRequested,
    validate_run_event,
)

EXAMPLES = Path(__file__).parent / "fixtures" / "run_events_v2.json"


def test_all_seven_privacy_safe_examples_roundtrip():
    samples = json.loads(EXAMPLES.read_text(encoding="utf-8"))
    assert {item["event_type"] for item in samples} == {item.value for item in EventType}
    assert set(PAYLOADS) == set(EventType)
    for item in samples:
        envelope = EventEnvelope.model_validate(item)
        validated = validate_run_event(envelope)
        assert (
            validate_run_event(EventEnvelope.model_validate_json(validated.model_dump_json()))
            == validated
        )
        assert validated.schema_version == "1.0"
        assert validated.data["payload_version"] == "agent-run-events-v2"


@pytest.mark.parametrize(
    "field", ["raw_arguments", "prompt", "cells", "access_token", "mock_label"]
)
def test_payloads_forbid_private_fields(field):
    for sample in json.loads(EXAMPLES.read_text(encoding="utf-8")):
        sample["data"][field] = "private"
        with pytest.raises(ValidationError):
            validate_run_event(EventEnvelope.model_validate(sample))


@pytest.mark.parametrize(
    "kind,name",
    [
        ("unknown", "inspect_dataset"),
        ("numerical", None),
        ("control", "inspect_dataset"),
        ("numerical", "request_clarification"),
        ("unknown", "arbitrary-private-name"),
    ],
)
def test_unknown_names_cannot_escape_in_tool_payload(kind, name):
    with pytest.raises(ValidationError):
        ToolRequested(
            tool_call_id=uuid4(),
            kind=kind,
            name=name,
            arguments_sha256="0" * 64,
            features=ToolFeatures(argument_shape="not_checked", dataset_selection="not_checked"),
        )


def test_controls_and_unknown_calls_have_no_numerical_name():
    assert (
        ToolFeatures(
            argument_shape="invalid_schema", dataset_selection="selected", column_count=129
        ).column_count
        == 129
    )
    for kind, name in [("unknown", None), ("control", "request_clarification")]:
        result = ToolRequested(
            tool_call_id=uuid4(),
            kind=kind,
            name=name,
            arguments_sha256="0" * 64,
            features=ToolFeatures(argument_shape="not_checked", dataset_selection="absent"),
        )
        assert result.kind == kind
    with pytest.raises(ValidationError):
        ToolFeatures(argument_shape="valid", dataset_selection="selected", column_count=True)


def test_policy_identity_and_terminal_count_cannot_contradict_outcome():
    evaluation = PolicyEvaluation(tool_call_id=uuid4(), status="not_evaluated")
    with pytest.raises(ValidationError):
        ToolCompleted(tool_call_id=uuid4(), policy_evaluation=evaluation)
    with pytest.raises(ValidationError):
        ToolCompleted(tool_call_id=evaluation.tool_call_id, policy_evaluation=evaluation)
    with pytest.raises(ValidationError):
        SessionEnded(outcome="completed", verified_result_count=0)
    assert SessionEnded(outcome="needs_input", verified_result_count=0).outcome == "needs_input"
    with pytest.raises(ValidationError):
        ModelFailed(model_call_id=uuid4(), attempt_index=1, latency_ms=-1, category="timeout")


def test_new_payload_version_is_explicit_and_does_not_reinterpret_legacy_events():
    legacy = EventEnvelope(
        event_id=uuid4(),
        event_type="session.ended",
        occurred_at=datetime.now(UTC),
        run_id=uuid4(),
        session_id=uuid4(),
        sequence=1,
        data={"outcome": "incomplete", "verified_result_count": 0},
    )
    assert EventEnvelope.model_validate_json(legacy.model_dump_json()) == legacy
    with pytest.raises(ValueError, match="Explicit"):
        validate_run_event(legacy)
