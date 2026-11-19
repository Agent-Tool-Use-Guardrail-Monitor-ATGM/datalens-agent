"""Real CrewAI configuration, controlled HTTP responses and typed tool doubles."""

import asyncio

import pytest

from statistics_agent.agent.errors import AdapterError
from statistics_agent.agent.prompts import PROMPT
from support.agent import create


def test_success_uses_computed_result_and_real_crewai_agent():
    session, dataset, captured, executed, sink, checks = create()
    result = asyncio.run(session.run("Average price?", (dataset,)))
    assert result.answer.status == "completed"
    assert result.answer.result_ids == (executed[0].tool_call_id,)
    assert result.state.versions.prompt == PROMPT.reference
    assert result.state.completed_attempts == 2
    assert result.state.artifacts == (result.results[0].artifact,)
    assert not result.state.pending_tool_calls
    assert len(captured) == 2 and len(checks) == 3
    assert not session.agent.allow_delegation and not session.agent.allow_code_execution
    assert session.agent.max_retry_limit == 0
    assert session.agent.tools == []
    assert [e.sequence for e in sink.events] == [1, 2, 3, 4]
    assert [e.event_type.value for e in sink.events] == [
        "tool_call.requested",
        "model_call.completed",
        "tool_call.completed",
        "model_call.completed",
    ]


@pytest.mark.parametrize(
    "options",
    [
        dict(scenario="bad_arguments"),
        dict(scenario="unknown"),
        dict(allow=False),
        dict(access=False),
        dict(scenario="provider_failure"),
        dict(scenario="invented_result"),
        dict(scenario="missing_id"),
        dict(scenario="truncated"),
        dict(scenario="extra_arguments"),
        dict(access=[True, False]),
    ],
)
def test_denials_failures_and_invented_results_do_not_execute(options):
    session, dataset, _, executed, _, _ = create(**options)
    result = asyncio.run(session.run("Analyze", (dataset,)))
    assert result.answer.status == "incomplete"
    assert executed == []


def test_tool_error_returns_explicit_incomplete_without_details():
    session, dataset, captured, executed, _, _ = create(executor_fails=True)
    result = asyncio.run(session.run("Analyze", (dataset,)))
    assert result.answer.status == "incomplete"
    assert len(executed) == 1 and len(captured) == 1
    assert "sensitive tool error" not in result.model_dump_json()


def test_sessions_are_single_use_and_state_does_not_leak():
    first, dataset, _, _, _, _ = create(scenario="unsupported")
    second, other_dataset, _, _, _, _ = create(scenario="unsupported")
    a = asyncio.run(first.run("private question one", (dataset,)))
    b = asyncio.run(second.run("private question two", (other_dataset,)))
    assert a.answer.status == b.answer.status == "unsupported"
    assert a.state.context.run_id != b.state.context.run_id
    assert "private question one" not in b.model_dump_json()
    assert str(dataset.dataset_id) not in b.model_dump_json()
    assert first.agent is not second.agent
    with pytest.raises(AdapterError, match="single use"):
        asyncio.run(first.run("again", (dataset,)))
