"""Independent bounded worker process; PostgreSQL dispatch, no Kafka command queue."""

import argparse
import asyncio
import itertools
import signal
from datetime import UTC, datetime

from statistics_agent.agent.composition import create_statistics_session
from statistics_agent.agent.prompts import PROMPT
from statistics_agent.agent.provider import ModelAdapter
from statistics_agent.agent.telemetry import observe_proposal
from statistics_agent.contracts.agent import AgentContext
from statistics_agent.contracts.auth import AuthorizedPrincipal
from statistics_agent.contracts.policy import ToolExecutionScope
from statistics_agent.core.event_loop import create_event_loop
from statistics_agent.core.session_lifecycle import SessionLifecycle
from statistics_agent.persistence.completions import finish_run
from statistics_agent.persistence.conversations import ConversationStore
from statistics_agent.persistence.jobs import JobStore, LeaseLost, require_lease
from statistics_agent.tools.statistics import STATISTICS_TOOLSET


class WorkerAccess:
    def __init__(self, store, provider, settings):
        self.store = store
        self.lifecycle = SessionLifecycle(store, provider, settings)

    async def resolve(self, claim):
        try:
            stored = await self.store.read_session_id(claim.auth_session_ref)
            if (
                stored is None
                or stored.issuer != claim.owner.issuer
                or stored.subject != claim.owner.subject
            ):
                return None
            identity = await self.lifecycle.authorize(stored)
            return AuthorizedPrincipal(
                **identity.model_dump(),
                auth_session_ref=stored.session_id,
            )
        except Exception:
            return None


class WorkerEvidence:
    def __init__(self, jobs, claim):
        self.jobs, self.claim = jobs, claim
        self.pending = None

    async def checkpoint(self, context, result):
        if self.pending is not None:
            raise ValueError("Worker checkpoint already pending")
        self.pending = context, result

    async def append(self, event):
        if self.pending is None:
            await self.jobs.append(self.claim, event)
        else:
            context, result = self.pending
            await self.jobs.checkpoint(self.claim, context, result, event=event)
            self.pending = None


class AgentWorker:
    def __init__(
        self,
        *,
        jobs,
        access,
        datasets,
        artifacts,
        parser,
        model_settings,
        toolset=STATISTICS_TOOLSET,
        transport=None,
    ):
        self.jobs, self.access = jobs, access
        self.datasets, self.artifacts, self.parser = datasets, artifacts, parser
        self.model_settings, self.toolset, self.transport = model_settings, toolset, transport
        self.conversations = ConversationStore(jobs.database)

    async def _execute(self, claim):
        principal = await self.access.resolve(claim)
        if principal is None:
            await self.jobs.interrupt(claim, "access_unavailable")
            return
        admission = await self.conversations.get_run(principal, claim.run_id)
        configuration = admission.configuration
        if (
            configuration.profile != self.model_settings.resolve()
            or configuration.prompt != PROMPT
            or configuration.versions.toolset != self.toolset
        ):
            await self.jobs.interrupt(claim, "configuration_unavailable")
            return
        context = AgentContext(
            principal=principal,
            run_id=claim.run_id,
            session_id=claim.attempt_id,
            request_id=claim.request_id,
        )

        async def authorize(current):
            if current != context or await self.access.resolve(claim) != principal:
                return False
            async with self.jobs.database.session() as session:
                await require_lease(session, claim.run_id, claim.attempt_id)
            return True

        async def before_model(request):
            if not await authorize(context):
                raise LeaseLost
            await self.jobs.before_model(claim, request)

        async def checkpoint(current, result):
            await sink.checkpoint(current, result)

        sink, sequence = WorkerEvidence(self.jobs, claim), itertools.count(2).__next__
        adapter = ModelAdapter(
            self.model_settings,
            sink,
            sequence,
            transport=self.transport,
            before_dispatch=before_model,
            observe_proposal=lambda raw, call_id: observe_proposal(
                raw, call_id, frozenset(item.dataset_id for item in admission.datasets)
            ),
        )
        session = create_statistics_session(
            scope=ToolExecutionScope(
                context=context, dataset_ids=tuple(item.dataset_id for item in admission.datasets)
            ),
            configuration=configuration.policy,
            datasets=self.datasets,
            worker=self.parser,
            artifacts=self.artifacts,
            adapter=adapter,
            toolset=self.toolset,
            authorize=authorize,
            sink=sink,
            sequence=sequence,
            deadline=claim.deadline,
            checkpoint=checkpoint,
        )
        prepared = await session.prepare_context(self.conversations, self.datasets, self.artifacts)
        execution = await session.run(prepared.question, prepared.datasets, prepared=prepared)
        if await authorize(context):
            await finish_run(self.jobs.database, execution, claim_id=claim.attempt_id)
        else:
            await self.jobs.interrupt(claim, "access_unavailable")

    async def _heartbeat(self, claim):
        while True:
            await asyncio.sleep(self.jobs.lease_seconds / 3)
            await self.jobs.heartbeat(claim)

    async def run_once(self):
        if await self.jobs.recover_one():
            return True
        claim = await self.jobs.claim()
        if claim is None:
            return False
        work = asyncio.create_task(self._execute(claim))
        heartbeat = asyncio.create_task(self._heartbeat(claim))
        reason = "execution_failed"
        try:
            seconds = max(0, (claim.deadline - datetime.now(UTC)).total_seconds())
            async with asyncio.timeout(seconds):
                done, _ = await asyncio.wait({work, heartbeat}, return_when=asyncio.FIRST_COMPLETED)
                if work in done:
                    await work
                else:
                    await heartbeat
        except asyncio.CancelledError:
            reason = "shutdown"
            raise
        except LeaseLost:
            pass
        except Exception:
            pass
        finally:
            for task in (work, heartbeat):
                task.cancel()
            await asyncio.gather(work, heartbeat, return_exceptions=True)
            # A completed run has no job; a lost lease can only be recovered by its successor.
            try:
                await self.jobs.interrupt(claim, reason)
            except LeaseLost:
                pass
        return True

    async def run(self, stop):
        async def loop():
            while not stop.is_set():
                if not await self.run_once():
                    await asyncio.sleep(0.5)

        task = asyncio.create_task(loop())
        stopped = asyncio.create_task(stop.wait())
        try:
            done, _ = await asyncio.wait({task, stopped}, return_when=asyncio.FIRST_COMPLETED)
            if task in done:
                await task
        finally:
            task.cancel()
            stopped.cancel()
            await asyncio.gather(task, stopped, return_exceptions=True)


