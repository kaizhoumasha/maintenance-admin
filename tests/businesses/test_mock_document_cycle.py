"""Document acceptance must wait for decision, device Result, and recorded report."""

import asyncio
import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event, select, text, update

from app.core.identifiers import uuid7
from app.integrations.ecs_command import PROTOCOL_RETRY_WINDOW
from app.main import app
from app.models import CallbackReceipt
from app.reliable import DuplicateMessage
from tests.helpers import flow_request


async def accept(flow, *, kind="wms", index=0):
    path, body = flow_request(flow, kind=kind, index=index)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        reply = await client.post(path, json=body)
    assert reply.status_code == (202 if kind == "wms" else 200), reply.text
    return body["data"]["execution_id"]


async def test_document_completes_only_after_original_result_and_recorded_report(mock_cycle):
    identity = await accept(mock_cycle)
    service = mock_cycle.runtime.services["seam_a"]
    row = (await mock_cycle.rows())[0]
    assert row["pending"] and not row["done"]
    assert row["next_intent"] == {"stage": "accepted", "position": "POSITION_0"}
    assert await service.advance(identity) == "decision"
    assert await service.advance(identity) == "waiting"
    await mock_cycle.dispatch()
    assert await service.advance(identity) == "command"
    await mock_cycle.dispatch()
    assert await service.advance(identity) == "waiting"
    await mock_cycle.result(identity)
    assert await service.advance(identity) == "report"
    assert await service.advance(identity) == "waiting"
    await mock_cycle.dispatch()
    assert await service.advance(identity) == "done"
    row = (await mock_cycle.rows())[0]
    assert row["done"] and not row["pending"]
    assert set(row["evidence"]) == {"decision", "command", "report"}
    assert await mock_cycle.rebuild().services["seam_a"].advance(identity) == "done"
    decision, command, report = [json.loads(raw) for _, raw in mock_cycle.peer.received]
    assert decision["operation"] == "mock_cycle.decide@v1"
    assert report["operation"] == "mock_cycle.record@v1"
    assert (
        decision["data"] == report["data"] == {"execution_id": identity, "position": "POSITION_0"}
    )
    assert command["params"] == {"position": "POSITION_0"}
    assert command["task_type"] == "MOVE" and command["timeout"] == 100
    assert command["command_code"] == row["next_intent"]["command"]["command_code"]


async def prepare(flow, identity, phase):
    service = flow.runtime.services["seam_a"]
    if phase == "decision":
        return
    assert await service.advance(identity) == "decision"
    await flow.dispatch()
    if phase == "command":
        return
    assert await service.advance(identity) == "command"
    await flow.dispatch()
    assert (await flow.result(identity)).status_code == 200
    if phase == "report":
        return
    assert await service.advance(identity) == "report"
    await flow.dispatch()


@pytest.mark.parametrize("kind", ["wms", "ecs"])
async def test_receipt_original_target_and_pending_work_share_acceptance_transaction(
    mock_cycle, monkeypatch, kind
):
    service = mock_cycle.runtime.services["seam_a"]
    original = service.accept

    async def fail(ctx, payload):
        await original(ctx, payload)
        raise RuntimeError("acceptance rollback")

    monkeypatch.setattr(service, "accept", fail)
    path, body = flow_request(mock_cycle, kind=kind)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.post(path, json=body)).status_code == 503
        assert await mock_cycle.rows() == []
        async with mock_cycle.factory() as session:
            assert not await session.scalar(
                select(CallbackReceipt.id).where(CallbackReceipt.id.in_(mock_cycle.receipt_ids))
            )
        monkeypatch.setattr(service, "accept", original)
        assert (await client.post(path, json=body)).status_code == (202 if kind == "wms" else 200)
    assert mock_cycle.peer.received == []
    row = (await mock_cycle.rows())[0]
    assert row["next_intent"] == {"stage": "accepted", "position": "POSITION_0"}


async def test_replayed_document_keeps_original_cycle_and_conflicting_input_is_rejected(mock_cycle):
    path, body = flow_request(mock_cycle, kind="wms")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.post(path, json=body)).status_code == 202
        assert (await client.post(path, json=body)).status_code == 200
        body["operation_id"] = str(uuid7())
        assert (await client.post(path, json=body)).status_code == 200
        body["operation_id"] = str(uuid7())
        body["data"]["token"] = "changed"
        assert (await client.post(path, json=body)).status_code == 409
    assert len(await mock_cycle.rows()) == 1
    identity = body["data"]["execution_id"]
    service = mock_cycle.runtime.services["seam_a"]
    assert await service.advance(identity) == "decision"
    original = deepcopy((await mock_cycle.rows())[0]["next_intent"])
    assert await mock_cycle.rebuild().services["seam_a"].advance(identity) == "waiting"
    assert (await mock_cycle.rows())[0]["next_intent"] == original


