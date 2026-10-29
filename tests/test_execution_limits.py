"""Offline evidence for bounded retries, reservations and cancellation-safe admission."""

import asyncio
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from test_statistics_agent import create


def unsupported():
    return httpx.Response(
        200,
        json={
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {"status": "unsupported", "explanation": "No supported operation."}
                        )
                    },
                    "finish_reason": "stop",
                }
            ]
        },
    )


def replace_transport(session, handler):
    session._adapter._transport = httpx.MockTransport(handler)


def test_retry_has_stable_logical_identity_distinct_attempt_indices_and_no_fallback():
    session, dataset, _, _, sink, _ = create()
    original = session._adapter._transport.handler
    calls, delays = [], []

    async def sleep(delay):
        delays.append(delay)

    def handler(request):
        calls.append((str(request.url), json.loads(request.content)["model"]))
        if len(calls) == 1:
            return httpx.Response(429, headers={"retry-after": "0.25"}, text="sensitive quota body")
        return original(request)

    session._sleep = sleep
    replace_transport(session, handler)
    result = asyncio.run(session.run("Average?", (dataset,)))
    assert result.answer.status == "completed"
    assert delays == [0.25]
    assert len(calls) == 3 and len(set(calls)) == 1
    terminal = [e for e in sink.events if e.event_type.value.startswith("model_call.")]
    assert [e.data["attempt_index"] for e in terminal] == [1, 2, 1]
    assert terminal[0].data["model_call_id"] == terminal[1].data["model_call_id"]
    assert terminal[2].data["model_call_id"] != terminal[1].data["model_call_id"]
    assert result.state.completed_attempts == 3
    assert result.state.remaining_steps == 5
    assert all("sensitive quota" not in e.model_dump_json() for e in sink.events)


@pytest.mark.parametrize(
    "status,reason,attempts",
    [
        (401, "model_authentication", 1),
        (403, "model_authentication", 1),
        (400, "model_provider_error", 1),
        (429, "model_retry_exhausted", 3),
        (503, "model_retry_exhausted", 3),
    ],
)
def test_permanent_failure_and_retry_exhaustion_are_bounded(status, reason, attempts):
    session, dataset, _, executed, sink, _ = create()
    calls = []

    async def sleep(_):
        pass

    session._sleep = sleep

    def handler(request):
        calls.append(request)
        return httpx.Response(status, text="secret error")

    replace_transport(session, handler)
    result = asyncio.run(session.run("Question", (dataset,)))
    assert result.answer.status == "incomplete"
    assert result.terminal_reason == reason
    assert len(calls) == len(sink.events) == attempts
    assert not executed


def test_unknown_timeout_is_not_retried():
    session, dataset, _, executed, sink, _ = create()
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout("secret", request=request)

    replace_transport(session, handler)
    result = asyncio.run(session.run("Question", (dataset,)))
    assert result.terminal_reason == "model_timeout"
    assert len(calls) == 1 and sink.events[0].data["outcome_unknown"]
    assert not executed


@pytest.mark.parametrize("header", ["11", "3601"])
def test_retry_after_above_local_wait_limit_stops_without_early_retry(header):
    session, dataset, _, _, sink, _ = create()
    replace_transport(session, lambda _: httpx.Response(429, headers={"retry-after": header}))
    result = asyncio.run(session.run("Question", (dataset,)))
    assert result.terminal_reason == "retry_delay_exceeded"
    assert len(sink.events) == 1


@pytest.mark.parametrize(
    "settings,reason",
    [
        ({"LLM_RUN_INPUT_BUDGET": 1}, "input_budget_exhausted"),
        ({"LLM_MAX_STEPS": 1}, "step_budget_exhausted"),
        ({"LLM_RUN_OUTPUT_BUDGET": 1024}, "output_budget_exhausted"),
    ],
)
def test_admission_budgets_prevent_additional_calls(settings, reason):
    session, dataset, captured, _, _, _ = create(settings_overrides=settings)
    result = asyncio.run(session.run("Question", (dataset,)))
    assert result.terminal_reason == reason
    assert len(captured) == (0 if reason == "input_budget_exhausted" else 1)
    assert result.state.remaining_input_tokens >= 0
    assert result.state.remaining_output_tokens >= 0


