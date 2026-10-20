import asyncio
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core import get_settings
from app.http_client import AsyncHttpClient, HttpClient, HttpClientConfig
from app.reliable import Outbox, ProtocolAdapter
from app.reliable.config import ReliableConfig
from app.reliable.sender import Sender
from app.reliable.store import MessageStore


@asynccontextmanager
async def reliable_lifespan(
    config: ReliableConfig,
    adapters: Mapping[str, ProtocolAdapter],
    *,
    database_url: str | None = None,
    client_factory: Callable[[HttpClientConfig], HttpClient] = AsyncHttpClient,
) -> AsyncIterator[Outbox | None]:
    url = database_url or get_settings().DATABASE_URL
    pool = dict(
        pool_size=config.db_pool_size, max_overflow=0, pool_timeout=config.db_pool_timeout_s
    )
    engine = create_async_engine(
        url, hide_parameters=True, **(pool if url.startswith("postgresql") else {})
    )
    clients = {}
    store = MessageStore(
        async_sessionmaker(engine, expire_on_commit=False), lease_margin_s=config.lease_margin_s
    )
    sender = Sender(store, config.targets, adapters, clients, budget=config, dispose=engine.dispose)
    try:
        for name, target in config.targets.items():
            if target.protocol in adapters:
                clients[name] = client_factory(target.http_config())
        sender.runner = asyncio.create_task(sender.run())
        yield Outbox(config.targets, adapters) if config.targets else None
    finally:
        await sender.aclose()
