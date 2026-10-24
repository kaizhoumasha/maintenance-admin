import json

import pytest
from sqlalchemy import func, select

from app.integrations.ecs_command.result import receive_ecs_result
from app.models import CallbackReceipt, EcsCommand, EcsResultEvidence


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {"command_code": "x"},
        {"command_code": "x", "device_code": "D", "result": "SUCCESS", "finish_time": True},
        {
            "command_code": "x",
            "device_code": "D",
            "result": "SUCCESS",
            "finish_time": 1,
            "data": None,
        },
        {
            "command_code": "x",
            "device_code": "D",
            "result": "FAILED",
            "finish_time": 1,
            "error_detail": {"code": "X", "msg": "x", "secret": "s"},
        },
    ],
)
async def test_invalid_result_has_no_evidence(ecs, payload):
    async with ecs.factory.begin() as session:
        reply = await receive_ecs_result(session, json.dumps(payload).encode(), ecs.service)
        assert reply.status_code == 400
        assert "secret" not in json.dumps(reply.body)
        for model in (EcsCommand, EcsResultEvidence, CallbackReceipt):
            assert await session.scalar(select(func.count()).select_from(model)) == 0


async def test_unknown_result_does_not_negative_cache(ecs):
    raw = b'{"command_code":"x","device_code":"D","result":"SUCCESS","finish_time":1}'
    async with ecs.factory.begin() as session:
        assert (await receive_ecs_result(session, raw, ecs.service)).status_code == 404
        assert await session.scalar(select(func.count()).select_from(CallbackReceipt)) == 0


async def test_result_transaction_rollback_removes_evidence_and_projection(ecs):
    from app.integrations.ecs_command.lifecycle import EcsLifecycle
    from app.integrations.ecs_command.protocol import PROTOCOL
    from app.reliable.store import MessageStore

    async with ecs.factory.begin() as session:
        command = await ecs.service.create(
            session,
            owner=ecs.owner,
            call_key="call",
            device_code="D1",
            task_type="MOVE",
            timeout_ms=1,
            params={"qty": 1},
        )
    store = MessageStore(ecs.factory, lifecycles={PROTOCOL: EcsLifecycle(ecs.service)})
    claim = (await store.claim(1, ecs.targets))[0]
    await store.before_send(claim, ecs.targets["ecs"])
    async with ecs.factory() as session:
        raw = json.dumps(
            dict(
                command_code=command.command_code,
                device_code="D1",
                result="SUCCESS",
                finish_time=1,
                data={"qty": 1},
            )
        ).encode()
        assert (await receive_ecs_result(session, raw, ecs.service)).status_code == 200
        await session.rollback()
    async with ecs.factory() as session:
        assert await session.scalar(select(func.count()).select_from(EcsResultEvidence)) == 0
        assert (await session.scalar(select(EcsCommand))).status == "DISPATCHING"


async def test_nonfinite_result_extension_is_400_before_lookup(ecs):
    raw = b'{"command_code":"x","device_code":"D","result":"SUCCESS","finish_time":1,"extra":1e400}'
    async with ecs.factory.begin() as session:
        assert (await receive_ecs_result(session, raw, ecs.service)).status_code == 400


async def test_annex_error_validation_points_to_error_detail(ecs):
    from dataclasses import replace
    from typing import Literal

    from app.integrations.ecs_command.contracts import ErrorDetail

    class KnownError(ErrorDetail):
        code: Literal["ALLOWED"]

    ecs.service.annexes["test", "1"] = replace(
        ecs.service.annexes["test", "1"], error_model=KnownError
    )
    async with ecs.factory.begin() as session:
        command = await ecs.service.create(
            session,
            owner=ecs.owner,
            call_key="call",
            device_code="D1",
            task_type="MOVE",
            timeout_ms=1,
            params={"qty": 1},
        )
    raw = json.dumps(
        dict(
            command_code=command.command_code,
            device_code="D1",
            result="FAILED",
            finish_time=1,
            data={"qty": 1},
            error_detail={"code": "UNKNOWN", "msg": "x"},
        )
    ).encode()
    async with ecs.factory.begin() as session:
        response = await receive_ecs_result(session, raw, ecs.service)
        assert response.status_code == 400
        assert response.body["error_detail"]["issues"][0]["field"] == "error_detail.code"


async def test_storage_incompatible_identity_is_invalid_before_database_lookup(ecs):
    raw = json.dumps(
        dict(command_code="x\u0000", device_code="D", result="SUCCESS", finish_time=1)
    ).encode()
    async with ecs.factory.begin() as session:
        assert (await receive_ecs_result(session, raw, ecs.service)).status_code == 400
