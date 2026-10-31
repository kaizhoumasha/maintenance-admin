"""Explicit original batches keep frozen FIFO members, targets and result ownership."""

import pytest

from app.businesses._components.bin_roller.ports import RollerWorkRef
from app.integrations.wms_transport import TransportCaller


async def test_unknown_request_keeps_frozen_prefix(roller_return):
    flow = roller_return
    await flow.bins(2)
    async with flow.factory.begin() as session:
        opportunity = await flow.selected(session)
        batch_id = await flow.start(session, opportunity)
        first = await flow.roller.original_record(session, "return_batch", batch_id)
        assert await flow.consume_return(session, batch_id) == "WAITING_FOR_ALLOCATION"
    await flow.bins(1, offset=2)
    async with flow.factory.begin() as session:
        assert await flow.start(session, opportunity, outlet="CHANGED") == batch_id
        batch = await flow.roller.original_record(session, "return_batch", batch_id)
        assert batch == first
        assert [m["bin_code"] for m in batch["members"]] == ["A000000001", "A000000002"]
        assert await flow.roller.get_pending_upper_id(session, kind="RETURN_BATCH") == batch_id
        assert await flow.roller.list_pending(
            session, kind="RETURN_BATCH", after_id=None, upper_id=batch_id, limit=10
        ) == (RollerWorkRef("RETURN_BATCH", batch_id),)
        other = await flow.selected(session)
        assert await flow.start(session, other) is None
        original = await flow.roller.original_record(session, "return_opportunity", opportunity)
        assert not original["ended"]


async def test_partial_ready_keeps_tail(roller_return):
    flow = roller_return
    await flow.bins(3)
    async with flow.factory.begin() as session:
        opportunity = await flow.selected(session, kind="SEGMENT")
        batch_id = await flow.start(session, opportunity)
        await flow.decide(session, batch_id, count=1)
        await flow.consume_return(session, batch_id)
        rounds = await flow.rows(session, "return_round")
        assert [r["status"] for r in rounds] == ["IN_TRANSIT", "PENDING", "PENDING"]
        assert await flow.roller.get_return_candidates(session, work_line_id=1) == ()
    await flow.success(batch_id)
    async with flow.factory.begin() as session:
        assert await flow.consume_return(session, batch_id) == "SUCCEEDED"
        candidates = await flow.roller.get_return_candidates(session, work_line_id=1)
        assert [r["bin_code"] for r in candidates] == ["A000000002", "A000000003"]
        original = await flow.roller.original_record(session, "return_opportunity", opportunity)
        assert original["ended"] and original["end_reason"] == "RETURN_SUCCEEDED"
        assert await flow.start(session, opportunity, source_ref=batch_id) is None
        assert len(await flow.rows(session, "return_batch")) == 1


async def test_return_success_retires_original_scan4_position_atomically(roller_return):
    flow = roller_return
    await flow.bins(1)
    async with flow.factory.begin() as session:
        batch_id = await flow.start(session, await flow.selected(session))
        await flow.decide(session, batch_id)
        await flow.consume_return(session, batch_id)
        original = await flow.store.repo.current_position(session, "BIN", "A000000001")
        assert original["location_code"] == "SCAN4"
    await flow.success(batch_id)
    with pytest.raises(RuntimeError, match="rollback"):
        async with flow.factory.begin() as session:
            assert await flow.consume_return(session, batch_id) == "SUCCEEDED"
            assert await flow.store.repo.current_position(session, "BIN", "A000000001") is None
            raise RuntimeError("rollback")
    async with flow.factory.begin() as session:
        current = await flow.store.repo.current_position(session, "BIN", "A000000001")
        assert current["id"] == original["id"]
        assert await flow.consume_return(session, batch_id) == "SUCCEEDED"
        assert await flow.store.repo.current_position(session, "BIN", "A000000001") is None


