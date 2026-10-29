"""One observed Chat Completions attempt. No retries or tool execution."""

import asyncio
import hashlib
import json
import math
import time
from collections.abc import Callable
from datetime import UTC, datetime
from uuid import uuid4

import httpx
from pydantic import ValidationError

from statistics_agent.agent.limits import ModelLimiter, provider_limiter
from statistics_agent.contracts.agent import (
    EventSink,
    ProviderFailure,
    ProviderRequest,
    ProviderResponse,
    TokenUsage,
    ToolProposal,
)
from statistics_agent.contracts.events import EventEnvelope
from statistics_agent.core.model_settings import ModelSettings

MAX_RESPONSE_BYTES = 1_048_576


class AdapterError(RuntimeError):
    """Fixed messages only. Do not wrap raw provider exceptions."""


class EvidenceDeliveryError(AdapterError):
    """Evidence failure prevents a response from reaching tool dispatch."""


class ModelAdapter:
    def __init__(
        self,
        settings: ModelSettings,
        sink: EventSink,
        sequence: Callable[[], int],
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        limiter: ModelLimiter | None = None,
    ):
        self.profile = settings.resolve()
        self._key = settings.api_key
        self._sink = sink
        self._sequence = sequence
        self._transport = transport
        self._limiter = limiter or provider_limiter(self.profile.provider, self.profile.concurrency)
        if self._limiter.capacity != self.profile.concurrency:
            raise AdapterError("Model limiter does not match configured concurrency")

    async def _emit(self, request, kind, data):
        event = EventEnvelope(
            event_id=uuid4(),
            event_type=kind,
            occurred_at=datetime.now(UTC),
            run_id=request.attempt.run_id,
            session_id=request.attempt.session_id,
            request_id=request.attempt.request_id,
            sequence=self._sequence(),
            data={
                "model_call_id": str(request.attempt.model_call_id),
                "attempt_index": request.attempt.attempt_index,
                "profile_id": str(self.profile.reference.config_id),
                **data,
            },
        )
        try:
            await self._sink.append(event)
            return
        except Exception:
            pass
        raise EvidenceDeliveryError("Model evidence delivery failed")

    def _payload(self, request: ProviderRequest, tools: list[dict] | None = None):
        if request.profile != self.profile.reference:
            raise AdapterError("Model profile reference mismatch")
        if request.max_output_tokens > self.profile.max_output_tokens:
            raise AdapterError("Model output limit exceeds profile")
        messages = []
        ids = {
            p.tool_call_id: p.provider_tool_call_id for m in request.messages for p in m.proposals
        }
        for message in request.messages:
            item = {"role": message.role, "content": message.content}
            if message.proposals:
                if any(not p.provider_tool_call_id for p in message.proposals):
                    raise AdapterError("Cannot replay tool proposal without provider identity")
                item["tool_calls"] = [
                    {
                        "id": p.provider_tool_call_id,
                        "type": "function",
                        "function": {"name": p.name, "arguments": p.raw_arguments},
                    }
                    for p in message.proposals
                ]
            if message.tool_call_id is not None:
                if not ids.get(message.tool_call_id):
                    raise AdapterError("Tool result has no matching proposal")
                item["tool_call_id"] = ids[message.tool_call_id]
            messages.append(item)
        payload = {
            "model": self.profile.model,
            "messages": messages,
            "max_completion_tokens": request.max_output_tokens,
            "stream": False,
        }
        if tools:
            self.profile.require()
            payload.update(tools=tools, parallel_tool_calls=False)
        if len(json.dumps(payload).encode()) > 65_536:
            raise AdapterError("Model request exceeds byte limit")
        return payload

    def input_reservation(self, request: ProviderRequest, *, tools=None) -> int:
        """Conservative byte-based admission estimate, not reported token usage."""
        return len(json.dumps(self._payload(request, tools), ensure_ascii=False).encode()) + 256

    async def complete(self, request: ProviderRequest, *, tools: list[dict] | None = None):
        payload = self._payload(request, tools)
        started = datetime.now(UTC)
        start = time.monotonic()
        budget = min(self.profile.timeout_seconds, (request.deadline - started).total_seconds())
        status = None
        retry_after = None
        body = None
        failure = None
        sent = False
        try:
            if budget <= 0:
                raise TimeoutError
            async with asyncio.timeout(budget):
                async with self._limiter.slot():
                    sent = True
                    async with httpx.AsyncClient(
                        transport=self._transport,
                        follow_redirects=False,
                        trust_env=False,
                        timeout=budget,
                    ) as client:
                        async with client.stream(
                            "POST",
                            self.profile.base_url + "/chat/completions",
                            json=payload,
                            headers={"Authorization": "Bearer " + self._key.get_secret_value()},
                        ) as response:
                            status = response.status_code
                            if response.headers.get("retry-after"):
                                try:
                                    value = float(response.headers["retry-after"])
                                    if math.isfinite(value) and value >= 0:
                                        retry_after = value
                                except ValueError:
                                    pass
                            if status != 200:
                                category = (
                                    "authentication"
                                    if status in (401, 403)
                                    else "rate_limit"
                                    if status == 429
                                    else "provider_error"
                                )
                                failure = ProviderFailure(
                                    attempt=request.attempt,
                                    category=category,
                                    http_status=status,
                                    retry_after_seconds=retry_after,
                                )
                            else:
                                raw = bytearray()
                                async for chunk in response.aiter_bytes():
                                    raw.extend(chunk)
                                    if len(raw) > MAX_RESPONSE_BYTES:
                                        raise ValueError
                                body = json.loads(raw)
        except (httpx.TimeoutException, TimeoutError):
            failure = ProviderFailure(
                attempt=request.attempt, category="timeout", outcome_unknown=sent
            )
        except httpx.RequestError:
            failure = ProviderFailure(
                attempt=request.attempt, category="network", outcome_unknown=True
            )
        except (ValueError, TypeError, UnicodeError):
            failure = ProviderFailure(
                attempt=request.attempt, category="invalid_response", http_status=status
            )
        # Sink exceptions are deliberately outside provider handlers: fail closed.
        common = {
            "started_at": started.isoformat(),
            "ended_at": datetime.now(UTC).isoformat(),
            "duration_ms": round((time.monotonic() - start) * 1000, 3),
            "http_status": status,
            "estimated_cost_usd": None,
            "pricing_version": "unpriced",
        }
        if failure is None:
            try:
                result = await self._normalize(request, body)
            except (ValidationError, ValueError, TypeError, KeyError, IndexError):
                failure = ProviderFailure(
                    attempt=request.attempt, category="invalid_response", http_status=status
                )
        if failure is not None:
            await self._emit(
                request,
                "model_call.failed",
                {
                    **common,
                    "error_category": failure.category,
                    "outcome_unknown": failure.outcome_unknown,
                    "retry_after_seconds": retry_after,
                },
            )
            return failure
        await self._emit(
            request,
            "model_call.completed",
            {
                **common,
                "usage": result.usage.model_dump(),
                "finish_reason": result.finish_reason,
                # Provider-controlled identifiers never appear as raw telemetry strings.
                "provider_request_id_sha256": self._digest(result.provider_request_id),
                "actual_model_sha256": self._digest(result.actual_model),
            },
        )
        return result

    @staticmethod
    def _digest(value):
        return hashlib.sha256(value.encode()).hexdigest() if value is not None else None

    async def _normalize(self, request, body):
        if not isinstance(body, dict) or len(body["choices"]) != 1:
            raise ValueError
        choice = body["choices"][0]
        if not isinstance(choice, dict):
            raise ValueError
        message = choice["message"]
        if not isinstance(message, dict):
            raise ValueError
        raw_proposals = message.get("tool_calls")
        if raw_proposals is None:
            raw_proposals = []
        if not isinstance(raw_proposals, list) or len(raw_proposals) > 32:
            raise ValueError
        proposals = []
        observed = [(uuid4(), raw) for raw in raw_proposals]
        for call_id, raw in observed:
            # Evidence before name lookup, argument parsing or framework filtering.
            encoded = json.dumps(raw, ensure_ascii=True, sort_keys=True).encode()
            await self._emit(
                request,
                "tool_call.requested",
                {
                    "tool_call_id": str(call_id),
                    "proposal_sha256": hashlib.sha256(encoded).hexdigest(),
                    "proposal_bytes": len(encoded),
                    "evidence_mode": "digest_only",
                },
            )
        for call_id, raw in observed:
            if not isinstance(raw, dict) or not isinstance(raw.get("function"), dict):
                raise ValueError
            proposals.append(
                ToolProposal(
                    tool_call_id=call_id,
                    provider_tool_call_id=raw.get("id"),
                    name=raw["function"]["name"],
                    raw_arguments=raw["function"]["arguments"],
                )
            )
        usage = body.get("usage")
        if usage is None:
            usage = {}
        if not isinstance(usage, dict):
            raise ValueError
        input_details = usage.get("prompt_tokens_details")
        output_details = usage.get("completion_tokens_details")
        if input_details is None:
            input_details = {}
        if output_details is None:
            output_details = {}
        if not isinstance(input_details, dict) or not isinstance(output_details, dict):
            raise ValueError
        counts = {
            "input_tokens": usage.get("prompt_tokens"),
            "output_tokens": usage.get("completion_tokens"),
            "cached_input_tokens": input_details.get("cached_tokens"),
            "reasoning_tokens": output_details.get("reasoning_tokens"),
        }
        normalized_usage = TokenUsage(
            source="provider" if any(v is not None for v in counts.values()) else "unavailable",
            **counts,
        )
        finish = choice.get("finish_reason")
        return ProviderResponse(
            attempt=request.attempt,
            provider_request_id=body.get("id"),
            actual_model=body.get("model"),
            content="" if message.get("content") is None else message["content"],
            proposals=tuple(proposals),
            usage=normalized_usage,
            finish_reason=finish
            if finish in {"stop", "tool_calls", "length", "filtered"}
            else "filtered"
            if finish == "content_filter"
            else "unknown",
        )
