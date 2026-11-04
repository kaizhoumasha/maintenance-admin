"""Actual Redis broker, worker and one Beat execute the formal manual pending entry."""

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
from sqlalchemy import select, update

from app import business_wakeups
from app.models import DeviceRoute, EcsCommand, ReliableMessage, TransportTask, WorkLine
from app.tasks.celery_app import celery_app
from tests.helpers import manual_departure_obligation, manual_source_departure


async def eventually(read, predicate, *, deadline_seconds=20):
    last = None
    async with asyncio.timeout(deadline_seconds):
        while True:
            last = await read()
            if predicate(last):
                return last
            await asyncio.sleep(0.05)


@pytest.fixture
async def manual_worker(formal_manual, real_redis, tmp_path, monkeypatch):
    flow = formal_manual
    database_url, redis_url = os.environ["TEST_DATABASE_URL"], os.environ["TEST_REDIS_URL"]
    assert database_url.startswith("postgresql+asyncpg://")
    assert str(flow.factory.kw["bind"].url).split("@")[-1] == database_url.split("@")[-1]
    assert urlsplit(flow.origin).hostname == "127.0.0.1"
    assert all(
        target["base_url"] == flow.origin for target in flow.target_config["targets"].values()
    )
    async with flow.factory() as session:
        routes = await session.scalars(
            select(DeviceRoute.ecs_origin).where(DeviceRoute.device_code.like(flow.prefix + "%"))
        )
        assert set(routes) == {flow.origin}
    queue = flow.prefix + "-queue"
    for key, value in dict(
        broker_url=redis_url,
        result_backend=redis_url,
        task_always_eager=False,
        task_default_queue=queue,
        task_routes={"business.advance_pending": {"queue": queue}},
    ).items():
        monkeypatch.setitem(celery_app.conf, key, value)
    # Each owned queue needs a fresh route table even when a prior test already sent tasks.
    monkeypatch.setitem(celery_app.amqp.__dict__, "_rtable", None)
    monkeypatch.setitem(celery_app.amqp.__dict__, "router", celery_app.amqp.Router())
    bootstrap = tmp_path / "m6_celery.py"
    bootstrap.write_text("""import os
from app.tasks.celery_app import celery_app
celery_app.conf.update(task_always_eager=False,
    task_default_queue=os.environ["M6_QUEUE"],
    task_routes={"business.advance_pending": {"queue": os.environ["M6_QUEUE"]}})
celery_app.conf.beat_schedule = {"advance-business-smt_manual":
    celery_app.conf.beat_schedule["advance-business-smt_manual"]}
""")
    env = {
        **os.environ,
        "DATABASE_URL": database_url,
        "REDIS_URL": redis_url,
        "ENVIRONMENT": "production",
        "RELIABLE_TARGETS": json.dumps(flow.target_config),
        "M6_QUEUE": queue,
        "SMT_MANUAL_FIRST_PLAN_WAIT_MS": "60000",
        "PYTHONPATH": str(tmp_path) + os.pathsep + str(Path.cwd()),
    }
    caller = tmp_path / "caller"
    caller.mkdir()
    (caller / ".env").write_text(
        "RELIABLE_TARGETS="
        + json.dumps(
            {
                "targets": {
                    f"foreign-{protocol}": {
                        "base_url": "http://127.0.0.1:1",
                        "protocol": protocol,
                    }
                    for protocol in ("wms-facts", "wms-decisions", "wms-transport")
                }
            }
        )
        + "\n"
    )
    monkeypatch.chdir(caller)
    processes, handles, paths, active_logs = {}, [], {}, {}

    async def start(role):
        assert role not in processes
        path = tmp_path / f"{role}-{len(paths)}.log"
        stream = path.open("w")
        handles.append(stream)
        paths[str(path)] = role
        active_logs[role] = path
        args = [sys.executable, "-m", "celery", "-A", "m6_celery:celery_app", role, "-l", "INFO"]
        if role == "worker":
            args += [
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
            ]
        else:
            args += [
                "--schedule",
                str(tmp_path / "beat-schedule"),
                "--pidfile",
                str(tmp_path / "beat.pid"),
            ]
        # Keep caller dotenv targets out of the worker/Beat's explicit test configuration.
        process = await asyncio.create_subprocess_exec(
            *args, cwd=tmp_path, env=env, stdout=stream, stderr=asyncio.subprocess.STDOUT
        )
        processes[role] = process
        if role == "worker":

            async def ready():
                assert process.returncode is None, path.read_text()
                return path.read_text()

            await eventually(ready, lambda text: " ready." in text)
        return process.pid

    async def stop(role):
        process = processes.pop(role, None)
        if process is None:
            return
        if process.returncode is None:
            process.terminate()
            try:
                async with asyncio.timeout(10):
                    await process.wait()
            except TimeoutError:
                process.kill()
                await process.wait()
        assert process.returncode is not None

    async def wait_idle():
        async def read():
            text = active_logs["worker"].read_text()
            assert "ERROR" not in text, text
            return dict(
                received=text.count("] received"),
                succeeded=text.count("] succeeded"),
                queued=await real_redis.llen(queue),
            )

        return await eventually(
            read,
            lambda row: (
                row["received"] > 0 and row["received"] == row["succeeded"] and row["queued"] == 0
            ),
        )

    async def beat_tick():
        async def read():
            assert processes["beat"].returncode is None
            return active_logs["beat"].read_text()

        return await eventually(
            read,
            lambda text: (
                "Sending due task advance-business-smt_manual (business.advance_pending)" in text
            ),
        )

    async def poll_rows(key):
        await flow.dispatch()
        return await flow.rows(key)

    state = SimpleNamespace(
        flow=flow,
        start=start,
        stop=stop,
        poll_rows=poll_rows,
        paths=paths,
        wait_idle=wait_idle,
        beat_tick=beat_tick,
    )
    try:
        yield state
    finally:
        await stop("beat")
        await stop("worker")
        for stream in handles:
            stream.close()
        await real_redis.delete(queue, "_kombu.binding." + queue)
        print("M6 owned process logs: " + json.dumps(paths))


