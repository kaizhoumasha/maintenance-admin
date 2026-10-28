import asyncio
import json
import logging
from datetime import UTC, datetime

import pytest
from sqlalchemy import event, select

from app.core.identifiers import uuid7
from app.integrations.wms_callback import receiver
from app.integrations.wms_transport import (
    BinMove,
    HandoffPosition,
    RackBinSlot,
    RackPosition,
)
from app.integrations.wms_transport.protocol import OPERATION as SUBMIT
from app.integrations.wms_transport.wire import bin_move_data, move_rack_data
from app.models import CallbackReceipt, ReliableMessage, TransportTask

PATH = "/api/v1/wms/events"
POSITION = "transport.task.member_position_changed@v1"
RESULTED = "transport.task.resulted@v1"


def slot(name):
    return {"kind": "RACK_BIN_SLOT", "rack_id": "R1", "rack_face": "90", "slot_id": name}


def rack_data(face="90"):
    return move_rack_data("R1", RackPosition("S1"), RackPosition("CTU01"), face)


def bin_data():
    return bin_move_data(
        [
            BinMove("C1", HandoffPosition("CNV1"), RackBinSlot("R1", "90", "S1")),
            BinMove("C2", HandoffPosition("CNV1"), RackBinSlot("R1", "90", "S2")),
        ]
    )


async def add_task(factory, data, *, frozen=True):
    task_id = "transport-" + str(uuid7())
    async with factory.begin() as session:
        session.add(
            TransportTask(
                client_request_id=str(uuid7()),
                transport_task_id=task_id,
                operation_id=str(uuid7()),
                kind=data["kind"],
                input_digest="a" * 64,
                workline_id="line",
                created_at=datetime.now(UTC),
                frozen=data if frozen else None,
            )
        )
    return task_id


async def task_row(factory, task_id):
    async with factory() as session:  # 新会话读回,证明确实落库
        return await session.scalar(
            select(TransportTask).where(TransportTask.transport_task_id == task_id)
        )


async def receipts(factory):
    async with factory() as session:
        return list(await session.scalars(select(CallbackReceipt).order_by(CallbackReceipt.id)))


async def send(client, operation, data, *, operation_id=None, timestamp=1):
    body = dict(
        operation_id=operation_id or str(uuid7()),
        operation=operation,
        timestamp=timestamp,
        data=data,
    )
    return await client.post(PATH, json=body)


def rack_result(task_id, revision=1, **fields):
    data = dict(
        transport_task_id=task_id,
        kind="RACK_MOVE",
        outcome_revision=revision,
        rack_id="R1",
        status="SUCCEEDED",
        final_position={"kind": "RACK_POSITION", "location_code": "CTU01"},
    )
    return data | fields


def bin_result(task_id, revision=1, results=None):
    results = results or [
        {"container_id": "C1", "status": "SUCCEEDED", "final_position": slot("S1")},
        {"container_id": "C2", "status": "SUCCEEDED", "final_position": slot("S2")},
    ]
    return dict(
        transport_task_id=task_id, kind="BIN_MOVE", outcome_revision=revision, results=results
    )


def placed(task_id, container, name):
    return dict(
        transport_task_id=task_id,
        container_id=container,
        milestone="TARGET_PLACED",
        final_position=slot(name),
    )


def ack(task_id):
    return {"transport_task_id": task_id}


