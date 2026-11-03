from dataclasses import replace

import pytest

from app.core.identifiers import uuid7

from . import apply_outcome, evidence, origin, respond, store


@pytest.fixture
async def rack(request):
    url = (
        request.getfixturevalue("pg_url")
        if getattr(request, "param", None) == "postgres"
        else "sqlite+aiosqlite://"
    )
    async with store(url) as (factory, dispatch), factory.begin() as session:
        yield session, dispatch


async def test_retried_origin_keeps_original_cycle(rack):
    db, dispatch = rack
    original = origin()
    first = await dispatch.register(db, original)
    retry = await dispatch.register(db, replace(original, cycle_id=str(uuid7())))
    assert first == original.cycle_id
    assert retry == first


async def setup_move(session, dispatch, kind, source="MANUAL_RETURN"):
    job = await dispatch.register(session, origin(source))
    entry = await dispatch.prepare(session, job_id=job, kind="ENTER", source_ref="entry", face="90")
    assert entry is not None
    move = (await dispatch.progress(session, job_id=job)).actions[-1]
    if kind == "ROTATE":
        await apply_outcome(session, move, "SUCCEEDED")
        await dispatch.advance(session, job_id=job, **evidence(job, demand="CONTINUE"), now_ms=1)
        assert await dispatch.prepare(session, job_id=job, kind=kind, source_ref="turn", face="270")
        move = (await dispatch.progress(session, job_id=job)).actions[-1]
    return job, move


@pytest.mark.parametrize("kind", ["ENTER", "ROTATE"])
@pytest.mark.parametrize("status", ["FAILED", "CANCELLED"])
@pytest.mark.parametrize(
    "condition",
    ["ended", "continuable", "pending", "observation", "unresolved", "demand", "handoff"],
)
async def test_safety_departure_after_non_success(rack, monkeypatch, kind, status, condition):
    db, dispatch = rack
    job, move = await setup_move(db, dispatch, kind)
    await apply_outcome(db, move, status)
    transport = dispatch.capabilities.transport
    read = transport.get_result
    predecessors = transport.list_unclosed_predecessors

    async def result(session, **identity):
        value = await read(session, **identity)
        if identity["transport_task_id"] != move.transport_task_id:
            return value
        if condition == "continuable":
            return replace(value, closure="OPEN")
        if condition == "pending":
            return replace(value, has_pending_outcome=True, pending_revision=2)
        if condition == "observation":
            return replace(value, position_observation={**value.outcome, "outcome_revision": 2})
        return value

    async def listed(session, **scope):
        value = await predecessors(session, **scope)
        return replace(value, complete=False) if condition == "unresolved" else value

    monkeypatch.setattr(transport, "get_result", result)
    monkeypatch.setattr(transport, "list_unclosed_predecessors", listed)
    facts = evidence(
        job,
        demand="CONTINUE" if condition == "demand" else "NONE",
        handoff="BLOCKED" if condition == "handoff" else "CLEAR",
    )
    for _ in range(2):
        view = await dispatch.advance(db, job_id=job, **facts, now_ms=5)
    assert [a.kind for a in view.actions if a.kind != "DEPARTURE_DECIDE"] == (
        ["ENTER", "ROTATE"] if kind == "ROTATE" else ["ENTER"]
    )
    assert sum(a.kind == "DEPARTURE_DECIDE" for a in view.actions) == int(condition == "ended")
    assert next(a for a in view.actions if a.id == move.id).outcome["status"] == status


