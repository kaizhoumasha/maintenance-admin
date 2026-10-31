import pytest
from sqlalchemy import select


@pytest.mark.asyncio
async def test_five_bins_split_four_and_one(roller_feed):
    env = roller_feed
    async with env.store.factory.begin() as session:
        original = await env.demand(session)
        await env.ready(session, original)
        feed_id = await env.start(session, original)
        await env.decide(session, feed_id)
        await env.consume_feed(session, original["id"])
        feed = (await env.rows(session, "feed"))[0]
        segments = await env.rows(session, "feed_segment")
        assert len(feed["manifest"]["bins"]) == 5
        assert [m["bin_code"] for m in segments[0]["members"]] == [
            "A000000001",
            "A000000002",
            "A000000003",
            "A000000004",
        ]
        assert len(segments) == 1
        assert not feed["feed_complete"]
        from app.models import ReliableMessage

        request = await session.scalar(
            select(ReliableMessage).where(ReliableMessage.id == feed["message_id"])
        )
        import json

        assert json.loads(request.request_raw)["data"] == {
            "task_id": "T1",
            "rack_id": "R1",
            "rack_face": "A",
        }
        from app.models import TransportTask

        task = await session.scalar(
            select(TransportTask).where(
                TransportTask.transport_task_id == segments[0]["transport_task_id"]
            )
        )
        assert task.workline_id == "LINE-ORIGINAL"
        assert task.frozen["moves"][0] == {
            "container_id": "A000000001",
            "source": {"kind": "RACK_BIN_SLOT", "rack_id": "R1", "rack_face": "A", "slot_id": "S1"},
            "target": {"kind": "HANDOFF_POSITION", "location_code": "INLET-ORIGINAL"},
        }


@pytest.mark.asyncio
async def test_segment_success_does_not_wait_for_scans(roller_feed):
    env = roller_feed
    async with env.store.factory.begin() as session:
        original = await env.demand(session)
        await env.ready(session, original)
        feed_id = await env.start(session, original)
        await env.decide(session, feed_id)
        await env.consume_feed(session, original["id"])
        segment = (await env.rows(session, "feed_segment"))[0]
    await env.success(segment)
    async with env.store.factory.begin() as session:
        await env.consume_feed(session, original["id"])
        opportunity = (await env.rows(session, "return_opportunity"))[0]
        assert opportunity["kind"] == "SEGMENT"
        assert not opportunity["ended"]
        assert await env.rows(session, "scan_fact") == []
        assert len(await env.rows(session, "feed_segment")) == 1
        table = env.store.tables["return_opportunity"]
        await session.execute(
            table.update()
            .where(table.c.id == opportunity["id"])
            .values(ended=True, end_reason="NO_CANDIDATES", end_evidence={})
        )
        await env.consume_feed(session, original["id"])
        segments = sorted(await env.rows(session, "feed_segment"), key=lambda r: r["segment_index"])
        assert [m["bin_code"] for m in segments[1]["members"]] == ["A000000005"]
        second = segments[1]
    await env.success(second)
    async with env.store.factory.begin() as session:
        await env.consume_feed(session, original["id"])
        await env.consume_feed(session, original["id"])
        assert [r["kind"] for r in await env.rows(session, "return_opportunity")] == [
            "SEGMENT",
            "FACE_END",
        ]
        progress = await env.roller.get_supply_progress(session, work_line_id=1, task_id="T1")
        assert progress[0].feed_complete and not progress[0].pending


@pytest.mark.asyncio
async def test_face_done_opens_return_without_fake_feed(roller_feed):
    env = roller_feed
    async with env.store.factory.begin() as session:
        original = await env.demand(session)
        await env.ready(session, original)
        feed_id = await env.start(session, original)
        await env.decide(session, feed_id, data={"result": "RACK_FACE_DONE"})
        await env.consume_feed(session, original["id"])
        await env.consume_feed(session, original["id"])
        assert await env.rows(session, "feed_segment") == []
        assert [r["kind"] for r in await env.rows(session, "return_opportunity")] == ["FACE_END"]
        assert (await env.roller.get_supply_progress(session, work_line_id=1, task_id="T1"))[
            0
        ].feed_complete


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "data",
    [
        {"result": "READY", "bins": []},
        {"result": "RACK_FACE_DONE", "bins": []},
        {"result": "NO_BATCH"},
        {
            "result": "READY",
            "bins": [
                {
                    "bin_code": "A000000001",
                    "source_locator": {
                        "type": "RACK_BIN_SLOT",
                        "rack_id": "OTHER",
                        "rack_face": "A",
                        "slot_id": "S1",
                    },
                }
            ],
        },
    ],
)
async def test_invalid_manifest_keeps_original_request_pending(roller_feed, data):
    env = roller_feed
    async with env.store.factory.begin() as session:
        original = await env.demand(session)
        await env.ready(session, original)
        feed_id = await env.start(session, original)
        await env.decide(session, feed_id, data=data)
        await env.consume_feed(session, original["id"])
        assert await env.rows(session, "feed_segment") == []
        feed = (await env.rows(session, "feed"))[0]
        assert feed["manifest"] is None and feed["pending"] and not feed["feed_complete"]


