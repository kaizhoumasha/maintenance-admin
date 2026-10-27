import pytest
from sqlalchemy import event, update

from app.integrations.wms_transport import TransportService
from app.models import TransportTask
from tests.helpers import result_task, transport_callback


async def query(session, task, ids=None, **options):
    return await TransportService().list_unclosed_predecessors(
        session,
        caller=task.caller,
        cycle_id=task.cycle_id,
        predecessor_ids=ids if ids is not None else (task.handle.transport_task_id,),
        **({"rack_id": "R1"} | options),
    )


async def test_historical_member_facts_do_not_override_newer_unknown(transport_rig):
    factory = transport_rig.factory
    task = await result_task(factory, bins=True)
    await transport_callback(factory, task, bins=True)
    async with factory() as session:
        first = await query(session, task)
        assert first.complete and first.unresolved == ()
        assert first.items[0].task_closure == "UNDETERMINED"
        assert first.items[0].member_ids == ("C1",)
    await transport_callback(factory, task, bins=True, revision=2, known=False)
    async with factory() as session:
        second = await query(session, task)
        item = second.items[0]
        assert item.closure == "UNDETERMINED" and item.outcome_revision == 2
        result = await task.service.get_result(
            session,
            transport_task_id=task.handle.transport_task_id,
            client_request_id=task.handle.client_request_id,
            caller=task.caller,
        )
        assert result.member_facts["C1"]["TARGET_PLACED"]["rack_id"] == "R1"
        assert result.closure == "UNDETERMINED"


async def test_predecessors_use_explicit_ids_and_original_scope(transport_rig):
    factory = transport_rig.factory
    task = await result_task(factory)
    other = await result_task(factory)
    await transport_callback(factory, task)
    async with factory() as session:
        found = await query(session, task)
        assert len(found.items) == 1 and found.complete and not found.unresolved
        invalid = await query(
            session,
            task,
            (task.handle.transport_task_id, other.handle.transport_task_id, "missing"),
        )
        assert len(invalid.items) == 3 and len(invalid.unresolved) == 2 and not invalid.complete
        assert invalid.items[1].reason == "SCOPE_MISMATCH"
        assert invalid.items[2].reason == "TASK_NOT_FOUND"


@pytest.mark.parametrize(
    "ids,options",
    [
        (("a", "a"), {}),
        (tuple(str(i) for i in range(101)), {}),
        (("a",), {"container_id": "C1"}),
        (("a",), {"rack_id": None}),
    ],
)
async def test_predecessor_query_never_silently_truncates(transport_rig, ids, options):
    task = await result_task(transport_rig.factory)
    async with transport_rig.factory() as session:
        with pytest.raises(ValueError):
            await query(session, task, ids, **options)


async def test_empty_and_hundred_ids_and_single_statement(transport_rig):
    task = await result_task(transport_rig.factory)
    statements = []

    def capture(conn, cursor, statement, parameters, context, many):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    event.listen(transport_rig.engine.sync_engine, "before_cursor_execute", capture)
    try:
        async with transport_rig.factory() as session:
            result = await query(session, task, ())
            assert result.complete and result.items == () and result.unresolved == ()
            result = await query(session, task, tuple(str(i) for i in range(100)))
            assert (
                len(result.items) == 100 and len(result.unresolved) == 100 and not result.complete
            )
        assert len(statements) == 1 and "FOR UPDATE" not in statements[0]
    finally:
        event.remove(transport_rig.engine.sync_engine, "before_cursor_execute", capture)


async def test_malformed_applied_result_cannot_close_a_resource(transport_rig):
    task = await result_task(transport_rig.factory, bins=True)
    await transport_callback(transport_rig.factory, task, bins=True)
    async with transport_rig.factory.begin() as session:
        await session.execute(
            update(TransportTask)
            .where(TransportTask.transport_task_id == task.handle.transport_task_id)
            .values(applied_outcome_revision=99)
        )
    async with transport_rig.factory() as session:
        result = await query(session, task)
        assert result.items[0].closure == "UNDETERMINED" and not result.complete
