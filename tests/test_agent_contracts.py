"""Controlled boundaries for later adapters; no provider or infrastructure access."""

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from statistics_agent.contracts.agent import (
    AgentContext,
    AgentMessage,
    AgentState,
    AgentVersions,
    EventSink,
    ModelAttempt,
    Provider,
    ProviderFailure,
    ProviderRequest,
    ProviderResponse,
    TokenUsage,
    ToolDecision,
    ToolExecutor,
    ToolPolicy,
    ToolProposal,
    ToolResult,
)
from statistics_agent.contracts.events import EventEnvelope


@pytest.fixture
def context():
    return AgentContext.model_validate(
        {
            "principal": {
                "issuer": "https://identity.example/realms/datalens",
                "subject": "fixture-user",
                "auth_session_ref": str(uuid4()),
                "roles": ["user"],
                "email_verified": True,
            },
            "run_id": str(uuid4()),
            "session_id": str(uuid4()),
            "request_id": str(uuid4()),
        }
    )


@pytest.fixture
def provider_request(context):
    return ProviderRequest(
        attempt=ModelAttempt(
            run_id=context.run_id,
            session_id=context.session_id,
            request_id=context.request_id,
            model_call_id=uuid4(),
            attempt_index=1,
        ),
        profile={"config_id": uuid4(), "version": "fixture-v1"},
        messages=(AgentMessage(role="user", content="Compute the mean"),),
        max_output_tokens=512,
        deadline=datetime(2030, 1, 1, tzinfo=UTC),
    )


@pytest.mark.parametrize(
    "field", ["access_token", "refresh_token", "cookie", "api_key", "password"]
)
@pytest.mark.parametrize("nested", [False, True])
def test_authorized_context_rejects_credential_fields(context, field, nested):
    data = context.model_dump(mode="json")
    target = data["principal"] if nested else data
    target[field] = "synthetic-secret"
    with pytest.raises(ValidationError) as error:
        AgentContext.model_validate(data)
    assert "synthetic-secret" not in str(error.value)


def test_unknown_usage_remains_distinct_from_reported_zero():
    missing = TokenUsage()
    zero = TokenUsage(source="provider", input_tokens=0, output_tokens=0)
    assert missing.input_tokens is None
    assert zero.input_tokens == 0
    assert TokenUsage.model_validate_json(missing.model_dump_json()) == missing
    assert TokenUsage.model_validate_json(zero.model_dump_json()) == zero
    partial = TokenUsage(source="provider", input_tokens=12, cached_input_tokens=4)
    assert partial.input_tokens == 12  # Cached tokens are a subset, not added.
    assert partial.output_tokens is None


@pytest.mark.parametrize(
    "data",
    [
        {"input_tokens": 0},
        {"source": "provider"},
        {"source": "provider", "input_tokens": -1},
        {"source": "provider", "input_tokens": True},
        {"source": "provider", "input_tokens": "12"},
        {"source": "provider", "input_tokens": 2, "cached_input_tokens": 3},
        {"source": "provider", "output_tokens": 2, "reasoning_tokens": 3},
    ],
)
def test_invalid_usage_is_rejected(data):
    with pytest.raises(ValidationError):
        TokenUsage.model_validate(data)


def test_request_roundtrip_preserves_correlation_and_excludes_identity(provider_request):
    assert (
        ProviderRequest.model_validate_json(provider_request.model_dump_json()) == provider_request
    )
    assert "principal" not in provider_request.model_dump()
    with pytest.raises(ValidationError):
        ProviderRequest.model_validate({**provider_request.model_dump(), "api_key": "secret"})


def test_unknown_tool_and_malformed_arguments_are_preserved(provider_request):
    proposal = ToolProposal(
        tool_call_id=uuid4(),
        provider_tool_call_id="provider-call-1",
        name="unknown tool!",
        raw_arguments=' {"column": broken ',
    )
    response = ProviderResponse(
        attempt=provider_request.attempt,
        proposals=(proposal,),
        finish_reason="tool_calls",
    )
    restored = ProviderResponse.model_validate_json(response.model_dump_json())
    assert restored.proposals == (proposal,)
    assert restored.attempt == provider_request.attempt
    assert proposal.raw_arguments not in repr(response)
    # Empty proposals also survive until application validation.
    assert ToolProposal(tool_call_id=uuid4(), name="", raw_arguments="").name == ""


