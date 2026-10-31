"""Recovery by test-owned host transactions; this does not install a production host."""

from dataclasses import replace

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.businesses._components.bin_roller.ports import RollerWorkRef
from app.core.identifiers import uuid7
from app.database import create_database_engine


async def test_waiting_pages_and_one_failure_do_not_starve_other_kinds(
    roller_application, monkeypatch
):
    flow = roller_application
    host = flow.hosts["first"]
    for i in range(3):
        await flow.application_scan("first", "SCAN1", f"A{i:09}-B", timestamp=i + 1)
    host.production.permit = True
    await flow.application_scan("first", "SCAN2", "A000000009-B", timestamp=9)
    async with flow.factory() as session:
        arrivals = await flow.rows(session, "scan_arrival")
        release = (await flow.rows(session, "scan2_release"))[0]
    await flow.result(release["command_code"], "SCAN2")
    first_id = arrivals[0]["id"]
    advance = host.advance

    async def one_failed(work):
        if work.record_id == first_id:
            raise ValueError("test unavailable original work")
        return await advance(work)

    monkeypatch.setattr(host, "advance", one_failed)
    await host.scan_pending(limit=1)
    assert {work.record_id for work in host.visited} >= {row["id"] for row in arrivals[:3]}
    assert host.visited[1].kind == "SCAN2_RELEASE"
    assert len(host.failures) == 1
    async with flow.factory() as session:
        assert (await flow.rows(session, "scan2_release"))[0]["pending"] is False
        assert (await flow.rows(session, "scan_arrival"))[0]["pending"] is True


async def test_new_work_is_left_for_next_bounded_sweep(roller_application, monkeypatch):
    flow = roller_application
    host = flow.hosts["first"]
    await flow.application_scan("first", "SCAN1", "A000000001-B")
    advance = host.advance
    created = False

    async def add_during_sweep(work):
        nonlocal created
        if not created:
            created = True
            await flow.application_scan("first", "SCAN1", "A000000002-B", timestamp=2)
        return await advance(work)

    monkeypatch.setattr(host, "advance", add_during_sweep)
    assert await host.scan_pending() == 1
    assert await host.scan_pending() == 2
    async with flow.factory() as session:
        assert len(await flow.rows(session, "scan_arrival")) == 2
        assert len(await flow.messages(session)) == 2


async def test_serializable_advance_does_not_leak_to_receiver_or_other_business(
    roller_application, monkeypatch
):
    flow = roller_application
    if flow.factory.kw["bind"].dialect.name != "postgresql":
        pytest.skip("requires PostgreSQL isolation and connection reuse")
    await flow.application_scan("first", "SCAN1", "A000000001-B")
    async with flow.factory() as session:
        scan = (await flow.rows(session, "scan_arrival"))[0]
    engine = create_database_engine(flow.factory.kw["bind"].url, pool_size=1, max_overflow=0)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    host = flow.hosts["first"]
    host.capabilities = replace(host.capabilities, session_factory=factory)
    observed = []

    async def observe(session, attempt):
        observed.append(
            (
                await session.scalar(text("SHOW transaction_isolation")),
                await session.scalar(text("SELECT pg_backend_pid()")),
            )
        )

    try:
        await host.advance(RollerWorkRef("SCAN", scan["id"]), before=observe)
        assert observed[0][0] == "serializable"
        monkeypatch.setattr(flow, "factory", factory)
        async with factory() as session, session.begin():
            await session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
            assert await session.scalar(text("SHOW transaction_isolation")) == "repeatable read"
            assert await session.scalar(text("SELECT pg_backend_pid()")) == observed[0][1]
            response = await flow.application_scan(
                "second", "SCAN2", "A000000002-B", timestamp=2, session=session
            )
            assert response.status_code == 200
        async with factory() as session:
            assert await session.scalar(text("SHOW transaction_isolation")) == "read committed"
            assert await session.scalar(text("SELECT pg_backend_pid()")) == observed[0][1]
            assert (await session.execute(select(flow.production_table))).mappings().one()[
                "business"
            ] == "second"
    finally:
        await engine.dispose()


