"""Publisher command behavior and redacted diagnostics."""

import asyncio
import logging
from contextlib import asynccontextmanager

import pytest

from statistics_agent import publisher
from support.database import run


@pytest.fixture(autouse=True)
def restore_sdk_logger(monkeypatch):
    logger = logging.getLogger("aiokafka")
    monkeypatch.setattr(logger, "disabled", logger.disabled)
    monkeypatch.setattr(logger, "level", logger.level)


def test_cli_failure_never_exposes_private_sdk_diagnostics(monkeypatch, capsys):
    async def failed(env_file):
        raise RuntimeError("private endpoint/password details")

    monkeypatch.setattr(publisher, "serve", failed)
    assert publisher.main([]) == 1
    captured = capsys.readouterr()
    assert captured.out == "Kafka publisher stopped; check configuration and service availability\n"
    assert not captured.err


def test_cli_uses_explicit_environment_source(monkeypatch):
    called = []

    async def stopped(env_file):
        called.append(env_file)

    monkeypatch.setattr(publisher, "serve", stopped)
    assert publisher.main(["--env-file", "test.env"]) == 0
    assert called == ["test.env"]


def test_disconnected_broker_warns_and_retries_without_provisioning(capsys):
    async def exercise():
        stop, attempts, delays = asyncio.Event(), [], []

        class Disconnected(publisher.OutboxPublisher):
            @asynccontextmanager
            async def epoch(self):
                attempts.append(1)
                if len(attempts) <= 2:
                    raise ConnectionError("private broker endpoint")
                yield object()

            async def publish_one(self, epoch):
                stop.set()
                return True

            async def _pause(self, stop, seconds):
                delays.append(seconds)

        instance = Disconnected(None, None, producer_factory=lambda: None)
        await instance.run(stop)
        assert len(attempts) == 3
        assert delays == [1, 2]

    run(exercise())
    output = capsys.readouterr().out
    assert output.count("pending events retained for retry") == 2
    assert "private broker endpoint" not in output
