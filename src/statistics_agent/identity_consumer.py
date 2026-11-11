"""Receive RabbitMQ identity events and acknowledge only committed effects."""

import argparse
import asyncio
import json
import logging
import signal
import ssl
import time
from datetime import UTC, datetime
from pathlib import Path
from threading import Event
from urllib.parse import unquote, urlsplit

import pika
import psycopg
from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.exc import DBAPIError
from sqlalchemy.exc import TimeoutError as PoolTimeout

from statistics_agent.contracts.identity_events import IdentityEventError
from statistics_agent.core.event_loop import create_event_loop
from statistics_agent.core.resource_auth import load_resource_auth_settings
from statistics_agent.core.settings import ConfigurationError, load_settings
from statistics_agent.persistence.database import Database
from statistics_agent.persistence.identity_projection import IdentityProjection
from statistics_agent.persistence.schema import read_schema_status

QUEUE = "agent.identity.v1"
LOG = logging.getLogger("identity-consumer")
BROKER_ERRORS = (pika.exceptions.AMQPError, OSError)


class ConsumerSettings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", hide_input_in_errors=True)

    url: SecretStr = Field(validation_alias="IDENTITY_CONSUMER_AMQP_URL")
    ca_file: Path | None = Field(default=None, validation_alias="IDENTITY_CONSUMER_CA_FILE")

    def parameters(self, app_env):
        url = urlsplit(self.url.get_secret_value())
        local = app_env in {"development", "test"} and url.hostname in {
            "localhost",
            "127.0.0.1",
            "::1",
            "rabbitmq",
        }
        if (
            not url.hostname
            or url.port == 0
            or url.path != "/identity"
            or url.query
            or url.fragment
            or url.scheme not in {"amqp", "amqps"}
            or (url.scheme == "amqp" and (not local or self.ca_file is not None))
            or url.username != "agent-identity-consumer"
            or not url.password
        ):
            raise ConfigurationError("Invalid identity RabbitMQ connection")
        tls = None
        if url.scheme == "amqps":
            tls = pika.SSLOptions(ssl.create_default_context(cafile=self.ca_file), url.hostname)
        return pika.ConnectionParameters(
            host=url.hostname,
            port=url.port or (5671 if tls else 5672),
            virtual_host="identity",
            credentials=pika.PlainCredentials(url.username, unquote(url.password, errors="strict")),
            ssl_options=tls,
            heartbeat=45,
            connection_attempts=1,
            socket_timeout=5,
            stack_timeout=10,
            blocked_connection_timeout=10,
        )


def retryable(error):
    if isinstance(error, (TimeoutError, PoolTimeout)):
        return True
    state = getattr(error.orig, "sqlstate", None)
    return (
        error.connection_invalidated
        or state is None
        and isinstance(error.orig, (OSError, psycopg.OperationalError))
        or state is not None
        and state.startswith("08")
        or state in {"40001", "40P01", "55P03", "57014", "57P01", "57P02", "57P03"}
    )


def handle_delivery(connection, channel, method, properties, body, apply, stop):
    try:
        apply(body, delivery=(properties.message_id, method.routing_key))
    except IdentityEventError:
        LOG.warning("Rejected invalid identity delivery to the dead-letter queue")
        channel.basic_reject(method.delivery_tag, requeue=False)
    except (DBAPIError, PoolTimeout, TimeoutError) as error:
        if not retryable(error):
            raise
        LOG.warning("Identity database temporarily unavailable; delivery remains pending")
        # Process broker heartbeats while backing off, then return the uncommitted delivery.
        connection.sleep(5)
        if not stop.is_set():
            channel.basic_nack(method.delivery_tag, requeue=True)
    else:
        channel.basic_ack(method.delivery_tag)
        occurred_at = datetime.fromisoformat(json.loads(body)["occurred_at"])
        if (datetime.now(UTC) - occurred_at).total_seconds() >= 300:
            LOG.warning("Identity delivery lag exceeded five minutes")


def consume_session(parameters, apply, stop):
    with pika.BlockingConnection(parameters) as connection:
        channel = connection.channel()
        channel.basic_qos(prefetch_count=1)
        next_check = 0
        for method, properties, body in channel.consume(
            QUEUE, auto_ack=False, inactivity_timeout=1
        ):
            if stop.is_set():
                return
            if time.monotonic() >= next_check:
                depth = channel.queue_declare(QUEUE, passive=True).method.message_count
                if depth >= 1000:
                    LOG.warning("Identity RabbitMQ backlog reached 1000 ready messages")
                next_check = time.monotonic() + 60
            if method is not None:
                handle_delivery(connection, channel, method, properties, body, apply, stop)
    if not stop.is_set():
        raise pika.exceptions.AMQPError("Identity queue consumer was cancelled")


def serve(env_file):
    settings = load_settings(env_file=env_file)
    auth = load_resource_auth_settings(env_file=env_file)
    parameters = ConsumerSettings(_env_file=env_file, _env_file_encoding="utf-8").parameters(
        settings.app_env
    )
    if auth.app_env != settings.app_env:
        raise ConfigurationError("Identity consumer environment mismatch")
    database = Database(settings)
    projection = IdentityProjection(database, auth.issuer)
    stop = Event()
    with asyncio.Runner(loop_factory=create_event_loop) as runner:

        async def check_schema():
            async with database.engine.connect() as connection:
                if not (await read_schema_status(connection)).is_current:
                    raise ConfigurationError("Identity consumer schema is not current")

        async def apply_delivery(body, delivery):
            async with asyncio.timeout(15):
                return await projection.apply(body, delivery=delivery)

        def apply(body, *, delivery):
            return runner.run(apply_delivery(body, delivery))

        handlers = {}
        try:
            runner.run(check_schema())
            for sig in (signal.SIGINT, signal.SIGTERM):
                handlers[sig] = signal.signal(sig, lambda *_: stop.set())
            delay = 1
            while not stop.is_set():
                try:
                    consume_session(parameters, apply, stop)
                except BROKER_ERRORS:
                    LOG.warning("Identity RabbitMQ disconnected; reconnecting in %s seconds", delay)
                    stop.wait(delay)
                    delay = min(2 * delay, 30)
        finally:
            for sig, handler in handlers.items():
                signal.signal(sig, handler)
            runner.run(database.close())


def main(argv=None):
    parser = argparse.ArgumentParser(description="DataLens RabbitMQ identity consumer")
    parser.add_argument("--env-file")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    logging.getLogger("pika").setLevel(logging.CRITICAL + 1)
    try:
        serve(args.env_file)
    except KeyboardInterrupt:
        return 0
    except Exception:
        LOG.error("Identity consumer stopped; check configuration and database availability")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
