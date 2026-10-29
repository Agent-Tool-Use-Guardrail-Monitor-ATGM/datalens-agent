"""Offline provider/real BaseLLM boundary checks; synthetic credentials only."""

import asyncio
import itertools
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest

from statistics_agent.agent.provider import AdapterError, EvidenceDeliveryError, ModelAdapter
from statistics_agent.contracts.agent import (
    AgentContext,
    AgentMessage,
    ModelAttempt,
    ProviderFailure,
    ProviderRequest,
)
from statistics_agent.core.model_settings import ModelSettings


class Sink:
    def __init__(self):
        self.events = []

    async def append(self, event):
        self.events.append(event)


@pytest.fixture
def setup():
    settings = ModelSettings(SERVICE_MODEL_API_KEY="synthetic-key", _env_file=None)
    sink = Sink()
    attempt = ModelAttempt(
        run_id=uuid4(),
        session_id=uuid4(),
        request_id=uuid4(),
        model_call_id=uuid4(),
        attempt_index=1,
    )
    request = ProviderRequest(
        attempt=attempt,
        profile=settings.resolve().reference,
        messages=(AgentMessage(role="user", content="private question"),),
        max_output_tokens=256,
        deadline=datetime.now(UTC) + timedelta(seconds=60),
    )
    return settings, sink, request


def body(**overrides):
    return {
        "id": "provider-1",
        "model": "returned-model",
        "choices": [{"message": {"content": "The mean is 4."}, "finish_reason": "stop"}],
        **overrides,
    }


def adapter_for(setup, handler):
    settings, sink, _ = setup
    return ModelAdapter(
        settings, sink, itertools.count(1).__next__, transport=httpx.MockTransport(handler)
    )


def test_attempt_usage_and_redacted_evidence(setup):
    calls = []

    def handler(request):
        calls.append(request)
        assert request.headers["authorization"] == "Bearer synthetic-key"
        assert request.url == "https://api.groq.com/openai/v1/chat/completions"
        return httpx.Response(
            200,
            json=body(
                usage={
                    "prompt_tokens": 20,
                    "completion_tokens": 10,
                    "prompt_tokens_details": {"cached_tokens": 4},
                    "completion_tokens_details": {"reasoning_tokens": 3},
                }
            ),
        )

    result = asyncio.run(adapter_for(setup, handler).complete(setup[2]))
    assert result.attempt == setup[2].attempt
    assert result.usage.input_tokens == 20
    assert result.usage.output_tokens == 10
    assert len(calls) == 1
    wire = setup[1].events[0].model_dump_json()
    assert all(value not in wire for value in ["synthetic-key", "private question", "The mean"])
    assert setup[1].events[0].data["estimated_cost_usd"] is None


@pytest.mark.parametrize("usage,expected", [(None, None), ({"prompt_tokens": 0}, 0)])
def test_missing_usage_is_not_zero(setup, usage, expected):
    result = asyncio.run(
        adapter_for(setup, lambda _: httpx.Response(200, json=body(usage=usage))).complete(setup[2])
    )
    assert result.usage.input_tokens == expected


def test_original_malformed_unknown_proposal_before_framework_filtering(setup):
    raw = {
        "id": "external-id",
        "type": "function",
        "function": {"name": "unknown_tool", "arguments": '{"bad": invalid synthetic-key'},
    }
    answer = body(
        choices=[{"message": {"content": None, "tool_calls": [raw]}, "finish_reason": "tool_calls"}]
    )
    result = asyncio.run(
        adapter_for(setup, lambda _: httpx.Response(200, json=answer)).complete(
            setup[2], tools=[{"type": "function", "function": {"name": "mean"}}]
        )
    )
    proposal = result.proposals[0]
    assert proposal.name == "unknown_tool"
    assert proposal.raw_arguments == raw["function"]["arguments"]
    assert proposal.provider_tool_call_id == "external-id"
    events = setup[1].events
    assert [e.event_type.value for e in events] == ["tool_call.requested", "model_call.completed"]
    assert events[0].data["tool_call_id"] == str(proposal.tool_call_id)
    assert "synthetic-key" not in json.dumps([e.model_dump(mode="json") for e in events])


@pytest.mark.parametrize(
    "status,category",
    [
        (401, "authentication"),
        (403, "authentication"),
        (429, "rate_limit"),
        (500, "provider_error"),
        (302, "provider_error"),
    ],
)
def test_provider_errors_are_safe_and_not_retried(setup, status, category):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            status,
            text="synthetic-key secret provider error",
            headers={"retry-after": "2", "location": "https://untrusted.invalid"},
        )

    result = asyncio.run(adapter_for(setup, handler).complete(setup[2]))
    assert result.category == category
    assert result.retry_after_seconds == 2
    assert len(calls) == 1
    assert "secret provider" not in setup[1].events[0].model_dump_json()