@pytest.mark.parametrize("known_status", ["SUCCEEDED", "FAILED"])
async def test_partial_return_retires_only_known_departed_members(roller_lifecycle, known_status):
    flow = roller_lifecycle
    await flow.bins(2)
    async with flow.factory.begin() as session:
        batch_id = await flow.start(session, await flow.selected(session))
        await flow.decide(session, batch_id)
        await flow.consume_return(session, batch_id)
        batch = await flow.roller.original_record(session, "return_batch", batch_id)
    target = dict(kind="RACK_BIN_SLOT", rack_id="R1", rack_face="A", slot_id="S2")
    if known_status == "SUCCEEDED":
        await flow.rack_callback(
            dict(
                transport_task_id=batch["transport_task_id"],
                container_id="A000000002",
                milestone="TARGET_PLACED",
                final_position=target,
            ),
            operation="transport.task.member_position_changed@v1",
        )
    await flow.rack_callback(
        dict(
            transport_task_id=batch["transport_task_id"],
            kind="BIN_MOVE",
            outcome_revision=1,
            results=[
                dict(
                    container_id="A000000001",
                    status="FAILED",
                    failure_code="RCS_EXECUTION_FAILED",
                    final_position=batch["source"],
                ),
                dict(
                    container_id="A000000002",
                    status=known_status,
                    final_position=target,
                    **(
                        {"failure_code": "RCS_EXECUTION_FAILED"} if known_status == "FAILED" else {}
                    ),
                ),
            ],
        )
    )
    async with flow.factory.begin() as session:
        assert await flow.consume_return(session, batch_id) == "FAILED"
        assert await flow.store.repo.current_position(session, "BIN", "A000000002") is None
        current = await flow.roller.original_record(session, "return_batch", batch_id)
        assert current["pending"] and current["transport_task_id"] == batch["transport_task_id"]
        assert {r["status"] for r in await flow.rows(session, "return_round")} == {"IN_TRANSIT"}


async def test_return_failure_at_source_keeps_current_scan4_fact(roller_lifecycle):
    flow = roller_lifecycle
    await flow.bins(1)
    async with flow.factory.begin() as session:
        batch_id = await flow.start(session, await flow.selected(session))
        await flow.decide(session, batch_id)
        await flow.consume_return(session, batch_id)
        batch = await flow.roller.original_record(session, "return_batch", batch_id)
        original = await flow.store.repo.current_position(session, "BIN", "A000000001")
    await flow.rack_callback(
        dict(
            transport_task_id=batch["transport_task_id"],
            kind="BIN_MOVE",
            outcome_revision=1,
            results=[
                dict(
                    container_id="A000000001",
                    status="FAILED",
                    failure_code="MANUAL_ABORTED",
                    final_position=batch["source"],
                )
            ],
        )
    )
    async with flow.factory.begin() as session:
        assert await flow.consume_return(session, batch_id) == "FAILED"
        current = await flow.store.repo.current_position(session, "BIN", "A000000001")
        assert current["id"] == original["id"] and current["location_code"] == "SCAN4"
        pending = await flow.roller.original_record(session, "return_batch", batch_id)
        assert pending["pending"] and pending["transport_task_id"] == batch["transport_task_id"]


@pytest.mark.parametrize(
    "invalid", ["skip", "reorder", "duplicate_slot", "wrong_face", "outside", "extra"]
)
async def test_ready_cannot_skip_or_reorder_frozen_candidates(roller_return, invalid):
    flow = roller_return
    await flow.bins(2)
    async with flow.factory.begin() as session:
        opportunity = await flow.selected(session)
        batch_id = await flow.start(session, opportunity)
        data = await flow.decide(session, batch_id)
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
        await flow.decide(session, batch_id, data=data)
        assert await flow.consume_return(session, batch_id) == "INVALID_ALLOCATION"
        batch = await flow.roller.original_record(session, "return_batch", batch_id)
        assert batch["transport_task_id"] is None and batch["manifest"] is None and batch["pending"]


