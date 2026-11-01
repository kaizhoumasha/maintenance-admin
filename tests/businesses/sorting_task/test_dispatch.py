"""Shared callbacks route by trusted workline once, then original task forever."""

import json
from dataclasses import replace

import pytest
from sqlalchemy import delete, select, update

from app.businesses import catalog
from app.integrations.wms_callback import (
    Accepted,
    CallbackResponse,
    Conflict,
    Rejected,
    Unavailable,
)
from app.models import CallbackReceipt, WorkLine


def issued(task_id="TASK-1", mode="MANUAL", line="SORT-0", direction="outbound"):
    return dict(
        task_id=task_id,
        task_type=mode,
        business_type=direction,
        workline_code=line,
        queue_revision=1,
        dispatch_sequence=1,
    )


@pytest.mark.parametrize(
    "mode,line,owner", [("MANUAL", "SORT-0", "first"), ("AUTO", "SORT-1", "second")]
)
async def test_new_issued_uses_trusted_workline(task_flow, mode, line, owner):
    f = task_flow
    response = await f.deliver(f.message("issued", issued(mode=mode, line=line)))
    assert response.status_code == 202
    assert f.notices == [owner]
    assert len(f.hosts[owner].calls) == 1
    other = "second" if owner == "first" else "first"
    assert f.hosts[other].calls == []


@pytest.mark.parametrize(
    "mode,line,status,reason",
    [
        ("AUTO", "SORT-0", 409, "STATE_CONFLICT"),
        ("MANUAL", "SORT-1", 409, "STATE_CONFLICT"),
        ("MANUAL", "SORT-2", 503, None),
        ("MANUAL", "MISSING", 409, "REFERENCE_CONFLICT"),
    ],
)
async def test_new_issued_refuses_untrusted_or_unsupported_host(
    task_flow, mode, line, status, reason
):
    f = task_flow
    response = await f.deliver(f.message("issued", issued(mode=mode, line=line)))
    assert response.status_code == status
    assert response.body["data"] == ({"reason_code": reason} if reason else {})
    async with f.factory() as session:
        for host in f.hosts.values():
            assert (await session.execute(select(host.tasks))).all() == []
    assert f.notices == []


async def test_existing_task_ignores_workline_rebinding(task_flow):
    f = task_flow
    await f.seed()
    async with f.factory() as session, session.begin():
        await session.execute(
            update(WorkLine).where(WorkLine.line_code == "SORT-0").values(business_code="second")
        )
    response = await f.deliver(f.message("issued", issued()))
    assert response.status_code == 202
    assert len(f.hosts["first"].calls) == 1
    assert f.hosts["second"].calls == []
    changed = issued(mode="AUTO")
    refused = await f.deliver(f.message("issued", changed))
    assert refused.status_code == 422
    assert refused.body["data"] == {"reason_code": "INVALID_DATA"}
    assert len(f.hosts["first"].calls) == 1


@pytest.mark.parametrize(
    "name,data",
    [
        ("queue_changed", dict(queue_revision=2, dispatch_sequence=3)),
        (
            "plan_delta",
            dict(plan_revision=1, added_bin_source_racks=[dict(rack_id="R", rack_face=["A"])]),
        ),
        ("cancel", dict(cancel_scope="TASK")),
    ],
)
async def test_reference_notifications_use_original_task(task_flow, name, data):
    f = task_flow
    await f.seed(task_id="AUTO-looking-but-manual")
    response = await f.deliver(f.message(name, dict(task_id="AUTO-looking-but-manual", **data)))
    assert response.status_code == 202
    assert f.notices == ["first"]
    assert len(f.hosts["first"].calls) == 1
    assert f.hosts["second"].calls == []


