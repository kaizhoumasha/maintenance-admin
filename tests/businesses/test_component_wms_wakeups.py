import json
from types import MappingProxyType, SimpleNamespace

import pytest
from sqlalchemy import Boolean, Column, Integer, MetaData, String, Table, delete, func, select

from app import business_wakeups
from app.businesses import catalog
from app.businesses.runtime import build_runtime
from app.core.identifiers import uuid7
from app.http_client import DeliveryState, HttpResult
from app.integrations.wms_request import ADAPTERS, DECISIONS_PROTOCOL
from app.models import ReliableMessage
from app.reliable import Outbox
from app.reliable.config import TargetConfig
from app.reliable.store import AttemptOutcome, MessageStore
from app.reliable.types import MessageStatus
from app.tasks.business_tasks import advance_pending


@pytest.fixture
async def wms_flow(callback_store, shared_wms_contract, monkeypatch):
    factory, contract = callback_store, shared_wms_contract
    engine = factory.kw["bind"]
    metadata = MetaData()
    table = Table(
        "test_shared_wms_pending",
        metadata,
        Column("identity", String, primary_key=True),
        Column("business", String, nullable=False),
        Column("message_id", Integer, nullable=False),
        Column("handled", Boolean, nullable=False),
    )
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)

    try:

        class Part:
            def __init__(self, business, capabilities):
                self.business, self.capabilities = business, capabilities

            async def scan_pending(self):
                async with self.capabilities.session_factory() as session:
                    rows = (
                        (
                            await session.execute(
                                select(table)
                                .where(
                                    table.c.business == self.business, table.c.handled.is_(False)
                                )
                                .order_by(table.c.identity)
                                .limit(5)
                            )
                        )
                        .mappings()
                        .all()
                    )
                consumed = 0
                for row in rows:
                    async with self.capabilities.session_factory() as session, session.begin():
                        if session.get_bind().dialect.name == "postgresql":
                            await session.connection(
                                execution_options={"isolation_level": "REPEATABLE READ"}
                            )
                        result = await self.capabilities.wms.get_result(
                            session,
                            message_id=row["message_id"],
                            operation=contract.operation,
                            operation_id=row["identity"],
                        )
                        if result.response_state == "VALID" and result.response_data == {
                            "result": "ALLOW"
                        }:
                            await session.execute(
                                table.update()
                                .where(table.c.identity == row["identity"])
                                .values(handled=True)
                            )
                            consumed += 1
                return consumed

        for entry in contract.entries[:2]:
            code = entry.DEFINITION.code
            entry.PROGRESSION = "scan_pending"

            def build(capabilities, business=code):
                part = Part(business, capabilities)
                return SimpleNamespace(
                    capabilities=capabilities,
                    components=MappingProxyType({"shared_wms": part}),
                    scan_pending=part.scan_pending,
                )

            entry.build_service = build
        contract.entries[2].build_service = lambda capabilities: object()
        catalog.validate_business_contracts()
        target = TargetConfig("component-decisions", "http://example.test", DECISIONS_PROTOCOL)
        targets = {target.name: target}
        outbox = Outbox(targets, ADAPTERS)
        runtime = build_runtime(outbox=outbox, ecs_commands=None, session_factory=factory)
        notified = []
        monkeypatch.setattr(business_wakeups, "enqueue_business", notified.append)
        yield SimpleNamespace(
            factory=factory,
            table=table,
            runtime=runtime,
            targets=targets,
            contract=contract,
            notices=notified,
        )
    finally:
        async with engine.begin() as conn:
            await conn.run_sync(metadata.drop_all)
        async with factory.begin() as session:
            await session.execute(
                delete(ReliableMessage).where(ReliableMessage.target == target.name)
            )


