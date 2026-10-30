import asyncio

import pytest
from sqlalchemy import delete, event, select
from sqlalchemy.sql.dml import Insert

from app.businesses import catalog
from app.core.identifiers import uuid7
from app.integrations.wms_request import InvalidRequestPayload, WmsRequests
from app.models import ReliableMessage


@pytest.mark.parametrize("competitor_owner", ["first", "second"])
async def test_concurrent_shared_request_preserves_original_owner(
    pg_sessions, wms, shared_wms_contract, competitor_owner
):
    factory = pg_sessions
    contract = shared_wms_contract
    first = WmsRequests(
        wms.outbox, owner_domain="first", contract_consumers=catalog.contract_consumers
    )
    other = WmsRequests(
        wms.outbox, owner_domain=competitor_owner, contract_consumers=catalog.contract_consumers
    )
    identity = str(uuid7())
    payload = contract.model(reference="original")
    inserting = asyncio.Event()

    def observe(conn, cursor, statement, parameters, context, executemany):
        compiled = context.compiled
        if (
            compiled is not None
            and isinstance(compiled.statement, Insert)
            and compiled.statement.table.name == ReliableMessage.__tablename__
        ):
            inserting.set()

    pending = None
    engine = factory.kw["bind"]
    async with factory() as session:
        original = await first.send(session, payload, operation_id=identity)
        row = await session.get(ReliableMessage, original.message_id)
        frozen = row.request_raw

        async def compete():
            async with factory.begin() as second:
                return await other.send(second, payload, operation_id=identity)

        event.listen(engine.sync_engine, "before_cursor_execute", observe)
        try:
            pending = asyncio.create_task(compete())
            async with asyncio.timeout(5):
                await inserting.wait()
                await session.commit()
                if competitor_owner == "first":
                    again = await pending
                    assert (
                        again.message_id == original.message_id
                        and again.business_key == "business:first"
                    )
                else:
                    with pytest.raises(InvalidRequestPayload, match="business"):
                        await pending
            async with factory() as reader:
                row = await reader.get(ReliableMessage, original.message_id)
                assert row.business_key == "business:first" and row.request_raw == frozen
                assert (
                    len(
                        list(
                            await reader.scalars(
                                select(ReliableMessage).where(
                                    ReliableMessage.operation_id == identity
                                )
                            )
                        )
                    )
                    == 1
                )
        finally:
            await session.rollback()
            event.remove(engine.sync_engine, "before_cursor_execute", observe)
            if pending is not None:
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
            async with factory.begin() as cleanup:
                await cleanup.execute(
                    delete(ReliableMessage).where(ReliableMessage.operation_id == identity)
                )
