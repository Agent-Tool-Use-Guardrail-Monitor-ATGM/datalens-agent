"""CrewAI boundary; tool proposals cannot bypass the later policy dispatcher."""

import asyncio
import os
from datetime import datetime
from uuid import uuid4

from pydantic import PrivateAttr

from statistics_agent.agent.provider import AdapterError, ModelAdapter
from statistics_agent.contracts.agent import (
    AgentContext,
    AgentMessage,
    ModelAttempt,
    ProviderFailure,
    ProviderRequest,
)

# CrewAI imports otherwise auto-load cwd .env and can initialize tracing.
# Our explicit Pydantic loaders own secrets; no implicit framework environment load.
os.environ["PYTHON_DOTENV_DISABLED"] = "1"
os.environ["OTEL_SDK_DISABLED"] = "true"
from crewai import Agent, BaseLLM  # noqa: E402, F401


class ToolDispatchRequired(AdapterError):
    """Controlled handoff carrying restricted proposals, never executed here."""

    def __init__(self, response):
        super().__init__("Tool proposals require the authorized dispatcher")
        self.response = response


class StatisticsLLM(BaseLLM):
    _adapter: ModelAdapter = PrivateAttr()
    _context: AgentContext = PrivateAttr()
    _deadline: datetime = PrivateAttr()

    @classmethod
    def _wrap_call_method(cls, method_name):
        # Pinned CrewAI 1.15.33 otherwise installs hidden rate-limit retries.
        # Keep retry ownership in plan 4.5, including opaque provider failures.
        return

    def __init__(self, adapter: ModelAdapter, context: AgentContext, deadline: datetime):
        super().__init__(model=adapter.profile.model)
        self._adapter, self._context, self._deadline = adapter, context, deadline

    def supports_function_calling(self):
        return True

    def supports_stop_words(self):
        return False

    def get_context_window_size(self):
        return self._adapter.profile.input_token_budget

    async def complete_request(self, request: ProviderRequest, *, tools=None):
        """Typed continuation for the controlled runner; same observed adapter."""
        if (
            request.attempt.run_id != self._context.run_id
            or request.attempt.session_id != self._context.session_id
            or request.attempt.request_id != self._context.request_id
        ):
            raise AdapterError("Model request does not belong to this execution")
        return await self._adapter.complete(request, tools=tools)

    async def acall(
        self,
        messages,
        tools=None,
        callbacks=None,
        available_functions=None,
        from_task=None,
        from_agent=None,
        response_model=None,
    ):
        if callbacks or response_model is not None:
            raise AdapterError("Unsupported model callback or output mode")
        if isinstance(messages, str):
            messages = [{"role": "user", "content": messages}]
        # Rich framework tool messages require the typed provider boundary;
        # do not silently strip identities or accept arbitrary framework execution.
        normalized = tuple(AgentMessage.model_validate(m) for m in messages)
        request = ProviderRequest(
            attempt=ModelAttempt(
                run_id=self._context.run_id,
                session_id=self._context.session_id,
                request_id=self._context.request_id,
                model_call_id=uuid4(),
                attempt_index=1,
            ),
            profile=self._adapter.profile.reference,
            messages=normalized,
            max_output_tokens=self._adapter.profile.max_output_tokens,
            deadline=self._deadline,
        )
        response = await self.complete_request(request, tools=tools)
        if isinstance(response, ProviderFailure):
            # Never propagate provider status/errors through CrewAI's retry heuristics.
            raise AdapterError("Model attempt failed")
        if response.proposals:
            raise ToolDispatchRequired(response)
        return response.content

    def call(
        self,
        messages,
        tools=None,
        callbacks=None,
        available_functions=None,
        from_task=None,
        from_agent=None,
        response_model=None,
    ):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(
                self.acall(
                    messages,
                    tools,
                    callbacks,
                    available_functions,
                    from_task,
                    from_agent,
                    response_model,
                )
            )
        raise AdapterError("Use asynchronous model calls inside an event loop")
