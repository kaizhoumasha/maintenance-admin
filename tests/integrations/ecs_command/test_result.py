import json

import pytest
from sqlalchemy import func, select

from app.integrations.ecs_command.result import receive_ecs_result
from app.models import CallbackReceipt, EcsCommand, EcsResultEvidence
from tests.helpers import EcsMove


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
            timeout_ms=1,
            command=EcsMove.model_validate({"qty": 1}),
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
    from typing import Literal

    from app.integrations.ecs_command import compile_commands, ecs_command
    from app.integrations.ecs_command.contracts import ErrorDetail

    class KnownError(ErrorDetail):
        code: Literal["ALLOWED"]

    @ecs_command(
        "MOVE",
        device_roles=("SCAN1",),
        annex_id="test",
        annex_version="1",
        result=EcsMove,
        error=KnownError,
        timeout_min_ms=1,
        timeout_max_ms=60000,
    )
    class Move(EcsMove):
        pass

    ecs.service.annexes = compile_commands(Move)[1]
    async with ecs.factory.begin() as session:
        command = await ecs.service.create(
            session,
            owner=ecs.owner,
            call_key="call",
            device_code="D1",
            timeout_ms=1,
            command=Move(qty=1),
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


async def test_shared_permissive_models_keep_original_result_contract(ecs):
    from fastapi import FastAPI

    from app.integrations.ecs_callback import CallbackPayload
    from app.integrations.ecs_command import compile_commands, ecs_command
    from app.integrations.ecs_command.contracts import ErrorDetail
    from app.integrations.ecs_command.dev import configure_local_ecs
    from app.integrations.ecs_command.lifecycle import EcsLifecycle
    from app.integrations.ecs_command.protocol import PROTOCOL
    from app.reliable.store import MessageStore

    class LegacyData(CallbackPayload):
        qty: int

    ecs_command(
        "MOVE",
        device_roles=("SCAN1",),
        annex_id="test",
        annex_version="1",
        result=LegacyData,
        error=ErrorDetail,
        timeout_min_ms=1,
        timeout_max_ms=60000,
    )(LegacyData)
    annex = compile_commands(LegacyData)[1]["test", "1"]
    ecs.service.annexes = {("test", "1"): annex}
    params = {"qty": 1, "vendor_extension": {"station": "旧站点"}}
    async with ecs.factory.begin() as session:
        command = await ecs.service.create(
            session,
            owner=ecs.owner,
            call_key="legacy",
            device_code="D1",
            timeout_ms=30000,
            command=LegacyData.model_validate(params),
        )
        row = await session.get(EcsCommand, command.command_code)
        assert json.loads(row.request_raw)["params"] == {"qty": 1}
        frozen = (row.request_raw, dict(row.route), row.expires_at)

    store = MessageStore(ecs.factory, lifecycles={PROTOCOL: EcsLifecycle(ecs.service)})
    claim = (await store.claim(1, ecs.targets))[0]
    assert await store.before_send(claim, ecs.targets["ecs"])
    retained = configure_local_ecs(
        FastAPI(), environment="local", history_annexes={("test", "1"): annex}
    )
    assert retained.annexes["test", "1"] is annex
    assert annex.params_model is annex.result_model is LegacyData
    assert LegacyData.model_config["strict"] is True
    assert LegacyData.model_config["extra"] == "ignore"
    result = dict(
        command_code=command.command_code,
        device_code="D1",
        result="SUCCESS",
        finish_time=1,
        data={"qty": 1, "vendor_extension": ["accepted", None]},
        extension="ignored",
    )
    async with ecs.factory.begin() as session:
        invalid = {**result, "data": {"qty": "1"}}
        assert (
            await receive_ecs_result(session, json.dumps(invalid).encode(), retained)
        ).status_code == 400
        assert (
            await retained.get_command(session, owner=ecs.owner, command_code=command.command_code)
        ).result is None
    async with ecs.factory.begin() as session:
        assert (
            await receive_ecs_result(session, json.dumps(result).encode(), retained)
        ).status_code == 200
    async with ecs.factory() as session:
        view = await retained.get_command(
            session, owner=ecs.owner, command_code=command.command_code
        )
        assert view.status == "SUCCEEDED" and view.result["data"] == result["data"]
        assert "extension" not in view.result
        row = await session.get(EcsCommand, command.command_code)
        assert (row.request_raw, row.route, row.expires_at) == frozen
        assert row.route["annex_version"] == "1"