@pytest.mark.parametrize("token", ["A", "changed"], ids=["same_content", "conflicting_content"])
async def test_concurrent_documents_compare_original_input_after_binding_lock(
    mock_cycle, monkeypatch, token
):
    path, first_body = flow_request(mock_cycle, kind="wms")
    second_body = deepcopy(first_body)
    second_body["operation_id"] = str(uuid7())
    second_body["data"]["token"] = token
    service = mock_cycle.runtime.services["seam_a"]
    original_accept = service.accept
    written, release, lock_started = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original_row = None

    async def pause_before_receiver_commit(ctx, payload):
        nonlocal original_row
        outcome = await original_accept(ctx, payload)
        if not written.is_set():
            original_row = dict(
                (await ctx.session.execute(select(mock_cycle.table))).mappings().one()
            )
            written.set()
            await release.wait()
        return outcome

    def observe_binding_lock(conn, cursor, statement, parameters, context, executemany):
        if written.is_set() and "FOR UPDATE" in statement and "work_line" in statement:
            lock_started.set()

    monkeypatch.setattr(service, "accept", pause_before_receiver_commit)
    engine = mock_cycle.factory.kw["bind"].sync_engine
    event.listen(engine, "before_cursor_execute", observe_binding_lock)
    tasks = []
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            try:
                async with asyncio.timeout(10):
                    tasks.append(asyncio.create_task(client.post(path, json=first_body)))
                    await written.wait()
                    # The first row is still uncommitted when the second precheck and lock start.
                    assert await mock_cycle.rows() == []
                    tasks.append(asyncio.create_task(client.post(path, json=second_body)))
                    await lock_started.wait()
                    assert not tasks[0].done() and not tasks[1].done()
                    release.set()
                    first, second = await asyncio.gather(*tasks)
            finally:
                release.set()
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        event.remove(engine, "before_cursor_execute", observe_binding_lock)
    assert first.status_code == 202, first.text
    assert second.status_code == (200 if token == "A" else 409), second.text
    rows = await mock_cycle.rows()
    assert [dict(row) for row in rows] == [original_row]
    assert rows[0]["accepted_payload"]["token"] == "A"
    assert rows[0]["pending"] and not rows[0]["done"]
    assert rows[0]["next_intent"] == {"stage": "accepted", "position": "POSITION_0"}
    assert await mock_cycle.dispatch() == 0
    assert mock_cycle.peer.received == []


@pytest.mark.parametrize("mode", ["deny", "reject", "invalid"])
async def test_decision_without_explicit_allow_preserves_original_identity_without_command(
    mock_cycle, mode
):
    mock_cycle.peer.decision = mode
    identity = await accept(mock_cycle)
    service = mock_cycle.runtime.services["seam_a"]
    assert await service.advance(identity) == "decision"
    original = deepcopy((await mock_cycle.rows())[0]["next_intent"]["decision"])
    await mock_cycle.dispatch()
    assert await service.advance(identity) == "blocked"
    assert await mock_cycle.rebuild().services["seam_a"].advance(identity) == "blocked"
    row = (await mock_cycle.rows())[0]
    assert row["pending"] and not row["done"] and row["next_intent"]["reason"]
    assert row["next_intent"]["decision"] == original
    assert "command" not in row["next_intent"] and not row["evidence"]
    assert row["next_intent"]["failure"]["message_id"] == original["message_id"]
    async with mock_cycle.factory() as session:
        assert (
            await mock_cycle.commands.get_by_call(
                session, owner=mock_cycle.owner, call_key=identity
            )
            is None
        )


@pytest.mark.parametrize("mode", ["reject", "invalid", "failed", "wrong_position"])
async def test_unconfirmed_move_never_emits_report_or_replacement_command(mock_cycle, mode):
    identity = await accept(mock_cycle)
    service = mock_cycle.runtime.services["seam_a"]
    await prepare(mock_cycle, identity, "command")
    assert await service.advance(identity) == "command"
    original = deepcopy((await mock_cycle.rows())[0]["next_intent"]["command"])
    mock_cycle.peer.command = mode
    await mock_cycle.dispatch()
    if mode in {"failed", "wrong_position"}:
        reply = await mock_cycle.result(
            identity,
            failed=mode == "failed",
            position="WRONG" if mode == "wrong_position" else None,
        )
        assert reply.status_code == 200
    assert await service.advance(identity) == "blocked"
    assert await mock_cycle.rebuild().services["seam_a"].advance(identity) == "blocked"
    row = (await mock_cycle.rows())[0]
    assert row["next_intent"]["command"] == original and row["next_intent"]["reason"]
    assert "report" not in row["next_intent"] and not row["done"]
    assert [name for name, _ in mock_cycle.peer.received] == ["decisions", "command"]


