"""Real CrewAI configuration, controlled HTTP responses and typed tool doubles."""

import asyncio
import itertools
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest

from statistics_agent.agent.provider import AdapterError, ModelAdapter
from statistics_agent.agent.statistics import PROMPT, DatasetSummary, StatisticsSession, ToolBinding
from statistics_agent.contracts.agent import AgentContext, ToolDecision, ToolResult
from statistics_agent.contracts.common import ConfigurationReference, Contract
from statistics_agent.core.model_settings import ModelSettings


class MeanArguments(Contract):
    dataset_id: str
    column: str


class Sink:
    def __init__(self):
        self.events = []

    async def append(self, event):
        self.events.append(event)


def create(*, scenario="success", allow=True, access=True, executor_fails=False):
    captured = []
    executed = []
    sink = Sink()
    sequence = itertools.count(1).__next__
    context = AgentContext(
        principal={
            "issuer": "https://identity.example",
            "subject": "private-user",
            "auth_session_ref": uuid4(),
            "roles": ["user"],
            "email_verified": True,
        },
        run_id=uuid4(),
        session_id=uuid4(),
        request_id=uuid4(),
    )
    dataset = DatasetSummary(dataset_id=uuid4(), row_count=3, columns=("price",))

    def handler(request):
        data = json.loads(request.content)
        captured.append(data)
        assert "private-user" not in request.content.decode()
        assert "synthetic-key" not in request.content.decode()
        assert str(context.principal.auth_session_ref) not in request.content.decode()
        if scenario == "provider_failure":
            return httpx.Response(503, text="private provider details")
        tool_results = [m for m in data["messages"] if m["role"] == "tool"]
        if scenario == "unsupported":
            message = {
                "content": json.dumps(
                    {"status": "unsupported", "explanation": "No supported tool."}
                )
            }
            finish = "stop"
        elif scenario == "invented_result":
            message = {
                "content": json.dumps(
                    {"status": "completed", "explanation": "Made up", "result_ids": [str(uuid4())]}
                )
            }
            finish = "stop"
        elif not tool_results:
            arguments = json.dumps({"dataset_id": str(dataset.dataset_id), "column": "price"})
            if scenario == "bad_arguments":
                arguments = "not-json"
            if scenario == "extra_arguments":
                arguments = json.dumps(
                    {"dataset_id": str(dataset.dataset_id), "column": "price", "code": "print(1)"}
                )
            message = {
                "content": None,
                "tool_calls": [
                    {
                        "id": "provider-tool-1",
                        "type": "function",
                        "function": {
                            "name": "unknown" if scenario == "unknown" else "mean",
                            "arguments": arguments,
                        },
                    }
                ],
            }
            if scenario == "missing_id":
                message["tool_calls"][0].pop("id")
            finish = "length" if scenario == "truncated" else "tool_calls"
        else:
            observation = json.loads(tool_results[-1]["content"])
            assert observation["summary"] == "mean=4; valid_count=3"
            assert tool_results[-1]["tool_call_id"] == "provider-tool-1"
            message = {
                "content": json.dumps(
                    {
                        "status": "completed",
                        "explanation": "Mean price is 4.",
                        "result_ids": [observation["tool_call_id"]],
                    }
                )
            }
            finish = "stop"
        return httpx.Response(
            200, json={"choices": [{"message": message, "finish_reason": finish}]}
        )

    class Policy:
        async def evaluate(self, owner, proposal):
            assert owner == context
            assert sink.events[0].event_type.value == "tool_call.requested"
            return ToolDecision(
                tool_call_id=proposal.tool_call_id,
                allowed=allow,
                reason="allowed" if allow else "denied",
            )

    class Executor:
        async def execute(self, owner, proposal):
            assert owner == context
            executed.append(proposal)
            if executor_fails:
                raise RuntimeError("sensitive tool error")
            return ToolResult(
                tool_call_id=proposal.tool_call_id,
                status="completed",
                summary="mean=4; valid_count=3",
                artifact={
                    "artifact_id": uuid4(),
                    "sha256": "0" * 64,
                    "media_type": "application/json",
                },
            )

    checks = []

    async def authorize(owner):
        assert owner == context
        checks.append(owner)
        return access.pop(0) if isinstance(access, list) else access

    settings = ModelSettings(SERVICE_MODEL_API_KEY="synthetic-key", _env_file=None)
    adapter = ModelAdapter(settings, sink, sequence, transport=httpx.MockTransport(handler))
    reference = ConfigurationReference(config_id=uuid4(), version="fixture-v1")
    session = StatisticsSession(
        context=context,
        adapter=adapter,
        toolset=reference,
        policy_version=reference,
        bindings=(ToolBinding("mean", "Mean of a dataset column", MeanArguments),),
        policy=Policy(),
        executor=Executor(),
        authorize=authorize,
        sink=sink,
        sequence=sequence,
        deadline=datetime.now(UTC) + timedelta(minutes=1),
    )
    return session, dataset, captured, executed, sink, checks


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
