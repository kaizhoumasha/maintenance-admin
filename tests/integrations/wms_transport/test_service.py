import json
from dataclasses import FrozenInstanceError, replace
from uuid import UUID

import pytest
from sqlalchemy import select

from app.integrations.wms_transport import (
    IdempotencyConflict,
    InvalidTransportRequest,
    Rack,
    RackPosition,
    TransportCaller,
    TransportService,
    Zone,
)
from app.integrations.wms_transport.protocol import SUBMIT_TTL
from app.models import Menu, ReliableMessage, TransportTask
from app.reliable import InvalidMessage

from .helpers import counts


async def test_same_normalized_input_replays_without_new_identity(
    transport_storage, transport_request
):
    rig = transport_storage
    async with rig.factory() as session:
        first = await rig.service.move_rack(session, **transport_request)
        await session.commit()

    def unexpected():
        raise AssertionError("replay must not generate a new identity or timestamp")

    replay = TransportService(
        rig.outbox, target=rig.target.name, clock=unexpected, uuid7=unexpected
    )
    async with rig.factory() as session:
        second = await replay.move_rack(session, **transport_request, rcs_template_id="F01")
        await session.commit()
        assert first == second and await counts(session, rig) == (1, 1)
        message = await session.scalar(
            select(ReliableMessage).where(ReliableMessage.target == rig.target.name)
        )
        task = await session.scalar(
            select(TransportTask).where(TransportTask.transport_task_id == first.transport_task_id)
        )
        payload = json.loads(message.request_raw)
        assert UUID(message.operation_id).version == 7
        assert UUID(first.transport_task_id.removeprefix("transport-")).version == 7
        assert first.transport_task_id.removeprefix("transport-") != message.operation_id
        assert task.operation_id == message.operation_id
        assert payload["data"]["transport_task_id"] == first.transport_task_id
        assert message.business_key == first.transport_task_id
        assert message.expires_at - message.created_at == SUBMIT_TTL  # 超时不再重发
        assert "workline_id" not in payload["data"] and "caller" not in payload
        frozen = {k: v for k, v in payload["data"].items() if k != "transport_task_id"}
        assert task.frozen == frozen  # 回调应用要用的冻结事实随登记写入
        assert task.workline_id == transport_request["caller"].workline_id
    with pytest.raises(FrozenInstanceError):
        first.transport_task_id = "changed"


@pytest.mark.parametrize(
    "changes",
    [
        {"target": Zone("other")},
        {"target_face": "90"},
        {"rcs_template_id": "CTU01"},
        {"caller": TransportCaller("another-workline", "station")},
    ],
)
async def test_same_identity_with_changed_input_is_a_conflict(
    transport_storage, transport_request, changes
):
    rig = transport_storage
    async with rig.factory() as session:
        original = await rig.service.move_rack(session, **transport_request)
        with pytest.raises(IdempotencyConflict):
            await rig.service.move_rack(session, **(transport_request | changes))
        await session.commit()
        assert await counts(session, rig) == (1, 1)
        assert await rig.service.move_rack(session, **transport_request) == original


@pytest.mark.parametrize(
    "changes",
    [
        {"client_request_id": "not-a-uuid"},
        {"client_request_id": "9af2751b-4d01-4203-88a7-649bbf69e810"},
        {"client_request_id": "01A13B00-CABD-7390-890A-0C093E7C07A1"},
        {"caller": {"workline_id": "line"}},
        {"rack_id": ""},
        {"rack_id": "rack\x00"},
        {"source": {"kind": "RACK", "location_code": "rack"}},
        {"source": Rack("another-rack")},
        {"target": Rack("another-rack")},
        {"target": Rack("rack")},
        {"target_face": ""},
        {"target_face": "face\x00"},
        {"target_face": "\ud800"},
        {"target_face": "x" * 11},
        {"target_face": 90},
        {"rcs_template_id": "f01"},
        {"rcs_template_id": []},
    ],
)
async def test_invalid_input_never_creates_a_task_or_message(
    transport_storage, transport_request, changes
):
    rig = transport_storage
    async with rig.factory() as session:
        with pytest.raises(InvalidTransportRequest):
            await rig.service.move_rack(session, **(transport_request | changes))
        await session.commit()
        assert await counts(session, rig) == (0, 0)


async def test_caller_rollback_removes_task_and_outbox_message(
    transport_storage, transport_request
):
    rig = transport_storage
    async with rig.factory() as session:
        await rig.service.move_rack(session, **transport_request)
        await session.rollback()
    async with rig.factory() as session:
        assert await counts(session, rig) == (0, 0)


