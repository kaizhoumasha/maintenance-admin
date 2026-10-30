import json
from dataclasses import dataclass, field
from datetime import timedelta

import pytest
from pydantic import AliasPath, ConfigDict, Field, RootModel, field_serializer, field_validator
from sqlalchemy import select

from app.core.identifiers import uuid7
from app.integrations.wms_request import InvalidRequestPayload, UnknownOperation
from app.integrations.wms_request.operation import lookup
from app.models import ReliableMessage
from app.reliable import DuplicateMessage, MessageStatus

FACT, DECISION = "example.fact.report@v1", "example.decision.ask@v1"
DATA = {"task_id": "T-1", "qty": 2}


def canonical_model():
    from app.integrations.wms_request import Endpoint, RequestPayload, request_operation

    class Item(RequestPayload):
        qty: int = Field(validation_alias=AliasPath("quantity", "value"))

    @request_operation("example.fact.model@v1", Endpoint.FACTS, ttl=timedelta(minutes=5))
    class ModelReport(RequestPayload):
        task_id: str = Field(alias="taskId")
        qty: int = 2
        note: str | None = None
        nested: list[Item]

        @field_serializer("task_id")
        def serialize_task_id(self, value):
            return str(value).upper()

    return ModelReport


async def rows(sessions):
    async with sessions() as session:
        return list(await session.scalars(select(ReliableMessage).order_by(ReliableMessage.id)))


async def test_operation_is_routed_to_its_endpoint_target_in_the_callers_transaction(
    sessions, wms, clock
):
    fact_id, decision_id = str(uuid7(clock())), str(uuid7(clock()))
    async with sessions() as session, session.begin():
        reference = await wms.send(session, lookup(FACT).data_model(**DATA), operation_id=fact_id)
        await wms.send(session, lookup(DECISION).data_model(**DATA), operation_id=decision_id)
    assert reference.status == MessageStatus.PENDING
    fact, decision = await rows(sessions)
    assert (fact.target, decision.target) == ("wms-facts", "wms-decisions")
    envelope = json.loads(fact.request_raw)
    assert list(envelope) == ["operation_id", "operation", "timestamp", "data"]
    assert envelope["data"] == DATA and envelope["operation_id"] == fact_id
    assert fact.expires_at is None  # 事实默认不过期


async def test_rollback_leaves_no_message(sessions, wms, clock):
    with pytest.raises(RuntimeError):
        async with sessions() as session, session.begin():
            await wms.send(
                session, lookup(FACT).data_model(**DATA), operation_id=str(uuid7(clock()))
            )
            raise RuntimeError("business rejected")
    assert await rows(sessions) == []


async def test_operation_declared_ttl_sets_expiry(sessions, wms, clock):
    async with sessions() as session, session.begin():
        await wms.send(
            session,
            lookup("example.fact.timed@v1").data_model(**DATA),
            operation_id=str(uuid7(clock())),
        )
    (row,) = await rows(sessions)
    assert row.expires_at is not None


@pytest.mark.parametrize(
    "operation,data,operation_id,error",
    [
        (FACT, {"task_id": "T-1"}, None, InvalidRequestPayload),  # 缺字段
        (FACT, {"task_id": "T-1", "qty": "2"}, None, InvalidRequestPayload),  # 类型不严格
        (FACT, {"task_id": "\ud800", "qty": 1}, None, InvalidRequestPayload),  # 非法 UTF-8
        (FACT, DATA, "not-a-uuid", InvalidRequestPayload),
    ],
)
async def test_invalid_requests_never_reach_the_outbox(
    sessions, wms, clock, operation, data, operation_id, error
):
    operation_id = operation_id or str(uuid7(clock()))
    with pytest.raises(error):
        async with sessions() as session, session.begin():
            await wms.send(
                session,
                lookup(operation).data_model.model_construct(**data),
                operation_id=operation_id,
            )
    assert await rows(sessions) == []


