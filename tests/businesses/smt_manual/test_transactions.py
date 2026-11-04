import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session


@pytest.mark.parametrize("failures", [1, 3])
async def test_serialization_retries_whole_advance_in_new_session(manual_host, failures):
    flow = manual_host
    await flow.issue()
    sessions, isolations = [], []

    def started(session, transaction, connection):
        sessions.append(session)
        isolations.append(connection.get_isolation_level())

    def committing(session):
        if len(sessions) <= failures:
            session.execute(
                text("DO $$ BEGIN RAISE EXCEPTION 'retry' USING ERRCODE='40001'; END $$")
            )

    event.listen(Session, "after_begin", started)
    event.listen(Session, "before_commit", committing)
    try:
        if failures == 3:
            with pytest.raises(DBAPIError):
                await flow.host.advance("TASK", "T1")
        else:
            assert await flow.host.advance("TASK", "T1") == "PREPARING"
    finally:
        event.remove(Session, "after_begin", started)
        event.remove(Session, "before_commit", committing)
    assert len(sessions) == (3 if failures == 3 else 2)
    assert len({id(s) for s in sessions}) == len(sessions)
    assert set(isolations) == {"SERIALIZABLE"}
    tasks = await flow.rows("task")
    assert tasks[0]["status"] == ("QUEUED" if failures == 3 else "PREPARING")
    if failures == 3:
        assert tasks[0]["prepare_operation_id"] is None
        assert await flow.host.scan_pending() == 1
    async with flow.factory() as session:
        assert await session.scalar(text("SHOW transaction_isolation")) == "read committed"


@pytest.mark.parametrize(
    "kind",
    [
        "TASK",
        "MANUAL_BIN",
        "MANUAL_COMPLETION",
        "MANUAL_RACK",
        "SCAN2_RELEASE",
        "RACK_REPORT",
        "PASSAGE",
        "RETURN",
        "FACE",
        "RACK_CYCLE",
        "DRAIN",
    ],
)
async def test_isolation_matches_original_work_kind(manual_host, kind, monkeypatch):
    flow = manual_host
    seen = []

    async def inspect_scope(session, original_kind, record_id):
        seen.append(
            (original_kind, record_id, await session.scalar(text("SHOW transaction_isolation")))
        )
        return "WAIT"

    monkeypatch.setattr(flow.host, "_advance", inspect_scope)
    assert await flow.host.advance(kind, "original") == "WAIT"
    assert seen == [
        (
            kind,
            "original",
            "repeatable read" if kind in {"SCAN2_RELEASE", "RACK_REPORT"} else "serializable",
        )
    ]
    async with flow.factory() as session:
        assert await session.scalar(text("SHOW transaction_isolation")) == "read committed"


async def test_commit_unknown_recovers_original_record_without_replacement(manual_host):
    flow = manual_host
    await flow.issue()

    def unknown(session):
        raise DBAPIError(
            "COMMIT", None, RuntimeError("connection lost"), connection_invalidated=True
        )

    event.listen(Session, "after_commit", unknown)
    try:
        with pytest.raises(DBAPIError):
            await flow.host.advance("TASK", "T1")
    finally:
        event.remove(Session, "after_commit", unknown)
    original = (await flow.rows("task"))[0]
    assert original["prepare_operation_id"]
    assert await flow.host.advance("TASK", "T1") == "PREPARING"
    restored = (await flow.rows("task"))[0]
    assert restored["prepare_operation_id"] == original["prepare_operation_id"]
    assert restored["prepare_message_id"] == original["prepare_message_id"]


async def test_component_result_waits_for_original_workline_lock(manual_host):
    from sqlalchemy import select

    from app.models import WorkLine

    flow = manual_host
    await flow.ready()
    await flow.scan()
    await flow.decision(dict(result="NO_WORK"))
    arrival = (await flow.rows("manual_bin"))[0]["arrival_id"]
    await flow.host.advance("MANUAL_BIN", arrival)
    release = (await flow.rows("scan2_release", component=True))[0]
    await flow.result(release["command_code"])

    def bound_wait(session, transaction, connection):
        connection.exec_driver_sql("SET LOCAL lock_timeout = '50ms'")

    async with flow.factory() as holder, holder.begin():
        await holder.scalar(select(WorkLine).where(WorkLine.id == flow.lines[0]).with_for_update())
        event.listen(Session, "after_begin", bound_wait)
        try:
            with pytest.raises(DBAPIError) as caught:
                await flow.host.advance("SCAN2_RELEASE", release["id"])
            assert caught.value.orig.sqlstate == "55P03"
        finally:
            event.remove(Session, "after_begin", bound_wait)
    assert (await flow.rows("scan2_release", component=True))[0]["pending"]
    assert await flow.host.advance("SCAN2_RELEASE", release["id"]) == "SUCCESS"
    assert not (await flow.rows("scan2_release", component=True))[0]["pending"]