# ---- 位置事件 ----
async def test_position_facts_accumulate_regardless_of_arrival_order(client, callback_store):
    task_id = await add_task(callback_store, bin_data())

    async def event(container, milestone, name=None):
        data = dict(transport_task_id=task_id, container_id=container, milestone=milestone)
        if name:
            data["final_position"] = slot(name)
        response = await send(client, POSITION, data)
        assert response.status_code == 202 and response.json()["data"] == ack(task_id)
        return (await task_row(callback_store, task_id)).member_positions

    assert (await event("C1", "TARGET_PLACED", "S1"))["C1"] == {"TARGET_PLACED": slot("S1")}
    assert (await event("C1", "SOURCE_PICKED"))["C1"] == {
        "TARGET_PLACED": slot("S1"),
        "SOURCE_PICKED": True,
    }
    assert (await event("C1", "SOURCE_PICKED"))["C1"]["TARGET_PLACED"] == slot("S1")  # 重复幂等
    # 位置未知不存状态,只留诊断;之后到达的事实照常累积
    assert "C2" not in await event("C2", "POSITION_UNKNOWN")
    assert (await event("C2", "SOURCE_PICKED"))["C2"] == {"SOURCE_PICKED": True}
    diagnostics = [r.diagnostic for r in await receipts(callback_store) if r.diagnostic]
    assert diagnostics[0][0]["reason_code"] == "POSITION_UNKNOWN_REPORTED"


async def test_position_that_contradicts_the_frozen_task_is_accepted_but_not_applied(
    client, callback_store
):
    task_id = await add_task(callback_store, bin_data())
    wrong = await send(client, POSITION, placed(task_id, "C1", "S9"))
    stranger = await send(client, POSITION, placed(task_id, "C9", "S1"))
    assert wrong.status_code == stranger.status_code == 202
    assert (await task_row(callback_store, task_id)).member_positions is None
    reasons = [item["reason_code"] for r in await receipts(callback_store) for item in r.diagnostic]
    assert reasons == ["TARGET_MISMATCH", "UNKNOWN_MEMBER"]


# ---- 最终结果:货架族 ----
async def test_rack_result_is_applied_with_the_frozen_target_face(client, callback_store):
    task_id = await add_task(callback_store, rack_data("90"))
    response = await send(client, RESULTED, rack_result(task_id))
    assert response.status_code == 202 and response.json()["data"] == ack(task_id)
    row = await task_row(callback_store, task_id)
    assert row.outcome_status == "SUCCEEDED" and row.applied_outcome_revision == 1
    assert row.applied_outcome["arrival_face"] == "90"  # 缺省时用冻结目标面补齐
    assert row.applied_outcome["final_position"]["location_code"] == "CTU01"


async def test_long_rack_and_position_codes_are_preserved_in_applied_results(
    client, callback_store
):
    rack, position = "架" * 257, "位" * 257
    task_id = await add_task(
        callback_store, move_rack_data(rack, RackPosition("source"), RackPosition(position))
    )
    data = rack_result(
        task_id, rack_id=rack, final_position={"kind": "RACK_POSITION", "location_code": position}
    )
    assert (await send(client, RESULTED, data)).status_code == 202
    row = await task_row(callback_store, task_id)
    assert row.outcome_status == "SUCCEEDED"
    assert row.applied_outcome["rack_id"] == rack
    assert row.applied_outcome["final_position"]["location_code"] == position
    assert (await send(client, RESULTED, data)).status_code == 200


@pytest.mark.parametrize("target_kind", ["rack", "handoff"])
async def test_long_bin_resource_codes_preserve_position_evidence_and_results(
    client, callback_store, target_kind
):
    container, rack, slot_id, handoff = (letter * 257 for letter in ("箱", "架", "格", "站"))
    target = RackBinSlot(rack, "90", slot_id) if target_kind == "rack" else HandoffPosition(handoff)
    source = (
        HandoffPosition("source") if target_kind == "rack" else RackBinSlot(rack, "90", slot_id)
    )
    data = bin_move_data([BinMove(container, source, target)])
    task_id = await add_task(callback_store, data)
    final_position = data["moves"][0]["target"]
    result = bin_result(
        task_id,
        results=[
            {"container_id": container, "status": "SUCCEEDED", "final_position": final_position}
        ],
    )
    assert (await send(client, RESULTED, result)).status_code == 202
    if target_kind == "rack":
        assert (await task_row(callback_store, task_id)).pending_outcome is not None
    event_data = dict(
        transport_task_id=task_id,
        container_id=container,
        milestone="TARGET_PLACED",
        final_position=final_position,
    )
    assert (await send(client, POSITION, event_data)).status_code == 202
    row = await task_row(callback_store, task_id)
    assert row.outcome_status == "SUCCEEDED" and row.pending_outcome is None
    assert row.applied_outcome["results"][0]["container_id"] == container
    assert row.applied_outcome["results"][0]["final_position"] == final_position
    assert row.member_positions[container]["TARGET_PLACED"] == final_position
    assert (await send(client, RESULTED, result)).status_code == 200