@pytest.mark.parametrize("mode", ["nonempty", "wrong_identity", "reject"])
async def test_report_must_match_original_request_and_valid_facts_contract(mock_cycle, mode):
    identity = await accept(mock_cycle)
    await prepare(mock_cycle, identity, "report")
    service = mock_cycle.runtime.services["seam_a"]
    assert await service.advance(identity) == "report"
    original = deepcopy((await mock_cycle.rows())[0]["next_intent"]["report"])
    mock_cycle.peer.report = mode
    await mock_cycle.dispatch()
    assert await service.advance(identity) == "blocked"
    assert await mock_cycle.rebuild().services["seam_a"].advance(identity) == "blocked"
    row = (await mock_cycle.rows())[0]
    assert row["next_intent"]["report"] == original and not row["done"]
    assert set(row["evidence"]) == {"decision", "command"}
    assert row["next_intent"]["failure"]["message_id"] == original["message_id"]


async def test_typed_reentry_reuses_original_messages_and_rejects_changed_input(mock_cycle):
    identity = await accept(mock_cycle)
    await prepare(mock_cycle, identity, "command")
    row = (await mock_cycle.rows())[0]
    original = row["next_intent"]["decision"]
    wms = mock_cycle.runtime.services["seam_a"].capabilities.wms
    async with mock_cycle.factory.begin() as session:
        replay = await wms.send(
            session,
            mock_cycle.models.decide(execution_id=identity, position="POSITION_0"),
            operation_id=original["operation_id"],
        )
        assert replay.message_id == original["message_id"]
    with pytest.raises(DuplicateMessage):
        async with mock_cycle.factory.begin() as session:
            await wms.send(
                session,
                mock_cycle.models.decide(execution_id=identity, position="WRONG"),
                operation_id=original["operation_id"],
            )
    service = mock_cycle.runtime.services["seam_a"]
    assert await service.advance(identity) == "command"
    original = (await mock_cycle.rows())[0]["next_intent"]["command"]
    async with mock_cycle.factory.begin() as session:
        command = await mock_cycle.commands.create(
            session,
            owner=mock_cycle.owner,
            call_key=identity,
            device_code=mock_cycle.codes[0],
            timeout_ms=100,
            command=mock_cycle.models.move(position="POSITION_0"),
        )
        assert command.command_code == original["command_code"]
    with pytest.raises(ValueError, match="different input"):
        async with mock_cycle.factory.begin() as session:
            await mock_cycle.commands.create(
                session,
                owner=mock_cycle.owner,
                call_key=identity,
                device_code=mock_cycle.codes[0],
                timeout_ms=100,
                command=mock_cycle.models.move(position="WRONG"),
            )


async def test_protocol_expiry_without_dispatch_keeps_original_command_pending_for_resolution(
    mock_cycle,
):
    identity = await accept(mock_cycle)
    await prepare(mock_cycle, identity, "command")
    service = mock_cycle.runtime.services["seam_a"]
    assert await service.advance(identity) == "command"
    original = deepcopy((await mock_cycle.rows())[0]["next_intent"]["command"])
    mock_cycle.store.clock = lambda: (
        datetime.now(UTC) + PROTOCOL_RETRY_WINDOW + timedelta(seconds=1)
    )
    assert await mock_cycle.sender.tick() == 0
    assert await service.advance(identity) == "blocked"
    row = (await mock_cycle.rows())[0]
    assert row["pending"] and not row["done"]
    assert row["next_intent"]["command"] == original
    assert row["next_intent"]["reason"] == "EXPIRED"
    assert "report" not in row["next_intent"]
    assert [name for name, _ in mock_cycle.peer.received] == ["decisions"]


@pytest.mark.parametrize("phase", ["decision", "command", "report", "done"])
async def test_consumed_result_and_next_intent_rollback_and_reenter_after_lost_return(
    mock_cycle, phase
):
    identity = await accept(mock_cycle)
    await prepare(mock_cycle, identity, phase)
    service = mock_cycle.runtime.services["seam_a"]
    before = dict((await mock_cycle.rows())[0])
    proposed = None

    async def crash(stage, attempt, session, evidence):
        nonlocal proposed
        if stage == "written":
            proposed = (
                (
                    await session.execute(
                        select(mock_cycle.table).where(mock_cycle.table.c.id == identity)
                    )
                )
                .mappings()
                .one()["next_intent"]
            )
            raise RuntimeError("before commit")

    with pytest.raises(RuntimeError, match="before commit"):
        await service.advance(identity, hook=crash)
    assert dict((await mock_cycle.rows())[0]) == before
    async with mock_cycle.factory() as session:
        if phase in {"decision", "report"}:
            result = await service.capabilities.wms.get_result(session, **proposed[phase])
            assert result.availability == "NOT_FOUND"
        if phase == "command":
            assert (
                await mock_cycle.commands.get_by_call(
                    session, owner=mock_cycle.owner, call_key=identity
                )
                is None
            )

    async def lost(stage, attempt, session, evidence):
        if stage == "committed":
            raise RuntimeError("lost return")

    with pytest.raises(RuntimeError, match="lost return"):
        await service.advance(identity, hook=lost)
    committed = dict((await mock_cycle.rows())[0])
    rebuilt = mock_cycle.rebuild().services["seam_a"]
    assert await rebuilt.advance(identity) == ("done" if phase == "done" else "waiting")
    assert dict((await mock_cycle.rows())[0]) == committed