def test_state_is_isolated_and_bounded(context, provider_request):
    versions = AgentVersions(
        **{key: provider_request.profile for key in ("provider", "prompt", "toolset", "policy")}
    )
    messages = [AgentMessage(role="user", content="private question")]
    first = AgentState(
        context=context,
        versions=versions,
        messages=messages,
        remaining_steps=8,
        remaining_input_tokens=4096,
        remaining_output_tokens=1024,
        deadline=provider_request.deadline,
    )
    messages.clear()
    assert len(first.messages) == 1
    assert AgentState.model_validate_json(first.model_dump_json()) == first
    assert "private question" not in repr(first)
    with pytest.raises(ValidationError):
        first.remaining_steps = 0
    with pytest.raises(ValidationError):
        AgentState.model_validate({**first.model_dump(), "messages": first.messages * 129})
    with pytest.raises(ValidationError):
        AgentMessage(role="user", content="x" * 16_385)


@pytest.mark.parametrize(
    "data",
    [
        {"role": "tool", "content": "value"},
        {"role": "user", "content": "value", "tool_call_id": uuid4()},
    ],
)
def test_tool_result_messages_require_matching_kind(data):
    with pytest.raises(ValidationError):
        AgentMessage.model_validate(data)


def test_failure_does_not_accept_raw_exception_body(provider_request):
    failure = ProviderFailure(
        attempt=provider_request.attempt, category="timeout", outcome_unknown=True
    )
    assert ProviderFailure.model_validate_json(failure.model_dump_json()) == failure
    with pytest.raises(ValidationError):
        ProviderFailure.model_validate({**failure.model_dump(), "body": "secret"})
    with pytest.raises(ValidationError):
        ToolDecision(tool_call_id=uuid4(), allowed=True, reason="denied")


def test_controlled_provider_policy_tool_and_event_handoffs(context, provider_request):
    """Fixture ordering is not a production dispatcher or durability guarantee."""
    proposal = ToolProposal(tool_call_id=uuid4(), name="mean", raw_arguments='{"column":"x"}')
    order = []

    class FakeProvider:
        async def complete(self, value):
            return ProviderResponse(
                attempt=value.attempt, proposals=(proposal,), finish_reason="tool_calls"
            )

    class FakePolicy:
        async def evaluate(self, owner, value):
            assert owner == context
            order.append("policy")
            return ToolDecision(tool_call_id=value.tool_call_id, allowed=True, reason="allowed")

    class FakeTool:
        async def execute(self, owner, value):
            assert owner == context
            order.append("execute")
            return ToolResult(tool_call_id=value.tool_call_id, status="completed")

    class FakeSink:
        async def append(self, event):
            assert event.run_id == context.run_id
            assert event.data["tool_call_id"] == str(proposal.tool_call_id)
            order.append("event")

    async def exercise():
        provider: Provider = FakeProvider()
        policy: ToolPolicy = FakePolicy()
        executor: ToolExecutor = FakeTool()
        sink: EventSink = FakeSink()
        response = await provider.complete(provider_request)
        assert isinstance(response, ProviderResponse)
        await sink.append(
            EventEnvelope(
                event_id=uuid4(),
                event_type="tool_call.requested",
                occurred_at=datetime.now(UTC),
                run_id=context.run_id,
                session_id=context.session_id,
                request_id=context.request_id,
                sequence=1,
                data={"tool_call_id": str(response.proposals[0].tool_call_id)},
            )
        )
        decision = await policy.evaluate(context, response.proposals[0])
        assert decision.allowed
        result = await executor.execute(context, response.proposals[0])
        assert result.tool_call_id == proposal.tool_call_id

    asyncio.run(exercise())
    assert order == ["event", "policy", "execute"]
