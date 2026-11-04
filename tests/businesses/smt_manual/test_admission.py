import pytest


@pytest.mark.parametrize("manual_sessions", ["sqlite", "postgres"], indirect=True)
@pytest.mark.parametrize(
    "decision,state",
    [("WORK_REQUIRED", "WORK_REQUIRED"), ("NO_WORK", "RELEASED"), ("WAIT", "WAIT")],
)
async def test_admission_decisions_keep_original_arrival(manual_bins, decision, state):
    flow = manual_bins
    await flow.ready_for_bin_work()
    assert (await flow.scan()).status_code == 200
    arrival = (await flow.rows("manual_bin"))[0]["arrival_id"]
    data = {"result": decision}
    data.update(
        {"task_id": "T1"}
        if decision == "WORK_REQUIRED"
        else {"retry_after_ms": 7}
        if decision == "WAIT"
        else {}
    )
    await flow.decision(data)
    assert await flow.advance("bin", arrival, now_ms=1000) == state
    assert await flow.advance("bin", arrival, now_ms=1001) == state
    assert len(await flow.rows("scan2_release", component=True)) == int(decision == "NO_WORK")
    if decision == "WAIT":
        assert (await flow.rows("admission"))[0]["due_at_ms"] == 1007


@pytest.mark.parametrize(
    "data",
    [
        {"result": "WORK_REQUIRED", "task_id": "OTHER"},
        {"result": "NO_WORK", "task_id": "T1"},
        {"result": "NO_WORK", "retry_after_ms": None},
        {"result": "WORK_REQUIRED", "task_id": "T1", "retry_after_ms": 1},
        {"result": "WAIT", "retry_after_ms": 0},
        {"result": "WAIT", "retry_after_ms": 60001},
        {"result": "WAIT", "retry_after_ms": "1"},
        {"result": "WAIT", "retry_after_ms": True},
        {"result": "WAIT", "retry_after_ms": None},
    ],
)
async def test_invalid_admission_response_never_releases(manual_bins, data):
    flow = manual_bins
    await flow.ready()
    assert (await flow.scan()).status_code == 200
    arrival = (await flow.rows("manual_bin"))[0]["arrival_id"]
    await flow.decision(data)
    assert await flow.advance("bin", arrival) == "ADMISSION_PENDING"
    assert len(await flow.rows("admission")) == 1
    assert not await flow.rows("scan2_release", component=True)


async def test_admission_release_is_atomic_with_outer_transaction(manual_bins):
    flow = manual_bins
    await flow.ready()
    assert (await flow.scan()).status_code == 200
    arrival = (await flow.rows("manual_bin"))[0]["arrival_id"]
    await flow.decision({"result": "NO_WORK"})
    assert await flow.advance("bin", arrival, rollback=True) == "RELEASED"
    assert (await flow.rows("manual_bin"))[0]["state"] == "ADMISSION_PENDING"
    assert not await flow.rows("scan2_release", component=True)
    assert await flow.advance("bin", arrival) == "RELEASED"


@pytest.mark.parametrize("failure", ["http", "code", "503", "unavailable", "not_found", "identity"])
async def test_uncertain_admission_keeps_original_identity(manual_bins, failure):
    from sqlalchemy import delete, select, update

    from app.businesses.smt_manual.manual_bin_flow import ManualBinFlow
    from app.models import ReliableMessage

    flow = manual_bins
    await flow.ready()
    assert (await flow.scan()).status_code == 200
    original = (await flow.rows("admission"))[0]
    async with flow.factory() as session:
        frozen = (await session.get(ReliableMessage, original["message_id"])).request_raw
    if failure == "http":
        await flow.decision({"result": "NO_WORK"}, http=202)
    elif failure == "code":
        await flow.decision({"result": "NO_WORK"}, code="RECEIVED")
    elif failure == "503":
        await flow.decision({}, http=503, code="UNAVAILABLE")
    else:
        async with flow.factory.begin() as session:
            query = (
                delete(ReliableMessage) if failure == "not_found" else update(ReliableMessage)
            ).where(ReliableMessage.id == original["message_id"])
            if failure == "unavailable":
                query = query.values(status="DONE", response_raw=None)
            elif failure == "identity":
                query = query.values(operation="sorting.task.prepare@v1")
            await session.execute(query)
    flow.manual = ManualBinFlow(
        flow.manual.capabilities, flow.roller, repository=flow.task.repository
    )
    arrival = original["arrival_id"]
    for now in (1000, 2000):
        assert await flow.advance("bin", arrival, now_ms=now) == "ADMISSION_PENDING"
    assert (await flow.rows("admission"))[0] == original
    assert not await flow.rows("scan2_release", component=True)
    async with flow.factory() as session:
        message = await session.scalar(
            select(ReliableMessage).where(ReliableMessage.id == original["message_id"])
        )
        assert message is None if failure == "not_found" else message.request_raw == frozen


async def test_unknown_admission_notes_do_not_change_a_known_decision(manual_bins):
    flow = manual_bins
    await flow.ready()
    assert (await flow.scan()).status_code == 200
    arrival = (await flow.rows("manual_bin"))[0]["arrival_id"]
    await flow.decision({"result": "NO_WORK", "note": "No manual work"})
    assert await flow.advance("bin", arrival) == "RELEASED"


async def test_admission_response_requires_positive_wire_timestamp(manual_bins):
    flow = manual_bins
    await flow.ready()
    assert (await flow.scan()).status_code == 200
    arrival = (await flow.rows("manual_bin"))[0]["arrival_id"]
    await flow.decision({"result": "NO_WORK"}, timestamp=0)
    assert await flow.advance("bin", arrival) == "ADMISSION_PENDING"
    assert not await flow.rows("scan2_release", component=True)


@pytest.mark.parametrize("retry_after_ms", [1, 60000])
async def test_wait_accepts_exact_wire_bounds(manual_bins, retry_after_ms):
    flow = manual_bins
    await flow.ready()
    assert (await flow.scan()).status_code == 200
    arrival = (await flow.rows("manual_bin"))[0]["arrival_id"]
    await flow.decision({"result": "WAIT", "retry_after_ms": retry_after_ms})
    assert await flow.advance("bin", arrival, now_ms=1000) == "WAIT"
    assert (await flow.rows("admission"))[0]["due_at_ms"] == 1000 + retry_after_ms


async def test_terminal_task_does_not_resume_original_admission(manual_bins):
    from sqlalchemy import update

    from app.models import WorkLine

    flow = manual_bins
    await flow.ready()
    assert (await flow.scan()).status_code == 200
    arrival = (await flow.rows("manual_bin"))[0]["arrival_id"]
    async with flow.factory.begin() as session:
        await session.execute(update(flow.tables["task"]).values(status="COMPLETED"))
        await session.execute(
            update(WorkLine).where(WorkLine.id == flow.lines[0]).values(is_enabled=False)
        )
    await flow.decision({"result": "NO_WORK"})
    assert await flow.advance("bin", arrival) == "CLOSED"
    assert not await flow.rows("scan2_release", component=True)