async def test_lost_notification_and_two_lines_keep_original_target_and_command_identity(
    mock_cycle,
):
    identities = [
        await accept(mock_cycle, kind=kind, index=index)
        for index, kind in enumerate(("wms", "ecs"))
    ]
    service = mock_cycle.rebuild().services["seam_a"]
    assert set(await service.pending_execution_ids()) == set(identities)
    for identity in identities:
        assert await service.advance(identity) == "decision"
    await mock_cycle.dispatch()
    for identity in identities:
        assert await service.advance(identity) == "command"
    await mock_cycle.dispatch()
    rows = {r["id"]: r for r in await mock_cycle.rows()}
    assert {r["next_intent"]["position"] for r in rows.values()} == {"POSITION_0", "POSITION_1"}
    assert len({r["next_intent"]["command"]["command_code"] for r in rows.values()}) == 2
    assert (
        await mock_cycle.result(identities[0], device_code=mock_cycle.codes[1])
    ).status_code == 404
    assert await service.advance(identities[0]) == "waiting"
    assert (await mock_cycle.result(identities[0])).status_code == 200
    assert await service.advance(identities[0]) == "report"
    assert await service.advance(identities[1]) == "waiting"
    await mock_cycle.dispatch()
    assert await service.advance(identities[0]) == "done"
    assert await service.advance(identities[1]) == "waiting"
    async with mock_cycle.factory() as session:
        command = await mock_cycle.commands.get_by_call(
            session, owner=mock_cycle.owner, call_key=identities[1]
        )
        assert command.command_code == rows[identities[1]]["next_intent"]["command"]["command_code"]


@pytest.mark.parametrize("exhaust", [False, True])
async def test_serialization_retries_whole_cycle_in_new_repeatable_read_transaction(
    mock_cycle, exhaust
):
    identity = await accept(mock_cycle)
    await prepare(mock_cycle, identity, "command")
    before = dict((await mock_cycle.rows())[0])
    sessions, statements = [], []
    engine = mock_cycle.factory.kw["bind"].sync_engine

    def observe(conn, cursor, statement, parameters, context, executemany):
        statements.append((conn, statement, conn.get_execution_options().get("isolation_level")))

    async def collide(stage, attempt, session, evidence):
        if stage == "opened":
            sessions.append(session)
            assert await session.scalar(text("SHOW transaction_isolation")) == "repeatable read"
        if stage == "located" and (attempt == 0 or exhaust):
            async with mock_cycle.factory.begin() as writer:
                await writer.execute(
                    update(mock_cycle.table)
                    .where(mock_cycle.table.c.id == identity)
                    .values(touch=attempt + 1)
                )

    event.listen(engine, "before_cursor_execute", observe)
    try:
        outcome = await mock_cycle.runtime.services["seam_a"].advance(identity, hook=collide)
    finally:
        event.remove(engine, "before_cursor_execute", observe)
    assert outcome == ("retry_later" if exhaust else "command")
    assert len(sessions) == (3 if exhaust else 2)
    assert len({id(session) for session in sessions}) == len(sessions)
    assert all(not session.in_transaction() for session in sessions)
    for connection in {conn for conn, _, isolation in statements if isolation == "REPEATABLE READ"}:
        sql = [statement for conn, statement, _ in statements if conn is connection]
        assert sql[0] == "SHOW transaction_isolation"
        locked = [statement for statement in sql if "FOR UPDATE" in statement]
        assert '"work_line"' in locked[0] or ".work_line" in locked[0]
        assert '"device"' in locked[1] or ".device" in locked[1]
        assert "business_seam_test_execution" in locked[2]
    row = (await mock_cycle.rows())[0]
    if exhaust:
        assert row["pending"] and not row["done"]
        assert row["next_intent"] == before["next_intent"] and row["evidence"] == before["evidence"]
        assert await mock_cycle.rebuild().services["seam_a"].advance(identity) == "command"
