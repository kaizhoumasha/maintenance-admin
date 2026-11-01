import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from sqlalchemy import Column, Integer, MetaData, String, Table, insert, select

from app.businesses import catalog
from app.businesses._components.sorting_task.contracts import PlanDelta
from app.businesses.runtime import BusinessRuntime, bind_callback_business
from app.core.identifiers import uuid7
from app.core.strict_json import canonical_digest
from app.integrations.callback_receipts import find_receipt
from app.integrations.registration import RegistrationSource
from app.integrations.wms_callback import (
    Accepted,
    CallbackErrors,
    CallbackResponse,
    Conflict,
    Rejected,
    Unavailable,
    callback_operation,
    receive,
    registry,
)
from app.main import app
from app.models import CallbackReceipt

ERRORS = CallbackErrors("INVALID_DATA", "INVALID_ENVELOPE", "IDEMPOTENCY_CONFLICT")
OPERATION = "isolated.plan@v1"


@pytest.fixture
async def extension(callback_db, monkeypatch):
    metadata = MetaData()
    table = Table(
        "test_sorting_callback_facts",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("kind", String(40)),
    )
    try:
        async with callback_db.kw["bind"].begin() as connection:
            await connection.run_sync(metadata.create_all)
        monkeypatch.setattr(registry, "_registry", {})
        source = RegistrationSource(
            "app.businesses._components.isolated.callback.isolated_plan_v1",
            "isolated_plan_v1.py",
            "accept",
            False,
        )
        monkeypatch.setattr(
            catalog,
            "input_target",
            lambda code, key: (
                SimpleNamespace(source=source, component_key="isolated")
                if code == "original" and key == ("wms_callback", OPERATION, None)
                else None
            ),
        )
        runtime = BusinessRuntime({}, {"original": object()})
        state = SimpleNamespace(
            ready=False,
            bound=True,
            outcome=None,
            rechecks=0,
            applied=0,
            guard=None,
            pause=None,
            entered=None,
            release=None,
        )

        async def record(ctx, kind):
            await ctx.session.execute(insert(table).values(kind=kind))

        async def accept(ctx, payload):
            if state.bound:
                bind_callback_business(ctx, "original")
            if state.outcome is not None:
                await record(ctx, "attempt")
                if getattr(state, "raise_outcome", False):
                    raise state.outcome
                return state.outcome
            if not state.ready:
                await record(ctx, "pending")
                return Unavailable()
            await record(ctx, "member")
            await record(ctx, "intent")
            state.applied += 1
            return Accepted()

        async def recheck(ctx, payload, original_response):
            bind_callback_business(ctx, "original")
            state.rechecks += 1
            assert original_response["code"] in {"UNAVAILABLE", "CONFLICT", "REJECTED"}
            with pytest.raises(TypeError):
                original_response["code"] = "tampered"
            with pytest.raises(TypeError):
                original_response["data"]["tampered"] = True
            if state.entered:
                state.entered.set()
                await state.release.wait()
            if not state.ready:
                return None
            await record(ctx, "member")
            await record(ctx, "intent")
            state.applied += 1
            return Accepted()

        async def guard(ctx, payload):
            if isinstance(state.guard, Exception):
                raise state.guard
            return state.guard

        def register(*, rechecking=True, guarding=False, errors=ERRORS):
            registry._registry.clear()
            callback_operation(
                OPERATION,
                data=PlanDelta,
                errors=errors,
                recheck=recheck if rechecking else None,
                archive_guard=guard if guarding else None,
            )(accept)
            registry._registry[OPERATION] = replace(
                registry._registry[OPERATION], registration_source=source
            )

        events = []

        async def send(body, session=None):
            options = dict(runtime=runtime, on_business_committed=events.append)
            if session is not None:
                return await receive(session, json.dumps(body).encode(), **options)
            async with callback_db() as own, own.begin():
                return await receive(own, json.dumps(body).encode(), **options)

        async def kinds():
            async with callback_db() as session:
                return list(await session.scalars(select(table.c.kind).order_by(table.c.id)))

        body = {
            "operation": OPERATION,
            "operation_id": str(uuid7()),
            "timestamp": 17,
            "data": {
                "task_id": "T",
                "plan_revision": 1,
                "added_bin_source_racks": [{"rack_id": "B", "rack_face": ["A", "B"]}],
            },
        }
        register()
        yield SimpleNamespace(
            factory=callback_db,
            runtime=runtime,
            state=state,
            table=table,
            body=body,
            register=register,
            send=send,
            kinds=kinds,
            events=events,
        )
    finally:
        async with callback_db.kw["bind"].begin() as connection:
            await connection.run_sync(metadata.drop_all)