@pytest.mark.parametrize(
    "source,template", [("MANUAL_TRANSFER", "F01"), ("ROLLER_ORDINARY", "CTU03")]
)
@pytest.mark.parametrize("destination", ["ZONE", "RACK_POSITION"])
@pytest.mark.parametrize("http,code", [(202, "RECEIVED"), (200, "DUPLICATE")])
@pytest.mark.parametrize("late", [None, "FAILED", "ORIGINAL_POSITION"])
async def test_closed_departure_ignores_late_result(
    rack, source, template, destination, http, code, late
):
    db, dispatch = rack
    job, move = await setup_move(db, dispatch, "ENTER", source)
    await apply_outcome(db, move, "SUCCEEDED")

    async def advance(now):
        return await dispatch.advance(db, job_id=job, **evidence(job), now_ms=now)

    root = (await advance(10)).actions[-1]
    await respond(
        db,
        root.decision_operation_id,
        200,
        "DECIDED",
        dict(result="READY", rack_destination=dict(type=destination, location_code="NEXT")),
    )
    depart = (await advance(11)).actions[-1]
    assert (depart.kind, depart.template) == ("DEPART", template)
    assert (await advance(12)).pending
    await respond(
        db, depart.submit_operation_id, http, code, dict(transport_task_id=depart.transport_task_id)
    )
    complete = await advance(13)
    assert (complete.state, complete.pending, complete.completed_at_ms) == ("COMPLETED", False, 13)
    assert complete.actions[-1].acceptance_basis["accepted_at"] == 100
    if late:
        await apply_outcome(db, depart, "FAILED" if late == "FAILED" else "CANCELLED")
    assert await advance(14) == complete
    result = await dispatch.final_result(db, job_id=job, action_id=depart.id)
    assert (result.outcome is not None) == bool(late)
    assert await dispatch.pending_upper_id(db, source_kinds=(source,)) is None


@pytest.mark.parametrize(
    "condition",
    ["same_point", "wrong_http", "invalid_wait", "unknown", "wrong_scope", "empty_anchor"],
)
async def test_invalid_departure_evidence_grants_no_move(rack, condition):
    db, dispatch = rack
    job, move = await setup_move(db, dispatch, "ENTER")
    await apply_outcome(db, move, "SUCCEEDED")
    facts = evidence(job)
    demand = facts["demand"]
    if condition == "unknown":
        facts["demand"] = replace(demand, state="UNKNOWN")
    if condition == "wrong_scope":
        facts["demand"] = replace(demand, point=replace(demand.point, job_id="another"))
    if condition == "empty_anchor":
        coverage = demand.coverage[0]
        facts["demand"] = replace(
            demand, coverage=(replace(coverage, anchor=replace(coverage.anchor, version=())),)
        )
    view = await dispatch.advance(db, job_id=job, **facts, now_ms=1)
    if condition in ("unknown", "wrong_scope", "empty_anchor"):
        assert [a.kind for a in view.actions] == ["ENTER"]
        return
    root = view.actions[-1]
    await respond(
        db,
        root.decision_operation_id,
        202 if condition == "wrong_http" else 200,
        "DECIDED",
        {"result": "WAIT", "retry_after_ms": 60001}
        if condition == "invalid_wait"
        else {
            "result": "READY",
            "rack_destination": {"type": "RACK_POSITION", "location_code": "P"},
        },
    )
    view = await dispatch.advance(db, job_id=job, **facts, now_ms=2)
    assert view.state == "RELEASING" and len(view.actions) == 2
    assert view.actions[-1].decision is None


@pytest.mark.parametrize(
    "invalid", ["template", "operation_id", "client_request_id", "cycle_id", "message_id", "status"]
)
async def test_ack_requires_original_departure_identity_and_template(rack, monkeypatch, invalid):
    db, dispatch = rack
    job, move = await setup_move(db, dispatch, "ENTER")
    await apply_outcome(db, move, "SUCCEEDED")

    async def step(n):
        return await dispatch.advance(db, job_id=job, **evidence(job), now_ms=n)

    root = (await step(1)).actions[-1]
    await respond(
        db,
        root.decision_operation_id,
        200,
        "DECIDED",
        dict(result="READY", rack_destination=dict(type="ZONE", location_code="Z")),
    )
    depart = (await step(2)).actions[-1]
    await apply_outcome(db, depart, "FAILED")
    assert (await step(3)).state == "RELEASING"
    await respond(
        db,
        depart.submit_operation_id,
        202,
        "RECEIVED",
        {"transport_task_id": depart.transport_task_id},
    )
    transport = dispatch.capabilities.transport
    original = transport.get_submission_acceptance

    async def mismatched(session, **identity):
        result = await original(session, **identity)
        assert result.state == "ACCEPTED"
        if invalid == "template":
            changes = dict(request_raw=result.basis.request_raw.replace('"F01"', '"CTU01"'))
        elif invalid == "status":
            changes = dict(http_status=200)
        else:
            changes = {invalid: 123456 if invalid == "message_id" else str(uuid7())}
        return replace(result, basis=replace(result.basis, **changes))

    monkeypatch.setattr(transport, "get_submission_acceptance", mismatched)
    view = await step(4)
    assert view.state == "RELEASING" and view.actions[-1].acceptance_basis is None