@pytest.mark.parametrize("formal_manual", [4], indirect=True)
async def test_manual_worker_advances_wait_completion_and_roller_pending(
    manual_worker, monkeypatch
):
    run, flow = manual_worker, manual_worker.flow
    first_pid = await run.start("worker")
    tasks = [flow.prefix + "-in", flow.prefix + "-out"]
    for line, direction in enumerate(("inbound", "outbound")):
        assert (await flow.issue(tasks[line], line=line, direction=direction)).status_code == 202
    await eventually(
        lambda: run.poll_rows("task"),
        lambda rows: len(rows) == 2 and all(r["prepare_accepted"] for r in rows),
    )
    flow.admission = {
        "A000000001": {"result": "WORK_REQUIRED", "task_id": tasks[0]},
        "A000000002": {"result": "WAIT", "retry_after_ms": 2000},
    }
    await flow.scan("SCAN2", "A000000001-C", line=0, timestamp=10)
    await flow.scan("SCAN2", "A000000002-C", line=1, timestamp=20)
    from app.integrations.wms_callback.receiver import receive

    async with flow.factory.begin() as session:
        for operation, data in (
            (
                "sorting.task.plan_delta@v1",
                dict(
                    task_id=tasks[0],
                    plan_revision=1,
                    added_bin_source_racks=[dict(rack_id="WITHDRAWN", rack_face=["A"])],
                ),
            ),
            (
                "sorting.task.cancel@v1",
                dict(
                    task_id=tasks[0],
                    cancel_scope="PLAN_MEMBERS",
                    bin_source_racks=[dict(plan_revision=1, rack_id="WITHDRAWN", rack_face=["A"])],
                ),
            ),
        ):
            response = await receive(
                session,
                flow.envelope(operation, data),
                runtime=flow.runtime,
                on_business_committed=business_wakeups.notify_business_committed,
            )
            assert response.status_code == 202
    works = await eventually(
        lambda: run.poll_rows("manual_bin"),
        lambda rows: len(rows) == 2 and {r["state"] for r in rows} == {"WORK_REQUIRED", "WAIT"},
    )
    waiting = next(r for r in works if r["state"] == "WAIT")
    original_admission = next(
        r for r in await flow.rows("admission") if r["arrival_id"] == waiting["arrival_id"]
    )
    async with flow.factory.begin() as session:
        await session.execute(
            update(WorkLine).where(WorkLine.id == flow.lines[1]).values(is_enabled=False)
        )
    flow.confirmation = {"result": "COMPLETED"}
    assert (
        await flow.callback(
            "manual_bin.work_completed@v1",
            dict(task_id=tasks[0], bin_code="A000000001", result="NORMAL", completed_at=30),
        )
    ).status_code == 202
    await eventually(lambda: flow.rows("confirmation"), lambda rows: len(rows) == 1)
    before_restart = await flow.rows("manual_bin")
    release = next(r for r in before_restart if r["task_id"] == tasks[0])
    assert release["state"] == "RELEASED" and release["command_code"]
    await run.stop("worker")
    cycle, action = await manual_departure_obligation(flow, line=2)
    flow.admission["A000000002"] = {"result": "NO_WORK"}

    def unavailable(business):
        raise ConnectionError("owned notification failure")

    with monkeypatch.context() as missed:
        missed.setattr(business_wakeups, "enqueue_business", unavailable)
        await flow.dispatch()
    async with flow.factory() as session:
        saved_transport = await session.scalar(
            select(TransportTask).where(
                TransportTask.transport_task_id == action["transport_task_id"]
            )
        )
        assert saved_transport.applied_outcome_revision is None
        frozen_transport = dict(saved_transport.frozen)
    second_pid = await run.start("worker")
    assert second_pid != first_pid
    await asyncio.sleep(
        max(0, (original_admission["due_at_ms"] - time.time() * 1000) / 1000) + 0.05
    )
    for _ in range(3):
        business_wakeups.enqueue_business("smt_manual")
    await eventually(
        lambda: flow.rows("task"),
        lambda rows: next(r for r in rows if r["task_id"] == tasks[0])["status"] == "COMPLETED",
    )
    idle_before_beat = await run.wait_idle()
    assert (
        len([r for r in await flow.rows("admission") if r["arrival_id"] == waiting["arrival_id"]])
        == 1
    )
    assert (
        next(r for r in await flow.rows("manual_bin") if r["arrival_id"] == waiting["arrival_id"])[
            "state"
        ]
        == "WAIT"
    )
    with monkeypatch.context() as setup:
        setup.setattr(business_wakeups, "enqueue_business", flow.notices.append)
        fallback_cycle, fallback_action = await manual_departure_obligation(flow, line=3)
    with monkeypatch.context() as missed:
        missed.setattr(business_wakeups, "enqueue_business", unavailable)
        await flow.dispatch()
    assert (
        next(
            r for r in await flow.rows("rack_cycle", roller=True) if r["id"] == fallback_cycle["id"]
        )["status"]
        == "ACTIVE"
    )
    # No enabling notification: only the original five-second Beat can find the due WAIT.
    async with flow.factory.begin() as session:
        await session.execute(
            update(WorkLine).where(WorkLine.id == flow.lines[1]).values(is_enabled=True)
        )
    await run.start("beat")
    await run.beat_tick()
    settled = await eventually(
        lambda: run.poll_rows("manual_bin"),
        lambda rows: all(r["state"] == "RELEASED" for r in rows),
        deadline_seconds=25,
    )
    current = next(r for r in settled if r["arrival_id"] == waiting["arrival_id"])
    assert (current["task_id"], current["bin_code"], current["scanned_at"]) == (
        tasks[1],
        "A000000002",
        20,
    )
    admissions = [
        r for r in await flow.rows("admission") if r["arrival_id"] == waiting["arrival_id"]
    ]
    assert (
        len(admissions) == 2 and admissions[0]["operation_id"] == original_admission["operation_id"]
    )
    assert len(await flow.rows("completion")) == 1
    assert len([r for r in await flow.rows("confirmation") if r["task_id"] == tasks[0]]) == 1
    original_work = next(
        r for r in await flow.rows("manual_bin") if r["arrival_id"] == release["arrival_id"]
    )
    original_releases = [
        r
        for r in await flow.rows("scan2_release", roller=True)
        if r["arrival_id"] == release["arrival_id"]
    ]
    assert original_work["command_code"] == release["command_code"]
    assert len(original_releases) == 1
    assert original_releases[0]["command_code"] == release["command_code"]
    assert not original_releases[0]["pending"]
    cycles = await eventually(
        lambda: flow.rows("rack_cycle", roller=True),
        lambda rows: next(r for r in rows if r["id"] == cycle["id"])["status"] == "RELEASED",
    )
    original_cycle = next(r for r in cycles if r["id"] == cycle["id"])
    assert original_cycle["release_basis"]["transport_task_id"] == action["transport_task_id"]
    recovered = await eventually(
        lambda: flow.rows("rack_cycle", roller=True),
        lambda rows: (
            next(r for r in rows if r["id"] == fallback_cycle["id"])["status"] == "RELEASED"
        ),
    )
    assert (
        next(r for r in recovered if r["id"] == fallback_cycle["id"])["release_basis"][
            "transport_task_id"
        ]
        == fallback_action["transport_task_id"]
    )
    async with flow.factory() as session:
        preserved = await session.scalar(
            select(TransportTask).where(
                TransportTask.transport_task_id == action["transport_task_id"]
            )
        )
        assert preserved.frozen == frozen_transport and preserved.applied_outcome_revision is None
        assert (
            await session.scalar(
                select(EcsCommand.owner_domain).where(
                    EcsCommand.command_code == release["command_code"]
                )
            )
            == "smt_manual"
        )
        assert (
            await session.scalar(
                select(ReliableMessage.status).where(
                    ReliableMessage.id == original_admission["message_id"]
                )
            )
            == "DONE"
        )
    print(
        json.dumps(
            dict(
                worker_pids=[first_pid, second_pid],
                idle_before_beat=idle_before_beat,
                task_ids=tasks,
                wait_arrival=waiting["arrival_id"],
                wait_operations=[r["operation_id"] for r in admissions],
                original_release_arrival=release["arrival_id"],
                original_release_command=release["command_code"],
                transport_id=action["transport_task_id"],
                beat_recovered_transport_id=fallback_action["transport_task_id"],
                transport_outcome_revision=None,
                beat_seconds=5,
            )
        )
    )


