"""Unauthenticated process probes with bounded, read-only database readiness."""

import asyncio

from fastapi import APIRouter, Request
from sqlalchemy import text
from starlette.responses import JSONResponse

from statistics_agent.api.http import error_response
from statistics_agent.contracts.http import ErrorEnvelope
from statistics_agent.persistence.schema import read_schema_status

router = APIRouter(prefix="/health", tags=["health"])
NO_STORE = {"Cache-Control": "no-store"}


@router.get("/live")
async def live() -> JSONResponse:
    """A responding process is alive, independently of infrastructure."""
    return JSONResponse({"status": "alive"}, headers=NO_STORE)


@router.get("/ready", responses={503: {"model": ErrorEnvelope, "description": "Service not ready"}})
async def ready(request: Request) -> JSONResponse:
    database = request.app.state.database
    available = False
    if request.app.state.started and database is not None:
        try:
            # The deadline covers pool acquisition, SQL and schema inspection.
            # The context managers also release/rollback the probe connection.
            async with asyncio.timeout(request.app.state.settings.db_health_timeout_seconds):
                async with database.engine.connect() as connection:
                    if await connection.scalar(text("SELECT 1")) == 1:
                        available = (await read_schema_status(connection)).is_current
        except Exception:
            # Infrastructure/version details stay private; external cancellation
            # is a BaseException and is deliberately allowed to propagate.
            available = False
    if available:
        return JSONResponse({"status": "ready"}, headers=NO_STORE)
    response = error_response(request.state.request_id, 503, "not_ready", "Service not ready")
    response.headers.update(NO_STORE)
    return response
