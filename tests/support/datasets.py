"""Shared synthetic datasets fixtures; no test-module imports."""

import json
from contextlib import asynccontextmanager
from uuid import uuid4

from sqlalchemy import event

from statistics_agent.contracts.agent import AgentContext, ToolProposal
from statistics_agent.contracts.auth import AuthorizedPrincipal
from statistics_agent.contracts.datasets import DatasetColumn
from statistics_agent.datasets.execution import DatasetParser
from statistics_agent.datasets.service import DatasetService
from statistics_agent.persistence.datasets import DatasetStore
from support.database import apply, sandbox

CONTENT = b"price\n2\n4\n6\n"


COLUMNS = (DatasetColumn(name="price", kind="numeric", non_missing_count=3, missing_count=0),)


def owner(**updates):
    return AuthorizedPrincipal(
        **(
            {
                "issuer": "https://identity.example/realms/datalens",
                "subject": "analyst",
                "auth_session_ref": uuid4(),
                "roles": ["user"],
                "email_verified": True,
            }
            | updates
        )
    )


@asynccontextmanager
async def storage(settings):
    async with sandbox(settings) as (database, schema, config):
        await apply(database, schema, config, "upgrade", "head")

        def checkout(connection, record, proxy):
            cursor = connection.cursor()
            cursor.execute(f"SET search_path TO {schema}")
            connection.commit()
            cursor.close()

        event.listen(database.engine.sync_engine, "checkout", checkout)
        try:
            yield database, DatasetStore(database)
        finally:
            event.remove(database.engine.sync_engine, "checkout", checkout)


@asynccontextmanager
async def application(settings, *, limits=None):
    async with storage(settings) as (database, store):
        async with DatasetParser(workers=1) as parser:
            yield database, store, parser, DatasetService(database, parser=parser, limits=limits)


def context(principal):
    return AgentContext(principal=principal, run_id=uuid4(), session_id=uuid4(), request_id=uuid4())


def proposal(name, dataset_id, **arguments):
    return ToolProposal(
        tool_call_id=uuid4(),
        provider_tool_call_id="fixture-tool-1",
        name=name,
        raw_arguments=json.dumps({"dataset_id": str(dataset_id)} | arguments),
    )