async def serve(env_file=None):
    from statistics_agent.core.auth import load_auth_settings
    from statistics_agent.core.auth_secrets import load_auth_secrets
    from statistics_agent.core.model_settings import load_model_settings
    from statistics_agent.core.oidc import OidcProvider
    from statistics_agent.core.settings import ConfigurationError, load_settings
    from statistics_agent.core.token_encryption import TokenEncryption
    from statistics_agent.datasets.execution import DatasetParser
    from statistics_agent.datasets.service import DatasetService
    from statistics_agent.persistence.auth import AuthStore
    from statistics_agent.persistence.database import Database
    from statistics_agent.persistence.datasets import DatasetStore
    from statistics_agent.persistence.schema import read_schema_status

    settings, auth = load_settings(env_file=env_file), load_auth_settings(env_file=env_file)
    if settings.app_env != auth.app_env:
        raise ConfigurationError("Worker authentication environment mismatch")
    secrets = load_auth_secrets(auth)
    model = load_model_settings(env_file=env_file)
    database = Database(settings)
    provider = OidcProvider(auth, secrets)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    previous = {}
    try:
        async with database.engine.connect() as connection:
            if not (await read_schema_status(connection)).is_current:
                raise ConfigurationError("Worker schema is not current")
        for name in (signal.SIGINT, signal.SIGTERM):
            previous[name] = signal.signal(name, lambda *_: loop.call_soon_threadsafe(stop.set))
        async with DatasetParser(workers=1) as parser:
            await AgentWorker(
                jobs=JobStore(database),
                access=WorkerAccess(
                    AuthStore(database, auth, TokenEncryption(secrets)), provider, auth
                ),
                datasets=DatasetService(database, parser=parser),
                artifacts=DatasetStore(database),
                parser=parser,
                model_settings=model,
            ).run(stop)
    finally:
        for name, handler in previous.items():
            signal.signal(name, handler)
        try:
            await provider.close()
        finally:
            await database.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description="DataLens durable agent worker")
    parser.add_argument("--env-file", default=None)
    args = parser.parse_args(argv)
    try:
        with asyncio.Runner(loop_factory=create_event_loop) as runner:
            runner.run(serve(args.env_file))
        return 0
    except (Exception, KeyboardInterrupt):
        print("Agent worker stopped; check configuration and service availability")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