@pytest.mark.parametrize(
    "kind,managed,state",
    [("ENTER", False, "NOT_ARRIVED"), ("ENTER", True, "ACTIVE"), ("ROTATE", False, "ACTIVE")],
)
async def test_only_ended_entry_at_unmanaged_external_position_ends_reservation(
    rack, kind, managed, state
):
    from app.models import WorkLine

    db, dispatch = rack
    job, move = await setup_move(db, dispatch, kind)
    if managed:
        db.add(WorkLine(line_name="other", line_code="OTHER", position_bindings={"ROLE": "Q"}))
    await apply_outcome(db, move, "CANCELLED", point="Q")
    view = await dispatch.advance(db, job_id=job, **evidence(job), now_ms=1)
    assert view.state == state and view.actions[-1].outcome["status"] == "CANCELLED"
    assert await dispatch.has_position_conflict(db, point="Q") == (managed or kind == "ROTATE")


async def test_inflight_entry_keeps_original_release_intent_and_same_face_can_be_revisited(rack):
    db, dispatch = rack
    job, move = await setup_move(db, dispatch, "ENTER")
    assert (
        await dispatch.prepare(db, job_id=job, kind="ENTER", source_ref="other", face="90")
        == move.id
    )
    assert (
        await dispatch.prepare(db, job_id=job, kind="ENTER", source_ref="other", face="270") is None
    )
    view = await dispatch.advance(db, job_id=job, **evidence(job), now_ms=1)
    assert view.release_requested and view.state == "ACTIVE" and len(view.actions) == 1
    await apply_outcome(db, move, "SUCCEEDED")
    for index, face in enumerate(("270", "90", "270")):
        await dispatch.advance(db, job_id=job, **evidence(job, demand="CONTINUE"), now_ms=2 + index)
        assert await dispatch.prepare(
            db, job_id=job, kind="ROTATE", source_ref=str(index), face=face
        )
        move = (await dispatch.progress(db, job_id=job)).actions[-1]
        await apply_outcome(db, move, "SUCCEEDED")
    view = await dispatch.progress(db, job_id=job)
    assert [a.target_face for a in view.actions] == ["90", "270", "90", "270"]
    assert len({a.id for a in view.actions}) == 4


async def test_pre_candidate_requires_its_original_unused_scope_and_no_move(rack):
    from app.businesses._components.rack_dispatch.contracts import Coverage, FactRef

    db, dispatch = rack
    candidate = replace(
        origin("ROLLER_DRAIN"), resources=None, point=None, caller=None, task_id=None
    )
    job = await dispatch.register(db, candidate)
    source = Coverage(
        "unused_drain_candidate",
        ("owner", "12", "ROLLER_DRAIN", "source-1", "rack"),
        FactRef("drain_decision", ("DRAIN", "source-1"), ("EMPTY", "v1")),
        (),
    )
    assert not await dispatch.withdraw_unused(
        db, job_id=job, source=replace(source, scope=source.scope[:-1])
    )
    assert not await dispatch.withdraw_unused(
        db, job_id=job, source=replace(source, rows=(source.anchor,))
    )
    assert await dispatch.withdraw_unused(db, job_id=job, source=source)
    assert (await dispatch.progress(db, job_id=job)).state == "WITHDRAWN"
    assert (
        await dispatch.prepare(db, job_id=job, kind="ENTER", source_ref="late", face="90") is None
    )


