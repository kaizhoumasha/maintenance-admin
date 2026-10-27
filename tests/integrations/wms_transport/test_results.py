import json

import pytest
from sqlalchemy import delete, select, update

from app.integrations.wms_transport import TransportCaller, TransportService
from app.models import ReliableMessage, TransportTask
from tests.helpers import result_task, transport_callback


async def read(factory, task):
    async with factory() as session:
        return await TransportService().get_result(
            session,
            transport_task_id=task.handle.transport_task_id,
            client_request_id=task.handle.client_request_id,
            caller=task.caller,
        )


async def test_transport_result_uses_applied_outcome_only(transport_rig):
    rig = transport_rig
    task = await result_task(rig.factory)
    assert (await read(rig.factory, task)).closure == "OPEN"
    await transport_callback(rig.factory, task)
    result = await read(rig.factory, task)
    assert result.closure == "CLOSED" and result.outcome_revision == 1
    assert result.outcome["arrival_face"] == "90"
    assert result.closure_basis["outcome_revision"] == 1
    async with rig.factory.begin() as session:
        await session.execute(delete(ReliableMessage).where(ReliableMessage.target == task.target))
    result = await read(rig.factory, task)
    assert result.closure == "CLOSED" and result.submission_state == "UNAVAILABLE"


@pytest.mark.parametrize(
    "status,uncertain,reason,closure",
    [
        ("PENDING", False, None, "OPEN"),
        ("SENDING", False, None, "OPEN"),
        ("STALLED", False, None, "OPEN"),
        ("DONE", False, None, "OPEN"),
        ("PENDING", True, None, "UNDETERMINED"),
        ("HELD", True, None, "UNDETERMINED"),
        ("FAILED", False, "LOCAL_ERROR", "UNDETERMINED"),
        ("EXPIRED", False, None, "UNDETERMINED"),
    ],
)
async def test_transport_closure_matrix(transport_rig, status, uncertain, reason, closure):
    task = await result_task(transport_rig.factory)
    async with transport_rig.factory.begin() as session:
        await session.execute(
            update(ReliableMessage)
            .where(ReliableMessage.target == task.target)
            .values(
                status=status,
                delivery_uncertain=uncertain,
                fail_reason=reason,
                next_attempt_at=None if status in {"HELD", "STALLED"} else transport_rig.clock(),
            )
        )
    assert (await read(transport_rig.factory, task)).closure == closure


@pytest.mark.parametrize("http,code", [(409, "CONFLICT"), (422, "REJECTED")])
async def test_confirmed_refusal_closes_only_without_execution_evidence(transport_rig, http, code):
    factory = transport_rig.factory
    task = await result_task(factory, bins=True)
    async with factory.begin() as session:
        row = await session.scalar(
            select(ReliableMessage).where(ReliableMessage.target == task.target)
        )
        row.status, row.fail_reason = "FAILED", code
        row.last_http_status, row.last_verdict = http, "PEER_TERMINAL"
        row.response_raw = json.dumps(
            dict(
                operation_id=row.operation_id,
                code=code,
                timestamp=1,
                data=dict(
                    transport_task_id=task.handle.transport_task_id, reason_code="INVALID_DATA"
                ),
            )
        ).encode()
    result = await read(factory, task)
    assert result.closure == "CLOSED" and result.closure_reason == "SUBMIT_REJECTION"
    await transport_callback(factory, task, bins=True, known=False)
    assert (await read(factory, task)).closure == "UNDETERMINED"


async def test_identity_missing_frozen_pending_and_ambiguous_submission(transport_rig):
    factory = transport_rig.factory
    task = await result_task(factory)
    async with factory() as session:
        result = await TransportService().get_result(
            session,
            transport_task_id=task.handle.transport_task_id,
            client_request_id=task.handle.client_request_id,
            caller=TransportCaller("other"),
        )
        assert result.availability == "IDENTITY_MISMATCH" and result.outcome is None
    async with factory.begin() as session:
        original = (
            (
                await session.execute(
                    select(*ReliableMessage.__table__.c).where(
                        ReliableMessage.target == task.target
                    )
                )
            )
            .mappings()
            .one()
        )
        values = dict(original)
        values.pop("id")
        values["target"] = "other-target"
        session.add(ReliableMessage(**values))
    result = await read(factory, task)
    assert result.submission_state == "AMBIGUOUS" and result.closure == "UNDETERMINED"
    async with factory.begin() as session:
        await session.execute(
            update(TransportTask)
            .where(TransportTask.transport_task_id == task.handle.transport_task_id)
            .values(frozen=None)
        )
    assert (await read(factory, task)).closure_reason == "MISSING_FROZEN"


