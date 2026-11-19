"""Process-local inference admission; not a distributed provider quota service."""

import asyncio
import threading
from contextlib import asynccontextmanager


class RunStopped(RuntimeError):
    """Fixed application reason, without provider text or secrets."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class ModelLimiter:
    def __init__(self, capacity: int):
        self.capacity = capacity
        self._slots = threading.BoundedSemaphore(capacity)

    @asynccontextmanager
    async def slot(self):
        # Nonblocking acquisition avoids a cancelled background acquire leaking a slot.
        while not self._slots.acquire(blocking=False):
            await asyncio.sleep(0.01)
        try:
            yield
        finally:
            self._slots.release()


_registry_lock = threading.Lock()
_limiters: dict[str, ModelLimiter] = {}


def provider_limiter(provider: str, capacity: int) -> ModelLimiter:
    """Share admission across sessions/event loops in this process; restart to resize."""
    with _registry_lock:
        if provider not in _limiters:
            _limiters[provider] = ModelLimiter(capacity)
        limiter = _limiters[provider]
        if limiter.capacity != capacity:
            raise ValueError("Conflicting provider concurrency; restart to reconfigure")
        return limiter