@pytest.mark.parametrize(
    "original",
    [Unavailable(), Conflict({"reason_code": "REVISION_CONFLICT"}), Rejected("WORKLINE_DISABLED")],
)
async def test_plan_recheck_requires_original_retry(extension, original):
    ext = extension
    ext.state.outcome = original
    first = await ext.send(ext.body)
    async with ext.factory() as session:
        saved = await find_receipt(session, OPERATION, ext.body["operation_id"])
        original_identity = (saved.id, saved.created_at, saved.raw_digest, saved.effective_digest)
    ext.state.outcome = None
    still = await ext.send(ext.body)
    assert still.body == first.body
    assert ext.state.applied == 0
    ext.state.ready = True
    assert "member" not in await ext.kinds()
    applied = await ext.send(ext.body)
    assert (applied.status_code, applied.body["code"], applied.body["data"]) == (
        202,
        "RECEIVED",
        {},
    )
    duplicate = await ext.send(ext.body)
    assert duplicate.status_code == 200 and duplicate.body == applied.body | {"code": "DUPLICATE"}
    assert ext.state.rechecks == 2 and ext.state.applied == 1
    async with ext.factory() as session:
        saved = await find_receipt(session, OPERATION, ext.body["operation_id"])
        assert (
            saved.id,
            saved.created_at,
            saved.raw_digest,
            saved.effective_digest,
        ) == original_identity
    assert ext.events == ["original"]


async def test_unavailable_keeps_only_pending_input(extension):
    ext = extension
    first = await ext.send(ext.body)
    assert first.status_code == 503 and first.body["data"] == {}
    assert await ext.kinds() == ["pending"]
    assert ext.events == []
    assert (await ext.send(ext.body)).body == first.body


@pytest.mark.parametrize("rechecking,bound", [(False, True), (True, False)])
async def test_unavailable_without_reliable_recheck_rolls_back_only_current_attempt(
    extension, rechecking, bound
):
    ext = extension
    ext.register(rechecking=rechecking)
    ext.state.bound = bound
    async with ext.factory() as session, session.begin():
        await session.execute(insert(ext.table).values(kind="outer"))
        response = await ext.send(ext.body, session)
        assert response.status_code == 503
    assert await ext.kinds() == ["outer"]
    async with ext.factory() as session:
        assert await find_receipt(session, OPERATION, ext.body["operation_id"]) is None
    ext.state.bound = True
    ext.state.ready = True
    assert (await ext.send(ext.body)).status_code == 202
    assert (await ext.send(ext.body)).status_code == 200
    assert ext.events == ["original"]


@pytest.mark.parametrize(
    "change,reason",
    [
        ({"timestamp": None}, "INVALID_ENVELOPE"),
        ({"data": {"task_id": "T", "plan_revision": False}}, "INVALID_DATA"),
    ],
)
async def test_new_task_error_contract_keeps_legacy_defaults(extension, change, reason):
    ext = extension
    response = await ext.send(ext.body | change)
    assert response.status_code == 422 and response.body["data"] == {"reason_code": reason}
    ext.register(errors=None)
    old = ext.body | {"operation_id": str(uuid7()), **change}
    response = await ext.send(old)
    assert response.body["data"] == {"reason_code": "INVALID_EVIDENCE"}