async def test_transport_acceptance_does_not_complete_return(roller_return):
    import json

    from app.models import ReliableMessage

    flow = roller_return
    await flow.bins(1)
    async with flow.factory.begin() as session:
        opportunity = await flow.selected(session)
        batch_id = await flow.start(session, opportunity)
        await flow.decide(session, batch_id)
        await flow.consume_return(session, batch_id)
        batch = await flow.roller.original_record(session, "return_batch", batch_id)
        await session.execute(
            ReliableMessage.__table__.update()
            .where(ReliableMessage.operation_id == batch["transport_operation_id"])
            .values(
                status="DONE",
                last_http_status=202,
                last_verdict="DELIVERED",
                response_raw=json.dumps(
                    dict(
                        operation_id=batch["transport_operation_id"],
                        code="RECEIVED",
                        timestamp=2,
                        data=dict(transport_task_id=batch["transport_task_id"]),
                    )
                ).encode(),
            )
        )
        accepted = await flow.roller.capabilities.transport.get_result(
            session,
            transport_task_id=batch["transport_task_id"],
            client_request_id=batch["client_request_id"],
            caller=TransportCaller(batch["caller_workline_id"], batch["station_id"]),
        )
        assert (
            accepted.submission_response_state == "VALID"
            and accepted.submission_response_code == "RECEIVED"
        )
        assert accepted.outcome is None
        assert await flow.consume_return(session, batch_id) == "WAITING_FOR_RETURN_RESULT"
        assert not await flow.roller.return_channel_empty(session, work_line_id=1)
    await flow.success(batch_id, placed=False)
    async with flow.factory.begin() as session:
        assert await flow.consume_return(session, batch_id) == "WAITING_FOR_RETURN_RESULT"
        assert (await flow.roller.original_record(session, "return_batch", batch_id))["pending"]
    await flow.success(batch_id)
    async with flow.factory.begin() as session:
        assert await flow.consume_return(session, batch_id) == "SUCCEEDED"
        assert await flow.roller.return_channel_empty(session, work_line_id=1)
        assert not (await flow.roller.original_record(session, "return_opportunity", opportunity))[
            "ended"
        ]


async def test_face_opportunity_preserves_distinct_original_batches(roller_return):
    flow = roller_return
    await flow.bins(2)
    async with flow.factory.begin() as session:
        opportunity = await flow.selected(session)
        first_id = await flow.start(session, opportunity)
        await flow.decide(session, first_id, count=1)
        await flow.consume_return(session, first_id)
    await flow.success(first_id)
    async with flow.factory.begin() as session:
        await flow.consume_return(session, first_id)
        assert len(await flow.rows(session, "return_batch")) == 1
        second_id = await flow.start(session, opportunity, source_ref=first_id)
        assert second_id != first_id
        assert await flow.start(session, opportunity, source_ref=first_id) == second_id
        first = await flow.roller.original_record(session, "return_batch", first_id)
        second = await flow.roller.original_record(session, "return_batch", second_id)
        assert second["operation_id"] != first["operation_id"]
        assert [m["bin_code"] for m in second["members"]] == ["A000000002"]
        result = await flow.roller.capabilities.transport.get_result(
            session,
            transport_task_id=first["transport_task_id"],
            client_request_id=first["client_request_id"],
            caller=TransportCaller("LINE-ORIGINAL"),
        )
        assert (
            await flow.roller.record_return_outcome(
                session,
                service_id=(
                    await flow.roller.original_record(session, "return_opportunity", opportunity)
                )["service_id"],
                opportunity_id=opportunity,
                batch_id=second_id,
                reason="RETURN_SUCCEEDED",
                evidence={
                    "transport_task_id": result.transport_task_id,
                    "outcome_revision": result.outcome_revision,
                },
            )
            == ""
        )
        assert await flow.consume_return(session, first_id) == "SUCCEEDED"
        assert len(await flow.rows(session, "return_batch")) == 2
        assert not (await flow.roller.original_record(session, "return_opportunity", opportunity))[
            "ended"
        ]