@pytest.mark.parametrize("problem", ["missing", "ambiguous", "query", "wrong_ref", "target"])
async def test_ambiguous_or_unavailable_owner_never_dispatches(task_flow, monkeypatch, problem):
    f = task_flow
    if problem != "missing":
        await f.seed()
    if problem == "ambiguous":
        await f.seed("second")
    if problem == "query":
        f.hosts["second"].read_error = RuntimeError("store unavailable")
    if problem == "wrong_ref":
        async with f.factory() as session:
            ref = await f.hosts["first"].read_task_owner(session, task_id="TASK-1")
        f.hosts["first"].ref_override = replace(ref, business_code="third")
    if problem == "target":
        monkeypatch.setattr(catalog, "input_target", lambda *args: None)
    raw = f.message("cancel", dict(task_id="TASK-1", cancel_scope="TASK"))
    if problem == "query":
        with pytest.raises(RuntimeError, match="store unavailable"):
            await f.deliver(raw)
    else:
        response = await f.deliver(raw)
        assert response.status_code == (409 if problem == "missing" else 503)
        assert response.body["data"] == (
            {"reason_code": "REFERENCE_CONFLICT"} if problem == "missing" else {}
        )
    assert all(host.calls == [] for host in f.hosts.values())
    assert f.notices == []


@pytest.mark.parametrize(
    "code,direction,resources,allowed",
    [
        (
            "first",
            "outbound",
            dict(added_bin_source_racks=[dict(rack_id="R", rack_face=["A"])]),
            True,
        ),
        (
            "second",
            "inbound",
            dict(added_bin_source_racks=[dict(rack_id="R", rack_face=["A"])]),
            True,
        ),
        (
            "second",
            "outbound",
            dict(added_bin_source_racks=[dict(rack_id="R", rack_face=["A"])]),
            False,
        ),
        ("first", "inbound", dict(added_source_racks=[dict(rack_id="R")]), False),
        (
            "second",
            "inbound",
            dict(
                added_direct_picks=[
                    dict(
                        source_locator=dict(
                            type="RACK_SLOT", rack_id="R", rack_face="A", slot_id="S"
                        )
                    )
                ]
            ),
            False,
        ),
    ],
)
async def test_task_branch_validation_follows_original_owner(
    task_flow, code, direction, resources, allowed
):
    f = task_flow
    await f.seed(code, direction=direction)
    response = await f.deliver(
        f.message("plan_delta", dict(task_id="TASK-1", plan_revision=1, **resources))
    )
    assert response.status_code == (202 if allowed else 422)
    if not allowed:
        assert response.body["data"] == {"reason_code": "INVALID_DATA"}
        assert f.hosts[code].calls == []


async def test_nested_cancel_slots_reach_original_manual_host(task_flow):
    f = task_flow
    await f.seed()
    data = dict(
        task_id="TASK-1",
        cancel_scope="PLAN_MEMBERS",
        direct_pick_sources=[
            dict(plan_revision=1, rack_id="R", rack_face="A", slot_ids=["S2", "S1"])
        ],
    )
    response = await f.deliver(f.message("cancel", data))
    assert response.status_code == 202
    assert f.hosts["first"].calls[0][1] == data


@pytest.mark.parametrize(
    "name,data",
    [
        ("issued", issued(mode="AUTO", line="SORT-1", direction="inbound")),
        ("queue_changed", dict(task_id="TASK-1", queue_revision=2, dispatch_sequence=3)),
        (
            "plan_delta",
            dict(task_id="TASK-1", plan_revision=1, added_source_racks=[dict(rack_id="R")]),
        ),
        ("cancel", dict(task_id="TASK-1", cancel_scope="TASK")),
    ],
)
async def test_archive_guard_routes_before_successful_replay(task_flow, name, data):
    f = task_flow
    await f.seed("second", direction="inbound")
    raw = f.message(name, data)
    first = await f.deliver(raw)
    assert first.status_code == 202
    original_calls = list(f.hosts["second"].calls)
    f.hosts["second"].guard = CallbackResponse(
        409,
        dict(
            operation_id=json.loads(raw)["operation_id"],
            code="CONFLICT",
            timestamp=777,
            data={"reason_code": "TASK_ARCHIVED"},
        ),
        "conflict",
    )
    refusal = await f.deliver(raw)
    assert refusal.body["timestamp"] == 777
    assert f.hosts["second"].calls == original_calls
    async with f.factory() as session:
        receipt = await session.scalar(
            select(CallbackReceipt).where(
                CallbackReceipt.operation_id == json.loads(raw)["operation_id"]
            )
        )
        assert receipt.body == first.body
    assert f.notices == ["second"]