@pytest.mark.parametrize(
    "answer",
    [
        {},
        {"choices": []},
        body(usage={"prompt_tokens": -1}),
        body(choices=[{"message": {"tool_calls": [None, None]}}]),
        body(choices=[{"message": "invalid"}]),
    ],
)
def test_invalid_response_and_capture_all_proposals(setup, answer):
    result = asyncio.run(
        adapter_for(setup, lambda _: httpx.Response(200, json=answer)).complete(setup[2])
    )
    assert isinstance(result, ProviderFailure)
    assert result.category == "invalid_response"
    assert setup[1].events[-1].event_type.value == "model_call.failed"
    if answer == body(choices=[{"message": {"tool_calls": [None, None]}}]):
        assert len(setup[1].events) == 3


@pytest.mark.parametrize("error", [httpx.ReadTimeout, httpx.ConnectError])
def test_transport_errors_safe(setup, error):
    def handler(request):
        raise error("synthetic-key", request=request)

    result = asyncio.run(adapter_for(setup, handler).complete(setup[2]))
    assert result.outcome_unknown
    assert "synthetic-key" not in result.model_dump_json()


def test_sink_failure_does_not_return_dispatchable_response(setup):
    class BrokenSink:
        async def append(self, event):
            raise ValueError("secret sink error")

    settings, _, request = setup
    adapter = ModelAdapter(
        settings,
        BrokenSink(),
        itertools.count(1).__next__,
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body())),
    )
    with pytest.raises(EvidenceDeliveryError, match="evidence delivery failed") as error:
        asyncio.run(adapter.complete(request))
    assert error.value.__context__ is None


def test_actual_crewai_base_boundary_does_not_invoke_available_functions(setup):

    from crewai import BaseLLM

    from statistics_agent.agent.crewai_llm import StatisticsLLM, ToolDispatchRequired

    context = AgentContext(
        principal={
            "issuer": "https://identity.example",
            "subject": "user",
            "auth_session_ref": uuid4(),
            "roles": ["user"],
            "email_verified": True,
        },
        run_id=setup[2].attempt.run_id,
        session_id=setup[2].attempt.session_id,
        request_id=setup[2].attempt.request_id,
    )
    raw = {"id": "p1", "function": {"name": "unknown", "arguments": "bad-json"}}
    adapter = adapter_for(
        setup,
        lambda _: httpx.Response(
            200,
            json=body(choices=[{"message": {"tool_calls": [raw]}, "finish_reason": "tool_calls"}]),
        ),
    )
    llm = StatisticsLLM(adapter, context, setup[2].deadline)
    assert isinstance(llm, BaseLLM)
    assert not getattr(StatisticsLLM.call, "_crewai_rate_limit_wrapped", False)
    assert not getattr(StatisticsLLM.acall, "_crewai_rate_limit_wrapped", False)

    def forbidden():
        pytest.fail("Framework bypassed policy")

    with pytest.raises(ToolDispatchRequired) as caught:
        llm.call("question", available_functions={"unknown": forbidden})
    assert caught.value.response.proposals[0].raw_arguments == "bad-json"


def test_profile_mismatch_rejected_before_network(setup):
    bad = setup[2].model_copy(
        update={"profile": setup[2].profile.model_copy(update={"config_id": uuid4()})}
    )
    with pytest.raises(AdapterError, match="reference mismatch"):
        asyncio.run(adapter_for(setup, lambda _: pytest.fail("network called")).complete(bad))


@pytest.mark.parametrize(
    "answer",
    [
        body(usage=0),
        body(usage={"prompt_tokens_details": 3}),
        body(choices=[{"message": {"content": 0}}]),
        body(choices=[{"message": {"tool_calls": False}}]),
    ],
)
def test_wrong_scalar_types_are_not_converted_to_empty_success(setup, answer):
    result = asyncio.run(
        adapter_for(setup, lambda _: httpx.Response(200, json=answer)).complete(setup[2])
    )
    assert result.category == "invalid_response"


def test_oversized_response_is_rejected(setup):
    result = asyncio.run(
        adapter_for(setup, lambda _: httpx.Response(200, content=b"x" * 1_048_577)).complete(
            setup[2]
        )
    )
    assert result.category == "invalid_response"


def test_azure_uses_resolved_endpoint_and_deployment(setup):
    settings = ModelSettings(
        SERVICE_MODEL_API_KEY="synthetic-key",
        LLM_PROFILE="azure_student",
        LLM_AZURE_RESOURCE="test-resource",
        LLM_AZURE_DEPLOYMENT="test-deployment",
        _env_file=None,
    )
    request = setup[2].model_copy(update={"profile": settings.resolve().reference})

    def handler(incoming):
        assert (
            str(incoming.url) == "https://test-resource.openai.azure.com/openai/v1/chat/completions"
        )
        assert json.loads(incoming.content)["model"] == "test-deployment"
        return httpx.Response(200, json=body())

    adapter = adapter_for((settings, setup[1], request), handler)
    assert asyncio.run(adapter.complete(request)).content == "The mean is 4."