async def test_no_batch_ends_only_original_opportunity(roller_return):
    flow = roller_return
    await flow.bins(1)
    async with flow.factory.begin() as session:
        opportunity = await flow.selected(session)
        batch_id = await flow.start(session, opportunity)
        await flow.decide(session, batch_id, data={"result": "NO_BATCH"})
        assert await flow.consume_return(session, batch_id) == "NO_BATCH"
        assert await flow.start(session, opportunity) == batch_id
        assert await flow.start(session, opportunity, source_ref=batch_id) is None
        assert [r["status"] for r in await flow.rows(session, "return_round")] == ["PENDING"]
        new_opportunity = await flow.selected(session)
        assert await flow.start(session, new_opportunity) != batch_id
        assert all(r["status"] == "READY" for r in await flow.rows(session, "face_service"))


async def test_no_candidates_is_local_evidence_without_empty_request(roller_return):
    flow = roller_return
    async with flow.factory.begin() as session:
        opportunity = await flow.selected(session)
        assert await flow.start(session, opportunity) is None
        original = await flow.roller.original_record(session, "return_opportunity", opportunity)
        assert original["ended"] and original["end_reason"] == "NO_CANDIDATES"
        assert await flow.rows(session, "return_batch") == []
    await flow.bins(1)
    async with flow.factory.begin() as session:
        assert await flow.start(session, opportunity) is None
        assert await flow.rows(session, "return_batch") == []


async def test_batch_target_validation_has_no_cross_request_reservation(roller_return):
    flow = roller_return
    await flow.bins(1)
    async with flow.factory.begin() as session:
        first = await flow.start(session, await flow.selected(session))
        await flow.decide(session, first)
        await flow.consume_return(session, first)
    flow.work_line_id = 2
    await flow.scan("SCAN3", "A000000001-B", timestamp=100)
    await flow.scan("SCAN4", "A000000001-B", timestamp=101)
    async with flow.factory() as session:
        arrival = next(
            a
            for a in await flow.rows(session, "scan_arrival")
            if a["point"] == "SCAN4" and a["work_line_id"] == 2
        )
    await flow.result(arrival["command_code"], "SCAN4", finish_time=102)
    async with flow.factory.begin() as session:
        second = await flow.start(
            session, await flow.selected(session, line=2), outlet="OUTLET-SECOND"
        )
        await flow.decide(session, second)
        assert await flow.consume_return(session, second) == "WAITING_FOR_RETURN_RESULT"
        batches = await flow.rows(session, "return_batch")
        assert len({b["transport_task_id"] for b in batches}) == 2
        assert {b["work_line_id"] for b in batches} == {1, 2}
        assert [b["members"][0]["bin_code"] for b in batches] == ["A000000001", "A000000001"]
        assert batches[0]["members"][0]["round_id"] != batches[1]["members"][0]["round_id"]
        assert (
            batches[0]["manifest"]["moves"][0]["target"]
            == batches[1]["manifest"]["moves"][0]["target"]
        )


async def test_original_batch_consumption_and_selected_next_intent_roll_back_together(
    roller_return,
):
    flow = roller_return
    await flow.bins(2)
    async with flow.factory.begin() as session:
        opportunity = await flow.selected(session)
        first = await flow.start(session, opportunity)
        await flow.decide(session, first, count=1)
        await flow.consume_return(session, first)
    await flow.success(first)
    with pytest.raises(RuntimeError, match="rollback"):
        async with flow.factory.begin() as session:
            assert await flow.consume_return(session, first) == "SUCCEEDED"
            assert await flow.start(session, opportunity, source_ref=first) != first
            raise RuntimeError("rollback")
    async with flow.factory.begin() as session:
        assert len(await flow.rows(session, "return_batch")) == 1
        assert (await flow.roller.original_record(session, "return_batch", first))["pending"]
        assert await flow.consume_return(session, first) == "SUCCEEDED"
        second = await flow.start(session, opportunity, source_ref=first)
        assert second != first
        assert len(await flow.rows(session, "return_batch")) == 2


