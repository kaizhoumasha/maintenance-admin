"""应用装配:Transport 适配器已注册给可靠发送器,配置目标后无需业务代码接线。"""

import logging

from sqlalchemy import select

from app.database import create_database_engine
from app.integrations.wms_transport import ADAPTERS, PROTOCOL
from app.main import app
from app.models.reliable import ReliableRetryBudget
from app.reliable.config import ReliableConfig, TargetConfig
from app.reliable_runtime import reliable_lifespan

WMS = "http://10.24.199.217:8284"


def test_the_application_registers_the_transport_adapter_for_the_sender():
    assert app.state.reliable_adapters[PROTOCOL] is ADAPTERS[PROTOCOL]


async def test_a_configured_wms_target_gets_an_http_client_and_no_startup_warning(
    monkeypatch, caplog, tmp_path, sqlite_tables
):
    import app.reliable_runtime as runtime

    class IdleSender:
        def __init__(self, *args, **kwargs):
            self.runner = None

        async def run(self):
            import asyncio

            await asyncio.Event().wait()

        async def aclose(self):
            self.runner.cancel()

    monkeypatch.setattr(runtime, "Sender", IdleSender)
    clients = []
    caplog.set_level(logging.WARNING)
    target = TargetConfig("wms-transport", WMS, PROTOCOL, max_not_received_attempts=3)
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'runtime.db'}"
    engine = create_database_engine(database_url)
    try:
        await sqlite_tables(engine)
        async with reliable_lifespan(
            ReliableConfig({"wms-transport": target}),
            app.state.reliable_adapters,
            database_url=database_url,
            client_factory=lambda config: clients.append(config.base_url) or object(),
        ) as outbox:
            assert outbox is not None and set(outbox.targets) == {"wms-transport"}
            async with engine.connect() as connection:
                budget = await connection.execute(
                    select(
                        ReliableRetryBudget.target,
                        ReliableRetryBudget.protocol,
                        ReliableRetryBudget.retry_limit,
                    )
                )
                assert budget.one() == ("wms-transport", PROTOCOL, 3)
    finally:
        await engine.dispose()
    assert clients == [WMS]
    assert "no registered adapter" not in caplog.text