async def test_original_submit_owner_wakeup_and_beat_refill_capacity(manual_worker, monkeypatch):
    run, flow = manual_worker, manual_worker.flow
    # Prepare all five demands before starting a worker; no setup notification is queued.
    with monkeypatch.context() as setup:
        setup.setattr(business_wakeups, "enqueue_business", flow.notices.append)
        async with flow.factory.begin() as session:
            await session.execute(
                update(WorkLine)
                .where(WorkLine.id == flow.lines[0])
                .values(
                    five_rack_request_limit=3,
                )
            )
        task = flow.prefix + "-capacity"
        assert (await flow.issue(task)).status_code == 202
        await flow.host.advance("TASK", task)
        await flow.dispatch()
        await flow.host.advance("TASK", task)
        assert (
            await flow.callback(
                "sorting.task.plan_delta@v1",
                dict(
                    task_id=task,
                    plan_revision=1,
                    added_bin_source_racks=[
                        dict(rack_id=f"CAPACITY-{i}", rack_face=["A"]) for i in range(5)
                    ],
                ),
            )
        ).status_code == 202
        await flow.host.scan_pending()
        await flow.dispatch()
        original = [a for a in await flow.rows("rack_action", roller=True) if a["kind"] == "CTU01"]
        assert len(original) == 3
        first_departure = await manual_source_departure(flow, original[0])

    async def entries():
        return [a for a in await flow.rows("rack_action", roller=True) if a["kind"] == "CTU01"]

    await run.start("worker")
    # No manual scan/new callback: the original submit-delivered hook queues the owning business.
    await flow.dispatch()
    await eventually(entries, lambda rows: len(rows) == 4)
    await run.wait_idle()
    await run.stop("worker")
    with monkeypatch.context() as setup:
        setup.setattr(business_wakeups, "enqueue_business", flow.notices.append)
        second_departure = await manual_source_departure(flow, original[1])

    def unavailable(_):
        raise ConnectionError("owned test intentionally drops the post-commit notification")

    with monkeypatch.context() as missed:
        missed.setattr(business_wakeups, "enqueue_business", unavailable)
        await flow.dispatch()
    assert len(await entries()) == 4
    await run.start("worker")
    await run.start("beat")
    await run.beat_tick()
    await eventually(entries, lambda rows: len(rows) == 5, deadline_seconds=25)
    await run.wait_idle()
    await run.stop("beat")
    await run.stop("worker")
    assert len(await entries()) == 5
    async with flow.factory() as session:
        for action in (first_departure, second_departure):
            transport = await session.scalar(
                select(TransportTask).where(
                    TransportTask.transport_task_id == action["transport_task_id"],
                )
            )
            assert transport.applied_outcome is None
    print("worker capacity CTU01: 3 -> owner wake 4 -> missed wake / Beat 5; no CTU03 final Result")