@pytest.mark.parametrize("failures", [1, 3])
async def test_commit_serialization_retry_reopens_whole_transaction_and_retains_pending(
    roller_application, failures
):
    flow = roller_application
    engine = flow.factory.kw["bind"]
    if engine.dialect.name != "postgresql":
        pytest.skip("requires a PostgreSQL deferred commit failure")
    host = flow.hosts["first"]
    await flow.application_scan("first", "SCAN1", "A000000001-B")
    async with flow.factory() as session:
        scan = (await flow.rows(session, "scan_arrival"))[0]
    await flow.result(scan["command_code"], "SCAN1")
    suffix = uuid7().hex
    function, sequence, trigger = (
        f"test_{name}_{suffix}" for name in ("commit", "attempt", "retry")
    )
    table = flow.store.tables["scan_arrival"]
    qualified = f"{table.schema}.{table.name}"
    async with engine.begin() as connection:
        await connection.execute(text(f"CREATE SEQUENCE {sequence}"))
        await connection.execute(
            text(f"""
            CREATE FUNCTION {function}() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                IF nextval('{sequence}') <= {failures} THEN
                    RAISE EXCEPTION 'test serialization at commit' USING ERRCODE = '40001';
                END IF;
                RETURN NEW;
            END $$
        """)
        )
        await connection.execute(
            text(f"""
            CREATE CONSTRAINT TRIGGER {trigger} AFTER UPDATE ON {qualified}
            DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
            WHEN (OLD.pending AND NOT NEW.pending) EXECUTE FUNCTION {function}()
        """)
        )
    work = RollerWorkRef("SCAN", scan["id"])
    try:
        if failures == 3:
            with pytest.raises(DBAPIError) as error:
                await host.advance(work)
            assert error.value.orig.sqlstate == "40001"
        else:
            await host.advance(work)
        assert len(host.attempts) == failures + (failures == 1)
        assert len({id(session) for session in host.attempts}) == len(host.attempts)
        async with flow.factory() as session:
            original = (await flow.rows(session, "scan_arrival"))[0]
            assert original["pending"] is (failures == 3)
            assert original["id"] == scan["id"]
            assert original["command_code"] == scan["command_code"]
            assert len(await flow.messages(session)) == 1
    finally:
        async with engine.begin() as connection:
            await connection.execute(text(f"DROP TRIGGER {trigger} ON {qualified}"))
            await connection.execute(text(f"DROP FUNCTION {function}()"))
            await connection.execute(text(f"DROP SEQUENCE {sequence}"))
    flow.application_rebuild()
    await flow.runtime.progressions["first"]()
    async with flow.factory() as session:
        original = (await flow.rows(session, "scan_arrival"))[0]
        assert original["pending"] is False
        assert original["command_code"] == scan["command_code"]
        assert len(await flow.messages(session)) == 1


@pytest.mark.parametrize("publish_fails", [False, True])
async def test_ctu03_acceptance_wakes_original_owner_and_recovers_without_final_result(
    roller_lifecycle, monkeypatch, publish_fails
):
    import json

    from app import business_wakeups
    from app.businesses import catalog
    from app.businesses._components.bin_roller.service import BinRoller
    from app.http_client import DeliveryState, HttpResult
    from app.integrations.wms_transport import ADAPTERS, PROTOCOL, TransportCaller
    from app.reliable.store import AttemptOutcome, MessageStore
    from app.reliable.types import MessageStatus

    flow = roller_lifecycle
    demand = await flow.demand()
    await flow.advance("FEED", demand)
    arrival = (await flow.all_rows("rack_action"))[0]
    await flow.rack_result(arrival)
    await flow.advance("FEED", demand)
    feed = (await flow.all_rows("feed"))[0]
    await flow.decision("feed", feed["id"], dict(result="RACK_FACE_DONE"))
    await flow.advance("FEED", demand)
    departure = (await flow.all_rows("departure"))[0]
    await flow.decision(
        "departure",
        departure["id"],
        dict(result="READY", rack_destination=dict(type="ZONE", location_code="STORAGE")),
    )
    await flow.advance("FEED", demand)
    action = (await flow.all_rows("rack_action"))[-1]
    assert action["kind"] == "CTU03"
    for entry in catalog.entries():
        entry.PROGRESSION = "scan_pending"
    catalog.validate_business_contracts()
    notices = []

    def publish(owner):
        if publish_fails:
            raise ConnectionError("test publisher unavailable")
        notices.append(owner)

    monkeypatch.setattr(business_wakeups, "enqueue_business", publish)
    store = MessageStore(
        flow.factory,
        on_result=business_wakeups.wms_result_committed,
        on_delivered=business_wakeups.transport_submit_delivered,
    )
    transport = flow.roller.capabilities.transport
    claims = await store.claim(100, transport.outbox.targets)
    claim = next(claim for claim in claims if claim.message.operation_id == action["operation_id"])
    assert claim.message.business_key == action["transport_task_id"]
    raw = json.dumps(
        dict(
            operation_id=action["operation_id"],
            code="RECEIVED",
            timestamp=1,
            data=dict(transport_task_id=action["transport_task_id"]),
        )
    ).encode()
    response = HttpResult(DeliveryState.HTTP_RESPONSE_COMPLETE, None, 202, (), raw)
    verdict = ADAPTERS[PROTOCOL].classify_response(claim.message, response)
    assert await store.finish(
        claim, AttemptOutcome(MessageStatus.DONE, result=response, verdict=verdict)
    )
    assert notices == ([] if publish_fails else ["first"])
    async with flow.factory() as session:
        result = await transport.get_result(
            session,
            transport_task_id=action["transport_task_id"],
            client_request_id=action["client_request_id"],
            caller=TransportCaller(action["caller_workline_id"], action["station_id"]),
        )
        assert result.outcome is None
        upper = await flow.roller.get_pending_upper_id(session, kind="RACK_CYCLE")
        pending = await flow.roller.list_pending(
            session, kind="RACK_CYCLE", after_id=None, upper_id=upper, limit=100
        )
        assert action["cycle_id"] in {work.record_id for work in pending}
    flow.roller = BinRoller(
        flow.roller.context, flow.roller.capabilities, flow.production, tables=flow.store.tables
    )
    await flow.advance("RACK_CYCLE", action["cycle_id"])
    await flow.advance("RACK_CYCLE", action["cycle_id"])
    cycle = (await flow.all_rows("rack_cycle"))[0]
    assert cycle["status"] == "RELEASED"
    assert [row["id"] for row in await flow.all_rows("rack_action")] == [
        arrival["id"],
        action["id"],
    ]
