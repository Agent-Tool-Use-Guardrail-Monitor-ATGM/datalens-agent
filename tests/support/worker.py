"""Real PostgreSQL/JWT/tools with mocked model/identity HTTP; no live providers."""

import json
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import httpx
from pydantic import SecretStr
from sqlalchemy import func, select, update

from statistics_agent.agent.prompts import PROMPT
from statistics_agent.contracts.agent import AgentVersions
from statistics_agent.contracts.auth import AuthorizedPrincipal
from statistics_agent.contracts.common import AuthenticatedPrincipal
from statistics_agent.contracts.conversations import DatasetSelection, RunSubmission
from statistics_agent.contracts.policy import ToolPolicyConfiguration
from statistics_agent.contracts.sessions import Credentials
from statistics_agent.core.model_settings import ModelSettings
from statistics_agent.core.token_encryption import TokenEncryption
from statistics_agent.persistence.auth import AuthStore
from statistics_agent.persistence.conversations import AdmissionConfiguration, ConversationStore
from statistics_agent.persistence.jobs import JobStore
from statistics_agent.persistence.models import (
    RunDispatch,
    RunJob,
)
from statistics_agent.tools.statistics import STATISTICS_TOOLSET
from statistics_agent.worker import AgentWorker, WorkerAccess
from support.auth import SyntheticProvider
from support.crypto import keyring
from support.datasets import CONTENT, application


def call(name, arguments):
    return httpx.Response(
        200,
        json={
            "choices": [
                {
                    "finish_reason": "tool_calls",
                    "message": {
                        "tool_calls": [
                            {
                                "id": "synthetic-call",
                                "type": "function",
                                "function": {
                                    "name": name,
                                    "arguments": json.dumps(arguments),
                                },
                            }
                        ],
                    },
                }
            ]
        },
    )


def answer(body):
    observations = [item for item in body["messages"] if item["role"] == "tool"]
    if observations:
        return call(
            "finalize_statistics",
            {
                "status": "completed",
                "explanation": "Mean is 4.",
                "result_ids": [json.loads(observations[-1]["content"])["tool_call_id"]],
            },
        )
    dataset = json.loads(body["messages"][1]["content"])["datasets"][0]
    return call(
        "describe_columns",
        {
            "dataset_id": dataset["dataset_id"],
            "columns": ["price"],
            "missing_policy": "omit",
        },
    )


@asynccontextmanager
async def environment(settings, handler=None, *, lease=30):
    async with application(settings) as (database, artifacts, parser, datasets):
        source = SyntheticProvider()
        provider = source.client()
        auth = AuthStore(database, source.settings, TokenEncryption(keyring()))
        created = await auth.create_session(
            AuthenticatedPrincipal(issuer=source.settings.issuer, subject="synthetic-user"),
            Credentials(
                SecretStr(source.token()),
                SecretStr("private-refresh-token"),
                SecretStr(source.token(False)),
            ),
        )
        identity = await provider.validate_access(
            created.session.credentials.access_token.get_secret_value()
        )
        actor = AuthorizedPrincipal(
            **identity.model_dump(), auth_session_ref=created.session.session_id
        )
        conversations = ConversationStore(database)
        conversation = await conversations.create(actor, title="Worker test")
        data = await datasets.ingest(actor, filename="input.csv", content=CONTENT)
        selection = DatasetSelection(dataset_ids=(data.dataset_id,))
        await conversations.set_attachments(actor, conversation.conversation_id, selection)
        model = ModelSettings(SERVICE_MODEL_API_KEY="synthetic-worker-key", _env_file=None)
        policy = ToolPolicyConfiguration(
            reference={"config_id": uuid4(), "version": "worker-policy-v1"},
            allowed_tools=(
                "inspect_dataset",
                "describe_columns",
                "aggregate_statistics",
                "correlate_columns",
            ),
        )
        configuration = AdmissionConfiguration(
            versions=AgentVersions(
                provider=model.resolve().reference,
                prompt=PROMPT.reference,
                policy=policy.reference,
                toolset=STATISTICS_TOOLSET,
            ),
            profile=model.resolve(),
            prompt=PROMPT,
            policy=policy,
        )
        admitted = await conversations.admit(
            actor,
            conversation.conversation_id,
            RunSubmission(
                idempotency_key=uuid4(),
                question="Mean price?",
                selection=selection,
            ),
            configuration,
        )
        requests = []
        held = ContextVar("transaction_held", default=False)
        original_session = database.session

        @asynccontextmanager
        async def tracked_session():
            async with original_session() as current:
                token = held.set(True)
                try:
                    yield current
                finally:
                    held.reset(token)

        database.session = tracked_session

        async def handle(request):
            assert not held.get(), "No transaction may span model HTTP"
            requests.append(json.loads(request.content))
            async with database.session() as session:
                pending = await session.scalar(
                    select(func.count())
                    .select_from(RunDispatch)
                    .where(
                        RunDispatch.kind == "model",
                        RunDispatch.outcome == "pending",
                    )
                )
                assert pending == 1, "Model intent must be committed before HTTP"
            return await handler(requests[-1]) if handler else answer(requests[-1])

        jobs = JobStore(database, lease_seconds=lease)
        worker = AgentWorker(
            jobs=jobs,
            access=WorkerAccess(auth, provider, source.settings),
            datasets=datasets,
            artifacts=artifacts,
            parser=parser,
            model_settings=model,
            transport=httpx.MockTransport(handle),
        )
        try:
            yield SimpleNamespace(
                db=database,
                artifacts=artifacts,
                datasets=datasets,
                source=source,
                provider=provider,
                auth=auth,
                actor=actor,
                conversations=conversations,
                data=data,
                selection=selection,
                configuration=configuration,
                admitted=admitted,
                jobs=jobs,
                worker=worker,
                requests=requests,
                created=created,
            )
        finally:
            await provider.close()


async def expire(env):
    async with env.db.session() as session:
        await session.execute(
            update(RunJob)
            .where(RunJob.run_id == env.admitted.run.run_id)
            .values(
                lease_expires_at=func.clock_timestamp() - timedelta(seconds=1),
            )
        )
        await session.commit()


async def stored(env):
    return (await env.conversations.get_run(env.actor, env.admitted.run.run_id)).run