@pytest.mark.asyncio
async def test_recovery_keeps_frozen_segments_and_original_message(roller_feed):
    env = roller_feed
    async with env.store.factory.begin() as session:
        original = await env.demand(session)
        await env.ready(session, original)
        feed_id = await env.start(session, original)
        await env.decide(session, feed_id)
        await env.consume_feed(session, original["id"])
        before = (await env.rows(session, "feed"))[0]
        segment = (await env.rows(session, "feed_segment"))[0]
    async with env.store.factory.begin() as session:
        assert await env.start(session, original, inlet="CHANGED") == feed_id
        await env.consume_feed(session, original["id"])
        assert dict((await env.rows(session, "feed"))[0]) == dict(before)
        assert dict((await env.rows(session, "feed_segment"))[0]) == dict(segment)
    await env.success(segment, status="FAILED")
    async with env.store.factory.begin() as session:
        await env.consume_feed(session, original["id"])
        assert await env.rows(session, "return_opportunity") == []
        progress = (await env.roller.get_supply_progress(session, work_line_id=1, task_id="T1"))[0]
        assert progress.pending and not progress.feed_complete
        assert segment["client_request_id"] in progress.original_ids
        assert segment["transport_task_id"] in progress.original_ids
        assert (await env.rows(session, "feed_segment"))[0]["transport_task_id"] == segment[
            "transport_task_id"
        ]


@pytest.mark.asyncio
async def test_feed_consumption_and_next_segment_roll_back_together(roller_feed):
    env = roller_feed
    async with env.store.factory.begin() as session:
        original = await env.demand(session)
        await env.ready(session, original)
        feed_id = await env.start(session, original)
        await env.decide(session, feed_id)
        await env.consume_feed(session, original["id"])
        first = (await env.rows(session, "feed_segment"))[0]
    await env.success(first)
    with pytest.raises(RuntimeError, match="rollback"):
        async with env.store.factory.begin() as session:
            await env.consume_feed(session, original["id"])
            opportunity = (await env.rows(session, "return_opportunity"))[0]
            table = env.store.tables["return_opportunity"]
            await session.execute(
                table.update()
                .where(table.c.id == opportunity["id"])
                .values(ended=True, end_reason="NO_CANDIDATES", end_evidence={})
            )
            await env.consume_feed(session, original["id"])
            assert len(await env.rows(session, "feed_segment")) == 2
            raise RuntimeError("rollback")
    async with env.store.factory.begin() as session:
        assert dict((await env.rows(session, "feed_segment"))[0]) == dict(first)
        assert await env.rows(session, "return_opportunity") == []
        await env.consume_feed(session, original["id"])
        assert len(await env.rows(session, "return_opportunity")) == 1


@pytest.mark.asyncio
async def test_concurrent_feed_completion_selects_one_original_next_segment(roller_feed):
    import asyncio

    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    env = roller_feed
    async with env.store.factory.begin() as session:
        original = await env.demand(session)
        await env.ready(session, original)
        feed_id = await env.start(session, original)
        await env.decide(session, feed_id)
        await env.consume_feed(session, original["id"])
        first = (await env.rows(session, "feed_segment"))[0]
    await env.success(first)
    async with env.store.factory.begin() as session:
        await env.consume_feed(session, original["id"])
        opportunity = (await env.rows(session, "return_opportunity"))[0]
        table = env.store.tables["return_opportunity"]
        await session.execute(
            table.update()
            .where(table.c.id == opportunity["id"])
            .values(ended=True, end_reason="NO_CANDIDATES", end_evidence={})
        )

    gate = asyncio.Barrier(2)
    postgres = env.store.factory.kw["bind"].dialect.name == "postgresql"

    async def advance():
        for attempt in range(3):
            try:
                async with env.store.factory.begin() as session:
                    if postgres:
                        await session.execute(text("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE"))
                    if postgres and attempt == 0:
                        await gate.wait()
                    await env.consume_feed(session, original["id"])
                return
            except DBAPIError as error:
                assert getattr(error.orig, "sqlstate", None) == "40001"
                if attempt == 2:
                    raise

    if postgres:
        await asyncio.gather(advance(), advance())
    else:
        await advance()
        await advance()
    async with env.store.factory.begin() as session:
        segments = sorted(await env.rows(session, "feed_segment"), key=lambda r: r["segment_index"])
        assert len(segments) == 2
        assert segments[0]["transport_task_id"] == first["transport_task_id"]
        assert [m["bin_code"] for m in segments[1]["members"]] == ["A000000005"]
        from app.models import ReliableMessage, TransportTask

        tasks = (
            await session.scalars(
                select(TransportTask).where(
                    TransportTask.cycle_id == (await env.rows(session, "rack_cycle"))[0]["id"]
                )
            )
        ).all()
        assert len(tasks) == 2
        messages = (
            await session.scalars(
                select(ReliableMessage).where(
                    ReliableMessage.operation_id.in_([t.operation_id for t in tasks])
                )
            )
        ).all()
        assert len(messages) == 2