async def test_identity_conflict_precedes_opt_in_archive_guard(extension):
    ext = extension
    ext.state.ready = True
    first = await ext.send(ext.body)
    ext.register(guarding=True)
    ext.state.guard = CallbackResponse(
        409,
        {
            "operation_id": ext.body["operation_id"],
            "code": "CONFLICT",
            "timestamp": 123,
            "data": {"reason_code": "STATE_CONFLICT"},
        },
        "conflict",
    )
    conflict = await ext.send(ext.body | {"timestamp": 18})
    assert conflict.status_code == 409 and conflict.body["data"] == {
        "reason_code": "IDEMPOTENCY_CONFLICT"
    }
    assert (await ext.send(ext.body)).body == ext.state.guard.body
    async with ext.factory() as session:
        assert (await find_receipt(session, OPERATION, ext.body["operation_id"])).body == first.body
    ext.register(guarding=False)
    assert (await ext.send(ext.body)).body == first.body | {"code": "DUPLICATE"}


async def test_storage_failure_rolls_back_all_facts(extension):
    ext = extension

    async def broken(ctx, payload):
        bind_callback_business(ctx, "original")
        await ctx.session.execute(insert(ext.table).values(kind="pending"))
        raise RuntimeError("storage failed")

    registry._registry[OPERATION] = replace(registry._registry[OPERATION], func=broken)
    with pytest.raises(RuntimeError, match="storage failed"):
        await ext.send(ext.body)
    assert await ext.kinds() == []
    async with ext.factory() as session:
        assert await find_receipt(session, OPERATION, ext.body["operation_id"]) is None
    assert ext.events == []


async def test_frozen_v1_effective_content_survives_unknown_notes(extension):
    ext = extension
    ext.state.ready = True
    first = await ext.send(ext.body)
    equivalent = ext.body | {
        "note": "envelope",
        "data": ext.body["data"] | {"future_note": "unknown"},
    }
    assert (await ext.send(equivalent)).body == first.body | {"code": "DUPLICATE"}
    for changed in [
        ext.body | {"timestamp": 18},
        ext.body | {"data": ext.body["data"] | {"added_source_racks": [{"rack_id": "S"}]}},
        ext.body
        | {
            "data": ext.body["data"]
            | {"added_bin_source_racks": [{"rack_id": "B", "rack_face": ["B", "A"]}]}
        },
    ]:
        assert (await ext.send(changed)).body["data"] == {"reason_code": "IDEMPOTENCY_CONFLICT"}


async def test_recheck_success_waits_for_outer_commit_and_rollback_keeps_pending(extension):
    ext = extension
    first = await ext.send(ext.body)
    ext.state.ready = True
    async with ext.factory() as session:
        async with session.begin():
            assert (await ext.send(ext.body, session)).status_code == 202
            assert ext.events == []
            await session.rollback()
    assert await ext.kinds() == ["pending"]
    async with ext.factory() as session:
        assert (await find_receipt(session, OPERATION, ext.body["operation_id"])).body == first.body
    assert ext.events == []
    assert (await ext.send(ext.body)).status_code == 202
    assert ext.events == ["original"]


async def test_recheck_serializes_original_receipt(extension, wait_for_lock):
    ext = extension
    if ext.factory.kw["bind"].dialect.name != "postgresql":
        pytest.skip("requires PostgreSQL row locks")
    await ext.send(ext.body)
    ext.state.ready = True
    ext.state.entered, ext.state.release = asyncio.Event(), asyncio.Event()
    first = asyncio.create_task(ext.send(ext.body))
    await asyncio.wait_for(ext.state.entered.wait(), 5)
    second = asyncio.create_task(ext.send(ext.body))
    try:
        await wait_for_lock(second)
        assert not second.done()
    finally:
        ext.state.release.set()
        results = await asyncio.gather(first, second)
    assert sorted(result.status_code for result in results) == [200, 202]
    assert ext.state.applied == ext.state.rechecks == 1
    assert (await ext.kinds()).count("member") == 1
    assert (await ext.kinds()).count("intent") == 1
    assert ext.events == ["original"]


