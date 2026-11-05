from dataclasses import replace

import pytest

from . import apply_outcome, evidence, origin, respond, store

SOURCES = ("AUTO_INBOUND_SOURCE", "AUTO_OUTBOUND_RETURN", "AUTO_OUTBOUND_TARGET")


@pytest.fixture
async def auto_rack():
    async with store("sqlite+aiosqlite://", owner="smt_auto") as (factory, dispatch):
        async with factory.begin() as session:
            yield session, dispatch


def auto_origin(source):
    return replace(origin(source), initial_face=None if source == SOURCES[0] else "90")


def facts(job, **kwargs):
    value = evidence(job, **kwargs)
    return {
        key: replace(
            fact,
            point=replace(fact.point, owner_domain="smt_auto"),
            coverage=tuple(replace(c, scope=("smt_auto", *c.scope[1:])) for c in fact.coverage),
        )
        for key, fact in value.items()
    }


@pytest.mark.parametrize("source", SOURCES)
@pytest.mark.parametrize("http,code", [(202, "RECEIVED"), (200, "DUPLICATE")])
@pytest.mark.parametrize("destination", ["ZONE", "RACK_POSITION"])
async def test_auto_original_moves_and_departure_acceptance(
    auto_rack, source, http, code, destination
):
    db, dispatch = auto_rack
    original = auto_origin(source)
    job = await dispatch.register(db, original)
    assert await dispatch.register(db, replace(original, cycle_id=origin().cycle_id)) == job
    entry = await dispatch.prepare(
        db, job_id=job, kind="ENTER", source_ref="entry", face=original.initial_face
    )
    view = await dispatch.progress(db, job_id=job)
    assert entry and view.actions[-1].template == "F01"
    assert (await dispatch.advance(db, job_id=job, **facts(job), now_ms=1)).pending
    assert len((await dispatch.progress(db, job_id=job)).actions) == 1
    await apply_outcome(db, view.actions[-1], "SUCCEEDED")
    await dispatch.advance(db, job_id=job, **facts(job, demand="CONTINUE"), now_ms=2)
    rotation = await dispatch.prepare(db, job_id=job, kind="ROTATE", source_ref="turn", face="270")
    view = await dispatch.progress(db, job_id=job)
    if source == SOURCES[0]:
        assert rotation is None and len(view.actions) == 1
    else:
        assert rotation and view.actions[-1].template == "CK04"
        await apply_outcome(db, view.actions[-1], "SUCCEEDED")
    root = (await dispatch.advance(db, job_id=job, **facts(job), now_ms=3)).actions[-1]
    await respond(
        db,
        root.decision_operation_id,
        200,
        "DECIDED",
        {"result": "READY", "rack_destination": {"type": destination, "location_code": "Z"}},
    )
    depart = (await dispatch.advance(db, job_id=job, **facts(job), now_ms=4)).actions[-1]
    assert (depart.kind, depart.template) == ("DEPART", "F01")
    assert (await dispatch.advance(db, job_id=job, **facts(job), now_ms=5)).pending
    await respond(
        db,
        depart.submit_operation_id,
        http,
        code,
        {"transport_task_id": depart.transport_task_id},
    )
    assert (await dispatch.advance(db, job_id=job, **facts(job), now_ms=6)).state == "COMPLETED"


@pytest.mark.parametrize("source", SOURCES)
@pytest.mark.parametrize(
    "field,value",
    [
        ("task_id", None),
        ("point", None),
        ("workline_code", None),
        ("caller", None),
        ("resources", {}),
        ("initial_face", "too-long-face"),
    ],
)
async def test_auto_incomplete_origin_creates_no_job(auto_rack, source, field, value):
    db, dispatch = auto_rack
    with pytest.raises(ValueError):
        await dispatch.register(db, replace(auto_origin(source), **{field: value}))
    assert await dispatch.pending_upper_id(db, source_kinds=SOURCES) is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("task_id", "different"),
        ("point", "other"),
        ("workline_code", "other"),
        ("caller", None),
        ("resources", {"role": "changed"}),
        ("initial_face", "270"),
    ],
)
async def test_auto_original_origin_cannot_drift(auto_rack, field, value):
    db, dispatch = auto_rack
    original = auto_origin(SOURCES[1])
    job = await dispatch.register(db, original)
    with pytest.raises(ValueError):
        await dispatch.register(db, replace(original, **{field: value}))
    assert (await dispatch.progress(db, job_id=job)).origin == original


