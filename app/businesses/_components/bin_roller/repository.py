"""Current checkpoints; writes stay inside the receiver/host transaction."""

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime

from sqlalchemy import Table, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.core.identifiers import uuid7

from .models import TABLES
from .ports import SourceFaceRef, SupplyChange, SupplyProgress


class RollerRepository:
    def __init__(
        self,
        owner_domain,
        work_line_id,
        *,
        rack_dispatch,
        tables: Mapping[str, Table] | None = None,
    ):
        self.owner_domain, self.work_line_id = owner_domain, work_line_id
        self.rack_dispatch = rack_dispatch
        self.tables = TABLES if tables is None else tables

    @property
    def scope(self):
        return dict(owner_domain=self.owner_domain, work_line_id=self.work_line_id)

    def scoped(self, table):
        return table.c.owner_domain == self.owner_domain, table.c.work_line_id == self.work_line_id

    async def _one(self, session, table, *conditions, lock=False):
        query = select(table).where(*self.scoped(table), *conditions)
        if lock:
            query = query.with_for_update()
        return (await session.execute(query)).mappings().one_or_none()

    async def _register(self, session, table, values, keys: Sequence[str], compare: Sequence[str]):
        insert = pg_insert if session.get_bind().dialect.name == "postgresql" else sqlite_insert
        await session.execute(insert(table).values(**values).on_conflict_do_nothing())
        row = await self._one(session, table, *(table.c[key] == values[key] for key in keys))
        if row is None or any(row[key] != values[key] for key in compare):
            raise ValueError("original business identity has conflicting input")
        return row

    async def update(self, session, key, record_id=None, **values):
        table = self.tables[key]
        conditions = self.scoped(table)
        if record_id is not None:
            conditions += (table.c.id == record_id,)
        await session.execute(
            table.update()
            .where(*conditions)
            .values(
                **values,
                revision=table.c.revision + 1,
                updated_at=datetime.now(UTC),
            )
        )

    async def close(self, session, key, record_id, reason):
        table = self.tables[key]
        await session.execute(
            table.update()
            .where(*self.scoped(table), table.c.id == record_id, table.c.closed_at.is_(None))
            .values(
                closed_at=datetime.now(UTC),
                close_reason=reason,
                revision=table.c.revision + 1,
                updated_at=datetime.now(UTC),
            )
        )

    async def close_retired_return(self, session, identity):
        table = self.tables["bin_return"]
        row = await self._one(session, table, table.c.id == identity)
        if (
            row
            and row["closed_at"] is None
            and not row["is_current"]
            and row["selected_face_work_id"] is None
            and row["transport_task_id"] is None
            and all(
                row[point + "_command_code"] is None
                or row[point + "_consumed_result_id"] is not None
                for point in ("scan3", "scan4")
            )
        ):
            await self.close(session, "bin_return", identity, "SUPERSEDED")

    async def line_state(self, session, *, start=False):
        table = self.tables["workline_state"]
        row = await self._register(
            session,
            table,
            dict(**self.scope, drain_phase="IDLE"),
            ("owner_domain", "work_line_id"),
            (),
        )
        if start and (row["line_run_id"] is None or row["drain_review_reason"] == "LINE_STOPPED"):
            await self.update(
                session, "workline_state", line_run_id=str(uuid7()), drain_review_reason=None
            )
            row = await self._one(session, table, lock=True)
        return row

    async def finish_line_if_idle(self, session):
        state_table = self.tables["workline_state"]
        state = await self._one(session, state_table)
        if not state or state["line_run_id"] is None or state["drain_phase"] != "IDLE":
            return
        for key in ("bin_passage", "bin_return", "face_work"):
            table = self.tables[key]
            query = select(table.c.id).where(
                *self.scoped(table),
                table.c.closed_at.is_(None),
                table.c.scope_id == state["line_run_id"],
            )
            if key == "bin_passage":
                query = query.where(table.c.scope_kind == "LINE")
            elif key == "face_work":
                query = query.where(table.c.purpose == "DRAIN")
            if await session.scalar(query.limit(1)) is not None:
                return
        if await self.rack_dispatch.jobs(
            session, work_line_id=self.work_line_id, source_kinds=("ROLLER_DRAIN",), pending=True
        ):
            return
        await self.update(session, "workline_state", line_run_id=None)

    async def apply_supply_change(self, session, change: SupplyChange):
        table = self.tables["face_work"]
        for source in change.added:
            await self._register(
                session,
                table,
                dict(
                    id=str(uuid7()),
                    **self.scope,
                    scope_id=change.task_id,
                    purpose="SUPPLY",
                    plan_revision=source.plan_revision,
                    change_id=change.change_id,
                    rack_id=source.rack_id,
                    rack_face=source.rack_face,
                    demand_state="ACTIVE",
                    phase="WAIT_RACK",
                    feed_cursor=0,
                ),
                ("purpose", "scope_id", "plan_revision", "rack_id", "rack_face"),
                (),
            )
        for source in change.withdrawn:
            row = await self._one(
                session,
                table,
                table.c.purpose == "SUPPLY",
                table.c.scope_id == change.task_id,
                table.c.plan_revision == source.plan_revision,
                table.c.rack_id == source.rack_id,
                table.c.rack_face == source.rack_face,
            )
            if row and row["closed_at"] is None:
                await self.update(session, "face_work", row["id"], demand_state="WITHDRAWN")
                if not await self.supply_started(session, row):
                    await self.close(session, "face_work", row["id"], "WITHDRAWN")

    async def demand(self, session, demand_id):
        table = self.tables["face_work"]
        return await self._one(session, table, table.c.id == demand_id, table.c.purpose == "SUPPLY")

    async def supply_started(self, session, demand):
        if demand["feed_operation_id"] is not None:
            return True
        if demand["rack_job_id"] and demand["preparation_action_id"]:
            job = await self.rack_dispatch.progress(session, job_id=demand["rack_job_id"])
            return bool(
                job
                and any(
                    a.id == demand["preparation_action_id"] and a.kind in ("ENTER", "ROTATE")
                    for a in job.actions
                )
            )
        return False

    async def bind_supply(self, session, *, demand_id, cycle_id):
        demand = await self.demand(session, demand_id)
        job = await self.rack_dispatch.progress(session, job_id=cycle_id)
        if (
            not demand
            or not job
            or (
                job.origin.task_id,
                job.origin.rack_id,
                job.origin.work_line_id,
                job.origin.source_kind,
            )
            != (demand["scope_id"], demand["rack_id"], self.work_line_id, "ROLLER_ORDINARY")
            or demand["rack_job_id"] not in (None, cycle_id)
        ):
            raise ValueError("original supply job association mismatch")
        await self.update(session, "face_work", demand_id, rack_job_id=cycle_id)

    async def supply_progress(self, session, task_id):
        table = self.tables["face_work"]
        rows = (
            await session.execute(
                select(table)
                .where(
                    *self.scoped(table), table.c.purpose == "SUPPLY", table.c.scope_id == task_id
                )
                .order_by(table.c.id)
            )
        ).mappings()
        result = []
        for face in rows:
            complete = face["close_reason"] == "COMPLETED"
            # Feed completion is independent of the current return window.
            if not complete and face["feed_message_id"] is not None:
                from .feed_flow import read_feed_manifest

                manifest = await read_feed_manifest(self.rack_dispatch.capabilities, session, face)
                complete = manifest is not None and (
                    manifest["result"] == "RACK_FACE_DONE"
                    or face["feed_cursor"] == len(manifest.get("bins", ()))
                )
            ids = tuple(
                str(face[k])
                for k in (
                    "id",
                    "rack_job_id",
                    "preparation_action_id",
                    "feed_operation_id",
                    "feed_message_id",
                    "step_id",
                    "current_client_request_id",
                    "current_transport_task_id",
                )
                if face[k] is not None
            )
            result.append(
                SupplyProgress(
                    task_id,
                    SourceFaceRef(face["plan_revision"], face["rack_id"], face["rack_face"]),
                    complete,
                    face["closed_at"] is None
                    and (
                        face["demand_state"] == "ACTIVE" or await self.supply_started(session, face)
                    ),
                    ids,
                )
            )
        return tuple(result)

    async def return_queue(self, session):
        table = self.tables["bin_return"]
        return (
            (
                await session.execute(
                    select(table)
                    .where(
                        *self.scoped(table),
                        table.c.closed_at.is_(None),
                        table.c.first_scan4_event_id.is_not(None),
                        table.c.is_current.is_(True),
                    )
                    .order_by(table.c.first_scan4_received_at, table.c.fifo_visit_key)
                )
            )
            .mappings()
            .all()
        )