@pytest.mark.parametrize(
    "original", [Unavailable(), Conflict({"reason_code": "REVISION_CONFLICT"})]
)
async def test_recheck_success_never_regresses_after_preloaded_failure(
    extension, wait_for_lock, original
):
    ext = extension
    if ext.factory.kw["bind"].dialect.name != "postgresql":
        pytest.skip("requires PostgreSQL row locks")
    ext.state.outcome = original
    await ext.send(ext.body)
    ext.state.outcome = None
    async with ext.factory() as stale:
        cached = await find_receipt(stale, OPERATION, ext.body["operation_id"])
        assert cached.status_code in {503, 409}
        ext.state.ready = True
        ext.state.entered, ext.state.release = asyncio.Event(), asyncio.Event()
        first = asyncio.create_task(ext.send(ext.body))
        await asyncio.wait_for(ext.state.entered.wait(), 5)

        async def late():
            response = await ext.send(ext.body, stale)
            await stale.commit()
            return response

        second = asyncio.create_task(late())
        try:
            await wait_for_lock(second)
            assert not second.done()
        finally:
            ext.state.release.set()
            results = await asyncio.gather(first, second)
    assert [result.status_code for result in results] == [202, 200]
    assert ext.state.applied == ext.state.rechecks == 1
    assert ext.events == ["original"]


async def test_recheck_lock_timeout_preserves_original_receipt(
    extension, wait_for_lock, client, monkeypatch
):
    ext = extension
    if ext.factory.kw["bind"].dialect.name != "postgresql":
        pytest.skip("requires PostgreSQL row locks")
    first = await ext.send(ext.body)
    ext.state.ready = True
    monkeypatch.setattr(app.state, "business_runtime", ext.runtime, raising=False)
    monkeypatch.setattr(app.state, "business_committed", ext.events.append, raising=False)
    async with ext.factory() as blocker, blocker.begin():
        await blocker.scalar(
            select(CallbackReceipt)
            .where(CallbackReceipt.operation_id == ext.body["operation_id"])
            .with_for_update()
        )
        pending = asyncio.create_task(client.post("/api/v1/wms/events", json=ext.body))
        await wait_for_lock(pending)
        response = await pending
        assert response.status_code == 503
        assert response.json()["code"] == "UNAVAILABLE"
        assert response.json()["data"] == {}
    assert ext.events == [] and await ext.kinds() == ["pending"]
    async with ext.factory() as session:
        saved = await find_receipt(session, OPERATION, ext.body["operation_id"])
        assert saved.body == first.body
    assert (await ext.send(ext.body)).status_code == 202


@pytest.mark.parametrize("raise_outcome", [False, True])
async def test_unavailable_without_optional_metadata_preserves_outer_facts(
    extension, raise_outcome
):
    ext = extension
    ext.register(rechecking=False, errors=None)
    ext.state.outcome = Unavailable()
    ext.state.raise_outcome = raise_outcome
    async with ext.factory() as session, session.begin():
        await session.execute(insert(ext.table).values(kind="outer"))
        response = await ext.send(ext.body, session)
        assert response.status_code == 503 and response.body["data"] == {}
    assert await ext.kinds() == ["outer"]
    async with ext.factory() as session:
        assert await find_receipt(session, OPERATION, ext.body["operation_id"]) is None
    assert ext.events == []


async def test_first_archive_rejection_never_becomes_success_receipt(extension):
    ext = extension
    ext.register(guarding=True)
    ext.state.guard = CallbackResponse(
        409,
        {
            "operation_id": ext.body["operation_id"],
            "code": "CONFLICT",
            "timestamp": 123,
            "data": {"reason_code": "STATE_CONFLICT"},
        },
        "conflict",
    )
    assert (await ext.send(ext.body)).body == ext.state.guard.body
    assert (await ext.send(ext.body)).body == ext.state.guard.body
    async with ext.factory() as session:
        saved = await find_receipt(session, OPERATION, ext.body["operation_id"])
        assert saved is None or saved.body == ext.state.guard.body
    assert await ext.kinds() == []
    assert ext.events == []