async def test_failed_enqueue_keeps_outer_business_transaction(
    transport_storage, transport_request
):
    rig = transport_storage
    async with rig.factory() as session:
        session.add(Menu(name=rig.target.name, path="/caller", title="Caller", component="Page"))
        rig.outbox.targets = {}
        with pytest.raises(InvalidMessage):
            await rig.service.move_rack(session, **transport_request)
        await session.commit()
    async with rig.factory() as session:
        assert await counts(session, rig) == (0, 0)
        assert await session.scalar(select(Menu.id).where(Menu.name == rig.target.name)) is not None


async def test_body_limit_cannot_be_increased_by_target_config(
    transport_storage, transport_request
):
    rig = transport_storage
    target = replace(rig.target, max_body_bytes=1024 * 1024)
    rig.outbox.targets = {target.name: target}
    oversized = transport_request | {"source": RackPosition("架" * 100000)}
    async with rig.factory() as session:
        with pytest.raises(InvalidTransportRequest):
            await rig.service.move_rack(session, **oversized)
        await session.commit()
        assert await counts(session, rig) == (0, 0)


@pytest.mark.parametrize(
    "source_type,target_type",
    [
        (Rack, Zone),
        (Rack, RackPosition),
        (Zone, Rack),
        (Zone, Zone),
        (Zone, RackPosition),
        (RackPosition, Rack),
        (RackPosition, Zone),
        (RackPosition, RackPosition),
    ],
)
async def test_distinct_locations_and_new_requests_do_not_lock_resources(
    transport_storage, transport_request, source_type, target_type
):
    rig = transport_storage
    request = transport_request | {
        "source": source_type("rack" if source_type is Rack else "source"),
        "target": target_type("rack" if target_type is Rack else "destination"),
        "target_face": "🧭" * 10,
    }
    async with rig.factory() as session:
        first = await rig.service.move_rack(session, **request)
        second = await rig.service.move_rack(
            session,
            **(
                request
                | {
                    "client_request_id": "01a13b00-cabd-7390-890a-0c093e7c07a2",
                }
            ),
        )
        await session.commit()
        assert first.transport_task_id != second.transport_task_id
        assert await counts(session, rig) == (2, 2)


def settings_with(monkeypatch, targets):
    from app.core.config import Settings
    from app.integrations.wms_transport import service as service_module

    settings = Settings(_env_file=None, RELIABLE_TARGETS={"targets": targets})
    monkeypatch.setattr(service_module, "get_settings", lambda: settings)


def wms(**extra):
    return {"base_url": "http://wms.example.test", "protocol": "wms-transport"} | extra


async def test_unconfigured_service_fails_clearly_before_touching_anything(monkeypatch):
    settings_with(monkeypatch, {})
    with pytest.raises(RuntimeError, match="Transport"):
        await TransportService().move_rack(
            None,
            client_request_id="01a13b00-cabd-7390-890a-0c093e7c07a1",
            caller=TransportCaller("line"),
            rack_id="rack",
            source=Rack("rack"),
            target=RackPosition("KT16"),
        )


def test_default_service_resolves_outbox_and_target_from_settings_once(monkeypatch):
    settings_with(monkeypatch, {"site-wms": wms(), "other": wms() | {"protocol": "another"}})
    service = TransportService()
    assert service.target == "site-wms"
    assert service.outbox is service.outbox
    assert set(service.outbox.targets) == {"site-wms", "other"}
    assert "wms-transport" in service.outbox.adapters


def test_default_service_rejects_ambiguous_wms_targets(monkeypatch):
    settings_with(monkeypatch, {"a": wms(), "b": wms()})
    with pytest.raises(RuntimeError, match="多个"):
        _ = TransportService().target


def test_explicit_arguments_take_precedence_over_settings(monkeypatch, transport_storage):
    rig = transport_storage
    settings_with(monkeypatch, {"site-wms": wms()})
    service = TransportService(rig.outbox, target=rig.target.name)
    assert service.outbox is rig.outbox and service.target == rig.target.name
    assert TransportService(rig.outbox).target == rig.target.name  # 由注入的 Outbox 按协议推断


def test_unconfigured_error_shows_the_setting_an_example_and_what_exists(monkeypatch):
    settings_with(monkeypatch, {})
    with pytest.raises(RuntimeError) as empty:
        _ = TransportService().target
    assert "RELIABLE_TARGETS" in str(empty.value) and "wms-transport" in str(empty.value)
    assert '"targets":{"wms-transport":{"base_url"' in str(empty.value)  # 带一个可照抄的示例

    settings_with(monkeypatch, {"ecs": wms() | {"protocol": "ecs-device"}})
    with pytest.raises(RuntimeError) as other:
        _ = TransportService().target
    assert "ecs[ecs-device]" in str(other.value)  # 指出现有目标及其协议,便于发现写错协议名
