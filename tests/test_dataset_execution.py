"""Thread offload/admission/cancellation without provider or database access."""

import asyncio
import threading

import pytest

from statistics_agent.contracts.datasets import DatasetLimits
from statistics_agent.datasets.csv import CsvValidationError, parse_csv
from statistics_agent.datasets.execution import (
    DatasetParser,
    DatasetParserBusyError,
    DatasetParserClosedError,
)


@pytest.mark.parametrize("workers", [0, 5, True, "2"])
def test_worker_count_is_bounded(workers):
    with pytest.raises(ValueError):
        DatasetParser(workers=workers)


def test_parse_offloads_work_and_rejects_excess_instead_of_queueing(monkeypatch):
    started, release = threading.Event(), threading.Event()
    threads = []

    def controlled(content, *, limits):
        threads.append(threading.get_ident())
        started.set()
        assert release.wait(5), "Test did not release parser"
        return parse_csv(content, limits=limits)

    monkeypatch.setattr("statistics_agent.datasets.execution.parse_csv", controlled)

    async def exercise():
        async with DatasetParser(workers=1) as parser:
            task = asyncio.create_task(parser.parse(b"x\n4", limits=DatasetLimits()))
            try:
                assert await asyncio.to_thread(started.wait, 3)
                assert len(threads) == 1 and threads[0] != threading.get_ident()
                with pytest.raises(DatasetParserBusyError, match="busy"):
                    await parser.parse(b"x\n5", limits=DatasetLimits())
            finally:
                release.set()
            assert (await task).rows == (("4",),)
            assert (await parser.parse(b"x\n6", limits=DatasetLimits())).rows == (("6",),)

    asyncio.run(exercise())


def test_cancelled_caller_keeps_slot_until_thread_finishes_and_errors_are_observed(monkeypatch):
    started, release = threading.Event(), threading.Event()

    def controlled(content, *, limits):
        started.set()
        assert release.wait(5), "Test did not release parser"
        return parse_csv(content, limits=limits)

    monkeypatch.setattr("statistics_agent.datasets.execution.parse_csv", controlled)

    async def exercise():
        loop = asyncio.get_running_loop()
        errors = []
        loop.set_exception_handler(lambda loop, context: errors.append(context))
        parser = DatasetParser(workers=1)
        task = asyncio.create_task(parser.parse(b'x\n"unclosed', limits=DatasetLimits()))
        try:
            assert await asyncio.to_thread(started.wait, 3)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            with pytest.raises(DatasetParserBusyError):
                await parser.parse(b"x\n4", limits=DatasetLimits())
        finally:
            release.set()
            await parser.aclose()
        await asyncio.sleep(0)
        with pytest.raises(DatasetParserClosedError, match="unavailable"):
            await parser.parse(b"x\n4", limits=DatasetLimits())
        await parser.aclose()
        assert errors == []

    asyncio.run(exercise())


def test_parse_failure_releases_admission_for_next_request():
    async def exercise():
        async with DatasetParser(workers=1) as parser:
            with pytest.raises(CsvValidationError):
                await parser.parse(b'x\n"bad', limits=DatasetLimits())
            assert (await parser.parse(b"x\n1", limits=DatasetLimits())).row_count == 1

    asyncio.run(exercise())


def test_server_owned_computation_offloads_and_shares_parser_admission():
    started, release = threading.Event(), threading.Event()

    def controlled():
        started.set()
        assert release.wait(5)
        return threading.get_ident()

    async def exercise():
        async with DatasetParser(workers=1) as parser:
            task = asyncio.create_task(parser.compute(controlled))
            try:
                assert await asyncio.to_thread(started.wait, 3)
                with pytest.raises(DatasetParserBusyError):
                    await parser.parse(b"x\n4", limits=DatasetLimits())
            finally:
                release.set()
            assert await task != threading.get_ident()
            assert (await parser.parse(b"x\n4", limits=DatasetLimits())).row_count == 1

    asyncio.run(exercise())