async def test_unknown_result_converges_to_a_later_determined_result(client, callback_store):
    task_id = await add_task(callback_store, rack_data())
    unknown = rack_result(task_id, 1, status="FAILED", failure_code="POSITION_UNKNOWN")
    unknown |= dict(final_position=None, position_unknown=True)
    assert (await send(client, RESULTED, unknown)).status_code == 202
    assert (await task_row(callback_store, task_id)).outcome_status == "UNKNOWN"
    assert (await send(client, RESULTED, rack_result(task_id, 2))).status_code == 202
    row = await task_row(callback_store, task_id)
    assert (row.outcome_status, row.applied_outcome_revision) == ("SUCCEEDED", 2)


async def test_versions_never_roll_back_and_terminal_results_are_not_rewritten(
    client, callback_store
):
    task_id = await add_task(callback_store, rack_data())
    assert (await send(client, RESULTED, rack_result(task_id, 2))).status_code == 202
    late = rack_result(task_id, 1, status="CANCELLED")  # 从未见过的低版本迟到
    assert (await send(client, RESULTED, late)).status_code == 202
    failed = rack_result(task_id, 3, status="FAILED", failure_code="MANUAL_ABORTED")
    assert (await send(client, RESULTED, failed)).status_code == 202
    row = await task_row(callback_store, task_id)
    assert (row.outcome_status, row.applied_outcome_revision) == ("SUCCEEDED", 2)
    reasons = [
        item["reason_code"] for r in await receipts(callback_store) for item in r.diagnostic or []
    ]
    assert reasons == ["TERMINAL_CONFLICT"]


async def test_same_revision_from_a_new_identity_is_duplicate_or_conflict(client, callback_store):
    task_id = await add_task(callback_store, rack_data())
    data = rack_result(task_id)
    assert (await send(client, RESULTED, data)).status_code == 202
    again = await send(client, RESULTED, data)
    assert again.status_code == 200 and again.json()["code"] == "DUPLICATE"
    assert again.json()["data"] == ack(task_id)
    other = await send(client, RESULTED, rack_result(task_id, status="CANCELLED"))
    assert other.status_code == 409 and other.json()["data"] == ack(task_id)
    assert (await task_row(callback_store, task_id)).outcome_status == "SUCCEEDED"


@pytest.mark.parametrize("conflicting", [False, True])
async def test_concurrent_results_have_one_winner_and_replayable_receipts(
    client, callback_store, conflicting
):
    if callback_store.kw["bind"].dialect.name != "postgresql":
        pytest.skip("PostgreSQL task-row serialization")
    task_id = await add_task(callback_store, rack_data())
    messages = [rack_result(task_id), rack_result(task_id)]
    if conflicting:
        messages[1]["status"] = "CANCELLED"
    identities = [str(uuid7()), str(uuid7())]
    responses = await asyncio.gather(
        *(
            send(client, RESULTED, data, operation_id=identity)
            for identity, data in zip(identities, messages, strict=True)
        )
    )
    assert sorted(response.status_code for response in responses) == (
        [202, 409] if conflicting else [200, 202]
    )
    winner = next(index for index, response in enumerate(responses) if response.status_code == 202)
    row = await task_row(callback_store, task_id)
    assert row.outcome_status == messages[winner]["status"]
    assert row.applied_outcome_revision == 1
    assert len(await receipts(callback_store)) == 2
    for identity, data, first in zip(identities, messages, responses, strict=True):
        replay = await send(client, RESULTED, data, operation_id=identity)
        expected = dict(first.json())
        if first.status_code == 202:
            expected["code"] = "DUPLICATE"
        assert replay.status_code == (200 if first.status_code == 202 else first.status_code)
        assert replay.json() == expected


