"""A current request cannot be consumed or replaced through another subject."""

from sqlalchemy import update


async def test_confirmation_pointer_keeps_original_subject(manual_completion_tools):
    flow = manual_completion_tools
    await flow.ready()
    await flow.bin_plan(cancel=True)
    await flow.confirm()
    original = (await flow.rows("confirmation"))[0]
    requests = flow.tables["request"]
    async with flow.factory.begin() as session:
        await session.execute(
            update(requests)
            .where(requests.c.kind == "COMPLETION_CONFIRM")
            .values(task_id="ANOTHER")
        )
    assert await flow.confirm() == "EXECUTING"
    current = await flow.rows("confirmation")
    assert len(current) == 1 and current[0]["operation_id"] == original["operation_id"]


async def test_admission_pointer_keeps_original_arrival(manual_bins):
    flow = manual_bins
    await flow.ready()
    await flow.scan()
    work = (await flow.rows("manual_bin"))[0]
    requests = flow.tables["request"]
    async with flow.factory.begin() as session:
        await session.execute(
            update(requests).where(requests.c.kind == "BIN_ADMISSION").values(arrival_id="ANOTHER")
        )
    assert await flow.advance("bin", work["arrival_id"]) == "ADMISSION_PENDING"
    assert len(await flow.rows("admission")) == 1
    assert await flow.rows("scan2_release", component=True) == []


async def test_report_pointer_keeps_original_rack(manual_racks):
    flow = manual_racks
    await flow.ready()
    await flow.plan()
    await flow.reconcile()
    await flow.rack_outcome()
    await flow.rack_advance()
    await flow.rack_response("rack_report", {}, code="RECORDED")
    requests = flow.tables["request"]
    async with flow.factory.begin() as session:
        await session.execute(
            update(requests)
            .where(requests.c.kind == "RACK_ARRIVAL_REPORT")
            .values(rack_work_id="ANOTHER")
        )
    assert await flow.consume_report() == "OWNER_CONFLICT"
    assert (await flow.rows("rack_report"))[0]["state"] == "REQUESTED"