@pytest.mark.parametrize(
    "guard_error",
    [RuntimeError("guard store unavailable"), Accepted(), CallbackResponse(200, {}, "success")],
)
async def test_guard_exception_and_positive_return_do_not_commit(task_flow, guard_error):
    f = task_flow
    await f.seed("second", direction="inbound")
    f.hosts["second"].guard = guard_error
    raw = f.message("cancel", dict(task_id="TASK-1", cancel_scope="TASK"))
    with pytest.raises((RuntimeError, TypeError)):
        await f.deliver(raw)
    assert f.hosts["second"].calls == []
    assert f.notices == []
    async with f.factory() as session:
        assert await session.scalar(select(CallbackReceipt.id)) is None


@pytest.mark.parametrize("failure", ["reject", "store", "outer_rollback"])
async def test_business_change_and_callback_receipt_commit_together(task_flow, failure):
    f = task_flow
    if failure == "reject":
        f.hosts["first"].outcome = Rejected("INVALID_DATA")
    elif failure == "store":
        f.hosts["first"].outcome = RuntimeError("host write failed")
    raw = f.message("issued", issued())
    if failure == "store":
        with pytest.raises(RuntimeError, match="host write failed"):
            await f.deliver(raw)
    else:
        response = await f.deliver(raw, rollback=failure == "outer_rollback")
        assert response.status_code == (422 if failure == "reject" else 202)
    async with f.factory() as session:
        assert (await session.execute(select(f.hosts["first"].tasks))).all() == []
        assert (await session.execute(select(f.hosts["first"].inputs))).all() == []
        receipts = (await session.execute(select(CallbackReceipt))).all()
        assert len(receipts) == (1 if failure == "reject" else 0)
    assert f.notices == []


async def test_plan_recheck_delegates_to_original_host(task_flow):
    f = task_flow
    await f.seed()
    f.hosts["first"].outcome = Unavailable()
    raw = f.message(
        "plan_delta",
        dict(
            task_id="TASK-1",
            plan_revision=1,
            added_bin_source_racks=[dict(rack_id="R", rack_face=["A"])],
        ),
    )
    first = await f.deliver(raw)
    assert first.status_code == 503
    # Test host keeps pending input; its own recheck applies after the obstacle clears.
    async with f.factory() as session, session.begin():
        await session.execute(delete(f.hosts["first"].inputs))
    f.hosts["first"].outcome = None
    assert (await f.deliver(raw)).status_code == 202
    assert f.hosts["first"].rechecks[0]["code"] == "UNAVAILABLE"
    assert f.notices == ["first"]


async def test_reference_resolution_survives_receipt_retention(task_flow):
    f = task_flow
    await f.seed()
    raw = f.message("cancel", dict(task_id="TASK-1", cancel_scope="TASK"))
    assert (await f.deliver(raw)).status_code == 202
    async with f.factory() as session, session.begin():
        await session.execute(delete(CallbackReceipt))
        await session.execute(delete(f.hosts["first"].inputs))
        await session.execute(
            update(WorkLine).where(WorkLine.line_code == "SORT-0").values(business_code="second")
        )
    assert (await f.deliver(raw)).status_code == 202
    assert f.hosts["second"].calls == []
    # Receipt replay follows history; it does not execute the host again.
    assert (await f.deliver(raw)).status_code == 200
    assert len(f.hosts["first"].calls) == 2
    async with f.factory() as session, session.begin():
        await session.execute(delete(CallbackReceipt))
        await session.execute(delete(f.hosts["first"].tasks))
    assert (await f.deliver(raw)).status_code >= 400
    assert len(f.hosts["first"].calls) == 2


@pytest.mark.parametrize(
    "name,data",
    [
        ("queue_changed", dict(queue_revision=2, not_before=1)),
        ("plan_delta", dict(plan_revision=1, added_source_racks=[dict(rack_id="R")])),
        ("cancel", dict(cancel_scope="TASK")),
    ],
)
async def test_reference_rejection_replays_original_response(task_flow, name, data):
    f = task_flow
    raw = f.message(name, dict(task_id="MISSING", **data))
    first = await f.deliver(raw)
    assert first.status_code == 409
    assert first.body["data"] == {"reason_code": "REFERENCE_CONFLICT"}
    replay = await f.deliver(raw)
    assert replay.body == first.body