async def test_reentry_with_the_same_payload_returns_the_original_reference(sessions, wms, clock):
    """调用方重入时时间戳已变,但 Payload 相同,应当得到原消息而不是冲突。"""
    operation_id = str(uuid7(clock()))
    async with sessions() as session, session.begin():
        first = await wms.send(session, lookup(FACT).data_model(**DATA), operation_id=operation_id)
    clock.advance(30)
    async with sessions() as session, session.begin():
        again = await wms.send(session, lookup(FACT).data_model(**DATA), operation_id=operation_id)
    assert again.message_id == first.message_id
    (row,) = await rows(sessions)
    assert json.loads(row.request_raw)["data"] == DATA


async def test_same_identity_with_changed_content_is_a_duplicate_message(sessions, wms, clock):
    operation_id = str(uuid7(clock()))
    async with sessions() as session, session.begin():
        await wms.send(session, lookup(FACT).data_model(**DATA), operation_id=operation_id)
    clock.advance(30)
    with pytest.raises(DuplicateMessage):
        async with sessions() as session, session.begin():
            await wms.send(
                session, lookup(FACT).data_model(**{**DATA, "qty": 3}), operation_id=operation_id
            )


async def test_missing_endpoint_target_is_reported(sessions, wms, clock):
    wms.outbox.targets.pop("wms-facts")
    with pytest.raises(RuntimeError, match="wms-facts"):
        async with sessions() as session, session.begin():
            await wms.send(
                session, lookup(FACT).data_model(**DATA), operation_id=str(uuid7(clock()))
            )


