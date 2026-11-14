"""Exercise the deployed API and worker paths against an isolated simulated peer."""

import asyncio
import json
import os
import subprocess
import sys

from fastapi import FastAPI, Request

from tests.helpers import ecs_status_document, loopback_app, run_alembic
from tests.migration_helpers import schema_database  # noqa: F401

VALIDATE = "inbound.coarse_sorting.validate_bind@v1"
ALLOCATE = "inbound.coarse_sorting.storage_allocate@v1"
REPORT = "inbound.coarse_sorting.execution_report@v1"
DEPART = "rack.departure_decide@v1"


async def test_integration_resumes_formal_work_only_after_tail_configuration(tmp_path):
    await exercise_integration(f"sqlite+aiosqlite:///{tmp_path / 'first-and-tail.db'}")


async def test_postgres_integration_resumes_original_work_across_processes(schema_database):  # noqa: F811
    url, _ = schema_database
    result = await run_alembic(url, "upgrade", "head")
    assert result.returncode == 0, result.stdout + result.stderr
    await exercise_integration(url)


async def test_configured_mock_line_completes_normal_and_ng_through_the_original_sender(tmp_path):
    await exercise_integration(f"sqlite+aiosqlite:///{tmp_path / 'mock.db'}", mock="fixed")


async def test_postgres_mock_round_with_real_outbox_transactions(schema_database):  # noqa: F811
    url, _ = schema_database
    result = await run_alembic(url, "upgrade", "head")
    assert result.returncode == 0, result.stdout + result.stderr
    await exercise_integration(url, mock="random")


