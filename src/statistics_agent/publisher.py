"""Singleton transactional Kafka publisher; database acknowledgement is at least once."""

import argparse
import asyncio
import logging
import signal
from contextlib import asynccontextmanager

from sqlalchemy import func, select, update

from statistics_agent.contracts.events import EventEnvelope
from statistics_agent.contracts.run_events import validate_run_event
from statistics_agent.core.event_loop import create_event_loop
from statistics_agent.persistence.models import EventOutbox


class PublisherCloseError(RuntimeError):
    pass


class OutboxPublisher:
    def __init__(self, database, settings, *, producer_factory=None):
        if producer_factory is None:
            from aiokafka import AIOKafkaProducer

            producer_factory = AIOKafkaProducer
        self.database, self.settings, self.factory = database, settings, producer_factory

    @asynccontextmanager
    async def epoch(self):
        """Hold one session lock, not a transaction, across bounded broker operations."""
        async with self.database.engine.connect() as owner:
            locked = await owner.scalar(select(func.pg_try_advisory_lock(714006)))
            await owner.commit()
            if not locked:
                yield None
                return
            producer, failed = None, True
            try:
                producer = self.factory(**self.settings.producer_options())
                async with asyncio.timeout(self.settings.timeout_seconds):
                    await producer.start()  # Stable transactional ID fences earlier broker epochs.
                yield owner, producer
                failed = False
            finally:
                try:
                    if producer is not None:
                        try:
                            async with asyncio.timeout(5):
                                if failed:
                                    # Close connections before a possibly unflushable queue.
                                    await producer.client.close()
                                await producer.stop()
                        except Exception:
                            raise PublisherCloseError(
                                "Kafka publisher could not close safely"
                            ) from None
                finally:
                    if not owner.invalidated and not owner.closed:
                        try:
                            await owner.rollback()
                            await owner.execute(select(func.pg_advisory_unlock(714006)))
                            await owner.commit()
                        except Exception:
                            # Never return a held session lock to the pool.
                            await owner.invalidate()

    async def publish_one(self, epoch):
        owner, producer = epoch
        await owner.execute(select(1))
        await owner.commit()
        record = (
            await owner.execute(
                select(EventOutbox.event_id, EventOutbox.envelope)
                .where(
                    EventOutbox.published_at.is_(None),
                )
                .order_by(EventOutbox.position)
                .limit(1)
            )
        ).one_or_none()
        await owner.commit()
        if record is None:
            return False
        envelope = validate_run_event(EventEnvelope.model_validate(record.envelope))
        key, value = str(envelope.session_id).encode(), envelope.model_dump_json().encode()
        # Explicit calls keep abort cleanup from hiding an outer timeout during broker outage.
        async with asyncio.timeout(self.settings.timeout_seconds):
            await producer.begin_transaction()
            await producer.send_and_wait(self.settings.topic, value, key=key)
            await producer.commit_transaction()
        await owner.execute(select(1))
        await owner.commit()
        await owner.execute(
            update(EventOutbox)
            .where(
                EventOutbox.event_id == envelope.event_id,
                EventOutbox.published_at.is_(None),
            )
            .values(published_at=func.clock_timestamp())
        )
        await owner.commit()
        return True

    async def run(self, stop):
        delay = 1
        while not stop.is_set():
            try:
                async with self.epoch() as epoch:
                    if epoch is not None:
                        while not stop.is_set():
                            if await self.publish_one(epoch):
                                delay = 1
                            else:
                                await self._pause(stop, 0.5)
                        return
            except PublisherCloseError:
                raise
            except Exception:
                # SDK/broker exceptions may include endpoints/credentials; never log their bodies.
                print(
                    "Kafka not connected or delivery unavailable; pending events retained for retry"
                )
            await self._pause(stop, delay)
            delay = min(delay * 2, 30)

    @staticmethod
    async def _pause(stop, seconds):
        try:
            async with asyncio.timeout(seconds):
                await stop.wait()
        except TimeoutError:
            pass


async def serve(env_file=None):
    from statistics_agent.core.kafka_settings import load_kafka_settings
    from statistics_agent.core.settings import ConfigurationError, load_settings
    from statistics_agent.persistence.database import Database
    from statistics_agent.persistence.schema import read_schema_status

    settings, kafka = load_settings(env_file=env_file), load_kafka_settings(env_file=env_file)
    if settings.app_env != kafka.app_env:
        raise ConfigurationError("Publisher environment mismatch")
    database, stop = Database(settings), asyncio.Event()
    loop, previous = asyncio.get_running_loop(), {}
    try:
        async with database.engine.connect() as connection:
            if not (await read_schema_status(connection)).is_current:
                raise ConfigurationError("Publisher schema is not current")
        for name in (signal.SIGINT, signal.SIGTERM):
            previous[name] = signal.signal(name, lambda *_: loop.call_soon_threadsafe(stop.set))
        await OutboxPublisher(database, kafka).run(stop)
    finally:
        for name, handler in previous.items():
            signal.signal(name, handler)
        await database.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description="DataLens Kafka telemetry publisher")
    parser.add_argument("--env-file", default=None)
    args = parser.parse_args(argv)
    # The SDK's connection diagnostics can contain sensitive connection configuration.
    logging.getLogger("aiokafka").disabled = True
    logging.getLogger("aiokafka").setLevel(logging.CRITICAL)
    try:
        with asyncio.Runner(loop_factory=create_event_loop) as runner:
            runner.run(serve(args.env_file))
        return 0
    except (Exception, KeyboardInterrupt):
        print("Kafka publisher stopped; check configuration and service availability")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