async def test_result_copy_and_no_autoflush(transport_rig):
    factory = transport_rig.factory
    task = await result_task(factory)
    await transport_callback(factory, task)
    async with factory() as session:
        row = await session.scalar(
            select(TransportTask).where(
                TransportTask.transport_task_id == task.handle.transport_task_id
            )
        )
        row.applied_outcome = None
        result = await task.service.get_result(
            session,
            transport_task_id=task.handle.transport_task_id,
            client_request_id=task.handle.client_request_id,
            caller=task.caller,
        )
        assert result.closure == "CLOSED" and row in session.dirty
        result.outcome["rack_id"] = "changed-copy"
        again = await task.service.get_result(
            session,
            transport_task_id=task.handle.transport_task_id,
            client_request_id=task.handle.client_request_id,
            caller=task.caller,
        )
        assert again.outcome["rack_id"] == "R1"


async def test_exchange_result_uses_its_frozen_pair_contract(transport_rig):
    from app.core.identifiers import uuid7
    from app.integrations.wms_transport import BinExchangePair, RackBinSlot, TransportCaller

    service = transport_rig.service
    caller = TransportCaller("exchange-line")
    async with transport_rig.factory.begin() as session:
        task = await service.exchange_bins(
            session,
            client_request_id=str(uuid7()),
            caller=caller,
            exchange_pairs=[
                BinExchangePair(
                    "C1", RackBinSlot("R1", "90", "S1"), "C2", RackBinSlot("R2", "90", "S2")
                )
            ],
        )
    async with transport_rig.factory() as session:
        result = await service.get_result(
            session,
            transport_task_id=task.transport_task_id,
            client_request_id=task.client_request_id,
            caller=caller,
        )
        assert result.availability == "FOUND" and result.closure == "OPEN"


@pytest.mark.parametrize(
    "status,unknown,closure",
    [
        ("FAILED", False, "CLOSED"),
        ("CANCELLED", False, "CLOSED"),
        ("FAILED", True, "UNDETERMINED"),
    ],
)
async def test_rack_execution_failure_and_cancellation_preserve_position(
    transport_rig, status, unknown, closure
):
    from tests.helpers import deliver_transport_callback

    task = await result_task(transport_rig.factory)
    data = dict(
        transport_task_id=task.handle.transport_task_id,
        kind="RACK_MOVE",
        rack_id="R1",
        outcome_revision=1,
        status=status,
    )
    if unknown:
        data.update(position_unknown=True, failure_code="POSITION_UNKNOWN")
    else:
        data["final_position"] = dict(kind="RACK_POSITION", location_code="FROM")
        if status == "FAILED":
            data["failure_code"] = "MANUAL_ABORTED"
    await deliver_transport_callback(transport_rig.factory, "transport.task.resulted@v1", data)
    result = await read(transport_rig.factory, task)
    assert result.closure == closure and result.outcome_revision == 1
    assert result.outcome["position_unknown"] is (True if unknown else None)


async def test_pending_callback_and_settled_delivery_are_not_applied_results(transport_rig):
    from tests.helpers import deliver_transport_callback

    factory = transport_rig.factory
    task = await result_task(factory, bins=True)
    await deliver_transport_callback(
        factory,
        "transport.task.resulted@v1",
        dict(
            transport_task_id=task.handle.transport_task_id,
            kind="BIN_MOVE",
            outcome_revision=1,
            results=[
                dict(
                    container_id=c,
                    status="SUCCEEDED",
                    final_position=dict(kind="RACK_BIN_SLOT", rack_id=r, rack_face="90", slot_id=s),
                )
                for c, r, s in [("C1", "R1", "S1"), ("C2", "R2", "S2")]
            ],
        ),
    )
    result = await read(factory, task)
    assert result.submission_delivery_status == "DONE"
    assert result.closure == "OPEN" and result.has_pending_outcome and result.pending_revision == 1
    assert result.outcome is None and result.outcome_revision is None
    assert not hasattr(result, "pending_outcome")
    async with factory() as session:
        q = await task.service.list_unclosed_predecessors(
            session,
            caller=task.caller,
            cycle_id=task.cycle_id,
            predecessor_ids=(task.handle.transport_task_id,),
            container_id="C1",
        )
        assert q.items[0].closure == "OPEN" and q.complete and q.unresolved


async def test_missing_task_and_bad_submission_never_become_success(transport_rig):
    task = await result_task(transport_rig.factory)
    async with transport_rig.factory() as session:
        missing = await task.service.get_result(
            session,
            transport_task_id="missing",
            client_request_id=task.handle.client_request_id,
            caller=task.caller,
        )
        assert missing.availability == "NOT_FOUND" and missing.closure == "UNDETERMINED"
    async with transport_rig.factory.begin() as session:
        await session.execute(
            update(ReliableMessage)
            .where(ReliableMessage.target == task.target)
            .values(request_raw=b"{}")
        )
    assert (await read(transport_rig.factory, task)).submission_state == "INVALID"
    assert (await read(transport_rig.factory, task)).closure == "UNDETERMINED"


