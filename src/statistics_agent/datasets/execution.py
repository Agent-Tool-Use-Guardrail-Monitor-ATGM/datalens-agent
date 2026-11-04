"""Bounded asynchronous admission for CSV parsing and server-owned computation."""

import asyncio
import contextvars
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from functools import partial

from statistics_agent.contracts.datasets import DatasetLimits
from statistics_agent.datasets.csv import CsvValidationError, ParsedDataset, parse_csv


class DatasetParserBusyError(RuntimeError):
    def __init__(self):
        super().__init__("Dataset parsing is busy")


class DatasetParserClosedError(RuntimeError):
    def __init__(self):
        super().__init__("Dataset parsing is unavailable")


def _observe(future: asyncio.Future) -> None:
    # A cancelled caller may no longer await the shielded parse. Retrieve its
    # exception without logging private input or suppressing it for active callers.
    if not future.cancelled():
        future.exception()


class DatasetParser:
    """Share one instance per process; callers do not own a parser per request.

    Reject excess work immediately instead of queuing requests. A slot belongs
    to the concurrent future, not the awaiting task, until actual work completes.
    Cancellation cannot kill a running Python thread.
    """

    def __init__(self, *, workers: int = 2):
        if type(workers) is not int or not 1 <= workers <= 4:
            raise ValueError("Parser workers must be between 1 and 4")
        self._executor = ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix="dataset-parser"
        )
        self._slots = threading.BoundedSemaphore(workers)
        self._lock = threading.Lock()
        self._closed = False

    async def parse(self, content: bytes, *, limits: DatasetLimits) -> ParsedDataset:
        bounds = DatasetLimits.model_validate(limits)
        if type(content) is not bytes or not content:
            raise CsvValidationError("invalid_csv")
        if len(content) > bounds.max_upload_bytes:
            raise CsvValidationError("dataset_limit_exceeded")
        return await self._run(partial(parse_csv, content, limits=bounds))

    async def compute[T](self, operation: Callable[[], T]) -> T:
        """Offload a server-owned numerical function, never model-provided code."""
        return await self._run(operation)

    async def _run[T](self, operation: Callable[[], T]) -> T:
        with self._lock:
            if self._closed:
                raise DatasetParserClosedError
            if not self._slots.acquire(blocking=False):
                raise DatasetParserBusyError
            try:
                future = self._executor.submit(contextvars.copy_context().run, operation)
            except BaseException:
                self._slots.release()
                raise
            future.add_done_callback(lambda finished: self._slots.release())
        wrapped = asyncio.wrap_future(future)
        wrapped.add_done_callback(_observe)
        # Do not cancel queued concurrent futures and accumulate cancelled queue
        # entries under repeated submit/cancel cycles. Admitted work stays bounded.
        return await asyncio.shield(wrapped)

    async def aclose(self) -> None:
        with self._lock:
            self._closed = True
        # Shutdown must not block the event loop while admitted work finishes.
        await asyncio.to_thread(self._executor.shutdown, wait=True, cancel_futures=False)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        await self.aclose()