@pytest.mark.parametrize(
    "name,data",
    [
        (
            "cancel",
            dict(cancel_scope="PLAN_MEMBERS", source_racks=[dict(plan_revision=1, rack_id="R")]),
        ),
        ("plan_delta", dict(plan_revision=1, added_source_racks=[dict(rack_id="R")])),
    ],
)
async def test_missing_reference_refusal_survives_later_incompatible_task(task_flow, name, data):
    f = task_flow
    raw = f.message(name, dict(task_id="TASK-1", **data))
    first = await f.deliver(raw)
    assert first.status_code == 409
    assert first.body["data"] == {"reason_code": "REFERENCE_CONFLICT"}
    assert (await f.deliver(f.message("issued", issued()))).status_code == 202
    notices = list(f.notices)
    replay = await f.deliver(raw)
    assert replay.status_code == first.status_code and replay.body == first.body
    assert len(f.hosts["first"].calls) == 1
    assert f.hosts["first"].rechecks == []
    assert f.hosts["second"].calls == []
    assert f.notices == notices == ["first"]
    async with f.factory() as session:
        receipt = await session.scalar(
            select(CallbackReceipt).where(
                CallbackReceipt.operation_id == json.loads(raw)["operation_id"]
            )
        )
        assert receipt.body == first.body
        assert list(await session.scalars(select(f.hosts["first"].inputs.c.operation))) == [
            "sorting.task.issued@v1"
        ]


async def test_refused_issued_replays_after_corrected_new_identity_creates_task(task_flow):
    f = task_flow
    raw = f.message("issued", issued(mode="AUTO"))
    first = await f.deliver(raw)
    assert first.status_code == 409
    assert first.body["data"] == {"reason_code": "STATE_CONFLICT"}
    assert (await f.deliver(f.message("issued", issued()))).status_code == 202
    calls = list(f.hosts["first"].calls)
    replay = await f.deliver(raw)
    assert replay.status_code == first.status_code and replay.body == first.body
    assert f.hosts["first"].calls == calls
    assert f.hosts["second"].calls == []
    assert f.notices == ["first"]


async def test_missing_plan_reference_does_not_authorize_recheck_after_task_creation(task_flow):
    f = task_flow
    raw = f.message(
        "plan_delta",
        dict(
            task_id="TASK-1",
            plan_revision=1,
            added_bin_source_racks=[dict(rack_id="R", rack_face=["A"])],
        ),
    )
    first = await f.deliver(raw)
    assert first.status_code == 409
    assert first.body["data"] == {"reason_code": "REFERENCE_CONFLICT"}
    assert (await f.deliver(f.message("issued", issued()))).status_code == 202
    replay = await f.deliver(raw)
    assert replay.status_code == first.status_code and replay.body == first.body
    assert f.hosts["first"].rechecks == []
    assert len(f.hosts["first"].calls) == 1
    assert f.notices == ["first"]


async def test_first_invalid_branch_precedes_host_archive_guard(task_flow):
    f = task_flow
    await f.seed("second", direction="inbound")
    f.hosts["second"].guard = RuntimeError("invalid input must not reach host guard")
    raw = f.message(
        "cancel",
        dict(
            task_id="TASK-1",
            cancel_scope="PLAN_MEMBERS",
            direct_pick_sources=[dict(plan_revision=1, rack_id="R", rack_face="A")],
        ),
    )
    response = await f.deliver(raw)
    assert response.status_code == 422
    assert response.body["data"] == {"reason_code": "INVALID_DATA"}
    assert f.hosts["second"].guards == f.hosts["second"].calls == []
    assert f.notices == []


async def test_refusal_replay_keeps_identity_and_real_archive_guard_precedence(task_flow):
    f = task_flow
    raw = f.message(
        "cancel",
        dict(
            task_id="TASK-1",
            cancel_scope="PLAN_MEMBERS",
            direct_pick_sources=[dict(plan_revision=1, rack_id="R", rack_face="A")],
        ),
    )
    first = await f.deliver(raw)
    assert first.status_code == 409
    assert first.body["data"] == {"reason_code": "REFERENCE_CONFLICT"}
    creation = issued(mode="AUTO", line="SORT-1", direction="inbound")
    assert (await f.deliver(f.message("issued", creation))).status_code == 202
    host = f.hosts["second"]
    host.guards.clear()
    host.guard = CallbackResponse(
        409,
        dict(code="CONFLICT", timestamp=777, data={"reason_code": "TASK_ARCHIVED"}),
        "conflict",
    )
    changed = json.dumps(json.loads(raw) | {"timestamp": 2}).encode()
    conflict = await f.deliver(changed)
    assert conflict.body["data"] == {"reason_code": "IDEMPOTENCY_CONFLICT"}
    assert host.guards == []
    assert (await f.deliver(raw)).body == host.guard.body
    assert host.guards == ["sorting.task.cancel@v1"]
    host.guard = None
    replay = await f.deliver(raw)
    assert replay.status_code == first.status_code and replay.body == first.body
    assert len(host.calls) == 1
    assert f.notices == ["second"]