async def test_concurrent_member_positions_do_not_overwrite_each_other(client, callback_store):
    if callback_store.kw["bind"].dialect.name != "postgresql":
        pytest.skip("PostgreSQL task-row serialization")
    task_id = await add_task(callback_store, bin_data())
    assert (await send(client, RESULTED, bin_result(task_id))).status_code == 202
    responses = await asyncio.gather(
        send(client, POSITION, placed(task_id, "C1", "S1")),
        send(client, POSITION, placed(task_id, "C2", "S2")),
    )
    assert [response.status_code for response in responses] == [202, 202]
    row = await task_row(callback_store, task_id)
    assert row.member_positions == {
        "C1": {"TARGET_PLACED": slot("S1")},
        "C2": {"TARGET_PLACED": slot("S2")},
    }
    assert row.outcome_status == "SUCCEEDED" and row.pending_outcome is None


async def test_results_that_contradict_the_frozen_task_are_accepted_but_not_applied(
    client, callback_store
):
    task_id = await add_task(callback_store, rack_data("90"))
    cases = [
        rack_result(task_id, 1, rack_id="OTHER"),
        rack_result(task_id, 2, final_position={"kind": "RACK_POSITION", "location_code": "X"}),
        rack_result(task_id, 3, arrival_face="270"),
        bin_result(task_id, 4),
    ]
    for data in cases:
        assert (await send(client, RESULTED, data)).status_code == 202
    assert (await task_row(callback_store, task_id)).outcome_status is None
    reasons = [item["reason_code"] for r in await receipts(callback_store) for item in r.diagnostic]
    assert reasons == ["RACK_MISMATCH", "TARGET_MISMATCH", "FACE_MISMATCH", "KIND_MISMATCH"]


async def test_failed_and_cancelled_results_may_end_away_from_the_target(client, callback_store):
    failed_id = await add_task(callback_store, rack_data())
    away = {"kind": "RACK_POSITION", "location_code": "SOMEWHERE"}
    failed = rack_result(
        failed_id, status="FAILED", failure_code="RCS_EXECUTION_FAILED", final_position=away
    )
    cancelled_id = await add_task(callback_store, rack_data())
    cancelled = rack_result(cancelled_id, status="CANCELLED", final_position=away)
    for data in (failed, cancelled):
        assert (await send(client, RESULTED, data)).status_code == 202
    assert (await task_row(callback_store, failed_id)).outcome_status == "FAILED"
    assert (await task_row(callback_store, cancelled_id)).outcome_status == "CANCELLED"


# ---- 最终结果:料箱族与前置位置 ----
async def test_bin_result_waits_for_its_target_positions_then_completes(client, callback_store):
    task_id = await add_task(callback_store, bin_data())
    assert (await send(client, RESULTED, bin_result(task_id))).status_code == 202
    row = await task_row(callback_store, task_id)
    assert row.outcome_status is None and row.pending_outcome["revision"] == 1
    await send(client, POSITION, placed(task_id, "C1", "S1"))
    assert (await task_row(callback_store, task_id)).outcome_status is None
    await send(client, POSITION, placed(task_id, "C2", "S2"))
    row = await task_row(callback_store, task_id)
    assert row.outcome_status == "SUCCEEDED" and row.pending_outcome is None
    assert [item["container_id"] for item in row.applied_outcome["results"]] == ["C1", "C2"]


