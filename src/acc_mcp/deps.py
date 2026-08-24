"""Per-connection client cache.

The server is multi-tenant: one deployment serves any number of ACCv7
instances. Each distinct (instance, operator) gets its own client and therefore
its own session and tokens — tenants never share credentials or sessions.

Clients are cached so repeat calls from the same connection reuse one logon.
The cache is bounded and evicts least-recently-used entries.
"""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict

from .acc.v7.client import Acc7Client, build_client
from .config import ConnectionConfig, get_server_settings, resolve_connection

logger = logging.getLogger(__name__)

_clients: OrderedDict[ConnectionConfig, Acc7Client] = OrderedDict()
_lock = asyncio.Lock()


async def get_client_for(connection: ConnectionConfig) -> Acc7Client:
    """Return the cached client for this connection, building it if needed."""
    async with _lock:
        client = _clients.get(connection)
        if client is not None:
            _clients.move_to_end(connection)
            return client

        settings = get_server_settings()
        client = build_client(connection, settings)
        _clients[connection] = client

        while len(_clients) > settings.acc_max_cached_connections:
            _, evicted = _clients.popitem(last=False)
            logger.info("Evicting cached connection %s", evicted.connection.base_url)
            asyncio.create_task(evicted.aclose())  # noqa: RUF006

        return client


async def get_client() -> Acc7Client:
    """Resolve the current request's connection and return its client."""
    return await get_client_for(resolve_connection())


async def close_all_clients() -> None:
    """Release every cached client. Used on shutdown and in tests."""
    async with _lock:
        clients = list(_clients.values())
        _clients.clear()
    for client in clients:
        await client.aclose()