@pytest.mark.parametrize("source", ["MANUAL_RETURN", "ROLLER_DRAIN"])
async def test_wait_retries_only_on_deadline_or_manual_original_status_change(rack, source):
    from app.businesses._components.rack_dispatch.contracts import FactRef

    db, dispatch = rack
    job, move = await setup_move(db, dispatch, "ENTER", source)
    await apply_outcome(db, move, "SUCCEEDED")

    async def step(now, version, status):
        facts = evidence(job, version=version)
        demand = facts["demand"]
        facts["demand"] = replace(
            demand,
            coverage=(
                replace(demand.coverage[0], rows=(FactRef("task_status", ("task",), (status,)),)),
            ),
        )
        return await dispatch.advance(db, job_id=job, **facts, now_ms=now)

    root = (await step(2, "1", "EXECUTING")).actions[-1]
    await respond(
        db, root.decision_operation_id, 200, "DECIDED", {"result": "WAIT", "retry_after_ms": 100}
    )
    assert (await step(9, "1", "EXECUTING")).actions[-1].due_at_ms == 109
    assert (await step(30, "2", "EXECUTING")).actions[-1].id == root.id
    latest = (await step(31, "2", "COMPLETED")).actions[-1]
    if source == "MANUAL_RETURN":
        assert latest.previous_action_id == root.id
        assert (await step(109, "2", "COMPLETED")).actions[-1].id == latest.id
    else:
        assert latest.id == root.id
        assert (await step(109, "2", "COMPLETED")).actions[-1].previous_action_id == root.id


async def test_new_rotate_after_original_result_has_its_own_action_without_an_intervening_scan(
    rack,
):
    db, dispatch = rack
    job, turn = await setup_move(db, dispatch, "ROTATE")
    await apply_outcome(db, turn, "SUCCEEDED")
    new = await dispatch.prepare(db, job_id=job, kind="ROTATE", source_ref="next-round", face="270")
    assert new is not None and new != turn.id


async def test_distinct_owner_and_source_windows_and_one_time_candidate_freeze(rack):
    from app.businesses._components.rack_dispatch.service import RackDispatch

    db, dispatch = rack
    initial = replace(origin("ROLLER_DRAIN"), caller=None, resources=None, point=None, task_id=None)
    job = await dispatch.register(db, initial)
    assert (
        await dispatch.prepare(db, job_id=job, kind="ENTER", source_ref="zero", face="90") is None
    )
    approved = replace(origin("ROLLER_DRAIN"), task_id=None, cycle_id=str(uuid7()))
    assert await dispatch.register(db, approved) == job
    assert await dispatch.register(db, replace(approved, point="ANOTHER")) == job
    assert (await dispatch.progress(db, job_id=job)).origin == replace(approved, cycle_id=job)
    foreign = RackDispatch("other", dispatch.capabilities)
    assert await foreign.progress(db, job_id=job) is None
    assert await foreign.pending_upper_id(db, source_kinds=("ROLLER_DRAIN",)) is None
    assert (
        await foreign.prepare(db, job_id=job, kind="ENTER", source_ref="alien", face="90") is None
    )
    assert (
        await dispatch.pending_ids(
            db, source_kinds=("MANUAL_RETURN",), after_id=None, upper_id=job, limit=1
        )
        == ()
    )
    assert await dispatch.pending_ids(
        db, source_kinds=("ROLLER_DRAIN",), after_id=None, upper_id=job, limit=1
    ) == (job,)
    assert (
        await dispatch.jobs(db, source_kinds=("ROLLER_DRAIN",), work_line_id=12, task_id="other")
        == ()
    )
    assert [
        v.id for v in await dispatch.jobs(db, source_kinds=("ROLLER_DRAIN",), work_line_id=12)
    ] == [job]


