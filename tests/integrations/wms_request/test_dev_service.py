import json
from datetime import datetime

import pytest
from pydantic import Field, field_serializer
from sqlalchemy import select

from app.core.errors import AppError, ErrorCode
from app.core.identifiers import uuid7
from app.integrations.wms_request import Endpoint, RequestPayload
from app.integrations.wms_request import operation as registry
from app.models import ReliableMessage
from app.services import dev_wms_request_service


@pytest.fixture(autouse=True)
def use_the_fixture_requests(monkeypatch, wms):
    monkeypatch.setattr(dev_wms_request_service, "wms_requests", wms)


@pytest.fixture
def dated():
    class Dated(RequestPayload):
        at: datetime

    registry.request_operation("example.fact.dated@v1", Endpoint.FACTS)(Dated)
    return "example.fact.dated@v1"


async def test_json_input_with_non_json_native_field_types_is_accepted(sessions, clock, dated):
    """联调请求体是 JSON,datetime 等字段以 ISO 字符串给出,仍要能走严格校验入队。"""
    async with sessions() as session:
        out = await dev_wms_request_service.send(
            session, dated, str(uuid7(clock())), {"at": "2026-01-01T00:00:00Z"}
        )
        row = await session.scalar(select(ReliableMessage))
    assert out.status == "PENDING"
    assert json.loads(row.request_raw)["data"] == {"at": "2026-01-01T00:00:00Z"}


async def test_json_input_keeps_registered_aliases_defaults_and_serializers(sessions, clock):
    @registry.request_operation("example.fact.aliased@v1", Endpoint.FACTS)
    class Aliased(RequestPayload):
        first: int = Field(alias="second")
        second: int = Field(alias="first")
        qty: int = 2

        @field_serializer("qty")
        def serialize_qty(self, value):
            return f"Q{value}"

    async with sessions() as session:
        out = await dev_wms_request_service.send(
            session, "example.fact.aliased@v1", str(uuid7(clock())), {"first": 11, "second": 22}
        )
        row = await session.get(ReliableMessage, out.message_id)
    assert json.loads(row.request_raw)["data"] == {"first": 22, "second": 11, "qty": "Q2"}


@pytest.mark.parametrize("data", [{"at": "not a date"}, {"at": "2026-01-01T00:00:00Z", "x": 1}])
async def test_json_input_that_breaks_the_payload_is_a_400(sessions, clock, dated, data):
    async with sessions() as session:
        with pytest.raises(AppError) as error:
            await dev_wms_request_service.send(session, dated, str(uuid7(clock())), data)
        assert await session.scalar(select(ReliableMessage)) is None
    assert error.value.code == ErrorCode.INVALID_REQUEST


async def test_a_request_too_large_for_the_target_is_a_400_not_a_500(sessions, clock):
    huge = {"task_id": "x" * 300_000, "qty": 1}
    async with sessions() as session:
        with pytest.raises(AppError) as error:
            await dev_wms_request_service.send(
                session, "example.fact.report@v1", str(uuid7(clock())), huge
            )
    assert error.value.code == ErrorCode.INVALID_REQUEST
