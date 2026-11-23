"""The actual AUTO composition recovers durable work through Redis and a real worker."""

import asyncio
import json
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from sqlalchemy import delete, select, update

from app.businesses._components.bin_roller.models import TABLES as ROLLER_TABLES
from app.businesses.smt_auto.models import (
    AutoBinWork,
    AutoOutboundDecisionRound,
    AutoTask,
    AutoTaskInput,
    AutoTaskRequest,
)
from app.core.identifiers import uuid7
from app.http_client import DeliveryState, HttpResult
from app.integrations import history as history_records
from app.integrations.wms_request import ADAPTERS, DECISIONS_PROTOCOL
from app.models import (
    Device,
    DeviceRoute,
    IntegrationExchange,
    ReliableMessage,
    WorkLine,
)
from app.reliable.config import TargetConfig
from app.reliable.store import AttemptOutcome, MessageStore
from app.reliable.types import MessageStatus
from tests.helpers import clear_integration_history


async def test_committed_auto_receiver_wakes_worker_and_beat_recovers_original_prepare(
    pg_sessions, real_redis, tmp_path
):
    task_id = str(uuid7())
    operation_id, cancel_id = str(uuid7()), str(uuid7())
    line_code = f"auto-{task_id[-12:]}"
    queue = f"{line_code}-queue"
    device_code, route_id = f"{line_code}-scan2", str(uuid7())
    target = TargetConfig(line_code, "http://127.0.0.1:1", DECISIONS_PROTOCOL)
    async with pg_sessions.begin() as session:
        line = WorkLine(
            line_code=line_code,
            line_name=line_code,
            business_code="smt_auto",
            is_enabled=True,
            device_bindings={"SCAN2": device_code},
        )
        session.add(line)
        await session.flush()
        line_id, line_version = line.id, line.version
        session.add(DeviceRoute(id=route_id, device_code=device_code, ecs_origin=target.base_url))
        await session.flush()
        session.add(
            Device(
                device_code=device_code,
                display_name=device_code,
                work_line_id=line_id,
                current_route_id=route_id,
            )
        )

    (tmp_path / "auto_celery.py").write_text("""import os

from app.tasks.celery_app import celery_app

celery_app.conf.update(task_always_eager=False, task_default_queue=os.environ["AUTO_QUEUE"])
key = "advance-business-smt_auto"
celery_app.conf.beat_schedule = {key: celery_app.conf.beat_schedule[key]}
""")
    env = {
        **os.environ,
        "DATABASE_URL": os.environ["TEST_DATABASE_URL"],
        "REDIS_URL": os.environ["TEST_REDIS_URL"],
        "PYTHONPATH": os.pathsep.join((str(tmp_path), str(Path.cwd()))),
        "ENVIRONMENT": "production",
        "RELIABLE_TARGETS": json.dumps(
            {"targets": {target.name: {"base_url": target.base_url, "protocol": target.protocol}}}
        ),
        "SMT_AUTO_FIRST_PLAN_WAIT_MS": "60000",
        "SMT_AUTO_WORKLINES": json.dumps(
            [
                {
                    "work_line_id": line_id,
                    "work_line_version": line_version,
                    "source_locations": [],
                }
            ]
        ),
        "AUTO_QUEUE": queue,
        "AUTO_DEVICE": device_code,
        "AUTO_RECEIPT": json.dumps(
            {
                "operation": "sorting.task.issued@v1",
                "operation_id": operation_id,
                "timestamp": 1,
                "data": {
                    "task_id": task_id,
                    "task_type": "AUTO",
                    "business_type": "outbound",
                    "workline_code": line_code,
                    "queue_revision": 1,
                    "dispatch_sequence": 1,
                },
            }
        ),
    }

    @asynccontextmanager
    async def process(name, *args):
        path = tmp_path / f"{name}.log"
        with path.open("w") as log:
            child = await asyncio.create_subprocess_exec(
                sys.executable,
                *args,
                cwd=tmp_path,
                env=env,
                stdout=log,
                stderr=asyncio.subprocess.STDOUT,
            )
            try:
                yield child, path
            finally:
                if child.returncode is None:
                    child.terminate()
                    try:
                        await asyncio.wait_for(child.wait(), timeout=8)
                    except TimeoutError:
                        child.kill()
                        await child.wait()

    async def read_until(predicate, *processes):
        async with asyncio.timeout(25):
            while True:
                for child, path in processes:
                    assert child.returncode is None, path.read_text()
                async with pg_sessions() as session:
                    task = await session.get(AutoTask, task_id)
                    original = tuple(
                        await session.scalars(
                            select(AutoTaskRequest).where(AutoTaskRequest.task_id == task_id)
                        )
                    )
                if task and len(original) == 1 and predicate(task, original[0]):
                    return task, original[0]
                await asyncio.sleep(0.05)

    async def settle(message_id, operation_id, status, code, data):
        store = MessageStore(pg_sessions, records=history_records)
        claims = await store.claim(1, {target.name: target})
        assert len(claims) == 1 and claims[0].message_id == message_id
        result = HttpResult(
            DeliveryState.HTTP_RESPONSE_COMPLETE,
            None,
            status,
            (),
            json.dumps(
                {
                    "operation_id": operation_id,
                    "timestamp": 2,
                    "code": code,
                    "data": data,
                }
            ).encode(),
        )
        verdict = ADAPTERS[DECISIONS_PROTOCOL].classify_response(claims[0].message, result)
        assert await store.finish(
            claims[0], AttemptOutcome(MessageStatus.DONE, result=result, verdict=verdict)
        )

    async def driver(name, code):
        async with process(name, "-c", code) as (child, path):
            assert await asyncio.wait_for(child.wait(), timeout=20) == 0, path.read_text()

    async def poll_record(model, condition, predicate, *processes):
        async with asyncio.timeout(25):
            while True:
                for child, path in processes:
                    assert child.returncode is None, path.read_text()
                async with pg_sessions() as session:
                    rows = tuple(await session.scalars(select(model).where(condition)))
                if len(rows) == 1 and predicate(rows[0]):
                    return rows[0]
                await asyncio.sleep(0.05)

    try:
        async with process(
            "receiver",
            "-c",
            """
import os
import auto_celery
from app.business_wakeups import notify_business_committed
from app.integrations.wms_callback.receiver import receive
from app.tasks._db import run_with_factory
from app.tasks.business_tasks import _runtime

async def deliver(factory):
    async with factory.begin() as session:
        response = await receive(
            session, os.environ["AUTO_RECEIPT"].encode(), runtime=_runtime(factory, None),
            on_business_committed=notify_business_committed,
        )
        assert response.status_code == 202, response.body

run_with_factory(os.environ["DATABASE_URL"], deliver)
""",
        ) as (child, path):
            assert await asyncio.wait_for(child.wait(), timeout=20) == 0, path.read_text()
        assert await real_redis.llen(queue) == 1
        worker_args = (
            "-m",
            "celery",
            "-A",
            "auto_celery:celery_app",
            "worker",
            "-l",
            "INFO",
            "-P",
            "solo",
            "-c",
            "1",
            "-Q",
            queue,
            "-n",
            queue + "@localhost",
            "--without-gossip",
            "--without-mingle",
            "--without-heartbeat",
        )
        async with process("worker", *worker_args) as worker:
            _, original = await read_until(lambda task, _: task.status == "PREPARING", worker)
        await settle(original.message_id, original.operation_id, 202, "PREPARE_ACCEPTED", {})
        assert await real_redis.llen(queue) == 0
        async with (
            process("worker-restarted", *worker_args) as worker,
            process(
                "beat",
                "-m",
                "celery",
                "-A",
                "auto_celery:celery_app",
                "beat",
                "-l",
                "INFO",
                "--schedule",
                str(tmp_path / "beat"),
                "--pidfile",
                str(tmp_path / "beat.pid"),
            ) as beat,
        ):
            task, recovered = await read_until(
                lambda task, request: (
                    task.prepare_accepted_at_ms is not None and not request.unresolved
                ),
                worker,
                beat,
            )
            await driver(
                "scan2",
                """
import asyncio
import json
import os

import auto_celery
from sqlalchemy import select

from app.business_wakeups import notify_business_committed
from app.businesses import catalog
from app.businesses.smt_auto.models import AutoBinWork
from app.integrations.ecs_callback.receiver import receive_ecs_event
from app.services.device_bindings import resolve_binding
from app.tasks._db import run_with_factory
from app.tasks.business_tasks import _runtime

async def deliver(factory):
    runtime = _runtime(factory, "smt_auto")
    async with factory.begin() as session:
        await session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
        response = await receive_ecs_event(
            session, json.dumps({
                "device_code": os.environ["AUTO_DEVICE"], "event_type": "SCAN_COMPLETED",
                "timestamp": 3, "data": {"bin_code": "A000000001-C"},
            }).encode(),
            resolve_binding=resolve_binding, resolve_input_target=catalog.input_target,
            runtime=runtime, on_business_committed=notify_business_committed,
        )
        assert response.status_code == 200, response.body
        work_id = await session.scalar(
            select(AutoBinWork.id).where(AutoBinWork.device_code == os.environ["AUTO_DEVICE"])
        )
        assert work_id is not None
    host = runtime.services["smt_auto"]
    await asyncio.gather(host.advance("BIN", work_id), host.advance("BIN", work_id))

run_with_factory(os.environ["DATABASE_URL"], deliver)
""",
            )
            decision = await poll_record(
                AutoOutboundDecisionRound,
                AutoOutboundDecisionRound.task_id == task_id,
                lambda record: record.operation == "outbound.bin.work_plan@v1",
                worker,
                beat,
            )
            await settle(
                decision.message_id, decision.operation_id, 200, "DECIDED", {"result": "NO_WORK"}
            )
            work = await poll_record(
                AutoBinWork,
                AutoBinWork.task_id == task_id,
                lambda record: record.release_command_code is not None,
                worker,
                beat,
            )
            env["AUTO_CANCEL"] = json.dumps(
                {
                    "operation": "sorting.task.cancel@v1",
                    "operation_id": cancel_id,
                    "timestamp": 4,
                    "data": {"task_id": task_id, "cancel_scope": "TASK"},
                }
            )
            await driver(
                "cancel",
                """
import os
import auto_celery
from app.business_wakeups import notify_business_committed
from app.integrations.wms_callback.receiver import receive
from app.tasks._db import run_with_factory
from app.tasks.business_tasks import _runtime

async def deliver(factory):
    async with factory.begin() as session:
        response = await receive(
            session, os.environ["AUTO_CANCEL"].encode(), runtime=_runtime(factory, None),
            on_business_committed=notify_business_committed,
        )
        assert response.status_code == 202, response.body

run_with_factory(os.environ["DATABASE_URL"], deliver)
""",
            )
            env["AUTO_COMMAND"] = work.release_command_code
            await driver(
                "scan2-result",
                """
import json
import os

import auto_celery
from app.business_wakeups import ecs_result_committed
from app.integrations.ecs_command.protocol import PROTOCOL
from app.integrations.ecs_command.result import receive_ecs_result
from app.reliable.store import MessageStore
from app.tasks._db import run_with_factory
from app.tasks.business_tasks import _runtime

async def deliver(factory):
    runtime = _runtime(factory, "smt_auto")
    host = runtime.services["smt_auto"]
    commands = host.capabilities.ecs
    outbox = commands.outbox
    target = outbox.targets[commands.target]
    from app.integrations import history as history_records
    store = MessageStore(
        factory, lifecycles={PROTOCOL: outbox.lifecycles[PROTOCOL]}, records=history_records
    )
    claims = await store.claim(1, {target.name: target})
    assert len(claims) == 1 and claims[0].message.business_key == os.environ["AUTO_COMMAND"]
    assert await store.before_send(claims[0], target)
    async with factory.begin() as session:
        response = await receive_ecs_result(session, json.dumps({
            "command_code": os.environ["AUTO_COMMAND"], "device_code": os.environ["AUTO_DEVICE"],
            "result": "SUCCESS", "finish_time": 4, "data": {},
        }).encode(), commands, on_result=ecs_result_committed)
        assert response.status_code == 200, response.body

run_with_factory(os.environ["DATABASE_URL"], deliver)
""",
            )
            recovered_work = await poll_record(
                AutoBinWork,
                AutoBinWork.task_id == task_id,
                lambda record: record.release_consumed_at_ms is not None,
                worker,
                beat,
            )
            assert recovered_work.arrival_id == work.arrival_id
            assert recovered_work.release_command_code == work.release_command_code
            assert recovered_work.release_result_ref and not recovered_work.pending
        assert recovered.id == original.id
        assert (recovered.operation_id, recovered.message_id) == (
            original.operation_id,
            original.message_id,
        )
        assert task.first_plan_deadline_ms == task.prepare_accepted_at_ms + 60000
    finally:
        async with pg_sessions.begin() as session:
            message_ids = select(AutoTaskRequest.message_id).where(
                AutoTaskRequest.task_id == task_id
            )
            decision_ids = select(AutoOutboundDecisionRound.message_id).where(
                AutoOutboundDecisionRound.task_id == task_id
            )
            command_codes = (
                select(IntegrationExchange.command_code)
                .where(IntegrationExchange.role == "ECS_COMMAND")
                .where(IntegrationExchange.device_code == device_code)
            )
            history_ids = list(
                await session.scalars(
                    select(IntegrationExchange.id).where(
                        (IntegrationExchange.device_code == device_code)
                        | IntegrationExchange.operation_id.in_((operation_id, cancel_id))
                        | IntegrationExchange.id.in_(
                            select(ReliableMessage.exchange_id).where(
                                ReliableMessage.id.in_(message_ids)
                                | ReliableMessage.id.in_(decision_ids)
                                | ReliableMessage.business_key.in_(command_codes)
                            )
                        )
                    )
                )
            )
            await session.execute(
                delete(ReliableMessage).where(
                    ReliableMessage.id.in_(decision_ids)
                    | ReliableMessage.business_key.in_(command_codes)
                )
            )
            await session.execute(
                delete(AutoOutboundDecisionRound).where(
                    AutoOutboundDecisionRound.task_id == task_id
                )
            )
            await session.execute(delete(AutoBinWork).where(AutoBinWork.task_id == task_id))
            for key in ("bin_passage", "workline_state"):
                table = ROLLER_TABLES[key]
                await session.execute(delete(table).where(table.c.work_line_id == line_id))
            await session.execute(
                update(IntegrationExchange)
                .where(
                    IntegrationExchange.role == "RECEIPT",
                    IntegrationExchange.receipt_active.is_(True),
                )
                .values(receipt_active=False)
                .where(IntegrationExchange.device_code == device_code)
            )
            await session.execute(delete(Device).where(Device.device_code == device_code))
            await session.execute(delete(DeviceRoute).where(DeviceRoute.id == route_id))
            await session.execute(
                delete(ReliableMessage).where(ReliableMessage.id.in_(message_ids))
            )
            await session.execute(delete(AutoTaskRequest).where(AutoTaskRequest.task_id == task_id))
            await session.execute(delete(AutoTaskInput).where(AutoTaskInput.task_id == task_id))
            await session.execute(delete(AutoTask).where(AutoTask.task_id == task_id))
            await session.execute(
                update(IntegrationExchange)
                .where(
                    IntegrationExchange.role == "RECEIPT",
                    IntegrationExchange.receipt_active.is_(True),
                )
                .values(receipt_active=False)
                .where(IntegrationExchange.operation_id.in_((operation_id, cancel_id)))
            )
            await clear_integration_history(session, history_ids)
            await session.execute(delete(WorkLine).where(WorkLine.id == line_id))
        await real_redis.delete(queue)