async def test_predecessor_projection_for_another_rack_cannot_open_departure(rack, monkeypatch):
    db, dispatch = rack
    job, move = await setup_move(db, dispatch, "ENTER")
    await apply_outcome(db, move, "FAILED")
    transport = dispatch.capabilities.transport
    read = transport.list_unclosed_predecessors

    async def other_rack(session, **scope):
        return replace(await read(session, **scope), rack_id="other")

    monkeypatch.setattr(transport, "list_unclosed_predecessors", other_rack)
    view = await dispatch.advance(db, job_id=job, **evidence(job), now_ms=1)
    assert view.pending and len(view.actions) == 1


@pytest.mark.parametrize("mutation", ["rack", "location", "same_point", "wrong_shape"])
async def test_recovered_ready_must_match_original_origin_before_creating_transport(rack, mutation):
    from sqlalchemy import update

    from app.businesses._components.rack_dispatch.models import RackAction

    db, dispatch = rack
    job, move = await setup_move(db, dispatch, "ENTER")
    await apply_outcome(db, move, "SUCCEEDED")
    root = (await dispatch.advance(db, job_id=job, **evidence(job), now_ms=1)).actions[-1]
    request = dict(root.request)
    ready = {"result": "READY", "rack_destination": {"type": "ZONE", "location_code": "Z"}}
    if mutation == "rack":
        request["rack_id"] = "other"
    if mutation == "location":
        request["current_location"] = {"type": "RACK_POSITION", "location_code": "other"}
    if mutation == "same_point":
        ready["rack_destination"] = request["current_location"]
    if mutation == "wrong_shape":
        ready["extra"] = True
    await db.execute(
        update(RackAction)
        .where(RackAction.id == root.id)
        .values(request=request, decision=ready, phase="READY")
    )
    view = await dispatch.advance(db, job_id=job, **evidence(job), now_ms=2)
    assert view.pending and len(view.actions) == 2


@pytest.mark.parametrize("kind", ["ENTER", "ROTATE"])
async def test_submit_ack_alone_cannot_finish_production_preparation(rack, kind):
    db, dispatch = rack
    job, move = await setup_move(db, dispatch, kind)
    await respond(
        db, move.submit_operation_id, 202, "RECEIVED", {"transport_task_id": move.transport_task_id}
    )
    view = await dispatch.advance(db, job_id=job, **evidence(job), now_ms=1)
    assert view.state == "ACTIVE" and view.actions[-1].id == move.id
    assert view.actions[-1].phase == "REGISTERED" and view.actions[-1].outcome is None


async def test_later_applied_revision_resolves_original_execution_without_new_entry(
    rack, monkeypatch
):
    db, dispatch = rack
    job, move = await setup_move(db, dispatch, "ENTER")
    await apply_outcome(db, move, "FAILED")
    transport = dispatch.capabilities.transport
    read = transport.get_result

    async def read_with_policy(session, **identity):
        view = await read(session, **identity)
        if view.outcome_revision == 1:
            return replace(view, closure="OPEN")
        return view

    monkeypatch.setattr(transport, "get_result", read_with_policy)
    first = await dispatch.advance(db, job_id=job, **evidence(job), now_ms=1)
    assert first.actions[-1].outcome["status"] == "FAILED" and first.state == "ACTIVE"
    # Exercise the public port contract for an applied correction under a future eligible policy.
    await apply_outcome(db, move, "SUCCEEDED", revision=2)
    next_view = await dispatch.advance(db, job_id=job, **evidence(job), now_ms=2)
    assert [(a.kind, a.outcome_revision) for a in next_view.actions] == [
        ("ENTER", 2),
        ("DEPARTURE_DECIDE", None),
    ]
    assert next_view.actions[0].id == move.id


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
async def test_rejected_prepare_keeps_cross_owner_managed_point_protected(backend, request):
    from app.businesses._components.rack_dispatch.service import RackDispatch
    from app.models import WorkLine

    url = request.getfixturevalue("pg_url") if backend == "postgres" else "sqlite+aiosqlite://"
    async with store(url) as (factory, dispatch):
        async with factory.begin() as db:
            job, move = await setup_move(db, dispatch, "ROTATE")
            db.add(WorkLine(line_name="other", line_code="OTHER", position_bindings={"ROLE": "Q"}))
            await apply_outcome(db, move, "CANCELLED", point="Q")
            await dispatch.advance(db, job_id=job, **evidence(job, demand="CONTINUE"), now_ms=1)
            assert await dispatch.has_position_conflict(db, point="Q")
        async with factory.begin() as db:
            assert (
                await dispatch.prepare(db, job_id=job, kind="ROTATE", source_ref="new", face="90")
                is None
            )
        async with factory.begin() as db:
            observer = RackDispatch("other", dispatch.capabilities)
            assert await observer.has_position_conflict(db, point="Q")


