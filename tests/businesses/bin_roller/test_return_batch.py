"""FIFO return allocation and original Transport evidence through current FaceWork."""

import json
from copy import deepcopy
from dataclasses import replace

import pytest
from sqlalchemy import select

from app.businesses._components.bin_roller.ports import RollerWorkRef
from app.models import ReliableMessage, TransportTask
from tests.helpers import read_history_body, set_message_bodies


async def test_unknown_request_keeps_frozen_prefix(roller_return):
    flow = roller_return
    await flow.bins(2)
    async with flow.factory.begin() as session:
        face_id = await flow.selected(session)
        await flow.start(session, face_id)
        before = await flow.roller.original_record(session, "face_work", face_id)
    await flow.bins(1, offset=2)
    async with flow.factory.begin() as session:
        assert await flow.start(session, face_id, outlet="CHANGED") == face_id
        assert await flow.consume_return(session, face_id) == "WAITING_FOR_ALLOCATION"
        after = await flow.roller.original_record(session, "face_work", face_id)
        assert after == before
        assert [m["bin_code"] for m in after["return_members"]] == ["A000000001", "A000000002"]
        assert RollerWorkRef("FACE", face_id) in await flow.roller.list_pending(
            session, kind="FACE", after_id=None, upper_id=face_id, limit=10
        )


async def test_partial_ready_keeps_tail(roller_return):
    flow = roller_return
    await flow.bins(3)
    async with flow.factory.begin() as session:
        face_id = await flow.selected(session, kind="SEGMENT")
        await flow.start(session, face_id)
        await flow.decide(session, face_id, count=1)
        await flow.consume_return(session, face_id)
        rows = await flow.rows(session, "bin_return")
        assert [r["return_state"] for r in rows] == ["MOVING", "READY", "READY"]
        assert await flow.roller.get_return_candidates(session, work_line_id=1) == ()
    await flow.success(face_id)
    async with flow.factory.begin() as session:
        assert await flow.consume_return(session, face_id) == "RETURN_SUCCEEDED"
        assert [
            r["bin_code"] for r in await flow.roller.get_return_candidates(session, work_line_id=1)
        ] == ["A000000002", "A000000003"]
        face = await flow.roller.original_record(session, "face_work", face_id)
        assert face["window_id"] is None and face["step_id"] is None
        assert face["phase"] == "WAIT_FEED_DECISION"


async def test_return_success_retires_original_scan4_position_atomically(roller_return):
    flow = roller_return
    await flow.bins(1)
    async with flow.factory.begin() as session:
        face_id = await flow.selected(session)
        await flow.start(session, face_id)
        await flow.decide(session, face_id)
        await flow.consume_return(session, face_id)
        before = (await flow.rows(session, "bin_return"))[0]
    await flow.success(face_id)
    with pytest.raises(RuntimeError, match="rollback"):
        async with flow.factory.begin() as session:
            assert await flow.consume_return(session, face_id) == "RETURN_SUCCEEDED"
            assert (await flow.rows(session, "bin_return"))[0]["closed_at"]
            raise RuntimeError("rollback")
    async with flow.factory.begin() as session:
        assert (await flow.rows(session, "bin_return"))[0] == before
        await flow.consume_return(session, face_id)
        assert (await flow.rows(session, "bin_return"))[0]["close_reason"] == "COMPLETED"
        assert await flow.roller.return_channel_empty(session, work_line_id=1)


async def test_partial_return_retires_only_known_departed_members(roller_return):
    flow = roller_return
    await flow.bins(2)
    async with flow.factory.begin() as session:
        face_id = await flow.selected(session)
        await flow.start(session, face_id)
        await flow.decide(session, face_id)
        await flow.consume_return(session, face_id)
        face = await flow.roller.original_record(session, "face_work", face_id)
    await flow.complete_transport(face["current_transport_task_id"], count=1)
    async with flow.factory.begin() as session:
        assert await flow.consume_return(session, face_id) == "WAITING_FOR_RETURN_RESULT"
        rows = await flow.rows(session, "bin_return")
        assert rows[0]["closed_at"] and rows[0]["selected_step_id"] is None
        assert rows[1]["closed_at"] is None and rows[1]["return_state"] == "MOVING"
        assert not await flow.roller.return_channel_empty(session, work_line_id=1)
    # A definite failed member cannot be fabricated into a successful retry.
    async with flow.factory.begin() as session:
        assert await flow.consume_return(session, face_id) == "WAITING_FOR_RETURN_RESULT"
        assert (await flow.roller.original_record(session, "face_work", face_id))[
            "current_transport_task_id"
        ] == face["current_transport_task_id"]


