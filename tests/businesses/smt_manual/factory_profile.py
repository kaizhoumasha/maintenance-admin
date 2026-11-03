"""Supplied factory profile, formal runtime, and loopback peer for acceptance tests."""

import json
from contextlib import asynccontextmanager
from pathlib import Path

from tests.helpers import loopback_app, simulator_client_factory

PROFILE = json.loads((Path(__file__).parent / "factory_source_profile.json").read_text())
OWNED_CODES = [d["device_code"] for line in PROFILE["lines"] for d in line["devices"]]


@asynccontextmanager
async def profile_fixture(factory, monkeypatch, evidence_dir, evidence_name):
    """Original formal runtime assembly, supplied codes, and an unlimited loopback peer."""
    import asyncio
    import json
    from types import SimpleNamespace

    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse
    from sqlalchemy import and_, delete, select

    from app import business_wakeups
    from app.businesses import catalog
    from app.businesses._components.bin_roller.models import TABLES as ROLLER_TABLES
    from app.businesses.smt_manual.models import TABLES
    from app.core import get_settings
    from app.core.identifiers import uuid7
    from app.http_client import AsyncHttpClient
    from app.integrations.ecs_callback.receiver import receive_ecs_event
    from app.integrations.ecs_command.clients import DynamicClients
    from app.integrations.ecs_command.result import receive_ecs_result
    from app.integrations.wms_callback.receiver import receive
    from app.models import (
        CallbackReceipt,
        Device,
        DeviceRoute,
        EcsCommand,
        EcsResultEvidence,
        EcsSendEvidence,
        ReliableMessage,
        TransportTask,
        WorkLine,
    )
    from app.reliable.sender import Sender
    from app.reliable.store import MessageStore
    from app.services.device_bindings import resolve_binding
    from app.tasks.business_tasks import _runtime

    prefix = "m6-" + uuid7().hex
    state = SimpleNamespace(
        factory=factory,
        prefix=prefix,
        lines=[],
        devices=[],
        positions=[],
        receipts=set(),
        requests=[],
        notices=[],
        admission={},
        transport_code="RECEIVED",
        transport_http=202,
        confirmation={"result": "BUSINESS_IN_PROGRESS", "retry_after_ms": 60000},
    )
    tables = {
        **{("host", k): v for k, v in TABLES.items()},
        **{("roller", k): v for k, v in ROLLER_TABLES.items()},
    }
    async with factory() as session:
        before = {
            key: set((await session.execute(select(*table.primary_key.columns))).all())
            for key, table in tables.items()
        }
    peer = FastAPI()

    @peer.post("/{path:path}")
    async def reply(path: str, request: Request):
        body = await request.json()
        state.requests.append((path, body))
        operation = body.get("operation")
        if path == "api/v1/device/command":
            return {"code": 200, "message": "ACK"}
        if operation == "transport.task.submit@v1":
            return JSONResponse(
                dict(
                    operation_id=body["operation_id"],
                    code=state.transport_code,
                    timestamp=1,
                    data={"transport_task_id": body["data"]["transport_task_id"]},
                ),
                status_code=state.transport_http,
            )
        code, status, data = "DECIDED", 200, {}
        if operation == "sorting.task.prepare@v1":
            code, status = "PREPARE_ACCEPTED", 202
        elif operation == "manual_bin.work_admission_decide@v1":
            data = state.admission.get(body["data"]["bin_code"], {"result": "NO_WORK"})
        elif operation == "sorting.task.completion_confirm@v1":
            data = state.confirmation
        elif operation == "rack.departure_decide@v1":
            data = getattr(state, "departure_decision", {"result": "WAIT", "retry_after_ms": 60000})
        elif operation == "outbound.bin.return_batch@v1":
            data = getattr(state, "return_decision", {"result": "NO_BATCH"})
        elif operation == "outbound.bin.inbound_batch@v1":
            data = getattr(state, "inbound_decision", {"result": "RACK_FACE_DONE"})
        elif operation == "workline.return_buffer.drain_rack_decide@v1":
            data = {
                "result": "WAIT",
                "reason_code": "NO_DRAIN_RACK_AVAILABLE",
                "retry_after_ms": 60000,
            }
        elif path.endswith("facts"):
            code = "RECORDED"
        return JSONResponse(
            dict(operation_id=body["operation_id"], code=code, timestamp=1, data=data),
            status_code=status,
        )

    async with loopback_app(peer) as origin:
        state.origin = origin
        targets = {
            prefix + "-" + protocol: {"base_url": origin, "protocol": protocol}
            for protocol in ("wms-facts", "wms-decisions", "wms-transport")
        }
        state.target_config = {"targets": targets}
        monkeypatch.setattr(get_settings(), "RELIABLE_TARGETS", state.target_config)
        state.runtime = _runtime(factory)
        state.host = state.runtime.services["smt_manual"]
        state.roller = state.host.components["bin_roller"]
        commands = state.host.capabilities.ecs
        outbox = commands.outbox
        state.store = MessageStore(
            factory,
            lifecycles=outbox.lifecycles,
            on_result=business_wakeups.wms_result_committed,
            on_delivered=business_wakeups.transport_submit_delivered,
        )
        sender = Sender(
            state.store,
            outbox.targets,
            outbox.adapters,
            {
                name: AsyncHttpClient(target.http_config())
                for name, target in outbox.targets.items()
                if target.address_mode == "STATIC"
            },
            provider=DynamicClients(factory=simulator_client_factory([origin])),
        )
        state.sender = sender
        async with factory.begin() as session:
            for profile in PROFILE["lines"]:
                devices = {d["role"]: d["device_code"] for d in profile["devices"] if d["role"]}
                positions = {p["role"]: p["code"] for p in profile["positions"]}
                line = WorkLine(
                    line_code=profile["code"],
                    line_name=profile["code"],
                    business_code="smt_manual",
                    is_enabled=profile["enabled"],
                    five_rack_request_limit=next(
                        p["capacity"] for p in profile["positions"] if p["role"] == "FIVE_RACK"
                    ),
                    version=profile["version"],
                    device_bindings=devices,
                    position_bindings=positions,
                )
                session.add(line)
                await session.flush()
                state.lines.append(line.id)
                state.devices.append(devices)
                state.positions.append(positions)
                for device in profile["devices"]:
                    code = device["device_code"]
                    route = DeviceRoute(id=str(uuid7()), device_code=code, ecs_origin=origin)
                    session.add(route)
                    await session.flush()
                    session.add(
                        Device(
                            device_code=code,
                            display_name=device.get("display_name", code),
                            work_line_id=line.id,
                            current_route_id=route.id,
                        )
                    )
        # Validate every active route/target before callbacks can freeze an intent.
        from urllib.parse import urlsplit

        assert urlsplit(origin).hostname == "127.0.0.1"
        assert {target["base_url"] for target in targets.values()} == {origin}
        async with factory() as session:
            assert set(await session.scalars(select(DeviceRoute.ecs_origin))) == {origin}
        state.source_mapping = {
            "checked_at": PROFILE["checked_at"],
            "origin": {"source": PROFILE["endpoint_base_url"], "test": origin},
            "lines": [
                {
                    "source_id": p["source_work_line_id"],
                    "local_id": line_id,
                    "code": p["code"],
                    "version": p["version"],
                }
                for p, line_id in zip(PROFILE["lines"], state.lines, strict=True)
            ],
        }

        def envelope(operation, data, *, identity=None, timestamp=100):
            identity = identity or str(uuid7())
            state.receipts.add(identity)
            return json.dumps(
                dict(operation=operation, operation_id=identity, timestamp=timestamp, data=data)
            ).encode()

        async def callback(operation, data, *, rollback=False, before_commit=None, identity=None):
            async with factory() as session:
                response = await receive(
                    session,
                    envelope(operation, data, identity=identity),
                    runtime=state.runtime,
                    on_business_committed=business_wakeups.notify_business_committed,
                    on_owner_committed=business_wakeups.notify_business_committed,
                )
                if before_commit:
                    await before_commit()
                await session.rollback() if rollback else await session.commit()
                return response

        async def issue(task, *, line=0, direction="inbound"):
            return await callback(
                "sorting.task.issued@v1",
                dict(
                    task_id=task,
                    task_type="MANUAL",
                    business_type=direction,
                    workline_code=PROFILE["lines"][line]["code"],
                    queue_revision=1,
                    dispatch_sequence=1,
                ),
            )

        async def scan(point, raw="A000000001-C", *, line=0, timestamp=10, rollback=False):
            async with factory() as session:
                response = await receive_ecs_event(
                    session,
                    json.dumps(
                        dict(
                            device_code=state.devices[line][point],
                            event_type="SCAN_COMPLETED",
                            timestamp=timestamp,
                            data={"bin_code": raw},
                        )
                    ).encode(),
                    resolve_binding=resolve_binding,
                    resolve_input_target=catalog.input_target,
                    runtime=state.runtime,
                    on_business_committed=business_wakeups.notify_business_committed,
                )
                state.receipts.add(response.digest)
                await session.rollback() if rollback else await session.commit()
                return response

        async def dispatch():
            async with factory() as session:
                assert set(await session.scalars(select(DeviceRoute.ecs_origin))) == {origin}
            assert all(t["base_url"] == origin for t in state.target_config["targets"].values())
            total = 0
            for _ in range(100):
                count = await sender.tick(maintain=False)
                if sender.inflight:
                    await asyncio.gather(*tuple(sender.inflight))
                total += count
                if not count:
                    return total
            raise AssertionError("loopback sender did not drain its bounded fixture work")

        async def rows(key, *, roller=False):
            if not roller and key in {"rack", "rack_action", "rack_departure"}:
                from tests.helpers import manual_dispatch_rows

                async with factory() as session:
                    return await manual_dispatch_rows(
                        session, state.roller.rack_dispatch, key, state.lines
                    )

            if roller and key in ("rack_cycle", "rack_action", "departure"):
                from tests.helpers import roller_dispatch_rows

                async with factory() as session:
                    records = [
                        r
                        for line in state.lines
                        for r in await roller_dispatch_rows(session, state.roller, key, line)
                    ]
                    return sorted(records, key=lambda r: r["id"])
            table = ROLLER_TABLES[key] if roller else TABLES[key]
            async with factory() as session:
                return [
                    dict(row)
                    for row in (
                        await session.execute(select(table).order_by(*table.primary_key.columns))
                    ).mappings()
                ]

        async def result(code, *, status="SUCCESS", rollback=False, before_commit=None):
            async with factory() as session:
                command = await session.scalar(
                    select(EcsCommand).where(EcsCommand.command_code == code)
                )
                raw = json.dumps(
                    dict(
                        command_code=code,
                        device_code=command.device_code,
                        result=status,
                        finish_time=1,
                        data={"actual_qty": 1},
                        error_detail={"code": "DEVICE_FAILURE", "msg": "local mock failure"}
                        if status == "FAILED"
                        else None,
                    )
                ).encode()
                response = await receive_ecs_result(
                    session, raw, commands, on_result=business_wakeups.ecs_result_committed
                )
                if before_commit:
                    await before_commit()
                await session.rollback() if rollback else await session.commit()
                return response

        state.callback, state.issue, state.scan = callback, issue, scan
        state.dispatch, state.rows, state.result, state.envelope = dispatch, rows, result, envelope
        try:
            yield state
        finally:
            # Evidence is retained before cleanup even when a capacity assertion fails.
            evidence = {
                "mapping": state.source_mapping,
                "requests": state.requests,
                "observations": getattr(state, "observations", {}),
            }
            async with factory() as session:
                from dataclasses import asdict

                from app.businesses._components.bin_roller.service import ROLLER_SOURCES

                evidence["rack_dispatch/jobs"] = [
                    asdict(j)
                    for line in state.lines
                    for j in await state.roller.rack_dispatch.jobs(
                        session, work_line_id=line, source_kinds=ROLLER_SOURCES
                    )
                ]
                for key, table in tables.items():
                    evidence["/".join(key)] = [
                        dict(r) for r in (await session.execute(select(table))).mappings()
                    ]
                for model in (
                    WorkLine,
                    Device,
                    DeviceRoute,
                    EcsCommand,
                    ReliableMessage,
                    TransportTask,
                    CallbackReceipt,
                    EcsResultEvidence,
                    EcsSendEvidence,
                ):
                    evidence[model.__tablename__] = [
                        dict(r) for r in (await session.execute(select(model.__table__))).mappings()
                    ]
            (evidence_dir / (evidence_name + ".evidence.json")).write_text(
                json.dumps(
                    evidence,
                    ensure_ascii=False,
                    indent=2,
                    default=lambda v: v.decode() if isinstance(v, bytes) else str(v),
                )
            )
            await sender.aclose()
            async with factory.begin() as session:
                codes = select(EcsCommand.command_code).where(
                    EcsCommand.device_code.in_(OWNED_CODES)
                )
                await session.execute(
                    delete(EcsResultEvidence).where(EcsResultEvidence.command_code.in_(codes))
                )
                await session.execute(
                    delete(EcsSendEvidence).where(EcsSendEvidence.command_code.in_(codes))
                )
                await session.execute(
                    delete(ReliableMessage).where(ReliableMessage.operation_id.in_(codes))
                )
                await session.execute(
                    delete(EcsCommand).where(EcsCommand.device_code.in_(OWNED_CODES))
                )
                await session.execute(
                    delete(TransportTask).where(
                        TransportTask.operation_id.in_(
                            select(ReliableMessage.operation_id).where(
                                ReliableMessage.target.in_(targets)
                            )
                        )
                    )
                )
                await session.execute(
                    delete(ReliableMessage).where(ReliableMessage.target.in_(targets))
                )
                await session.execute(
                    delete(CallbackReceipt).where(
                        (CallbackReceipt.operation_id.in_(state.receipts))
                        | CallbackReceipt.device_code.in_(OWNED_CODES)
                    )
                )
                from tests.helpers import clear_test_rack_jobs

                await clear_test_rack_jobs(session, state.lines)
                for key, table in reversed(tuple(tables.items())):
                    current = (await session.execute(select(*table.primary_key.columns))).all()
                    for row in set(current) - before[key]:
                        await session.execute(
                            table.delete().where(
                                and_(
                                    *(
                                        column == value
                                        for column, value in zip(
                                            table.primary_key.columns, row, strict=True
                                        )
                                    )
                                )
                            )
                        )
                await session.execute(delete(Device).where(Device.device_code.in_(OWNED_CODES)))
                await session.execute(
                    delete(DeviceRoute).where(DeviceRoute.device_code.in_(OWNED_CODES))
                )
                await session.execute(delete(WorkLine).where(WorkLine.id.in_(state.lines)))
            async with factory() as session:
                counts = {
                    "/".join(key): await session.scalar(
                        select(__import__("sqlalchemy").func.count()).select_from(table)
                    )
                    for key, table in tables.items()
                }
                counts.update(
                    {
                        model.__tablename__: await session.scalar(
                            select(__import__("sqlalchemy").func.count()).select_from(model)
                        )
                        for model in (
                            WorkLine,
                            Device,
                            DeviceRoute,
                            EcsCommand,
                            ReliableMessage,
                            TransportTask,
                            CallbackReceipt,
                            EcsResultEvidence,
                            EcsSendEvidence,
                        )
                    }
                )
            (evidence_dir / (evidence_name + ".cleanup.json")).write_text(
                json.dumps(counts, indent=2)
            )
            assert all(n == 0 for n in counts.values()), counts
