import pytest
from sqlalchemy import select

from app.core.identifiers import uuid7


async def fact(store, digest, raw="A", *, point="SCAN2", separator=None):
    async with store.factory.begin() as session:
        return await store.repo.accept_scan_fact(
            session,
            digest=digest,
            receipt_id=10,
            point=point,
            device_code="C",
            raw_bin_code=raw,
            bin_code=None,
            scanned_at=100,
            separator_command_code=separator,
        )


async def test_receipt_cleanup_keeps_original_digest_to_arrival(roller_store):
    store = roller_store
    first = await fact(store, "one")
    assert (await fact(store, "two"))["id"] == first["id"]
    await fact(store, "three", "B")
    assert (await fact(store, "two"))["id"] == first["id"]
    async with store.factory() as session:
        rows = (await session.execute(select(store.tables["scan_fact"]))).mappings().all()
        assert [(r["digest"], r["arrival_id"]) for r in rows[:2]] == [
            ("one", first["id"]),
            ("two", first["id"]),
        ]


async def test_raw_codes_and_original_result_separate_real_arrivals(roller_store):
    store = roller_store
    a = await fact(store, "one")
    assert (await fact(store, "two"))["id"] == a["id"]
    b = await fact(store, "three", "B")
    a2 = await fact(store, "four")
    assert a2["id"] not in {a["id"], b["id"]}
    a3 = await fact(store, "five", separator="original-command")
    assert a3["id"] != a2["id"]
    assert (await fact(store, "six", separator="original-command"))["id"] == a3["id"]


async def test_original_message_cannot_be_rebound(roller_store):
    store = roller_store
    await fact(store, "one")
    with pytest.raises(ValueError):
        await fact(store, "one", "B")


async def test_original_owner_and_request_identity_survive_restart(roller_lifecycle):
    from app.businesses._components.rack_dispatch.service import RackDispatch
    from app.models import ReliableMessage, TransportTask

    flow = roller_lifecycle
    demand = await flow.demand()
    await flow.advance("FEED", demand)
    original = (await flow.all_rows("rack_action"))[0]
    job_id = original["cycle_id"]
    async with flow.factory.begin() as session:
        dispatch = RackDispatch("first", flow.roller.capabilities)
        job = await dispatch.progress(session, job_id=job_id)
        action = job.actions[0]
        assert await dispatch.register(session, job.origin) == job_id
        assert (
            await dispatch.prepare(
                session,
                job_id=job_id,
                kind="ENTER",
                source_ref=action.source_ref,
                face=action.target_face,
            )
            == action.id
        )
        retained = (await dispatch.progress(session, job_id=job_id)).actions
        assert retained == (action,)
        assert (
            await RackDispatch("second", flow.roller.capabilities).progress(session, job_id=job_id)
            is None
        )
        original_wire = await session.scalar(
            select(ReliableMessage.request_raw).where(
                ReliableMessage.operation_id == action.submit_operation_id
            )
        )
        frozen = await session.scalar(
            select(TransportTask.frozen).where(
                TransportTask.transport_task_id == action.transport_task_id
            )
        )
        assert frozen["rack_id"] == "R1" and frozen["target_face"] == "A"
    await flow.advance("RACK_CYCLE", job_id)
    async with flow.factory() as session:
        message = await session.scalar(
            select(ReliableMessage).where(
                ReliableMessage.operation_id == action.submit_operation_id
            )
        )
        assert (
            message.business_key == action.transport_task_id
            and message.request_raw == original_wire
        )