@pytest.mark.parametrize(
    "invalid", ["skip", "reorder", "duplicate_slot", "wrong_face", "outside", "extra"]
)
async def test_ready_cannot_skip_or_reorder_frozen_candidates(roller_return, invalid):
    flow = roller_return
    await flow.bins(2)
    async with flow.factory.begin() as session:
        face_id = await flow.selected(session)
        await flow.start(session, face_id)
        data = await flow.decide(session, face_id)
        if invalid == "skip":
            data["moves"] = data["moves"][1:]
        elif invalid == "reorder":
            data["moves"].reverse()
        elif invalid == "duplicate_slot":
            data["moves"][1]["target"] = data["moves"][0]["target"]
        elif invalid == "wrong_face":
            data["moves"][0]["target"]["rack_face"] = "B"
        elif invalid == "outside":
            data["moves"][0]["bin_code"] = "A000000099"
        else:
            data["retry_after_ms"] = 1
        await flow.decide(session, face_id, data=data)
        await flow.consume_return(session, face_id)
        face = await flow.roller.original_record(session, "face_work", face_id)
        assert face["phase"] == "WAIT_RETURN_DECISION" and face["current_transport_task_id"] is None
        assert {r["return_state"] for r in await flow.rows(session, "bin_return")} == {"ALLOCATING"}


async def test_transport_acceptance_does_not_complete_return(roller_return):
    flow = roller_return
    await flow.bins(1)
    async with flow.factory.begin() as session:
        face_id = await flow.selected(session)
        await flow.start(session, face_id)
        await flow.decide(session, face_id)
        await flow.consume_return(session, face_id)
        face = await flow.roller.original_record(session, "face_work", face_id)
        task = await session.scalar(
            select(TransportTask).where(
                TransportTask.transport_task_id == face["current_transport_task_id"]
            )
        )
        msg = await session.scalar(
            select(ReliableMessage).where(ReliableMessage.operation_id == task.operation_id)
        )
        msg.status, msg.last_http_status, msg.last_verdict = "DONE", 202, "DELIVERED"
        await set_message_bodies(
            session,
            msg,
            response_raw=json.dumps(
                dict(
                    operation_id=msg.operation_id,
                    code="RECEIVED",
                    timestamp=2,
                    data=dict(transport_task_id=task.transport_task_id),
                )
            ).encode(),
        )
        await session.flush()
        assert await flow.consume_return(session, face_id) == "WAITING_FOR_RETURN_RESULT"
    await flow.success(face_id, placed=False)
    async with flow.factory.begin() as session:
        assert await flow.consume_return(session, face_id) == "WAITING_FOR_RETURN_RESULT"
    await flow.success(face_id)
    async with flow.factory.begin() as session:
        assert await flow.consume_return(session, face_id) == "RETURN_SUCCEEDED"
        assert (await flow.roller.original_record(session, "face_work", face_id))[
            "phase"
        ] == "RETURN_WINDOW"


async def test_face_opportunity_preserves_distinct_original_batches(roller_return):
    flow = roller_return
    await flow.bins(2)
    async with flow.factory.begin() as session:
        face_id = await flow.selected(session)
        await flow.start(session, face_id)
        await flow.decide(session, face_id, count=1)
        await flow.consume_return(session, face_id)
        first = await flow.roller.original_record(session, "face_work", face_id)
    await flow.success(face_id)
    async with flow.factory.begin() as session:
        await flow.consume_return(session, face_id)
        await flow.start(session, face_id)
        second = await flow.roller.original_record(session, "face_work", face_id)
        assert (
            second["step_id"] != first["step_id"]
            and second["return_operation_id"] != first["return_operation_id"]
        )
        assert [m["bin_code"] for m in second["return_members"]] == ["A000000002"]
        assert await session.scalar(
            select(TransportTask.id).where(
                TransportTask.transport_task_id == first["current_transport_task_id"]
            )
        )
        assert second["current_transport_task_id"] is None


