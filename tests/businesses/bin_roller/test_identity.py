import pytest
from sqlalchemy import select

from app.core.identifiers import uuid7


async def fact(store, digest, raw="A", *, point="SCAN2", separator=None):
    async with store.factory.begin() as session:
        return await store.repo.accept_scan_fact(
            session,
            digest=digest,
            receipt_id=10,
            point=point,
            device_code="C",
            raw_bin_code=raw,
            bin_code=None,
            scanned_at=100,
            separator_command_code=separator,
        )


async def test_receipt_cleanup_keeps_original_digest_to_arrival(roller_store):
    store = roller_store
    first = await fact(store, "one")
    assert (await fact(store, "two"))["id"] == first["id"]
    await fact(store, "three", "B")
    assert (await fact(store, "two"))["id"] == first["id"]
    async with store.factory() as session:
        rows = (await session.execute(select(store.tables["scan_fact"]))).mappings().all()
        assert [(r["digest"], r["arrival_id"]) for r in rows[:2]] == [
            ("one", first["id"]),
            ("two", first["id"]),
        ]


async def test_raw_codes_and_original_result_separate_real_arrivals(roller_store):
    store = roller_store
    a = await fact(store, "one")
    assert (await fact(store, "two"))["id"] == a["id"]
    b = await fact(store, "three", "B")
    a2 = await fact(store, "four")
    assert a2["id"] not in {a["id"], b["id"]}
    a3 = await fact(store, "five", separator="original-command")
    assert a3["id"] != a2["id"]
    assert (await fact(store, "six", separator="original-command"))["id"] == a3["id"]


async def test_original_message_cannot_be_rebound(roller_store):
    store = roller_store
    await fact(store, "one")
    with pytest.raises(ValueError):
        await fact(store, "one", "B")


async def test_original_owner_and_request_identity_survive_restart(roller_store):
    from app.businesses._components.bin_roller.repository import RollerRepository

    store = roller_store
    async with store.factory.begin() as session:
        await store.repo.create_cycle(
            session,
            cycle_id="cycle",
            rack_id="R1",
            purpose="ORDINARY",
            task_id="T1",
            source_ref="source",
        )
        original = await store.repo.register_rack_action(
            session,
            cycle_id="cycle",
            kind="CTU01",
            source_ref="source",
            action_id="action",
            client_request_id="request",
            transport_task_id="transport",
            operation_id="operation",
            target={"kind": "RACK_POSITION", "location_code": "CTU01"},
            target_face="A",
            caller_workline_id="1",
        )
    restarted = RollerRepository("first", 1, tables=store.tables)
    async with store.factory.begin() as session:
        replay = await restarted.register_rack_action(
            session,
            cycle_id="cycle",
            kind="CTU01",
            source_ref="source",
            action_id=str(uuid7()),
            client_request_id=str(uuid7()),
            transport_task_id="new",
            operation_id="new",
            target={"kind": "RACK_POSITION", "location_code": "CTU01"},
            target_face="A",
            caller_workline_id="1",
        )
        assert replay["id"] == original["id"]
        assert (
            replay["owner_domain"],
            replay["client_request_id"],
            replay["transport_task_id"],
            replay["operation_id"],
        ) == ("first", "request", "transport", "operation")
        assert (
            await RollerRepository("second", 1, tables=store.tables).get_action(session, "action")
            is None
        )


async def test_same_business_action_has_one_original_intent_across_work_kinds(roller_store):
    store = roller_store
    async with store.factory.begin() as session:
        await store.repo.create_cycle(
            session,
            cycle_id="cycle",
            rack_id="R1",
            purpose="ORDINARY",
            task_id="T1",
            source_ref="source",
        )
        for work in ["FEED", "RACK_CYCLE", "DRAIN"]:
            action = await store.repo.register_rack_action(
                session,
                cycle_id="cycle",
                kind="CTU02",
                source_ref="service-A-to-B",
                action_id=work,
                client_request_id=work,
                transport_task_id=work,
                operation_id=work,
                target={"kind": "RACK_POSITION", "location_code": "CTU01"},
                target_face="B",
                caller_workline_id="1",
            )
            assert action["client_request_id"] == "FEED"
        assert len((await session.execute(select(store.tables["rack_action"]))).all()) == 1


