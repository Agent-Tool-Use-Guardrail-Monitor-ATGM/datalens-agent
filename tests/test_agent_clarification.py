"""Control functions terminate without executing numerical tools or waiting."""

import asyncio
import json

import httpx
import pytest

from statistics_agent.agent.errors import EvidenceDeliveryError
from statistics_agent.contracts.agent import StatisticsAnswer
from support.agent import create


def response(calls):
    return httpx.Response(
        200,
        json={
            "choices": [
                {
                    "finish_reason": "tool_calls",
                    "message": {
                        "tool_calls": [
                            {
                                "id": f"call-{index}",
                                "type": "function",
                                "function": {
                                    "name": name,
                                    "arguments": arguments,
                                },
                            }
                            for index, (name, arguments) in enumerate(calls)
                        ]
                    },
                }
            ]
        },
    )


def test_clarification_is_offered_and_finishes_without_numerical_execution():
    def handler(request, _):
        names = [item["function"]["name"] for item in json.loads(request.content)["tools"]]
        assert names == ["mean", "request_clarification", "finalize_statistics"]
        return response(
            [
                (
                    "request_clarification",
                    json.dumps(
                        {
                            "question": "Which dataset?",
                            "options": [{"key": "first", "text": "First dataset"}],
                        }
                    ),
                )
            ]
        )

    session, dataset, _, executed, sink, _ = create(response_handler=handler)
    result = asyncio.run(session.run("Calculate", (dataset,)))
    assert result.answer.status == result.terminal_reason == "needs_input"
    assert result.answer.clarification.question == "Which dataset?"
    assert result.answer.clarification.options[0].key == "first"
    assert not result.state.pending_tool_calls
    assert not executed and not result.results
    assert sink.events[-1].data["reason"] == "clarification_validated"
    assert "policy_evaluation" not in sink.events[-1].data


@pytest.mark.parametrize(
    "arguments",
    [
        "not-json",
        '{"question":" "}',
        '{"question":"x","code":"run"}',
        json.dumps({"question": "x" * 513}),
        json.dumps({"question": "Which\x00?"}),
        json.dumps({"question": "Which?", "options": [{"key": "a", "text": "A\x00"}]}),
        json.dumps(
            {
                "question": "Which?",
                "options": [
                    {"key": "same", "text": "A"},
                    {"key": "same", "text": "B"},
                ],
            }
        ),
    ],
)
def test_invalid_clarification_fails_without_executing(arguments):
    session, dataset, _, executed, sink, _ = create(
        response_handler=lambda *_: response([("request_clarification", arguments)]),
    )
    result = asyncio.run(session.run("Analyze", (dataset,)))
    assert result.answer.status == "incomplete" and not executed
    assert sink.events[-1].data["reason"] == "invalid_clarification"


@pytest.mark.parametrize("other", ["mean", "finalize_statistics", "request_clarification"])
def test_control_mixed_with_any_other_proposal_executes_nothing(other):
    session, dataset, _, executed, sink, _ = create(
        response_handler=lambda *_: response(
            [
                (other, "{}"),
                ("request_clarification", '{"question":"Which?"}'),
            ]
        )
    )
    result = asyncio.run(session.run("Analyze", (dataset,)))
    assert result.answer.status == "incomplete" and not executed
    outcomes = [event for event in sink.events if event.event_type.value == "tool_call.failed"]
    assert len(outcomes) == 2
    assert all(event.data["reason"] == "mixed_finalization_batch" for event in outcomes)


def test_clarification_rechecks_access():
    session, dataset, _, executed, sink, _ = create(
        access=[True, False],
        response_handler=lambda *_: response([("request_clarification", '{"question":"Which?"}')]),
    )
    result = asyncio.run(session.run("Analyze", (dataset,)))
    assert result.answer.status == "incomplete" and not executed
    assert sink.events[-1].data["reason"] == "access_denied"


def test_successful_computation_survives_invalid_explanation():
    count = 0

    def handler(request, original):
        nonlocal count
        count += 1
        if count == 1:
            return original(request)
        return response(
            [("finalize_statistics", '{"status":"completed","explanation":"invented"}')]
        )

    session, dataset, _, executed, _, _ = create(response_handler=handler)
    result = asyncio.run(session.run("Analyze", (dataset,)))
    assert result.answer.status == "incomplete"
    assert len(executed) == len(result.results) == 1
    assert result.results[0].status == "completed" and result.results[0].artifact is not None
    assert isinstance(result.answer, StatisticsAnswer)


def test_control_cannot_finish_when_evidence_fails():
    session, dataset, _, executed, sink, _ = create(
        response_handler=lambda *_: response([("request_clarification", '{"question":"Which?"}')]),
    )
    original = sink.append

    async def append(event):
        if event.event_type.value == "tool_call.completed":
            raise RuntimeError("private sink failure")
        await original(event)

    sink.append = append
    with pytest.raises(EvidenceDeliveryError, match="Tool evidence delivery failed"):
        asyncio.run(session.run("Analyze", (dataset,)))
    assert not executed