async def test_auto_rejects_unknown_source_and_wrong_owner():
    async with store("sqlite+aiosqlite://") as (factory, dispatch), factory.begin() as db:
        for source in (*SOURCES, "AUTO_OTHER"):
            with pytest.raises(ValueError):
                await dispatch.register(db, auto_origin(source))
        assert await dispatch.pending_upper_id(db, source_kinds=SOURCES) is None


@pytest.mark.parametrize("source", SOURCES)
async def test_auto_wrong_point_face_and_source_ref_create_no_new_intent(auto_rack, source):
    db, dispatch = auto_rack
    original = auto_origin(source)
    wrong = await dispatch.register(db, replace(original, point="unknown"))
    assert (
        await dispatch.prepare(
            db, job_id=wrong, kind="ENTER", source_ref="entry", face=original.initial_face
        )
        is None
    )
    job = await dispatch.register(
        db, replace(original, source_ref="valid", cycle_id=origin().cycle_id)
    )
    assert (
        await dispatch.prepare(db, job_id=job, kind="ENTER", source_ref="entry", face="270") is None
    )
    entry = await dispatch.prepare(
        db, job_id=job, kind="ENTER", source_ref="entry", face=original.initial_face
    )
    await dispatch.advance(db, job_id=job, **facts(job), now_ms=1)
    assert (
        await dispatch.prepare(
            db, job_id=job, kind="ENTER", source_ref="drifted", face=original.initial_face
        )
        is None
    )
    assert (
        await dispatch.prepare(
            db, job_id=job, kind="ENTER", source_ref="entry", face=original.initial_face
        )
        == entry
    )
    assert len((await dispatch.progress(db, job_id=job)).actions) == 1


@pytest.mark.parametrize("source", SOURCES)
@pytest.mark.parametrize(
    "condition", ["unknown", "wrong_owner", "wrong_point", "template", "cycle"]
)
async def test_auto_unconfirmed_original_evidence_cannot_advance(
    auto_rack, monkeypatch, source, condition
):
    db, dispatch = auto_rack
    original = auto_origin(source)
    job = await dispatch.register(db, original)
    await dispatch.prepare(
        db, job_id=job, kind="ENTER", source_ref="entry", face=original.initial_face
    )
    entry = (await dispatch.progress(db, job_id=job)).actions[-1]
    await apply_outcome(
        db, entry, "SUCCEEDED", point="wrong" if condition == "wrong_point" else "P"
    )
    if condition in ("template", "cycle"):
        read = dispatch.capabilities.transport.get_result

        async def mismatched(session, **identity):
            result = await read(session, **identity)
            return replace(
                result,
                **(
                    {"frozen": {**result.frozen, "rcs_template_id": "CTU01"}}
                    if condition == "template"
                    else {"cycle_id": "wrong"}
                ),
            )

        monkeypatch.setattr(dispatch.capabilities.transport, "get_result", mismatched)
    inputs = facts(job, demand="UNKNOWN" if condition == "unknown" else "NONE")
    if condition == "wrong_owner":
        inputs["demand"] = replace(
            inputs["demand"], point=replace(inputs["demand"].point, owner_domain="other")
        )
    view = await dispatch.advance(db, job_id=job, **inputs, now_ms=1)
    assert view.pending and len(view.actions) == 1


@pytest.mark.parametrize("source", SOURCES)
async def test_auto_wait_uses_fixed_due_time(auto_rack, source):
    db, dispatch = auto_rack
    original = auto_origin(source)
    job = await dispatch.register(db, original)
    await dispatch.prepare(
        db, job_id=job, kind="ENTER", source_ref="entry", face=original.initial_face
    )
    await apply_outcome(db, (await dispatch.progress(db, job_id=job)).actions[-1], "SUCCEEDED")
    root = (await dispatch.advance(db, job_id=job, **facts(job), now_ms=1)).actions[-1]
    await respond(
        db, root.decision_operation_id, 200, "DECIDED", {"result": "WAIT", "retry_after_ms": 100}
    )
    wait = await dispatch.advance(db, job_id=job, **facts(job), now_ms=2)
    await dispatch.note_facts_changed(db, job_ids=(job,))
    before = await dispatch.advance(db, job_id=job, **facts(job, version="2"), now_ms=101)
    assert len(before.actions) == len(wait.actions)
    due = await dispatch.advance(db, job_id=job, **facts(job, version="2"), now_ms=102)
    assert len(due.actions) == len(wait.actions) + 1