async def test_applied_entry_and_prepared_rotation_share_outer_transaction(roller_lifecycle):
    from app.businesses._components.bin_roller.ports import RollerWorkRef
    from app.models import ReliableMessage, TransportTask

    flow = roller_lifecycle
    first, job_id = await flow.ready_supply(count=1)
    await flow.demand(face="B", revision=2)
    await flow.feed_success((await flow.all_rows("feed_segment"))[0])
    with pytest.raises(RuntimeError, match="abort"):
        async with flow.factory.begin() as session:
            await flow.roller.advance(session, RollerWorkRef("FEED", first))
            job = await flow.roller.rack_dispatch.progress(session, job_id=job_id)
            assert len(job.actions) == 2
            rolled_back = job.actions[-1]
            assert rolled_back.kind == "ROTATE"
            raise RuntimeError("abort")
    async with flow.factory() as session:
        assert (
            await session.scalar(
                select(TransportTask).where(
                    TransportTask.transport_task_id == rolled_back.transport_task_id
                )
            )
            is None
        )
        assert (
            await session.scalar(
                select(ReliableMessage).where(
                    ReliableMessage.operation_id == rolled_back.submit_operation_id
                )
            )
            is None
        )
        assert len((await flow.roller.rack_dispatch.progress(session, job_id=job_id)).actions) == 1
    await flow.advance("FEED", first)
    await flow.advance("RACK_CYCLE", job_id)
    actions = await flow.all_rows("rack_action")
    assert [a["kind"] for a in actions] == ["CTU01", "CTU02"]
    async with flow.factory() as session:
        message = await session.scalar(
            select(ReliableMessage).where(
                ReliableMessage.operation_id == actions[-1]["operation_id"]
            )
        )
        assert message.business_key == actions[-1]["transport_task_id"]


async def test_distinct_business_origins_can_request_same_rack(roller_store):
    from app.businesses._components.rack_dispatch.contracts import JobOrigin
    from app.businesses._components.rack_dispatch.service import RackDispatch

    dispatch = RackDispatch("first", None)
    async with roller_store.factory.begin() as session:
        for source in ("source-A", "source-B"):
            job_id = str(uuid7())
            assert (
                await dispatch.register(
                    session,
                    JobOrigin(
                        "ROLLER_ORDINARY",
                        source,
                        1,
                        "R1",
                        "T1",
                        None,
                        None,
                        None,
                        None,
                        job_id,
                        None,
                    ),
                )
                == job_id
            )
        jobs = await dispatch.jobs(session, work_line_id=1, source_kinds=("ROLLER_ORDINARY",))
        assert {j.origin.source_ref for j in jobs} == {"source-A", "source-B"}


async def test_three_face_rounds_reuse_only_their_original_movements(roller_lifecycle):
    flow = roller_lifecycle
    first, job_id = await flow.ready_supply(count=1)
    second = await flow.demand(face="B", revision=2)
    await flow.feed_success((await flow.all_rows("feed_segment"))[0])
    await flow.advance("FEED", first)
    first_rotation = (await flow.all_rows("rack_action"))[-1]
    await flow.rack_result(first_rotation)
    await flow.advance("FEED", second)
    third = await flow.demand(face="A", revision=3)
    feed = (await flow.all_rows("feed"))[-1]
    await flow.decision("feed", feed["id"], dict(result="RACK_FACE_DONE"))
    await flow.advance("FEED", second)
    second_rotation = (await flow.all_rows("rack_action"))[-1]
    await flow.rack_result(second_rotation)
    await flow.advance("FEED", third)
    feed = (await flow.all_rows("feed"))[-1]
    await flow.decision("feed", feed["id"], dict(result="RACK_FACE_DONE"))
    await flow.advance("FEED", third)
    await flow.finish_departure(job_id)
    await flow.advance("RACK_CYCLE", job_id)
    async with flow.factory() as session:
        job = await flow.roller.rack_dispatch.progress(session, job_id=job_id)
        assert [(a.kind, a.template, a.target_face) for a in job.actions if a.template] == [
            ("ENTER", "CTU01", "A"),
            ("ROTATE", "CTU02", "B"),
            ("ROTATE", "CTU02", "A"),
            ("DEPART", "CTU03", None),
        ]
        assert len({a.transport_task_id for a in job.actions if a.template}) == 4