async def test_business_progress_and_outbox_roll_back_together(roller_transport):
    from app.businesses._components.bin_roller.ports import RollerWorkRef
    from app.businesses._components.bin_roller.rack_flow import apply_rack_result
    from app.businesses._components.bin_roller.repository import RollerRepository
    from app.models import ReliableMessage, TransportTask

    rig, cycle_id = roller_transport, str(uuid7())
    store = rig.store
    async with store.factory.begin() as session:
        await store.repo.create_cycle(
            session,
            cycle_id=cycle_id,
            rack_id="R1",
            purpose="ORDINARY",
            task_id="T1",
            source_ref="source",
        )
        original = await rig.create_action(
            session, cycle_id=cycle_id, kind="CTU01", source_ref="source", target_face="A"
        )
    work = RollerWorkRef("RACK_CYCLE", cycle_id)
    await rig.callback(original)
    async with store.factory() as session:
        original_task = await session.scalar(
            select(TransportTask).where(
                TransportTask.transport_task_id == original["transport_task_id"]
            )
        )
        original_frozen = dict(original_task.frozen)
        original_raw = await session.scalar(
            select(ReliableMessage.request_raw).where(
                ReliableMessage.operation_id == original["operation_id"]
            )
        )

    async def advance_original(session):
        repository = RollerRepository("first", 1, tables=store.tables)
        actions = store.tables["rack_action"]
        entry = (
            (
                await session.execute(
                    select(actions).where(
                        actions.c.owner_domain == "first",
                        actions.c.work_line_id == 1,
                        actions.c.cycle_id == work.record_id,
                        actions.c.kind == "CTU01",
                    )
                )
            )
            .mappings()
            .one()
        )
        applied = await apply_rack_result(
            session,
            action_id=entry["id"],
            result=await rig.read_result(session, entry),
            repository=repository,
        )
        successor = await rig.create_action(
            session,
            cycle_id=work.record_id,
            kind="CTU02",
            source_ref=entry["id"] + ":A-to-B",
            target_face="B",
        )
        return applied, successor

    with pytest.raises(RuntimeError, match="rollback after successor"):
        async with store.factory.begin() as session:
            if session.get_bind().dialect.name == "postgresql":
                await session.connection(execution_options={"isolation_level": "SERIALIZABLE"})
            applied, successor = await advance_original(session)
            assert applied.action_changed
            consumed = await store.repo.get_action(session, original["id"])
            assert consumed["consumed_revision"] == 1 and not consumed["pending"]
            assert successor["target_face"] == "B" and successor["transport_task_id"]
            assert (
                len(
                    (
                        await session.execute(
                            select(ReliableMessage).where(ReliableMessage.target == rig.target)
                        )
                    ).all()
                )
                == 2
            )
            raise RuntimeError("rollback after successor")

    async with store.factory() as session:
        retained = await store.repo.get_action(session, original["id"])
        assert dict(retained) == dict(original)
        assert (await store.repo.get_cycle(session, work.record_id))["status"] == "ACTIVE"
        assert (await rig.read_result(session, retained)).outcome_revision == 1
        task = await session.scalar(
            select(TransportTask).where(
                TransportTask.transport_task_id == retained["transport_task_id"]
            )
        )
        assert task.frozen == original_frozen and task.owner_domain == "first"
        assert (
            await session.scalar(
                select(ReliableMessage.request_raw).where(
                    ReliableMessage.operation_id == retained["operation_id"]
                )
            )
            == original_raw
        )
        assert len((await session.execute(select(store.tables["rack_action"]))).all()) == 1
        assert (
            len(
                (
                    await session.execute(
                        select(ReliableMessage).where(ReliableMessage.target == rig.target)
                    )
                ).all()
            )
            == 1
        )

    for attempt in range(2):
        async with store.factory.begin() as session:
            if session.get_bind().dialect.name == "postgresql":
                await session.connection(execution_options={"isolation_level": "SERIALIZABLE"})
            applied, recovered = await advance_original(session)
            assert applied.action_changed is (attempt == 0)
            wire = await session.scalar(
                select(ReliableMessage.request_raw).where(
                    ReliableMessage.operation_id == recovered["operation_id"]
                )
            )
            if attempt == 0:
                committed = dict(recovered)
                committed_wire = wire
            else:
                assert dict(recovered) == committed
                assert wire == committed_wire
    async with store.factory() as session:
        progress = await store.repo.get_action(session, original["id"])
        assert progress["consumed_revision"] == 1 and not progress["pending"]
        assert progress["client_request_id"] == original["client_request_id"]
        assert progress["transport_task_id"] == original["transport_task_id"]
        assert progress["operation_id"] == original["operation_id"]
        assert len((await session.execute(select(store.tables["rack_action"]))).all()) == 2
        messages = list(
            await session.scalars(
                select(ReliableMessage).where(ReliableMessage.target == rig.target)
            )
        )
        assert len(messages) == 2
        successor_message = next(
            row for row in messages if row.operation_id == committed["operation_id"]
        )
        successor_task = await session.scalar(
            select(TransportTask).where(
                TransportTask.transport_task_id == committed["transport_task_id"]
            )
        )
        assert successor_task.kind == "RACK_ROTATE" and successor_task.owner_domain == "first"
        assert successor_task.workline_id == "1" and successor_task.station_id is None
        assert successor_task.client_request_id == committed["client_request_id"]
        assert successor_task.frozen["rack_id"] == "R1"
        assert successor_task.frozen["source"] == {
            "kind": "RACK_POSITION",
            "location_code": "CTU01",
        }
        assert successor_task.frozen["target"] == {
            "kind": "RACK_POSITION",
            "location_code": "CTU01",
        }
        assert (
            successor_task.frozen["target_face"] == "B"
            and successor_task.frozen["rcs_template_id"] == "CK04"
        )
        assert successor_message.business_key == committed["transport_task_id"]
        assert (
            next(
                row for row in messages if row.operation_id == original["operation_id"]
            ).request_raw
            == original_raw
        )


