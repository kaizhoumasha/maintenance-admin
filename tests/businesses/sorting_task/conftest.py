import importlib
import sys

import pytest

from app.integrations import discovery
from app.integrations.wms_request import operation


@pytest.fixture
def request_models(monkeypatch):
    # Isolated declarations have no production host yet; never publish them to the catalog.
    monkeypatch.setattr(operation, "_registry", {})
    monkeypatch.setattr(discovery, "validate_late_registration", lambda source: None)
    modules = [
        "app.businesses._components.sorting_task.request.sorting_task_prepare_v1",
        "app.businesses._components.sorting_task.request.sorting_task_completion_confirm_v1",
    ]
    try:
        loaded = [importlib.import_module(name) for name in modules]
        yield {"Prepare": loaded[0].Prepare, "CompletionConfirm": loaded[1].CompletionConfirm}
    finally:
        for name in modules:
            sys.modules.pop(name, None)


@pytest.fixture
async def task_flow(callback_store, monkeypatch):
    """Two independent host stores; the component never owns task state."""
    import json
    from types import SimpleNamespace

    from sqlalchemy import JSON, Column, Integer, MetaData, String, Table, delete, select

    from app.businesses import catalog
    from app.businesses._components.sorting_task.composition import COMPONENT
    from app.businesses._components.sorting_task.ports import TaskOwnerRef
    from app.businesses._components.sorting_task.service import SortingTask
    from app.businesses.definitions import BusinessDefinition
    from app.businesses.runtime import build_runtime
    from app.core.identifiers import uuid7
    from app.integrations.ecs_callback import registry as events
    from app.integrations.wms_callback import Accepted, Conflict, Duplicate, Outcome
    from app.integrations.wms_callback import registry as callbacks
    from app.integrations.wms_callback.receiver import receive
    from app.integrations.wms_request import operation as requests
    from app.models import CallbackReceipt, WorkLine
    from tests.helpers import business_composition

    factory = callback_store
    async with factory() as session:
        engine = session.bind
    metadata = MetaData()
    hosts = {}
    for code, mode in (("first", "MANUAL"), ("second", "AUTO")):
        tasks = Table(
            f"test_sorting_task_{code}",
            metadata,
            Column("task_id", String, primary_key=True),
            Column("task_type", String, nullable=False),
            Column("business_type", String, nullable=False),
            Column("work_line_id", Integer, nullable=False),
            Column("workline_code", String, nullable=False),
        )
        inputs = Table(
            f"test_sorting_input_{code}",
            metadata,
            Column("operation_id", String, primary_key=True),
            Column("task_id", String, nullable=False),
            Column("operation", String, nullable=False),
            Column("payload", JSON, nullable=False),
        )

        class Host:
            def __init__(self, code, mode, tasks, inputs):
                self.code, self.mode, self.tasks, self.inputs = code, mode, tasks, inputs
                self.read_error = None
                self.ref_override = None
                self.outcome = None
                self.guard = None
                self.calls = []
                self.guards = []
                self.rechecks = []

            async def read_task_owner(self, session, *, task_id):
                if self.read_error:
                    raise self.read_error
                row = (
                    (
                        await session.execute(
                            select(self.tasks).where(self.tasks.c.task_id == task_id)
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if row is None:
                    return None
                return self.ref_override or TaskOwnerRef(business_code=self.code, **row)

            async def accept(self, ctx, payload):
                assert ctx.business_code == self.code
                assert ctx.component_key == "sorting_task"
                self.calls.append((ctx.operation, payload.model_dump(mode="json")))
                if await ctx.session.scalar(
                    select(self.inputs.c.operation_id).where(
                        self.inputs.c.operation_id == ctx.operation_id
                    )
                ):
                    return Duplicate()
                if ctx.operation == "sorting.task.issued@v1":
                    if payload.task_type != self.mode:
                        return Conflict({"reason_code": "STATE_CONFLICT"})
                    existing = await self.read_task_owner(ctx.session, task_id=payload.task_id)
                    if existing is not None and existing.workline_code != payload.workline_code:
                        return Conflict({"reason_code": "STATE_CONFLICT"})
                    if existing is None:
                        line_id = await ctx.session.scalar(
                            select(WorkLine.id).where(WorkLine.line_code == payload.workline_code)
                        )
                        await ctx.session.execute(
                            self.tasks.insert().values(
                                task_id=payload.task_id,
                                task_type=payload.task_type,
                                business_type=payload.business_type,
                                work_line_id=line_id,
                                workline_code=payload.workline_code,
                            )
                        )
                await ctx.session.execute(
                    self.inputs.insert().values(
                        operation_id=ctx.operation_id,
                        task_id=payload.task_id,
                        operation=ctx.operation,
                        payload=payload.model_dump(mode="json"),
                    )
                )
                if isinstance(self.outcome, Exception):
                    raise self.outcome
                return self.outcome or Accepted()

            accept_task_issued = accept
            accept_task_queue_changed = accept
            accept_task_plan_delta = accept
            accept_task_cancel = accept

            async def recheck_task_plan(self, ctx, payload, original_response):
                assert ctx.business_code == self.code
                self.rechecks.append(original_response)
                return await self.accept(ctx, payload)

        host = Host(code, mode, tasks, inputs)
        if code == "second":

            async def archive_guard(ctx, payload, host=host):
                assert ctx.business_code == host.code
                host.guards.append(ctx.operation)
                if isinstance(host.guard, Exception) and not isinstance(host.guard, Outcome):
                    raise host.guard
                original = await host.read_task_owner(ctx.session, task_id=payload.task_id)
                return host.guard if original and original.business_type == "inbound" else None

            host.archive_guard = archive_guard
        hosts[code] = host
    line_ids = []
    try:
        async with engine.begin() as connection:
            await connection.run_sync(metadata.create_all)

        entries = []
        for code in ("first", "second", "third"):
            entry = business_composition(code)
            entry.DEFINITION = BusinessDefinition(code=code, display_name=code)
            entry.ENTRY_MODULES = ()
            entry.COMPONENTS = (COMPONENT,) if code != "third" else ()
            entry.PROGRESSION = "scan_pending"

            def build(capabilities, code=code):
                async def scan_pending():
                    return 0

                return SimpleNamespace(
                    components={"sorting_task": SortingTask(code, hosts[code])}
                    if code in hosts
                    else {},
                    scan_pending=scan_pending,
                )

            entry.build_service = build
            entries.append(entry)
        monkeypatch.setattr(catalog, "entries", lambda: tuple(entries))
        monkeypatch.setattr(catalog, "_validated_entries", None)
        monkeypatch.setattr(events, "_registry", {})
        monkeypatch.setattr(callbacks, "_registry", {})
        monkeypatch.setattr(requests, "_registry", {})
        for name in COMPONENT.entry_modules:
            if name in sys.modules:
                importlib.reload(sys.modules[name])
            else:
                importlib.import_module(name)
        catalog.validate_business_contracts()
        runtime = build_runtime(outbox=object(), ecs_commands=None, session_factory=factory)
        async with factory() as session, session.begin():
            for number, code in enumerate(("first", "second", "third")):
                line = WorkLine(line_code=f"SORT-{number}", line_name=code, business_code=code)
                session.add(line)
                await session.flush()
                line_ids.append(line.id)

        def message(name, data, operation_id=None):
            return json.dumps(
                dict(
                    operation=f"sorting.task.{name}@v1",
                    operation_id=operation_id or str(uuid7()),
                    timestamp=1,
                    data=data,
                )
            ).encode()

        notices = []

        async def deliver(raw, *, rollback=False):
            async with factory() as session:
                try:
                    response = await receive(
                        session, raw, runtime=runtime, on_business_committed=notices.append
                    )
                    if rollback:
                        await session.rollback()
                    else:
                        await session.commit()
                    return response
                except BaseException:
                    await session.rollback()
                    raise

        async def seed(code="first", task_id="TASK-1", direction="outbound"):
            host = hosts[code]
            async with factory() as session, session.begin():
                await session.execute(
                    host.tasks.insert().values(
                        task_id=task_id,
                        task_type=host.mode,
                        business_type=direction,
                        work_line_id=line_ids[0 if code == "first" else 1],
                        workline_code="SORT-0" if code == "first" else "SORT-1",
                    )
                )

        yield SimpleNamespace(
            factory=factory,
            hosts=hosts,
            runtime=runtime,
            component=COMPONENT,
            message=message,
            deliver=deliver,
            seed=seed,
            notices=notices,
            line_ids=line_ids,
        )
    finally:
        try:
            async with engine.begin() as connection:
                await connection.run_sync(metadata.drop_all)
            async with factory() as session, session.begin():
                await session.execute(
                    delete(CallbackReceipt).where(CallbackReceipt.operation.like("sorting.task.%"))
                )
                await session.execute(delete(WorkLine).where(WorkLine.id.in_(line_ids)))
        finally:
            for name in COMPONENT.entry_modules:
                sys.modules.pop(name, None)