@pytest.mark.parametrize(
    "mutation",
    [
        "extra_decision",
        "extra_request",
        "missing_message",
        "wrong_operation",
        "missing_operation_id",
    ],
)
@pytest.mark.parametrize("rack", ["sqlite", "postgres"], indirect=True)
async def test_ack_cannot_close_depart_with_invalid_recovered_ready(rack, mutation):
    from sqlalchemy import update

    from app.businesses._components.rack_dispatch.models import RackAction

    db, dispatch = rack
    job, move = await setup_move(db, dispatch, "ENTER")
    await apply_outcome(db, move, "SUCCEEDED")

    async def step(n):
        return await dispatch.advance(db, job_id=job, **evidence(job), now_ms=n)

    root = (await step(1)).actions[-1]
    decision = {"result": "READY", "rack_destination": {"type": "ZONE", "location_code": "Z"}}
    await respond(db, root.decision_operation_id, 200, "DECIDED", decision)
    depart = (await step(2)).actions[-1]
    assert depart.kind == "DEPART"
    changes = {
        "extra_decision": {"decision": {**decision, "extra": True}},
        "extra_request": {"request": {**root.request, "extra": True}},
        "missing_message": {"message_id": None},
        "wrong_operation": {"operation": "other"},
        "missing_operation_id": {"decision_operation_id": None},
    }[mutation]
    await db.execute(update(RackAction).where(RackAction.id == root.id).values(**changes))
    await respond(
        db,
        depart.submit_operation_id,
        202,
        "RECEIVED",
        {"transport_task_id": depart.transport_task_id},
    )
    view = await step(3)
    assert view.pending and view.state == "RELEASING" and view.actions[-1].acceptance_basis is None


@pytest.mark.parametrize(
    "decision",
    [
        {"result": "WAIT", "retry_after_ms": 10},
        {"result": "READY", "rack_destination": {"type": "ZONE", "location_code": "Z"}},
    ],
)
@pytest.mark.parametrize("barrier", ["BLOCKED", "UNKNOWN", "unresolved"])
@pytest.mark.parametrize("rack", ["sqlite", "postgres"], indirect=True)
async def test_blocked_departure_consumes_first_decision_before_successor_permission(
    rack, decision, barrier, monkeypatch
):
    db, dispatch = rack
    job, move = await setup_move(db, dispatch, "ENTER")
    await apply_outcome(db, move, "SUCCEEDED")
    root = (await dispatch.advance(db, job_id=job, **evidence(job), now_ms=1)).actions[-1]
    await respond(db, root.decision_operation_id, 200, "DECIDED", decision)
    transport = dispatch.capabilities.transport
    read = transport.get_result
    if barrier == "unresolved":

        async def unresolved(session, **identity):
            return replace(await read(session, **identity), closure="OPEN")

        monkeypatch.setattr(transport, "get_result", unresolved)
    facts = evidence(job, handoff="CLEAR" if barrier == "unresolved" else barrier)
    for now in (2, 15):
        view = await dispatch.advance(db, job_id=job, **facts, now_ms=now)
        assert len(view.actions) == 2
        consumed = view.actions[-1]
        assert consumed.decision == decision and consumed.decision_consumed_at_ms == 2
        assert consumed.due_at_ms == (12 if decision["result"] == "WAIT" else None)
    monkeypatch.setattr(transport, "get_result", read)
    final = await dispatch.advance(db, job_id=job, **evidence(job), now_ms=16)
    assert len(final.actions) == 3 and final.actions[-1].previous_action_id == root.id
    assert final.actions[-1].kind == (
        "DEPARTURE_DECIDE" if decision["result"] == "WAIT" else "DEPART"
    )