async def test_concurrent_batch_completion_creates_one_next_batch(roller_return):
    import asyncio

    from sqlalchemy import select, text
    from sqlalchemy.exc import DBAPIError

    from app.models import ReliableMessage

    flow = roller_return
    await flow.bins(2)
    async with flow.factory.begin() as session:
        opportunity = await flow.selected(session)
        first = await flow.start(session, opportunity)
        await flow.decide(session, first, count=1)
        await flow.consume_return(session, first)
    await flow.success(first)
    postgres = flow.factory.kw["bind"].dialect.name == "postgresql"
    gate = asyncio.Barrier(2)

    async def complete():
        for attempt in range(3):
            try:
                async with flow.factory.begin() as session:
                    if postgres:
                        await session.execute(text("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE"))
                    if postgres and attempt == 0:
                        await flow.roller.original_record(session, "return_batch", first)
                        await gate.wait()
                    await flow.consume_return(session, first)
                    selected = await flow.start(session, opportunity, source_ref=first)
                return selected
            except DBAPIError as error:
                assert getattr(error.orig, "sqlstate", None) == "40001"
                if attempt == 2:
                    raise

    if postgres:
        results = await asyncio.gather(complete(), complete())
    else:
        results = [await complete(), await complete()]
    assert results[0] == results[1] != first
    async with flow.factory() as session:
        batches = await flow.rows(session, "return_batch")
        assert len(batches) == 2
        messages = (
            await session.scalars(
                select(ReliableMessage.id).where(
                    ReliableMessage.id.in_([b["message_id"] for b in batches])
                )
            )
        ).all()
        assert len(messages) == 2


async def test_unknown_return_keeps_original_transport_and_corrected_result(roller_return):
    flow = roller_return
    await flow.bins(1)
    async with flow.factory.begin() as session:
        opportunity = await flow.selected(session)
        batch_id = await flow.start(session, opportunity)
        await flow.decide(session, batch_id)
        await flow.consume_return(session, batch_id)
        original = await flow.roller.original_record(session, "return_batch", batch_id)
    await flow.success(batch_id, placed=False, status="UNKNOWN")
    async with flow.factory.begin() as session:
        assert await flow.consume_return(session, batch_id) == "UNKNOWN"
        assert await flow.start(session, opportunity) == batch_id
        current = await flow.roller.original_record(session, "return_batch", batch_id)
        assert current["pending"] and current["transport_task_id"] == original["transport_task_id"]
        assert await flow.roller.get_return_candidates(session, work_line_id=1) == ()
    await flow.success(batch_id, revision=2)
    async with flow.factory.begin() as session:
        assert await flow.consume_return(session, batch_id) == "SUCCEEDED"
        assert (await flow.roller.original_record(session, "return_batch", batch_id))[
            "transport_task_id"
        ] == original["transport_task_id"]


