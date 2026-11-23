"""Disposable tables and real local outbox ports for rack contract tests."""

import json
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace

from sqlalchemy import MetaData, event, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.schema import CreateSchema, DropSchema

from app.businesses._components.rack_dispatch.contracts import (
    Coverage,
    DirectHandoff,
    FactRef,
    JobOrigin,
    PointDemand,
    PointRef,
)
from app.businesses._components.rack_dispatch.models import RackAction, RackJob
from app.businesses._components.rack_dispatch.service import RackDispatch
from app.businesses.runtime import Capabilities
from app.core.identifiers import uuid7
from app.database import SCHEMA, create_database_engine
from app.integrations import history as history_records
from app.integrations.wms_request import WmsRequests
from app.integrations.wms_request.adapter import ADAPTERS as WMS
from app.integrations.wms_transport import ADAPTERS, TransportCaller, TransportService
from app.models import ReliableMessage, TransportTask, WorkLine, WorkLinePosition
from app.reliable import Outbox
from app.reliable.config import TargetConfig


class TaskGrant:
    def __init__(self, task="task"):
        self.task = task

    async def allows_task_entry(self, session, *, work_line_id, task_id):
        return work_line_id == 12 and task_id == self.task


@asynccontextmanager
async def store(url, *, owner="owner"):
    pg = url.startswith("postgres")
    schema = "test_rack_" + uuid7().hex if pg else None
    engine = create_database_engine(
        url, execution_options={"schema_translate_map": {SCHEMA: schema}}
    )
    if not pg:

        @event.listens_for(engine.sync_engine, "connect")
        def configure(connection, record):
            connection.isolation_level = None
            connection.execute("PRAGMA foreign_keys=ON")

        @event.listens_for(engine.sync_engine, "begin")
        def begin(connection):
            connection.exec_driver_sql("BEGIN")

    metadata = MetaData(schema=SCHEMA)
    for model in (RackJob, RackAction, WorkLine, WorkLinePosition, ReliableMessage, TransportTask):
        model.__table__.to_metadata(metadata)
    from tests.helpers import copy_history_tables

    copy_history_tables(metadata)
    try:
        async with engine.begin() as conn:
            if pg:
                await conn.execute(CreateSchema(schema))
            await conn.run_sync(metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory.begin() as session:
            session.add(
                WorkLine(
                    id=12,
                    line_code="LINE",
                    line_name="LINE",
                    business_code=owner,
                    position_bindings={"RETURN_RACK": "P"},
                )
            )
            await session.flush()
            session.add(WorkLinePosition(work_line_id=12, position_code="P", capacity=None))
        targets = {
            k: TargetConfig(k, "http://example.test", k) for k in ("wms-decisions", "wms-transport")
        }
        outbox = Outbox(targets, {**WMS, **ADAPTERS}, records=history_records)

        async def allows_task_entry(session, *, work_line_id, task_id):
            return work_line_id == 12 and task_id == "task"

        yield (
            factory,
            RackDispatch(
                owner,
                Capabilities(
                    None,
                    WmsRequests(
                        outbox,
                        owner_domain=owner,
                        contract_consumers=lambda key: frozenset({owner}),
                    ),
                    TransportService(outbox, owner_domain=owner),
                    factory,
                ),
                task_admission=SimpleNamespace(allows_task_entry=allows_task_entry),
            ),
        )
    finally:
        async with engine.begin() as conn:
            table = RackAction.__table__
            for key in await conn.scalars(select(table.c.id).order_by(table.c.ordinal.desc())):
                await conn.execute(table.delete().where(table.c.id == key))
            await conn.execute(RackJob.__table__.delete())
            await conn.execute(ReliableMessage.__table__.delete())
            await conn.execute(TransportTask.__table__.delete())
            from app.models import IntegrationExchange
            from tests.helpers import clear_integration_history

            await clear_integration_history(
                conn, list(await conn.scalars(select(IntegrationExchange.id)))
            )
            await conn.run_sync(metadata.drop_all)
            if pg:
                await conn.execute(DropSchema(schema))
        await engine.dispose()


def origin(kind="MANUAL_RETURN"):
    return JobOrigin(
        kind,
        "source-1",
        12,
        "rack",
        None if kind == "ROLLER_DRAIN" else "task",
        "LINE",
        "P",
        "90",
        TransportCaller("caller", "station"),
        str(uuid7()),
        {"role": "return"},
    )


def evidence(job, demand="NONE", handoff="CLEAR", version="1"):
    point = PointRef("owner", 12, job, "rack", "P")
    coverage = Coverage(
        "members", ("owner", "12", job, "rack", "P"), FactRef("task", ("task",), (version,)), ()
    )
    return dict(
        demand=PointDemand(point, demand, (coverage,)),
        handoff=DirectHandoff(point, handoff, (replace(coverage, collection="executions"),)),
    )


async def apply_outcome(
    session, action, status, point="P", revision=1, rack_id="rack", arrival_face=None
):
    task = await session.scalar(
        select(TransportTask).where(TransportTask.transport_task_id == action.transport_task_id)
    )
    data = dict(
        transport_task_id=task.transport_task_id,
        kind=task.kind,
        outcome_revision=revision,
        rack_id=rack_id,
        status=status,
        final_position={"kind": "RACK_POSITION", "location_code": point},
        position_unknown=None,
        arrival_face=arrival_face
        if arrival_face is not None
        else None
        if status == "CANCELLED"
        else action.target_face or "90",
        results=None,
        failure_code="RCS_EXECUTION_FAILED" if status == "FAILED" else None,
    )
    task.applied_outcome, task.applied_outcome_revision, task.outcome_status = (
        data,
        revision,
        status,
    )
    await session.flush()


async def respond(session, operation_id, http, code, data):
    terminal = (http, code) in ((409, "CONFLICT"), (422, "REJECTED"))
    from tests.helpers import update_message_bodies

    await update_message_bodies(
        session,
        update(ReliableMessage).where(ReliableMessage.operation_id == operation_id),
        status="FAILED" if terminal else "DONE",
        last_http_status=http,
        last_verdict="PEER_TERMINAL" if terminal else "DELIVERED",
        fail_reason=code if terminal else None,
        response_raw=json.dumps(
            dict(operation_id=operation_id, code=code, timestamp=100, data=data)
        ).encode(),
    )

    await session.flush()