@pytest.mark.parametrize(
    "original", [Accepted(), Conflict({"reason_code": "STATE_CONFLICT"}), Unavailable()]
)
async def test_frozen_model_is_not_replaced_by_later_known_fields(extension, original):
    ext = extension
    ext.state.outcome = original
    first_body = ext.body | {"data": ext.body["data"] | {"future_note": "old unknown value"}}
    first = await ext.send(first_body)
    ext.state.outcome = None

    class CandidateModel(PlanDelta):
        future_note: str

    frozen = PlanDelta.model_validate(first_body["data"]).model_dump(mode="json")
    candidate = CandidateModel.model_validate(first_body["data"]).model_dump(mode="json")
    assert canonical_digest(frozen) != canonical_digest(candidate)
    changed_note = first_body | {"data": first_body["data"] | {"future_note": "new unknown value"}}
    replay = await ext.send(changed_note)
    assert replay.body == (
        first.body | {"code": "DUPLICATE"} if first.status_code == 202 else first.body
    )
    async with ext.factory() as session:
        saved = await find_receipt(session, OPERATION, ext.body["operation_id"])
        assert saved.effective_digest == canonical_digest(
            {
                "operation": OPERATION,
                "operation_id": ext.body["operation_id"],
                "timestamp": 17,
                "data": frozen,
            }
        )
    assert "not_before" not in frozen and "target_rack" not in frozen


async def test_known_slot_and_resource_changes_participate_in_identity(extension):
    from app.businesses._components.sorting_task.contracts import Cancel

    ext = extension
    ext.state.ready = True
    registry._registry[OPERATION] = replace(registry._registry[OPERATION], data_model=Cancel)
    body = ext.body | {
        "data": {
            "task_id": "T",
            "cancel_scope": "PLAN_MEMBERS",
            "direct_pick_sources": [
                {"plan_revision": 1, "rack_id": "D", "rack_face": "A", "slot_ids": ["1", "2"]}
            ],
        }
    }
    first = await ext.send(body)
    same = body | {
        "data": body["data"]
        | {"direct_pick_sources": [body["data"]["direct_pick_sources"][0] | {"unknown_note": "ok"}]}
    }
    assert (await ext.send(same)).body == first.body | {"code": "DUPLICATE"}
    changed = body | {
        "data": body["data"]
        | {
            "direct_pick_sources": [
                body["data"]["direct_pick_sources"][0] | {"slot_ids": ["2", "1"]}
            ]
        }
    }
    assert (await ext.send(changed)).body["data"] == {"reason_code": "IDEMPOTENCY_CONFLICT"}


async def test_missing_reference_remains_a_stable_conflict_without_recheck(extension):
    ext = extension
    ext.register(rechecking=False)
    ext.state.outcome = Conflict({"reason_code": "REFERENCE_CONFLICT"})
    first = await ext.send(ext.body)
    ext.state.outcome = None
    ext.state.ready = True
    assert first.status_code == 409
    assert (await ext.send(ext.body)).body == first.body
    assert "member" not in await ext.kinds()


@pytest.mark.parametrize(
    "guard,status,reason",
    [
        (Rejected("INVALID_DATA"), 422, "INVALID_DATA"),
        (Conflict({"reason_code": "REFERENCE_CONFLICT"}), 409, "REFERENCE_CONFLICT"),
    ],
)
async def test_guard_business_refusal_precedes_success_replay(extension, guard, status, reason):
    ext = extension
    ext.state.ready = True
    first = await ext.send(ext.body)
    ext.register(guarding=True)
    ext.state.guard = guard
    response = await ext.send(ext.body)
    assert response.status_code == status
    assert response.body["data"] == {"reason_code": reason}
    async with ext.factory() as session:
        assert (await find_receipt(session, OPERATION, ext.body["operation_id"])).body == first.body
    assert (await ext.kinds()).count("member") == 1
    assert ext.events == ["original"]


@pytest.mark.parametrize("existing", [False, True])
async def test_guard_unavailable_does_not_freeze_receipts(extension, existing):
    ext = extension
    ext.state.ready = True
    first = await ext.send(ext.body) if existing else None
    ext.register(guarding=True)
    ext.state.guard = Unavailable()
    async with ext.factory() as session, session.begin():
        await session.execute(insert(ext.table).values(kind="outer"))
        response = await ext.send(ext.body, session)
        assert response.status_code == 503 and response.body["data"] == {}
    async with ext.factory() as session:
        saved = await find_receipt(session, OPERATION, ext.body["operation_id"])
        assert (saved.body if saved else None) == (first.body if first else None)
    assert (await ext.kinds()).count("outer") == 1
    ext.state.guard = None
    assert (await ext.send(ext.body)).status_code == (200 if existing else 202)
    assert ext.events == ["original"]
