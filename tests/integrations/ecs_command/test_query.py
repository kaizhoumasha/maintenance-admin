import asyncio
import json

import pytest
from sqlalchemy import text

from app.integrations.ecs_command.lifecycle import EcsLifecycle
from app.integrations.ecs_command.protocol import PROTOCOL
from app.integrations.ecs_command.result import receive_ecs_result
from app.reliable.store import MessageStore


async def test_example_consumer_advances_once_in_its_own_transaction(ecs):
    if ecs.factory.kw["bind"].dialect.name != "postgresql":
        pytest.skip("PostgreSQL consumer concurrency")
    async with ecs.factory.begin() as session:
        command = await ecs.service.create(
            session,
            owner=ecs.owner,
            call_key="call",
            device_code="D1",
            task_type="MOVE",
            timeout_ms=30000,
            params={"qty": 1},
        )
        await session.execute(
            text(
                "CREATE TABLE ecs_test_consumer (consumer TEXT, command_code TEXT, PRIMARY "
                "KEY(consumer,command_code))"
            )
        )
    try:
        store = MessageStore(ecs.factory, lifecycles={PROTOCOL: EcsLifecycle(ecs.service)})
        claim = (await store.claim(1, ecs.targets))[0]
        await store.before_send(claim, ecs.targets["ecs"])
        async with ecs.factory.begin() as session:
            await receive_ecs_result(
                session,
                json.dumps(
                    dict(
                        command_code=command.command_code,
                        device_code="D1",
                        result="SUCCESS",
                        finish_time=1,
                        data={"qty": 1},
                    )
                ).encode(),
                ecs.service,
            )

        async def consume():
            async with ecs.factory.begin() as session:
                view = await ecs.service.get_command(
                    session, owner=ecs.owner, command_code=command.command_code
                )
                assert view.result["result"] == "SUCCESS"
                return await session.scalar(
                    text(
                        "INSERT INTO ecs_test_consumer VALUES (:owner,:code) "
                        "ON CONFLICT DO NOTHING "
                        "RETURNING command_code"
                    ),
                    {"owner": ecs.owner.domain, "code": view.command_code},
                )

        winners = await asyncio.gather(consume(), consume())
        assert sum(w is not None for w in winners) == 1
    finally:
        async with ecs.factory.begin() as session:
            await session.execute(text("DROP TABLE ecs_test_consumer"))