async def test_model_send_preserves_canonical_payload_and_reference(sessions, wms, clock):
    from datetime import timedelta

    from pydantic import Field

    from app.integrations.wms_request import Endpoint, RequestPayload, request_operation

    @request_operation("example.fact.canonical@v1", Endpoint.FACTS, ttl=timedelta(minutes=5))
    class CanonicalReport(RequestPayload):
        task_id: str = Field(alias="taskId")
        qty: int = 2
        note: str | None = None
        nested: dict[str, list[int | str | None]]

    operation = "example.fact.canonical@v1"
    operation_id = str(uuid7(clock()))
    data = {"taskId": "T-1", "note": None, "nested": {"items": [1, "中文", None]}}
    expected = {"task_id": "T-1", "qty": 2, "note": None, "nested": data["nested"]}
    async with sessions.begin() as session:
        first = await wms.send(
            session, CanonicalReport.model_validate(data), operation_id=operation_id
        )
    (original,) = await rows(sessions)
    frozen = (original.request_raw, original.expires_at)
    assert original.target == "wms-facts"
    assert original.expires_at is not None
    assert json.loads(original.request_raw)["data"] == expected
    assert original.request_raw == json.dumps(
        dict(
            operation_id=operation_id,
            operation=operation,
            timestamp=int(clock().timestamp() * 1000),
            data=expected,
        ),
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")

    clock.advance(30)
    explicit_defaults = {**data, "qty": 2}
    async with sessions.begin() as session:
        again = await wms.send(
            session, CanonicalReport.model_validate(explicit_defaults), operation_id=operation_id
        )
    (replayed,) = await rows(sessions)
    assert again.message_id == first.message_id
    assert replayed.target == original.target
    assert (replayed.request_raw, replayed.expires_at) == frozen


async def test_model_serialization_and_defaults_preserve_the_original_frozen_message(
    sessions, wms, clock
):
    model = canonical_model()
    data = {"taskId": "t-1", "nested": [{"quantity": {"value": 3}}]}
    payload = model.model_validate(data)
    identity = str(uuid7(clock()))

    async def send(session, value):
        return await wms.send(session, model.model_validate(value), operation_id=identity)

    async with sessions.begin() as session:
        first = await send(session, data)
    (original,) = await rows(sessions)
    frozen = (original.request_raw, original.expires_at, original.target)
    assert json.loads(original.request_raw)["data"] == {
        "task_id": "T-1",
        "qty": 2,
        "note": None,
        "nested": [{"qty": 3}],
    }
    assert original.target == "wms-facts"
    assert original.expires_at is not None
    clock.advance(30)
    async with sessions.begin() as session:
        again = await send(session, {**data, "qty": 2, "note": None})
    (replayed,) = await rows(sessions)
    assert again.message_id == first.message_id
    assert (replayed.request_raw, replayed.expires_at, replayed.target) == frozen
    with pytest.raises(DuplicateMessage):
        async with sessions.begin() as session:
            await send(session, {**data, "qty": 4})
    assert payload.model_config == {"strict": True, "extra": "forbid"}


async def test_model_send_rollback_leaves_no_message(sessions, wms, clock):
    model = canonical_model()
    with pytest.raises(RuntimeError, match="business rejected"):
        async with sessions.begin() as session:
            await wms.send(session, model(taskId="T", nested=[]), operation_id=str(uuid7(clock())))
            raise RuntimeError("business rejected")
    assert await rows(sessions) == []


@pytest.mark.parametrize("reciprocal", [False, True])
async def test_model_send_preserves_actual_fields_when_aliases_overlap(
    sessions, wms, clock, reciprocal
):
    from app.integrations.wms_request import Endpoint, RequestPayload, request_operation

    class Item(RequestPayload):
        qty: int

    @request_operation("example.fact.overlap@v1", Endpoint.FACTS)
    class Report(RequestPayload):
        first: int = Field(alias="second")
        second: int = Field(alias="first") if reciprocal else Field()
        item: Item

    data = {"second": 11, "item": {"qty": 3}}
    if reciprocal:
        data["first"] = 22
    expected = {"first": 11, "second": 22 if reciprocal else 11, "item": {"qty": 3}}
    payload = Report.model_validate(data)
    item = payload.item
    identity = str(uuid7(clock()))

    async with sessions.begin() as session:
        first = await wms.send(session, payload, operation_id=identity)
    (original,) = await rows(sessions)
    assert json.loads(original.request_raw)["data"] == expected
    frozen = (original.request_raw, original.expires_at, original.target)
    clock.advance(30)
    async with sessions.begin() as session:
        again = await wms.send(session, payload, operation_id=identity)
    (replayed,) = await rows(sessions)
    assert again.message_id == first.message_id
    assert (replayed.request_raw, replayed.expires_at, replayed.target) == frozen
    assert (payload.first, payload.second) == (11, 22 if reciprocal else 11)
    assert payload.item is item and item.qty == 3


async def test_model_send_preserves_nested_root_contract_and_frozen_message(sessions, wms, clock):
    from app.integrations.wms_request import Endpoint, RequestPayload, request_operation

    class Quantities(RootModel[list[int]]):
        model_config = ConfigDict(strict=True)

        @field_serializer("root")
        def serialize_root(self, values):
            return [value * 10 for value in values]

    @request_operation("example.fact.root@v1", Endpoint.FACTS)
    class Report(RequestPayload):
        quantities: Quantities

    quantities = Quantities([1, 2])
    original_values = quantities.root
    payload = Report(quantities=quantities)
    identity = str(uuid7(clock()))

    async with sessions.begin() as session:
        first = await wms.send(session, payload, operation_id=identity)
    (original,) = await rows(sessions)
    assert json.loads(original.request_raw)["data"] == {"quantities": [10, 20]}
    frozen = (original.request_raw, original.expires_at, original.target)
    clock.advance(30)
    async with sessions.begin() as session:
        again = await wms.send(session, payload, operation_id=identity)
    (replayed,) = await rows(sessions)
    assert again.message_id == first.message_id
    assert (replayed.request_raw, replayed.expires_at, replayed.target) == frozen
    assert payload.quantities is quantities
    assert quantities.root is original_values and original_values == [1, 2]


@pytest.mark.parametrize("invalid", ["constructed", "assigned", "validator", "missing", "cycle"])
async def test_model_send_revalidates_actual_nested_root_contents(sessions, wms, clock, invalid):
    from app.integrations.wms_request import Endpoint, RequestPayload, request_operation

    class Quantities(RootModel[list[int]]):
        model_config = ConfigDict(strict=True)

        @field_validator("root")
        @classmethod
        def positive(cls, values):
            if any(value < 1 for value in values):
                raise ValueError("quantities must be positive")
            return values

    @request_operation("example.fact.root@v1", Endpoint.FACTS)
    class Report(RequestPayload):
        quantities: Quantities

    quantities = Quantities([1, 2])
    if invalid == "constructed":
        quantities = Quantities.model_construct(root=["1"])
    elif invalid == "assigned":
        quantities.root[0] = "1"
    elif invalid == "validator":
        quantities.root[0] = -1
    elif invalid == "missing":
        del quantities.root
    else:
        quantities.root.append(quantities)
    payload = Report.model_construct(quantities=quantities)
    original_values = vars(quantities).get("root")
    match = {"validator": "positive", "missing": "missing", "cycle": "cyclic"}.get(invalid)
    with pytest.raises(InvalidRequestPayload, match=match):
        async with sessions.begin() as session:
            await wms.send(session, payload, operation_id=str(uuid7(clock())))
    assert await rows(sessions) == []
    assert payload.quantities is quantities
    assert vars(quantities).get("root") is original_values
    if invalid in {"constructed", "assigned"}:
        assert original_values[0] == "1"
    elif invalid == "validator":
        assert original_values[0] == -1
    elif invalid == "cycle":
        assert original_values[-1] is quantities
    else:
        assert "root" not in vars(quantities)


@pytest.mark.parametrize("invalid", ["constructed", "assigned", "nested", "list", "extra"])
async def test_model_send_revalidates_actual_fields_before_serializing(
    sessions, wms, clock, invalid
):
    model = canonical_model()
    payload = model(taskId="T", nested=[{"quantity": {"value": 1}}])
    if invalid == "constructed":
        payload = model.model_construct(task_id=1, nested=[])
    elif invalid == "assigned":
        payload.task_id = 1
    elif invalid == "nested":
        payload.nested[0].qty = "1"
    elif invalid == "list":
        payload.nested.append({"quantity": {"value": "1"}})
    else:
        payload.__dict__["unlisted"] = 1
    with pytest.raises(InvalidRequestPayload):
        async with sessions.begin() as session:
            await wms.send(session, payload, operation_id=str(uuid7(clock())))
    assert await rows(sessions) == []


@pytest.mark.parametrize("invalid", ["value", "missing"])
async def test_model_send_revalidates_mutable_dataclasses(sessions, wms, clock, invalid):
    from app.integrations.wms_request import Endpoint, RequestPayload, request_operation

    @dataclass
    class Quantity:
        __pydantic_config__ = ConfigDict(strict=True, extra="forbid")
        value: int

    @request_operation("example.fact.dataclass@v1", Endpoint.FACTS)
    class Report(RequestPayload):
        quantity: Quantity

    payload = Report(quantity=Quantity(2))
    if invalid == "missing":
        del payload.quantity.value
    else:
        payload.quantity.value = "2"
    with pytest.raises(InvalidRequestPayload):
        async with sessions.begin() as session:
            await wms.send(session, payload, operation_id=str(uuid7(clock())))
    assert await rows(sessions) == []


@pytest.mark.parametrize("cycle", ["model", "container"])
async def test_model_send_rejects_cycles_without_enqueue(sessions, wms, clock, cycle):
    from typing import Any

    from app.integrations.wms_request import Endpoint, RequestPayload, request_operation

    @request_operation("example.fact.graph@v1", Endpoint.FACTS)
    class Graph(RequestPayload):
        data: list[Any]

    payload = Graph(data=[])
    payload.data.append(payload if cycle == "model" else payload.data)
    with pytest.raises(InvalidRequestPayload):
        async with sessions.begin() as session:
            await wms.send(session, payload, operation_id=str(uuid7(clock())))
    assert await rows(sessions) == []


async def test_model_send_revalidates_extra_dataclass_fields(sessions, wms, clock):
    from app.integrations.wms_request import Endpoint, RequestPayload, request_operation

    @dataclass
    class Quantity:
        __pydantic_config__ = ConfigDict(strict=True, extra="forbid")
        value: int

    @request_operation("example.fact.extra@v1", Endpoint.FACTS)
    class Report(RequestPayload):
        quantity: Quantity

    payload = Report(quantity=Quantity(2))
    payload.quantity.extra = "unlisted"
    with pytest.raises(InvalidRequestPayload):
        async with sessions.begin() as session:
            await wms.send(session, payload, operation_id=str(uuid7(clock())))
    assert await rows(sessions) == []


async def test_model_send_preserves_nested_model_serializers(sessions, wms, clock):
    from typing import Any

    from app.integrations.wms_request import Endpoint, RequestPayload, request_operation

    class Item(RequestPayload):
        value: int

        @field_serializer("value")
        def serialize_value(self, value):
            return f"Q{value}"

    @request_operation("example.fact.serialized@v1", Endpoint.FACTS)
    class Report(RequestPayload):
        data: list[Any]

    identity = str(uuid7(clock()))
    payload = Report(data=[Item(value=2)])
    async with sessions.begin() as session:
        await wms.send(session, payload, operation_id=identity)
    (row,) = await rows(sessions)
    assert json.loads(row.request_raw)["data"] == {"data": [{"value": "Q2"}]}


async def test_model_send_revalidates_models_used_as_dictionary_keys(sessions, wms, clock):
    from app.integrations.wms_request import Endpoint, RequestPayload, request_operation

    class Key(RequestPayload):
        model_config = ConfigDict(frozen=True)
        value: int

    @request_operation("example.fact.keys@v1", Endpoint.FACTS)
    class Report(RequestPayload):
        counts: dict[Key, int]

    key = Key(value=1)
    payload = Report(counts={key: 2})
    object.__setattr__(key, "value", "1")
    with pytest.raises(InvalidRequestPayload):
        async with sessions.begin() as session:
            await wms.send(session, payload, operation_id=str(uuid7(clock())))
    assert await rows(sessions) == []


@pytest.mark.parametrize("invalid", [False, True])
async def test_model_send_preserves_dataclass_contract_inherited_from_parent(
    sessions, wms, clock, invalid
):
    from typing import Annotated

    from app.integrations.wms_request import Endpoint, RequestPayload, request_operation

    @dataclass
    class Quantity:
        value: int
        relaxed: Annotated[int, Field(strict=False)] = 1

    @request_operation("example.fact.inherited@v1", Endpoint.FACTS)
    class Report(RequestPayload):
        quantity: Quantity

    payload = Report(quantity=Quantity(2))
    if invalid:
        payload.quantity.value = "2"
        with pytest.raises(InvalidRequestPayload):
            async with sessions.begin() as session:
                await wms.send(session, payload, operation_id=str(uuid7(clock())))
        assert await rows(sessions) == []
        assert payload.quantity.value == "2"
    else:
        payload.quantity.relaxed = "3"
        async with sessions.begin() as session:
            await wms.send(session, payload, operation_id=str(uuid7(clock())))
        (row,) = await rows(sessions)
        assert json.loads(row.request_raw)["data"] == {"quantity": {"value": 2, "relaxed": 3}}
        assert payload.quantity.relaxed == "3"


async def test_model_send_preserves_explicitly_relaxed_dataclass_contract(sessions, wms, clock):
    from app.integrations.wms_request import Endpoint, RequestPayload, request_operation

    @dataclass
    class Quantity:
        __pydantic_config__ = ConfigDict(strict=False, extra="forbid")
        value: int

    @request_operation("example.fact.relaxed@v1", Endpoint.FACTS)
    class Report(RequestPayload):
        quantity: Quantity

    payload = Report(quantity=Quantity(2))
    payload.quantity.value = "3"
    async with sessions.begin() as session:
        await wms.send(session, payload, operation_id=str(uuid7(clock())))
    (row,) = await rows(sessions)
    assert json.loads(row.request_raw)["data"] == {"quantity": {"value": 3}}
    assert payload.quantity.value == "3"


@pytest.mark.parametrize("direct_fields", [False, True])
async def test_model_send_requires_an_unambiguous_original_dataclass_contract(
    sessions, wms, clock, direct_fields
):
    from typing import Generic, TypeVar

    from app.integrations.wms_request import Endpoint, RequestPayload, request_operation

    item_type = TypeVar("item_type")

    @dataclass
    class Box(Generic[item_type]):
        value: item_type

    if direct_fields:

        @request_operation("example.fact.generic@v1", Endpoint.FACTS)
        class Report(RequestPayload):
            number: Box[int]
            text: Box[str]

        data = {"number": Box(2), "text": Box("two")}
        expected = {"number": {"value": 2}, "text": {"value": "two"}}
    else:

        @request_operation("example.fact.generic@v1", Endpoint.FACTS)
        class Report(RequestPayload):
            items: tuple[Box[int], Box[str]]

        data = {"items": (Box(2), Box("two"))}
        expected = {"items": [{"value": 2}, {"value": "two"}]}

    payload = Report.model_validate(data)
    identity = str(uuid7(clock()))
    if direct_fields:
        async with sessions.begin() as session:
            await wms.send(session, payload, operation_id=identity)
    else:
        with pytest.raises(InvalidRequestPayload, match="ambiguous.*explicit nested models"):
            async with sessions.begin() as session:
                await wms.send(session, payload, operation_id=identity)
        assert await rows(sessions) == []
        assert payload.items[0].value == 2
        assert payload.items[1].value == "two"
        return
    (row,) = await rows(sessions)
    assert json.loads(row.request_raw)["data"] == expected


@pytest.mark.parametrize("invalid", [False, True])
async def test_field_frozen_dataclasses_require_an_explicit_nested_model(
    sessions, wms, clock, invalid
):
    from pydantic.dataclasses import dataclass as pydantic_dataclass

    from app.integrations.wms_request import Endpoint, RequestPayload, request_operation

    @pydantic_dataclass(config=ConfigDict(strict=True, extra="forbid"))
    class Quantity:
        values: list[int] = Field(frozen=True)
        measured: int = field(init=False, default=1)

    @request_operation("example.fact.field_frozen@v1", Endpoint.FACTS)
    class Report(RequestPayload):
        quantity: Quantity

    quantity = Quantity(values=[1])
    quantity.measured = 7
    payload = Report(quantity=quantity)
    if invalid:
        quantity.values.append("2")
    identity = str(uuid7(clock()))
    with pytest.raises(InvalidRequestPayload, match=r"Field\(frozen=True\).*explicit nested model"):
        async with sessions.begin() as session:
            await wms.send(session, payload, operation_id=identity)
    assert await rows(sessions) == []
    assert quantity.values == ([1, "2"] if invalid else [1])
    assert quantity.measured == 7


@pytest.mark.parametrize("invalid", [False, True])
async def test_model_send_preserves_and_validates_frozen_dataclass_fields(
    sessions, wms, clock, invalid
):
    from pydantic.dataclasses import dataclass as pydantic_dataclass

    from app.integrations.wms_request import Endpoint, RequestPayload, request_operation

    @pydantic_dataclass(config=ConfigDict(strict=True, extra="forbid"), frozen=True)
    class Quantity:
        value: int
        measured: int = field(init=False, default=1)

    @request_operation("example.fact.frozen@v1", Endpoint.FACTS)
    class Report(RequestPayload):
        quantity: Quantity

    payload = Report(quantity=Quantity(2))
    object.__setattr__(payload.quantity, "measured", "3" if invalid else 3)
    if invalid:
        with pytest.raises(InvalidRequestPayload):
            async with sessions.begin() as session:
                await wms.send(session, payload, operation_id=str(uuid7(clock())))
        assert await rows(sessions) == []
    else:
        async with sessions.begin() as session:
            await wms.send(session, payload, operation_id=str(uuid7(clock())))
        (row,) = await rows(sessions)
        assert json.loads(row.request_raw)["data"] == {"quantity": {"value": 2, "measured": 3}}
        assert payload.quantity.measured == 3


@pytest.mark.parametrize("kind", ["unregistered", "subclass", "ambiguous", "fake_identity"])
async def test_model_send_requires_one_exact_registered_type(sessions, wms, clock, kind):
    from app.integrations.wms_request import Endpoint, RequestPayload, request_operation

    model = canonical_model()
    if kind == "ambiguous":
        request_operation("example.fact.second@v1", Endpoint.FACTS)(model)
    elif kind == "subclass":

        class Child(model):
            pass

        model = Child
    elif kind in {"unregistered", "fake_identity"}:

        class Other(RequestPayload):
            task_id: str

        Other.__name__ = model.__name__
        Other.operation = "example.fact.model@v1"
        model = Other
    payload = (
        model(task_id="T")
        if kind in {"unregistered", "fake_identity"}
        else model(taskId="T", nested=[])
    )
    error = InvalidRequestPayload if kind == "ambiguous" else UnknownOperation
    with pytest.raises(error):
        async with sessions.begin() as session:
            await wms.send(session, payload, operation_id=str(uuid7(clock())))
    assert await rows(sessions) == []


async def test_shared_request_freezes_original_business_and_wire(
    sessions, wms, clock, shared_wms_contract
):
    from app.businesses import catalog
    from app.integrations.wms_request import WmsRequests

    contract = shared_wms_contract
    first = WmsRequests(
        wms.outbox, owner_domain="first", contract_consumers=catalog.contract_consumers, clock=clock
    )
    second = WmsRequests(
        wms.outbox,
        owner_domain="second",
        contract_consumers=catalog.contract_consumers,
        clock=clock,
    )
    payload = contract.model(reference="original")
    identity = str(uuid7(clock()))
    async with sessions.begin() as session:
        original = await first.send(session, payload, operation_id=identity)
        row = await session.get(ReliableMessage, original.message_id)
        frozen = row.request_raw
        assert row.business_key == original.business_key == "business:first"
        assert set(json.loads(frozen)) == {"operation_id", "operation", "timestamp", "data"}
    clock.advance(30)
    async with sessions.begin() as session:
        assert (
            await first.send(session, payload, operation_id=identity)
        ).message_id == original.message_id
    with pytest.raises(InvalidRequestPayload, match="business"):
        async with sessions.begin() as session:
            await second.send(session, payload, operation_id=identity)
    async with sessions.begin() as session:
        other = await second.send(session, payload, operation_id=str(uuid7(clock())))
        assert other.business_key == "business:second"
        row = await session.get(ReliableMessage, original.message_id)
        assert row.business_key == "business:first" and row.request_raw == frozen


async def test_shared_formal_request_requires_bound_consumer(
    sessions, wms, shared_wms_contract, monkeypatch
):
    from app.businesses import catalog
    from app.integrations.wms_request import WmsRequests
    from app.integrations.wms_request.service import wms_requests

    monkeypatch.setattr(wms_requests, "_outbox", wms.outbox)
    payload = shared_wms_contract.model(reference="original")
    for service in (
        wms,
        wms_requests,
        WmsRequests(
            wms.outbox, owner_domain="third", contract_consumers=catalog.contract_consumers
        ),
    ):
        with pytest.raises(InvalidRequestPayload, match="business|consumer"):
            async with sessions.begin() as session:
                await service.send(session, payload, operation_id=str(uuid7()))
    assert await rows(sessions) == []


async def test_legacy_null_business_is_not_adopted_by_bound_facade(sessions, wms):
    from app.integrations.wms_request import WmsRequests

    payload = lookup(FACT).data_model(**DATA)
    identity = str(uuid7())
    async with sessions.begin() as session:
        original = await wms.send(session, payload, operation_id=identity)
    bound = WmsRequests(
        wms.outbox, owner_domain="first", contract_consumers=lambda key: frozenset({"first"})
    )
    with pytest.raises(InvalidRequestPayload, match="business"):
        async with sessions.begin() as session:
            await bound.send(session, payload, operation_id=identity)
    async with sessions() as session:
        row = await session.get(ReliableMessage, original.message_id)
        assert row.business_key is None