async def test_result_reader_never_writes_locks_flushes_or_sends(transport_rig, monkeypatch):
    from sqlalchemy import event

    factory = transport_rig.factory
    task = await result_task(factory)
    await transport_callback(factory, task)
    statements = []

    def capture(conn, cursor, statement, parameters, context, many):
        if statement != "BEGIN":
            statements.append(statement)

    def forbidden(*args, **kwargs):
        raise AssertionError("reader attempted a side effect")

    event.listen(transport_rig.engine.sync_engine, "before_cursor_execute", capture)
    try:
        async with factory() as session:
            for method in ("commit", "rollback", "flush"):
                monkeypatch.setattr(session, method, forbidden)
            monkeypatch.setattr(task.service, "_outbox", None)
            result = await task.service.get_result(
                session,
                transport_task_id=task.handle.transport_task_id,
                client_request_id=task.handle.client_request_id,
                caller=task.caller,
            )
            assert result.closure == "CLOSED"
        assert len(statements) == 1 and statements[0].lstrip().startswith("SELECT")
        assert "FOR UPDATE" not in statements[0]
    finally:
        event.remove(transport_rig.engine.sync_engine, "before_cursor_execute", capture)


async def test_temporary_read_failures_propagate(transport_rig):
    from sqlalchemy import event

    task = await result_task(transport_rig.factory)

    def fail(conn, cursor, statement, parameters, context, many):
        if statement.lstrip().startswith("SELECT"):
            raise TimeoutError("temporary read failure")

    event.listen(transport_rig.engine.sync_engine, "before_cursor_execute", fail)
    try:
        with pytest.raises(TimeoutError):
            await read(transport_rig.factory, task)
    finally:
        event.remove(transport_rig.engine.sync_engine, "before_cursor_execute", fail)


@pytest.mark.parametrize("http,code", [(409, "CONFLICT"), (422, "REJECTED")])
@pytest.mark.parametrize("milestone", ["SOURCE_PICKED", "TARGET_PLACED", "POSITION_UNKNOWN"])
async def test_refusal_and_applied_position_milestones_require_reconciliation(
    transport_rig, http, code, milestone
):
    from tests.helpers import deliver_transport_callback

    factory = transport_rig.factory
    task = await result_task(factory, bins=True)
    async with factory.begin() as session:
        row = await session.scalar(
            select(ReliableMessage).where(ReliableMessage.target == task.target)
        )
        row.status, row.fail_reason = "FAILED", code
        row.last_http_status, row.last_verdict = http, "PEER_TERMINAL"
        row.response_raw = json.dumps(
            dict(
                operation_id=row.operation_id,
                code=code,
                timestamp=1,
                data=dict(
                    transport_task_id=task.handle.transport_task_id, reason_code="INVALID_DATA"
                ),
            )
        ).encode()
    event = dict(
        transport_task_id=task.handle.transport_task_id, container_id="C1", milestone=milestone
    )
    if milestone == "TARGET_PLACED":
        event["final_position"] = dict(
            kind="RACK_BIN_SLOT", rack_id="R1", rack_face="90", slot_id="S1"
        )
    await deliver_transport_callback(factory, "transport.task.member_position_changed@v1", event)
    result = await read(factory, task)
    conflict = (
        milestone != "POSITION_UNKNOWN"
    )  # This event is diagnostic-only in the existing callback.
    assert result.submission_response_state == "VALID"
    assert result.submission_http_status == http and result.submission_response_code == code
    assert result.outcome is None and not result.has_pending_outcome
    assert result.closure == ("UNDETERMINED" if conflict else "CLOSED")
    assert result.closure_reason == ("INVALID_EVIDENCE" if conflict else "SUBMIT_REJECTION")
    if conflict:
        assert result.member_facts["C1"][milestone] == event.get("final_position", True)
        assert result.closure_basis is None
    else:
        assert result.member_facts == {}
    async with factory() as session:
        for resource in (dict(rack_id="R1"), dict(container_id="C1")):
            q = await task.service.list_unclosed_predecessors(
                session,
                caller=task.caller,
                cycle_id=task.cycle_id,
                predecessor_ids=(task.handle.transport_task_id,),
                **resource,
            )
            assert q.items[0].closure == result.closure
            assert q.complete is (not conflict)
            assert bool(q.unresolved) is conflict
    # A later valid, applied result can resolve the conflict without erasing historical milestones.
    await deliver_transport_callback(
        factory,
        "transport.task.resulted@v1",
        dict(
            transport_task_id=task.handle.transport_task_id,
            kind="BIN_MOVE",
            outcome_revision=1,
            results=[
                dict(
                    container_id=c,
                    status="FAILED",
                    failure_code="MANUAL_ABORTED",
                    final_position=dict(kind="HANDOFF_POSITION", location_code=position),
                )
                for c, position in [("C1", "SCAN"), ("C2", "SCAN2")]
            ],
        ),
    )
    resolved = await read(factory, task)
    assert resolved.closure == "CLOSED" and resolved.closure_reason == "APPLIED_RESULT"
    assert resolved.member_facts == result.member_facts
