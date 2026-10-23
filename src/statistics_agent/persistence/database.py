"""Bounded asynchronous connections and explicit transaction ownership."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from statistics_agent.core.settings import Settings


class Database:
    """One pool per process/lifespan; one session per operation.

    Constructing the pool performs no I/O. Callers explicitly commit or roll back;
    leaving a session without committing rolls back pending work and releases its
    connection. Do not share sessions across concurrent tasks or remote calls.
    """

    def __init__(self, settings: Settings) -> None:
        self.engine = create_async_engine(
            settings.database_url.get_secret_value(),
            pool_size=settings.db_pool_size,
            max_overflow=0,
            pool_timeout=settings.db_pool_timeout_seconds,
            pool_pre_ping=True,
            echo=False,
            hide_parameters=True,
            connect_args={
                "connect_timeout": settings.db_connect_timeout_seconds,
                "options": f"-c statement_timeout={settings.db_statement_timeout_ms}",
                "application_name": settings.service_name,
            },
        )
        self._sessions = async_sessionmaker(self.engine, expire_on_commit=False, autoflush=False)
        self._closed = False

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        if self._closed:
            raise RuntimeError("Database lifecycle has ended")
        async with self._sessions() as session:
            yield session

    async def close(self) -> None:
        """Dispose idle connections after operations finish; prohibit future sessions."""
        self._closed = True
        await self.engine.dispose()