async def test_older_pending_result_never_overrides_a_newer_applied_one(client, callback_store):
    task_id = await add_task(callback_store, bin_data())
    assert (await send(client, RESULTED, bin_result(task_id, 1))).status_code == 202
    unknown = [
        {"container_id": "C1", "status": "FAILED", "failure_code": "POSITION_UNKNOWN",
         "position_unknown": True},
        {"container_id": "C2", "status": "FAILED", "failure_code": "POSITION_UNKNOWN",
         "position_unknown": True},
    ]  # fmt: skip
    assert (await send(client, RESULTED, bin_result(task_id, 2, unknown))).status_code == 202
    assert (await task_row(callback_store, task_id)).outcome_status == "UNKNOWN"
    await send(client, POSITION, placed(task_id, "C1", "S1"))
    await send(client, POSITION, placed(task_id, "C2", "S2"))
    row = await task_row(callback_store, task_id)
    assert (row.outcome_status, row.applied_outcome_revision) == ("UNKNOWN", 2)
    assert row.pending_outcome is None


async def test_pending_result_is_discarded_once_a_terminal_result_is_applied(
    client, callback_store
):
    task_id = await add_task(callback_store, bin_data())
    assert (await send(client, RESULTED, bin_result(task_id, 3))).status_code == 202
    away = {"kind": "HANDOFF_POSITION", "location_code": "CNV1"}
    failed = [
        {"container_id": "C1", "status": "FAILED", "failure_code": "MANUAL_ABORTED",
         "final_position": away},
        {"container_id": "C2", "status": "FAILED", "failure_code": "MANUAL_ABORTED",
         "final_position": away},
    ]  # fmt: skip
    assert (await send(client, RESULTED, bin_result(task_id, 2, failed))).status_code == 202
    row = await task_row(callback_store, task_id)
    assert (row.outcome_status, row.applied_outcome_revision) == ("FAILED", 2)
    assert row.pending_outcome is None  # 终态不会被改写,暂存的更高版本被丢弃
    await send(client, POSITION, placed(task_id, "C1", "S1"))
    reasons = [
        item["reason_code"] for r in await receipts(callback_store) for item in r.diagnostic or []
    ]
    assert reasons == []  # 之后的位置事件不会反复重放那条暂存结果


async def test_bin_result_must_cover_exactly_the_frozen_members(client, callback_store):
    task_id = await add_task(callback_store, bin_data())
    partial = [{"container_id": "C1", "status": "SUCCEEDED", "final_position": slot("S1")}]
    assert (await send(client, RESULTED, bin_result(task_id, 1, partial))).status_code == 202
    row = await task_row(callback_store, task_id)
    assert row.outcome_status is None and row.pending_outcome is None
    [diag] = [r.diagnostic for r in await receipts(callback_store) if r.diagnostic]
    assert diag[0]["reason_code"] == "MEMBERS_MISMATCH"


# ---- 未知任务、冻结缺失、隔离 ----
async def test_unknown_task_and_missing_frozen_are_acked_with_a_durable_diagnostic(
    client, callback_store
):
    ghost = "transport-" + str(uuid7())
    response = await send(client, RESULTED, rack_result(ghost))
    assert response.status_code == 202 and response.json()["data"] == ack(ghost)
    legacy = await add_task(callback_store, rack_data(), frozen=False)
    assert (await send(client, RESULTED, rack_result(legacy))).status_code == 202
    reasons = [r.diagnostic[0]["reason_code"] for r in await receipts(callback_store)]
    assert reasons == ["UNKNOWN_TASK", "FROZEN_MISSING"]
    assert (await task_row(callback_store, legacy)).outcome_status is None