async def test_return_request_has_only_original_wire_fields(roller_return):
    import json

    from pydantic import ValidationError
    from sqlalchemy import select

    from app.businesses._components.bin_roller.request.outbound_bin_return_batch_v1 import (
        ReturnBatchRequest,
    )
    from app.models import ReliableMessage

    flow = roller_return
    await flow.bins(1)
    async with flow.factory.begin() as session:
        batch_id = await flow.start(session, await flow.selected(session, face="90"))
        batch = await flow.roller.original_record(session, "return_batch", batch_id)
        raw = await session.scalar(
            select(ReliableMessage.request_raw).where(ReliableMessage.id == batch["message_id"])
        )
        data = json.loads(raw)["data"]
        assert data == dict(
            workline_code="KT16",
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


async def test_return_outcome_requires_explicit_original_association_and_evidence(roller_return):
    flow = roller_return
    await flow.bins(1)
    async with flow.factory.begin() as session:
        opportunity = await flow.selected(session, kind="SEGMENT")
        service_id = (
            await flow.roller.original_record(session, "return_opportunity", opportunity)
        )["service_id"]
        batch_id = await flow.start(session, opportunity)
        await flow.decide(session, batch_id)
        await flow.consume_return(session, batch_id)
        batch = await flow.roller.original_record(session, "return_batch", batch_id)
        evidence = {
            "batch_id": batch_id,
            "transport_task_id": batch["transport_task_id"],
            "outcome_revision": 1,
        }
        assert (
            await flow.roller.record_return_outcome(
                session,
                service_id=service_id,
                opportunity_id=opportunity,
                batch_id=batch_id,
                reason="RETURN_SUCCEEDED",
                evidence=evidence,
            )
            == ""
        )
        for reason in ("NO_CANDIDATES", "BUFFER_EMPTY"):
            assert (
                await flow.roller.record_return_outcome(
                    session,
                    service_id=service_id,
                    opportunity_id=opportunity,
                    batch_id=None,
                    reason=reason,
                    evidence={},
                )
                == ""
            )
    await flow.success(batch_id)
    async with flow.factory.begin() as session:
        await flow.consume_return(session, batch_id)
        assert (
            await flow.roller.record_return_outcome(
                session,
                service_id=service_id,
                opportunity_id=opportunity,
                batch_id=batch_id,
                reason="RETURN_SUCCEEDED",
                evidence=evidence,
            )
            == opportunity
        )
        assert (
            await flow.roller.record_return_outcome(
                session,
                service_id="other",
                opportunity_id=opportunity,
                batch_id=batch_id,
                reason="RETURN_SUCCEEDED",
                evidence=evidence,
            )
            == ""
        )
        assert (
            await flow.roller.record_return_outcome(
                session,
                service_id=service_id,
                opportunity_id=opportunity,
                batch_id=None,
                reason="RETURN_SUCCEEDED",
                evidence=evidence,
            )
            == ""
        )


async def test_local_empty_evidence_cannot_skip_ready_head(roller_return):
    flow = roller_return
    await flow.bins(1)
    async with flow.factory.begin() as session:
        opportunity = await flow.selected(session)
        service_id = (
            await flow.roller.original_record(session, "return_opportunity", opportunity)
        )["service_id"]
        for reason in ("NO_CANDIDATES", "BUFFER_EMPTY"):
            assert (
                await flow.roller.record_return_outcome(
                    session,
                    service_id=service_id,
                    opportunity_id=opportunity,
                    batch_id=None,
                    reason=reason,
                    evidence={},
                )
                == ""
            )
        assert not (await flow.roller.original_record(session, "return_opportunity", opportunity))[
            "ended"
        ]


@pytest.mark.parametrize("http,code", [(503, "UNAVAILABLE"), (409, "CONFLICT"), (422, "REJECTED")])
async def test_original_request_failure_never_becomes_no_batch(roller_return, http, code):
    import json

    from app.models import ReliableMessage

    flow = roller_return
    await flow.bins(1)
    async with flow.factory.begin() as session:
        opportunity = await flow.selected(session)
        batch_id = await flow.start(session, opportunity)
        original = await flow.roller.original_record(session, "return_batch", batch_id)
        await session.execute(
            ReliableMessage.__table__.update()
            .where(ReliableMessage.id == original["message_id"])
            .values(
                status="FAILED",
                last_http_status=http,
                last_verdict="PEER_TERMINAL",
                response_raw=json.dumps(
                    dict(operation_id=original["operation_id"], code=code, timestamp=2, data={})
                ).encode(),
            )
        )
        assert await flow.consume_return(session, batch_id) == "WAITING_FOR_ALLOCATION"
        assert await flow.start(session, opportunity) == batch_id
        assert await flow.roller.original_record(session, "return_batch", batch_id) == original
        assert not (await flow.roller.original_record(session, "return_opportunity", opportunity))[
            "ended"
        ]


async def test_buffer_empty_records_only_original_opportunity(roller_return):
    flow = roller_return
    async with flow.factory.begin() as session:
        opportunity = await flow.selected(session, kind="PURE_RETURN")
        service_id = (
            await flow.roller.original_record(session, "return_opportunity", opportunity)
        )["service_id"]
        assert (
            await flow.roller.record_return_outcome(
                session,
                service_id=service_id,
                opportunity_id=opportunity,
                batch_id=None,
                reason="BUFFER_EMPTY",
                evidence={"basis": "actual original FIFO"},
            )
            == opportunity
        )
        original = await flow.roller.original_record(session, "return_opportunity", opportunity)
        assert original["ended"] and original["end_reason"] == "BUFFER_EMPTY"
        assert (await flow.roller.original_record(session, "face_service", service_id))[
            "status"
        ] == "READY"
        assert await flow.rows(session, "return_batch") == []