async def exercise_integration(integration_database, *, mock=None):
    peer = FastAPI()
    requests, commands, reports, transports = [], [], [], []
    status_checks = []
    allocation = {
        "result": "ALLOCATED",
        "workline_code": "COARSE",
        "rack_id": "RACK-1",
        "bin_code": "BIN-1",
        "bin_location": "B",
        "cell_id": "CELL-1",
        "cell_index": 2,
        "bin_type": "3",
        "reel_totalthickness": 120,
    }

    @peer.post("/api/v1/wes/decisions")
    async def decide(request: Request):
        wire = await request.json()
        requests.append(wire)
        if wire["operation"] == ALLOCATE:
            decision = (
                {
                    "result": "REPLACE_REQUIRED",
                    "workline_code": "COARSE",
                    "current_rack": {"rack_id": "OLD"},
                    "incoming_rack": {"rack_id": "RACK-1", "target_face": "90"},
                }
                if sum(message["operation"] == ALLOCATE for message in requests) == 1
                else allocation
            )
        elif wire["operation"] == DEPART:
            decision = (
                {"result": "READY", "rack_destination": {"type": "ZONE", "location_code": "STORE"}}
                if wire["data"]["rack_id"] == "OLD"
                else {"result": "WAIT", "retry_after_ms": 5000}
            )
        else:
            assert wire["operation"] == VALIDATE
            decision = (
                {"result": "NG", "reason_code": "BARCODE_INVALID"}
                if wire["data"]["barcode"] == "NG"
                else {"result": "OK", "binding_id": "ORIGINAL-BIND"}
            )
        return {
            "operation_id": wire["operation_id"],
            "code": "DECIDED",
            "timestamp": 1,
            "data": decision,
        }

    @peer.get("/api/v1/device/status")
    async def status(device_code: str):
        status_checks.append(device_code)
        return ecs_status_document(device_code, status="IDLE")

    @peer.post("/api/v1/device/command")
    async def command(request: Request):
        wire = await request.json()
        assert wire["device_code"] in status_checks
        status_checks.remove(wire["device_code"])
        commands.append(wire)
        return {"code": 200, "message": "ACK"}

    @peer.post("/api/v1/wes/facts")
    async def report(request: Request):
        wire = await request.json()
        reports.append(wire)
        return {
            "operation_id": wire["operation_id"],
            "code": "RECORDED",
            "timestamp": 1,
            "data": {},
        }

    @peer.post("/api/v1/wes/transport-requests", status_code=202)
    async def transport(request: Request):
        wire = await request.json()
        transports.append(wire["data"])
        return {
            "operation_id": wire["operation_id"],
            "code": "RECEIVED",
            "timestamp": 1,
            "data": {"transport_task_id": wire["data"]["transport_task_id"]},
        }

    async def run(phase, *, tail=False, **locations):
        targets = {
            protocol: {"protocol": protocol, "base_url": origin}
            for protocol in ("wms-decisions", "wms-facts", "wms-transport")
        }
        if phase == "mock":
            targets.update(
                {
                    f"coarse-sorting-mock-wms-{endpoint}": {
                        "protocol": f"wms-{endpoint}",
                        "base_url": origin,
                        "explicit_only": True,
                    }
                    for endpoint in ("decisions", "facts")
                }
            )
        result = await asyncio.to_thread(
            subprocess.run,
            [sys.executable, "-m", __name__, phase, origin],
            env={
                **os.environ,
                "ENVIRONMENT": "local",
                "COARSE_SORTING_TAIL_ENABLED": str(tail).lower(),
                "DATABASE_URL": integration_database,
                "REDIS_URL": "redis://127.0.0.1:1/0",
                "RELIABLE_TARGETS": json.dumps({"targets": targets}),
                "COARSE_SORTING_OUTPUT_LOCATIONS": "{}",
                "COARSE_SORTING_NG_LOCATIONS": "{}",
                **locations,
            },
            capture_output=True,
            text=True,
            timeout=35,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        return json.loads(result.stdout.splitlines()[-1])

    async with loopback_app(peer) as origin:
        if mock:
            await run(
                "mock",
                tail=True,
                COARSE_SORTING_WMS_MOCK=json.dumps(
                    {
                        "round_id": "test-1",
                        "ecs_source_ips": ["127.0.0.1"],
                        "rack_position": "RACK-POINT",
                    }
                    | (
                        {"empty_cells": [allocation | {"reel_totalthickness": 0}]}
                        if mock == "random"
                        else {
                            "normal_barcode": "NORMAL",
                            "ng_barcode": "NG",
                            "allocation": allocation,
                        }
                    )
                ),
                COARSE_SORTING_OUTPUT_LOCATIONS='{"COARSE":"OUTPUT-POINT"}',
                COARSE_SORTING_NG_LOCATIONS='{"COARSE":"NG-POINT"}',
            )
            assert not requests and not reports and not transports
            assert sorted(command["task_type"] for command in commands) == [
                "MOVE_FORWARD",
                "MOVE_TO_NG",
                "PUT_TO_BIN",
            ]
            return
        original = await run("first")
        assert [request["operation"] for request in requests] == [VALIDATE, VALIDATE]
        assert [command["task_type"] for command in commands] == ["MOVE_FORWARD"]
        assert not reports

        blocked = await run("blocked", tail=True)
        assert blocked == original
        assert len(requests) == 2 and len(commands) == 1 and not reports and not transports

        completed = await run(
            "resume",
            tail=True,
            COARSE_SORTING_OUTPUT_LOCATIONS='{"COARSE":"OUTPUT-POINT"}',
            COARSE_SORTING_NG_LOCATIONS='{"COARSE":"NG-POINT"}',
        )
    assert completed == original
    assert [request["operation"] for request in requests[:5]] == [
        VALIDATE,
        VALIDATE,
        ALLOCATE,
        DEPART,
        ALLOCATE,
    ]
    allocations = [wire for wire in requests if wire["operation"] == ALLOCATE]
    assert [wire["data"] for wire in allocations] == [
        {"workline_code": "COARSE", "binding_id": "ORIGINAL-BIND"}
    ] * 2
    assert len({wire["operation_id"] for wire in requests[:5]}) == 5
    assert [(wire["rack_id"], wire["rcs_template_id"]) for wire in transports] == [
        ("OLD", "F01"),
        ("RACK-1", "F01"),
    ]
    forward, *tail = commands
    assert forward["command_code"] == original["NORMAL"]["command_code"]
    assert {command["task_type"] for command in tail} == {"MOVE_TO_NG", "PUT_TO_BIN"}
    assert len(commands) == 3
    assert {command["task_type"]: command["device_code"] for command in tail} == {
        "MOVE_TO_NG": "INPUT",
        "PUT_TO_BIN": "OUTPUT",
    }
    (report_wire,) = reports
    assert report_wire["operation"] == REPORT
    assert report_wire["data"] == {
        **{key: allocation[key] for key in ("bin_code", "bin_location", "cell_id", "cell_index")},
        "workline_code": "COARSE",
        "binding_id": "ORIGINAL-BIND",
        "completed_at": 2,
    }


async def exercise(phase, origin):
    from contextlib import AsyncExitStack
    from dataclasses import replace
    from types import SimpleNamespace

    from httpx import ASGITransport, AsyncClient
    from pytest import raises
    from sqlalchemy import select

    from app import business_wakeups, main
    from app.businesses.catalog import load_business_models
    from app.businesses.coarse_sorting.contracts import RandomMockRound
    from app.businesses.coarse_sorting.mock_storage import CoarseSortingMockRound
    from app.businesses.coarse_sorting.models import CoarseSortingAction, CoarseSortingJob
    from app.core import get_current_user
    from app.core.database import engine, session_factory
    from app.core.identifiers import uuid7
    from app.http_client import AsyncHttpClient
    from app.integrations.wms_request import InvalidRequestPayload, wms_requests
    from app.integrations.wms_request.adapter import (
        COARSE_MOCK_DECISIONS_TARGET,
        COARSE_MOCK_FACTS_TARGET,
    )
    from app.integrations.wms_request.operation import lookup
    from app.main import app
    from app.models import (
        Base,
        Device,
        DeviceRoute,
        IntegrationExchange,
        TransportTask,
        WorkLinePosition,
    )
    from app.tasks.business_tasks import advance_pending
    from tests.helpers import add_bound_device

    # Drive the worker's task body directly, without a broker or a second sender.
    business_wakeups.enqueue_business = lambda _: None
    if phase in ("first", "mock"):
        load_business_models()
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with session_factory.begin() as session:
            line, _, _ = await add_bound_device(
                session, "INPUT", origin, role="INPUT_ARM", line_code="COARSE"
            )
            line.business_code = "coarse_sorting"
            line.device_bindings = {
                "INPUT_ARM": "INPUT",
                "CONVEYOR": "CONVEYOR",
                "OUTPUT_ARM": "OUTPUT",
            }
            line.position_bindings = {"OUTPUT_RACK": "RACK-POINT"}
            session.add(
                WorkLinePosition(work_line_id=line.id, position_code="RACK-POINT", capacity=1)
            )
            for code in ("CONVEYOR", "OUTPUT"):
                route = DeviceRoute(id=str(uuid7()), device_code=code, ecs_origin=origin)
                session.add(route)
                await session.flush()
                session.add(
                    Device(
                        device_code=code,
                        display_name=code,
                        work_line_id=line.id,
                        current_route_id=route.id,
                    )
                )

    async def advance_until(expected):
        async with asyncio.timeout(15):
            while True:
                await advance_pending(session_factory, "coarse_sorting")
                async with session_factory() as session:
                    jobs = (await session.scalars(select(CoarseSortingJob))).all()
                    if phase == "mock":
                        jobs = [job for job in jobs if job.payload["barcode"] != "BEFORE-MOCK"]
                    reasons = {job.payload["barcode"]: job.wait_reason for job in jobs}
                    if reasons == expected:
                        return {job.payload["barcode"]: job for job in jobs}
                await asyncio.sleep(0.05)

    async with AsyncExitStack() as stack:
        if phase == "mock":
            async with engine.begin() as connection:
                await connection.run_sync(CoarseSortingMockRound.__table__.create)

            async def drop_mock_table():
                async with engine.begin() as connection:
                    await connection.run_sync(CoarseSortingMockRound.__table__.drop)

            stack.push_async_callback(drop_mock_table)
            mock_origin = await stack.enter_async_context(loopback_app(app))
            real_lifespan = main.reliable_lifespan

            def client_factory(config):
                if config.destination_key in (
                    COARSE_MOCK_DECISIONS_TARGET,
                    COARSE_MOCK_FACTS_TARGET,
                ):
                    config = replace(config, base_url=mock_origin)
                return AsyncHttpClient(config)

            main.reliable_lifespan = lambda *a, **kw: real_lifespan(
                *a, **kw, client_factory=client_factory
            )
        await stack.enter_async_context(app.router.lifespan_context(app))
        random_mock = phase == "mock" and isinstance(
            app.state.coarse_sorting_mock_round, RandomMockRound
        )
        if random_mock:
            from app.businesses.coarse_sorting import mock_wms

            draws = iter((1, 0, 1))
            mock_wms.randbelow = lambda _: next(draws)
        normal, ng, full = (
            (
                f"PTEST-{kind},Q2000,MMAKER,D2610,LLOT,SREEL-{kind}"
                for kind in ("NORMAL", "NG", "FULL")
            )
            if random_mock
            else ("NORMAL", "NG", "FULL")
        )
        client = await stack.enter_async_context(
            AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
        )

        async def complete_commands(results):
            async with asyncio.timeout(12):
                while True:
                    async with session_factory() as session:
                        commands = (
                            await session.scalars(
                                select(IntegrationExchange)
                                .where(IntegrationExchange.role == "ECS_COMMAND")
                                .where(
                                    IntegrationExchange.task_type.in_(results),
                                    IntegrationExchange.delivery_status == "DONE",
                                )
                            )
                        ).all()
                    if {command.task_type for command in commands} == results.keys():
                        break
                    await asyncio.sleep(0.05)
            for command in commands:
                response = await client.post(
                    "/api/v1/callback/result",
                    json={
                        "command_code": command.command_code,
                        "device_code": command.device_code,
                        "result": "SUCCESS",
                        "finish_time": 2,
                        "data": results[command.task_type],
                    },
                )
                assert response.status_code == 200, response.text

        if phase == "mock":
            probe = {
                "device_code": "INPUT",
                "event_type": "SCAN_COMPLETED",
                "timestamp": 1,
                "data": {"barcode": "NORMAL", "size_inch": 7, "thickness_mm": 20},
            }
            app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=999, roles=())
            debug = await client.post("/api/v1/dev/ecs/events", json=probe)
            assert debug.json()["status_code"] == 403
            untrusted_client = await stack.enter_async_context(
                AsyncClient(
                    transport=ASGITransport(app=app, client=("192.0.2.25", 1234)),
                    base_url="http://test",
                )
            )
            untrusted = await untrusted_client.post(
                "/api/v1/callback/event", json=probe, headers={"x-forwarded-for": "127.0.0.1"}
            )
            assert untrusted.status_code == 403
            async with session_factory() as session:
                assert await session.scalar(select(CoarseSortingJob.id)) is None

            runtime = app.state.business_runtime
            app.state.business_runtime = replace(
                runtime,
                services={
                    **runtime.services,
                    "coarse_sorting": replace(
                        runtime.services["coarse_sorting"], mock_workline=None, mock_round_id=None
                    ),
                },
            )
            try:
                queued = await client.post(
                    "/api/v1/callback/event",
                    json=probe | {"data": probe["data"] | {"barcode": "BEFORE-MOCK"}},
                )
                assert queued.status_code == 200
            finally:
                app.state.business_runtime = runtime
            await advance_pending(session_factory, "coarse_sorting")
            async with session_factory() as session:
                queued_job = (await session.scalars(select(CoarseSortingJob))).one()
                assert queued_job.validation_operation_id is None
                assert queued_job.wait_reason == "MOCK_ROUND_RECONCILIATION_REQUIRED"

        if phase in ("first", "mock"):
            for timestamp, barcode in enumerate((normal, ng), start=1):
                response = await client.post(
                    "/api/v1/callback/event",
                    json={
                        "device_code": "INPUT",
                        "event_type": "SCAN_COMPLETED",
                        "timestamp": timestamp,
                        "data": {"barcode": barcode, "size_inch": 7, "thickness_mm": 20},
                    },
                )
                assert response.status_code == 200, response.text
                if random_mock and barcode == normal:
                    await advance_until({normal: "AWAITING_MOVE_RESULT"})
            if phase == "mock":
                await advance_until({normal: "AWAITING_MOVE_RESULT", ng: "AWAITING_NG_RESULT"})
                await complete_commands({"MOVE_FORWARD": {"actual_qty": 1}})
                await advance_until({normal: "AWAITING_PUT_RESULT", ng: "AWAITING_NG_RESULT"})
                await complete_commands(
                    {
                        "PUT_TO_BIN": {"actual_qty": 1, "location": "OUTPUT-POINT"},
                        "MOVE_TO_NG": {"actual_qty": 1, "location": "NG-POINT"},
                    }
                )
                expected = {normal: "COMPLETED", ng: "NG_COMPLETED"}
                if random_mock:
                    await advance_until(expected)
                    response = await client.post(
                        "/api/v1/callback/event",
                        json=probe | {"timestamp": 3, "data": probe["data"] | {"barcode": full}},
                    )
                    assert response.status_code == 200, response.text
                    expected[full] = "VALIDATION_RECONCILIATION_REQUIRED"
            else:
                await advance_until({"NORMAL": "AWAITING_MOVE_RESULT", "NG": "AWAITING_NG_STAGE"})
                await complete_commands({"MOVE_FORWARD": {"actual_qty": 1}})
                expected = {"NORMAL": "AWAITING_ALLOCATION_STAGE", "NG": "AWAITING_NG_STAGE"}
        elif phase == "blocked":
            app.dependency_overrides[get_current_user] = lambda: None
            binding = {"workline_code": "COARSE", "binding_id": "ORIGINAL-BIND"}
            for operation, data in (
                (ALLOCATE, binding),
                (
                    REPORT,
                    {
                        **binding,
                        "bin_code": "BIN-1",
                        "bin_location": "B",
                        "cell_id": "CELL-1",
                        "cell_index": 2,
                        "completed_at": 2,
                    },
                ),
            ):
                response = await client.post(
                    "/api/v1/dev/wms/requests", json={"operation": operation, "data": data}
                )
                assert response.status_code == 400, response.text
                payload = lookup(operation).data_model.model_validate(data)
                async with session_factory() as session:
                    with raises(InvalidRequestPayload, match="requires a bound business"):
                        await wms_requests.send(session, payload, operation_id=str(uuid7()))
            expected = {
                "NORMAL": "OUTPUT_ARM_CONFIGURATION_REQUIRED",
                "NG": "NG_PLATFORM_CONFIGURATION_REQUIRED",
            }
        else:
            async with asyncio.timeout(15):
                while True:
                    await advance_pending(session_factory, "coarse_sorting")
                    async with session_factory() as session:
                        tasks = (await session.scalars(select(TransportTask))).all()
                        incoming = next(
                            (
                                task
                                for task in tasks
                                if task.frozen["rack_id"] == "RACK-1" and task.submission_acceptance
                            ),
                            None,
                        )
                        if incoming:
                            assert (
                                await session.scalars(
                                    select(IntegrationExchange)
                                    .where(IntegrationExchange.role == "ECS_COMMAND")
                                    .where(IntegrationExchange.task_type == "PUT_TO_BIN")
                                )
                            ).first() is None
                            break
                    await asyncio.sleep(0.05)
            response = await client.post(
                "/api/v1/wms/events",
                json={
                    "operation": "transport.task.resulted@v1",
                    "operation_id": str(uuid7()),
                    "timestamp": 1,
                    "data": {
                        "transport_task_id": incoming.transport_task_id,
                        "kind": "RACK_MOVE",
                        "outcome_revision": 1,
                        "rack_id": "RACK-1",
                        "status": "SUCCEEDED",
                        "final_position": incoming.frozen["target"],
                        "arrival_face": "90",
                    },
                },
            )
            assert response.status_code == 202, response.text
            await advance_until({"NORMAL": "AWAITING_PUT_RESULT", "NG": "AWAITING_NG_RESULT"})
            await complete_commands(
                {
                    "PUT_TO_BIN": {"actual_qty": 1, "location": "OUTPUT-POINT"},
                    "MOVE_TO_NG": {"actual_qty": 1, "location": "NG-POINT"},
                }
            )
            expected = {"NORMAL": "COMPLETED", "NG": "NG_COMPLETED"}
        jobs = await advance_until(expected)
        if phase == "mock":
            assert jobs[normal].validation_response["target"] == COARSE_MOCK_DECISIONS_TARGET
            assert jobs[ng].validation_response["target"] == COARSE_MOCK_DECISIONS_TARGET
            if random_mock:
                assert jobs[full].command_code is None
            print(json.dumps({barcode: job.state for barcode, job in jobs.items()}))
            return
        async with session_factory() as session:
            actions = (await session.scalars(select(CoarseSortingAction))).all()
        identities = {}
        for barcode, job in jobs.items():
            history = [action for action in actions if action.job_id == job.id]
            chain = {action.kind: action for action in history}
            original = chain["MOVE_FORWARD"] if barcode == "NORMAL" else chain["VALIDATE_BIND"]
            if phase == "resume":
                successor = (
                    next(
                        action
                        for action in history
                        if action.kind == "ALLOCATE_STORAGE" and action.round_no == 1
                    )
                    if barcode == "NORMAL"
                    else chain["MOVE_TO_NG"]
                )
                assert successor.predecessor_action_id == original.id
            else:
                assert set(chain) == (
                    {"VALIDATE_BIND", "MOVE_FORWARD"} if barcode == "NORMAL" else {"VALIDATE_BIND"}
                )
            identities[barcode] = {
                "job_id": job.id,
                "action_id": original.id,
                "validation_operation_id": job.validation_operation_id,
                "command_code": job.command_code,
                "binding_id": job.binding_id,
            }
        print(json.dumps(identities))


if __name__ == "__main__":
    asyncio.run(exercise(*sys.argv[1:]))