@pytest.mark.parametrize("commit_fails", [False, True])
async def test_abnormal_callback_alerts_only_after_its_receipt_commits(
    client, callback_store, caplog, commit_fails
):
    task_id = "transport-" + str(uuid7())
    operation_id = str(uuid7())
    engine = callback_store.kw["bind"]
    if commit_fails:

        def fail_once(connection):
            event.remove(engine.sync_engine, "commit", fail_once)
            raise RuntimeError("commit unavailable")

        event.listen(engine.sync_engine, "commit", fail_once)
    caplog.set_level(logging.WARNING)
    response = await send(client, RESULTED, rack_result(task_id), operation_id=operation_id)
    assert response.status_code == (503 if commit_fails else 202)
    alerts = [record for record in caplog.records if hasattr(record, "diagnostic")]
    if commit_fails:
        assert alerts == [] and await receipts(callback_store) == []
    else:
        [alert] = alerts
        assert alert.diagnostic["reason_code"] == "UNKNOWN_TASK"
        assert alert.diagnostic["transport_task_id"] == task_id
        assert alert.operation == RESULTED and alert.operation_id == operation_id
        assert (await receipts(callback_store))[0].diagnostic[0]["reason_code"] == "UNKNOWN_TASK"


@pytest.mark.parametrize("rollback", [False, True])
async def test_callback_diagnostic_follows_the_callers_savepoint(callback_store, caplog, rollback):
    task_id = "transport-" + str(uuid7())
    envelope = dict(
        operation_id=str(uuid7()), operation=RESULTED, timestamp=1, data=rack_result(task_id)
    )
    caplog.set_level(logging.WARNING)
    async with callback_store.begin() as session:
        async with session.begin_nested() as savepoint:
            response = await receiver.receive(session, json.dumps(envelope).encode())
            assert response.status_code == 202
            if rollback:
                await savepoint.rollback()
        assert not any(hasattr(record, "diagnostic") for record in caplog.records)
    alerts = [record for record in caplog.records if hasattr(record, "diagnostic")]
    assert len(alerts) == len(await receipts(callback_store)) == (0 if rollback else 1)


async def test_an_abnormal_callback_does_not_affect_another_task(client, callback_store):
    broken = await add_task(callback_store, rack_data())
    healthy = await add_task(callback_store, rack_data())
    await send(client, RESULTED, rack_result(broken, rack_id="OTHER"))
    assert (await send(client, RESULTED, rack_result(healthy))).status_code == 202
    assert (await task_row(callback_store, healthy)).outcome_status == "SUCCEEDED"
    assert (await task_row(callback_store, broken)).outcome_status is None


# ---- 回显与拒绝 ----
async def test_invalid_payload_is_rejected_and_still_echoes_a_legal_task_id(client, callback_store):
    task_id = await add_task(callback_store, rack_data())
    bad = rack_result(task_id, status="SUCCEEDED", failure_code="MANUAL_ABORTED")
    response = await send(client, RESULTED, bad)
    assert response.status_code == 422
    assert response.json()["data"] == {"reason_code": "INVALID_EVIDENCE", **ack(task_id)}


@pytest.mark.parametrize("operation", [POSITION, RESULTED])
@pytest.mark.parametrize("task_id", ["x" * 257, "x" * 300, "bad-\ud800-id"])
async def test_unusable_task_ids_are_rejected_without_echo_or_server_error(
    client, callback_store, operation, task_id
):
    """无认证入口:超长或无法渲染的编号按非法证据拒绝,不回显、不写进收据,也不 500。"""
    data = (
        rack_result(task_id)
        if operation == RESULTED
        else dict(transport_task_id=task_id, container_id="C1", milestone="SOURCE_PICKED")
    )
    body = dict(operation_id=str(uuid7()), operation=operation, timestamp=1, data=data)
    # 用 ASCII 转义的原始字节发送:孤立代理字符只能这样出现在线上
    raw = json.dumps(body).encode()
    headers = {"content-type": "application/json"}
    response = await client.post(PATH, content=raw, headers=headers)
    assert response.status_code == 422
    assert response.json()["data"] == {"reason_code": "INVALID_EVIDENCE"}
    replay = await client.post(PATH, content=raw, headers=headers)  # 同身份重放仍可渲染
    assert (replay.status_code, replay.json()["data"]) == (422, {"reason_code": "INVALID_EVIDENCE"})