async def test_changed_issued_workline_is_judged_by_original_host(task_flow):
    f = task_flow
    await f.seed()
    response = await f.deliver(f.message("issued", issued(line="SORT-1")))
    assert response.status_code == 409
    assert response.body["data"] == {"reason_code": "STATE_CONFLICT"}
    assert len(f.hosts["first"].calls) == 1
    assert f.hosts["second"].calls == []
    assert f.notices == []


@pytest.mark.parametrize("code", ["first", "second"])
async def test_outbound_replay_keeps_original_success_without_inbound_policy(task_flow, code):
    f = task_flow
    await f.seed(code, direction="outbound")
    raw = f.message("cancel", dict(task_id="TASK-1", cancel_scope="TASK"))
    first = await f.deliver(raw)
    assert first.status_code == 202
    f.hosts[code].guard = CallbackResponse(
        409, dict(code="CONFLICT", timestamp=777, data={"reason_code": "TASK_ARCHIVED"}), "conflict"
    )
    replay = await f.deliver(raw)
    assert replay.status_code == 200
    assert replay.body["timestamp"] == first.body["timestamp"]
    assert len(f.hosts[code].calls) == 1


@pytest.mark.parametrize("caller_fact", [True, False])
async def test_business_wakeup_waits_for_outer_commit(task_flow, caller_fact):
    from app.integrations.wms_callback.receiver import receive

    f = task_flow
    async with f.factory() as session:
        if not caller_fact and session.get_bind().dialect.name == "sqlite":
            pytest.skip("fresh caller root SAVEPOINT has legacy SQLite early-commit semantics")
        if caller_fact:
            await session.execute(
                update(WorkLine).where(WorkLine.id == f.line_ids[0]).values(line_name="caller fact")
            )
        async with session.begin_nested():
            response = await receive(
                session,
                f.message("issued", issued()),
                runtime=f.runtime,
                on_business_committed=f.notices.append,
            )
            assert response.status_code == 202
        assert f.notices == []
        await session.rollback()
    assert f.notices == []
    async with f.factory() as session:
        assert (await session.execute(select(f.hosts["first"].tasks))).all() == []
        assert await session.scalar(select(CallbackReceipt.id)) is None


@pytest.mark.parametrize("name", ["issued", "cancel"])
async def test_successful_replay_without_original_owner_is_unavailable(task_flow, name):
    f = task_flow
    await f.seed()
    raw = f.message(
        name, issued() if name == "issued" else dict(task_id="TASK-1", cancel_scope="TASK")
    )
    first = await f.deliver(raw)
    assert first.status_code == 202
    async with f.factory() as session, session.begin():
        await session.execute(delete(f.hosts["first"].tasks))
    replay = await f.deliver(raw)
    assert replay.status_code == 503
    assert replay.body["data"] == {}
    async with f.factory() as session:
        receipt = await session.scalar(
            select(CallbackReceipt).where(
                CallbackReceipt.operation_id == json.loads(raw)["operation_id"]
            )
        )
        assert receipt.body == first.body
    assert len(f.hosts["first"].calls) == 1
    assert f.notices == ["first"]


async def test_invalid_branch_replays_original_rejection_time(task_flow):
    f = task_flow
    await f.seed("first", direction="inbound")
    raw = f.message(
        "plan_delta",
        dict(task_id="TASK-1", plan_revision=1, added_source_racks=[dict(rack_id="R")]),
    )
    first = await f.deliver(raw)
    assert first.status_code == 422
    replay = await f.deliver(raw)
    assert replay.body == first.body
    assert f.hosts["first"].calls == []


