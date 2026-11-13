"""Pooled HTTP client for AI provider requests, one per running event loop."""
from __future__ import annotations

import asyncio
import weakref

import httpx

from ..config import get_config

# httpx clients are bound to the loop that first uses them, so each loop gets
# its own. Keys are weak so a finished loop's entry disappears with it.
_clients: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, httpx.AsyncClient] = (
    weakref.WeakKeyDictionary()
)


def get_client() -> httpx.AsyncClient:
    loop = asyncio.get_running_loop()
    client = _clients.get(loop)
    if client is None or client.is_closed:
        client = httpx.AsyncClient(timeout=get_config().ai.timeout)
        _clients[loop] = client
    return client


async def close_client() -> None:
    client = _clients.pop(asyncio.get_running_loop(), None)
    if client is not None and not client.is_closed:
        await client.aclose()
