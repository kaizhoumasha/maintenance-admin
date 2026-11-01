import copy
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select, update

from app.businesses._components.sorting_task.contracts import CompletionDecision, PrepareAccepted
from app.core.clock import unix_ms
from app.core.identifiers import uuid7
from app.integrations.wms_callback import Accepted, Unavailable
from app.integrations.wms_callback.receiver import receive
from app.models import CallbackReceipt, WorkLine


@pytest.fixture
def recorded_cases():
    return json.loads((Path(__file__).parent / "fixtures/kt16_task_cases.json").read_text())


async def start_recorded_task(flow, cases):
    async with flow.factory() as session, session.begin():
        await session.execute(
            update(WorkLine).where(WorkLine.id == flow.line_ids[0]).values(line_code="KT16")
        )
    task_id = str(uuid7())
    issued = copy.deepcopy(cases["good"]["issued"]["data"])
    issued.update(task_id=task_id, business_type="outbound")
    raw = recorded_message(flow, "issued", issued)
    assert (await flow.deliver(raw)).status_code == 202
    return task_id


def recorded_message(flow, name, data):
    envelope = json.loads(flow.message(name, data))
    envelope["timestamp"] = unix_ms(datetime.now(UTC))
    return json.dumps(envelope).encode()


def test_recorded_prepare_and_completion_are_decisions_without_physical_evidence(recorded_cases):
    prepare = recorded_cases["good"]["prepare_response"]
    completed = recorded_cases["good"]["completion_response"]
    assert PrepareAccepted.parse(202, prepare["code"], prepare["data"]).model_dump() == {}
    assert CompletionDecision.parse(200, completed["code"], completed["data"]).model_dump() == {
        "result": "COMPLETED"
    }
    with pytest.raises(ValueError):
        PrepareAccepted.parse(200, prepare["code"], prepare["data"])
    with pytest.raises(ValueError):
        CompletionDecision.parse(202, completed["code"], completed["data"])


async def test_archived_operation_name_is_not_silently_accepted_as_new_contract(
    task_flow, recorded_cases
):
    original = copy.deepcopy(recorded_cases["good"]["issued"])
    original["operation_id"] = str(uuid7())
    original["timestamp"] = unix_ms(datetime.now(UTC))
    original["data"]["task_id"] = str(uuid7())
    response = await task_flow.deliver(json.dumps(original).encode())
    assert response.status_code == 422
    assert response.body["data"] == {"reason_code": "UNSUPPORTED_OPERATION"}
    assert task_flow.hosts["first"].calls == task_flow.hosts["second"].calls == []
    assert task_flow.notices == []


async def test_recorded_plan_preserves_six_source_faces_and_replays_once(task_flow, recorded_cases):
    f, cases = task_flow, recorded_cases
    task_id = await start_recorded_task(f, cases)
    plan = copy.deepcopy(cases["good"]["plan_delta"]["data"])
    plan["task_id"] = task_id
    raw = recorded_message(f, "plan_delta", plan)
    first = await f.deliver(raw)
    assert first.status_code == 202
    duplicate = await f.deliver(raw)
    assert duplicate.status_code == 200
    assert duplicate.body == first.body | {"code": "DUPLICATE"}
    async with f.factory() as session:
        saved = await session.scalar(
            select(f.hosts["first"].inputs.c.payload).where(
                f.hosts["first"].inputs.c.operation_id == json.loads(raw)["operation_id"]
            )
        )
    assert saved == plan
    assert [(r["rack_id"], r["rack_face"]) for r in saved["added_bin_source_racks"]] == [
        ("510001", ["90"]),
        ("510002", ["270"]),
        ("510017", ["270"]),
        ("510024", ["90"]),
        ("510027", ["90"]),
        ("510034", ["90"]),
    ]
    assert saved["target_rack"] == {"rack_id": "610017", "rack_face": "90"}
    assert f.notices == ["first", "first"]


@pytest.mark.parametrize("change", ["timestamp", "source_order"])
async def test_recorded_plan_identity_drift_keeps_original_receipt(
    task_flow, recorded_cases, change
):
    f = task_flow
    task_id = await start_recorded_task(f, recorded_cases)
    plan = copy.deepcopy(recorded_cases["good"]["plan_delta"]["data"])
    plan["task_id"] = task_id
    raw = recorded_message(f, "plan_delta", plan)
    first = await f.deliver(raw)
    drift = json.loads(raw)
    if change == "timestamp":
        drift["timestamp"] += 1
    else:
        drift["data"]["added_bin_source_racks"].reverse()
    conflict = await f.deliver(json.dumps(drift).encode())
    assert conflict.status_code == 409
    assert conflict.body["data"] == {"reason_code": "IDEMPOTENCY_CONFLICT"}
    assert (await f.deliver(raw)).body == first.body | {"code": "DUPLICATE"}
    assert len(f.hosts["first"].calls) == 2
    assert f.notices == ["first", "first"]