@pytest.mark.parametrize("operation", [POSITION, RESULTED])
async def test_task_ids_at_the_length_limit_are_preserved(client, callback_store, operation):
    task_id = "t" * 256
    data = (
        rack_result(task_id)
        if operation == RESULTED
        else dict(transport_task_id=task_id, container_id="C1", milestone="SOURCE_PICKED")
    )
    response = await send(client, operation, data)
    assert response.status_code == 202 and response.json()["data"] == ack(task_id)


@pytest.mark.parametrize("rack_id", ["", "bad\x00", "\ud800", None, 123])
async def test_invalid_resource_codes_are_rejected_without_applying_a_result(
    client, callback_store, rack_id
):
    task_id = await add_task(callback_store, rack_data())
    body = dict(
        operation_id=str(uuid7()),
        operation=RESULTED,
        timestamp=1,
        data=rack_result(task_id, rack_id=rack_id),
    )
    response = await client.post(PATH, content=json.dumps(body).encode())
    assert response.status_code == 422
    assert response.json()["data"] == {"reason_code": "INVALID_EVIDENCE", **ack(task_id)}
    assert (await task_row(callback_store, task_id)).outcome_status is None


@pytest.mark.parametrize("length", [10, 11])
async def test_callback_faces_keep_the_unicode_code_point_limit(client, callback_store, length):
    task_id = await add_task(
        callback_store, move_rack_data("R1", RackPosition("S1"), RackPosition("CTU01"))
    )
    face = "🧭" * length
    response = await send(client, RESULTED, rack_result(task_id, arrival_face=face))
    assert response.status_code == (202 if length == 10 else 422)
    row = await task_row(callback_store, task_id)
    if length == 10:
        assert row.applied_outcome["arrival_face"] == face
    else:
        assert row.outcome_status is None


async def test_identity_conflict_echoes_the_first_messages_task_id(client, callback_store):
    first_id, second_id = await add_task(callback_store, rack_data()), "transport-other"
    operation_id = str(uuid7())
    assert (
        await send(client, RESULTED, rack_result(first_id), operation_id=operation_id)
    ).status_code == 202
    changed = await send(client, RESULTED, rack_result(second_id), operation_id=operation_id)
    assert changed.status_code == 409 and changed.json()["data"] == ack(first_id)

    rejected_id = str(uuid7())
    bad = rack_result(first_id, status="SUCCEEDED", failure_code="MANUAL_ABORTED")
    assert (await send(client, RESULTED, bad, operation_id=rejected_id)).status_code == 422
    again = await send(client, RESULTED, bad | {"outcome_revision": 2}, operation_id=rejected_id)
    assert again.status_code == 409 and again.json()["data"] == ack(first_id)

    junk_id = str(uuid7())
    junk = rack_result(first_id) | {"transport_task_id": 123}
    assert (await send(client, RESULTED, junk, operation_id=junk_id)).status_code == 422
    again = await send(client, RESULTED, junk | {"outcome_revision": 2}, operation_id=junk_id)
    assert again.status_code == 409 and again.json()["data"] == {}


# ---- 收到回调即结束提交消息的发送 ----
async def submit_message(factory, task_id, *, status, operation=SUBMIT):
    now = datetime.now(UTC)
    async with factory.begin() as session:
        session.add(
            ReliableMessage(
                target="wms",
                operation=operation,
                operation_id=str(uuid7()),
                request_raw=json.dumps({}).encode(),
                status=status,
                next_attempt_at=None if status == "STALLED" else now,
                business_key=task_id,
                created_at=now,
                updated_at=now,
            )
        )