def test_reported_counts_refund_reservations_without_double_counting_subsets():
    session, dataset, _, _, _, _ = create()
    original = session._adapter._transport.handler

    def handler(request):
        data = original(request).json()
        data["usage"] = {
            "prompt_tokens": 20,
            "completion_tokens": 10,
            "prompt_tokens_details": {"cached_tokens": 5},
            "completion_tokens_details": {"reasoning_tokens": 4},
        }
        return httpx.Response(200, json=data)

    replace_transport(session, handler)
    result = asyncio.run(session.run("Question", (dataset,)))
    assert result.answer.status == "completed"
    assert result.state.remaining_input_tokens == 32768 - 40
    assert result.state.remaining_output_tokens == 8192 - 20


def test_reported_overrun_prevents_tool_dispatch():
    session, dataset, _, executed, _, _ = create()
    original = session._adapter._transport.handler

    def handler(request):
        data = original(request).json()
        data["usage"] = {"prompt_tokens": 9000, "completion_tokens": 2}
        return httpx.Response(200, json=data)

    replace_transport(session, handler)
    result = asyncio.run(session.run("Question", (dataset,)))
    assert result.terminal_reason == "reported_usage_exceeded_reservation"
    assert not executed


def test_tool_timeout_is_not_retried_and_returns_explicit_outcome():
    session, dataset, _, _, sink, _ = create(settings_overrides={"LLM_TOOL_TIMEOUT_SECONDS": 0.02})
    calls = []

    async def execute(*args):
        calls.append(args)
        await asyncio.sleep(10)

    session._executor.execute = execute
    result = asyncio.run(session.run("Question", (dataset,)))
    assert result.answer.status == "incomplete"
    assert len(calls) == 1 and result.results[0].status == "timed_out"
    assert sink.events[-1].event_type.value == "tool_call.failed"


def test_deadline_includes_authorization_and_prevents_model_call():
    session, dataset, captured, _, _, _ = create(
        settings_overrides={"LLM_RUN_TIMEOUT_SECONDS": 0.02}
    )

    async def authorize(_):
        await asyncio.sleep(10)
        return True

    session._authorize = authorize
    result = asyncio.run(session.run("Question", (dataset,)))
    assert result.terminal_reason == "deadline_exceeded"
    assert not captured


def test_expired_deadline_permits_no_call():
    session, dataset, captured, _, _, _ = create()
    session._state = session._state.model_copy(
        update={"deadline": datetime.now(UTC) - timedelta(seconds=1)}
    )
    assert asyncio.run(session.run("Question", (dataset,))).terminal_reason == "deadline_exceeded"
    assert not captured


def test_access_is_rechecked_after_backoff():
    session, dataset, _, _, sink, _ = create(access=[True, False])

    async def sleep(_):
        pass

    session._sleep = sleep
    replace_transport(session, lambda _: httpx.Response(429))
    result = asyncio.run(session.run("Question", (dataset,)))
    assert result.terminal_reason == "access_unavailable"
    assert len(sink.events) == 1


def test_prompt_admission_rejects_excess_input_before_request():
    session, dataset, captured, _, _, _ = create()
    result = asyncio.run(session.run("x" * 5000, (dataset,)))
    assert result.terminal_reason == "request_input_limit"
    assert not captured


def test_deadline_covers_retry_backoff():
    session, dataset, _, _, sink, _ = create(settings_overrides={"LLM_RUN_TIMEOUT_SECONDS": 0.03})

    async def sleep(_):
        await asyncio.sleep(10)

    session._sleep = sleep
    replace_transport(session, lambda _: httpx.Response(429, headers={"retry-after": "0"}))
    result = asyncio.run(session.run("Question", (dataset,)))
    assert result.terminal_reason == "deadline_exceeded"
    assert len(sink.events) == 1


def test_process_shared_concurrency_and_queue_cancellation_release_slots():
    async def exercise():
        first, dataset, *_ = create()
        second, other, *_ = create()
        assert first._adapter._limiter is second._adapter._limiter
        active = peak = 0

        async def handler(_):
            nonlocal active, peak
            active += 1
            peak = max(active, peak)
            await asyncio.sleep(0.02)
            active -= 1
            return unsupported()

        replace_transport(first, handler)
        replace_transport(second, handler)
        outcomes = await asyncio.gather(first.run("one", (dataset,)), second.run("two", (other,)))
        assert peak == 1 and all(r.answer.status == "unsupported" for r in outcomes)
        queued, data, *_ = create()
        replace_transport(queued, lambda _: pytest.fail("queued request was sent"))
        async with queued._adapter._limiter.slot():
            task = asyncio.create_task(queued.run("cancel", (data,)))
            await asyncio.sleep(0.02)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        # Cancellation neither consumes nor over-releases the next slot.
        final, data, *_ = create(scenario="unsupported")
        assert (await final.run("after cancellation", (data,))).answer.status == "unsupported"

    asyncio.run(exercise())