async def test_recorded_source_cancellation_keeps_exact_selector_and_original_host(
    task_flow, recorded_cases
):
    f = task_flow
    task_id = await start_recorded_task(f, recorded_cases)
    async with f.factory() as session, session.begin():
        await session.execute(
            update(WorkLine).where(WorkLine.id == f.line_ids[0]).values(business_code="second")
        )
    cancel = recorded_cases["bad"]["cancel"] | {"task_id": task_id}
    raw = recorded_message(f, "cancel", cancel)
    first = await f.deliver(raw)
    assert first.status_code == 202
    assert (await f.deliver(raw)).body == first.body | {"code": "DUPLICATE"}
    async with f.factory() as session:
        saved = await session.scalar(
            select(f.hosts["first"].inputs.c.payload).where(
                f.hosts["first"].inputs.c.operation_id == json.loads(raw)["operation_id"]
            )
        )
    assert saved == cancel
    assert saved["bin_source_racks"] == [
        {"plan_revision": 1, "rack_id": "510002", "rack_face": ["90"]}
    ]
    assert f.hosts["second"].calls == []
    assert f.notices == ["first", "first"]


async def test_recorded_cancel_unavailable_preserves_caller_fact_without_freezing_input(
    task_flow, recorded_cases
):
    f = task_flow
    task_id = await start_recorded_task(f, recorded_cases)
    raw = recorded_message(f, "cancel", recorded_cases["bad"]["cancel"] | {"task_id": task_id})
    f.hosts["first"].outcome = Unavailable()
    async with f.factory() as session:
        await session.execute(
            update(WorkLine).where(WorkLine.id == f.line_ids[0]).values(line_name="caller fact")
        )
        response = await receive(
            session, raw, runtime=f.runtime, on_business_committed=f.notices.append
        )
        assert response.status_code == 503
        await session.commit()
    async with f.factory() as session:
        assert (
            await session.scalar(select(WorkLine.line_name).where(WorkLine.id == f.line_ids[0]))
            == "caller fact"
        )
        assert (
            await session.scalar(
                select(CallbackReceipt.id).where(
                    CallbackReceipt.operation_id == json.loads(raw)["operation_id"]
                )
            )
            is None
        )
        assert list(await session.scalars(select(f.hosts["first"].inputs.c.operation))) == [
            "sorting.task.issued@v1"
        ]
    assert f.notices == ["first"]
    f.hosts["first"].outcome = None
    assert (await f.deliver(raw)).status_code == 202
    assert f.notices == ["first", "first"]


async def test_recorded_plan_failure_rolls_back_then_recovers_same_identity(
    task_flow, recorded_cases
):
    f = task_flow
    task_id = await start_recorded_task(f, recorded_cases)
    plan = recorded_cases["good"]["plan_delta"]["data"] | {"task_id": task_id}
    raw = recorded_message(f, "plan_delta", plan)
    f.hosts["first"].outcome = RuntimeError("failure after host input write")
    with pytest.raises(RuntimeError, match="failure after host input write"):
        await f.deliver(raw)
    async with f.factory() as session:
        assert list(await session.scalars(select(f.hosts["first"].inputs.c.operation))) == [
            "sorting.task.issued@v1"
        ]
        assert (
            await session.scalar(
                select(CallbackReceipt.id).where(
                    CallbackReceipt.operation_id == json.loads(raw)["operation_id"]
                )
            )
            is None
        )
    assert f.notices == ["first"]
    f.hosts["first"].outcome = None
    assert (await f.deliver(raw)).status_code == 202
    assert f.notices == ["first", "first"]


async def test_recorded_plan_recheck_outer_rollback_keeps_original_pending(
    task_flow, recorded_cases
):
    f = task_flow
    task_id = await start_recorded_task(f, recorded_cases)
    plan = recorded_cases["good"]["plan_delta"]["data"] | {"task_id": task_id}
    raw = recorded_message(f, "plan_delta", plan)
    f.hosts["first"].outcome = Unavailable()
    pending = await f.deliver(raw)
    assert pending.status_code == 503

    async def apply_original(ctx, payload, original):
        assert original == pending.body
        await ctx.session.execute(
            f.hosts["first"]
            .inputs.update()
            .where(f.hosts["first"].inputs.c.operation_id == ctx.operation_id)
            .values(payload={"applied": True})
        )
        return Accepted()

    f.hosts["first"].recheck_task_plan = apply_original
    assert (await f.deliver(raw, rollback=True)).status_code == 202
    async with f.factory() as session:
        receipt = await session.scalar(
            select(CallbackReceipt).where(
                CallbackReceipt.operation_id == json.loads(raw)["operation_id"]
            )
        )
        assert receipt.body == pending.body
        assert (
            await session.scalar(
                select(f.hosts["first"].inputs.c.payload).where(
                    f.hosts["first"].inputs.c.operation_id == json.loads(raw)["operation_id"]
                )
            )
            == plan
        )
    assert f.notices == ["first"]
    assert (await f.deliver(raw)).status_code == 202
    assert f.notices == ["first", "first"]
