"""Synthetic provider contract check; never a public agent endpoint or production tool."""

import argparse
import asyncio
import itertools
import json
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from importlib.metadata import version
from statistics import mean
from uuid import uuid4

import httpx
from pydantic import ValidationError

from statistics_agent.agent.provider import ModelAdapter
from statistics_agent.agent.statistics import (
    FINALIZE,
    DatasetSummary,
    StatisticsAnswer,
    StatisticsSession,
    ToolBinding,
)
from statistics_agent.contracts.agent import AgentContext, ProviderFailure, ToolDecision, ToolResult
from statistics_agent.contracts.auth import AuthorizedPrincipal
from statistics_agent.contracts.common import ConfigurationReference, Contract
from statistics_agent.core.model_settings import ModelSettings, load_model_settings

VALUES = (2.0, 4.0, 6.0)


class MeanArguments(Contract):
    dataset_id: str
    column: str


async def check_provider(
    settings: ModelSettings,
    principal: AuthorizedPrincipal,
    authorize: Callable[[AgentContext], Awaitable[bool]],
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict:
    """Caller owns authentication. Only synthetic fixture data reaches the model."""
    # Keep the opt-in probe small and never silently retry an API failure.
    settings = settings.model_copy(update={"max_steps": 3, "max_retries": 0})
    profile = settings.resolve()
    events, responses, computed = [], [], []
    sequence = itertools.count(1).__next__
    dataset = DatasetSummary(dataset_id=uuid4(), row_count=len(VALUES), columns=("price",))
    context = AgentContext(
        principal=principal, run_id=uuid4(), session_id=uuid4(), request_id=uuid4()
    )

    class Sink:
        async def append(self, event):
            events.append(event)

    class ObservedAdapter(ModelAdapter):
        async def complete(self, request, *, tools=None):
            response = await super().complete(request, tools=tools)
            responses.append(response)
            return response

    class Policy:
        async def evaluate(self, owner, proposal):
            args = MeanArguments.model_validate_json(proposal.raw_arguments, strict=True)
            allowed = (
                owner == context
                and proposal.name == "verification_mean"
                and args.dataset_id == str(dataset.dataset_id)
                and args.column == "price"
            )
            return ToolDecision(
                tool_call_id=proposal.tool_call_id,
                allowed=allowed,
                reason="allowed" if allowed else "denied",
            )

    class Executor:
        async def execute(self, owner, proposal):
            # Recheck fixture scope; this is arithmetic on constants, not dataset loading.
            if not (await Policy().evaluate(owner, proposal)).allowed:
                raise ValueError("Verification tool is outside fixture scope")
            value = mean(VALUES)
            computed.append(value)
            return ToolResult(
                tool_call_id=proposal.tool_call_id,
                status="completed",
                summary=json.dumps({"mean": value, "count": len(VALUES)}),
            )

    sink = Sink()
    adapter = ObservedAdapter(settings, sink, sequence, transport=transport)
    reference = ConfigurationReference(config_id=uuid4(), version="verification-fixture-v1")
    session = StatisticsSession(
        context=context,
        adapter=adapter,
        toolset=reference,
        policy_version=reference,
        bindings=(
            ToolBinding(
                "verification_mean",
                "Compute the mean of the price column in this synthetic dataset.",
                MeanArguments,
            ),
        ),
        policy=Policy(),
        executor=Executor(),
        authorize=authorize,
        sink=sink,
        sequence=sequence,
        deadline=datetime.now(UTC) + timedelta(seconds=90),
    )
    result = await session.run(
        "Calculate the mean price using verification_mean, then return the answer JSON.", (dataset,)
    )
    models = []
    for response in responses:
        if not isinstance(response, ProviderFailure) and response.actual_model:
            candidate = response.actual_model
            # Do not publish arbitrary provider strings or echoed keys.
            if (
                re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9/._:-]{0,127}", candidate)
                and settings.api_key.get_secret_value() not in candidate
            ):
                models.append(candidate)
    passed = (
        result.answer.status == "completed"
        and computed == [4.0]
        and len(result.answer.result_ids) == 1
        and len(responses) >= 2
    )
    final_checks = []
    for response in responses:
        if isinstance(response, ProviderFailure):
            continue
        for proposal in response.proposals:
            if proposal.name != FINALIZE:
                continue
            try:
                answer = StatisticsAnswer.model_validate_json(proposal.raw_arguments)
                known = {r.tool_call_id for r in result.results if r.status == "completed"}
                final_checks.append(
                    {
                        "schema_valid": True,
                        "references_match": set(answer.result_ids) <= known,
                        "uses_dataset_id": dataset.dataset_id in answer.result_ids,
                    }
                )
            except ValidationError as error:
                final_checks.append(
                    {
                        "schema_valid": False,
                        "error_types": sorted(
                            {e["type"] for e in error.errors(include_input=False)}
                        ),
                    }
                )
    return {
        "checked_at": datetime.now(UTC).isoformat(),
        "status": "passed" if passed else "failed",
        "terminal_reason": result.terminal_reason,
        "profile": profile.name,
        "requested_model": profile.model,
        "reported_models": sorted(set(models)),
        "reported_model_snapshot_version": None,
        "expected_mean": 4.0,
        "computed_values": computed,
        "attempts": len(responses),
        "event_count": len(events),
        "tool_outcomes": [
            e.data.get("reason")
            for e in events
            if e.event_type.value in {"tool_call.completed", "tool_call.failed"}
        ],
        "final_answer_checks": final_checks,
        "unrecognized_name_classes": [
            p.name
            if p.name
            in {
                "functions.finalize_statistics",
                "StatisticsAnswer",
                "final_answer",
                "finalize",
                "finish",
                "finish_analysis",
            }
            else "other"
            for r in responses
            if not isinstance(r, ProviderFailure)
            for p in r.proposals
            if p.name not in {FINALIZE, "verification_mean"}
        ],
        "http_statuses": [
            e.data.get("http_status")
            for e in events
            if e.event_type.value.startswith("model_call.")
        ],
        "sequence_contiguous": [e.sequence for e in events] == list(range(1, len(events) + 1)),
        "usage": [r.usage.model_dump() for r in responses if not isinstance(r, ProviderFailure)],
        "failure_categories": [r.category for r in responses if isinstance(r, ProviderFailure)],
        "versions": {p: version(p) for p in ("crewai", "pydantic", "httpx", "fastapi")},
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Opt-in, synthetic model/tool contract verification"
    )
    parser.add_argument(
        "--live", action="store_true", help="Allow real inference with synthetic data"
    )
    parser.add_argument("--env-file", default=".env")
    args = parser.parse_args()
    if not args.live:
        parser.error("Pass --live explicitly; ordinary test runs must not use provider credentials")
    try:
        settings = load_model_settings(env_file=args.env_file)
        # This CLI verifies provider compatibility, not user authentication.
        principal = AuthorizedPrincipal(
            issuer="https://verification.example.invalid",
            subject="synthetic-verification-user",
            auth_session_ref=uuid4(),
            roles=["user"],
            email_verified=True,
        )

        async def fixture_access(context):
            return context.principal == principal

        report = asyncio.run(check_provider(settings, principal, fixture_access))
        report["authorization_scope"] = "synthetic_fixture_only_not_real_user_login"
    except Exception:
        report = {"status": "failed", "terminal_reason": "verification_runtime_error"}
    print(json.dumps(report, sort_keys=True))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