async def message_statuses(factory, task_id):
    async with factory() as session:
        rows = await session.scalars(
            select(ReliableMessage).where(ReliableMessage.business_key == task_id)
        )
        return sorted((m.operation, m.status) for m in rows)


@pytest.mark.parametrize("status", ["PENDING", "SENDING", "STALLED"])
@pytest.mark.parametrize("kind", ["position", "result", "contradicting_result"])
async def test_any_callback_for_a_task_ends_its_submit_retries(
    client, callback_store, status, kind
):
    task_id = await add_task(callback_store, rack_data())
    other = await add_task(callback_store, rack_data())
    await submit_message(callback_store, task_id, status=status)
    await submit_message(callback_store, task_id, status=status, operation="other.operation@v1")
    await submit_message(callback_store, other, status=status)
    if kind == "position":
        message = (
            POSITION,
            dict(transport_task_id=task_id, container_id="C1", milestone="SOURCE_PICKED"),
        )
    elif kind == "result":
        message = (RESULTED, rack_result(task_id))
    else:  # 与冻结任务矛盾,只告警不应用,但仍是对端已知该任务的证据
        message = (RESULTED, rack_result(task_id, rack_id="RACK-OTHER"))
    assert (await send(client, *message)).status_code == 202
    assert await message_statuses(callback_store, task_id) == [
        ("other.operation@v1", status),
        (SUBMIT, "DONE"),
    ]
    assert await message_statuses(callback_store, other) == [(SUBMIT, status)]


async def test_unknown_task_callback_settles_nothing(client, callback_store):
    task_id = "transport-" + str(uuid7())
    await submit_message(callback_store, task_id, status="PENDING")
    assert (await send(client, RESULTED, rack_result(task_id))).status_code == 202
    assert await message_statuses(callback_store, task_id) == [(SUBMIT, "PENDING")]


@pytest.mark.parametrize("contradicting", [False, True])
async def test_failed_commit_rolls_back_task_revision_diagnostic_and_settlement(
    client, callback_store, caplog, contradicting
):
    task_id = await add_task(callback_store, rack_data())
    await submit_message(callback_store, task_id, status="PENDING")
    engine = callback_store.kw["bind"]

    def fail_once(connection):
        event.remove(engine.sync_engine, "commit", fail_once)
        raise RuntimeError("commit unavailable")

    event.listen(engine.sync_engine, "commit", fail_once)
    caplog.set_level(logging.WARNING)
    data = rack_result(task_id, rack_id="OTHER" if contradicting else "R1")
    response = await send(client, RESULTED, data)
    assert response.status_code == 503 and response.json()["code"] == "UNAVAILABLE"
    row = await task_row(callback_store, task_id)
    assert row.outcome_status is row.applied_outcome is row.applied_outcome_revision is None
    assert row.outcome_digests is None
    assert await receipts(callback_store) == []
    assert await message_statuses(callback_store, task_id) == [(SUBMIT, "PENDING")]
    assert not any(hasattr(record, "diagnostic") for record in caplog.records)


# ---- 锁等待上限 ----
async def test_waiting_for_a_task_row_lock_is_bounded(client, callback_store, monkeypatch):
    if callback_store.kw["bind"].dialect.name != "postgresql":
        pytest.skip("PostgreSQL row-lock behavior")
    task_id = await add_task(callback_store, rack_data())
    monkeypatch.setattr(receiver, "LOCK_TIMEOUT", "300ms")
    async with callback_store() as holder:
        await holder.execute(
            select(TransportTask)
            .where(TransportTask.transport_task_id == task_id)
            .with_for_update()
        )
        response = await send(client, RESULTED, rack_result(task_id))
        await holder.rollback()
    assert response.status_code == 503 and response.json()["code"] == "UNAVAILABLE"
    assert await receipts(callback_store) == []
    assert (await task_row(callback_store, task_id)).outcome_status is None