@pytest.mark.parametrize("rack", ["sqlite", "postgres"], indirect=True)
async def test_jobs_filter_original_source_and_include_terminal_candidates(rack):
    db, dispatch = rack
    base = origin("ROLLER_DRAIN")
    own = await dispatch.register(db, base)
    other = await dispatch.register(
        db, replace(base, source_ref="other-decision", cycle_id=str(uuid7()))
    )
    active = await dispatch.register(
        db, replace(base, rack_id="second-rack", cycle_id=str(uuid7()))
    )
    await dispatch.advance(db, job_id=own, **evidence(own), now_ms=1)
    from sqlalchemy import event

    from app.businesses._components.rack_dispatch.models import RackJob

    await db.flush()
    db.expunge_all()
    loaded = []

    def observe(session, record):
        if isinstance(record, RackJob):
            loaded.append(record.id)

    event.listen(db.sync_session, "loaded_as_persistent", observe)
    try:
        selected = await dispatch.jobs(
            db, work_line_id=12, source_kinds=("ROLLER_DRAIN",), source_ref=base.source_ref
        )
    finally:
        event.remove(db.sync_session, "loaded_as_persistent", observe)
    assert set(loaded) == {own, active}
    assert {(j.id, j.pending) for j in selected} == {(own, False), (active, True)}
    assert {
        j.id for j in await dispatch.jobs(db, work_line_id=12, source_kinds=("ROLLER_DRAIN",))
    } == {own, active, other}


@pytest.mark.parametrize("already_registered", [False, True])
async def test_missing_binding_without_proven_cutover_absence_denies_first_registration(
    rack, already_registered
):
    from sqlalchemy import func, select

    from app.businesses._components.rack_dispatch.models import RackAction
    from app.models import ReliableMessage, TransportTask

    db, dispatch = rack
    job = await dispatch.register(db, origin("ROLLER_ORDINARY"))
    client = str(uuid7())
    if already_registered:
        await dispatch.prepare(db, job_id=job, kind="ENTER", source_ref="entry", face="90")
        action = (await dispatch.progress(db, job_id=job)).actions[-1]
        client = action.client_request_id
        old = await db.get(RackAction, action.id)
        old.transport_task_id = old.submit_operation_id = old.submit_message_id = (
            old.submit_target
        ) = None
    else:
        old = RackAction(
            id="original",
            job_id=job,
            owner_domain="owner",
            work_line_id=12,
            ordinal=1,
            kind="ENTER",
            source_ref="entry",
            phase="REGISTERED",
            caller={"workline_id": "caller", "station_id": "station"},
            source={"kind": "RACK", "location_code": "rack"},
            target={"kind": "RACK_POSITION", "location_code": "P"},
            target_face="90",
            template="CTU01",
            completion_rule="APPLIED_RESULT",
            client_request_id=client,
        )
        db.add(old)
    old.basis = {
        "legacy_table": "bin_roller_rack_action",
        "legacy_id": old.id,
        "legacy_state": "PENDING",
        "legacy_pending": True,
    }
    await db.flush()
    for _ in range(2):
        progress = await dispatch.advance(
            db, job_id=job, **evidence(job, demand="CONTINUE"), now_ms=10
        )
    (retained,) = progress.actions
    assert progress.pending and progress.review_reason == "ORIGINAL_REGISTRATION_UNPROVED"
    assert retained.transport_task_id is None and retained.client_request_id == client
    assert await db.scalar(select(func.count()).select_from(TransportTask)) == int(
        already_registered
    )
    assert await db.scalar(select(func.count()).select_from(ReliableMessage)) == int(
        already_registered
    )