async def test_shared_wms_result_wakes_and_advances_only_original_business(wms_flow):
    flow = wms_flow
    identities = {}
    async with flow.factory.begin() as session:
        for business in ("first", "second"):
            identity = str(uuid7())
            ref = await flow.runtime.services[business].capabilities.wms.send(
                session, flow.contract.model(reference=business), operation_id=identity
            )
            await session.execute(
                flow.table.insert().values(
                    identity=identity, business=business, message_id=ref.message_id, handled=False
                )
            )
            identities[business] = identity
    store = MessageStore(flow.factory, on_result=business_wakeups.wms_result_committed)
    claims = await store.claim(2, flow.targets)
    claim = next(claim for claim in claims if claim.message.operation_id == identities["first"])
    raw = json.dumps(
        {
            "operation_id": identities["first"],
            "code": "DECIDED",
            "timestamp": 1,
            "data": {"result": "ALLOW"},
        }
    ).encode()
    response = HttpResult(DeliveryState.HTTP_RESPONSE_COMPLETE, None, 200, (), raw)
    verdict = ADAPTERS[DECISIONS_PROTOCOL].classify_response(claim.message, response)
    outcome = AttemptOutcome(MessageStatus.DONE, result=response, verdict=verdict)
    assert await store.finish(claim, outcome)
    assert flow.notices == ["first"]
    assert not await store.finish(claim, outcome)
    assert flow.notices == ["first"]
    assert await advance_pending(flow.factory, flow.notices.pop()) == 1
    async with flow.factory() as session:
        rows = (
            await session.execute(
                select(flow.table.c.business, flow.table.c.handled).order_by(flow.table.c.business)
            )
        ).all()
        assert rows == [("first", True), ("second", False)]
        assert (
            await session.scalar(
                select(func.count())
                .select_from(ReliableMessage)
                .where(ReliableMessage.target.in_(flow.targets))
            )
            == 2
        )
    # A rebuilt task/Beat consumes saved evidence without creating replacement messages.
    assert await advance_pending(flow.factory, "first") == 0


@pytest.mark.parametrize(
    "association,expected",
    [
        ("business:first", ["first"]),
        ("business:second", ["second"]),
        (None, []),
        ("SECRET_ASSOCIATION", []),
        ("business:third", []),
        ("business:missing", []),
    ],
)
def test_shared_result_never_guesses_or_broadcasts_business(
    shared_wms_contract, monkeypatch, caplog, association, expected
):
    for entry in shared_wms_contract.entries[:2]:
        entry.PROGRESSION = "scan_pending"
    catalog.validate_business_contracts()
    notices = []
    monkeypatch.setattr(business_wakeups, "enqueue_business", notices.append)
    business_wakeups.wms_result_committed(shared_wms_contract.operation, association)
    assert notices == expected
    assert "SECRET_ASSOCIATION" not in caplog.text
    if not expected:
        assert caplog.records


def test_bound_result_without_progression_is_diagnosed_not_reassigned(
    shared_wms_contract, monkeypatch, caplog
):
    notices = []
    monkeypatch.setattr(business_wakeups, "enqueue_business", notices.append)
    business_wakeups.wms_result_committed(shared_wms_contract.operation, "business:first")
    assert notices == [] and caplog.records


def test_legacy_exclusive_null_result_keeps_original_source(shared_wms_contract, monkeypatch):
    from app.integrations.wms_request import Endpoint, RequestPayload, request_operation

    entry = shared_wms_contract.entries[0]
    module = "app.businesses.first.request.exclusive_ask_v1"
    entry.ENTRY_MODULES = (module,)
    entry.PROGRESSION = "scan_pending"

    class Ask(RequestPayload):
        reference: str

    Ask.__module__ = module
    request_operation("exclusive.ask@v1", Endpoint.DECISIONS)(Ask)
    catalog.validate_business_contracts()
    notices = []
    monkeypatch.setattr(business_wakeups, "enqueue_business", notices.append)
    business_wakeups.wms_result_committed("exclusive.ask@v1", None)
    assert notices == ["first"]