async def test_no_batch_ends_only_original_opportunity(roller_return):
    flow = roller_return
    await flow.bins(1)
    async with flow.factory.begin() as session:
        face_id = await flow.selected(session, kind="SEGMENT")
        await flow.start(session, face_id)
        await flow.decide(session, face_id, data={"result": "NO_BATCH"})
        assert await flow.consume_return(session, face_id) == "NO_BATCH"
        face = await flow.roller.original_record(session, "face_work", face_id)
        assert face["closed_at"] is None and face["window_id"] is None
        assert face["phase"] == "WAIT_FEED_DECISION"
        assert (await flow.rows(session, "bin_return"))[0]["return_state"] == "READY"


async def test_no_candidates_is_local_evidence_without_empty_request(roller_return):
    flow = roller_return
    async with flow.factory.begin() as session:
        face_id = await flow.selected(session, kind="PURE_RETURN")
        assert await flow.start(session, face_id) is None
        face = await flow.roller.original_record(session, "face_work", face_id)
        assert face["close_reason"] == "COMPLETED" and face["return_message_id"] is None
    await flow.bins(1)
    async with flow.factory.begin() as session:
        assert await flow.start(session, face_id) is None


async def test_original_batch_consumption_and_selected_next_intent_roll_back_together(
    roller_return,
):
    flow = roller_return
    await flow.bins(2)
    async with flow.factory.begin() as session:
        face_id = await flow.selected(session)
        await flow.start(session, face_id)
        await flow.decide(session, face_id, count=1)
        await flow.consume_return(session, face_id)
        before = await flow.roller.original_record(session, "face_work", face_id)
    await flow.success(face_id)
    with pytest.raises(RuntimeError, match="rollback"):
        async with flow.factory.begin() as session:
            await flow.consume_return(session, face_id)
            await flow.start(session, face_id)
            raise RuntimeError("rollback")
    async with flow.factory.begin() as session:
        assert await flow.roller.original_record(session, "face_work", face_id) == before
        await flow.consume_return(session, face_id)
        await flow.start(session, face_id)
        assert (await flow.roller.original_record(session, "face_work", face_id))[
            "step_id"
        ] != before["step_id"]


@pytest.mark.parametrize(
    "mismatch", ["owner", "task", "client", "caller", "cycle", "members", "target", "pending"]
)
async def test_return_outcome_requires_original_association_and_evidence(
    roller_return, monkeypatch, mismatch
):
    flow = roller_return
    await flow.bins(1)
    async with flow.factory.begin() as session:
        face_id = await flow.selected(session, kind="SEGMENT")
        await flow.start(session, face_id)
        await flow.decide(session, face_id)
        await flow.consume_return(session, face_id)
        face = await flow.roller.original_record(session, "face_work", face_id)
        job = await flow.roller.rack_dispatch.progress(session, job_id=face["rack_job_id"])
    await flow.success(face_id)
    port = flow.roller.capabilities.transport
    reader = port.get_result
    async with flow.factory() as session:
        result = await reader(
            session,
            transport_task_id=face["current_transport_task_id"],
            client_request_id=face["current_client_request_id"],
            caller=job.origin.caller,
        )
    changes = {
        "owner": {"owner_domain": "other"},
        "task": {"transport_task_id": "other"},
        "client": {"client_request_id": "other"},
        "caller": {"workline_id": "other"},
        "cycle": {"cycle_id": "other"},
        "pending": {"has_pending_outcome": True},
    }
    if mismatch in changes:
        bad = replace(result, **changes[mismatch])
    else:
        frozen = deepcopy(result.frozen)
        if mismatch == "members":
            frozen["moves"].clear()
        else:
            frozen["moves"][0]["target"]["rack_face"] = "OTHER"
        bad = replace(result, frozen=frozen)

    async def wrong(*args, **kwargs):
        return bad

    monkeypatch.setattr(port, "get_result", wrong)
    async with flow.factory.begin() as session:
        await flow.consume_return(session, face_id)
        assert (await flow.roller.original_record(session, "face_work", face_id))[
            "step_id"
        ] == face["step_id"]
    monkeypatch.setattr(port, "get_result", reader)
    async with flow.factory.begin() as session:
        assert await flow.consume_return(session, face_id) == "RETURN_SUCCEEDED"