async def test_guard_receives_deeply_readonly_original_response(task_flow):
    f = task_flow
    await f.seed("second", direction="inbound")
    snapshots = []

    async def guard(ctx, payload):
        snapshots.append(getattr(ctx, "original_response", None))
        return None

    f.hosts["second"].archive_guard = guard
    f.hosts["second"].outcome = Accepted({"nested": {"items": [1, 2]}})
    raw = f.message("cancel", dict(task_id="TASK-1", cancel_scope="TASK"))
    first = await f.deliver(raw)
    replay = await f.deliver(raw)
    assert first.status_code == 202 and replay.status_code == 200
    assert snapshots[0] is None
    original = snapshots[1]
    assert original is not None
    assert original["code"] == "RECEIVED"
    assert original["data"]["nested"]["items"] == (1, 2)
    with pytest.raises(TypeError):
        original["code"] = "CHANGED"
    with pytest.raises(TypeError):
        original["data"]["nested"]["items"][0] = 9
    async with f.factory() as session:
        receipt = await session.scalar(
            select(CallbackReceipt).where(
                CallbackReceipt.operation_id == json.loads(raw)["operation_id"]
            )
        )
        assert receipt.body == first.body


async def test_guard_and_recheck_share_original_response_snapshot(task_flow):
    f = task_flow
    await f.seed()
    snapshots = []

    async def guard(ctx, payload):
        snapshots.append(getattr(ctx, "original_response", None))
        return None

    f.hosts["first"].archive_guard = guard
    f.hosts["first"].outcome = Unavailable()
    raw = f.message(
        "plan_delta",
        dict(
            task_id="TASK-1",
            plan_revision=1,
            added_bin_source_racks=[dict(rack_id="R", rack_face=["A"])],
        ),
    )
    assert (await f.deliver(raw)).status_code == 503
    async with f.factory() as session, session.begin():
        await session.execute(delete(f.hosts["first"].inputs))
    f.hosts["first"].outcome = None
    assert (await f.deliver(raw)).status_code == 202
    assert snapshots[-1] is f.hosts["first"].rechecks[0]


async def test_rejected_issued_never_selects_rebound_workline(task_flow):
    f = task_flow
    raw = f.message("issued", issued(mode="AUTO"))
    first = await f.deliver(raw)
    assert first.status_code == 409
    assert first.body["data"] == {"reason_code": "STATE_CONFLICT"}
    async with f.factory() as session, session.begin():
        await session.execute(
            update(WorkLine).where(WorkLine.line_code == "SORT-0").values(business_code="second")
        )
    f.hosts["second"].guard = RuntimeError("must not select rebound business")
    replay = await f.deliver(raw)
    assert replay.body == first.body
    assert f.hosts["second"].calls == []
    assert f.hosts["second"].guards == []
    assert f.notices == []


@pytest.mark.parametrize(
    "name,refusal,status",
    [
        ("plan_delta", "state", 503),
        ("plan_delta", "rejected", 422),
        ("queue_changed", "state", 409),
    ],
)
async def test_missing_owner_preserves_only_definite_original_refusals(
    task_flow, name, refusal, status
):
    f = task_flow
    await f.seed()
    f.hosts["first"].outcome = (
        Rejected("INVALID_DATA")
        if refusal == "rejected"
        else Conflict({"reason_code": "STATE_CONFLICT"})
    )
    data = dict(task_id="TASK-1")
    if name == "plan_delta":
        data.update(plan_revision=1, added_bin_source_racks=[dict(rack_id="R", rack_face=["A"])])
    else:
        data.update(queue_revision=2, dispatch_sequence=3)
    raw = f.message(name, data)
    first = await f.deliver(raw)
    assert first.status_code == (422 if refusal == "rejected" else 409)
    async with f.factory() as session, session.begin():
        await session.execute(delete(f.hosts["first"].tasks))
    replay = await f.deliver(raw)
    assert replay.status_code == status
    if status != 503:
        assert replay.body == first.body
    else:
        assert replay.body["data"] == {}
    async with f.factory() as session:
        receipt = await session.scalar(
            select(CallbackReceipt).where(
                CallbackReceipt.operation_id == json.loads(raw)["operation_id"]
            )
        )
        assert receipt.body == first.body
    assert len(f.hosts["first"].calls) == 1
    assert f.notices == []