@pytest.mark.asyncio
async def test_feed_success_requires_original_applied_members_and_target(roller_feed):
    from dataclasses import replace

    from app.integrations.wms_transport import TransportCaller

    env = roller_feed
    async with env.store.factory.begin() as session:
        original = await env.demand(session)
        await env.ready(session, original)
        feed_id = await env.start(session, original)
        await env.decide(session, feed_id, count=1)
        await env.consume_feed(session, original["id"])
        segment = (await env.rows(session, "feed_segment"))[0]
    await env.success(segment)
    async with env.store.factory.begin() as session:
        result = await env.roller.capabilities.transport.get_result(
            session,
            transport_task_id=segment["transport_task_id"],
            client_request_id=segment["client_request_id"],
            caller=TransportCaller("LINE-ORIGINAL"),
        )
        wrong_target = {
            **result.outcome,
            "results": [
                {
                    **result.outcome["results"][0],
                    "final_position": {"kind": "HANDOFF_POSITION", "location_code": "OTHER"},
                }
            ],
        }
        for invalid in (
            replace(result, owner_domain="second"),
            replace(result, cycle_id="other"),
            replace(result, operation_id="other"),
            replace(result, has_pending_outcome=True),
            replace(result, outcome=None),
            replace(result, outcome=wrong_target),
        ):
            assert (
                await env.roller.record_feed_success(
                    session, feed_id=feed_id, segment_id=segment["id"], result=invalid
                )
                == ""
            )
        assert await env.rows(session, "return_opportunity") == []
        assert not (await env.rows(session, "feed"))[0]["feed_complete"]
        assert await env.roller.record_feed_success(
            session, feed_id=feed_id, segment_id=segment["id"], result=result
        )
        assert (await env.rows(session, "feed_segment"))[0]["status"] == "SUCCEEDED"


@pytest.mark.asyncio
async def test_inbound_request_is_strict_three_fields_and_opaque_face(roller_feed):
    from pydantic import ValidationError

    from app.businesses._components.bin_roller.request.outbound_bin_inbound_batch_v1 import (
        InboundBatch,
    )

    assert InboundBatch(task_id="T1", rack_id="R1", rack_face=" 90 ").model_dump() == {
        "task_id": "T1",
        "rack_id": "R1",
        "rack_face": " 90 ",
    }
    for values in (
        dict(task_id="T1", rack_id="R1", rack_face="A", plan_revision=1),
        dict(task_id="", rack_id="R1", rack_face="A"),
        dict(task_id="T1", rack_id="R1", rack_face=""),
        dict(task_id="T1", rack_id="R1", rack_face="A" * 11),
        dict(task_id="T1", rack_id="R1", rack_face=1),
    ):
        with pytest.raises(ValidationError):
            InboundBatch(**values)


@pytest.mark.asyncio
async def test_unknown_feed_keeps_original_unresolved_obligation(roller_feed):
    env = roller_feed
    async with env.store.factory.begin() as session:
        original = await env.demand(session)
        await env.ready(session, original)
        feed_id = await env.start(session, original)
        await env.decide(session, feed_id, count=1)
        await env.consume_feed(session, original["id"])
        first = (await env.rows(session, "feed_segment"))[0]
    await env.success(first, status="UNKNOWN")
    async with env.store.factory.begin() as session:
        await env.consume_feed(session, original["id"])
        segment = (await env.rows(session, "feed_segment"))[0]
        assert segment["status"] == "UNKNOWN"
        assert segment["pending"] and segment["transport_task_id"] == first["transport_task_id"]
        assert await env.rows(session, "return_opportunity") == []
        assert not (await env.roller.get_supply_progress(session, work_line_id=1, task_id="T1"))[
            0
        ].feed_complete
    await env.success(first, revision=2)
    async with env.store.factory.begin() as session:
        await env.consume_feed(session, original["id"])
        assert len(await env.rows(session, "feed_segment")) == 1
        assert (await env.rows(session, "feed_segment"))[0]["transport_task_id"] == first[
            "transport_task_id"
        ]
        assert (await env.roller.get_supply_progress(session, work_line_id=1, task_id="T1"))[
            0
        ].feed_complete
        assert await env.roller.get_pending_upper_id(session, kind="FEED") is None