async def test_cycle_has_one_entry_and_departure_but_distinct_face_transitions(roller_store):
    store = roller_store
    async with store.factory.begin() as session:
        await store.repo.create_cycle(
            session,
            cycle_id="cycle",
            rack_id="R1",
            purpose="ORDINARY",
            task_id="T1",
            source_ref="source",
        )
        for kind in ["CTU01", "CTU03"]:
            await store.repo.register_rack_action(
                session,
                cycle_id="cycle",
                kind=kind,
                source_ref="original",
                action_id=kind,
                client_request_id=kind,
                transport_task_id=kind,
                operation_id=kind,
                target={"kind": "RACK_POSITION", "location_code": "P"},
                caller_workline_id="1",
            )
    for kind in ["CTU01", "CTU03"]:
        with pytest.raises(ValueError, match="original business input"):
            async with store.factory.begin() as session:
                await store.repo.register_rack_action(
                    session,
                    cycle_id="cycle",
                    kind=kind,
                    source_ref="replacement",
                    action_id=kind + "-new",
                    client_request_id=kind + "-new",
                    transport_task_id=kind + "-new",
                    operation_id=kind + "-new",
                    target={"kind": "RACK_POSITION", "location_code": "P"},
                    caller_workline_id="1",
                )
    async with store.factory.begin() as session:
        for transition in ["service-A-to-B", "service-B-to-A"]:
            await store.repo.register_rack_action(
                session,
                cycle_id="cycle",
                kind="CTU02",
                source_ref=transition,
                action_id=transition,
                client_request_id=transition,
                transport_task_id=transition,
                operation_id=transition,
                target={"kind": "RACK_POSITION", "location_code": "P"},
                caller_workline_id="1",
            )
        assert len((await session.execute(select(store.tables["rack_action"]))).all()) == 4


async def test_rack_cycles_are_business_identity_not_physical_occupancy(roller_store):
    store = roller_store
    async with store.factory.begin() as session:
        for cycle, source in [("first", "source-1"), ("second", "source-2")]:
            await store.repo.create_cycle(
                session,
                cycle_id=cycle,
                rack_id="R1",
                purpose="ORDINARY",
                task_id="T1",
                source_ref=source,
            )
        assert len((await session.execute(select(store.tables["rack_cycle"]))).all()) == 2
