"""Regression: final-answer fields must not be generated as calculation arguments."""

import asyncio
import json

import httpx
import pytest

from test_statistics_agent import create

FINAL = "finalize_statistics"


def transcript(
    *, invalid=False, mixed=False, unsupported=False, access=True, more_calculations=False
):
    session, dataset, captured, executed, sink, _ = create(access=access)
    original = session._adapter._transport.handler

    def handler(request):
        data = json.loads(request.content)
        definitions = {tool["function"]["name"]: tool["function"] for tool in data["tools"]}
        assert FINAL in definitions, "No distinct final-answer channel is offered to the model"
        assert definitions["mean"]["parameters"]["properties"].keys() == {"dataset_id", "column"}
        assert "status" in definitions[FINAL]["parameters"]["properties"]
        observations = [m for m in data["messages"] if m["role"] == "tool"]
        reference_schema = definitions[FINAL]["parameters"]["properties"]["result_ids"]
        if not observations:
            assert reference_schema["maxItems"] == 0
        else:
            expected = [json.loads(m["content"])["tool_call_id"] for m in observations]
            assert reference_schema["items"]["enum"] == expected
        if more_calculations and len(observations) == 1:
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "finish_reason": "tool_calls",
                            "message": {
                                "tool_calls": [
                                    {
                                        "id": "second-mean",
                                        "type": "function",
                                        "function": {
                                            "name": "mean",
                                            "arguments": json.dumps(
                                                {
                                                    "dataset_id": str(dataset.dataset_id),
                                                    "column": "price",
                                                }
                                            ),
                                        },
                                    }
                                ]
                            },
                        }
                    ]
                },
            )
        if not observations and not unsupported:
            return original(request)
        result_ids = (
            [] if unsupported else [json.loads(observations[-1]["content"])["tool_call_id"]]
        )
        if invalid:
            result_ids = ["00000000-0000-0000-0000-000000000000"]
        proposal = {
            "id": "final-call",
            "type": "function",
            "function": {
                "name": FINAL,
                "arguments": json.dumps(
                    {
                        "status": "unsupported" if unsupported else "completed",
                        "explanation": "No operation available." if unsupported else "Mean is 4.",
                        "result_ids": result_ids,
                        "assumptions": [],
                    }
                ),
            },
        }
        proposals = [proposal]
        if mixed:
            proposals.append(
                {
                    "id": "mixed-call",
                    "type": "function",
                    "function": {
                        "name": "mean",
                        "arguments": json.dumps(
                            {"dataset_id": str(dataset.dataset_id), "column": "price"}
                        ),
                    },
                }
            )
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"tool_calls": proposals}, "finish_reason": "tool_calls"}]
            },
        )

    session._adapter._transport = httpx.MockTransport(handler)
    return session, dataset, captured, executed, sink


def test_native_final_answer_validates_references_without_running_a_statistics_tool():
    session, dataset, _, executed, sink = transcript()
    result = asyncio.run(session.run("Average?", (dataset,)))
    assert result.answer.status == "completed"
    assert result.answer.result_ids == (executed[0].tool_call_id,)
    assert len(executed) == len(result.results) == 1
    assert not result.state.pending_tool_calls
    assert result.state.messages[-1].role == "tool"
    assert sink.events[-1].data["reason"] == "answer_validated"


@pytest.mark.parametrize("options", [dict(invalid=True), dict(mixed=True)])
def test_invalid_or_mixed_final_answer_cannot_bypass_dispatch_rules(options):
    session, dataset, _, executed, sink = transcript(**options)
    result = asyncio.run(session.run("Average?", (dataset,)))
    assert result.answer.status == "incomplete"
    assert len(executed) == 1
    assert sink.events[-1].event_type.value == "tool_call.failed"


def test_unsupported_final_answer_needs_no_fake_computed_result():
    session, dataset, _, executed, _ = transcript(unsupported=True)
    result = asyncio.run(session.run("Unsupported?", (dataset,)))
    assert result.answer.status == "unsupported"
    assert result.results == () and not executed


def test_access_loss_before_finalization_denies_the_result():
    session, dataset, _, executed, _ = transcript(access=[True, True, True, False])
    result = asyncio.run(session.run("Average?", (dataset,)))
    assert result.answer.status == "incomplete"
    assert len(executed) == 1


def test_calculations_can_continue_subject_to_existing_input_limit():
    session, dataset, _, executed, _ = transcript(more_calculations=True)
    result = asyncio.run(session.run("Calculate twice, then explain.", (dataset,)))
    # A second calculation remains allowed; accumulated history then hits the
    # existing conservative 4096-unit request cap before a third model call.
    assert len(executed) == len(result.results) == 2
    assert result.state.completed_attempts == 2
    assert result.answer.status == "incomplete"
    assert result.terminal_reason == "request_input_limit"
