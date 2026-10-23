"""Supported async runtime for Psycopg on Windows and Linux."""

import asyncio
import sys


def create_event_loop() -> asyncio.AbstractEventLoop:
    """Uvicorn custom loop factory; avoid Windows' unsupported Proactor loop."""
    if sys.platform == "win32":
        return asyncio.SelectorEventLoop()
    return asyncio.new_event_loop()