async def test_auto_prepare_rollback_leaves_no_actions_or_transport_intents(auto_rack):
    from sqlalchemy import func, select

    from app.models import ReliableMessage, TransportTask

    db, dispatch = auto_rack
    original = auto_origin(SOURCES[1])
    job = await dispatch.register(db, original)
    with pytest.raises(RuntimeError):
        async with db.begin_nested():
            assert await dispatch.prepare(
                db, job_id=job, kind="ENTER", source_ref="entry", face="90"
            )
            raise RuntimeError("abort host transaction")
    assert not (await dispatch.progress(db, job_id=job)).actions
    assert await db.scalar(select(func.count()).select_from(TransportTask)) == 0
    assert await db.scalar(select(func.count()).select_from(ReliableMessage)) == 0


@pytest.mark.parametrize("source", SOURCES)
@pytest.mark.parametrize(
    "invalid", ["template", "owner_domain", "cycle_id", "operation_id", "message_id"]
)
async def test_auto_departure_requires_exact_original_acceptance(
    auto_rack, monkeypatch, source, invalid
):
    db, dispatch = auto_rack
    original = auto_origin(source)
    job = await dispatch.register(db, original)
    await dispatch.prepare(
        db, job_id=job, kind="ENTER", source_ref="entry", face=original.initial_face
    )
    await apply_outcome(db, (await dispatch.progress(db, job_id=job)).actions[-1], "SUCCEEDED")
    root = (await dispatch.advance(db, job_id=job, **facts(job), now_ms=1)).actions[-1]
    await respond(
        db,
        root.decision_operation_id,
        200,
        "DECIDED",
        {"result": "READY", "rack_destination": {"type": "ZONE", "location_code": "Z"}},
    )
    depart = (await dispatch.advance(db, job_id=job, **facts(job), now_ms=2)).actions[-1]
    await respond(
        db,
        depart.submit_operation_id,
        202,
        "RECEIVED",
        {"transport_task_id": depart.transport_task_id},
    )
    read = dispatch.capabilities.transport.get_submission_acceptance

    async def mismatched(session, **identity):
        result = await read(session, **identity)
        changes = (
            {"request_raw": result.basis.request_raw.replace('"F01"', '"CTU03"')}
            if invalid == "template"
            else {invalid: "wrong"}
        )
        return replace(result, basis=replace(result.basis, **changes))

    monkeypatch.setattr(dispatch.capabilities.transport, "get_submission_acceptance", mismatched)
    view = await dispatch.advance(db, job_id=job, **facts(job), now_ms=3)
    assert view.state == "RELEASING" and view.pending and len(view.actions) == 3


@pytest.mark.parametrize("source", SOURCES[1:])
@pytest.mark.parametrize("kind", ["ENTER", "ROTATE"])
async def test_auto_preparation_requires_exact_arrival_face(auto_rack, source, kind):
    db, dispatch = auto_rack
    original = replace(auto_origin(source), initial_face="A")
    job = await dispatch.register(db, original)
    await dispatch.prepare(db, job_id=job, kind="ENTER", source_ref="entry", face="A")
    action = (await dispatch.progress(db, job_id=job)).actions[-1]
    if kind == "ROTATE":
        await apply_outcome(db, action, "SUCCEEDED")
        await dispatch.advance(db, job_id=job, **facts(job, demand="CONTINUE"), now_ms=1)
        assert await dispatch.prepare(db, job_id=job, kind="ROTATE", source_ref="turn", face="B")
        action = (await dispatch.progress(db, job_id=job)).actions[-1]
    await apply_outcome(db, replace(action, target_face="other"), "SUCCEEDED")
    view = await dispatch.advance(db, job_id=job, **facts(job), now_ms=2)
    assert view.pending and view.review_reason in (
        "POSITION_CONFLICT",
        "ORIGINAL_RESULT_UNAVAILABLE",
    )
    assert all(a.kind != "DEPARTURE_DECIDE" for a in view.actions)
