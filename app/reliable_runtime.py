import asyncio
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core import get_settings
from app.database import create_database_engine as create_async_engine
from app.diagnostics import log_exception
from app.http_client import AsyncHttpClient, HttpClient, HttpClientConfig
from app.integrations import history as history_records
from app.integrations.http_exchanges import reconcile_outbound
from app.integrations.runtime_authority import require_reconciled
from app.reliable import Outbox, OutgoingMessage, ProtocolAdapter
from app.reliable.config import ReliableConfig
from app.reliable.sender import Sender
from app.reliable.store import AttemptOutcome, Claim, MessageStore

logger = logging.getLogger(__name__)


@asynccontextmanager
async def reliable_lifespan(
    config: ReliableConfig,
    adapters: Mapping[str, ProtocolAdapter],
    *,
    database_url: str | None = None,
    client_factory: Callable[[HttpClientConfig], HttpClient] = AsyncHttpClient,
    lifecycles=None,
    client_provider=None,
    on_result: Callable[[str, str | None, str], None] | None = None,
    observe_delivered: Callable[[Claim, AttemptOutcome], Awaitable[None]] | None = None,
    on_delivered: Callable[[AsyncSession, OutgoingMessage], Awaitable[bool | None]] | None = None,
) -> AsyncIterator[Outbox | None]:
    url = database_url or get_settings().DATABASE_URL
    pool = dict(
        pool_size=config.db_pool_size, max_overflow=0, pool_timeout=config.db_pool_timeout_s
    )
    engine = create_async_engine(
        url, hide_parameters=True, **(pool if url.startswith("postgresql") else {})
    )
    clients = {}
    factory = async_sessionmaker(engine, expire_on_commit=False)
    store = MessageStore(
        factory,
        lease_margin_s=config.lease_margin_s,
        lifecycles=lifecycles,
        served_snapshot_protocols=set(adapters) if client_provider is not None else set(),
        on_result=on_result,
        on_delivered=on_delivered,
        records=history_records,
    )
    sender = Sender(
        store,
        config.targets,
        adapters,
        clients,
        budget=config,
        dispose=engine.dispose,
        provider=client_provider,
        observe_delivered=observe_delivered,
    )

    async def project_pending():
        last_failure = float("-inf")
        while True:
            try:
                async with asyncio.timeout(config.poll_interval_s):
                    await reconcile_outbound(
                        factory,
                        retention_days=get_settings().INTEGRATION_HISTORY_RETENTION_DAYS,
                    )
            except Exception as error:
                now = time.monotonic()
                if now - last_failure >= 60:
                    log_exception(
                        logger,
                        "HTTP exchange projection failed",
                        error,
                        component="http_exchange",
                        phase="reconcile_outbound",
                    )
                    last_failure = now
            await asyncio.sleep(config.poll_interval_s)

    projection = None
    try:
        await require_reconciled(factory)
        for name, target in config.targets.items():
            if target.address_mode == "SNAPSHOT":
                if client_provider is None or target.protocol not in (lifecycles or {}):
                    logger.error(
                        "snapshot target %r unavailable: explicit lifecycle/client "
                        "assembly required",
                        name,
                    )
                continue
            if target.protocol in adapters:
                clients[name] = client_factory(target.http_config())
            else:
                # 入队不依赖发送器的适配器注册,漏注册会让消息静默停在 PENDING,所以启动时就报出来。
                logger.warning(
                    "reliable target %r protocol %r has no registered adapter: "
                    "messages are enqueued but never sent",
                    name,
                    target.protocol,
                )
        sender.runner = asyncio.create_task(sender.run())
        if config.targets:
            projection = asyncio.create_task(project_pending())
        yield (
            Outbox(config.targets, adapters, lifecycles=lifecycles, records=history_records)
            if config.targets
            else None
        )
    finally:
        if projection is not None:
            projection.cancel()
            await asyncio.gather(projection, return_exceptions=True)
        await sender.aclose()