async def test_unknown_return_keeps_original_transport_and_corrected_result(roller_return):
    flow = roller_return
    await flow.bins(1)
    async with flow.factory.begin() as session:
        face_id = await flow.selected(session)
        await flow.start(session, face_id)
        await flow.decide(session, face_id)
        await flow.consume_return(session, face_id)
        original = await flow.roller.original_record(session, "face_work", face_id)
    await flow.success(face_id, placed=False, status="UNKNOWN")
    async with flow.factory.begin() as session:
        assert await flow.consume_return(session, face_id) == "WAITING_FOR_RETURN_RESULT"
        assert (await flow.roller.original_record(session, "face_work", face_id))[
            "current_transport_task_id"
        ] == original["current_transport_task_id"]
        assert await flow.roller.get_return_candidates(session, work_line_id=1) == ()
    await flow.success(face_id, revision=2)
    async with flow.factory.begin() as session:
        assert await flow.consume_return(session, face_id) == "RETURN_SUCCEEDED"


async def test_return_success_requires_target_placed_fact(roller_return):
    flow = roller_return
    await flow.bins(1)
    async with flow.factory.begin() as session:
        face_id = await flow.selected(session)
        await flow.start(session, face_id)
        await flow.decide(session, face_id)
        await flow.consume_return(session, face_id)
    await flow.success(face_id, placed=False)
    async with flow.factory.begin() as session:
        assert await flow.consume_return(session, face_id) == "WAITING_FOR_RETURN_RESULT"
        assert (await flow.roller.original_record(session, "face_work", face_id))["step_id"]
    await flow.success(face_id, placed=True, revision=2)
    async with flow.factory.begin() as session:
        assert await flow.consume_return(session, face_id) == "RETURN_SUCCEEDED"


@pytest.mark.parametrize("http,code", [(503, "UNAVAILABLE"), (409, "CONFLICT"), (422, "REJECTED")])
async def test_original_request_failure_never_becomes_no_batch(roller_return, http, code):
    flow = roller_return
    await flow.bins(1)
    async with flow.factory.begin() as session:
        face_id = await flow.selected(session)
        await flow.start(session, face_id)
        before = await flow.roller.original_record(session, "face_work", face_id)
        message = await session.get(ReliableMessage, before["return_message_id"])
        message.status, message.last_http_status, message.last_verdict = (
            "FAILED",
            http,
            "PEER_TERMINAL",
        )
        await set_message_bodies(
            session,
            message,
            response_raw=json.dumps(
                dict(operation_id=message.operation_id, code=code, timestamp=2, data={})
            ).encode(),
        )
        await session.flush()
        assert await flow.consume_return(session, face_id) == "WAITING_FOR_ALLOCATION"
        assert await flow.roller.original_record(session, "face_work", face_id) == before


async def test_return_request_has_only_original_wire_fields(roller_return):
    import json

    from pydantic import ValidationError
    from sqlalchemy import select

    from app.businesses._components.bin_roller.request.bin_return_batch_v1 import (
        ReturnBatchRequest,
    )
    from app.models import ReliableMessage

    flow = roller_return
    await flow.bins(1)
    async with flow.factory.begin() as session:
        batch_id = await flow.start(session, await flow.selected(session, face="90"))
        batch = await flow.record(session, "return_batch", batch_id)
        raw = await read_history_body(
            session,
            await session.scalar(
                select(ReliableMessage.request_record_id).where(
                    ReliableMessage.id == batch["message_id"]
                )
            ),
        )
        data = json.loads(raw)["data"]
        assert data == dict(
            workline_code="LINE",
            rack_id="R1",
            rack_face="90",
            return_candidates=[
                dict(
                    sequence_no=1,
                    bin_code="A000000001",
                    source=dict(type="HANDOFF_POSITION", location_code="OUTLET-ORIGINAL"),
                )
            ],
        )
        assert ReturnBatchRequest.model_validate(data).rack_face == "90"
        for changed in (
            {**data, "task_id": "T1"},
            {**data, "return_candidates": []},
            {**data, "rack_face": 90},
        ):
            with pytest.raises(ValidationError):
                ReturnBatchRequest.model_validate(changed)
        for key, value in (("sequence_no", True), ("sequence_no", 2), ("bin_code", 123)):
            wrong = {**data, "return_candidates": [{**data["return_candidates"][0], key: value}]}
            with pytest.raises(ValidationError):
                ReturnBatchRequest.model_validate(wrong)
