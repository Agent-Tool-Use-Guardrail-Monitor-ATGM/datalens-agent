"""Broker settlement behavior and a real RabbitMQ-to-PostgreSQL delivery."""

import asyncio
import json
import os
import ssl
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from threading import Event, Timer
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pika
import pytest
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError

from statistics_agent.contracts.identity_events import IdentityEventError
from statistics_agent.core.settings import ConfigurationError
from statistics_agent.identity_consumer import (
    QUEUE,
    ConsumerSettings,
    consume_session,
    handle_delivery,
)
from statistics_agent.persistence.identity_projection import IdentityProjection
from support.database import run
from support.datasets import storage

ISSUER = "https://identity.example/realms/datalens"
EXCHANGE = "identity.events.v1"


def test_consumer_requires_scoped_credentials_and_hosted_tls():
    url = "amqp://agent-identity-consumer:secret@localhost/identity"
    settings = ConsumerSettings(IDENTITY_CONSUMER_AMQP_URL=url)
    assert settings.parameters("test").virtual_host == "identity"
    with pytest.raises(ConfigurationError):
        settings.parameters("production")
    with pytest.raises(ConfigurationError):
        ConsumerSettings(
            IDENTITY_CONSUMER_AMQP_URL=url.replace("agent-identity-consumer", "guest")
        ).parameters("test")
    tls = (
        ConsumerSettings(IDENTITY_CONSUMER_AMQP_URL=url.replace("amqp:", "amqps:"))
        .parameters("production")
        .ssl_options
    )
    assert tls.context.check_hostname and tls.context.verify_mode == ssl.CERT_REQUIRED


def test_ack_follows_commit_and_database_errors_preserve_delivery():
    channel, connection, stop = Mock(), Mock(), Event()
    method = SimpleNamespace(delivery_tag=1, routing_key="user.created")
    props = SimpleNamespace(message_id="event-id")
    body = json.dumps({"occurred_at": datetime.now(UTC).isoformat()}).encode()

    def apply(body, *, delivery):
        assert delivery == ("event-id", "user.created")
        channel.basic_ack.assert_not_called()

    handle_delivery(connection, channel, method, props, body, apply, stop)
    channel.basic_ack.assert_called_once_with(1)
    channel.reset_mock()
    handle_delivery(connection, channel, method, props, body, Mock(side_effect=TimeoutError), stop)
    connection.sleep.assert_called_once_with(5)
    channel.basic_nack.assert_called_once_with(1, requeue=True)
    channel.basic_ack.assert_not_called()
    channel.basic_reject.assert_not_called()
    permanent = ProgrammingError("test", None, Exception("private detail"))
    with pytest.raises(ProgrammingError):
        handle_delivery(connection, channel, method, props, body, Mock(side_effect=permanent), stop)
    channel.basic_ack.assert_not_called()
    handle_delivery(
        connection, channel, method, props, body, Mock(side_effect=IdentityEventError), stop
    )
    channel.basic_reject.assert_called_once_with(1, requeue=False)


@pytest.fixture
def rabbitmq(tmp_path):
    root = Path(os.getenv("IDENTITY_MESSAGING_ROOT", "../datalens-messaging")).resolve()
    secrets = tmp_path / "broker"
    subprocess.run(
        [sys.executable, str(root / "prepare.py"), "--secrets-dir", str(secrets)],
        cwd=root,
        check=True,
    )
    env = os.environ | {
        "RABBITMQ_PORT": "0",
        "RABBITMQ_DEFINITIONS_FILE": str(secrets / "definitions.json"),
    }
    project = "agent-consumer-test-" + uuid4().hex[:12]

    def compose(*args):
        return subprocess.run(
            ["docker", "compose", "-p", project, *args],
            cwd=root,
            env=env,
            check=True,
            capture_output=True,
            text=True,
            timeout=200,
        ).stdout.strip()

    try:
        compose("up", "-d", "--wait", "--wait-timeout", "180")
        address = compose("port", "rabbitmq", "5672")
        assert address.startswith("127.0.0.1:")
        yield compose, address, secrets
    finally:
        compose("down", "--volumes")


@pytest.mark.postgres
def test_rabbitmq_consumer_commits_replays_and_quarantines(migration_settings, rabbitmq):
    compose, address, secrets = rabbitmq

    def url(username):
        password = (secrets / f"{username}.password").read_text(encoding="ascii").strip()
        return f"amqp://{username}:{password}@{address}/identity"

    event_id = str(uuid4())
    body = json.dumps(
        {
            "schema_version": 1,
            "event_id": event_id,
            "event_type": "user.created",
            "issuer": ISSUER,
            "subject": "broker-test",
            "occurred_at": "2026-11-11T12:00:00Z",
            "data": {
                "profile_revision": 1,
                "display_name": "Broker fixture",
                "username": None,
                "email": None,
            },
        }
    ).encode()
    with pika.BlockingConnection(pika.URLParameters(url("identity-publisher"))) as publisher:
        channel = publisher.channel()
        channel.confirm_delivery()
        for message_id in (event_id, event_id, str(uuid4())):
            channel.basic_publish(
                EXCHANGE,
                "user.created",
                body,
                pika.BasicProperties(message_id=message_id, delivery_mode=2),
                mandatory=True,
            )

    async def exercise():
        async with storage(migration_settings) as (database, _):
            projection = IdentityProjection(database, ISSUER)
            loop = asyncio.get_running_loop()
            stop, outcomes = Event(), []

            def apply(payload, *, delivery):
                future = asyncio.run_coroutine_threadsafe(
                    projection.apply(payload, delivery=delivery), loop
                )
                try:
                    outcomes.append(future.result(timeout=15))
                except IdentityEventError:
                    outcomes.append("rejected")
                    raise
                finally:
                    if len(outcomes) == 3:
                        stop.set()

            settings = ConsumerSettings(IDENTITY_CONSUMER_AMQP_URL=url("agent-identity-consumer"))
            watchdog = Timer(30, stop.set)
            watchdog.start()
            try:
                await asyncio.to_thread(consume_session, settings.parameters("test"), apply, stop)
            finally:
                watchdog.cancel()
            assert outcomes == [True, False, "rejected"]
            async with database.session() as session:
                assert await session.scalar(text("SELECT count(*) FROM agent_identity_inbox")) == 1
                assert (
                    await session.scalar(text("SELECT display_name FROM agent_user_profiles"))
                    == "Broker fixture"
                )

    run(exercise())
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        rows = json.loads(
            compose(
                "exec",
                "-T",
                "rabbitmq",
                "rabbitmqctl",
                "-q",
                "list_queues",
                "-p",
                "identity",
                "name",
                "messages_ready",
                "--formatter=json",
            )
        )
        counts = {row["name"]: row["messages_ready"] for row in rows}
        if counts == {QUEUE: 0, "agent.identity.dead.v1": 1}:
            return
        time.sleep(0.25)
    pytest.fail("Broker did not settle the deliveries")
